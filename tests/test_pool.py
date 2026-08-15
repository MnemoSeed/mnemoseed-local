"""Watermark score-pool state machine (FR-1.5 / AC-3).

The pool accumulates per-profile S points; pool >= 10.0 plus idle >= 5s emits
a dream-trigger event, and pool >= 50.0 forces a micro-consolidation event
regardless of idleness. Idle is computed from an injected clock only — no
wall-clock sleeps anywhere.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind, PoolStats, ScorePool
from mnemoseed_local.storage.ports import PoolState, TurnRange


class _Clock:
    """Deterministic injected clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _sink() -> tuple[list[PoolEvent], Callable[[PoolEvent], None]]:
    events: list[PoolEvent] = []

    def sink(event: PoolEvent) -> None:
        events.append(event)

    return events, sink


class _FakeBackend:
    """MetaStore-shaped persistence stub satisfying the PoolBackend seam."""

    def __init__(self) -> None:
        self.credits: list[tuple[str, float, TurnRange]] = []  # pool_credit calls
        self.fired: list[tuple[str, float, TurnRange]] = []  # pool_add calls
        self._rows: dict[str, PoolState] = {}

    def pool_add(self, profile_id: str, points: float, turn_range: TurnRange) -> None:
        self.fired.append((profile_id, points, turn_range))
        state = self._rows.get(profile_id, PoolState(balance=0.0))
        self._rows[profile_id] = PoolState(balance=state.balance + points, watermark=state.watermark)

    def pool_credit(self, profile_id: str, balance: float, turn_range: TurnRange) -> None:
        self.credits.append((profile_id, balance, turn_range))
        self._rows[profile_id] = PoolState(balance=balance, watermark=turn_range)

    def pool_states(self) -> dict[str, PoolState]:
        return dict(self._rows)


def test_no_trigger_below_threshold() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    pool.add_points("p", 4.0, TurnRange(0, 0))
    pool.add_points("p", 4.0, TurnRange(1, 1))
    assert events == []


def test_no_trigger_below_threshold_even_after_idle() -> None:
    # Pins the dream threshold: balance < 10.0 must NEVER trigger, even once
    # the 5s idle window has fully elapsed (kills a threshold=mutant).
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    pool.add_points("p", 4.0, TurnRange(0, 0))
    pool.add_points("p", 4.0, TurnRange(1, 1))  # balance 8.0, below threshold
    clock.advance(6.0)
    assert pool.evaluate() == ()
    assert events == []
    assert pool.stats("p").balance == pytest.approx(8.0)


def test_no_trigger_while_busy_even_above_threshold() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    for index in range(4):
        clock.advance(1.0)  # active conversation, never idle 5s
        pool.add_points("p", 4.0, TurnRange(index, index))
    assert events == []  # balance >= 10 but idle < 5s


def test_trigger_after_idle_window_with_correct_turn_range() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    for index in range(3):
        pool.add_points("p", 4.0, TurnRange(index, index))
    clock.advance(5.0)
    pool.add_points("p", 1.0, TurnRange(3, 3))
    event = events[0]
    assert event.kind is PoolEventKind.DREAM_TRIGGER
    assert event.turn_range == TurnRange(0, 3)  # AC-3: window spans pooled turns
    assert event.balance == pytest.approx(13.0)
    assert event.fired_at == pytest.approx(clock.t)
    # the pool drained after the event
    assert pool.stats("p") is not None
    assert pool.stats("p").balance == pytest.approx(0.0)


def test_evaluate_fires_quietly_accumulated_balance() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    pool.add_points("p", 4.0, TurnRange(0, 0))
    pool.add_points("p", 4.0, TurnRange(1, 1))
    pool.add_points("p", 4.0, TurnRange(2, 2))
    clock.advance(6.0)
    fired = pool.evaluate()
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.DREAM_TRIGGER
    assert fired[0].turn_range == TurnRange(0, 2)


def test_forced_micro_consolidation_at_cap_ignores_idle() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    for index in range(6):
        clock.advance(1.0)  # busy the whole time
        fired = pool.add_points("p", 9.0, TurnRange(index, index))
        if index < 5:
            assert fired == ()
        else:
            assert len(fired) == 1
            assert fired[0].kind is PoolEventKind.FORCED_CONSOLIDATION
            assert fired[0].balance == pytest.approx(54.0)
    assert len(events) == 1
    assert events[0].kind is PoolEventKind.FORCED_CONSOLIDATION


def test_dream_trigger_takes_precedence_below_cap_only() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    # reach 49 busy -> nothing; then idle + big jump straight past 50
    for index in range(5):
        pool.add_points("p", 9.0, TurnRange(index, index))
    clock.advance(6.0)
    fired = pool.add_points("p", 9.0, TurnRange(5, 5))
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.FORCED_CONSOLIDATION


def test_per_profile_isolation() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    pool.add_points("a", 4.0, TurnRange(0, 0))
    pool.add_points("a", 4.0, TurnRange(1, 1))
    pool.add_points("b", 4.0, TurnRange(0, 0))
    clock.advance(6.0)
    fired = pool.add_points("a", 4.0, TurnRange(2, 2))
    assert len(fired) == 1
    assert fired[0].profile_id == "a"
    assert len(events) == 1
    # profile b never triggered and keeps its own balance
    stats_b = pool.stats("b")
    assert stats_b is not None
    assert stats_b.balance == pytest.approx(4.0)
    assert stats_b.dream_triggers == 0
    assert pool.stats("a").balance == pytest.approx(0.0)


def test_stats_observability() -> None:
    clock = _Clock()
    pool = ScorePool(clock=clock)
    pool.add_points("p", 4.0, TurnRange(0, 0))
    pool.add_points("p", 4.0, TurnRange(1, 1))
    stats = pool.stats("p")
    assert isinstance(stats, PoolStats)
    assert stats.profile_id == "p"
    assert stats.balance == pytest.approx(8.0)
    assert stats.points_added == pytest.approx(8.0)
    assert stats.turns_pooled == 2
    assert stats.dream_triggers == 0
    assert pool.stats("ghost") is None


def test_meta_backend_persists_credits_and_fired_events() -> None:
    """Every non-firing credit persists the live ledger; a fired event records
    itself and resets the persisted balance to 0."""
    clock = _Clock()
    backend = _FakeBackend()
    pool = ScorePool(clock=clock, backend=backend)
    for index in range(3):
        pool.add_points("p", 4.0, TurnRange(index, index))
    clock.advance(5.0)
    pool.add_points("p", 1.0, TurnRange(3, 3))
    assert backend.credits == [
        ("p", 4.0, TurnRange(0, 0)),
        ("p", 8.0, TurnRange(0, 1)),
        ("p", 12.0, TurnRange(0, 2)),
        ("p", 0.0, TurnRange(0, 3)),  # the fire reset the persisted ledger
    ]
    assert backend.fired == [("p", 13.0, TurnRange(0, 3))]
    assert backend.pool_states()["p"].balance == pytest.approx(0.0)
    assert backend.pool_states()["p"].watermark == TurnRange(0, 3)


def test_backend_persists_per_profile_isolation() -> None:
    clock = _Clock()
    backend = _FakeBackend()
    pool = ScorePool(clock=clock, backend=backend)
    pool.add_points("a", 3.0, TurnRange(0, 0))
    pool.add_points("b", 5.0, TurnRange(0, 0))
    pool.add_points("a", 4.0, TurnRange(1, 1))
    states = backend.pool_states()
    assert states["a"].balance == pytest.approx(7.0)
    assert states["a"].watermark == TurnRange(0, 1)
    assert states["b"].balance == pytest.approx(5.0)
    assert set(states) == {"a", "b"}


def test_fire_resets_persisted_balance_and_records_event() -> None:
    clock = _Clock()
    backend = _FakeBackend()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink, backend=backend)
    for index in range(3):
        pool.add_points("p", 4.0, TurnRange(index, index))
    clock.advance(5.0)
    pool.add_points("p", 1.0, TurnRange(3, 3))
    assert backend.pool_states()["p"].balance == pytest.approx(0.0)
    assert backend.fired == [("p", 13.0, TurnRange(0, 3))]
    assert len(events) == 1


def test_backend_optional_keeps_in_memory_behavior() -> None:
    clock = _Clock()
    pool = ScorePool(clock=clock)
    pool.add_points("p", 4.0, TurnRange(0, 0))
    assert pool.stats("p").balance == pytest.approx(4.0)


def test_restore_seeds_ledger_without_firing_or_persisting() -> None:
    clock = _Clock()
    backend = _FakeBackend()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink, backend=backend)
    pool.restore("p", 12.0, TurnRange(2, 5))
    ledger = pool.stats("p")
    assert ledger is not None
    assert ledger.balance == pytest.approx(12.0)
    assert ledger.turns_pooled == 0
    # conservative boot: a restored backlog cannot instantly dream-trigger even
    # once the idle window has elided, because last_add stays fresh
    clock.advance(10.0)
    assert pool.evaluate() == ()
    assert events == []
    # restore writes nothing through the backend, either direction
    assert backend.credits == []
    assert backend.fired == []
    assert backend.pool_states() == {}


def test_restore_is_idempotent() -> None:
    clock = _Clock()
    pool = ScorePool(clock=clock)
    pool.restore("p", 8.0, TurnRange(0, 2))
    pool.restore("p", 8.0, TurnRange(0, 2))
    assert pool.stats("p").balance == pytest.approx(8.0)


def test_restored_pool_accumulates_and_fires_like_a_live_one() -> None:
    clock = _Clock()
    backend = _FakeBackend()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink, backend=backend)
    pool.restore("p", 8.0, TurnRange(2, 5))
    pool.add_points("p", 4.0, TurnRange(6, 6))  # 8 + 4 = 12, idle fresh -> no fire
    assert pool.stats("p").balance == pytest.approx(12.0)
    assert backend.credits[-1] == ("p", 12.0, TurnRange(2, 6))
    clock.advance(6.0)
    fired = pool.evaluate()
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.DREAM_TRIGGER
    assert fired[0].turn_range == TurnRange(2, 6)
    assert backend.fired[-1] == ("p", 12.0, TurnRange(2, 6))
    assert backend.pool_states()["p"].balance == pytest.approx(0.0)


def test_restored_balance_at_forced_cap_fires_forced_consolidation() -> None:
    clock = _Clock()
    backend = _FakeBackend()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink, backend=backend)
    pool.restore("p", 50.0, TurnRange(0, 9))
    fired = pool.evaluate()
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.FORCED_CONSOLIDATION
    assert backend.pool_states()["p"].balance == pytest.approx(0.0)


def test_balances_snapshot() -> None:
    clock = _Clock()
    pool = ScorePool(clock=clock)
    pool.add_points("a", 3.0, TurnRange(0, 0))
    pool.add_points("b", 5.0, TurnRange(0, 0))
    assert pool.balances() == {"a": 3.0, "b": 5.0}
