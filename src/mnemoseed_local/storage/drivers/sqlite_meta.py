"""SQLite meta driver: profiles, tokens, score pool, config, audit, dream runs.

Every pool mutation (pool_add / pool_drain / advance_watermark) is a
transaction: the WAL journal makes concurrent writer waits safe (busy_timeout).
audit_log is append-only at the database level via BEFORE UPDATE/DELETE
triggers, not just by driver convention.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from collections.abc import Sequence as CSeq
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mnemoseed_local.config import CONFIG_DIR
from mnemoseed_local.storage.drivers._migrations import apply_migrations
from mnemoseed_local.storage.drivers._threadlocal import ThreadLocalConnections
from mnemoseed_local.storage.drivers._time import epoch_from_iso, iso8601_utc
from mnemoseed_local.storage.ports import (
    AuditEntry,
    AuditFilter,
    Capability,
    ConfigEntry,
    DreamRun,
    DreamRunFilter,
    DriverInfo,
    OwnerConflictError,
    Page,
    PageResult,
    PoolState,
    StorageError,
    StoredProfile,
    StoredUser,
    Token,
    TurnRange,
)
from mnemoseed_local.storage.registry import META_DRIVERS, register

_CAPABILITIES = frozenset({Capability.META_TRANSACTION, Capability.META_CONCURRENT_READERS})


@register(META_DRIVERS)
class SqliteMetaDriver:
    """MetaStore over a single SQLite file."""

    info = DriverInfo(
        name="sqlite_meta",
        capabilities=_CAPABILITIES,
        description="profiles/tokens/score-pool/config/audit/dream-runs over SQLite",
    )

    def __init__(self, path: str | os.PathLike[str] | None = None, **kwargs: Any) -> None:
        self.params: dict[str, Any] = kwargs
        self._path = Path(os.path.expanduser(str(path))) if path is not None else CONFIG_DIR / "meta.db"
        extra = kwargs.get("path")
        if extra is not None and path is None:
            self._path = Path(os.path.expanduser(str(extra)))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadLocalConnections(self._path)
        # The main-thread connection is opened eagerly here so migrations and
        # any direct ``driver._conn`` access (internals, tests) run on a live
        # handle; worker threads get their own handle on first use.
        apply_migrations(self._pool.get(), "meta")

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    @property
    def _conn(self) -> sqlite3.Connection:
        """The calling thread's connection (one handle per thread)."""
        return self._pool.get()

    async def close(self) -> None:
        self._pool.close_all()

    # ------------------------------------------------------------ score pool

    def pool_add(self, profile_id: str, points: float, turn_range: TurnRange) -> None:
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO profile_score_pool (profile_id, balance, watermark_start, "
                "watermark_end, last_event_start, last_event_end) "
                "VALUES (?, ?, 0, 0, ?, ?) "
                "ON CONFLICT(profile_id) DO UPDATE SET "
                "balance = balance + ?, last_event_start = ?, last_event_end = ?",
                (
                    profile_id,
                    points,
                    turn_range.start,
                    turn_range.end,
                    points,
                    turn_range.start,
                    turn_range.end,
                ),
            )

    def pool_credit(self, profile_id: str, balance: float, turn_range: TurnRange) -> None:
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO profile_score_pool (profile_id, balance, watermark_start, "
                "watermark_end, last_event_start, last_event_end) "
                "VALUES (?, ?, ?, ?, 0, 0) "
                "ON CONFLICT(profile_id) DO UPDATE SET "
                "balance = excluded.balance, watermark_start = excluded.watermark_start, "
                "watermark_end = excluded.watermark_end",
                (profile_id, balance, turn_range.start, turn_range.end),
            )

    def pool_drain(self, profile_id: str, turn_range: TurnRange) -> float:
        """Move the whole pending gauge into the lifetime ledger atomically.

        The read of the pending balance and its reset + filing happen inside one
        BEGIN IMMEDIATE transaction, so concurrent drainers serialize: exactly
        one of them wins the balance and files it.
        """
        with _transaction(self._conn):
            row = self._conn.execute(
                "SELECT balance FROM profile_score_pool WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            drained = float(row["balance"]) if row is not None else 0.0
            self._conn.execute(
                "INSERT INTO profile_score_pool (profile_id, balance, watermark_start, "
                "watermark_end, last_event_start, last_event_end, filed_points_total) "
                "VALUES (?, 0, 0, 0, ?, ?, ?) "
                "ON CONFLICT(profile_id) DO UPDATE SET "
                "balance = 0, last_event_start = excluded.last_event_start, "
                "last_event_end = excluded.last_event_end, "
                "filed_points_total = filed_points_total + excluded.filed_points_total",
                (
                    profile_id,
                    turn_range.start,
                    turn_range.end,
                    drained,
                ),
            )
            return drained

    def pool_state(self, profile_id: str) -> PoolState:
        """The profile's pending gauge plus its lifetime filed total."""
        row = self._conn.execute(
            "SELECT balance, watermark_start, watermark_end, filed_points_total "
            "FROM profile_score_pool WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        if row is None:
            return PoolState()
        # a row whose watermark never advanced still carries an honest gauge
        watermark = None
        if int(row["watermark_end"]) != 0:
            watermark = TurnRange(start=int(row["watermark_start"]), end=int(row["watermark_end"]))
        return PoolState(
            balance=float(row["balance"]),
            watermark=watermark,
            filed_points_total=float(row["filed_points_total"]),
        )

    def pool_states(self) -> dict[str, PoolState]:
        rows = self._conn.execute(
            "SELECT profile_id, balance, watermark_start, watermark_end, filed_points_total "
            "FROM profile_score_pool"
        ).fetchall()
        states: dict[str, PoolState] = {}
        for row in rows:
            watermark: TurnRange | None = None
            if int(row["watermark_end"]) != 0:
                watermark = TurnRange(start=int(row["watermark_start"]), end=int(row["watermark_end"]))
            states[str(row["profile_id"])] = PoolState(
                balance=float(row["balance"]),
                watermark=watermark,
                filed_points_total=float(row["filed_points_total"]),
            )
        return states

    def advance_watermark(self, profile_id: str, turn_range: TurnRange) -> None:
        current = self._conn.execute(
            "SELECT watermark_start, watermark_end FROM profile_score_pool WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        with _transaction(self._conn):
            if current is None or int(current["watermark_end"]) == 0:
                self._conn.execute(
                    "INSERT INTO profile_score_pool (profile_id, balance, watermark_start, "
                    "watermark_end, last_event_start, last_event_end) "
                    "VALUES (?, 0.0, ?, ?, 0, 0) "
                    "ON CONFLICT(profile_id) DO UPDATE SET watermark_start = excluded.watermark_start, "
                    "watermark_end = excluded.watermark_end",
                    (profile_id, turn_range.start, turn_range.end),
                )
                return
            start = int(current["watermark_start"])
            end = int(current["watermark_end"])
            new_start, new_end = _merge_watermark((start, end), (turn_range.start, turn_range.end))
            self._conn.execute(
                "UPDATE profile_score_pool SET watermark_start = ?, watermark_end = ? WHERE profile_id = ?",
                (new_start, new_end, profile_id),
            )

    # ------------------------------------------------------------ profiles

    def upsert_profile(self, profile: StoredProfile) -> None:
        created = profile.created_at if profile.created_at else time.time()
        self._conn.execute(
            "INSERT INTO profiles (profile_id, display_name, created_at, archived) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(profile_id) DO UPDATE SET display_name = excluded.display_name",
            (profile.profile_id, profile.display_name, iso8601_utc(created), int(profile.archived)),
        )

    def create_profile(self, profile: StoredProfile) -> bool:
        """Insert-only creation: one transaction serializes concurrent
        duplicate creates (the BEGIN IMMEDIATE create_owner precedent), so the
        loser re-reads an existing row and returns False instead of overwriting
        it; a UNIQUE-violation backstop maps to the same False."""
        with _transaction(self._conn):
            exists = self._conn.execute(
                "SELECT 1 FROM profiles WHERE profile_id = ?", (profile.profile_id,)
            ).fetchone()
            if exists is not None:
                return False
            created = profile.created_at if profile.created_at else time.time()
            try:
                self._conn.execute(
                    "INSERT INTO profiles (profile_id, display_name, created_at, archived) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        profile.profile_id,
                        profile.display_name,
                        iso8601_utc(created),
                        int(profile.archived),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def get_profile(self, profile_id: str) -> StoredProfile | None:
        row = self._conn.execute(
            "SELECT profile_id, display_name, created_at, archived FROM profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredProfile(
            profile_id=str(row["profile_id"]),
            display_name=str(row["display_name"]),
            created_at=epoch_from_iso(str(row["created_at"])),
            archived=bool(int(row["archived"])),
        )

    def delete_profile(self, profile_id: str) -> None:
        # tokens cascade via FK
        self._conn.execute("DELETE FROM profiles WHERE profile_id = ?", (profile_id,))

    def archive_profile(self, profile_id: str, archived: bool) -> None:
        with _transaction(self._conn):
            cursor = self._conn.execute(
                "UPDATE profiles SET archived = ? WHERE profile_id = ?",
                (int(archived), profile_id),
            )
            if cursor.rowcount == 0:
                raise StorageError(f"cannot archive unknown profile {profile_id!r}")

    def list_profiles(self) -> list[StoredProfile]:
        rows = self._conn.execute(
            "SELECT profile_id, display_name, created_at, archived FROM profiles ORDER BY created_at"
        ).fetchall()
        return [
            StoredProfile(
                profile_id=str(r["profile_id"]),
                display_name=str(r["display_name"]),
                created_at=epoch_from_iso(str(r["created_at"])),
                archived=bool(int(r["archived"])),
            )
            for r in rows
        ]

    # ------------------------------------------------------------ tokens

    def issue_token(
        self,
        profile_id: str,
        scopes: CSeq[str],
        expires_at: float | None = None,
    ) -> Token:
        """Issue a profile token whose bearer secret is returned exactly once.

        Only the sha256 digest is persisted (``tokens.token_hash``); the raw
        secret survives solely in the returned Token.token_secret field, so a
        later table read can never recover a usable credential (PRD-06).
        """
        token_id = uuid.uuid4().hex
        secret = secrets.token_urlsafe(32)
        issued_at = time.time()
        with _transaction(self._conn):
            profile = self._conn.execute(
                "SELECT profile_id FROM profiles WHERE profile_id = ?", (profile_id,)
            ).fetchone()
            if profile is None:
                raise StorageError(f"cannot issue token for unknown profile {profile_id!r}")
            self._conn.execute(
                "INSERT INTO tokens (token_id, profile_id, scopes, issued_at, expires_at, "
                "revoked, token_hash) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (
                    token_id,
                    profile_id,
                    json.dumps(list(scopes)),
                    iso8601_utc(issued_at),
                    iso8601_utc(expires_at) if expires_at is not None else None,
                    _token_hash(secret),
                ),
            )
        return Token(
            token_id=token_id,
            profile_id=profile_id,
            scopes=tuple(scopes),
            issued_at=issued_at,
            expires_at=expires_at,
            revoked=False,
            token_secret=secret,
        )

    def revoke_token(self, token_id: str) -> None:
        self._conn.execute("UPDATE tokens SET revoked = 1 WHERE token_id = ?", (token_id,))

    def authenticate_token(self, secret: str) -> Token | None:
        row = self._conn.execute(
            "SELECT token_id, profile_id, scopes, issued_at, expires_at, revoked "
            "FROM tokens WHERE token_hash = ? AND revoked = 0 "
            "AND (expires_at IS NULL OR expires_at >= ?)",
            (_token_hash(secret), iso8601_utc(time.time())),
        ).fetchone()
        if row is None:
            return None
        return _decode_token(row)

    # ------------------------------------------------------------ users (FR-6.1a)

    def create_user(self, user: StoredUser) -> None:
        self._conn.execute(
            "INSERT INTO users (user_id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                user.user_id,
                user.username,
                user.password_hash,
                user.role,
                iso8601_utc(user.created_at if user.created_at else time.time()),
            ),
        )

    def create_owner(self, owner: StoredUser, profile: StoredProfile, audit: AuditEntry) -> None:
        """Create the single owner + default profile + audit in one transaction.

        BEGIN IMMEDIATE serializes concurrent writers: the losing setup blocks
        here until the winner commits, then re-reads the owner count and raises
        the typed ``OwnerConflictError``. The username UNIQUE constraint is a
        final backstop, translated to the same typed conflict.
        """
        with _transaction(self._conn):
            row = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
            if row is not None and int(row[0]) > 0:
                raise OwnerConflictError("an owner account already exists")
            try:
                self._conn.execute(
                    "INSERT INTO users (user_id, username, password_hash, role, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        owner.user_id,
                        owner.username,
                        owner.password_hash,
                        owner.role,
                        iso8601_utc(owner.created_at if owner.created_at else time.time()),
                    ),
                )
                created = profile.created_at if profile.created_at else time.time()
                self._conn.execute(
                    "INSERT INTO profiles (profile_id, display_name, created_at) VALUES (?, ?, ?)",
                    (profile.profile_id, profile.display_name, iso8601_utc(created)),
                )
                self._conn.execute(
                    "INSERT INTO audit_log (actor, action, detail, at) VALUES (?, ?, ?, ?)",
                    (
                        audit.actor,
                        audit.action,
                        json.dumps(audit.detail),
                        iso8601_utc(audit.at if audit.at else time.time()),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OwnerConflictError("an owner account already exists") from exc

    def get_user_by_username(self, username: str) -> StoredUser | None:
        row = self._conn.execute(
            "SELECT user_id, username, password_hash, role, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        return _decode_user(row)

    def count_users(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0]) if row is not None else 0

    def list_users(self) -> list[StoredUser]:
        rows = self._conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [_decode_user(row) for row in rows]

    def update_user_password(self, user_id: str, password_hash: str) -> None:
        self._conn.execute(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (password_hash, user_id),
        )

    # ------------------------------------------------------------ config

    def get_config(self, key: str, version: int | None = None) -> ConfigEntry | None:
        if version is None:
            row = self._conn.execute(
                "SELECT key, value, version, updated_at FROM config "
                "WHERE key = ? ORDER BY version DESC LIMIT 1",
                (key,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT key, value, version, updated_at FROM config WHERE key = ? AND version = ?",
                (key, version),
            ).fetchone()
        if row is None:
            return None
        return ConfigEntry(
            key=str(row["key"]),
            value=json.loads(str(row["value"])),
            version=int(row["version"]),
            updated_at=epoch_from_iso(str(row["updated_at"])),
        )

    def set_config(self, key: str, value: dict[str, Any]) -> int:
        with _transaction(self._conn):
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM config WHERE key = ?", (key,)
            ).fetchone()
            version = int(row["v"]) + 1 if row is not None else 1
            self._conn.execute(
                "INSERT INTO config (key, value, version, updated_at) VALUES (?, ?, ?, ?)",
                (key, json.dumps(value), version, iso8601_utc(time.time())),
            )
        return version

    def rollback_config(self, key: str, version: int) -> None:
        row = self._conn.execute(
            "SELECT value FROM config WHERE key = ? AND version = ?", (key, version)
        ).fetchone()
        if row is None:
            raise StorageError(f"config key {key!r} has no version {version}")
        with _transaction(self._conn):
            current = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM config WHERE key = ?", (key,)
            ).fetchone()
            next_version = int(current["v"]) + 1 if current is not None else 1
            self._conn.execute(
                "INSERT INTO config (key, value, version, updated_at) VALUES (?, ?, ?, ?)",
                (key, str(row["value"]), next_version, iso8601_utc(time.time())),
            )

    # ------------------------------------------------------------ audit

    def audit_append(self, entry: AuditEntry) -> None:
        self._conn.execute(
            "INSERT INTO audit_log (actor, action, detail, at) VALUES (?, ?, ?, ?)",
            (
                entry.actor,
                entry.action,
                json.dumps(entry.detail),
                iso8601_utc(entry.at if entry.at else time.time()),
            ),
        )

    def audit_query(self, filter: AuditFilter, page: Page) -> PageResult[AuditEntry]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter.actor is not None:
            clauses.append("actor = ?")
            params.append(filter.actor)
        if filter.action is not None:
            clauses.append("action = ?")
            params.append(filter.action)
        if filter.since is not None:
            clauses.append("at >= ?")
            params.append(iso8601_utc(filter.since))
        if filter.until is not None:
            clauses.append("at <= ?")
            params.append(iso8601_utc(filter.until))
        where = " AND ".join(clauses) if clauses else "1 = 1"
        count_row = self._conn.execute(f"SELECT COUNT(*) FROM audit_log WHERE {where}", params).fetchone()
        total = int(count_row[0]) if count_row is not None else 0
        rows = self._conn.execute(
            f"SELECT id, actor, action, detail, at FROM audit_log WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
            [*params, page.limit, page.offset],
        ).fetchall()
        items = [_decode_audit(r) for r in rows]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    # ------------------------------------------------------------ dream runs

    def record_dream_run(self, run: DreamRun) -> str:
        run_id = run.run_id if run.run_id else uuid.uuid4().hex
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO dream_runs (run_id, session_id, turn_start, turn_end, model_id, "
                "started_at, finished_at, tokens, cost, interrupted, dropped_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    run.session_id,
                    run.turn_range.start if run.turn_range is not None else None,
                    run.turn_range.end if run.turn_range is not None else None,
                    run.model_id,
                    iso8601_utc(run.started_at if run.started_at else time.time()),
                    iso8601_utc(run.finished_at) if run.finished_at is not None else None,
                    run.tokens,
                    run.cost,
                    int(run.interrupted),
                    run.dropped_count,
                ),
            )
        return run_id

    def list_dream_runs(self, filter: DreamRunFilter, page: Page) -> PageResult[DreamRun]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter.session_id is not None:
            clauses.append("session_id = ?")
            params.append(filter.session_id)
        if filter.since is not None:
            clauses.append("started_at >= ?")
            params.append(iso8601_utc(filter.since))
        if filter.until is not None:
            clauses.append("started_at <= ?")
            params.append(iso8601_utc(filter.until))
        if filter.interrupted is not None:
            clauses.append("interrupted = ?")
            params.append(int(filter.interrupted))
        where = " AND ".join(clauses) if clauses else "1 = 1"
        count_row = self._conn.execute(f"SELECT COUNT(*) FROM dream_runs WHERE {where}", params).fetchone()
        total = int(count_row[0]) if count_row is not None else 0
        rows = self._conn.execute(
            f"SELECT * FROM dream_runs WHERE {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            [*params, page.limit, page.offset],
        ).fetchall()
        items = [_decode_dream_run(r) for r in rows]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    def update_dream_run_model(self, run_id: str, model_id: str) -> None:
        """F2: record the model pinned at reflect run start (the run row is
        registered at snapshot capture, before route resolution)."""
        self._conn.execute(
            "UPDATE dream_runs SET model_id = ? WHERE run_id = ?",
            (model_id, run_id),
        )

    def finish_dream_run(
        self,
        run_id: str,
        *,
        finished_at: float,
        tokens: int,
        cost: float,
        dropped_count: int,
    ) -> None:
        """Complete a run row at merge commit (finish time + metered totals)."""
        self._conn.execute(
            "UPDATE dream_runs SET finished_at = ?, tokens = ?, cost = ?, dropped_count = ? WHERE run_id = ?",
            (iso8601_utc(finished_at), int(tokens), float(cost), int(dropped_count), run_id),
        )

    # ------------------------------------------------------------ dream token ledger (FR-2.5b)

    def add_token_usage(self, profile_id: str, year_month: str, tokens: int) -> None:
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO dream_token_ledger (profile_id, year_month, tokens) VALUES (?, ?, ?) "
                "ON CONFLICT(profile_id, year_month) DO UPDATE SET "
                "tokens = tokens + excluded.tokens",
                (profile_id, year_month, tokens),
            )

    def token_usage(self, profile_id: str, year_month: str) -> int:
        row = self._conn.execute(
            "SELECT tokens FROM dream_token_ledger WHERE profile_id = ? AND year_month = ?",
            (profile_id, year_month),
        ).fetchone()
        return int(row["tokens"]) if row is not None else 0

    # ------------------------------------------------------------ migrations

    def schema_version(self) -> int:
        from mnemoseed_local.storage.drivers._migrations import current_schema_version

        return current_schema_version(self._conn, "meta")

    def migrate(self, target: int | None = None) -> int:
        return apply_migrations(self._conn, "meta", target)


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


def _merge_watermark(current: tuple[int, int], incoming: tuple[int, int]) -> tuple[int, int]:
    """Monotonic forward merge of watermark ranges; gaps raise."""
    cur_start, cur_end = current
    new_start, new_end = incoming
    if cur_end == 0:
        return new_start, new_end
    if new_start > cur_end + 1:
        raise ValueError(
            f"watermark advance jumps over unprocessed turns "
            f"(current end {cur_end}, incoming start {new_start})"
        )
    return min(cur_start, new_start), max(cur_end, new_end)


def _decode_audit(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        actor=str(row["actor"]),
        action=str(row["action"]),
        detail=json.loads(str(row["detail"])),
        at=epoch_from_iso(str(row["at"])),
        id=int(row["id"]),
    )


def _decode_dream_run(row: sqlite3.Row) -> DreamRun:
    return DreamRun(
        run_id=str(row["run_id"]),
        session_id=row["session_id"],
        turn_range=_turn_range_or_none(row["turn_start"], row["turn_end"]),
        model_id=str(row["model_id"]) if row["model_id"] is not None else "",
        started_at=epoch_from_iso(str(row["started_at"])),
        finished_at=_maybe_epoch(row["finished_at"]),
        tokens=int(row["tokens"]),
        cost=float(row["cost"]),
        interrupted=bool(int(row["interrupted"])),
        dropped_count=int(row["dropped_count"]),
    )


def _turn_range_or_none(start: Any, end: Any) -> TurnRange | None:
    if start is None or end is None:
        return None
    return TurnRange(start=int(start), end=int(end))


def _maybe_epoch(value: Any) -> float | None:
    return epoch_from_iso(str(value)) if value is not None else None


def _token_hash(secret: str) -> str:
    """Deterministic sha256 digest of a bearer secret (never reversible)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _decode_token(row: sqlite3.Row) -> Token:
    return Token(
        token_id=str(row["token_id"]),
        profile_id=str(row["profile_id"]),
        scopes=tuple(json.loads(str(row["scopes"]))) if row["scopes"] else (),
        issued_at=epoch_from_iso(str(row["issued_at"])),
        expires_at=_maybe_epoch(row["expires_at"]),
        revoked=bool(int(row["revoked"])),
    )


def _decode_user(row: sqlite3.Row) -> StoredUser:
    return StoredUser(
        user_id=str(row["user_id"]),
        username=str(row["username"]),
        password_hash=str(row["password_hash"]),
        role=str(row["role"]),
        created_at=epoch_from_iso(str(row["created_at"])),
    )
