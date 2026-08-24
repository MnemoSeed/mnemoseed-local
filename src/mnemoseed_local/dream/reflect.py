"""Reflection orchestrator + de-biasing seam (PRD-02 T3; FR-2.2 / FR-2.12, §7).

The orchestrator consumes an adopted Snapshot (from the trigger's DREAMING
state), renders the versioned de-biasing prompt, drives the narrow ReflectLLM
seam, folds duplicate mentions (AC-3), persists the REFLECT_DONE marker into
the snapshot file BEFORE reporting completion (crash-safe, NFR-2.3), and on
model failure degrades with exponential-backoff retry x3 into a typed outcome:
never a raise, never a block on ingestion (design/02 section 7).

The output contract is the T4 seam: a frozen ReflectionResult carrying
deduplicated triples, each with per-triple provenance (tiers, chunk ids, turn
range), confidence, and a route (core | isolated | salvage) per dual-track
rules. The deterministic StubReflectLLM implements the same de-biasing contract
offline, so the whole pipeline is exercisable without any network (the M1
manual-first phase and tests). No graph writes happen here (T4 owns them).

The reflect call goes through a DeltaPacker (T5, FR-2.5): the stable cache
prefix goes to the system segment, the per-dream delta goes to the user
segment, and overflow chunks are deferred (reported, never an error). The ids
the delta packed (``consumed_chunk_ids``) ride on the journaled result as the
safe-clear allow-list, so a committed dream purges exactly the rows the model
saw and overflow rows survive for a later dream. Per-dream token counts ride
out on ReflectOutcome.report (NFR-2.2 substrate).

An optional monthly token ledger (T5b) records what a completed dream consumed
(delta + provider-reported output tokens) into the current UTC month, before
the marker persist (T3b: pure token bookkeeping — it never gates the reflect
boundary).

Negation rule (g2, engine invariant): each mention carries a polarity
("positive" / "negative") derived from negation markers on the matched span
(e.g. "I never use vim"). AC-3 folding groups mentions by (subject, predicate,
object) AND polarity: contradictory-polarity mentions of the same key are NOT
folded into one false-confident reinforced triple — both are dropped and the
key is reported on ``ReflectionResult.conflicts``. Same-polarity mentions still
fold normally. The result is journaled inside the snapshot file (as the opaque
``Snapshot.reflect_result`` payload) together with the REFLECT_DONE marker, so
a crash after reflect resumes at the merge boundary without re-running reflect.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from mnemoseed_local.config import CONFIG_DIR
from mnemoseed_local.dream.delta import DeltaPacker, DeltaReport, DeltaRequest
from mnemoseed_local.dream.ledger import TokenLedger
from mnemoseed_local.dream.prompts import (
    ChunkBlock,
    ordered_chunks,
    origin_of,
    parse_chunk_blocks,
)
from mnemoseed_local.dream.snapshot import (
    Snapshot,
    SnapshotPhase,
    load_snapshot_file,
    write_snapshot_file,
)
from mnemoseed_local.llm.types import ChatResult, LLMUnavailable, Usage
from mnemoseed_local.schema.stamp import CognitiveTier
from mnemoseed_local.storage.ports import TurnRange

logger = logging.getLogger("mnemoseed_local.dream.reflect")

#: Batched reflection (#99): the per-dream bound on LLM calls when batching is
#: enabled. The total packed delta across the batches stays within the same
#: delta-budget ceiling a single legacy call respected, so the dream's cost
#: envelope is unchanged — it is only split into model-sized pieces.
_MAX_REFLECT_BATCHES_PER_DREAM = 4


# ---------------------------------------------------------------- output contract


class Route(StrEnum):
    """Dual-track routing of one reflected triple (design/02 section 4)."""

    CORE = "core"
    ISOLATED = "isolated"
    SALVAGE = "salvage"


@dataclass(frozen=True)
class ReflectedTriple:
    """One deduplicated entity triple plus its provenance and route."""

    subject: str
    predicate: str
    object: str
    tiers: tuple[CognitiveTier, ...]  # source tier(s) across the evidence chunks
    chunk_ids: tuple[str, ...]  # provenance refs pinning the exact chunks
    turn_range: TurnRange  # the snapshot scope the evidence came from
    confidence: float  # 0..1, reinforced by dedup folding (AC-3)
    route: Route  # core | isolated | salvage; tier-3 provenance never yields core
    preference: bool = False  # preference-type extraction (FR-2.12 boundary)
    polarity: str = "positive"  # "positive" | "negative" (negation guard, g2)
    model_id: str | None = None  # B5 vote: the generating seat's model (triple-level attribution)
    vote_disagreement: bool = False  # B5 vote: preserved from a disputed predicate (needs_reconcile)


@dataclass(frozen=True)
class ReflectionResult:
    """The T4 seam: everything the splitter needs to route and write back."""

    snapshot_id: str
    profile_id: str
    turn_range: TurnRange
    prompt_version: str
    triples: tuple[ReflectedTriple, ...]
    conflicts: tuple[tuple[str, str, str], ...] = ()  # dropped contradictory-polarity keys
    overflow_chunk_ids: tuple[str, ...] = ()  # T5: chunks deferred beyond the delta budget.
    consumed_chunk_ids: tuple[str, ...] = ()  # T5: delta ids the model saw; the safe-clear allow-list.


@dataclass(frozen=True)
class ReflectOutcome:
    """Typed result of one reflect pass. ``ok`` is always set."""

    ok: bool
    result: ReflectionResult | None = None
    error: str | None = None
    skipped: bool = False  # marker gate: reflect had already completed
    report: DeltaReport | None = None  # T5 cost telemetry (NFR-2.2 substrate)
    llm_unavailable: bool = False  # T6: sticky — set once any attempt raised LLMUnavailable, even if
    # the final failure was a non-provider error; False when a retry eventually succeeded
    batched: bool = False  # #99: the batched seat ran, so every covered chunk was fully handed to the
    # model under budget — an empty extraction verdict on the covered range is a genuine "nothing
    # durable here", not truncation evidence, and merge may commit it (clearing exactly the covered ids)


# ---------------------------------------------------------------- the LLM seam


class ReflectLLM(Protocol):
    """Narrow chat-completion seam. T6's full DreamLLM port can satisfy this
    single method; the deterministic StubReflectLLM satisfies it offline."""

    def chat(self, *, system: str, user: str) -> str: ...


class ChatLLM(Protocol):
    """The T3 seam widened by T6: a model may return plain text (T3 stub) or a
    full ChatResult (text + provider-reported usage). Both satisfy this."""

    def chat(self, *, system: str, user: str) -> str | ChatResult: ...


class Verifier(Protocol):
    """Structural seam for the ensemble verify pass (B1, design/01 decision 1).

    Implemented by ``dream.verify.TripleVerifier``; declared here so the
    orchestrator depends on the narrow shape only (verify.py imports reflect.py
    helpers — importing it back would be a cycle). The verifier judges A's
    folded core triples and returns the result merge should see; on ANY
    failure it returns A's original result (fallback + audit), so this call
    never raises into the reflect boundary."""

    def verify(self, snapshot: Snapshot, result: ReflectionResult) -> ReflectionResult: ...


def _split_response(response: str | ChatResult) -> tuple[str, Usage | None]:
    """Normalize a ChatLLM response: (text, provider usage). A plain str is
    treated as text with no provider-reported usage."""
    if isinstance(response, ChatResult):
        return response.text, response.usage
    return response, None


def _loads_json_array(text: str) -> list[Any]:
    """Tolerant reflect-output parse (D4 resilience, service-root stability).

    Strict parse first; on failure, fall back to the widest [..] bracket span
    in the text — small/quantized models reliably wrap their answers in
    markdown fences (```json) or preface them with chatter, and a strict
    json.loads otherwise degrades an answer the model actually produced.
    Anything that parses to a non-list is still an error; text with no
    parseable array is an error (typed failure drives the retry lane).
    """
    try:
        payload = json.loads(text)
        if isinstance(payload, list):
            return payload
        raise ValueError("reflect output is not a JSON array")
    except (json.JSONDecodeError, ValueError) as strict_error:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end > start:
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, list):
                return payload
        raise strict_error


def _with_provider_usage(report: DeltaReport, usage: Usage | None) -> DeltaReport:
    """Attach provider-reported usage to a report; identity (unchanged) when
    the provider reported none (T6 additive seam, NFR-2.2)."""
    if usage is None:
        return report
    return report.with_provider_usage(usage)


def _aggregate_reports(
    reports: list[DeltaReport],
    usages: list[Usage | None],
    *,
    overflow_count: int,
) -> DeltaReport:
    """Fold per-batch telemetry into one honest per-dream report (#99).

    ``delta_tokens`` sums (every batch's call happened); ``prefix_tokens``
    takes the max (the cache-resident prefix is the same stable text every
    batch, not a per-batch cost); provider usage sums the reported legs.
    """
    completion = sum(u.completion_tokens or 0 for u in usages if u is not None)
    prompt = sum(u.prompt_tokens or 0 for u in usages if u is not None)
    usage = (
        Usage(prompt_tokens=prompt or None, completion_tokens=completion or None)
        if prompt or completion
        else None
    )
    return DeltaReport(
        delta_tokens=sum(r.delta_tokens for r in reports),
        prefix_tokens=max((r.prefix_tokens for r in reports), default=0),
        overflow_count=overflow_count,
        budget_tokens=reports[-1].budget_tokens if reports else 0,
        provider_usage=usage,
    )


# ---------------------------------------------------------------- field-level coercion (D4)
#
# Live finding (2026-08 model matrix): small/local models emit schema-sloppy
# FIELDS in whole-attempt modes — qwen3.5:9b renders tiers as "tier_1" /
# "tier-1" strings, gemma4:e4b renders confidence as "high". Strict parsing
# silently dropped EVERY such mention (a parseable array of all-skipped items
# returned an empty result with ok=True). Coercion is bounded: digits and a
# fixed word map only — anything else is still garbage and drops the mention.

_TIER_DIGIT_RE = re.compile(r"[1-3]")

#: Self-assessed confidence words observed live, mapped to the clamped scale.
_CONFIDENCE_WORDS: dict[str, float] = {
    "high": 0.85,
    "medium": 0.7,
    "mid": 0.7,
    "low": 0.5,
}


def _coerce_tier(value: Any) -> CognitiveTier:
    """Coerce one tier field: an int (strict path), a digit string ("2"), or a
    digit-bearing word form ("tier_1" / "tier-1" / "Tier 2"). Values with no
    1-3 digit are garbage (ValueError drops the mention upstream)."""
    if isinstance(value, bool):
        raise ValueError(f"tier must not be a bool: {value!r}")
    if isinstance(value, int):
        return CognitiveTier(value)
    text = str(value).strip()
    try:
        return CognitiveTier(int(text))
    except ValueError:
        pass
    match = _TIER_DIGIT_RE.search(text)
    if match:
        return CognitiveTier(int(match.group(0)))
    raise ValueError(f"tier is not coercible: {value!r}")


def _coerce_confidence(value: Any) -> float:
    """Coerce one confidence field: a number (strict path), a numeric string
    ("0.6"), or a self-assessed word in the fixed map ("high"/"medium"/"low").
    Anything else is garbage (ValueError drops the mention upstream)."""
    if isinstance(value, bool):
        raise ValueError(f"confidence must not be a bool: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().casefold()
    try:
        return float(text)
    except ValueError:
        pass
    if text in _CONFIDENCE_WORDS:
        return _CONFIDENCE_WORDS[text]
    raise ValueError(f"confidence is not coercible: {value!r}")


def _list_field(raw: Any) -> list[Any]:
    """A repeated field the model sometimes renders singular ("tier_1" where a
    list is asked): normalize to a list so a bare string is never iterated by
    character."""
    if isinstance(raw, list):
        return raw
    return [raw]


# Stripped personal color: emotional/flavor intensifiers, sentence-final tone
# particles, and honorific/role-play mannerisms an anima renders. These are
# never stored with a fact (design/02 section 5).
STRIP_TOKENS: frozenset[str] = frozenset(
    {
        "really",
        "very",
        "so",
        "super",
        "absolutely",
        "extremely",
        "totally",
        "literally",
        "honestly",
        "definitely",
        "just",
        "超级",
        "非常",
        "特别",
        "极其",
        "简直",
        "真的",
        "啦",
        "呀",
        "呢",
        "嘛",
        "哈",
        "喔",
        "哦",
        "呗",
        "喵",
        "嘻嘻",
        "哈哈",
        "嘿嘿",
        "陛下",
        "殿下",
        "主人",
        "大人",
        "master",
        "亲爱",
        "亲爱的",
        "人家",
        "奴家",
        "本座",
        "咱家",
        "超",
    }
)

_STRIP_RE = re.compile(
    "|".join(re.escape(token) for token in sorted(STRIP_TOKENS, key=len, reverse=True)),
    re.IGNORECASE,
)
_NON_WORD_RE = re.compile(r"[^\w\s一-鿿-]")

# Negation markers used by the stub's polarity judgment (g2). v1 covers the
# explicit English "never" plus strong Chinese negation adverbs; pattern gaps
# (e.g. "不喝" embedded inside an object without a strong adverb) default to
# "positive" and are documented as v1 stub scope, not engine truth.
_NEGATION_RE = re.compile(r"\bnever\b|从不|不再|再也不")

_PREF_EN = re.compile(
    r"\b(?:i|we)\b[^.!?\n]{0,25}?\b(?:like|love|prefer|enjoy|value|favour|favor)\b"
    r"(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)
_PREF_ZH = re.compile(
    r"我[^。！？\n]{0,12}?(?:喜欢|爱|偏爱|偏好|欣赏|倾向于|钟意|认可|推崇)(?P<obj>[^。！？\n]{1,30})"
)
_HABIT_EN = re.compile(
    r"\b(?:i|we)\b[^.!?\n]{0,25}?\b(?:always|never|usually|typically|habitually)\b"
    r"(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)
_HABIT_ZH = re.compile(r"我[^。！？\n]{0,10}?(?:每次|总是|通常|习惯)(?:都)?(?P<obj>[^。！？\n]{1,30})")
_DECIDE_EN = re.compile(
    r"\b(?:i|we)\b[^.!?\n]{0,25}?\b(?:decided|switched to|committed to)\b"
    r"(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)
_DECIDE_ZH = re.compile(r"我(?:决定|打算|以后都|从今往后)(?P<obj>[^。！？\n]{1,30})")
_STANCE_EN = re.compile(
    r"\b(?:i|we)\b[^.!?\n]{0,25}?\b(?:believe|think|support|oppose)\b(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)
_STANCE_ZH = re.compile(r"我(?:认为|觉得|相信|坚持|反对|支持)(?P<obj>[^。！？\n]{1,30})")
_ASSERT_PATTERN = re.compile(
    r"\b(?:definitely|absolutely|certainly|surely|no doubt)\b(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)

_CANONICAL_PREDICATE: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (_PREF_EN, "prefers", True),
    (_PREF_ZH, "prefers", True),
    (_HABIT_EN, "has_habit", False),
    (_HABIT_ZH, "has_habit", False),
    (_DECIDE_EN, "decided", False),
    (_DECIDE_ZH, "decided", False),
    (_STANCE_EN, "believes", False),
    (_STANCE_ZH, "believes", False),
)

_BASE_CONFIDENCE: dict[str, float] = {
    "prefers": 0.7,
    "has_habit": 0.65,
    "decided": 0.7,
    "believes": 0.6,
    "asserts": 0.5,
}

_ROUTE_ORDER: dict[Route, int] = {Route.CORE: 1, Route.ISOLATED: 2, Route.SALVAGE: 3}


class StubReflectLLM:
    """Deterministic, offline ReflectLLM for tests and the M1 manual-first phase.

    Implements the same de-biasing contract the prompt demands: rule-based
    triple extraction over the prompt's chunk blocks, personal color stripped
    from every component, speaking style never emitted, preference-type
    extractions restricted to user-originated chunks (FR-2.12), and tier-3
    evidence routed to salvage (durable) or isolated (noise claim), never core.
    """

    def chat(self, *, system: str, user: str) -> str:
        del system
        mentions: list[dict[str, Any]] = []
        for block in parse_chunk_blocks(user):
            mentions.extend(self._extract_block(block))
        return json.dumps(mentions, ensure_ascii=False)

    def _extract_block(self, block: ChunkBlock) -> list[dict[str, Any]]:
        tier = CognitiveTier(block.tier)
        mentions: list[dict[str, Any]] = []
        for pattern, predicate, is_preference in _CANONICAL_PREDICATE:
            for match in pattern.finditer(block.text):
                obj = _clean_components(match.group("obj"))
                if not obj:
                    continue
                polarity = "negative" if _NEGATION_RE.search(match.group(0)) else "positive"
                mentions.append(
                    {
                        "subject": "user",
                        "predicate": predicate,
                        "object": obj,
                        "tiers": [int(tier)],
                        "chunk_ids": [block.chunk_id],
                        "confidence": _BASE_CONFIDENCE[predicate],
                        "route": _route_for(tier, predicate),
                        "preference": is_preference,
                        "polarity": polarity,
                    }
                )
        # tier-3 low-value noise: confident-but-unverifiable claims from a
        # non-user source, routed to the physical isolation track (AC-2 audit)
        if tier is CognitiveTier.TIER_3 and block.origin != "user":
            for match in _ASSERT_PATTERN.finditer(block.text):
                obj = _clean_components(match.group("obj"))
                if not obj:
                    continue
                mentions.append(
                    {
                        "subject": "assistant",
                        "predicate": "asserts",
                        "object": obj,
                        "tiers": [int(tier)],
                        "chunk_ids": [block.chunk_id],
                        "confidence": _BASE_CONFIDENCE["asserts"],
                        "route": Route.ISOLATED.value,
                        "preference": False,
                    }
                )
        if block.origin != "user":
            mentions = [m for m in mentions if not m["preference"]]
        return mentions


def _clean_components(raw: str) -> str:
    text = _STRIP_RE.sub(" ", raw)
    text = _NON_WORD_RE.sub(" ", text)
    return " ".join(text.split())


def _route_for(tier: CognitiveTier, predicate: str) -> str:
    if tier is CognitiveTier.TIER_3:
        return Route.ISOLATED.value if predicate == "asserts" else Route.SALVAGE.value
    return Route.CORE.value


# ---------------------------------------------------------------- orchestrator


class ReflectOrchestrator:
    """Runs the reflection pipeline over one adopted snapshot and reports
    completion through the ``on_done`` seam (wired to trigger.on_reflect_complete).

    Async-friendly in shape: it is O(chunk text) per call and never performs
    blocking I/O beyond the injectable LLM seam, so the daemon runs it on a
    background task — nothing touches the /ingest hot path.
    """

    def __init__(
        self,
        *,
        llm: ChatLLM,
        directory: Path | None = None,
        on_done: Callable[[str], None] | None = None,
        on_unavailable: Callable[[str], None] | None = None,
        on_run_started: Callable[[str, str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        packer: DeltaPacker | None = None,
        ledger: TokenLedger | None = None,
        resolve_llm: Callable[[], ChatLLM] | None = None,
        verifier: Verifier | None = None,
        vote_llm: ChatLLM | None = None,
        resolve_vote_llm: Callable[[], ChatLLM] | None = None,
        batch_max_tokens: int | None = None,
    ) -> None:
        self._llm = llm
        # F2 hot-apply seam: when wired, every reflect pass materializes the
        # route AT RUN START (pinned for the run) instead of reusing the boot
        # instance, so a configwrite change lands on the NEXT dream run without
        # a daemon restart. ``llm`` stays the boot-time fallback.
        self._resolve_llm = resolve_llm
        # F2 model pinning seam: called with (run_id, model) once per run with
        # the RESOLVED instance's model, so the caller can record
        # dream_runs.model for the run. Never a raise into the pipeline.
        self._on_run_started = on_run_started
        self._directory = directory if directory is not None else CONFIG_DIR / "dreams"
        self._on_done = on_done
        self._on_unavailable = on_unavailable
        self._sleep = sleep
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._packer = packer if packer is not None else DeltaPacker()
        self._ledger = ledger
        # B1 ensemble verify seam (design/01 decision 1): when wired AND the
        # live config mode says verify, model B judges A's folded core triples
        # between assembly and the atomic journal write — FALL-SOFT by
        # contract, a verifier failure lands A's original result + audit.
        self._verifier = verifier
        # B5 vote seam: the B seat's own generation LLM (the same judging-seat
        # role, reused as a full generator). Falls back to A's boot instance
        # when not wired, so vote degrades to a same-seat pass rather than crash.
        self._vote_llm = vote_llm if vote_llm is not None else llm
        self._resolve_vote_llm = resolve_vote_llm
        # Batched reflection (#99): None (the default) keeps the legacy
        # single-pack path byte-identical; a positive cap slices oversized
        # backlogs into model-sized batches drained over successive dreams.
        self._batch_max_tokens = batch_max_tokens

    def reflect(self, snapshot: Snapshot) -> ReflectOutcome:
        """Run the single-model reflect pass (off / verify ensemble modes).

        The marker gate makes a completed reflect a no-op: a recovered snapshot
        that already wrote back never re-runs."""
        if SnapshotPhase.REFLECT_DONE.value in snapshot.phases:
            return ReflectOutcome(ok=True, result=None, skipped=True)
        llm = self._resolve_llm() if self._resolve_llm is not None else self._llm
        self._announce_run(snapshot, llm)
        outcome = self._run_seat(snapshot, llm)
        if not outcome.ok or outcome.result is None:
            return outcome
        result = outcome.result
        if self._verifier is not None:
            # B1: verify rides the SAME reflect run — the journaled payload
            # below is whatever the verifier returned (verified result, or A's
            # original on fallback), so merge / crash-resume see exactly one
            # consistent outcome and REFLECT_DONE marker semantics never change.
            result = self._verifier.verify(snapshot, result)
        return self._record_and_finalize(
            snapshot,
            result,
            outcome.report,
            outcome.llm_unavailable,
            phase=SnapshotPhase.REFLECT_DONE,
            seat=None,
        )

    def reflect_vote_a(self, snapshot: Snapshot) -> ReflectOutcome:
        """B5 vote: run seat A's full generation over the delta and journal its
        result under REFLECT_A_DONE. A recovered snapshot that already ran A is
        a no-op (only B / combine / merge remain)."""
        if SnapshotPhase.REFLECT_A_DONE.value in snapshot.phases:
            return ReflectOutcome(ok=True, result=None, skipped=True)
        llm = self._resolve_llm() if self._resolve_llm is not None else self._llm
        self._announce_run(snapshot, llm)
        outcome = self._run_seat(snapshot, llm)
        if not outcome.ok or outcome.result is None:
            return outcome
        return self._record_and_finalize(
            snapshot,
            outcome.result,
            outcome.report,
            outcome.llm_unavailable,
            phase=SnapshotPhase.REFLECT_A_DONE,
            seat="a",
        )

    def reflect_vote_b(self, snapshot: Snapshot) -> ReflectOutcome:
        """B5 vote: run seat B's full generation over the delta and journal its
        result under REFLECT_B_DONE. A recovered snapshot that already ran B is
        a no-op (only combine / merge remain)."""
        if SnapshotPhase.REFLECT_B_DONE.value in snapshot.phases:
            return ReflectOutcome(ok=True, result=None, skipped=True)
        llm = self._resolve_vote_llm() if self._resolve_vote_llm is not None else self._vote_llm
        self._announce_run(snapshot, llm)
        outcome = self._run_seat(snapshot, llm)
        if not outcome.ok or outcome.result is None:
            return outcome
        return self._record_and_finalize(
            snapshot,
            outcome.result,
            outcome.report,
            outcome.llm_unavailable,
            phase=SnapshotPhase.REFLECT_B_DONE,
            seat="b",
        )

    def combine(self, snapshot: Snapshot) -> ReflectOutcome:
        """B5 vote: fold seat A's and seat B's journaled results into the single
        merge-facing result (COMBINE_DONE + ``reflect_result``). Pure and
        deterministic; a no-op once combined."""
        if SnapshotPhase.COMBINE_DONE.value in snapshot.phases:
            return ReflectOutcome(ok=True, result=None, skipped=True)
        base = load_snapshot_file(self._directory / f"{snapshot.snapshot_id}.json") or snapshot
        votes = base.vote_results or {}
        a_payload = votes.get("a")
        b_payload = votes.get("b")
        if a_payload is None or b_payload is None:
            logger.warning(
                "combine for %s: missing vote seat payload (a=%s, b=%s); staying journaled",
                snapshot.profile_id,
                a_payload is not None,
                b_payload is not None,
            )
            return ReflectOutcome(
                ok=False,
                error="vote combine requires both seat payloads; snapshot stays journaled",
            )
        a_result = result_from_payload(a_payload)
        b_result = result_from_payload(b_payload)
        if a_result is None or b_result is None:
            logger.warning(
                "combine for %s: a vote seat payload is not recoverable; staying journaled",
                snapshot.profile_id,
            )
            return ReflectOutcome(
                ok=False,
                error="vote combine: a seat payload is not recoverable; snapshot stays journaled",
            )
        # Local import: combine.py imports this module's types, so importing it
        # at module scope would be a cycle.
        from mnemoseed_local.dream.combine import combine_results

        combined = combine_results(a_result, b_result)
        try:
            self._finalize_combined(snapshot, combined)
        except Exception as exc:  # noqa: BLE001 - marker before progress
            logger.warning(
                "combine done but COMBINE_DONE persist failed for %s: %s", snapshot.profile_id, exc
            )
            return ReflectOutcome(ok=False, error=f"persist failed: {exc}")
        return ReflectOutcome(ok=True, result=combined)

    def _announce_run(self, snapshot: Snapshot, llm: ChatLLM) -> None:
        """F2: record the run's resolved model once per run (best-effort)."""
        if self._on_run_started is None:
            return
        model = str(getattr(llm, "model", "") or "")
        if not model:
            return
        try:
            self._on_run_started(snapshot.snapshot_id, model)
        except Exception as exc:  # noqa: BLE001 - a recording failure never breaks the dream
            logger.warning("dream_runs model recording failed for %s: %s", snapshot.snapshot_id, exc)

    def _run_seat(self, snapshot: Snapshot, llm: ChatLLM) -> ReflectOutcome:
        """Run one full generation pass against a seat LLM (pack -> retry ->
        assemble). Returns a typed outcome: ok=True with ``result`` set on
        success, a degraded outcome (defer / retries exhausted) otherwise."""
        if self._batch_max_tokens is not None and self._batch_max_tokens > 0:
            batches = self._packer.plan_batches(snapshot, batch_max_tokens=self._batch_max_tokens)
            if len(batches) > 1:
                return self._run_batched_seat(snapshot, llm, batches)
        request = self._packer.pack(snapshot)
        report = self._packer.report(request)
        if not request.delta and request.overflow_chunk_ids:
            # D1 (FR-2.5, never-drop invariant): every chunk is over the delta
            # budget, so there is nothing worth a cloud call. Defer instead of
            # reflecting an empty delta: keep the snapshot journaled at the
            # reflect boundary so a later dream (larger budget / manual run) can
            # pick the overflow chunks up before any commit can purge them.
            logger.warning(
                "reflect deferred for %s: all %d chunks exceed the delta budget; "
                "snapshot retained at the reflect boundary",
                snapshot.profile_id,
                len(request.overflow_chunk_ids),
            )
            return ReflectOutcome(
                ok=False,
                result=None,
                error="all chunks exceed the delta budget; snapshot retained for a later dream",
                report=report,
            )
        result: ReflectionResult | None = None
        last_error = ""
        provider_usage: Usage | None = None
        unavailable = False
        model_id = str(getattr(llm, "model", "") or "")
        for attempt in range(self._max_retries + 1):
            try:
                response = llm.chat(system=request.cache_prefix, user=request.delta)
                text, provider_usage = _split_response(response)
                payload = _loads_json_array(text)
                if not isinstance(payload, list):
                    raise ValueError("reflect output is not a JSON array")
                result = self._assemble(
                    snapshot,
                    request.version,
                    payload,
                    overflow_chunk_ids=request.overflow_chunk_ids,
                    consumed_chunk_ids=request.packed_chunk_ids,
                    model_id=model_id,
                )
                break
            except LLMUnavailable as exc:  # FR-2.6: typed provider outage, flagged + retried
                unavailable = True
                if self._on_unavailable is not None:
                    self._on_unavailable(str(exc))
                last_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - degrade, never raise into the caller
                last_error = str(exc)
            if attempt >= self._max_retries:
                logger.warning(
                    "reflect failed for %s after %d retries: %s",
                    snapshot.profile_id,
                    self._max_retries,
                    last_error,
                )
                return ReflectOutcome(
                    ok=False,
                    result=None,
                    error=last_error,
                    report=_with_provider_usage(report, provider_usage),
                    llm_unavailable=unavailable,
                )
            self._sleep(self._backoff(attempt))
        assert result is not None
        return ReflectOutcome(
            ok=True,
            result=result,
            report=_with_provider_usage(report, provider_usage),
            llm_unavailable=unavailable,
        )

    def _run_batched_seat(
        self,
        snapshot: Snapshot,
        llm: ChatLLM,
        batches: list[DeltaRequest],
    ) -> ReflectOutcome:
        """Batched generation pass (#99): run up to
        ``_MAX_REFLECT_BATCHES_PER_DREAM`` model-sized LLM calls per dream,
        collect raw mentions across batches, then fold ONCE globally so dedup
        sees the whole picture. Chunks whose batch never ran (the tail beyond
        the per-dream cap) stay honest overflow — merge commits exactly the
        covered ids via the allow-list safe-clear while the rest stays
        journaled for later dreams. Any batch failure degrades the whole seat:
        the snapshot stays journaled at the reflect boundary (existing
        retry-lane semantics), bounded re-burn next attempt by the batch cap.
        """
        model_id = str(getattr(llm, "model", "") or "")
        origin_by_chunk = {c.chunk_id: origin_of(c) for c in snapshot.chunks}
        mentions: list[ReflectedTriple] = []
        covered: list[str] = []
        reports: list[DeltaReport] = []
        usages: list[Usage | None] = []
        unavailable = False
        ran = 0
        for request in batches[:_MAX_REFLECT_BATCHES_PER_DREAM]:
            if not request.delta and not request.packed_chunk_ids:
                # D1 parity: a solo chunk whose block alone exceeds the packer
                # budget clips inside pack() to an empty delta. Calling the
                # LLM with an empty user turn would burn retries and degrade
                # the seat forever OUTSIDE the merge-boundary parking guard.
                # Defer instead: the chunk stays uncovered (honest overflow).
                logger.warning(
                    "batched reflect deferred %d oversized chunk(s) for %s: "
                    "block alone exceeds the delta budget; staying journaled",
                    len(request.overflow_chunk_ids),
                    snapshot.profile_id,
                )
                continue
            ran += 1
            payload, usage, batch_unavailable, error = self._chat_batch_with_retries(llm, request)
            reports.append(self._packer.report(request))
            usages.append(usage)
            unavailable = unavailable or batch_unavailable
            if payload is None:
                logger.warning(
                    "batched reflect degraded for %s at batch %d/%d: %s",
                    snapshot.profile_id,
                    ran,
                    min(len(batches), _MAX_REFLECT_BATCHES_PER_DREAM),
                    error,
                )
                degraded_covered = set(covered)
                uncovered_count = sum(1 for c in snapshot.chunks if c.chunk_id not in degraded_covered)
                return ReflectOutcome(
                    ok=False,
                    result=None,
                    error=error,
                    report=_aggregate_reports(reports, usages, overflow_count=uncovered_count),
                    llm_unavailable=unavailable,
                )
            for item in payload:
                triple = _parse_triple(snapshot, item, origin_by_chunk, model_id=model_id or None)
                if triple is not None:
                    mentions.append(triple)
            covered.extend(request.packed_chunk_ids)
        covered_set = set(covered)
        uncovered = tuple(
            c.chunk_id for c in ordered_chunks(snapshot.chunks) if c.chunk_id not in covered_set
        )
        result = _fold_triples(
            snapshot,
            batches[0].version,
            mentions,
            overflow_chunk_ids=uncovered,
            consumed_chunk_ids=tuple(covered),
            model_id=model_id or None,
        )
        return ReflectOutcome(
            ok=True,
            result=result,
            report=_aggregate_reports(reports, usages, overflow_count=len(uncovered)),
            llm_unavailable=unavailable,
            batched=True,
        )

    def _chat_batch_with_retries(
        self,
        llm: ChatLLM,
        request: DeltaRequest,
    ) -> tuple[list[Any] | None, Usage | None, bool, str]:
        """One batch's LLM call under the same retry/backoff contract as the
        legacy single-pack path. Returns (payload, usage, llm_unavailable,
        error); payload is None iff every retry was exhausted."""
        provider_usage: Usage | None = None
        last_error = ""
        unavailable = False
        for attempt in range(self._max_retries + 1):
            try:
                response = llm.chat(system=request.cache_prefix, user=request.delta)
                text, provider_usage = _split_response(response)
                payload = _loads_json_array(text)
                if not isinstance(payload, list):
                    raise ValueError("reflect output is not a JSON array")
                return payload, provider_usage, unavailable, ""
            except LLMUnavailable as exc:  # FR-2.6: typed provider outage, flagged + retried
                unavailable = True
                if self._on_unavailable is not None:
                    self._on_unavailable(str(exc))
                last_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - degrade, never raise into the caller
                last_error = str(exc)
            if attempt >= self._max_retries:
                return None, provider_usage, unavailable, last_error
            self._sleep(self._backoff(attempt))
        raise AssertionError("unreachable: retry loop must return")

    def _record_and_finalize(
        self,
        snapshot: Snapshot,
        result: ReflectionResult,
        report: DeltaReport | None,
        unavailable: bool,
        *,
        phase: SnapshotPhase,
        seat: str | None,
    ) -> ReflectOutcome:
        """Meter the run's tokens and persist the phase marker + payload as one
        atomic journal write (marker-before-progress, NFR-2.3)."""
        tracked_report = report
        if self._ledger is not None:
            # Token metering (T5b / T3b): the dream consumed the packed delta
            # plus the provider-reported output tokens; record them into the
            # current UTC month before the marker persist (the call happened,
            # the usage is attributable even if the marker write fails).
            completion = (
                tracked_report.provider_usage.completion_tokens
                if tracked_report is not None and tracked_report.provider_usage is not None
                else None
            )
            self._ledger.record(
                snapshot.profile_id,
                delta_tokens=tracked_report.delta_tokens if tracked_report is not None else 0,
                prefix_tokens=tracked_report.prefix_tokens if tracked_report is not None else 0,
                output_tokens=completion or 0,
            )
        try:
            self._finalize(snapshot, result, phase=phase, seat=seat)
        except Exception as exc:  # noqa: BLE001 - marker before progress
            logger.warning(
                "reflect done but %s persist failed for %s: %s",
                phase.value,
                snapshot.profile_id,
                exc,
            )
            return ReflectOutcome(
                ok=False,
                result=result,
                error=f"persist failed: {exc}",
                report=tracked_report,
                llm_unavailable=unavailable,
            )
        return ReflectOutcome(ok=True, result=result, report=tracked_report, llm_unavailable=unavailable)

    def _backoff(self, attempt: int) -> float:
        """Exponential schedule: base, 2*base, 4*base across retries 1..3."""
        return self._backoff_base * (1 << attempt)

    def _finalize(
        self,
        snapshot: Snapshot,
        result: ReflectionResult,
        *,
        phase: SnapshotPhase = SnapshotPhase.REFLECT_DONE,
        seat: str | None = None,
    ) -> None:
        """Persist the phase marker AND the reflection payload as one atomic
        journal file write (marker-before-progress, NFR-2.3): a crash after this
        point resumes at the merge boundary with the result intact, never
        re-runs reflect. A ``seat`` carries a vote phase's payload into the
        per-seat carrier instead of the single ``reflect_result``. Each phase
        builds on the on-disk journal (the authoritative copy) so a vote seat's
        write never clobbers a previously-finalized seat."""
        base = load_snapshot_file(self._directory / f"{snapshot.snapshot_id}.json") or snapshot
        if seat is None:
            carried = base.with_reflect(_result_to_payload(result))
        else:
            carried = base.with_vote_seat(seat, _result_to_payload(result))
        marked = carried.with_phase(phase.value)
        write_snapshot_file(self._directory, marked)
        if self._on_done is not None:
            self._on_done(snapshot.profile_id)

    def _finalize_combined(self, snapshot: Snapshot, result: ReflectionResult) -> None:
        """Persist the COMBINE_DONE marker with the combined result written into
        the single ``reflect_result`` carrier, so the merge boundary reads it
        exactly like a single-model dream (one consistent seam)."""
        base = load_snapshot_file(self._directory / f"{snapshot.snapshot_id}.json") or snapshot
        carried = base.with_reflect(_result_to_payload(result))
        marked = carried.with_phase(SnapshotPhase.COMBINE_DONE.value)
        write_snapshot_file(self._directory, marked)
        if self._on_done is not None:
            self._on_done(snapshot.profile_id)

    # ------------------------------------------------------------ contract assembly

    def _assemble(
        self,
        snapshot: Snapshot,
        version: str,
        payload: list[dict[str, Any]],
        *,
        overflow_chunk_ids: tuple[str, ...],
        consumed_chunk_ids: tuple[str, ...],
        model_id: str | None = None,
    ) -> ReflectionResult:
        origin_by_chunk = {c.chunk_id: origin_of(c) for c in snapshot.chunks}
        mentions: list[ReflectedTriple] = []
        for item in payload:
            triple = _parse_triple(snapshot, item, origin_by_chunk, model_id=model_id)
            if triple is not None:
                mentions.append(triple)
        return _fold_triples(
            snapshot,
            version,
            mentions,
            overflow_chunk_ids=overflow_chunk_ids,
            consumed_chunk_ids=consumed_chunk_ids,
            model_id=model_id,
        )


def _parse_triple(
    snapshot: Snapshot,
    item: dict[str, Any],
    origin_by_chunk: dict[str, str],
    model_id: str | None = None,
) -> ReflectedTriple | None:
    try:
        subject = str(item["subject"]).strip()
        predicate = str(item["predicate"]).strip()
        obj = str(item["object"]).strip()
        tiers = tuple(sorted({_coerce_tier(t) for t in _list_field(item["tiers"])}, key=int))
        chunk_ids = tuple(sorted({str(c) for c in _list_field(item["chunk_ids"])}))
        confidence = max(0.0, min(0.95, _coerce_confidence(item["confidence"])))
        route = Route(str(item["route"]))
        preference = bool(item.get("preference", False))
        polarity = str(item.get("polarity", "positive"))
    except (KeyError, TypeError, ValueError):
        return None  # malformed mention: skip, keep the pipeline alive
    if not subject or not predicate or not obj:
        return None
    if polarity not in ("positive", "negative"):
        polarity = "positive"
    # FR-2.12 engine invariant: preference-type evidence must be user-originated
    if preference and not all(origin_by_chunk.get(cid) == "user" for cid in chunk_ids):
        return None
    # anti-backflow engine invariant: tier-3 evidence never routes to the main graph
    if any(t is CognitiveTier.TIER_3 for t in tiers) and route is Route.CORE:
        route = Route.ISOLATED if predicate == "asserts" else Route.SALVAGE
    return ReflectedTriple(
        subject=subject,
        predicate=predicate,
        object=obj,
        tiers=tiers,
        chunk_ids=chunk_ids,
        turn_range=snapshot.turn_range,
        confidence=confidence,
        route=route,
        preference=preference,
        polarity=polarity,
        model_id=model_id,
    )


def _fold_triples(
    snapshot: Snapshot,
    version: str,
    mentions: list[ReflectedTriple],
    *,
    overflow_chunk_ids: tuple[str, ...] = (),
    consumed_chunk_ids: tuple[str, ...] = (),
    model_id: str | None = None,
) -> ReflectionResult:
    """AC-3 dedup fold: repeated mentions of the same canonical triple collapse
    into one entry with reinforced confidence, merged provenance, and the most
    restrictive route (tier-3 evidence always dominates).

    Negation guard (g2): groups are keyed by (subject, predicate, object) AND
    polarity. A key evidenced by BOTH polarities is never folded into one
    reinforced triple — both are dropped and the key is reported on
    ``conflicts`` so no downstream consumer mistakes a contradiction for
    confidence. Same-polarity mentions fold normally.
    """
    groups: dict[tuple[str, str, str], dict[str, list[ReflectedTriple]]] = {}
    for mention in mentions:
        key = (
            mention.subject.casefold().strip(),
            mention.predicate.casefold().strip(),
            mention.object.casefold().strip(),
        )
        groups.setdefault(key, {}).setdefault(mention.polarity, []).append(mention)

    folded: list[ReflectedTriple] = []
    conflicts: list[tuple[str, str, str]] = []
    for key, by_polarity in groups.items():
        if len(by_polarity) > 1:
            conflicts.append(key)
            logger.warning(
                "negation guard: contradictory-polarity mentions of %s dropped "
                "(both positive and negative evidence)",
                key,
            )
            continue
        _, member_list = next(iter(by_polarity.items()))
        group = member_list
        tiers = tuple(sorted({tier for m in group for tier in m.tiers}, key=int))
        chunk_ids = tuple(sorted({cid for m in group for cid in m.chunk_ids}))
        subject = min((m.subject for m in group), key=str.casefold)
        predicate = min((m.predicate for m in group), key=str.casefold)
        obj = min((m.object for m in group), key=str.casefold)
        confidence = min(0.95, max(m.confidence for m in group) + 0.05 * (len(group) - 1))
        route = max((m.route for m in group), key=lambda r: _ROUTE_ORDER[r])
        if any(t is CognitiveTier.TIER_3 for t in tiers) and route is Route.CORE:
            route = Route.ISOLATED if predicate == "asserts" else Route.SALVAGE
        folded.append(
            ReflectedTriple(
                subject=subject,
                predicate=predicate,
                object=obj,
                tiers=tiers,
                chunk_ids=chunk_ids,
                turn_range=snapshot.turn_range,
                confidence=confidence,
                route=route,
                preference=any(m.preference for m in group),
                polarity=str(next(iter(by_polarity))),
                model_id=model_id or next((m.model_id for m in group if m.model_id), None),
            )
        )

    folded.sort(
        key=lambda t: (t.route.value, t.subject.casefold(), t.predicate.casefold(), t.object.casefold())
    )
    return ReflectionResult(
        snapshot_id=snapshot.snapshot_id,
        profile_id=snapshot.profile_id,
        turn_range=snapshot.turn_range,
        prompt_version=version,
        triples=tuple(folded),
        conflicts=tuple(conflicts),
        overflow_chunk_ids=overflow_chunk_ids,
        consumed_chunk_ids=consumed_chunk_ids,
    )


# ---------------------------------------------------------------- journal payload (T4 seam)


def _result_to_payload(result: ReflectionResult) -> dict[str, Any]:
    """Serialize a ReflectionResult for the opaque snapshot journal carrier.

    Plain JSON words only (no enums/datetimes), so old engines can ignore the
    key and new engines can rebuild the result byte-for-byte.
    """

    def _range(rng: TurnRange) -> dict[str, int]:
        return {"start": rng.start, "end": rng.end}

    return {
        "snapshot_id": result.snapshot_id,
        "profile_id": result.profile_id,
        "turn_range": _range(result.turn_range),
        "prompt_version": result.prompt_version,
        "conflicts": [[s, p, o] for s, p, o in result.conflicts],
        "delta_overflow": list(result.overflow_chunk_ids),
        "consumed_chunk_ids": list(result.consumed_chunk_ids),
        "triples": [
            {
                "subject": t.subject,
                "predicate": t.predicate,
                "object": t.object,
                "tiers": [int(v) for v in t.tiers],
                "chunk_ids": list(t.chunk_ids),
                "turn_range": _range(t.turn_range),
                "confidence": t.confidence,
                "route": t.route.value,
                "preference": t.preference,
                "polarity": t.polarity,
                "model_id": t.model_id,
                "vote_disagreement": t.vote_disagreement,
            }
            for t in result.triples
        ],
    }


def result_from_payload(payload: dict[str, Any] | None) -> ReflectionResult | None:
    """Rebuild a ReflectionResult from the journal payload; None on malformed
    content (degrade, never a raise — a merge-boundary recovery logs and keeps
    the snapshot journaled when the payload is not recoverable)."""
    if payload is None:
        return None
    try:
        turn_range = payload["turn_range"]
        triples = tuple(_triple_from_payload(t) for t in payload.get("triples") or [])
        conflicts_raw = payload.get("conflicts") or []
        conflicts = tuple(
            (str(c[0]), str(c[1]), str(c[2])) for c in conflicts_raw if isinstance(c, list) and len(c) == 3
        )
        overflow_raw = payload.get("delta_overflow") or []
        overflow_chunk_ids = tuple(str(c) for c in overflow_raw if isinstance(c, str))
        consumed_raw = payload.get("consumed_chunk_ids") or []
        consumed_chunk_ids = tuple(str(c) for c in consumed_raw if isinstance(c, str))
        return ReflectionResult(
            snapshot_id=str(payload["snapshot_id"]),
            profile_id=str(payload["profile_id"]),
            turn_range=TurnRange(int(turn_range["start"]), int(turn_range["end"])),
            prompt_version=str(payload.get("prompt_version", "")),
            triples=triples,
            conflicts=conflicts,
            overflow_chunk_ids=overflow_chunk_ids,
            consumed_chunk_ids=consumed_chunk_ids,
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("ignoring unrecoverable reflect_result payload")
        return None


def _triple_from_payload(item: dict[str, Any]) -> ReflectedTriple:
    tr = item["turn_range"]
    polarity = str(item.get("polarity", "positive"))
    return ReflectedTriple(
        subject=str(item["subject"]),
        predicate=str(item["predicate"]),
        object=str(item["object"]),
        tiers=tuple(sorted({CognitiveTier(int(v)) for v in item["tiers"]}, key=int)),
        chunk_ids=tuple(str(c) for c in item["chunk_ids"]),
        turn_range=TurnRange(int(tr["start"]), int(tr["end"])),
        confidence=float(item["confidence"]),
        route=Route(str(item["route"])),
        preference=bool(item.get("preference", False)),
        polarity=polarity if polarity in ("positive", "negative") else "positive",
        model_id=str(item["model_id"]) if item.get("model_id") else None,
        vote_disagreement=bool(item.get("vote_disagreement", False)),
    )
