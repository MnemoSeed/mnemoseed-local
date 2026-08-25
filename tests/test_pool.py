"""Watermark score-pool state machine (FR-1.5 / AC-3).

The pool accumulates per-profile S points; pool >= 10.0 plus idle >= 5s emits
a dream-trigger event, and pool >= 50.0 forces a micro-consolidation event
regardless of idleness. Idle is computed from an injected clock only — no
wall-clock sleeps anywhere.
"""

from __future__ import annotations

import threading
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
        self.drains: list[tuple[str, float, TurnRange]] = []  # pool_drain calls
        self.sequence: list[str] = []  # call order across both verbs
        self._rows: dict[str, PoolState] = {}

    def pool_credit(self, profile_id: str, balance: float, turn_range: TurnRange) -> None:
        self.sequence.append("credit")
        self.credits.append((profile_id, balance, turn_range))
        state = self._rows.get(profile_id, PoolState(balance=0.0))
        self._rows[profile_id] = PoolState(
            balance=balance,
            watermark=turn_range,
            filed_points_total=state.filed_points_total,
        )

    def pool_drain(self, profile_id: str, turn_range: TurnRange) -> float:
        self.sequence.append("drain")
        state = self._rows.get(profile_id, PoolState(balance=0.0))
        self._rows[profile_id] = PoolState(
            balance=0.0,
            watermark=state.watermark,
            filed_points_total=state.filed_points_total + state.balance,
        )
        self.drains.append((profile_id, state.balance, turn_range))
        return state.balance

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


def test_meta_backend_persists_credits_and_filed_events() -> None:
    """Every credit persists the live gauge first; a fired event then files its
    whole gauge into the lifetime ledger and zeroes the balance."""
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
        ("p", 13.0, TurnRange(0, 3)),  # the triggering credit is mirrored too
    ]
    assert backend.drains == [("p", 13.0, TurnRange(0, 3))]
    state = backend.pool_states()["p"]
    assert state.balance == pytest.approx(0.0)
    assert state.filed_points_total == pytest.approx(13.0)
    assert state.watermark == TurnRange(0, 3)


def test_fire_files_the_gauge_in_one_atomic_drain() -> None:
    """The fired event's points move into the lifetime ledger and the persisted
    gauge resets inside ONE transaction — the old add-then-reset pair had a
    crash window between its two writes."""
    clock = _Clock()
    backend = _FakeBackend()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink, backend=backend)
    for index in range(3):
        pool.add_points("p", 4.0, TurnRange(index, index))
    clock.advance(5.0)
    fired = pool.add_points("p", 1.0, TurnRange(3, 3))
    assert len(fired) == 1
    # exactly one drain carries the whole accumulated balance; no zeroing
    # credit follows it
    assert backend.drains == [("p", 13.0, TurnRange(0, 3))]
    assert backend.credits[-1] == ("p", 13.0, TurnRange(0, 3))
    assert len(events) == 1


def test_fire_mirrors_the_credit_before_the_atomic_drain() -> None:
    """Ordering pin: within a fire, the triggering credit lands on the backend
    BEFORE the atomic drain — persistence completes before the ledger move, so
    a crash between the two leaves points pending instead of consuming them."""
    clock = _Clock()
    backend = _FakeBackend()
    pool = ScorePool(clock=clock, backend=backend)
    for index in range(3):
        pool.add_points("p", 4.0, TurnRange(index, index))
    clock.advance(5.0)
    pool.add_points("p", 1.0, TurnRange(3, 3))  # the fire under test
    assert backend.sequence[-2:] == ["credit", "drain"]


class _RaisingDrainBackend(_FakeBackend):
    """A backend whose atomic drain fails (a storage hiccup)."""

    def pool_drain(self, profile_id: str, turn_range: TurnRange) -> float:
        raise RuntimeError("simulated drain failure")


class _InterlockBackend(_FakeBackend):
    """Coordinates one armed credit so a drain can land mid-credit."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.credit_started = threading.Event()
        self.release_credit = threading.Event()

    def pool_credit(self, profile_id: str, balance: float, turn_range: TurnRange) -> None:
        if self.armed:
            self.credit_started.set()
            self.release_credit.wait(timeout=10.0)
        super().pool_credit(profile_id, balance, turn_range)


def test_concurrent_add_and_drain_never_double_count() -> None:
    """Interleaving oracle: a drain landing mid-credit from another thread
    (hard-deadline fires need no idle) must leave every point either filed or
    pending exactly once — never counted in both ledgers."""
    clock = _Clock()
    backend = _InterlockBackend()
    pool = ScorePool(clock=clock, backend=backend)
    for index in range(3):
        pool.add_points("p", 4.0, TurnRange(index, index))  # gauge 12, persisted 12
    backend.armed = True  # interlock only the racing credit below

    def adder() -> None:
        pool.add_points("p", 1.0, TurnRange(3, 3))

    drained: dict[str, float] = {}

    def drainer() -> None:
        drained["filed"] = pool.drain("p", TurnRange(0, 3))

    thread = threading.Thread(target=adder)
    thread.start()
    assert backend.credit_started.wait(timeout=10.0)
    race = threading.Thread(target=drainer)
    race.start()  # races the paused credit (or blocks on the pool lock)
    backend.release_credit.set()
    thread.join(timeout=10.0)
    race.join(timeout=10.0)

    assert "filed" in drained, "the racing drain never completed (deadlock?)"
    state = backend.pool_states()["p"]
    # every credited point is exactly once in the ledger or the gauge
    assert state.filed_points_total + state.balance == pytest.approx(13.0)
    assert drained["filed"] + state.balance == pytest.approx(13.0)


def test_failed_backend_drain_keeps_both_gauges() -> None:
    """If the atomic backend drain fails, the in-process gauge must NOT be
    reset ahead of it: both ledgers keep their values and the next credit
    re-persists the full balance — no point vanishes."""
    clock = _Clock()
    backend = _RaisingDrainBackend()
    pool = ScorePool(clock=clock, backend=backend)
    for index in range(3):
        pool.add_points("p", 4.0, TurnRange(index, index))
    with pytest.raises(RuntimeError, match="simulated drain failure"):
        pool.drain("p", TurnRange(0, 3))
    # neither ledger lost the points
    assert pool.stats("p") is not None
    assert pool.stats("p").balance == pytest.approx(12.0)
    state = backend.pool_states()["p"]
    assert state.balance == pytest.approx(12.0)
    assert state.filed_points_total == pytest.approx(0.0)
    # the next credit re-persists the untouched balance: nothing vanished
    pool.add_points("p", 4.0, TurnRange(4, 4))
    state = backend.pool_states()["p"]
    assert state.balance == pytest.approx(16.0)
    assert state.filed_points_total == pytest.approx(0.0)


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
    assert backend.drains == [("p", 13.0, TurnRange(0, 3))]
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
    assert events == []
    # restore writes nothing through the backend, either direction
    assert backend.credits == []
    assert backend.drains == []
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
    fired = pool.add_points("p", 1.0, TurnRange(7, 7))
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.DREAM_TRIGGER
    assert fired[0].turn_range == TurnRange(2, 7)
    assert backend.drains[-1] == ("p", 13.0, TurnRange(2, 7))
    assert backend.pool_states()["p"].balance == pytest.approx(0.0)


def test_restored_balance_at_forced_cap_fires_forced_consolidation() -> None:
    clock = _Clock()
    backend = _FakeBackend()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink, backend=backend)
    pool.restore("p", 45.0, TurnRange(0, 9))
    fired = pool.add_points("p", 6.0, TurnRange(10, 10))  # 45 + 6 crosses the cap
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.FORCED_CONSOLIDATION
    state = backend.pool_states()["p"]
    assert state.balance == pytest.approx(0.0)
    assert state.filed_points_total == pytest.approx(51.0)


def test_balances_snapshot() -> None:
    clock = _Clock()
    pool = ScorePool(clock=clock)
    pool.add_points("a", 3.0, TurnRange(0, 0))
    pool.add_points("b", 5.0, TurnRange(0, 0))
    assert pool.balances() == {"a": 3.0, "b": 5.0}


# ---------------------------------------------------------------- forced cap from config (T3a / AC6)


def test_forced_cap_fires_at_the_config_value() -> None:
    """A pool bound to a live Config reads dream.pool_forced_cap: with a 20
    cap the forced consolidation fires at 20 points, not the 50 default."""
    from mnemoseed_local.config import Config, DreamConfig

    clock = _Clock()
    events, sink = _sink()
    config = Config()
    config.dream = DreamConfig(pool_forced_cap=20.0)
    pool = ScorePool(clock=clock, sink=sink, config=config)
    pool.add_points("p", 10.0, TurnRange(0, 0))
    assert events == []  # 10 < 20: below the configured cap
    fired = pool.add_points("p", 10.0, TurnRange(1, 1))
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.FORCED_CONSOLIDATION
    assert fired[0].balance == pytest.approx(20.0)


def test_forced_cap_hot_applies_without_reconstruction() -> None:
    """The pool holds a live Config reference: a configwrite cap change fires
    the SAME pool instance at the new cap (no daemon restart)."""
    from mnemoseed_local.config import Config, DreamConfig

    clock = _Clock()
    events, sink = _sink()
    config = Config()  # cap 50.0 default
    pool = ScorePool(clock=clock, sink=sink, config=config)
    pool.add_points("p", 30.0, TurnRange(0, 0))
    assert events == []  # 30 < 50 default cap, busy idle -> nothing fires

    # hot-apply: the configwrite seam replaces config.dream on the SAME object
    config.dream = DreamConfig(pool_forced_cap=20.0)
    fired = pool.add_points("p", 5.0, TurnRange(1, 1))
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.FORCED_CONSOLIDATION
    assert fired[0].balance == pytest.approx(35.0)


def test_forced_cap_constructor_arg_binds_when_no_config() -> None:
    """Unbound pools keep the constructor forced_cap contract (regression
    fence for every historic direct construction)."""
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink, forced_cap=20.0)
    pool.add_points("p", 10.0, TurnRange(0, 0))
    assert events == []
    fired = pool.add_points("p", 10.0, TurnRange(1, 1))
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.FORCED_CONSOLIDATION
