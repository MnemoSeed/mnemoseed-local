"""Durable ingest queue data layer (PRD-409 S3).

The meta store owns the raw event envelope, the seen/applied split, and the
atomic applied-marker + pool-credit commit that the boot replay relies on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mnemoseed_local.daemon.ingest_queue import (
    IngestQueue,
    derive_ingest_event_id,
)
from mnemoseed_local.storage.drivers._migrations import latest_version
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import (
    IngestAppliedEvent,
    IngestRecord,
    IngestStream,
    PoolCreditOp,
    TurnRange,
)

PROFILE = "default"
SESSION = "session-409"
ROLLOUT = "rollout-a"
STREAM = IngestStream(PROFILE, SESSION, ROLLOUT)


@pytest.fixture
def meta(tmp_path: Path) -> SqliteMetaDriver:
    driver = SqliteMetaDriver(path=tmp_path / "meta.db")
    yield driver  # type: ignore[misc]
    asyncio.run(driver.close())


def _record(ordinal: int, *, event_id: str | None = None, kind: str = "event") -> IngestRecord:
    return IngestRecord(
        event_id=event_id or derive_ingest_event_id(PROFILE, SESSION, ROLLOUT, ordinal, "hello"),
        profile_id=PROFILE,
        session_id=SESSION,
        rollout_id=ROLLOUT,
        ordinal=ordinal,
        kind=kind,
        payload='{"text": "hello"}',
        actor="hook",
        host="opencode",
        received_at=float(ordinal),
    )


def test_ingest_tables_are_part_of_the_meta_migration(meta: SqliteMetaDriver) -> None:
    assert meta.schema_version() == latest_version()
    tables = {
        str(row[0])
        for row in meta._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"ingest_events", "ingest_watermarks", "ingest_applied"} <= tables


def test_event_id_is_deterministic_and_content_sensitive() -> None:
    base = derive_ingest_event_id(PROFILE, SESSION, ROLLOUT, 7, "hello")
    assert base == derive_ingest_event_id(PROFILE, SESSION, ROLLOUT, 7, "hello")
    assert base != derive_ingest_event_id(PROFILE, SESSION, ROLLOUT, 7, "other")
    assert base != derive_ingest_event_id(PROFILE, SESSION, ROLLOUT, 8, "hello")
    assert base != derive_ingest_event_id(PROFILE, SESSION, "rollout-b", 7, "hello")


def test_record_persists_envelope_and_advances_seen_together(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    record = _record(4)
    queue.record_envelope(record)
    row = meta._conn.execute(
        "SELECT payload, actor, host, rollout_id, ordinal FROM ingest_events WHERE event_id = ?",
        (record.event_id,),
    ).fetchone()
    assert row is not None
    assert (row["payload"], row["actor"], row["host"]) == ('{"text": "hello"}', "hook", "opencode")
    assert row["ordinal"] == 4
    assert queue.watermark(STREAM) == (4, -1)


def test_seen_and_applied_are_independent_watermarks(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    first, second, third = _record(1), _record(2), _record(3)
    for record in (first, second, third):
        queue.record_envelope(record)
    queue.apply(
        STREAM,
        (
            IngestAppliedEvent(first.event_id, 1, 0.25),
            IngestAppliedEvent(second.event_id, 2, 0.5),
        ),
        credits=(),
        at=10.0,
    )
    assert queue.watermark(STREAM) == (3, 2)
    assert [row.event_id for row in queue.pending()] == [third.event_id]


def test_record_insert_is_idempotent_on_event_id(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    record = _record(5)
    queue.record_envelope(record)
    queue.record_envelope(record)
    assert queue.watermark(STREAM) == (5, -1)
    assert len(queue.pending()) == 1


def test_apply_writes_marker_and_pool_credit_in_one_transaction(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    record = _record(6)
    queue.record_envelope(record)
    queue.apply(
        STREAM,
        (IngestAppliedEvent(record.event_id, 6, 0.75),),
        credits=(PoolCreditOp("credit", PROFILE, 0.75, TurnRange(6, 6)),),
        at=11.0,
    )
    marker = meta._conn.execute(
        "SELECT points FROM ingest_applied WHERE event_id = ?", (record.event_id,)
    ).fetchone()
    assert marker is not None and marker["points"] == 0.75
    assert meta.pool_state(PROFILE).balance == 0.75
    assert queue.pending() == ()


def test_a_failed_credit_rolls_back_the_applied_marker(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    record = _record(7)
    queue.record_envelope(record)
    with pytest.raises(ValueError, match="credit balance must be finite and non-negative"):
        queue.apply(
            STREAM,
            (IngestAppliedEvent(record.event_id, 7, 0.5),),
            credits=(PoolCreditOp("credit", PROFILE, -1.0, TurnRange(7, 7)),),
            at=12.0,
        )
    assert queue.watermark(STREAM) == (7, -1)
    assert len(queue.pending()) == 1
    assert meta.pool_state(PROFILE).balance == 0.0


def test_applied_rows_are_skipped_by_pending(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    marker = _record(-1, kind="flush")
    queue.record_envelope(marker)
    assert len(queue.pending()) == 1
    queue.apply(
        STREAM,
        (IngestAppliedEvent(marker.event_id, -1, 0.0),),
        credits=(),
        at=9.0,
    )
    assert queue.pending() == ()


def test_failed_commit_leaves_the_event_pending_for_one_replay(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    record = _record(8)
    queue.record_envelope(record)
    with pytest.raises(ValueError):
        queue.apply(
            STREAM,
            (IngestAppliedEvent(record.event_id, 8, 0.4),),
            credits=(PoolCreditOp("credit", PROFILE, -1.0, TurnRange(8, 8)),),
            at=13.0,
        )
    assert [row.event_id for row in queue.pending()] == [record.event_id]
    queue.apply(
        STREAM,
        (IngestAppliedEvent(record.event_id, 8, 0.4),),
        credits=(PoolCreditOp("credit", PROFILE, 0.4, TurnRange(8, 8)),),
        at=14.0,
    )
    assert meta.pool_state(PROFILE).balance == 0.4


def test_prune_bounds_terminal_rows_but_keeps_watermarks(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    for ordinal in range(1, 12):
        record = _record(ordinal)
        queue.record_envelope(record)
        queue.apply(
            STREAM,
            (IngestAppliedEvent(record.event_id, ordinal, 0.1),),
            credits=(),
            at=float(ordinal),
        )
    removed = queue.prune(keep=3)
    assert removed == 8
    rows = meta._conn.execute("SELECT COUNT(*) FROM ingest_events").fetchone()
    assert rows is not None and rows[0] == 3
    assert queue.watermark(STREAM) == (11, 11)
