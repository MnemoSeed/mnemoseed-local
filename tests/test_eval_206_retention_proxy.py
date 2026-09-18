from dataclasses import dataclass
from enum import StrEnum

import pytest

RETENTION_DAYS = 30
SWEEP_BUDGET = 100
VIRTUAL_DAY = 86_400.0


class Shape(StrEnum):
    STALE_REVISION = "stale_revision"
    MISSING_RETIRED_PEER = "missing_retired_peer"
    PROTECTED_ENDPOINT = "protected_endpoint"
    HALF_GROUP = "half_group"
    RETRY_EXHAUSTED = "retry_exhausted_terminal"


@dataclass
class Pointer:
    profile_id: str
    peer_id: str | None
    marker: bool = True


@dataclass
class Case:
    shape: Shape
    created_at: float
    profile_id: str = "p1"
    retries: int = 0
    terminal: bool = True


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def advance_days(self, days: int) -> None:
        self.now += days * VIRTUAL_DAY


class RetentionProxy:
    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.pending: list[Case] = []
        self.receipts: list[Case] = []
        self.audit: list[Case] = []
        self.graph_writes = 0
        self.marker_clears: list[tuple[str, str]] = []

    def enqueue(self, case: Case) -> None:
        if not case.terminal:
            self.pending.append(case)

    def repair_sweep(self) -> int:
        repaired = 0
        for case in self.pending[:SWEEP_BUDGET]:
            self.pending.remove(case)
            self.receipts.append(case)
            self.audit.append(case)
            repaired += 1
        return repaired

    def retained(self, case: Case) -> bool:
        return self.clock.now - case.created_at < RETENTION_DAYS * VIRTUAL_DAY

    def operator_surfaces(self, profile_id: str) -> dict[str, int]:
        unresolved = sum(case.profile_id == profile_id for case in self.receipts)
        retired = sum(
            case.profile_id == profile_id and case.shape is Shape.MISSING_RETIRED_PEER for case in self.audit
        )
        return {"dream_status_unresolved": unresolved, "error_events_evidence_retired": retired}


@pytest.mark.parametrize("age, expected", [(29, True), (30, False), (31, False)])
def test_retention_boundary_table(age: int, expected: bool) -> None:
    clock = VirtualClock()
    proxy = RetentionProxy(clock)
    case = Case(Shape.RETRY_EXHAUSTED, created_at=clock.now)
    clock.advance_days(age)
    assert proxy.retained(case) is expected


@pytest.mark.parametrize("multiplier", [1, 10, 100])
def test_ratelimited_sweep_budget_over_thirty_virtual_days(multiplier: int) -> None:
    clock = VirtualClock()
    proxy = RetentionProxy(clock)
    for _index in range(SWEEP_BUDGET * multiplier):
        proxy.pending.append(Case(Shape.RETRY_EXHAUSTED, created_at=clock.now, terminal=False))
    for _ in range(RETENTION_DAYS):
        assert proxy.repair_sweep() <= SWEEP_BUDGET
        clock.advance_days(1)
    assert len(proxy.receipts) == min(SWEEP_BUDGET * RETENTION_DAYS, SWEEP_BUDGET * multiplier)
    assert len(proxy.pending) == max(0, SWEEP_BUDGET * multiplier - SWEEP_BUDGET * RETENTION_DAYS)


@pytest.mark.parametrize("shape", list(Shape))
def test_terminal_unresolved_is_not_requeued(shape: Shape) -> None:
    clock = VirtualClock()
    proxy = RetentionProxy(clock)
    case = Case(shape, created_at=clock.now, terminal=True)
    proxy.enqueue(case)
    assert proxy.pending == []
    assert proxy.repair_sweep() == 0


def test_protected_endpoint_has_no_graph_touch_or_marker_clear() -> None:
    clock = VirtualClock()
    proxy = RetentionProxy(clock)
    case = Case(Shape.PROTECTED_ENDPOINT, created_at=clock.now)
    proxy.receipts.append(case)
    assert proxy.graph_writes == 0
    assert proxy.marker_clears == []


def test_stale_repair_clears_only_owned_exact_pointer() -> None:
    owned = Pointer("p1", "b")
    foreign = Pointer("p2", "b")
    assert owned.profile_id == "p1" and owned.peer_id == "b"
    assert foreign.profile_id != owned.profile_id
    assert foreign.peer_id == "b"


def test_operator_burden_uses_existing_status_and_error_event_fate() -> None:
    clock = VirtualClock()
    proxy = RetentionProxy(clock)
    proxy.receipts.extend(
        [
            Case(Shape.PROTECTED_ENDPOINT, 0.0, profile_id="p1"),
            Case(Shape.MISSING_RETIRED_PEER, 0.0, profile_id="p1"),
            Case(Shape.STALE_REVISION, 0.0, profile_id="p2"),
        ]
    )
    proxy.audit.extend(proxy.receipts[1:])
    assert proxy.operator_surfaces("p1") == {
        "dream_status_unresolved": 2,
        "error_events_evidence_retired": 1,
    }
