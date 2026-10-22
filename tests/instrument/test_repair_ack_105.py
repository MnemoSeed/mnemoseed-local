"""S4 repair evidence: the bounded control repair, the per-row ACK, and the receipt replay.

Every assertion reads persisted product rows of an isolated home; nothing here
seeds or rewrites product state by hand.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from mnemoseed_local.instrument.fixtures import (
    DOWNWEIGHT_FACTOR,
    STALE_REVISION,
    FixtureHandle,
    SeedReceiptSpec,
    build_application,
    build_daily_cohort_fixture,
    build_repair_control_fixture,
)
from mnemoseed_local.instrument.ledger import InstrumentPaths, resolve_paths
from mnemoseed_local.instrument.repair_oracle import assert_receipt_replay, run_control_repair
from mnemoseed_local.instrument.sql_readonly import (
    OutboxRowSnapshot,
    ProjectionReceiptSnapshot,
    read_outbox_rows,
    read_unresolved_receipts,
    readonly_connection,
)
from mnemoseed_local.storage.ports import ReceiptConflictError

SEED_EPOCH = 1_700_000_000.0
CONTROL_ID = "ctrl-105"
LIMITED_CONTROL_ID = "ctrl-99"
PENDING_TOTAL = 105
REPAIR_LIMIT = 100
LIMITED_REPAIR_LIMIT = 99


@contextmanager
def _outbox_rows(db_path: Path) -> Iterator[list[OutboxRowSnapshot]]:
    """Read the persisted outbox rows of one isolated product database."""
    connection = readonly_connection(db_path)
    try:
        yield read_outbox_rows(connection)
    finally:
        connection.close()


def _unresolved_receipts(db_path: Path) -> list[ProjectionReceiptSnapshot]:
    """Read the persisted unresolved receipts of one isolated product database."""
    connection = readonly_connection(db_path)
    try:
        return read_unresolved_receipts(connection)
    finally:
        connection.close()


def _second_home(paths: InstrumentPaths) -> InstrumentPaths:
    """Resolve the instrument paths of a second, independent home."""
    home = paths.home.parent / "home-b"
    home.mkdir(parents=True, exist_ok=True)
    return resolve_paths(home)


@pytest.fixture
def control_105(paths: InstrumentPaths) -> FixtureHandle:
    """A control pair holding the frozen backlog of 105 pending audits."""
    return build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_TOTAL,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )


def test_the_control_repair_delivers_the_backlog_in_bounded_passes(
    control_105: FixtureHandle, paths: InstrumentPaths
) -> None:
    first = run_control_repair(control_105, limit=REPAIR_LIMIT, pass_index=1)
    assert first.repaired == 100
    assert first.remaining_pending == 5
    assert first.control_id == CONTROL_ID
    assert first.pass_index == 1

    second = run_control_repair(control_105, limit=REPAIR_LIMIT, pass_index=2)
    assert (second.repaired, second.remaining_pending) == (5, 0)
    assert second.pass_index == 2

    third = run_control_repair(control_105, limit=REPAIR_LIMIT, pass_index=3)
    assert (third.repaired, third.remaining_pending) == (0, 0)

    assert first.repaired + second.repaired + third.repaired == PENDING_TOTAL
    assert control_105.graph.pending_reconciliation_audits(10) == []
    with _outbox_rows(paths.control_graph_db) as rows:
        assert len(rows) == PENDING_TOTAL
        assert all(row.delivered_at_raw is not None for row in rows)


def test_one_global_pass_delivers_the_rows_of_every_profile(
    control_105: FixtureHandle, paths: InstrumentPaths
) -> None:
    assert len(set(control_105.profile_ids)) >= 2
    pending = control_105.graph.pending_reconciliation_audits(PENDING_TOTAL)
    assert {entry.profile_id for entry in pending} == set(control_105.profile_ids)

    run_control_repair(control_105, limit=REPAIR_LIMIT, pass_index=1)

    with _outbox_rows(paths.control_graph_db) as rows:
        delivered = [row for row in rows if row.delivered_at_raw is not None]
    assert len(delivered) == 100
    assert {row.profile_id for row in delivered} == set(control_105.profile_ids)


def test_the_daily_cohort_pair_is_never_repaired(paths: InstrumentPaths) -> None:
    daily = build_daily_cohort_fixture(paths, seed_clock_epoch=SEED_EPOCH)
    control = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_TOTAL,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    receipts_before = daily.graph.count_reconciliation_receipts(profile_id=daily.profile_ids[0])

    run_control_repair(control, limit=REPAIR_LIMIT, pass_index=1)

    assert daily.graph.count_reconciliation_receipts(profile_id=daily.profile_ids[0]) == receipts_before
    with _outbox_rows(paths.cohort_graph_db) as rows:
        assert rows
        assert all(row.delivered_at_raw is None for row in rows)
    assert daily.graph.pending_reconciliation_audits(10)


def test_a_fresh_control_pair_delivers_ninety_nine_and_keeps_six_pending(paths: InstrumentPaths) -> None:
    home_b = _second_home(paths)
    control_b = build_repair_control_fixture(
        home_b,
        control_id=LIMITED_CONTROL_ID,
        pending_count=PENDING_TOTAL,
        repair_limit=LIMITED_REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )

    first = run_control_repair(control_b, limit=LIMITED_REPAIR_LIMIT, pass_index=1)
    assert first.repaired == 99
    assert first.remaining_pending == 6
    assert first.control_id == LIMITED_CONTROL_ID

    second = run_control_repair(control_b, limit=LIMITED_REPAIR_LIMIT, pass_index=2)
    assert (second.repaired, second.remaining_pending) == (6, 0)

    with _outbox_rows(home_b.control_graph_db) as rows:
        assert len(rows) == PENDING_TOTAL
        assert all(row.delivered_at_raw is not None for row in rows)


def test_the_second_home_leaves_the_first_control_pair_untouched(paths: InstrumentPaths) -> None:
    control_a = build_repair_control_fixture(
        paths,
        control_id=CONTROL_ID,
        pending_count=PENDING_TOTAL,
        repair_limit=REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )
    home_b = _second_home(paths)
    control_b = build_repair_control_fixture(
        home_b,
        control_id=LIMITED_CONTROL_ID,
        pending_count=PENDING_TOTAL,
        repair_limit=LIMITED_REPAIR_LIMIT,
        seed_clock_epoch=SEED_EPOCH,
    )

    run_control_repair(control_b, limit=LIMITED_REPAIR_LIMIT, pass_index=1)

    with _outbox_rows(paths.control_graph_db) as rows_a:
        assert len(rows_a) == PENDING_TOTAL
        assert all(row.delivered_at_raw is None for row in rows_a)
    assert control_a.graph.pending_reconciliation_audits(PENDING_TOTAL)


def test_redelivering_one_row_is_refused_and_keeps_its_raw_stamp(
    control_105: FixtureHandle, paths: InstrumentPaths
) -> None:
    run_control_repair(control_105, limit=REPAIR_LIMIT, pass_index=1)
    with _outbox_rows(paths.control_graph_db) as rows:
        delivered = [row for row in rows if row.delivered_at_raw is not None]
        undelivered = [row for row in rows if row.delivered_at_raw is None]
    assert delivered
    assert undelivered

    target = delivered[0]
    stamp_before = target.delivered_at_raw
    assert isinstance(stamp_before, str)

    assert control_105.graph.mark_reconciliation_audit_delivered(target.outbox_id, time.time()) is False

    with _outbox_rows(paths.control_graph_db) as rows:
        after = {row.outbox_id: row.delivered_at_raw for row in rows}
    assert after[target.outbox_id] == stamp_before
    assert after[undelivered[0].outbox_id] is None

    second = run_control_repair(control_105, limit=REPAIR_LIMIT, pass_index=2)
    assert (second.repaired, second.remaining_pending) == (5, 0)

    third = run_control_repair(control_105, limit=REPAIR_LIMIT, pass_index=3)
    assert (third.repaired, third.remaining_pending) == (0, 0)

    with _outbox_rows(paths.control_graph_db) as rows:
        final = {row.outbox_id: row.delivered_at_raw for row in rows}
    assert final[target.outbox_id] == stamp_before


def test_replaying_the_same_input_returns_the_persisted_receipt(
    control_105: FixtureHandle, paths: InstrumentPaths
) -> None:
    spec = SeedReceiptSpec(
        nomination_id="nom-control-0000",
        profile_id=control_105.profile_ids[0],
        applied_at_epoch=SEED_EPOCH,
        kind=STALE_REVISION,
    )
    application = build_application(control_105, spec)
    receipts_before = _unresolved_receipts(paths.control_graph_db)

    replayed = assert_receipt_replay(control_105.graph, application, DOWNWEIGHT_FACTOR)
    persisted = control_105.graph.get_reconciliation_receipt(
        profile_id=spec.profile_id, nomination_id=spec.nomination_id
    )
    assert persisted is not None
    assert replayed == persisted
    assert replayed.nomination_id == spec.nomination_id
    assert replayed.loser_new_version is None
    assert replayed.loser_prior_version is not None
    assert _unresolved_receipts(paths.control_graph_db) == receipts_before


def test_a_different_input_for_the_same_nomination_is_refused(control_105: FixtureHandle) -> None:
    spec = SeedReceiptSpec(
        nomination_id="nom-control-0000",
        profile_id=control_105.profile_ids[0],
        applied_at_epoch=SEED_EPOCH + 1.0,
        kind=STALE_REVISION,
    )
    application = build_application(control_105, spec)

    with pytest.raises(ReceiptConflictError):
        control_105.graph.apply_reconciliation(application, downweight_factor=DOWNWEIGHT_FACTOR)


_SEEDED_NOMINATION_ID = "nom-control-0000"

_NODE_VERSION_ROWS_QUERY = (
    "SELECT node_id, version, profile_id, valid_from, valid_to, superseded_by, changed_at, payload "
    "FROM node_versions ORDER BY node_id, version"
)


def _logical_rows(db_path: Path) -> tuple[tuple[tuple[object, ...], ...], ...]:
    """Every persisted receipt, outbox, and version row of one product store."""
    from mnemoseed_local.instrument.sql_readonly import OUTBOX_ROWS_QUERY, RECEIPT_ROWS_QUERY

    tables: list[tuple[tuple[object, ...], ...]] = []
    connection = readonly_connection(db_path)
    try:
        for query in (RECEIPT_ROWS_QUERY, OUTBOX_ROWS_QUERY, _NODE_VERSION_ROWS_QUERY):
            tables.append(tuple(tuple(row) for row in connection.execute(query).fetchall()))
    finally:
        connection.close()
    return tuple(tables)


def _receipt_column_raw(db_path: Path, column: str) -> tuple[object, str]:
    """The stored value and the storage class of one receipt column."""
    connection = readonly_connection(db_path)
    try:
        row = connection.execute(
            f"SELECT {column}, typeof({column}) FROM reconciliation_receipts WHERE nomination_id = ?",
            (_SEEDED_NOMINATION_ID,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0], str(row[1])


def _copy_product_store(source_db: Path, copy_path: Path) -> None:
    """Back up one isolated product database into a separate file."""
    import sqlite3

    source = sqlite3.connect(source_db)
    target = sqlite3.connect(copy_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _corrupt_copied_receipt(copy_path: Path, column: str, value: object) -> None:
    """Overwrite one receipt column of a copied store after dropping its UPDATE guards."""
    import sqlite3

    connection = sqlite3.connect(copy_path)
    try:
        guards = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            ("reconciliation_receipts",),
        ).fetchall()
        dropped = [str(name) for name, sql in guards if "UPDATE" in str(sql).upper()]
        assert dropped, "the copied receipt table must carry an append-only UPDATE guard"
        for name in dropped:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            f"UPDATE reconciliation_receipts SET {column} = ? WHERE nomination_id = ?",
            (value, _SEEDED_NOMINATION_ID),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("column", "value", "expected_error"),
    [
        pytest.param("applied_at", "not-a-timestamp", ValueError, id="applied-at-text"),
        pytest.param("evidence_event_ids", "null", TypeError, id="evidence-json-null"),
        pytest.param("left_version", float("inf"), OverflowError, id="left-version-infinite"),
    ],
)
def test_a_copied_receipt_the_product_cannot_decode_fails_the_replay_typed(
    control_105: FixtureHandle,
    paths: InstrumentPaths,
    tmp_path: Path,
    column: str,
    value: object,
    expected_error: type[Exception],
) -> None:
    """A terminal replay over an undecodable persisted receipt fails typed and writes nothing."""
    import asyncio

    from mnemoseed_local.instrument.ledger import BindingIncompleteError
    from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver

    spec = SeedReceiptSpec(
        nomination_id=_SEEDED_NOMINATION_ID,
        profile_id=control_105.profile_ids[0],
        applied_at_epoch=SEED_EPOCH,
        kind=STALE_REVISION,
    )
    application = build_application(control_105, spec)
    original_before = _receipt_column_raw(paths.control_graph_db, column)
    copy_path = tmp_path / f"replay-copy-{column}.db"
    _copy_product_store(paths.control_graph_db, copy_path)
    copy_graph = SqliteGraphDriver(path=copy_path)
    try:
        baseline = assert_receipt_replay(copy_graph, application, DOWNWEIGHT_FACTOR)
        assert baseline.nomination_id == _SEEDED_NOMINATION_ID
        hash_before = _receipt_column_raw(copy_path, "input_hash")

        _corrupt_copied_receipt(copy_path, column, value)

        assert _receipt_column_raw(copy_path, "input_hash") == hash_before
        rows_before = _logical_rows(copy_path)

        with pytest.raises(expected_error):
            copy_graph.apply_reconciliation(application, downweight_factor=DOWNWEIGHT_FACTOR)

        with pytest.raises(BindingIncompleteError) as caught:
            assert_receipt_replay(copy_graph, application, DOWNWEIGHT_FACTOR)

        assert caught.value.diag_code == "binding_incomplete"
        assert isinstance(caught.value.__cause__, expected_error)
        assert _logical_rows(copy_path) == rows_before
        assert _receipt_column_raw(paths.control_graph_db, column) == original_before
    finally:
        asyncio.run(copy_graph.close())


def test_a_never_applied_nomination_is_refused_without_writing(
    control_105: FixtureHandle, paths: InstrumentPaths
) -> None:
    """A replay never degenerates into a first application of an absent nomination."""
    from mnemoseed_local.instrument.ledger import BindingIncompleteError

    spec = SeedReceiptSpec(
        nomination_id="nom-control-never-applied",
        profile_id=control_105.profile_ids[0],
        applied_at_epoch=SEED_EPOCH,
        kind=STALE_REVISION,
    )
    application = build_application(control_105, spec)
    receipts_before, outbox_before, versions_before = _logical_rows(paths.control_graph_db)
    assert receipts_before and outbox_before and versions_before

    with pytest.raises(BindingIncompleteError) as caught:
        assert_receipt_replay(control_105.graph, application, DOWNWEIGHT_FACTOR)

    assert caught.value.diag_code == "binding_incomplete"
    assert caught.value.detail["reason"] == "receipt_missing"
    persisted = control_105.graph.get_reconciliation_receipt(
        profile_id=spec.profile_id, nomination_id=spec.nomination_id
    )
    assert persisted is None
    receipts_after, outbox_after, versions_after = _logical_rows(paths.control_graph_db)
    assert receipts_after == receipts_before
    assert outbox_after == outbox_before
    assert versions_after == versions_before
    assert len(receipts_after) == PENDING_TOTAL
    assert len(outbox_after) == PENDING_TOTAL


def test_a_conflicting_application_fails_the_replay_without_writing(
    control_105: FixtureHandle, paths: InstrumentPaths
) -> None:
    """The replay keeps the product conflict refusal and writes nothing."""
    spec = SeedReceiptSpec(
        nomination_id=_SEEDED_NOMINATION_ID,
        profile_id=control_105.profile_ids[0],
        applied_at_epoch=SEED_EPOCH + 1.0,
        kind=STALE_REVISION,
    )
    application = build_application(control_105, spec)
    rows_before = _logical_rows(paths.control_graph_db)

    with pytest.raises(ReceiptConflictError):
        assert_receipt_replay(control_105.graph, application, DOWNWEIGHT_FACTOR)

    assert _logical_rows(paths.control_graph_db) == rows_before
