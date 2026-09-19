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

FACTOR = 0.5


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
    left: str = "a",
    right: str = "b",
    profile_id: str = "p",
    evidence: tuple[int, int] = (7, 9),
    winner: str | None = None,
    loser: str | None = None,
    disposition: Disposition = Disposition.ACCEPTED,
    reason: ReasonCode = ReasonCode.CONFLICT_CONFIRMED,
    **changes: object,
) -> ReconciliationApplication:
    application = ReconciliationApplication(
        nomination_id=nomination_id,
        profile_id=profile_id,
        disposition=disposition,
        reason_code=reason,
        canonical_kind="read_conflict",
        composite_group_id=f"group-{nomination_id}",
        evidence_event_ids=evidence,
        verdict="conflict",
        quality="verified",
        left_node_id=left,
        left_version=1,
        left_expected_peer_id=right,
        right_node_id=right,
        right_version=1,
        right_expected_peer_id=left,
        winner_node_id=winner if winner is not None else left,
        loser_node_id=loser if loser is not None else right,
        loser_prior_version=None,
        loser_new_version=None,
        applied_at=100.0,
    )
    return replace(application, **changes)


def _stores(tmp_path: Path) -> tuple[SqliteGraphDriver, SqliteMetaDriver]:
    return SqliteGraphDriver(tmp_path / "graph.db"), SqliteMetaDriver(tmp_path / "meta.db")


def _seed(
    graph: SqliteGraphDriver,
    left: str,
    right: str,
    *,
    profile_id: str = "p",
    protected: bool = False,
) -> None:
    graph.upsert_node(_node(left, profile_id=profile_id))
    graph.upsert_node(_node(right, profile_id=profile_id, protected=protected))
    graph.set_read_conflict(left, right)
    graph.set_read_conflict(right, left)


ACCEPTED_ROWS = (
    ("pair-ab-forward", "a1", "b1", "a1", "b1", (11, 12)),
    ("pair-cd-reverse", "c2", "d2", "d2", "c2", (21, 22)),
    ("pair-ef-forward", "e3", "f3", "e3", "f3", (31, 34)),
    ("pair-gh-reverse", "g4", "h4", "h4", "g4", (41, 47)),
    ("pair-ij-forward", "i5", "j5", "i5", "j5", (51, 59)),
    ("pair-kl-reverse", "k6", "l6", "l6", "k6", (61, 68)),
    ("pair-mn-forward", "m7", "n7", "m7", "n7", (71, 79)),
    ("pair-op-reverse", "o8", "p8", "p8", "o8", (81, 89)),
)

UNSAFE_ROWS = (
    (
        "object-level",
        "u01",
        {"winner_node_id": None, "loser_node_id": None},
        ReasonCode.CONFLICT_WITHOUT_DIRECTION,
        None,
    ),
    ("single-side", "u02", {"right_expected_peer_id": "other"}, ReasonCode.STALE_REVISION, None),
    (
        "polarity",
        "u03",
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
    ("stale-version", "u04", {"right_version": 2}, ReasonCode.STALE_REVISION, None),
    ("changed-pointer", "u05", {}, ReasonCode.STALE_REVISION, "pointer"),
    ("protected", "u06", {}, ReasonCode.PROTECTED_ENDPOINT, "protected"),
    ("no-exact-identity", "u07", {"profile_id": "other"}, ReasonCode.STALE_REVISION, None),
    (
        "timeout",
        "u08",
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
        "u09",
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
    ("missing-endpoint", "u10", {}, ReasonCode.MISSING_ENDPOINT, "missing"),
    ("closed-endpoint", "u11", {}, ReasonCode.CLOSED_ENDPOINT, "closed"),
    ("tombstoned-endpoint", "u12", {}, ReasonCode.TOMBSTONED_ENDPOINT, "tombstone"),
    ("zero-weight", "u13", {}, ReasonCode.DECAY_WEIGHT_NOT_LOWERABLE, "zero"),
    (
        "deferred",
        "u14",
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
        "u15",
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
        "u16",
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
)

EXPECTED_DISPOSITIONS_AND_FACTORS = {
    **{row[0]: (Disposition.ACCEPTED, 0.5) for row in ACCEPTED_ROWS},
    **{
        row[0]: (
            row[2].get("disposition", Disposition.UNRESOLVED)
            if row[2].get("disposition") is not None
            else Disposition.UNRESOLVED,
            FACTOR if row[2].get("disposition", Disposition.ACCEPTED) == Disposition.ACCEPTED else None,
        )
        for row in UNSAFE_ROWS
        if row[0] != "deferred"
    },
    "deferred": (None, None),
}


def test_false_obsolete_corpus_size_and_expected_table() -> None:
    assert len(ACCEPTED_ROWS) == 8
    assert len(UNSAFE_ROWS) == 16
    assert len(EXPECTED_DISPOSITIONS_AND_FACTORS) == 24
    assert len({row[0] for row in ACCEPTED_ROWS}) == 8
    assert len({row[0] for row in UNSAFE_ROWS}) == 16


@pytest.mark.parametrize(
    "name,left,right,winner,loser,evidence",
    ACCEPTED_ROWS,
    ids=lambda row: row[0] if isinstance(row, tuple) else str(row),
)
def test_false_obsolete_corpus_accepts_only_exact_rows_at_frozen_factor(
    tmp_path: Path, name: str, left: str, right: str, winner: str, loser: str, evidence: tuple[int, int]
) -> None:
    graph, meta = _stores(tmp_path)
    _seed(graph, left, right)
    before = graph.get_node(loser)
    assert before is not None
    application = _application(name, left=left, right=right, evidence=evidence, winner=winner, loser=loser)
    expected_disposition, expected_factor = EXPECTED_DISPOSITIONS_AND_FACTORS[name]
    receipt = graph.apply_reconciliation(application, downweight_factor=FACTOR)
    assert receipt.disposition is expected_disposition
    current = graph.get_node(loser)
    assert current is not None
    applied = graph.versions(loser)[-1]
    assert applied.decay_weight == before.decay_weight * 0.5
    assert current.node_id == before.node_id
    assert current.props == before.props
    assert current.entities == before.entities
    assert current.confidence == before.confidence
    assert current.provenance.model_dump(exclude={"history"}) == before.provenance.model_dump(
        exclude={"history"}
    )
    assert current.provenance.history[:-1] == before.provenance.history
    assert len(graph.versions(loser)) == 2
    replay = graph.apply_reconciliation(application, downweight_factor=FACTOR)
    assert replay == receipt
    assert len(graph.versions(loser)) == 2
    assert repair_reconciliation_audit(graph, meta, limit=10) == 1
    assert meta.audit_query(AuditFilter(action="reconcile_accepted"), _page()).total == 1


def _page():
    from mnemoseed_local.storage.ports import Page

    return Page(0, 50)


@pytest.mark.parametrize(
    "name,suffix,changes,reason,mutate",
    UNSAFE_ROWS,
    ids=lambda row: row[0] if isinstance(row, tuple) else str(row),
)
def test_false_obsolete_unsafe_shapes_never_downweight(
    tmp_path: Path,
    name: str,
    suffix: str,
    changes: dict[str, object],
    reason: ReasonCode,
    mutate: str | None,
) -> None:
    graph, _ = _stores(tmp_path)
    left, right = f"a-{suffix}", f"b-{suffix}"
    _seed(graph, left, right, protected=mutate == "protected")
    if mutate == "pointer":
        graph.set_read_conflict(left, f"c-{suffix}")
    elif mutate == "missing":
        graph.tombstone(right, deleted_at=50.0)
    elif mutate == "closed":
        graph.invalidate(right, 50.0)
    elif mutate == "tombstone":
        graph.tombstone(right, deleted_at=50.0)
    elif mutate == "zero":
        graph._conn.execute("UPDATE nodes SET decay_weight = 0.0 WHERE node_id = ?", (right,))
    application = _application(
        f"unsafe-{name}", left=left, right=right, evidence=(100, 100 + int(suffix[1:])), **changes
    )
    expected_disposition, expected_factor = EXPECTED_DISPOSITIONS_AND_FACTORS[name]
    if name == "deferred":
        with pytest.raises(ValueError, match="must be accepted, rejected, or unresolved"):
            graph.apply_reconciliation(application, downweight_factor=None)
        assert len(graph.versions(right)) == 1
        return
    factor = FACTOR if application.disposition is Disposition.ACCEPTED else None
    receipt = graph.apply_reconciliation(application, downweight_factor=factor)
    assert (receipt.disposition, receipt.downweight_factor) == (expected_disposition, expected_factor)
    assert len(graph.versions(right)) == 1
    current = graph.get_node(right)
    if current is not None:
        expected_weight = 0.0 if mutate == "zero" else 0.8
        assert current.decay_weight == pytest.approx(expected_weight)
