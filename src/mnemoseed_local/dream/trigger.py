"""Dream trigger state machine (PRD-02 T1; FR-2.1 / FR-2.4 trigger side).

The trigger consumes ScorePool events (FR-2.1) and drives exactly the design/02
section 2 lifecycle, one per profile:

    IDLE -> ACCUMULATING -> TRIGGERED -> SNAPSHOTTING -> DREAMING -> MERGING -> IDLE
    DREAMING | MERGING -> INTERRUPTED -> (ACCUMULATING | MERGING)

The pool has already evaluated the threshold and the idle window; the trigger
consumes the event and never re-evaluates a threshold. A FORCED_CONSOLIDATION
event triggers regardless of the current state: while a dream is in flight it
queues rather than aborting, and applies to a NEW range once the in-flight
dream finishes (design/02 section 7 overflow rule).

Invariants, enforced by construction:

- Every public method is O(1) state bookkeeping. The snapshot is the trigger's
  only outbound seam call; reflect / merge completion are inbound callbacks. No
  heavy work ever runs inline.
- One dream per profile at a time: an event that arrives while a dream is in
  flight (SNAPSHOTTING/DREAMING/MERGING/INTERRUPTED, or a background dream that
  survived an interrupt) joins the overflow queue and is drained one-per-dream.
- The pool event's turn_range is carried through to the snapshot request
  unchanged, and is re-used as the current_range observability field.

Manual-first discipline (FR-2.8): with ``auto_trigger=False`` (the default)
every pool event is recorded as ``pending_manual`` and drives nothing; the
console's ``dream --once`` (later task) calls ``dream_once`` to run exactly one
cycle. ``notify_activity`` is the interruption seam, wired to a new turn for a
profile; the /ingest hook-up is a later task.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, cast

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
from mnemoseed_local.config import Config
from mnemoseed_local.dream.snapshot import SnapshotResult
from mnemoseed_local.schema.stamp import ChunkStamp
from mnemoseed_local.storage.ports import ChunkFilter, Page, PoolState, TurnRange, VectorStore

logger = logging.getLogger("mnemoseed_local.dream.trigger")


class DreamState(StrEnum):
    """One profile's dream-lifecycle state (design/02 section 2)."""

    IDLE = "idle"
    ACCUMULATING = "accumulating"
    TRIGGERED = "triggered"
    SNAPSHOTTING = "snapshotting"
    DREAMING = "dreaming"
    MERGING = "merging"
    INTERRUPTED = "interrupted"


class Snapshotter(Protocol):
    """Read-only snapshot seam. The real implementation (T2) captures a frozen
    copy of the profile's chunks and reports completion synchronously through
    ``on_ready``; the trigger otherwise advances from TRIGGERED to
    SNAPSHOTTING. Store failures return a typed result, never raise into the
    ingestion hot path (design/02 section 7)."""

    def request(self, profile_id: str, turn_range: TurnRange) -> SnapshotResult: ...


class NullSnapshotter:
    """Void seam (tests and pre-T2 wiring): records nothing, always succeeds,
    so the trigger advances through SNAPSHOTTING untouched."""

    def request(self, profile_id: str, turn_range: TurnRange) -> SnapshotResult:
        del profile_id, turn_range
        return SnapshotResult(snapshot=None, ok=True)


@dataclass(frozen=True)
class TriggerStatus:
    """Observability snapshot of one profile's trigger (console reads this)."""

    profile_id: str
    state: DreamState
    pending_queue: int  # events queued while a dream ran (forced / overflows)
    pending_manual: int  # events held while auto_trigger=False (FR-2.8)
    last_event: PoolEvent | None
    current_range: TurnRange | None


@dataclass
class _Profile:
    """Per-profile trigger state (D5 isolation: never shared across profiles)."""

    state: DreamState = DreamState.IDLE
    queued: deque[PoolEvent] = field(default_factory=deque)
    pending_manual: deque[PoolEvent] = field(default_factory=deque)
    last_event: PoolEvent | None = None
    current_range: TurnRange | None = None
    dream_in_flight: bool = False  # a background (post-interrupt) dream still runs


class DreamTrigger:
    """Consumes pool events and drives one dream lifecycle per profile."""

    def __init__(
        self,
        snapshotter: Snapshotter,
        *,
        auto_trigger: bool = False,
        purger: Callable[[str, TurnRange], int] | None = None,
    ) -> None:
        self._snapshotter = snapshotter
        self._auto_trigger = auto_trigger
        self._purger = purger  # safe-clear seam, invoked exactly on merge-commit
        self._profiles: dict[str, _Profile] = {}

    # ------------------------------------------------------------ pool intake

    def handle_event(self, event: PoolEvent) -> None:
        """Consume one pool event (bound directly as the ScorePool sink)."""
        rec = self._profiles.setdefault(event.profile_id, _Profile())
        rec.last_event = event
        if not self._auto_trigger:
            rec.pending_manual.append(event)
            return
        self._deliver(event)

    def __call__(self, event: PoolEvent) -> None:
        self.handle_event(event)

    def _deliver(self, event: PoolEvent) -> None:
        """Auto path: launch a dream, or queue it behind an in-flight one."""
        rec = self._profiles.setdefault(event.profile_id, _Profile())
        if rec.dream_in_flight or rec.state in (
            DreamState.TRIGGERED,
            DreamState.SNAPSHOTTING,
            DreamState.DREAMING,
            DreamState.MERGING,
            DreamState.INTERRUPTED,
        ):
            rec.queued.append(event)
            return
        self._launch(event.profile_id, rec, event)

    # ------------------------------------------------------------ interruption

    def notify_activity(self, profile_id: str) -> None:
        """A new turn arrived for ``profile_id``.

        IDLE starts accumulating; a dream or merge in progress is interrupted
        (the background extends, snapshot scope stays fixed); an interrupted
        profile resumes accumulating on further turns, 0-latency.
        """
        rec = self._profiles.setdefault(profile_id, _Profile())
        if rec.state is DreamState.IDLE:
            rec.state = DreamState.ACCUMULATING
        elif rec.state in (DreamState.DREAMING, DreamState.MERGING):
            rec.state = DreamState.INTERRUPTED
            rec.dream_in_flight = True
        elif rec.state is DreamState.INTERRUPTED:
            rec.state = DreamState.ACCUMULATING

    # ------------------------------------------------------------ auto toggle (PRD-07)

    def set_auto_trigger(self, enabled: bool) -> None:
        """Flip the manual-first flag at runtime (console toggle, FR-2.8).

        The trigger keeps its current state; only the auto path is re-armed:
        with True the next pool event launches a dream directly, with False it
        resumes holding events as ``pending_manual``. The persisted config file
        stays the source of truth across restarts (the console writes it back).
        """
        self._auto_trigger = enabled

    @property
    def auto_trigger_enabled(self) -> bool:
        """Current auto-trigger flag (console dashboard reads this)."""
        return self._auto_trigger

    # ------------------------------------------------------------ manual (FR-2.8)

    def dream_once(self, profile_id: str) -> bool:
        """Run exactly one manual cycle; True if a dream was launched.

        Consumes the oldest pending-manual event (falling back to an overflow
        queued event) and never overlaps a dream already in flight.
        """
        rec = self._profiles.setdefault(profile_id, _Profile())
        if rec.dream_in_flight or rec.state not in (DreamState.IDLE, DreamState.ACCUMULATING):
            return False
        if rec.pending_manual:
            event = rec.pending_manual.popleft()
        elif rec.queued:
            event = rec.queued.popleft()
        else:
            return False
        self._launch(profile_id, rec, event)
        return True

    # ------------------------------------------------------------ seam callbacks

    def on_snapshot_ready(self, profile_id: str) -> None:
        """Snapshot seam completion: the read-only copy is ready.

        Accepts TRIGGERED too, because the real snapshot (T2) completes
        synchronously from inside the request: ``_launch`` leaves the state at
        TRIGGERED and this callback already advanced it to DREAMING.
        """
        rec = self._profiles.get(profile_id)
        if rec is None or rec.state not in (DreamState.SNAPSHOTTING, DreamState.TRIGGERED):
            return
        rec.state = DreamState.DREAMING

    def on_reflect_complete(self, profile_id: str) -> None:
        """Reflect seam completion: write-back of snapshot-scoped deltas starts.

        Also carries INTERRUPTED (and a background dream resumed under
        ACCUMULATING) into MERGING, the design/02 "write-back complete, only
        covering the snapshot range" edge.
        """
        rec = self._profiles.get(profile_id)
        if rec is None:
            return
        if rec.state in (DreamState.DREAMING, DreamState.INTERRUPTED):
            rec.state = DreamState.MERGING
        elif rec.state is DreamState.ACCUMULATING and rec.dream_in_flight:
            rec.state = DreamState.MERGING

    def on_merge_committed(self, profile_id: str) -> None:
        """Merge seam completion: write-back committed + safe-clear of the
        snapshot range. The dream ends; one queued overflow event drains next."""
        rec = self._profiles.get(profile_id)
        if rec is None:
            return
        if rec.state in (DreamState.MERGING, DreamState.INTERRUPTED):
            self._finish(profile_id, rec)
        elif rec.state is DreamState.ACCUMULATING and rec.dream_in_flight:
            self._finish(profile_id, rec)

    # ------------------------------------------------------------ recovery (NFR-2.3)

    def resume(self, profile_id: str, turn_range: TurnRange) -> bool:
        """Resume an interrupted dream from a recovered snapshot (NFR-2.3).

        Only applies when the profile is not already dreaming, so double
        recovery is a no-op (idempotent boot). The snapshot was already adopted
        by the snapshotter; this restores the trigger's in-flight bookkeeping
        and fixes the scope to the snapshot's range, ready for reflect.
        """
        rec = self._profiles.setdefault(profile_id, _Profile())
        if rec.dream_in_flight or rec.state not in (DreamState.IDLE, DreamState.ACCUMULATING):
            return False
        rec.state = DreamState.DREAMING
        rec.dream_in_flight = True
        rec.current_range = turn_range
        return True

    def resume_merge(self, profile_id: str, turn_range: TurnRange) -> bool:
        """Resume an interrupted dream at the merge boundary (NFR-2.3).

        The recovered snapshot already ran reflect (REFLECT_DONE), so it must
        never re-run it: the write-back committed, and re-entering DREAMING
        would duplicate graph writes on the next reflect completion. Position
        the profile straight into MERGING, so the (T4) merge-commit seam fires
        the safe-clear exactly once and the journal marks the dream complete,
        terminating recovery. Idempotent: double recovery is a no-op.
        """
        rec = self._profiles.setdefault(profile_id, _Profile())
        if rec.dream_in_flight or rec.state not in (DreamState.IDLE, DreamState.ACCUMULATING):
            return False
        rec.state = DreamState.MERGING
        rec.dream_in_flight = True
        rec.current_range = turn_range
        return True

    # ------------------------------------------------------------ internals

    def _launch(self, profile_id: str, rec: _Profile, event: PoolEvent) -> None:
        """Eligible -- trigger the dream: request the snapshot over the event's
        range. A failed snapshot (typed result) degrades the dream back to
        ACCUMULATING: ingestion is never blocked (design/02 section 7).

        On success the real snapshot completes synchronously through
        ``on_ready`` (already DREAMING); TRIGGERED remaining here means a void
        seam, so advance to SNAPSHOTTING.
        """
        rec.dream_in_flight = True
        rec.state = DreamState.TRIGGERED
        rec.current_range = event.turn_range
        result = self._snapshotter.request(profile_id, event.turn_range)
        if not result.ok:
            rec.state = DreamState.ACCUMULATING
            rec.dream_in_flight = False
            rec.current_range = None
            return
        if rec.state is DreamState.TRIGGERED:
            rec.state = DreamState.SNAPSHOTTING

    def _finish(self, profile_id: str, rec: _Profile) -> None:
        # safe-clear seam: purge the snapshot's range only once the merge for
        # that range committed. Best-effort; the merge already wrote back.
        if rec.current_range is not None and self._purger is not None:
            try:
                self._purger(profile_id, rec.current_range)
            except Exception:
                logger.warning("safe-clear failed for %s; snapshot stays journaled", profile_id)
        rec.state = DreamState.IDLE
        rec.dream_in_flight = False
        rec.current_range = None
        # one dream per profile: a queued overflow launches only after the
        # in-flight dream fully finishes, never alongside it
        if rec.queued:
            next_event = rec.queued.popleft()
            self._launch(profile_id, rec, next_event)

    # ------------------------------------------------------------ observability

    def status(self, profile_id: str) -> TriggerStatus:
        """Snapshot state, pending queue depths, and the last pool event."""
        rec = self._profiles.get(profile_id, _Profile())
        return TriggerStatus(
            profile_id=profile_id,
            state=rec.state,
            pending_queue=len(rec.queued),
            pending_manual=len(rec.pending_manual),
            last_event=rec.last_event,
            current_range=rec.current_range,
        )


# ---------------------------------------------------------------- A2 schedule (FR-2.1 / FR-2.4)
#
# The A2 schedule is score-pool based (design/01 + PRD-02): every durable
# capture turn credits its S importance into the profile's ScorePool, which
# mirrors the balance into the per-profile MetaStore row; the scheduler reads
# that balance on every tick:
#
#   floor+idle  - a profile is eligible when its pool balance >=
#                 floor_pool_points AND the profile has been idle (no capture
#                 activity) for >= idle_min_sec (defaults 10.0 points / 900s);
#   hard-deadline - a profile is forced once the OLDEST pending verbatim chunk
#                 has waited >= hard_deadline_sec (default 24h); skipped when
#                 nothing is pending.
#
# A fired dream consumes the pool (drain): the persisted balance resets to 0,
# so the same credits never trigger twice — re-firing requires the pool to
# earn toward the floor again. The hard deadline is the post-drain backstop.
#
# Both rules are CONFIG keys ([dream] table, configwrite registry) and are
# re-read on every tick, so a ``config set`` hot-applies to the next tick
# without a daemon restart. The schedule stays deterministic: an injectable
# clock replaces the wall clock in tests, and all reads go through the
# VectorStore/MetaStore ports (pending = ``consolidated=False`` chunks, the
# persisted effect of the last dream's watermark advance).

#: Daemon scheduler loop cadence (seconds). Not a config key: the trigger rules
#: themselves are; the cadence only bounds how fast a rule change is observed.
SCHEDULER_INTERVAL_S = 60.0

#: Bounded read page for the pending-chunk probe.
_SCHED_PAGE_LIMIT = 10_000


@dataclass(frozen=True)
class DreamEligibility:
    """One schedule decision: the profile is due for a dream and why."""

    profile_id: str
    reason: str  # "floor_idle" | "hard_deadline"
    pool_points: float  # the pool balance at decision time (0 when drained)
    idle_sec: float
    first_chunk_at: float
    turn_range: TurnRange  # the pending window the dream should cover


class DreamScheduler:
    """Evaluates the A2 dream trigger rules over the vector store.

    ``due_profiles`` returns every profile that is currently eligible (either
    rule), ``tick`` dedup-emits newly eligible profiles to the trigger as
    synthetic pool events: with ``auto_trigger=False`` (the default) they are
    recorded as pending_manual for ``dream --once``; with True the trigger
    launches a dream immediately. The fingerprint dedup keeps the pending queue
    at one event per (profile, window) — the same window is never re-emitted
    while a dream is pending or in flight.
    """

    def __init__(
        self,
        stores: object,
        config: Config,
        *,
        trigger: DreamTrigger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._vector: VectorStore = _vector_of(stores)
        self._meta: _MetaProbe = _meta_of(stores)
        self._config = config
        self._trigger = trigger
        self._clock = clock
        self._last: dict[str, tuple[str, int, int]] = {}

    # ------------------------------------------------------------ rules

    def eligibility(self, profile_id: str) -> DreamEligibility | None:
        """Evaluate both trigger rules for one profile; None when not due.

        The floor rule is score-pool based: ``MetaStore.pool_state`` returns
        the profile's persisted pool balance — the per-profile row the capture
        ScorePool mirrors after every credit and drains after every fired
        event. A drained pool (balance <= 0, consumed by a previous dream) is
        never floor-eligible: the same points never trigger twice, and
        re-firing requires the pool to earn toward the floor again. Idle is
        measured from the profile's latest capture activity (any chunk). The
        hard deadline counts from the OLDEST pending chunk and is skipped when
        nothing is pending.
        """
        config = self._config.dream
        now = self._clock()
        balance = self._meta.pool_state(profile_id).balance
        pending = self._pending_chunks(profile_id)
        if not pending:
            return None  # nothing unconsolidated: both rules skip
        turns = _distinct_turns(pending)
        if not turns:
            return None
        first_chunk_at = min(c.ingested_at for c in pending)
        last_activity = self._last_activity(profile_id)
        if last_activity is None:
            return None
        idle = max(0.0, now - last_activity)
        rng = TurnRange(min(start for start, _ in turns), max(end for _, end in turns))
        if balance >= config.floor_pool_points and idle >= config.idle_min_sec:
            return DreamEligibility(
                profile_id=profile_id,
                reason="floor_idle",
                pool_points=balance,
                idle_sec=idle,
                first_chunk_at=first_chunk_at,
                turn_range=rng,
            )
        if now - first_chunk_at >= config.hard_deadline_sec:
            return DreamEligibility(
                profile_id=profile_id,
                reason="hard_deadline",
                pool_points=balance,
                idle_sec=idle,
                first_chunk_at=first_chunk_at,
                turn_range=rng,
            )
        return None

    def due_profiles(self) -> list[DreamEligibility]:
        """Every profile currently eligible, in profile-id order."""
        found: list[DreamEligibility] = []
        for profile_id in sorted(self._profile_ids()):
            eligible = self.eligibility(profile_id)
            if eligible is not None:
                found.append(eligible)
        return found

    # ------------------------------------------------------------ emission

    def tick(self) -> list[DreamEligibility]:
        """Emit newly eligible profiles to the trigger (dedup by window).

        Returns the emitted eligibilities. A fingerprint of the pending window
        (reason + turn range) is stored per profile; an unchanged window is
        never re-emitted, so the manual pending queue does not accumulate
        duplicate events for the same backlog — the pool drain after a dream is
        what clears the balance, and only re-earned points over a fresh window
        re-emit.
        """
        emitted: list[DreamEligibility] = []
        for eligible in self.due_profiles():
            fingerprint = (eligible.reason, eligible.turn_range.start, eligible.turn_range.end)
            if self._last.get(eligible.profile_id) == fingerprint:
                continue
            self._last[eligible.profile_id] = fingerprint
            emitted.append(eligible)
            if self._trigger is not None:
                self._trigger.handle_event(
                    PoolEvent(
                        kind=PoolEventKind.DREAM_TRIGGER,
                        profile_id=eligible.profile_id,
                        turn_range=eligible.turn_range,
                        balance=eligible.pool_points,
                        fired_at=self._clock(),
                    )
                )
        return emitted

    async def run_forever(self) -> None:
        """The daemon-owned periodic loop over the trigger rules.

        Ticks immediately once, then sleeps one cadence; the config keys are
        re-read every tick, so a configwrite change hot-applies to the next
        tick. Never raises: a failed tick logs and retries.
        """
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("dream schedule tick failed; the next tick retries")
            await asyncio.sleep(SCHEDULER_INTERVAL_S)

    # ------------------------------------------------------------ plumbing

    def _pending_chunks(self, profile_id: str) -> list[ChunkStamp]:
        """Unconsolidated chunks: a dream's merge marks the covered range
        consolidated (the watermark advance's persisted effect on the vector
        side), so a window a previous dream already covered never re-enters the
        pending read."""
        page = self._vector.list_chunks(
            ChunkFilter(profile_id=profile_id, consolidated=False),
            Page(offset=0, limit=_SCHED_PAGE_LIMIT),
        )
        return list(page.items)

    def _last_activity(self, profile_id: str) -> float | None:
        page = self._vector.list_chunks(
            ChunkFilter(profile_id=profile_id), Page(offset=0, limit=_SCHED_PAGE_LIMIT)
        )
        if not page.items:
            return None
        return max(c.ingested_at for c in page.items)

    def _profile_ids(self) -> set[str]:
        """Every profile with a row or a score-pool ledger (D5 isolation)."""
        known = {profile.profile_id for profile in self._meta.list_profiles()}
        known.update(self._meta.pool_states())
        return known


class _MetaProbe(Protocol):
    """The minimal meta surface the scheduler probes: profile discovery and
    the score-pool balance read (the canonical persisted pool row)."""

    def list_profiles(self) -> list[Any]: ...
    def pool_state(self, profile_id: str) -> PoolState: ...
    def pool_states(self) -> dict[str, Any]: ...


def _vector_of(stores: object) -> VectorStore:
    """Resolve the vector store from a Stores object or a bare VectorStore."""
    vector = getattr(stores, "vector", None)
    if vector is not None:
        return cast(VectorStore, vector)
    if hasattr(stores, "list_chunks"):
        return cast(VectorStore, stores)
    raise TypeError("DreamScheduler requires a Stores object or a VectorStore")


def _meta_of(stores: object) -> _MetaProbe:
    """Resolve the meta store from a Stores object; must expose list_profiles,
    pool_state and pool_states (the MetaStore port surface)."""
    meta = getattr(stores, "meta", None)
    if meta is not None:
        return cast(_MetaProbe, meta)
    if all(hasattr(stores, name) for name in ("list_profiles", "pool_state", "pool_states")):
        return cast(_MetaProbe, stores)
    raise TypeError("DreamScheduler requires a Stores object or a MetaStore")


def _distinct_turns(chunks: Sequence[ChunkStamp]) -> list[tuple[int, int]]:
    """Distinct (turn_start, turn_end) capture windows among the chunks."""
    seen: set[tuple[int, int]] = set()
    ordered: list[tuple[int, int]] = []
    for chunk in chunks:
        if chunk.turn_start is None or chunk.turn_end is None:
            continue
        key = (chunk.turn_start, chunk.turn_end)
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    ordered.sort()
    return ordered
