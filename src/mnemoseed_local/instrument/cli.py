"""The instrument CLI surface: the four frozen commands over one isolated home.

``init`` registers a fresh home (real product seeding, rollback-journal
normalization, cohort rows, manifest last). ``collect`` records one observation
run, ``control-repair`` runs the bounded control repair passes, and ``summary``
re-derives the report quantities from the ledger alone. Every failure mode is
typed: an :class:`InstrumentError` exits 1 with a canonical diagnostic, a usage
error exits 2, and success exits 0 with the frozen canonical JSON report.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from mnemoseed_local.instrument.admission import (
    AdmissionRow,
    _advanced_floor,
    recompute_admission,
)
from mnemoseed_local.instrument.clock import (
    CaptureCheck,
    ClockCapture,
    capture_span,
    check_capture_invariants,
    parse_raw,
    utc_date_from_raw,
)
from mnemoseed_local.instrument.fixtures import (
    FixtureHandle,
    build_daily_cohort_fixture,
    build_repair_control_fixture,
)
from mnemoseed_local.instrument.ledger import (
    GUARD_TRIGGERS,
    SCHEMA_VERSION,
    BindingIncompleteError,
    ClockValueError,
    CohortIdentityMismatchError,
    CohortRecord,
    FreshInit,
    InstrumentError,
    InstrumentPaths,
    LedgerCorruptError,
    LedgerEvent,
    LedgerHandle,
    LedgerMissingError,
    WriterLock,
    WriterLockHeld,
    build_config_evidence,
    canonical_json,
    check_cohort_continuity,
    compute_config_hash,
    compute_source_hash,
    find_cohort_by_control_id,
    find_prior_unclosed_run_ids,
    open_ledger,
    read_events,
    read_run_state,
    resolve_paths,
    write_invocation_receipt,
)
from mnemoseed_local.instrument.projection import (
    BucketedReceipt,
    ProjectionInput,
    ProjectionResult,
    project,
)
from mnemoseed_local.instrument.repair_oracle import RepairPassResult, run_control_repair
from mnemoseed_local.instrument.sql_readonly import (
    NODE_VERSIONS_QUERY_TEMPLATE,
    RECEIPT_ROWS_QUERY,
    RECEIPT_UNRESOLVED_QUERY,
    SCHEMA_MASTER_QUERY,
    TABLE_INFO_NODE_VERSIONS,
    TABLE_INFO_NODES,
    assert_node_integrity,
    capture_node_integrity,
    crosscheck_receipts,
    query_hash,
    query_hash_for,
    read_unresolved_receipts,
    readonly_connection,
)
from mnemoseed_local.storage.drivers._threadlocal import ThreadLocalConnections
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import NodeFilter, Page

DAILY_KIND = "daily_cohort"
CONTROL_KIND = "repair_control"
COHORT_KIND_CHOICES = (DAILY_KIND, CONTROL_KIND)

CONTROL_FIXTURE_CHOICES = ("control_105_cap100", "control_105_cap99")
CONTROL_FIXTURE_CAPS: dict[str, int] = {
    "control_105_cap100": 100,
    "control_105_cap99": 99,
}
CONTROL_PENDING_COUNT = 105

SEED_CLOCK_EPOCH = 1_700_000_000.0

_STORE_SUFFIXES = ("-wal", "-shm")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_ROLLBACK_HEADER_PAIR = b"\x01\x01"

_PROJECTION_INPUTS_KEY = "projection_inputs"
_PROJECTION_INPUT_KEYS = frozenset({"nomination_id", "disposition", "reason_code", "applied_at_raw"})

_PROJECTION_GROUP_NAMES = ("expired", "recent", "unclassified", "filtered_out")

_REPAIR_RECORD_KEYS = ("pass_index", "repaired", "remaining_pending")


# ---------------------------------------------------------------------------
# Strict read-only driver adapters
# ---------------------------------------------------------------------------


class _ReadonlyStorePool(ThreadLocalConnections):
    """A per-thread pool that opens every connection through the RO factory."""

    def _connect(self) -> sqlite3.Connection:
        _assert_rollback_store(self._path)
        connection = readonly_connection(self._path)
        self._open.add(connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


def _bind_readonly_driver(driver: SqliteGraphDriver | SqliteMetaDriver, path: Path) -> None:
    """Bind a driver instance to a read-only pool without the parent constructor."""
    driver.params = {}
    driver._path = path
    driver._pool = _ReadonlyStorePool(path)


class _ReadonlyGraphDriver(SqliteGraphDriver):
    """The graph store of one rollback-journal pair opened strictly read-only."""

    def __init__(self, path: Path) -> None:
        _bind_readonly_driver(self, path)


class _ReadonlyMetaDriver(SqliteMetaDriver):
    """The meta store of one rollback-journal pair opened strictly read-only."""

    def __init__(self, path: Path) -> None:
        _bind_readonly_driver(self, path)


def _assert_rollback_store(db_path: Path) -> None:
    """Fail closed before any SQLite open when the store is not rollback-journal."""
    if not db_path.exists():
        raise BindingIncompleteError({"reason": "store_file_missing", "path": str(db_path)})
    _assert_store_header(db_path, require_sqlite=True, require_rollback=True)
    sidecars = _store_sidecars(db_path)
    if sidecars:
        raise BindingIncompleteError(
            {"reason": "store_sidecar_present", "path": str(db_path), "sidecars": sidecars}
        )


def _assert_store_header(db_path: Path, *, require_sqlite: bool, require_rollback: bool) -> None:
    try:
        header = db_path.read_bytes()[:100]
    except OSError as exc:
        raise BindingIncompleteError(
            {"reason": "store_header_unreadable", "path": str(db_path), "error": str(exc)}
        ) from exc
    if require_sqlite and not header.startswith(_SQLITE_MAGIC):
        raise BindingIncompleteError({"reason": "store_not_sqlite", "path": str(db_path)})
    if len(header) >= 20:
        header_pair = header[18:20]
        if require_rollback and header_pair != _ROLLBACK_HEADER_PAIR:
            raise BindingIncompleteError(
                {
                    "reason": "store_journal_not_rollback",
                    "path": str(db_path),
                    "header_pair": header_pair.hex(),
                }
            )
    else:
        raise BindingIncompleteError({"reason": "store_header_truncated", "path": str(db_path)})


def _store_sidecars(db_path: Path) -> list[str]:
    return [suffix for suffix in _STORE_SUFFIXES if Path(str(db_path) + suffix).exists()]


def _connect_expected(db_path: Path) -> sqlite3.Connection:
    """Open the store read-write on the same thread, outside any product pool."""
    try:
        return sqlite3.connect(db_path, isolation_level=None)
    except sqlite3.Error as exc:
        raise _store_open_failure(db_path, exc) from exc


def _store_open_failure(db_path: Path, exc: Exception) -> BindingIncompleteError:
    return BindingIncompleteError({"reason": "store_not_normalized", "path": str(db_path), "error": str(exc)})


def _normalize_to_rollback_journal(db_path: Path) -> None:
    """Switch one seeded product store to DELETE journal mode, byte-verified.

    A state-probe open runs first, then the conversion open carries the
    ``PRAGMA journal_mode=DELETE``; the persisted header pair and the absence
    of sidecars are verified after the connection closes. Any failure is typed
    and leaves the store (including a WAL store) exactly as it was.
    """
    _assert_store_header(db_path, require_sqlite=True, require_rollback=False)
    probe = _connect_expected(db_path)
    try:
        probe.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.Error as exc:
        probe.close()
        raise _store_open_failure(db_path, exc) from exc
    probe.close()
    connection = _connect_expected(db_path)
    try:
        mode = str(connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0])
    except sqlite3.Error as exc:
        connection.close()
        raise _store_open_failure(db_path, exc) from exc
    finally:
        connection.close()
    if mode.lower() != "delete":
        raise _store_open_failure(db_path, sqlite3.OperationalError(f"journal mode stayed {mode}"))
    _assert_store_header(db_path, require_sqlite=True, require_rollback=True)
    sidecars = _store_sidecars(db_path)
    if sidecars:
        raise BindingIncompleteError(
            {
                "reason": "store_normalization_unverified",
                "path": str(db_path),
                "sidecars": sidecars,
            }
        )


def _normalize_seeded_stores(store_paths: tuple[Path, ...]) -> None:
    for db_path in store_paths:
        _normalize_to_rollback_journal(db_path)


def _close_seeded_drivers(fixtures: tuple[FixtureHandle, ...]) -> None:
    for fixture in fixtures:
        asyncio.run(fixture.graph.close())
        asyncio.run(fixture.meta.close())


# ---------------------------------------------------------------------------
# Shared private helpers of the handlers
# ---------------------------------------------------------------------------


def _source_manifest_document(path: Path) -> dict[str, str]:
    """Parse the caller-supplied declared baseline manifest."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CohortIdentityMismatchError(
            {"reason": "source_manifest_unreadable", "path": str(path), "error": str(exc)}
        ) from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise CohortIdentityMismatchError(
            {"reason": "source_manifest_unparsable", "path": str(path)}
        ) from exc
    shape_invalid = not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    )
    if shape_invalid:
        raise CohortIdentityMismatchError({"reason": "source_manifest_shape", "path": str(path)})
    return {str(key): str(value) for key, value in parsed.items()}


def _cutoff_argument(text: str) -> Any:
    """The cutoff as a project-validated object; unparsable text stays visible."""
    try:
        return float(text)
    except (TypeError, ValueError, OverflowError):
        return text


def _recorded_projection_payload(inputs: tuple[ProjectionInput, ...]) -> dict[str, Any]:
    """The payload fragment the collect terminal carries for later summary use."""
    return {
        _PROJECTION_INPUTS_KEY: [
            {
                "nomination_id": item.nomination_id,
                "disposition": item.disposition,
                "reason_code": item.reason_code,
                "applied_at_raw": item.applied_at_raw,
            }
            for item in inputs
        ]
    }


def _recorded_projection_inputs(terminal: LedgerEvent) -> tuple[ProjectionInput, ...]:
    """Decode the raw projection inputs stored verbatim in one terminal payload."""
    document = terminal.payload.get(_PROJECTION_INPUTS_KEY)
    if not isinstance(document, list):
        raise BindingIncompleteError({"reason": "projection_inputs_missing", "event_id": terminal.event_id})
    inputs: list[ProjectionInput] = []
    for item in document:
        invalid_shape = (
            not isinstance(item, dict)
            or set(item) != _PROJECTION_INPUT_KEYS
            or not all(isinstance(value, str) for value in item.values())
        )
        if invalid_shape:
            raise BindingIncompleteError(
                {"reason": "projection_inputs_malformed", "event_id": terminal.event_id}
            )
        inputs.append(
            ProjectionInput(
                nomination_id=str(item["nomination_id"]),
                disposition=str(item["disposition"]),
                reason_code=str(item["reason_code"]),
                applied_at_raw=str(item["applied_at_raw"]),
            )
        )
    return tuple(inputs)


def _emit_report(report: dict[str, Any]) -> None:
    sys.stdout.write(canonical_json(report) + "\n")


def _print_error(exc: InstrumentError) -> None:
    sys.stderr.write(canonical_json({"diag_code": exc.diag_code, "detail": exc.detail}) + "\n")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InitSeedPlan:
    """The seeded fixture knowledge the init handler needs after seeding."""

    fixtures: tuple[FixtureHandle, ...]
    daily: FixtureHandle | None
    control: FixtureHandle
    store_paths: tuple[Path, ...]


def _run_init(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.home)
    source_manifest = _source_manifest_document(Path(args.source_manifest))
    source_hash, _files = compute_source_hash(
        source_commit=args.source_commit, source_manifest=source_manifest
    )
    if not args.source_commit:
        raise CohortIdentityMismatchError({"reason": "source_commit_empty"})
    cap = CONTROL_FIXTURE_CAPS[args.control_fixture]
    builder: Callable[[InstrumentPaths], _InitSeedPlan]
    if args.kind == DAILY_KIND:
        builder = functools.partial(_seed_daily_home, control_id=args.control_id, cap=cap)
    else:
        builder = functools.partial(_seed_control_home, control_id=args.control_id, cap=cap)
    fresh = FreshInit(paths)
    fresh.assert_fresh()
    with fresh as context:
        seeded = context.seed(builder)
        plan: _InitSeedPlan = seeded
        _close_seeded_drivers(plan.fixtures)
        _normalize_seeded_stores(plan.store_paths)
        primary, control_cohort = _finalize_plan_records(
            plan,
            paths=paths,
            control_id=args.control_id,
            cap=cap,
            source_commit=args.source_commit,
            source_hash=source_hash,
        )
        ledger = context.create_ledger()
        try:
            context.register(primary)
            if control_cohort is not None and control_cohort.cohort_id != primary.cohort_id:
                context.register(control_cohort)
            manifest_path = context.write_manifest(primary)
        finally:
            ledger.close()
    report = {
        "cohort_id": primary.cohort_id,
        "kind": primary.kind,
        "control_id": primary.control_id,
        "control_cohort_id": (control_cohort.cohort_id if control_cohort is not None else primary.cohort_id),
        "schema_hash": primary.schema_hash,
        "instrument_hash": primary.instrument_hash,
        "config_hash": primary.config_hash,
        "source_hash": primary.source_hash,
        "manifest_path": str(manifest_path),
    }
    _emit_report(report)
    return 0


def _seed_daily_home(paths: InstrumentPaths, *, control_id: str, cap: int) -> _InitSeedPlan:
    """Seed the daily pair and the matched control pair of one home."""
    control = build_repair_control_fixture(
        paths,
        control_id=control_id,
        pending_count=CONTROL_PENDING_COUNT,
        repair_limit=cap,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    return _InitSeedPlan(
        fixtures=(daily, control),
        daily=daily,
        control=control,
        store_paths=(
            paths.cohort_graph_db,
            paths.cohort_meta_db,
            paths.control_graph_db,
            paths.control_meta_db,
        ),
    )


def _seed_control_home(paths: InstrumentPaths, *, control_id: str, cap: int) -> _InitSeedPlan:
    """Seed only the repair-control pair of a control-only home."""
    control = build_repair_control_fixture(
        paths,
        control_id=control_id,
        pending_count=CONTROL_PENDING_COUNT,
        repair_limit=cap,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )
    return _InitSeedPlan(
        fixtures=(control,),
        daily=None,
        control=control,
        store_paths=(paths.control_graph_db, paths.control_meta_db),
    )


def _finalize_plan_records(
    plan: _InitSeedPlan,
    *,
    paths: InstrumentPaths,
    control_id: str,
    cap: int,
    source_commit: str,
    source_hash: str,
) -> tuple[CohortRecord, CohortRecord | None]:
    """Finalize the caller-supplied preregistration fields of the seeded cohorts."""
    control_record = _finalize_cohort(
        plan.control.cohort,
        home=str(paths.home),
        kind=CONTROL_KIND,
        control_id=control_id,
        repair_limit=cap,
        source_commit=source_commit,
        source_hash=source_hash,
    )
    if plan.daily is None:
        return control_record, None
    primary = _finalize_cohort(
        plan.daily.cohort,
        home=str(paths.home),
        kind=DAILY_KIND,
        control_id=control_id,
        repair_limit=None,
        source_commit=source_commit,
        source_hash=source_hash,
    )
    return primary, control_record


def _finalize_cohort(
    cohort: CohortRecord,
    *,
    home: str,
    kind: str,
    control_id: str,
    repair_limit: int | None,
    source_commit: str,
    source_hash: str,
) -> CohortRecord:
    """Bind the finalized preregistration fields; the cohort identity is untouched."""
    config_json = build_config_evidence(
        home=home,
        kind=kind,
        cohort_id=cohort.cohort_id,
        control_id=control_id,
        repair_limit=repair_limit,
    )
    return replace(
        cohort,
        control_id=control_id,
        source_commit=source_commit,
        source_hash=source_hash,
        config_hash=compute_config_hash(config_json),
        config_json=config_json,
    )


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RecordedObservation:
    """The latest complete collect observation recorded in the ledger."""

    terminal: LedgerEvent
    inputs: tuple[ProjectionInput, ...]
    clock_source: str


def _open_ledger_readonly(paths: InstrumentPaths) -> LedgerHandle:
    """Open the ledger through a read-only URI with the frozen integrity checks."""
    if not paths.ledger_db.exists():
        raise LedgerMissingError({"reason": "ledger_db_missing", "path": str(paths.ledger_db)})
    try:
        connection = sqlite3.connect(paths.ledger_db.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise LedgerCorruptError(
            {
                "reason": "ledger_readonly_open_failed",
                "path": str(paths.ledger_db),
                "error": str(exc),
            }
        ) from exc
    try:
        connection.execute("PRAGMA query_only = ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise LedgerCorruptError(
                {"reason": "schema_version_mismatch", "expected": SCHEMA_VERSION, "actual": version}
            )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise LedgerCorruptError({"reason": "integrity_check_failed", "result": integrity})
        triggers = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
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


def _latest_complete_observation(ledger: LedgerHandle, *, cohort_id: str) -> _RecordedObservation | None:
    """The newest closed run's successful collect terminal, or None without one."""
    rows = read_events(ledger, cohort_id=cohort_id)
    closed_run_id: str | None = None
    for appended in reversed(rows):
        if appended.event.event_type == "run_closed":
            closed_run_id = appended.event.run_id
            break
    if closed_run_id is None:
        return None
    terminals = [
        appended.event
        for appended in rows
        if appended.event.run_id == closed_run_id and appended.event.event_type == "attempt_terminal"
    ]
    if not terminals:
        raise BindingIncompleteError({"reason": "closed_run_without_terminal", "run_id": closed_run_id})
    terminal = terminals[-1]
    if terminal.outcome != "observed_ok":
        raise BindingIncompleteError(
            {
                "reason": "closed_run_not_observed_ok",
                "run_id": closed_run_id,
                "outcome": terminal.outcome,
            }
        )
    clock_source = terminal.clock_source
    if clock_source is None:
        raise BindingIncompleteError({"reason": "clock_source_missing", "event_id": terminal.event_id})
    return _RecordedObservation(
        terminal=terminal,
        inputs=_recorded_projection_inputs(terminal),
        clock_source=clock_source,
    )


def _run_summary(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.home)
    ledger = _open_ledger_readonly(paths)
    try:
        cohort = check_cohort_continuity(paths, ledger)
        if args.cohort_id != cohort.cohort_id:
            raise CohortIdentityMismatchError(
                {
                    "reason": "cohort_id_mismatch",
                    "requested": args.cohort_id,
                    "expected": cohort.cohort_id,
                }
            )
        observation = _latest_complete_observation(ledger, cohort_id=args.cohort_id)
        admission = recompute_admission(ledger, cohort_id=args.cohort_id)
    finally:
        ledger.close()
    cutoff = _cutoff_argument(args.cutoff_seconds)
    report = _summary_report(cohort, admission, observation, cutoff)
    _emit_report(report)
    return 0


def _projection_counts(result: Any) -> dict[str, int]:
    return {name: len(getattr(result, name)) for name in _PROJECTION_GROUP_NAMES}


def _summary_report(
    cohort: CohortRecord,
    admission: Any,
    observation: _RecordedObservation | None,
    cutoff: Any,
) -> dict[str, Any]:
    """The frozen summary quantities, as of the latest complete observation."""
    projection_groups = None
    if observation is not None:
        projected = project(
            observation.inputs,
            now_raw=observation.terminal.collected_at_raw,
            clock_source=observation.clock_source,
            cutoff_seconds=cutoff,
        )
        projection_groups = _projection_counts(projected)
    numerator = tuple(admission.numerator)
    first, last = _admitted_bounds(numerator)
    holder_dates = {row.utc_date for row in numerator if row.utc_date is not None}
    return {
        "first_admitted_utc_date": first,
        "last_admitted_utc_date": last,
        "date_difference_days": (
            (date.fromisoformat(last) - date.fromisoformat(first)).days
            if first is not None and last is not None
            else None
        ),
        "elapsed_seconds": _elapsed_seconds(numerator),
        "calendar_gap_days": _calendar_gap_days(first, last, holder_dates),
        "gap_dates": sorted({str(item) for item in admission.gap_dates}),
        "numerator": len(numerator),
        "shadowed": len(admission.shadowed),
        "revoked": len(admission.revoked),
        "projection": projection_groups,
        "cohort_created_at": cohort.cohort_created_at_raw,
    }


def _admitted_bounds(numerator: tuple[AdmissionRow, ...]) -> tuple[str | None, str | None]:
    dated = {
        row.collected_at_raw: row.utc_date
        for row in numerator
        if row.collected_at_raw is not None and row.utc_date is not None
    }
    if not dated:
        return None, None
    ordered = sorted(dated, key=parse_raw)
    return dated[ordered[0]], dated[ordered[-1]]


def _elapsed_seconds(numerator: tuple[AdmissionRow, ...]) -> float | None:
    instants = [parse_raw(row.collected_at_raw) for row in numerator if row.collected_at_raw is not None]
    if not instants:
        return None
    return max(instants) - min(instants)


def _calendar_gap_days(first: str | None, last: str | None, holder_dates: set[str]) -> int:
    """Count the UTC dates of the admitted span that carry no admitted holder."""
    if first is None or last is None:
        return 0
    current = date.fromisoformat(first)
    final = date.fromisoformat(last)
    missing = 0
    while current <= final:
        if current.isoformat() not in holder_dates:
            missing += 1
        current += timedelta(days=1)
    return missing


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

_PRODUCT_READ_LIMIT = 256
_COLLECT_KIND = "collect"
_RECOVERY_KIND = "recovery"
_OBSERVED_OK = "observed_ok"
_FAILED_READ = "failed_read"
_CLOCK_INVALID = "clock_invalid"

_QUERY_LABELS: tuple[tuple[str, str], ...] = (
    ("receipt_unresolved", RECEIPT_UNRESOLVED_QUERY),
    ("receipt_rows", RECEIPT_ROWS_QUERY),
    ("schema_master", SCHEMA_MASTER_QUERY),
    ("table_info_nodes", TABLE_INFO_NODES),
    ("table_info_node_versions", TABLE_INFO_NODE_VERSIONS),
)


@dataclass(frozen=True)
class _ProductRead:
    """The evidence of one bounded read of the cohort pair."""

    inputs: tuple[ProjectionInput, ...]
    profiles: tuple[str, ...]
    meta_profiles: int
    current_nodes: int
    version_rows: int
    receipt_counts: dict[str, int]
    queries: dict[str, str]


def _now_raw() -> str:
    return float.hex(time.time())


def _relative_store(paths: InstrumentPaths, db_path: Path) -> str:
    """The store path relative to the isolated home, for provenance."""
    try:
        return db_path.relative_to(paths.home).as_posix()
    except ValueError:
        return str(db_path)


def _read_cohort_pair(paths: InstrumentPaths) -> _ProductRead:
    """Read the cohort pair through the strict read-only adapters.

    The pair is guarded before the first open, every read goes through the
    inherited product methods, and the node integrity snapshot brackets the
    node reads so a version row or a node value never drifts unnoticed.
    """
    _assert_rollback_store(paths.cohort_graph_db)
    _assert_rollback_store(paths.cohort_meta_db)
    connection = readonly_connection(paths.cohort_graph_db)
    graph = _ReadonlyGraphDriver(paths.cohort_graph_db)
    meta = _ReadonlyMetaDriver(paths.cohort_meta_db)
    try:
        snapshots = read_unresolved_receipts(connection)
        inputs = tuple(
            ProjectionInput(
                nomination_id=item.nomination_id,
                disposition=item.disposition,
                reason_code=item.reason_code,
                applied_at_raw=item.applied_at_raw,
            )
            for item in snapshots
        )
        checks = crosscheck_receipts(
            connection, graph, nomination_ids=[item.nomination_id for item in inputs]
        )
        mismatched = [check.nomination_id for check in checks if not check.fields_match]
        if mismatched:
            raise BindingIncompleteError(
                {"reason": "receipt_crosscheck_mismatch", "nomination_ids": mismatched}
            )
        meta_profiles = len(meta.list_profiles())
        profiles = sorted(
            {entry.profile_id for entry in graph.pending_reconciliation_audits(_PRODUCT_READ_LIMIT)}
        )
        queries = {label: query_hash(text) for label, text in _QUERY_LABELS}
        current_nodes = 0
        version_rows = 0
        receipt_counts: dict[str, int] = {}
        for profile in profiles:
            page = graph.list_nodes(NodeFilter(profile_id=profile), Page(offset=0, limit=_PRODUCT_READ_LIMIT))
            node_ids = [node.node_id for node in page.items]
            if page.total > len(node_ids):
                raise BindingIncompleteError(
                    {"reason": "node_page_truncated", "profile_id": profile, "total": page.total}
                )
            queries[f"node_versions[{profile}]"] = query_hash_for(NODE_VERSIONS_QUERY_TEMPLATE, len(node_ids))
            before = capture_node_integrity(graph, connection, profile_id=profile, node_ids=node_ids)
            for node_id in node_ids:
                if graph.get_node(node_id) is None:
                    raise BindingIncompleteError({"reason": "node_missing", "node_id": node_id})
                version_rows += len(graph.versions(node_id))
            after = capture_node_integrity(graph, connection, profile_id=profile, node_ids=node_ids)
            assert_node_integrity(before, after)
            current_nodes += len(node_ids)
            counts = graph.count_reconciliation_receipts(profile_id=profile)
            for disposition, count in counts.items():
                receipt_counts[disposition] = receipt_counts.get(disposition, 0) + count
        return _ProductRead(
            inputs=inputs,
            profiles=tuple(profiles),
            meta_profiles=meta_profiles,
            current_nodes=current_nodes,
            version_rows=version_rows,
            receipt_counts=receipt_counts,
            queries=queries,
        )
    finally:
        connection.close()
        asyncio.run(graph.close())
        asyncio.run(meta.close())


def _run_event(event_type: str, *, cohort_id: str, run_id: str, payload: dict[str, Any]) -> LedgerEvent:
    """One run-scoped row: no attempt and no sample fields."""
    return LedgerEvent(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        cohort_id=cohort_id,
        run_id=run_id,
        attempt_id="",
        recovers_attempt_id=None,
        observation_kind=None,
        outcome=None,
        diag_code=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=None,
        target_event_id="",
        reason_code=None,
        payload=payload,
        appended_at_raw=_now_raw(),
    )


def _attempt_started_event(
    *,
    cohort_id: str,
    run_id: str,
    attempt_id: str,
    store: dict[str, str],
    observation_kind: str = _COLLECT_KIND,
) -> LedgerEvent:
    """One attempt start row, appended before its capture begins."""
    return LedgerEvent(
        event_id=str(uuid.uuid4()),
        event_type="attempt_started",
        cohort_id=cohort_id,
        run_id=run_id,
        attempt_id=attempt_id,
        recovers_attempt_id=None,
        observation_kind=observation_kind,
        outcome=None,
        diag_code=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=None,
        target_event_id="",
        reason_code=None,
        payload={"event_type": "attempt_started", "store": store},
        appended_at_raw=_now_raw(),
    )


def _attempt_terminal_event(
    *,
    cohort_id: str,
    run_id: str,
    attempt_id: str,
    capture: ClockCapture,
    outcome: str,
    diag_code: str | None,
    calendar_eligible: int,
    utc_date: str,
    payload: dict[str, Any],
    observation_kind: str = _COLLECT_KIND,
    recovers_attempt_id: str | None = None,
) -> LedgerEvent:
    """One attempt terminal row carrying the real capture and its outcome."""
    return LedgerEvent(
        event_id=str(uuid.uuid4()),
        event_type="attempt_terminal",
        cohort_id=cohort_id,
        run_id=run_id,
        attempt_id=attempt_id,
        recovers_attempt_id=recovers_attempt_id,
        observation_kind=observation_kind,
        outcome=outcome,
        diag_code=diag_code,
        clock_source=capture.clock_source,
        capture_pre_raw=capture.capture_pre_raw,
        capture_post_raw=capture.capture_post_raw,
        monotonic_delta_ns=capture.monotonic_delta_ns,
        collected_at_raw=capture.collected_at_raw,
        collected_at_iso=capture.collected_at_iso,
        utc_date=utc_date,
        calendar_eligible=calendar_eligible,
        target_event_id="",
        reason_code=None,
        payload=payload,
        appended_at_raw=_now_raw(),
    )


def _collect_floor(ledger: LedgerHandle, *, cohort_id: str) -> str | None:
    """The clock floor of the collect rows already appended to one cohort."""
    floor: str | None = None
    for appended in read_events(ledger, cohort_id=cohort_id):
        floor = _advanced_floor(floor, appended.event)
    return floor


def _collect_outcome(failure: InstrumentError | None, check: CaptureCheck) -> tuple[str, str | None]:
    """The outcome and primary diagnostic of one attempt."""
    if failure is not None:
        return (_FAILED_READ, failure.diag_code)
    if check.invalid_codes:
        return (_CLOCK_INVALID, check.diag_code)
    return (_OBSERVED_OK, None)


def _collect_error(
    failure: InstrumentError | None, check: CaptureCheck, diag_code: str | None
) -> InstrumentError:
    """The typed failure of an attempt that is not an observation."""
    if failure is not None:
        return failure
    return ClockValueError(
        {"reason": "capture_invalid", "invalid_codes": list(check.invalid_codes)},
        diag_code=diag_code,
        invalid_codes=check.invalid_codes,
    )


def _terminal_payload(
    paths: InstrumentPaths, read: _ProductRead | None, failure: InstrumentError | None
) -> dict[str, Any]:
    """The evidence payload of one attempt terminal row."""
    payload: dict[str, Any] = {
        "event_type": "attempt_terminal",
        "store": {
            "graph": _relative_store(paths, paths.cohort_graph_db),
            "meta": _relative_store(paths, paths.cohort_meta_db),
        },
    }
    if read is not None:
        payload["queries"] = read.queries
        payload["profiles"] = list(read.profiles)
        payload["read_summary"] = {
            "meta_profiles": read.meta_profiles,
            "unresolved_receipts": len(read.inputs),
            "current_nodes": read.current_nodes,
            "version_rows": read.version_rows,
            "receipts": read.receipt_counts,
        }
        payload.update(_recorded_projection_payload(read.inputs))
    if failure is not None:
        payload["failure"] = {
            "diag_code": failure.diag_code,
            "reason": str(failure.detail.get("reason", "")),
        }
    return payload


def _bucketed_document(item: BucketedReceipt) -> dict[str, Any]:
    return {
        "nomination_id": item.nomination_id,
        "bucket": item.bucket,
        "diag_codes": list(item.diag_codes),
        "trust_codes": list(item.trust_codes),
    }


def _projection_document(result: ProjectionResult) -> dict[str, Any]:
    """The collect report projection: every bucket with its receipts."""
    return {
        "expired": [_bucketed_document(item) for item in result.expired],
        "recent": [_bucketed_document(item) for item in result.recent],
        "unclassified": [_bucketed_document(item) for item in result.unclassified],
        "filtered_out": list(result.filtered_out),
    }


def _observe_cohort(
    paths: InstrumentPaths, cohort: CohortRecord, floor: str | None
) -> tuple[ClockCapture, _ProductRead | None, InstrumentError | None, CaptureCheck]:
    """One bracketed observation of the cohort pair with its classified capture."""
    span = capture_span()
    span.begin()
    read: _ProductRead | None = None
    failure: InstrumentError | None = None
    try:
        read = _read_cohort_pair(paths)
    except InstrumentError as exc:
        failure = exc
    except sqlite3.Error as exc:
        failure = BindingIncompleteError({"reason": "product_read_failed", "error": str(exc)})
    capture = span.end()
    check = check_capture_invariants(
        capture, prior_raw=floor, cohort_created_at_raw=cohort.cohort_created_at_raw
    )
    return capture, read, failure, check


def _unrecovered_attempts(
    ledger: LedgerHandle, *, cohort_id: str, prior_unclosed: tuple[str, ...]
) -> tuple[str, ...]:
    """The interrupted attempts of prior unclosed runs that carry no recovery evidence."""
    recovered: set[str] = set()
    for appended in read_events(ledger, cohort_id=cohort_id):
        if appended.event.recovers_attempt_id is not None:
            recovered.add(appended.event.recovers_attempt_id)
    targets: list[str] = []
    for run_id in prior_unclosed:
        state = read_run_state(ledger, cohort_id=cohort_id, run_id=run_id)
        for attempt_id in (*state.open_attempts, *state.gap_attempts):
            if attempt_id not in recovered and attempt_id not in state.terminal_attempts:
                targets.append(attempt_id)
    return tuple(targets)


def _append_recovery_attempt(
    paths: InstrumentPaths,
    ledger: LedgerHandle,
    *,
    cohort: CohortRecord,
    run_id: str,
    store: dict[str, str],
    floor: str | None,
    interrupted_attempt: str,
) -> None:
    """One actual-time recovery of an interrupted attempt: a bounded re-read, no calendar credit."""
    attempt_id = str(uuid.uuid4())
    ledger.append(
        _attempt_started_event(
            cohort_id=cohort.cohort_id,
            run_id=run_id,
            attempt_id=attempt_id,
            store=store,
            observation_kind=_RECOVERY_KIND,
        )
    )
    capture, read, failure, check = _observe_cohort(paths, cohort, floor)
    outcome, diag_code = _collect_outcome(failure, check)
    ledger.append(
        _attempt_terminal_event(
            cohort_id=cohort.cohort_id,
            run_id=run_id,
            attempt_id=attempt_id,
            recovers_attempt_id=interrupted_attempt,
            observation_kind=_RECOVERY_KIND,
            capture=capture,
            outcome=outcome,
            diag_code=diag_code,
            calendar_eligible=0,
            utc_date=utc_date_from_raw(capture.collected_at_raw),
            payload=_terminal_payload(paths, read, failure),
        )
    )


def _collect_locked(paths: InstrumentPaths, *, cutoff_text: str) -> int:
    """Run one collect observation while the whole-run lock is held."""
    ledger = open_ledger(paths)
    try:
        cohort = check_cohort_continuity(paths, ledger)
        prior_unclosed = find_prior_unclosed_run_ids(ledger, cohort_id=cohort.cohort_id)
        floor = _collect_floor(ledger, cohort_id=cohort.cohort_id)
        run_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        store = {
            "graph": _relative_store(paths, paths.cohort_graph_db),
            "meta": _relative_store(paths, paths.cohort_meta_db),
        }
        ledger.append(
            _run_event(
                "run_started",
                cohort_id=cohort.cohort_id,
                run_id=run_id,
                payload={
                    "event_type": "run_started",
                    "schema_hash": cohort.schema_hash,
                    "instrument_hash": cohort.instrument_hash,
                    "source_hash": cohort.source_hash,
                    "restart_of_run_id": prior_unclosed[-1] if prior_unclosed else None,
                    "prior_run_unclosed": list(prior_unclosed),
                },
            )
        )
        targets = _unrecovered_attempts(ledger, cohort_id=cohort.cohort_id, prior_unclosed=prior_unclosed)
        for interrupted_attempt in targets:
            _append_recovery_attempt(
                paths,
                ledger,
                cohort=cohort,
                run_id=run_id,
                store=store,
                floor=floor,
                interrupted_attempt=interrupted_attempt,
            )
        ledger.append(
            _attempt_started_event(
                cohort_id=cohort.cohort_id, run_id=run_id, attempt_id=attempt_id, store=store
            )
        )
        capture, read, failure, check = _observe_cohort(paths, cohort, floor)
        outcome, diag_code = _collect_outcome(failure, check)
        utc_date = utc_date_from_raw(capture.collected_at_raw)
        calendar_eligible = 1 if (outcome == _OBSERVED_OK and cohort.kind == DAILY_KIND) else 0
        terminal = ledger.append(
            _attempt_terminal_event(
                cohort_id=cohort.cohort_id,
                run_id=run_id,
                attempt_id=attempt_id,
                capture=capture,
                outcome=outcome,
                diag_code=diag_code,
                calendar_eligible=calendar_eligible,
                utc_date=utc_date,
                payload=_terminal_payload(paths, read, failure),
            )
        )
        if outcome != _OBSERVED_OK:
            raise _collect_error(failure, check, diag_code)
        if read is None:
            raise BindingIncompleteError({"reason": "product_read_missing", "run_id": run_id})
        state = read_run_state(ledger, cohort_id=cohort.cohort_id, run_id=run_id)
        if not state.closable:
            raise BindingIncompleteError(
                {"reason": "run_not_closable", "run_id": run_id, "block": state.close_block_reason}
            )
        ledger.append(
            _run_event(
                "run_closed",
                cohort_id=cohort.cohort_id,
                run_id=run_id,
                payload={
                    "event_type": "run_closed",
                    "terminal": len(state.terminal_attempts),
                    "gap": len(state.gap_attempts),
                    "incomplete": len(state.open_attempts),
                    "prior_run_unclosed": list(prior_unclosed),
                },
            )
        )
        projected = project(
            read.inputs,
            now_raw=capture.collected_at_raw,
            clock_source=capture.clock_source,
            cutoff_seconds=_cutoff_argument(cutoff_text),
        )
        _emit_report(
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "event_id": terminal.event.event_id,
                "ledger_seq": terminal.ledger_seq,
                "outcome": outcome,
                "utc_date": utc_date,
                "projection": _projection_document(projected),
                "diag_codes": list(check.invalid_codes),
                "trust_codes": list(check.trust_codes),
            }
        )
        return 0
    finally:
        ledger.close()


def _run_collect(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.home)
    lock = WriterLock(paths.writer_lock)
    try:
        lock.acquire()
    except WriterLockHeld as exc:
        run_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        write_invocation_receipt(
            paths,
            run_id=run_id,
            attempt_id=attempt_id,
            diag_code=exc.diag_code,
            detail=exc.detail,
        )
        raise
    try:
        return _collect_locked(paths, cutoff_text=args.cutoff_seconds)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# control-repair
# ---------------------------------------------------------------------------


def _frozen_control_limit(control_row: CohortRecord) -> int:
    """The repair cap bound into the registered control configuration evidence."""
    parsed = json.loads(control_row.config_json)
    limit = parsed["repair_limit"]
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise BindingIncompleteError({"reason": "control_limit_untyped", "cohort_id": control_row.cohort_id})
    return limit


def _control_profile_ids(graph: SqliteGraphDriver) -> tuple[str, ...]:
    """The control profiles of the rows still pending in the audit backlog."""
    pending = {entry.profile_id for entry in graph.pending_reconciliation_audits(_PRODUCT_READ_LIMIT)}
    return tuple(sorted(pending))


def _control_fixture(paths: InstrumentPaths, control_row: CohortRecord) -> FixtureHandle:
    """A writable fixture view over the already-seeded control pair of one home."""
    graph = SqliteGraphDriver(path=paths.control_graph_db)
    meta = SqliteMetaDriver(path=paths.control_meta_db)
    profile_ids = _control_profile_ids(graph)
    node_ids: list[str] = []
    for profile_id in profile_ids:
        page = graph.list_nodes(NodeFilter(profile_id=profile_id), Page(offset=0, limit=_PRODUCT_READ_LIMIT))
        node_ids.extend(node.node_id for node in page.items)
    return FixtureHandle(
        control_id=control_row.control_id,
        graph=graph,
        meta=meta,
        profile_ids=profile_ids,
        node_ids=tuple(node_ids),
        cohort=control_row,
        seed_manifest_hash=control_row.seed_manifest_hash or "",
    )


def _pass_record(item: RepairPassResult) -> dict[str, Any]:
    """The frozen canonical record of one repair pass."""
    return {key: getattr(item, key) for key in _REPAIR_RECORD_KEYS}


def _control_repair_locked(paths: InstrumentPaths, *, control_id: str, limit: int, passes: int) -> int:
    """Run the bounded control repair passes while the whole-run lock is held."""
    for db_path in (paths.control_graph_db, paths.control_meta_db):
        if not db_path.exists():
            raise BindingIncompleteError({"reason": "control_store_missing", "path": str(db_path)})
    ledger = open_ledger(paths)
    try:
        cohort = check_cohort_continuity(paths, ledger)
        if control_id != cohort.control_id:
            raise CohortIdentityMismatchError(
                {"reason": "control_id_mismatch", "requested": control_id, "expected": cohort.control_id}
            )
        control_row = find_cohort_by_control_id(ledger, control_id=control_id)
        if control_row is None:
            raise CohortIdentityMismatchError({"reason": "control_cohort_missing", "control_id": control_id})
        frozen = _frozen_control_limit(control_row)
        if limit != frozen:
            raise CohortIdentityMismatchError(
                {"reason": "control_repair_limit_mismatch", "requested": limit, "frozen": frozen}
            )
    finally:
        ledger.close()
    records: list[RepairPassResult] = []
    control: FixtureHandle | None = None
    try:
        control = _control_fixture(paths, control_row)
        records = [run_control_repair(control, limit=limit, pass_index=index + 1) for index in range(passes)]
    except sqlite3.Error as exc:
        raise BindingIncompleteError({"reason": "control_repair_failed", "error": str(exc)}) from exc
    finally:
        if control is not None:
            _close_seeded_drivers((control,))
    _normalize_seeded_stores((paths.control_graph_db, paths.control_meta_db))
    for item in records:
        _emit_report(_pass_record(item))
    return 0


def _run_control_repair(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.home)
    if args.passes < 1:
        raise BindingIncompleteError({"field": "passes"})
    lock = WriterLock(paths.writer_lock)
    try:
        lock.acquire()
    except WriterLockHeld as exc:
        run_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        write_invocation_receipt(
            paths,
            run_id=run_id,
            attempt_id=attempt_id,
            diag_code=exc.diag_code,
            detail=exc.detail,
        )
        raise
    try:
        return _control_repair_locked(paths, control_id=args.control_id, limit=args.limit, passes=args.passes)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Parser and module entry
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mnemoseed_local.instrument.cli",
        description="The isolated measurement-provenance instrument CLI.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    init = commands.add_parser("init", help="register a fresh isolated home")
    init.add_argument("--home", required=True)
    init.add_argument("--kind", required=True, choices=COHORT_KIND_CHOICES)
    init.add_argument("--control-id", required=True)
    init.add_argument("--control-fixture", required=True, choices=CONTROL_FIXTURE_CHOICES)
    init.add_argument("--source-commit", required=True)
    init.add_argument("--source-manifest", required=True)

    collect = commands.add_parser("collect", help="record one observation run")
    collect.add_argument("--home", required=True)
    collect.add_argument("--cutoff-seconds", required=True)

    repair = commands.add_parser("control-repair", help="run bounded control repair passes")
    repair.add_argument("--home", required=True)
    repair.add_argument("--control-id", required=True)
    repair.add_argument("--limit", required=True, type=int)
    repair.add_argument("--passes", required=True, type=int)

    summary = commands.add_parser("summary", help="report ledger-derived quantities")
    summary.add_argument("--home", required=True)
    summary.add_argument("--cohort-id", required=True)
    summary.add_argument("--cutoff-seconds", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one frozen instrument command; typed failures exit 1, usage exits 2."""
    parser = _build_parser()
    argv_values = argv if argv is not None else sys.argv[1:]
    parsed = parser.parse_args(argv_values)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "init": _run_init,
        "collect": _run_collect,
        "control-repair": _run_control_repair,
        "summary": _run_summary,
    }
    try:
        return handlers[parsed.command](parsed)
    except InstrumentError as exc:
        _print_error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
