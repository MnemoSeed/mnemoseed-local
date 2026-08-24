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
counts (delta, cache prefix, overflow, provider-reported output) — T3b removed
the USD price model, the report is pure token bookkeeping.
Pure arithmetic, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from mnemoseed_local.dream.prompts import (
    PROMPT_VERSION,
    build_cache_prefix,
    ordered_chunks,
    render_chunk_block,
    render_chunk_blocks,
)
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.llm.types import Usage

if TYPE_CHECKING:
    from mnemoseed_local.config import Config

# The no-arg packer resolves its budget from the pending backlog (FR-2.5):
# budget = clamp(backlog tokens, 5k, 32k) — measured before every dream, no
# feedback loop, no persisted state (design/02 section 6). DEFAULT_DELTA_BUDGET
# _TOKENS survives as the legacy fixed default for callers that pass an explicit
# budget (the regression fence: explicit > dynamic resolution).
DEFAULT_DELTA_BUDGET_TOKENS = 10000
DELTA_BUDGET_FLOOR_TOKENS = 5000
DELTA_BUDGET_CEILING_TOKENS = 32000


def resolve_delta_budget(backlog_tokens: int, *, ceiling_tokens: int | None = None) -> int:
    """FR-2.5: clamp the pending backlog into the 5k..32k delta budget band.

    The floor keeps micro-backlogs above the pathological empty-delta edge; the
    ceiling bounds the single-dream cloud cost to ~$0.0045 at short-increment
    pricing (NFR-2.2). Pure local arithmetic over a directly-observable backlog.

    ``ceiling_tokens`` (T3a) overrides the module constant — the dynamic
    ceiling becomes configurable while the constant stays the default value
    source for the dream.delta_budget_ceiling_tokens config key.
    """
    ceiling = DELTA_BUDGET_CEILING_TOKENS if ceiling_tokens is None else ceiling_tokens
    return max(DELTA_BUDGET_FLOOR_TOKENS, min(ceiling, backlog_tokens))


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
    """Per-dream token telemetry (NFR-2.2 substrate; surfaced by T6/console)."""

    delta_tokens: int
    prefix_tokens: int
    overflow_count: int
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

    T3a: a bound ``config`` replaces the clamp's ceiling with the live
    ``dream.delta_budget_ceiling_tokens`` key, hot-applied to the next pack.
    """

    def __init__(
        self,
        *,
        budget_tokens: int | None = None,
        graph_digest: GraphDigest | None = None,
        config: Config | None = None,
    ) -> None:
        # None means "dynamic FR-2.5 resolution at pack time"; an explicit int
        # keeps the legacy fixed-budget contract of every historic caller.
        self._budget = budget_tokens
        self._digest = graph_digest if graph_digest is not None else NullGraphDigest()
        self._config = config

    def _ceiling(self) -> int:
        """The dynamic clamp's ceiling (T3a): the live config key when bound,
        the module constant otherwise (the constant remains the value source
        for the key's default)."""
        if self._config is None:
            return DELTA_BUDGET_CEILING_TOKENS
        return self._config.dream.delta_budget_ceiling_tokens

    def _resolve_budget(self, snapshot: Snapshot) -> int:
        """Explicit budget wins; otherwise measure the backlog and clamp it."""
        if self._budget is not None:
            return self._budget
        backlog = estimate_tokens(render_chunk_blocks(snapshot.chunks))
        return resolve_delta_budget(backlog, ceiling_tokens=self._ceiling())

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
        """Per-dream token telemetry for a packed request (NFR-2.2 substrate)."""
        return DeltaReport(
            delta_tokens=request.delta_tokens,
            prefix_tokens=request.prefix_tokens,
            overflow_count=len(request.overflow_chunk_ids),
            budget_tokens=request.budget_tokens,
        )

    def plan_batches(self, snapshot: Snapshot, *, batch_max_tokens: int) -> list[DeltaRequest]:
        """Slice the snapshot into whole-chunk batches for batched reflection (#99).

        Greedy fill in ``ordered_chunks`` order: a chunk joins the current
        batch while the running token estimate fits the EFFECTIVE cap
        (``min(batch_max_tokens, the packer's ceiling)`` — ``pack`` binds at
        the ceiling, so a larger configured cap would be silently disrespected),
        otherwise a new batch starts. A single chunk larger than the effective
        cap still gets its own batch (never split mid-text — same contract as
        ``pack``). Every chunk lands in exactly one batch; NOTE that a chunk
        whose block alone exceeds the packer's BUDGET still clips inside
        ``pack`` (empty delta, empty packed ids) exactly like the legacy path —
        the seat owes such batches the same D1 defer treatment. Deterministic
        and pure.

        Raises ValueError for a non-positive cap (0/None means "batching
        disabled" at the caller and must be checked there).
        """
        if batch_max_tokens <= 0:
            raise ValueError("batch_max_tokens must be positive; disable batching with None instead")
        effective_cap = min(batch_max_tokens, self._ceiling())
        batches: list[list[SnapshotChunk]] = []
        current: list[SnapshotChunk] = []
        acc_text = ""
        for chunk in ordered_chunks(snapshot.chunks):
            block = render_chunk_block(chunk)
            candidate = acc_text + block
            if current and estimate_tokens(candidate) > effective_cap:
                batches.append(current)
                current = []
                acc_text = ""
                candidate = block
            current.append(chunk)
            acc_text = candidate
        if current:
            batches.append(current)
        return [self.pack(replace(snapshot, chunks=tuple(batch))) for batch in batches]
