"""S3 control fixture 99: the fresh home B control pair with the cap of ninety-nine.

Home B is a second disposable home with its own control id and its own database
pair; it never rebuilds, renames, or re-measures the home A control pair.
"""

from __future__ import annotations

from pathlib import Path

from mnemoseed_local.instrument.fixtures import (
    build_daily_cohort_fixture,
    build_repair_control_fixture,
)
from mnemoseed_local.instrument.ledger import (
    InstrumentPaths,
    compute_cohort_id,
    resolve_paths,
)
from mnemoseed_local.storage.drivers._time import iso8601_utc

HOME_A_CONTROL_ID = "ctrl-105"
HOME_B_CONTROL_ID = "ctrl-99"
PENDING_COUNT = 105
SEED_CLOCK_EPOCH = 1_600_000_000.0


def _home_b_paths(tmp_path: Path) -> InstrumentPaths:
    home = tmp_path / "home-b"
    home.mkdir()
    return resolve_paths(home)


def test_home_b_is_a_separate_home_with_its_own_control_pair(
    paths: InstrumentPaths,
    tmp_path: Path,
) -> None:
    paths_b = _home_b_paths(tmp_path)

    home_a = build_repair_control_fixture(
        paths,
        control_id=HOME_A_CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=100,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )
    home_b = build_repair_control_fixture(
        paths_b,
        control_id=HOME_B_CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=99,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    assert paths_b.home != paths.home
    assert paths_b.control_graph_db != paths.control_graph_db
    assert paths_b.control_meta_db != paths.control_meta_db
    assert home_a.control_id == HOME_A_CONTROL_ID
    assert home_b.control_id == HOME_B_CONTROL_ID
    assert home_b.cohort.control_id == HOME_B_CONTROL_ID
    assert len(home_b.graph.pending_reconciliation_audits(PENDING_COUNT + 10)) == PENDING_COUNT
    assert len(home_a.graph.pending_reconciliation_audits(PENDING_COUNT + 10)) == PENDING_COUNT


def test_home_b_control_pair_is_fresh_and_leaves_home_a_untouched(
    paths: InstrumentPaths,
    tmp_path: Path,
) -> None:
    paths_b = _home_b_paths(tmp_path)
    build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    home_a = build_repair_control_fixture(
        paths,
        control_id=HOME_A_CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=100,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )
    home_a_pending = len(home_a.graph.pending_reconciliation_audits(PENDING_COUNT + 10))

    build_repair_control_fixture(
        paths_b,
        control_id=HOME_B_CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=99,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    assert len(home_a.graph.pending_reconciliation_audits(PENDING_COUNT + 10)) == home_a_pending
    assert not paths_b.cohort_graph_db.exists()
    assert not paths_b.cohort_meta_db.exists()
    assert not paths_b.ledger_db.exists()
    assert not paths_b.manifest_path.exists()


def test_home_b_control_row_identity_binds_its_own_control_id(
    paths: InstrumentPaths,
    tmp_path: Path,
) -> None:
    paths_b = _home_b_paths(tmp_path)
    fixture = build_repair_control_fixture(
        paths_b,
        control_id=HOME_B_CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=99,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )
    cohort = fixture.cohort

    assert cohort.kind == "repair_control"
    assert cohort.control_id == HOME_B_CONTROL_ID
    assert cohort.fixture_seed_clock_raw is not None
    assert float.fromhex(cohort.fixture_seed_clock_raw) == SEED_CLOCK_EPOCH

    rederived = compute_cohort_id(
        kind=cohort.kind,
        cohort_created_at_iso=iso8601_utc(float.fromhex(cohort.cohort_created_at_raw)),
        seed_manifest_hash=cohort.seed_manifest_hash,
        schema_hash=cohort.schema_hash,
        instrument_hash=cohort.instrument_hash,
    )
    assert rederived == cohort.cohort_id
