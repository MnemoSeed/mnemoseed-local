"""AC-5 migration parity: the shared, store-tagged migration sequence is the
single source of truth — the same version sequence, the same v1 -> head forward
upgrade, and the same column model must materialize on SQLite.

Per PRD-08 the stores live in separate files, so graph and meta never share one
``schema_version`` table.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence

from _support import make_pref

from mnemoseed_local.schema.graph import GraphNode
from mnemoseed_local.storage.drivers._migrations import (
    _SQLITE_KINDS,
    MIGRATIONS,
    SCHEMA_VERSION_TABLE,
    AddColumn,
    AddTrigger,
    CreateIndex,
    CreateTable,
    apply_migrations,
    current_schema_version,
    latest_version,
    render_sqlite,
)
from mnemoseed_local.storage.drivers._time import iso8601_utc

# ---------------------------------------------------------------- ddl parsing


def _table_body(sql: str) -> str:
    """Body of a ``CREATE TABLE ... (...)`` statement (paren-depth aware)."""
    start = sql.index("(")
    depth = 0
    for i in range(start, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[start + 1 : i]
    raise AssertionError(f"unbalanced CREATE TABLE: {sql}")


def _split_top_level(body: str) -> list[str]:
    clauses: list[str] = []
    current: list[str] = []
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            clauses.append("".join(current).strip())
            current = []
            i += 1
            if i < len(body) and body[i] == " ":
                i += 1
            continue
        current.append(ch)
        i += 1
    if current:
        clauses.append("".join(current).strip())
    return clauses


def _parse_column_clause(clause: str) -> tuple[str, str, bool, bool, bool, bool]:
    """(name, kind, has_pk, has_not_null, has_default, has_references)."""
    upper = clause.upper()
    name, rest = clause.split(maxsplit=1)
    kind = rest.split(maxsplit=1)[0]
    has_pk = "PRIMARY KEY" in upper
    has_nn = "NOT NULL" in upper
    has_default = "DEFAULT" in upper
    has_ref = "REFERENCES" in upper
    return (name, kind, has_pk, has_nn, has_default, has_ref)


def _parse_create_table(sql: str) -> list[tuple[str, str, bool, bool, bool, bool]]:
    columns = (
        _parse_column_clause(cl)
        for cl in _split_top_level(_table_body(sql))
        if not cl.upper().startswith("UNIQUE")
    )
    return [c for c in columns if c]


def _parse_index(sql: str) -> tuple[bool, str, str, tuple[str, ...]]:
    match = re.search(r"CREATE (UNIQUE )?INDEX IF NOT EXISTS (\w+) ON (\w+) \(([^)]+)\)", sql)
    assert match is not None, sql
    unique, name, table, columns = match.groups()
    return bool(unique), name, table, tuple(part.strip() for part in columns.split(","))


# ------------------------------------------------------- shared-op schema parity


def test_version_sequences_are_shared_and_forward_only() -> None:
    """The dialect-agnostic sequence IS the parity baseline (graph 1,2,5,10; meta 1,3,4,6,7,8,9,11)."""
    assert latest_version() == 11
    graph_versions = sorted(m.version for m in MIGRATIONS if m.applies_to("graph"))
    meta_versions = sorted(m.version for m in MIGRATIONS if m.applies_to("meta"))
    assert graph_versions == [1, 2, 5, 10]
    assert meta_versions == [1, 3, 4, 6, 7, 8, 9, 11]
    assert len(MIGRATIONS) == latest_version()


def test_v8_config_scope_column_is_nullable_reserved() -> None:
    """E1-4: migration v8 reserves a nullable ``scope`` column on the versioned
    config table; the column carries no default so existing rows back-fill NULL."""
    scope_ops = [op for m in MIGRATIONS if m.version == 8 for op in m.ops if isinstance(op, AddColumn)]
    assert len(scope_ops) == 1
    op = scope_ops[0]
    assert op.store == "meta"
    assert op.table == "config"
    assert op.column.name == "scope"
    assert op.column.kind == "TEXT"
    assert op.column.not_null is False  # reserved: rows today carry NULL
    assert op.column.default is None
    sqlite_sql = render_sqlite(op)
    assert "ADD COLUMN scope TEXT" in sqlite_sql
    assert "NOT NULL" not in sqlite_sql


def test_create_tables_render_the_shared_op_model() -> None:
    """The SQLite renderer materialises the shared column model; only the kind map differs."""
    for migration in MIGRATIONS:
        for op in migration.ops:
            if not isinstance(op, CreateTable):
                continue
            cols = _parse_create_table(render_sqlite(op))
            assert len(cols) == len(op.columns)
            for index, column in enumerate(op.columns):
                parsed = cols[index]
                assert parsed[0] == column.name
                assert parsed[1] == _SQLITE_KINDS[column.kind]
                assert parsed[2] == column.primary_key  # has pk
                assert parsed[3] == column.not_null  # not null
                assert parsed[4] == (column.default is not None)  # default
                assert parsed[5] == (column.references is not None)  # references


def test_indexes_render_from_shared_ops() -> None:
    for migration in MIGRATIONS:
        for op in migration.ops:
            if not isinstance(op, CreateIndex):
                continue
            assert _parse_index(render_sqlite(op)) == (op.unique, op.name, op.table, op.columns)


def test_add_columns_render_from_shared_ops() -> None:
    for migration in MIGRATIONS:
        for op in migration.ops:
            if not isinstance(op, AddColumn):
                continue
            clause = render_sqlite(op).split("ADD COLUMN ")[1]
            parsed = _parse_column_clause(clause)
            assert parsed[0] == op.column.name
            assert parsed[1] == _SQLITE_KINDS[op.column.kind]
            assert parsed[2] == op.column.primary_key
            assert parsed[3] == op.column.not_null


def test_triggers_render_with_event_and_timing() -> None:
    """The append-only audit barrier is part of the frozen schema."""
    for migration in MIGRATIONS:
        for op in migration.ops:
            if not isinstance(op, AddTrigger):
                continue
            sql = render_sqlite(op)
            assert op.table in sql and op.timing in sql and op.event in sql


# ------------------------------------------------------------ sqlite arm (offline)


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _node_row_mirror(node: GraphNode) -> dict[str, object]:
    """The exact v1 nodes row shape the driver's ``_node_row`` produces."""
    return {
        "node_id": node.node_id,
        "node_type": node.node_type.value,
        "profile_id": node.profile_id,
        "payload": json.dumps(node.props),
        "entities": json.dumps(node.entities),
        "confidence": node.confidence,
        "decay_weight": node.decay_weight,
        "never_decay": int(node.never_decay),
        "conflict_flag": int(node.conflict_flag),
        "conflict_group": node.conflict_group,
        "needs_reconcile": int(node.needs_reconcile),
        "pending_consolidation": int(node.pending_consolidation),
        "peripheral_gaps": int(node.peripheral_gaps),
        "valid_from": iso8601_utc(node.valid_from),
        "valid_to": iso8601_utc(node.valid_to) if node.valid_to is not None else None,
        "last_reinforced": iso8601_utc(node.last_reinforced),
        "hit_count": node.hit_count,
        "last_hit_at": iso8601_utc(node.last_hit_at) if node.last_hit_at is not None else None,
        "reinforce_count": node.reinforce_count,
        "cognitive_tier": node.cognitive_tier,
        "provenance": node.provenance.model_dump_json(),
        "created_at": iso8601_utc(node.created_at),
        "updated_at": iso8601_utc(node.updated_at),
        "version": node.version,
        "prev_version_id": node.prev_version_id,
    }


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(row.values()))


def _snapshot(conn: sqlite3.Connection, tables: Sequence[str]) -> dict[str, list[dict[str, object]]]:
    """Row-by-row copy of whole tables for byte-level preservation checks.

    A table that does not exist yet in the store (e.g. the v6 ``users`` table
    on a v1 meta install) snapshots as ``[]`` so the before/after comparison
    stays total: it was empty before the upgrade and stays empty after it
    (v6 has no backfill).
    """
    snap: dict[str, list[dict[str, object]]] = {}
    for table in tables:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if exists is None:
            snap[table] = []
            continue
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        snap[table] = [dict(row) for row in rows]
    return snap


def _load_v1_sqlite() -> tuple[sqlite3.Connection, sqlite3.Connection]:
    """Seed a v1 install: separate graph/meta stores (D6 separate files)."""
    # autocommit mode, matching the drivers: DML must not open an implicit
    # transaction that later collides with the migration runner's BEGIN IMMEDIATE
    graph = sqlite3.connect(":memory:", isolation_level=None)
    graph.row_factory = sqlite3.Row
    meta = sqlite3.connect(":memory:", isolation_level=None)
    meta.row_factory = sqlite3.Row
    assert apply_migrations(graph, "graph", target=1) == 1
    assert apply_migrations(meta, "meta", target=1) == 1

    node = make_pref(node_id="mv1", entities=["ui", "theme"])
    _insert(graph, "nodes", _node_row_mirror(node))
    _insert(
        graph,
        "node_versions",
        {
            "node_id": "mv1",
            "version": 1,
            "profile_id": node.profile_id,
            "valid_from": iso8601_utc(node.valid_from),
            "valid_to": None,
            "superseded_by": None,
            "changed_at": iso8601_utc(node.updated_at),
            "payload": json.dumps(node.props),
        },
    )
    _insert(
        graph,
        "edges",
        {
            "id": "e1",
            "src": "mv1",
            "dst": "mv2",
            "rel": "evidenced_by",
            "weight": 1.0,
            "profile_id": node.profile_id,
            "provenance": json.dumps({"asserted_by": "contract-agent"}),
            "created_at": iso8601_utc(node.updated_at),
        },
    )
    _insert(
        meta,
        "profiles",
        {"profile_id": "u1", "display_name": "Uma", "created_at": iso8601_utc(node.updated_at)},
    )
    _insert(
        meta,
        "tokens",
        {
            "token_id": "tok-1",
            "profile_id": "u1",
            "scopes": json.dumps(["graph:read"]),
            "issued_at": iso8601_utc(node.updated_at),
            "expires_at": None,
            "revoked": 0,
        },
    )
    _insert(
        meta,
        "config",
        {
            "key": "theme",
            "value": json.dumps({"mode": "dark"}),
            "version": 1,
            "updated_at": iso8601_utc(node.updated_at),
        },
    )
    # score_pool singleton exists via defaults; insert id=1 explicitly
    _insert(
        meta,
        "score_pool",
        {
            "id": 1,
            "balance": 0.0,
            "watermark_start": 0,
            "watermark_end": 0,
            "last_event_start": 0,
            "last_event_end": 0,
        },
    )
    _insert(
        meta,
        "audit_log",
        {
            "id": 1,
            "actor": "alice",
            "action": "insert",
            "detail": json.dumps({"n": 1}),
            "at": iso8601_utc(node.updated_at),
        },
    )
    _insert(
        meta,
        "dream_runs",
        {
            "run_id": "run-1",
            "session_id": "s1",
            "turn_start": 1,
            "turn_end": 3,
            "model_id": "claude",
            "started_at": iso8601_utc(node.updated_at),
            "finished_at": None,
            "tokens": 42,
            "cost": 0.0042,
            "interrupted": 0,
            "dropped_count": 0,
        },
    )
    return graph, meta


_GRAPH_TABLES = ("nodes", "node_versions", "edges")
_META_TABLES = (
    "profiles",
    "tokens",
    "score_pool",
    "config",
    "audit_log",
    "dream_runs",
    "users",
)


def test_sqlite_v1_to_head_forward_migration_preserves_data() -> None:
    """A real file at v1 upgrades to head: column added, every row survives."""
    graph, meta = _load_v1_sqlite()
    assert "pinned" not in _column_names(graph, "nodes")
    graph_before = _snapshot(graph, _GRAPH_TABLES)
    meta_before = _snapshot(meta, _META_TABLES)
    assert current_schema_version(graph, "graph") == 1
    assert current_schema_version(meta, "meta") == 1

    # forward migration: graph advances to 10, meta to 11 (v2/v5/v10 are
    # graph-only; v6 is meta-only: identity users table + hashed token column;
    # v7 is the profile archive flag; v8 is the reserved nullable config.scope
    # column; v9 is the lifetime filed-points ledger column on
    # profile_score_pool; v11 is the append-only error-event ledger)
    assert apply_migrations(graph, "graph") == 10
    assert apply_migrations(meta, "meta") == 11
    assert current_schema_version(graph, "graph") == 10
    assert current_schema_version(meta, "meta") == 11

    assert "pinned" in _column_names(graph, "nodes")
    assert "promotion_status" in _column_names(graph, "nodes")
    assert "read_conflict_id" in _column_names(graph, "nodes")
    row = dict(
        graph.execute("SELECT pinned, promotion_status, payload FROM nodes WHERE node_id = 'mv1'").fetchone()
    )
    assert row["pinned"] == 0  # NOT NULL DEFAULT 0 backfills the existing row
    assert row["promotion_status"] == "promoted"  # v5 default back-compat on the same row
    assert json.loads(row["payload"]) == make_pref(node_id="mv1").props  # byte-identical payload

    graph_after = _snapshot(graph, _GRAPH_TABLES)
    meta_after = _snapshot(meta, _META_TABLES)
    assert _project_without_deltas(graph_after["nodes"]) == graph_before["nodes"]
    for table in ("node_versions", "edges"):
        assert graph_after[table] == graph_before[table], f"row drift in {table} across v1->v3"
    for table in _META_TABLES:
        # v6 adds the nullable token_hash column, v7 the profiles.archived
        # flag, v8 the reserved nullable config.scope column and v9 the
        # filed_points_total ledger column; legacy rows are preserved
        # byte-for-byte, projected without the new (empty/NULL) columns.
        rows = [r for r in meta_after[table]]
        if table == "tokens":
            rows = [{k: v for k, v in r.items() if k != "token_hash"} for r in rows]
        if table == "profiles":
            rows = [{k: v for k, v in r.items() if k != "archived"} for r in rows]
        if table == "config":
            rows = [{k: v for k, v in r.items() if k != "scope"} for r in rows]
        assert rows == meta_before[table], f"row drift in {table} across v1->head"

    # v8: the reserved scope column exists on config and back-fills NULL on the
    # legacy row (no default, never a value).
    config_columns = _column_names(meta, "config")
    assert "scope" in config_columns
    legacy_config = dict(meta.execute("SELECT key, value, scope FROM config WHERE key = 'theme'").fetchone())
    assert legacy_config["scope"] is None

    # v3 supersedes the legacy score_pool: the new per-profile table exists,
    # empty (no data migration), and the old singleton row is untouched.
    assert _column_names(meta, "profile_score_pool") == [
        "profile_id",
        "balance",
        "watermark_start",
        "watermark_end",
        "last_event_start",
        "last_event_end",
        "filed_points_total",
    ]
    empty = meta.execute("SELECT COUNT(*) FROM profile_score_pool").fetchone()
    assert int(empty[0]) == 0
    legacy = dict(meta.execute("SELECT id, balance FROM score_pool").fetchone())
    assert legacy["id"] == 1
    assert legacy["balance"] == 0.0

    # v4 dream_token_ledger exists, empty (no backfill: it is born empty)
    assert _column_names(meta, "dream_token_ledger") == ["profile_id", "year_month", "tokens"]
    unique_constraint = meta.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'dream_token_ledger'"
    ).fetchone()
    assert "UNIQUE (profile_id, year_month)" in unique_constraint[0]

    # v6 identity: the users table exists (empty after upgrade — no backfill),
    # and the legacy tokens row gained a nullable token_hash (NULL for v1 era).
    assert _column_names(meta, "users") == ["user_id", "username", "password_hash", "role", "created_at"]
    user_row = dict(meta.execute("SELECT token_hash FROM tokens WHERE token_id = 'tok-1'").fetchone())
    assert user_row["token_hash"] is None  # pre-v6 tokens hold no hash (blessed empty)

    # tracker advanced one step per graph/meta delta (v2/v5/v10 graph, v4/v6/v8/v9/v11 meta)
    graph_versions = [int(r[0]) for r in graph.execute(f"SELECT version FROM {SCHEMA_VERSION_TABLE}")]
    meta_versions = [int(r[0]) for r in meta.execute(f"SELECT version FROM {SCHEMA_VERSION_TABLE}")]
    assert sorted(graph_versions) == [1, 2, 5, 10]
    assert sorted(meta_versions) == [1, 3, 4, 6, 7, 8, 9, 11]

    # v11: the append-only error-event ledger is a dedicated meta table (born
    # empty, no backfill) — the E1 signal-agnostic nomination ledger.
    assert _column_names(meta, "error_events") == [
        "id",
        "profile_id",
        "signal_type",
        "observed_at",
        "evidence_kind",
        "evidence_id",
        "session_id",
        "turn_start",
        "turn_end",
        "detector_id",
        "eligibility_tag",
    ]
    assert int(meta.execute("SELECT COUNT(*) FROM error_events").fetchone()[0]) == 0


def _project_without_deltas(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Drop the post-v1 migration delta columns (pinned v2, promotion_status v5, read_conflict_id v10)."""
    deltas = {"pinned", "promotion_status", "read_conflict_id"}
    return [{k: v for k, v in row.items() if k not in deltas} for row in rows]
