"""S-A reconciliation nomination: atomic composite NODE nomination.

TDD RED: every test here fails before the S-A implementation lands
(ports + v14 migration + sqlite_meta port + dream/nominate scanner + hook).
Contract freeze F1–F10 lives in design/12; tests pin the freeze, not prose.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mnemoseed_local.dream import nominate
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import Provenance
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import (
    ErrorEvent,
    ErrorEventFilter,
    ErrorSignalType,
    EvidenceKind,
    EvidencePointer,
    GraphFlag,
    NodeFilter,
    NominationOutcome,
    NominationRequest,
    Page,
    PageResult,
    derive_composite_group_id,
    derive_nomination_id,
)

PROFILE = "p1"

_PREF_PROPS = {
    "domain": "coding",
    "statement": "dark mode",
    "valence": 0.8,
    "prior_width": 0.3,
    "trait_anchor": "anima-1",
    "evidence_chain": [{"event": "created", "at": 123.0}],
}


def _prov() -> Provenance:
    return Provenance(asserted_by="s-a-test", source="session://s-sa")


def _node(node_id: str, profile: str = PROFILE, version: int = 1) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=profile,
        node_type=NodeType.PREFERENCE,
        entities=["ui"],
        props=dict(_PREF_PROPS),
        provenance=_prov(),
        valid_from=1000.0,
        version=version,
    )


@pytest.fixture
def meta(tmp_path: Path) -> SqliteMetaDriver:
    return SqliteMetaDriver(path=tmp_path / "meta.db")


@pytest.fixture
def graph(tmp_path: Path) -> SqliteGraphDriver:
    return SqliteGraphDriver(path=tmp_path / "cortex.db")


def _request(a: str = "na", b: str = "nb", **over) -> NominationRequest:
    base: dict = {
        "profile_id": PROFILE,
        "canonical_kind": "read_conflict",
        "node_a": a,
        "version_a": 1,
        "expected_peer_a": b,
        "node_b": b,
        "version_b": 1,
        "expected_peer_b": a,
        "source_generation": 1,
        "observed_at": 1700000000.0,
        "source_channels": ("read_conflict_flag",),
    }
    base.update(over)
    if "evidence" not in over:
        base["evidence"] = (
            EvidencePointer(kind=EvidenceKind.NODE, id=base["node_a"]),
            EvidencePointer(kind=EvidenceKind.NODE, id=base["node_b"]),
        )
    return NominationRequest(**base)


def _carrier_rows(meta: SqliteMetaDriver) -> list[dict]:
    rows = meta._conn.execute("SELECT * FROM reconcile_nominations").fetchall()
    return [dict(r) for r in rows]


def _group_events(meta: SqliteMetaDriver, group: str) -> list[dict]:
    rows = meta._conn.execute("SELECT * FROM error_events WHERE composite_group_id = ?", (group,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ F2 / F3


def test_nomination_id_stable_order_canonical_and_sized() -> None:
    first = derive_nomination_id("read_conflict", PROFILE, "na", 1, "nb", 1, 1)
    assert first == derive_nomination_id("read_conflict", PROFILE, "na", 1, "nb", 1, 1)
    assert first == derive_nomination_id("read_conflict", PROFILE, "nb", 1, "na", 1, 1)
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)
    assert derive_nomination_id("read_conflict", PROFILE, "na", 1, "nb", 1, 2) != first
    assert derive_nomination_id("read_conflict", PROFILE, "na", 1, "nc", 1, 1) != first
    assert derive_nomination_id("vote_disagreement", PROFILE, "na", 1, "nb", 1, 1) != first


def test_composite_group_id_is_nom_prefixed_slice_of_nomination_id() -> None:
    nid = derive_nomination_id("read_conflict", PROFILE, "na", 2, "nb", 1, 1)
    assert derive_composite_group_id(nid) == "nom-" + nid[:16]


def test_nomination_id_rejects_delimiter_ambiguity() -> None:
    pipe_profile = derive_nomination_id("read_conflict", "p|x", "a", 1, "z", 1, 1)
    pipe_node = derive_nomination_id("read_conflict", "p", "x|a", 1, "z", 1, 1)
    assert pipe_profile != pipe_node


# ------------------------------------------------------------- F1 / F8 port


def test_append_nomination_writes_carrier_plus_two_rows(meta: SqliteMetaDriver) -> None:
    result = meta.append_reconcile_nomination(_request())
    assert result.outcome is NominationOutcome.APPENDED
    expected_id = derive_nomination_id("read_conflict", PROFILE, "na", 1, "nb", 1, 1)
    assert result.nomination_id == expected_id

    carriers = _carrier_rows(meta)
    assert len(carriers) == 1
    carrier = carriers[0]
    assert carrier["nomination_id"] == expected_id
    assert carrier["profile_id"] == PROFILE
    assert carrier["canonical_kind"] == "read_conflict"
    assert carrier["composite_group_id"] == "nom-" + expected_id[:16]
    assert carrier["source_generation"] == 1
    assert {carrier["lo_node_id"], carrier["hi_node_id"]} == {"na", "nb"}

    events = _group_events(meta, carrier["composite_group_id"])
    assert len(events) == 2
    assert {e["evidence_id"] for e in events} == {"na", "nb"}
    for event in events:
        assert event["signal_type"] == ErrorSignalType.PUBLISHED.value
        assert event["evidence_kind"] == EvidenceKind.NODE.value
        assert event["detector_id"] == "read_conflict_scanner"
        assert event["eligibility_tag"] == "mark-as-is"
        assert event["nomination_id"] == expected_id
    assert {e["id"] for e in events} == set(json.loads(carrier["evidence_event_ids"]))


def test_append_nomination_duplicate_rolls_back_halves(meta: SqliteMetaDriver) -> None:
    first = meta.append_reconcile_nomination(_request())
    second = meta.append_reconcile_nomination(_request())
    assert first.outcome is NominationOutcome.APPENDED
    assert second.outcome is NominationOutcome.DUPLICATED
    assert second.nomination_id == first.nomination_id
    assert len(_carrier_rows(meta)) == 1
    assert len(_group_events(meta, "nom-" + first.nomination_id[:16])) == 2


def test_append_nomination_rejects_open_kind_and_blank_profile(
    meta: SqliteMetaDriver,
) -> None:
    with pytest.raises(ValueError):
        meta.append_reconcile_nomination(_request(canonical_kind="user_correction"))
    with pytest.raises(ValueError):
        meta.append_reconcile_nomination(_request(profile_id="   "))
    assert _carrier_rows(meta) == []
    assert meta._conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0] == 0


def test_append_nomination_rejects_foreign_provenance(meta: SqliteMetaDriver) -> None:
    with pytest.raises(ValueError):
        meta.append_reconcile_nomination(_request(source_channels=("other_flag",)))
    with pytest.raises(ValueError):
        meta.append_reconcile_nomination(
            _request(evidence=(EvidencePointer(kind=EvidenceKind.SESSION, id="s1"),))
        )
    with pytest.raises(ValueError):
        meta.append_reconcile_nomination(
            _request(
                evidence=(
                    EvidencePointer(kind=EvidenceKind.NODE, id="na"),
                    EvidencePointer(kind=EvidenceKind.NODE, id="nx"),
                )
            )
        )
    assert _carrier_rows(meta) == []
    assert meta._conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0] == 0


def test_append_nomination_reraises_non_dedup_fault(meta: SqliteMetaDriver) -> None:
    meta._conn.execute(
        "CREATE TRIGGER force_integrity BEFORE INSERT ON error_events "
        "BEGIN SELECT RAISE(ABORT, 'forced non-dedup fault'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        meta.append_reconcile_nomination(_request())
    assert _carrier_rows(meta) == []
    assert meta._conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0] == 0


def test_append_nomination_reraises_fault_despite_existing_carrier(
    meta: SqliteMetaDriver,
) -> None:
    first = meta.append_reconcile_nomination(_request())
    assert first.outcome is NominationOutcome.APPENDED
    meta._conn.execute(
        "CREATE TRIGGER force_integrity BEFORE INSERT ON error_events "
        "BEGIN SELECT RAISE(ABORT, 'forced unrelated fault'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        meta.append_reconcile_nomination(_request())
    assert len(_carrier_rows(meta)) == 1
    group = "nom-" + first.nomination_id[:16]
    assert len(_group_events(meta, group)) == 2


def test_append_nomination_carrier_trigger_fault_raises(meta: SqliteMetaDriver) -> None:
    meta._conn.execute(
        "CREATE TRIGGER force_carrier BEFORE INSERT ON reconcile_nominations "
        "BEGIN SELECT RAISE(ABORT, 'forced carrier fault'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        meta.append_reconcile_nomination(_request())
    assert _carrier_rows(meta) == []
    assert meta._conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0] == 0


def test_append_nomination_carrier_ignore_reports_fault(meta: SqliteMetaDriver) -> None:
    meta._conn.execute(
        "CREATE TRIGGER suppress_carrier BEFORE INSERT ON reconcile_nominations "
        "BEGIN SELECT RAISE(IGNORE); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        meta.append_reconcile_nomination(_request())
    assert _carrier_rows(meta) == []
    assert meta._conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0] == 0


def test_append_nomination_rejects_blank_endpoint_ids(meta: SqliteMetaDriver) -> None:
    with pytest.raises(ValueError):
        meta.append_reconcile_nomination(_request(expected_peer_a=None))
    with pytest.raises(ValueError):
        meta.append_reconcile_nomination(_request(node_a="   "))
    with pytest.raises(ValueError):
        meta.append_reconcile_nomination(_request(expected_peer_b=""))
    assert _carrier_rows(meta) == []
    assert meta._conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0] == 0


def test_legacy_event_rows_decode_unattributed(meta: SqliteMetaDriver) -> None:
    meta.append_error_event(
        ErrorEvent(
            profile_id=PROFILE,
            signal_type=ErrorSignalType.PUBLISHED,
            observed_at=1700000000.0,
            evidence_ptr=EvidencePointer(kind=EvidenceKind.NODE, id="nx"),
        )
    )
    page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE), Page(offset=0, limit=10))
    assert len(page.items) == 1
    assert page.items[0].nomination_id is None


# ------------------------------------------------------------------ F5 mint


def test_generation_mint_bumps_only_on_terminal_markers(
    meta: SqliteMetaDriver,
) -> None:
    prod = nominate.materialize_nominations
    assert prod is not None  # scanner entry exists; behavior pinned below
    assert nominate.mint_generation(0) == 1
    assert nominate.mint_generation(3) == 4


# ------------------------------------------------------------------ F6 scan


def _flag(graph: SqliteGraphDriver, a: str, b: str) -> None:
    graph.set_read_conflict(a, b)


def test_scanner_materializes_reciprocal_pair(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    graph.upsert_node(_node("na"))
    graph.upsert_node(_node("nb", version=1))
    graph.upsert_node(_node("na", version=2))  # current revision is v2
    _flag(graph, "na", "nb")
    report = nominate.materialize_nominations(graph, meta, PROFILE, clock=lambda: 1700000000.0)
    assert len(report.appended) == 1
    assert report.duplicated == () and report.skipped == 0
    carrier = _carrier_rows(meta)[0]
    assert carrier["lo_node_id"] == "na" and carrier["hi_node_id"] == "nb"
    assert (carrier["lo_version"], carrier["hi_version"]) == (2, 1)
    assert carrier["lo_expected_peer"] == "nb" and carrier["hi_expected_peer"] == "na"


def test_scanner_ignores_one_sided_overwrite(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    graph.upsert_node(_node("na"))
    graph.upsert_node(_node("nb"))
    _flag(graph, "na", "nb")
    graph.clear_read_conflict("nb")  # b repointed elsewhere; a's pointer orphaned
    report = nominate.materialize_nominations(graph, meta, PROFILE)
    assert report.appended == () and report.skipped == 1
    assert _carrier_rows(meta) == []


def test_scanner_nominates_current_reciprocal_only(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    for nid in ("na", "nb", "nc"):
        graph.upsert_node(_node(nid))
    _flag(graph, "na", "nb")
    _flag(graph, "nb", "nc")  # b repointed: a->b orphaned, b<->c current
    report = nominate.materialize_nominations(graph, meta, PROFILE)
    assert len(report.appended) == 1
    carrier = _carrier_rows(meta)[0]
    assert {carrier["lo_node_id"], carrier["hi_node_id"]} == {"nb", "nc"}


def test_scanner_ignores_missing_closed_peer(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    graph.upsert_node(_node("na"))
    graph.upsert_node(_node("nb"))
    _flag(graph, "na", "nb")
    assert graph.tombstone("nb") is True
    report = nominate.materialize_nominations(graph, meta, PROFILE)
    assert report.appended == () and report.skipped == 1
    assert _carrier_rows(meta) == []


def test_scanner_ignores_cross_profile_pair(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    graph.upsert_node(_node("na", profile="p1"))
    graph.upsert_node(_node("nb", profile="p2"))
    _flag(graph, "na", "nb")
    report = nominate.materialize_nominations(graph, meta, "p1")
    assert report.appended == () and report.skipped == 1
    assert _carrier_rows(meta) == []


def test_scanner_ignores_self_pair(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    graph.upsert_node(_node("na"))
    _flag(graph, "na", "na")
    report = nominate.materialize_nominations(graph, meta, PROFILE)
    assert report.appended == ()
    assert _carrier_rows(meta) == []


def test_scanner_second_scan_dedups(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    graph.upsert_node(_node("na"))
    graph.upsert_node(_node("nb"))
    _flag(graph, "na", "nb")
    first = nominate.materialize_nominations(graph, meta, PROFILE)
    second = nominate.materialize_nominations(graph, meta, PROFILE)
    assert len(first.appended) == 1
    assert second.appended == () and second.duplicated == first.appended
    assert len(_carrier_rows(meta)) == 1


def test_scanner_leaves_unlinkable_carriers_alone(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    meta.append_error_event(
        ErrorEvent(
            profile_id=PROFILE,
            signal_type=ErrorSignalType.PUBLISHED,
            observed_at=1700000000.0,
            evidence_ptr=EvidencePointer(kind=EvidenceKind.NODE, id="nx"),
        )
    )
    graph.upsert_node(_node("na"))
    graph.set_flags(["na"], [GraphFlag.NEEDS_RECONCILE])
    report = nominate.materialize_nominations(graph, meta, PROFILE)
    assert report.appended == () and report.duplicated == ()
    assert _carrier_rows(meta) == []


def test_scanner_crash_window_recovers_exactly_once(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    assert nominate.materialize_nominations(graph, meta, PROFILE).appended == ()
    graph.upsert_node(_node("na"))
    graph.upsert_node(_node("nb"))
    _flag(graph, "na", "nb")
    # crash between flag-raise and materialization: the next dream's scan
    # recovers; a further scan must not mint a second nomination.
    assert len(nominate.materialize_nominations(graph, meta, PROFILE).appended) == 1
    recovered = nominate.materialize_nominations(graph, meta, PROFILE)
    assert recovered.appended == () and len(recovered.duplicated) == 1
    assert len(_carrier_rows(meta)) == 1


def test_generation_bump_reopens_after_terminal(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    graph.upsert_node(_node("na"))
    graph.upsert_node(_node("nb"))
    _flag(graph, "na", "nb")
    gen1 = nominate.materialize_nominations(graph, meta, PROFILE)
    assert len(gen1.appended) == 1
    # S-C terminal marker appears later (fake source at S-A time): the same
    # flag state must mint a NEW generation, not collide with gen 1.
    gen2 = nominate.materialize_nominations(
        graph, meta, PROFILE, terminal_reader=lambda profile_id, lo, hi: 1
    )
    assert len(gen2.appended) == 1
    assert gen2.appended[0] != gen1.appended[0]
    assert len(_carrier_rows(meta)) == 2


# ------------------------------------------------------------------ F4 hook


def test_run_committed_nominations_swallows_scanner_failure() -> None:
    class _Boom:
        def list_nodes(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert nominate.run_committed_nominations(_Boom(), object(), PROFILE) is None


def test_after_commit_records_then_scans_and_swallows() -> None:
    calls: list[str] = []

    def _record(completion) -> None:
        calls.append("record")

    def _scan() -> None:
        calls.append("scan")
        raise RuntimeError("scanner blew up")

    nominate.after_commit(_record, _scan, object())
    assert calls == ["record", "scan"]


# ------------------------------------------------- NodeFilter flag (F6 scan)


def test_list_nodes_has_read_conflict_filter(
    graph: SqliteGraphDriver,
) -> None:
    for nid in ("na", "nb", "nc"):
        graph.upsert_node(_node(nid))
    _flag(graph, "na", "nb")
    flagged = graph.list_nodes(
        NodeFilter(profile_id=PROFILE, has_read_conflict=True), Page(offset=0, limit=10)
    )
    assert {n.node_id for n in flagged.items} == {"na", "nb"}
    everything = graph.list_nodes(NodeFilter(profile_id=PROFILE), Page(offset=0, limit=10))
    assert {n.node_id for n in everything.items} == {"na", "nb", "nc"}


def test_scanner_pages_past_a_full_first_page() -> None:
    first = [_node(f"n{i:04d}") for i in range(1000)]
    rest = [_node("tail")]
    seen_offsets: list[int] = []

    class _PagedGraph:
        def list_nodes(self, _flt, page):
            seen_offsets.append(page.offset)
            if page.offset == 0:
                return PageResult(items=first, total=1001, offset=0, limit=1000)
            return PageResult(items=rest, total=1001, offset=1000, limit=1000)

    found = nominate._scannable_nodes(_PagedGraph(), PROFILE)
    assert seen_offsets == [0, 1000]
    assert len(found) == 1001


def test_scanner_skips_malformed_pair_and_continues(graph: SqliteGraphDriver, meta: SqliteMetaDriver) -> None:
    graph.upsert_node(_node("   "))
    graph.upsert_node(_node("peer"))
    _flag(graph, "   ", "peer")
    graph.upsert_node(_node("va"))
    graph.upsert_node(_node("vb"))
    _flag(graph, "va", "vb")
    report = nominate.materialize_nominations(graph, meta, PROFILE, clock=lambda: 1700000000.0)
    assert len(report.appended) == 1
    assert report.skipped == 1
    assert len(_carrier_rows(meta)) == 1
