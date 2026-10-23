"""S7 CLI control-repair surface: frozen passes, ACK idempotence, and receipt replay."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import io
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.admission import recompute_admission
from mnemoseed_local.instrument.cli import main
from mnemoseed_local.instrument.fixtures import (
    DOWNWEIGHT_FACTOR,
    STALE_REVISION,
    FixtureHandle,
    SeedReceiptSpec,
    build_application,
)
from mnemoseed_local.instrument.ledger import (
    FROZEN_SOURCE_PATHS,
    InstrumentPaths,
    canonical_json,
    find_cohort_by_control_id,
    open_ledger,
    read_events,
    resolve_paths,
)
from mnemoseed_local.instrument.repair_oracle import assert_receipt_replay
from mnemoseed_local.instrument.sql_readonly import read_outbox_rows, readonly_connection
from mnemoseed_local.storage.drivers._threadlocal import ThreadLocalConnections
from mnemoseed_local.storage.drivers._time import epoch_from_iso
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver

SOURCE_COMMIT = "16006b897a66b39f80748be85b48d46b312fce23"
DECLARED_MANIFEST = {path: "0" * 64 for path in sorted(FROZEN_SOURCE_PATHS)}
CONTROL_ID = "ctrl-105"
LIMITED_CONTROL_ID = "ctrl-99"
PENDING_TOTAL = 105
DAILY_RECEIPTS = 2
STORE_NAMES = ("cohort/graph.db", "cohort/meta.db", "control/graph.db", "control/meta.db")


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


def _init_argv(
    home: Path,
    manifest_path: Path,
    *,
    kind: str = "daily_cohort",
    control_id: str = CONTROL_ID,
    control_fixture: str = "control_105_cap100",
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
        str(manifest_path),
    ]


def _collect_argv(home: Path) -> list[str]:
    return ["collect", "--home", str(home), "--cutoff-seconds", "3600"]


def _repair_argv(home: Path, control_id: str, limit: int, passes: int) -> list[str]:
    return [
        "control-repair",
        "--home",
        str(home),
        "--control-id",
        control_id,
        "--limit",
        str(limit),
        "--passes",
        str(passes),
    ]


def _pass_records(stdout: str) -> list[dict[str, Any]]:
    """The per-pass repair records of one command output."""
    stripped = stdout.strip()
    try:
        document = json.loads(stripped)
    except ValueError:
        return [json.loads(line) for line in stripped.splitlines() if line.strip()]
    if isinstance(document, dict) and isinstance(document.get("passes"), list):
        return document["passes"]
    if isinstance(document, list):
        return document
    return [document]


def _check_pass_records(stdout: str, expected: list[tuple[int, int, int]]) -> None:
    records = _pass_records(stdout)
    assert [record["pass_index"] for record in records] == [index for index, _, _ in expected]
    assert [record["repaired"] for record in records] == [repaired for _, repaired, _ in expected]
    remaining_now = [record["remaining_pending"] for record in records]
    assert remaining_now == [remaining for _, _, remaining in expected]
    for record in records:
        assert set(record) == {"pass_index", "repaired", "remaining_pending"}


@pytest.fixture
def source_manifest_file(tmp_path: Path) -> Path:
    return _write_source_manifest(tmp_path)


@pytest.fixture
def home_a(isolated_home: Path, source_manifest_file: Path) -> InstrumentPaths:
    code, _ = _run_cli(*_init_argv(isolated_home, source_manifest_file))
    assert code == 0
    return _paths(isolated_home)


def _outbox_snapshots(db_path: Path) -> list[Any]:
    connection = readonly_connection(db_path)
    try:
        return read_outbox_rows(connection)
    finally:
        connection.close()


def _delivered_stamps(endpoints: InstrumentPaths) -> dict[int, str | None]:
    stamps: dict[int, str | None] = {}
    for row in _outbox_snapshots(endpoints.control_graph_db):
        stamps[row.outbox_id] = row.delivered_at_raw
    return stamps


def _ro_scalar(db_path: Path, query: str) -> int:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return int(connection.execute(query).fetchone()[0])
    finally:
        connection.close()


def _ro(db_path: Path, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, params)]
    finally:
        connection.close()


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


def _head_pair(path: Path) -> str:
    return path.read_bytes()[18:20].hex()


def _journal_mode(db_path: Path) -> str:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        connection.close()


def _sidecars(paths: InstrumentPaths) -> list[Path]:
    found: list[Path] = []
    for name in STORE_NAMES:
        root = _store_path(paths, name)
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(root) + suffix)
            if candidate.exists():
                found.append(candidate)
    return found


def _pools_with_open_connections(home: Path) -> list[ThreadLocalConnections]:
    prefix = str(home)
    return [item for item in gc.get_objects() if _is_open_pool(item, prefix)]


def _is_open_pool(item: object, prefix: str) -> bool:
    if not isinstance(item, ThreadLocalConnections):
        return False
    return bool(item._open) and str(item._path).startswith(prefix)


def _control_fixture_view(endpoints: InstrumentPaths) -> FixtureHandle:
    """A fixture view over the already-seeded control pair of an initialized home."""
    graph = SqliteGraphDriver(path=endpoints.control_graph_db)
    meta = SqliteMetaDriver(path=endpoints.control_meta_db)
    ledger = open_ledger(endpoints)
    try:
        control_row = find_cohort_by_control_id(ledger, control_id=CONTROL_ID)
    finally:
        ledger.close()
    assert control_row is not None
    return FixtureHandle(
        control_id=CONTROL_ID,
        graph=graph,
        meta=meta,
        profile_ids=("instrument-profile-a", "instrument-profile-b"),
        node_ids=tuple(
            f"instrument-control-{key}{suffix}"
            for key in ("a", "b")
            for suffix in ("-left", "-right", "-protected")
        ),
        cohort=control_row,
        seed_manifest_hash=control_row.seed_manifest_hash or "",
    )


def test_the_control_repair_delivers_the_backlog_in_the_frozen_passes(
    home_a: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = home_a
    daily_digests = {
        name: _digest(_store_path(endpoints, name)) for name in ("cohort/graph.db", "cohort/meta.db")
    }
    before = {
        "control_receipts": _ro_scalar(
            endpoints.control_graph_db, "SELECT COUNT(*) FROM reconciliation_receipts"
        ),
        "cohort_receipts": _ro_scalar(
            endpoints.cohort_graph_db, "SELECT COUNT(*) FROM reconciliation_receipts"
        ),
    }
    code, out = _run_cli(*_repair_argv(isolated_home, CONTROL_ID, 100, 3))
    assert code == 0
    _check_pass_records(out, [(1, 100, 5), (2, 5, 0), (3, 0, 0)])
    assert sum(record["repaired"] for record in _pass_records(out)) == PENDING_TOTAL
    rows = _outbox_snapshots(endpoints.control_graph_db)
    assert len(rows) == PENDING_TOTAL
    assert all(row.delivered_at_raw is not None for row in rows)
    assert (
        _ro_scalar(
            endpoints.control_graph_db,
            "SELECT COUNT(*) FROM reconciliation_audit_outbox WHERE delivered_at IS NULL",
        )
        == 0
    )
    assert (
        _ro_scalar(endpoints.control_graph_db, "SELECT COUNT(*) FROM reconciliation_receipts")
        == before["control_receipts"]
    )
    assert (
        _ro_scalar(endpoints.cohort_graph_db, "SELECT COUNT(*) FROM reconciliation_receipts")
        == before["cohort_receipts"]
    )
    for name in ("control/graph.db", "control/meta.db"):
        assert _head_pair(_store_path(endpoints, name)) == "0101"
        assert _journal_mode(_store_path(endpoints, name)) == "delete"
    assert _sidecars(endpoints) == []
    for name, digest in daily_digests.items():
        assert _digest(_store_path(endpoints, name)) == digest
    ledger = open_ledger(endpoints)
    try:
        control_row = find_cohort_by_control_id(ledger, control_id=CONTROL_ID)
        assert control_row is not None
        assert recompute_admission(ledger, cohort_id=control_row.cohort_id).numerator == ()
        assert all(item.event.calendar_eligible in (0, None) for item in read_events(ledger))
    finally:
        ledger.close()
    assert _pools_with_open_connections(isolated_home) == []


def test_one_global_pass_delivers_the_rows_of_every_profile(
    home_a: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = home_a
    pending_profiles = {
        row["profile_id"]
        for row in _ro(
            endpoints.control_graph_db,
            "SELECT DISTINCT profile_id FROM reconciliation_audit_outbox WHERE delivered_at IS NULL",
        )
    }
    assert len(pending_profiles) >= 2
    code, out = _run_cli(*_repair_argv(isolated_home, CONTROL_ID, 100, 1))
    assert code == 0
    _check_pass_records(out, [(1, 100, 5)])
    delivered = _ro(
        endpoints.control_graph_db,
        "SELECT DISTINCT profile_id FROM reconciliation_audit_outbox WHERE delivered_at IS NOT NULL",
    )
    assert {row["profile_id"] for row in delivered} == pending_profiles


def test_every_delivered_row_refuses_a_second_ack_with_a_stable_raw_stamp(
    home_a: InstrumentPaths, isolated_home: Path
) -> None:
    endpoints = home_a
    assert _run_cli(*_repair_argv(isolated_home, CONTROL_ID, 100, 1))[0] == 0
    snapshots = _outbox_snapshots(endpoints.control_graph_db)
    delivered = [row for row in snapshots if row.delivered_at_raw is not None]
    undelivered = [row for row in snapshots if row.delivered_at_raw is None]
    assert len(delivered) == 100
    assert len(undelivered) == 5
    stamps = {row.outbox_id: row.delivered_at_raw for row in snapshots}
    driver = SqliteGraphDriver(path=endpoints.control_graph_db)
    try:
        for row in delivered:
            assert driver.mark_reconciliation_audit_delivered(row.outbox_id, time.time()) is False
    finally:
        asyncio.run(driver.close())
    reread = _delivered_stamps(endpoints)
    assert reread == stamps
    assert all(reread[row.outbox_id] is None for row in undelivered)


def test_a_fresh_cap_99_control_delivers_99_then_6_without_contamination(
    home_a: InstrumentPaths, source_manifest_file: Path, tmp_path: Path
) -> None:
    endpoints_a = home_a
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    code, out = _run_cli(
        *_init_argv(
            home_b,
            source_manifest_file,
            kind="repair_control",
            control_id=LIMITED_CONTROL_ID,
            control_fixture="control_105_cap99",
        )
    )
    assert code == 0
    report = _canonical(out)
    assert report["kind"] == "repair_control"
    assert report["control_id"] == LIMITED_CONTROL_ID
    assert not (home_b / "cohort").exists()
    code, out = _run_cli(*_repair_argv(home_b, LIMITED_CONTROL_ID, 99, 1))
    assert code == 0
    _check_pass_records(out, [(1, 99, 6)])
    code, out = _run_cli(*_repair_argv(home_b, LIMITED_CONTROL_ID, 99, 2))
    assert code == 0
    records = _pass_records(out)
    assert [record["repaired"] for record in records] == [6, 0]
    rows_b = _outbox_snapshots(home_b / "control" / "graph.db")
    assert len(rows_b) == PENDING_TOTAL
    assert all(row.delivered_at_raw is not None for row in rows_b)
    assert _head_pair(home_b / "control" / "graph.db") == "0101"
    untouched = _outbox_snapshots(endpoints_a.control_graph_db)
    assert len(untouched) == PENDING_TOTAL
    assert all(row.delivered_at_raw is None for row in untouched)
    daily_receipts = _ro_scalar(endpoints_a.cohort_graph_db, "SELECT COUNT(*) FROM reconciliation_receipts")
    assert daily_receipts == DAILY_RECEIPTS
    code, _ = _run_cli(*_collect_argv(home_b))
    assert code == 1
    assert not (home_b / "cohort").exists()
    assert _pools_with_open_connections(home_b) == []


def test_replaying_the_same_input_returns_the_persisted_control_receipt(
    home_a: InstrumentPaths,
    isolated_home: Path,
) -> None:
    endpoints = home_a
    assert _run_cli(*_repair_argv(isolated_home, CONTROL_ID, 100, 1))[0] == 0
    stamps = _delivered_stamps(endpoints)
    row = _ro(
        endpoints.control_graph_db,
        "SELECT profile_id, applied_at FROM reconciliation_receipts WHERE nomination_id = ?",
        ("nom-control-0000",),
    )[0]
    spec = SeedReceiptSpec(
        nomination_id="nom-control-0000",
        profile_id=str(row["profile_id"]),
        applied_at_epoch=epoch_from_iso(str(row["applied_at"])),
        kind=STALE_REVISION,
    )
    view = _control_fixture_view(endpoints)
    try:
        application = build_application(view, spec)
        assert application.applied_at == spec.applied_at_epoch
        replayed = assert_receipt_replay(view.graph, application, DOWNWEIGHT_FACTOR)
    finally:
        asyncio.run(view.graph.close())
        asyncio.run(view.meta.close())
    assert replayed.nomination_id == "nom-control-0000"
    assert str(replayed.disposition) == "unresolved"
    assert str(replayed.reason_code) == "stale_revision"
    assert replayed.downweight_factor == DOWNWEIGHT_FACTOR
    assert _head_pair(endpoints.cohort_graph_db) == "0101"
    assert _head_pair(endpoints.control_graph_db) == "0202"
    assert _delivered_stamps(endpoints) == stamps


def test_a_failed_control_normalization_fails_closed_without_rollback(
    home_a: InstrumentPaths, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoints = home_a
    daily_digests = {
        name: _digest(_store_path(endpoints, name)) for name in ("cohort/graph.db", "cohort/meta.db")
    }
    target = str(endpoints.control_graph_db)
    original_connect = sqlite3.connect
    seen = {"count": 0}

    def _refuse_second(file: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if str(file) == target:
            seen["count"] += 1
            if seen["count"] > 1:
                raise sqlite3.OperationalError("database is locked")
        return original_connect(file, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _refuse_second)
    code, _ = _run_cli(*_repair_argv(isolated_home, CONTROL_ID, 100, 3))
    monkeypatch.undo()
    assert code == 1
    assert seen["count"] > 1
    assert _head_pair(endpoints.control_graph_db) == "0202"
    assert _sidecars(endpoints) == []
    rows = _outbox_snapshots(endpoints.control_graph_db)
    assert len(rows) == PENDING_TOTAL
    assert all(row.delivered_at_raw is not None for row in rows)
    stamps = {row.outbox_id: row.delivered_at_raw for row in rows}
    for name, digest in daily_digests.items():
        assert _digest(_store_path(endpoints, name)) == digest
    code, out = _run_cli(*_repair_argv(isolated_home, CONTROL_ID, 100, 1))
    assert code == 0
    _check_pass_records(out, [(1, 0, 0)])
    assert _head_pair(endpoints.control_graph_db) == "0101"
    assert _journal_mode(endpoints.control_graph_db) == "delete"
    reread = _delivered_stamps(endpoints)
    assert reread == stamps
    for name, digest in daily_digests.items():
        assert _digest(_store_path(endpoints, name)) == digest
    assert _pools_with_open_connections(isolated_home) == []
