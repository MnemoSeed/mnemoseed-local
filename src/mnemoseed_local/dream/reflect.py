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
from mnemoseed_local.dream.delta import DeltaPacker, DeltaReport
from mnemoseed_local.dream.ledger import TokenLedger
from mnemoseed_local.dream.prompts import (
    ChunkBlock,
    origin_of,
    parse_chunk_blocks,
)
from mnemoseed_local.dream.snapshot import (
    Snapshot,
    SnapshotPhase,
    write_snapshot_file,
)
from mnemoseed_local.llm.types import ChatResult, LLMUnavailable, Usage
from mnemoseed_local.schema.stamp import CognitiveTier
from mnemoseed_local.storage.ports import TurnRange

logger = logging.getLogger("mnemoseed_local.dream.reflect")


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


# ---------------------------------------------------------------- the LLM seam


class ReflectLLM(Protocol):
    """Narrow chat-completion seam. T6's full DreamLLM port can satisfy this
    single method; the deterministic StubReflectLLM satisfies it offline."""

    def chat(self, *, system: str, user: str) -> str: ...


class ChatLLM(Protocol):
    """The T3 seam widened by T6: a model may return plain text (T3 stub) or a
    full ChatResult (text + provider-reported usage). Both satisfy this."""

    def chat(self, *, system: str, user: str) -> str | ChatResult: ...


def _split_response(response: str | ChatResult) -> tuple[str, Usage | None]:
    """Normalize a ChatLLM response: (text, provider usage). A plain str is
    treated as text with no provider-reported usage."""
    if isinstance(response, ChatResult):
        return response.text, response.usage
    return response, None


def _with_provider_usage(report: DeltaReport, usage: Usage | None) -> DeltaReport:
    """Attach provider-reported usage to a report; identity (unchanged) when
    the provider reported none (T6 additive seam, NFR-2.2)."""
    if usage is None:
        return report
    return report.with_provider_usage(usage)


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

    def reflect(self, snapshot: Snapshot) -> ReflectOutcome:
        """Run one reflect pass. The marker gate makes a completed reflect a
        no-op: a recovered snapshot that already wrote back never re-runs."""
        if SnapshotPhase.REFLECT_DONE.value in snapshot.phases:
            return ReflectOutcome(ok=True, result=None, skipped=True)

        # F2: resolve the route fresh for THIS run (pinned per run), unless no
        # resolver is wired and the boot-time instance is the only seam.
        llm = self._resolve_llm() if self._resolve_llm is not None else self._llm
        if self._on_run_started is not None:
            model = str(getattr(llm, "model", "") or "")
            if model:
                try:
                    self._on_run_started(snapshot.snapshot_id, model)
                except Exception as exc:  # noqa: BLE001 - a recording failure never breaks the dream
                    logger.warning("dream_runs model recording failed for %s: %s", snapshot.snapshot_id, exc)

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
        for attempt in range(self._max_retries + 1):
            try:
                response = llm.chat(system=request.cache_prefix, user=request.delta)
                text, provider_usage = _split_response(response)
                payload = json.loads(text)
                if not isinstance(payload, list):
                    raise ValueError("reflect output is not a JSON array")
                result = self._assemble(
                    snapshot,
                    request.version,
                    payload,
                    overflow_chunk_ids=request.overflow_chunk_ids,
                    consumed_chunk_ids=request.packed_chunk_ids,
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
        tracked_report = _with_provider_usage(report, provider_usage)
        if self._ledger is not None:
            # Token metering (T5b / T3b): the dream consumed the packed delta
            # plus the provider-reported output tokens; record them into the
            # current UTC month before the marker persist (the call happened,
            # the usage is attributable even if the marker write fails).
            completion = provider_usage.completion_tokens if provider_usage is not None else None
            self._ledger.record(
                snapshot.profile_id,
                delta_tokens=tracked_report.delta_tokens,
                prefix_tokens=tracked_report.prefix_tokens,
                output_tokens=completion or 0,
            )
        try:
            self._finalize(snapshot, result)
        except Exception as exc:  # noqa: BLE001 - marker before progress
            logger.warning(
                "reflect done but REFLECT_DONE persist failed for %s: %s", snapshot.profile_id, exc
            )
            return ReflectOutcome(
                ok=False,
                result=result,
                error=f"persist failed: {exc}",
                report=tracked_report,
                llm_unavailable=unavailable,
            )
        return ReflectOutcome(ok=True, result=result, report=tracked_report)

    def _backoff(self, attempt: int) -> float:
        """Exponential schedule: base, 2*base, 4*base across retries 1..3."""
        return self._backoff_base * (1 << attempt)

    def _finalize(self, snapshot: Snapshot, result: ReflectionResult) -> None:
        """Persist the REFLECT_DONE marker AND the reflection payload as one
        atomic journal file write (marker-before-progress, NFR-2.3): a crash
        after this point resumes at the merge boundary with the result intact,
        never re-runs reflect."""
        carried = snapshot.with_reflect(_result_to_payload(result))
        marked = carried.with_phase(SnapshotPhase.REFLECT_DONE.value)
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
    ) -> ReflectionResult:
        origin_by_chunk = {c.chunk_id: origin_of(c) for c in snapshot.chunks}
        mentions: list[ReflectedTriple] = []
        for item in payload:
            triple = _parse_triple(snapshot, item, origin_by_chunk)
            if triple is not None:
                mentions.append(triple)
        return _fold_triples(
            snapshot,
            version,
            mentions,
            overflow_chunk_ids=overflow_chunk_ids,
            consumed_chunk_ids=consumed_chunk_ids,
        )


def _parse_triple(
    snapshot: Snapshot,
    item: dict[str, Any],
    origin_by_chunk: dict[str, str],
) -> ReflectedTriple | None:
    try:
        subject = str(item["subject"]).strip()
        predicate = str(item["predicate"]).strip()
        obj = str(item["object"]).strip()
        tiers = tuple(sorted({CognitiveTier(int(t)) for t in item["tiers"]}, key=int))
        chunk_ids = tuple(sorted({str(c) for c in item["chunk_ids"]}))
        confidence = max(0.0, min(0.95, float(item["confidence"])))
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
    )


def _fold_triples(
    snapshot: Snapshot,
    version: str,
    mentions: list[ReflectedTriple],
    *,
    overflow_chunk_ids: tuple[str, ...] = (),
    consumed_chunk_ids: tuple[str, ...] = (),
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
    )
