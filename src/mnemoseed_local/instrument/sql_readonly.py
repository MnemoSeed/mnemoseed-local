"""S4 read-only evidence surface: the frozen query texts, the receipt crosscheck,
and the node integrity snapshot.

Every statement reads an explicit absolute path of an isolated product database
through a read-only connection. This module owns no write path: it never
migrates, seeds, or repairs a store, and it never reserializes a persisted
value. The raw text of a persisted timestamp stays the text that was stored.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mnemoseed_local.instrument.ledger import (
    BindingIncompleteError,
    canonical_json,
    canonical_node_json,
    sha256_hex,
)
from mnemoseed_local.storage.drivers._time import epoch_from_iso

if TYPE_CHECKING:
    from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
    from mnemoseed_local.storage.ports import ReconciliationReceipt


OUTBOX_ROWS_QUERY = (
    "SELECT id, nomination_id, profile_id, action, created_at, delivered_at "
    "FROM reconciliation_audit_outbox ORDER BY id"
)

RECEIPT_UNRESOLVED_QUERY = (
    "SELECT nomination_id, disposition, reason_code, applied_at "
    "FROM reconciliation_receipts WHERE disposition='unresolved' ORDER BY nomination_id"
)

RECEIPT_ROWS_QUERY = (
    "SELECT nomination_id, profile_id, disposition, reason_code, canonical_kind, "
    "composite_group_id, evidence_event_ids, verdict, quality, left_node_id, left_version, "
    "left_expected_peer_id, right_node_id, right_version, right_expected_peer_id, winner_node_id, "
    "loser_node_id, loser_prior_version, loser_new_version, downweight_factor, applied_at "
    "FROM reconciliation_receipts ORDER BY nomination_id"
)

NODE_VERSIONS_QUERY_TEMPLATE = (
    "SELECT node_id, version, profile_id, valid_from, valid_to, superseded_by, changed_at, payload "
    "FROM node_versions WHERE profile_id = ? AND node_id IN ({placeholders}) ORDER BY node_id, version"
)

SCHEMA_MASTER_QUERY = "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"

TABLE_INFO_NODES = "PRAGMA table_info(nodes)"
TABLE_INFO_NODE_VERSIONS = "PRAGMA table_info(node_versions)"

RECEIPT_PUBLIC_FIELDS: tuple[str, ...] = (
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

#: The required receipt column set with the declared type of every column. The
#: internal input hash column is required by the schema but never enters the
#: public comparison; the neutral JSON vocabulary renders as TEXT in SQLite,
#: which is why the evidence column is declared TEXT.
RECEIPT_COLUMN_TYPES: dict[str, str] = {
    "nomination_id": "TEXT",
    "profile_id": "TEXT",
    "disposition": "TEXT",
    "reason_code": "TEXT",
    "canonical_kind": "TEXT",
    "composite_group_id": "TEXT",
    "evidence_event_ids": "TEXT",
    "verdict": "TEXT",
    "quality": "TEXT",
    "left_node_id": "TEXT",
    "left_version": "INTEGER",
    "left_expected_peer_id": "TEXT",
    "right_node_id": "TEXT",
    "right_version": "INTEGER",
    "right_expected_peer_id": "TEXT",
    "winner_node_id": "TEXT",
    "loser_node_id": "TEXT",
    "loser_prior_version": "INTEGER",
    "loser_new_version": "INTEGER",
    "downweight_factor": "REAL",
    "applied_at": "TEXT",
    "input_hash": "TEXT",
}

READABLE_TABLES: frozenset[str] = frozenset({"nodes", "node_versions", "reconciliation_receipts"})

NODE_VERSION_COLUMNS: tuple[str, ...] = (
    "node_id",
    "version",
    "profile_id",
    "valid_from",
    "valid_to",
    "superseded_by",
    "changed_at",
    "payload",
)


@dataclass(frozen=True)
class OutboxRowSnapshot:
    """One persisted audit outbox row with its raw stamps."""

    outbox_id: int
    nomination_id: str
    profile_id: str
    action: str
    created_at_raw: str
    delivered_at_raw: str | None


@dataclass(frozen=True)
class ProjectionReceiptSnapshot:
    """One unresolved receipt with its persisted applied-at text."""

    nomination_id: str
    disposition: str
    reason_code: str
    applied_at_raw: str


@dataclass(frozen=True)
class ReceiptCrosscheck:
    """The verdict of one persisted row against the product decode."""

    nomination_id: str
    fields_match: bool
    mismatch_fields: tuple[str, ...]


@dataclass(frozen=True)
class NodeVersionSnapshot:
    """One version-chain row with the hash of its selected columns."""

    node_id: str
    version: int
    row_hash: str


@dataclass(frozen=True)
class NodeIntegritySnapshot:
    """The integrity evidence of one profile's nodes at one point in time."""

    profile_id: str
    version_row_count: int
    version_row_hashes: dict[str, str]
    schema_master_hash: str
    current_node_hashes: dict[str, str]
    table_info_hashes: dict[str, str]


def readonly_connection(db_path: Path) -> sqlite3.Connection:
    """Open one read-only connection to an isolated product database.

    The result is a plain connection: the caller closes it. The database is
    opened read-only, so no parent directory, database file, or migration is
    ever created here.
    """
    path = Path(db_path).expanduser().resolve()
    uri = f"{path.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        detail = {"reason": "database_unavailable", "path": str(path), "error": str(exc)}
        raise BindingIncompleteError(detail) from exc
    try:
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        connection.close()
        detail = {"reason": "query_only_unavailable", "path": str(path), "error": str(exc)}
        raise BindingIncompleteError(detail) from exc
    return connection


def query_hash(text: str) -> str:
    """Return the digest of one query text."""
    return sha256_hex(text.encode("utf-8"))


def query_hash_for(template: str, placeholder_count: int) -> str:
    """Return the digest of a template rendered with its ordered placeholder list."""
    count = _require_placeholder_count(placeholder_count)
    try:
        rendered = template.format(placeholders=", ".join(["?"] * count))
    except (IndexError, KeyError, ValueError) as exc:
        detail = {"reason": "template_not_renderable", "template": template}
        raise BindingIncompleteError(detail) from exc
    return query_hash(rendered)


def read_outbox_rows(conn: sqlite3.Connection) -> list[OutboxRowSnapshot]:
    """Read the persisted audit outbox rows with their raw stamps."""
    rows = _fetch_rows(conn, OUTBOX_ROWS_QUERY)
    return [
        OutboxRowSnapshot(
            outbox_id=int(row[0]),
            nomination_id=str(row[1]),
            profile_id=str(row[2]),
            action=str(row[3]),
            created_at_raw=str(row[4]),
            delivered_at_raw=None if row[5] is None else str(row[5]),
        )
        for row in rows
    ]


def read_unresolved_receipts(conn: sqlite3.Connection) -> list[ProjectionReceiptSnapshot]:
    """Read the persisted unresolved receipts with their raw applied-at text."""
    rows = _fetch_rows(conn, RECEIPT_UNRESOLVED_QUERY)
    return [
        ProjectionReceiptSnapshot(
            nomination_id=str(row[0]),
            disposition=str(row[1]),
            reason_code=str(row[2]),
            applied_at_raw=str(row[3]),
        )
        for row in rows
    ]


def read_schema_master(conn: sqlite3.Connection) -> str:
    """Return the digest of the schema objects of one database."""
    rows = _fetch_rows(conn, SCHEMA_MASTER_QUERY)
    return sha256_hex(canonical_json(_row_values(rows)).encode("utf-8"))


def read_table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Return the column names of one whitelisted table."""
    if table not in READABLE_TABLES:
        raise ValueError(f"table is outside the readable whitelist: {table!r}")
    rows = _fetch_rows(conn, f"PRAGMA table_info({table})")
    return tuple(str(row[1]) for row in rows)


def verify_receipt_schema(conn: sqlite3.Connection) -> None:
    """Check the receipt table against the required column set and declared types.

    The check is set-based: the physical column order of the source DDL differs
    from the public comparison order, so only the names and the types decide.
    """
    columns = read_table_columns(conn, "reconciliation_receipts")
    declared = _column_types(conn, "reconciliation_receipts")
    required = set(RECEIPT_COLUMN_TYPES)
    present = set(columns)
    missing = sorted(required - present)
    unexpected = sorted(present - required)
    mistyped = {
        name: {"expected": RECEIPT_COLUMN_TYPES[name], "actual": declared.get(name)}
        for name in sorted(required & present)
        if declared.get(name) != RECEIPT_COLUMN_TYPES[name]
    }
    if missing or unexpected or mistyped:
        detail = {
            "reason": "receipt_schema_mismatch",
            "missing_columns": missing,
            "unexpected_columns": unexpected,
            "mistyped_columns": mistyped,
        }
        raise BindingIncompleteError(detail)


def crosscheck_receipts(
    conn: sqlite3.Connection,
    graph: SqliteGraphDriver,
    *,
    nomination_ids: Sequence[str],
) -> list[ReceiptCrosscheck]:
    """Compare the persisted receipt rows with the product decode, field by field.

    Both sides of a comparison come from the same persisted row: the nullable
    fields keep None exactly, the timestamp is decoded from the same persisted
    text on both sides, and no value is reserialized.
    """
    verify_receipt_schema(conn)
    rows = _fetch_rows(conn, RECEIPT_ROWS_QUERY)
    persisted = {str(row[0]): row for row in rows}
    checks: list[ReceiptCrosscheck] = []
    for nomination_id in nomination_ids:
        row = persisted.get(nomination_id)
        if row is None:
            check = ReceiptCrosscheck(
                nomination_id=nomination_id,
                fields_match=False,
                mismatch_fields=("row_missing",),
            )
            checks.append(check)
            continue
        values = dict(zip(RECEIPT_PUBLIC_FIELDS, tuple(row), strict=True))
        receipt = _read_product_receipt(
            graph,
            profile_id=values["profile_id"],
            nomination_id=nomination_id,
        )
        if receipt is None:
            check = ReceiptCrosscheck(
                nomination_id=nomination_id,
                fields_match=False,
                mismatch_fields=("receipt_missing",),
            )
            checks.append(check)
            continue
        mismatches = _receipt_mismatches(values, receipt)
        check = ReceiptCrosscheck(
            nomination_id=nomination_id,
            fields_match=not mismatches,
            mismatch_fields=tuple(mismatches),
        )
        checks.append(check)
    return checks


def read_node_versions(
    conn: sqlite3.Connection,
    *,
    profile_id: str,
    node_ids: Sequence[str],
) -> list[NodeVersionSnapshot]:
    """Read the version-chain rows of one profile with the hash of each row."""
    identifiers = tuple(node_ids)
    if not identifiers:
        return []
    placeholders = ", ".join(["?"] * len(identifiers))
    query = NODE_VERSIONS_QUERY_TEMPLATE.format(placeholders=placeholders)
    rows = _fetch_rows(conn, query, (profile_id, *identifiers))
    return [
        NodeVersionSnapshot(
            node_id=str(row[0]),
            version=int(row[1]),
            row_hash=_version_row_hash(row),
        )
        for row in rows
    ]


def capture_node_integrity(
    graph: SqliteGraphDriver,
    conn: sqlite3.Connection,
    *,
    profile_id: str,
    node_ids: Sequence[str],
) -> NodeIntegritySnapshot:
    """Capture the version chain, the schema, and the current node content."""
    versions = read_node_versions(conn, profile_id=profile_id, node_ids=node_ids)
    return NodeIntegritySnapshot(
        profile_id=profile_id,
        version_row_count=len(versions),
        version_row_hashes={f"{item.node_id}:{item.version}": item.row_hash for item in versions},
        schema_master_hash=read_schema_master(conn),
        current_node_hashes={node_id: _current_node_hash(graph, node_id) for node_id in node_ids},
        table_info_hashes=_table_info_hashes(conn),
    )


def assert_node_integrity(before: NodeIntegritySnapshot, after: NodeIntegritySnapshot) -> None:
    """Block on any drift between two integrity snapshots."""
    drift: list[str] = []
    if before.profile_id != after.profile_id:
        drift.append("profile_id")
    if before.version_row_count != after.version_row_count:
        drift.append("version_row_count")
    if before.version_row_hashes != after.version_row_hashes:
        drift.append("version_row_hashes")
    if before.schema_master_hash != after.schema_master_hash:
        drift.append("schema_master_hash")
    if before.current_node_hashes != after.current_node_hashes:
        drift.append("current_node_hashes")
    if before.table_info_hashes != after.table_info_hashes:
        drift.append("table_info_hashes")
    if drift:
        detail = {"reason": "node_integrity_drift", "drift": drift, "profile_id": after.profile_id}
        raise BindingIncompleteError(detail)


def _receipt_mismatches(values: dict[str, Any], receipt: ReconciliationReceipt) -> list[str]:
    """The public fields of one receipt whose persisted value differs.

    Every field is compared in its own type: a raw value that carries another
    storage class never matches the product decode, and no raw value is
    coerced into the type of the field it fills.
    """
    mismatches: list[str] = []
    for name, expected_text in (
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
        if not _matches_text(values[name], expected_text):
            mismatches.append(name)
    for name, expected_version in (
        ("left_version", receipt.left_version),
        ("right_version", receipt.right_version),
    ):
        if not _matches_integer(values[name], expected_version):
            mismatches.append(name)
    for name, expected_node in (
        ("winner_node_id", receipt.winner_node_id),
        ("loser_node_id", receipt.loser_node_id),
    ):
        if not _matches_optional_text(values[name], expected_node):
            mismatches.append(name)
    for name, expected_optional_version in (
        ("loser_prior_version", receipt.loser_prior_version),
        ("loser_new_version", receipt.loser_new_version),
    ):
        if not _matches_optional_integer(values[name], expected_optional_version):
            mismatches.append(name)
    if not _matches_optional_float(values["downweight_factor"], receipt.downweight_factor):
        mismatches.append("downweight_factor")
    if not _matches_evidence(values["evidence_event_ids"], receipt.evidence_event_ids):
        mismatches.append("evidence_event_ids")
    if not _matches_applied_at(values["applied_at"], receipt.applied_at):
        mismatches.append("applied_at")
    return mismatches


def _matches_text(raw: Any, expected: str) -> bool:
    """Whether a raw text value equals the product decode verbatim."""
    if not isinstance(raw, str):
        return False
    return raw == expected


def _matches_integer(raw: Any, expected: int) -> bool:
    """Whether a raw integer value equals the product decode exactly."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return False
    return raw == expected


def _matches_optional_text(raw: Any, expected: str | None) -> bool:
    """Whether a raw nullable text value equals the product decode verbatim."""
    if raw is None or expected is None:
        return raw is None and expected is None
    return _matches_text(raw, expected)


def _matches_optional_integer(raw: Any, expected: int | None) -> bool:
    """Whether a raw nullable integer value equals the product decode exactly."""
    if raw is None or expected is None:
        return raw is None and expected is None
    return _matches_integer(raw, expected)


def _matches_optional_float(raw: Any, expected: float | None) -> bool:
    """Whether a raw nullable numeric value equals the product decode exactly."""
    if raw is None or expected is None:
        return raw is None and expected is None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return False
    return raw == expected


def _matches_evidence(raw: Any, expected: tuple[int, ...]) -> bool:
    """Whether the persisted evidence text decodes to the product evidence list."""
    if not isinstance(raw, str):
        return False
    try:
        decoded: object = json.loads(raw)
    except ValueError:
        return False
    return decoded == list(expected)


def _matches_applied_at(raw: Any, expected: float) -> bool:
    """Whether the persisted timestamp text decodes to the product instant."""
    if not isinstance(raw, str):
        return False
    try:
        decoded = epoch_from_iso(raw)
    except (TypeError, ValueError):
        return False
    return decoded == expected


def _fetch_rows(conn: sqlite3.Connection, query: str, params: Sequence[Any] = ()) -> list[Any]:
    """Run one read and convert a SQLite failure into a binding failure."""
    try:
        return list(conn.execute(query, tuple(params)).fetchall())
    except sqlite3.Error as exc:
        detail = {"reason": "sqlite_error", "query": query, "error": str(exc)}
        raise BindingIncompleteError(detail) from exc


def _read_product_receipt(
    graph: SqliteGraphDriver, *, profile_id: str, nomination_id: str
) -> ReconciliationReceipt | None:
    """The product decode of one receipt behind the typed read boundary.

    A SQLite failure of the product connection and a receipt content the
    product cannot decode both become binding failures, each with its own
    reason, and keep the original error as their cause.
    """
    try:
        return graph.get_reconciliation_receipt(profile_id=profile_id, nomination_id=nomination_id)
    except sqlite3.Error as exc:
        detail = _product_receipt_detail("product_receipt_query_error", profile_id, nomination_id, exc)
        raise BindingIncompleteError(detail) from exc
    except (ValueError, TypeError, OverflowError) as exc:
        detail = _product_receipt_detail("product_receipt_decode_error", profile_id, nomination_id, exc)
        raise BindingIncompleteError(detail) from exc


def _product_receipt_detail(
    reason: str, profile_id: str, nomination_id: str, error: BaseException
) -> dict[str, Any]:
    """The typed failure detail of one product receipt read."""
    return {
        "reason": reason,
        "profile_id": profile_id,
        "nomination_id": nomination_id,
        "error": str(error),
    }


def _row_values(rows: Sequence[Sequence[Any]]) -> list[list[Any]]:
    """The row values of a result set, with the text of every non-null value."""
    return [[None if value is None else str(value) for value in row] for row in rows]


def _column_types(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """The declared type of every column of one whitelisted table."""
    rows = _fetch_rows(conn, f"PRAGMA table_info({table})")
    return {str(row[1]): str(row[2]) for row in rows}


def _version_row_hash(row: Sequence[Any]) -> str:
    """The hash of the selected columns of one version-chain row."""
    payload = {name: str(value) for name, value in zip(NODE_VERSION_COLUMNS, row, strict=True)}
    return sha256_hex(canonical_json(payload).encode("utf-8"))


def _current_node_hash(graph: SqliteGraphDriver, node_id: str) -> str:
    """The hash of the full current content of one node."""
    node = graph.get_node(node_id)
    if node is None:
        return sha256_hex(canonical_json(None).encode("utf-8"))
    return sha256_hex(canonical_node_json(node).encode("utf-8"))


def _table_info_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    """The hash of the column layout of the node tables."""
    hashes: dict[str, str] = {}
    tables = (("nodes", TABLE_INFO_NODES), ("node_versions", TABLE_INFO_NODE_VERSIONS))
    for table, pragma in tables:
        rows = _fetch_rows(conn, pragma)
        hashes[table] = sha256_hex(canonical_json(_row_values(rows)).encode("utf-8"))
    return hashes


def _require_placeholder_count(value: object) -> int:
    """The number of placeholders of a rendered template."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BindingIncompleteError({"field": "placeholder_count"})
    return value


__all__ = [
    "NODE_VERSIONS_QUERY_TEMPLATE",
    "OUTBOX_ROWS_QUERY",
    "RECEIPT_ROWS_QUERY",
    "RECEIPT_UNRESOLVED_QUERY",
    "SCHEMA_MASTER_QUERY",
    "TABLE_INFO_NODE_VERSIONS",
    "TABLE_INFO_NODES",
    "NodeIntegritySnapshot",
    "NodeVersionSnapshot",
    "OutboxRowSnapshot",
    "ProjectionReceiptSnapshot",
    "ReceiptCrosscheck",
    "assert_node_integrity",
    "capture_node_integrity",
    "crosscheck_receipts",
    "query_hash",
    "query_hash_for",
    "read_node_versions",
    "read_outbox_rows",
    "read_schema_master",
    "read_table_columns",
    "read_unresolved_receipts",
    "readonly_connection",
    "verify_receipt_schema",
]
