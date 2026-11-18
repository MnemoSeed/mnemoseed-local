"""The probe loop's product decodes stay behind the typed read boundary.

Every damaged-store case runs against an isolated home under a temporary
directory: a payload the product cannot decode must surface as the instrument's
typed binding_incomplete diagnostic, never as a raw JSON or validation
traceback, and a NULL receipt identifier must never render as text "None".
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mnemoseed_local.instrument.cli import main
from mnemoseed_local.instrument.fixtures import build_repair_control_fixture
from mnemoseed_local.instrument.ledger import (
    FROZEN_SOURCE_PATHS,
    BindingIncompleteError,
    InstrumentPaths,
    canonical_json,
    open_ledger,
    read_events,
    resolve_paths,
)
from mnemoseed_local.instrument.sql_readonly import (
    RECEIPT_COLUMN_TYPES,
    crosscheck_receipts,
    read_unresolved_receipts,
    readonly_connection,
)
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver

SEED_EPOCH = 1_700_000_000.0
CONTROL_ID = "ctrl-365"
DAMAGED_PAYLOAD = "{damaged-probe-payload"
DAMAGED_TIMESTAMP = "not-a-timestamp"
CUTOFF_SECONDS = "3600"


@contextmanager
def _rows(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = readonly_connection(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as stop:
            code = int(stop.code) if isinstance(stop.code, int) else -1
    return code, out.getvalue(), err.getvalue()


def _init_argv(home: Path, manifest: Path) -> list[str]:
    return [
        "init",
        "--home",
        str(home),
        "--kind",
        "daily_cohort",
        "--control-id",
        CONTROL_ID,
        "--control-fixture",
        "control_105_cap100",
        "--source-commit",
        "a" * 40,
        "--source-manifest",
        str(manifest),
    ]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, InstrumentPaths]:
    isolated = tmp_path / "home"
    isolated.mkdir()
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(isolated))
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(
        canonical_json({path: "0" * 64 for path in sorted(FROZEN_SOURCE_PATHS)}), encoding="utf-8"
    )
    code, _, err = _run_cli(*_init_argv(isolated, manifest))
    assert code == 0, err
    return isolated, resolve_paths(isolated)


def _damage_stored_copy(
    source_db: Path, copy_db: Path, statements: tuple[tuple[str, tuple[Any, ...]], ...]
) -> None:
    """Back up one seeded store, damage the copy, and swap it into the isolated home."""
    source = sqlite3.connect(source_db)
    target = sqlite3.connect(copy_db)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    writer = sqlite3.connect(copy_db)
    try:
        for statement, params in statements:
            writer.execute(statement, params)
        writer.commit()
    finally:
        writer.close()
    shutil.copyfile(copy_db, source_db)


def _damage_graph(paths: InstrumentPaths, tmp_path: Path, target: str) -> None:
    updates: dict[str, tuple[tuple[str, tuple[Any, ...]], ...]] = {
        "current_node": (
            (
                "UPDATE nodes SET payload = ? WHERE rowid = (SELECT rowid FROM nodes ORDER BY rowid LIMIT 1)",
                (DAMAGED_PAYLOAD,),
            ),
        ),
        "version": (
            (
                "UPDATE node_versions SET payload = ? "
                "WHERE rowid = (SELECT rowid FROM node_versions ORDER BY rowid LIMIT 1)",
                (DAMAGED_PAYLOAD,),
            ),
        ),
        "outbox": (
            ("DROP TRIGGER trg_reconciliation_audit_outbox_delivered_only", ()),
            (
                "UPDATE reconciliation_audit_outbox SET detail = ? "
                "WHERE rowid = (SELECT rowid FROM reconciliation_audit_outbox ORDER BY rowid LIMIT 1)",
                (DAMAGED_PAYLOAD,),
            ),
        ),
    }
    _damage_stored_copy(paths.cohort_graph_db, tmp_path / "damaged-graph.db", updates[target])


def _damage_meta(paths: InstrumentPaths, tmp_path: Path) -> None:
    _damage_stored_copy(
        paths.cohort_meta_db,
        tmp_path / "damaged-meta.db",
        (
            (
                "INSERT INTO profiles (profile_id, display_name, created_at, archived) VALUES (?, ?, ?, 0)",
                ("probe-damaged-profile", "damaged", DAMAGED_TIMESTAMP),
            ),
        ),
    )


def _terminal_events(paths: InstrumentPaths) -> list[Any]:
    ledger = open_ledger(paths)
    try:
        return [item.event for item in read_events(ledger) if item.event.event_type == "attempt_terminal"]
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("target", "expected_reason"),
    [
        pytest.param("current_node", "product_node_page_decode_error", id="current-node-json"),
        pytest.param("version", "product_version_decode_error", id="version-pydantic"),
        pytest.param("outbox", "product_audit_decode_error", id="outbox-json"),
        pytest.param("meta", "product_profile_decode_error", id="meta-timestamp"),
    ],
)
def test_a_damaged_payload_in_the_probe_store_is_a_typed_diag_event(
    home: tuple[Path, InstrumentPaths], tmp_path: Path, target: str, expected_reason: str
) -> None:
    """A copied-store decode failure reaches the collector as a typed diagnostic."""
    isolated, paths = home
    if target == "meta":
        _damage_meta(paths, tmp_path)
    else:
        _damage_graph(paths, tmp_path, target)

    try:
        code, out, err = _run_cli("collect", "--home", str(isolated), "--cutoff-seconds", CUTOFF_SECONDS)
    except (json.JSONDecodeError, ValidationError) as exc:
        pytest.fail(f"a raw product decode escaped the probe loop: {type(exc).__name__}: {exc}")

    assert code == 1
    combined = out + err
    assert "Traceback" not in combined
    diagnostic = json.loads(err.strip())
    assert diagnostic["diag_code"] == "binding_incomplete"
    assert diagnostic["detail"]["reason"] == expected_reason

    terminals = _terminal_events(paths)
    assert terminals
    assert all(item.outcome == "failed_read" for item in terminals)
    assert all(item.diag_code == "binding_incomplete" for item in terminals)
    assert all(item.payload["failure"]["reason"] == expected_reason for item in terminals)


def test_a_healthy_probe_store_still_collects(home: tuple[Path, InstrumentPaths]) -> None:
    """An intact store keeps the frozen collect behavior: one observed_ok report."""
    isolated, _ = home
    code, out, err = _run_cli("collect", "--home", str(isolated), "--cutoff-seconds", CUTOFF_SECONDS)
    assert code == 0, err
    report = json.loads(out.strip())
    assert report["outcome"] == "observed_ok"
    assert report["diag_codes"] == []


def _control_copy(tmp_path: Path, paths: InstrumentPaths, column: str) -> Path:
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=2,
        repair_limit=100,
        seed_clock_epoch=SEED_EPOCH,
    )
    asyncio.run(control.graph.close())
    asyncio.run(control.meta.close())
    copy = tmp_path / f"receipt-{column}-copy.db"
    source = sqlite3.connect(paths.control_graph_db)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    columns = ", ".join(RECEIPT_COLUMN_TYPES)
    ddl = ", ".join(f"{name} {kind}" for name, kind in RECEIPT_COLUMN_TYPES.items())
    copies = ", ".join("NULL" if name == column else name for name in RECEIPT_COLUMN_TYPES)
    writer = sqlite3.connect(copy)
    try:
        writer.execute("ALTER TABLE reconciliation_receipts RENAME TO receipts_damaged_shape")
        writer.execute(f"CREATE TABLE reconciliation_receipts ({ddl})")
        writer.execute(
            f"INSERT INTO reconciliation_receipts ({columns}) SELECT {copies} FROM receipts_damaged_shape"
        )
        writer.execute("DROP TABLE receipts_damaged_shape")
        writer.commit()
    finally:
        writer.close()
    return copy


@pytest.mark.parametrize("column", ["nomination_id", "reason_code"])
def test_a_null_unresolved_receipt_field_is_a_typed_failure(
    home: tuple[Path, InstrumentPaths], tmp_path: Path, column: str
) -> None:
    """A copied receipt row with a NULL identifier or reason fails typed, never as text None."""
    _, paths = home
    copy = _control_copy(tmp_path, paths, column)
    with _rows(copy) as connection:
        with pytest.raises(BindingIncompleteError) as caught:
            read_unresolved_receipts(connection)
    assert caught.value.diag_code == "binding_incomplete"
    assert caught.value.detail["reason"] == "receipt_row_missing"
    assert caught.value.detail["column"] == column
    assert "None" not in json.dumps(caught.value.detail)


def test_a_null_crosscheck_nomination_id_is_a_typed_failure(
    home: tuple[Path, InstrumentPaths], tmp_path: Path
) -> None:
    """A copied receipt row with a NULL nomination id fails typed before any crosscheck."""
    _, paths = home
    copy = _control_copy(tmp_path, paths, "nomination_id")
    graph = SqliteGraphDriver(path=copy)
    try:
        with _rows(copy) as connection:
            with pytest.raises(BindingIncompleteError) as caught:
                crosscheck_receipts(connection, graph, nomination_ids=["nom-control-0000"])
        assert caught.value.diag_code == "binding_incomplete"
        assert caught.value.detail["reason"] == "receipt_row_missing"
        assert caught.value.detail["column"] == "nomination_id"
        assert "None" not in json.dumps(caught.value.detail)
    finally:
        asyncio.run(graph.close())
