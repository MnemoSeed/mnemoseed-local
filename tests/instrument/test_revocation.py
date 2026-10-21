"""Revocation and correction: append rules, and their effect on the admission.

A revocation withdraws one sample without touching it. These cases pin the
append rules of the two targeted event types and the recomputation that
follows: the revocation is applied before the per-date deduplication, the
revoked rows are reported apart from the suppressed duplicates, and no failed
or withdrawn row is ever promoted into a date.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

import pytest

from mnemoseed_local.instrument.admission import append_correction, append_revocation, recompute_admission
from mnemoseed_local.instrument.ledger import (
    InstrumentError,
    find_prior_unclosed_run_ids,
    read_events,
    read_run_state,
)

FIRST_EPOCH = 1_700_000_000.0
LATER_SAME_DAY_EPOCH = 1_700_000_600.0
NEXT_DAY_EPOCH = 1_700_086_400.0
FIRST_DATE = "2023-11-14"
NEXT_DAY_DATE = "2023-11-15"
REVOCATION_REASON = "clock_evidence_invalid"


def _ids(rows: Iterable[Any]) -> tuple[str, ...]:
    """Return the event ids of the given admission rows."""
    return tuple(row.event_id for row in rows)


def _revoke(ledger, cohort_id, target, *, run_id, reason_code=REVOCATION_REASON):
    """Append one revocation of the given sample row."""
    payload = {
        "event_type": "revocation",
        "target_event_id": target.event_id,
        "reason_code": reason_code,
        "original_observation": {
            "collected_at_raw": target.collected_at_raw,
            "collected_at_iso": target.collected_at_iso,
            "utc_date": target.utc_date,
        },
    }
    return append_revocation(
        ledger,
        cohort_id=cohort_id,
        run_id=run_id,
        attempt_id=str(uuid.uuid4()),
        target_event_id=target.event_id,
        reason_code=reason_code,
        payload=payload,
    )


def _correct(ledger, cohort_id, target):
    """Append one correction of the given sample row."""
    return append_correction(
        ledger,
        cohort_id=cohort_id,
        run_id=str(uuid.uuid4()),
        attempt_id=str(uuid.uuid4()),
        target_event_id=target.event_id,
        reason_code="operator_withdrawal",
        payload={"event_type": "correction", "target_event_id": target.event_id},
    )


def test_revoking_the_only_holder_leaves_the_date_uncredited(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    _revoke(home.ledger, home.primary.cohort_id, sample.event, run_id=str(uuid.uuid4()))

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert admission.numerator == ()
    assert _ids(admission.revoked) == (sample.event.event_id,)
    assert _ids(admission.shadowed) == ()
    assert admission.revoked_no_survivor_dates == (FIRST_DATE,)
    assert admission.gap_dates == (FIRST_DATE,)


def test_revoking_the_first_of_two_rows_promotes_the_survivor(initialize_home, observation_builder):
    home = initialize_home()
    first = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    second = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)

    _revoke(home.ledger, home.primary.cohort_id, first.event, run_id=str(uuid.uuid4()))

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (second.event.event_id,)
    assert _ids(admission.revoked) == (first.event.event_id,)
    assert _ids(admission.shadowed) == ()
    assert admission.revoked_no_survivor_dates == ()
    assert admission.gap_dates == ()


def test_revoking_a_duplicate_leaves_the_earlier_holder(initialize_home, observation_builder):
    home = initialize_home()
    first = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    second = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)

    _revoke(home.ledger, home.primary.cohort_id, second.event, run_id=str(uuid.uuid4()))

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (first.event.event_id,)
    assert _ids(admission.revoked) == (second.event.event_id,)
    assert _ids(admission.shadowed) == ()


def test_revoked_rows_are_not_counted_as_shadowed_duplicates(initialize_home, observation_builder):
    home = initialize_home()
    first = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    second = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)
    third = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH + 1.0)

    _revoke(home.ledger, home.primary.cohort_id, second.event, run_id=str(uuid.uuid4()))

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (first.event.event_id,)
    assert _ids(admission.shadowed) == (third.event.event_id,)
    assert _ids(admission.revoked) == (second.event.event_id,)


def test_revoking_one_date_leaves_the_other_date_credited(initialize_home, observation_builder):
    home = initialize_home()
    first = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    second = observation_builder(home.ledger, home.primary, epoch=NEXT_DAY_EPOCH)

    _revoke(home.ledger, home.primary.cohort_id, first.event, run_id=str(uuid.uuid4()))

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (second.event.event_id,)
    assert [row.utc_date for row in admission.numerator] == [NEXT_DAY_DATE]
    assert admission.revoked_no_survivor_dates == (FIRST_DATE,)
    assert admission.gap_dates == (FIRST_DATE,)


def test_revoking_the_holder_does_not_promote_a_failed_row(initialize_home, observation_builder):
    home = initialize_home()
    failed = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH, outcome="failed_read")
    holder = observation_builder(home.ledger, home.primary, epoch=LATER_SAME_DAY_EPOCH)

    _revoke(home.ledger, home.primary.cohort_id, holder.event, run_id=str(uuid.uuid4()))

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert admission.numerator == ()
    assert failed.event.event_id not in _ids(admission.numerator)
    assert _ids(admission.revoked) == (holder.event.event_id,)
    assert admission.revoked_no_survivor_dates == (FIRST_DATE,)


def test_revocation_keeps_the_original_row_verbatim(initialize_home, observation_builder, stored_row):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    before = stored_row(sample.event.event_id)

    appended = _revoke(home.ledger, home.primary.cohort_id, sample.event, run_id=str(uuid.uuid4()))

    assert stored_row(sample.event.event_id) == before
    assert appended.event.target_event_id == sample.event.event_id
    assert appended.event.reason_code == REVOCATION_REASON
    assert appended.event.attempt_id != sample.event.attempt_id
    assert uuid.UUID(appended.event.attempt_id).version == 4
    assert appended.event.calendar_eligible in (0, None)


def test_revocation_payload_preserves_the_original_values(initialize_home, observation_builder, stored_row):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    appended = _revoke(home.ledger, home.primary.cohort_id, sample.event, run_id=str(uuid.uuid4()))

    persisted = stored_row(appended.event.event_id)
    assert persisted["event_type"] == "revocation"
    assert sample.event.event_id in persisted["payload_json"]
    assert sample.event.collected_at_raw in persisted["payload_json"]
    assert sample.event.collected_at_iso in persisted["payload_json"]


def test_revocation_with_an_unlisted_reason_code_is_refused(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    with pytest.raises(InstrumentError):
        _revoke(
            home.ledger,
            home.primary.cohort_id,
            sample.event,
            run_id=str(uuid.uuid4()),
            reason_code="not_a_reason_code",
        )

    revocations = read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="revocation")
    assert revocations == []
    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)
    assert _ids(admission.numerator) == (sample.event.event_id,)


def test_a_second_revocation_of_one_target_is_refused(initialize_home, observation_builder, stored_row):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    first = _revoke(home.ledger, home.primary.cohort_id, sample.event, run_id=str(uuid.uuid4()))
    before = stored_row(first.event.event_id)

    with pytest.raises(InstrumentError):
        _revoke(home.ledger, home.primary.cohort_id, sample.event, run_id=str(uuid.uuid4()))

    revocations = read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="revocation")
    assert len(revocations) == 1
    assert stored_row(first.event.event_id) == before


def test_a_revocation_cannot_target_a_row_of_another_cohort(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    with pytest.raises(InstrumentError):
        _revoke(home.ledger, home.control.cohort_id, sample.event, run_id=str(uuid.uuid4()))

    revocations = read_events(home.ledger, cohort_id=home.control.cohort_id, event_type="revocation")
    assert revocations == []


def test_a_correction_alone_does_not_change_the_admitted_date(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    _correct(home.ledger, home.primary.cohort_id, sample.event)

    admission = recompute_admission(home.ledger, cohort_id=home.primary.cohort_id)

    assert _ids(admission.numerator) == (sample.event.event_id,)
    assert admission.revoked == ()


def test_a_second_correction_of_one_target_is_refused(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    _correct(home.ledger, home.primary.cohort_id, sample.event)

    with pytest.raises(InstrumentError):
        _correct(home.ledger, home.primary.cohort_id, sample.event)

    corrections = read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="correction")
    assert len(corrections) == 1


def test_a_correction_and_a_revocation_of_one_target_coexist(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)

    _correct(home.ledger, home.primary.cohort_id, sample.event)
    _revoke(home.ledger, home.primary.cohort_id, sample.event, run_id=str(uuid.uuid4()))

    corrections = read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="correction")
    revocations = read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="revocation")
    assert len(corrections) == 1
    assert len(revocations) == 1
    assert corrections[0].event.target_event_id == sample.event.event_id
    assert revocations[0].event.target_event_id == sample.event.event_id


def test_a_revocation_does_not_change_run_closability(initialize_home, observation_builder):
    home = initialize_home()
    sample = observation_builder(home.ledger, home.primary, epoch=FIRST_EPOCH)
    before = read_run_state(home.ledger, cohort_id=home.primary.cohort_id, run_id=sample.event.run_id)
    assert before.closable is True
    assert before.revocation_rows == 0

    _revoke(home.ledger, home.primary.cohort_id, sample.event, run_id=sample.event.run_id)

    after = read_run_state(home.ledger, cohort_id=home.primary.cohort_id, run_id=sample.event.run_id)
    assert after.closable is True
    assert after.close_block_reason is None
    assert after.revocation_rows == 1
    assert after.observation_attempts == before.observation_attempts
    assert after.failed_outcomes == ()
    unclosed = find_prior_unclosed_run_ids(home.ledger, cohort_id=home.primary.cohort_id)
    assert unclosed == (sample.event.run_id,)
