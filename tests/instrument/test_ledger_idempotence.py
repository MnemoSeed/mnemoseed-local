"""Idempotence behavior of the instrument ledger: the full application-side
column comparison, retry preservation, and the correction/revocation namespaces."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from mnemoseed_local.instrument.ledger import (
    EventRejectedError,
    InstrumentPaths,
    LedgerEvent,
    LedgerIdempotenceConflictError,
    canonical_json,
    read_events,
)

COMPARED_COLUMNS = (
    "event_id",
    "cohort_id",
    "recovers_attempt_id",
    "observation_kind",
    "outcome",
    "diag_code",
    "clock_source",
    "capture_pre_raw",
    "capture_post_raw",
    "monotonic_delta_ns",
    "collected_at_raw",
    "collected_at_iso",
    "utc_date",
    "calendar_eligible",
    "reason_code",
    "payload",
    "appended_at_raw",
)


def _changed_value(field: str, base: LedgerEvent, other_cohort_id: str) -> Any:
    if field == "event_id":
        return str(uuid.uuid4())
    if field == "cohort_id":
        return other_cohort_id
    if field == "recovers_attempt_id":
        return str(uuid.uuid4())
    if field == "observation_kind":
        return "replay_check"
    if field == "outcome":
        return "failed_read"
    if field == "diag_code":
        return "clock_backward"
    if field == "clock_source":
        return "seed_fixture"
    if field == "capture_pre_raw":
        return float.hex(1_800_000_000.0)
    if field == "capture_post_raw":
        return float.hex(1_800_000_001.0)
    if field == "monotonic_delta_ns":
        return 12345
    if field == "collected_at_raw":
        return float.hex(1_800_000_002.0)
    if field == "collected_at_iso":
        return "2030-01-01T00:00:00.000Z"
    if field == "utc_date":
        return "2030-01-01"
    if field == "calendar_eligible":
        return 0
    if field == "reason_code":
        return "double_capture"
    if field == "payload":
        return {"event_type": base.event_type, "note": "changed"}
    if field == "appended_at_raw":
        return float.hex(1_800_000_003.0)
    raise AssertionError(f"no changed value is defined for {field}")


def _correction_event(
    event_builder: Callable[..., LedgerEvent],
    cohort_id: str,
    target: LedgerEvent,
    *,
    attempt_id: str | None = None,
    reason_code: str = "double_capture",
) -> LedgerEvent:
    return event_builder(
        cohort_id,
        event_type="correction",
        run_id=target.run_id,
        attempt_id=attempt_id or str(uuid.uuid4()),
        target_event_id=target.event_id,
        reason_code=reason_code,
        observation_kind=None,
        outcome=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=0,
        payload={
            "event_type": "correction",
            "target_event_id": target.event_id,
            "reason_code": reason_code,
            "original_observation": {
                "collected_at_raw": target.collected_at_raw,
                "outcome": target.outcome,
            },
        },
    )


def _revocation_event(
    event_builder: Callable[..., LedgerEvent],
    cohort_id: str,
    target: LedgerEvent,
    *,
    attempt_id: str | None = None,
    reason_code: str = "operator_withdrawal",
) -> LedgerEvent:
    return event_builder(
        cohort_id,
        event_type="revocation",
        run_id=target.run_id,
        attempt_id=attempt_id or str(uuid.uuid4()),
        target_event_id=target.event_id,
        reason_code=reason_code,
        observation_kind=None,
        outcome=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=0,
        payload={
            "event_type": "revocation",
            "target_event_id": target.event_id,
            "reason_code": reason_code,
            "original_observation": {
                "collected_at_raw": target.collected_at_raw,
                "outcome": target.outcome,
            },
        },
    )


def test_an_identical_retry_returns_the_recorded_row(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    stored_row,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    before = stored_row(appended.event.event_id)
    retry = home.ledger.append(appended.event)
    assert retry.was_new is False
    assert retry.ledger_seq == appended.ledger_seq
    assert retry.event.event_id == appended.event.event_id
    assert retry.event.appended_at_raw == appended.event.appended_at_raw
    assert stored_row(appended.event.event_id) == before
    assert len(read_events(home.ledger, cohort_id=home.primary.cohort_id)) == 3


def test_a_retry_keeps_the_original_event_id_and_append_time(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    stored_row,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    original = stored_row(appended.event.event_id)
    retry = home.ledger.append(appended.event)
    stored = stored_row(appended.event.event_id)
    assert stored["event_id"] == original["event_id"] == appended.event.event_id
    assert stored["appended_at_raw"] == original["appended_at_raw"]
    assert retry.event.appended_at_raw == appended.event.appended_at_raw
    assert len(read_events(home.ledger, cohort_id=home.primary.cohort_id)) == 3


def test_the_payload_is_stored_canonically_and_never_reserialized(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    stored_row,
) -> None:
    home = initialize_home()
    payload = {"event_type": "attempt_terminal", "store_path": "cohort/graph.db"}
    appended = observation_builder(home.ledger, home.primary, payload=payload)
    stored = stored_row(appended.event.event_id)
    assert stored["payload_json"] == canonical_json(payload)
    retry = home.ledger.append(appended.event)
    assert retry.was_new is False
    assert stored_row(appended.event.event_id)["payload_json"] == stored["payload_json"]


def test_an_equal_payload_in_another_key_order_is_the_same_row(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    stored_row,
) -> None:
    home = initialize_home()
    payload = {"event_type": "attempt_terminal", "store_path": "cohort/graph.db"}
    appended = observation_builder(home.ledger, home.primary, payload=payload)
    reordered = replace(
        appended.event,
        payload={"store_path": "cohort/graph.db", "event_type": "attempt_terminal"},
    )
    retry = home.ledger.append(reordered)
    assert retry.was_new is False
    assert retry.ledger_seq == appended.ledger_seq
    assert stored_row(appended.event.event_id)["payload_json"] == canonical_json(payload)


@pytest.mark.parametrize("field", COMPARED_COLUMNS)
def test_a_changed_application_column_is_refused(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    recovery_observation_builder,
    stored_row,
    field: str,
) -> None:
    home = initialize_home()
    if field == "recovers_attempt_id":
        appended = recovery_observation_builder(home.ledger, home.primary)
    else:
        appended = observation_builder(home.ledger, home.primary)
    base = appended.event
    other_cohort_id = home.control.cohort_id if home.control is not None else base.cohort_id
    changed = replace(base, **{field: _changed_value(field, base, other_cohort_id)})
    before = stored_row(base.event_id)
    recorded = len(read_events(home.ledger, cohort_id=home.primary.cohort_id))
    with pytest.raises(LedgerIdempotenceConflictError) as failure:
        home.ledger.append(changed)
    assert failure.value.diag_code == "ledger_idempotence_conflict"
    assert field in str(failure.value.detail)
    assert stored_row(base.event_id) == before
    assert len(read_events(home.ledger, cohort_id=home.primary.cohort_id)) == recorded


def test_a_recovery_link_on_a_collect_is_refused_at_the_static_stage(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    stored_row,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    base = appended.event
    changed = replace(base, recovers_attempt_id=str(uuid.uuid4()))
    before = stored_row(base.event_id)
    recorded = len(read_events(home.ledger, cohort_id=home.primary.cohort_id))
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(changed)
    assert failure.value.diag_code == "binding_incomplete"
    assert failure.value.detail["stage"] == "static"
    assert failure.value.detail["field"]
    assert stored_row(base.event_id) == before
    assert len(read_events(home.ledger, cohort_id=home.primary.cohort_id)) == recorded


def test_a_different_attempt_is_a_different_idempotence_domain(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
) -> None:
    home = initialize_home()
    first = observation_builder(home.ledger, home.primary)
    second = observation_builder(home.ledger, home.primary)
    assert first.event.attempt_id != second.event.attempt_id
    assert second.ledger_seq > first.ledger_seq
    assert len(read_events(home.ledger, cohort_id=home.primary.cohort_id)) == 6


def test_a_correction_and_a_revocation_share_one_target(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    target = appended.event
    home.ledger.append(_correction_event(event_builder, home.primary.cohort_id, target))
    home.ledger.append(_revocation_event(event_builder, home.primary.cohort_id, target))
    corrections = read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="correction")
    revocations = read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="revocation")
    assert len(corrections) == 1
    assert len(revocations) == 1
    assert corrections[0].event.target_event_id == target.event_id
    assert revocations[0].event.target_event_id == target.event_id


def test_a_second_correction_on_one_target_is_refused(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    target = appended.event
    home.ledger.append(_correction_event(event_builder, home.primary.cohort_id, target))
    conflicting = _correction_event(
        event_builder,
        home.primary.cohort_id,
        target,
        reason_code="wrong_store_binding",
    )
    with pytest.raises(LedgerIdempotenceConflictError) as failure:
        home.ledger.append(conflicting)
    assert failure.value.diag_code == "ledger_idempotence_conflict"
    assert len(read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="correction")) == 1


def test_a_second_revocation_on_one_target_is_refused(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    target = appended.event
    home.ledger.append(_revocation_event(event_builder, home.primary.cohort_id, target))
    conflicting = _revocation_event(
        event_builder,
        home.primary.cohort_id,
        target,
        reason_code="double_capture",
    )
    with pytest.raises(LedgerIdempotenceConflictError) as failure:
        home.ledger.append(conflicting)
    assert failure.value.diag_code == "ledger_idempotence_conflict"
    assert len(read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="revocation")) == 1


def test_an_identical_correction_retry_returns_the_recorded_row(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    correction = _correction_event(event_builder, home.primary.cohort_id, appended.event)
    first = home.ledger.append(correction)
    retry = home.ledger.append(correction)
    assert first.was_new is True
    assert retry.was_new is False
    assert retry.ledger_seq == first.ledger_seq
    assert len(read_events(home.ledger, cohort_id=home.primary.cohort_id, event_type="correction")) == 1


def test_a_correction_of_an_unknown_target_is_refused(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    target = appended.event
    unknown = replace(target, event_id=str(uuid.uuid4()))
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(_correction_event(event_builder, home.primary.cohort_id, unknown))
    assert failure.value.diag_code == "binding_incomplete"


def test_a_revocation_with_an_unlisted_reason_is_refused(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    target = appended.event
    unlisted = _revocation_event(
        event_builder,
        home.primary.cohort_id,
        target,
        reason_code="because",
    )
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(unlisted)
    assert failure.value.diag_code == "binding_incomplete"
