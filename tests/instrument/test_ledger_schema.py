"""Schema-level behavior of the instrument ledger: table shape, guard triggers,
foreign keys, open failure modes, and cohort registration."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from mnemoseed_local.instrument.ledger import (
    CohortIdentityMismatchError,
    CohortRecord,
    InstrumentPaths,
    LedgerCorruptError,
    LedgerMissingError,
    create_ledger_schema,
    find_cohort_by_control_id,
    get_cohort,
    list_cohorts,
    open_ledger,
    register_cohort,
)

EVENT_COLUMNS = frozenset(
    {
        "ledger_seq",
        "event_id",
        "event_type",
        "cohort_id",
        "run_id",
        "attempt_id",
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
        "target_event_id",
        "reason_code",
        "payload_json",
        "appended_at_raw",
    }
)

COHORT_COLUMNS = frozenset(
    {
        "cohort_id",
        "kind",
        "cohort_created_at_raw",
        "seed_manifest_hash",
        "schema_hash",
        "instrument_hash",
        "config_hash",
        "config_json",
        "source_commit",
        "source_hash",
        "control_id",
        "fixture_seed_clock_raw",
    }
)

GUARD_TRIGGERS = frozenset({"events_no_update", "events_no_delete", "cohorts_no_update", "cohorts_no_delete"})


def _columns(db_path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def _trigger_names(db_path: Path) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        return {row[0] for row in rows}
    finally:
        connection.close()


def _user_version(db_path: Path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def test_schema_declares_the_frozen_tables_and_columns(paths: InstrumentPaths) -> None:
    create_ledger_schema(paths)
    assert _columns(paths.ledger_db, "events") == EVENT_COLUMNS
    assert _columns(paths.ledger_db, "cohorts") == COHORT_COLUMNS


def test_schema_pins_user_version_one(paths: InstrumentPaths) -> None:
    create_ledger_schema(paths)
    assert _user_version(paths.ledger_db) == 1


def test_schema_declares_the_guard_triggers(paths: InstrumentPaths) -> None:
    create_ledger_schema(paths)
    assert GUARD_TRIGGERS <= _trigger_names(paths.ledger_db)


def test_schema_enforces_the_cohort_foreign_key(paths: InstrumentPaths) -> None:
    create_ledger_schema(paths)
    connection = sqlite3.connect(paths.ledger_db)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO events (event_id, event_type, cohort_id, run_id, attempt_id,"
                " payload_json, appended_at_raw) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("event-1", "run_started", "unknown-cohort", "run-1", "", "{}", "0x1.0p+0"),
            )
    finally:
        connection.close()


def test_open_ledger_refuses_a_missing_database(paths: InstrumentPaths) -> None:
    with pytest.raises(LedgerMissingError) as failure:
        open_ledger(paths)
    assert failure.value.diag_code == "ledger_missing"
    assert not paths.ledger_db.exists()


def test_open_ledger_refuses_a_wrong_schema_version(paths: InstrumentPaths) -> None:
    create_ledger_schema(paths)
    connection = sqlite3.connect(paths.ledger_db)
    try:
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(LedgerMissingError) as failure:
        open_ledger(paths)
    assert failure.value.diag_code == "ledger_missing"
    assert "schema_version_mismatch" in str(failure.value.detail)
    assert _user_version(paths.ledger_db) == 2


def test_open_ledger_refuses_a_corrupt_database(paths: InstrumentPaths) -> None:
    paths.ledger_db.write_bytes(b"this is not a sqlite database" * 16)
    with pytest.raises(LedgerCorruptError) as failure:
        open_ledger(paths)
    assert failure.value.diag_code == "ledger_corrupt"


def test_open_ledger_refuses_a_database_without_guard_triggers(paths: InstrumentPaths) -> None:
    create_ledger_schema(paths)
    connection = sqlite3.connect(paths.ledger_db)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
        for (name,) in rows:
            connection.execute(f"DROP TRIGGER {name}")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(LedgerCorruptError) as failure:
        open_ledger(paths)
    assert failure.value.diag_code == "ledger_corrupt"


def test_register_cohort_is_idempotent_for_an_identical_row(
    paths: InstrumentPaths,
    cohort_builder: Callable[..., CohortRecord],
) -> None:
    create_ledger_schema(paths)
    ledger = open_ledger(paths)
    try:
        cohort = cohort_builder()
        first = register_cohort(ledger, cohort)
        second = register_cohort(ledger, cohort)
        assert first == second
        assert list_cohorts(ledger) == (first,)
        assert get_cohort(ledger, cohort_id=cohort.cohort_id) == first
    finally:
        ledger.close()


def test_register_cohort_refuses_a_conflicting_row(
    paths: InstrumentPaths,
    cohort_builder: Callable[..., CohortRecord],
) -> None:
    create_ledger_schema(paths)
    ledger = open_ledger(paths)
    try:
        cohort = cohort_builder()
        registered = register_cohort(ledger, cohort)
        conflicting = replace(cohort, source_commit="0123456789abcdef0123456789abcdef01234567")
        with pytest.raises(CohortIdentityMismatchError) as failure:
            register_cohort(ledger, conflicting)
        assert failure.value.diag_code == "cohort_identity_mismatch"
        assert list_cohorts(ledger) == (registered,)
    finally:
        ledger.close()


def test_find_cohort_by_control_id_never_returns_a_daily_row(
    paths: InstrumentPaths,
    cohort_builder: Callable[..., CohortRecord],
) -> None:
    create_ledger_schema(paths)
    ledger = open_ledger(paths)
    try:
        register_cohort(ledger, cohort_builder(kind="daily_cohort", control_id="ctrl-105"))
        assert find_cohort_by_control_id(ledger, control_id="ctrl-105") is None
    finally:
        ledger.close()


def test_find_cohort_by_control_id_returns_the_linked_control_row(
    paths: InstrumentPaths,
    cohort_builder: Callable[..., CohortRecord],
) -> None:
    create_ledger_schema(paths)
    ledger = open_ledger(paths)
    try:
        daily = cohort_builder(kind="daily_cohort", control_id="ctrl-105")
        control = cohort_builder(kind="repair_control", control_id="ctrl-105", repair_limit=100)
        register_cohort(ledger, daily)
        register_cohort(ledger, control)
        assert find_cohort_by_control_id(ledger, control_id="ctrl-105") == control
    finally:
        ledger.close()


def test_find_cohort_by_control_id_refuses_duplicate_control_rows(
    paths: InstrumentPaths,
    cohort_builder: Callable[..., CohortRecord],
) -> None:
    create_ledger_schema(paths)
    ledger = open_ledger(paths)
    try:
        first = cohort_builder(kind="repair_control", control_id="ctrl-105", repair_limit=100)
        second = cohort_builder(
            kind="repair_control",
            control_id="ctrl-105",
            repair_limit=100,
            cohort_created_at_epoch=1_600_000_001.0,
        )
        register_cohort(ledger, first)
        register_cohort(ledger, second)
        with pytest.raises(CohortIdentityMismatchError):
            find_cohort_by_control_id(ledger, control_id="ctrl-105")
    finally:
        ledger.close()
