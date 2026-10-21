"""Shared fixtures for the instrument test suite: an isolated home, the resolved
instrument paths, and builders for valid cohorts, events, and recovery rows.

The fixtures here are the single shared owner of the instrument test surface.
Later slices read them instead of copying fixture code.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.ledger import (
    AppendedEvent,
    CohortRecord,
    FreshInit,
    InstrumentPaths,
    LedgerEvent,
    LedgerHandle,
    build_config_evidence,
    compute_cohort_id,
    compute_config_hash,
    compute_instrument_hash,
    compute_source_hash,
    resolve_paths,
    schema_hash,
    sha256_hex,
)
from mnemoseed_local.storage.drivers._time import iso8601_utc

DAILY_KIND = "daily_cohort"
CONTROL_KIND = "repair_control"
CONTROL_ID = "ctrl-105"
SOURCE_COMMIT = "16006b897a66b39f80748be85b48d46b312fce23"
SOURCE_MANIFEST_PATHS: tuple[str, ...] = (
    "storage/drivers/sqlite_graph.py",
    "storage/drivers/sqlite_meta.py",
    "storage/drivers/_migrations.py",
    "storage/drivers/_time.py",
    "storage/ports.py",
    "dream/repair.py",
    "schema/graph.py",
)
COHORT_CREATED_EPOCH = 1_600_000_000.0
CAPTURE_EPOCH = 1_700_000_000.0
SEED_MANIFEST_HASH = sha256_hex(b"instrument-seed-manifest")
FROZEN_AT_ISO = "2026-10-21T00:00:00.000Z"


def declared_source_manifest() -> dict[str, str]:
    """The frozen baseline paths with declared digests."""
    return {path: sha256_hex(path.encode("utf-8")) for path in SOURCE_MANIFEST_PATHS}


def declared_source_hash(manifest: dict[str, str] | None = None) -> str:
    """The declared digest of the frozen baseline manifest."""
    return compute_source_hash(
        source_commit=SOURCE_COMMIT,
        source_manifest=declared_source_manifest() if manifest is None else manifest,
    )[0]


def build_cohort(
    paths: InstrumentPaths,
    *,
    kind: str = DAILY_KIND,
    control_id: str | None = None,
    repair_limit: int | None = None,
    cohort_created_at_epoch: float = COHORT_CREATED_EPOCH,
    seed_manifest_hash: str | None = SEED_MANIFEST_HASH,
    instrument_hash: str | None = None,
    ledger_schema_hash: str | None = None,
    config_json: str | None = None,
    config_hash: str | None = None,
    cohort_id: str | None = None,
) -> CohortRecord:
    """Build a cohort whose identity, hashes, and config evidence are mutually consistent."""
    resolved_instrument_hash = instrument_hash or compute_instrument_hash()[0]
    resolved_schema_hash = ledger_schema_hash or schema_hash()
    created_raw = float.hex(cohort_created_at_epoch)
    created_iso = iso8601_utc(cohort_created_at_epoch)
    resolved_cohort_id = cohort_id or compute_cohort_id(
        kind=kind,
        cohort_created_at_iso=created_iso,
        seed_manifest_hash=seed_manifest_hash,
        schema_hash=resolved_schema_hash,
        instrument_hash=resolved_instrument_hash,
    )
    evidence = config_json
    if evidence is None:
        evidence = build_config_evidence(
            home=str(paths.home.resolve()),
            kind=kind,
            cohort_id=resolved_cohort_id,
            control_id=control_id,
            repair_limit=repair_limit,
        )
    resolved_config_hash = config_hash or compute_config_hash(evidence)
    return CohortRecord(
        cohort_id=resolved_cohort_id,
        kind=kind,
        cohort_created_at_raw=created_raw,
        seed_manifest_hash=seed_manifest_hash,
        schema_hash=resolved_schema_hash,
        instrument_hash=resolved_instrument_hash,
        config_hash=resolved_config_hash,
        config_json=evidence,
        source_commit=SOURCE_COMMIT,
        source_hash=declared_source_hash(),
        control_id=control_id,
        fixture_seed_clock_raw=None,
    )


def build_event(cohort_id: str, **overrides: Any) -> LedgerEvent:
    """Build one application-side event; every column defaults to a coherent sample."""
    event_type: str = overrides.pop("event_type", "attempt_terminal")
    epoch: float = overrides.pop("epoch", CAPTURE_EPOCH)
    captured_raw = float.hex(epoch)
    captured_iso = iso8601_utc(epoch)
    fields: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "cohort_id": cohort_id,
        "run_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
        "recovers_attempt_id": None,
        "observation_kind": "collect",
        "outcome": "observed_ok",
        "diag_code": None,
        "clock_source": "os_utc_wall",
        "capture_pre_raw": captured_raw,
        "capture_post_raw": captured_raw,
        "monotonic_delta_ns": 0,
        "collected_at_raw": captured_raw,
        "collected_at_iso": captured_iso,
        "utc_date": captured_iso[:10],
        "calendar_eligible": 1,
        "target_event_id": "",
        "reason_code": None,
        "payload": {"event_type": event_type},
        "appended_at_raw": captured_raw,
    }
    fields.update(overrides)
    return LedgerEvent(**fields)


def run_started_event(cohort: CohortRecord, *, run_id: str) -> LedgerEvent:
    """Build the run_started row of a run."""
    return build_event(
        cohort.cohort_id,
        event_type="run_started",
        run_id=run_id,
        attempt_id="",
        observation_kind=None,
        outcome=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=None,
        payload={
            "event_type": "run_started",
            "schema_hash": cohort.schema_hash,
            "instrument_hash": cohort.instrument_hash,
            "source_hash": cohort.source_hash,
            "restart_of_run_id": None,
            "prior_run_unclosed": None,
        },
    )


def attempt_started_event(cohort: CohortRecord, *, run_id: str, attempt_id: str) -> LedgerEvent:
    """Build the attempt_started row of an observation attempt."""
    return build_event(
        cohort.cohort_id,
        event_type="attempt_started",
        run_id=run_id,
        attempt_id=attempt_id,
        observation_kind=None,
        outcome=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=None,
        payload={"event_type": "attempt_started"},
    )


def gap_event(
    cohort: CohortRecord,
    *,
    run_id: str,
    attempt_id: str,
    **overrides: Any,
) -> LedgerEvent:
    """Build an instrument_gap row with its required payload fields."""
    fields: dict[str, Any] = {
        "event_type": "instrument_gap",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "observation_kind": "collect",
        "outcome": None,
        "clock_source": None,
        "capture_pre_raw": None,
        "capture_post_raw": None,
        "monotonic_delta_ns": None,
        "collected_at_raw": None,
        "collected_at_iso": None,
        "utc_date": None,
        "calendar_eligible": 0,
        "diag_code": "ledger_commit_unknown",
        "payload": {
            "event_type": "instrument_gap",
            "product_effect_may_exist": True,
            "phase": "attempt_terminal",
            "error_summary": "ledger commit outcome unknown",
        },
    }
    fields.update(overrides)
    return build_event(cohort.cohort_id, **fields)


def append_observation(
    ledger: LedgerHandle,
    cohort: CohortRecord,
    *,
    terminal: str | None = "attempt_terminal",
    **overrides: Any,
) -> Any:
    """Append a valid run/attempt chain and return its appended terminal row."""
    run_id: str = overrides.pop("run_id", None) or str(uuid.uuid4())
    attempt_id: str = overrides.pop("attempt_id", None) or str(uuid.uuid4())
    started = attempt_started_event(cohort, run_id=run_id, attempt_id=attempt_id)
    ledger.append(run_started_event(cohort, run_id=run_id))
    ledger.append(started)
    if terminal is None:
        return ledger.append(started)
    if terminal == "instrument_gap":
        return ledger.append(gap_event(cohort, run_id=run_id, attempt_id=attempt_id, **overrides))
    outcome = overrides.get("outcome", "observed_ok")
    fields = dict(overrides)
    fields["run_id"] = run_id
    fields["attempt_id"] = attempt_id
    fields.setdefault("calendar_eligible", 1 if outcome == "observed_ok" else 0)
    return ledger.append(build_event(cohort.cohort_id, event_type="attempt_terminal", **fields))


def recovery_event(
    cohort: CohortRecord,
    *,
    run_id: str,
    attempt_id: str,
    recovers_attempt_id: str,
    epoch: float = CAPTURE_EPOCH,
    **overrides: Any,
) -> LedgerEvent:
    """Build a recovery observation row that links to a prior attempt."""
    fields: dict[str, Any] = {
        "event_type": "attempt_terminal",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "recovers_attempt_id": recovers_attempt_id,
        "observation_kind": "recovery",
        "outcome": "observed_ok",
        "calendar_eligible": 0,
        "epoch": epoch,
    }
    fields.update(overrides)
    return build_event(cohort.cohort_id, **fields)


def append_recovery_observation(
    ledger: LedgerHandle,
    cohort: CohortRecord,
    *,
    epoch: float = CAPTURE_EPOCH,
    **overrides: Any,
) -> AppendedEvent:
    """Append a failed attempt chain and the recovery row that links to it."""
    prior = append_observation(ledger, cohort, outcome="failed_read", epoch=epoch)
    run_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    ledger.append(run_started_event(cohort, run_id=run_id))
    ledger.append(attempt_started_event(cohort, run_id=run_id, attempt_id=attempt_id))
    return ledger.append(
        recovery_event(
            cohort,
            run_id=run_id,
            attempt_id=attempt_id,
            recovers_attempt_id=prior.event.attempt_id,
            epoch=epoch + 1.0,
            **overrides,
        )
    )


@dataclass(frozen=True)
class InitializedHome:
    """A home whose ledger, cohorts, and manifest are already frozen."""

    paths: InstrumentPaths
    ledger: LedgerHandle
    primary: CohortRecord
    control: CohortRecord | None


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A disposable home that never touches an installed runtime."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    return home


@pytest.fixture
def paths(isolated_home: Path) -> InstrumentPaths:
    """The instrument paths of the disposable home."""
    resolved = resolve_paths(isolated_home)
    resolved.ledger_db.parent.mkdir(parents=True, exist_ok=True)
    return resolved


@pytest.fixture
def cohort_builder(paths: InstrumentPaths) -> Callable[..., CohortRecord]:
    """Builds a consistent cohort record for the disposable home."""
    return lambda **overrides: build_cohort(paths, **overrides)


@pytest.fixture
def event_builder() -> Callable[..., LedgerEvent]:
    """Builds one application-side event."""
    return build_event


@pytest.fixture
def observation_builder() -> Callable[..., Any]:
    """Appends a valid run/attempt chain to a ledger."""
    return append_observation


@pytest.fixture
def recovery_observation_builder() -> Callable[..., Any]:
    """Appends a failed attempt chain and the recovery row that links to it."""
    return append_recovery_observation


@pytest.fixture
def initialize_home(paths: InstrumentPaths) -> Iterator[Callable[..., InitializedHome]]:
    """Initializes a home through the fresh-init lifecycle and yields the frozen result."""
    handles: list[LedgerHandle] = []

    def _initialize(
        *,
        with_control: bool = True,
        control_id: str | None = CONTROL_ID,
    ) -> InitializedHome:
        primary = build_cohort(paths, kind=DAILY_KIND, control_id=control_id)
        control = None
        if with_control:
            control = build_cohort(paths, kind=CONTROL_KIND, control_id=control_id, repair_limit=100)
        with FreshInit(paths) as context:
            context.seed(lambda _paths: None)
            ledger = context.create_ledger()
            handles.append(ledger)
            registered = context.register(primary)
            if control is not None:
                context.register(control)
            context.write_manifest(registered, control_id)
        return InitializedHome(paths=paths, ledger=ledger, primary=registered, control=control)

    yield _initialize
    for handle in handles:
        handle.close()


@pytest.fixture
def manifest_builder() -> Callable[..., dict[str, Any]]:
    """Builds a manifest document with exactly the approved keys."""

    def _build(cohort: CohortRecord, **overrides: Any) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "manifest_version": 1,
            "cohort_id": cohort.cohort_id,
            "kind": cohort.kind,
            "seed_manifest_hash": cohort.seed_manifest_hash,
            "schema_hash": cohort.schema_hash,
            "instrument_hash": cohort.instrument_hash,
            "config_hash": cohort.config_hash,
            "control_id": cohort.control_id,
            "cohort_created_at_raw": cohort.cohort_created_at_raw,
            "frozen_at_iso": FROZEN_AT_ISO,
        }
        manifest.update(overrides)
        return manifest

    return _build


@pytest.fixture
def source_manifest() -> dict[str, str]:
    """The declared baseline manifest of the frozen path set."""
    return declared_source_manifest()


@pytest.fixture
def source_commit() -> str:
    """The frozen baseline commit of the declared source manifest."""
    return SOURCE_COMMIT


@pytest.fixture
def stored_row(paths: InstrumentPaths) -> Callable[[str], dict[str, Any]]:
    """Reads the persisted row of one event verbatim."""

    def _read(event_id: str) -> dict[str, Any]:
        connection = sqlite3.connect(paths.ledger_db)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            return {}
        return dict(row)

    return _read
