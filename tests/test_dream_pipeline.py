"""Dream pipeline orchestration (PRD-02 T4 wiring; FR-2.3 / NFR-2.3).

The DreamPipeline drives the reflect -> merge -> commit chain off the /ingest
hot path. A snapshot at the reflect boundary runs reflect then merge; a
recovered snapshot at the merge boundary runs merge ONLY (the persisted
reflect result is re-loaded, reflect is never re-run). A degraded path (reflect
failure, or a merge-boundary snapshot without a persisted result) leaves the
snapshot journaled and never calls the merger.
"""

from __future__ import annotations

from pathlib import Path

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
from mnemoseed_local.dream import (
    DreamState,
    DreamTrigger,
    FileSnapshotter,
    NullSnapshotter,
    ReflectedTriple,
    ReflectionResult,
    ReflectOrchestrator,
    ReflectOutcome,
    Route,
    SnapshotPhase,
    StubReflectLLM,
    load_snapshot_file,
    resume_boundary,
)
from mnemoseed_local.dream.merge import MergeOutcome
from mnemoseed_local.dream.pipeline import DreamPipeline
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.llm.types import LLMUnavailable
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import TurnRange

_RANGE = TurnRange(0, 2)
_PROFILE = "alice"
_SNAP_ID = "snap-p1"


# ---------------------------------------------------------------- spies


class _SpyReflector:
    def __init__(self, outcome: ReflectOutcome) -> None:
        self.outcome = outcome
        self.calls: list[Snapshot] = []

    def reflect(self, snapshot: Snapshot) -> ReflectOutcome:
        self.calls.append(snapshot)
        return self.outcome


class _SpyMerger:
    def __init__(self) -> None:
        self.calls: list[tuple[Snapshot, ReflectionResult]] = []

    def merge(self, snapshot: Snapshot, result: ReflectionResult) -> MergeOutcome:
        self.calls.append((snapshot, result))
        return MergeOutcome(ok=True, committed=True)


class _FailingMerger:
    """Merger whose typed outcome is a merge degradation (never a raise)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Snapshot, ReflectionResult]] = []

    def merge(self, snapshot: Snapshot, result: ReflectionResult) -> MergeOutcome:
        self.calls.append((snapshot, result))
        return MergeOutcome(ok=False, error="graph write failed")


class _FailingLLM:
    """ReflectLLM seam that always raises the typed LLMUnavailable (FR-2.6)."""

    def chat(self, *, system: str, user: str) -> str:
        del system, user
        raise LLMUnavailable("model unavailable")


class _EmptyStore:
    """VectorStore-shaped double: captures nothing, so the snapshot is empty."""

    def capabilities(self) -> frozenset[object]:
        return frozenset()

    def snapshot_read(self, filter: object) -> list[ChunkStamp]:
        del filter
        return []


class _RunMeta:
    """MetaStore-shaped double satisfying FileSnapshotter's registration seam."""

    def __init__(self) -> None:
        self.runs: list[object] = []

    def record_dream_run(self, run: object) -> str:
        self.runs.append(run)
        return str(getattr(run, "run_id", ""))


class _NoActive:
    """Snapshotter-shaped double that never holds an active snapshot."""

    def active(self, profile_id: str) -> Snapshot | None:
        del profile_id
        return None


def _result() -> ReflectionResult:
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


def _stamp(text: str, chunk_id: str = "c1") -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id="s1", source="manual"),
        turn_start=0,
        turn_end=1,
    )


def _snap(*stamps: ChunkStamp, phases: frozenset[str] = frozenset({"snapshot_done"})) -> Snapshot:
    return Snapshot(
        snapshot_id=_SNAP_ID,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in stamps),
        created_at=1000.0,
        phases=phases,
    )


def _snapshotter(tmp_path: Path) -> FileSnapshotter:
    return FileSnapshotter(store=_EmptyStore(), meta=_RunMeta(), directory=tmp_path / "dreams")


def _pipeline(
    *,
    snapshotter: object | None = None,
    trigger: DreamTrigger | None = None,
    reflector: object | None = None,
    merger: object | None = None,
    on_outcome: object | None = None,
) -> DreamPipeline:
    return DreamPipeline(
        trigger=trigger or DreamTrigger(snapshotter=NullSnapshotter(), auto_trigger=True),
        snapshotter=snapshotter if snapshotter is not None else _NoActive(),  # type: ignore[arg-type]
        reflector=reflector or _SpyReflector(ReflectOutcome(ok=True, result=_result())),
        merger=merger or _SpyMerger(),
        on_outcome=on_outcome,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- fresh path


def test_fresh_snapshot_runs_reflect_then_merge(tmp_path: Path) -> None:
    snapshotter = _snapshotter(tmp_path)
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=True)
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=_result()))
    merger = _SpyMerger()
    pipeline = DreamPipeline(trigger=trigger, snapshotter=snapshotter, reflector=reflector, merger=merger)
    snapshotter.on_ready = pipeline.on_snapshot_ready

    trigger.handle_event(
        PoolEvent(
            kind=PoolEventKind.DREAM_TRIGGER,
            profile_id=_PROFILE,
            turn_range=_RANGE,
            balance=12.0,
            fired_at=1.0,
        )
    )

    assert len(reflector.calls) == 1
    captured = reflector.calls[0]
    assert captured.profile_id == _PROFILE
    assert captured.turn_range == _RANGE
    assert len(merger.calls) == 1
    snapshot, result = merger.calls[0]
    assert snapshot is captured
    assert result.triples[0].predicate == "prefers"


def test_pipeline_reflect_failure_degrades_never_merges(tmp_path: Path) -> None:
    del tmp_path
    snap = _snap(_stamp("I prefer dark mode"))
    reflector = _SpyReflector(ReflectOutcome(ok=False, error="model unreachable"))
    merger = _SpyMerger()
    pipeline = _pipeline(reflector=reflector, merger=merger)

    pipeline.run(snap)

    assert reflector.calls == [snap]  # reflect was attempted, then degraded
    assert merger.calls == []  # nothing handed to the split writer


# ---------------------------------------------------------------- merge-boundary recovery


def test_merge_boundary_runs_only_merge_never_rereflect(tmp_path: Path) -> None:
    snap = _snap(_stamp("I prefer dark mode", "c1"))
    persist = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path / "dreams").reflect(snap)
    assert persist.ok
    on_disk = load_snapshot_file(tmp_path / "dreams" / f"{_SNAP_ID}.json")
    assert on_disk is not None
    assert SnapshotPhase.REFLECT_DONE.value in on_disk.phases
    assert resume_boundary(on_disk) == "merge"

    reflector = _SpyReflector(ReflectOutcome(ok=True, result=None))
    merger = _SpyMerger()
    pipeline = _pipeline(snapshotter=_NoActive(), reflector=reflector, merger=merger)

    pipeline.run(on_disk)

    assert reflector.calls == []  # reflect must never re-run at the merge boundary
    assert len(merger.calls) == 1
    snapshot, result = merger.calls[0]
    assert snapshot is on_disk
    assert result is not None
    assert result.snapshot_id == _SNAP_ID
    assert len(result.triples) == 1
    assert result.triples[0].route is Route.CORE


def test_merge_boundary_without_persisted_result_degrades(tmp_path: Path) -> None:
    snap = _snap(_stamp("I prefer dark mode"), phases=frozenset({"snapshot_done", "reflect_done"}))
    assert resume_boundary(snap) == "merge"
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=None))
    merger = _SpyMerger()
    pipeline = _pipeline(snapshotter=_NoActive(), reflector=reflector, merger=merger)

    pipeline.run(snap)

    assert reflector.calls == []
    assert merger.calls == []  # left journaled, never re-reflected, never a raise


def test_recovered_after_merge_committed_is_no_op(tmp_path: Path) -> None:
    # the journal terminated the dream: resume_boundary returns None, the
    # pipeline must not resurrect it into a re-run
    done = _snap(
        _stamp("I prefer dark mode"), phases=frozenset({"snapshot_done", "reflect_done", "merge_done"})
    )
    assert resume_boundary(done) is None
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=None))
    merger = _SpyMerger()
    pipeline = _pipeline(snapshotter=_NoActive(), reflector=reflector, merger=merger)

    pipeline.run(done)

    assert reflector.calls == []
    assert merger.calls == []


# ---------------------------------------------------------------- trigger integration


def test_pipeline_on_snapshot_ready_advances_trigger_then_runs(tmp_path: Path) -> None:
    snapshotter = _snapshotter(tmp_path)
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=True)
    reflector = _SpyReflector(ReflectOutcome(ok=True, result=_result()))
    merger = _SpyMerger()
    pipeline = DreamPipeline(trigger=trigger, snapshotter=snapshotter, reflector=reflector, merger=merger)
    snapshotter.on_ready = pipeline.on_snapshot_ready
    trigger.handle_event(
        PoolEvent(
            kind=PoolEventKind.DREAM_TRIGGER,
            profile_id=_PROFILE,
            turn_range=_RANGE,
            balance=12.0,
            fired_at=1.0,
        )
    )

    # reflect never reports completion in this unit seam (spy), so the state
    # machine parks at DREAMING with the chain already handed to the merger
    assert trigger.status(_PROFILE).state is DreamState.DREAMING
    assert merger.calls


# ---------------------------------------------------------------- outcome seam (A2.5 T1 backoff)


def test_pipeline_reports_reflect_failure_to_outcome_seam(tmp_path: Path) -> None:
    """The REAL reflect failure path (a stub LLM raising LLMUnavailable, FR-2.6)
    invokes the outcome seam with ok=False — the seam the daemon wires to the
    scheduler's retry backoff. Never a raise, never a merge."""
    del tmp_path
    snap = _snap(_stamp("I prefer dark mode"))
    outcomes: list[tuple[str, TurnRange, bool, str | None]] = []
    reflector = ReflectOrchestrator(llm=_FailingLLM(), sleep=lambda _: None)
    merger = _SpyMerger()
    pipeline = _pipeline(
        reflector=reflector,
        merger=merger,
        on_outcome=lambda profile_id, rng, ok, err: outcomes.append((profile_id, rng, ok, err)),
    )

    pipeline.run(snap)

    assert merger.calls == []  # degraded: nothing handed to the split writer
    assert outcomes == [(_PROFILE, _RANGE, False, "model unavailable")]


def test_pipeline_reports_merge_failure_to_outcome_seam(tmp_path: Path) -> None:
    """A merge-boundary degradation (typed ok=False, never a raise) also reports
    ok=False through the outcome seam, so the failed window is re-scheduled."""
    snap = _snap(_stamp("I prefer dark mode"))
    outcomes: list[tuple[str, TurnRange, bool, str | None]] = []
    reflector = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path / "dreams")
    pipeline = _pipeline(
        reflector=reflector,
        merger=_FailingMerger(),
        on_outcome=lambda profile_id, rng, ok, err: outcomes.append((profile_id, rng, ok, err)),
    )

    pipeline.run(snap)

    assert outcomes == [(_PROFILE, _RANGE, False, "graph write failed")]


def test_pipeline_reports_success_to_outcome_seam(tmp_path: Path) -> None:
    """A committed dream reports ok=True through the outcome seam, so the
    scheduler can reset the profile's retry streak."""
    snap = _snap(_stamp("I prefer dark mode"))
    outcomes: list[tuple[str, TurnRange, bool, str | None]] = []
    reflector = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path / "dreams")
    pipeline = _pipeline(
        reflector=reflector,
        merger=_SpyMerger(),
        on_outcome=lambda profile_id, rng, ok, err: outcomes.append((profile_id, rng, ok, err)),
    )

    pipeline.run(snap)

    assert outcomes == [(_PROFILE, _RANGE, True, None)]
