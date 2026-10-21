"""S3 control fixture 105: the pending backlog of the home A control pair.

The control pair is seeded with real product writes only; the assertions read
the backlog and the receipts back through the product driver API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.fixtures import (
    build_daily_cohort_fixture,
    build_repair_control_fixture,
)
from mnemoseed_local.instrument.ledger import (
    BindingIncompleteError,
    CohortIdentityMismatchError,
    InstrumentPaths,
    compute_cohort_id,
    resolve_paths,
    verify_config_evidence,
)
from mnemoseed_local.storage.drivers._time import iso8601_utc
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver

CONTROL_ID = "ctrl-105"
PENDING_COUNT = 105
REPAIR_LIMIT = 100
SEED_CLOCK_EPOCH = 1_600_000_000.0


def _receipt_total(graph: SqliteGraphDriver, profile_ids: tuple[str, ...]) -> int:
    counts = [graph.count_reconciliation_receipts(profile_id=profile) for profile in profile_ids]
    return sum(sum(counts_for_profile.values()) for counts_for_profile in counts)


def test_control_fixture_seeds_the_requested_pending_backlog(paths: InstrumentPaths) -> None:
    fixture = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    pending = fixture.graph.pending_reconciliation_audits(PENDING_COUNT + 10)

    assert len(pending) == PENDING_COUNT
    assert {entry.profile_id for entry in pending} <= set(fixture.profile_ids)


def test_control_backlog_spans_more_than_one_profile(paths: InstrumentPaths) -> None:
    fixture = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    pending = fixture.graph.pending_reconciliation_audits(PENDING_COUNT + 10)
    profiles = {entry.profile_id for entry in pending}

    assert len(profiles) >= 2
    assert len(set(fixture.profile_ids)) >= 2


def test_control_pending_rows_are_backed_by_real_receipts(paths: InstrumentPaths) -> None:
    fixture = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    pending = fixture.graph.pending_reconciliation_audits(PENDING_COUNT + 10)
    for entry in pending:
        receipt = fixture.graph.get_reconciliation_receipt(
            profile_id=entry.profile_id,
            nomination_id=entry.nomination_id,
        )
        assert receipt is not None
        assert receipt.nomination_id == entry.nomination_id
        assert receipt.profile_id == entry.profile_id

    assert _receipt_total(fixture.graph, fixture.profile_ids) == PENDING_COUNT


def test_control_row_carries_the_control_id_and_the_seed_clock(paths: InstrumentPaths) -> None:
    fixture = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )
    cohort = fixture.cohort

    assert fixture.control_id == CONTROL_ID
    assert cohort.kind == "repair_control"
    assert cohort.control_id == CONTROL_ID
    assert cohort.fixture_seed_clock_raw is not None
    assert float.fromhex(cohort.fixture_seed_clock_raw) == SEED_CLOCK_EPOCH
    assert float.fromhex(cohort.cohort_created_at_raw) != SEED_CLOCK_EPOCH


def test_control_identity_derives_from_creation_and_seed_facts(paths: InstrumentPaths) -> None:
    fixture = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )
    cohort = fixture.cohort

    rederived = compute_cohort_id(
        kind=cohort.kind,
        cohort_created_at_iso=iso8601_utc(float.fromhex(cohort.cohort_created_at_raw)),
        seed_manifest_hash=cohort.seed_manifest_hash,
        schema_hash=cohort.schema_hash,
        instrument_hash=cohort.instrument_hash,
    )
    assert rederived == cohort.cohort_id


def test_control_config_evidence_stays_unbound_for_the_cli(paths: InstrumentPaths) -> None:
    fixture = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    with pytest.raises(CohortIdentityMismatchError):
        verify_config_evidence(fixture.cohort, paths=paths)


def test_control_seeding_leaves_the_daily_pair_untouched(paths: InstrumentPaths) -> None:
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    daily_receipts = _receipt_total(daily.graph, daily.profile_ids)
    daily_pending = len(daily.graph.pending_reconciliation_audits(1000))

    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_COUNT,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    assert _receipt_total(daily.graph, daily.profile_ids) == daily_receipts
    assert len(daily.graph.pending_reconciliation_audits(1000)) == daily_pending
    assert len(control.graph.pending_reconciliation_audits(PENDING_COUNT + 10)) == PENDING_COUNT


@pytest.mark.parametrize("pending_count", [True, False, 1.5, "105", -1])
def test_control_builder_rejects_an_invalid_pending_count_before_any_product_write(
    paths: InstrumentPaths,
    pending_count: Any,
) -> None:
    with pytest.raises(BindingIncompleteError) as failure:
        build_repair_control_fixture(
            paths,
            control_id=CONTROL_ID,
            pending_count=pending_count,
            repair_limit=REPAIR_LIMIT,
            seed_clock_epoch=SEED_CLOCK_EPOCH,
        )

    assert failure.value.diag_code == "binding_incomplete"
    assert not paths.control_graph_db.exists()
    assert not paths.control_meta_db.exists()


@pytest.mark.parametrize("repair_limit", [True, False, 0, 98, 101])
def test_control_builder_rejects_a_repair_limit_outside_the_frozen_allowlist(
    paths: InstrumentPaths,
    repair_limit: Any,
) -> None:
    with pytest.raises(BindingIncompleteError) as failure:
        build_repair_control_fixture(
            paths,
            control_id=CONTROL_ID,
            pending_count=PENDING_COUNT,
            repair_limit=repair_limit,
            seed_clock_epoch=SEED_CLOCK_EPOCH,
        )

    assert failure.value.diag_code == "binding_incomplete"
    assert not paths.control_graph_db.exists()
    assert not paths.control_meta_db.exists()


def test_control_builder_accepts_zero_pending_and_the_two_frozen_limits(
    paths: InstrumentPaths,
    tmp_path: Path,
) -> None:
    home_b = tmp_path / "home-b"
    home_b.mkdir()

    capped_at_100 = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=0,
        repair_limit=100,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )
    capped_at_99 = build_repair_control_fixture(
        resolve_paths(home_b),
        control_id=CONTROL_ID,
        pending_count=0,
        repair_limit=99,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    assert len(capped_at_100.graph.pending_reconciliation_audits(10)) == 0
    assert _receipt_total(capped_at_100.graph, capped_at_100.profile_ids) == 0
    assert len(capped_at_99.graph.pending_reconciliation_audits(10)) == 0
    assert _receipt_total(capped_at_99.graph, capped_at_99.profile_ids) == 0
    assert capped_at_100.seed_manifest_hash != capped_at_99.seed_manifest_hash
