"""Completion ownership and failure isolation through the pipeline surface."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from mnemoseed_local.dream.merge import MergeOutcome
from mnemoseed_local.dream.pipeline import DreamPipeline
from mnemoseed_local.dream.reflect import ReflectionResult, ReflectOutcome
from mnemoseed_local.dream.snapshot import Snapshot
from mnemoseed_local.storage.ports import TurnRange

# Synthetic fixture values have no product meaning.
TEST_ONLY_CLOCK = 1000.0


def run_pipeline(*, outcome=None, consumer=None, profile_id="profile"):
    events = []
    snapshot = Snapshot(
        snapshot_id="run",
        profile_id=profile_id,
        turn_range=TurnRange(0, 1),
        chunks=(),
        created_at=TEST_ONLY_CLOCK,
        phases=frozenset({"snapshot_done"}),
    )
    result = ReflectionResult(
        snapshot_id="run",
        profile_id=profile_id,
        turn_range=snapshot.turn_range,
        prompt_version="v1",
        triples=(),
    )

    def merge(snapshot, result):
        events.append("graph")
        return outcome if outcome is not None else MergeOutcome(ok=True, committed=True)

    def committed(event):
        assert event.profile_id == profile_id
        assert event.dream_run_id == "run"
        with pytest.raises(FrozenInstanceError):
            event.profile_id = "other"
        events.append("consumer")
        if consumer:
            consumer(event)

    pipeline = DreamPipeline(
        trigger=SimpleNamespace(
            on_merge_committed=lambda profile: events.append("completion"),
            on_dream_failed=lambda profile: events.append("failed"),
        ),
        snapshotter=SimpleNamespace(active=lambda profile: snapshot),
        reflector=SimpleNamespace(reflect=lambda snapshot: ReflectOutcome(ok=True, result=result)),
        merger=SimpleNamespace(merge=merge),
        on_graph_committed=committed,
        on_run_committed=lambda event: events.extend(("record", "scan")),
    )
    pipeline.run(snapshot)
    return events


def test_merger_without_completion_callback_writes_main_graph(tmp_path):
    import asyncio

    from mnemoseed_local.dream.merge import Merger
    from mnemoseed_local.dream.reflect import ReflectedTriple, Route
    from mnemoseed_local.schema.stamp import CognitiveTier
    from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
    from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
    from mnemoseed_local.storage.ports import NodeFilter, Page

    main = SqliteGraphDriver(tmp_path / "main.db")
    isolated = SqliteGraphDriver(tmp_path / "isolated.db")
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        snapshot = Snapshot(
            snapshot_id="run",
            profile_id="profile",
            turn_range=TurnRange(0, 1),
            chunks=(),
            created_at=TEST_ONLY_CLOCK,
            phases=frozenset({"snapshot_done"}),
        )
        result = ReflectionResult(
            snapshot_id="run",
            profile_id="profile",
            turn_range=snapshot.turn_range,
            prompt_version="v1",
            triples=(
                ReflectedTriple(
                    subject="user",
                    predicate="prefers",
                    object="dark mode",
                    tiers=(CognitiveTier.TIER_1,),
                    chunk_ids=(),
                    turn_range=snapshot.turn_range,
                    confidence=0.7,
                    route=Route.CORE,
                ),
            ),
        )
        outcome = Merger(
            graph_main=main,
            graph_isolated=isolated,
            meta=meta,
            clock=lambda: TEST_ONLY_CLOCK,
        ).merge(snapshot, result)
        assert outcome.ok and outcome.committed and not outcome.skipped
        nodes = main.list_nodes(NodeFilter(profile_id="profile"), Page()).items
        assert len(nodes) == 1
        assert nodes[0].props["predicate"] == "prefers"
        assert nodes[0].props["object"] == "dark mode"
        assert isolated.list_nodes(NodeFilter(profile_id="profile"), Page()).items == []
    finally:
        for store in (main, isolated, meta):
            asyncio.run(store.close())


def exercise_queued_merge(tmp_path):
    import threading

    from test_reconcile_consumer import (
        PROFILE,
        _close,
        _consumer,
        _nominate,
        _policy,
        _seed_pair,
        _stores,
        _timeout_seats,
    )

    from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
    from mnemoseed_local.dream.snapshot import SnapshotResult
    from mnemoseed_local.dream.trigger import DreamTrigger

    graph, meta = _stores(tmp_path)
    _seed_pair(graph)
    _nominate(graph, meta)
    entered, release, pumping, release_pump, next_write = (threading.Event() for _ in range(5))
    errors = []
    consumer = _consumer(graph, meta, _policy(lambda: TEST_ONLY_CLOCK))

    def seats(nomination):
        entered.set()
        if not release.wait(10):
            raise BaseException("consumer release timeout")
        return _timeout_seats()(nomination)

    consumer._seats = seats
    original_repair = consumer.repair

    def repair(profile):
        pumping.set()
        if not release_pump.wait(10):
            raise BaseException("pump release timeout")
        return original_repair(profile)

    consumer.repair = repair

    class Snapshotter:
        def request(self, profile, turn_range):
            snapshot = Snapshot(
                snapshot_id=str(turn_range.start),
                profile_id=profile,
                turn_range=turn_range,
                chunks=(),
                created_at=TEST_ONLY_CLOCK,
                phases=frozenset({"snapshot_done"}),
            )
            trigger.on_snapshot_ready(profile)
            pipeline.run(snapshot)
            return SnapshotResult(snapshot=snapshot, ok=True)

        def active(self, profile):
            return None

    snapshotter = Snapshotter()
    trigger = DreamTrigger(snapshotter)

    def reflect(snapshot):
        trigger.on_reflect_complete(PROFILE)
        return ReflectOutcome(
            ok=True,
            result=ReflectionResult(
                snapshot_id=snapshot.snapshot_id,
                profile_id=PROFILE,
                turn_range=snapshot.turn_range,
                prompt_version="v1",
                triples=(),
            ),
        )

    def merge(snapshot, result):
        if snapshot.snapshot_id == "0":
            trigger.handle_event(
                PoolEvent(
                    profile_id=PROFILE,
                    kind=PoolEventKind.FORCED_CONSOLIDATION,
                    turn_range=TurnRange(2, 3),
                    balance=0.0,
                    fired_at=TEST_ONLY_CLOCK,
                )
            )
        else:
            next_write.set()
        graph.upsert_node(__import__("test_reconcile_consumer")._node("merged-" + snapshot.snapshot_id))
        return MergeOutcome(ok=True, committed=True)

    pipeline = DreamPipeline(
        trigger=trigger,
        snapshotter=snapshotter,
        reflector=SimpleNamespace(reflect=reflect),
        merger=SimpleNamespace(merge=merge),
        on_graph_committed=lambda event: consumer.coordinate(
            event, ratified=True, configured=True, ensemble_active=True
        ),
    )

    def run():
        try:
            trigger.handle_event(
                PoolEvent(
                    profile_id=PROFILE,
                    kind=PoolEventKind.FORCED_CONSOLIDATION,
                    turn_range=TurnRange(0, 1),
                    balance=0.0,
                    fired_at=TEST_ONLY_CLOCK,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(10), errors
        assert trigger.status(PROFILE).pending_queue == 1
        assert not next_write.is_set()
        release.set()
        assert pumping.wait(10), errors
        assert not next_write.is_set()
        release_pump.set()
        worker.join(10)
        assert not worker.is_alive()
        assert errors == []
        assert next_write.is_set()
    finally:
        release.set()
        release_pump.set()
        worker.join(10)
        _close(graph, meta)


def test_pump_returns_before_queued_dream_graph_write(tmp_path):
    exercise_queued_merge(tmp_path)


def test_success_calls_consumer_before_completion():
    assert run_pipeline() == ["graph", "consumer", "completion", "record", "scan"]


def test_failed_merge_never_calls_consumer_or_completion():
    assert run_pipeline(outcome=MergeOutcome(ok=False, error="write failed")) == ["graph", "failed"]


def test_skipped_merge_never_repeats_completion():
    assert run_pipeline(outcome=MergeOutcome(ok=True, skipped=True)) == ["graph"]


@pytest.mark.parametrize("fault", [None, "process", "repair"])
def test_coordinator_process_repair_completion_order(tmp_path, fault):
    from test_reconcile_consumer import _close, _consumer, _policy, _stores

    graph, meta = _stores(tmp_path)
    calls = []
    consumer = _consumer(graph, meta, _policy(lambda: TEST_ONLY_CLOCK))

    def process(profile_id, *, dream_run_id):
        calls.append(("process", profile_id, dream_run_id))
        if fault == "process":
            raise RuntimeError("process failed")
        return 0

    def repair(profile_id):
        calls.append(("repair", profile_id))
        if fault == "repair":
            raise RuntimeError("repair failed")
        return 0

    consumer.process = process
    consumer.repair = repair
    try:
        events = run_pipeline(
            consumer=lambda event: consumer.coordinate(
                event,
                ratified=True,
                configured=True,
                ensemble_active=True,
            )
        )
        assert calls == [("process", "profile", "run"), ("repair", "profile")]
        assert events == ["graph", "consumer", "completion", "record", "scan"]
    finally:
        _close(graph, meta)


@pytest.mark.parametrize("fault_kind", ["provider", "seats"])
def test_runtime_fault_records_deferred_and_completes_dream(tmp_path, fault_kind):
    from test_reconcile_consumer import (
        _attempts,
        _close,
        _consumer,
        _deferred_audits,
        _nominate,
        _policy,
        _seed_pair,
        _stores,
    )

    from mnemoseed_local.dream.adjudicate import ReasonCode

    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)

        def provider_fault(nomination):
            raise RuntimeError("provider transport down")

        def seats_fault(nomination):
            raise RuntimeError("seat assembly exploded")

        consumer = _consumer(
            graph,
            meta,
            _policy(lambda: TEST_ONLY_CLOCK),
            provider_fault if fault_kind == "provider" else seats_fault,
        )
        events = run_pipeline(
            profile_id=consumer_graph_profile(graph),
            consumer=lambda event: consumer.coordinate(
                event, ratified=True, configured=True, ensemble_active=True
            ),
        )
        assert events == ["graph", "consumer", "completion", "record", "scan"]
        audits = _deferred_audits(meta)
        assert len(audits.items) == 1
        assert audits.items[0].detail["reason_code"] == ReasonCode.SEAT_UNAVAILABLE
        assert len(_attempts(meta, carrier.nomination_id)) == 1
        assert len(graph.versions("na")) == 1
        assert (
            graph.get_reconciliation_receipt(
                profile_id=consumer_graph_profile(graph), nomination_id=carrier.nomination_id
            )
            is None
        )
    finally:
        _close(graph, meta)


def consumer_graph_profile(graph) -> str:
    return graph.get_node("na").profile_id


def test_terminal_audit_failure_keeps_receipt_and_completes_dream(tmp_path):
    from test_reconcile_consumer import (
        _close,
        _consumer,
        _decision_for,
        _nominate,
        _policy,
        _seats_for,
        _seed_pair,
        _stores,
    )

    from mnemoseed_local.dream.adjudicate import Verdict

    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        consumer = _consumer(
            graph,
            meta,
            _policy(lambda: TEST_ONLY_CLOCK),
            _seats_for(_decision_for(carrier, Verdict.NOT_CONFLICT)),
        )

        def audit_fault(entry):
            raise RuntimeError("audit store unavailable")

        meta.audit_append = audit_fault
        events = run_pipeline(
            profile_id=consumer_graph_profile(graph),
            consumer=lambda event: consumer.coordinate(
                event, ratified=True, configured=True, ensemble_active=True
            ),
        )
        assert events == ["graph", "consumer", "completion", "record", "scan"]
        receipt = graph.get_reconciliation_receipt(
            profile_id=consumer_graph_profile(graph), nomination_id=carrier.nomination_id
        )
        assert receipt is not None and receipt.disposition == "rejected"
        pending = graph.pending_reconciliation_audits(10)
        assert len(pending) == 1 and pending[0].action == "reconcile_rejected"
        assert graph.get_node(carrier.hi_node_id).read_conflict_id is None
    finally:
        _close(graph, meta)


def test_deferred_audit_failure_keeps_reservation_and_completes_dream(tmp_path):
    from test_reconcile_consumer import (
        _attempts,
        _close,
        _consumer,
        _decision_for,
        _nominate,
        _policy,
        _seats_for,
        _seed_pair,
        _stores,
    )

    from mnemoseed_local.dream.adjudicate import Verdict
    from mnemoseed_local.storage.ports import AuditFilter, Page

    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        consumer = _consumer(
            graph,
            meta,
            _policy(lambda: TEST_ONLY_CLOCK, attempt_limit=1),
            _seats_for(_decision_for(carrier, Verdict.NOT_CONFLICT)),
        )

        def apply_fault(*args, **kwargs):
            raise RuntimeError("apply boundary down")

        graph.apply_reconciliation = apply_fault

        def audit_fault(entry):
            raise RuntimeError("audit store unavailable")

        meta.audit_append = audit_fault
        events = run_pipeline(
            profile_id=consumer_graph_profile(graph),
            consumer=lambda event: consumer.coordinate(
                event, ratified=True, configured=True, ensemble_active=True
            ),
        )
        assert events == ["graph", "consumer", "completion", "record", "scan"]
        assert (
            graph.get_reconciliation_receipt(
                profile_id=consumer_graph_profile(graph), nomination_id=carrier.nomination_id
            )
            is None
        )
        attempts = _attempts(meta, carrier.nomination_id)
        assert len(attempts) == 1
        assert meta.audit_query(AuditFilter(action="reconcile_deferred"), Page(limit=50)).items == []
    finally:
        _close(graph, meta)


def test_pump_failure_does_not_block_completion():
    def fail(event):
        raise RuntimeError("audit unavailable")

    assert run_pipeline(consumer=fail) == ["graph", "consumer", "completion", "record", "scan"]


def seam_stamp_payload(chunk_id, profile_id="alice"):
    return {
        "chunk_id": chunk_id,
        "profile_id": profile_id,
        "text": "I prefer dark mode" if chunk_id == "c1" else "Overflow verbatim",
        "cognitive_tier": 1,
        "model_id": "test-model",
        "persona_id": None,
        "origin_agent": None,
        "session_parent_id": None,
        "cues": {
            "project": None,
            "host": None,
            "task": None,
            "tools_used": [],
            "time_bucket": None,
            "entities": [],
            "emotion": None,
        },
        "provenance": {
            "asserted_by": "user",
            "agent_id": None,
            "session_id": "s1",
            "source": "manual",
            "confidence": 0.5,
            "asserted_at": 1000.0,
            "history": [],
        },
        "decay_weight": 1.0,
        "last_reinforced": None,
        "score": 0.0,
        "consolidated": False,
        "needs_reconcile": False,
        "ingested_at": 1000.0,
        "turn_start": 0,
        "turn_end": 1,
        "rules": [],
    }


def seam_snapshot(profile_id="alice"):
    from mnemoseed_local.dream.snapshot import SnapshotChunk
    from mnemoseed_local.schema.stamp import ChunkStamp

    return Snapshot(
        snapshot_id="fixed-id",
        profile_id=profile_id,
        turn_range=TurnRange(0, 2),
        chunks=tuple(
            SnapshotChunk.from_stamp(ChunkStamp.model_validate(seam_stamp_payload(cid, profile_id)))
            for cid in ("c1", "c2")
        ),
        created_at=1000.0,
        phases=frozenset({"snapshot_done", "reflect_done"}),
        reflect_result={
            "snapshot_id": "fixed-id",
            "profile_id": profile_id,
            "turn_range": {"start": 0, "end": 2},
            "prompt_version": "v1",
            "triples": [
                {
                    "subject": "user",
                    "predicate": "prefers",
                    "object": obj,
                    "tiers": [1],
                    "chunk_ids": ["c1"],
                    "turn_range": {"start": 0, "end": 2},
                    "confidence": 0.8,
                    "route": route,
                    "polarity": "positive",
                }
                for obj, route in [("dark mode", "core"), ("light mode", "salvage")]
            ],
            "consumed_chunk_ids": ["c1"],
            "overflow_chunk_ids": ["c2"],
        },
    )


class CountingStore:
    def __init__(self, wrapped):
        from collections import Counter

        self.wrapped = wrapped
        self.calls = Counter()

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls[name] += 1
            return getattr(self.wrapped, name)(*args, **kwargs)

        return call


def test_disabled_seam_matches_baseline(tmp_path):
    import json

    from test_reconcile_consumer import _close

    from mnemoseed_local.dream.merge import Merger
    from mnemoseed_local.dream.snapshot import FileSnapshotter, write_snapshot_file
    from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
    from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
    from mnemoseed_local.storage.ports import AuditFilter, DreamRunFilter, NodeFilter, Page

    main = SqliteGraphDriver(tmp_path / "main.db")
    isolated = SqliteGraphDriver(tmp_path / "isolated.db")
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    counted_main, counted_isolated, counted_meta = map(CountingStore, (main, isolated, meta))
    marked = []
    vector = CountingStore(SimpleNamespace(mark_consolidated=lambda ids: marked.append(list(ids))))
    journal = tmp_path / "dreams"
    snap = seam_snapshot()
    fs = FileSnapshotter(store=vector, meta=counted_meta, directory=journal)
    fs.adopt(snap)
    write_snapshot_file(journal, snap)
    try:
        pipeline = DreamPipeline(
            trigger=SimpleNamespace(
                on_merge_committed=lambda profile: fs.purge_snapshot(profile, TurnRange(0, 2)),
                on_dream_failed=lambda profile: pytest.fail("golden dream failed"),
            ),
            snapshotter=fs,
            reflector=SimpleNamespace(),
            merger=Merger(
                graph_main=counted_main,
                graph_isolated=counted_isolated,
                meta=counted_meta,
                clock=lambda: 2000.0,
            ),
        )
        pipeline.run(snap)
        expected_ids = ["7f6800f696d0c434404003cd14a5dcb9", "d54b1da2661309d83e0523f3fb498bb4"]
        for store, node_id, obj in zip(
            (main, isolated), expected_ids, ("dark mode", "light mode"), strict=True
        ):
            nodes = store.list_nodes(NodeFilter(profile_id="alice"), Page(limit=100)).items
            assert [node.node_id for node in nodes] == [node_id]
            versions = store.versions(node_id)
            assert len(versions) == 1
            for node in [nodes[0], *versions]:
                assert node.props == {
                    "subject": "user",
                    "predicate": "prefers",
                    "object": obj,
                    "polarity": "positive",
                    "domain": "",
                    "statement": obj,
                    "valence": 0.5,
                    "prior_width": 0.3,
                    "trait_anchor": "",
                    "evidence_chain": ["c1"],
                }
                assert (
                    node.version,
                    node.prev_version_id,
                    node.valid_to,
                    node.decay_weight,
                    node.confidence,
                    node.reinforce_count,
                    node.read_conflict_id,
                ) == (1, None, None, 1.0, 0.8, 1, None)
                assert (node.created_at, node.updated_at, node.last_reinforced) == (2000.0, 2000.0, 2000.0)
                assert node.provenance.model_dump() == {
                    "asserted_by": "user",
                    "agent_id": None,
                    "session_id": "s1",
                    "source": "dream:fixed-id:turns:0-2",
                    "confidence": 0.8,
                    "asserted_at": 2000.0,
                    "history": [
                        {
                            "at": 2000.0,
                            "action": "created",
                            "actor": "dream-engine",
                            "detail": {"chunk_ids": ["c1"], "snapshot_id": "fixed-id"},
                        }
                    ],
                }
        audits = meta.audit_query(AuditFilter(), Page(limit=100)).items
        assert [(a.actor, a.action, a.at, a.detail) for a in audits] == [
            (
                "alice",
                "salvage_queued",
                2000.0,
                {
                    "subject": "user",
                    "predicate": "prefers",
                    "object": "light mode",
                    "confidence": 0.8,
                    "chunk_ids": ["c1"],
                    "snapshot_id": "fixed-id",
                    "turn_range": {"start": 0, "end": 2},
                },
            )
        ]
        raw = json.loads((journal / "fixed-id.json").read_text(encoding="utf-8"))
        for chunk in raw["chunks"]:
            chunk["stamp_json"] = json.loads(chunk["stamp_json"])
        assert raw == {
            "snapshot_id": "fixed-id",
            "profile_id": "alice",
            "turn_range": {"start": 0, "end": 2},
            "created_at": 1000.0,
            "phases": ["merge_done", "reflect_done", "snapshot_done"],
            "vote_results": None,
            "chunks": [
                {
                    "chunk_id": cid,
                    "profile_id": "alice",
                    "text": text,
                    "session_id": "s1",
                    "turn_start": 0,
                    "turn_end": 1,
                    "stamp_json": seam_stamp_payload(cid),
                }
                for cid, text in [("c1", "I prefer dark mode"), ("c2", "Overflow verbatim")]
            ],
            "reflect_result": {
                "snapshot_id": "fixed-id",
                "profile_id": "alice",
                "turn_range": {"start": 0, "end": 2},
                "prompt_version": "v1",
                "consumed_chunk_ids": ["c1"],
                "overflow_chunk_ids": ["c2"],
                "triples": [
                    {
                        "subject": "user",
                        "predicate": "prefers",
                        "object": obj,
                        "tiers": [1],
                        "chunk_ids": ["c1"],
                        "turn_range": {"start": 0, "end": 2},
                        "confidence": 0.8,
                        "route": route,
                        "polarity": "positive",
                    }
                    for obj, route in [("dark mode", "core"), ("light mode", "salvage")]
                ],
            },
        }
        assert marked == [["c1"]]
        assert dict(counted_main.calls) == {"find_same_predicate": 1, "upsert_node": 1}
        assert dict(counted_isolated.calls) == {"find_same_predicate": 1, "upsert_node": 1}
        assert dict(counted_meta.calls) == {"audit_query": 1, "audit_append": 1}
        assert dict(vector.calls) == {"mark_consolidated": 1}
        assert meta.list_dream_runs(DreamRunFilter(), Page(limit=10)).items == []
    finally:
        _close(main, isolated, meta)
