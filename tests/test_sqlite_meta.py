"""SqliteMetaDriver behavior: score pool + watermark atomicity, profiles,
tokens, versioned config + rollback, append-only audit, dream runs, migrations.
"""

import asyncio
import sqlite3
import threading
import time

import pytest

from mnemoseed_local.storage.drivers._migrations import apply_migrations, current_schema_version
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import (
    AuditEntry,
    AuditFilter,
    Capability,
    DreamRun,
    DreamRunFilter,
    Page,
    PoolState,
    StorageError,
    StoredProfile,
    StoredUser,
    TurnRange,
)
from mnemoseed_local.storage.registry import META_DRIVERS, register


@pytest.fixture(autouse=True)
def _ensure_registered():
    """Re-register the driver if test_registry's autouse clearing ran."""
    if not META_DRIVERS.contains("sqlite_meta"):
        register(META_DRIVERS)(SqliteMetaDriver)
    yield


@pytest.fixture
def driver(tmp_path):
    db = SqliteMetaDriver(path=tmp_path / "meta.db")
    yield db
    asyncio.run(db.close())


def test_registered_in_shared_registry():
    assert META_DRIVERS.contains("sqlite_meta")


def test_capabilities_full_set():
    caps = SqliteMetaDriver.info.capabilities
    assert Capability.META_TRANSACTION in caps
    assert Capability.META_CONCURRENT_READERS in caps
    assert len(caps) == 2


def test_pragmas_wal_and_foreign_keys(tmp_path):
    db = SqliteMetaDriver(path=tmp_path / "pragma.db")
    try:
        assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        asyncio.run(db.close())


# ---------------------------------------------------------------- score pool


def test_pool_add_and_state(driver):
    assert driver.pool_state("u1") == PoolState(balance=0.0)
    driver.pool_add("u1", 10.0, TurnRange(start=0, end=4))
    state = driver.pool_state("u1")
    assert state.balance == 10.0
    driver.advance_watermark("u1", TurnRange(start=0, end=4))
    assert driver.pool_state("u1").watermark == TurnRange(start=0, end=4)
    driver.pool_add("u1", -3.0, TurnRange(start=5, end=6))
    assert driver.pool_state("u1").balance == 7.0


def test_pool_and_watermark_are_per_profile(driver):
    driver.pool_add("a", 10.0, TurnRange(start=0, end=4))
    driver.pool_add("b", 3.0, TurnRange(start=0, end=4))
    assert driver.pool_state("a").balance == 10.0
    assert driver.pool_state("b").balance == 3.0
    driver.advance_watermark("a", TurnRange(start=0, end=4))
    assert driver.pool_state("a").watermark == TurnRange(start=0, end=4)
    assert driver.pool_state("b").watermark is None
    assert driver.pool_state("ghost") == PoolState(balance=0.0)


def test_pool_credit_sets_row_absolutely(driver):
    driver.pool_credit("u1", 5.0, TurnRange(start=0, end=2))
    state = driver.pool_state("u1")
    assert state.balance == 5.0
    assert state.watermark == TurnRange(start=0, end=2)
    driver.pool_credit("u1", 3.0, TurnRange(start=3, end=5))
    state = driver.pool_state("u1")
    assert state.balance == 3.0
    assert state.watermark == TurnRange(start=3, end=5)


def test_pool_states_returns_all_rows(driver):
    driver.pool_credit("u1", 4.0, TurnRange(start=0, end=1))
    driver.pool_add("u2", 7.0, TurnRange(start=2, end=3))
    states = driver.pool_states()
    assert set(states) == {"u1", "u2"}
    assert states["u1"].balance == 4.0
    assert states["u1"].watermark == TurnRange(start=0, end=1)  # pool_credit carries the span
    assert states["u2"].balance == 7.0
    assert states["u2"].watermark is None  # pool_add alone advances no watermark


def test_advance_watermark_monotonic_merge(driver):
    driver.advance_watermark("u1", TurnRange(start=0, end=4))
    driver.advance_watermark("u1", TurnRange(start=5, end=8))
    state = driver.pool_state("u1")
    assert state.watermark == TurnRange(start=0, end=8)
    # overlapping/backward advance stays a superset
    driver.advance_watermark("u1", TurnRange(start=2, end=6))
    assert driver.pool_state("u1").watermark == TurnRange(start=0, end=8)


def test_advance_watermark_gap_raises(driver):
    driver.advance_watermark("u1", TurnRange(start=0, end=4))
    with pytest.raises(ValueError, match="watermark advance jumps over"):
        driver.advance_watermark("u1", TurnRange(start=7, end=9))


def test_advance_watermark_on_empty_pool(driver):
    driver.advance_watermark("u1", TurnRange(start=3, end=9))
    assert driver.pool_state("u1").watermark == TurnRange(start=3, end=9)


def test_pool_add_atomic_under_concurrent_writers(tmp_path):
    """Six driver instances racing pool_add must land exactly 30.0."""
    path = tmp_path / "concurrent.db"
    seed = SqliteMetaDriver(path=path)
    seed.pool_add("race", 0.0, TurnRange(start=0, end=0))  # create the profile row
    asyncio.run(seed.close())

    add_count = 5
    writer_count = 6
    barrier = threading.Barrier(writer_count)

    def worker(index: int) -> None:
        db = SqliteMetaDriver(path=path)
        barrier.wait()
        try:
            # let pools of items catch up too
            for j in range(add_count):
                db.pool_add("race", 1.0, TurnRange(start=index * add_count + j, end=index * add_count + j))
        finally:
            asyncio.run(db.close())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(writer_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = SqliteMetaDriver(path=path)
    try:
        assert check.pool_state("race").balance == writer_count * add_count
    finally:
        asyncio.run(check.close())


# ---------------------------------------------------------------- profiles / tokens


def test_profile_crud_and_list(driver):
    p1 = StoredProfile(profile_id="u1", display_name="Uma", created_at=100.0)
    p2 = StoredProfile(profile_id="u2", display_name="Ben", created_at=200.0)
    driver.upsert_profile(p1)
    driver.upsert_profile(p2)
    assert [p.profile_id for p in driver.list_profiles()] == ["u1", "u2"]
    got = driver.get_profile("u1")
    assert got.display_name == "Uma"
    assert got.created_at == 100.0
    driver.upsert_profile(StoredProfile(profile_id="u1", display_name="Umami", created_at=100.0))
    assert driver.get_profile("u1").display_name == "Umami"
    driver.delete_profile("u2")
    assert driver.get_profile("u2") is None


def test_issue_revoke_token(driver):
    driver.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma"))
    token = driver.issue_token("u1", ["graph:read", "meta:write"], expires_at=time.time() + 60.0)
    assert token.profile_id == "u1"
    assert tuple(token.scopes) == ("graph:read", "meta:write")
    assert token.revoked is False
    driver.revoke_token(token.token_id)
    row = driver._conn.execute("SELECT revoked FROM tokens WHERE token_id = ?", (token.token_id,)).fetchone()
    assert row is not None and int(row["revoked"]) == 1


def test_issue_token_unknown_profile_raises(driver):
    with pytest.raises(StorageError, match="unknown profile"):
        driver.issue_token("ghost", ("graph:read",))


def test_delete_profile_cascades_tokens(driver):
    driver.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma"))
    token = driver.issue_token("u1", ("graph:read",))
    driver.delete_profile("u1")
    row = driver._conn.execute("SELECT 1 FROM tokens WHERE token_id = ?", (token.token_id,)).fetchone()
    assert row is None


def test_users_crud_and_password_rotation(driver):
    assert driver.count_users() == 0
    driver.create_user(
        StoredUser(
            user_id="u-owner",
            username="owner",
            password_hash="argon2-stub",
            role="owner",
            created_at=100.0,
        )
    )
    assert driver.count_users() == 1
    got = driver.get_user_by_username("owner")
    assert got is not None
    assert got.user_id == "u-owner"
    assert got.role == "owner"
    assert got.password_hash == "argon2-stub"
    assert driver.get_user_by_username("ghost") is None
    driver.update_user_password("u-owner", "argon2-rotated")
    assert driver.get_user_by_username("owner").password_hash == "argon2-rotated"
    assert [user.user_id for user in driver.list_users()] == ["u-owner"]


def test_token_secret_hashed_authenticates_and_revokes(driver):
    """The bearer secret is stored as its sha256 digest only; authenticate
    resolves the digest back to the live (unrevoked, unexpired) token."""
    driver.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma"))
    token = driver.issue_token("u1", ("graph:read",))
    assert token.token_secret, "issue_token must return the one-shot bearer secret"
    row = driver._conn.execute(
        "SELECT token_hash FROM tokens WHERE token_id = ?", (token.token_id,)
    ).fetchone()
    assert row is not None and str(row["token_hash"])
    assert str(row["token_hash"]) != token.token_secret  # plaintext never persisted
    found = driver.authenticate_token(token.token_secret)
    assert found is not None
    assert found.token_id == token.token_id
    assert found.profile_id == "u1"
    assert driver.authenticate_token("unknown-secret") is None
    driver.revoke_token(token.token_id)
    assert driver.authenticate_token(token.token_secret) is None


def test_authenticate_respects_expiry(driver):
    driver.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma"))
    expired = driver.issue_token("u1", ("graph:read",), expires_at=time.time() - 1.0)
    assert driver.authenticate_token(expired.token_secret) is None


# ---------------------------------------------------------------- config


def test_config_versions_and_rollback(driver):
    v1 = driver.set_config("theme", {"mode": "dark"})
    assert v1 == 1
    v2 = driver.set_config("theme", {"mode": "light"})
    assert v2 == 2
    assert driver.get_config("theme").version == 2
    assert driver.get_config("theme").value == {"mode": "light"}
    assert driver.get_config("theme", version=1).value == {"mode": "dark"}

    driver.rollback_config("theme", 1)
    entry = driver.get_config("theme")
    assert entry.version == 3
    assert entry.value == {"mode": "dark"}
    with pytest.raises(StorageError, match="has no version 99"):
        driver.rollback_config("theme", 99)


def test_config_isolation_between_keys(driver):
    driver.set_config("theme", {"mode": "dark"})
    driver.set_config("audio", {"volume": 0.5})
    assert driver.get_config("theme").version == 1
    assert driver.get_config("audio").version == 1


# ---------------------------------------------------------------- audit


def test_audit_append_and_query(driver):
    driver.audit_append(AuditEntry(actor="alice", action="insert", detail={"n": 1}, at=100.0))
    driver.audit_append(AuditEntry(actor="alice", action="delete", detail={"n": 2}, at=200.0))
    driver.audit_append(AuditEntry(actor="bob", action="insert", detail={"n": 3}, at=300.0))

    all_rows = driver.audit_query(AuditFilter(), Page(0, 50))
    assert all_rows.total == 3
    assert [e.actor for e in all_rows.items] == ["alice", "alice", "bob"]

    by_actor = driver.audit_query(AuditFilter(actor="alice"), Page(0, 50))
    assert by_actor.total == 2

    by_action = driver.audit_query(AuditFilter(action="insert"), Page(0, 50))
    assert by_action.total == 2

    window = driver.audit_query(AuditFilter(since=150.0, until=250.0), Page(0, 50))
    assert window.total == 1
    assert window.items[0].detail == {"n": 2}

    page = driver.audit_query(AuditFilter(), Page(offset=1, limit=1))
    assert page.total == 3
    assert len(page.items) == 1


def test_audit_append_only_enforced_by_db(driver):
    driver.audit_append(AuditEntry(actor="alice", action="insert", at=100.0))
    driver.audit_append(AuditEntry(actor="bob", action="delete", at=200.0))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        driver._conn.execute("UPDATE audit_log SET action = 'tampered' WHERE actor = 'alice'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        driver._conn.execute("DELETE FROM audit_log")


# ---------------------------------------------------------------- dream runs


def test_dream_run_record_and_list(driver):
    run_id = driver.record_dream_run(
        DreamRun(
            session_id="s1",
            turn_range=TurnRange(start=1, end=3),
            model_id="claude",
            started_at=1000.0,
            finished_at=2000.0,
            tokens=42,
            cost=0.0042,
            interrupted=True,
            dropped_count=1,
        )
    )
    assert len(run_id) == 32
    rows = driver.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50))
    assert rows.total == 1
    run = rows.items[0]
    assert run.session_id == "s1"
    assert run.turn_range == TurnRange(start=1, end=3)
    assert run.tokens == 42
    assert run.cost == 0.0042
    assert run.interrupted is True
    assert run.dropped_count == 1


def test_dream_run_list_orders_and_filters(driver):
    driver.record_dream_run(DreamRun(run_id="r1", session_id="s1", started_at=100.0, interrupted=False))
    driver.record_dream_run(DreamRun(run_id="r2", session_id="s2", started_at=200.0, interrupted=True))
    driver.record_dream_run(DreamRun(run_id="r3", session_id="s1", started_at=300.0, interrupted=False))

    all_rows = driver.list_dream_runs(DreamRunFilter(), Page(0, 50))
    assert [r.run_id for r in all_rows.items] == ["r3", "r2", "r1"]  # started_at DESC

    session = driver.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50))
    assert {r.run_id for r in session.items} == {"r1", "r3"}
    assert session.total == 2

    interrupted = driver.list_dream_runs(DreamRunFilter(interrupted=True), Page(0, 50))
    assert {r.run_id for r in interrupted.items} == {"r2"}


def test_dream_run_model_recorded_after_resolution(driver):
    """F2: a run registered without a model (snapshot capture precedes route
    resolution) gets the pinned model written back once reflect resolves the
    route at run start; a later resolution overwrites it."""
    driver.record_dream_run(DreamRun(run_id="run-a", session_id="s1", started_at=100.0))
    assert driver.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50)).items[0].model_id == ""
    driver.update_dream_run_model("run-a", "kimi-k3")
    runs = driver.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50))
    assert runs.items[0].model_id == "kimi-k3"
    driver.update_dream_run_model("run-a", "deepseek-v4-flash")
    assert driver.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50)).items[0].model_id == (
        "deepseek-v4-flash"
    )
    # an unknown run is a silent no-op (the run is always registered first)
    driver.update_dream_run_model("no-such-run", "kimi-k3")


# ---------------------------------------------------------------- migrations


def test_meta_migration_preserves_data_and_is_forward_only(tmp_path):
    """A meta-only file at global version 1 auto-upgrades to head idempotently."""
    path = tmp_path / "migrate-meta.db"
    conn = sqlite3.connect(path, isolation_level=None)
    apply_migrations(conn, "meta", target=1)
    assert current_schema_version(conn, "meta") == 1
    conn.execute(
        "INSERT INTO profiles (profile_id, display_name, created_at) VALUES (?, ?, ?)",
        ("u1", "survivor", "2026-01-01T00:00:00.000Z"),
    )
    conn.commit()
    conn.close()

    driver = SqliteMetaDriver(path=path)
    try:
        # graph-tagged v2 is not applied to a meta-only file; v3 adds the
        # per-profile score pool, v4 the dream token ledger, v6 the identity
        # users table + hashed token column, v7 the profile archive flag and
        # v8 the reserved config.scope column, so meta lands at 8 (the legacy
        # singleton score_pool is neither dropped nor migrated).
        assert driver.schema_version() == 8
        got = driver.get_profile("u1")
        assert got is not None
        assert got.display_name == "survivor"
        driver.migrate()  # idempotent re-run
        assert driver.schema_version() == 8
    finally:
        asyncio.run(driver.close())


def test_schema_version_equals_latest_new_install(tmp_path):
    db = SqliteMetaDriver(path=tmp_path / "fresh.db")
    try:
        assert db.schema_version() == db.migrate()
        assert db.schema_version() == 8
        # a profile row written after init survives a migrate() no-op
        db.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma"))
        db.migrate()
        assert db.get_profile("u1").display_name == "Uma"
    finally:
        asyncio.run(db.close())
