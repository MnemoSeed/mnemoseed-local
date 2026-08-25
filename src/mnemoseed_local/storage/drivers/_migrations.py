"""Schema migrations: one forward-only, dialect-agnostic version sequence.

Migrations are structured DDL operations (not raw SQL strings) rendered to
SQLite by ``render_sqlite``. A migration carries a ``store`` tag per operation
("graph" / "meta") so a standalone SQLite file only applies its own tables but
still advances the shared global version sequence — the AC-5 parity baseline.

The ``schema_version`` table is owned by the migration mechanism itself and is
created idempotently by the runner, never by a migration entry.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from mnemoseed_local.storage.drivers._time import iso8601_utc

type StoreTag = Literal["graph", "meta"]

SCHEMA_VERSION_TABLE = "schema_version"


# ---------------------------------------------------------------- DDL ops


@dataclass(frozen=True)
class Column:
    """One column in neutral type vocabulary (TEXT/INTEGER/REAL/JSON)."""

    name: str
    kind: str  # TEXT | INTEGER | REAL | JSON
    primary_key: bool = False
    not_null: bool = False
    default: str | int | float | None = None
    references: tuple[str, str] | None = None  # (table, column)
    on_delete: str | None = None  # cascade / set null / restrict


@dataclass(frozen=True)
class CreateTable:
    store: StoreTag
    name: str
    columns: tuple[Column, ...]
    # table-level unique constraints, e.g. (("node_id", "version"),); rendered as
    # UNIQUE (node_id, version) so INSERT OR REPLACE keeps its replace semantics.
    unique: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class CreateIndex:
    store: StoreTag
    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool = False


@dataclass(frozen=True)
class AddColumn:
    store: StoreTag
    table: str
    column: Column


@dataclass(frozen=True)
class AddTrigger:
    store: StoreTag
    name: str
    timing: str  # BEFORE / AFTER
    event: str  # UPDATE / DELETE / INSERT
    table: str
    action: str  # sqlite body after "FOR EACH ROW" inside BEGIN/END
    pg_action: str | None = None  # plpgsql statement wrapped in the PG function body


type DDLOp = CreateTable | CreateIndex | AddColumn | AddTrigger


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    ops: tuple[DDLOp, ...]

    def applies_to(self, store: str) -> bool:
        return any(op.store == store for op in self.ops)


# ---------------------------------------------------------------- renderer (SQLite)

_SQLITE_KINDS: dict[str, str] = {
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
    "REAL": "REAL",
    "JSON": "TEXT",
}


def _sqlite_column_sql(column: Column) -> str:
    parts = [column.name, _SQLITE_KINDS[column.kind]]
    if column.primary_key:
        parts.append("PRIMARY KEY")
    if column.not_null:
        parts.append("NOT NULL")
    if column.default is not None:
        if isinstance(column.default, str):
            parts.append(f"DEFAULT {column.default!r}")
        else:
            parts.append(f"DEFAULT {column.default}")
    if column.references is not None:
        table, ref_col = column.references
        parts.append(f"REFERENCES {table}({ref_col})")
        if column.on_delete is not None:
            parts.append(f"ON DELETE {column.on_delete.upper()}")
    return " ".join(parts)


def render_sqlite(op: DDLOp) -> str:
    """Render one structured DDL operation as SQLite SQL."""
    if isinstance(op, CreateTable):
        parts = [_sqlite_column_sql(c) for c in op.columns]
        parts.extend(f"UNIQUE ({', '.join(cols)})" for cols in op.unique)
        body = ", ".join(parts)
        return f"CREATE TABLE IF NOT EXISTS {op.name} ({body})"
    if isinstance(op, CreateIndex):
        unique = "UNIQUE " if op.unique else ""
        cols = ", ".join(op.columns)
        return f"CREATE {unique}INDEX IF NOT EXISTS {op.name} ON {op.table} ({cols})"
    if isinstance(op, AddColumn):
        return f"ALTER TABLE {op.table} ADD COLUMN {_sqlite_column_sql(op.column)}"
    if isinstance(op, AddTrigger):
        return (
            f"CREATE TRIGGER IF NOT EXISTS {op.name} {op.timing} {op.event} ON {op.table} "
            f"FOR EACH ROW {op.action};"
        )
    raise TypeError(f"unknown DDL op {op!r}")


# ---------------------------------------------------------------- runner


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def ensure_schema_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)"
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(f"SELECT version FROM {SCHEMA_VERSION_TABLE}").fetchall()
    return {int(row[0]) for row in rows}


def current_schema_version(conn: sqlite3.Connection, store: str) -> int:
    """Highest applied global version that touched ``store`` (0 when none)."""
    ensure_schema_table(conn)
    applied = _applied_versions(conn)
    matched = (m.version for m in MIGRATIONS if m.applies_to(store) and m.version in applied)
    return max(matched, default=0)


def apply_migrations(conn: sqlite3.Connection, store: str, target: int | None = None) -> int:
    """Apply all pending migrations relevant to ``store`` in one transaction.

    A single ''BEGIN IMMEDIATE'' serializes concurrent first-initialization runs
    (e.g. several driver instances opening the same file): the second writer
    re-reads the applied-versions set after acquiring the lock and sees the
    first writer's commit. ``target`` limits the highest version applied (used
    by tests to simulate an old install before upgrading). Returns the resulting
    current version.
    """
    ensure_schema_table(conn)
    with _transaction(conn):
        applied = _applied_versions(conn)
        pending = _pending_for_store(store, target, applied)
        for migration in pending:
            for op in migration.ops:
                # a migration may mix graph + meta ops; only the ops tagged for
                # this store are executed so a file stays store-scoped (D6).
                if op.store == store:
                    conn.execute(render_sqlite(op))
            now = iso8601_utc(time.time())
            conn.execute(
                f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, applied_at, description) VALUES (?, ?, ?)",
                (migration.version, now, migration.description),
            )
    return current_schema_version(conn, store)


def _pending_for_store(store: str, target: int | None, applied: set[int]) -> list[Migration]:
    """Un-applied migrations relevant to ``store``, capped at ``target``."""
    pending = sorted(
        (m for m in MIGRATIONS if m.version not in applied and m.applies_to(store)),
        key=lambda m: m.version,
    )
    if target is not None:
        pending = [m for m in pending if m.version <= target]
    return pending


# ---------------------------------------------------------------- v1 frozen schema

_NODES_TABLE = CreateTable(
    store="graph",
    name="nodes",
    columns=(
        Column("node_id", "TEXT", primary_key=True),
        Column("node_type", "TEXT", not_null=True),
        Column("profile_id", "TEXT", not_null=True),
        Column("payload", "JSON", not_null=True),
        Column("entities", "JSON", not_null=True, default="[]"),
        Column("confidence", "REAL", not_null=True, default=0.5),
        Column("decay_weight", "REAL", not_null=True, default=1.0),
        Column("never_decay", "INTEGER", not_null=True, default=0),
        Column("conflict_flag", "INTEGER", not_null=True, default=0),
        Column("conflict_group", "TEXT"),
        Column("needs_reconcile", "INTEGER", not_null=True, default=0),
        Column("pending_consolidation", "INTEGER", not_null=True, default=0),
        Column("peripheral_gaps", "INTEGER", not_null=True, default=0),
        Column("valid_from", "TEXT", not_null=True),
        Column("valid_to", "TEXT"),
        Column("last_reinforced", "TEXT", not_null=True),
        Column("hit_count", "INTEGER", not_null=True, default=0),
        Column("last_hit_at", "TEXT"),
        Column("reinforce_count", "INTEGER", not_null=True, default=0),
        Column("cognitive_tier", "INTEGER", not_null=True, default=1),
        Column("provenance", "JSON", not_null=True),
        Column("created_at", "TEXT", not_null=True),
        Column("updated_at", "TEXT", not_null=True),
        Column("version", "INTEGER", not_null=True),
        Column("prev_version_id", "TEXT"),
    ),
)

_NODE_VERSIONS_TABLE = CreateTable(
    store="graph",
    name="node_versions",
    columns=(
        Column("node_id", "TEXT", not_null=True),
        Column("version", "INTEGER", not_null=True),
        Column("profile_id", "TEXT", not_null=True),
        Column("valid_from", "TEXT", not_null=True),
        Column("valid_to", "TEXT"),
        Column("superseded_by", "INTEGER"),
        Column("changed_at", "TEXT", not_null=True),
        Column("payload", "JSON", not_null=True),
    ),
    # version-chain identity: one physical row per (node_id, version); the
    # driver's INSERT OR REPLACE turns into real replace semantics here.
    unique=(("node_id", "version"),),
)

_EDGES_TABLE = CreateTable(
    store="graph",
    name="edges",
    columns=(
        Column("id", "TEXT", primary_key=True),
        Column("src", "TEXT", not_null=True),
        Column("dst", "TEXT", not_null=True),
        Column("rel", "TEXT", not_null=True),
        Column("weight", "REAL", not_null=True, default=1.0),
        Column("profile_id", "TEXT", not_null=True),
        Column("provenance", "JSON", not_null=True, default="{}"),
        Column("created_at", "TEXT", not_null=True),
    ),
)

_PROFILES_TABLE = CreateTable(
    store="meta",
    name="profiles",
    columns=(
        Column("profile_id", "TEXT", primary_key=True),
        Column("display_name", "TEXT", not_null=True, default=""),
        Column("created_at", "TEXT", not_null=True),
    ),
)

_TOKENS_TABLE = CreateTable(
    store="meta",
    name="tokens",
    columns=(
        Column("token_id", "TEXT", primary_key=True),
        Column(
            "profile_id", "TEXT", not_null=True, references=("profiles", "profile_id"), on_delete="cascade"
        ),
        Column("scopes", "JSON", not_null=True, default="[]"),
        Column("issued_at", "TEXT", not_null=True),
        Column("expires_at", "TEXT"),
        Column("revoked", "INTEGER", not_null=True, default=0),
    ),
)

_SCORE_POOL_TABLE = CreateTable(
    store="meta",
    name="score_pool",
    columns=(
        Column("id", "INTEGER", primary_key=True, default=1),
        Column("balance", "REAL", not_null=True, default=0.0),
        Column("watermark_start", "INTEGER", not_null=True, default=0),
        Column("watermark_end", "INTEGER", not_null=True, default=0),
        Column("last_event_start", "INTEGER", not_null=True, default=0),
        Column("last_event_end", "INTEGER", not_null=True, default=0),
    ),
)

_CONFIG_TABLE = CreateTable(
    store="meta",
    name="config",
    columns=(
        Column("key", "TEXT", not_null=True),
        Column("value", "JSON", not_null=True),
        Column("version", "INTEGER", not_null=True),
        Column("updated_at", "TEXT", not_null=True),
    ),
)

_AUDIT_LOG_TABLE = CreateTable(
    store="meta",
    name="audit_log",
    columns=(
        Column("id", "INTEGER", primary_key=True),
        Column("actor", "TEXT", not_null=True),
        Column("action", "TEXT", not_null=True),
        Column("detail", "JSON", not_null=True, default="{}"),
        Column("at", "TEXT", not_null=True),
    ),
)

_DREAM_RUNS_TABLE = CreateTable(
    store="meta",
    name="dream_runs",
    columns=(
        Column("run_id", "TEXT", primary_key=True),
        Column("session_id", "TEXT"),
        Column("turn_start", "INTEGER"),
        Column("turn_end", "INTEGER"),
        Column("model_id", "TEXT"),
        Column("started_at", "TEXT", not_null=True),
        Column("finished_at", "TEXT"),
        Column("tokens", "INTEGER", not_null=True, default=0),
        Column("cost", "REAL", not_null=True, default=0.0),
        Column("interrupted", "INTEGER", not_null=True, default=0),
        Column("dropped_count", "INTEGER", not_null=True, default=0),
    ),
)

_AUDIT_UPDATE_TRIGGER = AddTrigger(
    store="meta",
    name="trg_audit_no_update",
    timing="BEFORE",
    event="UPDATE",
    table="audit_log",
    action="BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
    pg_action="RAISE EXCEPTION 'audit_log is append-only'",
)

_AUDIT_DELETE_TRIGGER = AddTrigger(
    store="meta",
    name="trg_audit_no_delete",
    timing="BEFORE",
    event="DELETE",
    table="audit_log",
    action="BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
    pg_action="RAISE EXCEPTION 'audit_log is append-only'",
)

# v2: the AC-5 harmless-column demonstration migration. The frozen v1 schema is
# complete in its own right; v2 exists so a real 1->2 upgrade with data
# preservation can be exercised on SQLite and mirrored on Postgres.
_V2_ADD_PINNED = AddColumn(
    store="graph",
    table="nodes",
    column=Column("pinned", "INTEGER", not_null=True, default=0),
)

# v3: per-profile score-pool persistence (M1-T6). The v1 score_pool singleton
# is superseded rather than dropped: legacy installs keep their singleton row
# untouched (no data migration), and the per-profile table carries each
# profile's live balance and watermark keyed by profile_id (no FK).
_PROFILE_SCORE_POOL_TABLE = CreateTable(
    store="meta",
    name="profile_score_pool",
    columns=(
        Column("profile_id", "TEXT", primary_key=True),
        Column("balance", "REAL", not_null=True, default=0.0),
        Column("watermark_start", "INTEGER", not_null=True, default=0),
        Column("watermark_end", "INTEGER", not_null=True, default=0),
        Column("last_event_start", "INTEGER", not_null=True, default=0),
        Column("last_event_end", "INTEGER", not_null=True, default=0),
    ),
)

# v4: per-profile monthly dream-token ledger (M1-T5b, FR-2.5b). One row per
# (profile_id, UTC year_month); the composite UNIQUE is the auto-recovery key and
# the ON CONFLICT increment target on both dialects. Born empty — no backfill:
# existing installs simply start metering from day one of the upgrade.
_DREAM_TOKEN_LEDGER_TABLE = CreateTable(
    store="meta",
    name="dream_token_ledger",
    columns=(
        Column("profile_id", "TEXT", not_null=True),
        Column("year_month", "TEXT", not_null=True),
        Column("tokens", "INTEGER", not_null=True, default=0),
    ),
    unique=(("profile_id", "year_month"),),
)

# v5: promotion-gate status carrier on graph.nodes (prd-08 A.2 "promotion-status field
# (v5)"). No data rewrite: NOT NULL DEFAULT 'promoted' back-fills every existing
# row as promoted (back-compat), and the gate logic itself is a later task.
_V5_ADD_PROMOTION_STATUS = AddColumn(
    store="graph",
    table="nodes",
    column=Column("promotion_status", "TEXT", not_null=True, default="promoted"),
)

# v6 (PRD-06 FR-6.1a/b, issue #14): identity chain. The users table carries the
# local owner account (username UNIQUE, password_hash = argon2id digest computed
# by the identity service — the port only stores what it is given); tokens gain
# a nullable ``token_hash`` column holding the sha256 digest of the bearer
# secret. Nullable because v1-era tokens predate hashing: pre-v6 bearer values
# were plaintext token_ids, so after the upgrade they simply stop authenticating
# (the digest never matches) — re-login re-issues. No data rewrite, born empty.
_USERS_TABLE = CreateTable(
    store="meta",
    name="users",
    columns=(
        Column("user_id", "TEXT", primary_key=True),
        Column("username", "TEXT", not_null=True),
        Column("password_hash", "TEXT", not_null=True),
        Column("role", "TEXT", not_null=True, default="owner"),
        Column("created_at", "TEXT", not_null=True),
    ),
    unique=(("username",),),
)

_V6_ADD_TOKEN_HASH = AddColumn(
    store="meta",
    table="tokens",
    column=Column("token_hash", "TEXT"),
)

_V6_TOKEN_HASH_INDEX = CreateIndex(
    store="meta",
    name="idx_tokens_hash",
    table="tokens",
    columns=("token_hash",),
)

# v7 (PRD-07 FR-7.3 console profile archive): the archived flag on profiles.
# No data rewrite: NOT NULL DEFAULT 0 back-fills every existing row as active;
# the console surface reads/writes it through the meta port.
_V7_ADD_PROFILE_ARCHIVED = AddColumn(
    store="meta",
    table="profiles",
    column=Column("archived", "INTEGER", not_null=True, default=0),
)

# v8 (E1-4 / D1 "settings DB primary"): reserved nullable ``scope`` column on
# the versioned config table. Phase 0 reserves the column only — rows written
# today carry NULL scope (settings are system-wide until per-scope settings
# land); nullable + no default so every existing row back-fills NULL.
_V8_ADD_CONFIG_SCOPE = AddColumn(
    store="meta",
    table="config",
    column=Column("scope", "TEXT"),
)

# v9 (B2.11 score-pool split): lifetime filed-points ledger column on the
# per-profile score pool. NOT NULL DEFAULT 0 back-fills every existing row as
# born-empty; incremental writes happen only inside the atomic pool_drain.
_V9_ADD_POOL_FILED_TOTAL = AddColumn(
    store="meta",
    table="profile_score_pool",
    column=Column("filed_points_total", "REAL", not_null=True, default=0),
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description=(
            "frozen v1 schema: graph (nodes/edges/node_versions) and meta "
            "(profiles/tokens/score_pool/config/audit_log/dream_runs)"
        ),
        ops=(
            _NODES_TABLE,
            CreateIndex(
                store="graph",
                name="idx_nodes_profile_type",
                table="nodes",
                columns=("profile_id", "node_type"),
            ),
            CreateIndex(store="graph", name="idx_nodes_valid", table="nodes", columns=("valid_to",)),
            _NODE_VERSIONS_TABLE,
            CreateIndex(
                store="graph",
                name="idx_node_versions_temporal",
                table="node_versions",
                columns=("profile_id", "valid_from", "valid_to"),
            ),
            _EDGES_TABLE,
            CreateIndex(store="graph", name="idx_edges_src", table="edges", columns=("src",)),
            CreateIndex(store="graph", name="idx_edges_dst", table="edges", columns=("dst",)),
            CreateIndex(store="graph", name="idx_edges_profile", table="edges", columns=("profile_id",)),
            _PROFILES_TABLE,
            _TOKENS_TABLE,
            CreateIndex(store="meta", name="idx_tokens_profile", table="tokens", columns=("profile_id",)),
            _SCORE_POOL_TABLE,
            _CONFIG_TABLE,
            _AUDIT_LOG_TABLE,
            CreateIndex(store="meta", name="idx_audit_at", table="audit_log", columns=("at",)),
            _AUDIT_UPDATE_TRIGGER,
            _AUDIT_DELETE_TRIGGER,
            _DREAM_RUNS_TABLE,
            CreateIndex(store="meta", name="idx_dream_session", table="dream_runs", columns=("session_id",)),
        ),
    ),
    Migration(
        version=2,
        description="AC-5 parity: harmless pinned column on graph.nodes",
        ops=(_V2_ADD_PINNED,),
    ),
    Migration(
        version=3,
        description="per-profile score pool: profile_id-keyed balances and watermarks",
        ops=(_PROFILE_SCORE_POOL_TABLE,),
    ),
    Migration(
        version=4,
        description="monthly dream token ledger: per-profile (profile_id, year_month) counters",
        ops=(_DREAM_TOKEN_LEDGER_TABLE,),
    ),
    Migration(
        version=5,
        description="promotion-gate status carrier on graph.nodes (default promoted, back-compat)",
        ops=(_V5_ADD_PROMOTION_STATUS,),
    ),
    Migration(
        version=6,
        description=(
            "identity chain: users table (owner account, username UNIQUE) + "
            "nullable tokens.token_hash digest column + lookup index"
        ),
        ops=(_USERS_TABLE, _V6_ADD_TOKEN_HASH, _V6_TOKEN_HASH_INDEX),
    ),
    Migration(
        version=7,
        description=("console profile archive: NOT NULL DEFAULT 0 archived flag on profiles (PRD-07 FR-7.3)"),
        ops=(_V7_ADD_PROFILE_ARCHIVED,),
    ),
    Migration(
        version=8,
        description=(
            "settings DB primary (E1-4): reserved nullable scope column on config "
            "(NULL today, system-wide until per-scope settings land)"
        ),
        ops=(_V8_ADD_CONFIG_SCOPE,),
    ),
    Migration(
        version=9,
        description=(
            "score-pool split: lifetime filed_points_total ledger column on "
            "profile_score_pool (born-empty backfill, written only by pool_drain)"
        ),
        ops=(_V9_ADD_POOL_FILED_TOTAL,),
    ),
)


def latest_version() -> int:
    """Highest defined migration version (the end state for fresh installs)."""
    return max(m.version for m in MIGRATIONS)
