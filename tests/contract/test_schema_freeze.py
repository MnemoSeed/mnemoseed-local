"""AC-6 schema-freeze checklist walk over appendix A.

An automated, offline walk of the frozen schema surfaces: the vector storage
shape (A.1), the graph relational schema (A.2), the meta relational schema
(A.3), and the ability to construct named graph instances (D6 "main" plus a
second name) on both backends. Freeze checks assert stability, not behavior;
behavioral semantics live in the ``test_contract_*`` modules.
"""

from __future__ import annotations

import asyncio

import pyarrow as pa

from mnemoseed_local.schema.graph import NodeType
from mnemoseed_local.storage.drivers._migrations import (
    MIGRATIONS,
    AddColumn,
    AddTrigger,
    CreateIndex,
    CreateTable,
    current_schema_version,
)
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver

_DIM = 64

# ---------------------------------------------------------------- A.1 vector


FROZEN_A1_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "profile_id",
    "text",
    "vector_dense",
    "vector_sparse",
    "session_id",
    "turn_start",
    "turn_end",
    "cognitive_tier",
    "model_id",
    "persona_id",
    "cues",
    "provenance",
    "score",
    "decay_weight",
    "last_reinforced",
    "consolidated",
    "ingested_at",
    "peripheral_gaps",
    "needs_reconcile",
    "hit_count",
    "last_hit_at",
    "reinforce_count",
)


def test_vector_schema_freeze_lance(tmp_path) -> None:
    """lancedb_embedded._schema(): every A.1 field plus the structured legs."""
    store = LanceDbEmbeddedStore(uri=str(tmp_path / "freeze.lance"), dimensions=_DIM)
    schema = store._schema()
    names = [field.name for field in schema]
    for field in FROZEN_A1_FIELDS:
        assert field in names, f"A.1 field {field!r} missing from the lance schema"

    assert len(names) == len(set(names))
    sparse = _field_type(schema, "vector_sparse", pa.StructType)
    assert set(sparse.names) == {"indices", "values"}
    cues = _field_type(schema, "cues", pa.StructType)
    for subfield in ("project", "host", "task", "tools_used", "time_bucket", "emotion_valence", "entities"):
        assert subfield in cues.names, f"cues.{subfield} missing from the lance schema"

    # turn bounds are stored columns but nullable: plain chunks have no window
    for name in ("session_id", "turn_start", "turn_end"):
        assert schema.field(name).nullable


def _field_type(schema: pa.Schema, name: str, kind: type) -> pa.StructType:
    field = schema.field(name)
    assert isinstance(field.type, kind), f"{name} should be {kind!r}, got {field.type!r}"
    return field.type


# ---------------------------------------------------------------- A.2 graph


_FROZEN_GRAPH_TABLES = ("nodes", "node_versions", "edges")
_FROZEN_GRAPH_INDEXES = (
    "idx_nodes_profile_type",
    "idx_nodes_valid",
    "idx_node_versions_temporal",
    "idx_edges_src",
    "idx_edges_dst",
    "idx_edges_profile",
)
_FROZEN_NODE_TYPES = frozenset(
    {
        "USER",
        "HABIT",
        "PREFERENCE",
        "ANIMA",
        "INTENTION",
        "CONSTRAINT",
        "EPISODE",
        "SKILL_SEQUENCE",
        "DECISION",
        "PROJECT",
        "TOOL",
    }
)
_NODES_LIFECYCLE_COLUMNS = (
    "valid_from",
    "valid_to",
    "version",
    "prev_version_id",
    "pending_consolidation",
    "needs_reconcile",
    "hit_count",
    "peripheral_gaps",
)


def test_node_type_enum_is_frozen() -> None:
    assert len(NodeType.frozen_set()) == 11
    assert NodeType.frozen_set() == _FROZEN_NODE_TYPES


def test_graph_schema_freeze_walk() -> None:
    tables: dict[str, CreateTable] = {}
    indexes: set[str] = set()
    added_columns: set[str] = set()
    for migration in MIGRATIONS:
        for op in migration.ops:
            if isinstance(op, CreateTable) and op.store == "graph":
                tables[op.name] = op
            elif isinstance(op, CreateIndex) and op.store == "graph":
                indexes.add(op.name)
            elif isinstance(op, AddColumn) and op.store == "graph":
                added_columns.add(op.column.name)
    assert set(tables) == set(_FROZEN_GRAPH_TABLES)
    assert set(_FROZEN_GRAPH_INDEXES) == indexes

    names = {column.name for column in tables["nodes"].columns}
    assert "node_id" in names and "payload" in names and "entities" in names
    assert set(_NODES_LIFECYCLE_COLUMNS) <= names
    # v2/v5 freeze items: the pinned and promotion_status columns enter the head
    # schema via AddColumn deltas, never as v1 base columns
    assert "pinned" in added_columns
    assert "pinned" not in names, "pinned is a migration delta, not a v1 base column"
    assert "promotion_status" in added_columns
    assert "promotion_status" not in names, "promotion_status is a migration delta, not a v1 base column"
    # v10: the read-side conflict evidence pointer is an AddColumn delta, never a
    # v1 base column — readers raise it reversibly, capture and vote never touch it
    assert "read_conflict_id" in added_columns
    assert "read_conflict_id" not in names, "read_conflict_id is a migration delta, not a v1 base column"


# ---------------------------------------------------------------- A.3 meta


_FROZEN_META_TABLES = (
    "profiles",
    "tokens",
    "users",
    "score_pool",
    "profile_score_pool",
    "config",
    "audit_log",
    "dream_runs",
    "dream_token_ledger",
)
_FROZEN_META_INDEXES = ("idx_tokens_profile", "idx_audit_at", "idx_dream_session", "idx_tokens_hash")
_FROZEN_TRIGGERS = (("trg_audit_no_update", "UPDATE"), ("trg_audit_no_delete", "DELETE"))


def test_meta_schema_freeze_walk() -> None:
    tables: dict[str, CreateTable] = {}
    indexes: set[str] = set()
    triggers: set[tuple[str, str]] = set()
    added_meta_columns: set[str] = set()
    for migration in MIGRATIONS:
        for op in migration.ops:
            if isinstance(op, CreateTable) and op.store == "meta":
                tables[op.name] = op
            elif isinstance(op, CreateIndex) and op.store == "meta":
                indexes.add(op.name)
            elif isinstance(op, AddColumn) and op.store == "meta":
                added_meta_columns.add(op.column.name)
            elif isinstance(op, AddTrigger) and op.store == "meta":
                assert op.table == "audit_log"
                triggers.add((op.name, op.event))
    assert set(tables) == set(_FROZEN_META_TABLES)
    assert set(_FROZEN_META_INDEXES) == indexes
    assert triggers == set(_FROZEN_TRIGGERS)

    tokens = tables["tokens"]
    profile_fk = next(column.references for column in tokens.columns if column.name == "profile_id")
    assert profile_fk == ("profiles", "profile_id")
    # v6 identity-delta freeze items: the users table carries the owner account
    # (username UNIQUE); tokens.token_hash is the bearer-digest AddColumn delta,
    # never a v1 base column (pre-v6 tokens stop authenticating after upgrade)
    user_names = {column.name for column in tables["users"].columns}
    assert {"user_id", "username", "password_hash", "role", "created_at"} <= user_names
    assert "token_hash" in added_meta_columns
    assert "token_hash" not in {column.name for column in tokens.columns}
    # v8 (E1-4): the reserved config.scope column lands as a migration delta,
    # never a v1 base column — the D1 "settings DB primary" reservation.
    assert "scope" in added_meta_columns
    assert "scope" not in {column.name for column in tables["config"].columns}


# ------------------------------------------------------- D6 named graph instances


def test_named_graph_instances_build(tmp_path) -> None:
    """D6: "main" and a second named instance come up on sqlite."""
    main = SqliteGraphDriver(path=tmp_path / "graph-main.db")
    isolated = SqliteGraphDriver(path=tmp_path / "graph-isolated.db")
    assert main.info.name == isolated.info.name == "sqlite_graph"
    assert current_schema_version(main._conn, "graph") == 10
    assert current_schema_version(isolated._conn, "graph") == 10
    asyncio.run(main.close())
    asyncio.run(isolated.close())
