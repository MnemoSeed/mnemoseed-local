"""Dream trigger state machine (PRD-02 T1, FR-2.1 / FR-2.4 trigger side).

The trigger consumes ScorePool events and drives one dream lifecycle per
profile: IDLE -> ACCUMULATING -> TRIGGERED -> SNAPSHOTTING -> DREAMING ->
MERGING -> (IDLE | INTERRUPTED -> ...), exactly the design/02 section 2
state diagram. The snapshot is a seam (Snapshotter Protocol); reflect / merge
completion are callback seams that later tasks (T2 snapshot, T3 reflect)
connect. Every public call is O(1) bookkeeping.
"""

from __future__ import annotations

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
from mnemoseed_local.dream import DreamState, DreamTrigger, NullSnapshotter, SnapshotResult, TriggerStatus
from mnemoseed_local.storage.ports import TurnRange


class _RecordingSnapshotter:
    """Recording snapshot seam stub: captures every snapshot request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, TurnRange]] = []

    def request(self, profile_id: str, turn_range: TurnRange) -> SnapshotResult:
        self.requests.append((profile_id, turn_range))
        return SnapshotResult(snapshot=None, ok=True)


_DEFAULT_RANGE = TurnRange(0, 3)


def _event(
    kind: PoolEventKind = PoolEventKind.DREAM_TRIGGER,
    profile: str = "p",
    rng: TurnRange = _DEFAULT_RANGE,
    balance: float = 12.0,
    fired: float = 1000.0,
) -> PoolEvent:
    return PoolEvent(
        kind=kind,
        profile_id=profile,
        turn_range=rng,
        balance=balance,
        fired_at=fired,
    )


# ---------------------------------------------------------------- IDLE/ACCUMULATING


def test_first_turn_moves_idle_to_accumulating() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap)
    trigger.notify_activity("p")
    assert trigger.status("p").state is DreamState.ACCUMULATING
    assert snap.requests == []


def test_repeated_turns_stay_accumulating() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter())
    trigger.notify_activity("p")
    trigger.notify_activity("p")
    assert trigger.status("p").state is DreamState.ACCUMULATING


# ---------------------------------------------------------------- trigger lifecycle


def test_pool_event_requests_snapshot_with_event_range() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=True)
    trigger.handle_event(_event(rng=TurnRange(2, 7)))
    assert snap.requests == [("p", TurnRange(2, 7))]
    assert trigger.status("p").state is DreamState.SNAPSHOTTING


def test_snapshot_ready_moves_to_dreaming() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    assert trigger.status("p").state is DreamState.DREAMING


def test_reflect_complete_moves_to_merging() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.on_reflect_complete("p")
    assert trigger.status("p").state is DreamState.MERGING


def test_merge_committed_returns_to_idle() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.on_reflect_complete("p")
    trigger.on_merge_committed("p")
    assert trigger.status("p").state is DreamState.IDLE


def test_happy_path_completion_order() -> None:
    """The seam fakes record snapshot -> snapshot-ready -> reflect -> merge."""
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=True)
    trigger.handle_event(_event(rng=TurnRange(0, 5)))
    assert snap.requests == [("p", TurnRange(0, 5))]
    assert trigger.status("p").state is DreamState.SNAPSHOTTING
    trigger.on_snapshot_ready("p")
    assert trigger.status("p").state is DreamState.DREAMING
    trigger.on_reflect_complete("p")
    assert trigger.status("p").state is DreamState.MERGING
    trigger.on_merge_committed("p")
    assert trigger.status("p").state is DreamState.IDLE


def test_trigger_binds_as_callable_pool_sink() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=True)
    trigger(_event())
    assert snap.requests == [("p", TurnRange(0, 3))]


# ---------------------------------------------------------------- forced consolidation


def test_forced_consolidation_launches_from_idle() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event(kind=PoolEventKind.FORCED_CONSOLIDATION, rng=TurnRange(0, 9)))
    assert trigger.status("p").state is DreamState.SNAPSHOTTING


def test_forced_during_dream_queues_not_aborts() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=True)
    trigger.handle_event(_event(rng=TurnRange(0, 3)))
    trigger.on_snapshot_ready("p")
    trigger.handle_event(_event(kind=PoolEventKind.FORCED_CONSOLIDATION, rng=TurnRange(4, 9)))
    # the running dream is not aborted and no second snapshot is requested
    assert trigger.status("p").state is DreamState.DREAMING
    assert snap.requests == [("p", TurnRange(0, 3))]
    assert trigger.status("p").pending_queue == 1
    # the dream in flight finishes its scope; the forced event drives a new range
    trigger.on_reflect_complete("p")
    trigger.on_merge_committed("p")
    assert snap.requests == [("p", TurnRange(0, 3)), ("p", TurnRange(4, 9))]
    assert trigger.status("p").state is DreamState.SNAPSHOTTING


def test_pool_event_during_snapshotting_queues() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=True)
    trigger.handle_event(_event(rng=TurnRange(0, 3)))
    trigger.handle_event(_event(rng=TurnRange(5, 8)))
    assert snap.requests == [("p", TurnRange(0, 3))]
    assert trigger.status("p").state is DreamState.SNAPSHOTTING
    assert trigger.status("p").pending_queue == 1


def test_pool_event_while_interrupted_queues_not_abort() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.notify_activity("p")
    trigger.handle_event(_event(kind=PoolEventKind.FORCED_CONSOLIDATION, rng=TurnRange(5, 9)))
    assert trigger.status("p").pending_queue == 1


# ---------------------------------------------------------------- interruption


def test_interrupt_mid_dream() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.notify_activity("p")
    assert trigger.status("p").state is DreamState.INTERRUPTED


def test_interrupt_mid_merge() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.on_reflect_complete("p")
    trigger.notify_activity("p")
    assert trigger.status("p").state is DreamState.INTERRUPTED


def test_interrupted_background_reflection_resumes_merging() -> None:
    """Write-back completion edge: INTERRUPTED --(reflect complete)--> MERGING."""
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.notify_activity("p")
    trigger.on_reflect_complete("p")
    assert trigger.status("p").state is DreamState.MERGING


def test_interrupted_merge_commit_finishes_dream() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.on_reflect_complete("p")
    trigger.notify_activity("p")
    trigger.on_merge_committed("p")
    assert trigger.status("p").state is DreamState.IDLE


def test_interrupted_moves_to_accumulating_on_new_turn() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.notify_activity("p")
    trigger.notify_activity("p")
    assert trigger.status("p").state is DreamState.ACCUMULATING


def test_background_dream_completes_after_re_accumulation() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.notify_activity("p")  # DREAMING -> INTERRUPTED
    trigger.notify_activity("p")  # INTERRUPTED -> ACCUMULATING, dream still running
    trigger.on_reflect_complete("p")  # background reflection done -> write-back
    assert trigger.status("p").state is DreamState.MERGING
    trigger.on_merge_committed("p")
    assert trigger.status("p").state is DreamState.IDLE


def test_new_event_while_background_dream_in_flight_queues() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("p")
    trigger.notify_activity("p")
    trigger.notify_activity("p")  # ACCUMULATING + background dream in flight
    snap.requests.clear()
    trigger.handle_event(_event(rng=TurnRange(6, 9)))
    assert snap.requests == []
    assert trigger.status("p").pending_queue == 1


# ---------------------------------------------------------------- turn_range passthrough


def test_turn_range_passthrough_forced() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=True)
    trigger.handle_event(_event(kind=PoolEventKind.FORCED_CONSOLIDATION, rng=TurnRange(10, 20)))
    assert snap.requests == [("p", TurnRange(10, 20))]
    assert trigger.status("p").current_range == TurnRange(10, 20)


def test_turn_range_passthrough_after_reaccumulation() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=True)
    trigger.handle_event(_event(rng=TurnRange(0, 3)))
    trigger.on_snapshot_ready("p")
    trigger.handle_event(_event(rng=TurnRange(7, 9)))
    trigger.on_reflect_complete("p")
    trigger.on_merge_committed("p")
    assert snap.requests[-1] == ("p", TurnRange(7, 9))


# ---------------------------------------------------------------- manual-first (FR-2.8)


def test_auto_trigger_false_records_pending_manual_only() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=False)
    trigger.notify_activity("p")
    trigger.handle_event(_event())
    trigger.handle_event(_event(kind=PoolEventKind.FORCED_CONSOLIDATION, rng=TurnRange(5, 8)))
    assert snap.requests == []
    assert trigger.status("p").state is DreamState.ACCUMULATING
    assert trigger.status("p").pending_manual == 2


def test_pending_manual_default_is_false() -> None:
    """The FR-2.8 safety default is pinned: a default-constructed trigger
    records pool events as pending_manual and never launches a snapshot."""
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap)
    trigger.handle_event(_event())
    assert snap.requests == []
    assert trigger.status("p").state is DreamState.IDLE
    assert trigger.status("p").pending_manual == 1


def test_dream_once_runs_exactly_one_manual_cycle() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=False)
    trigger.handle_event(_event(rng=TurnRange(0, 3)))
    trigger.handle_event(_event(rng=TurnRange(6, 9)))
    assert trigger.status("p").pending_manual == 2
    assert trigger.dream_once("p") is True
    assert snap.requests == [("p", TurnRange(0, 3))]
    assert trigger.status("p").state is DreamState.SNAPSHOTTING
    # a running dream never overlaps a manual run
    assert trigger.dream_once("p") is False
    # completing the cycle lets the next manual run consume the remaining event
    trigger.on_snapshot_ready("p")
    trigger.on_reflect_complete("p")
    trigger.on_merge_committed("p")
    assert trigger.dream_once("p") is True
    assert snap.requests == [("p", TurnRange(0, 3)), ("p", TurnRange(6, 9))]


def test_dream_once_without_pending_is_no_op() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=False)
    assert trigger.dream_once("p") is False
    assert trigger.status("p").state is DreamState.IDLE


# ---------------------------------------------------------------- one-dream-at-a-time


def test_overlapping_trigger_while_dreaming_queues_one_dream_at_a_time() -> None:
    snap = _RecordingSnapshotter()
    trigger = DreamTrigger(snapshotter=snap, auto_trigger=True)
    trigger.handle_event(_event(rng=TurnRange(0, 3)))
    trigger.on_snapshot_ready("p")
    trigger.handle_event(_event(rng=TurnRange(4, 7)))
    assert snap.requests == [("p", TurnRange(0, 3))]
    assert trigger.status("p").pending_queue == 1
    trigger.on_reflect_complete("p")
    trigger.on_merge_committed("p")
    assert snap.requests == [("p", TurnRange(0, 3)), ("p", TurnRange(4, 7))]


# ---------------------------------------------------------------- isolation + status


def test_multi_profile_isolation() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.notify_activity("a")
    trigger.handle_event(_event(profile="a"))
    trigger.on_snapshot_ready("a")
    trigger.on_reflect_complete("a")
    trigger.handle_event(_event(profile="b"))
    assert trigger.status("a").state is DreamState.MERGING
    assert trigger.status("b").state is DreamState.SNAPSHOTTING
    trigger.on_merge_committed("a")
    assert trigger.status("a").state is DreamState.IDLE
    assert trigger.status("b").state is DreamState.SNAPSHOTTING
    assert trigger.status("a").pending_queue == 0


def test_profile_notices_are_independent() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event(profile="a"))
    trigger.on_snapshot_ready("a")
    trigger.notify_activity("a")  # interrupts only profile a
    trigger.handle_event(_event(profile="b"))
    trigger.on_snapshot_ready("b")
    assert trigger.status("a").state is DreamState.INTERRUPTED
    assert trigger.status("b").state is DreamState.DREAMING


def test_status_observability() -> None:
    trigger = DreamTrigger(snapshotter=_RecordingSnapshotter(), auto_trigger=True)
    ev = _event(rng=TurnRange(1, 4))
    trigger.handle_event(ev)
    status = trigger.status("p")
    assert isinstance(status, TriggerStatus)
    assert status.profile_id == "p"
    assert status.state is DreamState.SNAPSHOTTING
    assert status.pending_queue == 0
    assert status.pending_manual == 0
    assert status.last_event is ev
    assert status.current_range == TurnRange(1, 4)
    # unknown profiles report a neutral idle snapshot, never an error
    assert trigger.status("ghost").state is DreamState.IDLE


def test_null_snapshotter_advances_state_without_recording() -> None:
    trigger = DreamTrigger(snapshotter=NullSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    assert trigger.status("p").state is DreamState.SNAPSHOTTING
