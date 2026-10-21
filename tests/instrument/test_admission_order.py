"""Admission recomputation: which row holds a UTC date, and which rows never can.

The admission surface derives the calendar credit of a cohort from the
immutable ledger rows. These cases pin contract-visible outcomes only: the
ordering authority, the single holder of a UTC date, the rows that are
excluded with a diagnostic, and the rows that are never candidates at all.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from mnemoseed_local.instrument.admission import recompute_admission, recompute_calendar_eligible
from mnemoseed_local.instrument.ledger import DIAG_CODES
from mnemoseed_local.storage.drivers._time import iso8601_utc

FIRST_EPOCH = 1_700_000_000.0
LATER_SAME_DAY_EPOCH = 1_700_000_600.0
NEXT_DAY_EPOCH = 1_700_086_400.0
FIRST_DATE = "2023-11-14"
NEXT_DAY_DATE = "2023-11-15"
NON_FINITE_RAW = "0x1p+1024"
MALFORMED_RAW = "not-a-raw-instant"
GAP_EPOCH = NEXT_DAY_EPOCH
AFTER_GAP_EPOCH = 1_700_087_000.0


def _ids(rows: Iterable[Any]) -> tuple[str, ...]:
    """Return the event ids of the given admission rows."""
    return tuple(row.event_id for row in rows)


def test_single_collect_holds_its_utc_date(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (sample.event.event_id,)
    assert [row.utc_date for row in admission.numerator] == [FIRST_DATE]
    assert [row.collected_at_raw for row in admission.numerator] == [sample.event.collected_at_raw]
    assert admission.shadowed == ()
    assert admission.revoked == ()
    assert admission.gap_dates == ()
    assert admission.revoked_no_survivor_dates == ()


def test_earlier_append_holds_a_date_collected_twice(initialize_home, observation_builder):
    home = initialize_home()
    first = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    second = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (first.event.event_id,)
    assert admission.numerator[0].ledger_seq == first.ledger_seq
    assert _ids(admission.shadowed) == (second.event.event_id,)
    assert admission.gap_dates == ()
    assert admission.revoked_no_survivor_dates == ()


def test_failed_attempt_does_not_hold_the_date(initialize_home, observation_builder):
    home = initialize_home()
    failed = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH, outcome="failed_read")
    valid = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (valid.event.event_id,)
    assert failed.event.event_id not in _ids(admission.numerator)
    assert failed.event.event_id not in _ids(admission.shadowed)
    assert failed.event.event_id not in _ids(admission.revoked)


def test_valid_collect_below_an_earlier_failed_attempt(initialize_home, observation_builder):
    home = initialize_home()
    observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH, outcome="failed_read")
    valid = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert admission.numerator == ()
    assert valid.event.event_id not in _ids(admission.shadowed)
    assert valid.event.event_id not in _ids(admission.revoked)


def test_stored_eligibility_bit_below_recomputed_is_excluded(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(
        home.ledger,
        home.primary,
        epoch=FIRST_EPOCH,
        calendar_eligible=0,
    )

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.excluded) == (sample.event.event_id,)
    assert admission.excluded[0].role == "calendar_eligible_mismatch"
    assert admission.numerator == ()


def test_stored_utc_date_drift_is_excluded(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(
        home.ledger,
        home.primary,
        epoch=FIRST_EPOCH,
        utc_date=NEXT_DAY_DATE,
    )

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.excluded) == (sample.event.event_id,)
    assert admission.excluded[0].role == "date_derivation_mismatch"
    assert admission.numerator == ()


def test_forged_eligibility_bit_never_holds_the_date(initialize_home, observation_builder):
    home = initialize_home()
    forged = observation_builder(
        home.ledger,
        home.primary,
        epoch=FIRST_EPOCH,
        outcome="failed_read",
        calendar_eligible=1,
    )
    valid = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (valid.event.event_id,)
    assert forged.event.event_id not in _ids(admission.numerator)
    assert forged.event.event_id not in _ids(admission.shadowed)


def test_malformed_collected_instant_is_inadmissible(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(
        home.ledger,
        home.primary,
        epoch=FIRST_EPOCH,
        collected_at_raw=MALFORMED_RAW,
    )

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert admission.numerator == ()
    assert sample.event.event_id in _ids(admission.excluded)
    assert {row.role for row in admission.excluded} <= DIAG_CODES


def test_non_finite_collected_instant_is_inadmissible(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(
        home.ledger,
        home.primary,
        epoch=FIRST_EPOCH,
        collected_at_raw=NON_FINITE_RAW,
    )

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert admission.numerator == ()
    assert sample.event.event_id in _ids(admission.excluded)
    assert {row.role for row in admission.excluded} <= DIAG_CODES


def test_collected_instant_differing_from_its_capture(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(
        home.ledger,
        home.primary,
        epoch=FIRST_EPOCH,
        collected_at_raw=float.hex(FIRST_EPOCH + 0.5),
    )

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert admission.numerator == ()
    assert sample.event.event_id in _ids(admission.excluded)
    assert {row.role for row in admission.excluded} <= DIAG_CODES


def test_recovery_row_never_holds_a_date(initialize_home, recovery_observation_builder):
    home = initialize_home()
    recovery = recovery_observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert admission.numerator == ()
    assert recovery.event.event_id not in _ids(admission.numerator)
    assert recovery.event.event_id not in _ids(admission.shadowed)
    assert recovery.event.event_id not in _ids(admission.excluded)
    assert recovery.event.event_id not in _ids(admission.revoked)


def test_a_seed_clock_row_never_holds_a_date(initialize_home, observation_builder):
    home = initialize_home()
    seeded = observation_builder(
        home.ledger,
        home.primary,
        epoch=FIRST_EPOCH,
        clock_source="seed_fixture",
        calendar_eligible=1,
    )
    valid = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (valid.event.event_id,)
    assert seeded.event.event_id not in _ids(admission.numerator)


def test_control_cohort_row_never_holds_a_date(initialize_home, observation_builder):
    home = initialize_home()
    control_sample = observation_builder(home.ledger, home.control, epoch=FIRST_EPOCH)
    daily_sample = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)

    control_admission = recompute_admission(home.ledger, cohort_id=home.control.cohort_id)
    daily_admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert control_admission.numerator == ()
    assert control_sample.event.event_id not in _ids(control_admission.numerator)
    assert _ids(daily_admission.numerator) == (daily_sample.event.event_id,)


def test_calendar_eligible_accepts_a_complete_collect_row(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    eligibility, diag_code = recompute_calendar_eligible(sample.event, home.primary)

    assert eligibility == 1
    assert diag_code is None


def test_calendar_eligible_reports_stored_bit_mismatch(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(
        home.ledger,
        home.primary,
        epoch=FIRST_EPOCH,
        calendar_eligible=0,
    )

    eligibility, diag_code = recompute_calendar_eligible(sample.event, home.primary)

    assert eligibility == 0
    assert diag_code == "calendar_eligible_mismatch"


def test_calendar_eligible_reports_date_derivation_mismatch(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(
        home.ledger,
        home.primary,
        epoch=FIRST_EPOCH,
        utc_date=NEXT_DAY_DATE,
    )

    eligibility, diag_code = recompute_calendar_eligible(sample.event, home.primary)

    assert eligibility == 0
    assert diag_code == "date_derivation_mismatch"


def test_admission_recomputation_is_deterministic(initialize_home, observation_builder):
    home = initialize_home()
    observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)

    first = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)
    second = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert first == second


def _append_gap(
    observation_builder: Any,
    ledger: Any,
    cohort: Any,
    *,
    raw: str,
    epoch: float,
) -> Any:
    """Append an instrument_gap row that carries the given collected instant text."""
    return observation_builder(
        ledger,
        cohort,
        terminal="instrument_gap",
        clock_source="os_utc_wall",
        capture_pre_raw=raw,
        capture_post_raw=raw,
        collected_at_raw=raw,
        monotonic_delta_ns=0,
        utc_date=iso8601_utc(epoch)[:10],
        collected_at_iso=iso8601_utc(epoch),
    )


def test_earlier_gap_row_bounds_a_later_collect_row(initialize_home, observation_builder, stored_row):
    home = initialize_home()
    gap = _append_gap(
        observation_builder,
        home.ledger,
        home.primary,
        raw=float.hex(GAP_EPOCH),
        epoch=GAP_EPOCH,
    )
    earlier = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert stored_row(gap.event.event_id)["collected_at_raw"] == float.hex(GAP_EPOCH)
    assert earlier.event.event_id not in _ids(admission.numerator)
    assert _ids(admission.numerator) == ()
    assert earlier.event.event_id not in _ids(admission.shadowed)
    roles = {row.role for row in admission.excluded if row.event_id == earlier.event.event_id}
    assert roles == {"clock_backward"}
    assert gap.event.event_id not in _ids(admission.numerator)


def test_collect_row_at_the_gap_instant_is_admitted(initialize_home, observation_builder):
    home = initialize_home()
    gap = _append_gap(
        observation_builder,
        home.ledger,
        home.primary,
        raw=float.hex(GAP_EPOCH),
        epoch=GAP_EPOCH,
    )
    same = observation_builder(home.ledger, home.primary, epoch=GAP_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (same.event.event_id,)
    assert admission.numerator[0].utc_date == NEXT_DAY_DATE
    assert gap.event.event_id not in _ids(admission.numerator)


def test_collect_row_after_the_gap_instant_is_admitted(initialize_home, observation_builder):
    home = initialize_home()
    gap = _append_gap(
        observation_builder,
        home.ledger,
        home.primary,
        raw=float.hex(GAP_EPOCH),
        epoch=GAP_EPOCH,
    )
    later = observation_builder(home.ledger, home.primary, epoch=AFTER_GAP_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (later.event.event_id,)
    assert admission.numerator[0].utc_date == NEXT_DAY_DATE
    assert gap.event.event_id not in _ids(admission.numerator)


def test_later_gap_row_does_not_invalidate_an_earlier_collect_row(initialize_home, observation_builder):
    home = initialize_home()
    earlier = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    gap = _append_gap(
        observation_builder,
        home.ledger,
        home.primary,
        raw=float.hex(GAP_EPOCH),
        epoch=GAP_EPOCH,
    )

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (earlier.event.event_id,)
    assert admission.numerator[0].utc_date == FIRST_DATE
    assert gap.event.event_id not in _ids(admission.numerator)


def test_gap_row_holds_no_calendar_credit(initialize_home, observation_builder):
    home = initialize_home()
    gap = _append_gap(
        observation_builder,
        home.ledger,
        home.primary,
        raw=float.hex(GAP_EPOCH),
        epoch=GAP_EPOCH,
    )

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert admission.numerator == ()
    assert gap.event.event_id not in _ids(admission.numerator)
    assert gap.event.event_id not in _ids(admission.shadowed)
    assert gap.event.event_id not in _ids(admission.revoked)


def test_malformed_gap_raw_is_safe_and_invents_no_floor(initialize_home, observation_builder, stored_row):
    home = initialize_home()
    gap = _append_gap(
        observation_builder,
        home.ledger,
        home.primary,
        raw=MALFORMED_RAW,
        epoch=GAP_EPOCH,
    )
    later = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert stored_row(gap.event.event_id)["collected_at_raw"] == MALFORMED_RAW
    assert gap.event.event_id not in _ids(admission.numerator)
    roles = {row.role for row in admission.excluded if row.event_id == later.event.event_id}
    assert roles <= set(DIAG_CODES)
    assert "clock_backward" not in roles
    admitted = _ids(admission.numerator)
    excluded = _ids(admission.excluded)
    assert later.event.event_id in admitted
    assert later.event.event_id not in excluded
