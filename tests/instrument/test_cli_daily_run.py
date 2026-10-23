"""S7 CLI daily surface: init, collect, summary, and the store journal binding."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.admission import recompute_admission
from mnemoseed_local.instrument.cli import main
from mnemoseed_local.instrument.clock import parse_raw, utc_date_from_raw
from mnemoseed_local.instrument.fixtures import build_daily_cohort_fixture, build_repair_control_fixture
from mnemoseed_local.instrument.ledger import (
    FROZEN_SOURCE_PATHS,
    InstrumentPaths,
    WriterLock,
    canonical_json,
    check_cohort_continuity,
    compute_source_hash,
    find_cohort_by_control_id,
    open_ledger,
    read_events,
    resolve_paths,
    verify_config_evidence,
)
from mnemoseed_local.instrument.projection import ProjectionInput, project
from mnemoseed_local.instrument.sql_readonly import read_unresolved_receipts, readonly_connection
from mnemoseed_local.storage.drivers._threadlocal import ThreadLocalConnections
from mnemoseed_local.storage.drivers._time import iso8601_utc

DAILY_KIND = "daily_cohort"
CONTROL_KIND = "repair_control"
CONTROL_ID = "ctrl-105"
CONTROL_FIXTURE = "control_105_cap100"
SOURCE_COMMIT = "16006b897a66b39f80748be85b48d46b312fce23"
SEED_EPOCH = 1_700_000_000.0
CUTOFF_SECONDS = "3600"
CONTROL_PENDING = 105
DAILY_UNRESOLVED = ("nom-fixture-protected_endpoint", "nom-fixture-stale_revision")
STORE_NAMES = ("cohort/graph.db", "cohort/meta.db", "control/graph.db", "control/meta.db")
INIT_REPORT_KEYS = {
    "cohort_id",
    "kind",
    "control_id",
    "control_cohort_id",
    "schema_hash",
    "instrument_hash",
    "config_hash",
    "source_hash",
    "manifest_path",
}
COLLECT_REPORT_KEYS = {
    "run_id",
    "attempt_id",
    "event_id",
    "ledger_seq",
    "outcome",
    "utc_date",
    "projection",
    "diag_codes",
    "trust_codes",
}
SUMMARY_REPORT_KEYS = {
    "first_admitted_utc_date",
    "last_admitted_utc_date",
    "date_difference_days",
    "elapsed_seconds",
    "calendar_gap_days",
    "gap_dates",
    "numerator",
    "shadowed",
    "revoked",
    "projection",
    "cohort_created_at",
}
DECLARED_MANIFEST = {path: "0" * 64 for path in sorted(FROZEN_SOURCE_PATHS)}


def _raw_epoch(raw: str | None) -> float:
    return parse_raw(raw or "")


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


def _write_source_manifest(base: Path, manifest: dict[str, str]) -> Path:
    path = base / "source-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(manifest), encoding="utf-8")
    return path


def _store_path(paths: InstrumentPaths, name: str) -> Path:
    mapping = {
        "cohort/graph.db": paths.cohort_graph_db,
        "cohort/meta.db": paths.cohort_meta_db,
        "control/graph.db": paths.control_graph_db,
        "control/meta.db": paths.control_meta_db,
    }
    return mapping[name]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _store_digests(paths: InstrumentPaths) -> dict[str, str]:
    return {name: _digest(_store_path(paths, name)) for name in STORE_NAMES}


def _head_pair(path: Path) -> str:
    return path.read_bytes()[18:20].hex()


def _write_header_pair(path: Path, pair: bytes) -> None:
    data = bytearray(path.read_bytes())
    data[18:20] = pair
    path.write_bytes(bytes(data))


def _sidecars(paths: InstrumentPaths) -> list[Path]:
    found: list[Path] = []
    for name in STORE_NAMES:
        root = _store_path(paths, name)
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(root) + suffix)
            if candidate.exists():
                found.append(candidate)
    return found


def _ro_rows(db_path: Path, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, params)]
    finally:
        connection.close()


def _ro_scalar(db_path: Path, query: str) -> int:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return int(connection.execute(query).fetchone()[0])
    finally:
        connection.close()


def _journal_mode(db_path: Path) -> str:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        connection.close()


def _rows_digest(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()


def _schema_digest(db_path: Path) -> str:
    query = "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    return _rows_digest(_ro_rows(db_path, query))


def _table_info_digests(db_path: Path) -> dict[str, str]:
    return {
        table: _rows_digest(_ro_rows(db_path, f"PRAGMA table_info({table})"))
        for table in ("nodes", "node_versions", "reconciliation_receipts")
    }


def _init_argv(
    home: Path,
    source_manifest_path: Path,
    *,
    kind: str = DAILY_KIND,
    control_id: str = CONTROL_ID,
    control_fixture: str = CONTROL_FIXTURE,
) -> list[str]:
    return [
        "init",
        "--home",
        str(home),
        "--kind",
        kind,
        "--control-id",
        control_id,
        "--control-fixture",
        control_fixture,
        "--source-commit",
        SOURCE_COMMIT,
        "--source-manifest",
        str(source_manifest_path),
    ]


def _collect_argv(home: Path, *, cutoff: str = CUTOFF_SECONDS, extra: tuple[str, ...] = ()) -> list[str]:
    return ["collect", "--home", str(home), "--cutoff-seconds", cutoff, *extra]


def _summary_argv(home: Path, *, cutoff: str = CUTOFF_SECONDS, cohort_id: str | None = None) -> list[str]:
    resolved = cohort_id if cohort_id is not None else _manifest_cohort_id(home)
    return ["summary", "--home", str(home), "--cohort-id", resolved, "--cutoff-seconds", cutoff]


def _manifest_cohort_id(home: Path) -> str:
    return str(_manifest_document(home)["cohort_id"])


def _manifest_document(home: Path) -> dict[str, Any]:
    return json.loads((home / "instrument" / "expected_cohort.json").read_text(encoding="utf-8"))


def _event_types(paths: InstrumentPaths) -> tuple[str, ...]:
    ledger = open_ledger(paths)
    try:
        return tuple(item.event.event_type for item in read_events(ledger))
    finally:
        ledger.close()


def _observed_terminal_count(paths: InstrumentPaths) -> int:
    ledger = open_ledger(paths)
    try:
        rows = read_events(ledger, event_type="attempt_terminal")
        return sum(1 for item in rows if item.event.outcome == "observed_ok")
    finally:
        ledger.close()


def _latest_terminal(paths: InstrumentPaths) -> Any:
    ledger = open_ledger(paths)
    try:
        rows = read_events(ledger, event_type="attempt_terminal")
    finally:
        ledger.close()
    assert rows
    return rows[-1].event


def _pools_with_open_connections(home: Path) -> list[ThreadLocalConnections]:
    prefix = str(home)
    return [item for item in gc.get_objects() if _is_open_pool(item, prefix)]


def _is_open_pool(item: object, prefix: str) -> bool:
    if not isinstance(item, ThreadLocalConnections):
        return False
    return bool(item._open) and str(item._path).startswith(prefix)


def _scratch_paths(tmp_path: Path) -> InstrumentPaths:
    scratch = tmp_path / "scratch-home"
    return InstrumentPaths(
        home=scratch,
        ledger_db=scratch / "instrument" / "ledger.db",
        writer_lock=scratch / "instrument" / "writer.lock",
        invocations_dir=scratch / "instrument" / "invocations",
        manifest_path=scratch / "instrument" / "expected_cohort.json",
        cohort_graph_db=scratch / "cohort" / "graph.db",
        cohort_meta_db=scratch / "cohort" / "meta.db",
        control_graph_db=scratch / "control" / "graph.db",
        control_meta_db=scratch / "control" / "meta.db",
    )


def _wal_reference_digests(tmp_path: Path) -> dict[str, str]:
    """Seed the same recipes into a scratch pair left in the driver journal mode."""
    layout = _scratch_paths(tmp_path)
    daily = build_daily_cohort_fixture(layout, seed_clock_epoch=SEED_EPOCH)
    control = build_repair_control_fixture(
        layout,
        control_id=CONTROL_ID,
        pending_count=CONTROL_PENDING,
        repair_limit=100,
        seed_clock_epoch=SEED_EPOCH,
    )
    asyncio.run(daily.graph.close())
    asyncio.run(daily.meta.close())
    asyncio.run(control.graph.close())
    asyncio.run(control.meta.close())
    return {
        "cohort/graph.db": _schema_digest(layout.cohort_graph_db),
        "cohort/meta.db": _schema_digest(layout.cohort_meta_db),
        "control/graph.db": _schema_digest(layout.control_graph_db),
        "control/meta.db": _schema_digest(layout.control_meta_db),
    }


def _expected_projection(endpoints: InstrumentPaths, *, now_raw: str, cutoff: str) -> dict[str, int]:
    connection = readonly_connection(endpoints.cohort_graph_db)
    try:
        snapshots = read_unresolved_receipts(connection)
    finally:
        connection.close()
    result = project(
        [
            ProjectionInput(item.nomination_id, item.disposition, item.reason_code, item.applied_at_raw)
            for item in snapshots
        ],
        now_raw=now_raw,
        clock_source="os_utc_wall",
        cutoff_seconds=float(cutoff),
    )
    return {
        "expired": len(result.expired),
        "recent": len(result.recent),
        "unclassified": len(result.unclassified),
        "filtered_out": len(result.filtered_out),
    }


@pytest.fixture
def source_manifest_path(tmp_path: Path) -> Path:
    return _write_source_manifest(tmp_path, DECLARED_MANIFEST)


@pytest.fixture
def daily_home(isolated_home: Path, source_manifest_path: Path) -> InstrumentPaths:
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    assert code == 0
    return _paths(isolated_home)


def test_the_daily_init_registers_the_preregistration_pair(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    code, out = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    assert code == 0
    report = _canonical(out)
    assert set(report) == INIT_REPORT_KEYS
    assert report["kind"] == DAILY_KIND
    assert report["control_id"] == CONTROL_ID
    assert Path(report["manifest_path"]) == isolated_home / "instrument" / "expected_cohort.json"
    endpoints = _paths(isolated_home)
    ledger = open_ledger(endpoints)
    try:
        assert read_events(ledger) == []
        assert len(ledger.connection.execute("SELECT cohort_id FROM cohorts").fetchall()) == 2
        daily = check_cohort_continuity(endpoints, ledger)
        assert report["cohort_id"] == daily.cohort_id
        verify_config_evidence(daily, paths=endpoints)
        assert json.loads(daily.config_json)["repair_limit"] is None
        linked = find_cohort_by_control_id(ledger, control_id=CONTROL_ID)
        assert linked is not None
        assert linked.kind == CONTROL_KIND
        assert linked.cohort_id != daily.cohort_id
        verify_config_evidence(linked, paths=endpoints)
        assert json.loads(linked.config_json)["repair_limit"] == 100
    finally:
        ledger.close()
    assert _journal_mode(endpoints.ledger_db) == "delete"
    assert not endpoints.invocations_dir.exists() or list(endpoints.invocations_dir.iterdir()) == []
    assert _pools_with_open_connections(isolated_home) == []


def test_the_daily_init_accepts_a_declared_baseline_only(isolated_home: Path, tmp_path: Path) -> None:
    declared = {path: f"{index % 16:x}" * 64 for index, path in enumerate(sorted(FROZEN_SOURCE_PATHS))}
    manifest_path = _write_source_manifest(tmp_path, declared)
    code, out = _run_cli(*_init_argv(isolated_home, manifest_path))
    assert code == 0
    expected = compute_source_hash(source_commit=SOURCE_COMMIT, source_manifest=declared)[0]
    assert _canonical(out)["source_hash"] == expected
    other = {path: "f" * 64 for path in FROZEN_SOURCE_PATHS}
    other_manifest = _write_source_manifest(tmp_path / "second", other)
    elsewhere = tmp_path / "home-b"
    elsewhere.mkdir()
    code, out = _run_cli(*_init_argv(elsewhere, other_manifest))
    assert code == 0
    other_expected = compute_source_hash(source_commit=SOURCE_COMMIT, source_manifest=other)[0]
    assert _canonical(out)["source_hash"] == other_expected
    assert other_expected != expected


def test_the_daily_init_rejects_a_broken_manifest_path_set(isolated_home: Path, tmp_path: Path) -> None:
    missing = {key: "0" * 64 for key in DECLARED_MANIFEST if key != "storage/ports.py"}
    code, _ = _run_cli(*_init_argv(isolated_home, _write_source_manifest(tmp_path, missing)))
    assert code == 1
    assert not (isolated_home / "instrument").exists()
    instrumented = dict(DECLARED_MANIFEST, **{"instrument/ledger.py": "0" * 64})
    second = tmp_path / "home-b"
    second.mkdir()
    code, _ = _run_cli(*_init_argv(second, _write_source_manifest(tmp_path, instrumented)))
    assert code == 1
    assert not (second / "instrument").exists()


def test_the_daily_init_refuses_partial_state_and_a_second_initialization(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    endpoints = _paths(isolated_home)
    endpoints.cohort_graph_db.parent.mkdir(parents=True, exist_ok=True)
    endpoints.cohort_graph_db.write_bytes(b"partial")
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    assert code == 1
    assert endpoints.cohort_graph_db.read_bytes() == b"partial"
    assert not endpoints.ledger_db.exists()
    assert not endpoints.manifest_path.exists()
    endpoints.control_graph_db.parent.mkdir(parents=True, exist_ok=True)
    endpoints.control_graph_db.write_bytes(b"partial-control")
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    assert code == 1
    assert endpoints.control_graph_db.read_bytes() == b"partial-control"
    endpoints.cohort_graph_db.unlink()
    endpoints.control_graph_db.unlink()
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    assert code == 0
    digests = _store_digests(endpoints)
    ledger_digest = _digest(endpoints.ledger_db)
    manifest_bytes = endpoints.manifest_path.read_bytes()
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    assert code == 1
    assert _store_digests(endpoints) == digests
    assert _digest(endpoints.ledger_db) == ledger_digest
    assert endpoints.manifest_path.read_bytes() == manifest_bytes
    assert _pools_with_open_connections(isolated_home) == []


def test_the_daily_init_normalizes_the_four_stores_to_rollback_journal(
    isolated_home: Path, source_manifest_path: Path, tmp_path: Path
) -> None:
    endpoints = _paths(isolated_home)
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    assert code == 0
    reference = _wal_reference_digests(tmp_path)
    scratch = _scratch_paths(tmp_path)
    for name in STORE_NAMES:
        db_path = _store_path(endpoints, name)
        seeded = _store_path(scratch, name)
        assert db_path.exists()
        assert _head_pair(db_path) == "0101"
        assert _journal_mode(db_path) == "delete"
        assert _schema_digest(db_path) == reference[name]
        assert _table_info_digests(db_path) == _table_info_digests(seeded)
    assert _sidecars(endpoints) == []
    receipts = _ro_rows(
        endpoints.cohort_graph_db,
        "SELECT nomination_id, disposition, reason_code FROM reconciliation_receipts ORDER BY nomination_id",
    )
    assert [(row["nomination_id"], row["disposition"], row["reason_code"]) for row in receipts] == [
        ("nom-fixture-protected_endpoint", "unresolved", "protected_endpoint"),
        ("nom-fixture-stale_revision", "unresolved", "stale_revision"),
    ]
    pending = _ro_scalar(
        endpoints.control_graph_db,
        "SELECT COUNT(*) FROM reconciliation_audit_outbox WHERE delivered_at IS NULL",
    )
    assert pending == CONTROL_PENDING
    control_receipts = _ro_rows(endpoints.control_graph_db, "SELECT disposition FROM reconciliation_receipts")
    assert len(control_receipts) == CONTROL_PENDING
    assert {row["disposition"] for row in control_receipts} == {"unresolved"}
    assert _ro_scalar(endpoints.cohort_graph_db, "SELECT COUNT(*) FROM node_versions") >= 6
    assert _ro_scalar(endpoints.cohort_graph_db, "SELECT COUNT(*) FROM nodes") == 6


def test_the_first_collect_closes_the_lifecycle_with_the_real_capture_chain(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    code, out = _run_cli(*_collect_argv(isolated_home))
    assert code == 0
    report = _canonical(out)
    assert set(report) == COLLECT_REPORT_KEYS
    assert report["outcome"] == "observed_ok"
    assert report["diag_codes"] == []
    assert report["trust_codes"] == ["clock_future_unverifiable"]
    projection = report["projection"]
    assert set(projection) == {"expired", "recent", "unclassified", "filtered_out"}
    assert projection["unclassified"] == []
    assert projection["filtered_out"] == []
    bucketed = [*projection["expired"], *projection["recent"]]
    assert [item["nomination_id"] for item in bucketed] == list(DAILY_UNRESOLVED)
    expected_keys = {"nomination_id", "bucket", "diag_codes", "trust_codes"}
    assert all(set(item) == expected_keys and item["diag_codes"] == [] for item in bucketed)
    ledger = open_ledger(endpoints)
    try:
        appended = read_events(ledger)
        assert [item.event.event_type for item in appended] == [
            "run_started",
            "attempt_started",
            "attempt_terminal",
            "run_closed",
        ]
        daily = check_cohort_continuity(endpoints, ledger)
        started = appended[0].event
        terminal = appended[2].event
        closed = appended[3].event
        assert started.payload["restart_of_run_id"] is None
        assert started.payload["prior_run_unclosed"] == []
        assert started.payload["schema_hash"] == daily.schema_hash
        assert started.payload["instrument_hash"] == daily.instrument_hash
        assert started.payload["source_hash"] == daily.source_hash
        assert terminal.run_id == report["run_id"]
        assert terminal.attempt_id == report["attempt_id"]
        assert terminal.outcome == "observed_ok"
        assert terminal.clock_source == "os_utc_wall"
        assert terminal.calendar_eligible == 1
        assert terminal.collected_at_raw is not None
        assert terminal.collected_at_raw == terminal.capture_pre_raw
        assert _raw_epoch(terminal.capture_post_raw) >= _raw_epoch(terminal.capture_pre_raw)
        assert terminal.monotonic_delta_ns is not None and terminal.monotonic_delta_ns >= 0
        derived_date = utc_date_from_raw(terminal.collected_at_raw)
        assert terminal.utc_date == derived_date == report["utc_date"]
        assert parse_raw(terminal.collected_at_raw) >= parse_raw(daily.cohort_created_at_raw)
        assert closed.run_id == report["run_id"]
        assert closed.attempt_id == ""
        payload = closed.payload
        assert payload["terminal"] == 1 and payload["gap"] == 0 and payload["incomplete"] == 0
    finally:
        ledger.close()
    assert _event_types(_paths(isolated_home)) == (
        "run_started",
        "attempt_started",
        "attempt_terminal",
        "run_closed",
    )
    assert _pools_with_open_connections(isolated_home) == []


def test_a_fresh_collect_leaves_the_product_stores_exact(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    before = _store_digests(endpoints)
    schemas = {name: _schema_digest(_store_path(endpoints, name)) for name in STORE_NAMES}
    tables = {name: _table_info_digests(_store_path(endpoints, name)) for name in STORE_NAMES}
    leads = {name: _head_pair(_store_path(endpoints, name)) for name in STORE_NAMES}
    code, out = _run_cli(*_collect_argv(isolated_home))
    assert code == 0
    report = _canonical(out)
    assert report["outcome"] == "observed_ok"
    assert _store_digests(endpoints) == before
    for name in STORE_NAMES:
        assert _schema_digest(_store_path(endpoints, name)) == schemas[name]
        assert _table_info_digests(_store_path(endpoints, name)) == tables[name]
        assert _head_pair(_store_path(endpoints, name)) == leads[name]
        assert _journal_mode(_store_path(endpoints, name)) == "delete"
    assert _sidecars(endpoints) == []
    assert _pools_with_open_connections(isolated_home) == []


def test_summary_before_any_observation_reports_null_and_needs_no_lock(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    with WriterLock(endpoints.writer_lock) as lock:
        assert lock.path.exists()
        code, out = _run_cli(*_summary_argv(isolated_home))
        assert code == 0
        report = _canonical(out)
        assert set(report) == SUMMARY_REPORT_KEYS
        assert report["projection"] is None
        assert report["first_admitted_utc_date"] is None
        assert report["last_admitted_utc_date"] is None
        assert report["date_difference_days"] is None
        assert report["elapsed_seconds"] is None
        assert report["calendar_gap_days"] == 0
        assert report["gap_dates"] == []
        assert report["numerator"] == 0
        assert report["shadowed"] == 0
        assert report["revoked"] == 0
        assert _creation_reported_separately(report, endpoints)
        assert _pools_with_open_connections(isolated_home) == []
        digests = _store_digests(endpoints)
        ledger_digest = _digest(endpoints.ledger_db)
        manifest_bytes = endpoints.manifest_path.read_bytes()
    assert _run_cli(*_summary_argv(isolated_home))[0] == 0
    assert _store_digests(endpoints) == digests
    assert _digest(endpoints.ledger_db) == ledger_digest
    assert endpoints.manifest_path.read_bytes() == manifest_bytes
    invocations = endpoints.invocations_dir
    assert not invocations.exists() or list(invocations.iterdir()) == []


def _creation_reported_separately(report: dict[str, Any], endpoints: InstrumentPaths) -> bool:
    ledger = open_ledger(endpoints)
    try:
        raw = check_cohort_continuity(endpoints, ledger).cohort_created_at_raw
    finally:
        ledger.close()
    iso = iso8601_utc(parse_raw(raw))
    return report.get("cohort_created_at") in (raw, iso)


def test_a_complete_observation_reports_the_recorded_projection_as_of_the_ledger(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    collect = _run_cli(*_collect_argv(isolated_home))
    assert collect[0] == 0
    the_date = _canonical(collect[1])["utc_date"]
    again = _run_cli(*_collect_argv(isolated_home))
    assert again[0] == 0
    assert _canonical(again[1])["utc_date"] == the_date
    digests = _store_digests(endpoints)
    ledger_digest = _digest(endpoints.ledger_db)
    code, out = _run_cli(*_summary_argv(isolated_home))
    assert code == 0
    report = _canonical(out)
    assert set(report) == SUMMARY_REPORT_KEYS
    assert report["projection"] is not None
    assert report["date_difference_days"] == 0
    assert report["gap_dates"] == []
    assert report["elapsed_seconds"] == 0
    assert report["calendar_gap_days"] == 0
    assert report["numerator"] == 1
    assert report["shadowed"] == 1
    assert report["revoked"] == 0
    assert report["first_admitted_utc_date"] == the_date
    assert report["last_admitted_utc_date"] == the_date
    terminal = _latest_terminal(endpoints)
    assert terminal.utc_date == the_date
    assert report["projection"] == _expected_projection(
        endpoints, now_raw=terminal.collected_at_raw or "", cutoff=CUTOFF_SECONDS
    )
    ledger = open_ledger(endpoints)
    try:
        admission = recompute_admission(ledger, cohort_id=_manifest_cohort_id(isolated_home))
        assert len(admission.numerator) == 1
        assert len(admission.shadowed) == 1
        assert admission.numerator[0].utc_date == the_date
    finally:
        ledger.close()
    assert _creation_reported_separately(report, endpoints)
    assert _store_digests(endpoints) == digests
    assert _digest(endpoints.ledger_db) == ledger_digest
    assert _sidecars(endpoints) == []
    assert _pools_with_open_connections(isolated_home) == []


def test_summary_reprojects_the_recorded_inputs_with_each_explicit_cutoff(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    assert _run_cli(*_collect_argv(isolated_home))[0] == 0
    terminal = _latest_terminal(endpoints)
    digests = _store_digests(endpoints)
    manifest_bytes = endpoints.manifest_path.read_bytes()
    for cutoff, expected in (
        ("0.000001", {"expired": 2, "recent": 0, "unclassified": 0, "filtered_out": 0}),
        ("1000000000000000", {"expired": 0, "recent": 2, "unclassified": 0, "filtered_out": 0}),
    ):
        code, out = _run_cli(*_summary_argv(isolated_home, cutoff=cutoff))
        assert code == 0
        report = _canonical(out)
        assert report["projection"] == expected
        assert report["projection"] == _expected_projection(
            endpoints, now_raw=terminal.collected_at_raw or "", cutoff=cutoff
        )
    assert _store_digests(endpoints) == digests
    assert endpoints.manifest_path.read_bytes() == manifest_bytes
    assert _sidecars(endpoints) == []
    assert _pools_with_open_connections(isolated_home) == []


def test_a_non_positive_cutoff_never_falls_back_to_a_default(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    code, out = _run_cli(*_collect_argv(isolated_home, cutoff="0"))
    assert code == 0
    projection = _canonical(out)["projection"]
    assert projection["expired"] == []
    assert projection["recent"] == []
    assert [item["nomination_id"] for item in projection["unclassified"]] == list(DAILY_UNRESOLVED)
    assert all(item["diag_codes"] == ["invalid_cutoff"] for item in projection["unclassified"])
    assert projection["filtered_out"] == []
    code, out = _run_cli(*_summary_argv(isolated_home, cutoff="0"))
    assert code == 0
    assert _canonical(out)["projection"] == {
        "expired": 0,
        "recent": 0,
        "unclassified": 2,
        "filtered_out": 0,
    }


def test_an_unsupported_store_journal_fails_collect_closed_without_sidecars(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    before = _digest(endpoints.cohort_graph_db)
    sidecar = Path(str(endpoints.cohort_graph_db) + "-wal")
    sidecar.write_bytes(b"not-a-real-wal")
    code, _ = _run_cli(*_collect_argv(isolated_home))
    assert code == 1
    assert sidecar.read_bytes() == b"not-a-real-wal"
    assert _digest(endpoints.cohort_graph_db) == before
    assert _observed_terminal_count(endpoints) == 0
    assert "run_closed" not in _event_types(endpoints)
    sidecar.unlink()
    original = endpoints.cohort_graph_db.read_bytes()
    _write_header_pair(endpoints.cohort_graph_db, b"\x02\x01")
    tampered = endpoints.cohort_graph_db.read_bytes()
    assert tampered != original
    code, _ = _run_cli(*_collect_argv(isolated_home))
    assert code == 1
    assert endpoints.cohort_graph_db.read_bytes() == tampered
    assert _sidecars(endpoints) == []
    assert _observed_terminal_count(endpoints) == 0
    code, out = _run_cli(*_summary_argv(isolated_home))
    assert code == 0
    assert _creation_reported_separately(_canonical(out), endpoints)
    assert endpoints.cohort_graph_db.read_bytes() == tampered
    assert _sidecars(endpoints) == []
    assert _pools_with_open_connections(isolated_home) == []


def test_a_missing_daily_store_fails_collect_closed_without_recreating_it(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    endpoints.cohort_graph_db.unlink()
    code, _ = _run_cli(*_collect_argv(isolated_home))
    assert code == 1
    assert not endpoints.cohort_graph_db.exists()
    assert not Path(str(endpoints.cohort_graph_db) + "-wal").exists()
    assert _observed_terminal_count(endpoints) == 0
    assert "run_closed" not in _event_types(endpoints)
    assert _pools_with_open_connections(isolated_home) == []


@pytest.mark.parametrize("mode", ["table", "column"])
def test_a_corrupt_receipt_schema_fails_collect_visible(
    mode: str, daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    connection = sqlite3.connect(endpoints.cohort_graph_db, isolation_level=None)
    try:
        if mode == "table":
            connection.execute("DROP TABLE reconciliation_receipts")
        else:
            connection.execute("ALTER TABLE reconciliation_receipts DROP COLUMN loser_prior_version")
    finally:
        connection.close()
    code, _ = _run_cli(*_collect_argv(isolated_home))
    assert code == 1
    assert _observed_terminal_count(endpoints) == 0
    assert "run_closed" not in _event_types(endpoints)
    assert _pools_with_open_connections(isolated_home) == []


@pytest.mark.parametrize("mode", ["version", "triggers", "integrity"])
def test_a_corrupt_ledger_fails_visible_without_reset(
    mode: str, daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    if mode == "integrity":
        endpoints.ledger_db.write_bytes(b"not-a-sqlite-database" * 64)
    else:
        connection = sqlite3.connect(endpoints.ledger_db, isolation_level=None)
        try:
            if mode == "version":
                connection.execute("PRAGMA user_version = 2")
            else:
                connection.execute("DROP TRIGGER events_no_delete")
        finally:
            connection.close()
    ledger_bytes = endpoints.ledger_db.read_bytes()
    code, _ = _run_cli(*_collect_argv(isolated_home))
    assert code == 1
    assert endpoints.ledger_db.read_bytes() == ledger_bytes
    code, _ = _run_cli(*_summary_argv(isolated_home))
    assert code == 1
    assert endpoints.ledger_db.read_bytes() == ledger_bytes


def test_a_broken_continuity_fails_visible_without_auto_initialization(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    rows = _cohort_rows(endpoints)
    document = _manifest_document(isolated_home)
    document["config_hash"] = "1" * 64
    manifest_path = isolated_home / "instrument" / "expected_cohort.json"
    manifest_path.write_text(canonical_json(document), encoding="utf-8")
    tampered = endpoints.manifest_path.read_bytes()
    code, _ = _run_cli(*_collect_argv(isolated_home))
    assert code == 1
    assert _cohort_rows(endpoints) == rows
    code, _ = _run_cli(*_summary_argv(isolated_home))
    assert code == 1
    assert _cohort_rows(endpoints) == rows
    code, _ = _run_cli(*_collect_argv(isolated_home))
    assert code == 1
    code, _ = _run_cli(*_summary_argv(isolated_home))
    assert code == 1
    assert endpoints.manifest_path.read_bytes() == tampered
    assert _cohort_rows(endpoints) == rows
    assert _observed_terminal_count(endpoints) == 0
    assert _sidecars(endpoints) == []


def _cohort_rows(endpoints: InstrumentPaths) -> int:
    ledger = open_ledger(endpoints)
    try:
        return int(ledger.connection.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0])
    finally:
        ledger.close()


def test_missing_cutoff_and_invented_flags_refuse_usage(
    daily_home: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = daily_home
    code, _ = _run_cli("collect", "--home", str(isolated_home))
    assert code == 2
    assert _event_types(endpoints) == ()
    code, _ = _run_cli(*_collect_argv(isolated_home, extra=("--date", "2026-01-01")))
    assert code == 2
    code, _ = _run_cli(*_collect_argv(isolated_home, extra=("--ratify", "pass")))
    assert code == 2
    code, _ = _run_cli(
        "summary", "--home", str(isolated_home), "--cohort-id", _manifest_cohort_id(isolated_home)
    )
    assert code == 2
    assert _event_types(endpoints) == ()


def test_a_failed_init_normalization_stays_partial_visible(
    isolated_home: Path, source_manifest_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoints = _paths(isolated_home)
    target = str(endpoints.control_meta_db)
    original_connect = sqlite3.connect
    seen = {"count": 0}

    def _refuse_second(file: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if str(file) == target:
            seen["count"] += 1
            if seen["count"] > 1:
                raise sqlite3.OperationalError("database is locked")
        return original_connect(file, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _refuse_second)
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    monkeypatch.undo()
    assert code == 1
    assert seen["count"] > 1
    assert not endpoints.manifest_path.exists()
    assert not endpoints.ledger_db.exists()
    assert endpoints.control_meta_db.exists()
    assert endpoints.cohort_graph_db.exists()
    assert _journal_mode(endpoints.control_meta_db) == "wal"
    seeded_digest = _digest(endpoints.cohort_graph_db)
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    assert code == 1
    assert _digest(endpoints.cohort_graph_db) == seeded_digest
    assert not endpoints.manifest_path.exists()
    assert not endpoints.ledger_db.exists()


def test_the_readonly_pool_closes_the_connection_it_opened(tmp_path: Path) -> None:
    from mnemoseed_local.instrument.cli import _ReadonlyStorePool

    database = tmp_path / "readonly-pool.db"
    with contextlib.closing(sqlite3.connect(database)) as initializer:
        initializer.execute("CREATE TABLE probe (value INTEGER)")
        initializer.commit()
    pool = _ReadonlyStorePool(database)
    connection = pool.get()
    try:
        pool.close_all()
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    finally:
        connection.close()
