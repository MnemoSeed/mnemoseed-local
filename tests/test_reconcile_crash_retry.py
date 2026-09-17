"""Restart recovery across the S-D crash windows: close and reopen file databases between phases."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from mnemoseed_local.dream import nominate
from mnemoseed_local.dream.adjudicate import (
    PairSeatDecision,
    SeatOutcome,
    SeatStatus,
    Verdict,
)
from mnemoseed_local.dream.consumer import ConsumerPolicy, ReconciliationConsumer
from mnemoseed_local.dream.repair import repair_reconciliation_audit
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import Provenance
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import (
    AuditFilter,
    EvidenceKind,
    EvidencePointer,
    NominationRequest,
    Page,
)

# Mechanism input only; this value has no product meaning.
TEST_ONLY_DOWNWEIGHT_FACTOR = 0.5
PROFILE = "p1"


def _node(node_id: str) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=PROFILE,
        node_type=NodeType.PREFERENCE,
        entities=["ui"],
        props={
            "domain": "editor",
            "statement": f"statement-{node_id}",
            "valence": 0.4,
            "prior_width": 0.2,
            "trait_anchor": "stable-anchor",
            "evidence_chain": [{"chunk_id": f"chunk-{node_id}"}],
        },
        provenance=Provenance(asserted_by="s-d-test", source="session://s-sd"),
        valid_from=1000.0,
        version=1,
    )


@contextlib.contextmanager
def _opened(tmp_path: Path):
    graph = SqliteGraphDriver(tmp_path / "graph.db")
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        yield graph, meta
    finally:
        asyncio.run(graph.close())
        asyncio.run(meta.close())


def _seed_pair(graph: SqliteGraphDriver) -> None:
    graph.upsert_node(_node("na"))
    graph.upsert_node(_node("nb"))
    graph.set_read_conflict("na", "nb")


def _nominate(graph: SqliteGraphDriver, meta: SqliteMetaDriver):
    meta.append_reconcile_nomination(
        NominationRequest(
            profile_id=PROFILE,
            canonical_kind="read_conflict",
            node_a="na",
            version_a=1,
            expected_peer_a="nb",
            node_b="nb",
            version_b=1,
            expected_peer_b="na",
            source_generation=1,
            observed_at=1000.0,
            evidence=(
                EvidencePointer(kind=EvidenceKind.NODE, id="na"),
                EvidencePointer(kind=EvidenceKind.NODE, id="nb"),
            ),
            source_channels=("read_conflict_flag",),
        )
    )
    page = meta.query_reconciliation_nominations(profile_id=PROFILE, limit=10, cursor=None)
    return page.items[0]


def _policy(clock, **over) -> ConsumerPolicy:
    base: dict = {
        "nomination_limit": 10,
        "scan_limit": 10,
        "audit_repair_limit": 10,
        "attempt_limit": 3,
        "retry_backoff_seconds": 60.0,
        "attempt_timeout_seconds": 30.0,
        "seat_mode": "verify",
        "downweight_factor": TEST_ONLY_DOWNWEIGHT_FACTOR,
        "clock": clock,
    }
    base.update(over)
    return ConsumerPolicy(**base)


def _directed_seats(carrier, calls=None):
    decision = PairSeatDecision(
        nomination_id=carrier.nomination_id,
        profile_id=carrier.profile_id,
        left_node_id=carrier.lo_node_id,
        left_version=carrier.lo_version,
        right_node_id=carrier.hi_node_id,
        right_version=carrier.hi_version,
        verdict=Verdict.CONFLICT,
        winner_node_id=carrier.lo_node_id,
        loser_node_id=carrier.hi_node_id,
        target_node_ids=(carrier.lo_node_id, carrier.hi_node_id),
        evidence_event_ids=tuple(carrier.evidence_event_ids),
    )

    def run(nomination):
        if calls is not None:
            calls.append("seat")
        return (SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,)),)

    return run


def _timeout_seats(calls=None):
    def run(nomination):
        if calls is not None:
            calls.append("seat")
        return (SeatOutcome(status=SeatStatus.TIMEOUT),)

    return run


def _reconciliation_tables(conn, store: str):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'reconcil%'"
    ).fetchall()
    return {row[0] for row in rows}


class Crash(BaseException):
    pass


def test_restart_after_reserve_crash_before_seat_no_refund(tmp_path: Path) -> None:
    with _opened(tmp_path) as (graph, meta):
        _seed_pair(graph)
        carrier = _nominate(graph, meta)

        def crash_reserve(*args, **kwargs):
            raise Crash("reserved then crashed before the seat call")

        meta.reserve_attempt = crash_reserve
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1000.0), seats=_timeout_seats()
        )
        with pytest.raises(Crash):
            consumer.process(PROFILE, dream_run_id="run-1")
    with _opened(tmp_path) as (graph, meta):
        attempts = meta.list_reconciliation_attempts(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert len(attempts) == 0
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1200.0), seats=_timeout_seats()
        )
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        ordinals = [
            a.attempt_ordinal
            for a in meta.list_reconciliation_attempts(
                profile_id=PROFILE, nomination_id=carrier.nomination_id
            )
        ]
        assert ordinals == [1]


def test_restart_after_reserve_before_seat_no_refund_or_model_call(tmp_path: Path) -> None:
    calls: list = []
    with _opened(tmp_path) as (graph, meta):
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1000.0), seats=_timeout_seats(calls)
        )

        def crash_after_seat(nomination):
            calls.append("seat")
            raise Crash("reserved and seat crashed before any result")

        consumer._seats = crash_after_seat
        with pytest.raises(Crash):
            consumer.process(PROFILE, dream_run_id="run-1")
        assert calls == ["seat"]
    with _opened(tmp_path) as (graph, meta):
        attempts = meta.list_reconciliation_attempts(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert len(attempts) == 1
        deferred = meta.audit_query(AuditFilter(action="reconcile_deferred"), Page(limit=50))
        assert deferred.items == []
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1200.0), seats=_timeout_seats(calls)
        )
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        assert calls == ["seat", "seat"]
        ordinals = [
            a.attempt_ordinal
            for a in meta.list_reconciliation_attempts(
                profile_id=PROFILE, nomination_id=carrier.nomination_id
            )
        ]
        assert ordinals == [1, 2]


def test_restart_before_nomination_recovers_exactly_once(tmp_path: Path) -> None:
    with _opened(tmp_path) as (graph, meta):
        _seed_pair(graph)
    with _opened(tmp_path) as (graph, meta):
        report = nominate.materialize_nominations(graph, meta, PROFILE, clock=lambda: 1000.0)
        assert len(report.appended) == 1
    with _opened(tmp_path) as (graph, meta):
        again = nominate.materialize_nominations(graph, meta, PROFILE, clock=lambda: 1000.0)
        assert again.appended == ()
        assert len(again.duplicated) == 1
        page = meta.query_reconciliation_nominations(profile_id=PROFILE, limit=10, cursor=None)
        assert len(page.items) == 1


def test_restart_after_nomination_preserves_identity(tmp_path: Path) -> None:
    with _opened(tmp_path) as (graph, meta):
        _seed_pair(graph)
        first = _nominate(graph, meta)
    with _opened(tmp_path) as (graph, meta):
        page = meta.query_reconciliation_nominations(profile_id=PROFILE, limit=10, cursor=None)
        assert [item.nomination_id for item in page.items] == [first.nomination_id]
        evidence = meta.read_reconciliation_evidence(profile_id=PROFILE, nomination_id=first.nomination_id)
        assert evidence is not None and len(evidence) == 2


def test_restart_after_reservation_does_not_refund_budget(tmp_path: Path) -> None:
    with _opened(tmp_path) as (graph, meta):
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1000.0), seats=_timeout_seats()
        )
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
    with _opened(tmp_path) as (graph, meta):
        attempts = meta.list_reconciliation_attempts(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert [a.attempt_ordinal for a in attempts] == [1]
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1200.0), seats=_timeout_seats()
        )
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        ordinals = [
            a.attempt_ordinal
            for a in meta.list_reconciliation_attempts(
                profile_id=PROFILE, nomination_id=carrier.nomination_id
            )
        ]
        assert ordinals == [1, 2]


def test_restart_after_model_readjudicates_without_result_store(tmp_path: Path) -> None:
    calls = []
    with _opened(tmp_path) as (graph, meta):
        _seed_pair(graph)
        carrier = _nominate(graph, meta)

        def crash_apply(application, **kwargs):
            assert application.disposition == "accepted"
            raise Crash("typed result produced, apply not committed")

        graph.apply_reconciliation = crash_apply
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1000.0), seats=_directed_seats(carrier, calls)
        )
        with pytest.raises(Crash):
            consumer.process(PROFILE, dream_run_id="run-1")
        assert calls == ["seat"]
    with _opened(tmp_path) as (graph, meta):
        consumer = ReconciliationConsumer(
            graph=graph,
            meta=meta,
            policy=_policy(lambda: 1200.0),
            seats=_directed_seats(carrier, calls),
        )
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        assert calls == ["seat", "seat"]
        assert len(graph.versions("na")) == 1
        assert len(graph.versions("nb")) == 2
        assert _reconciliation_tables(graph._conn, "graph") == {
            "reconciliation_receipts",
            "reconciliation_audit_outbox",
        }
        assert _reconciliation_tables(meta._conn, "meta") == {
            "reconcile_nominations",
            "reconciliation_attempts",
        }


def test_restart_after_receipt_repairs_without_readjudication(tmp_path: Path) -> None:
    with _opened(tmp_path) as (graph, meta):
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1000.0), seats=_directed_seats(carrier)
        )
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
    with _opened(tmp_path) as (graph, meta):
        calls: list = []
        consumer = ReconciliationConsumer(
            graph=graph,
            meta=meta,
            policy=_policy(lambda: 1200.0),
            seats=_timeout_seats(calls),
        )
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        assert calls == []
        assert len(graph.versions("nb")) == 2
        assert repair_reconciliation_audit(graph, meta, limit=10) == 1
        assert len(graph.pending_reconciliation_audits(10)) == 0


def test_restart_after_audit_before_ack_is_idempotent(tmp_path: Path) -> None:
    with _opened(tmp_path) as (graph, meta):
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1000.0), seats=_directed_seats(carrier)
        )
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1

        def crash_ack(outbox_id, at):
            raise Crash("audit committed, ack lost")

        graph.mark_reconciliation_audit_delivered = crash_ack
        with pytest.raises(Crash):
            repair_reconciliation_audit(graph, meta, limit=10)
    with _opened(tmp_path) as (graph, meta):
        assert repair_reconciliation_audit(graph, meta, limit=10) == 1
        assert len(graph.pending_reconciliation_audits(10)) == 0
        assert repair_reconciliation_audit(graph, meta, limit=10) == 0


def test_restart_after_deferred_preserves_backoff_and_cap(tmp_path: Path) -> None:
    with _opened(tmp_path) as (graph, meta):
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1000.0), seats=_timeout_seats()
        )
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
    with _opened(tmp_path) as (graph, meta):
        attempts = meta.list_reconciliation_attempts(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert len(attempts) == 1 and attempts[0].next_eligible_at == 1060.0
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1200.0), seats=_timeout_seats()
        )
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        ordinals = [
            a.attempt_ordinal
            for a in meta.list_reconciliation_attempts(
                profile_id=PROFILE, nomination_id=carrier.nomination_id
            )
        ]
        assert ordinals == [1, 2]
