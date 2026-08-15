"""Watermark score-pool state machine (FR-1.5 / AC-3).

The pool accumulates per-profile S points from the scorer. Two events drive the
dream engine:

- pool >= 10.0 AND idle >= 5s  -> DREAM_TRIGGER (a dream run becomes eligible)
- pool >= 50.0                 -> FORCED_CONSOLIDATION (micro-consolidation,
                                  independent of idleness)

Idle is measured from the injected clock only: the pool never reads a wall
clock and tests never sleep. The per-profile ledger is authoritative in-process
state; the optional ``backend`` seam mirrors it into the MetaStore per-profile
score pool after every state change (so a daemon restart can restore balances),
and records each trigger event there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from mnemoseed_local.storage.ports import PoolState, TurnRange


class PoolEventKind(StrEnum):
    """What a pool event tells the downstream consumer to do."""

    DREAM_TRIGGER = "dream_trigger"  # eligible to run a dream pass
    FORCED_CONSOLIDATION = "forced_consolidation"  # micro-consolidation now


@dataclass(frozen=True)
class PoolEvent:
    """One pool decision delivered to the sink / returned from a call."""

    kind: PoolEventKind
    profile_id: str
    turn_range: TurnRange  # window of turns whose points triggered the event
    balance: float  # accumulated balance at fire time (before reset)
    fired_at: float  # injected-clock timestamp


@dataclass(frozen=True)
class PoolStats:
    """Observability snapshot for one profile ledger."""

    profile_id: str
    balance: float
    points_added: float
    turns_pooled: int
    dream_triggers: int
    forced_triggers: int


class PoolBackend(Protocol):
    """MetaStore-shaped persistence seam (structural subset of MetaStore).

    ``pool_credit`` mirrors a non-firing ledger state into the store; ``pool_add``
    records a fired event's accumulated total; ``pool_states`` is the boot-time
    read back. The full MetaStore satisfies the seam, so ``stores.meta`` binds
    directly.
    """

    def pool_add(self, profile_id: str, points: float, turn_range: TurnRange) -> None: ...

    def pool_credit(self, profile_id: str, balance: float, turn_range: TurnRange) -> None: ...

    def pool_states(self) -> dict[str, PoolState]: ...


@dataclass
class _Ledger:
    balance: float = 0.0
    range_start: int | None = None
    range_end: int = 0
    last_add: float | None = None  # injected-clock ts of the last pooled point
    points_added: float = 0.0
    turns_pooled: int = 0
    dream_triggers: int = 0
    forced_triggers: int = 0


class ScorePool:
    """Per-profile score pool with deterministic, injected-clock evaluation."""

    def __init__(
        self,
        clock: Callable[[], float],
        sink: Callable[[PoolEvent], None] | None = None,
        backend: PoolBackend | None = None,
        *,
        dream_threshold: float = 10.0,
        forced_cap: float = 50.0,
        idle_window_sec: float = 5.0,
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._backend = backend
        self._dream_threshold = dream_threshold
        self._forced_cap = forced_cap
        self._idle_window_sec = idle_window_sec
        self._ledgers: dict[str, _Ledger] = {}

    def add_points(
        self,
        profile_id: str,
        points: float,
        turn_range: TurnRange,
    ) -> tuple[PoolEvent, ...]:
        """Pool points for a profile; returns any events fired by this credit.

        ``points`` are the S value of one scored turn; ``turn_range`` bounds that
        turn. Every state change mirrors into the optional backend: a non-firing
        credit persists the new balance and span, a fired event records the
        accumulated total (``pool_add``) and then resets the persisted balance to
        0 (``pool_credit``) before the sink is notified.
        """
        ledger = self._ledgers.setdefault(profile_id, _Ledger())
        ledger.points_added += points
        ledger.turns_pooled += 1
        now = self._clock()
        idle = now - ledger.last_add if ledger.last_add is not None else 0.0
        accumulator = ledger.balance + points
        if ledger.balance == 0:
            start, end = turn_range.start, turn_range.end
        else:
            start = ledger.range_start if ledger.range_start is not None else turn_range.start
            end = max(ledger.range_end, turn_range.end)
        span = TurnRange(start, end)

        event: PoolEvent | None = None
        if accumulator >= self._forced_cap:
            event = PoolEvent(
                kind=PoolEventKind.FORCED_CONSOLIDATION,
                profile_id=profile_id,
                turn_range=span,
                balance=accumulator,
                fired_at=now,
            )
            ledger.forced_triggers += 1
        elif accumulator >= self._dream_threshold and idle >= self._idle_window_sec:
            event = PoolEvent(
                kind=PoolEventKind.DREAM_TRIGGER,
                profile_id=profile_id,
                turn_range=span,
                balance=accumulator,
                fired_at=now,
            )
            ledger.dream_triggers += 1
        else:
            ledger.balance = accumulator
            ledger.range_start = start
            ledger.range_end = end
            ledger.last_add = now
            if self._backend is not None:
                self._backend.pool_credit(profile_id, accumulator, span)

        if event is not None:
            ledger.balance = 0.0
            ledger.range_start = None
            ledger.range_end = 0
            ledger.last_add = None
            if self._backend is not None:
                self._backend.pool_add(profile_id, accumulator, span)
                self._backend.pool_credit(profile_id, 0.0, span)
            if self._sink is not None:
                self._sink(event)
            return (event,)
        return ()

    def evaluate(self) -> tuple[PoolEvent, ...]:
        """Re-check every non-empty ledger against the same rules.

        Used when the daemon polls between turns; returns every event fired and
        delivers the same backend/sink notifications as ``add_points``.
        """
        now = self._clock()
        fired: list[PoolEvent] = []
        for profile_id, ledger in self._ledgers.items():
            if ledger.balance <= 0:
                continue
            idle = now - ledger.last_add if ledger.last_add is not None else 0.0
            if ledger.range_start is None:
                continue
            span = TurnRange(ledger.range_start, ledger.range_end)
            if ledger.balance >= self._forced_cap:
                event = PoolEvent(
                    kind=PoolEventKind.FORCED_CONSOLIDATION,
                    profile_id=profile_id,
                    turn_range=span,
                    balance=ledger.balance,
                    fired_at=now,
                )
                ledger.forced_triggers += 1
            elif ledger.balance >= self._dream_threshold and idle >= self._idle_window_sec:
                event = PoolEvent(
                    kind=PoolEventKind.DREAM_TRIGGER,
                    profile_id=profile_id,
                    turn_range=span,
                    balance=ledger.balance,
                    fired_at=now,
                )
                ledger.dream_triggers += 1
            else:
                continue
            fired.append(event)
            accumulator = event.balance
            ledger.balance = 0.0
            ledger.range_start = None
            ledger.range_end = 0
            ledger.last_add = None
            if self._backend is not None:
                self._backend.pool_add(profile_id, accumulator, span)
                self._backend.pool_credit(profile_id, 0.0, span)
            if self._sink is not None:
                self._sink(event)
        return tuple(fired)

    def stats(self, profile_id: str) -> PoolStats | None:
        """Snapshot a profile ledger, or None if nothing was ever pooled."""
        ledger = self._ledgers.get(profile_id)
        if ledger is None:
            return None
        return PoolStats(
            profile_id=profile_id,
            balance=ledger.balance,
            points_added=ledger.points_added,
            turns_pooled=ledger.turns_pooled,
            dream_triggers=ledger.dream_triggers,
            forced_triggers=ledger.forced_triggers,
        )

    def restore(self, profile_id: str, balance: float, watermark: TurnRange | None) -> None:
        """Seed a ledger from persisted state, e.g. at daemon boot.

        Never fires events and never writes back through the backend: the
        restored ledger is in-process state until the next real credit.
        ``last_add`` stays None (idle-fresh) so a restored lineage cannot dream
        or consolidate instantly — the returned balance only participates in
        evaluation once a new turn advances the clock.
        """
        if balance <= 0 or watermark is None:
            return
        ledger = self._ledgers.setdefault(profile_id, _Ledger())
        ledger.balance = balance
        ledger.range_start = watermark.start
        ledger.range_end = watermark.end

    def balances(self) -> dict[str, float]:
        """Current per-profile balances (profile_id -> points)."""
        return {profile_id: ledger.balance for profile_id, ledger in self._ledgers.items()}
