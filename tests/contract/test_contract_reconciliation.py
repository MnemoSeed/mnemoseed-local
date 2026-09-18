"""Durable reservation contracts; fixture numbers test mechanics, not product policy."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from mnemoseed_local import config
from mnemoseed_local.storage import ports
from mnemoseed_local.storage.drivers._migrations import apply_migrations
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import EvidenceKind, EvidencePointer, NominationRequest


def nominate(meta, *, profile_id="p", generation=1):
    return meta.append_reconcile_nomination(
        NominationRequest(
            profile_id=profile_id,
            canonical_kind="read_conflict",
            node_a="a",
            version_a=1,
            expected_peer_a="b",
            node_b="b",
            version_b=2,
            expected_peer_b="a",
            source_generation=generation,
            observed_at=10.0,
            evidence=(EvidencePointer(EvidenceKind.NODE, "a"), EvidencePointer(EvidenceKind.NODE, "b")),
            source_channels=("read_conflict_flag",),
        )
    ).nomination_id


def reserve(meta, nomination_id, *, run="run", cap=2):
    return meta.reserve_attempt(
        profile_id="p",
        nomination_id=nomination_id,
        dream_run_id=run,
        attempt_limit=cap,
        reserved_at=10.0,
        next_eligible_at=20.0,
    )


def test_reservation_replay_restart_and_budget(tmp_path):
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        nomination_id = nominate(meta)
        first = reserve(meta, nomination_id)
        assert first.outcome == "reserved"
        assert first.newly_reserved is True
        assert first.reservation.attempt_ordinal == 1
        assert first.reservation.profile_id == "p"
        assert first.reservation.nomination_id == nomination_id
        assert first.reservation.dream_run_id == "run"
        assert first.reservation.reserved_at == 10.0
        assert first.reservation.next_eligible_at == 20.0
        replay = meta.reserve_attempt(
            profile_id="p",
            nomination_id=nomination_id,
            dream_run_id="run",
            attempt_limit=1,
            reserved_at=30.0,
            next_eligible_at=40.0,
        )
        assert replay == replace(first, newly_reserved=False)
        asyncio.run(meta.close())
        meta = SqliteMetaDriver(tmp_path / "meta.db")
        assert reserve(meta, nomination_id) == replay
        second = reserve(meta, nomination_id, run="next")
        assert second.reservation.attempt_ordinal == 2
        exhausted = reserve(meta, nomination_id, run="third")
        assert exhausted.outcome == "budget_exhausted"
        assert exhausted.reservation is None
        assert exhausted.newly_reserved is False
        assert meta.list_reconciliation_attempts(profile_id="p", nomination_id=nomination_id) == (
            first.reservation,
            second.reservation,
        )
        assert meta.list_reconciliation_attempts(profile_id="other", nomination_id=nomination_id) == ()
    finally:
        asyncio.run(meta.close())


@pytest.mark.parametrize("cap", [None, True, False, 0, -1, 1.5, "2"])
def test_reservation_requires_explicit_positive_integer_budget(tmp_path, cap):
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        nomination_id = nominate(meta)
        with pytest.raises(ValueError):
            reserve(meta, nomination_id, cap=cap)
        assert meta.list_reconciliation_attempts(profile_id="p", nomination_id=nomination_id) == ()
    finally:
        asyncio.run(meta.close())


@pytest.mark.parametrize("times", [(float("nan"), 20), (10, float("inf")), (20, 20), (20, 10), (True, 20)])
def test_reservation_time_validation(tmp_path, times):
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        nomination_id = nominate(meta)
        with pytest.raises(ValueError):
            meta.reserve_attempt(
                profile_id="p",
                nomination_id=nomination_id,
                dream_run_id="run",
                attempt_limit=1,
                reserved_at=times[0],
                next_eligible_at=times[1],
            )
        assert meta.list_reconciliation_attempts(profile_id="p", nomination_id=nomination_id) == ()
    finally:
        asyncio.run(meta.close())


def test_reservation_profile_and_failure_rollback(tmp_path):
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        nomination_id = nominate(meta, profile_id="other")
        for missing in (nomination_id, "missing"):
            with pytest.raises(ports.StorageError):
                reserve(meta, missing)
        nomination_id = nominate(meta)
        with sqlite3.connect(tmp_path / "meta.db") as conn:
            conn.execute(
                "CREATE TRIGGER fail_attempt AFTER INSERT ON reconciliation_attempts "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            reserve(meta, nomination_id)
        assert meta.list_reconciliation_attempts(profile_id="p", nomination_id=nomination_id) == ()
        with sqlite3.connect(tmp_path / "meta.db") as conn:
            conn.execute("DROP TRIGGER fail_attempt")
        first = reserve(meta, nomination_id, cap=1)
        with sqlite3.connect(tmp_path / "meta.db") as conn:
            conn.execute(
                "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_log "
                "BEGIN SELECT RAISE(ABORT, 'audit failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="audit failure"):
            meta.audit_append(ports.AuditEntry(actor="test", action="reconcile_deferred"))
        assert meta.list_reconciliation_attempts(profile_id="p", nomination_id=nomination_id) == (
            first.reservation,
        )
        assert reserve(meta, nomination_id, cap=1, run="next").outcome == "budget_exhausted"
    finally:
        asyncio.run(meta.close())


def test_reservation_concurrent_cap_and_same_pair(tmp_path):
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        nomination_id = nominate(meta)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda n: reserve(meta, nomination_id, run=str(n), cap=2), range(8)))
        assert sum(result.newly_reserved for result in results) == 2
        assert sorted(result.reservation.attempt_ordinal for result in results if result.reservation) == [
            1,
            2,
        ]
        other_id = nominate(meta, generation=2)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: reserve(meta, other_id, cap=1), range(8)))
        assert sum(result.newly_reserved for result in results) == 1
        assert all(result.reservation == results[0].reservation for result in results)
    finally:
        asyncio.run(meta.close())


def test_nomination_snapshot_cursor_and_exact_evidence(tmp_path):
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        expected = sorted(nominate(meta, generation=i) for i in range(1, 8))
        nominate(meta, profile_id="other")
        page = meta.query_reconciliation_nominations(profile_id="p", limit=2)
        assert isinstance(page, ports.ReconciliationNominationPage)
        assert isinstance(page.items, tuple)
        assert len(page.items) == 2
        cursor = page.next_cursor
        assert isinstance(cursor, str)
        first = page.items[0]
        assert isinstance(first, ports.ReconciliationNominationCarrier)
        assert (first.profile_id, first.canonical_kind, first.lo_node_id, first.hi_node_id) == (
            "p",
            "read_conflict",
            "a",
            "b",
        )
        assert (first.lo_version, first.hi_version, first.lo_expected_peer, first.hi_expected_peer) == (
            1,
            2,
            "b",
            "a",
        )
        assert first.source_channels == ("read_conflict_flag",)
        assert isinstance(first.created_at, str)
        with pytest.raises(FrozenInstanceError):
            first.profile_id = "other"
        evidence = meta.read_reconciliation_evidence(profile_id="p", nomination_id=first.nomination_id)
        assert tuple(event.id for event in evidence) == first.evidence_event_ids
        assert tuple(event.evidence_ptr.id for event in evidence) == ("a", "b")
        meta.append_error_event(replace(evidence[0], id=None))
        assert (
            meta.read_reconciliation_evidence(profile_id="p", nomination_id=first.nomination_id) == evidence
        )
        assert (
            meta.read_reconciliation_evidence(profile_id="other", nomination_id=first.nomination_id) is None
        )
        assert meta.read_reconciliation_evidence(profile_id="p", nomination_id="missing") is None
        for i in range(8, 24):
            nominate(meta, generation=i)
        second = meta.query_reconciliation_nominations(profile_id="p", limit=2, cursor=cursor)
        asyncio.run(meta.close())
        meta = SqliteMetaDriver(tmp_path / "meta.db")
        assert meta.query_reconciliation_nominations(profile_id="p", limit=2, cursor=cursor) == second
        found = list(page.items)
        while cursor is not None:
            page = meta.query_reconciliation_nominations(profile_id="p", limit=2, cursor=cursor)
            assert len(page.items) <= 2
            found.extend(page.items)
            cursor = page.next_cursor
        assert [item.nomination_id for item in found] == expected
        assert len(meta.query_reconciliation_nominations(profile_id="p", limit=30).items) == 23
        assert meta.query_reconciliation_nominations(profile_id="absent", limit=1).items == ()
    finally:
        asyncio.run(meta.close())


@pytest.mark.parametrize("limit", [None, True, 0, -1, 1.5, "2"])
def test_nomination_query_bounds(tmp_path, limit):
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        with pytest.raises(ValueError):
            meta.query_reconciliation_nominations(profile_id="p", limit=limit)
    finally:
        asyncio.run(meta.close())


def test_nomination_cursor_invalid_and_cross_profile(tmp_path):
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        for i in range(3):
            nominate(meta, generation=i)
        cursor = meta.query_reconciliation_nominations(profile_id="p", limit=1).next_cursor
        for value in ("", "garbage", "[]", "null", "{}", 7):
            with pytest.raises(ValueError):
                meta.query_reconciliation_nominations(profile_id="p", limit=1, cursor=value)
        with pytest.raises(ValueError):
            meta.query_reconciliation_nominations(profile_id="other", limit=1, cursor=cursor)
    finally:
        asyncio.run(meta.close())


@pytest.mark.parametrize("corruption", ["profile", "group", "link", "missing", "duplicate", "shape"])
def test_exact_evidence_rejects_corrupt_membership(tmp_path, corruption):
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        nomination_id = nominate(meta)
        carrier = meta.query_reconciliation_nominations(profile_id="p", limit=1).items[0]
        with sqlite3.connect(tmp_path / "meta.db") as conn:
            if corruption in ("profile", "group", "link"):
                conn.execute("DROP TRIGGER trg_error_events_no_update")
                column = {"profile": "profile_id", "group": "composite_group_id", "link": "nomination_id"}[
                    corruption
                ]
                conn.execute(
                    f"UPDATE error_events SET {column} = 'wrong' WHERE id = ?",
                    (carrier.evidence_event_ids[0],),
                )
            else:
                conn.execute("DROP TRIGGER trg_reconcile_nominations_no_update")
                ids = {"missing": [99999], "duplicate": [carrier.evidence_event_ids[0]] * 2, "shape": [True]}[
                    corruption
                ]
                conn.execute("UPDATE reconcile_nominations SET evidence_event_ids = ?", (json.dumps(ids),))
        with pytest.raises(ports.StorageError):
            meta.read_reconciliation_evidence(profile_id="p", nomination_id=nomination_id)
    finally:
        asyncio.run(meta.close())


def test_receipt_read_is_profile_scoped_and_read_only(tmp_path):
    graph = SqliteGraphDriver(tmp_path / "graph.db")
    try:
        application = ports.ReconciliationApplication(
            nomination_id="nom",
            profile_id="p",
            disposition="unresolved",
            reason_code="missing_endpoint",
            canonical_kind="read_conflict",
            composite_group_id="group",
            evidence_event_ids=(1, 2),
            verdict="insufficient",
            quality="degraded",
            left_node_id="a",
            left_version=1,
            left_expected_peer_id="b",
            right_node_id="b",
            right_version=2,
            right_expected_peer_id="a",
            winner_node_id=None,
            loser_node_id=None,
            loser_prior_version=None,
            loser_new_version=None,
            applied_at=10.0,
        )
        receipt = graph.apply_reconciliation(application, downweight_factor=None)
        pending = graph.pending_reconciliation_audits(10)
        with sqlite3.connect(tmp_path / "graph.db") as conn:
            before = tuple(conn.iterdump())
        for _ in range(3):
            assert graph.get_reconciliation_receipt(profile_id="p", nomination_id="nom") == receipt
            assert graph.get_reconciliation_receipt(profile_id="other", nomination_id="nom") is None
            assert graph.get_reconciliation_receipt(profile_id="p", nomination_id="missing") is None
        assert graph.pending_reconciliation_audits(10) == pending
        with sqlite3.connect(tmp_path / "graph.db") as conn:
            assert tuple(conn.iterdump()) == before
    finally:
        asyncio.run(graph.close())


def test_deferred_dedup_key_is_compact_and_collision_safe():
    helper = ports.reconciliation_deferred_dedup_key
    assert helper("n", "r", "reconcile_deferred") == '["n","r","reconcile_deferred"]'
    assert json.loads(helper('n"', "r,", "action")) == ['n"', "r,", "action"]
    assert (
        len({helper("n", "r", "a"), helper("n", "r2", "a"), helper("n2", "r", "a"), helper("n", "r", "a2")})
        == 4
    )
    assert ports.reconciliation_audit_dedup_key("n", "a") == '["n","a"]'


def test_reconcile_counts_are_cumulative_and_profile_scoped(tmp_path):
    graph = SqliteGraphDriver(tmp_path / "graph.db")
    meta = SqliteMetaDriver(tmp_path / "meta.db")
    try:
        assert graph.count_reconciliation_receipts(profile_id="p") == {}
        assert meta.count_reconciliation_deferred(profile_id="p") == 0
        for i in range(2):
            meta.audit_append(
                ports.AuditEntry(
                    actor="dream-engine",
                    action="reconcile_deferred",
                    detail={"profile_id": "p", "nomination_id": f"n{i}"},
                    at=10.0 + i,
                    dedup_key=ports.reconciliation_deferred_dedup_key(f"n{i}", f"r{i}", "reconcile_deferred"),
                )
            )
        meta.audit_append(
            ports.AuditEntry(
                actor="dream-engine",
                action="reconcile_deferred",
                detail={"profile_id": "other", "nomination_id": "nforeign"},
                at=12.0,
                dedup_key=ports.reconciliation_deferred_dedup_key("nforeign", "r", "reconcile_deferred"),
            )
        )
        meta.audit_append(
            ports.AuditEntry(
                actor="dream-engine", action="reconcile_deferred", detail={"nomination_id": "nk"}
            )
        )
        meta.audit_append(
            ports.AuditEntry(
                actor="console-user",
                action="reconcile_deferred",
                detail={"profile_id": "p", "nomination_id": "nactor"},
            )
        )
        application = ports.ReconciliationApplication(
            nomination_id="nom",
            profile_id="p",
            disposition="unresolved",
            reason_code="missing_endpoint",
            canonical_kind="read_conflict",
            composite_group_id="group",
            evidence_event_ids=(1, 2),
            verdict="insufficient",
            quality="degraded",
            left_node_id="a",
            left_version=1,
            left_expected_peer_id="b",
            right_node_id="b",
            right_version=2,
            right_expected_peer_id="a",
            winner_node_id=None,
            loser_node_id=None,
            loser_prior_version=None,
            loser_new_version=None,
            applied_at=10.0,
        )
        for _ in range(3):
            assert graph.apply_reconciliation(application, downweight_factor=None).disposition == "unresolved"
        assert graph.count_reconciliation_receipts(profile_id="p") == {"unresolved": 1}
        assert graph.count_reconciliation_receipts(profile_id="other") == {}
        assert meta.count_reconciliation_deferred(profile_id="p") == 2
        assert meta.count_reconciliation_deferred(profile_id="other") == 1
    finally:
        asyncio.run(graph.close())
        asyncio.run(meta.close())


def test_reconciliation_config_defaults_and_loader(tmp_path):
    defaults = config.DreamConfig().reconciliation
    expected = {
        "enabled": False,
        "nomination_limit": None,
        "scan_limit": None,
        "audit_repair_limit": None,
        "attempt_limit": None,
        "retry_backoff_seconds": None,
        "attempt_timeout_seconds": None,
    }
    assert asdict(defaults) == expected
    with pytest.raises(FrozenInstanceError):
        defaults.enabled = True
    text = config.default_config_toml()
    assert all(key not in text for key in expected if key != "enabled")
    assert "reconciliation" not in tomllib.loads(text).get("dream", {})
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    assert config.load_config(path).dream.reconciliation == defaults
    path.write_text("[dream.reconciliation]\nenabled = true\n", encoding="utf-8")
    loaded = config.load_config(path).dream
    assert loaded.reconciliation == replace(defaults, enabled=True)
    assert loaded.ensemble == "off"
    assert loaded.experience_channel.enabled is False
    values = dict(
        enabled=True,
        nomination_limit=2,
        scan_limit=3,
        audit_repair_limit=4,
        attempt_limit=1,
        retry_backoff_seconds=2.5,
        attempt_timeout_seconds=3.5,
    )
    path.write_text(
        "[dream.reconciliation]\n"
        + "\n".join(f"{key} = {str(value).lower()}" for key, value in values.items()),
        encoding="utf-8",
    )
    assert asdict(config.load_config(path).dream.reconciliation) == values


@pytest.mark.parametrize("key", ["nomination_limit", "scan_limit", "audit_repair_limit", "attempt_limit"])
@pytest.mark.parametrize("value", ["0", "-1", "true", "false", "1.5", '"2"', "nan", "inf"])
def test_reconciliation_config_rejects_invalid_caps(tmp_path, key, value):
    path = tmp_path / "config.toml"
    path.write_text(f"[dream.reconciliation]\n{key} = {value}\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match=key):
        config.load_config(path)


@pytest.mark.parametrize("key", ["retry_backoff_seconds", "attempt_timeout_seconds"])
@pytest.mark.parametrize("value", ["0", "-1", "true", "nan", "inf", "-inf", '"2"'])
def test_reconciliation_config_rejects_invalid_durations(tmp_path, key, value):
    path = tmp_path / "config.toml"
    path.write_text(f"[dream.reconciliation]\n{key} = {value}\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match=key):
        config.load_config(path)


@pytest.mark.parametrize("text", ["[dream]\nreconciliation = false", "[dream.reconciliation]\nenabled = 1"])
def test_reconciliation_config_rejects_invalid_shape(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(config.ConfigError, match="reconciliation"):
        config.load_config(path)


def test_v16_attempt_schema_forward_empty_and_immutable(tmp_path):
    path = tmp_path / "meta.db"
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        assert apply_migrations(conn, "meta", target=15) == 15
        conn.execute("INSERT INTO audit_log(actor, action, at) VALUES ('test', 'old', 'old')")
        before = tuple(tuple(row) for row in conn.execute("SELECT * FROM audit_log"))
        assert apply_migrations(conn, "meta") == 16
        assert tuple(tuple(row) for row in conn.execute("SELECT * FROM audit_log")) == before
        assert conn.execute("SELECT COUNT(*) FROM reconciliation_attempts").fetchone()[0] == 0
        columns = [
            (row[1], row[2], row[3]) for row in conn.execute("PRAGMA table_info(reconciliation_attempts)")
        ]
        assert columns == [
            ("profile_id", "TEXT", 1),
            ("nomination_id", "TEXT", 1),
            ("dream_run_id", "TEXT", 1),
            ("attempt_ordinal", "INTEGER", 1),
            ("reserved_at", "REAL", 1),
            ("next_eligible_at", "REAL", 1),
        ]
        conn.execute("INSERT INTO reconciliation_attempts VALUES ('p', 'n', 'r', 1, 10, 20)")
        for values in [
            ("p", "n", "r", 2, 10, 20),
            ("p", "n", "r2", 1, 10, 20),
            ("p", "n2", "r", 0, 10, 20),
            ("p", "n2", "r", 1.5, 10, 20),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO reconciliation_attempts VALUES (?, ?, ?, ?, ?, ?)", values)
        for statement in (
            "UPDATE reconciliation_attempts SET attempt_ordinal = 2",
            "DELETE FROM reconciliation_attempts",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(statement)
