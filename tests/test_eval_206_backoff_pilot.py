from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mnemoseed_local.dream.adjudicate import PairSeatDecision, SeatOutcome, SeatStatus, Verdict
from mnemoseed_local.dream.consumer import ConsumerPolicy, ReconciliationConsumer
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import Provenance
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import EvidenceKind, EvidencePointer, NominationRequest

PROFILE = "pilot"
NOW = 1000.0
BACKOFF = 900.0


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> float:
        return self.value


def _node(node_id: str, *, never_decay: bool = False) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=PROFILE,
        node_type=NodeType.PREFERENCE,
        entities=["pilot"],
        props={
            "domain": "pilot",
            "statement": node_id,
            "valence": 0.4,
            "prior_width": 0.2,
            "trait_anchor": "pilot",
            "evidence_chain": [{"chunk_id": node_id}],
        },
        provenance=Provenance(asserted_by="pilot", source="session://pilot"),
        valid_from=NOW,
        never_decay=never_decay,
    )


def _stores(tmp_path: Path) -> tuple[SqliteGraphDriver, SqliteMetaDriver]:
    return SqliteGraphDriver(tmp_path / "graph.db"), SqliteMetaDriver(tmp_path / "meta.db")


def _close(*stores: object) -> None:
    for store in stores:
        asyncio.run(store.close())


def _nominate(graph: SqliteGraphDriver, meta: SqliteMetaDriver, index: int):
    left, right = f"left-{index}", f"right-{index}"
    graph.upsert_node(_node(left))
    graph.upsert_node(_node(right))
    graph.set_read_conflict(left, right)
    meta.append_reconcile_nomination(
        NominationRequest(
            profile_id=PROFILE,
            canonical_kind="read_conflict",
            node_a=left,
            version_a=1,
            expected_peer_a=right,
            node_b=right,
            version_b=1,
            expected_peer_b=left,
            source_generation=1,
            observed_at=NOW,
            evidence=(
                EvidencePointer(kind=EvidenceKind.NODE, id=left),
                EvidencePointer(kind=EvidenceKind.NODE, id=right),
            ),
            source_channels=("read_conflict_flag",),
        )
    )
    return meta.query_reconciliation_nominations(profile_id=PROFILE, limit=50, cursor=None).items[-1]


def _policy(clock: Clock) -> ConsumerPolicy:
    return ConsumerPolicy(
        nomination_limit=50,
        scan_limit=50,
        audit_repair_limit=50,
        attempt_limit=3,
        retry_backoff_seconds=BACKOFF,
        attempt_timeout_seconds=30.0,
        seat_mode="verify",
        downweight_factor=0.5,
        clock=clock,
    )


def _success(carrier):
    return PairSeatDecision(
        nomination_id=carrier.nomination_id,
        profile_id=PROFILE,
        left_node_id=carrier.lo_node_id,
        left_version=1,
        right_node_id=carrier.hi_node_id,
        right_version=1,
        verdict=Verdict.CONFLICT,
        winner_node_id=carrier.lo_node_id,
        loser_node_id=carrier.hi_node_id,
        target_node_ids=(carrier.lo_node_id, carrier.hi_node_id),
        evidence_event_ids=tuple(carrier.evidence_event_ids),
    )


def _seats(status: SeatStatus, carrier, calls: list[str], success_after: int | None = None):
    def run(_nomination):
        calls.append(carrier.nomination_id)
        if success_after is not None and len(calls) >= success_after:
            return (SeatOutcome(status=SeatStatus.COMPLETE, decisions=(_success(carrier),)),)
        return (SeatOutcome(status=status),)

    return run


def test_pilot_freezes_exact_900_second_eligibility_and_idempotent_audit(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    try:
        carrier = _nominate(graph, meta, 0)
        clock = Clock()
        calls: list[str] = []
        consumer = ReconciliationConsumer(
            graph=graph, meta=meta, policy=_policy(clock), seats=_seats(SeatStatus.TIMEOUT, carrier, calls)
        )
        assert consumer.process(PROFILE, dream_run_id="run-0") == 1
        attempt = meta.list_reconciliation_attempts(profile_id=PROFILE, nomination_id=carrier.nomination_id)[
            0
        ]
        assert attempt.next_eligible_at == NOW + 900.0
        clock.value = NOW + BACKOFF - 1
        assert consumer.process(PROFILE, dream_run_id="run-1") == 0
        assert len(calls) == 1
        clock.value = NOW + BACKOFF
        assert consumer.process(PROFILE, dream_run_id="run-1") == 1
        assert consumer.process(PROFILE, dream_run_id="run-1") == 0
        assert len(calls) == 2
        assert (
            len(meta.list_reconciliation_attempts(profile_id=PROFILE, nomination_id=carrier.nomination_id))
            == 2
        )
    finally:
        _close(graph, meta)


PATTERNS = [
    "first-attempt-timeout-then-success",
    "repeated-timeout",
    "invalid-then-success",
    "seat-unavailable",
    "protected",
    "stale",
    "permanent-failure",
] * 2 + [
    "repeated-timeout",
    "protected",
    "stale",
    "permanent-failure",
    "seat-unavailable",
    "repeated-timeout",
]


@pytest.mark.parametrize("pattern", PATTERNS)
def test_twenty_nomination_pilot_patterns_are_bounded_and_nonblocking(
    tmp_path: Path, pattern: str, request: pytest.FixtureRequest
) -> None:
    graph, meta = _stores(tmp_path)
    try:
        clock = Clock()
        carrier = _nominate(graph, meta, request.node.callspec.indices["pattern"])
        calls: list[str] = []
        if pattern == "protected":
            node = graph.get_node(carrier.hi_node_id)
            assert node is not None
            graph.upsert_node(node.model_copy(update={"never_decay": True}))
        elif pattern == "stale":
            graph.append_version(graph.get_node(carrier.hi_node_id).model_copy(update={"version": 2}))
        status = SeatStatus.INVALID_OUTPUT if pattern == "invalid-then-success" else SeatStatus.TIMEOUT
        if pattern == "seat-unavailable":
            status = SeatStatus.UNAVAILABLE
        seats = _seats(
            status,
            carrier,
            calls,
            success_after=2
            if pattern in {"first-attempt-timeout-then-success", "invalid-then-success"}
            else None,
        )
        consumer = ReconciliationConsumer(graph=graph, meta=meta, policy=_policy(clock), seats=seats)
        for run in range(3):
            assert consumer.process(PROFILE, dream_run_id=f"{pattern}-{run}") in {0, 1}
            clock.value += BACKOFF
        assert len(calls) <= 3
        attempts = meta.list_reconciliation_attempts(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert len(attempts) <= 3
        receipt = graph.get_reconciliation_receipt(profile_id=PROFILE, nomination_id=carrier.nomination_id)
        assert receipt is not None or len(attempts) == 3
    finally:
        _close(graph, meta)
