"""S7 CLI restart, gap, and capture-floor surface."""

from __future__ import annotations

import contextlib
import gc
import io
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.cli import main
from mnemoseed_local.instrument.clock import parse_raw
from mnemoseed_local.instrument.ledger import (
    FROZEN_SOURCE_PATHS,
    InstrumentPaths,
    LedgerEvent,
    WriterLock,
    canonical_json,
    check_cohort_continuity,
    find_prior_unclosed_run_ids,
    open_ledger,
    read_events,
    read_run_state,
    resolve_paths,
)
from mnemoseed_local.storage.drivers._threadlocal import ThreadLocalConnections
from mnemoseed_local.storage.drivers._time import iso8601_utc

CUTOFF_SECONDS = "3600"
SOURCE_COMMIT = "16006b897a66b39f80748be85b48d46b312fce23"
DECLARED_MANIFEST = {path: "0" * 64 for path in sorted(FROZEN_SOURCE_PATHS)}


def _run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            code = main(list(argv))
        except SystemExit as stop:
            code = int(stop.code) if isinstance(stop.code, int) else -1
    return code, out.getvalue()


def _canonical(text: str) -> dict[str, Any]:
    report = json.loads(text.strip())
    assert canonical_json(report) == text.strip(), "stdout is not canonical JSON"
    return report


def _paths(home: Path) -> InstrumentPaths:
    return resolve_paths(home)


def _write_source_manifest(base: Path) -> Path:
    path = base / "source-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(DECLARED_MANIFEST), encoding="utf-8")
    return path


def _init_argv(home: Path, manifest_path: Path) -> list[str]:
    return [
        "init",
        "--home",
        str(home),
        "--kind",
        "daily_cohort",
        "--control-id",
        "ctrl-105",
        "--control-fixture",
        "control_105_cap100",
        "--source-commit",
        SOURCE_COMMIT,
        "--source-manifest",
        str(manifest_path),
    ]


def _collect_argv(home: Path) -> list[str]:
    return ["collect", "--home", str(home), "--cutoff-seconds", CUTOFF_SECONDS]


def _summary_argv(home: Path, cohort_id: str) -> list[str]:
    return ["summary", "--home", str(home), "--cohort-id", cohort_id, "--cutoff-seconds", CUTOFF_SECONDS]


def _manifest_cohort_id(home: Path) -> str:
    return str(
        json.loads((home / "instrument" / "expected_cohort.json").read_text(encoding="utf-8"))["cohort_id"]
    )


@pytest.fixture
def source_manifest_file(tmp_path: Path) -> Path:
    return _write_source_manifest(tmp_path)


@pytest.fixture
def daily_home(isolated_home: Path, source_manifest_file: Path) -> InstrumentPaths:
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_file))
    assert code == 0
    return _paths(isolated_home)


def _events(paths: InstrumentPaths) -> list[LedgerEvent]:
    ledger = open_ledger(paths)
    try:
        return [item.event for item in read_events(ledger)]
    finally:
        ledger.close()


def _events_by_id(paths: InstrumentPaths) -> dict[str, LedgerEvent]:
    return {event.event_id: event for event in _events(paths)}


def _recovering(events: dict[str, LedgerEvent], attempt_id: str) -> list[LedgerEvent]:
    return [event for event in events.values() if event.recovers_attempt_id == attempt_id]


def _pools_with_open_connections(home: Path) -> list[ThreadLocalConnections]:
    prefix = str(home)
    return [item for item in gc.get_objects() if _is_open_pool(item, prefix)]


def _is_open_pool(item: object, prefix: str) -> bool:
    if not isinstance(item, ThreadLocalConnections):
        return False
    return bool(item._open) and str(item._path).startswith(prefix)


def _run_started_row(cohort: Any, run_id: str, *, epoch: float) -> LedgerEvent:
    raw = float.hex(epoch)
    return LedgerEvent(
        event_id=str(uuid.uuid4()),
        event_type="run_started",
        cohort_id=cohort.cohort_id,
        run_id=run_id,
        attempt_id="",
        recovers_attempt_id=None,
        observation_kind=None,
        outcome=None,
        diag_code=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=None,
        target_event_id="",
        reason_code=None,
        payload={"event_type": "run_started"},
        appended_at_raw=raw,
    )


def _attempt_started_row(cohort: Any, run_id: str, attempt_id: str) -> LedgerEvent:
    return LedgerEvent(
        event_id=str(uuid.uuid4()),
        event_type="attempt_started",
        cohort_id=cohort.cohort_id,
        run_id=run_id,
        attempt_id=attempt_id,
        recovers_attempt_id=None,
        observation_kind=None,
        outcome=None,
        diag_code=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=None,
        target_event_id="",
        reason_code=None,
        payload={"event_type": "attempt_started"},
        appended_at_raw=float.hex(time.time()),
    )


def _terminal_row(
    cohort: Any,
    run_id: str,
    attempt_id: str,
    *,
    epoch: float,
    outcome: str,
    observation_kind: str,
    clock_source: str,
    calendar_eligible: int,
    recovers_attempt_id: str | None = None,
) -> LedgerEvent:
    raw = float.hex(epoch)
    iso = iso8601_utc(epoch)
    return LedgerEvent(
        event_id=str(uuid.uuid4()),
        event_type="attempt_terminal",
        cohort_id=cohort.cohort_id,
        run_id=run_id,
        attempt_id=attempt_id,
        recovers_attempt_id=recovers_attempt_id,
        observation_kind=observation_kind,
        outcome=outcome,
        diag_code=None,
        clock_source=clock_source,
        capture_pre_raw=raw,
        capture_post_raw=raw,
        monotonic_delta_ns=0,
        collected_at_raw=raw,
        collected_at_iso=iso,
        utc_date=iso[:10],
        calendar_eligible=calendar_eligible,
        target_event_id="",
        reason_code=None,
        payload={"event_type": "attempt_terminal"},
        appended_at_raw=raw,
    )


def _gap_row(cohort: Any, run_id: str, attempt_id: str, *, epoch: float) -> LedgerEvent:
    raw = float.hex(epoch)
    return LedgerEvent(
        event_id=str(uuid.uuid4()),
        event_type="instrument_gap",
        cohort_id=cohort.cohort_id,
        run_id=run_id,
        attempt_id=attempt_id,
        recovers_attempt_id=None,
        observation_kind="collect",
        outcome=None,
        diag_code="ledger_commit_unknown",
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=raw,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=0,
        target_event_id="",
        reason_code=None,
        payload={
            "event_type": "instrument_gap",
            "product_effect_may_exist": True,
            "phase": "attempt_terminal",
            "error_summary": "ledger commit outcome unknown",
        },
        appended_at_raw=raw,
    )


def _run_state_of(endpoints: InstrumentPaths, run_id: str) -> Any:
    ledger = open_ledger(endpoints)
    try:
        cohort = check_cohort_continuity(endpoints, ledger)
        return read_run_state(ledger, cohort_id=cohort.cohort_id, run_id=run_id)
    finally:
        ledger.close()


def _journal_mode(path: Path) -> str:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        connection.close()


def test_collect_under_a_held_lock_writes_only_the_sidecar(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    receipts_dir = endpoints.invocations_dir
    with WriterLock(endpoints.writer_lock):
        code, _ = _run_cli(*_collect_argv(isolated_home))
        assert code == 1
        assert _events(endpoints) == []
        assert receipts_dir.exists()
        files = sorted(receipts_dir.iterdir())
        assert len(files) == 1
        receipt = json.loads(files[0].read_text(encoding="utf-8"))
        assert receipt["receipt_version"] == 1
        assert receipt["diag_code"] == "writer_lock_held"
        assert set(receipt) == {
            "receipt_version",
            "run_id",
            "attempt_id",
            "diag_code",
            "detail",
            "recorded_at_raw",
            "recorded_at_iso",
        }
        assert files[0].name == f"{receipt['run_id']}.{receipt['attempt_id']}.receipt.json"
    with WriterLock(endpoints.writer_lock):
        code, _ = _run_cli(*_collect_argv(isolated_home))
        assert code == 1
        assert _events(endpoints) == []
        assert len(sorted(receipts_dir.iterdir())) == 2
    assert _pools_with_open_connections(isolated_home) == []


def test_a_restart_after_an_unclosed_run_records_the_restart_provenance(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    ledger = open_ledger(endpoints)
    try:
        cohort = check_cohort_continuity(endpoints, ledger)
        interrupted = str(uuid.uuid4())
        ledger.append(_run_started_row(cohort, interrupted, epoch=time.time()))
    finally:
        ledger.close()
    code, out = _run_cli(*_collect_argv(isolated_home))
    assert code == 0
    report = _canonical(out)
    assert report["run_id"] != interrupted
    started = [event for event in _events(endpoints) if event.event_type == "run_started"]
    assert len(started) == 2
    restart_payload = started[-1].payload
    assert restart_payload["restart_of_run_id"] == interrupted
    assert restart_payload["prior_run_unclosed"] == [interrupted]
    ledger = open_ledger(endpoints)
    try:
        assert find_prior_unclosed_run_ids(ledger, cohort_id=cohort.cohort_id) == (interrupted,)
    finally:
        ledger.close()


def test_recovery_evidence_is_appended_once_for_an_interrupted_attempt(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    ledger = open_ledger(endpoints)
    try:
        cohort = check_cohort_continuity(endpoints, ledger)
        run_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        base_epoch = time.time()
        ledger.append(_run_started_row(cohort, run_id, epoch=base_epoch))
        ledger.append(_attempt_started_row(cohort, run_id, attempt_id))
    finally:
        ledger.close()
    before = _events_by_id(endpoints)
    interrupted_attempt = next(
        event.attempt_id for event in before.values() if event.event_type == "attempt_started"
    )
    code, out = _run_cli(*_collect_argv(isolated_home))
    assert code == 0
    report = _canonical(out)
    assert report["outcome"] == "observed_ok"
    after = _events_by_id(endpoints)
    recoveries = _recovering(after, interrupted_attempt)
    assert len(recoveries) == 1
    recovery = recoveries[0]
    assert recovery.observation_kind == "recovery"
    assert recovery.calendar_eligible == 0
    assert recovery.clock_source == "os_utc_wall"
    assert recovery.collected_at_raw is not None
    for event_id, snapshot in before.items():
        assert after[event_id] == snapshot, "old rows must stay unchanged"
    second = _run_cli(*_collect_argv(isolated_home))
    assert second[0] == 0
    final = _events_by_id(endpoints)
    still = _recovering(final, interrupted_attempt)
    assert len(still) == 1
    for event_id, snapshot in before.items():
        assert final[event_id] == snapshot, "old rows must stay unchanged"
    assert _pools_with_open_connections(isolated_home) == []


def test_a_failed_collect_never_closes_or_succeeds_the_run(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    connection = sqlite3.connect(endpoints.cohort_graph_db, isolation_level=None)
    try:
        connection.execute("DROP TABLE reconciliation_receipts")
    finally:
        connection.close()
    code, _ = _run_cli(*_collect_argv(isolated_home))
    assert code == 1
    events = _events(endpoints)
    assert "run_closed" not in [event.event_type for event in events]
    assert not [event for event in events if event.outcome == "observed_ok"]
    ledger = open_ledger(endpoints)
    try:
        cohort = check_cohort_continuity(endpoints, ledger)
        unclosed = find_prior_unclosed_run_ids(ledger, cohort_id=cohort.cohort_id)
        assert unclosed
        state = read_run_state(ledger, cohort_id=cohort.cohort_id, run_id=unclosed[0])
        assert not state.closable
        assert state.close_block_reason is not None
    finally:
        ledger.close()


def test_a_gapped_run_stays_unclosed_after_a_later_successful_collect(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    ledger = open_ledger(endpoints)
    try:
        cohort = check_cohort_continuity(endpoints, ledger)
        run_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        past = time.time() - 7200.0
        ledger.append(_run_started_row(cohort, run_id, epoch=past))
        ledger.append(_attempt_started_row(cohort, run_id, attempt_id))
        ledger.append(_gap_row(cohort, run_id, attempt_id, epoch=past))
    finally:
        ledger.close()
    code, out = _run_cli(*_collect_argv(isolated_home))
    assert code == 0
    report = _canonical(out)
    assert report["run_id"] != run_id
    run_types = [event.event_type for event in _events(endpoints) if event.run_id == run_id]
    assert "run_closed" not in run_types
    state = _run_state_of(endpoints, run_id)
    assert state.gap_attempts == (attempt_id,)
    assert not state.closable
    assert state.close_block_reason == "gap_present"
    closed = _run_state_of(endpoints, report["run_id"])
    assert closed.closable
    ledger = open_ledger(endpoints)
    try:
        assert find_prior_unclosed_run_ids(ledger, cohort_id=cohort.cohort_id) == (run_id,)
    finally:
        ledger.close()
    assert _pools_with_open_connections(isolated_home) == []


@pytest.mark.parametrize(
    ("kind", "expects_success"),
    [
        ("failed_collect", False),
        ("recovery", True),
        ("seed_fixture_terminal", True),
        ("gap", False),
    ],
)
def test_the_capture_floor_advances_only_for_collect_rows(
    kind: str,
    expects_success: bool,
    isolated_home: Path,
    source_manifest_file: Path,
) -> None:
    endpoints = _paths(isolated_home)
    assert _run_cli(*_init_argv(isolated_home, source_manifest_file))[0] == 0
    future = time.time() + 120.0
    ledger = open_ledger(endpoints)
    try:
        cohort = check_cohort_continuity(endpoints, ledger)
        run_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        ledger.append(_run_started_row(cohort, run_id, epoch=future - 60.0))
        ledger.append(_attempt_started_row(cohort, run_id, attempt_id))
        if kind == "failed_collect":
            ledger.append(
                _terminal_row(
                    cohort,
                    run_id,
                    attempt_id,
                    epoch=future,
                    outcome="failed_read",
                    observation_kind="collect",
                    clock_source="os_utc_wall",
                    calendar_eligible=0,
                )
            )
        elif kind == "recovery":
            ledger.append(
                _terminal_row(
                    cohort,
                    run_id,
                    attempt_id,
                    epoch=future,
                    outcome="observed_ok",
                    observation_kind="recovery",
                    clock_source="os_utc_wall",
                    calendar_eligible=0,
                    recovers_attempt_id=str(uuid.uuid4()),
                )
            )
        elif kind == "seed_fixture_terminal":
            ledger.append(
                _terminal_row(
                    cohort,
                    run_id,
                    attempt_id,
                    epoch=future,
                    outcome="observed_ok",
                    observation_kind="collect",
                    clock_source="seed_fixture",
                    calendar_eligible=0,
                )
            )
        else:
            ledger.append(_gap_row(cohort, run_id, attempt_id, epoch=future))
    finally:
        ledger.close()
    code, _ = _run_cli(*_collect_argv(isolated_home))
    terminals = [event for event in _events(endpoints) if event.event_type == "attempt_terminal"]
    new_terminals = [event for event in terminals if event.run_id != run_id]
    if expects_success:
        assert code == 0
        assert any(
            event.outcome == "observed_ok" and event.observation_kind == "collect" for event in new_terminals
        )
    else:
        assert code == 1
        assert not any(event.outcome == "observed_ok" for event in new_terminals)
        assert any(
            event.outcome == "clock_invalid" and event.diag_code == "clock_backward"
            for event in new_terminals
        )


def test_thirty_synthetic_backdated_rows_make_no_thirty_day_claim(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    collect = _run_cli(*_collect_argv(isolated_home))
    assert collect[0] == 0
    real_date = _canonical(collect[1])["utc_date"]
    ledger = open_ledger(endpoints)
    try:
        cohort = check_cohort_continuity(endpoints, ledger)
        base = parse_raw(cohort.cohort_created_at_raw)
        fraud_row: LedgerEvent | None = None
        for day in range(1, 31):
            run_id = str(uuid.uuid4())
            attempt_id = str(uuid.uuid4())
            backdated = base - float(day * 86_400)
            ledger.append(_run_started_row(cohort, run_id, epoch=backdated))
            ledger.append(_attempt_started_row(cohort, run_id, attempt_id))
            appended = ledger.append(
                _terminal_row(
                    cohort,
                    run_id,
                    attempt_id,
                    epoch=backdated,
                    outcome="observed_ok",
                    observation_kind="collect",
                    clock_source="os_utc_wall",
                    calendar_eligible=1,
                )
            )
            fraud_row = appended.event
        assert fraud_row is not None
        assert fraud_row.calendar_eligible == 1
    finally:
        ledger.close()
    cohort_id = _manifest_cohort_id(isolated_home)
    summary_code, summary_out = _run_cli(*_summary_argv(isolated_home, cohort_id))
    assert summary_code == 0
    report = _canonical(summary_out)
    assert report["first_admitted_utc_date"] == real_date
    assert report["last_admitted_utc_date"] == real_date
    assert report["date_difference_days"] == 0
    assert report["numerator"] == 1
    assert report["shadowed"] == 0
    banned = {"ratified", "ratify", "gate_pass", "passed", "verdict", "owner_gate_met", "threshold"}
    assert not banned & set(report)


def test_later_collects_stay_single_attempt_and_never_renormalize(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    heads = {
        "cohort/graph.db": endpoints.cohort_graph_db,
        "control/graph.db": endpoints.control_graph_db,
    }
    code, out = _run_cli(*_collect_argv(isolated_home))
    assert code == 0
    first = _canonical(out)
    state = _run_state_of(endpoints, first["run_id"])
    assert len(state.observation_attempts) == 1
    heads_before = {name: path.read_bytes()[18:20] for name, path in heads.items()}
    code, out = _run_cli(*_collect_argv(isolated_home))
    assert code == 0
    second = _canonical(out)
    assert second["run_id"] != first["run_id"]
    assert second["attempt_id"] != first["attempt_id"]
    assert _run_state_of(endpoints, second["run_id"]).terminal_attempts == (second["attempt_id"],)
    for name, path in heads.items():
        assert path.read_bytes()[18:20] == heads_before[name]
        assert _journal_mode(path) == "delete"
    assert not Path(str(endpoints.cohort_graph_db) + "-wal").exists()
    assert not Path(str(endpoints.cohort_graph_db) + "-shm").exists()
    started = [event for event in _events(endpoints) if event.event_type == "run_started"]
    assert started[0].payload["restart_of_run_id"] is None
    assert started[0].payload["prior_run_unclosed"] == []
    summary = _run_cli(*_summary_argv(isolated_home, _manifest_cohort_id(isolated_home)))
    assert summary[0] == 0
    report = _canonical(summary[1])
    if first["utc_date"] == second["utc_date"]:
        assert report["first_admitted_utc_date"] == first["utc_date"]
        assert report["last_admitted_utc_date"] == first["utc_date"]
        assert report["date_difference_days"] == 0
    assert _pools_with_open_connections(isolated_home) == []
