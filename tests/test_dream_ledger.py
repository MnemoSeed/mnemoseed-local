"""Monthly per-profile dream token ledger (PRD-02 T5b; FR-2.5b / NFR-2.2).

Testable behaviors through the public surface:

- UTC year-month bucketing is deterministic (the auto-recovery key).
- record() accumulates delta + output tokens for the current UTC month.
- usage() reads the current month's counter; unknown months are zero.
- per-profile and per-month keys never mix.
- clock rollover: a new UTC month opens a fresh zero counter (auto-recovery).
- USD projection equals prior usage plus this dream's exact cost arithmetic.
- within_budget is the FR-2.5b gate predicate (projected spend > budget defers).
- status() is the observability seam: current-month usage + budget limit.
- record_refusal() appends the audit trail (FR-2.5b capture-only mode).
"""

from __future__ import annotations

import pytest

from mnemoseed_local.config import DEFAULT_DREAM_TOKEN_BUDGET_USD
from mnemoseed_local.dream.delta import PriceTable
from mnemoseed_local.dream.ledger import (
    DEFAULT_MONTHLY_BUDGET_USD,
    LedgerStatus,
    TokenLedger,
    year_month_for,
)
from mnemoseed_local.storage.ports import AuditEntry

# 2026-08-01T00:00:00Z and 2026-09-01T00:00:00Z (UTC, pinned deterministically).
_AUG = 1785542400.0
_SEP = 1788220800.0

_DEFAULT_PRICE = PriceTable()


class _FakeMeta:
    """MetaStore-shaped double for the ledger's two port calls + the audit seam."""

    def __init__(self) -> None:
        self.counters: dict[tuple[str, str], int] = {}
        self.audit: list[AuditEntry] = []

    def add_token_usage(self, profile_id: str, year_month: str, tokens: int) -> None:
        key = (profile_id, year_month)
        self.counters[key] = self.counters.get(key, 0) + tokens

    def token_usage(self, profile_id: str, year_month: str) -> int:
        return self.counters.get((profile_id, year_month), 0)

    def audit_append(self, entry: AuditEntry) -> None:
        self.audit.append(entry)


class _MutableClock:
    def __init__(self, ts: float) -> None:
        self.ts = ts

    def __call__(self) -> float:
        return self.ts


def _clock_at(ts: float):
    return lambda: ts


# ---------------------------------------------------------------- bucketing


def test_year_month_is_utc_and_deterministic() -> None:
    assert year_month_for(0.0) == "1970-01"
    assert year_month_for(_AUG) == "2026-08"
    assert year_month_for(_AUG - 1.0) == "2026-07"  # 1s before the new month rolls over
    assert year_month_for(_SEP) == "2026-09"
    assert year_month_for(_AUG) == year_month_for(_AUG)


# ---------------------------------------------------------------- record + usage


def test_record_accumulates_delta_and_output_tokens() -> None:
    meta = _FakeMeta()
    ledger = TokenLedger(meta, clock=_clock_at(_AUG))
    ledger.record("alice", delta_tokens=100, prefix_tokens=50, output_tokens=25)
    ledger.record("alice", delta_tokens=7, output_tokens=3)
    assert meta.counters == {("alice", "2026-08"): 135}


def test_usage_returns_current_month_counter_per_profile() -> None:
    meta = _FakeMeta()
    ledger = TokenLedger(meta, clock=_clock_at(_AUG))
    ledger.record("alice", delta_tokens=100)
    assert ledger.usage("alice") == 100
    assert ledger.usage("bob") == 0  # a different profile shares no ledger row


def test_usage_zero_for_unknown_month() -> None:
    ledger = TokenLedger(_FakeMeta())
    assert ledger.usage("alice") == 0


def test_per_profile_and_per_month_isolation() -> None:
    """A prior month's direct port write never bleeds into the current month."""
    meta = _FakeMeta()
    meta.add_token_usage("alice", "2026-07", 900)  # spent in a previous month
    ledger = TokenLedger(meta, clock=_clock_at(_AUG))
    ledger.record("alice", delta_tokens=100)
    ledger.record("bob", delta_tokens=5)
    assert ledger.usage("alice") == 100
    assert ledger.usage("bob") == 5


def test_month_rollover_reopens_budget_and_resets_usage() -> None:
    """FR-2.5b auto-recovery: the counter is keyed by UTC year-month, so a fresh
    month reads zero — no rollover job, no persisted "spent" flag."""
    clock = _MutableClock(_AUG)
    ledger = TokenLedger(_FakeMeta(), clock=clock)
    ledger.record("alice", delta_tokens=1000, output_tokens=2000)
    assert ledger.usage("alice") == 3000
    clock.ts = _SEP
    assert ledger.usage("alice") == 0
    assert ledger.within_budget("alice", delta_tokens=1000) is True


# ---------------------------------------------------------------- USD projection + gate


def test_projection_math_is_exact_for_pending_dream() -> None:
    meta = _FakeMeta()
    price = PriceTable(input_usd_per_m=0.14, cache_read_usd_per_m=0.028, output_usd_per_m=0.28)
    ledger = TokenLedger(meta, budget_usd=5.0, price=price, clock=_clock_at(_AUG))
    ledger.record("alice", delta_tokens=20000)  # prior month usage metered at input rate
    projected = ledger.projection("alice", delta_tokens=1000, prefix_tokens=500, output_tokens=250)
    expected = (
        20000 * price.input_usd_per_m / 1e6
        + (1000 * price.input_usd_per_m + 500 * price.cache_read_usd_per_m + 250 * price.output_usd_per_m)
        / 1e6
    )
    assert projected == pytest.approx(expected)


def test_within_budget_is_the_gate_predicate() -> None:
    meta = _FakeMeta()
    price = PriceTable(input_usd_per_m=0.14, cache_read_usd_per_m=0.028, output_usd_per_m=0.28)
    ledger = TokenLedger(meta, budget_usd=0.001, price=price, clock=_clock_at(_AUG))
    assert ledger.within_budget("alice", delta_tokens=1000) is True  # ~$0.00014
    assert ledger.within_budget("alice", delta_tokens=9000) is False  # ~$0.00126 > $0.001


def test_usage_usd_sums_recorded_tokens_at_input_rate() -> None:
    meta = _FakeMeta()
    ledger = TokenLedger(meta, price=PriceTable(), clock=_clock_at(_AUG))
    ledger.record("alice", delta_tokens=5000, output_tokens=1000)
    assert ledger.usage_usd("alice") == pytest.approx(6000 * _DEFAULT_PRICE.input_usd_per_m / 1e6)


# ---------------------------------------------------------------- observability


def test_status_exposes_usage_and_budget_limit() -> None:
    meta = _FakeMeta()
    ledger = TokenLedger(meta, budget_usd=5.0, clock=_clock_at(_AUG))
    ledger.record("alice", delta_tokens=5000, output_tokens=1000)
    status = ledger.status("alice")
    assert isinstance(status, LedgerStatus)
    assert status.profile_id == "alice"
    assert status.year_month == "2026-08"
    assert status.used_tokens == 6000
    assert status.used_usd == pytest.approx(6000 * _DEFAULT_PRICE.input_usd_per_m / 1e6)
    assert status.budget_usd == 5.0
    assert status.remaining_usd == pytest.approx(5.0 - 6000 * _DEFAULT_PRICE.input_usd_per_m / 1e6)


def test_record_refusal_appends_audit() -> None:
    meta = _FakeMeta()
    ledger = TokenLedger(meta, budget_usd=0.01, clock=_clock_at(_AUG))
    ledger.record_refusal("alice", delta_tokens=100, prefix_tokens=50, output_tokens=25)
    assert len(meta.audit) == 1
    entry = meta.audit[0]
    assert isinstance(entry, AuditEntry)
    assert entry.action == "token_budget_cap"
    assert entry.detail["year_month"] == "2026-08"
    assert entry.detail["delta_tokens"] == 100
    assert entry.detail["budget_usd"] == 0.01


# ---------------------------------------------------------------- default parity


def test_ledger_default_budget_matches_config_default() -> None:
    """FR-2.5b default $5/month is the same number config exposes as a table."""
    assert DEFAULT_MONTHLY_BUDGET_USD == DEFAULT_DREAM_TOKEN_BUDGET_USD == 5.0
