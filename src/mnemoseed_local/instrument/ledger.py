"""Append-only instrument ledger: event store, cohort registry, and continuity manifest.

The ledger is the instrument's own provenance record. It stores the immutable
application-side event columns, the registered cohort identities, and nothing
else. Every write path is append-only; the schema itself refuses updates and
deletes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from mnemoseed_local.storage.drivers._time import iso8601_utc

SCHEMA_VERSION = 1
OWNER_GATE_DENOMINATOR = 30

DIAG_CODES: frozenset[str] = frozenset(
    {
        "invalid_now",
        "unbound_now",
        "invalid_cutoff",
        "non_finite",
        "unparsable",
        "future_timestamp",
        "clock_backward",
        "clock_precohort",
        "clock_monotonic_inconsistent",
        "clock_unbound",
        "clock_future_unverifiable",
        "ledger_commit_unknown",
        "prior_run_unclosed",
        "date_derivation_mismatch",
        "calendar_eligible_mismatch",
        "ledger_idempotence_conflict",
        "binding_incomplete",
        "writer_lock_held",
        "invocation_receipt_unavailable",
        "ledger_missing",
        "cohort_identity_mismatch",
        "store_binding_mismatch",
        "ledger_corrupt",
        "duplicate_suppressed",
        "revoked_no_survivor",
        "seed_clock_not_calendar",
    }
)

REVOCATION_REASON_CODES: frozenset[str] = frozenset(
    {
        "wrong_store_binding",
        "clock_evidence_invalid",
        "double_capture",
        "operator_withdrawal",
    }
)

COHORT_KINDS: frozenset[str] = frozenset({"daily_cohort", "repair_control"})
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_started",
        "run_closed",
        "attempt_started",
        "attempt_terminal",
        "instrument_gap",
        "correction",
        "revocation",
    }
)
OBSERVATION_KINDS: frozenset[str] = frozenset({"collect", "replay_check", "recovery"})
OUTCOMES: frozenset[str] = frozenset(
    {"observed_ok", "failed_read", "clock_invalid", "unbound_clock", "aborted"}
)
CLOCK_SOURCES: frozenset[str] = frozenset({"os_utc_wall", "seed_fixture", "unbound"})
ATTEMPT_LIFECYCLE_EVENT_TYPES: frozenset[str] = frozenset(
    {"attempt_started", "attempt_terminal", "instrument_gap"}
)
OBSERVATION_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"attempt_terminal", "instrument_gap"})
RUN_EVENT_TYPES: frozenset[str] = frozenset({"run_started", "run_closed"})
TARGETED_EVENT_TYPES: frozenset[str] = frozenset({"correction", "revocation"})

GUARD_TRIGGERS: frozenset[str] = frozenset(
    {"events_no_update", "events_no_delete", "cohorts_no_update", "cohorts_no_delete"}
)

MANIFEST_KEYS: frozenset[str] = frozenset(
    {
        "manifest_version",
        "cohort_id",
        "kind",
        "seed_manifest_hash",
        "schema_hash",
        "instrument_hash",
        "config_hash",
        "control_id",
        "cohort_created_at_raw",
        "frozen_at_iso",
    }
)
CONFIG_KEYS: frozenset[str] = frozenset(
    {"home", "denominator", "kind", "cohort_id", "control_id", "repair_limit"}
)
ALLOWED_REPAIR_LIMITS: frozenset[int] = frozenset({100, 99})

FROZEN_SOURCE_PATHS: frozenset[str] = frozenset(
    {
        "storage/drivers/sqlite_graph.py",
        "storage/drivers/sqlite_meta.py",
        "storage/drivers/_migrations.py",
        "storage/drivers/_time.py",
        "storage/ports.py",
        "dream/repair.py",
        "schema/graph.py",
    }
)

SAMPLE_FIELDS: tuple[str, ...] = (
    "observation_kind",
    "outcome",
    "clock_source",
    "capture_pre_raw",
    "capture_post_raw",
    "monotonic_delta_ns",
    "collected_at_raw",
    "collected_at_iso",
    "utc_date",
)

_UTC_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

LEDGER_DDL: str = """
CREATE TABLE IF NOT EXISTS cohorts (
    cohort_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('daily_cohort', 'repair_control')),
    cohort_created_at_raw TEXT NOT NULL,
    seed_manifest_hash TEXT,
    schema_hash TEXT NOT NULL,
    instrument_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    source_commit TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    control_id TEXT,
    fixture_seed_clock_raw TEXT
);

CREATE TABLE IF NOT EXISTS events (
    ledger_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type IN ('run_started', 'run_closed',
        'attempt_started', 'attempt_terminal', 'instrument_gap', 'correction', 'revocation')),
    cohort_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    recovers_attempt_id TEXT,
    observation_kind TEXT CHECK (observation_kind IN ('collect', 'replay_check', 'recovery')),
    outcome TEXT CHECK (outcome IN ('observed_ok', 'failed_read', 'clock_invalid',
        'unbound_clock', 'aborted')),
    diag_code TEXT,
    clock_source TEXT CHECK (clock_source IN ('os_utc_wall', 'seed_fixture', 'unbound')),
    capture_pre_raw TEXT,
    capture_post_raw TEXT,
    monotonic_delta_ns INTEGER,
    collected_at_raw TEXT,
    collected_at_iso TEXT,
    utc_date TEXT CHECK (utc_date IS NULL
        OR utc_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    calendar_eligible INTEGER CHECK (calendar_eligible IN (0, 1)),
    target_event_id TEXT NOT NULL DEFAULT '',
    reason_code TEXT,
    payload_json TEXT NOT NULL,
    appended_at_raw TEXT NOT NULL,
    UNIQUE (run_id, attempt_id, event_type, target_event_id),
    FOREIGN KEY (cohort_id) REFERENCES cohorts(cohort_id)
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN
    SELECT RAISE(ABORT, 'events rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN
    SELECT RAISE(ABORT, 'events rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS cohorts_no_update BEFORE UPDATE ON cohorts BEGIN
    SELECT RAISE(ABORT, 'cohorts rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS cohorts_no_delete BEFORE DELETE ON cohorts BEGIN
    SELECT RAISE(ABORT, 'cohorts rows are append-only');
END;
"""

_EVENT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "event_type",
    "cohort_id",
    "run_id",
    "attempt_id",
    "recovers_attempt_id",
    "observation_kind",
    "outcome",
    "diag_code",
    "clock_source",
    "capture_pre_raw",
    "capture_post_raw",
    "monotonic_delta_ns",
    "collected_at_raw",
    "collected_at_iso",
    "utc_date",
    "calendar_eligible",
    "target_event_id",
    "reason_code",
    "payload",
    "appended_at_raw",
)

_EVENT_SELECT = (
    "ledger_seq, event_id, event_type, cohort_id, run_id, attempt_id, recovers_attempt_id, "
    "observation_kind, outcome, diag_code, clock_source, capture_pre_raw, capture_post_raw, "
    "monotonic_delta_ns, collected_at_raw, collected_at_iso, utc_date, calendar_eligible, "
    "target_event_id, reason_code, payload_json, appended_at_raw"
)

_COHORT_SELECT = (
    "cohort_id, kind, cohort_created_at_raw, seed_manifest_hash, schema_hash, instrument_hash, "
    "config_hash, config_json, source_commit, source_hash, control_id, fixture_seed_clock_raw"
)

_EVENT_INSERT = (
    "INSERT INTO events (event_id, event_type, cohort_id, run_id, attempt_id, "
    "recovers_attempt_id, observation_kind, outcome, diag_code, clock_source, capture_pre_raw, "
    "capture_post_raw, monotonic_delta_ns, collected_at_raw, collected_at_iso, utc_date, "
    "calendar_eligible, target_event_id, reason_code, payload_json, appended_at_raw) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_COHORT_INSERT = (
    "INSERT INTO cohorts (cohort_id, kind, cohort_created_at_raw, seed_manifest_hash, schema_hash, "
    "instrument_hash, config_hash, config_json, source_commit, source_hash, control_id, "
    "fixture_seed_clock_raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_HELD_LOCK_PATHS: set[str] = set()


class InstrumentError(Exception):
    """Base error of the instrument; every failure carries a diagnostic code."""

    diag_code = "instrument_error"

    def __init__(
        self,
        detail: dict[str, Any] | None = None,
        *,
        diag_code: str | None = None,
        invalid_codes: tuple[str, ...] = (),
    ) -> None:
        self.diag_code = diag_code or type(self).diag_code
        self.detail: dict[str, Any] = dict(detail or {})
        self.invalid_codes = invalid_codes
        super().__init__(self.diag_code)


class WriterLockHeld(InstrumentError):
    """The single writer lock is held elsewhere."""

    diag_code = "writer_lock_held"


class InvocationReceiptUnavailable(InstrumentError):
    """The out-of-ledger invocation receipt could not be written."""

    diag_code = "invocation_receipt_unavailable"


class LedgerMissingError(InstrumentError):
    """The ledger, its manifest, or the expected cohort row is missing."""

    diag_code = "ledger_missing"


class CohortIdentityMismatchError(InstrumentError):
    """A cohort identity, its link, or its configuration evidence does not match."""

    diag_code = "cohort_identity_mismatch"


class LedgerCorruptError(InstrumentError):
    """The ledger fails its integrity or guard-trigger checks."""

    diag_code = "ledger_corrupt"


class LedgerIdempotenceConflictError(InstrumentError):
    """An idempotence key repeats with different application-side content."""

    diag_code = "ledger_idempotence_conflict"


class BindingIncompleteError(InstrumentError):
    """A required binding of an observation is missing or invalid."""

    diag_code = "binding_incomplete"


class EventRejectedError(InstrumentError):
    """An event is refused by the static preflight or the dynamic state checks."""

    diag_code = "binding_incomplete"


class ClockValueError(InstrumentError):
    """A clock value is not finite or cannot be parsed."""

    diag_code = "non_finite"


@dataclass(frozen=True)
class InstrumentPaths:
    """Every explicit path of one isolated instrument home."""

    home: Path
    ledger_db: Path
    writer_lock: Path
    invocations_dir: Path
    manifest_path: Path
    cohort_graph_db: Path
    cohort_meta_db: Path
    control_graph_db: Path
    control_meta_db: Path


@dataclass(frozen=True)
class CohortRecord:
    """One registered cohort identity with its frozen bindings."""

    cohort_id: str
    kind: str
    cohort_created_at_raw: str
    seed_manifest_hash: str | None
    schema_hash: str
    instrument_hash: str
    config_hash: str
    config_json: str
    source_commit: str
    source_hash: str
    control_id: str | None
    fixture_seed_clock_raw: str | None


@dataclass(frozen=True)
class LedgerEvent:
    """The application-side immutable columns of one ledger row."""

    event_id: str
    event_type: str
    cohort_id: str
    run_id: str
    attempt_id: str
    recovers_attempt_id: str | None
    observation_kind: str | None
    outcome: str | None
    diag_code: str | None
    clock_source: str | None
    capture_pre_raw: str | None
    capture_post_raw: str | None
    monotonic_delta_ns: int | None
    collected_at_raw: str | None
    collected_at_iso: str | None
    utc_date: str | None
    calendar_eligible: int | None
    target_event_id: str
    reason_code: str | None
    payload: dict[str, Any]
    appended_at_raw: str


@dataclass(frozen=True)
class AppendedEvent:
    """One ledger row with its ordering authority."""

    ledger_seq: int
    event: LedgerEvent
    was_new: bool


@dataclass(frozen=True)
class RunState:
    """The derived lifecycle state of one run."""

    run_id: str
    observation_attempts: tuple[str, ...]
    terminal_attempts: tuple[str, ...]
    gap_attempts: tuple[str, ...]
    open_attempts: tuple[str, ...]
    failed_outcomes: tuple[str, ...]
    correction_rows: int
    revocation_rows: int
    closable: bool
    close_block_reason: str | None


@dataclass(frozen=True)
class _AttemptLedger:
    """The attempt bookkeeping shared by the closability predicate and the run state."""

    attempts: tuple[str, ...]
    terminal: tuple[str, ...]
    gaps: tuple[str, ...]
    open_attempts: tuple[str, ...]
    failed_outcomes: tuple[str, ...]
    corrections: int
    revocations: int


def canonical_json(obj: Any) -> str:
    """Serialize one object to its canonical JSON text."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_node_json(node: Any) -> str:
    """Serialize a product node to canonical JSON for content hashing."""
    return canonical_json(node.model_dump(mode="json"))


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hexadecimal sha256 digest of the given bytes."""
    return hashlib.sha256(data).hexdigest()


def schema_hash() -> str:
    """Return the digest of the frozen ledger DDL."""
    return sha256_hex(LEDGER_DDL.encode("utf-8"))


def compute_instrument_hash(package_dir: Path | None = None) -> tuple[str, list[dict[str, str]]]:
    """Return the digest and the file list of the instrument package."""
    root = Path(package_dir) if package_dir is not None else Path(__file__).resolve().parent
    files = [
        {"path": f"instrument/{item.name}", "sha256": sha256_hex(item.read_bytes())}
        for item in sorted(root.glob("*.py"))
    ]
    digest = sha256_hex(canonical_json({"files": files}).encode("utf-8"))
    return digest, files


def compute_source_hash(
    *, source_commit: str, source_manifest: dict[str, str]
) -> tuple[str, list[dict[str, str]]]:
    """Return the declared digest of the frozen baseline manifest.

    The manifest is a declared baseline listing; the instrument never reads or
    re-hashes the product files themselves.
    """
    if set(source_manifest) != FROZEN_SOURCE_PATHS:
        raise CohortIdentityMismatchError(
            {
                "reason": "source_manifest_path_mismatch",
                "expected": sorted(FROZEN_SOURCE_PATHS),
                "actual": sorted(source_manifest),
            }
        )
    for path, digest in source_manifest.items():
        _validate_source_path(path)
        if not _SHA256_PATTERN.fullmatch(digest):
            raise CohortIdentityMismatchError({"reason": "source_manifest_digest_shape", "path": path})
    files = [{"path": path, "sha256": source_manifest[path]} for path in sorted(source_manifest)]
    digest = sha256_hex(canonical_json({"commit": source_commit, "files": source_manifest}).encode("utf-8"))
    return digest, files


def compute_config_hash(config_json: str) -> str:
    """Return the digest of the canonical configuration evidence text."""
    return sha256_hex(config_json.encode("utf-8"))


def compute_cohort_id(
    *,
    kind: str,
    cohort_created_at_iso: str,
    seed_manifest_hash: str | None,
    schema_hash: str,
    instrument_hash: str,
) -> str:
    """Return the cohort identity derived from its frozen creation inputs."""
    return sha256_hex(
        canonical_json(
            {
                "cohort_created_at_iso": cohort_created_at_iso,
                "instrument_hash": instrument_hash,
                "kind": kind,
                "schema_hash": schema_hash,
                "seed_manifest_hash": seed_manifest_hash,
            }
        ).encode("utf-8")
    )


def build_config_evidence(
    *,
    home: str,
    kind: str,
    cohort_id: str,
    control_id: str | None,
    repair_limit: int | None,
    denominator: int = OWNER_GATE_DENOMINATOR,
) -> str:
    """Return the canonical configuration evidence text of one cohort."""
    if isinstance(denominator, bool) or not isinstance(denominator, int):
        raise CohortIdentityMismatchError({"reason": "denominator_not_an_integer"})
    if denominator != OWNER_GATE_DENOMINATOR:
        raise CohortIdentityMismatchError({"reason": "denominator_not_frozen", "denominator": denominator})
    if repair_limit is not None:
        if isinstance(repair_limit, bool) or not isinstance(repair_limit, int):
            raise CohortIdentityMismatchError({"reason": "repair_limit_not_an_integer"})
        if repair_limit not in ALLOWED_REPAIR_LIMITS:
            raise CohortIdentityMismatchError(
                {"reason": "repair_limit_not_allowed", "repair_limit": repair_limit}
            )
    return canonical_json(
        {
            "home": str(Path(home).resolve()),
            "denominator": denominator,
            "kind": kind,
            "cohort_id": cohort_id,
            "control_id": control_id,
            "repair_limit": repair_limit,
        }
    )


def verify_config_evidence(row: CohortRecord, *, paths: InstrumentPaths) -> None:
    """Check that the stored configuration evidence matches the row and the home."""
    if compute_config_hash(row.config_json) != row.config_hash:
        raise CohortIdentityMismatchError({"reason": "config_hash_mismatch", "cohort_id": row.cohort_id})
    try:
        parsed = json.loads(row.config_json)
    except ValueError as exc:
        raise CohortIdentityMismatchError(
            {"reason": "config_json_unparsable", "cohort_id": row.cohort_id}
        ) from exc
    if not isinstance(parsed, dict):
        raise CohortIdentityMismatchError({"reason": "config_json_not_an_object", "cohort_id": row.cohort_id})
    if set(parsed) != CONFIG_KEYS:
        raise CohortIdentityMismatchError(
            {
                "reason": "config_key_mismatch",
                "cohort_id": row.cohort_id,
                "unexpected": sorted(set(parsed) - CONFIG_KEYS),
                "missing": sorted(CONFIG_KEYS - set(parsed)),
            }
        )
    try:
        canonical = canonical_json(parsed)
    except (TypeError, ValueError) as exc:
        raise CohortIdentityMismatchError(
            {"reason": "config_json_not_serializable", "cohort_id": row.cohort_id}
        ) from exc
    if canonical != row.config_json:
        raise CohortIdentityMismatchError({"reason": "config_json_not_canonical", "cohort_id": row.cohort_id})
    if parsed["home"] != str(paths.home.resolve()):
        raise CohortIdentityMismatchError({"reason": "config_home_mismatch", "cohort_id": row.cohort_id})
    denominator = parsed["denominator"]
    if isinstance(denominator, bool) or not isinstance(denominator, int):
        raise CohortIdentityMismatchError(
            {"reason": "config_denominator_not_an_integer", "cohort_id": row.cohort_id}
        )
    if denominator != OWNER_GATE_DENOMINATOR:
        raise CohortIdentityMismatchError(
            {"reason": "config_denominator_not_frozen", "cohort_id": row.cohort_id}
        )
    for name, value in (
        ("kind", row.kind),
        ("cohort_id", row.cohort_id),
        ("control_id", row.control_id),
    ):
        if parsed[name] != value:
            raise CohortIdentityMismatchError(
                {"reason": "config_column_mismatch", "cohort_id": row.cohort_id, "field": name}
            )
    repair_limit = parsed["repair_limit"]
    if repair_limit is not None:
        if isinstance(repair_limit, bool) or not isinstance(repair_limit, int):
            raise CohortIdentityMismatchError(
                {"reason": "config_repair_limit_not_an_integer", "cohort_id": row.cohort_id}
            )
        if repair_limit not in ALLOWED_REPAIR_LIMITS:
            raise CohortIdentityMismatchError(
                {"reason": "config_repair_limit_not_allowed", "cohort_id": row.cohort_id}
            )


def resolve_paths(home: str | Path) -> InstrumentPaths:
    """Return the explicit instrument paths of one existing home directory."""
    root = Path(home).expanduser().resolve()
    if not root.is_dir():
        raise CohortIdentityMismatchError(
            {"reason": "home_not_a_directory", "home": str(root)},
            diag_code="store_binding_mismatch",
        )
    instrument = root / "instrument"
    return InstrumentPaths(
        home=root,
        ledger_db=instrument / "ledger.db",
        writer_lock=instrument / "writer.lock",
        invocations_dir=instrument / "invocations",
        manifest_path=instrument / "expected_cohort.json",
        cohort_graph_db=root / "cohort" / "graph.db",
        cohort_meta_db=root / "cohort" / "meta.db",
        control_graph_db=root / "control" / "graph.db",
        control_meta_db=root / "control" / "meta.db",
    )


def create_ledger_schema(paths: InstrumentPaths) -> None:
    """Create the ledger database with the frozen schema and no cohort row."""
    if paths.ledger_db.exists():
        raise CohortIdentityMismatchError(
            {"reason": "partial_initialization", "artifacts": [str(paths.ledger_db)]}
        )
    paths.ledger_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(paths.ledger_db, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(LEDGER_DDL)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        connection.close()


def open_ledger(paths: InstrumentPaths) -> LedgerHandle:
    """Open an existing ledger and check its schema, integrity, and guard triggers."""
    if not paths.ledger_db.exists():
        raise LedgerMissingError({"reason": "ledger_db_missing", "path": str(paths.ledger_db)})
    connection = sqlite3.connect(paths.ledger_db, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise LedgerMissingError(
                {
                    "reason": "schema_version_mismatch",
                    "expected": SCHEMA_VERSION,
                    "actual": version,
                }
            )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise LedgerCorruptError({"reason": "integrity_check_failed", "result": integrity})
        triggers = {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        missing = sorted(GUARD_TRIGGERS - triggers)
        if missing:
            raise LedgerCorruptError({"reason": "guard_triggers_missing", "missing": missing})
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise LedgerCorruptError({"reason": "sqlite_error", "error": str(exc)}) from exc
    except BaseException:
        connection.close()
        raise
    return LedgerHandle(connection)


class LedgerHandle:
    """An open ledger connection with the single append path."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def close(self) -> None:
        """Close the ledger connection."""
        self.connection.close()

    def append(self, event: LedgerEvent) -> AppendedEvent:
        """Append one event, or return the already recorded row of an identical retry."""
        _static_preflight(event)
        recorded = self._find_by_key(event)
        if recorded is not None:
            differing = _differing_fields(event, recorded.event)
            if differing:
                raise LedgerIdempotenceConflictError(
                    {
                        "reason": "application_content_differs",
                        "differing_fields": differing,
                        "run_id": event.run_id,
                        "attempt_id": event.attempt_id,
                        "event_type": event.event_type,
                        "target_event_id": event.target_event_id,
                    }
                )
            return AppendedEvent(recorded.ledger_seq, recorded.event, False)
        self._validate_state(event)
        return self._insert(event)

    def _find_by_key(self, event: LedgerEvent) -> AppendedEvent | None:
        row = self.connection.execute(
            f"SELECT {_EVENT_SELECT} FROM events WHERE run_id = ? AND attempt_id = ? "
            "AND event_type = ? AND target_event_id = ?",
            (event.run_id, event.attempt_id, event.event_type, event.target_event_id),
        ).fetchone()
        if row is None:
            return None
        return AppendedEvent(int(row[0]), _event_from_row(row), False)

    def _find_by_event_id(self, event_id: str) -> LedgerEvent | None:
        row = self.connection.execute(
            f"SELECT {_EVENT_SELECT} FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        return _event_from_row(row)

    def _cohort_exists(self, cohort_id: str) -> bool:
        row = self.connection.execute("SELECT 1 FROM cohorts WHERE cohort_id = ?", (cohort_id,)).fetchone()
        return row is not None

    def _run_started_exists(self, cohort_id: str, run_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM events WHERE cohort_id = ? AND run_id = ? AND event_type = 'run_started'",
            (cohort_id, run_id),
        ).fetchone()
        return row is not None

    def _attempt_started_exists(self, cohort_id: str, run_id: str, attempt_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM events WHERE cohort_id = ? AND run_id = ? AND attempt_id = ? "
            "AND event_type = 'attempt_started'",
            (cohort_id, run_id, attempt_id),
        ).fetchone()
        return row is not None

    def _target_row_count(self, cohort_id: str, event_type: str, target_event_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM events WHERE cohort_id = ? AND event_type = ? AND target_event_id = ?",
            (cohort_id, event_type, target_event_id),
        ).fetchone()
        return int(row[0])

    def _validate_state(self, event: LedgerEvent) -> None:
        if not self._cohort_exists(event.cohort_id):
            raise EventRejectedError(
                {
                    "stage": "state",
                    "check": "cohort_missing",
                    "cohort_id": event.cohort_id,
                }
            )
        if event.event_type in TARGETED_EVENT_TYPES:
            self._validate_target(event)
        elif event.event_type == "attempt_started":
            if not self._run_started_exists(event.cohort_id, event.run_id):
                raise EventRejectedError(
                    {
                        "stage": "state",
                        "check": "run_started_missing",
                        "run_id": event.run_id,
                    }
                )
        elif event.event_type == "attempt_terminal":
            self._validate_terminal_linkage(event)
        elif event.event_type == "run_closed":
            self._validate_run_closable(event)

    def _validate_target(self, event: LedgerEvent) -> None:
        target = self._find_by_event_id(event.target_event_id)
        if target is None or target.cohort_id != event.cohort_id or target.event_type != "attempt_terminal":
            raise EventRejectedError(
                {
                    "stage": "state",
                    "check": "target_missing",
                    "target_event_id": event.target_event_id,
                }
            )
        if target.attempt_id == event.attempt_id:
            raise EventRejectedError({"stage": "state", "check": "target_attempt_reused"})
        if event.recovers_attempt_id is not None:
            raise EventRejectedError({"stage": "state", "check": "recovers_attempt_id_present"})
        if event.event_type == "revocation" and event.reason_code not in REVOCATION_REASON_CODES:
            raise EventRejectedError(
                {
                    "stage": "state",
                    "check": "reason_code_unlisted",
                    "reason_code": event.reason_code,
                }
            )
        if self._target_row_count(event.cohort_id, event.event_type, event.target_event_id):
            raise LedgerIdempotenceConflictError(
                {
                    "reason": "second_row_on_one_target",
                    "event_type": event.event_type,
                    "target_event_id": event.target_event_id,
                }
            )

    def _validate_terminal_linkage(self, event: LedgerEvent) -> None:
        if event.observation_kind == "collect" and not self._attempt_started_exists(
            event.cohort_id, event.run_id, event.attempt_id
        ):
            raise EventRejectedError(
                {
                    "stage": "state",
                    "check": "attempt_started_missing",
                    "run_id": event.run_id,
                    "attempt_id": event.attempt_id,
                }
            )

    def _validate_run_closable(self, event: LedgerEvent) -> None:
        if not self._run_started_exists(event.cohort_id, event.run_id):
            raise EventRejectedError(
                {"stage": "state", "check": "run_started_missing", "run_id": event.run_id}
            )
        events = read_run_events(self, cohort_id=event.cohort_id, run_id=event.run_id)
        closable, reason = evaluate_run_closability(events)
        if not closable:
            raise EventRejectedError(
                {
                    "stage": "state",
                    "check": "run_not_closable",
                    "reason": reason,
                    "run_id": event.run_id,
                }
            )

    def _insert(self, event: LedgerEvent) -> AppendedEvent:
        payload_json = canonical_json(event.payload)
        cursor: sqlite3.Cursor | None = None
        try:
            with _immediate_transaction(self.connection):
                cursor = self.connection.execute(
                    _EVENT_INSERT,
                    (
                        event.event_id,
                        event.event_type,
                        event.cohort_id,
                        event.run_id,
                        event.attempt_id,
                        event.recovers_attempt_id,
                        event.observation_kind,
                        event.outcome,
                        event.diag_code,
                        event.clock_source,
                        event.capture_pre_raw,
                        event.capture_post_raw,
                        event.monotonic_delta_ns,
                        event.collected_at_raw,
                        event.collected_at_iso,
                        event.utc_date,
                        event.calendar_eligible,
                        event.target_event_id,
                        event.reason_code,
                        payload_json,
                        event.appended_at_raw,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if self._find_by_event_id(event.event_id) is not None:
                raise LedgerIdempotenceConflictError(
                    {"reason": "event_id_conflict", "event_id": event.event_id}
                ) from exc
            raise EventRejectedError(
                {"stage": "insert", "reason": "constraint_violation", "event_type": event.event_type}
            ) from exc
        if cursor is None or cursor.lastrowid is None:
            raise EventRejectedError({"stage": "insert", "reason": "missing_row_id"})
        return AppendedEvent(int(cursor.lastrowid), event, True)


def register_cohort(ledger: LedgerHandle, cohort: CohortRecord) -> CohortRecord:
    """Register one cohort identity; an identical row is a no-op."""
    if cohort.kind not in COHORT_KINDS:
        raise CohortIdentityMismatchError({"reason": "unknown_cohort_kind", "kind": cohort.kind})
    existing = get_cohort(ledger, cohort_id=cohort.cohort_id)
    if existing is not None:
        if existing == cohort:
            return existing
        raise CohortIdentityMismatchError({"reason": "cohort_row_conflict", "cohort_id": cohort.cohort_id})
    try:
        with _immediate_transaction(ledger.connection):
            ledger.connection.execute(
                _COHORT_INSERT,
                (
                    cohort.cohort_id,
                    cohort.kind,
                    cohort.cohort_created_at_raw,
                    cohort.seed_manifest_hash,
                    cohort.schema_hash,
                    cohort.instrument_hash,
                    cohort.config_hash,
                    cohort.config_json,
                    cohort.source_commit,
                    cohort.source_hash,
                    cohort.control_id,
                    cohort.fixture_seed_clock_raw,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise CohortIdentityMismatchError(
            {"reason": "cohort_row_conflict", "cohort_id": cohort.cohort_id}
        ) from exc
    return cohort


def get_cohort(ledger: LedgerHandle, *, cohort_id: str) -> CohortRecord | None:
    """Return the registered cohort of one identity, or None."""
    row = ledger.connection.execute(
        f"SELECT {_COHORT_SELECT} FROM cohorts WHERE cohort_id = ?", (cohort_id,)
    ).fetchone()
    if row is None:
        return None
    return _cohort_from_row(row)


def list_cohorts(ledger: LedgerHandle) -> tuple[CohortRecord, ...]:
    """Return every registered cohort of the ledger."""
    rows = ledger.connection.execute(f"SELECT {_COHORT_SELECT} FROM cohorts ORDER BY cohort_id").fetchall()
    return tuple(_cohort_from_row(row) for row in rows)


def find_cohort_by_control_id(ledger: LedgerHandle, *, control_id: str) -> CohortRecord | None:
    """Return the unique repair control cohort of one business control id."""
    rows = ledger.connection.execute(
        f"SELECT {_COHORT_SELECT} FROM cohorts WHERE kind = 'repair_control' AND control_id = ?",
        (control_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise CohortIdentityMismatchError({"reason": "duplicate_control_rows", "control_id": control_id})
    return _cohort_from_row(rows[0])


def read_events(
    ledger: LedgerHandle, *, cohort_id: str | None = None, event_type: str | None = None
) -> list[AppendedEvent]:
    """Return the ledger rows in append order, optionally filtered."""
    clauses: list[str] = []
    params: list[Any] = []
    if cohort_id is not None:
        clauses.append("cohort_id = ?")
        params.append(cohort_id)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    query = f"SELECT {_EVENT_SELECT} FROM events"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY ledger_seq"
    rows = ledger.connection.execute(query, params).fetchall()
    return [AppendedEvent(int(row[0]), _event_from_row(row), False) for row in rows]


def read_run_events(ledger: LedgerHandle, *, cohort_id: str, run_id: str) -> tuple[LedgerEvent, ...]:
    """Return the events of one run in append order."""
    rows = ledger.connection.execute(
        f"SELECT {_EVENT_SELECT} FROM events WHERE cohort_id = ? AND run_id = ? ORDER BY ledger_seq",
        (cohort_id, run_id),
    ).fetchall()
    return tuple(_event_from_row(row) for row in rows)


def max_prior_collected_raw(ledger: LedgerHandle, *, cohort_id: str) -> str | None:
    """Return the latest collected raw instant of a cohort, failed attempts included."""
    rows = ledger.connection.execute(
        "SELECT collected_at_raw FROM events WHERE cohort_id = ? AND collected_at_raw IS NOT NULL",
        (cohort_id,),
    ).fetchall()
    best_raw: str | None = None
    best_value: float | None = None
    for row in rows:
        raw = str(row[0])
        value = float.fromhex(raw)
        if best_value is None or value > best_value:
            best_value = value
            best_raw = raw
    return best_raw


def evaluate_run_closability(events: Sequence[LedgerEvent]) -> tuple[bool, str | None]:
    """Return whether a run may be closed and the first blocking reason."""
    collected = _collect_attempts(events)
    if not collected.attempts:
        return (False, "no_attempt")
    if collected.open_attempts:
        return (False, "open_attempt")
    if collected.gaps:
        return (False, "gap_present")
    if collected.failed_outcomes:
        return (False, "failed_outcome")
    return (True, None)


def read_run_state(ledger: LedgerHandle, *, cohort_id: str, run_id: str) -> RunState:
    """Return the derived lifecycle state of one run."""
    events = read_run_events(ledger, cohort_id=cohort_id, run_id=run_id)
    collected = _collect_attempts(events)
    closable, reason = evaluate_run_closability(events)
    return RunState(
        run_id=run_id,
        observation_attempts=collected.attempts,
        terminal_attempts=collected.terminal,
        gap_attempts=collected.gaps,
        open_attempts=collected.open_attempts,
        failed_outcomes=collected.failed_outcomes,
        correction_rows=collected.corrections,
        revocation_rows=collected.revocations,
        closable=closable,
        close_block_reason=reason,
    )


def find_prior_unclosed_run_ids(ledger: LedgerHandle, *, cohort_id: str) -> tuple[str, ...]:
    """Return the runs of a cohort that were started and never closed."""
    started = ledger.connection.execute(
        "SELECT run_id FROM events WHERE cohort_id = ? AND event_type = 'run_started' "
        "GROUP BY run_id ORDER BY MIN(ledger_seq)",
        (cohort_id,),
    ).fetchall()
    closed = {
        str(row[0])
        for row in ledger.connection.execute(
            "SELECT DISTINCT run_id FROM events WHERE cohort_id = ? AND event_type = 'run_closed'",
            (cohort_id,),
        ).fetchall()
    }
    return tuple(str(row[0]) for row in started if str(row[0]) not in closed)


def read_manifest(paths: InstrumentPaths) -> dict[str, Any]:
    """Return the frozen continuity manifest document of the home."""
    if not paths.manifest_path.exists():
        raise LedgerMissingError({"reason": "manifest_missing", "path": str(paths.manifest_path)})
    try:
        document = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerMissingError({"reason": "manifest_unreadable", "path": str(paths.manifest_path)}) from exc
    if not isinstance(document, dict):
        raise LedgerMissingError({"reason": "manifest_not_an_object"})
    return document


def write_manifest(
    paths: InstrumentPaths, *, cohort: CohortRecord, control_cohort_id: str | None = None
) -> Path:
    """Write the frozen continuity manifest of the primary cohort."""
    control_id = cohort.control_id if control_cohort_id is None else control_cohort_id
    if control_id != cohort.control_id:
        raise CohortIdentityMismatchError(
            {
                "reason": "control_link_mismatch",
                "manifest_control_id": control_id,
                "row_control_id": cohort.control_id,
            }
        )
    document = {
        "manifest_version": 1,
        "cohort_id": cohort.cohort_id,
        "kind": cohort.kind,
        "seed_manifest_hash": cohort.seed_manifest_hash,
        "schema_hash": cohort.schema_hash,
        "instrument_hash": cohort.instrument_hash,
        "config_hash": cohort.config_hash,
        "control_id": control_id,
        "cohort_created_at_raw": cohort.cohort_created_at_raw,
        "frozen_at_iso": iso8601_utc(time.time()),
    }
    paths.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    paths.manifest_path.write_text(canonical_json(document), encoding="utf-8")
    return paths.manifest_path


def check_cohort_continuity(paths: InstrumentPaths, ledger: LedgerHandle) -> CohortRecord:
    """Check the manifest, the primary cohort row, and the linked control row."""
    manifest = read_manifest(paths)
    if manifest.get("manifest_version") != 1:
        raise LedgerMissingError(
            {
                "reason": "manifest_version_mismatch",
                "manifest_version": manifest.get("manifest_version"),
            }
        )
    if set(manifest) != MANIFEST_KEYS:
        raise LedgerMissingError(
            {
                "reason": "manifest_key_mismatch",
                "unexpected": sorted(set(manifest) - MANIFEST_KEYS),
                "missing": sorted(MANIFEST_KEYS - set(manifest)),
            }
        )
    cohort_id = manifest["cohort_id"]
    row = get_cohort(ledger, cohort_id=cohort_id)
    if row is None:
        raise LedgerMissingError({"reason": "cohort_row_missing", "cohort_id": cohort_id})
    if row.cohort_created_at_raw != manifest["cohort_created_at_raw"]:
        raise LedgerMissingError({"reason": "cohort_created_at_raw_mismatch"})
    for name in ("kind", "seed_manifest_hash", "schema_hash", "instrument_hash", "config_hash"):
        if getattr(row, name) != manifest[name]:
            raise LedgerMissingError({"reason": "cohort_identity_mismatch", "field": name})
    if row.control_id != manifest["control_id"]:
        raise LedgerMissingError({"reason": "cohort_identity_mismatch", "field": "control_id"})
    rederived = _rederive_cohort_id(row)
    if rederived != row.cohort_id or rederived != manifest["cohort_id"]:
        raise LedgerMissingError({"reason": "cohort_id_rederivation_mismatch", "rederived": rederived})
    try:
        verify_config_evidence(row, paths=paths)
    except CohortIdentityMismatchError as exc:
        raise LedgerMissingError({"reason": "config_evidence_mismatch", "detail": exc.detail}) from exc
    if row.control_id is not None:
        _check_linked_control(paths, ledger, row, manifest)
    return row


def _check_linked_control(
    paths: InstrumentPaths,
    ledger: LedgerHandle,
    row: CohortRecord,
    manifest: dict[str, Any],
) -> None:
    control_id = row.control_id
    if control_id is None:
        raise LedgerMissingError({"reason": "linked_control_id_missing", "cohort_id": row.cohort_id})
    linked = find_cohort_by_control_id(ledger, control_id=control_id)
    if linked is None:
        raise LedgerMissingError({"reason": "linked_control_missing", "control_id": control_id})
    if linked.kind != "repair_control" or linked.control_id != manifest["control_id"]:
        raise LedgerMissingError({"reason": "linked_control_mismatch", "control_id": control_id})
    for name in ("schema_hash", "instrument_hash", "source_commit", "source_hash"):
        if getattr(linked, name) != getattr(row, name):
            raise LedgerMissingError({"reason": "linked_control_mismatch", "field": name})
    try:
        verify_config_evidence(linked, paths=paths)
    except CohortIdentityMismatchError as exc:
        raise LedgerMissingError({"reason": "linked_config_evidence_mismatch", "detail": exc.detail}) from exc


def _rederive_cohort_id(row: CohortRecord) -> str:
    try:
        created_iso = iso8601_utc(float.fromhex(row.cohort_created_at_raw))
    except (ValueError, OverflowError, OSError) as exc:
        raise LedgerMissingError(
            {"reason": "cohort_created_at_raw_unparsable", "cohort_id": row.cohort_id}
        ) from exc
    return compute_cohort_id(
        kind=row.kind,
        cohort_created_at_iso=created_iso,
        seed_manifest_hash=row.seed_manifest_hash,
        schema_hash=row.schema_hash,
        instrument_hash=row.instrument_hash,
    )


class WriterLock:
    """The whole-run single writer lock of one home."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self._key: str | None = None

    def acquire(self) -> None:
        """Take the lock without waiting; contention fails closed."""
        if self._fd is not None:
            raise WriterLockHeld({"reason": "lock_already_held", "path": str(self.path)})
        key = str(self.path.absolute())
        if key in _HELD_LOCK_PATHS:
            raise WriterLockHeld({"reason": "lock_held_in_process", "path": str(self.path)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_descriptor(fd)
        except OSError as exc:
            os.close(fd)
            raise WriterLockHeld(
                {"reason": "os_lock_unavailable", "path": str(self.path), "error": str(exc)}
            ) from exc
        _HELD_LOCK_PATHS.add(key)
        self._fd = fd
        self._key = key

    def release(self) -> None:
        """Release the lock; releasing an unlocked lock does nothing."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        if self._key is not None:
            _HELD_LOCK_PATHS.discard(self._key)
            self._key = None
        try:
            _unlock_descriptor(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> WriterLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class FreshInit:
    """The fresh initialization context: lock, freshness check, seed, ledger, manifest."""

    def __init__(self, paths: InstrumentPaths, *, lock: WriterLock | None = None) -> None:
        self.paths = paths
        self.lock = lock if lock is not None else WriterLock(paths.writer_lock)
        self._fresh_verified = False
        self._seeded: Any = None
        self._ledger: LedgerHandle | None = None

    def __enter__(self) -> FreshInit:
        self.lock.acquire()
        try:
            self.assert_fresh()
        except BaseException:
            self.lock.release()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.lock.release()

    @property
    def seeded(self) -> Any:
        """Return the opaque result of the injected seeding callable."""
        return self._seeded

    @property
    def ledger(self) -> LedgerHandle | None:
        """Return the ledger handle created in this context."""
        return self._ledger

    def assert_fresh(self) -> None:
        """Check once per context that no durable artifact of a prior run exists."""
        if self._fresh_verified:
            return
        artifacts = self._durable_artifacts()
        if artifacts:
            raise CohortIdentityMismatchError({"reason": "partial_initialization", "artifacts": artifacts})
        self._fresh_verified = True

    def _durable_artifacts(self) -> list[str]:
        paths = self.paths
        artifacts = [
            str(candidate)
            for candidate in (
                paths.ledger_db,
                paths.manifest_path,
                paths.cohort_graph_db,
                paths.cohort_meta_db,
                paths.control_graph_db,
                paths.control_meta_db,
            )
            if candidate.exists()
        ]
        if paths.invocations_dir.exists():
            try:
                occupied = any(paths.invocations_dir.iterdir())
            except OSError:
                occupied = True
            if occupied:
                artifacts.append(str(paths.invocations_dir))
        return artifacts

    def seed(self, seed: Callable[[InstrumentPaths], Any]) -> Any:
        """Run the injected seeding callable after the freshness check has passed."""
        if not self._fresh_verified:
            raise CohortIdentityMismatchError({"reason": "freshness_not_verified"})
        self._seeded = seed(self.paths)
        return self._seeded

    def create_ledger(self) -> LedgerHandle:
        """Create the ledger schema and open the ledger handle."""
        create_ledger_schema(self.paths)
        self._ledger = open_ledger(self.paths)
        return self._ledger

    def register(self, cohort: CohortRecord) -> CohortRecord:
        """Register one cohort identity in the ledger of this context."""
        if self._ledger is None:
            raise LedgerMissingError({"reason": "ledger_not_created"})
        return register_cohort(self._ledger, cohort)

    def write_manifest(self, cohort: CohortRecord, linked_control_id: str | None = None) -> Path:
        """Write the frozen continuity manifest last."""
        return write_manifest(self.paths, cohort=cohort, control_cohort_id=linked_control_id)


def write_invocation_receipt(
    paths: InstrumentPaths,
    *,
    run_id: str,
    attempt_id: str,
    diag_code: str,
    detail: dict[str, Any],
) -> Path:
    """Write the one-time invocation receipt outside the ledger, without overwriting."""
    target = paths.invocations_dir / f"{run_id}.{attempt_id}.receipt.json"
    document = {
        "receipt_version": 1,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "diag_code": diag_code,
        "detail": detail,
        "recorded_at_raw": float.hex(time.time()),
        "recorded_at_iso": iso8601_utc(time.time()),
    }
    text = canonical_json(document).encode("utf-8")
    try:
        paths.invocations_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InvocationReceiptUnavailable(
            {
                "reason": "invocations_dir_unavailable",
                "path": str(paths.invocations_dir),
                "error": str(exc),
            }
        ) from exc
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise InvocationReceiptUnavailable(
                {"reason": "receipt_unreadable", "path": str(target), "error": str(exc)}
            ) from exc
        if existing == text:
            return target
        raise InvocationReceiptUnavailable(
            {"reason": "receipt_exists_with_other_content", "path": str(target)}
        ) from None
    except OSError as exc:
        raise InvocationReceiptUnavailable(
            {"reason": "receipt_not_writable", "path": str(target), "error": str(exc)}
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text)
    except OSError as exc:
        raise InvocationReceiptUnavailable(
            {"reason": "receipt_not_writable", "path": str(target), "error": str(exc)}
        ) from exc
    return target


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if _is_locked_error(exc):
            raise WriterLockHeld({"reason": "database_locked", "error": str(exc)}) from exc
        raise
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()


def _lock_descriptor(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_descriptor(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _static_preflight(event: LedgerEvent) -> None:
    if event.event_type not in EVENT_TYPES:
        _reject("event_type")
    if not isinstance(event.target_event_id, str):
        _reject("target_event_id")
    if event.event_type not in TARGETED_EVENT_TYPES and event.target_event_id:
        _reject("target_event_id")
    if not _is_uuid4(event.event_id):
        _reject("event_id")
    if not isinstance(event.run_id, str) or not event.run_id:
        _reject("run_id")
    if event.event_type in RUN_EVENT_TYPES:
        if event.attempt_id != "":
            _reject("attempt_id")
    elif not _is_uuid4(event.attempt_id):
        _reject("attempt_id")
    if event.observation_kind is not None and event.observation_kind not in OBSERVATION_KINDS:
        _reject("observation_kind")
    if event.recovers_attempt_id is not None:
        if not _is_uuid4(event.recovers_attempt_id) or event.observation_kind != "recovery":
            _reject("recovers_attempt_id")
    if event.outcome is not None and event.outcome not in OUTCOMES:
        _reject("outcome")
    if event.clock_source is not None and event.clock_source not in CLOCK_SOURCES:
        _reject("clock_source")
    if event.diag_code is not None and event.diag_code not in DIAG_CODES:
        _reject("diag_code")
    if event.utc_date is not None and not _UTC_DATE_PATTERN.fullmatch(event.utc_date):
        _reject("utc_date")
    if event.calendar_eligible is not None:
        if isinstance(event.calendar_eligible, bool) or event.calendar_eligible not in (0, 1):
            _reject("calendar_eligible")
    if event.monotonic_delta_ns is not None:
        if isinstance(event.monotonic_delta_ns, bool) or not isinstance(event.monotonic_delta_ns, int):
            _reject("monotonic_delta_ns")
        if event.monotonic_delta_ns < 0:
            _reject("monotonic_delta_ns")
    _validate_payload(event)
    _validate_event_shape(event)


def _validate_payload(event: LedgerEvent) -> None:
    if not isinstance(event.payload, dict):
        _reject("payload")
    if event.payload.get("event_type") != event.event_type:
        _reject("payload")
    try:
        canonical_json(event.payload)
    except (TypeError, ValueError) as exc:
        raise EventRejectedError({"stage": "static", "field": "payload"}) from exc
    if _contains_float(event.payload):
        _reject("payload")


def _validate_event_shape(event: LedgerEvent) -> None:
    if event.event_type in RUN_EVENT_TYPES:
        _reject_sample_fields(event, allow_zero_flag=False)
    elif event.event_type in TARGETED_EVENT_TYPES:
        _reject_sample_fields(event, allow_zero_flag=True)
        if not event.target_event_id:
            _reject("target_event_id")
        if not event.reason_code:
            _reject("reason_code")
    elif event.event_type == "attempt_terminal":
        if not event.outcome:
            _reject("outcome")
        if not event.observation_kind:
            _reject("observation_kind")
        if not event.clock_source:
            _reject("clock_source")
    elif event.event_type == "instrument_gap":
        if not event.diag_code:
            _reject("diag_code")
        if not event.observation_kind:
            _reject("observation_kind")
        if event.calendar_eligible != 0:
            _reject("calendar_eligible")
        payload = event.payload
        if not isinstance(payload.get("product_effect_may_exist"), bool):
            _reject("payload")
        for name in ("phase", "error_summary"):
            value = payload.get(name)
            if not isinstance(value, str) or not value:
                _reject("payload")


def _reject_sample_fields(event: LedgerEvent, *, allow_zero_flag: bool) -> None:
    for name in SAMPLE_FIELDS:
        if getattr(event, name) is not None:
            _reject(name)
    flag = event.calendar_eligible
    if allow_zero_flag:
        if flag is not None and flag != 0:
            _reject("calendar_eligible")
    elif flag is not None:
        _reject("calendar_eligible")


def _reject(field: str) -> NoReturn:
    raise EventRejectedError({"stage": "static", "field": field})


def _contains_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_float(item) for item in value)
    return False


def _is_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed.version == 4


def _validate_source_path(path: str) -> None:
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or "\\" in path:
        raise CohortIdentityMismatchError({"reason": "source_manifest_path_not_relative", "path": path})


def _application_values(event: LedgerEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "cohort_id": event.cohort_id,
        "run_id": event.run_id,
        "attempt_id": event.attempt_id,
        "recovers_attempt_id": event.recovers_attempt_id,
        "observation_kind": event.observation_kind,
        "outcome": event.outcome,
        "diag_code": event.diag_code,
        "clock_source": event.clock_source,
        "capture_pre_raw": event.capture_pre_raw,
        "capture_post_raw": event.capture_post_raw,
        "monotonic_delta_ns": event.monotonic_delta_ns,
        "collected_at_raw": event.collected_at_raw,
        "collected_at_iso": event.collected_at_iso,
        "utc_date": event.utc_date,
        "calendar_eligible": event.calendar_eligible,
        "target_event_id": event.target_event_id,
        "reason_code": event.reason_code,
        "payload": canonical_json(event.payload),
        "appended_at_raw": event.appended_at_raw,
    }


def _differing_fields(event: LedgerEvent, recorded: LedgerEvent) -> list[str]:
    incoming = _application_values(event)
    stored = _application_values(recorded)
    return [name for name in _EVENT_COLUMNS if incoming[name] != stored[name]]


def _collect_attempts(events: Sequence[LedgerEvent]) -> _AttemptLedger:
    attempts: list[str] = []
    started: set[str] = set()
    terminal: list[str] = []
    gaps: list[str] = []
    failed: list[str] = []
    corrections = 0
    revocations = 0
    for event in events:
        if event.event_type == "correction":
            corrections += 1
            continue
        if event.event_type == "revocation":
            revocations += 1
            continue
        if event.event_type not in ATTEMPT_LIFECYCLE_EVENT_TYPES:
            continue
        if event.attempt_id not in attempts:
            attempts.append(event.attempt_id)
        if event.event_type == "attempt_started":
            started.add(event.attempt_id)
        elif event.event_type == "attempt_terminal":
            terminal.append(event.attempt_id)
            if event.outcome != "observed_ok":
                failed.append(event.outcome or "")
        else:
            gaps.append(event.attempt_id)
    open_attempts = [
        attempt
        for attempt in attempts
        if attempt in started and attempt not in terminal and attempt not in gaps
    ]
    return _AttemptLedger(
        attempts=tuple(attempts),
        terminal=tuple(terminal),
        gaps=tuple(gaps),
        open_attempts=tuple(open_attempts),
        failed_outcomes=tuple(failed),
        corrections=corrections,
        revocations=revocations,
    )


def _event_from_row(row: Sequence[Any]) -> LedgerEvent:
    raw_payload = json.loads(row[20])
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    return LedgerEvent(
        event_id=str(row[1]),
        event_type=str(row[2]),
        cohort_id=str(row[3]),
        run_id=str(row[4]),
        attempt_id=str(row[5]),
        recovers_attempt_id=row[6],
        observation_kind=row[7],
        outcome=row[8],
        diag_code=row[9],
        clock_source=row[10],
        capture_pre_raw=row[11],
        capture_post_raw=row[12],
        monotonic_delta_ns=row[13],
        collected_at_raw=row[14],
        collected_at_iso=row[15],
        utc_date=row[16],
        calendar_eligible=row[17],
        target_event_id=str(row[18]),
        reason_code=row[19],
        payload=payload,
        appended_at_raw=str(row[21]),
    )


def _cohort_from_row(row: Sequence[Any]) -> CohortRecord:
    return CohortRecord(
        cohort_id=str(row[0]),
        kind=str(row[1]),
        cohort_created_at_raw=str(row[2]),
        seed_manifest_hash=row[3],
        schema_hash=str(row[4]),
        instrument_hash=str(row[5]),
        config_hash=str(row[6]),
        config_json=str(row[7]),
        source_commit=str(row[8]),
        source_hash=str(row[9]),
        control_id=row[10],
        fixture_seed_clock_raw=row[11],
    )
