"""Monthly per-profile dream token ledger (PRD-02 T5b; FR-2.5b / NFR-2.2).

T3b (A2.5 batch 3): the USD budget concept is removed (design/01 §4.1). The
ledger is pure token bookkeeping: it RECORDS what a dream actually consumed
(the packed delta plus any provider-reported output tokens) and reads the
current UTC month's counter. There is no budget gate, no USD projection, no
refusal audit action — the counter is keyed by ``(profile_id, year_month)``,
so auto-recovery falls out of the key; there is no rollover job.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from mnemoseed_local.storage.ports import MetaStore


def year_month_for(timestamp: float) -> str:
    """UTC year-month bucket, e.g. 2026-08-01T00:00:00Z -> "2026-08".

    Deterministic and monotonic; the month rollover IS the auto-recovery seam.
    """
    return time.strftime("%Y-%m", time.gmtime(timestamp))


@dataclass(frozen=True)
class LedgerStatus:
    """Observability: current-month token usage only (no budget surface)."""

    profile_id: str
    year_month: str
    used_tokens: int


class TokenLedger:
    """Per-profile monthly dream-token counter.

    Pure bookkeeping over the MetaStore's two ledger port calls (atomic
    increment / current-month read); never performs storage I/O itself and
    never gates the reflect boundary. The clock is injectable (the existing
    seam used by the snapshotter) so tests can pin UTC year-months and drive
    rollovers.
    """

    def __init__(
        self,
        meta: MetaStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._meta = meta
        self._clock = clock

    def _month(self) -> str:
        return year_month_for(self._clock())

    # ------------------------------------------------------------ metering

    def record(
        self, profile_id: str, *, delta_tokens: int, prefix_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        """Meter one completed dream into the current UTC month.

        The counter accumulates delta + output tokens; the cache-resident
        prefix is not metered (the delta carries the newly-consumed tokens).
        """
        del prefix_tokens
        self._meta.add_token_usage(profile_id, self._month(), delta_tokens + output_tokens)

    def usage(self, profile_id: str) -> int:
        """This profile's recorded token counter for the current UTC month."""
        return self._meta.token_usage(profile_id, self._month())

    # ------------------------------------------------------------ observability

    def status(self, profile_id: str) -> LedgerStatus:
        """Current-month token usage (observability only)."""
        return LedgerStatus(
            profile_id=profile_id,
            year_month=self._month(),
            used_tokens=self.usage(profile_id),
        )
