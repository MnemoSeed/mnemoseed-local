"""Driver-agnostic contract tests for the GraphStore port (prd-08 appendix B.2).

Every method of the graph port gets at least one behavioral test, run against
the embedded (sqlite_graph) driver family. Assertions are behavioral so a
future driver that honours the same semantics passes unchanged.
"""

from __future__ import annotations

import time

import pytest
from _support import PROFILE, make_edge, make_intention, make_pref, make_prov
from pydantic import ValidationError

from mnemoseed_local.schema.graph import GraphNode, NodeType, PromotionStatus, RelType
from mnemoseed_local.storage.drivers._time import epoch_from_iso, iso8601_utc
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.ports import (
    Capability,
    EdgeFilter,
    EdgeKind,
    GraphFlag,
    GraphWeightUpdate,
    IntentionStatus,
    NodeFilter,
    Page,
    StorageError,
)

# ---------------------------------------------------------------- B.2 surface


def test_capabilities(stack) -> None:
    expected = frozenset(
        {
            Capability.GRAPH_TRAVERSE_2HOP,
            Capability.GRAPH_VERSION_CHAIN,
            Capability.GRAPH_COOCCURRENCE_EDGES,
            Capability.GRAPH_EDGE_LIST,
        }
    )
    assert stack.graph.capabilities() == stack.graph.info.capabilities == expected


def test_upsert_get_roundtrip(stack) -> None:
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
    stack.graph.upsert_node(node)
    got = stack.graph.get_node(node.node_id)
    assert got is not None
    assert got.node_type is node.node_type
    assert got.props["statement"] == "dark mode"
    assert got.entities == ["ui", "theme"]
    assert got.conflict_flag is True
    assert got.conflict_group == "cg-1"
    assert got.pending_consolidation is True
    assert got.peripheral_gaps is True
    assert got.needs_reconcile is True
    assert got.hit_count == 3
    assert got.reinforce_count == 2
    assert got.last_hit_at == pytest.approx(node.last_hit_at, abs=0.002)
    assert got.decay_weight == pytest.approx(0.7, abs=1e-9)
    assert got.confidence == pytest.approx(0.9, abs=1e-9)
    assert got.is_current
    assert stack.graph.get_node("missing") is None


# ------------------------------------------------- promotion_status (schema v5)


def test_promotion_status_defaults_to_promoted(stack) -> None:
    """v5 back-compat: an un-gated write is promoted without the caller saying so."""
    node = make_pref(node_id="ps-default")
    stack.graph.upsert_node(node)
    assert stack.graph.get_node("ps-default").promotion_status is PromotionStatus.PROMOTED


def test_promotion_status_roundtrips(stack) -> None:
    """The carrier field survives every read surface: current reads, traversal,
    the version chain, and as_of replay (gate filtering is a later task)."""
    v1 = make_pref(
        node_id="ps-rt",
        valid_from=time.time() - 200.0,
        promotion_status=PromotionStatus.QUARANTINED,
    )
    stack.graph.upsert_node(v1)
    assert stack.graph.get_node("ps-rt").promotion_status is PromotionStatus.QUARANTINED

    leaf = make_pref(node_id="ps-leaf", promotion_status=PromotionStatus.PENDING)
    stack.graph.upsert_node(leaf)
    stack.graph.add_edge(make_edge("ps-rt", "ps-leaf"))
    reached = stack.graph.traverse("ps-rt", depth=1, filter=NodeFilter(profile_id=PROFILE))
    statuses = {n.node_id: n.promotion_status for n in reached}
    assert statuses["ps-rt"] is PromotionStatus.QUARANTINED
    assert statuses["ps-leaf"] is PromotionStatus.PENDING

    listed = {
        n.node_id: n.promotion_status
        for n in stack.graph.list_nodes(NodeFilter(profile_id=PROFILE), Page(0, 50)).items
    }
    assert listed["ps-rt"] is PromotionStatus.QUARANTINED

    take_over = time.time()
    v2 = make_pref(
        node_id="ps-rt",
        version=2,
        valid_from=take_over,
        promotion_status=PromotionStatus.SCRAPPED,
        props={**v1.props, "statement": "rolled back"},
    )
    stack.graph.invalidate("ps-rt", take_over)
    stack.graph.append_version(v2)
    versions = stack.graph.versions("ps-rt")
    assert [v.promotion_status for v in versions] == [
        PromotionStatus.QUARANTINED,
        PromotionStatus.SCRAPPED,
    ]

    by_status = {
        n.node_id: n.promotion_status
        for n in stack.graph.as_of(take_over + 1.0, NodeFilter(profile_id=PROFILE))
    }
    assert by_status["ps-rt"] is PromotionStatus.SCRAPPED


def test_invalid_promotion_status_rejected(stack) -> None:
    """Free text never lands in the column: the enum is the write boundary."""
    with pytest.raises(ValidationError, match="promotion_status"):
        make_pref(promotion_status="bogus")


def test_list_nodes_filter_pagination(stack) -> None:
    now = time.time()
    a = make_pref(node_id="n-a", entities=["ui"], decay_weight=0.9, updated_at=now - 30.0)
    b = make_pref(node_id="n-b", entities=["typography"], decay_weight=0.3, updated_at=now - 20.0)
    tool = GraphNode(
        node_id="n-t",
        profile_id=PROFILE,
        node_type=NodeType.TOOL,
        props={"name": "gh"},
        provenance=make_prov(),
        decay_weight=0.2,
        updated_at=now - 10.0,
    )
    other_p = make_pref(node_id="n-c", profile_id="p2", updated_at=now - 5.0)
    for node in (a, b, tool, other_p):
        stack.graph.upsert_node(node)

    all_p1 = stack.graph.list_nodes(NodeFilter(profile_id=PROFILE), Page(0, 50))
    assert {n.node_id for n in all_p1.items} == {"n-a", "n-b", "n-t"}
    assert all_p1.total == 3

    by_type = stack.graph.list_nodes(
        NodeFilter(profile_id=PROFILE, node_type=NodeType.PREFERENCE), Page(0, 50)
    )
    assert {n.node_id for n in by_type.items} == {"n-a", "n-b"}

    by_decay = stack.graph.list_nodes(NodeFilter(profile_id=PROFILE, min_decay=0.5), Page(0, 50))
    assert {n.node_id for n in by_decay.items} == {"n-a"}

    by_entity = stack.graph.list_nodes(NodeFilter(profile_id=PROFILE, entities=("ui",)), Page(0, 50))
    assert {n.node_id for n in by_entity.items} == {"n-a"}

    first = stack.graph.list_nodes(NodeFilter(profile_id=PROFILE), Page(offset=0, limit=2))
    assert len(first.items) == 2
    assert first.total == 3


# ---------------------------------------------------------------- edges / traversal


def test_add_edge_weight_overwrite(stack) -> None:
    a = make_pref(node_id="e-a")
    b = make_pref(node_id="e-b")
    stack.graph.upsert_node(a)
    stack.graph.upsert_node(b)
    stack.graph.add_edge(make_edge("e-a", "e-b", rel=RelType.EVIDENCED_BY))
    stack.graph.add_edge(make_edge("e-a", "e-b", rel=RelType.EVIDENCED_BY))  # same key: overwrite, not dup
    reached = stack.graph.traverse("e-a", depth=1, filter=NodeFilter(profile_id=PROFILE))
    assert {n.node_id for n in reached} == {"e-a", "e-b"}


def test_bump_cooccurrence_symmetric_and_increments(stack) -> None:
    for node_id in ("node-a", "node-b", "other"):
        stack.graph.upsert_node(make_pref(node_id=node_id))
    stack.graph.bump_cooccurrence("node-a", "node-b", PROFILE)
    stack.graph.bump_cooccurrence("node-b", "node-a", PROFILE)
    stack.graph.bump_cooccurrence("node-a", "node-b", PROFILE)
    stack.graph.bump_cooccurrence("other", "node-a", PROFILE)
    reached = stack.graph.traverse("node-a", depth=1, filter=NodeFilter(profile_id=PROFILE))
    assert {n.node_id for n in reached} == {"node-a", "node-b", "other"}


def test_list_edges_kinds_filters_and_stable_pagination(stack) -> None:
    """prd-08 appendix B.2 v1.1: the bulk edge listing contract.

    Every item carries edge id / endpoints / kind (relation | cooccurrence) /
    weight / timestamp; filters cover profile isolation, endpoint node types,
    the created time window, cognitive tier and a min-weight floor; pages use a
    stable order with honest totals.
    """
    now = time.time()
    for node_id, ntype, tier in (
        ("le-a", NodeType.PREFERENCE, 1),
        ("le-b", NodeType.PREFERENCE, 1),
        ("le-c", NodeType.PREFERENCE, 2),
        ("le-tool", NodeType.TOOL, 2),
    ):
        props = {"name": "gh"} if ntype is NodeType.TOOL else dict(_support_props())
        stack.graph.upsert_node(
            GraphNode(
                node_id=node_id,
                profile_id=PROFILE,
                node_type=ntype,
                props=props,
                cognitive_tier=tier,
                provenance=make_prov(),
            )
        )
    stack.graph.upsert_node(
        GraphNode(
            node_id="le-foreign",
            profile_id="p2",
            node_type=NodeType.PREFERENCE,
            props=dict(_support_props()),
            cognitive_tier=1,
            provenance=make_prov(),
        )
    )
    # created order: e1 oldest (now-300), then e3, e4, e5, and the
    # cooccurrence bump lands newest (~now).
    stack.graph.add_edge(
        make_edge("le-a", "le-b", rel=RelType.EVIDENCED_BY, weight=0.8, created_at=now - 300.0)
    )
    stack.graph.add_edge(
        make_edge("le-tool", "le-b", rel=RelType.CONTAINS, weight=0.4, created_at=now - 100.0)
    )
    stack.graph.add_edge(make_edge("le-c", "le-a", rel=RelType.HAS, weight=0.6, created_at=now - 50.0))
    stack.graph.add_edge(
        make_edge("le-tool", "le-c", rel=RelType.EVIDENCED_BY, weight=0.9, created_at=now - 25.0)
    )
    stack.graph.bump_cooccurrence("le-a", "le-tool", PROFILE)  # weight 1.0, ~now
    stack.graph.add_edge(make_edge("le-foreign", "le-a", profile_id="p2", created_at=now - 50.0))

    edge_filter = EdgeFilter(profile_id=PROFILE)
    all_edges = stack.graph.list_edges(edge_filter, Page(0, 50))
    assert all_edges.total == 5
    assert [e.created_at for e in all_edges.items] == sorted(
        (e.created_at for e in all_edges.items), reverse=True
    )
    by_id = {e.edge_id: e for e in all_edges.items}
    assert len(by_id) == 5  # every edge id is unique and non-empty
    kinds = {frozenset((e.src, e.dst)): e.kind for e in all_edges.items}
    assert kinds[frozenset(("le-a", "le-b"))] is EdgeKind.RELATION
    assert kinds[frozenset(("le-a", "le-tool"))] is EdgeKind.COOCCURRENCE
    assert kinds[frozenset(("le-tool", "le-b"))] is EdgeKind.RELATION
    cooc = next(e for e in all_edges.items if e.kind is EdgeKind.COOCCURRENCE)
    assert cooc.weight == pytest.approx(1.0)
    assert cooc.created_at >= now - 2.0

    by_node_type = stack.graph.list_edges(
        EdgeFilter(profile_id=PROFILE, node_types=(NodeType.PREFERENCE,)), Page(0, 50)
    )
    assert {frozenset((e.src, e.dst)) for e in by_node_type.items} == {
        frozenset(("le-a", "le-b")),
        frozenset(("le-c", "le-a")),
    }
    both_types = stack.graph.list_edges(
        EdgeFilter(profile_id=PROFILE, node_types=(NodeType.PREFERENCE, NodeType.TOOL)), Page(0, 50)
    )
    assert both_types.total == 5

    by_tier = stack.graph.list_edges(EdgeFilter(profile_id=PROFILE, tier=1), Page(0, 50))
    assert {frozenset((e.src, e.dst)) for e in by_tier.items} == {frozenset(("le-a", "le-b"))}
    tier_two = stack.graph.list_edges(EdgeFilter(profile_id=PROFILE, tier=2), Page(0, 50))
    assert {frozenset((e.src, e.dst)) for e in tier_two.items} == {frozenset(("le-tool", "le-c"))}

    by_weight = stack.graph.list_edges(EdgeFilter(profile_id=PROFILE, min_weight=0.7), Page(0, 50))
    assert {frozenset((e.src, e.dst)) for e in by_weight.items} == {
        frozenset(("le-a", "le-b")),
        frozenset(("le-a", "le-tool")),
        frozenset(("le-tool", "le-c")),
    }

    window = stack.graph.list_edges(EdgeFilter(profile_id=PROFILE, created_after=now - 200.0), Page(0, 50))
    assert {frozenset((e.src, e.dst)) for e in window.items} == {
        frozenset(("le-tool", "le-b")),
        frozenset(("le-c", "le-a")),
        frozenset(("le-tool", "le-c")),
        frozenset(("le-a", "le-tool")),
    }
    bounded = stack.graph.list_edges(EdgeFilter(profile_id=PROFILE, created_before=now - 200.0), Page(0, 50))
    assert {frozenset((e.src, e.dst)) for e in bounded.items} == {frozenset(("le-a", "le-b"))}

    first = stack.graph.list_edges(edge_filter, Page(offset=0, limit=2))
    second = stack.graph.list_edges(edge_filter, Page(offset=2, limit=2))
    third = stack.graph.list_edges(edge_filter, Page(offset=4, limit=2))
    beyond = stack.graph.list_edges(edge_filter, Page(offset=10, limit=2))
    assert len(first.items) == 2 and len(second.items) == 2 and len(third.items) == 1
    assert beyond.items == []
    assert first.total == second.total == third.total == beyond.total == 5
    seen = {e.edge_id for e in [*first.items, *second.items, *third.items, *beyond.items]}
    assert seen == {e.edge_id for e in all_edges.items}
    assert first.items[0].created_at >= first.items[1].created_at  # stable, newest first


def _support_props() -> dict:
    """The frozen PREFERENCE payload the driver validates on write."""
    return {
        "domain": "coding",
        "statement": "dark mode",
        "valence": 0.8,
        "prior_width": 0.3,
        "trait_anchor": "anima-1",
        "evidence_chain": [{"event": "created", "at": 123.0}],
    }


def test_list_edges_excludes_edges_with_stale_endpoints(stack) -> None:
    """Both-endpoints-current guard (QA defect 2 + 3): list_edges must never
    leak an edge whose endpoint is a tombstoned or superseded (non-current)
    revision — with type/tier filters AND unfiltered. A mutation removing the
    current-revision endpoint condition must make this test red on both driver
    dialects."""
    now = time.time()
    for node_id, tier in (("lz-a", 1), ("lz-b", 1), ("lz-c", 1), ("lz-d", 1)):
        stack.graph.upsert_node(
            GraphNode(
                node_id=node_id,
                profile_id=PROFILE,
                node_type=NodeType.PREFERENCE,
                props=dict(_support_props()),
                cognitive_tier=tier,
                provenance=make_prov(),
            )
        )
    stack.graph.add_edge(make_edge("lz-a", "lz-b", rel=RelType.HAS, weight=0.8, created_at=now))
    stack.graph.add_edge(make_edge("lz-b", "lz-c", rel=RelType.HAS, weight=0.6, created_at=now))
    stack.graph.add_edge(make_edge("lz-c", "lz-d", rel=RelType.HAS, weight=0.5, created_at=now))

    deleted_at = time.time()
    stack.graph.tombstone("lz-b", deleted_at)  # no current revision remains
    stack.graph.invalidate("lz-c", deleted_at)  # superseded without replacement

    # unfiltered: NO type/tier filter — the stale-endpoint edges must still vanish
    unfiltered = stack.graph.list_edges(EdgeFilter(profile_id=PROFILE), Page(0, 50))
    assert unfiltered.items == []
    assert unfiltered.total == 0

    # filtered by type: both endpoints must be current AND matching
    by_type = stack.graph.list_edges(
        EdgeFilter(profile_id=PROFILE, node_types=(NodeType.PREFERENCE,)), Page(0, 50)
    )
    assert by_type.items == []
    assert by_type.total == 0

    # filtered by tier: the current-revision restriction holds for tier too
    by_tier = stack.graph.list_edges(EdgeFilter(profile_id=PROFILE, tier=1), Page(0, 50))
    assert by_tier.items == []
    assert by_tier.total == 0


def test_traverse_profile_scoped(stack) -> None:
    hub = make_pref(node_id="hub", entities=["h"])
    leaf = make_pref(node_id="leaf", entities=["l"])
    other = make_pref(node_id="other", profile_id="p2", entities=["o"])
    stack.graph.upsert_node(hub)
    stack.graph.upsert_node(leaf)
    stack.graph.upsert_node(other)
    stack.graph.add_edge(make_edge("hub", "leaf"))
    stack.graph.add_edge(make_edge("other", "hub", profile_id="p2"))

    scoped = stack.graph.traverse("hub", depth=1, filter=NodeFilter(profile_id=PROFILE))
    assert {n.node_id for n in scoped} == {"hub", "leaf"}
    unscoped = stack.graph.traverse("hub", depth=1)
    assert "other" in {n.node_id for n in unscoped}


def test_traverse_depth_capped_at_two(stack) -> None:
    for node_id in ("e0", "e1", "e2"):
        stack.graph.upsert_node(make_pref(node_id=node_id))
    stack.graph.add_edge(make_edge("e0", "e1"))
    stack.graph.add_edge(make_edge("e1", "e2"))
    reached = stack.graph.traverse("e0", depth=99, filter=NodeFilter(profile_id=PROFILE))
    assert {n.node_id for n in reached} == {"e0", "e1", "e2"}


def test_find_same_predicate(stack) -> None:
    fp = dict(
        domain="coding",
        statement="dark mode",
        valence=0.8,
        prior_width=0.3,
        trait_anchor="anima-1",
        evidence_chain=[{"event": "created", "at": 123.0}],
    )
    a = make_pref(node_id="fp1", props={**fp, "subject": "user", "predicate": "indent", "value": "spaces"})
    b = make_pref(node_id="fp2", props={**fp, "subject": "user", "predicate": "indent", "value": "tabs"})
    stack.graph.upsert_node(a)
    stack.graph.upsert_node(b)
    found = {n.node_id for n in stack.graph.find_same_predicate("user", "indent", PROFILE)}
    assert found == {"fp1", "fp2"}


# ---------------------------------------------------------------- flags


def test_set_and_clear_flags(stack) -> None:
    node = make_pref(node_id="fl")
    stack.graph.upsert_node(node)
    flags = [
        GraphFlag.NEEDS_RECONCILE,
        GraphFlag.PENDING_CONSOLIDATION,
        GraphFlag.PERIPHERAL_GAPS,
    ]
    stack.graph.set_flags(["fl"], flags)
    got = stack.graph.get_node("fl")
    assert got.needs_reconcile and got.pending_consolidation and got.peripheral_gaps
    stack.graph.clear_flags(["fl"], flags)
    got = stack.graph.get_node("fl")
    assert not got.needs_reconcile and not got.pending_consolidation and not got.peripheral_gaps


def test_conflict_group_pairing_set_and_clear(stack) -> None:
    a = make_pref(node_id="ca")
    b = make_pref(node_id="cb")
    stack.graph.upsert_node(a)
    stack.graph.upsert_node(b)
    stack.graph.set_flags(["ca", "cb"], [GraphFlag.CONFLICT_GROUP])
    ga, gb = stack.graph.get_node("ca"), stack.graph.get_node("cb")
    assert ga.conflict_flag and gb.conflict_flag
    assert ga.conflict_group == gb.conflict_group
    assert ga.conflict_group is not None
    stack.graph.clear_flags(["ca", "cb"], [GraphFlag.CONFLICT_GROUP])
    assert stack.graph.get_node("ca").conflict_group is None
    assert stack.graph.get_node("cb").conflict_group is None


# ---------------------------------------------------------------- version chain


def test_invalidate_closes_current_revision(stack) -> None:
    node = make_pref(node_id="iv")
    stack.graph.upsert_node(node)
    close_at = time.time()
    stack.graph.invalidate("iv", close_at)
    got = stack.graph.get_node("iv")
    assert got is None  # no current revision remains
    archived = stack.graph.versions("iv")
    assert len(archived) == 1
    assert archived[0].valid_to == pytest.approx(close_at, abs=0.002)


def test_supersede_link_closes_and_links_in_one_transaction(stack) -> None:
    """The supersede verb's storage unit: the invalidation and the SUPERSEDES
    edge land together or not at all, from one clock reading."""
    stack.graph.upsert_node(make_pref(node_id="sp-old"))
    stack.graph.upsert_node(make_pref(node_id="sp-new"))
    closed_at = time.time()

    closed = stack.graph.supersede_link("sp-old", "sp-new", profile_id=PROFILE, closed_at=closed_at)

    assert closed is True
    assert stack.graph.get_node("sp-old") is None
    chain = stack.graph.versions("sp-old")
    assert chain[0].valid_to == pytest.approx(closed_at, abs=0.002)
    rows = stack.graph._conn.execute(
        "SELECT src, dst, created_at FROM edges WHERE rel = ? AND profile_id = ?", ("supersedes", PROFILE)
    ).fetchall()
    assert [(str(row["src"]), str(row["dst"])) for row in rows] == [("sp-new", "sp-old")]
    assert all(epoch_from_iso(str(row["created_at"])) == pytest.approx(closed_at, abs=0.002) for row in rows)


def test_supersede_link_without_a_current_revision_writes_nothing(stack) -> None:
    stack.graph.upsert_node(make_pref(node_id="sp-live"))

    closed = stack.graph.supersede_link("sp-missing", "sp-live", profile_id=PROFILE)

    assert closed is False
    assert stack.graph._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0


def test_supersede_link_rechecks_under_the_write_lock(stack, monkeypatch) -> None:
    """A competing close that becomes visible only once the write lock is held
    must count as "nothing to close": the edge is never written and the caller
    is not told a revision was closed."""
    stack.graph.upsert_node(make_pref(node_id="lk-old"))
    stack.graph.upsert_node(make_pref(node_id="lk-new"))
    original_check = SqliteGraphDriver._get_current_version

    def check_closing_once_locked(self, node_id: str):
        version = original_check(self, node_id)
        if node_id == "lk-old" and self._conn.in_transaction and version is not None:
            # stands in for a competing writer whose commit serialized ahead
            # of this transaction's BEGIN IMMEDIATE
            self._conn.execute(
                "UPDATE nodes SET valid_to = ? WHERE node_id = ? AND valid_to IS NULL",
                (iso8601_utc(time.time()), node_id),
            )
            version = None
        return version

    monkeypatch.setattr(SqliteGraphDriver, "_get_current_version", check_closing_once_locked)

    closed = stack.graph.supersede_link("lk-old", "lk-new", profile_id=PROFILE)

    assert closed is False
    assert stack.graph._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0


def test_append_version_supersedes_previous(stack) -> None:
    v1 = make_pref(node_id="av", valid_from=time.time() - 200.0)
    stack.graph.upsert_node(v1)
    take_over = time.time()
    v2 = make_pref(
        node_id="av",
        version=2,
        valid_from=take_over,
        props={**v1.props, "statement": "dark mode at night"},
    )
    stack.graph.invalidate("av", take_over)
    stack.graph.append_version(v2)
    current = stack.graph.get_node("av")
    assert current is not None and current.version == 2
    assert current.props["statement"] == "dark mode at night"


def test_append_version_pair_is_atomic(stack) -> None:
    """invalidate + append_version in one call: a failed write rolls back both."""
    v1 = make_pref(node_id="r1", valid_from=time.time() - 100.0)
    stack.graph.upsert_node(v1)
    bad = make_pref(node_id="r1", version=2, props={"domain": "coding"})  # invalid payload
    with pytest.raises(ValueError, match="missing required field"):
        stack.graph.append_version(bad, invalidate_at=time.time())
    current = stack.graph.get_node("r1")
    assert current is not None
    assert current.version == 1
    assert current.valid_to is None


def test_versions_chain(stack) -> None:
    v1 = make_pref(node_id="vc", valid_from=time.time() - 200.0)
    stack.graph.upsert_node(v1)
    take_over = time.time()
    v2 = make_pref(node_id="vc", version=2, valid_from=take_over, props={**v1.props, "statement": "v2"})
    stack.graph.invalidate("vc", take_over)
    stack.graph.append_version(v2)
    versions = stack.graph.versions("vc")
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].valid_to is not None
    assert versions[1].valid_to is None


def test_diff_reports_payload_change(stack) -> None:
    v1 = make_pref(node_id="df", valid_from=time.time() - 10.0)
    stack.graph.upsert_node(v1)
    v2 = make_pref(
        node_id="df",
        version=2,
        valid_from=time.time(),
        props={**v1.props, "statement": "changed", "valence": 0.9},
    )
    stack.graph.append_version(v2)
    result = stack.graph.diff("df:1", "df:2")
    assert result["a"]["version"] == 1
    assert result["b"]["version"] == 2
    fields = {change["field"] for change in result["changed"]}
    assert "props.statement" in fields
    assert "props.valence" in fields


def test_diff_unknown_version_raises(stack) -> None:
    stack.graph.upsert_node(make_pref(node_id="df-miss"))
    with pytest.raises(StorageError, match="unknown version"):
        stack.graph.diff("df-miss:1", "df-miss:99")


def test_timeline_replays_versions(stack) -> None:
    v1 = make_pref(node_id="tl", valid_from=time.time() - 100.0)
    stack.graph.upsert_node(v1)
    v2 = make_pref(node_id="tl", version=2, valid_from=time.time(), props={**v1.props, "statement": "v2"})
    stack.graph.append_version(v2)
    events = stack.graph.timeline("tl")
    assert [event.version for event in events] == [1, 2]
    assert all(isinstance(event.when, float) for event in events)
    assert all(event.summary for event in events)


def test_as_of_bi_temporal_replay(stack) -> None:
    v1 = make_pref(node_id="ao", valid_from=time.time() - 200.0)
    stack.graph.upsert_node(v1)
    take_over = time.time()
    v2 = make_pref(
        node_id="ao",
        version=2,
        valid_from=take_over,
        props={**v1.props, "statement": "dark mode at night"},
    )
    stack.graph.invalidate("ao", take_over)
    stack.graph.append_version(v2)
    before = stack.graph.as_of(take_over - 1.0, NodeFilter(profile_id=PROFILE))
    assert {n.version for n in before} == {1}
    after = stack.graph.as_of(take_over + 1.0, NodeFilter(profile_id=PROFILE))
    assert {n.version for n in after} == {2}


# ---------------------------------------------------------------- weights / intentions


def test_batch_update_weights(stack) -> None:
    a = make_pref(node_id="w1")
    b = make_pref(node_id="w2")
    stack.graph.upsert_node(a)
    stack.graph.upsert_node(b)
    stack.graph.batch_update_weights(
        [GraphWeightUpdate(node_id="w1", decay_weight=0.4), GraphWeightUpdate(node_id="w2", decay_weight=0.9)]
    )
    assert stack.graph.get_node("w1").decay_weight == pytest.approx(0.4, abs=1e-9)
    assert stack.graph.get_node("w2").decay_weight == pytest.approx(0.9, abs=1e-9)


# ---------------------------------------------------------------- tombstone


def test_tombstone_tombstoned_node_via_port(stack) -> None:
    """`tombstone` (design/03 storage-layer erasure): a deleted node is invisible to reads /
    traversal / future as_of, yet its version chain survives for audit.

    Tombstone semantics are expressible entirely through the existing
    version-chain machinery: close the current revision at ``deleted_at`` and
    append a ``deleted`` provenance event to the archived payload — nothing is
    ever physically dropped (GDPR log-preserve, design/03 3).
    """
    leaf = make_pref(node_id="tm-leaf", entities=["tm"], decay_weight=0.9)
    hub = make_pref(node_id="tm-hub", entities=["tm"], decay_weight=0.9)
    stack.graph.upsert_node(leaf)
    stack.graph.upsert_node(hub)
    stack.graph.add_edge(make_edge("tm-hub", "tm-leaf"))

    deleted_at = time.time()
    assert stack.graph.tombstone("tm-leaf", deleted_at) is True

    # invisible to the current-revision reads and to the future as_of window
    assert stack.graph.get_node("tm-leaf") is None
    current_ids = {
        n.node_id for n in stack.graph.list_nodes(NodeFilter(profile_id=PROFILE), Page(0, 50)).items
    }
    assert current_ids == {"tm-hub"}
    reachable = {
        n.node_id for n in stack.graph.traverse("tm-hub", depth=1, filter=NodeFilter(profile_id=PROFILE))
    }
    assert "tm-leaf" not in reachable
    after = {n.node_id for n in stack.graph.as_of(deleted_at + 1.0, NodeFilter(profile_id=PROFILE))}
    assert "tm-leaf" not in after

    # the historical read still finds the fact as it was before the deletion
    before = {n.node_id for n in stack.graph.as_of(deleted_at - 1.0, NodeFilter(profile_id=PROFILE))}
    assert "tm-leaf" in before

    # the version chain is preserved: one closed revision marking the deletion
    versions = stack.graph.versions("tm-leaf")
    assert len(versions) == 1
    assert versions[0].valid_to == pytest.approx(deleted_at, abs=0.002)
    names = {event.action for event in versions[0].provenance.history}
    assert "deleted" in names

    # deleting an unknown node reports False so the caller can report honestly
    assert stack.graph.tombstone("tm-missing", time.time()) is False


def test_query_intentions_status_and_due(stack) -> None:
    now = time.time()
    due = make_intention(node_id="i1", valid_from=now - 50.0)
    later = make_intention(node_id="i2", valid_from=now + 500.0)
    fired = make_intention({"status": "fired"}, node_id="i3")
    stack.graph.upsert_node(due)
    stack.graph.upsert_node(later)
    stack.graph.upsert_node(fired)

    pending = stack.graph.query_intentions(IntentionStatus.PENDING, now)
    assert {n.node_id for n in pending} == {"i1"}
    fired_hits = stack.graph.query_intentions(IntentionStatus.FIRED, now + 99999.0)
    assert {n.node_id for n in fired_hits} == {"i3"}
