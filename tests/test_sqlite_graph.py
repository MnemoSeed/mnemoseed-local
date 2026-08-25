"""SqliteGraphDriver behavior: CRUD, version chain, flags, co-occurrence,
weights, intentions, and the migration 1->head data-preservation simulation."""

import asyncio
import os
import random
import sqlite3
import time

import pytest

from mnemoseed_local.schema.graph import Edge, GraphNode, NodeType, RelType
from mnemoseed_local.schema.stamp import Provenance
from mnemoseed_local.storage.drivers._migrations import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
)
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.ports import (
    Capability,
    EdgeFilter,
    GraphFlag,
    GraphWeightUpdate,
    IntentionStatus,
    NodeFilter,
    Page,
    StorageError,
)
from mnemoseed_local.storage.registry import GRAPH_DRIVERS, register

_PREF_PROPS: dict = {
    "domain": "coding",
    "statement": "dark mode",
    "valence": 0.8,
    "prior_width": 0.3,
    "trait_anchor": "anima-1",
    "evidence_chain": [{"event": "created", "at": 123.0}],
}


def make_prov(**over) -> Provenance:
    base = dict(asserted_by="test-agent", source="session://s1")
    base.update(over)
    return Provenance(**base)


def make_pref(**over) -> GraphNode:
    base: dict = dict(
        profile_id="p1",
        node_type=NodeType.PREFERENCE,
        entities=["ui"],
        props=dict(_PREF_PROPS),
        provenance=make_prov(),
        valid_from=time.time() - 100.0,
    )
    base.update(over)
    return GraphNode(**base)


def make_anima(**over) -> GraphNode:
    base: dict = dict(
        profile_id="p1",
        node_type=NodeType.ANIMA,
        props={
            "name": "lin",
            "core_traits": [{"dim": "warmth", "mean": 0.5, "width": 0.2}],
            "dye_layer": {"surface": "neutral"},
            "idiographic_notes": "plain-text persona summary",
            "drift_history": [{"at": 1.0, "delta": 0.0}],
        },
        provenance=make_prov(),
    )
    base.update(over)
    return GraphNode(**base)


@pytest.fixture(autouse=True)
def _ensure_registered():
    """Re-register the driver if test_registry's autouse clearing ran."""
    if not GRAPH_DRIVERS.contains("sqlite_graph"):
        register(GRAPH_DRIVERS)(SqliteGraphDriver)
    yield


@pytest.fixture
def driver(tmp_path):
    db = SqliteGraphDriver(path=tmp_path / "graph.db")
    yield db
    asyncio.run(db.close())


def test_registered_in_shared_registry():
    assert GRAPH_DRIVERS.contains("sqlite_graph")


def test_capabilities_full_set():
    caps = SqliteGraphDriver.info.capabilities
    assert Capability.GRAPH_VERSION_CHAIN in caps
    assert Capability.GRAPH_COOCCURRENCE_EDGES in caps
    assert Capability.GRAPH_TRAVERSE_2HOP in caps
    assert Capability.GRAPH_EDGE_LIST in caps
    assert len(caps) == 4


def test_pragmas_wal_and_foreign_keys(tmp_path):
    db = SqliteGraphDriver(path=tmp_path / "pragma.db")
    try:
        assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        asyncio.run(db.close())


def test_upsert_get_roundtrip_preserves_all_a2_fields(driver):
    node = make_pref(
        conflict_flag=True,
        conflict_group="cg-1",
        pending_consolidation=True,
        peripheral_gaps=True,
        needs_reconcile=True,
        hit_count=3,
        last_hit_at=time.time() - 5,
        reinforce_count=2,
        decay_weight=0.7,
        confidence=0.9,
        entities=["ui", "theme"],
    )
    driver.upsert_node(node)
    got = driver.get_node(node.node_id)
    assert got is not None
    assert got.node_type is NodeType.PREFERENCE
    assert got.props["statement"] == "dark mode"
    assert got.entities == ["ui", "theme"]
    assert got.conflict_flag is True
    assert got.conflict_group == "cg-1"
    assert got.pending_consolidation is True
    assert got.peripheral_gaps is True
    assert got.needs_reconcile is True
    assert got.hit_count == 3
    assert got.reinforce_count == 2
    # ISO8601 storage rounds to milliseconds at the DB boundary
    assert abs(got.last_hit_at - node.last_hit_at) < 0.001
    assert abs(got.decay_weight - 0.7) < 1e-9
    assert got.is_current


def test_get_missing_returns_none(driver):
    assert driver.get_node("nope") is None


def test_list_nodes_filter_type_decay_entities_pagination(driver):
    a = make_pref(node_id="a", entities=["ui"], decay_weight=0.9)
    b = make_pref(node_id="b", entities=["typography"], decay_weight=0.3)
    tool = GraphNode(
        node_id="t1",
        profile_id="p1",
        node_type=NodeType.TOOL,
        props={"name": "gh"},
        decay_weight=0.2,
        provenance=make_prov(),
    )
    other_p = make_pref(node_id="c", profile_id="p2")
    for n in (a, b, tool, other_p):
        driver.upsert_node(n)

    all_p1 = driver.list_nodes(NodeFilter(profile_id="p1"), Page(0, 50))
    assert {x.node_id for x in all_p1.items} == {"a", "b", "t1"}
    assert all_p1.total == 3

    filtered = driver.list_nodes(NodeFilter(profile_id="p1", node_type=NodeType.PREFERENCE), Page(0, 50))
    assert {x.node_id for x in filtered.items} == {"a", "b"}

    dashed = driver.list_nodes(NodeFilter(profile_id="p1", min_decay=0.5), Page(0, 50))
    assert {x.node_id for x in dashed.items} == {"a"}

    by_entity = driver.list_nodes(NodeFilter(profile_id="p1", entities=("ui",)), Page(0, 50))
    assert {x.node_id for x in by_entity.items} == {"a"}

    paged = driver.list_nodes(NodeFilter(profile_id="p1"), Page(offset=0, limit=2))
    assert len(paged.items) == 2
    assert paged.total == 3
    assert all(i.valid_to is None for i in all_p1.items)


def test_payload_validation_per_type(driver):
    # missing required preference field
    bad = make_pref(props={"domain": "coding"})
    with pytest.raises(ValueError, match="missing required field"):
        driver.upsert_node(bad)
    # wrong type (int where str expected)
    bad_type = make_pref(props={**_PREF_PROPS, "domain": 7})
    with pytest.raises(ValueError, match="must be str"):
        driver.upsert_node(bad_type)

    anima = make_anima()
    driver.upsert_node(anima)
    assert driver.get_node(anima.node_id).props["name"] == "lin"

    # intention status validated
    good_int = GraphNode(
        profile_id="p1",
        node_type=NodeType.INTENTION,
        props={"trigger_condition": "when cron fires", "action": "send digest", "status": "pending"},
        provenance=make_prov(),
    )
    driver.upsert_node(good_int)
    bad_int = GraphNode(
        profile_id="p1",
        node_type=NodeType.INTENTION,
        props={"trigger_condition": "x", "action": "y", "status": "maybe"},
        provenance=make_prov(),
    )
    with pytest.raises(ValueError, match="status must be one of"):
        driver.upsert_node(bad_int)

    # PROJECT / TOOL are free-form
    project = GraphNode(
        node_id="prj-1",
        profile_id="p1",
        node_type=NodeType.PROJECT,
        props={"anything": "goes"},
        provenance=make_prov(),
    )
    driver.upsert_node(project)
    assert driver.get_node("prj-1").props == {"anything": "goes"}


def test_version_chain_invalidate_append_versions_timeline_as_of(driver):
    v1 = make_pref(node_id="n1", valid_from=time.time() - 200.0)
    driver.upsert_node(v1)
    take_over = time.time()
    v2 = make_pref(
        node_id="n1",
        version=2,
        valid_from=take_over,
        props={**v1.props, "statement": "dark mode at night"},
    )
    driver.invalidate("n1", take_over)
    driver.append_version(v2)

    current = driver.get_node("n1")
    assert current.version == 2
    assert current.props["statement"] == "dark mode at night"

    versions = driver.versions("n1")
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].valid_to is not None  # archived
    assert versions[1].valid_to is None  # live

    tl = driver.timeline("n1")
    assert [e.version for e in tl] == [1, 2]
    assert all(isinstance(e.when, float) for e in tl)

    before = driver.as_of(take_over - 1.0, NodeFilter(profile_id="p1"))
    assert {n.version for n in before} == {1}
    after = driver.as_of(take_over + 1.0, NodeFilter(profile_id="p1"))
    assert {n.version for n in after} == {2}


def test_as_of_without_invalidation_uses_supersession_boundary(driver):
    v1 = make_pref(node_id="n2", valid_from=time.time() - 100.0)
    v2 = make_pref(
        node_id="n2",
        version=2,
        valid_from=time.time(),
        props={**v1.props, "statement": "updated without invalidate"},
    )
    driver.upsert_node(v1)
    driver.append_version(v2)  # no invalidate call — supersession still closes v1
    before = driver.as_of(time.time() - 50.0, NodeFilter(profile_id="p1"))
    assert {n.version for n in before} == {1}
    after = driver.as_of(time.time() + 1.0, NodeFilter(profile_id="p1"))
    assert {n.version for n in after} == {2}


def test_diff_reports_payload_change(driver):
    v1 = make_pref(node_id="n3", valid_from=time.time() - 10.0)
    v2 = make_pref(
        node_id="n3",
        version=2,
        valid_from=time.time(),
        props={**v1.props, "statement": "changed", "valence": 0.9},
    )
    driver.upsert_node(v1)
    driver.append_version(v2)
    result = driver.diff("n3:1", "n3:2")
    assert result["a"]["version"] == 1
    assert result["b"]["version"] == 2
    fields = {c["field"] for c in result["changed"]}
    assert "props.statement" in fields
    assert "props.valence" in fields


def test_diff_unknown_version_raises(driver):
    driver.upsert_node(make_pref(node_id="n4"))
    with pytest.raises(StorageError, match="unknown version"):
        driver.diff("n4:1", "n4:99")


def test_conflict_group_pairing_set_and_clear(driver):
    a = make_pref(node_id="ca")
    b = make_pref(node_id="cb")
    driver.upsert_node(a)
    driver.upsert_node(b)
    driver.set_flags(["ca", "cb"], [GraphFlag.CONFLICT_GROUP])
    ga = driver.get_node("ca")
    gb = driver.get_node("cb")
    assert ga.conflict_flag is True
    assert gb.conflict_flag is True
    assert ga.conflict_group == gb.conflict_group
    assert ga.conflict_group is not None

    # a new member joins the shared group only when passed alongside a member
    c = make_pref(node_id="cc")
    driver.upsert_node(c)
    driver.set_flags(["cc", "ca"], [GraphFlag.CONFLICT_GROUP])
    assert driver.get_node("cc").conflict_group == ga.conflict_group
    assert ga.conflict_group == gb.conflict_group

    driver.clear_flags(["ca", "cb", "cc"], [GraphFlag.CONFLICT_GROUP])
    assert driver.get_node("ca").conflict_flag is False
    assert driver.get_node("ca").conflict_group is None
    assert driver.get_node("cb").conflict_group is None


def test_flag_set_clear_roundtrip(driver):
    n = make_pref(node_id="fl")
    driver.upsert_node(n)
    driver.set_flags(
        ["fl"],
        [GraphFlag.NEEDS_RECONCILE, GraphFlag.PENDING_CONSOLIDATION, GraphFlag.PERIPHERAL_GAPS],
    )
    got = driver.get_node("fl")
    assert got.needs_reconcile and got.pending_consolidation and got.peripheral_gaps
    driver.clear_flags(
        ["fl"],
        [GraphFlag.NEEDS_RECONCILE, GraphFlag.PENDING_CONSOLIDATION, GraphFlag.PERIPHERAL_GAPS],
    )
    got = driver.get_node("fl")
    assert not got.needs_reconcile and not got.pending_consolidation and not got.peripheral_gaps


def test_bump_cooccurrence_is_symmetric(driver):
    driver.bump_cooccurrence("node-a", "node-b", "p1")
    driver.bump_cooccurrence("node-b", "node-a", "p1")
    driver.bump_cooccurrence("a", "b", "p1")
    rows = driver._conn.execute("SELECT weight FROM edges WHERE rel = 'co_occurred'").fetchall()
    assert len(rows) == 2
    assert sorted(float(r["weight"]) for r in rows) == [1.0, 2.0]


def test_add_edge_and_traverse_with_profile_filter(driver):
    hub = make_pref(node_id="hub", entities=["h"])
    nbr1 = make_pref(node_id="leaf1", entities=["l1"])
    nbr2 = make_pref(node_id="leaf2", entities=["l2"])
    other = make_pref(node_id="other", profile_id="p2", entities=["o"])
    driver.upsert_node(hub)
    driver.upsert_node(nbr1)
    driver.upsert_node(nbr2)
    driver.upsert_node(other)
    driver.add_edge(Edge(src="hub", dst="leaf1", rel=RelType.EVIDENCED_BY, profile_id="p1"))
    driver.add_edge(Edge(src="leaf2", dst="hub", rel=RelType.EVIDENCED_BY, profile_id="p1"))
    driver.add_edge(Edge(src="other", dst="hub", rel=RelType.EVIDENCED_BY, profile_id="p2"))

    reached = driver.traverse("hub", depth=1, filter=NodeFilter(profile_id="p1"))
    assert {n.node_id for n in reached} == {"hub", "leaf1", "leaf2"}

    # unfiltered traversal escapes the profile
    reached_all = driver.traverse("hub", depth=1)
    assert "other" in {n.node_id for n in reached_all}


def test_transverse_depth_cap_is_two(driver):
    driver.upsert_node(make_pref(node_id="e0"))
    driver.upsert_node(make_pref(node_id="e1"))
    driver.upsert_node(make_pref(node_id="e2"))
    driver.add_edge(Edge(src="e0", dst="e1", rel=RelType.EVIDENCED_BY, profile_id="p1"))
    driver.add_edge(Edge(src="e1", dst="e2", rel=RelType.EVIDENCED_BY, profile_id="p1"))
    reached = driver.traverse("e0", depth=99, filter=NodeFilter(profile_id="p1"))
    assert {n.node_id for n in reached} == {"e0", "e1", "e2"}


def test_find_same_predicate(driver):
    a = make_pref(
        node_id="fp1",
        props={**_PREF_PROPS, "subject": "user", "predicate": "indent", "value": "spaces"},
    )
    b = make_pref(
        node_id="fp2",
        props={**_PREF_PROPS, "subject": "user", "predicate": "indent", "value": "tabs"},
    )
    unrelated = make_pref(node_id="fp3", props={**_PREF_PROPS, "subject": "user", "predicate": "style"})
    driver.upsert_node(a)
    driver.upsert_node(b)
    driver.upsert_node(unrelated)
    found = {n.node_id for n in driver.find_same_predicate("user", "indent", "p1")}
    assert found == {"fp1", "fp2"}


def test_batch_update_weights_in_one_call(driver):
    a = make_pref(node_id="w1")
    b = make_pref(node_id="w2")
    driver.upsert_node(a)
    driver.upsert_node(b)
    driver.batch_update_weights(
        [GraphWeightUpdate(node_id="w1", decay_weight=0.4), GraphWeightUpdate(node_id="w2", decay_weight=0.9)]
    )
    assert abs(driver.get_node("w1").decay_weight - 0.4) < 1e-9
    assert abs(driver.get_node("w2").decay_weight - 0.9) < 1e-9


def test_query_intentions_status_and_due(driver):
    due = GraphNode(
        node_id="i1",
        profile_id="p1",
        node_type=NodeType.INTENTION,
        props={"trigger_condition": "when", "action": "act", "status": "pending"},
        valid_from=time.time() - 50.0,
        provenance=make_prov(),
    )
    later = GraphNode(
        node_id="i2",
        profile_id="p1",
        node_type=NodeType.INTENTION,
        props={"trigger_condition": "when", "action": "act", "status": "pending"},
        valid_from=time.time() + 500.0,
        provenance=make_prov(),
    )
    fired = GraphNode(
        node_id="i3",
        profile_id="p1",
        node_type=NodeType.INTENTION,
        props={"trigger_condition": "when", "action": "act", "status": "fired"},
        provenance=make_prov(),
    )
    driver.upsert_node(due)
    driver.upsert_node(later)
    driver.upsert_node(fired)
    hits = driver.query_intentions(IntentionStatus.PENDING, time.time())
    assert {n.node_id for n in hits} == {"i1"}
    hits_fired = driver.query_intentions(IntentionStatus.FIRED, time.time() + 99999.0)
    assert {n.node_id for n in hits_fired} == {"i3"}


def _insert_node_row_at_v1(conn, driver_scratch, node) -> None:
    # The scratch driver is at head (schema v5); the target is a v1 file whose
    # nodes table predates the delta columns, so the head-added columns
    # (promotion_status v5; pinned v2, already absent from _NODE_COLUMNS) are
    # excluded and backfilled by the migration's NOT NULL DEFAULTs instead.
    row = driver_scratch._node_row(node)
    dropped = {"promotion_status"}
    columns = [c for c in driver_scratch._NODE_COLUMNS if c not in dropped]
    values = [v for c, v in zip(driver_scratch._NODE_COLUMNS, row, strict=True) if c not in dropped]
    placeholders = ", ".join(["?"] * len(columns))
    conn.execute(f"INSERT INTO nodes ({', '.join(columns)}) VALUES ({placeholders})", values)


def test_migration_1_to_head_data_preserved(tmp_path):
    """AC-5 SQLite half: data written under v1 survives the 1->head upgrade."""
    path = tmp_path / "migrate.db"
    conn = sqlite3.connect(path, isolation_level=None)
    apply_migrations(conn, "graph", target=1)
    assert current_schema_version(conn, "graph") == 1
    assert "pinned" not in _column_names(conn, "nodes")
    assert "promotion_status" not in _column_names(conn, "nodes")

    node = make_pref(
        node_id="survivor",
        props={**_PREF_PROPS, "statement": "keep me"},
    )
    scratch = SqliteGraphDriver(path=tmp_path / "scratch.db")
    try:
        _insert_node_row_at_v1(conn, scratch, node)
        conn.execute(
            "INSERT INTO node_versions (node_id, version, profile_id, valid_from, valid_to, "
            "superseded_by, changed_at, payload) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                node.node_id,
                1,
                "p1",
                "2026-01-01T00:00:00.000Z",
                None,
                "2026-01-01T00:00:00.000Z",
                node.model_dump_json(),
            ),
        )
        conn.commit()
    finally:
        asyncio.run(scratch.close())

    # a driver opening the v1 file auto-migrates it to head and preserves rows
    driver = SqliteGraphDriver(path=path)
    try:
        assert current_schema_version(driver._conn, "graph") == 5
        assert "pinned" in _column_names(driver._conn, "nodes")
        assert "promotion_status" in _column_names(driver._conn, "nodes")
        got = driver.get_node("survivor")
        assert got is not None
        assert got.props["statement"] == "keep me"
        assert got.profile_id == "p1"
        # v5 back-compat: the migrated row reads back as promoted
        assert got.promotion_status.value == "promoted"
    finally:
        asyncio.run(driver.close())


def test_same_version_reupsert_does_not_duplicate(driver):
    """INSERT OR REPLACE on node_versions must keep version-chain identity."""
    node = make_pref(node_id="dup")
    driver.upsert_node(node)
    driver.upsert_node(node)  # same node_id, same version -> replace, not append
    count = driver._conn.execute(
        "SELECT COUNT(*) FROM node_versions WHERE node_id = ?", (node.node_id,)
    ).fetchone()[0]
    assert count == 1
    versions = driver.versions(node.node_id)
    assert [v.version for v in versions] == [1]


def test_append_version_with_invalidate_at_is_one_atomic_replacement(driver):
    v1 = make_pref(node_id="r2", valid_from=time.time() - 100.0)
    driver.upsert_node(v1)
    close_at = time.time()
    v2 = make_pref(
        node_id="r2",
        version=2,
        valid_from=close_at + 1.0,
        props={**v1.props, "statement": "replacement"},
    )
    driver.append_version(v2, invalidate_at=close_at)

    versions = driver.versions("r2")
    assert [v.version for v in versions] == [1, 2]
    assert abs(versions[0].valid_to - close_at) < 0.001
    assert versions[1].valid_to is None
    linked = driver._conn.execute(
        "SELECT superseded_by FROM node_versions WHERE node_id = 'r2' AND version = 1"
    ).fetchone()
    assert int(linked["superseded_by"]) == 2
    # bi-temporal view: v1 only before close_at, v2 only after
    before = driver.as_of(close_at - 1.0, NodeFilter(profile_id="p1"))
    assert {n.version for n in before} == {1}
    after = driver.as_of(close_at + 2.0, NodeFilter(profile_id="p1"))
    assert {n.version for n in after} == {2}


def test_append_version_invalidate_pair_rolls_back_atomically(driver):
    """Crash-safety: the B.2 pair either fully applies or fully does not."""
    v1 = make_pref(node_id="r1", valid_from=time.time() - 100.0)
    driver.upsert_node(v1)
    bad = make_pref(node_id="r1", version=2, props={"domain": "coding"})  # invalid payload
    with pytest.raises(ValueError, match="missing required field"):
        driver.append_version(bad, invalidate_at=time.time())
    # neither the invalidation nor the append survived the rollback
    current = driver.get_node("r1")
    assert current is not None
    assert current.version == 1
    assert current.valid_to is None
    versions = driver.versions("r1")
    assert len(versions) == 1
    assert versions[0].valid_to is None


def test_graph_file_contains_only_graph_tables(tmp_path):
    conn = sqlite3.connect(tmp_path / "g.db", isolation_level=None)
    try:
        apply_migrations(conn, "graph")
        tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == {"schema_version", "nodes", "node_versions", "edges"}
        assert current_schema_version(conn, "graph") == 5  # v2/v5 are graph-tagged
    finally:
        conn.close()


def test_meta_file_contains_only_meta_tables(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db", isolation_level=None)
    try:
        apply_migrations(conn, "meta")
        tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == {
            "schema_version",
            "profiles",
            "tokens",
            "users",
            "score_pool",
            "profile_score_pool",
            "config",
            "audit_log",
            "dream_runs",
            "dream_token_ledger",
        }
        # v2/v5 are graph-only; v3/v4/v6/v7/v8/v9 are meta (identity chain lands
        # in v6, the profile archive flag in v7, the reserved config.scope in
        # v8, the pool filed-points ledger in v9)
        assert current_schema_version(conn, "meta") == 9
    finally:
        conn.close()


def test_migration_sequence_is_shared_and_forward_only():
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    stores = {op.store for m in MIGRATIONS for op in m.ops}
    assert stores == {"graph", "meta"}
    # every store-region can reach the tail of the shared sequence independently
    assert any(m.applies_to("graph") for m in MIGRATIONS)
    assert any(m.applies_to("meta") for m in MIGRATIONS)


def _column_names(conn, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------- list_edges (B.2 v1.1)


def test_list_edges_orders_by_created_desc_id_asc(driver):
    """Stable order contract: created_at desc, edge id asc tie-breaker."""
    for node_id in ("e-a", "e-b", "e-c", "e-d"):
        driver.upsert_node(make_pref(node_id=node_id))
    now = time.time()
    driver.add_edge(
        Edge(src="e-a", dst="e-b", rel=RelType.EVIDENCED_BY, profile_id="p1", created_at=now - 10)
    )
    driver.add_edge(Edge(src="e-b", dst="e-c", rel=RelType.EVIDENCED_BY, profile_id="p1", created_at=now - 5))
    driver.add_edge(Edge(src="e-c", dst="e-d", rel=RelType.EVIDENCED_BY, profile_id="p1", created_at=now - 5))
    page = driver.list_edges(EdgeFilter(profile_id="p1"), Page(limit=10))
    assert page.total == 3
    newest = page.items[0]
    # the two same-time edges fall back to the id tie-breaker: strictly ordered
    assert [e.created_at for e in page.items] == sorted((e.created_at for e in page.items), reverse=True)
    assert newest.src in {"e-b", "e-c"}
    assert newest.dst in {"e-c", "e-d"}
    assert newest.created_at == pytest.approx(now - 5, abs=0.002)


@pytest.mark.skipif(
    not os.environ.get("MNEMOSEED_GRAPH_PERF"),
    reason="set MNEMOSEED_GRAPH_PERF=1 for the list_edges page-scale benchmark "
    "(seeds ~5k nodes / ~19k edges; NFR-7.2 first-paint target)",
)
def test_list_edges_5k_graph_page_returns_at_design_scale(driver):
    """NFR-7.2 design-scale page read: on a ~5k-node / ~19k-edge graph a single
    paginated page must return far inside the first-paint budget (generous CI
    bound; the real cost is a profile-indexed range scan, not a table scan)."""
    rng = random.Random(7)
    now = time.time()
    node_ids: list[str] = []
    for index in range(5_000):
        node_id = f"perf-{index:05d}"
        node_ids.append(node_id)
        driver.upsert_node(
            make_pref(
                node_id=node_id,
                profile_id="p1",
                cognitive_tier=rng.choice((1, 1, 2, 3)),
                decay_weight=round(rng.uniform(0.05, 1.0), 3),
                created_at=now - rng.uniform(0.0, 90 * 24 * 3600.0),
            )
        )
    pairs: set[tuple[str, str]] = set()
    while len(pairs) < 19_000:
        a = rng.choice(node_ids)
        b = rng.choice(node_ids)
        if a == b:
            continue
        pairs.add(tuple(sorted((a, b))))
    for a, b in pairs:
        driver.add_edge(
            Edge(
                src=a,
                dst=b,
                rel=rng.choice((RelType.EVIDENCED_BY, RelType.CO_OCCURRED)),
                profile_id="p1",
                weight=round(rng.uniform(0.1, 1.0), 3),
                created_at=now - rng.uniform(0.0, 90 * 24 * 3600.0),
            )
        )
    started = time.perf_counter()
    page = driver.list_edges(EdgeFilter(profile_id="p1"), Page(offset=0, limit=500))
    elapsed = time.perf_counter() - started
    filtered = driver.list_edges(
        EdgeFilter(profile_id="p1", node_types=(NodeType.PREFERENCE,), min_weight=0.5),
        Page(offset=0, limit=500),
    )
    filtered_elapsed = time.perf_counter() - started - elapsed
    assert page.total == 19_000
    assert len(page.items) == 500
    print(
        f"[list_edges@5k] page500={elapsed * 1000:.1f}ms "
        f"filtered={filtered_elapsed * 1000:.1f}ms total={filtered.total}"
    )
    assert elapsed < 2.0, f"bulk edge page on a 5k graph took {elapsed:.2f}s"
