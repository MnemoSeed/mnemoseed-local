"""Volatile, ranking-only short-term activation state."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

ActivationKey = tuple[str, str, str]
ActivationEntry = tuple[float, float]


@dataclass(frozen=True)
class ActivationSnapshot:
    entries: Mapping[ActivationKey, ActivationEntry]
    enabled: bool
    half_life: float
    boost_value: float

    def boost(self, memory_id: str, *, profile_id: str, session_id: str, now: float) -> float:
        if not self.enabled:
            return 0.0
        try:
            entry = self.entries.get((profile_id, session_id, memory_id))
            if entry is None:
                return 0.0
            refreshed_at, value = entry
            if self.half_life <= 0 or not math.isfinite(refreshed_at) or not math.isfinite(value):
                return 0.0
            elapsed = max(0.0, now - refreshed_at)
            result = value * math.pow(0.5, elapsed / self.half_life)
            return result if math.isfinite(result) and result > 0.0 else 0.0
        except (ArithmeticError, TypeError, ValueError):
            return 0.0

    def score(self, memory_id: str, *, profile_id: str, session_id: str, now: float) -> float:
        return self.boost(memory_id, profile_id=profile_id, session_id=session_id, now=now)


class ShortTermActivation:
    def __init__(
        self,
        *,
        enabled: bool,
        half_life: float,
        boost_value: float,
        capacity: int,
        clock: Callable[[], float],
    ) -> None:
        self._enabled = enabled
        self._half_life = half_life
        self._boost_value = boost_value
        self._capacity = capacity
        self._clock = clock
        self._entries: OrderedDict[ActivationKey, ActivationEntry] = OrderedDict()

    def refresh(self, memory_id: str, *, profile_id: str, session_id: str, now: float | None = None) -> None:
        if not self._enabled or not self._valid_id(memory_id):
            return
        try:
            timestamp = self._clock() if now is None else now
            if not math.isfinite(timestamp) or not math.isfinite(self._boost_value):
                return
            key = (profile_id, session_id, memory_id)
            self._entries.pop(key, None)
            self._entries[key] = (timestamp, self._boost_value)
            while self._capacity >= 0 and len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        except (ArithmeticError, TypeError, ValueError):
            return

    def snapshot(self) -> ActivationSnapshot:
        return ActivationSnapshot(
            entries=MappingProxyType(dict(self._entries)),
            enabled=self._enabled,
            half_life=self._half_life,
            boost_value=self._boost_value,
        )

    def score(self, memory_id: str, *, profile_id: str, session_id: str, now: float | None = None) -> float:
        timestamp = self._clock() if now is None else now
        return self.snapshot().score(memory_id, profile_id=profile_id, session_id=session_id, now=timestamp)

    @staticmethod
    def _valid_id(memory_id: str) -> bool:
        return isinstance(memory_id, str) and ":" in memory_id and bool(memory_id.split(":", 1)[0])
