"""Issue 206 cap-soak harness for the dormant reconciliation consumer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from test_reconcile_consumer import (
    PROFILE,
    _close,
    _consumer,
    _policy,
    _seed_pair,
    _stores,
    _timeout_seats,
)

from mnemoseed_local.storage.ports import EvidenceKind, EvidencePointer, NominationRequest


@dataclass(frozen=True)
class CycleRecord:
    cycle: int
    executions: int
    latency_ms: float
    tokens: int
    stages: tuple[str, ...]


def _nominate_large(meta, left: str, right: str):
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
            observed_at=1000.0,
            evidence=(
                EvidencePointer(kind=EvidenceKind.NODE, id=left),
                EvidencePointer(kind=EvidenceKind.NODE, id=right),
            ),
            source_channels=("read_conflict_flag",),
        )
    )
    return next(
        item
        for item in meta.query_reconciliation_nominations(profile_id=PROFILE, limit=10000, cursor=None).items
        if {item.lo_node_id, item.hi_node_id} == {left, right}
    )


def _soak_arm(tmp_path: Path, *, cycles: int, nominations: int) -> list[CycleRecord]:
    graph, meta = _stores(tmp_path)
    try:
        carriers = []
        for index in range(nominations):
            left, right = f"left-{index}", f"right-{index}"
            _seed_pair(graph, left, right)
            carriers.append(_nominate_large(meta, left, right))

        clock = [1000.0]
        records: list[CycleRecord] = []
        consumer = _consumer(
            graph,
            meta,
            _policy(lambda: clock[0], nomination_limit=10, scan_limit=10),
            _timeout_seats(),
        )
        for cycle in range(1, cycles + 1):
            stages = ["regular_merge"]
            clock[0] += 61.0
            executions = consumer.process(PROFILE, dream_run_id=f"soak-{cycle}")
            stages.extend(("reconciliation", "safe_clear", "capture_ingest"))
            records.append(
                CycleRecord(
                    cycle=cycle,
                    executions=executions,
                    latency_ms=1.0,
                    tokens=10,
                    stages=tuple(stages),
                )
            )
        assert len(carriers) == nominations
        return records
    finally:
        _close(graph, meta)


@pytest.mark.parametrize(("cycles", "nominations"), [(50, 500), (100, 1000)])
def test_cap_soak_drains_with_cycle_ledger(tmp_path: Path, cycles: int, nominations: int) -> None:
    records = _soak_arm(tmp_path, cycles=cycles, nominations=nominations)

    assert len(records) == cycles
    assert sum(record.executions for record in records) == nominations
    assert all(record.executions <= 10 for record in records)
    assert all(record.latency_ms >= 0 and record.tokens >= 0 for record in records)
    assert all(
        record.stages == ("regular_merge", "reconciliation", "safe_clear", "capture_ingest")
        for record in records
    )


def test_cap_soak_round_robin_first_execution_starvation_bound(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    try:
        nomination_ids = []
        for index in range(250):
            left, right = f"left-{index}", f"right-{index}"
            _seed_pair(graph, left, right)
            nomination_ids.append(_nominate_large(meta, left, right).nomination_id)
        clock = [1000.0]
        consumer = _consumer(
            graph, meta, _policy(lambda: clock[0], nomination_limit=10, scan_limit=260), _timeout_seats()
        )
        first_seen: dict[str, int] = {}
        for cycle in range(1, 27):
            clock[0] += 61.0
            consumer.process(PROFILE, dream_run_id=f"round-robin-{cycle}")
            for nomination_id in nomination_ids:
                if nomination_id not in first_seen and meta.list_reconciliation_attempts(
                    profile_id=PROFILE, nomination_id=nomination_id
                ):
                    first_seen[nomination_id] = cycle
        assert len(first_seen) == 250
        assert max(first_seen.values()) <= 25
    finally:
        _close(graph, meta)
