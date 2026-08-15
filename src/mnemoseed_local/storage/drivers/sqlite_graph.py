"""SQLite graph driver: cortex over self-built adjacency tables.

Implements the full GraphStore protocol over three tables (nodes / edges /
node_versions) per prd-08 appendix A.2 with D6 double-instance support: each
instance is its own SQLite file (graph.main / graph.isolated). Version-chain
writes (invalidate / append_version) run in explicit transactions so a
reconsolidation never leaves the graph half-written.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from collections.abc import Sequence as CSeq
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mnemoseed_local.config import CONFIG_DIR
from mnemoseed_local.schema.graph import (
    Edge,
    GraphNode,
    RelType,
    validate_node_payload,
)
from mnemoseed_local.storage.drivers._migrations import apply_migrations
from mnemoseed_local.storage.drivers._threadlocal import ThreadLocalConnections
from mnemoseed_local.storage.drivers._time import epoch_from_iso, iso8601_utc
from mnemoseed_local.storage.ports import (
    Capability,
    DriverInfo,
    EdgeEntry,
    EdgeFilter,
    EdgeKind,
    GraphFlag,
    GraphWeightUpdate,
    IntentionStatus,
    NodeFilter,
    Page,
    PageResult,
    StorageError,
    TimelineEvent,
)
from mnemoseed_local.storage.registry import GRAPH_DRIVERS, register

_CAPABILITIES = frozenset(
    {
        Capability.GRAPH_TRAVERSE_2HOP,
        Capability.GRAPH_VERSION_CHAIN,
        Capability.GRAPH_COOCCURRENCE_EDGES,
        Capability.GRAPH_EDGE_LIST,
    }
)


@register(GRAPH_DRIVERS)
class SqliteGraphDriver:
    """GraphStore over a single SQLite file (nodes/edges/node_versions)."""

    info = DriverInfo(
        name="sqlite_graph",
        capabilities=_CAPABILITIES,
        description="self-built adjacency tables over SQLite (embedded default)",
    )

    _NODE_COLUMNS: tuple[str, ...] = (
        "node_id",
        "node_type",
        "profile_id",
        "payload",
        "entities",
        "confidence",
        "decay_weight",
        "never_decay",
        "conflict_flag",
        "conflict_group",
        "needs_reconcile",
        "pending_consolidation",
        "peripheral_gaps",
        "valid_from",
        "valid_to",
        "last_reinforced",
        "hit_count",
        "last_hit_at",
        "reinforce_count",
        "cognitive_tier",
        "provenance",
        "created_at",
        "updated_at",
        "version",
        "prev_version_id",
        "promotion_status",
    )

    def __init__(self, path: str | os.PathLike[str] | None = None, **kwargs: Any) -> None:
        default_path = CONFIG_DIR / "graph.db"
        self.params: dict[str, Any] = kwargs
        self._path = Path(os.path.expanduser(str(path))) if path is not None else default_path
        if kwargs:
            extra = kwargs.get("path")
            if extra is not None and path is None:
                self._path = Path(os.path.expanduser(str(extra)))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadLocalConnections(self._path)
        # The main-thread connection is opened eagerly here so migrations and
        # any direct ``driver._conn`` access (internals, tests) run on a live
        # handle; worker threads get their own handle on first use.
        apply_migrations(self._pool.get(), "graph")

    # ------------------------------------------------------------ capabilities

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    @property
    def _conn(self) -> sqlite3.Connection:
        """The calling thread's connection (one handle per thread)."""
        return self._pool.get()

    async def close(self) -> None:
        self._pool.close_all()

    # ------------------------------------------------------------ helpers

    def _node_row(self, node: GraphNode) -> tuple[Any, ...]:
        row: dict[str, Any] = {
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
            "promotion_status": node.promotion_status.value,
        }
        return tuple(row[column] for column in self._NODE_COLUMNS)

    def _decode_node(self, row: sqlite3.Row) -> GraphNode:
        data: dict[str, Any] = {
            "node_id": str(row["node_id"]),
            "node_type": str(row["node_type"]),
            "profile_id": str(row["profile_id"]),
            "props": json.loads(str(row["payload"])),
            "entities": json.loads(str(row["entities"])),
            "confidence": float(row["confidence"]),
            "decay_weight": float(row["decay_weight"]),
            "never_decay": bool(int(row["never_decay"])),
            "conflict_flag": bool(int(row["conflict_flag"])),
            "conflict_group": row["conflict_group"],
            "needs_reconcile": bool(int(row["needs_reconcile"])),
            "pending_consolidation": bool(int(row["pending_consolidation"])),
            "peripheral_gaps": bool(int(row["peripheral_gaps"])),
            "valid_from": epoch_from_iso(str(row["valid_from"])),
            "valid_to": _maybe_epoch(row["valid_to"]),
            "last_reinforced": epoch_from_iso(str(row["last_reinforced"])),
            "hit_count": int(row["hit_count"]),
            "last_hit_at": _maybe_epoch(row["last_hit_at"]),
            "reinforce_count": int(row["reinforce_count"]),
            "cognitive_tier": int(row["cognitive_tier"]),
            "provenance": json.loads(str(row["provenance"])),
            "created_at": epoch_from_iso(str(row["created_at"])),
            "updated_at": epoch_from_iso(str(row["updated_at"])),
            "version": int(row["version"]),
            "prev_version_id": row["prev_version_id"],
            "promotion_status": str(row["promotion_status"]),
        }
        return GraphNode.model_validate(data)

    def _get_current_version(self, node_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT version FROM nodes WHERE node_id = ? AND valid_to IS NULL", (node_id,)
        ).fetchone()
        return int(row["version"]) if row is not None else None

    def _write_revision(self, node: GraphNode, invalidate_at: float | None = None) -> None:
        """Write ``node`` as the current revision in one transaction.

        When ``invalidate_at`` is given, the current revision is closed at that
        epoch and marked superseded by ``node`` in the same transaction (PRD-08
        appendix B.2: invalidate + append_version is one atomic unit). Without
        it, a lower-versioned current revision is auto-superseded at
        ``node.valid_from`` when ``node.version`` is higher.
        """
        placeholders = ", ".join(["?"] * len(self._NODE_COLUMNS))
        columns = ", ".join(self._NODE_COLUMNS)
        values = self._node_row(node)
        with _transaction(self._conn):
            validate_node_payload(node.node_type, node.props)
            current_version = self._get_current_version(node.node_id)
            if current_version is not None:
                if invalidate_at is not None:
                    self._invalidate_locked(node.node_id, invalidate_at)
                    self._conn.execute(
                        "UPDATE node_versions SET superseded_by = ? WHERE node_id = ? AND version = ?",
                        (node.version, node.node_id, current_version),
                    )
                elif current_version < node.version:
                    self._supersede_snapshot(node.node_id, current_version, node.version, node.valid_from)
            self._conn.execute(f"INSERT OR REPLACE INTO nodes ({columns}) VALUES ({placeholders})", values)
            self._conn.execute(
                "INSERT OR REPLACE INTO node_versions "
                "(node_id, version, profile_id, valid_from, valid_to, superseded_by, changed_at, payload) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    node.node_id,
                    node.version,
                    node.profile_id,
                    iso8601_utc(node.valid_from),
                    iso8601_utc(node.valid_to) if node.valid_to is not None else None,
                    iso8601_utc(node.updated_at),
                    node.model_dump_json(),
                ),
            )

    def _supersede_snapshot(
        self, node_id: str, old_version: int, new_version: int, took_over_at: float
    ) -> None:
        """Link the old snapshot as superseded by ``new_version``."""
        row = self._conn.execute(
            "SELECT payload, valid_to FROM node_versions WHERE node_id = ? AND version = ?",
            (node_id, old_version),
        ).fetchone()
        if row is None:
            return
        payload = json.loads(str(row["payload"]))
        if row["valid_to"] is None and payload.get("valid_to") is None:
            payload["valid_to"] = took_over_at
            self._conn.execute(
                "UPDATE node_versions SET payload = ?, valid_to = ? WHERE node_id = ? AND version = ?",
                (
                    json.dumps(payload),
                    iso8601_utc(took_over_at),
                    node_id,
                    old_version,
                ),
            )
        self._conn.execute(
            "UPDATE node_versions SET superseded_by = ? WHERE node_id = ? AND version = ?",
            (new_version, node_id, old_version),
        )

    # ------------------------------------------------------------ node CRUD

    def upsert_node(self, node: GraphNode) -> None:
        self._write_revision(node)

    def get_node(self, node_id: str) -> GraphNode | None:
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE node_id = ? AND valid_to IS NULL", (node_id,)
        ).fetchone()
        return self._decode_node(row) if row is not None else None

    def list_nodes(self, filter: NodeFilter, page: Page) -> PageResult[GraphNode]:
        clauses = ["valid_to IS NULL", "profile_id = ?"]
        params: list[Any] = [filter.profile_id]
        if filter.node_type is not None:
            clauses.append("node_type = ?")
            params.append(filter.node_type.value)
        if filter.min_decay > 0.0:
            clauses.append("decay_weight >= ?")
            params.append(filter.min_decay)
        if filter.entities:
            placeholders = ", ".join(["?"] * len(filter.entities))
            clauses.append(f"EXISTS (SELECT 1 FROM json_each(entities) e WHERE e.value IN ({placeholders}))")
            params.extend(filter.entities)
        where = " AND ".join(clauses)
        total = _count(self._conn, "nodes", where, params)
        rows = self._conn.execute(
            f"SELECT * FROM nodes WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            [*params, page.limit, page.offset],
        ).fetchall()
        items = [self._decode_node(r) for r in rows]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    def find_same_predicate(self, subject: str, predicate: str, profile_id: str) -> list[GraphNode]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE profile_id = ? AND valid_to IS NULL "
            "AND json_extract(payload, '$.subject') = ? AND json_extract(payload, '$.predicate') = ?",
            (profile_id, subject, predicate),
        ).fetchall()
        return [self._decode_node(r) for r in rows]

    # ------------------------------------------------------------ edges

    def add_edge(self, edge: Edge) -> None:
        with _transaction(self._conn):
            existing = self._conn.execute(
                "SELECT id FROM edges WHERE src = ? AND dst = ? AND rel = ? AND profile_id = ?",
                (edge.src, edge.dst, edge.rel.value, edge.profile_id),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE edges SET weight = ? WHERE id = ?", (edge.weight, str(existing["id"]))
                )
            else:
                self._conn.execute(
                    "INSERT INTO edges (id, src, dst, rel, weight, profile_id, provenance, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        edge.src,
                        edge.dst,
                        edge.rel.value,
                        edge.weight,
                        edge.profile_id,
                        "{}",
                        iso8601_utc(edge.created_at),
                    ),
                )

    def bump_cooccurrence(self, node_a: str, node_b: str, profile_id: str) -> None:
        with _transaction(self._conn):
            existing = self._conn.execute(
                "SELECT id, weight FROM edges WHERE rel = ? AND profile_id = ? "
                "AND ((src = ? AND dst = ?) OR (src = ? AND dst = ?))",
                (RelType.CO_OCCURRED.value, profile_id, node_a, node_b, node_b, node_a),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE edges SET weight = ? WHERE id = ?",
                    (float(existing["weight"]) + 1.0, str(existing["id"])),
                )
            else:
                self._conn.execute(
                    "INSERT INTO edges (id, src, dst, rel, weight, profile_id, provenance, created_at) "
                    "VALUES (?, ?, ?, ?, 1.0, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        node_a,
                        node_b,
                        RelType.CO_OCCURRED.value,
                        profile_id,
                        "{}",
                        iso8601_utc(time.time()),
                    ),
                )

    def list_edges(self, filter: EdgeFilter, page: Page) -> PageResult[EdgeEntry]:
        """Bulk edge listing (prd-08 appendix B.2 v1.1): the console Graph View
        reads one page of the profile's edges with a stable order (created_at
        desc, edge id asc). BOTH endpoints must be current-revision nodes
        (valid_to IS NULL) — unconditionally, so an edge touching a tombstoned
        or superseded node never leaks; ``node_types`` / ``tier`` additionally
        require both endpoints to match the type / tier. The time window and
        min_weight apply to the edge row itself. ``kind`` collapses rel to the
        document vocabulary: ``co_occurred`` is cooccurrence, everything else
        is relation."""
        clauses = ["e.profile_id = ?"]
        params: list[Any] = [filter.profile_id]
        if filter.min_weight > 0.0:
            clauses.append("e.weight >= ?")
            params.append(filter.min_weight)
        if filter.created_after is not None:
            clauses.append("e.created_at >= ?")
            params.append(iso8601_utc(filter.created_after))
        if filter.created_before is not None:
            clauses.append("e.created_at <= ?")
            params.append(iso8601_utc(filter.created_before))
        src_conds = ["na.node_id = e.src", "na.valid_to IS NULL"]
        dst_conds = ["nb.node_id = e.dst", "nb.valid_to IS NULL"]
        if filter.node_types:
            types = [t.value for t in filter.node_types]
            placeholders = ", ".join(["?"] * len(types))
            src_conds.append(f"na.node_type IN ({placeholders})")
            dst_conds.append(f"nb.node_type IN ({placeholders})")
            params.extend(types)
            params.extend(types)
        if filter.tier is not None:
            src_conds.append("na.cognitive_tier = ?")
            dst_conds.append("nb.cognitive_tier = ?")
            params.append(filter.tier)
            params.append(filter.tier)
        clauses.append("EXISTS (SELECT 1 FROM nodes na WHERE " + " AND ".join(src_conds) + ")")
        clauses.append("EXISTS (SELECT 1 FROM nodes nb WHERE " + " AND ".join(dst_conds) + ")")
        where = " AND ".join(clauses)
        total = _count(self._conn, "edges e", where, params)
        rows = self._conn.execute(
            f"SELECT e.* FROM edges e WHERE {where} ORDER BY e.created_at DESC, e.id ASC LIMIT ? OFFSET ?",
            [*params, page.limit, page.offset],
        ).fetchall()
        items = [
            EdgeEntry(
                edge_id=str(row["id"]),
                src=str(row["src"]),
                dst=str(row["dst"]),
                kind=(
                    EdgeKind.COOCCURRENCE
                    if str(row["rel"]) == RelType.CO_OCCURRED.value
                    else EdgeKind.RELATION
                ),
                weight=float(row["weight"]),
                created_at=epoch_from_iso(str(row["created_at"])),
            )
            for row in rows
        ]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    def traverse(self, node_id: str, depth: int = 2, filter: NodeFilter | None = None) -> list[GraphNode]:
        if depth < 0:
            raise ValueError("traverse depth must be non-negative")
        depth = min(depth, 2)
        order: list[str] = []
        visited: set[str] = set()
        frontier: list[str] = [node_id]
        for hop in range(depth + 1):
            next_frontier: list[str] = []
            for nid in frontier:
                if nid in visited:
                    continue
                visited.add(nid)
                order.append(nid)
                if hop == depth:
                    continue
                params: list[Any] = [nid, nid, nid]
                profile_clause = ""
                if filter is not None:
                    profile_clause = " AND profile_id = ?"
                    params.append(filter.profile_id)
                neighbors = self._conn.execute(
                    "SELECT CASE WHEN src = ? THEN dst ELSE src END AS neighbor "
                    f"FROM edges WHERE (src = ? OR dst = ?){profile_clause}",
                    params,
                ).fetchall()
                next_frontier.extend(str(n["neighbor"]) for n in neighbors)
            frontier = next_frontier
        if not order:
            return []
        placeholders = ", ".join(["?"] * len(order))
        rows = self._conn.execute(
            f"SELECT * FROM nodes WHERE node_id IN ({placeholders}) AND valid_to IS NULL", order
        ).fetchall()
        decoded = {node.node_id: node for node in (self._decode_node(r) for r in rows)}
        result = [decoded[nid] for nid in order if nid in decoded]
        if filter is not None:
            result = [
                n
                for n in result
                if (filter.node_type is None or n.node_type is filter.node_type)
                and n.decay_weight >= filter.min_decay
            ]
        return result

    # ------------------------------------------------------------ flags

    def set_flags(self, nodes: CSeq[str], flags: CSeq[GraphFlag]) -> None:
        self._apply_flags(nodes, flags, set_to=True)

    def clear_flags(self, nodes: CSeq[str], flags: CSeq[GraphFlag]) -> None:
        self._apply_flags(nodes, flags, set_to=False)

    def _apply_flags(self, nodes: CSeq[str], flags: CSeq[GraphFlag], set_to: bool) -> None:
        if not nodes:
            return
        placeholders = ", ".join(["?"] * len(nodes))
        params: list[Any] = list(nodes)
        with _transaction(self._conn):
            if GraphFlag.CONFLICT_GROUP in flags:
                if set_to:
                    existing = self._conn.execute(
                        f"SELECT conflict_group FROM nodes WHERE node_id IN ({placeholders}) "
                        "AND valid_to IS NULL",
                        params,
                    ).fetchall()
                    group_id = next(
                        (str(r["conflict_group"]) for r in existing if r["conflict_group"] is not None), None
                    )
                    if group_id is None:
                        group_id = uuid.uuid4().hex
                    self._conn.execute(
                        f"UPDATE nodes SET conflict_flag = 1, conflict_group = ? "
                        f"WHERE node_id IN ({placeholders}) AND valid_to IS NULL",
                        [group_id, *params],
                    )
                else:
                    self._conn.execute(
                        f"UPDATE nodes SET conflict_flag = 0, conflict_group = NULL "
                        f"WHERE node_id IN ({placeholders}) AND valid_to IS NULL",
                        params,
                    )
            column_map: dict[GraphFlag, str] = {
                GraphFlag.NEEDS_RECONCILE: "needs_reconcile",
                GraphFlag.PENDING_CONSOLIDATION: "pending_consolidation",
                GraphFlag.PERIPHERAL_GAPS: "peripheral_gaps",
            }
            for flag, column in column_map.items():
                if flag not in flags:
                    continue
                value = 1 if set_to else 0
                self._conn.execute(
                    f"UPDATE nodes SET {column} = ? WHERE node_id IN ({placeholders}) AND valid_to IS NULL",
                    [value, *params],
                )

    # ------------------------------------------------------------ version chain

    def invalidate(self, node_id: str, valid_to: float) -> None:
        """Close the current revision at ``valid_to`` (single atomic step)."""
        with _transaction(self._conn):
            self._invalidate_locked(node_id, valid_to)

    def _invalidate_locked(self, node_id: str, valid_to: float) -> None:
        """Close the current revision at ``valid_to`` (caller holds a transaction)."""
        current_version = self._get_current_version(node_id)
        if current_version is None:
            return
        row = self._conn.execute(
            "SELECT payload FROM node_versions WHERE node_id = ? AND version = ?",
            (node_id, current_version),
        ).fetchone()
        if row is not None:
            payload = json.loads(str(row["payload"]))
            if payload.get("valid_to") is None:
                payload["valid_to"] = valid_to
            self._conn.execute(
                "UPDATE node_versions SET valid_to = ?, payload = ? WHERE node_id = ? AND version = ?",
                (iso8601_utc(valid_to), json.dumps(payload), node_id, current_version),
            )
        self._conn.execute(
            "UPDATE nodes SET valid_to = ?, updated_at = ? WHERE node_id = ? AND valid_to IS NULL",
            (iso8601_utc(valid_to), iso8601_utc(time.time()), node_id),
        )

    def tombstone(self, node_id: str, deleted_at: float | None = None) -> bool:
        """Tombstone the current revision (design/03 storage-layer erasure, GDPR right-to-erasure).

        Close the current revision at ``deleted_at`` and append a ``deleted``
        provenance event to that revision's version-chain payload. Nothing is
        physically removed: every current-revision read (get / list / traverse)
        and any future as_of stops seeing the node, while the chain survives for
        audit and as_of historical replay. Returns False when the node has no
        current revision to tombstone.
        """
        at = time.time() if deleted_at is None else deleted_at
        with _transaction(self._conn):
            current_version = self._get_current_version(node_id)
            if current_version is None:
                return False
            self._invalidate_locked(node_id, at)
            row = self._conn.execute(
                "SELECT payload FROM node_versions WHERE node_id = ? AND version = ?",
                (node_id, current_version),
            ).fetchone()
            if row is not None:
                payload = json.loads(str(row["payload"]))
                provenance = payload.setdefault("provenance", {})
                history = provenance.setdefault("history", [])
                if not any(event.get("action") == "deleted" for event in history):
                    history.append({"at": at, "action": "deleted", "actor": "user", "detail": {}})
                self._conn.execute(
                    "UPDATE node_versions SET payload = ? WHERE node_id = ? AND version = ?",
                    (json.dumps(payload), node_id, current_version),
                )
            return True

    def append_version(self, node: GraphNode, *, invalidate_at: float | None = None) -> None:
        """Write ``node`` as a new current revision.

        With ``invalidate_at``, closing the previous revision and writing
        ``node`` are one atomic unit (PRD-08 appendix B.2): a crash between the
        two steps is impossible — either both apply or neither does.
        """
        self._write_revision(node, invalidate_at=invalidate_at)

    def versions(self, node_id: str) -> list[GraphNode]:
        rows = self._conn.execute(
            "SELECT payload, version, valid_from, valid_to FROM node_versions "
            "WHERE node_id = ? ORDER BY version",
            (node_id,),
        ).fetchall()
        return [_decode_version(r) for r in rows]

    def diff(self, version_a: str, version_b: str) -> dict[str, Any]:
        node_a = self._fetch_version(version_a)
        node_b = self._fetch_version(version_b)
        if node_a is None or node_b is None:
            missing = version_a if node_a is None else version_b
            raise StorageError(f"unknown version identifier {missing!r}")
        changes = _deep_changes(node_a.model_dump(), node_b.model_dump())
        return {
            "a": {"node_id": node_a.node_id, "version": node_a.version},
            "b": {"node_id": node_b.node_id, "version": node_b.version},
            "changed": changes,
        }

    def timeline(self, node_id: str) -> list[TimelineEvent]:
        rows = self._conn.execute(
            "SELECT version, changed_at, payload FROM node_versions WHERE node_id = ? ORDER BY version",
            (node_id,),
        ).fetchall()
        events: list[TimelineEvent] = []
        for row in rows:
            version = int(row["version"])
            node = GraphNode.model_validate_json(str(row["payload"]))
            events.append(
                TimelineEvent(
                    when=epoch_from_iso(str(row["changed_at"])),
                    version=version,
                    summary=_summarize_version(node, version),
                )
            )
        return events

    def as_of(self, timestamp: float, filter: NodeFilter) -> list[GraphNode]:
        clauses = ["profile_id = ?", "valid_from <= ?", "(valid_to IS NULL OR valid_to > ?)"]
        params: list[Any] = [filter.profile_id, iso8601_utc(timestamp), iso8601_utc(timestamp)]
        if filter.node_type is not None:
            clauses.append("json_extract(payload, '$.node_type') = ?")
            params.append(filter.node_type.value)
        if filter.min_decay > 0.0:
            clauses.append("json_extract(payload, '$.decay_weight') >= ?")
            params.append(filter.min_decay)
        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT payload, version, valid_from, valid_to FROM node_versions "
            f"WHERE {where} ORDER BY valid_from",
            params,
        ).fetchall()
        return [_decode_version(r) for r in rows]

    # ------------------------------------------------------------ weights / intentions

    def batch_update_weights(self, updates: CSeq[GraphWeightUpdate]) -> None:
        if not updates:
            return
        now = iso8601_utc(time.time())
        with _transaction(self._conn):
            for update in updates:
                self._conn.execute(
                    "UPDATE nodes SET decay_weight = ?, updated_at = ? "
                    "WHERE node_id = ? AND valid_to IS NULL",
                    (update.decay_weight, now, update.node_id),
                )

    def query_intentions(self, status: IntentionStatus, due_before: float) -> list[GraphNode]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE valid_to IS NULL AND node_type = 'INTENTION' "
            "AND json_extract(payload, '$.status') = ? AND valid_from <= ? ORDER BY valid_from",
            (status.value, iso8601_utc(due_before)),
        ).fetchall()
        return [self._decode_node(r) for r in rows]

    # ------------------------------------------------------------ internals

    def _fetch_version(self, version_id: str) -> GraphNode | None:
        node_id, _, version_str = version_id.partition(":")
        if not node_id or not version_str.isdigit():
            return None
        row = self._conn.execute(
            "SELECT payload, version, valid_from, valid_to FROM node_versions "
            "WHERE node_id = ? AND version = ?",
            (node_id, int(version_str)),
        ).fetchone()
        return _decode_version(row) if row is not None else None


# ---------------------------------------------------------------- module helpers


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Explicit BEGIN IMMEDIATE / COMMIT transaction, ROLLBACK on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _maybe_epoch(value: Any) -> float | None:
    return epoch_from_iso(str(value)) if value is not None else None


def _count(conn: sqlite3.Connection, table: str, where: str, params: CSeq[Any]) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", list(params)).fetchone()
    return int(row[0]) if row is not None else 0


def _decode_version(row: sqlite3.Row) -> GraphNode:
    node = GraphNode.model_validate_json(str(row["payload"]))
    # Temporal columns are authoritative for the chain state (supersession /
    # invalidation patch them after the snapshot was serialized).
    valid_from = row["valid_from"]
    valid_to = row["valid_to"]
    if valid_from is not None:
        node.valid_from = epoch_from_iso(str(valid_from))
    if valid_to is not None:
        node.valid_to = epoch_from_iso(str(valid_to))
    node.version = int(row["version"])
    return node


def _summarize_version(node: GraphNode, version: int) -> str:
    hint_keys = ("statement", "summary", "rule", "action", "name", "task_type", "domain")
    hint = next((node.props[key] for key in hint_keys if isinstance(node.props.get(key), str)), "")
    tail = f" {hint}" if hint else ""
    return f"v{version} {node.node_type.value}{tail}"


def _deep_changes(a: dict[str, Any], b: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(set(a) | set(b)):
        field = f"{prefix}{key}"
        if key not in a:
            changes.append({"field": field, "before": None, "after": b[key]})
        elif key not in b:
            changes.append({"field": field, "before": a[key], "after": None})
        elif isinstance(a[key], dict) and isinstance(b[key], dict):
            changes.extend(_deep_changes(a[key], b[key], prefix=f"{field}."))
        elif isinstance(a[key], list) and isinstance(b[key], list) and list(a[key]) != list(b[key]):
            changes.append({"field": field, "before": a[key], "after": b[key]})
        elif not isinstance(a[key], (dict, list)) and a[key] != b[key]:
            changes.append({"field": field, "before": a[key], "after": b[key]})
    return changes
