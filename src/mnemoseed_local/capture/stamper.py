"""Stamp writer — FR-1.6 stamp assembly + FR-1.8 near-duplicate dual branch.

Completes the capture funnel: scored durable turns in, chunks written to the
VectorStore. A high-confidence near-duplicate reinforces in place (Hebbian
encoding-time reinforcement, no new shard), a conflict-band near-duplicate
flags the hit chunk ``needs_reconcile`` and credits the score pool with the
prediction-error bonus, and only genuinely new content is upserted as a fresh
chunk. Emotion cues (with the ``peripheral_gaps`` flag) travel from the scorer
output into the written stamp unchanged.

Red line: capture never reads anima/preference state. The active soul id
arrives via ``WriteContext.agent_label`` — a plain neutral carrier field — and
is written onto the stamp's ``persona_id`` label; nothing in this module reads
soul/preference state (see tests/test_capture_neutrality.py).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from mnemoseed_local.capture.pool import ScorePool
from mnemoseed_local.capture.scorer import ScoredTurn
from mnemoseed_local.schema.stamp import (
    ChunkStamp,
    CognitiveTier,
    Cues,
    Provenance,
    ProvenanceEvent,
)
from mnemoseed_local.schema.turn import Turn, TurnRole
from mnemoseed_local.storage.ports import (
    Embedder,
    SparseVector,
    TurnRange,
    VectorStore,
    WeightUpdate,
)

# ---------------------------------------------------------------- configuration

# B6 batch drain: intra-batch ingested_at is constructively monotonic
# (now + i * step), not per-item real clock reads — the pre-B6 write
# semantics are reproduced as-is.
# MUST keep ε >= 1ms: _focal_scan rounds ingested_at to 3 decimals; a smaller
# epsilon may collapse back to a tie for some timestamps (value-dependent).
_BATCH_INGEST_STEP_S = 0.001


@dataclass(frozen=True)
class WriteConfig:
    """Near-duplicate decision thresholds (FR-1.8) and rebound constants."""

    reinforce_threshold: float = 0.9  # >= this and consistent -> Hebbian rebound
    conflict_threshold: float = 0.85  # >= this and conflicting -> needs_reconcile
    reinforce_bonus: float = 0.1  # decay_weight rebound step toward 1.0
    prediction_error_bonus: float = 2.0  # pool points credited on a conflict


@dataclass(frozen=True)
class WriteContext:
    """Per-write encoding context assembled by the daemon per profile/request.

    Carries the situational fields that are not on the Turn wire model
    (cognitive tier, project, task). ``agent_label`` is a plain neutral carrier
    for the active soul id — capture never reads soul/preference state; the
    daemon extracts the value and the stamp writer only writes it onto the
    stamp's ``persona_id`` label (FR-1.6).
    """

    profile_id: str
    agent_label: str | None = None
    cognitive_tier: CognitiveTier = CognitiveTier.TIER_1
    project: str | None = None
    host: str | None = None
    task: str | None = None
    time_bucket: str | None = None
    entities: tuple[str, ...] = ()
    tools_used: tuple[str, ...] = ()


class WriteOutcomeKind(StrEnum):
    """What one write did to the hippocampus."""

    NEW_CHUNK = "new_chunk"
    REINFORCED = "reinforced"
    NEEDS_RECONCILE = "needs_reconcile"


@dataclass(frozen=True)
class WriteOutcome:
    """One drain turn's write result: the outcome kind and the touched chunk."""

    kind: WriteOutcomeKind
    chunk_id: str
    similarity: float | None = None


@dataclass(frozen=True)
class _PendingWrite:
    """One decided-but-not-yet-persisted write (B6 drain batching).

    Exactly one persistence action is set: a fresh ``chunk`` (with its dense/
    sparse vectors) for a new write, a ``weight_update`` for a reinforcement,
    or a ``reconcile_id`` for a conflict flag. The caller applies it either
    immediately (``write``) or batches same-kind actions into one store call
    (``write_many``).
    """

    outcome: WriteOutcome
    chunk: ChunkStamp | None = None
    dense: Sequence[float] | None = None
    sparse: SparseVector | None = None
    weight_update: WeightUpdate | None = None
    reconcile_id: str | None = None


# --------------------------------------------------------------- text assembly


def _message_text(turn: Turn, role: TurnRole) -> str:
    """Forward, joined content of one role's steps (verbatim channel)."""
    return " ".join(step.content for step in turn.steps if step.role is role)


def _assemble_text(turn: Turn) -> str:
    """Canonical chunk text: USER then ASSISTANT message text, one labelled
    line per present role. Tool steps never join the verbatim text — tool use
    is not part of the AI response — and the verbatim channel never summarizes
    these lines. Tool names travel separately as cues (Option C)."""
    parts: list[str] = []
    user = _message_text(turn, TurnRole.USER)
    assistant = _message_text(turn, TurnRole.ASSISTANT)
    if user:
        parts.append(f"user: {user}")
    if assistant:
        parts.append(f"assistant: {assistant}")
    return "\n".join(parts)


# -------------------------------------------------------- consistency rule set
#
# Near-duplicate content consistency v1 — rules only, zero LLM. Conservative:
# when unsure the verdict is CONSISTENT so the turn reinforces and a missed
# conflict surfaces later in dream reconciliation (design/01 §1, FR-1.8).
#
#   R1  revocation flip: new text revokes/replaces a practice the old text
#       affirms (不再/不用/弃用/换成/改用; "no longer"/"switched to"...).
#   R2  quoted value mismatch: both texts carry exactly one quoted literal
#       each and the values differ ("indent": "tabs" vs "indent": "spaces").
#       Unquoted prose value flips are left to reconciliation (conservative).
#   R3  time-scoped supersession: a temporal scope word (以后/从现在/from now
#       on) combined with an explicit revoke/replace verb, old text affirming.

_REVOKE = r"(?:不再(?:用)?|不再|不用|别(?:再)?用|不要(?:再)?用|弃用|停止(?:用|使用))"
_REPLACE = r"(?:换成|改用|换用|改成|改为)"
_REVOKE_EN = r"\b(?:no longer|not anymore|stop(?:ped)? using|don'?t use|w?on'?t use|never again)\b"
_REPLACE_EN = r"\b(?:switch(?:ed)? to|switching to|replac(?:ing|ed)|moving away from)\b"
_TIME_SCOPE = r"(?:从今往后|以后|从现在开始|今后|接下来|from now on|going forward|henceforth)"
_AFFIRM_ZH = r"(?:用|喜欢|爱用|使用|采用|偏好|倾向)"
_AFFIRM_EN = r"\b(?:use|uses|using|like|likes|prefer|prefers)\b"
_QUOTED = re.compile(r"[\"']([^\"']{2,60})[\"']")

_REVOKE_RE = re.compile(_REVOKE + "|" + _REVOKE_EN)
_REPLACE_RE = re.compile(_REPLACE + "|" + _REPLACE_EN)
_TIME_SCOPE_RE = re.compile(_TIME_SCOPE)
_AFFIRM_RE = re.compile(_AFFIRM_ZH + "|" + _AFFIRM_EN)


class ConsistencyVerdict(StrEnum):
    """Outcome of the near-duplicate content consistency check."""

    CONSISTENT = "consistent"
    CONFLICT = "conflict"


class NearDuplicateChecker:
    """Rule-only consistency check between two near-duplicate texts.

    Fires CONFLICT only on the documented v1 signals (see module docstring);
    every unsure case is CONSISTENT. The caller only runs this after a
    near-duplicate probe (similarity >= conflict threshold), so the two texts
    are already known to be on the same topic.
    """

    def check(self, new_text: str, old_text: str) -> ConsistencyVerdict:
        if self._affirms_practice(old_text) and self._supersedes(new_text):
            return ConsistencyVerdict.CONFLICT
        if self._value_mismatch(new_text, old_text):
            return ConsistencyVerdict.CONFLICT
        return ConsistencyVerdict.CONSISTENT

    def _supersedes(self, new_text: str) -> bool:
        """R1 + R3: an explicit revoke or replace marker on the new text."""
        if _REVOKE_RE.search(new_text) is not None or _REPLACE_RE.search(new_text) is not None:
            return True
        return _TIME_SCOPE_RE.search(new_text) is not None and (
            _REVOKE_RE.search(new_text) is not None or _REPLACE_RE.search(new_text) is not None
        )

    def _affirms_practice(self, old_text: str) -> bool:
        """Old text explicitly states it uses / likes / prefers the practice."""
        return _AFFIRM_RE.search(old_text) is not None

    def _value_mismatch(self, new_text: str, old_text: str) -> bool:
        """R2: the quoted literals differ between the two near-duplicate texts."""
        old_values = set(_QUOTED.findall(old_text))
        new_values = set(_QUOTED.findall(new_text))
        if not old_values or not new_values:
            return False
        return old_values != new_values


# ------------------------------------------------------------------ stamp writer


class StampWriter:
    """Assembles complete ChunkStamps and routes them through the dual branch."""

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        *,
        clock: Callable[[], float],
        pool: ScorePool | None = None,
        config: WriteConfig | None = None,
        checker: NearDuplicateChecker | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._clock = clock
        self._pool = pool
        self._config = config if config is not None else WriteConfig()
        self._checker = checker if checker is not None else NearDuplicateChecker()

    @property
    def config(self) -> WriteConfig:
        return self._config

    def write(self, scored: ScoredTurn, ctx: WriteContext) -> WriteOutcome:
        """Write one durable scored turn; returns the outcome.

        The chunk text is embedded and probed against the store: a strong and
        consistent near-duplicate reinforces the existing chunk in place, a
        conflict (>= 0.85 similarity) flags the hit chunk needs_reconcile and
        credits the pool with the prediction-error bonus, anything else is
        upserted as a fresh chunk.
        """
        pending = self._decide(scored, ctx, self._clock())
        self._flush_single(pending, scored, ctx)
        return pending.outcome

    def write_many(self, items: Sequence[tuple[ScoredTurn, WriteContext]]) -> list[WriteOutcome]:
        """Write several scored turns, batching the store persistence (B6).

        Each turn is decided exactly as ``write`` (same single ranked probe at
        the conflict threshold), but the new-chunk upserts, reinforcement
        weight updates, and reconcile flags are each flushed in ONE batched
        store call instead of one per turn — a single lock/commit per action
        kind for the whole drain, not per turn. The pool prediction-error
        credits still fire per conflict (they are in-memory ledgers, not store
        round-trips). Returns one outcome per item, in order. As-is boundary:
        intra-batch ingested_at is constructively monotonic (``now + i * 1ms``
        in items order), not a per-item real clock read — isomorphic to the
        pre-B6 per-write semantics.
        """
        now = self._clock()
        pendings = [
            self._decide(item, ctx, now + index * _BATCH_INGEST_STEP_S)
            for index, (item, ctx) in enumerate(items)
        ]
        self._flush_batch(pendings, items)
        return [pending.outcome for pending in pendings]

    def _decide(self, scored: ScoredTurn, ctx: WriteContext, now: float) -> _PendingWrite:
        """Probe + near-duplicate decision for one turn, with no persistence.

        Pure of store writes: a single ranked probe at the conflict threshold
        serves both the strong (>= reinforce) and band (>= conflict) sets, so
        the drain path runs one ANN search per turn instead of two. The result
        carries the concrete persistence action (new chunk / weight update /
        reconcile flag) for the caller to apply single or batched.
        """
        stamp = self._assemble(scored, ctx, now)
        embedded = self._embedder.embed(stamp.text)
        config = self._config

        # B6 single probe: near_duplicate_ranked at the conflict threshold
        # returns everything >= 0.85 (sorted desc); strong membership is those
        # at/above the reinforce threshold. The drivers' rank order pins band[0]
        # as the strongest match (dense desc, chunk_id asc).
        ranked = self._store.near_duplicate_ranked(
            embedded.dense, config.conflict_threshold, profile_id=ctx.profile_id
        )
        if not ranked:
            return _PendingWrite(
                WriteOutcome(WriteOutcomeKind.NEW_CHUNK, stamp.chunk_id),
                chunk=stamp,
                dense=embedded.dense,
                sparse=embedded.sparse,
            )

        hit, hit_sim = ranked[0]
        strong_ids = {hit.chunk_id for hit, sim in ranked if sim >= config.reinforce_threshold}
        verdict = self._checker.check(stamp.text, hit.text)

        if hit.chunk_id in strong_ids and verdict is ConsistencyVerdict.CONSISTENT:
            rebound = min(1.0, hit.decay_weight + config.reinforce_bonus)
            return _PendingWrite(
                WriteOutcome(WriteOutcomeKind.REINFORCED, hit.chunk_id),
                weight_update=WeightUpdate(hit.chunk_id, decay_weight=rebound, last_reinforced=now),
            )
        if verdict is ConsistencyVerdict.CONFLICT:
            return _PendingWrite(
                WriteOutcome(WriteOutcomeKind.NEEDS_RECONCILE, hit.chunk_id),
                reconcile_id=hit.chunk_id,
            )
        return _PendingWrite(
            WriteOutcome(WriteOutcomeKind.NEW_CHUNK, stamp.chunk_id),
            chunk=stamp,
            dense=embedded.dense,
            sparse=embedded.sparse,
        )

    def _flush_single(self, pending: _PendingWrite, scored: ScoredTurn, ctx: WriteContext) -> None:
        """Apply one pending write's persistence immediately (the standalone
        ``write`` path)."""
        if pending.chunk is not None:
            self._store.upsert_chunk(pending.chunk, pending.dense or [], pending.sparse)
        if pending.weight_update is not None:
            self._store.update_weights([pending.weight_update])
        if pending.reconcile_id is not None:
            self._store.update_chunk_state([pending.reconcile_id], needs_reconcile=True)
        if pending.outcome.kind is WriteOutcomeKind.NEEDS_RECONCILE:
            self._credit_prediction_error(ctx, scored.turn.turn_index)

    def _flush_batch(
        self, pendings: Sequence[_PendingWrite], items: Sequence[tuple[ScoredTurn, WriteContext]]
    ) -> None:
        """Apply the pending writes of a drain, one batched store call per
        action kind (B6)."""
        new_entries: list[tuple[ChunkStamp, Sequence[float], SparseVector | None]] = []
        weight_updates: list[WeightUpdate] = []
        reconcile_ids: list[str] = []
        for pending in pendings:
            if pending.chunk is not None:
                new_entries.append((pending.chunk, pending.dense or [], pending.sparse))
            if pending.weight_update is not None:
                weight_updates.append(pending.weight_update)
            if pending.reconcile_id is not None:
                reconcile_ids.append(pending.reconcile_id)
        if new_entries:
            self._store.upsert_chunks(new_entries)
        if weight_updates:
            self._store.update_weights(weight_updates)
        if reconcile_ids:
            self._store.update_chunk_state(reconcile_ids, needs_reconcile=True)
        for pending, (scored, ctx) in zip(pendings, items, strict=True):
            if pending.outcome.kind is WriteOutcomeKind.NEEDS_RECONCILE:
                self._credit_prediction_error(ctx, scored.turn.turn_index)

    def _credit_prediction_error(self, ctx: WriteContext, turn_index: int) -> None:
        """FR-1.8: prediction-error acceleration on a detected conflict."""
        if self._pool is not None:
            self._pool.add_points(
                ctx.profile_id,
                self._config.prediction_error_bonus,
                TurnRange(turn_index, turn_index),
            )

    def _assemble(self, scored: ScoredTurn, ctx: WriteContext, now: float) -> ChunkStamp:
        """Build a complete stamp from the scored turn + write context.

        Confidence is the F2 durability confidence (marker-based), never the
        emotion — the red line keeps emotion out of provenance.confidence
        (design/01 §1.6, flashbulb uncertainty).
        """
        turn = scored.turn
        model_id = turn.model_id or "unknown"
        cues = Cues(
            project=ctx.project,
            host=ctx.host,
            task=ctx.task,
            time_bucket=ctx.time_bucket,
            entities=list(ctx.entities),
            tools_used=list(ctx.tools_used),
            emotion=scored.emotion,
        )
        provenance = Provenance(
            asserted_by=model_id if turn.model_id is not None else "user",
            agent_id=model_id if turn.model_id is not None else None,
            session_id=turn.session_id,
            source=f"{turn.host.value}-chat",
            confidence=scored.durability.confidence,
            asserted_at=now,
            history=[ProvenanceEvent(action="created", actor="capture", at=now)],
        )
        stamp = ChunkStamp(
            chunk_id=uuid.uuid4().hex,
            profile_id=ctx.profile_id,
            text=_assemble_text(turn),
            cognitive_tier=ctx.cognitive_tier,
            model_id=model_id,
            cues=cues,
            provenance=provenance,
            decay_weight=1.0,
            score=scored.importance,
            ingested_at=now,
            turn_start=turn.turn_index,
            turn_end=turn.turn_index,
        )
        stamp.persona_id = ctx.agent_label
        return stamp
