"""Damaged-evidence behavior of the instrument's read-only surface.

Every case works on a copied store under a temporary home: an unreadable
protected-node payload, a missing receipt timestamp, a damaged outbox row,
and a damaged version-chain row must surface as the instrument's typed
binding_incomplete failure, and a failing evidence read must leave the
inspected store byte-identical.
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
from mnemoseed_local.instrument.ledger import (
    BindingIncompleteError,
    InstrumentPaths,
    canonical_json,
    resolve_paths,
    sha256_hex,
)
from mnemoseed_local.instrument.sql_readonly import (
    NODE_VERSION_COLUMNS,
    assert_node_integrity,
    capture_node_integrity,
    read_node_versions,
    read_outbox_rows,
    read_unresolved_receipts,
    readonly_connection,
)
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver

SEED_EPOCH = 1_700_000_000.0
CONTROL_ID = "ctrl-105"
REPAIR_LIMIT = 100
DAMAGED_PAYLOAD = "{damaged-node-payload"
DAMAGED_NUMBER_TEXT = "not-a-number"


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


@contextmanager
def _writer(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _recreate_with_unsupported_shape(
    writer: sqlite3.Connection, *, table: str, columns_ddl: str, copy_sql: str
) -> None:
    """Rebuild one table without its constraints, carrying the original rows through copy_sql."""
    writer.execute(f"ALTER TABLE {table} RENAME TO {table}_damaged_shape")
    writer.execute(f"CREATE TABLE {table} ({columns_ddl})")
    writer.execute(copy_sql)
    writer.execute(f"DROP TABLE {table}_damaged_shape")


def _store_version_node_ids(db_path: Path, profile_id: str) -> tuple[str, ...]:
    with _rows(db_path) as connection:
        rows = connection.execute(
            "SELECT DISTINCT node_id FROM node_versions WHERE profile_id = ? ORDER BY node_id",
            (profile_id,),
        ).fetchall()
    assert rows
    return tuple(str(row[0]) for row in rows)


def _first_version_row(db_path: Path, profile_id: str) -> tuple[str, int]:
    with _rows(db_path) as connection:
        row = connection.execute(
            "SELECT node_id, version FROM node_versions WHERE profile_id = ? ORDER BY node_id, version",
            (profile_id,),
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1])


def _expected_version_row_hash(raw: Sequence[Any]) -> str:
    """The documented row-hash contract: an absent cell stays absent, every other cell keeps its text."""
    payload = {
        name: None if value is None else str(value)
        for name, value in zip(NODE_VERSION_COLUMNS, tuple(raw), strict=True)
    }
    return sha256_hex(canonical_json(payload).encode("utf-8"))


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


@pytest.mark.parametrize(
    ("id_expression", "created_at_expression", "expected_reason", "expected_column", "expected_cause"),
    [
        pytest.param("NULL", "created_at", "outbox_row_missing", "id", None, id="null_id"),
        pytest.param("id", "NULL", "outbox_row_missing", "created_at", None, id="null_created_at"),
        pytest.param(
            f"'{DAMAGED_NUMBER_TEXT}'",
            "created_at",
            "outbox_row_decode_error",
            "id",
            ValueError,
            id="text_id",
        ),
    ],
)
def test_a_damaged_outbox_row_fails_typed_without_writes(
    paths: InstrumentPaths,
    tmp_path: Path,
    id_expression: str,
    created_at_expression: str,
    expected_reason: str,
    expected_column: str,
    expected_cause: type[BaseException] | None,
) -> None:
    """A copied store whose outbox row loses a required column or carries an undecodable id fails typed."""
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=2,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    _close_fixture(control)
    copy_path = tmp_path / "outbox-copy.db"
    _backup(paths.control_graph_db, copy_path)
    with _writer(copy_path) as writer:
        _recreate_with_unsupported_shape(
            writer,
            table="reconciliation_audit_outbox",
            columns_ddl=(
                "id INTEGER, nomination_id TEXT, profile_id TEXT, action TEXT, "
                "created_at TEXT, delivered_at TEXT"
            ),
            copy_sql=(
                "INSERT INTO reconciliation_audit_outbox "
                "(id, nomination_id, profile_id, action, created_at) "
                f"SELECT {id_expression}, nomination_id, profile_id, action, {created_at_expression} "
                "FROM reconciliation_audit_outbox_damaged_shape"
            ),
        )

    intact_hash = _file_sha256(copy_path)
    with _rows(copy_path) as connection:
        with pytest.raises(BindingIncompleteError) as caught:
            read_outbox_rows(connection)
    assert caught.value.diag_code == "binding_incomplete"
    assert caught.value.detail["reason"] == expected_reason
    assert caught.value.detail["column"] == expected_column
    assert "None" not in json.dumps(caught.value.detail)
    if expected_cause is None:
        assert caught.value.__cause__ is None
    else:
        assert isinstance(caught.value.__cause__, expected_cause)
    assert _file_sha256(copy_path) == intact_hash


def test_an_undecodable_version_number_fails_typed_without_writes(
    paths: InstrumentPaths, tmp_path: Path
) -> None:
    """A copied store whose version number is stored as undecodable text fails typed, never raw."""
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    try:
        profile_id = daily.profile_ids[0]
        node_ids = _store_version_node_ids(paths.cohort_graph_db, profile_id)
        copy_path = tmp_path / "versions-copy.db"
        _backup(paths.cohort_graph_db, copy_path)
        target = _first_version_row(copy_path, profile_id)
        with _writer(copy_path) as writer:
            writer.execute(
                "UPDATE node_versions SET version = ? WHERE node_id = ? AND version = ?",
                (DAMAGED_NUMBER_TEXT, target[0], target[1]),
            )

        intact_hash = _file_sha256(copy_path)
        with _rows(copy_path) as connection:
            with pytest.raises(BindingIncompleteError) as caught:
                read_node_versions(connection, profile_id=profile_id, node_ids=node_ids)
        assert caught.value.diag_code == "binding_incomplete"
        assert caught.value.detail["reason"] == "node_version_row_decode_error"
        assert caught.value.detail["column"] == "version"
        assert caught.value.detail["node_id"] == target[0]
        assert isinstance(caught.value.__cause__, ValueError)
        assert _file_sha256(copy_path) == intact_hash
    finally:
        _close_fixture(daily)


@pytest.mark.parametrize("damaged_column", ["version", "changed_at"])
def test_an_absent_required_version_column_fails_typed_without_writes(
    paths: InstrumentPaths, tmp_path: Path, damaged_column: str
) -> None:
    """A copied store whose version row loses a required column reports missing evidence, never text None."""
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    try:
        profile_id = daily.profile_ids[0]
        node_ids = _store_version_node_ids(paths.cohort_graph_db, profile_id)
        copy_path = tmp_path / "versions-copy.db"
        _backup(paths.cohort_graph_db, copy_path)
        select_expressions = ["NULL" if name == damaged_column else name for name in NODE_VERSION_COLUMNS]
        with _writer(copy_path) as writer:
            _recreate_with_unsupported_shape(
                writer,
                table="node_versions",
                columns_ddl=(
                    "node_id TEXT, version INTEGER, profile_id TEXT, valid_from TEXT, valid_to TEXT, "
                    "superseded_by INTEGER, changed_at TEXT, payload TEXT"
                ),
                copy_sql=(
                    f"INSERT INTO node_versions ({', '.join(NODE_VERSION_COLUMNS)}) "
                    f"SELECT {', '.join(select_expressions)} FROM node_versions_damaged_shape"
                ),
            )

        intact_hash = _file_sha256(copy_path)
        with _rows(copy_path) as connection:
            with pytest.raises(BindingIncompleteError) as caught:
                read_node_versions(connection, profile_id=profile_id, node_ids=node_ids)
        assert caught.value.diag_code == "binding_incomplete"
        assert caught.value.detail["reason"] == "node_version_row_missing"
        assert caught.value.detail["column"] == damaged_column
        assert "None" not in json.dumps(caught.value.detail)
        assert _file_sha256(copy_path) == intact_hash
    finally:
        _close_fixture(daily)


def test_the_version_row_hash_preserves_an_absent_cell_as_absent(paths: InstrumentPaths) -> None:
    """The row hash renders an absent nullable cell as JSON null, never as the text None."""
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    try:
        profile_id = daily.profile_ids[0]
        node_ids = _store_version_node_ids(paths.cohort_graph_db, profile_id)
        with _rows(paths.cohort_graph_db) as connection:
            raw = connection.execute(
                "SELECT node_id, version, profile_id, valid_from, valid_to, superseded_by, "
                "changed_at, payload FROM node_versions WHERE profile_id = ? ORDER BY node_id, version",
                (profile_id,),
            ).fetchall()
            snapshots = read_node_versions(connection, profile_id=profile_id, node_ids=node_ids)
        assert any(item[4] is None or item[5] is None for item in raw)
        expected = {f"{str(item[0])}:{int(item[1])}": _expected_version_row_hash(tuple(item)) for item in raw}
        assert {f"{item.node_id}:{item.version}": item.row_hash for item in snapshots} == expected
    finally:
        _close_fixture(daily)


def test_a_damaged_version_payload_stays_verbatim_evidence(paths: InstrumentPaths, tmp_path: Path) -> None:
    """The typed version read never decodes a payload: damaged content is hashed verbatim, never repaired."""
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    try:
        profile_id = daily.profile_ids[0]
        node_ids = _store_version_node_ids(paths.cohort_graph_db, profile_id)
        copy_path = tmp_path / "payload-copy.db"
        _backup(paths.cohort_graph_db, copy_path)
        target = _first_version_row(copy_path, profile_id)
        with _writer(copy_path) as writer:
            writer.execute(
                "UPDATE node_versions SET payload = ? WHERE node_id = ? AND version = ?",
                (DAMAGED_PAYLOAD, target[0], target[1]),
            )

        intact_hash = _file_sha256(copy_path)
        with _rows(copy_path) as connection:
            snapshots = read_node_versions(connection, profile_id=profile_id, node_ids=node_ids)
            raw = connection.execute(
                "SELECT node_id, version, profile_id, valid_from, valid_to, superseded_by, "
                "changed_at, payload FROM node_versions WHERE node_id = ? AND version = ?",
                target,
            ).fetchone()
        assert raw is not None
        damaged = next(item for item in snapshots if item.node_id == target[0] and item.version == target[1])
        assert damaged.row_hash == _expected_version_row_hash(tuple(raw))
        assert _file_sha256(copy_path) == intact_hash
    finally:
        _close_fixture(daily)


def test_healthy_outbox_rows_keep_the_persisted_stamps(paths: InstrumentPaths) -> None:
    """A healthy store keeps the outbox identifiers and raw stamps verbatim."""
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=2,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    _close_fixture(control)
    with _rows(paths.control_graph_db) as connection:
        rows = read_outbox_rows(connection)
        raw_rows = connection.execute(
            "SELECT id, nomination_id, profile_id, action, created_at, delivered_at "
            "FROM reconciliation_audit_outbox ORDER BY id"
        ).fetchall()
    assert len(rows) == len(raw_rows) > 0
    assert [row.outbox_id for row in rows] == [int(item[0]) for item in raw_rows]
    assert [row.created_at_raw for row in rows] == [str(item[4]) for item in raw_rows]
    assert all(
        row.delivered_at_raw == (None if item[5] is None else str(item[5]))
        for row, item in zip(rows, raw_rows, strict=True)
    )


def test_healthy_version_chain_reads_stay_stable(paths: InstrumentPaths) -> None:
    """A healthy store keeps the version-chain identities and stable row hashes."""
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    try:
        profile_id = daily.profile_ids[0]
        node_ids = _store_version_node_ids(paths.cohort_graph_db, profile_id)
        with _rows(paths.cohort_graph_db) as connection:
            first = read_node_versions(connection, profile_id=profile_id, node_ids=node_ids)
            second = read_node_versions(connection, profile_id=profile_id, node_ids=node_ids)
            raw = connection.execute(
                "SELECT node_id, version FROM node_versions WHERE profile_id = ? ORDER BY node_id, version",
                (profile_id,),
            ).fetchall()
        assert first == second
        assert [(item.node_id, item.version) for item in first] == [(str(row[0]), int(row[1])) for row in raw]
    finally:
        _close_fixture(daily)
