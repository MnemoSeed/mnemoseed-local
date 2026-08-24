"""Oversized-delta hot-loop guard: identical no-progress deferrals of the same
pending window park after a bounded number of attempts (one loud audit record
via the seam, then cheap no-op retries with zero LLM cost). The strike counter
is keyed by the (profile, turn-range) window — NOT by snapshot id — because
every production retry recaptures a fresh snapshot id. Progress (window growth
or committed merge) resets the counter; parking is in-process and clears on
daemon restart."""

from __future__ import annotations

from typing import Any

from mnemoseed_local.dream.pipeline import DreamPipeline, ExtractFailure, RunCompletion
from mnemoseed_local.dream.reflect import ReflectionResult, ReflectOrchestrator, ReflectOutcome
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import TurnRange

_PROFILE = "default"
_RANGE = TurnRange(start=0, end=1)


def _snap(snapshot_id: str, turn_range: TurnRange = _RANGE) -> Snapshot:
    stamp = ChunkStamp(
        chunk_id="c1",
        profile_id=_PROFILE,
        text="I prefer dark mode",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id="s1", source="manual"),
        turn_start=turn_range.start,
        turn_end=turn_range.end,
    )
    return Snapshot(
        snapshot_id=snapshot_id,
        profile_id=_PROFILE,
        turn_range=turn_range,
        chunks=(SnapshotChunk.from_stamp(stamp),),
        created_at=1000.0,
        phases=frozenset({"snapshot_done"}),
    )


def _empty_with_overflow() -> ReflectionResult:
    return ReflectionResult(
        snapshot_id="x",
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(),
        overflow_chunk_ids=tuple(f"ov{i}" for i in range(597)),
    )


class _SpyReflector(ReflectOrchestrator):
    def __init__(self, outcome: ReflectOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def reflect(self, snapshot: Snapshot) -> ReflectOutcome:
        self.calls += 1
        return ReflectOutcome(
            ok=self.outcome.ok,
            result=self.outcome.result,
            error=self.outcome.error,
            report=self.outcome.report,
            llm_unavailable=self.outcome.llm_unavailable,
            batched=self.outcome.batched,
        )


class _RecordingTrigger:
    """Minimal trigger double that tracks in-flight bookkeeping like the real
    state machine does: on_snapshot_ready advances to DREAMING (in flight),
    on_dream_failed releases it."""

    def __init__(self) -> None:
        self.in_flight = False
        self.failed_releases = 0

    def on_snapshot_ready(self, profile_id: str) -> None:
        self.in_flight = True

    def on_dream_failed(self, profile_id: str) -> None:
        if not self.in_flight:
            return
        self.in_flight = False
        self.failed_releases += 1

    def on_merge_committed(self, profile_id: str) -> None:
        self.in_flight = False


def _pipeline(
    reflector: _SpyReflector,
    failures: list[ExtractFailure],
    committed: list[RunCompletion],
    outcomes: list[tuple[str, bool]],
) -> tuple[DreamPipeline, _RecordingTrigger]:
    trigger = _RecordingTrigger()

    class _Merger:
        def merge(self, snapshot: Snapshot, result: ReflectionResult) -> Any:
            outcome = type("_MO", (), {"ok": True, "committed": True, "error": None})()
            trigger.on_merge_committed(snapshot.profile_id)
            return outcome

    pipeline = DreamPipeline(
        trigger=trigger,  # type: ignore[arg-type]
        snapshotter=type("_NS", (), {"active": staticmethod(lambda pid: None)})(),  # type: ignore[arg-type]
        reflector=reflector,  # type: ignore[arg-type]
        merger=_Merger(),  # type: ignore[arg-type]
        on_extract_failed=failures.append,
        on_run_committed=committed.append,
        on_outcome=lambda profile, rng, ok, err: outcomes.append((profile, ok)),
    )
    return pipeline, trigger


def _deliver(pipeline: DreamPipeline, delivery: int, turn_range: TurnRange = _RANGE) -> None:
    """One production-shaped delivery: launch advances the state machine, then
    a brand-new snapshot id reaches the pipeline (fresh capture per retry)."""
    pipeline.on_snapshot_ready(_PROFILE)
    pipeline.run(_snap(f"snap-{_PROFILE}-{delivery}", turn_range))


def test_identical_no_progress_deferrals_park_after_limit() -> None:
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=_empty_with_overflow()))
    failures: list[ExtractFailure] = []
    committed: list[RunCompletion] = []
    outcomes: list[tuple[str, bool]] = []
    pipeline, trigger = _pipeline(reflector, failures, committed, outcomes)

    for delivery in range(10):  # production retries never reuse a snapshot id
        _deliver(pipeline, delivery)

    parked_rows = [f for f in failures if f.failure_class == "oversized_parked"]
    deferred_rows = [f for f in failures if f.failure_class == "truncated_delta_deferred"]
    assert len(deferred_rows) == 2, "the first two strikes are honest deferrals"
    assert len(parked_rows) == 1, "one loud oversized_parked record"
    assert reflector.calls == 3, "PARK_LIMIT real attempts, then parked windows never reach the LLM again"
    assert committed == []
    assert outcomes[-1] == (_PROFILE, False)
    assert trigger.in_flight is False, "every attempt releases in-flight bookkeeping"


def test_parked_noop_releases_bookkeeping_and_reports_outcome() -> None:
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=_empty_with_overflow()))
    failures: list[ExtractFailure] = []
    outcomes: list[tuple[str, bool]] = []
    pipeline, trigger = _pipeline(reflector, failures, [], outcomes)
    for delivery in range(3):
        _deliver(pipeline, delivery)

    noop_failures_before = len(failures)
    for delivery in range(3, 6):
        trigger.in_flight = True  # simulate the real launch path re-advancing
        _deliver(pipeline, delivery)

    assert reflector.calls == 3, "post-park deliveries are cheap no-ops before any LLM call"
    assert len(failures) == noop_failures_before, "no extra audit spam after parking"
    assert trigger.in_flight is False, "parked runs release DREAMING bookkeeping instead of wedging"
    post_park = [ok for _, ok in outcomes[2:]]
    assert all(ok is False for ok in post_park), "post-park outcomes report failure so backoff counts them"


def test_parking_is_per_window_and_growth_gets_fresh_attempts() -> None:
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=_empty_with_overflow()))
    failures: list[ExtractFailure] = []
    pipeline, _ = _pipeline(reflector, failures, [], [])

    for delivery in range(5):
        _deliver(pipeline, delivery)
    assert reflector.calls == 3, "same window: parked after 3 strikes"

    grown = TurnRange(start=0, end=20)
    _deliver(pipeline, 100, grown)
    assert reflector.calls == 4, "a genuinely grown window is a different key with fresh attempts"

    disjoint = TurnRange(start=100, end=120)
    _deliver(pipeline, 200, disjoint)
    assert reflector.calls == 5, "unrelated windows are never blocked by another window's park"


def test_boot_replay_deferrals_never_arm_parking() -> None:
    """Boot recovery replays journaled merge-boundary verdicts with NO new LLM
    evidence; replaying an old verdict must not arm the parking guard, or every
    boot would re-park the window before any fresh attempt could run."""
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=_empty_with_overflow()))
    failures: list[ExtractFailure] = []
    outcomes: list[tuple[str, bool]] = []
    pipeline, _ = _pipeline(reflector, failures, [], outcomes)

    for replay in range(6):
        pipeline.on_snapshot_ready(_PROFILE)
        pipeline.run(_snap(f"snap-replay-{replay}"), counts_toward_parking=False)

    assert not [f for f in failures if f.failure_class == "oversized_parked"], "replay never parks"
    assert reflector.calls == 6, "every replay still ran its boundary honestly"
    deferred_rows = [f for f in failures if f.failure_class == "truncated_delta_deferred"]
    assert len(deferred_rows) == 6, "each replay defers with its audit record"


def test_batched_empty_verdict_commits_covered_range() -> None:
    """Batched seat: every covered chunk was fully handed to the model under
    budget, so an empty verdict is a genuine 'nothing durable here' — merge
    commits and the allow-list safe-clear advances past an unextractable head
    of queue instead of wedging the backlog forever (legacy path still defers)."""
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=_empty_with_overflow(), batched=True))
    failures: list[ExtractFailure] = []
    committed: list[RunCompletion] = []
    outcomes: list[tuple[str, bool]] = []
    pipeline, _ = _pipeline(reflector, failures, committed, outcomes)

    pipeline.on_snapshot_ready(_PROFILE)
    pipeline.run(_snap("snap-batched-empty"))

    assert not [f for f in failures if f.failure_class == "truncated_delta_deferred"], (
        "a batched empty verdict is honest, not truncation evidence"
    )
    assert len(committed) == 1, "merge committed the covered range"
    assert outcomes[-1] == (_PROFILE, True)
