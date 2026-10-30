"""Damaged-evidence behavior of the instrument's read-only surface.

Every case works on a copied store under a temporary home: an unreadable
protected-node payload and a missing receipt timestamp must surface as the
instrument's typed binding_incomplete failure, and a failing evidence read
must leave the inspected store byte-identical.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.fixtures import (
    FixtureHandle,
    build_daily_cohort_fixture,
    build_repair_control_fixture,
)
from mnemoseed_local.instrument.ledger import BindingIncompleteError, InstrumentPaths, resolve_paths
from mnemoseed_local.instrument.sql_readonly import (
    assert_node_integrity,
    capture_node_integrity,
    read_unresolved_receipts,
    readonly_connection,
)
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver

SEED_EPOCH = 1_700_000_000.0
CONTROL_ID = "ctrl-105"
REPAIR_LIMIT = 100
DAMAGED_PAYLOAD = "{damaged-node-payload"


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> InstrumentPaths:
    """The instrument paths of one disposable home, isolated from any runtime."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    return resolve_paths(home)


@contextmanager
def _rows(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = readonly_connection(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _backup(source_db: Path, copy_path: Path) -> None:
    source = sqlite3.connect(source_db)
    copy = sqlite3.connect(copy_path)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profile_node_ids(graph: SqliteGraphDriver, node_ids: Sequence[str], profile_id: str) -> tuple[str, ...]:
    owned: list[str] = []
    for node_id in node_ids:
        node = graph.get_node(node_id)
        if node is not None and node.profile_id == profile_id:
            owned.append(node_id)
    return tuple(owned)


def _close_fixture(fixture: FixtureHandle) -> None:
    asyncio.run(fixture.graph.close())
    asyncio.run(fixture.meta.close())


@pytest.mark.parametrize(
    ("column", "value", "expected_error"),
    [
        ("payload", DAMAGED_PAYLOAD, ValueError),
        ("version", float("inf"), OverflowError),
    ],
)
def test_an_unreadable_protected_node_payload_fails_as_binding_incomplete_without_writes(
    paths: InstrumentPaths, tmp_path: Path, column: str, value: Any, expected_error: type[BaseException]
) -> None:
    """A copied store whose protected-node content cannot be decoded fails typed and unchanged."""
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    try:
        profile_id = daily.profile_ids[0]
        node_ids = _profile_node_ids(daily.graph, daily.node_ids, profile_id)
        protected_id = next(node_id for node_id in node_ids if node_id.endswith("-protected"))
        copy_path = tmp_path / "graph-copy.db"
        _backup(paths.cohort_graph_db, copy_path)
        copy_graph = SqliteGraphDriver(path=copy_path)
        try:
            with _rows(copy_path) as connection:
                baseline = capture_node_integrity(
                    copy_graph, connection, profile_id=profile_id, node_ids=node_ids
                )
            assert baseline.current_node_hashes

            writer = sqlite3.connect(copy_path)
            try:
                writer.execute(f"UPDATE nodes SET {column} = ? WHERE node_id = ?", (value, protected_id))
                writer.commit()
            finally:
                writer.close()

            intact_hash = _file_sha256(copy_path)
            with _rows(copy_path) as connection:
                with pytest.raises(BindingIncompleteError) as caught:
                    capture_node_integrity(copy_graph, connection, profile_id=profile_id, node_ids=node_ids)
            assert caught.value.diag_code == "binding_incomplete"
            assert caught.value.detail["node_id"] == protected_id
            assert isinstance(caught.value.__cause__, expected_error)
            assert _file_sha256(copy_path) == intact_hash
        finally:
            asyncio.run(copy_graph.close())
    finally:
        _close_fixture(daily)


def test_a_missing_receipt_timestamp_is_missing_evidence_not_the_text_none(
    paths: InstrumentPaths, tmp_path: Path
) -> None:
    """A copied store whose receipt timestamp is absent reports missing evidence, never text None."""
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=1,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    _close_fixture(control)
    copy_path = tmp_path / "receipt-copy.db"
    _backup(paths.control_graph_db, copy_path)

    probe = sqlite3.connect(copy_path)
    try:
        nomination_id = str(probe.execute("SELECT nomination_id FROM reconciliation_receipts").fetchone()[0])
    finally:
        probe.close()

    writer = sqlite3.connect(copy_path)
    try:
        writer.execute("ALTER TABLE reconciliation_receipts RENAME TO receipts_damaged_shape")
        writer.execute(
            "CREATE TABLE reconciliation_receipts ("
            "nomination_id TEXT, disposition TEXT, reason_code TEXT, applied_at TEXT)"
        )
        writer.execute(
            "INSERT INTO reconciliation_receipts (nomination_id, disposition, reason_code, applied_at) "
            "SELECT nomination_id, disposition, reason_code, NULL FROM receipts_damaged_shape "
            "WHERE nomination_id = ?",
            (nomination_id,),
        )
        writer.execute("DROP TABLE receipts_damaged_shape")
        writer.commit()
    finally:
        writer.close()

    intact_hash = _file_sha256(copy_path)
    with _rows(copy_path) as connection:
        with pytest.raises(BindingIncompleteError) as caught:
            read_unresolved_receipts(connection)
    assert caught.value.diag_code == "binding_incomplete"
    assert caught.value.detail["nomination_id"] == nomination_id
    assert "None" not in json.dumps(caught.value.detail)
    assert _file_sha256(copy_path) == intact_hash


def test_healthy_copied_stores_keep_the_existing_receipt_and_integrity_reads(
    paths: InstrumentPaths, tmp_path: Path
) -> None:
    """Normal receipts read verbatim and the integrity snapshot stays stable on an intact copy."""
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=2,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    try:
        profile_id = control.profile_ids[0]
        node_ids = _profile_node_ids(control.graph, control.node_ids, profile_id)
        copy_path = tmp_path / "healthy-copy.db"
        _backup(paths.control_graph_db, copy_path)
        copy_graph = SqliteGraphDriver(path=copy_path)
        try:
            with _rows(copy_path) as connection:
                snapshots = read_unresolved_receipts(connection)
                raw = connection.execute(
                    "SELECT nomination_id, applied_at FROM reconciliation_receipts"
                ).fetchall()
                before = capture_node_integrity(
                    copy_graph, connection, profile_id=profile_id, node_ids=node_ids
                )
                after = capture_node_integrity(
                    copy_graph, connection, profile_id=profile_id, node_ids=node_ids
                )
            assert_node_integrity(before, after)
            persisted = {str(row[0]): str(row[1]) for row in raw}
            assert {item.nomination_id: item.applied_at_raw for item in snapshots} == persisted
            assert all(item.applied_at_raw != "None" for item in snapshots)
        finally:
            asyncio.run(copy_graph.close())
    finally:
        _close_fixture(control)
