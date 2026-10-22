"""S4 read-only evidence surface: the frozen queries, the receipt crosscheck, and the integrity snapshot.

Every read goes through the instrument's read-only surface or through a
read-only connection to an isolated product database; no product row is written
by hand.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.fixtures import (
    PROTECTED_ENDPOINT,
    SeedReceiptSpec,
    build_daily_cohort_fixture,
    build_repair_control_fixture,
    seed_receipt,
)
from mnemoseed_local.instrument.ledger import BindingIncompleteError, InstrumentError, InstrumentPaths
from mnemoseed_local.instrument.repair_oracle import run_control_repair
from mnemoseed_local.instrument.sql_readonly import (
    NODE_VERSIONS_QUERY_TEMPLATE,
    OUTBOX_ROWS_QUERY,
    RECEIPT_ROWS_QUERY,
    RECEIPT_UNRESOLVED_QUERY,
    SCHEMA_MASTER_QUERY,
    TABLE_INFO_NODE_VERSIONS,
    TABLE_INFO_NODES,
    crosscheck_receipts,
    query_hash,
    query_hash_for,
    read_outbox_rows,
    read_schema_master,
    read_table_columns,
    read_unresolved_receipts,
    readonly_connection,
    verify_receipt_schema,
)
from mnemoseed_local.storage.drivers._time import epoch_from_iso
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.ports import ReconciliationReceipt

try:
    from mnemoseed_local.instrument.sql_readonly import (
        NodeIntegritySnapshot,
        assert_node_integrity,
        capture_node_integrity,
    )
except ImportError:  # pragma: no cover - the integrity helpers may live in the repair oracle
    from mnemoseed_local.instrument.repair_oracle import (
        NodeIntegritySnapshot,
        assert_node_integrity,
        capture_node_integrity,
    )

SEED_EPOCH = 1_700_000_000.0
CONTROL_ID = "ctrl-105"
REPAIR_LIMIT = 100

ColumnSpec = tuple[str, str, bool, bool]

PUBLIC_RECEIPT_FIELDS: tuple[str, ...] = (
    "nomination_id",
    "profile_id",
    "disposition",
    "reason_code",
    "canonical_kind",
    "composite_group_id",
    "evidence_event_ids",
    "verdict",
    "quality",
    "left_node_id",
    "left_version",
    "left_expected_peer_id",
    "right_node_id",
    "right_version",
    "right_expected_peer_id",
    "winner_node_id",
    "loser_node_id",
    "loser_prior_version",
    "loser_new_version",
    "downweight_factor",
    "applied_at",
)

_RECEIPT_TABLE_COLUMNS: tuple[ColumnSpec, ...] = (
    ("nomination_id", "TEXT", False, True),
    ("profile_id", "TEXT", True, False),
    ("disposition", "TEXT", True, False),
    ("reason_code", "TEXT", True, False),
    ("left_node_id", "TEXT", True, False),
    ("left_version", "INTEGER", True, False),
    ("left_expected_peer_id", "TEXT", True, False),
    ("right_node_id", "TEXT", True, False),
    ("right_version", "INTEGER", True, False),
    ("right_expected_peer_id", "TEXT", True, False),
    ("winner_node_id", "TEXT", False, False),
    ("loser_node_id", "TEXT", False, False),
    ("downweight_factor", "REAL", False, False),
    ("applied_at", "TEXT", True, False),
    ("input_hash", "TEXT", True, False),
    ("canonical_kind", "TEXT", True, False),
    ("composite_group_id", "TEXT", True, False),
    ("evidence_event_ids", "TEXT", True, False),
    ("verdict", "TEXT", True, False),
    ("quality", "TEXT", True, False),
    ("loser_prior_version", "INTEGER", False, False),
    ("loser_new_version", "INTEGER", False, False),
)

RECEIPT_TYPES: dict[str, str] = {name: kind for name, kind, _null, _key in _RECEIPT_TABLE_COLUMNS}

_RECEIPT_FIELDS_SQL = ", ".join(PUBLIC_RECEIPT_FIELDS)
_RECEIPT_SELECT = f"SELECT {_RECEIPT_FIELDS_SQL} FROM reconciliation_receipts WHERE nomination_id = ?"
_OUTBOX_RAW_SQL = "SELECT id, delivered_at FROM reconciliation_audit_outbox ORDER BY id"
_RECEIPT_RAW_SQL = "SELECT nomination_id, applied_at, profile_id FROM reconciliation_receipts"
_TABLE_INFO_SQL = "PRAGMA table_info(reconciliation_receipts)"


@contextmanager
def _rows(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open one read-only connection to an isolated product database."""
    connection = readonly_connection(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _normalized(text: str) -> str:
    """Collapse the whitespace of a query text so only its tokens matter."""
    return " ".join(text.split())


def _selected_columns(query: str) -> list[str]:
    """The column names a select statement names, in their written order."""
    body = query.split("FROM", 1)[0]
    return [part.strip() for part in body.removeprefix("SELECT ").split(",")]


def _scratch_receipt_db(path: Path, columns: Sequence[ColumnSpec]) -> None:
    """Create a scratch database whose receipt table carries the given columns."""
    parts: list[str] = []
    for name, kind, not_null, primary_key in columns:
        piece = f'"{name}" {kind}'
        if primary_key:
            piece += " PRIMARY KEY"
        elif not_null:
            piece += " NOT NULL"
        parts.append(piece)
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"CREATE TABLE reconciliation_receipts ({', '.join(parts)})")
        connection.commit()
    finally:
        connection.close()


def _without(columns: Sequence[ColumnSpec], name: str) -> tuple[ColumnSpec, ...]:
    """The column list without one column."""
    kept: list[ColumnSpec] = []
    for column in columns:
        if column[0] != name:
            kept.append(column)
    return tuple(kept)


def _with_type(columns: Sequence[ColumnSpec], name: str, kind: str) -> tuple[ColumnSpec, ...]:
    """The column list with one column declared under another type."""
    changed: list[ColumnSpec] = []
    for column in columns:
        if column[0] == name:
            changed.append((column[0], kind, column[2], column[3]))
        else:
            changed.append(column)
    return tuple(changed)


def _public_fields_match(values: dict[str, Any], receipt: ReconciliationReceipt) -> bool:
    """Compare the persisted row with the product decode, field by field."""
    for name, expected in (
        ("nomination_id", receipt.nomination_id),
        ("profile_id", receipt.profile_id),
        ("disposition", str(receipt.disposition)),
        ("reason_code", str(receipt.reason_code)),
        ("canonical_kind", receipt.canonical_kind),
        ("composite_group_id", receipt.composite_group_id),
        ("verdict", receipt.verdict),
        ("quality", receipt.quality),
        ("left_node_id", receipt.left_node_id),
        ("left_expected_peer_id", receipt.left_expected_peer_id),
        ("right_node_id", receipt.right_node_id),
        ("right_expected_peer_id", receipt.right_expected_peer_id),
    ):
        if str(values[name]) != expected:
            return False
    for name, expected in (
        ("left_version", receipt.left_version),
        ("right_version", receipt.right_version),
    ):
        value = values[name]
        if value is None or type(value) is not int or value != expected:
            return False
    for name, expected in (
        ("winner_node_id", receipt.winner_node_id),
        ("loser_node_id", receipt.loser_node_id),
    ):
        value = values[name]
        if value is None and expected is None:
            continue
        if value is None or expected is None or str(value) != expected:
            return False
    for name, expected in (
        ("loser_prior_version", receipt.loser_prior_version),
        ("loser_new_version", receipt.loser_new_version),
    ):
        value = values[name]
        if value is None and expected is None:
            continue
        if value is None or expected is None:
            return False
        if type(value) is not int or value != expected:
            return False
    factor = values["downweight_factor"]
    if factor is None and receipt.downweight_factor is None:
        pass
    elif factor is None or receipt.downweight_factor is None:
        return False
    elif float(factor) != receipt.downweight_factor:
        return False
    if json.loads(str(values["evidence_event_ids"])) != list(receipt.evidence_event_ids):
        return False
    if epoch_from_iso(str(values["applied_at"])) != receipt.applied_at:
        return False
    return True


def _profile_node_ids(graph: SqliteGraphDriver, node_ids: Sequence[str], profile_id: str) -> tuple[str, ...]:
    """The node ids that belong to one profile of a seeded pair."""
    owned: list[str] = []
    for node_id in node_ids:
        node = graph.get_node(node_id)
        if node is not None and node.profile_id == profile_id:
            owned.append(node_id)
    return tuple(owned)


def test_the_read_only_query_texts_are_the_frozen_ones() -> None:
    assert _normalized(OUTBOX_ROWS_QUERY) == _normalized(
        "SELECT id, nomination_id, profile_id, action, created_at, delivered_at "
        "FROM reconciliation_audit_outbox ORDER BY id"
    )
    assert _normalized(RECEIPT_UNRESOLVED_QUERY) == _normalized(
        "SELECT nomination_id, disposition, reason_code, applied_at "
        "FROM reconciliation_receipts WHERE disposition='unresolved' ORDER BY nomination_id"
    )
    assert _normalized(SCHEMA_MASTER_QUERY) == _normalized(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    )
    assert _normalized(TABLE_INFO_NODES) == _normalized("PRAGMA table_info(nodes)")
    assert _normalized(TABLE_INFO_NODE_VERSIONS) == _normalized("PRAGMA table_info(node_versions)")
    assert _normalized(NODE_VERSIONS_QUERY_TEMPLATE) == _normalized(
        "SELECT node_id, version, profile_id, valid_from, valid_to, superseded_by, changed_at, payload "
        "FROM node_versions WHERE profile_id = ? AND node_id IN ({placeholders}) ORDER BY node_id, version"
    )


def test_the_receipt_query_names_the_public_fields_and_keeps_the_internal_one_out() -> None:
    selected = _selected_columns(RECEIPT_ROWS_QUERY)
    assert selected == list(PUBLIC_RECEIPT_FIELDS)
    assert "input_hash" not in selected
    assert "SELECT *" not in RECEIPT_ROWS_QUERY
    assert "FROM reconciliation_receipts" in RECEIPT_ROWS_QUERY
    assert "ORDER BY nomination_id" in RECEIPT_ROWS_QUERY


def test_the_query_hashes_cover_the_executed_text() -> None:
    assert query_hash(OUTBOX_ROWS_QUERY) == hashlib.sha256(OUTBOX_ROWS_QUERY.encode("utf-8")).hexdigest()
    assert query_hash(SCHEMA_MASTER_QUERY) == hashlib.sha256(SCHEMA_MASTER_QUERY.encode("utf-8")).hexdigest()
    rendered = query_hash_for(NODE_VERSIONS_QUERY_TEMPLATE, 3)
    assert len(rendered) == 64
    assert rendered != query_hash(NODE_VERSIONS_QUERY_TEMPLATE)
    assert query_hash_for(NODE_VERSIONS_QUERY_TEMPLATE, 3) != query_hash_for(NODE_VERSIONS_QUERY_TEMPLATE, 2)


def test_the_read_only_connection_refuses_writes(paths: InstrumentPaths) -> None:
    build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=2,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    connection = readonly_connection(paths.control_graph_db)
    try:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO reconciliation_audit_outbox "
                "(nomination_id, profile_id, action, detail, created_at) "
                "VALUES ('nom-x', 'profile-x', 'reconcile_unresolved', '{}', '2026-01-01T00:00:00.000Z')"
            )
    finally:
        connection.close()


def test_the_outbox_rows_keep_the_persisted_stamps_verbatim(paths: InstrumentPaths) -> None:
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=5,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    with _rows(paths.control_graph_db) as connection:
        rows = read_outbox_rows(connection)
        raw_rows = connection.execute(_OUTBOX_RAW_SQL).fetchall()
    assert len(rows) == 5
    assert [row.outbox_id for row in rows] == [int(item[0]) for item in raw_rows]
    assert all(row.delivered_at_raw is None for row in rows)
    assert all(item[1] is None for item in raw_rows)

    run_control_repair(control, limit=REPAIR_LIMIT, pass_index=1)

    with _rows(paths.control_graph_db) as connection:
        rows = read_outbox_rows(connection)
        raw_rows = connection.execute(_OUTBOX_RAW_SQL).fetchall()
    raw: dict[int, Any] = {}
    for item in raw_rows:
        raw[int(item[0])] = item[1]
    delivered = [row for row in rows if row.delivered_at_raw is not None]
    assert len(delivered) == 5
    for row in delivered:
        assert isinstance(row.delivered_at_raw, str)
        assert row.delivered_at_raw == raw[row.outbox_id]


def test_the_unresolved_receipts_keep_the_persisted_applied_at_text(paths: InstrumentPaths) -> None:
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=3,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    with _rows(paths.control_graph_db) as connection:
        snapshots = read_unresolved_receipts(connection)
        raw_rows = connection.execute(_RECEIPT_RAW_SQL).fetchall()
    raw: dict[str, tuple[str, str]] = {}
    for item in raw_rows:
        raw[str(item[0])] = (str(item[1]), str(item[2]))
    assert len(snapshots) == 3
    assert [snapshot.nomination_id for snapshot in snapshots] == sorted(raw)
    for snapshot in snapshots:
        applied_at_raw, profile_id = raw[snapshot.nomination_id]
        assert snapshot.disposition == "unresolved"
        assert snapshot.reason_code == "stale_revision"
        assert isinstance(snapshot.applied_at_raw, str)
        assert snapshot.applied_at_raw == applied_at_raw
        receipt = control.graph.get_reconciliation_receipt(
            profile_id=profile_id, nomination_id=snapshot.nomination_id
        )
        assert receipt is not None
        assert epoch_from_iso(snapshot.applied_at_raw) == receipt.applied_at


def test_the_crosscheck_matches_every_public_receipt_field(paths: InstrumentPaths) -> None:
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=3,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    nomination_ids = ["nom-control-0000", "nom-control-0001"]
    with _rows(paths.control_graph_db) as connection:
        verify_receipt_schema(connection)
        checks = crosscheck_receipts(connection, control.graph, nomination_ids=nomination_ids)
        for nomination_id in nomination_ids:
            row = connection.execute(_RECEIPT_SELECT, (nomination_id,)).fetchone()
            values = dict(zip(PUBLIC_RECEIPT_FIELDS, tuple(row), strict=True))
            receipt = control.graph.get_reconciliation_receipt(
                profile_id=values["profile_id"], nomination_id=nomination_id
            )
            assert receipt is not None
            assert _public_fields_match(values, receipt)
    assert [check.nomination_id for check in checks] == nomination_ids
    assert all(check.fields_match for check in checks)
    assert all(check.mismatch_fields == () for check in checks)


def test_the_nullable_receipt_fields_stay_none(paths: InstrumentPaths) -> None:
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=2,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    receipt = control.graph.get_reconciliation_receipt(
        profile_id=control.profile_ids[0], nomination_id="nom-control-0000"
    )
    assert receipt is not None
    assert receipt.loser_new_version is None
    assert receipt.loser_prior_version is not None
    with _rows(paths.control_graph_db) as connection:
        row = connection.execute(_RECEIPT_SELECT, ("nom-control-0000",)).fetchone()
    values = dict(zip(PUBLIC_RECEIPT_FIELDS, tuple(row), strict=True))
    assert values["loser_new_version"] is None
    assert values["loser_prior_version"] == receipt.loser_prior_version
    assert values["downweight_factor"] == receipt.downweight_factor


def test_the_receipt_schema_of_the_product_database_matches_the_source_ddl(paths: InstrumentPaths) -> None:
    build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=1,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    with _rows(paths.control_graph_db) as connection:
        verify_receipt_schema(connection)
        columns = read_table_columns(connection, "reconciliation_receipts")
        info_rows = connection.execute(_TABLE_INFO_SQL).fetchall()
    info: dict[str, str] = {}
    for row in info_rows:
        info[str(row[1])] = str(row[2])
    assert set(columns) == set(RECEIPT_TYPES)
    assert info == RECEIPT_TYPES


def test_the_receipt_schema_check_is_order_insensitive(tmp_path: Path) -> None:
    scratch = tmp_path / "shuffled-receipts.db"
    _scratch_receipt_db(scratch, tuple(reversed(_RECEIPT_TABLE_COLUMNS)))
    with _rows(scratch) as connection:
        verify_receipt_schema(connection)


def test_a_missing_receipt_column_is_a_binding_incomplete_error(tmp_path: Path) -> None:
    scratch = tmp_path / "missing-receipts.db"
    _scratch_receipt_db(scratch, _without(_RECEIPT_TABLE_COLUMNS, "input_hash"))
    with _rows(scratch) as connection:
        with pytest.raises(BindingIncompleteError) as caught:
            verify_receipt_schema(connection)
    assert caught.value.diag_code == "binding_incomplete"
    assert "input_hash" in json.dumps(caught.value.detail, default=str)


def test_a_missing_public_receipt_column_is_a_binding_incomplete_error(tmp_path: Path) -> None:
    scratch = tmp_path / "missing-public-receipts.db"
    _scratch_receipt_db(scratch, _without(_RECEIPT_TABLE_COLUMNS, "applied_at"))
    with _rows(scratch) as connection:
        with pytest.raises(BindingIncompleteError) as caught:
            verify_receipt_schema(connection)
    assert caught.value.diag_code == "binding_incomplete"
    assert "applied_at" in json.dumps(caught.value.detail, default=str)


def test_an_extra_receipt_column_is_a_binding_incomplete_error(tmp_path: Path) -> None:
    scratch = tmp_path / "extra-receipts.db"
    surplus = ("surplus_column", "TEXT", False, False)
    _scratch_receipt_db(scratch, (*_RECEIPT_TABLE_COLUMNS, surplus))
    with _rows(scratch) as connection:
        with pytest.raises(BindingIncompleteError) as caught:
            verify_receipt_schema(connection)
    assert caught.value.diag_code == "binding_incomplete"
    assert "surplus_column" in json.dumps(caught.value.detail, default=str)


def test_a_mistyped_receipt_column_is_a_binding_incomplete_error(tmp_path: Path) -> None:
    scratch = tmp_path / "mistyped-receipts.db"
    _scratch_receipt_db(scratch, _with_type(_RECEIPT_TABLE_COLUMNS, "downweight_factor", "TEXT"))
    with _rows(scratch) as connection:
        with pytest.raises(BindingIncompleteError) as caught:
            verify_receipt_schema(connection)
    assert caught.value.diag_code == "binding_incomplete"
    assert "downweight_factor" in json.dumps(caught.value.detail, default=str)


def test_a_receipt_query_error_becomes_a_binding_incomplete_error(
    paths: InstrumentPaths, tmp_path: Path
) -> None:
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=1,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    scratch = tmp_path / "empty-receipts.db"
    sqlite3.connect(scratch).close()
    with _rows(scratch) as connection:
        with pytest.raises(BindingIncompleteError) as caught:
            crosscheck_receipts(connection, control.graph, nomination_ids=["nom-control-0000"])
    assert caught.value.diag_code == "binding_incomplete"


def test_only_the_frozen_tables_may_be_read(paths: InstrumentPaths) -> None:
    build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=1,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    with _rows(paths.control_graph_db) as connection:
        assert read_table_columns(connection, "nodes")
        assert read_table_columns(connection, "node_versions")
        assert read_table_columns(connection, "reconciliation_receipts")
        with pytest.raises((InstrumentError, ValueError)):
            read_table_columns(connection, "sqlite_master")


def test_the_schema_digest_is_stable_and_specific(paths: InstrumentPaths, tmp_path: Path) -> None:
    build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=1,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with _rows(paths.control_graph_db) as connection:
        first = read_schema_master(connection)
        second = read_schema_master(connection)
    with _rows(empty) as connection:
        other = read_schema_master(connection)
    assert first == second
    assert len(first) == 64
    assert first != other


def test_counting_receipts_requires_an_explicit_profile(paths: InstrumentPaths) -> None:
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    counted = daily.graph.count_reconciliation_receipts(profile_id=daily.profile_ids[0])
    assert counted
    with pytest.raises(TypeError):
        daily.graph.count_reconciliation_receipts()


def test_the_protected_endpoint_adds_no_version_row_and_no_node_change(paths: InstrumentPaths) -> None:
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    profile_id = daily.profile_ids[0]
    node_ids = _profile_node_ids(daily.graph, daily.node_ids, profile_id)
    protected_id = next(node_id for node_id in node_ids if node_id.endswith("-protected"))
    with _rows(paths.cohort_graph_db) as connection:
        before = capture_node_integrity(daily.graph, connection, profile_id=profile_id, node_ids=node_ids)
    versions_before = len(daily.graph.versions(protected_id))

    receipt = seed_receipt(
        daily,
        SeedReceiptSpec(
            nomination_id="nom-protected-fresh",
            profile_id=profile_id,
            applied_at_epoch=SEED_EPOCH + 9.0,
            kind=PROTECTED_ENDPOINT,
        ),
    )
    assert str(receipt.disposition) == "unresolved"
    assert str(receipt.reason_code) == "protected_endpoint"

    with _rows(paths.cohort_graph_db) as connection:
        after = capture_node_integrity(daily.graph, connection, profile_id=profile_id, node_ids=node_ids)
    assert_node_integrity(before, after)
    assert after.version_row_count == before.version_row_count
    assert after.version_row_hashes == before.version_row_hashes
    assert after.schema_master_hash == before.schema_master_hash
    assert after.current_node_hashes == before.current_node_hashes
    assert after.table_info_hashes == before.table_info_hashes
    assert len(daily.graph.versions(protected_id)) == versions_before


def test_every_integrity_drift_is_blocked(paths: InstrumentPaths) -> None:
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    profile_id = daily.profile_ids[0]
    node_ids = _profile_node_ids(daily.graph, daily.node_ids, profile_id)
    with _rows(paths.cohort_graph_db) as connection:
        snapshot = capture_node_integrity(daily.graph, connection, profile_id=profile_id, node_ids=node_ids)
    row_key = next(iter(snapshot.version_row_hashes))
    node_key = next(iter(snapshot.current_node_hashes))
    table_key = next(iter(snapshot.table_info_hashes))
    drifted: tuple[NodeIntegritySnapshot, ...] = (
        replace(snapshot, version_row_count=snapshot.version_row_count + 1),
        replace(snapshot, version_row_hashes={**snapshot.version_row_hashes, row_key: "0" * 64}),
        replace(snapshot, schema_master_hash="0" * 64),
        replace(snapshot, current_node_hashes={**snapshot.current_node_hashes, node_key: "0" * 64}),
        replace(snapshot, table_info_hashes={**snapshot.table_info_hashes, table_key: "0" * 64}),
    )
    for candidate in drifted:
        with pytest.raises(BindingIncompleteError):
            assert_node_integrity(snapshot, candidate)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("payload", json.dumps({"statement": "tampered fixture payload"})),
        ("confidence", 0.25),
    ],
)
def test_a_nodes_only_change_is_detected_while_the_version_chain_stays_intact(
    paths: InstrumentPaths, tmp_path: Path, column: str, value: Any
) -> None:
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    profile_id = daily.profile_ids[0]
    node_ids = _profile_node_ids(daily.graph, daily.node_ids, profile_id)
    target = node_ids[0]

    copy_path = tmp_path / f"graph-copy-{column}.db"
    source = sqlite3.connect(paths.cohort_graph_db)
    target_connection = sqlite3.connect(copy_path)
    try:
        source.backup(target_connection)
    finally:
        target_connection.close()
        source.close()
    copy_graph = SqliteGraphDriver(path=copy_path)

    with _rows(copy_path) as connection:
        before = capture_node_integrity(copy_graph, connection, profile_id=profile_id, node_ids=node_ids)

    writer = sqlite3.connect(copy_path)
    try:
        writer.execute(f"UPDATE nodes SET {column} = ? WHERE node_id = ?", (value, target))
        writer.commit()
    finally:
        writer.close()

    with _rows(copy_path) as connection:
        after = capture_node_integrity(copy_graph, connection, profile_id=profile_id, node_ids=node_ids)
    assert after.version_row_count == before.version_row_count
    assert after.version_row_hashes == before.version_row_hashes
    with pytest.raises(BindingIncompleteError):
        assert_node_integrity(before, after)


_CORRUPTION_NOMINATION_ID = "nom-control-0000"


def _receipt_column_raw(db_path: Path, column: str) -> tuple[Any, str]:
    """The raw stored value and the storage class of one receipt column."""
    with _rows(db_path) as connection:
        row = connection.execute(
            f"SELECT {column}, typeof({column}) FROM reconciliation_receipts WHERE nomination_id = ?",
            (_CORRUPTION_NOMINATION_ID,),
        ).fetchone()
    assert row is not None
    return row[0], str(row[1])


def _copied_control_graph(source_db: Path, copy_path: Path) -> SqliteGraphDriver:
    """Back up a seeded control database into a separate copy and open a driver on it."""
    source = sqlite3.connect(source_db)
    target = sqlite3.connect(copy_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return SqliteGraphDriver(path=copy_path)


def _corrupt_receipt_column(copy_path: Path, column: str, value: Any) -> None:
    """Overwrite one receipt column of the copied database, dropping its UPDATE guard."""
    connection = sqlite3.connect(copy_path)
    try:
        guards = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            ("reconciliation_receipts",),
        ).fetchall()
        dropped = [str(name) for name, sql in guards if "UPDATE" in str(sql).upper()]
        for name in dropped:
            connection.execute(f'DROP TRIGGER "{name}"')
        assert dropped, "the copied receipt table must carry an append-only UPDATE guard"
        connection.execute(
            f"UPDATE reconciliation_receipts SET {column} = ? WHERE nomination_id = ?",
            (value, _CORRUPTION_NOMINATION_ID),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("column", "value", "storage_class"),
    [
        ("left_version", 1.5, "real"),
        ("right_version", 1.5, "real"),
        ("loser_prior_version", 1.5, "real"),
        ("loser_new_version", 1.5, "real"),
        ("left_node_id", b"\x00corrupt-left-node-id", "blob"),
    ],
)
def test_a_raw_type_mismatch_in_a_copied_receipt_row_is_flagged(
    paths: InstrumentPaths,
    tmp_path: Path,
    column: str,
    value: Any,
    storage_class: str,
) -> None:
    """A copied receipt row whose raw storage type differs from its product decode is flagged."""
    build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=1,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    original_before = _receipt_column_raw(paths.control_graph_db, column)
    copy_path = tmp_path / f"receipt-copy-{column}.db"
    copy_graph = _copied_control_graph(paths.control_graph_db, copy_path)
    try:
        with _rows(copy_path) as connection:
            baseline = crosscheck_receipts(
                connection,
                copy_graph,
                nomination_ids=[_CORRUPTION_NOMINATION_ID],
            )
        assert len(baseline) == 1
        assert baseline[0].fields_match is True
        assert baseline[0].mismatch_fields == ()

        _corrupt_receipt_column(copy_path, column, value)

        with _rows(copy_path) as connection:
            stored = connection.execute(
                f"SELECT typeof({column}) FROM reconciliation_receipts WHERE nomination_id = ?",
                (_CORRUPTION_NOMINATION_ID,),
            ).fetchone()
            checks = crosscheck_receipts(
                connection,
                copy_graph,
                nomination_ids=[_CORRUPTION_NOMINATION_ID],
            )
        assert stored is not None
        assert str(stored[0]) == storage_class
        assert len(checks) == 1
        assert checks[0].fields_match is False
        assert column in checks[0].mismatch_fields
        assert _receipt_column_raw(paths.control_graph_db, column) == original_before
    finally:
        asyncio.run(copy_graph.close())


_PRODUCT_CONNECTION_ATTRIBUTE = "_conn"


def _sqlite_failure_in_chain(error: BaseException) -> sqlite3.Error | None:
    """The original SQLite failure behind a chain of typed read failures."""
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, sqlite3.Error):
            return current
        current = current.__cause__ or current.__context__
    return None


def test_a_product_receipt_query_failure_becomes_a_binding_incomplete_error(
    paths: InstrumentPaths,
) -> None:
    """A closed product connection surfaces through the public crosscheck as a typed failure."""
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=1,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    nomination_id = "nom-control-0000"
    try:
        with _rows(paths.control_graph_db) as connection:
            row = connection.execute(_RECEIPT_SELECT, (nomination_id,)).fetchone()
            assert row is not None
            values = dict(zip(PUBLIC_RECEIPT_FIELDS, tuple(row), strict=True))
            profile_id = str(values["profile_id"])

            # The fault is injected into the product's own connection, so the product
            # read half fails for real while the independent read-only half stays valid.
            product_connection = getattr(control.graph, _PRODUCT_CONNECTION_ATTRIBUTE)
            product_connection.close()

            with pytest.raises(sqlite3.Error) as product_failure:
                control.graph.get_reconciliation_receipt(profile_id=profile_id, nomination_id=nomination_id)
            assert "closed" in str(product_failure.value).lower()

            with pytest.raises(BindingIncompleteError) as caught:
                crosscheck_receipts(connection, control.graph, nomination_ids=[nomination_id])

            assert caught.value.diag_code == "binding_incomplete"
            assert _sqlite_failure_in_chain(caught.value) is not None
    finally:
        asyncio.run(control.graph.close())
        asyncio.run(control.meta.close())


@pytest.mark.parametrize(
    ("column", "value", "expected_errors"),
    [
        pytest.param("applied_at", "not-a-timestamp", (ValueError,), id="applied_at-text"),
        pytest.param(
            "evidence_event_ids",
            "not-json",
            (ValueError, json.JSONDecodeError),
            id="evidence-not-json",
        ),
        pytest.param("evidence_event_ids", "null", (TypeError,), id="evidence-json-null"),
        pytest.param("left_version", "not-an-int", (ValueError,), id="left-version-text"),
        pytest.param("left_version", float("inf"), (OverflowError,), id="left-version-infinite"),
        pytest.param("disposition", "invalid-disposition", (ValueError,), id="disposition-unknown"),
        pytest.param("reason_code", "invalid-reason", (ValueError,), id="reason-code-unknown"),
    ],
)
def test_an_undecodable_receipt_content_becomes_a_binding_incomplete_error(
    paths: InstrumentPaths,
    tmp_path: Path,
    column: str,
    value: Any,
    expected_errors: tuple[type[BaseException], ...],
) -> None:
    """A receipt whose content the product cannot decode fails the crosscheck typed."""
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=1,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    original_before = _receipt_column_raw(paths.control_graph_db, column)
    copy_path = tmp_path / f"decode-copy-{column}.db"
    copy_graph = _copied_control_graph(paths.control_graph_db, copy_path)
    try:
        with _rows(copy_path) as connection:
            row = connection.execute(
                "SELECT profile_id FROM reconciliation_receipts WHERE nomination_id = ?",
                (_CORRUPTION_NOMINATION_ID,),
            ).fetchone()
            assert row is not None
            profile_id = str(row[0])
            verify_receipt_schema(connection)
            baseline = crosscheck_receipts(
                connection,
                copy_graph,
                nomination_ids=[_CORRUPTION_NOMINATION_ID],
            )
            columns = read_table_columns(connection, "reconciliation_receipts")
        assert len(baseline) == 1
        assert baseline[0].fields_match is True
        assert baseline[0].mismatch_fields == ()
        assert len(RECEIPT_TYPES) == 22
        assert set(columns) == set(RECEIPT_TYPES)

        _corrupt_receipt_column(copy_path, column, value)

        with _rows(copy_path) as connection:
            verify_receipt_schema(connection)
            with pytest.raises(expected_errors):
                copy_graph.get_reconciliation_receipt(
                    profile_id=profile_id,
                    nomination_id=_CORRUPTION_NOMINATION_ID,
                )
            with pytest.raises(BindingIncompleteError) as caught:
                crosscheck_receipts(
                    connection,
                    copy_graph,
                    nomination_ids=[_CORRUPTION_NOMINATION_ID],
                )
        assert caught.value.diag_code == "binding_incomplete"
        assert isinstance(caught.value.__cause__, expected_errors)
        assert _receipt_column_raw(paths.control_graph_db, column) == original_before
    finally:
        asyncio.run(copy_graph.close())
        asyncio.run(control.graph.close())
        asyncio.run(control.meta.close())


def test_a_missing_receipt_table_fails_the_unresolved_read_as_a_binding_incomplete_error(
    tmp_path: Path,
) -> None:
    """The unresolved read of a database without the receipt table fails typed."""
    empty = tmp_path / "empty-unresolved.db"
    sqlite3.connect(empty).close()
    with _rows(empty) as connection:
        with pytest.raises(BindingIncompleteError) as caught:
            read_unresolved_receipts(connection)
    assert caught.value.diag_code == "binding_incomplete"
    failure = _sqlite_failure_in_chain(caught.value)
    assert isinstance(failure, sqlite3.OperationalError)
    assert "no such table" in str(failure).lower()
