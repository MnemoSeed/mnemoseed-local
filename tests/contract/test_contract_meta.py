"""Driver-agnostic contract tests for the MetaStore port (prd-08 appendix B.3).

Every meta method gets at least one behavioral test against the embedded
(sqlite_meta) driver. The driver carries the append-only audit enforcement at
the database level, and the same atomic pool semantics.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from _support import make_prov, raw_meta_row

from mnemoseed_local.storage.ports import (
    AuditEntry,
    AuditFilter,
    Capability,
    ConfigEntry,
    DreamRun,
    DreamRunFilter,
    ErrorEvent,
    ErrorEventFilter,
    ErrorSignalType,
    EvidenceKind,
    EvidencePointer,
    OwnerConflictError,
    Page,
    PoolState,
    StorageError,
    StoredProfile,
    StoredUser,
    TurnRange,
)


def _pool_profile(stack, profile_id: str = "u1") -> StoredProfile:
    return StoredProfile(profile_id=profile_id, display_name="Uma", created_at=time.time())


# ---------------------------------------------------------------- B.3 surface


def test_capabilities(stack) -> None:
    expected = frozenset({Capability.META_TRANSACTION, Capability.META_CONCURRENT_READERS})
    assert stack.meta.capabilities() == stack.meta.info.capabilities == expected


def test_pool_add_state_advance_watermark(stack) -> None:
    profile = "u1"
    assert stack.meta.pool_state(profile) == PoolState(balance=0.0)
    stack.meta.pool_add(profile, 10.0, TurnRange(start=0, end=4))
    stack.meta.advance_watermark(profile, TurnRange(start=0, end=4))
    state = stack.meta.pool_state(profile)
    assert state.balance == 10.0
    assert state.watermark == TurnRange(start=0, end=4)

    stack.meta.advance_watermark(profile, TurnRange(start=1, end=8))
    assert stack.meta.pool_state(profile).watermark == TurnRange(start=0, end=8)

    stack.meta.pool_add(profile, 5.0, TurnRange(start=5, end=9))
    assert stack.meta.pool_state(profile).balance == 15.0


def test_pool_per_profile_isolation(stack) -> None:
    stack.meta.pool_add("a", 10.0, TurnRange(start=0, end=4))
    stack.meta.pool_add("b", 3.0, TurnRange(start=0, end=4))
    assert stack.meta.pool_state("a").balance == 10.0
    assert stack.meta.pool_state("b").balance == 3.0
    stack.meta.advance_watermark("a", TurnRange(start=0, end=4))
    assert stack.meta.pool_state("a").watermark == TurnRange(start=0, end=4)
    assert stack.meta.pool_state("b").watermark is None


def test_pool_state_unknown_profile_is_empty(stack) -> None:
    assert stack.meta.pool_state("ghost") == PoolState(balance=0.0)


def test_pool_credit_upserts_row(stack) -> None:
    """pool_credit sets a profile's row absolutely (balance + watermark)."""
    stack.meta.pool_credit("u1", 5.0, TurnRange(start=0, end=2))
    state = stack.meta.pool_state("u1")
    assert state.balance == 5.0
    assert state.watermark == TurnRange(start=0, end=2)
    # a second credit overwrites instead of accumulating
    stack.meta.pool_credit("u1", 3.0, TurnRange(start=3, end=5))
    state = stack.meta.pool_state("u1")
    assert state.balance == 3.0
    assert state.watermark == TurnRange(start=3, end=5)


def test_pool_drain_files_the_lifetime_ledger(stack) -> None:
    """pool_drain moves the pending gauge into the lifetime filed total in one
    transaction and returns the filed amount; the watermark is untouched."""
    stack.meta.pool_credit("u1", 7.0, TurnRange(start=0, end=4))
    assert stack.meta.pool_drain("u1", TurnRange(start=0, end=4)) == 7.0
    state = stack.meta.pool_state("u1")
    assert state.balance == 0.0
    assert state.filed_points_total == 7.0
    assert state.watermark == TurnRange(start=0, end=4)
    # the ledger accumulates across fires
    stack.meta.pool_credit("u1", 4.0, TurnRange(start=5, end=6))
    assert stack.meta.pool_drain("u1", TurnRange(start=5, end=6)) == 4.0
    state = stack.meta.pool_state("u1")
    assert state.balance == 0.0
    assert state.filed_points_total == 11.0


def test_pool_drain_unknown_profile_is_empty(stack) -> None:
    assert stack.meta.pool_drain("ghost", TurnRange(start=0, end=0)) == 0.0
    assert stack.meta.pool_state("ghost") == PoolState(balance=0.0)


def test_pool_states_returns_all_rows(stack) -> None:
    stack.meta.pool_credit("u1", 4.0, TurnRange(start=0, end=1))
    stack.meta.pool_add("u2", 7.0, TurnRange(start=2, end=3))
    states = stack.meta.pool_states()
    assert set(states) == {"u1", "u2"}
    assert states["u1"].balance == 4.0
    assert states["u1"].watermark == TurnRange(start=0, end=1)  # pool_credit carries the span
    assert states["u2"].balance == 7.0
    assert states["u2"].watermark is None  # pool_add alone advances no watermark


def test_pool_watermark_gap_raises(stack) -> None:
    stack.meta.advance_watermark("u1", TurnRange(start=0, end=4))
    with pytest.raises(ValueError, match="jumps over unprocessed turns"):
        stack.meta.advance_watermark("u1", TurnRange(start=10, end=12))


def test_profile_crud_and_token_cascade(stack) -> None:
    stack.meta.upsert_profile(_pool_profile(stack))
    assert stack.meta.get_profile("u1").display_name == "Uma"
    stack.meta.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma Updated"))
    assert stack.meta.get_profile("u1").display_name == "Uma Updated"
    stack.meta.upsert_profile(StoredProfile(profile_id="u2", display_name="Bob"))
    assert {p.profile_id for p in stack.meta.list_profiles()} == {"u1", "u2"}

    token = stack.meta.issue_token("u1", ("graph:read",))
    stack.meta.delete_profile("u1")
    assert stack.meta.get_profile("u1") is None
    assert raw_meta_row(stack, "tokens", "token_id", token.token_id) == {}  # FK cascade


def test_create_profile_is_insert_only(stack) -> None:
    """#109 lifecycle race contract: create_profile never overwrites — a
    duplicate id (including one landing between the caller's check and the
    insert) returns False and leaves the existing row untouched."""
    stack.meta.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma"))
    assert stack.meta.create_profile(StoredProfile(profile_id="u2", display_name="Bob")) is True
    assert stack.meta.get_profile("u2").display_name == "Bob"
    assert stack.meta.create_profile(StoredProfile(profile_id="u1", display_name="Clobber")) is False
    assert stack.meta.get_profile("u1").display_name == "Uma"


def test_profile_archive_flag(stack) -> None:
    """FR-7.3 console profile archive: the flag roundtrips, rename never
    touches it (upsert updates display_name only), and unknown profiles raise."""
    stack.meta.upsert_profile(_pool_profile(stack))
    assert stack.meta.get_profile("u1").archived is False
    stack.meta.archive_profile("u1", True)
    assert stack.meta.get_profile("u1").archived is True
    assert stack.meta.list_profiles()[0].archived is True
    stack.meta.archive_profile("u1", False)
    assert stack.meta.get_profile("u1").archived is False

    # rename preserves the flag
    stack.meta.archive_profile("u1", True)
    stack.meta.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma Renamed"))
    got = stack.meta.get_profile("u1")
    assert got.display_name == "Uma Renamed"
    assert got.archived is True

    with pytest.raises(StorageError, match="unknown profile"):
        stack.meta.archive_profile("ghost", True)


def test_issue_token_and_revoke(stack) -> None:
    stack.meta.upsert_profile(_pool_profile(stack))
    token = stack.meta.issue_token("u1", ("graph:read", "graph:write"), expires_at=time.time() + 60.0)
    assert token.profile_id == "u1"
    assert tuple(token.scopes) == ("graph:read", "graph:write")
    assert token.revoked is False
    with pytest.raises(StorageError, match="unknown profile"):
        stack.meta.issue_token("ghost", ("graph:read",))

    stack.meta.revoke_token(token.token_id)
    assert int(raw_meta_row(stack, "tokens", "token_id", token.token_id)["revoked"]) == 1


def test_users_crud_and_password_rotation(stack) -> None:
    """Account layer (PRD-06 FR-6.1a): users rows; the password_hash is opaque
    (argon2 at the service layer) — the port only stores what it is given."""
    assert stack.meta.count_users() == 0
    stack.meta.create_user(
        StoredUser(
            user_id="u-owner",
            username="owner",
            password_hash="argon2-stub",
            role="owner",
            created_at=100.0,
        )
    )
    assert stack.meta.count_users() == 1
    got = stack.meta.get_user_by_username("owner")
    assert got is not None
    assert got.user_id == "u-owner"
    assert got.role == "owner"
    assert got.password_hash == "argon2-stub"
    assert stack.meta.get_user_by_username("ghost") is None
    stack.meta.update_user_password("u-owner", "argon2-rotated")
    assert stack.meta.get_user_by_username("owner").password_hash == "argon2-rotated"
    assert [user.user_id for user in stack.meta.list_users()] == ["u-owner"]


def test_create_owner_atomic_and_conflict(stack) -> None:
    """FR-6.1a exact-once at the port: create_owner writes the owner's user,
    default profile and audit row in ONE transaction, and a second call is the
    typed OwnerConflictError -- never a bare IntegrityError."""
    assert stack.meta.count_users() == 0
    stack.meta.create_owner(
        StoredUser(
            user_id="u-owner",
            username="owner",
            password_hash="argon2-stub",
            role="owner",
            created_at=100.0,
        ),
        StoredProfile(profile_id="default", display_name="owner", created_at=100.0),
        AuditEntry(actor="setup", action="owner_created", detail={"username": "owner"}, at=100.0),
    )
    assert stack.meta.count_users() == 1
    assert stack.meta.get_profile("default").display_name == "owner"
    page = stack.meta.audit_query(AuditFilter(action="owner_created"), Page(0, 50))
    assert page.total == 1

    with pytest.raises(OwnerConflictError):
        stack.meta.create_owner(
            StoredUser(
                user_id="u-owner-2",
                username="second",
                password_hash="argon2-stub",
                role="owner",
                created_at=101.0,
            ),
            StoredProfile(profile_id="default", display_name="second", created_at=101.0),
            AuditEntry(actor="setup", action="owner_created", detail={"username": "second"}, at=101.0),
        )
    # the rejected setup mutated nothing: no second user, profile, or audit row
    assert stack.meta.count_users() == 1
    assert stack.meta.audit_query(AuditFilter(action="owner_created"), Page(0, 50)).total == 1


def test_token_secret_hashed_at_rest_and_authenticates(stack) -> None:
    """issue_token hands back a bearer secret that is NEVER stored verbatim:
    the row keeps only its sha256 digest, and authenticate_token resolves the
    digest back to the live token."""
    stack.meta.upsert_profile(_pool_profile(stack))
    token = stack.meta.issue_token("u1", ("graph:read", "meta:read"))
    assert token.token_secret, "issue_token must return the one-shot bearer secret"
    row = raw_meta_row(stack, "tokens", "token_id", token.token_id)
    assert row != {}
    assert row["token_hash"]
    assert row["token_hash"] != token.token_secret  # plaintext never persisted
    found = stack.meta.authenticate_token(token.token_secret)
    assert found is not None
    assert found.token_id == token.token_id
    assert found.profile_id == "u1"
    assert tuple(found.scopes) == ("graph:read", "meta:read")
    assert stack.meta.authenticate_token("unknown-secret") is None


def test_authenticate_respects_revocation_and_expiry(stack) -> None:
    """A revoked or expired token stops authenticating (the gate relies on it)."""
    stack.meta.upsert_profile(_pool_profile(stack))
    token = stack.meta.issue_token("u1", ("graph:read",), expires_at=time.time() + 60.0)
    assert stack.meta.authenticate_token(token.token_secret) is not None
    stack.meta.revoke_token(token.token_id)
    assert stack.meta.authenticate_token(token.token_secret) is None
    expired = stack.meta.issue_token("u1", ("graph:read",), expires_at=time.time() - 1.0)
    assert stack.meta.authenticate_token(expired.token_secret) is None


def test_config_versioned_get_set_rollback(stack) -> None:
    v1 = stack.meta.set_config("theme", {"mode": "dark"})
    assert v1 == 1
    stack.meta.set_config("theme", {"mode": "light"})
    latest = stack.meta.get_config("theme")
    assert isinstance(latest, ConfigEntry)
    assert latest.version == 2
    assert latest.value == {"mode": "light"}
    assert stack.meta.get_config("theme", version=1).value == {"mode": "dark"}
    assert stack.meta.get_config("missing-key") is None

    stack.meta.rollback_config("theme", v1)
    rolled = stack.meta.get_config("theme")
    assert rolled.version == 3
    assert rolled.value == {"mode": "dark"}
    with pytest.raises(StorageError, match="has no version 99"):
        stack.meta.rollback_config("theme", 99)


def test_audit_append_and_query(stack) -> None:
    stack.meta.audit_append(AuditEntry(actor="alice", action="insert", detail={"n": 1}, at=100.0))
    stack.meta.audit_append(AuditEntry(actor="bob", action="read", detail={"n": 2}, at=200.0))
    page = stack.meta.audit_query(AuditFilter(actor="alice"), Page(0, 50))
    assert page.total == 1
    assert page.items[0].detail == {"n": 1}
    assert page.items[0].actor == "alice"
    both = stack.meta.audit_query(AuditFilter(since=0.0, until=250.0), Page(0, 50))
    assert both.total == 2


def test_audit_append_only_enforced_by_database(stack) -> None:
    """The driver refuses to mutate audit_log at the database level."""
    stack.meta.audit_append(AuditEntry(actor="alice", action="insert", at=100.0))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        stack.meta._conn.execute("UPDATE audit_log SET action = 'tampered'")


def test_dream_runs_roundtrip(stack) -> None:
    run_id = stack.meta.record_dream_run(
        DreamRun(
            run_id="run-1",
            session_id="s1",
            turn_range=TurnRange(start=1, end=3),
            model_id="claude",
            tokens=42,
            cost=0.0042,
            interrupted=True,
        )
    )
    assert run_id == "run-1"
    page = stack.meta.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50))
    assert page.total == 1
    run = page.items[0]
    assert run.turn_range == TurnRange(start=1, end=3)
    assert run.tokens == 42
    assert run.interrupted is True

    second = stack.meta.list_dream_runs(DreamRunFilter(interrupted=False), Page(0, 50))
    assert second.total == 0


def test_dream_run_model_update_records_resolved_model(stack) -> None:
    """F2: a run is registered at snapshot capture without a model; the
    per-run route resolution pins the model and records it on the run row."""
    stack.meta.record_dream_run(DreamRun(run_id="run-m1", session_id="s1", started_at=100.0))
    stack.meta.update_dream_run_model("run-m1", "kimi-k3")
    run = stack.meta.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50)).items[0]
    assert run.model_id == "kimi-k3"
    stack.meta.update_dream_run_model("run-m1", "deepseek-v4-flash")
    run = stack.meta.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50)).items[0]
    assert run.model_id == "deepseek-v4-flash"
    stack.meta.update_dream_run_model("no-such-run", "kimi-k3")  # unknown run: silent no-op


def test_dream_run_finish_completes_the_row(stack) -> None:
    """The dream log surface: a committed run completes with finish time and
    metered totals (the completion parity of the model pin at reflect start)."""
    stack.meta.record_dream_run(DreamRun(run_id="run-fin", session_id="s1", started_at=100.0))
    stack.meta.finish_dream_run("run-fin", finished_at=200.0, tokens=1234, cost=0.0, dropped_count=0)
    run = stack.meta.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50)).items[0]
    assert run.finished_at == 200.0
    assert run.tokens == 1234
    assert run.dropped_count == 0
    # re-finish overwrites cleanly; unknown run ids follow the model pin's
    # silent no-op contract
    stack.meta.finish_dream_run("run-fin", finished_at=300.0, tokens=200, cost=0.0, dropped_count=0)
    assert stack.meta.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50)).items[0].tokens == 200
    stack.meta.finish_dream_run("no-such-run", finished_at=400.0, tokens=1, cost=0.0, dropped_count=0)


def test_schema_version_and_migrate_forward_only(stack) -> None:
    """meta's head is v12 (frozen v1 schema + v3 profile_score_pool + v4
    dream_token_ledger + v6 identity users/token_hash + v7 profile archive
    flag + v8 reserved config.scope + v9 pool filed_points_total ledger +
    v11 error-event ledger + v12 provider fingerprint); migrate is idempotent and forward-only."""
    assert stack.meta.schema_version() == 12
    assert stack.meta.migrate() == 12
    assert stack.meta.migrate(target=1) == 12  # back-targeting is a no-op at head
    assert stack.meta.schema_version() == 12


def test_dream_token_ledger_atomic_increment(stack) -> None:
    """FR-2.5b port: add_token_usage is an atomic upsert-increment on the
    (profile_id, year_month) key; unknown keys read as zero and never span."""
    stack.meta.add_token_usage("u1", "2026-08", 100)
    stack.meta.add_token_usage("u1", "2026-08", 50)
    assert stack.meta.token_usage("u1", "2026-08") == 150
    assert stack.meta.token_usage("u1", "2026-07") == 0  # other month stays zero
    assert stack.meta.token_usage("ghost", "2026-08") == 0  # unknown profile stays zero


def test_dream_token_ledger_per_profile_isolation(stack) -> None:
    stack.meta.add_token_usage("a", "2026-08", 10)
    stack.meta.add_token_usage("b", "2026-08", 3)
    stack.meta.add_token_usage("a", "2026-09", 7)
    assert stack.meta.token_usage("a", "2026-08") == 10
    assert stack.meta.token_usage("b", "2026-08") == 3
    assert stack.meta.token_usage("a", "2026-09") == 7


def test_meta_stamp_helpers_used(stack) -> None:
    """The stamp helpers are exercised so ruff never prunes them from the suite."""
    prov = make_prov(session_id="s-meta")
    assert prov.session_id == "s-meta"


# ---------------------------------------------------------- error-event ledger (E1)


def test_error_event_append_and_query_profile_scoped(stack) -> None:
    """E1 ledger: append is a deterministic write seam; the read is profile-scoped
    and paginated. The evidence pointer references a source without asserting
    correctness."""
    stack.meta.append_error_event(
        ErrorEvent(
            profile_id="u1",
            signal_type=ErrorSignalType.USER_CORRECTION,
            observed_at=100.0,
            evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id="chunk-1"),
            session_id="s1",
        )
    )
    stack.meta.append_error_event(
        ErrorEvent(
            profile_id="u1",
            signal_type=ErrorSignalType.EVENT_OUTCOME,
            observed_at=200.0,
            evidence_ptr=EvidencePointer(kind=EvidenceKind.NODE, id="node-9"),
            turn_range=TurnRange(start=2, end=4),
        )
    )
    stack.meta.append_error_event(
        ErrorEvent(
            profile_id="u2",
            signal_type=ErrorSignalType.PUBLISHED,
            observed_at=150.0,
            evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id="s-u2"),
        )
    )

    page = stack.meta.query_error_events(ErrorEventFilter(profile_id="u1"), Page(0, 50))
    assert page.total == 2
    assert [e.evidence_ptr.id for e in page.items] == ["chunk-1", "node-9"]
    for event in page.items:
        assert event.profile_id == "u1"

    other = stack.meta.query_error_events(ErrorEventFilter(profile_id="u2"), Page(0, 50))
    assert other.total == 1
    assert other.items[0].evidence_ptr.kind is EvidenceKind.SESSION


def test_error_event_signal_type_filter_and_isolation(stack) -> None:
    stack.meta.append_error_event(
        ErrorEvent(
            profile_id="u1",
            signal_type=ErrorSignalType.USER_CORRECTION,
            observed_at=100.0,
            evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id="c1"),
        )
    )
    stack.meta.append_error_event(
        ErrorEvent(
            profile_id="u1",
            signal_type=ErrorSignalType.EVENT_OUTCOME,
            observed_at=200.0,
            evidence_ptr=EvidencePointer(kind=EvidenceKind.NODE, id="n1"),
        )
    )
    filtered = stack.meta.query_error_events(
        ErrorEventFilter(profile_id="u1", signal_type=ErrorSignalType.EVENT_OUTCOME), Page(0, 50)
    )
    assert filtered.total == 1
    assert filtered.items[0].evidence_ptr.kind is EvidenceKind.NODE


def test_error_event_append_only_enforced_by_db(stack) -> None:
    """The ledger is append-only at the database level: rows are never mutated
    or deleted (audit_log precedent)."""
    stack.meta.append_error_event(
        ErrorEvent(
            profile_id="u1",
            signal_type=ErrorSignalType.USER_CORRECTION,
            observed_at=100.0,
            evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id="c1"),
        )
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        stack.meta._conn.execute("UPDATE error_events SET signal_type = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        stack.meta._conn.execute("DELETE FROM error_events")
