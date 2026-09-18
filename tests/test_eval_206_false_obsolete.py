from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mnemoseed_local.dream import repair_reconciliation_audit
from mnemoseed_local.dream.adjudicate import Disposition, ReasonCode
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import Provenance, ProvenanceEvent
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import AuditFilter, ReconciliationApplication


def _node(node_id: str, *, profile_id: str = "p", weight: float = 0.8, protected: bool = False) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=profile_id,
        node_type=NodeType.PREFERENCE,
        props={
            "domain": "editor",
            "statement": f"statement-{node_id}",
            "object": "editor",
            "valence": 0.4,
            "prior_width": 0.2,
            "trait_anchor": "stable-anchor",
            "evidence_chain": [{"chunk_id": f"chunk-{node_id}"}],
        },
        entities=["editor"],
        confidence=0.73,
        decay_weight=weight,
        never_decay=protected,
        needs_reconcile=True,
        conflict_flag=True,
        conflict_group="group",
        read_conflict_id=None,
        version=1,
        provenance=Provenance(
            asserted_by="source-model",
            agent_id="source-agent",
            session_id="source-session",
            source=f"chunk://{node_id}",
            confidence=0.61,
            asserted_at=10.0,
            history=[ProvenanceEvent(at=11.0, action="created", actor="source-model")],
        ),
        created_at=12.0,
        updated_at=13.0,
        valid_from=12.0,
    )


def _application(
    nomination_id: str,
    *,
    disposition: Disposition = Disposition.ACCEPTED,
    reason: ReasonCode = ReasonCode.CONFLICT_CONFIRMED,
    **changes: object,
) -> ReconciliationApplication:
    application = ReconciliationApplication(
        nomination_id=nomination_id,
        profile_id="p",
        disposition=disposition,
        reason_code=reason,
        canonical_kind="read_conflict",
        composite_group_id=f"group-{nomination_id}",
        evidence_event_ids=(7, 9),
        verdict="conflict",
        quality="verified",
        left_node_id="a",
        left_version=1,
        left_expected_peer_id="b",
        right_node_id="b",
        right_version=1,
        right_expected_peer_id="a",
        winner_node_id="a" if disposition is Disposition.ACCEPTED else None,
        loser_node_id="b" if disposition is Disposition.ACCEPTED else None,
        loser_prior_version=None,
        loser_new_version=None,
        applied_at=100.0,
    )
    return replace(application, **changes)


def _stores(tmp_path: Path) -> tuple[SqliteGraphDriver, SqliteMetaDriver]:
    return SqliteGraphDriver(tmp_path / "graph.db"), SqliteMetaDriver(tmp_path / "meta.db")


def _seed(
    graph: SqliteGraphDriver,
    left: str = "a",
    right: str = "b",
    *,
    profile_id: str = "p",
    protected: bool = False,
) -> None:
    graph.upsert_node(_node(left, profile_id=profile_id))
    graph.upsert_node(_node(right, profile_id=profile_id, protected=protected))
    graph.set_read_conflict(left, right)
    graph.set_read_conflict(right, left)


@pytest.mark.parametrize("row", range(8))
def test_false_obsolete_corpus_accepts_only_exact_rows_at_frozen_factor(tmp_path: Path, row: int) -> None:
    graph, meta = _stores(tmp_path)
    _seed(graph)
    before = graph.get_node("b")
    assert before is not None
    application = _application(f"accepted-{row}")
    receipt = graph.apply_reconciliation(application, downweight_factor=0.5)
    assert receipt.disposition is Disposition.ACCEPTED
    current = graph.get_node("b")
    assert current is not None
    assert current.decay_weight == pytest.approx(before.decay_weight * 0.5)
    assert current.node_id == before.node_id
    assert current.props == before.props
    assert current.confidence == before.confidence
    assert current.provenance.model_dump(exclude={"history"}) == before.provenance.model_dump(
        exclude={"history"}
    )
    assert current.provenance.history[:-1] == before.provenance.history
    assert len(graph.versions("b")) == 2
    replay = graph.apply_reconciliation(application, downweight_factor=0.5)
    assert replay == receipt
    assert len(graph.versions("b")) == 2
    assert repair_reconciliation_audit(graph, meta, limit=10) == 1
    assert meta.audit_query(AuditFilter(action="reconcile_accepted"), _page()).total == 1


def _page():
    from mnemoseed_local.storage.ports import Page

    return Page(0, 50)


@pytest.mark.parametrize(
    ("name", "changes", "reason", "mutate"),
    [
        (
            "object-level",
            {"winner_node_id": None, "loser_node_id": None},
            ReasonCode.CONFLICT_WITHOUT_DIRECTION,
            None,
        ),
        ("single-side", {"right_expected_peer_id": "c"}, ReasonCode.STALE_REVISION, None),
        (
            "polarity",
            {
                "disposition": Disposition.REJECTED,
                "reason_code": ReasonCode.NOT_CONFLICT_CONFIRMED,
                "verdict": "not_conflict",
                "winner_node_id": None,
                "loser_node_id": None,
            },
            ReasonCode.NOT_CONFLICT_CONFIRMED,
            None,
        ),
        ("stale-version", {"right_version": 2}, ReasonCode.STALE_REVISION, None),
        ("changed-pointer", {}, ReasonCode.STALE_REVISION, "pointer"),
        ("protected", {}, ReasonCode.PROTECTED_ENDPOINT, "protected"),
        ("no-exact-identity", {"profile_id": "other"}, ReasonCode.STALE_REVISION, None),
        (
            "timeout",
            {
                "disposition": Disposition.UNRESOLVED,
                "reason_code": ReasonCode.SEAT_TIMEOUT,
                "verdict": "insufficient",
                "quality": "degraded",
                "winner_node_id": None,
                "loser_node_id": None,
            },
            ReasonCode.SEAT_TIMEOUT,
            None,
        ),
        (
            "invalid-output",
            {
                "disposition": Disposition.UNRESOLVED,
                "reason_code": ReasonCode.INVALID_TYPED_OUTPUT,
                "verdict": "insufficient",
                "quality": "degraded",
                "winner_node_id": None,
                "loser_node_id": None,
            },
            ReasonCode.INVALID_TYPED_OUTPUT,
            None,
        ),
        ("missing-endpoint", {}, ReasonCode.MISSING_ENDPOINT, "missing"),
        ("closed-endpoint", {}, ReasonCode.CLOSED_ENDPOINT, "closed"),
        ("tombstoned-endpoint", {}, ReasonCode.TOMBSTONED_ENDPOINT, "tombstone"),
        ("zero-weight", {}, ReasonCode.DECAY_WEIGHT_NOT_LOWERABLE, "zero"),
        (
            "deferred",
            {
                "disposition": "deferred",
                "reason_code": ReasonCode.INSUFFICIENT,
                "verdict": "insufficient",
                "quality": "degraded",
                "winner_node_id": None,
                "loser_node_id": None,
            },
            ReasonCode.INSUFFICIENT,
            None,
        ),
        (
            "single-side-output",
            {
                "disposition": Disposition.UNRESOLVED,
                "reason_code": ReasonCode.SINGLE_SIDE,
                "verdict": "insufficient",
                "quality": "degraded",
                "winner_node_id": None,
                "loser_node_id": None,
            },
            ReasonCode.SINGLE_SIDE,
            None,
        ),
        (
            "polarity-output",
            {
                "disposition": Disposition.UNRESOLVED,
                "reason_code": ReasonCode.POLARITY_DROP,
                "verdict": "insufficient",
                "quality": "degraded",
                "winner_node_id": None,
                "loser_node_id": None,
            },
            ReasonCode.POLARITY_DROP,
            None,
        ),
    ],
)
def test_false_obsolete_unsafe_shapes_never_downweight(
    tmp_path: Path, name: str, changes: dict[str, object], reason: ReasonCode, mutate: str | None
) -> None:
    graph, _ = _stores(tmp_path)
    _seed(graph, protected=mutate == "protected")
    if mutate == "pointer":
        graph.set_read_conflict("a", "c")
    elif mutate == "missing":
        graph.tombstone("b", deleted_at=50.0)
    elif mutate == "closed":
        graph.invalidate("b", 50.0)
    elif mutate == "tombstone":
        graph.tombstone("b", deleted_at=50.0)
    elif mutate == "zero":
        graph._conn.execute("UPDATE nodes SET decay_weight = 0.0 WHERE node_id = 'b'")
    application = _application(f"unsafe-{name}", **changes)
    if name == "deferred":
        with pytest.raises(ValueError, match="must be accepted, rejected, or unresolved"):
            graph.apply_reconciliation(application, downweight_factor=None)
        assert len(graph.versions("b")) == 1
        return
    factor = 0.5 if str(application.disposition) == "accepted" else None
    receipt = graph.apply_reconciliation(application, downweight_factor=factor)
    assert receipt.disposition is not Disposition.ACCEPTED
    assert receipt.reason_code is not ReasonCode.CONFLICT_CONFIRMED
    assert len(graph.versions("b")) == 1
    current = graph.get_node("b")
    if current is not None:
        expected_weight = 0.0 if mutate == "zero" else 0.8
        assert current.decay_weight == pytest.approx(expected_weight)
