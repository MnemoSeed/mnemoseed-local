"""S3 store topology: the seeded product pairs of one home and the pre-registration
identity of the fixture cohorts.

Every assertion reads the real product stores through the driver API and through
the explicit database files of the disposable home. No ledger row, no manifest,
and no runtime outside the home is written or read.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.fixtures import (
    DOWNWEIGHT_FACTOR,
    FixtureHandle,
    SeedReceiptSpec,
    build_application,
    build_daily_cohort_fixture,
    build_repair_control_fixture,
    seed_receipt,
)
from mnemoseed_local.instrument.ledger import (
    DIAG_CODES,
    ClockValueError,
    CohortIdentityMismatchError,
    InstrumentPaths,
    compute_cohort_id,
    compute_instrument_hash,
    compute_source_hash,
    resolve_paths,
    schema_hash,
    verify_config_evidence,
)
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.storage.drivers._time import iso8601_utc

SEED_CLOCK_EPOCH = 1_600_000_000.0
APPLIED_AT_EPOCH = 1_600_000_000.5
CONTROL_ID = "ctrl-105"
RECIPE_KINDS = ("stale_revision", "protected_endpoint")


def _read_rows(db_path: Path, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, params)]
    finally:
        connection.close()


def _receipt_count(db_path: Path) -> int:
    rows = _read_rows(db_path, "SELECT COUNT(*) AS total FROM reconciliation_receipts")
    return int(rows[0]["total"])


def _outbox_count(db_path: Path) -> int:
    rows = _read_rows(db_path, "SELECT COUNT(*) AS total FROM reconciliation_audit_outbox")
    return int(rows[0]["total"])


def _persisted_reason_codes(db_path: Path) -> set[str]:
    rows = _read_rows(db_path, "SELECT reason_code FROM reconciliation_receipts")
    return {str(row["reason_code"]) for row in rows}


def _protected_profile_id(fixture: FixtureHandle) -> str:
    for node_id in fixture.node_ids:
        node = fixture.graph.get_node(node_id)
        if node is not None and node.never_decay:
            return node.profile_id
    raise AssertionError("the fixture must seed a protected endpoint node")


def _seed_recipe(fixture: FixtureHandle, kind: str, nomination_id: str) -> None:
    if kind == "protected_endpoint":
        profile_id = _protected_profile_id(fixture)
    else:
        profile_id = fixture.profile_ids[0]
    seed_receipt(
        fixture,
        SeedReceiptSpec(
            nomination_id=nomination_id,
            profile_id=profile_id,
            applied_at_epoch=APPLIED_AT_EPOCH,
            kind=kind,
        ),
    )


def test_daily_fixture_binds_its_writes_to_the_explicit_cohort_paths(paths: InstrumentPaths) -> None:
    build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)

    assert paths.cohort_graph_db.exists()
    assert paths.cohort_meta_db.exists()
    assert not paths.control_graph_db.exists()
    assert not paths.control_meta_db.exists()

    assert _receipt_count(paths.cohort_graph_db) > 0
    tables = _read_rows(paths.cohort_meta_db, "SELECT name FROM sqlite_master WHERE type = 'table'")
    assert tables


def test_control_fixture_binds_its_writes_to_the_explicit_control_paths(paths: InstrumentPaths) -> None:
    build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=105,
        repair_limit=100,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    assert paths.control_graph_db.exists()
    assert paths.control_meta_db.exists()
    assert not paths.cohort_graph_db.exists()
    assert not paths.cohort_meta_db.exists()

    assert _receipt_count(paths.control_graph_db) == 105


def test_home_a_keeps_the_cohort_pair_and_the_control_pair_apart(paths: InstrumentPaths) -> None:
    build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    daily_receipts = _receipt_count(paths.cohort_graph_db)
    daily_outbox = _outbox_count(paths.cohort_graph_db)

    build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=105,
        repair_limit=100,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    store_files = {
        paths.cohort_graph_db,
        paths.cohort_meta_db,
        paths.control_graph_db,
        paths.control_meta_db,
    }
    assert len(store_files) == 4
    assert _receipt_count(paths.cohort_graph_db) == daily_receipts
    assert _outbox_count(paths.cohort_graph_db) == daily_outbox
    assert _receipt_count(paths.control_graph_db) == 105


def test_daily_cohort_identity_derives_from_creation_and_seed_facts(paths: InstrumentPaths) -> None:
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    cohort = fixture.cohort

    assert cohort.kind == "daily_cohort"
    assert cohort.seed_manifest_hash == fixture.seed_manifest_hash
    assert cohort.schema_hash == schema_hash()
    assert cohort.instrument_hash == compute_instrument_hash()[0]

    rederived = compute_cohort_id(
        kind=cohort.kind,
        cohort_created_at_iso=iso8601_utc(float.fromhex(cohort.cohort_created_at_raw)),
        seed_manifest_hash=cohort.seed_manifest_hash,
        schema_hash=cohort.schema_hash,
        instrument_hash=cohort.instrument_hash,
    )
    assert rederived == cohort.cohort_id


def test_fixture_clock_never_backdates_the_cohort_creation(paths: InstrumentPaths) -> None:
    before = time.time()
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    created = float.fromhex(fixture.cohort.cohort_created_at_raw)

    assert created >= before - 1.0
    assert created > SEED_CLOCK_EPOCH
    assert created != SEED_CLOCK_EPOCH


def test_daily_cohort_row_keeps_the_control_only_seed_clock_column_empty(paths: InstrumentPaths) -> None:
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)

    assert fixture.cohort.fixture_seed_clock_raw is None


def test_pre_registration_cohort_leaves_cli_finalized_bindings_unbound(
    paths: InstrumentPaths,
    source_manifest: dict[str, str],
    source_commit: str,
) -> None:
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    cohort = fixture.cohort

    with pytest.raises(CohortIdentityMismatchError):
        verify_config_evidence(cohort, paths=paths)

    assert cohort.control_id is None
    assert cohort.source_commit != source_commit
    declared_digest = compute_source_hash(
        source_commit=source_commit,
        source_manifest=source_manifest,
    )[0]
    assert cohort.source_hash != declared_digest


def test_seeding_writes_no_ledger_row_and_no_manifest(paths: InstrumentPaths) -> None:
    build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=105,
        repair_limit=100,
        seed_clock_epoch=SEED_CLOCK_EPOCH,
    )

    assert not paths.ledger_db.exists()
    assert not paths.manifest_path.exists()
    assert not paths.invocations_dir.exists() or not any(paths.invocations_dir.iterdir())


def test_seeded_nodes_are_real_graph_nodes_of_the_fixture_profiles(paths: InstrumentPaths) -> None:
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)

    assert fixture.profile_ids
    assert fixture.node_ids
    for node_id in fixture.node_ids:
        node = fixture.graph.get_node(node_id)
        assert isinstance(node, GraphNode)
        assert isinstance(node.node_type, NodeType)
        assert node.profile_id in fixture.profile_ids


def test_daily_store_holds_a_real_receipt_for_every_deterministic_recipe(
    paths: InstrumentPaths,
) -> None:
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)

    present = _persisted_reason_codes(paths.cohort_graph_db)
    for kind in RECIPE_KINDS:
        if kind not in present:
            _seed_recipe(fixture, kind, f"nom-recipe-{kind}")

    assert set(RECIPE_KINDS) <= _persisted_reason_codes(paths.cohort_graph_db)

    rows = _read_rows(
        paths.cohort_graph_db,
        "SELECT nomination_id, profile_id, disposition FROM reconciliation_receipts",
    )
    for row in rows:
        receipt = fixture.graph.get_reconciliation_receipt(
            profile_id=str(row["profile_id"]),
            nomination_id=str(row["nomination_id"]),
        )
        assert receipt is not None
        assert str(receipt.disposition) == str(row["disposition"])


def test_seed_receipt_persists_the_caller_supplied_applied_at(paths: InstrumentPaths) -> None:
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    profile_id = fixture.profile_ids[0]
    nomination_id = "nom-fixture-applied-at"

    receipt = seed_receipt(
        fixture,
        SeedReceiptSpec(
            nomination_id=nomination_id,
            profile_id=profile_id,
            applied_at_epoch=APPLIED_AT_EPOCH,
            kind="stale_revision",
        ),
    )

    assert receipt.nomination_id == nomination_id
    assert receipt.profile_id == profile_id
    assert str(receipt.reason_code) == "stale_revision"
    assert str(receipt.disposition) == "unresolved"

    persisted = fixture.graph.get_reconciliation_receipt(
        profile_id=profile_id,
        nomination_id=nomination_id,
    )
    assert persisted is not None
    assert persisted.applied_at == APPLIED_AT_EPOCH

    rows = _read_rows(
        paths.cohort_graph_db,
        "SELECT applied_at FROM reconciliation_receipts WHERE nomination_id = ?",
        (nomination_id,),
    )
    assert [str(row["applied_at"]) for row in rows] == [iso8601_utc(APPLIED_AT_EPOCH)]


def test_protected_endpoint_recipe_adds_no_version_row(paths: InstrumentPaths) -> None:
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    profile_id = _protected_profile_id(fixture)
    before = {node_id: len(fixture.graph.versions(node_id)) for node_id in fixture.node_ids}

    receipt = seed_receipt(
        fixture,
        SeedReceiptSpec(
            nomination_id="nom-fixture-protected",
            profile_id=profile_id,
            applied_at_epoch=APPLIED_AT_EPOCH,
            kind="protected_endpoint",
        ),
    )
    after = {node_id: len(fixture.graph.versions(node_id)) for node_id in fixture.node_ids}

    assert str(receipt.reason_code) == "protected_endpoint"
    assert str(receipt.disposition) == "unresolved"
    assert after == before


@pytest.mark.parametrize("seed_clock_epoch", [True, "1600000000", float("nan"), float("inf"), float("-inf")])
def test_daily_builder_rejects_an_invalid_seed_clock_before_any_product_write(
    paths: InstrumentPaths,
    seed_clock_epoch: Any,
) -> None:
    with pytest.raises(ClockValueError) as failure:
        build_daily_cohort_fixture(paths, seed_clock_epoch=seed_clock_epoch)

    assert failure.value.diag_code in DIAG_CODES
    assert not paths.cohort_graph_db.exists()
    assert not paths.cohort_meta_db.exists()


@pytest.mark.parametrize("seed_clock_epoch", [True, "1600000000", float("nan"), float("inf"), float("-inf")])
def test_control_builder_rejects_an_invalid_seed_clock_before_any_product_write(
    paths: InstrumentPaths,
    seed_clock_epoch: Any,
) -> None:
    with pytest.raises(ClockValueError) as failure:
        build_repair_control_fixture(
            paths,
            control_id=CONTROL_ID,
            pending_count=1,
            repair_limit=100,
            seed_clock_epoch=seed_clock_epoch,
        )

    assert failure.value.diag_code in DIAG_CODES
    assert not paths.control_graph_db.exists()
    assert not paths.control_meta_db.exists()


@pytest.mark.parametrize("applied_at_epoch", [True, "1600000000.5", float("nan"), float("inf")])
def test_seed_receipt_rejects_an_invalid_applied_at_without_touching_the_store(
    paths: InstrumentPaths,
    applied_at_epoch: Any,
) -> None:
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    receipts_before = _receipt_count(paths.cohort_graph_db)
    outbox_before = _outbox_count(paths.cohort_graph_db)

    with pytest.raises(ClockValueError) as failure:
        seed_receipt(
            fixture,
            SeedReceiptSpec(
                nomination_id="nom-invalid-applied-at",
                profile_id=fixture.profile_ids[0],
                applied_at_epoch=applied_at_epoch,
                kind="stale_revision",
            ),
        )

    assert failure.value.diag_code in DIAG_CODES
    assert _receipt_count(paths.cohort_graph_db) == receipts_before
    assert _outbox_count(paths.cohort_graph_db) == outbox_before


def test_integer_epochs_are_accepted_and_normalized_to_float(paths: InstrumentPaths) -> None:
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=1,
        repair_limit=100,
        seed_clock_epoch=1_600_000_000,
    )

    assert control.cohort.fixture_seed_clock_raw == float.hex(1_600_000_000.0)

    receipt = seed_receipt(
        control,
        SeedReceiptSpec(
            nomination_id="nom-integer-epoch",
            profile_id=control.profile_ids[0],
            applied_at_epoch=1_600_000_000,
            kind="stale_revision",
        ),
    )

    assert isinstance(receipt.applied_at, float)
    assert receipt.applied_at == 1_600_000_000.0

    rows = _read_rows(
        paths.control_graph_db,
        "SELECT applied_at FROM reconciliation_receipts WHERE nomination_id = ?",
        ("nom-integer-epoch",),
    )
    assert [str(row["applied_at"]) for row in rows] == [iso8601_utc(1_600_000_000.0)]


def test_seeded_nodes_carry_the_synthetic_fixture_provenance(paths: InstrumentPaths) -> None:
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)

    for node_id in fixture.node_ids:
        node = fixture.graph.get_node(node_id)
        assert node is not None
        assert node.provenance.asserted_by == "instrument-fixture"
        assert node.provenance.source == "instrument-fixture"
        assert node.provenance.history == []
        assert node.provenance.asserted_at == SEED_CLOCK_EPOCH
        assert node.last_reinforced == SEED_CLOCK_EPOCH


def test_two_independent_builds_share_the_seed_manifest(
    paths: InstrumentPaths,
    tmp_path: Path,
) -> None:
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    before = time.time()

    first = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    second = build_daily_cohort_fixture(resolve_paths(home_b), seed_clock_epoch=SEED_CLOCK_EPOCH)

    assert first.seed_manifest_hash == second.seed_manifest_hash
    created_first = float.fromhex(first.cohort.cohort_created_at_raw)
    created_second = float.fromhex(second.cohort.cohort_created_at_raw)
    assert created_first >= before - 1.0
    assert created_second >= before - 1.0
    assert created_first != SEED_CLOCK_EPOCH
    assert created_second != SEED_CLOCK_EPOCH


def test_rebuilt_application_matches_the_receipt_the_store_persisted(paths: InstrumentPaths) -> None:
    """The rebuild must be the same application input, not a look-alike."""
    fixture = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_CLOCK_EPOCH)
    spec = SeedReceiptSpec(
        nomination_id="nom-drift-guard",
        profile_id=fixture.profile_ids[0],
        applied_at_epoch=APPLIED_AT_EPOCH,
        kind="stale_revision",
    )

    receipt = seed_receipt(fixture, spec)
    application = build_application(fixture, spec)

    assert application.nomination_id == receipt.nomination_id
    assert application.profile_id == receipt.profile_id
    assert application.canonical_kind == receipt.canonical_kind
    assert application.composite_group_id == receipt.composite_group_id
    assert application.evidence_event_ids == receipt.evidence_event_ids
    assert application.verdict == receipt.verdict
    assert application.quality == receipt.quality
    assert application.left_node_id == receipt.left_node_id
    assert application.left_version == receipt.left_version
    assert application.left_expected_peer_id == receipt.left_expected_peer_id
    assert application.right_node_id == receipt.right_node_id
    assert application.right_version == receipt.right_version
    assert application.right_expected_peer_id == receipt.right_expected_peer_id
    assert application.winner_node_id == receipt.winner_node_id
    assert application.loser_node_id == receipt.loser_node_id
    assert application.applied_at == receipt.applied_at
    assert receipt.downweight_factor == DOWNWEIGHT_FACTOR

    replayed = fixture.graph.apply_reconciliation(application, downweight_factor=DOWNWEIGHT_FACTOR)

    assert replayed.nomination_id == receipt.nomination_id
    assert replayed.left_node_id == receipt.left_node_id
    assert replayed.left_version == receipt.left_version
    assert replayed.applied_at == receipt.applied_at
    assert replayed.downweight_factor == DOWNWEIGHT_FACTOR
