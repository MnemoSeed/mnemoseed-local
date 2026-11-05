"""The staggered boundary fixture: capture-time-anchored synthetic ages in one explicit home.

Six receipts are seeded at ``anchor - k * 86400`` seconds for k in
{0, 5, 25, 29, 30, 35} next to the stable epoch pair, so consecutive daily
observations cross the candidate 30-day cutoff at day ``30 - k``.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.cli import main
from mnemoseed_local.instrument.clock import parse_raw
from mnemoseed_local.instrument.fixtures import build_daily_cohort_fixture
from mnemoseed_local.instrument.ledger import (
    FROZEN_SOURCE_PATHS,
    BindingIncompleteError,
    canonical_json,
    open_ledger,
    read_events,
    resolve_paths,
)
from mnemoseed_local.instrument.projection import ProjectionInput, project
from mnemoseed_local.instrument.sql_readonly import read_unresolved_receipts, readonly_connection
from mnemoseed_local.storage.drivers._time import epoch_from_iso, iso8601_utc

CONTROL_ID = "ctrl-105"
CONTROL_FIXTURE = "control_105_cap100"
SOURCE_COMMIT = "16006b897a66b39f80748be85b48d46b312fce23"
DECLARED_MANIFEST = {path: "0" * 64 for path in sorted(FROZEN_SOURCE_PATHS)}
SEED_EPOCH = 1_700_000_000.0
STAGGERED_AGE_DAYS = (0, 5, 25, 29, 30, 35)
THIRTY_DAYS_IN_SECONDS = 2_592_000.0
SECONDS_PER_DAY = 86_400.0
OBSERVE_OFFSET_SECONDS = 3_600.0
DAILY_AGE_RECIPE_KEYS = {"daily_age_recipe", "staggered_age_anchor_raw", "staggered_age_days"}
INIT_REPORT_KEYS = {
    "cohort_id",
    "kind",
    "control_id",
    "control_cohort_id",
    "schema_hash",
    "instrument_hash",
    "config_hash",
    "source_hash",
    "manifest_path",
}
EPOCH_PAIR_NOMINATIONS = ("nom-fixture-protected_endpoint", "nom-fixture-stale_revision")


def _staggered_nomination(age: int) -> str:
    return f"nom-fixture-staggered-{age:02d}"


def _run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as stop:
            code = int(stop.code) if isinstance(stop.code, int) else -1
    return code, out.getvalue(), err.getvalue()


def _canonical(text: str) -> dict[str, Any]:
    report = json.loads(text.strip())
    assert canonical_json(report) == text.strip(), "stdout is not canonical JSON"
    return report


def _write_source_manifest(base: Path) -> Path:
    path = base / "source-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(DECLARED_MANIFEST), encoding="utf-8")
    return path


def _init_argv(
    home: Path, manifest_path: Path, *, kind: str = "daily_cohort", recipe: str | None = None
) -> list[str]:
    extra = [] if recipe is None else ["--daily-age-recipe", recipe]
    return [
        "init",
        "--home",
        str(home),
        "--kind",
        kind,
        "--control-id",
        CONTROL_ID,
        "--control-fixture",
        CONTROL_FIXTURE,
        "--source-commit",
        SOURCE_COMMIT,
        "--source-manifest",
        str(manifest_path),
        *extra,
    ]


def _collect_argv(home: Path) -> list[str]:
    return ["collect", "--home", str(home), "--cutoff-seconds", "2592000"]


def _receipt_rows(home: Path) -> list[dict[str, Any]]:
    db_path = resolve_paths(home).cohort_graph_db
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        query = (
            "SELECT nomination_id, disposition, reason_code, applied_at "
            "FROM reconciliation_receipts ORDER BY nomination_id"
        )
        return [dict(row) for row in connection.execute(query)]
    finally:
        connection.close()


def _applied_epochs(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {row["nomination_id"]: epoch_from_iso(row["applied_at"]) for row in rows}


def _unresolved_inputs(home: Path) -> tuple[ProjectionInput, ...]:
    connection = readonly_connection(resolve_paths(home).cohort_graph_db)
    try:
        return tuple(
            ProjectionInput(
                snapshot.nomination_id,
                snapshot.disposition,
                snapshot.reason_code,
                snapshot.applied_at_raw,
            )
            for snapshot in read_unresolved_receipts(connection)
        )
    finally:
        connection.close()


def _store_digests(home: Path) -> dict[str, str]:
    endpoints = resolve_paths(home)
    stores = {
        "cohort/graph.db": endpoints.cohort_graph_db,
        "cohort/meta.db": endpoints.cohort_meta_db,
        "control/graph.db": endpoints.control_graph_db,
        "control/meta.db": endpoints.control_meta_db,
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in stores.items()}


def _init_staggered_home(home: Path, manifest_path: Path) -> dict[str, Any]:
    code, out, _ = _run_cli(*_init_argv(home, manifest_path, recipe="staggered_boundary"))
    assert code == 0
    return _canonical(out)


@pytest.fixture
def source_manifest_path(tmp_path: Path) -> Path:
    return _write_source_manifest(tmp_path)


def test_the_staggered_boundary_init_reports_the_anchor_and_the_declared_ages(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    code, out, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path, recipe="staggered_boundary"))
    assert code == 0
    report = _canonical(out)
    assert set(report) == INIT_REPORT_KEYS | DAILY_AGE_RECIPE_KEYS
    assert report["daily_age_recipe"] == "staggered_boundary"
    anchor = parse_raw(report["staggered_age_anchor_raw"])
    assert math.isfinite(anchor)
    assert abs(anchor - time.time()) < 60.0
    assert report["staggered_age_days"] == [0, 5, 25, 29, 30, 35]


def test_the_control_only_init_reports_null_daily_recipe_fields(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    code, out, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path, kind="repair_control"))
    assert code == 0
    report = _canonical(out)
    assert set(report) == INIT_REPORT_KEYS | DAILY_AGE_RECIPE_KEYS
    assert report["kind"] == "repair_control"
    assert report["daily_age_recipe"] is None
    assert report["staggered_age_anchor_raw"] is None
    assert report["staggered_age_days"] is None


def test_the_staggered_home_seeds_the_declared_ages_relative_to_the_recorded_anchor(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    report = _init_staggered_home(isolated_home, source_manifest_path)
    anchor = parse_raw(report["staggered_age_anchor_raw"])
    applied = _applied_epochs(_receipt_rows(isolated_home))
    staggered = {
        nomination: epoch
        for nomination, epoch in applied.items()
        if nomination.startswith("nom-fixture-staggered-")
    }
    assert set(staggered) == {_staggered_nomination(age) for age in STAGGERED_AGE_DAYS}
    for age in STAGGERED_AGE_DAYS:
        declared = anchor - age * SECONDS_PER_DAY
        persisted = staggered[_staggered_nomination(age)]
        assert persisted == epoch_from_iso(iso8601_utc(declared))
        assert 0.0 <= declared - persisted < 0.001
    rows = _receipt_rows(isolated_home)
    assert all(row["disposition"] == "unresolved" for row in rows)
    staggered_rows = [row for row in rows if row["nomination_id"].startswith("nom-fixture-staggered-")]
    assert {row["reason_code"] for row in staggered_rows} == {"stale_revision", "protected_endpoint"}


def test_the_staggered_home_keeps_the_epoch_pair_rows_as_stable_expired_controls(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    _init_staggered_home(isolated_home, source_manifest_path)
    applied = _applied_epochs(_receipt_rows(isolated_home))
    assert len(applied) == 8
    assert applied["nom-fixture-stale_revision"] == SEED_EPOCH + 0.5
    assert applied["nom-fixture-protected_endpoint"] == SEED_EPOCH + 1.5


def test_the_default_recipe_keeps_the_epoch_pair_home_unchanged(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    code, out, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path))
    assert code == 0
    report = _canonical(out)
    assert report["daily_age_recipe"] == "epoch_pair"
    assert report["staggered_age_anchor_raw"] is None
    assert report["staggered_age_days"] == []
    assert len(_receipt_rows(isolated_home)) == 2
    explicit_home = isolated_home.parent / "home-explicit-epoch-pair"
    explicit_home.mkdir()
    code, out, _ = _run_cli(*_init_argv(explicit_home, source_manifest_path, recipe="epoch_pair"))
    assert code == 0
    explicit = _canonical(out)
    assert explicit["daily_age_recipe"] == "epoch_pair"
    assert explicit["staggered_age_anchor_raw"] is None
    assert explicit["staggered_age_days"] == []
    assert len(_receipt_rows(explicit_home)) == 2


def test_the_staggered_set_straddles_the_boundary_at_the_declared_days(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    report = _init_staggered_home(isolated_home, source_manifest_path)
    anchor = parse_raw(report["staggered_age_anchor_raw"])
    applied = _applied_epochs(_receipt_rows(isolated_home))
    inputs = _unresolved_inputs(isolated_home)
    # The synthetic clock observes every day at one fixed time-of-day, so each
    # receipt flips exactly at the day 30 - k of its declared age.
    buckets: dict[str, dict[int, str]] = {}
    for day in range(32):
        now_raw = float.hex(anchor + day * SECONDS_PER_DAY + OBSERVE_OFFSET_SECONDS)
        result = project(
            inputs,
            now_raw=now_raw,
            clock_source="os_utc_wall",
            cutoff_seconds=THIRTY_DAYS_IN_SECONDS,
        )
        for entry in (*result.expired, *result.recent):
            buckets.setdefault(entry.nomination_id, {})[day] = entry.bucket
        assert result.unclassified == ()
        assert result.filtered_out == ()
    assert set(buckets) == set(applied)
    for age in (29, 25, 5, 0):
        nomination = _staggered_nomination(age)
        trajectory = buckets[nomination]
        flip_day = 30 - age
        assert all(bucket == "recent" for day, bucket in trajectory.items() if day < flip_day)
        assert all(bucket == "expired" for day, bucket in trajectory.items() if day >= flip_day)
        observed = anchor + flip_day * SECONDS_PER_DAY + OBSERVE_OFFSET_SECONDS - applied[nomination]
        preceding = anchor + (flip_day - 1) * SECONDS_PER_DAY + OBSERVE_OFFSET_SECONDS - applied[nomination]
        assert preceding < THIRTY_DAYS_IN_SECONDS <= observed
    for age in (30, 35):
        assert set(buckets[_staggered_nomination(age)].values()) == {"expired"}
    assert set(buckets["nom-fixture-stale_revision"].values()) == {"expired"}
    assert set(buckets["nom-fixture-protected_endpoint"].values()) == {"expired"}
    assert buckets[_staggered_nomination(29)][0] == "recent"
    assert buckets[_staggered_nomination(30)][0] == "expired"


def test_a_collect_over_the_staggered_home_stays_read_only_and_buckets_the_whole_set(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    _init_staggered_home(isolated_home, source_manifest_path)
    before = _store_digests(isolated_home)
    code, out, _ = _run_cli(*_collect_argv(isolated_home))
    assert code == 0
    report = _canonical(out)
    assert report["outcome"] == "observed_ok"
    assert report["diag_codes"] == []
    recent = {item["nomination_id"] for item in report["projection"]["recent"]}
    expired = {item["nomination_id"] for item in report["projection"]["expired"]}
    assert recent == {_staggered_nomination(age) for age in (0, 5, 25, 29)}
    assert expired == {_staggered_nomination(age) for age in (30, 35)} | set(EPOCH_PAIR_NOMINATIONS)
    assert report["projection"]["unclassified"] == []
    assert report["projection"]["filtered_out"] == []
    assert _store_digests(isolated_home) == before
    ledger = open_ledger(resolve_paths(isolated_home))
    try:
        events = read_events(ledger)
        assert [item.event.event_type for item in events] == [
            "run_started",
            "attempt_started",
            "attempt_terminal",
            "run_closed",
        ]
        terminal = events[2].event
        assert terminal.calendar_eligible == 1
    finally:
        ledger.close()


def test_the_staggered_recipe_refuses_a_control_only_home(
    isolated_home: Path, source_manifest_path: Path
) -> None:
    code, out, err = _run_cli(
        *_init_argv(isolated_home, source_manifest_path, kind="repair_control", recipe="staggered_boundary")
    )
    assert code == 1
    diagnostic = json.loads(err.strip())
    assert diagnostic["diag_code"] == "binding_incomplete"
    assert diagnostic["detail"]["reason"] == "daily_age_recipe_requires_daily_cohort"
    assert not (isolated_home / "instrument").exists()
    assert not (isolated_home / "cohort").exists()
    assert not (isolated_home / "control").exists()


def test_an_unknown_daily_age_recipe_refuses_usage(isolated_home: Path, source_manifest_path: Path) -> None:
    code, _, _ = _run_cli(*_init_argv(isolated_home, source_manifest_path, recipe="sometimes"))
    assert code == 2
    assert not (isolated_home / "instrument").exists()


def test_the_staggered_age_recipe_parameters_are_inseparable(isolated_home: Path) -> None:
    endpoints = resolve_paths(isolated_home)
    with pytest.raises(BindingIncompleteError):
        build_daily_cohort_fixture(
            endpoints, seed_clock_epoch=SEED_EPOCH, staggered_age_anchor_epoch=SEED_EPOCH
        )
    with pytest.raises(BindingIncompleteError):
        build_daily_cohort_fixture(
            endpoints, seed_clock_epoch=SEED_EPOCH, staggered_age_days=STAGGERED_AGE_DAYS
        )
    assert not endpoints.cohort_graph_db.exists()
