"""Monthly per-profile dream token ledger (PRD-02 T5b; FR-2.5b / NFR-2.2).

The monthly ledger is the cost-deadlock second layer (design/02 section 6): an
overspent month degrades the dream engine to capture-only, and a new UTC month
reopens it automatically — the counter is keyed by ``(profile_id, year_month)``,
so auto-recovery falls out of the key; there is no rollover job.

The meter records what a dream actually consumed: the packed delta plus any
provider-reported output tokens (the cache-resident prefix is billed in the
projection but not metered into the counter). Prior months are metered at the
input rate (a documented approximation: the ledger stores a single token
counter, not a tiered bill); each pending dream's projection is exact tiered
arithmetic through ``delta.estimate_cost_usd``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from mnemoseed_local.dream.delta import PriceTable, estimate_cost_usd
from mnemoseed_local.storage.ports import AuditEntry, MetaStore

# FR-2.5b default monthly budget in USD. Mirrored as
# DEFAULT_DREAM_TOKEN_BUDGET_USD in config.py (config cannot import this module
# without a cycle); a synchronisation test pins them equal.
DEFAULT_MONTHLY_BUDGET_USD: float = 5.0


def year_month_for(timestamp: float) -> str:
    """UTC year-month bucket, e.g. 2026-08-01T00:00:00Z -> "2026-08".

    Deterministic and monotonic; the month rollover IS the auto-recovery seam.
    """
    return time.strftime("%Y-%m", time.gmtime(timestamp))


@dataclass(frozen=True)
class LedgerStatus:
    """FR-2.5b observability: current-month usage and the monthly budget limit."""

    profile_id: str
    year_month: str
    used_tokens: int
    used_usd: float
    budget_usd: float
    remaining_usd: float


class TokenLedger:
    """Per-profile monthly dream-token counter with a USD budget gate.

    Pure bookkeeping over the MetaStore's two ledger port calls (atomic
    increment / current-month read) plus the shared audit seam; never performs
    storage I/O itself. The clock is injectable (the existing seam used by the
    snapshotter) so tests can pin UTC year-months and drive rollovers.
    """

    def __init__(
        self,
        meta: MetaStore,
        *,
        budget_usd: float = DEFAULT_MONTHLY_BUDGET_USD,
        price: PriceTable | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._meta = meta
        self._budget_usd = budget_usd
        self._price = price if price is not None else PriceTable()
        self._clock = clock

    def _month(self) -> str:
        return year_month_for(self._clock())

    # ------------------------------------------------------------ metering

    def record(
        self, profile_id: str, *, delta_tokens: int, prefix_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        """Meter one completed dream into the current UTC month.

        The counter accumulates delta + output tokens; the cache-resident prefix
        is excluded from the meter (it is priced into ``projection``, which the
        gate uses, at the discounted cache-read rate).
        """
        del prefix_tokens  # billed in the projection, not counted in the meter
        self._meta.add_token_usage(profile_id, self._month(), delta_tokens + output_tokens)

    def usage(self, profile_id: str) -> int:
        """This profile's recorded token counter for the current UTC month."""
        return self._meta.token_usage(profile_id, self._month())

    def _token_usd(self, tokens: int) -> float:
        return tokens * self._price.input_usd_per_m / 1_000_000.0

    def usage_usd(self, profile_id: str) -> float:
        """Recorded counter priced at the input rate (documented approximation:
        the meter stores one counter, not a tiered bill)."""
        return self._token_usd(self.usage(profile_id))

    # ------------------------------------------------------------ the gate

    def projection(
        self, profile_id: str, *, delta_tokens: int, prefix_tokens: int = 0, output_tokens: int = 0
    ) -> float:
        """Projected month spend: recorded usage plus this dream's exact cost."""
        pending = estimate_cost_usd(
            delta_tokens=delta_tokens,
            prefix_tokens=prefix_tokens,
            output_tokens=output_tokens,
            price=self._price,
        )
        return self.usage_usd(profile_id) + pending

    def within_budget(
        self, profile_id: str, *, delta_tokens: int, prefix_tokens: int = 0, output_tokens: int = 0
    ) -> bool:
        """FR-2.5b gate predicate: True when the projected spend stays at or
        under the monthly budget."""
        return (
            self.projection(
                profile_id,
                delta_tokens=delta_tokens,
                prefix_tokens=prefix_tokens,
                output_tokens=output_tokens,
            )
            <= self._budget_usd
        )

    # ------------------------------------------------------------ observability + audit

    def status(self, profile_id: str) -> LedgerStatus:
        """Current-month usage and the budget limit (FR-2.5b observability)."""
        used_tokens = self.usage(profile_id)
        used_usd = self.usage_usd(profile_id)
        return LedgerStatus(
            profile_id=profile_id,
            year_month=self._month(),
            used_tokens=used_tokens,
            used_usd=used_usd,
            budget_usd=self._budget_usd,
            remaining_usd=max(0.0, self._budget_usd - used_usd),
        )

    def record_refusal(
        self, profile_id: str, *, delta_tokens: int, prefix_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        """Append the FR-2.5b capture-only refusal to the audit trail."""
        projected = self.projection(
            profile_id,
            delta_tokens=delta_tokens,
            prefix_tokens=prefix_tokens,
            output_tokens=output_tokens,
        )
        self._meta.audit_append(
            AuditEntry(
                actor="dream",
                action="token_budget_cap",
                detail={
                    "profile_id": profile_id,
                    "year_month": self._month(),
                    "delta_tokens": delta_tokens,
                    "prefix_tokens": prefix_tokens,
                    "output_tokens": output_tokens,
                    "used_usd": self.usage_usd(profile_id),
                    "projected_usd": projected,
                    "budget_usd": self._budget_usd,
                },
                at=self._clock(),
            )
        )
