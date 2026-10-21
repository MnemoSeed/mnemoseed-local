"""Append-only behavior of the instrument ledger: guard triggers, row
immutability, the static append preflight, and run lifecycle linkage."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from mnemoseed_local.instrument.ledger import (
    EventRejectedError,
    InstrumentPaths,
    LedgerEvent,
    evaluate_run_closability,
    find_prior_unclosed_run_ids,
    max_prior_collected_raw,
    read_events,
    read_run_state,
)


def _run_closed_event(
    event_builder: Callable[..., LedgerEvent],
    cohort_id: str,
    run_id: str,
    **overrides: Any,
) -> LedgerEvent:
    fields: dict[str, Any] = {
        "event_type": "run_closed",
        "run_id": run_id,
        "attempt_id": "",
        "observation_kind": None,
        "outcome": None,
        "clock_source": None,
        "capture_pre_raw": None,
        "capture_post_raw": None,
        "monotonic_delta_ns": None,
        "collected_at_raw": None,
        "collected_at_iso": None,
        "utc_date": None,
        "calendar_eligible": None,
        "payload": {
            "event_type": "run_closed",
            "terminal_count": 1,
            "incomplete_count": 0,
            "gap_count": 0,
            "prior_run_unclosed": None,
        },
    }
    fields.update(overrides)
    return event_builder(cohort_id, **fields)


def test_events_rows_refuse_updates_and_deletes(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
) -> None:
    home = initialize_home()
    observation_builder(home.ledger, home.primary)
    connection = sqlite3.connect(paths.ledger_db)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        recorded = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert recorded > 0
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE events SET outcome = 'aborted'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM events")
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == recorded
    finally:
        connection.close()


def test_cohorts_rows_refuse_updates_and_deletes(
    paths: InstrumentPaths,
    initialize_home,
) -> None:
    initialize_home()
    connection = sqlite3.connect(paths.ledger_db)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE cohorts SET config_hash = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM cohorts")
        assert connection.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0] == 2
    finally:
        connection.close()


def test_appending_never_mutates_an_existing_row(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    stored_row,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    before = stored_row(appended.event.event_id)
    observation_builder(home.ledger, home.primary)
    assert stored_row(appended.event.event_id) == before
    assert before["ledger_seq"] == appended.ledger_seq


def test_the_ledger_seq_orders_the_appended_rows(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
) -> None:
    home = initialize_home()
    first = observation_builder(home.ledger, home.primary)
    second = observation_builder(home.ledger, home.primary)
    assert second.ledger_seq > first.ledger_seq
    events = read_events(home.ledger, cohort_id=home.primary.cohort_id)
    assert [event.ledger_seq for event in events] == sorted(event.ledger_seq for event in events)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": "not_an_event_type"},
        {"event_type": "attempt_started", "attempt_id": ""},
        {"event_type": "run_started", "attempt_id": str(uuid.uuid4())},
        {"attempt_id": "not-a-uuid"},
        {"payload": {"event_type": "run_closed"}},
        {"diag_code": "not_a_diag_code"},
        {"observation_kind": "not_a_kind"},
        {"outcome": "not_an_outcome"},
        {"clock_source": "not_a_source"},
        {"utc_date": "2020-1-1"},
        {"calendar_eligible": 2},
        {"target_event_id": None},
        {"event_type": "correction", "target_event_id": ""},
        {"event_type": "revocation", "target_event_id": ""},
        {"outcome": None},
        {"clock_source": None},
        {
            "event_type": "instrument_gap",
            "diag_code": "ledger_commit_unknown",
            "observation_kind": "collect",
            "calendar_eligible": 0,
            "payload": {"event_type": "instrument_gap"},
        },
    ],
    ids=[
        "unknown-event-type",
        "sentinel-attempt-id",
        "uuid-attempt-id-on-run",
        "malformed-attempt-id",
        "payload-type-mismatch",
        "unknown-diag-code",
        "unknown-observation-kind",
        "unknown-outcome",
        "unknown-clock-source",
        "malformed-utc-date",
        "illegal-calendar-flag",
        "null-target",
        "correction-without-target",
        "revocation-without-target",
        "terminal-without-outcome",
        "terminal-without-clock-source",
        "gap-without-payload",
    ],
)
def test_the_static_preflight_refuses_malformed_events(
    paths: InstrumentPaths,
    initialize_home,
    event_builder,
    overrides: dict[str, Any],
) -> None:
    home = initialize_home()
    event = event_builder(home.primary.cohort_id, **overrides)
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(event)
    assert failure.value.diag_code == "binding_incomplete"
    assert failure.value.detail["stage"] == "static"
    assert failure.value.detail["field"]
    assert read_events(home.ledger, cohort_id=home.primary.cohort_id) == []


def test_a_closed_run_links_its_events(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    terminal = appended.event
    closed = home.ledger.append(_run_closed_event(event_builder, home.primary.cohort_id, terminal.run_id))
    assert closed.was_new
    state = read_run_state(home.ledger, cohort_id=home.primary.cohort_id, run_id=terminal.run_id)
    assert state.closable
    assert state.close_block_reason is None
    assert state.terminal_attempts == (terminal.attempt_id,)
    assert terminal.run_id not in find_prior_unclosed_run_ids(home.ledger, cohort_id=home.primary.cohort_id)


def test_an_orphan_run_cannot_be_closed(
    paths: InstrumentPaths,
    initialize_home,
    event_builder,
) -> None:
    home = initialize_home()
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(_run_closed_event(event_builder, home.primary.cohort_id, str(uuid.uuid4())))
    assert failure.value.diag_code == "binding_incomplete"
    detail = str(failure.value.detail)
    assert any(token in detail for token in ("run_started_missing", "run_not_closable", "no_attempt"))


def test_an_open_attempt_cannot_be_closed(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary, terminal=None)
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(_run_closed_event(event_builder, home.primary.cohort_id, appended.event.run_id))
    assert failure.value.diag_code == "binding_incomplete"
    detail = str(failure.value.detail)
    assert any(token in detail for token in ("run_not_closable", "open_attempt"))


def test_a_failed_attempt_cannot_be_closed(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary, outcome="failed_read")
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(_run_closed_event(event_builder, home.primary.cohort_id, appended.event.run_id))
    assert failure.value.diag_code == "binding_incomplete"
    detail = str(failure.value.detail)
    assert any(token in detail for token in ("run_not_closable", "failed_outcome"))


def test_a_gapped_attempt_cannot_be_closed(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary, terminal="instrument_gap")
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(_run_closed_event(event_builder, home.primary.cohort_id, appended.event.run_id))
    assert failure.value.diag_code == "binding_incomplete"
    detail = str(failure.value.detail)
    assert any(token in detail for token in ("run_not_closable", "gap_present"))


def test_the_closability_reasons_have_a_fixed_order(
    paths: InstrumentPaths,
    initialize_home,
    event_builder,
) -> None:
    home = initialize_home()
    cohort_id = home.primary.cohort_id
    run_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    run_started = event_builder(
        cohort_id,
        event_type="run_started",
        run_id=run_id,
        attempt_id="",
        observation_kind=None,
        outcome=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=None,
        payload={"event_type": "run_started"},
    )
    started = event_builder(
        cohort_id,
        event_type="attempt_started",
        run_id=run_id,
        attempt_id=attempt_id,
        observation_kind=None,
        outcome=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=None,
        payload={"event_type": "attempt_started"},
    )
    terminal = event_builder(
        cohort_id,
        event_type="attempt_terminal",
        run_id=run_id,
        attempt_id=attempt_id,
    )
    gap = event_builder(
        cohort_id,
        event_type="instrument_gap",
        run_id=run_id,
        attempt_id=attempt_id,
        observation_kind="collect",
        outcome=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=0,
        diag_code="ledger_commit_unknown",
        payload={
            "event_type": "instrument_gap",
            "product_effect_may_exist": True,
            "phase": "attempt_terminal",
            "error_summary": "ledger commit outcome unknown",
        },
    )
    assert evaluate_run_closability([run_started]) == (False, "no_attempt")
    assert evaluate_run_closability([run_started, started]) == (False, "open_attempt")
    assert evaluate_run_closability([run_started, started, gap]) == (False, "gap_present")
    assert evaluate_run_closability([run_started, started, terminal]) == (True, None)


def test_corrections_and_revocations_are_not_observation_attempts(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    terminal = appended.event
    correction_attempt = str(uuid.uuid4())
    correction = event_builder(
        home.primary.cohort_id,
        event_type="correction",
        run_id=terminal.run_id,
        attempt_id=correction_attempt,
        target_event_id=terminal.event_id,
        reason_code="double_capture",
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
            "target_event_id": terminal.event_id,
            "reason_code": "double_capture",
            "original_observation": {
                "collected_at_raw": terminal.collected_at_raw,
                "outcome": terminal.outcome,
            },
        },
    )
    home.ledger.append(correction)
    state = read_run_state(home.ledger, cohort_id=home.primary.cohort_id, run_id=terminal.run_id)
    assert state.observation_attempts == (terminal.attempt_id,)
    assert correction_attempt not in state.observation_attempts
    assert state.correction_rows == 1
    assert state.closable


def test_prior_unclosed_runs_stay_visible_until_the_run_closes(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
    event_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    terminal = appended.event
    assert terminal.run_id in find_prior_unclosed_run_ids(home.ledger, cohort_id=home.primary.cohort_id)
    home.ledger.append(_run_closed_event(event_builder, home.primary.cohort_id, terminal.run_id))
    assert terminal.run_id not in find_prior_unclosed_run_ids(home.ledger, cohort_id=home.primary.cohort_id)


def test_the_prior_collect_bound_includes_failed_observations(
    paths: InstrumentPaths,
    initialize_home,
    observation_builder,
) -> None:
    home = initialize_home()
    observation_builder(home.ledger, home.primary, epoch=1_610_612_736.0)
    observation_builder(home.ledger, home.primary, epoch=2_147_483_648.0, outcome="failed_read")
    assert max_prior_collected_raw(home.ledger, cohort_id=home.primary.cohort_id) == float.hex(
        2_147_483_648.0
    )


def test_an_orphan_event_is_refused(
    paths: InstrumentPaths,
    initialize_home,
    event_builder,
) -> None:
    home = initialize_home()
    event = event_builder("unknown-cohort-id")
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(event)
    assert failure.value.diag_code == "binding_incomplete"
    assert read_events(home.ledger, cohort_id=home.primary.cohort_id) == []


ORDINARY_EVENT_SHAPES: dict[str, dict[str, Any]] = {
    "run_started": {
        "event_type": "run_started",
        "attempt_id": "",
        "observation_kind": None,
        "outcome": None,
        "clock_source": None,
        "capture_pre_raw": None,
        "capture_post_raw": None,
        "monotonic_delta_ns": None,
        "collected_at_raw": None,
        "collected_at_iso": None,
        "utc_date": None,
        "calendar_eligible": None,
        "payload": {"event_type": "run_started"},
    },
    "run_closed": {
        "event_type": "run_closed",
        "attempt_id": "",
        "observation_kind": None,
        "outcome": None,
        "clock_source": None,
        "capture_pre_raw": None,
        "capture_post_raw": None,
        "monotonic_delta_ns": None,
        "collected_at_raw": None,
        "collected_at_iso": None,
        "utc_date": None,
        "calendar_eligible": None,
        "payload": {"event_type": "run_closed"},
    },
    "attempt_started": {
        "event_type": "attempt_started",
        "observation_kind": None,
        "outcome": None,
        "clock_source": None,
        "capture_pre_raw": None,
        "capture_post_raw": None,
        "monotonic_delta_ns": None,
        "collected_at_raw": None,
        "collected_at_iso": None,
        "utc_date": None,
        "calendar_eligible": None,
        "payload": {"event_type": "attempt_started"},
    },
    "attempt_terminal": {
        "event_type": "attempt_terminal",
        "payload": {"event_type": "attempt_terminal"},
    },
    "instrument_gap": {
        "event_type": "instrument_gap",
        "observation_kind": "collect",
        "outcome": None,
        "clock_source": None,
        "capture_pre_raw": None,
        "capture_post_raw": None,
        "monotonic_delta_ns": None,
        "collected_at_raw": None,
        "collected_at_iso": None,
        "utc_date": None,
        "calendar_eligible": 0,
        "diag_code": "ledger_commit_unknown",
        "payload": {
            "event_type": "instrument_gap",
            "product_effect_may_exist": True,
            "phase": "attempt_terminal",
            "error_summary": "ledger commit outcome unknown",
        },
    },
}


def _ordinary_event(
    event_builder: Callable[..., LedgerEvent],
    cohort_id: str,
    event_type: str,
    **overrides: Any,
) -> LedgerEvent:
    fields = dict(ORDINARY_EVENT_SHAPES[event_type])
    fields.update(overrides)
    return event_builder(cohort_id, **fields)


@pytest.mark.parametrize("event_type", sorted(ORDINARY_EVENT_SHAPES))
def test_an_ordinary_event_with_a_nonempty_target_is_refused(
    initialize_home,
    event_builder,
    event_type: str,
) -> None:
    home = initialize_home()
    forged = _ordinary_event(
        event_builder,
        home.primary.cohort_id,
        event_type,
        target_event_id="forged-nonempty-target",
    )
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(forged)
    assert failure.value.diag_code == "binding_incomplete"
    assert failure.value.detail == {"stage": "static", "field": "target_event_id"}
    assert read_events(home.ledger, cohort_id=home.primary.cohort_id) == []


def test_a_forged_run_started_adds_no_second_row_for_the_same_run(
    initialize_home,
    event_builder,
) -> None:
    home = initialize_home()
    run_id = str(uuid.uuid4())
    valid = _ordinary_event(event_builder, home.primary.cohort_id, "run_started", run_id=run_id)
    home.ledger.append(valid)
    forged = replace(
        valid,
        event_id=str(uuid.uuid4()),
        target_event_id="forged-nonempty-target",
    )
    with pytest.raises(EventRejectedError) as failure:
        home.ledger.append(forged)
    assert failure.value.detail == {"stage": "static", "field": "target_event_id"}
    rows = read_events(home.ledger, cohort_id=home.primary.cohort_id)
    assert len(rows) == 1
    assert rows[0].event.event_id == valid.event_id
    assert rows[0].event.run_id == run_id
    assert rows[0].event.target_event_id == ""


def test_an_ordinary_row_and_its_exact_retry_stay_one_row(
    initialize_home,
    event_builder,
) -> None:
    home = initialize_home()
    valid = _ordinary_event(event_builder, home.primary.cohort_id, "run_started")
    first = home.ledger.append(valid)
    retry = home.ledger.append(valid)
    assert first.was_new is True
    assert retry.was_new is False
    assert retry.ledger_seq == first.ledger_seq
    assert len(read_events(home.ledger, cohort_id=home.primary.cohort_id)) == 1


def test_a_correction_still_names_its_target(
    initialize_home,
    observation_builder,
) -> None:
    home = initialize_home()
    appended = observation_builder(home.ledger, home.primary)
    correction = replace(
        appended.event,
        event_id=str(uuid.uuid4()),
        event_type="correction",
        attempt_id=str(uuid.uuid4()),
        recovers_attempt_id=None,
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
        target_event_id=appended.event.event_id,
        reason_code="double_capture",
        payload={
            "event_type": "correction",
            "target_event_id": appended.event.event_id,
            "reason_code": "double_capture",
        },
    )
    recorded = home.ledger.append(correction)
    assert recorded.event.target_event_id == appended.event.event_id
