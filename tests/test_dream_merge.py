"""Merge write-back + salvage queue (PRD-02 T4; FR-2.3 / FR-2.4 / AC-2 /
NFR-2.3).

The Merger consumes a T3 ReflectionResult and its Snapshot and routes each
triple by ``route``: core -> main graph instance, isolated/salvage -> the
isolated named instance, salvage additionally enqueued into the salvage queue
(survives restart through the append-only audit log). Idempotent write-back:
an existing (subject, predicate, object) node is reinforced in place, never
duplicated. Anti-backflow: a tier-3-evidenced triple is never written to the
main graph even if its route says core. Completion fires the on_committed seam
exactly once; partial failure returns a typed outcome with no completion.

Tests assert behavior through the public surface: Merger.merge,
MergeOutcome / MergeSummary, and the real sqlite drivers behind the ports.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mnemoseed_local.config import Config, DreamConfig
from mnemoseed_local.dream import (
    ReflectedTriple,
    ReflectionResult,
    ReflectOrchestrator,
    Route,
    StubReflectLLM,
)
from mnemoseed_local.dream.merge import Merger
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import AuditFilter, NodeFilter, Page, TurnRange

_RANGE = TurnRange(0, 2)
_PROFILE = "alice"


# ---------------------------------------------------------------- fakes


class _Recorder:
    """Records on_committed completions."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, profile_id: str) -> None:
        self.calls.append(profile_id)


class _BoomGraph:
    """GraphStore-shaped double that fails exactly on node writes."""

    def __init__(self) -> None:
        self.raised = 0

    def upsert_node(self, node: object) -> None:
        del node
        self.raised += 1
        raise RuntimeError("disk full")

    def find_same_predicate(self, subject: str, predicate: str, profile_id: str) -> list[object]:
        del subject, predicate, profile_id
        return []


# ---------------------------------------------------------------- helpers


def _stamp(
    chunk_id: str,
    text: str,
    *,
    tier: CognitiveTier = CognitiveTier.TIER_1,
    origin: str = "user",
    session: str = "s1",
    turn_start: int = 0,
    turn_end: int = 1,
) -> ChunkStamp:
    asserted_by = "user" if origin == "user" else "anima-model"
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=tier,
        model_id="anima-model" if origin == "agent" else "test-model",
        persona_id=None if origin == "user" else "anima-1",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by=asserted_by, session_id=session, source="manual"),
        turn_start=turn_start,
        turn_end=turn_end,
    )


def _snap(*stamps: ChunkStamp, phases: frozenset[str] = frozenset({"snapshot_done"})) -> Snapshot:
    return Snapshot(
        snapshot_id="snap-p1",
        profile_id=_PROFILE,
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in stamps),
        created_at=1000.0,
        phases=phases,
    )


def _reflect(snap: Snapshot, tmp_path: Path) -> ReflectionResult:
    outcome = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path / "dreams").reflect(snap)
    assert outcome.ok
    assert outcome.result is not None
    return outcome.result


def _result(*triples: ReflectedTriple, snapshot_id: str = "snap-p1") -> ReflectionResult:
    return ReflectionResult(
        snapshot_id=snapshot_id,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=triples,
    )


def _triple(
    predicate: str = "prefers",
    obj: str = "dark mode",
    *,
    route: Route = Route.CORE,
    tiers: tuple[CognitiveTier, ...] = (CognitiveTier.TIER_1,),
    subject: str = "user",
    confidence: float = 0.8,
    preference: bool = False,
    polarity: str = "positive",
) -> ReflectedTriple:
    return ReflectedTriple(
        subject=subject,
        predicate=predicate,
        object=obj,
        tiers=tiers,
        chunk_ids=("c1",),
        turn_range=_RANGE,
        confidence=confidence,
        route=route,
        preference=preference,
        polarity=polarity,
    )


def _nodes(graph: SqliteGraphDriver) -> list[object]:
    return graph.list_nodes(NodeFilter(profile_id=_PROFILE), Page(limit=100)).items


@pytest.fixture
def graphs(tmp_path: Path):
    main = SqliteGraphDriver(path=tmp_path / "main.db")
    isolated = SqliteGraphDriver(path=tmp_path / "isolated.db")
    meta = SqliteMetaDriver(path=tmp_path / "meta.db")
    yield main, isolated, meta
    asyncio.run(main.close())
    asyncio.run(isolated.close())
    asyncio.run(meta.close())


def _merger(
    main: object,
    isolated: object,
    meta: SqliteMetaDriver,
    *,
    on_committed: _Recorder | None = None,
    config: Config | None = None,
) -> Merger:
    return Merger(
        graph_main=main,  # type: ignore[arg-type]
        graph_isolated=isolated,  # type: ignore[arg-type]
        meta=meta,
        on_committed=on_committed,
        clock=lambda: 2000.0,
        config=config,
    )


# ---------------------------------------------------------------- routing


def test_core_routes_to_main_graph_only(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    result = _reflect(snap, tmp_path)
    done = _Recorder()
    outcome = _merger(main, isolated, meta, on_committed=done).merge(snap, result)

    assert outcome.ok
    assert outcome.committed
    assert done.calls == [_PROFILE]
    assert outcome.summary is not None
    assert outcome.summary.core == 1
    assert outcome.summary.isolated == 0
    assert outcome.summary.created == 1
    assert outcome.summary.reinforced == 0

    nodes = _nodes(main)  # type: ignore[arg-type]
    assert len(nodes) == 1
    node = nodes[0]
    assert node.props["predicate"] == "prefers"  # type: ignore[attr-defined]
    assert node.props["object"] == "dark mode"  # type: ignore[attr-defined]
    assert node.cognitive_tier == 1  # type: ignore[attr-defined]
    assert _nodes(isolated) == []  # type: ignore[arg-type]


def test_tier3_salvage_writes_isolated_and_enqueues(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(_stamp("c1", "I prefer dark mode", tier=CognitiveTier.TIER_3))
    result = _reflect(snap, tmp_path)
    assert all(t.route is Route.SALVAGE for t in result.triples)

    outcome = _merger(main, isolated, meta).merge(snap, result)
    assert outcome.ok
    assert outcome.summary is not None
    assert outcome.summary.core == 0
    assert outcome.summary.isolated == 1
    assert outcome.summary.salvage == 1
    assert _nodes(main) == []  # type: ignore[arg-type]
    assert len(_nodes(isolated)) == 1  # type: ignore[arg-type]

    page = meta.audit_query(AuditFilter(actor=_PROFILE, action="salvage_queued"), Page(limit=100))
    assert page.total == 1
    assert page.items[0].detail["object"] == "dark mode"


def test_tier3_asserts_routes_isolated_not_salvage(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(
        _stamp(
            "c1",
            "The answer is definitely option B, trust me.",
            tier=CognitiveTier.TIER_3,
            origin="agent",
            session="s2",
        )
    )
    result = _reflect(snap, tmp_path)
    assert len(result.triples) == 1
    assert result.triples[0].route is Route.ISOLATED

    outcome = _merger(main, isolated, meta).merge(snap, result)
    assert outcome.ok
    assert outcome.summary is not None
    assert outcome.summary.isolated == 1
    assert outcome.summary.salvage == 0
    assert _nodes(main) == []  # type: ignore[arg-type]
    assert len(_nodes(isolated)) == 1  # type: ignore[arg-type]


# ---------------------------------------------------------------- anti-backflow


def test_anti_backflow_tier3_never_writes_main_even_if_route_core(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    hostile = _result(_triple(route=Route.CORE, tiers=(CognitiveTier.TIER_3,)))

    outcome = _merger(main, isolated, meta).merge(snap, hostile)
    assert outcome.ok
    assert outcome.summary is not None
    assert outcome.summary.deflected == 1
    assert outcome.summary.core == 0
    assert _nodes(main) == []  # type: ignore[arg-type]
    assert _nodes(isolated) == []  # type: ignore[arg-type]


def test_no_isolated_instance_fails_typed_before_any_write(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    """AC2: the isolated instance is mandatory — a merge that needs it (salvage
    / isolated routes) without it fails typed BEFORE the first write: nothing is
    stranded silently and nothing is enqueued, so a fixed retry with the
    instance configured replays the same snapshot cleanly."""
    main, isolated, meta = graphs  # type: ignore[misc]
    del isolated
    snap = _snap(_stamp("c1", "I prefer dark mode", tier=CognitiveTier.TIER_3))
    result = _reflect(snap, tmp_path)

    outcome = _merger(main, None, meta).merge(snap, result)
    assert outcome.ok is False
    assert outcome.committed is False
    assert outcome.summary is None
    assert outcome.error is not None and "isolated" in outcome.error
    assert _nodes(main) == []  # type: ignore[arg-type]
    # the salvage queue is untouched too: the pre-check failed before anything
    # was enqueued
    page = meta.audit_query(AuditFilter(actor=_PROFILE, action="salvage_queued"), Page(limit=100))
    assert page.total == 0


def test_floor_never_downgrades_a_deflected_tier3_core_claim(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    """QA O-FIX-1: with the isolated instance present AND a confidence floor
    set, a tier-3 triple claiming CORE is deflected by the anti-backflow gate
    BEFORE the floor is consulted — it is never wrongly downgraded into the
    isolated graph (the floor applies to core triples only, and this one was
    deflected, not core)."""
    main, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    hostile = _result(_triple(route=Route.CORE, tiers=(CognitiveTier.TIER_3,), confidence=0.4))
    cfg = Config()
    cfg.dream = DreamConfig(core_confidence_floor=0.9)

    outcome = _merger(main, isolated, meta, config=cfg).merge(snap, hostile)
    assert outcome.ok
    assert outcome.summary is not None
    assert outcome.summary.deflected == 1
    assert outcome.summary.isolated == 0
    assert outcome.summary.core == 0
    assert _nodes(main) == []  # type: ignore[arg-type]
    assert _nodes(isolated) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------- idempotency (NFR-2.3)


def test_merge_rerun_reinforces_not_duplicates(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    result = _reflect(snap, tmp_path)
    first = _Recorder()
    _merger(main, isolated, meta, on_committed=first).merge(snap, result)
    assert len(_nodes(main)) == 1  # type: ignore[arg-type]

    # a fresh Merger = a daemon boot re-running the committed write-back
    second = _Recorder()
    outcome = _merger(main, isolated, meta, on_committed=second).merge(snap, result)
    assert outcome.ok
    assert outcome.summary is not None
    assert outcome.summary.reinforced == 1
    assert outcome.summary.created == 0
    assert outcome.summary.written == 1

    nodes = _nodes(main)  # type: ignore[arg-type]
    assert len(nodes) == 1  # no duplicate row ever
    node = nodes[0]
    assert node.reinforce_count == 2  # type: ignore[attr-defined]
    assert first.calls == [_PROFILE]
    assert second.calls == [_PROFILE]


def test_reinforce_appends_history_never_rewrites_provenance(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    result = _reflect(snap, tmp_path)
    _merger(main, isolated, meta).merge(snap, result)
    _merger(main, isolated, meta).merge(snap, result)

    node = _nodes(main)[0]  # type: ignore[arg-type]
    assert node.provenance.asserted_by == "user"  # type: ignore[attr-defined]
    assert node.provenance.source == "dream:snap-p1:turns:0-2"  # type: ignore[attr-defined]
    assert node.provenance.session_id == "s1"  # type: ignore[attr-defined]
    actions = [e.action for e in node.provenance.history]  # type: ignore[attr-defined]
    assert actions == ["created", "reinforced"]


def test_salvage_dedup_on_rerun(graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(_stamp("c1", "I prefer dark mode", tier=CognitiveTier.TIER_3))
    result = _reflect(snap, tmp_path)
    _merger(main, isolated, meta).merge(snap, result)
    _merger(main, isolated, meta).merge(snap, result)

    assert len(_nodes(isolated)) == 1  # type: ignore[arg-type]
    page = meta.audit_query(AuditFilter(actor=_PROFILE, action="salvage_queued"), Page(limit=100))
    assert page.total == 1  # one queue entry, never two


def test_merge_done_marker_gate_skips(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    del tmp_path
    snap = _snap(
        _stamp("c1", "I prefer dark mode"), phases=frozenset({"snapshot_done", "reflect_done", "merge_done"})
    )
    # the marker gate makes reflect itself a no-op on a completed dream, so the
    # result is constructed directly: the merge gate must skip regardless
    result = _result(_triple())
    done = _Recorder()
    outcome = _merger(main, isolated, meta, on_committed=done).merge(snap, result)
    assert outcome.ok
    assert outcome.skipped
    assert not outcome.committed
    assert done.calls == []
    assert _nodes(main) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------- failure (design/02 §7)


def test_partial_failure_return_typed_outcome_no_commit(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    _, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    result = _reflect(snap, tmp_path)
    boom = _BoomGraph()
    done = _Recorder()

    outcome = _merger(boom, isolated, meta, on_committed=done).merge(snap, result)
    assert not outcome.ok
    assert outcome.error
    assert not outcome.committed
    assert outcome.summary is None
    assert done.calls == []  # snapshot stays journaled for resume_merge


# ---------------------------------------------------------------- crash-safe queue


def test_salvage_queue_survives_store_reopen(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    snap = _snap(_stamp("c1", "I prefer dark mode", tier=CognitiveTier.TIER_3))
    result = _reflect(snap, tmp_path)
    _merger(main, isolated, meta).merge(snap, result)

    # daemon restart: fresh driver connections over the same files
    asyncio.run(main.close())  # type: ignore[attr-defined]
    asyncio.run(isolated.close())  # type: ignore[attr-defined]
    asyncio.run(meta.close())  # type: ignore[attr-defined]
    main2 = SqliteGraphDriver(path=tmp_path / "main.db")
    isolated2 = SqliteGraphDriver(path=tmp_path / "isolated.db")
    meta2 = SqliteMetaDriver(path=tmp_path / "meta.db")

    page = meta2.audit_query(AuditFilter(actor=_PROFILE, action="salvage_queued"), Page(limit=100))
    assert page.total == 1
    assert len(_nodes(isolated2)) == 1
    assert _nodes(main2) == []

    asyncio.run(main2.close())
    asyncio.run(isolated2.close())
    asyncio.run(meta2.close())


# ---------------------------------------------------------------- confidence floor (T3a / AC3)


def _floor_merger(
    main: object,
    isolated: object,
    meta: SqliteMetaDriver,
    config: Config,
    *,
    on_committed: _Recorder | None = None,
) -> Merger:
    """Merger bound to a live Config (design/01 §4.7 hot-read seam)."""
    return Merger(
        graph_main=main,  # type: ignore[arg-type]
        graph_isolated=isolated,  # type: ignore[arg-type]
        meta=meta,
        on_committed=on_committed,
        config=config,
        clock=lambda: 2000.0,
    )


def test_floor_downgrades_low_confidence_core_to_isolated(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    main, isolated, meta = graphs  # type: ignore[misc]
    config = Config()
    config.dream = DreamConfig(core_confidence_floor=0.7)
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    low = _result(_triple(route=Route.CORE, confidence=0.5))

    outcome = _floor_merger(main, isolated, meta, config).merge(snap, low)
    assert outcome.ok
    assert outcome.summary is not None
    assert outcome.summary.core == 0
    assert outcome.summary.isolated == 1
    assert _nodes(main) == []  # type: ignore[arg-type]
    assert len(_nodes(isolated)) == 1  # type: ignore[arg-type]


def test_floor_zero_keeps_current_behavior(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    """floor == 0.0 must be byte-identical to the pre-T3a routing: a core
    triple of ANY confidence still writes to the main graph."""
    main, isolated, meta = graphs  # type: ignore[misc]
    config = Config()  # default floor 0.0
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    low = _result(_triple(route=Route.CORE, confidence=0.5))

    outcome = _floor_merger(main, isolated, meta, config).merge(snap, low)
    assert outcome.ok
    assert outcome.summary is not None
    assert outcome.summary.core == 1
    assert outcome.summary.isolated == 0
    assert len(_nodes(main)) == 1  # type: ignore[arg-type]
    assert _nodes(isolated) == []  # type: ignore[arg-type]


def test_floor_without_isolated_instance_fails_typed_and_writes_nothing(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    """A floor downgrade with no isolated graph instance is a typed merge
    failure: never a silent drop, never a silent write to the main graph (the
    scheduler backoff files the failed dream — T1b linkage)."""
    main, isolated, meta = graphs  # type: ignore[misc]
    del isolated
    config = Config()
    config.dream = DreamConfig(core_confidence_floor=0.7)
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    low = _result(_triple(route=Route.CORE, confidence=0.5))
    done = _Recorder()

    outcome = _floor_merger(main, None, meta, config, on_committed=done).merge(snap, low)
    assert not outcome.ok
    assert outcome.error is not None
    assert "isolated" in outcome.error
    assert not outcome.committed
    assert done.calls == []
    assert _nodes(main) == []  # type: ignore[arg-type]  # never a silent core write


def test_floor_mixed_triples_without_isolated_never_writes_anything(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    """D-T3a-1 regression: the no-isolated failure must be ATOMIC. A mixed
    triple set (a core above the floor that would legitimately hit main, plus
    a core below the floor that needs the downgrade) with no isolated instance
    fails typed BEFORE any write — the above-floor triple must NOT leak into
    the main graph. Per-triple in-loop detection wrote it first and only then
    raised, permanently parking a node that a fixed retry would have sent to
    isolated."""
    main, isolated, meta = graphs  # type: ignore[misc]
    del isolated
    config = Config()
    config.dream = DreamConfig(core_confidence_floor=0.7)
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    # the above-floor triple comes FIRST, exactly the order that exposed the
    # partial write (high confidence hit main before the low one raised)
    mixed = _result(
        _triple(route=Route.CORE, confidence=0.9),
        _triple(obj="vim", route=Route.CORE, confidence=0.3),
    )
    done = _Recorder()

    outcome = _floor_merger(main, None, meta, config, on_committed=done).merge(snap, mixed)
    assert not outcome.ok
    assert outcome.error is not None
    assert "isolated" in outcome.error
    assert not outcome.committed
    assert done.calls == []
    assert _nodes(main) == []  # type: ignore[arg-type]  # zero pollution on failure


def test_floor_mixed_triples_without_isolated_is_atomic_in_reverse_order(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    """D-T3a-1: the atomic failure must not depend on the reflection's triple
    order — the downgrade candidate leading is the same zero-write typed
    failure."""
    main, isolated, meta = graphs  # type: ignore[misc]
    del isolated
    config = Config()
    config.dream = DreamConfig(core_confidence_floor=0.7)
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    mixed = _result(
        _triple(obj="vim", route=Route.CORE, confidence=0.3),
        _triple(route=Route.CORE, confidence=0.9),
    )

    outcome = _floor_merger(main, None, meta, config).merge(snap, mixed)
    assert not outcome.ok
    assert outcome.error is not None
    assert "isolated" in outcome.error
    assert _nodes(main) == []  # type: ignore[arg-type]


def test_floor_hot_applies_to_next_merge_without_restart(
    graphs: tuple[object, object, SqliteMetaDriver], tmp_path: Path
) -> None:
    """The Merger holds a live Config reference: a configwrite floor change
    affects the NEXT merge of the SAME merger instance (no daemon restart)."""
    main, isolated, meta = graphs  # type: ignore[misc]
    del tmp_path
    config = Config()  # floor 0.0: current behavior
    merger = _floor_merger(main, isolated, meta, config)
    snap = _snap(_stamp("c1", "I prefer dark mode"))

    first = merger.merge(snap, _result(_triple(route=Route.CORE, confidence=0.5)))
    assert first.ok
    assert first.summary is not None and first.summary.core == 1

    # hot-apply: the configwrite seam replaces config.dream on the SAME object
    config.dream = DreamConfig(core_confidence_floor=0.9)
    second = merger.merge(snap, _result(_triple(obj="vim", confidence=0.5)))
    assert second.ok
    assert second.summary is not None
    assert second.summary.core == 0
    assert second.summary.isolated == 1
    assert len(_nodes(main)) == 1  # type: ignore[arg-type]  # only the pre-floor triple
    assert len(_nodes(isolated)) == 1  # type: ignore[arg-type]  # the downgraded triple
