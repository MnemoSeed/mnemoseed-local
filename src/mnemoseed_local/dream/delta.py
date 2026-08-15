"""Delta packing and prompt-cache partition (PRD-02 T5; FR-2.5, NFR-2.2).

The cost-deadlock layer (design/02 section 6): a cloud dream call sends only
the per-dream increment (the delta), never the whole snapshot. Chunks are
packed whole (never split mid-text) in deterministic order until the delta
token budget (a dynamic budget by default, FR-2.5: the backlog itself clamped
to the 5k..32k band; an explicit budget still binds); chunks that do not fit
are reported as overflow so a later dream can pick them up — overflow is
reported, never an error. The
stable part of the request (system instruction + user header + optional graph
digest) is the cache-resident prefix a provider prompt cache keys on and never
counts against the delta budget.

Token accounting is a local, deterministic estimator: one token per CJK char
plus ceil of the remaining chars over four. No network, no tokenizer model — a
real BPE tokenizer would need a downloaded tokenizer file, which violates the
zero-network constraint, so this estimator stands in until T6 replaces it with
provider-reported usage. Accuracy is a documented approximation: within roughly
the 3-5 chars/token envelope a BPE tokenizer averages for English prose, and
exact for CJK text (one token per CJK char for most modern tokenizers).
Deterministic across runs and platforms (code point counts, never code units).
Worst-case bias is on entropy-dense ASCII (hex, base64, URL-dense paths), where
a byte-level BPE can emit up to ~4x the estimator's count (~0.25x the true token
count); T6 must calibrate the budget against provider-reported usage before
relying on the 32k cap as a hard cost bound.

Cost telemetry is the NFR-2.2 substrate: DeltaReport carries per-dream token
counts and an estimated USD cost from a configurable per-role price table
(input / cache-read / output USD per million tokens). Defaults follow design/02
section 6's short-increment track: DeepSeek V4 Flash via Fireworks at
$0.14/M input, $0.028/M cache read, $0.28/M output (verified against the
provider catalog).
Pure arithmetic, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from mnemoseed_local.dream.prompts import (
    PROMPT_VERSION,
    build_cache_prefix,
    ordered_chunks,
    render_chunk_block,
    render_chunk_blocks,
)
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.llm.types import Usage

# The no-arg packer resolves its budget from the pending backlog (FR-2.5):
# budget = clamp(backlog tokens, 5k, 32k) — measured before every dream, no
# feedback loop, no persisted state (design/02 section 6). DEFAULT_DELTA_BUDGET
# _TOKENS survives as the legacy fixed default for callers that pass an explicit
# budget (the regression fence: explicit > dynamic resolution).
DEFAULT_DELTA_BUDGET_TOKENS = 10000
DELTA_BUDGET_FLOOR_TOKENS = 5000
DELTA_BUDGET_CEILING_TOKENS = 32000


def resolve_delta_budget(backlog_tokens: int) -> int:
    """FR-2.5: clamp the pending backlog into the 5k..32k delta budget band.

    The floor keeps micro-backlogs above the pathological empty-delta edge; the
    ceiling bounds the single-dream cloud cost to ~$0.0045 at short-increment
    pricing (NFR-2.2). Pure local arithmetic over a directly-observable backlog.
    """
    return max(DELTA_BUDGET_FLOOR_TOKENS, min(DELTA_BUDGET_CEILING_TOKENS, backlog_tokens))


_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul Syllables
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Deterministic local token estimate for one text.

    Formula: one token per CJK char plus ceil of the remaining chars over four
    (a documented chars/4 token approximation for English prose, exact for CJK).
    """
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


# ---------------------------------------------------------------- cost model


@dataclass(frozen=True)
class PriceTable:
    """Per-role cloud pricing for the delta cost model (USD per million tokens).

    Defaults follow design/02 section 6's short-increment track: DeepSeek V4
    Flash via Fireworks — $0.14/M input, $0.028/M cache read, $0.28/M output.
    """

    input_usd_per_m: float = 0.14
    cache_read_usd_per_m: float = 0.028
    output_usd_per_m: float = 0.28


def estimate_cost_usd(
    *,
    delta_tokens: int,
    prefix_tokens: int,
    price: PriceTable,
    output_tokens: int = 0,
) -> float:
    """Pure per-dream cloud cost estimate (NFR-2.2 substrate). The delta is
    billed as fresh input, the cache-resident prefix at the cache-read rate, and
    output at the configured output rate (zero before the call completes)."""
    return (
        delta_tokens * price.input_usd_per_m
        + prefix_tokens * price.cache_read_usd_per_m
        + output_tokens * price.output_usd_per_m
    ) / 1_000_000.0


# ---------------------------------------------------------------- graph digest seam


class GraphDigest(Protocol):
    """Optional provider of a profile-stable graph digest for the cache prefix.

    The delta layer never reads the graph itself; real graph-digest assembly is
    a later task. The digest is read once at pack time and only affects the
    byte-stable prefix (a stable digest keeps the prefix cache-resident).
    """

    def digest(self, profile_id: str) -> str: ...


class NullGraphDigest:
    """Null default: no digest section, so the cache prefix stays a fixed
    constant across dreams of a profile."""

    def digest(self, profile_id: str) -> str:
        del profile_id
        return ""


# ---------------------------------------------------------------- request + report


@dataclass(frozen=True)
class DeltaRequest:
    """One packed cloud request split into two explicit segments: the stable
    ``cache_prefix`` (system instruction + header + optional graph digest) and
    the per-dream ``delta``. Overflow chunk ids are part of the result so a
    later dream can pick them up — never silently dropped."""

    version: str
    profile_id: str
    cache_prefix: str
    delta: str
    packed_chunk_ids: tuple[str, ...]
    overflow_chunk_ids: tuple[str, ...]
    delta_tokens: int
    prefix_tokens: int
    budget_tokens: int = 0  # FR-2.5: the budget this pack ran under; 0 for hand-built requests


@dataclass(frozen=True)
class DeltaReport:
    """Per-dream cost telemetry (NFR-2.2 substrate; surfaced by T6/console)."""

    delta_tokens: int
    prefix_tokens: int
    overflow_count: int
    estimated_cost_usd: float
    budget_tokens: int = 0  # FR-2.5: the resolved budget that produced this report
    provider_usage: Usage | None = None  # T6: provider-reported tokens, when the driver reports them

    def with_provider_usage(self, usage: Usage | None) -> DeltaReport:
        """Return a copy carrying the provider-reported usage (additive: all
        estimated fields untouched; pass None to carry no usage)."""
        return replace(self, provider_usage=usage)


# ---------------------------------------------------------------- packer


class DeltaPacker:
    """Pack a snapshot's chunks into a delta under a token budget.

    Chunks are packed whole and in deterministic order (the same order the full
    prompt render uses). The cache prefix never counts against the delta budget.
    Under no budget pressure the packed delta IS the full snapshot render, so a
    default packer preserves the pre-delta reflect behavior.

    FR-2.5: the no-arg packer resolves its budget dynamically from the pending
    backlog (``budget = clamp(backlog, 5k, 32k)``) before packing; an explicit
    ``budget_tokens`` still binds exactly as before (regression fence).
    """

    def __init__(
        self,
        *,
        budget_tokens: int | None = None,
        price: PriceTable | None = None,
        graph_digest: GraphDigest | None = None,
    ) -> None:
        # None means "dynamic FR-2.5 resolution at pack time"; an explicit int
        # keeps the legacy fixed-budget contract of every historic caller.
        self._budget = budget_tokens
        self._price = price if price is not None else PriceTable()
        self._digest = graph_digest if graph_digest is not None else NullGraphDigest()

    @property
    def price(self) -> PriceTable:
        """The active price table (for report / telemetry consumers)."""
        return self._price

    def _resolve_budget(self, snapshot: Snapshot) -> int:
        """Explicit budget wins; otherwise measure the backlog and clamp it."""
        if self._budget is not None:
            return self._budget
        backlog = estimate_tokens(render_chunk_blocks(snapshot.chunks))
        return resolve_delta_budget(backlog)

    def pack(self, snapshot: Snapshot) -> DeltaRequest:
        """Assemble the delta request: pack whole chunks up to the budget,
        report the rest as overflow (never dropped, never split mid-text)."""
        budget = self._resolve_budget(snapshot)
        prefix = build_cache_prefix(self._digest.digest(snapshot.profile_id))
        packed: list[tuple[SnapshotChunk, str]] = []
        overflow: list[SnapshotChunk] = []
        acc_text = ""
        acc_tokens = 0
        for chunk in ordered_chunks(snapshot.chunks):
            block = render_chunk_block(chunk)
            candidate = acc_text + block
            candidate_tokens = estimate_tokens(candidate)
            if candidate_tokens <= budget:
                packed.append((chunk, block))
                acc_text = candidate
                acc_tokens = candidate_tokens
            else:
                overflow.append(chunk)
        return DeltaRequest(
            version=PROMPT_VERSION,
            profile_id=snapshot.profile_id,
            cache_prefix=prefix,
            delta=acc_text,
            packed_chunk_ids=tuple(c.chunk_id for c, _ in packed),
            overflow_chunk_ids=tuple(c.chunk_id for c in overflow),
            delta_tokens=acc_tokens,
            prefix_tokens=estimate_tokens(prefix),
            budget_tokens=budget,
        )

    def report(self, request: DeltaRequest) -> DeltaReport:
        """Per-dream cost telemetry for a packed request (NFR-2.2 substrate)."""
        return DeltaReport(
            delta_tokens=request.delta_tokens,
            prefix_tokens=request.prefix_tokens,
            overflow_count=len(request.overflow_chunk_ids),
            estimated_cost_usd=estimate_cost_usd(
                delta_tokens=request.delta_tokens,
                prefix_tokens=request.prefix_tokens,
                price=self._price,
            ),
            budget_tokens=request.budget_tokens,
        )
