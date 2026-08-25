"""Watermark score-pool state machine (FR-1.5 / AC-3).

The pool accumulates per-profile S points from the scorer. Two events drive the
dream engine:

- pool >= 10.0 AND idle >= 5s  -> DREAM_TRIGGER (a dream run becomes eligible)
- pool >= 50.0                 -> FORCED_CONSOLIDATION (micro-consolidation,
                                  independent of idleness)

Idle is measured from the injected clock only: the pool never reads a wall
clock and tests never sleep. The per-profile ledger is authoritative in-process
state; the optional ``backend`` seam mirrors it into the MetaStore per-profile
score pool after every state change, so a daemon restart can restore balances.
The persisted row splits two roles: ``balance`` is only the pending gauge,
``filed_points_total`` is the lifetime ledger of already-fired points.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from mnemoseed_local.storage.ports import PoolState, TurnRange

if TYPE_CHECKING:
    from mnemoseed_local.config import Config


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

    ``pool_credit`` mirrors a non-firing ledger state into the store;
    ``pool_drain`` files a fired event's whole gauge into the lifetime ledger
    and zeroes the balance in one transaction; ``pool_states`` is the boot-time
    read back. The full MetaStore satisfies the seam, so ``stores.meta`` binds
    directly.
    """

    def pool_credit(self, profile_id: str, balance: float, turn_range: TurnRange) -> None: ...

    def pool_drain(self, profile_id: str, turn_range: TurnRange) -> float: ...

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
        config: Config | None = None,
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._backend = backend
        self._dream_threshold = dream_threshold
        self._forced_cap = forced_cap
        self._idle_window_sec = idle_window_sec
        self._config = config
        self._ledgers: dict[str, _Ledger] = {}
        # one lock for every ledger mutation: add_points runs on the capture
        # drain-lane threads while the scheduler-fire drain runs on the tick
        # thread, so their read-modify-write sections must never interleave
        self._lock = threading.Lock()

    def _forced_cap_value(self) -> float:
        """The forced-consolidation cap (T3a): the live dream.pool_forced_cap
        config key when a Config is bound, the constructor value otherwise.
        Read at every evaluation, so a configwrite change fires the SAME pool
        at the new cap — no daemon restart."""
        if self._config is None:
            return self._forced_cap
        return self._config.dream.pool_forced_cap

    def add_points(
        self,
        profile_id: str,
        points: float,
        turn_range: TurnRange,
    ) -> tuple[PoolEvent, ...]:
        """Pool points for a profile; returns any events fired by this credit.

        ``points`` are the S value of one scored turn; ``turn_range`` bounds that
        turn. Every state change mirrors into the optional backend: the new
        balance persists first (ordinary credit), then a fired event moves the
        whole gauge into the lifetime ledger with ONE atomic ``pool_drain``
        before the sink is notified — persistence always completes before the
        event is delivered, so a crash can leave points pending but never lets
        a launched dream re-fire them.
        """
        with self._lock:
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
            ledger.balance = accumulator
            ledger.range_start = start
            ledger.range_end = end
            ledger.last_add = now

            event: PoolEvent | None = None
            forced_cap = self._forced_cap_value()
            if accumulator >= forced_cap:
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

            if self._backend is not None:
                self._backend.pool_credit(profile_id, accumulator, span)

            if event is not None:
                ledger.balance = 0.0
                ledger.range_start = None
                ledger.range_end = 0
                ledger.last_add = None
                if self._backend is not None:
                    self._backend.pool_drain(profile_id, span)
                if self._sink is not None:
                    self._sink(event)
                return (event,)
            return ()

    def drain(self, profile_id: str, turn_range: TurnRange) -> float:
        """File one profile's fired points out of the gauge, both ledgers.

        The scheduler-fire path lands here. Mirroring the credit-first
        discipline of ``add_points``, the atomic backend drain runs FIRST and
        the in-process gauge resets only once it succeeded — a failed drain
        leaves both ledgers untouched instead of orphaning the persisted
        balance under an emptied gauge. Returns the filed amount.
        """
        with self._lock:
            filed = 0.0
            if self._backend is not None:
                filed = self._backend.pool_drain(profile_id, turn_range)
            ledger = self._ledgers.get(profile_id)
            if ledger is not None:
                ledger.balance = 0.0
                ledger.range_start = None
                ledger.range_end = 0
                ledger.last_add = None
            return filed

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
