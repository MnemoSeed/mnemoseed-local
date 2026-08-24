"""Oversized-delta hot-loop guard: identical no-progress deferrals of the same
snapshot park after a bounded number of attempts (one loud audit record via
the seam, then cheap no-op retries with zero LLM cost). Progress resets the
counter; parking is per-snapshot and clears on daemon restart."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mnemoseed_local.dream.pipeline import DreamPipeline, ExtractFailure, RunCompletion
from mnemoseed_local.dream.reflect import ReflectionResult, ReflectOrchestrator, ReflectOutcome
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import TurnRange

_PROFILE = "default"
_RANGE = TurnRange(start=0, end=1)


def _snap(snapshot_id: str) -> Snapshot:
    stamp = ChunkStamp(
        chunk_id="c1",
        profile_id=_PROFILE,
        text="I prefer dark mode",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id="s1", source="manual"),
        turn_start=0,
        turn_end=1,
    )
    return Snapshot(
        snapshot_id=snapshot_id,
        profile_id=_PROFILE,
        turn_range=_RANGE,
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
        )


def _pipeline(
    reflector: _SpyReflector,
    failures: list[ExtractFailure],
    committed: list[RunCompletion],
    outcomes: list[tuple[str, bool]],
) -> DreamPipeline:
    trigger = type(
        "_T",
        (),
        {
            "on_snapshot_ready": staticmethod(lambda pid: None),
            "on_dream_failed": staticmethod(lambda pid: None),
            "on_merge_committed": staticmethod(lambda pid: None),
        },
    )()

    class _Merger:
        def merge(self, snapshot: Snapshot, result: ReflectionResult) -> Any:
            return type("_MO", (), {"ok": True, "committed": True, "error": None})()

    return DreamPipeline(
        trigger=trigger,
        snapshotter=type("_NS", (), {"active": staticmethod(lambda pid: None)})(),  # type: ignore[arg-type]
        reflector=reflector,  # type: ignore[arg-type]
        merger=_Merger(),  # type: ignore[arg-type]
        on_extract_failed=failures.append,
        on_run_committed=committed.append,
        on_outcome=lambda profile, rng, ok, err: outcomes.append((profile, ok)),
    )


def test_identical_no_progress_deferrals_park_after_limit(tmp_path: Path) -> None:
    del tmp_path
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=_empty_with_overflow()))
    failures: list[ExtractFailure] = []
    committed: list[RunCompletion] = []
    outcomes: list[tuple[str, bool]] = []
    pipeline = _pipeline(reflector, failures, committed, outcomes)

    for _ in range(10):
        pipeline.run(_snap("snap-big"))

    parked_rows = [f for f in failures if f.failure_class == "oversized_parked"]
    deferred_rows = [f for f in failures if f.failure_class == "truncated_delta_deferred"]
    assert len(deferred_rows) == 2, "the first two strikes are honest deferrals"
    assert len(parked_rows) == 1, "one loud oversized_parked record"
    assert reflector.calls == 3, "PARK_LIMIT real attempts, then parked snapshots never reach the LLM again"
    assert committed == []
    assert outcomes[-1] == (_PROFILE, False)


def test_parking_is_per_snapshot(tmp_path: Path) -> None:
    del tmp_path
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=_empty_with_overflow()))
    failures: list[ExtractFailure] = []
    pipeline = _pipeline(reflector, failures, [], [])

    for _ in range(5):
        pipeline.run(_snap("snap-a"))
    pipeline.run(_snap("snap-b"))
    assert reflector.calls == 4, "3 attempts for snap-a (then parked) + 1 fresh attempt for snap-b"
