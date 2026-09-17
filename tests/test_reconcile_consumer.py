"""Bounded dormant reconciliation consumer: serialization, bounds, backoff, exhaustion, disabled proofs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemoseed_local.dream import consumer_enabled
from mnemoseed_local.dream.adjudicate import (
    PairSeatDecision,
    SeatOutcome,
    SeatStatus,
    Verdict,
)
from mnemoseed_local.dream.consumer import ConsumerPolicy, ReconciliationConsumer
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


def _node(
    node_id: str,
    *,
    profile_id: str = PROFILE,
    weight: float = 0.8,
    version: int = 1,
    never_decay: bool = False,
) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=profile_id,
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
        version=version,
        decay_weight=weight,
        never_decay=never_decay,
    )


def _stores(tmp_path: Path) -> tuple[SqliteGraphDriver, SqliteMetaDriver]:
    return SqliteGraphDriver(tmp_path / "graph.db"), SqliteMetaDriver(tmp_path / "meta.db")


def _close(*stores) -> None:
    for store in stores:
        asyncio.run(store.close())


def _seed_pair(graph: SqliteGraphDriver, a: str = "na", b: str = "nb") -> None:
    graph.upsert_node(_node(a))
    graph.upsert_node(_node(b))
    graph.set_read_conflict(a, b)


def _nominate(graph: SqliteGraphDriver, meta: SqliteMetaDriver, a: str = "na", b: str = "nb"):
    meta.append_reconcile_nomination(
        NominationRequest(
            profile_id=PROFILE,
            canonical_kind="read_conflict",
            node_a=a,
            version_a=1,
            expected_peer_a=b,
            node_b=b,
            version_b=1,
            expected_peer_b=a,
            source_generation=1,
            observed_at=1000.0,
            evidence=(
                EvidencePointer(kind=EvidenceKind.NODE, id=a),
                EvidencePointer(kind=EvidenceKind.NODE, id=b),
            ),
            source_channels=("read_conflict_flag",),
        )
    )
    page = meta.query_reconciliation_nominations(profile_id=PROFILE, limit=10, cursor=None)
    return next(item for item in page.items if {item.lo_node_id, item.hi_node_id} == {a, b})


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


def _consumer(graph, meta, policy, seats=None) -> ReconciliationConsumer:
    return ReconciliationConsumer(graph=graph, meta=meta, policy=policy, seats=seats)


def _decision_for(carrier, verdict: Verdict, winner=None, loser=None) -> PairSeatDecision:
    return PairSeatDecision(
        nomination_id=carrier.nomination_id,
        profile_id=carrier.profile_id,
        left_node_id=carrier.lo_node_id,
        left_version=carrier.lo_version,
        right_node_id=carrier.hi_node_id,
        right_version=carrier.hi_version,
        verdict=verdict,
        winner_node_id=winner,
        loser_node_id=loser,
        target_node_ids=(carrier.lo_node_id, carrier.hi_node_id),
        evidence_event_ids=tuple(carrier.evidence_event_ids),
    )


def _seats_for(*decisions, calls=None):
    def run(nomination):
        if calls is not None:
            calls.append("seat")
        return (SeatOutcome(status=SeatStatus.COMPLETE, decisions=decisions),)

    return run


def _timeout_seats(calls=None):
    def run(nomination):
        if calls is not None:
            calls.append("seat")
        return (SeatOutcome(status=SeatStatus.TIMEOUT),)

    return run


def _attempts(meta, nomination_id: str):
    return meta.list_reconciliation_attempts(profile_id=PROFILE, nomination_id=nomination_id)


def _deferred_audits(meta):
    return meta.audit_query(AuditFilter(action="reconcile_deferred"), Page(offset=0, limit=50))


def test_same_profile_jobs_do_not_overlap_consumer_and_merge(tmp_path: Path) -> None:
    from test_dream_completion_seam import exercise_queued_merge

    exercise_queued_merge(tmp_path)


def test_profiles_do_not_share_nomination_retry_state(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    try:
        graph.upsert_node(_node("na", profile_id="p1"))
        graph.upsert_node(_node("nb", profile_id="p1"))
        graph.set_read_conflict("na", "nb")
        graph.upsert_node(_node("ma", profile_id="p2"))
        graph.upsert_node(_node("mb", profile_id="p2"))
        graph.set_read_conflict("ma", "mb")
        meta.append_reconcile_nomination(
            NominationRequest(
                profile_id="p1",
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
        meta.append_reconcile_nomination(
            NominationRequest(
                profile_id="p2",
                canonical_kind="read_conflict",
                node_a="ma",
                version_a=1,
                expected_peer_a="mb",
                node_b="mb",
                version_b=1,
                expected_peer_b="ma",
                source_generation=1,
                observed_at=1000.0,
                evidence=(
                    EvidencePointer(kind=EvidenceKind.NODE, id="ma"),
                    EvidencePointer(kind=EvidenceKind.NODE, id="mb"),
                ),
                source_channels=("read_conflict_flag",),
            )
        )
        first = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1000.0), seats=_timeout_seats()
        )
        second = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(lambda: 1000.0), seats=_timeout_seats()
        )
        assert first.process("p1", dream_run_id="run-1") == 1
        assert second.process("p2", dream_run_id="run-1") == 1
        first_id = next(
            item.nomination_id
            for item in meta.query_reconciliation_nominations(profile_id="p1", limit=10, cursor=None).items
        )
        second_id = next(
            item.nomination_id
            for item in meta.query_reconciliation_nominations(profile_id="p2", limit=10, cursor=None).items
        )
        p1_attempts = meta.list_reconciliation_attempts(profile_id="p1", nomination_id=first_id)
        p2_attempts = meta.list_reconciliation_attempts(profile_id="p2", nomination_id=second_id)
        assert len(p1_attempts) == 1 and len(p2_attempts) == 1
        assert graph.get_node("na").read_conflict_id == "nb"
        assert graph.get_node("ma").read_conflict_id == "mb"
    finally:
        _close(graph, meta)


def test_nomination_cap_bounds_attempts(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph, "na", "nb")
        _seed_pair(graph, "nc", "nd")
        first = _nominate(graph, meta, "na", "nb")
        second = _nominate(graph, meta, "nc", "nd")
        consumer = _consumer(graph, meta, _policy(lambda: 1000.0, nomination_limit=1), _timeout_seats())
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
        handled = [first.nomination_id, second.nomination_id]
        counts = {nid: len(_attempts(meta, nid)) for nid in handled}
        assert sorted(counts.values()) == [0, 1]
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        assert all(len(_attempts(meta, nid)) == 1 for nid in handled)
    finally:
        _close(graph, meta)


def test_scan_budget_bounds_terminal_prefix_work(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph, "na", "nb")
        _seed_pair(graph, "nc", "nd")
        _seed_pair(graph, "ne", "nf")
        ids = [
            _nominate(graph, meta, "na", "nb").nomination_id,
            _nominate(graph, meta, "nc", "nd").nomination_id,
            _nominate(graph, meta, "ne", "nf").nomination_id,
        ]
        consumer = _consumer(graph, meta, _policy(lambda: 1000.0, scan_limit=1), _timeout_seats())
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
        assert sum(len(_attempts(meta, nid)) for nid in ids) == 1
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        assert sum(len(_attempts(meta, nid)) for nid in ids) == 2
    finally:
        _close(graph, meta)


@pytest.mark.parametrize("scan_limit,nomination_limit", [(1, 10), (10, 1), (2, 3)])
def test_cursor_sweep_skips_backoff_without_starving_tail(tmp_path, scan_limit, nomination_limit):
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph, "na", "nb")
        _seed_pair(graph, "nc", "nd")
        _nominate(graph, meta, "na", "nb")
        _nominate(graph, meta, "nc", "nd")
        query = meta.query_reconciliation_nominations
        carriers = query(profile_id=PROFILE, limit=10, cursor=None).items
        first, second = carriers
        meta.reserve_attempt(
            profile_id=PROFILE,
            nomination_id=first.nomination_id,
            dream_run_id="prior",
            attempt_limit=3,
            reserved_at=1000.0,
            next_eligible_at=1060.0,
        )
        queries = []

        def page(*, profile_id, limit, cursor):
            queries.append((limit, cursor))
            start = 0 if cursor is None else 1
            items = carriers[start : start + limit]
            return SimpleNamespace(items=items, next_cursor="opaque-tail" if start + limit < 2 else None)

        meta.query_reconciliation_nominations = page
        now = [1000.0]
        calls = []
        consumer = _consumer(
            graph,
            meta,
            _policy(lambda: now[0], scan_limit=scan_limit, nomination_limit=nomination_limit),
            _timeout_seats(calls),
        )
        if scan_limit == 1:
            assert consumer.process(PROFILE, dream_run_id="pass-1") == 0
            assert queries == [(1, None)]
            assert calls == []
            assert not _attempts(meta, second.nomination_id)
            assert consumer.process(PROFILE, dream_run_id="pass-2") == 1
            assert queries == [(1, None), (1, "opaque-tail")]
        else:
            assert consumer.process(PROFILE, dream_run_id="pass-1") == 1
            expected = [(1, None), (1, "opaque-tail")] if nomination_limit == 1 else [(2, None)]
            assert queries == expected
        assert calls == ["seat"]
        now[0] = 1200.0
        queries.clear()
        assert consumer.process(PROFILE, dream_run_id="next-sweep") >= 1
        assert queries[0] == (min(scan_limit, nomination_limit), None)
        assert len(_attempts(meta, first.nomination_id)) == 2
    finally:
        _close(graph, meta)


def test_partial_page_exception_preserves_incoming_cursor(tmp_path):
    graph, meta = _stores(tmp_path)
    try:
        for a, b in [("na", "nb"), ("nc", "nd"), ("ne", "nf")]:
            _seed_pair(graph, a, b)
            _nominate(graph, meta, a, b)
        carriers = meta.query_reconciliation_nominations(profile_id=PROFILE, limit=10, cursor=None).items
        queries = []

        def page(*, profile_id, limit, cursor):
            queries.append((limit, cursor))
            if cursor is None:
                return SimpleNamespace(items=carriers[:1], next_cursor="incoming")
            return SimpleNamespace(items=carriers[1:], next_cursor=None)

        meta.query_reconciliation_nominations = page
        lookup = graph.get_reconciliation_receipt
        fail = [True]

        def receipt(**kwargs):
            if kwargs["nomination_id"] == carriers[2].nomination_id and fail[0]:
                raise Crash("partial page")
            return lookup(**kwargs)

        graph.get_reconciliation_receipt = receipt
        consumer = _consumer(graph, meta, _policy(lambda: 1000.0, scan_limit=3), _timeout_seats())
        with pytest.raises(Crash, match="partial page"):
            consumer.process(PROFILE, dream_run_id="pass-1")
        assert queries == [(3, None), (2, "incoming")]
        fail[0] = False
        queries.clear()
        assert consumer.process(PROFILE, dream_run_id="pass-2") == 1
        assert queries == [(3, "incoming")]
    finally:
        _close(graph, meta)


def test_backoff_skips_without_model_call(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        now = [1000.0]
        calls: list = []
        consumer = _consumer(graph, meta, _policy(lambda: now[0]), _timeout_seats(calls))
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
        assert calls == ["seat"]
        assert len(_attempts(meta, carrier.nomination_id)) == 1
        now[0] = 1050.0
        assert consumer.process(PROFILE, dream_run_id="run-2") == 0
        assert calls == ["seat"]
        assert len(_attempts(meta, carrier.nomination_id)) == 1
        now[0] = 1200.0
        assert consumer.process(PROFILE, dream_run_id="run-3") == 1
        ordinals = [a.attempt_ordinal for a in _attempts(meta, carrier.nomination_id)]
        assert ordinals == [1, 2]
        assert calls == ["seat", "seat"]
    finally:
        _close(graph, meta)


def test_exhaustion_is_terminal_without_model_call(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        now = [1000.0]
        calls: list = []
        consumer = _consumer(graph, meta, _policy(lambda: now[0], attempt_limit=1), _timeout_seats(calls))
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
        assert calls == ["seat"]
        now[0] = 1200.0
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        assert calls == ["seat"]
        receipt = graph.get_reconciliation_receipt(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert receipt is not None
        assert receipt.disposition == "unresolved"
        assert receipt.reason_code == "retry_exhausted"
        assert len(graph.versions("na")) == 1
        assert len(graph.versions("nb")) == 1
        pending = graph.pending_reconciliation_audits(10)
        assert len(pending) == 1 and pending[0].action == "reconcile_unresolved"
    finally:
        _close(graph, meta)


@pytest.mark.parametrize("crash", [False, True])
def test_same_snapshot_retry_cap_cannot_reuse_reservation(tmp_path: Path, crash: bool) -> None:
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        now = [1000.0]
        calls = []
        snapshot_run_id = 'snapshot-["same",1]'

        def seats(nomination):
            calls.append("seat")
            assert len(_attempts(meta, carrier.nomination_id)) == 1
            if crash and len(calls) == 1:
                raise Crash("seat crashed after reservation")
            return _timeout_seats()(nomination)

        consumer = _consumer(graph, meta, _policy(lambda: now[0], attempt_limit=1), seats)
        if crash:
            with pytest.raises(Crash):
                consumer.process(PROFILE, dream_run_id=snapshot_run_id)
        else:
            assert consumer.process(PROFILE, dream_run_id=snapshot_run_id) == 1
        assert calls == ["seat"]
        now[0] = 1200.0
        assert consumer.process(PROFILE, dream_run_id=snapshot_run_id) == 1
        assert calls == ["seat"]
        assert [a.attempt_ordinal for a in _attempts(meta, carrier.nomination_id)] == [1]
        receipt = graph.get_reconciliation_receipt(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert receipt is not None
        assert receipt.disposition == "unresolved"
        assert receipt.reason_code == "retry_exhausted"
        assert consumer.process(PROFILE, dream_run_id=snapshot_run_id) == 1
        assert calls == ["seat"]
    finally:
        _close(graph, meta)


def test_replayed_reservation_never_calls_seats(tmp_path: Path) -> None:
    from mnemoseed_local.storage.ports import AttemptReservationOutcome, AttemptReservationResult

    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        reserved = meta.reserve_attempt(
            profile_id=PROFILE,
            nomination_id=carrier.nomination_id,
            dream_run_id="prior-owner",
            attempt_limit=1,
            reserved_at=1000.0,
            next_eligible_at=1060.0,
        )
        reservations = []

        def replay(**kwargs):
            reservations.append(kwargs)
            return AttemptReservationResult(AttemptReservationOutcome.RESERVED, reserved.reservation, False)

        meta.reserve_attempt = replay
        consumer = _consumer(graph, meta, _policy(lambda: 1200.0, attempt_limit=1), bomb_seats)
        assert consumer.process(PROFILE, dream_run_id="same-snapshot") == 0
        assert len(reservations) == 1
        assert len(_attempts(meta, carrier.nomination_id)) == 1
        assert _deferred_audits(meta).items == []
        assert (
            graph.get_reconciliation_receipt(profile_id=PROFILE, nomination_id=carrier.nomination_id) is None
        )
    finally:
        _close(graph, meta)


def test_disabled_consumer_performs_no_storage_or_provider_calls() -> None:
    class _Bomb:
        def __getattr__(self, name: str):
            raise AssertionError(f"store must not be touched: {name}")

    def _bomb_seats(nomination):
        raise AssertionError("seats must not run")

    policy = ConsumerPolicy(
        nomination_limit=None,
        scan_limit=None,
        audit_repair_limit=None,
        attempt_limit=None,
        retry_backoff_seconds=None,
        seat_mode="verify",
        downweight_factor=None,
        clock=lambda: 1000.0,
    )
    consumer = ReconciliationConsumer(graph=_Bomb(), meta=_Bomb(), policy=policy, seats=_bomb_seats)
    assert consumer.process(PROFILE, dream_run_id="run-1") == 0
    assert consumer.repair(PROFILE) == 0


@pytest.mark.parametrize("ensemble_mode", ["off", "verify", "vote", "unknown_mode"])
@pytest.mark.parametrize("channel_enabled", [False, True])
@pytest.mark.parametrize("ratified", [False, True])
def test_disabled_posture_matrix_never_runs_with_defaults(ensemble_mode, channel_enabled, ratified, tmp_path):
    class _Counting:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.calls = 0

        def __getattr__(self, name):
            if name == "calls":
                raise AttributeError

            def record(*args, **kwargs):
                self.calls += 1
                return getattr(self._wrapped, name)(*args, **kwargs)

            return record

    graph, meta = _stores(tmp_path / "posture")
    try:
        _seed_pair(graph)
        _nominate(graph, meta)
        counting_graph = _Counting(graph)
        counting_meta = _Counting(meta)
        policy = ConsumerPolicy(
            nomination_limit=None,
            scan_limit=None,
            audit_repair_limit=None,
            attempt_limit=None,
            retry_backoff_seconds=None,
            attempt_timeout_seconds=None,
            seat_mode="verify",
            downweight_factor=None,
            clock=lambda: 1000.0,
        )
        consumer = ReconciliationConsumer(
            graph=counting_graph, meta=counting_meta, policy=policy, seats=bomb_seats
        )
        consumer.coordinate(
            SimpleNamespace(profile_id=PROFILE, dream_run_id="run"),
            ratified=ratified,
            configured=False,
            ensemble_active=ensemble_mode != "off",
        )
        assert counting_graph.calls == 0
        assert counting_meta.calls == 0
    finally:
        _close(graph, meta)


class Crash(BaseException):
    pass


def bomb_seats(nomination):
    raise Crash("seats must not run")


@pytest.mark.parametrize(
    "field",
    [
        "nomination_limit",
        "scan_limit",
        "audit_repair_limit",
        "attempt_limit",
        "retry_backoff_seconds",
        "downweight_factor",
        "attempt_timeout_seconds",
    ],
)
def test_each_unratified_field_disables_all_work(field):
    class Bomb:
        def __getattr__(self, name):
            raise Crash(f"unexpected storage access: {name}")

    consumer = _consumer(Bomb(), Bomb(), _policy(lambda: 1000.0, **{field: None}), bomb_seats)
    assert consumer.process(PROFILE, dream_run_id="run") == 0
    assert consumer.repair(PROFILE) == 0


@pytest.mark.parametrize("fate", ["protected", "missing", "closed", "version", "overwritten", "cleared"])
def test_terminal_fate_never_calls_seats(tmp_path, fate):
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        node = graph.get_node("nb")
        if fate == "protected":
            graph.upsert_node(node.model_copy(update={"never_decay": True}))
        elif fate == "missing":
            original = graph.get_node
            graph.get_node = lambda node_id: None if node_id == "nb" else original(node_id)
        elif fate == "closed":
            graph.upsert_node(node.model_copy(update={"valid_to": 1100.0}))
        elif fate == "version":
            graph.append_version(node.model_copy(update={"version": 2}))
        elif fate == "overwritten":
            graph.upsert_node(_node("nc"))
            graph.set_read_conflict("nb", "nc")
        else:
            graph.clear_read_conflict("nb")
        consumer = _consumer(graph, meta, _policy(lambda: 1200.0), bomb_seats)
        assert consumer.process(PROFILE, dream_run_id="run") == 1
        receipt = graph.get_reconciliation_receipt(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert receipt.disposition == "unresolved"
        assert _deferred_audits(meta).items == []
    finally:
        _close(graph, meta)


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("exhausted", [False, True])
def test_apply_failure_checks_receipt_before_deferred(tmp_path, committed, exhausted):
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        if exhausted:
            meta.reserve_attempt(
                profile_id=PROFILE,
                nomination_id=carrier.nomination_id,
                dream_run_id="old",
                attempt_limit=1,
                reserved_at=1000.0,
                next_eligible_at=1060.0,
            )
        original = graph.apply_reconciliation

        def apply(*args, **kwargs):
            if committed:
                original(*args, **kwargs)
            raise RuntimeError("apply boundary")

        graph.apply_reconciliation = apply
        consumer = _consumer(
            graph,
            meta,
            _policy(lambda: 1200.0, attempt_limit=1),
            _seats_for(_decision_for(carrier, Verdict.NOT_CONFLICT)),
        )
        assert consumer.process(PROFILE, dream_run_id="run") == 1
        assert len(_deferred_audits(meta).items) == (0 if committed else 1)
        assert bool(graph.pending_reconciliation_audits(10)) == committed
    finally:
        _close(graph, meta)


def test_reason_code_closed_set_and_reexport_pin() -> None:
    import enum

    from mnemoseed_local.dream import consumer
    from mnemoseed_local.storage.ports import ReasonCode

    assert ReasonCode.RETRY_EXHAUSTED == "retry_exhausted"
    assert consumer.ReasonCode is ReasonCode
    assert {member.name: member.value for member in ReasonCode} == {
        "CONFLICT_CONFIRMED": "conflict_confirmed",
        "NOT_CONFLICT_CONFIRMED": "not_conflict_confirmed",
        "INSUFFICIENT": "insufficient",
        "ENSEMBLE_OFF": "ensemble_off",
        "UNEXPECTED_ENSEMBLE_MODE": "unexpected_ensemble_mode",
        "IDENTITY_MISMATCH": "identity_mismatch",
        "STALE_REVISION": "stale_revision",
        "PROTECTED_ENDPOINT": "protected_endpoint",
        "DECAY_WEIGHT_NOT_LOWERABLE": "decay_weight_not_lowerable",
        "MISSING_ENDPOINT": "missing_endpoint",
        "CLOSED_ENDPOINT": "closed_endpoint",
        "TOMBSTONED_ENDPOINT": "tombstoned_endpoint",
        "ABSENT_RESULT": "absent_result",
        "SEAT_TIMEOUT": "seat_timeout",
        "SEAT_UNAVAILABLE": "seat_unavailable",
        "INVALID_TYPED_OUTPUT": "invalid_typed_output",
        "COLLAPSE_UNRECOVERED": "collapse_unrecovered",
        "SINGLE_SIDE": "single_side",
        "SALVAGE": "salvage",
        "POLARITY_DROP": "polarity_drop",
        "CONFLICT_WITHOUT_DIRECTION": "conflict_without_direction",
        "SEAT_DISAGREEMENT": "seat_disagreement",
        "EMPTY_RESPONSE": "empty_response",
        "DUPLICATE_RESPONSE": "duplicate_response",
        "EXTRA_RESPONSE": "extra_response",
        "BOTH_SUPPORTED_WITHOUT_VERDICT": "both_supported_without_explicit_not_conflict",
        "LEGACY_DIAGNOSTIC_ONLY": "legacy_diagnostic_only",
        "RETRY_EXHAUSTED": "retry_exhausted",
    }
    assert issubclass(ReasonCode, enum.StrEnum)


def test_deferred_audit_failure_is_not_counted_as_processed(tmp_path):
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)

        def audit_fault(entry):
            raise RuntimeError("audit store unavailable")

        meta.audit_append = audit_fault
        consumer = _consumer(graph, meta, _policy(lambda: 1200.0), _timeout_seats())
        assert consumer.process(PROFILE, dream_run_id="run") == 0
        assert len(_attempts(meta, carrier.nomination_id)) == 1
        assert (
            graph.get_reconciliation_receipt(profile_id=PROFILE, nomination_id=carrier.nomination_id) is None
        )
        assert graph.pending_reconciliation_audits(10) == []
    finally:
        _close(graph, meta)


def test_receipt_lookup_failure_does_not_guess_absent(tmp_path):
    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        original_lookup = graph.get_reconciliation_receipt
        looked_up = []

        def lookup(*args, **kwargs):
            result = original_lookup(*args, **kwargs)
            looked_up.append(result)
            if looked_up.count(result) > 1:
                raise Crash("receipt lookup failed inside the apply boundary")
            return result

        graph.get_reconciliation_receipt = lookup

        def apply(*args, **kwargs):
            raise RuntimeError("apply boundary")

        graph.apply_reconciliation = apply
        consumer = _consumer(
            graph,
            meta,
            _policy(lambda: 1200.0, attempt_limit=1),
            _seats_for(_decision_for(carrier, Verdict.NOT_CONFLICT)),
        )
        with pytest.raises(Crash):
            consumer.process(PROFILE, dream_run_id="run")
        assert _deferred_audits(meta).items == []
        assert len(_attempts(meta, carrier.nomination_id)) == 1
        assert len(graph.versions("na")) == 1
    finally:
        _close(graph, meta)


def test_consumer_enabled_requires_all_four() -> None:
    assert consumer_enabled(ratified=True, configured=True, ensemble_active=True, policy_ok=True)
    assert not consumer_enabled(ratified=False, configured=True, ensemble_active=True, policy_ok=True)
    assert not consumer_enabled(ratified=True, configured=False, ensemble_active=True, policy_ok=True)
    assert not consumer_enabled(ratified=True, configured=True, ensemble_active=False, policy_ok=True)
    assert not consumer_enabled(ratified=True, configured=True, ensemble_active=True, policy_ok=False)


def test_production_wiring_never_binds_consumer_before_ratification() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "mnemoseed_local"
    watched = ["dream/pipeline.py", "dream/trigger.py", "dream/merge.py", "daemon/app.py"]
    for relative in watched:
        text = (root / relative).read_text(encoding="utf-8")
        assert "ReconciliationConsumer" not in text, relative
        assert "repair_reconciliation_audit" not in text, relative
        assert "consumer_enabled" not in text, relative


def test_safe_clear_preserves_deferred_ledger_and_node_evidence(tmp_path: Path) -> None:
    from test_dream_completion_seam import seam_snapshot

    from mnemoseed_local.dream.snapshot import FileSnapshotter, load_snapshot_file, write_snapshot_file
    from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore

    graph, meta = _stores(tmp_path)
    vector = LanceDbEmbeddedStore(tmp_path / "vectors", dimensions=2)
    try:
        _seed_pair(graph)
        carrier = _nominate(graph, meta)
        evidence = meta.read_reconciliation_evidence(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        nodes = [graph.get_node(nid) for nid in ("na", "nb")]
        snap = seam_snapshot(PROFILE)
        for chunk in snap.chunks:
            vector.upsert_chunk(chunk.to_stamp(), [1.0, 0.0])
        marked = []
        mark = vector.mark_consolidated

        def record_mark(ids):
            marked.append(list(ids))
            mark(ids)

        vector.mark_consolidated = record_mark
        journal = tmp_path / "journal"
        fs = FileSnapshotter(store=vector, meta=meta, directory=journal)
        fs.adopt(snap)
        write_snapshot_file(journal, snap)
        consumer = _consumer(graph, meta, _policy(lambda: 1000.0), _timeout_seats())
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
        attempts = _attempts(meta, carrier.nomination_id)
        audits = _deferred_audits(meta).items
        assert len(attempts) == len(audits) == 1
        assert fs.purge_snapshot(PROFILE, snap.turn_range) == 1
        assert marked == [["c1"]]
        _close(graph, meta, vector)
        graph, meta = _stores(tmp_path)
        vector = LanceDbEmbeddedStore(tmp_path / "vectors", dimensions=2)
        reopened = load_snapshot_file(journal / "fixed-id.json")
        assert reopened == snap.with_phase("merge_done")
        fs = FileSnapshotter(store=vector, meta=meta, directory=journal)
        fs.adopt(reopened)
        assert fs.recover() == []
        assert fs.purge_snapshot(PROFILE, snap.turn_range) == 0
        for chunk in snap.chunks:
            assert (
                vector.get_chunk(chunk.chunk_id).model_dump()
                == chunk.to_stamp()
                .model_copy(update={"consolidated": chunk.chunk_id == "c1", "last_reinforced": 1000.0})
                .model_dump()
            )
        assert (
            meta.read_reconciliation_evidence(profile_id=PROFILE, nomination_id=carrier.nomination_id)
            == evidence
        )
        assert meta.query_reconciliation_nominations(profile_id=PROFILE, limit=10, cursor=None).items == (
            carrier,
        )
        assert _attempts(meta, carrier.nomination_id) == attempts
        assert _deferred_audits(meta).items == audits
        assert [graph.get_node(nid) for nid in ("na", "nb")] == nodes
        calls = []
        now = [1050.0]
        consumer = _consumer(graph, meta, _policy(lambda: now[0]), _timeout_seats(calls))
        assert consumer.process(PROFILE, dream_run_id="run-2") == 0
        assert calls == []
        now[0] = 1200.0
        assert consumer.process(PROFILE, dream_run_id="run-3") == 1
        assert calls == ["seat"]
        assert [attempt.attempt_ordinal for attempt in _attempts(meta, carrier.nomination_id)] == [1, 2]
        assert len(_deferred_audits(meta).items) == 2
        assert (
            meta.read_reconciliation_evidence(profile_id=PROFILE, nomination_id=carrier.nomination_id)
            == evidence
        )
    finally:
        _close(graph, meta, vector)


def test_clear_affected_evidence_without_pinning_is_not_retryable(tmp_path: Path) -> None:
    from mnemoseed_local.storage.ports import StorageError

    graph, meta = _stores(tmp_path)
    try:
        _seed_pair(graph)
        _nominate(graph, meta)
        _seed_pair(graph, "nc", "nd")
        other = _nominate(graph, meta, "nc", "nd")
        meta._conn.execute(
            "INSERT INTO reconcile_nominations (nomination_id, profile_id, "
            "canonical_kind, composite_group_id, source_generation, lo_node_id, "
            "hi_node_id, lo_version, hi_version, lo_expected_peer, "
            "hi_expected_peer, evidence_event_ids, source_channels, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "corrupt",
                PROFILE,
                "read_conflict",
                "nom-corrupt",
                1,
                "na",
                "nb",
                1,
                1,
                "nb",
                "na",
                f"[{other.evidence_event_ids[0]}]",
                '["read_conflict_flag"]',
                "2026-09-17T00:00:00Z",
            ),
        )
        meta._conn.commit()
        consumer = _consumer(graph, meta, _policy(lambda: 1000.0), _timeout_seats())
        with pytest.raises(StorageError):
            consumer.process(PROFILE, dream_run_id="run-1")
        assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 0
        assert len(graph.versions("na")) == 1
        assert len(graph.versions("nb")) == 1
    finally:
        _close(graph, meta)


def test_protected_terminal_is_not_readjudicated_or_reminted(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    try:
        graph.upsert_node(_node("na"))
        graph.upsert_node(_node("nb", never_decay=True))
        graph.set_read_conflict("na", "nb")
        carrier = _nominate(graph, meta)
        calls: list = []
        consumer = _consumer(graph, meta, _policy(lambda: 1000.0), _timeout_seats(calls))
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
        receipt = graph.get_reconciliation_receipt(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert receipt is not None
        assert receipt.disposition == "unresolved"
        assert receipt.reason_code == "protected_endpoint"
        assert len(graph.versions("na")) == 1
        assert len(graph.versions("nb")) == 1
        assert graph.get_node("na").read_conflict_id == "nb"
        assert calls == []
        assert consumer.process(PROFILE, dream_run_id="run-2") == 1
        assert calls == []
        assert len(graph.versions("nb")) == 1
    finally:
        _close(graph, meta)
