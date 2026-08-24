"""Dream extraction-failure observation: every failed dream attempt lands one
classified audit record via the ``on_extract_failed`` seam, and a faulty audit
sink never breaks the pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mnemoseed_local.dream.delta import DeltaReport
from mnemoseed_local.dream.pipeline import DreamPipeline, ExtractFailure
from mnemoseed_local.dream.reflect import (
    ReflectionResult,
    ReflectOrchestrator,
    ReflectOutcome,
    Route,
)
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import TurnRange

_PROFILE = "default"
_RANGE = TurnRange(start=0, end=1)
_SNAP_ID = "snap-1"


class _SpyReflector(ReflectOrchestrator):
    def __init__(self, outcome: ReflectOutcome) -> None:
        self.outcome = outcome

    def reflect(self, snapshot: Snapshot) -> ReflectOutcome:
        return self.outcome


def _snap() -> Snapshot:
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
        snapshot_id=_SNAP_ID,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        chunks=(SnapshotChunk.from_stamp(stamp),),
        created_at=1000.0,
        phases=frozenset({"snapshot_done"}),
    )


class _NullSnapshotter:
    def active(self, profile_id: str) -> None:
        del profile_id


class _NoMerger:
    def merge(self, snapshot: Snapshot, result: ReflectionResult) -> Any:
        raise AssertionError("merger must not run on a failed dream")


def _run(outcome: ReflectOutcome, failures: list[ExtractFailure]) -> None:
    trigger_calls: list[tuple[str, bool]] = []
    trigger = type(
        "_T",
        (),
        {
            "on_snapshot_ready": staticmethod(lambda pid: trigger_calls.append((pid, True))),
            "on_dream_failed": staticmethod(lambda pid: trigger_calls.append((pid, False))),
            "on_merge_committed": staticmethod(lambda pid: None),
        },
    )()
    pipeline = DreamPipeline(
        trigger=trigger,
        snapshotter=_NullSnapshotter(),  # type: ignore[arg-type]
        reflector=_SpyReflector(outcome),  # type: ignore[arg-type]
        merger=_NoMerger(),  # type: ignore[arg-type]
        on_extract_failed=failures.append,
    )
    pipeline.run(_snap())


def test_failed_reflect_reports_llm_unreachable_class(tmp_path: Path) -> None:
    del tmp_path
    failures: list[ExtractFailure] = []
    _run(
        ReflectOutcome(ok=False, error="connection reset", llm_unavailable=True),
        failures,
    )
    assert len(failures) == 1
    f = failures[0]
    assert f.profile_id == _PROFILE
    assert f.stage == "reflect"
    assert f.failure_class == "llm_unreachable"
    assert f.tokens == 0


def test_persist_failure_gets_its_own_class(tmp_path: Path) -> None:
    del tmp_path
    failures: list[ExtractFailure] = []
    _run(ReflectOutcome(ok=False, error="persist failed: disk full"), failures)
    assert failures[0].failure_class == "persist_failed"
    assert failures[0].stage == "reflect"


def test_truncated_delta_defer_is_classified_not_silent(tmp_path: Path) -> None:
    del tmp_path
    empty_with_overflow = ReflectionResult(
        snapshot_id=_SNAP_ID,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(),
        overflow_chunk_ids=("c9",),
    )
    failures: list[ExtractFailure] = []

    class _DeferredMerger:
        def merge(self, snapshot: Snapshot, result: ReflectionResult) -> Any:
            raise AssertionError("deferred result must not reach the merger")

    trigger_calls: list[tuple[str, bool]] = []
    trigger = type(
        "_T",
        (),
        {
            "on_snapshot_ready": staticmethod(lambda pid: trigger_calls.append((pid, True))),
            "on_dream_failed": staticmethod(lambda pid: trigger_calls.append((pid, False))),
            "on_merge_committed": staticmethod(lambda pid: None),
        },
    )()
    report = DeltaReport(delta_tokens=120, prefix_tokens=0, overflow_count=1)
    pipeline = DreamPipeline(
        trigger=trigger,
        snapshotter=_NullSnapshotter(),  # type: ignore[arg-type]
        reflector=_SpyReflector(ReflectOutcome(ok=True, result=empty_with_overflow, report=report)),  # type: ignore[arg-type]
        merger=_DeferredMerger(),  # type: ignore[arg-type]
        on_extract_failed=failures.append,
    )
    pipeline.run(_snap())
    assert len(failures) == 1
    assert failures[0].failure_class == "truncated_delta_deferred"
    assert failures[0].tokens == 120


def test_faulty_failure_sink_never_breaks_the_pipeline(tmp_path: Path) -> None:
    del tmp_path
    outcomes: list[tuple[str, bool]] = []

    def broken_sink(failure: ExtractFailure) -> None:
        raise RuntimeError("audit store down")

    trigger_calls: list[tuple[str, bool]] = []
    trigger = type(
        "_T",
        (),
        {
            "on_snapshot_ready": staticmethod(lambda pid: trigger_calls.append((pid, True))),
            "on_dream_failed": staticmethod(lambda pid: trigger_calls.append((pid, False))),
            "on_merge_committed": staticmethod(lambda pid: None),
        },
    )()
    pipeline = DreamPipeline(
        trigger=trigger,
        snapshotter=_NullSnapshotter(),  # type: ignore[arg-type]
        reflector=_SpyReflector(ReflectOutcome(ok=False, error="boom")),  # type: ignore[arg-type]
        merger=_NoMerger(),  # type: ignore[arg-type]
        on_extract_failed=broken_sink,
        on_outcome=lambda profile, rng, ok, err: outcomes.append((profile, ok)),
    )
    pipeline.run(_snap())
    assert outcomes == [(_PROFILE, False)]


def _ok_result() -> ReflectionResult:
    from mnemoseed_local.dream.reflect import ReflectedTriple

    triple = ReflectedTriple(
        subject="user",
        predicate="prefers",
        object="dark mode",
        tiers=(CognitiveTier.TIER_1,),
        chunk_ids=("c1",),
        turn_range=_RANGE,
        confidence=0.7,
        route=Route.CORE,
        preference=True,
    )
    return ReflectionResult(
        snapshot_id=_SNAP_ID,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(triple,),
    )


def test_successful_cycle_writes_no_failure_record(tmp_path: Path) -> None:
    del tmp_path
    failures: list[ExtractFailure] = []

    class _OkMerger:
        def merge(self, snapshot: Snapshot, result: ReflectionResult) -> Any:
            return type("_MO", (), {"ok": True, "committed": True, "error": None})()

    trigger_calls: list[tuple[str, bool]] = []
    trigger = type(
        "_T",
        (),
        {
            "on_snapshot_ready": staticmethod(lambda pid: trigger_calls.append((pid, True))),
            "on_dream_failed": staticmethod(lambda pid: trigger_calls.append((pid, False))),
            "on_merge_committed": staticmethod(lambda pid: None),
        },
    )()
    pipeline = DreamPipeline(
        trigger=trigger,
        snapshotter=_NullSnapshotter(),  # type: ignore[arg-type]
        reflector=_SpyReflector(ReflectOutcome(ok=True, result=_ok_result())),  # type: ignore[arg-type]
        merger=_OkMerger(),  # type: ignore[arg-type]
        on_extract_failed=failures.append,
    )
    pipeline.run(_snap())
    assert failures == []
