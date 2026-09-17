from __future__ import annotations

import json
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemoseed_local.decay.reinforce import ReinforceConfig, Reinforcer
from mnemoseed_local.dream import repair_reconciliation_audit
from mnemoseed_local.dream.adjudicate import Disposition, ReasonCode
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import Provenance, ProvenanceEvent
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import (
    AuditEntry,
    AuditFilter,
    ReceiptConflictError,
    ReconciliationApplication,
    ReconciliationAuditOutboxEntry,
    reconciliation_audit_dedup_key,
)

# Test mechanism only; this value has no product meaning.
TEST_ONLY_DOWNWEIGHT_FACTOR = 0.5


def _node(
    node_id: str,
    *,
    profile_id: str = "p1",
    peer: str | None = None,
    weight: float = 0.8,
    version: int = 1,
    never_decay: bool = False,
) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=profile_id,
        node_type=NodeType.PREFERENCE,
        props={
            "domain": "editor",
            "statement": f"statement-{node_id}",
            "valence": 0.4,
            "prior_width": 0.2,
            "trait_anchor": "stable-anchor",
            "evidence_chain": [{"chunk_id": f"chunk-{node_id}"}],
        },
        entities=["editor"],
        confidence=0.73,
        decay_weight=weight,
        never_decay=never_decay,
        needs_reconcile=True,
        conflict_flag=True,
        conflict_group="ownerless-group",
        read_conflict_id=peer,
        version=version,
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
    nomination_id: str = "nom-1",
    *,
    profile_id: str = "p1",
    disposition: Disposition = Disposition.ACCEPTED,
    reason_code: ReasonCode = ReasonCode.CONFLICT_CONFIRMED,
    verdict: str = "conflict",
    quality: str = "verified",
) -> ReconciliationApplication:
    return ReconciliationApplication(
        nomination_id=nomination_id,
        profile_id=profile_id,
        disposition=disposition,
        reason_code=reason_code,
        canonical_kind="read_conflict",
        composite_group_id=f"group-{nomination_id}",
        evidence_event_ids=(7, 9),
        verdict=verdict,
        quality=quality,
        left_node_id="a",
        left_version=1,
        left_expected_peer_id="b",
        right_node_id="b",
        right_version=1,
        right_expected_peer_id="a",
        winner_node_id="a" if disposition == Disposition.ACCEPTED else None,
        loser_node_id="b" if disposition == Disposition.ACCEPTED else None,
        loser_prior_version=None,
        loser_new_version=None,
        applied_at=100.0,
    )


def _stores(tmp_path: Path) -> tuple[SqliteGraphDriver, SqliteMetaDriver]:
    return SqliteGraphDriver(tmp_path / "graph.db"), SqliteMetaDriver(tmp_path / "meta.db")


def _seed_pair(graph: SqliteGraphDriver, *, profile_id: str = "p1", protected: bool = False) -> None:
    graph.upsert_node(_node("a", profile_id=profile_id, peer="b"))
    graph.upsert_node(_node("b", profile_id=profile_id, peer="a", never_decay=protected))


def test_drifted_peer_clears_only_the_still_exact_side(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    graph.set_read_conflict("b", "c")
    receipt = graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.UNRESOLVED
    assert receipt.reason_code is ReasonCode.STALE_REVISION
    assert receipt.loser_prior_version == 1
    assert receipt.loser_new_version is None
    assert graph.get_node("a").read_conflict_id is None
    assert graph.get_node("b").read_conflict_id == "c"
    assert len(graph.versions("a")) == 1
    assert len(graph.versions("b")) == 1


def test_receipt_commit_before_audit_repairs_once(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    _seed_pair(graph)
    receipt = graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.ACCEPTED
    assert (
        meta.audit_query(AuditFilter(action="reconcile_accepted"), page=replace(_page(), limit=50)).total == 0
    )
    pending = graph.pending_reconciliation_audits(1)
    assert isinstance(pending, list)
    entry = pending[0]
    assert isinstance(entry, ReconciliationAuditOutboxEntry)
    meta.audit_append(
        AuditEntry(
            actor="dream-engine",
            action=entry.action,
            detail=entry.detail,
            at=entry.created_at,
            dedup_key=reconciliation_audit_dedup_key(entry.nomination_id, entry.action),
        )
    )
    assert repair_reconciliation_audit(graph, meta, limit=1) == 1
    assert repair_reconciliation_audit(graph, meta, limit=1) == 0
    audit = meta.audit_query(AuditFilter(action="reconcile_accepted"), _page())
    assert audit.total == 1
    assert set(audit.items[0].detail) == {
        "nomination_id",
        "profile_id",
        "reason_code",
        "disposition",
        "left_node_id",
        "left_version",
        "right_node_id",
        "right_version",
        "winner_node_id",
        "loser_node_id",
    }
    assert "statement-a" not in str(audit.items[0].detail)
    assert len(graph.versions("b")) == 2
    with pytest.raises(sqlite3.IntegrityError, match="delivered-only"):
        graph._conn.execute(
            "UPDATE reconciliation_audit_outbox SET delivered_at = NULL WHERE nomination_id = 'nom-1'"
        )


def _page():
    from mnemoseed_local.storage.ports import Page

    return Page(0, 50)


def test_same_nomination_sequential_replay_n_times_is_exactly_once(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    _seed_pair(graph)
    app = _application()
    receipts = [
        graph.apply_reconciliation(app, downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR) for _ in range(4)
    ]
    assert receipts == [receipts[0]] * 4
    assert len(graph.versions("b")) == 2
    assert repair_reconciliation_audit(graph, meta, limit=10) == 1
    assert meta.audit_query(AuditFilter(action="reconcile_accepted"), _page()).total == 1
    with pytest.raises(ReceiptConflictError):
        graph.apply_reconciliation(
            replace(app, winner_node_id="b", loser_node_id="a"),
            downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR,
        )
    with pytest.raises((FrozenInstanceError, AttributeError)):
        receipts[0].reason_code = ReasonCode.STALE_REVISION  # type: ignore[misc]


def test_same_nomination_concurrent_replay_is_exactly_once(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    app = _application()
    # N=4 is test-only contention coverage, not a capacity bar.
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(
            pool.map(
                lambda _: graph.apply_reconciliation(app, downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR),
                range(4),
            )
        )
    assert receipts == [receipts[0]] * 4
    assert len(graph.versions("b")) == 2
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 1
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_audit_outbox").fetchone()[0] == 1


def test_accepted_preserves_semantic_source_fields_and_prior_versions(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    prior = graph.get_node("b")
    prior_payload = graph._conn.execute(
        "SELECT payload FROM node_versions WHERE node_id = 'b' AND version = 1"
    ).fetchone()[0]
    graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    versions = graph.versions("b")
    current = versions[-1]
    assert len(versions) == 2
    assert (
        graph._conn.execute(
            "SELECT payload FROM node_versions WHERE node_id = 'b' AND version = 1"
        ).fetchone()[0]
        == prior_payload
    )
    assert current.version == prior.version + 1
    assert current.decay_weight == pytest.approx(prior.decay_weight * TEST_ONLY_DOWNWEIGHT_FACTOR)
    for field in ("props", "entities", "confidence", "created_at"):
        assert getattr(current, field) == getattr(prior, field)
    assert current.provenance.model_dump(exclude={"history"}) == prior.provenance.model_dump(
        exclude={"history"}
    )
    assert current.provenance.history[:-1] == prior.provenance.history
    event = current.provenance.history[-1]
    assert event.action == "reconciled" and event.actor == "dream-engine"
    assert event.detail == {
        "nomination_id": "nom-1",
        "winner_node_id": "a",
        "prior_version": 1,
        "disposition": "accepted",
    }
    for factor in (0.0, 1.0, -0.1, math.inf, -math.inf, math.nan):
        with pytest.raises(ValueError, match="finite and strictly between zero and one"):
            graph.apply_reconciliation(_application(f"bad-{factor}"), downweight_factor=factor)


@pytest.mark.parametrize("disposition", [Disposition.REJECTED, Disposition.UNRESOLVED])
def test_rejected_and_unresolved_never_downweight(tmp_path: Path, disposition: Disposition) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph, protected=disposition is Disposition.UNRESOLVED)
    reason = (
        ReasonCode.NOT_CONFLICT_CONFIRMED
        if disposition is Disposition.REJECTED
        else ReasonCode.PROTECTED_ENDPOINT
    )
    app = _application(disposition=disposition, reason_code=reason)
    receipt = graph.apply_reconciliation(app, downweight_factor=None)
    assert receipt.disposition is disposition
    assert len(graph.versions("b")) == 1
    if disposition is Disposition.UNRESOLVED:
        assert graph.get_node("a").read_conflict_id == "b"
        assert graph.get_node("b").read_conflict_id == "a"
    with pytest.raises(ValueError, match="only for accepted"):
        graph.apply_reconciliation(replace(app, nomination_id="wrong-factor"), downweight_factor=0.5)


def test_protected_endpoint_accepted_is_terminal_unresolved(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    _seed_pair(graph, protected=True)
    receipt = graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.UNRESOLVED
    assert receipt.reason_code is ReasonCode.PROTECTED_ENDPOINT
    assert len(graph.versions("a")) == 1
    assert len(graph.versions("b")) == 1
    assert graph.get_node("a").read_conflict_id == "b"
    assert graph.get_node("b").read_conflict_id == "a"
    pending = graph.pending_reconciliation_audits(10)
    assert len(pending) == 1
    assert pending[0].action == "reconcile_unresolved"
    assert repair_reconciliation_audit(graph, meta, limit=10) == 1
    assert meta.audit_query(AuditFilter(action="reconcile_unresolved"), _page()).total == 1


def test_zero_weight_loser_is_not_lowerable(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    graph.upsert_node(_node("c", peer="d"))
    graph.upsert_node(_node("d", peer="c", weight=0.0))
    zero_app = replace(
        _application("zero-weight"),
        left_node_id="c",
        left_expected_peer_id="d",
        right_node_id="d",
        right_expected_peer_id="c",
        winner_node_id="c",
        loser_node_id="d",
    )
    receipt = graph.apply_reconciliation(zero_app, downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.UNRESOLVED
    assert receipt.reason_code is ReasonCode.DECAY_WEIGHT_NOT_LOWERABLE
    assert [node.decay_weight for node in graph.versions("d")] == [0.0]
    assert graph.get_node("c").read_conflict_id == "d"
    assert graph.get_node("d").read_conflict_id == "c"
    assert len(graph.pending_reconciliation_audits(10)) == 1
    assert repair_reconciliation_audit(graph, meta, limit=10) == 1


def test_moved_pointer_is_stale_with_no_clear_claim(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    first = graph.apply_reconciliation(_application("nom-1"), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert first.disposition is Disposition.ACCEPTED
    assert graph.get_node("a").read_conflict_id is None
    assert graph.get_node("b").read_conflict_id is None
    stale = graph.apply_reconciliation(_application("nom-2"), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert stale.disposition is Disposition.UNRESOLVED
    assert stale.reason_code is ReasonCode.STALE_REVISION
    assert len(graph.versions("b")) == 2
    assert graph.get_node("a").read_conflict_id is None
    assert graph.get_node("b").read_conflict_id is None
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 2


def test_mid_transaction_fault_rolls_back_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph, meta = _stores(tmp_path)
    _seed_pair(graph)
    original = graph._append_reconciliation_version

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected infrastructure fault")

    monkeypatch.setattr(graph, "_append_reconciliation_version", fail_after_write)
    with pytest.raises(OSError, match="injected"):
        graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 0
    assert len(graph.versions("b")) == 1
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_audit_outbox").fetchone()[0] == 0
    assert graph.pending_reconciliation_audits(10) == []
    assert graph.get_node("a").read_conflict_id == "b"
    assert graph.get_node("b").read_conflict_id == "a"
    assert repair_reconciliation_audit(graph, meta, limit=10) == 0


def test_receipt_conflict_leaves_original_receipt(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    app = _application()
    original = graph.apply_reconciliation(app, downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    with pytest.raises(ReceiptConflictError):
        graph.apply_reconciliation(
            replace(app, winner_node_id="b", loser_node_id="a"),
            downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR,
        )
    replayed = graph.apply_reconciliation(app, downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert replayed == original
    assert len(graph.versions("b")) == 2
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 1
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_audit_outbox").fetchone()[0] == 1


def test_repaired_audit_carries_no_memory_text(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    _seed_pair(graph)
    graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    pending = graph.pending_reconciliation_audits(10)
    assert len(pending) == 1
    assert "statement-a" not in str(pending[0].detail)
    assert "statement-b" not in str(pending[0].detail)
    assert repair_reconciliation_audit(graph, meta, limit=10) == 1
    audit = meta.audit_query(AuditFilter(action="reconcile_accepted"), _page())
    assert audit.total == 1
    assert "statement-a" not in str(audit.items[0].detail)
    assert "statement-b" not in str(audit.items[0].detail)


def test_audit_dedup_key_is_compact_json_pair() -> None:
    assert reconciliation_audit_dedup_key("nom-1", "reconcile_accepted") == '["nom-1","reconcile_accepted"]'


def test_reconciliation_tables_are_append_only_with_delivered_at_lane(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        graph._conn.execute("UPDATE reconciliation_receipts SET profile_id = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        graph._conn.execute("DELETE FROM reconciliation_receipts")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        graph._conn.execute("DELETE FROM reconciliation_audit_outbox")
    with pytest.raises(sqlite3.IntegrityError, match="delivered-only"):
        graph._conn.execute(
            "UPDATE reconciliation_audit_outbox SET detail = '{}' WHERE nomination_id = 'nom-1'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="delivered-only"):
        graph._conn.execute(
            "UPDATE reconciliation_audit_outbox SET nomination_id = 'tampered' WHERE nomination_id = 'nom-1'"
        )
    pending = graph.pending_reconciliation_audits(10)
    assert isinstance(pending, list)
    assert len(pending) == 1
    assert graph.mark_reconciliation_audit_delivered(pending[0].outbox_id, 200.0) is True
    assert graph.mark_reconciliation_audit_delivered(pending[0].outbox_id, 300.0) is False
    with pytest.raises(sqlite3.IntegrityError, match="delivered-only"):
        graph._conn.execute(
            "UPDATE reconciliation_audit_outbox SET delivered_at = NULL WHERE nomination_id = 'nom-1'"
        )
    assert graph.pending_reconciliation_audits(10) == []


def test_terminal_application_clears_only_owned_read_conflict(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    receipt = graph.apply_reconciliation(
        _application(disposition=Disposition.REJECTED, reason_code=ReasonCode.NOT_CONFLICT_CONFIRMED),
        downweight_factor=None,
    )
    assert receipt.disposition is Disposition.REJECTED
    for node_id in ("a", "b"):
        node = graph.get_node(node_id)
        assert node.read_conflict_id is None
        assert node.needs_reconcile is True
        assert node.conflict_flag is True
        assert node.conflict_group == "ownerless-group"
    graph._conn.execute("UPDATE nodes SET read_conflict_id = 'b' WHERE node_id = 'a'")
    graph._conn.execute("UPDATE nodes SET valid_to = '2026-01-01T00:00:00Z' WHERE node_id = 'a'")
    stale = graph.apply_reconciliation(
        _application("cas-zero"), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR
    )
    assert stale.disposition is Disposition.UNRESOLVED


def test_real_reinforcement_rebounds_without_rewriting_reconciliation_revision(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    _seed_pair(graph)
    graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    reconciled = graph.versions("b")[-1]
    reconciled_payload = graph._conn.execute(
        "SELECT payload FROM node_versions WHERE node_id = 'b' AND version = 2"
    ).fetchone()[0]
    stores = SimpleNamespace(vector=object(), graph=graph, meta=meta, embed=object())
    Reinforcer(
        stores,
        clock=lambda: 200.0,
        config=ReinforceConfig(bonus=0.1, min_decay=0.0),
    ).record_hits([], ["b"])
    versions = graph.versions("b")
    assert len(versions) == 3
    assert (
        graph._conn.execute(
            "SELECT payload FROM node_versions WHERE node_id = 'b' AND version = 2"
        ).fetchone()[0]
        == reconciled_payload
    )
    assert versions[1].provenance == reconciled.provenance
    assert versions[2].decay_weight == pytest.approx(reconciled.decay_weight + 0.1)
    assert versions[2].provenance.history[-1].action == "reinforced"


def test_ordinary_reinforcement_reuses_revision(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    _seed_pair(graph)
    before = graph.get_node("a")
    assert before is not None
    stores = SimpleNamespace(vector=object(), graph=graph, meta=meta, embed=object())
    Reinforcer(
        stores,
        clock=lambda: 200.0,
        config=ReinforceConfig(bonus=0.1, min_decay=0.0),
    ).record_hits([], ["a"])
    after = graph.get_node("a")
    assert after is not None
    assert after.version == before.version == 1
    assert len(graph.versions("a")) == 1
    assert after.decay_weight == pytest.approx(min(1.0, before.decay_weight + 0.1))
    assert after.hit_count == before.hit_count + 1


def test_same_profile_applications_serialize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    original = graph._append_reconciliation_version

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected infrastructure fault")

    monkeypatch.setattr(graph, "_append_reconciliation_version", fail_after_write)
    with pytest.raises(OSError, match="injected"):
        graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert len(graph.versions("b")) == 1
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 0
    monkeypatch.setattr(graph, "_append_reconciliation_version", original)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                graph.apply_reconciliation,
                _application(nomination_id),
                downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR,
            )
            for nomination_id in ("nom-1", "nom-2")
        ]
    assert {future.result().disposition for future in futures} == {
        Disposition.ACCEPTED,
        Disposition.UNRESOLVED,
    }
    assert len(graph.versions("b")) == 2


def test_cross_profile_applications_are_data_isolated(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph, profile_id="p1")
    graph.upsert_node(_node("c", profile_id="p2", peer="d"))
    graph.upsert_node(_node("d", profile_id="p2", peer="c"))
    app_p2 = replace(
        _application("nom-p2"),
        profile_id="p2",
        left_node_id="c",
        left_expected_peer_id="d",
        right_node_id="d",
        right_expected_peer_id="c",
        winner_node_id="c",
        loser_node_id="d",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(
                lambda app: graph.apply_reconciliation(app, downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR),
                (_application("nom-p1"), app_p2),
            )
        )
    assert {receipt.profile_id for receipt in receipts} == {"p1", "p2"}
    assert all(receipt.disposition is Disposition.ACCEPTED for receipt in receipts)
    assert len(graph.versions("b")) == 2
    assert len(graph.versions("d")) == 2
    assert graph.get_node("a").profile_id == "p1"
    assert graph.get_node("c").profile_id == "p2"
    assert graph._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    for table in ("reconciliation_receipts", "reconciliation_audit_outbox"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only|delivered"):
            graph._conn.execute(f"UPDATE {table} SET nomination_id = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            graph._conn.execute(f"DELETE FROM {table}")


def test_receipt_records_every_enriched_field(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    receipt = graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.ACCEPTED
    assert receipt.canonical_kind == "read_conflict"
    assert receipt.composite_group_id == "group-nom-1"
    assert receipt.evidence_event_ids == (7, 9)
    assert receipt.verdict == "conflict"
    assert receipt.quality == "verified"
    assert receipt.loser_prior_version == 1
    assert receipt.loser_new_version == 2
    row = dict(
        graph._conn.execute("SELECT * FROM reconciliation_receipts WHERE nomination_id = 'nom-1'").fetchone()
    )
    assert row["canonical_kind"] == "read_conflict"
    assert row["composite_group_id"] == "group-nom-1"
    assert json.loads(row["evidence_event_ids"]) == [7, 9]
    assert row["verdict"] == "conflict"
    assert row["quality"] == "verified"
    assert row["loser_prior_version"] == 1
    assert row["loser_new_version"] == 2


def test_rejected_receipt_carries_no_loser_versions(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    receipt = graph.apply_reconciliation(
        _application(
            disposition=Disposition.REJECTED,
            reason_code=ReasonCode.NOT_CONFLICT_CONFIRMED,
            verdict="not_conflict",
        ),
        downweight_factor=None,
    )
    assert receipt.disposition is Disposition.REJECTED
    assert receipt.canonical_kind == "read_conflict"
    assert receipt.composite_group_id == "group-nom-1"
    assert receipt.evidence_event_ids == (7, 9)
    assert receipt.verdict == "not_conflict"
    assert receipt.quality == "verified"
    assert receipt.loser_prior_version is None
    assert receipt.loser_new_version is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canonical_kind", "vote_disagreement"),
        ("composite_group_id", "group-other"),
        ("evidence_event_ids", (7, 10)),
        ("verdict", "not_conflict"),
        ("quality", "voted"),
    ],
)
def test_replay_with_altered_enriched_field_conflicts(tmp_path: Path, field: str, value: object) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    app = _application()
    original = graph.apply_reconciliation(app, downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    with pytest.raises(ReceiptConflictError):
        graph.apply_reconciliation(
            replace(app, **{field: value}),  # type: ignore[arg-type]
            downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR,
        )
    replayed = graph.apply_reconciliation(app, downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert replayed == original
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 1
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_audit_outbox").fetchone()[0] == 1


@pytest.mark.parametrize("disposition", ["deferred", "bogus"])
def test_non_terminal_disposition_is_typed_rejection(tmp_path: Path, disposition: str) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    with pytest.raises(ValueError, match="must be accepted, rejected, or unresolved"):
        graph.apply_reconciliation(
            _application("nom-deferred", disposition=disposition),  # type: ignore[arg-type]
            downweight_factor=None,
        )
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 0
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_audit_outbox").fetchone()[0] == 0
    assert len(graph.versions("a")) == 1
    assert len(graph.versions("b")) == 1
    assert graph.get_node("a").read_conflict_id == "b"
    assert graph.get_node("b").read_conflict_id == "a"


def test_one_sided_orphan_clears_only_the_live_side(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    assert graph.tombstone("b", deleted_at=50.0) is True
    receipt = graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.UNRESOLVED
    assert receipt.reason_code is ReasonCode.STALE_REVISION
    assert receipt.loser_prior_version is None
    assert receipt.loser_new_version is None
    assert graph.get_node("a").read_conflict_id is None
    assert graph.get_node("b") is None
    assert len(graph.versions("a")) == 1
    assert len(graph.versions("b")) == 1
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 1
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_audit_outbox").fetchone()[0] == 1


def test_accepted_with_closed_loser_clears_winner_with_zero_appends(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    graph.invalidate("b", 50.0)
    receipt = graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.UNRESOLVED
    assert receipt.reason_code is ReasonCode.STALE_REVISION
    assert receipt.loser_prior_version is None
    assert receipt.loser_new_version is None
    assert graph.get_node("a").read_conflict_id is None
    assert graph.get_node("b") is None
    assert len(graph.versions("a")) == 1
    assert len(graph.versions("b")) == 1


def test_accepted_with_both_sides_dead_clears_nothing(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    assert graph.tombstone("a", deleted_at=50.0) is True
    assert graph.tombstone("b", deleted_at=51.0) is True
    receipt = graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.UNRESOLVED
    assert receipt.reason_code is ReasonCode.STALE_REVISION
    assert graph.get_node("a") is None
    assert graph.get_node("b") is None
    assert len(graph.versions("a")) == 1
    assert len(graph.versions("b")) == 1
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_receipts").fetchone()[0] == 1
    assert graph._conn.execute("SELECT COUNT(*) FROM reconciliation_audit_outbox").fetchone()[0] == 1


def test_cas_zero_row_on_exact_side_falls_back_with_no_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    monkeypatch.setattr(graph, "_cas_clear_side", lambda **kwargs: 0)
    receipt = graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.UNRESOLVED
    assert receipt.reason_code is ReasonCode.STALE_REVISION
    assert receipt.loser_new_version is None
    assert graph.get_node("a").read_conflict_id == "b"
    assert graph.get_node("b").read_conflict_id == "a"
    assert len(graph.versions("a")) == 1
    assert len(graph.versions("b")) == 1


def test_outbox_composite_key_keeps_table_open_for_future_actions(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    graph._conn.execute(
        "INSERT INTO reconciliation_audit_outbox "
        "(nomination_id, profile_id, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        ("nom-1", "p1", "future_action", "{}", "2026-02-01T00:00:00Z"),
    )
    rows = graph._conn.execute(
        "SELECT action FROM reconciliation_audit_outbox WHERE nomination_id = 'nom-1' ORDER BY id"
    ).fetchall()
    assert [row[0] for row in rows] == ["reconcile_accepted", "future_action"]


def test_repointed_side_keeps_its_pointer(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    graph.upsert_node(_node("c"))
    graph.set_read_conflict("a", "c")
    receipt = graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert receipt.disposition is Disposition.UNRESOLVED
    assert receipt.reason_code is ReasonCode.STALE_REVISION
    assert graph.get_node("a").read_conflict_id == "c"
    assert graph.get_node("b").read_conflict_id is None
    assert graph.get_node("c").read_conflict_id == "a"
    assert len(graph.versions("a")) == 1
    assert len(graph.versions("b")) == 1


def test_repair_audit_failure_leaves_pending_and_raises(tmp_path: Path) -> None:
    graph, meta = _stores(tmp_path)
    _seed_pair(graph)
    graph.apply_reconciliation(_application(), downweight_factor=TEST_ONLY_DOWNWEIGHT_FACTOR)
    assert len(graph.pending_reconciliation_audits(10)) == 1

    def _boom(entry: AuditEntry) -> None:
        raise OSError("meta-down")

    boom = SimpleNamespace(audit_append=_boom)
    with pytest.raises(OSError, match="meta-down"):
        repair_reconciliation_audit(graph, boom, limit=10)
    assert len(graph.pending_reconciliation_audits(10)) == 1
    assert repair_reconciliation_audit(graph, meta, limit=10) == 1
    assert len(graph.pending_reconciliation_audits(10)) == 0


def test_cas_clear_requires_exact_peer_predicate(tmp_path: Path) -> None:
    graph, _ = _stores(tmp_path)
    _seed_pair(graph)
    assert (
        graph._cas_clear_side(
            node_id="a",
            profile_id="p1",
            version=1,
            expected_peer_id="WRONG",
        )
        == 0
    )
    assert graph.get_node("a").read_conflict_id == "b"
    assert (
        graph._cas_clear_side(
            node_id="a",
            profile_id="p1",
            version=1,
            expected_peer_id="b",
        )
        == 1
    )
    assert graph.get_node("a").read_conflict_id is None
