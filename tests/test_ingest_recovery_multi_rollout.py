"""Applied markers and watermarks stay per rollout during a drain."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.daemon.ingest_queue import IngestQueue, derive_ingest_event_id
from mnemoseed_local.daemon.ingest_recovery import queue_drain_fn
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import IngestRecord, IngestStream, PoolCreditOp, TurnRange

PROFILE = "default"
SESSION = "sess-two-rollouts"
A = "rollout-a"
B = "rollout-b"


@pytest.fixture
def meta(tmp_path: Path) -> Any:
    driver = SqliteMetaDriver(path=tmp_path / "meta.db")
    yield driver
    asyncio.run(driver.close())


class _Capture:
    def __init__(self, credit: bool = False) -> None:
        self.credit = credit

    def drain(self, session_id: str, credit_ops: list[PoolCreditOp] | None = None) -> tuple[()]:
        if credit_ops is not None and self.credit:
            credit_ops.append(PoolCreditOp("credit", PROFILE, 1.0, TurnRange(0, 2)))
        return ()


def _record(rollout: str, ordinal: int, text: str = "hello") -> IngestRecord:
    return IngestRecord(
        event_id=derive_ingest_event_id(PROFILE, SESSION, rollout, ordinal, text),
        profile_id=PROFILE,
        session_id=SESSION,
        rollout_id=rollout,
        ordinal=ordinal,
        kind="event",
        payload='{"text": "hello"}',
        actor="hook",
        host="opencode",
        received_at=float(ordinal),
    )


def test_two_rollouts_advance_independent_watermarks_and_markers(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    for rollout in (A, B):
        for ordinal in (1, 2):
            queue.record_envelope(_record(rollout, ordinal))

    queue_drain_fn(queue, _Capture())(SESSION)

    assert queue.watermark(IngestStream(PROFILE, SESSION, A)) == (2, 2)
    assert queue.watermark(IngestStream(PROFILE, SESSION, B)) == (2, 2)

    rows = meta._conn.execute("SELECT event_id, rollout_id FROM ingest_applied").fetchall()
    labels = {row["event_id"]: row["rollout_id"] for row in rows}
    for rollout in (A, B):
        for ordinal in (1, 2):
            event_id = derive_ingest_event_id(PROFILE, SESSION, rollout, ordinal, "hello")
            assert labels[event_id] == rollout, "a marker must carry its own stream's rollout"


def test_new_ordinal_on_each_stream_is_replayed_after_drain(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    queue.record_envelope(_record(A, 1))
    for ordinal in (1, 2):
        queue.record_envelope(_record(B, ordinal))

    drain = queue_drain_fn(queue, _Capture())

    drain(SESSION)
    queue.record_envelope(_record(A, 2, "later-a"))
    queue.record_envelope(_record(B, 3, "later-b"))

    pending = {(record.rollout_id, record.ordinal) for record in queue.pending(session_id=SESSION)}
    assert pending == {(A, 2), (B, 3)}, "a new ordinal on either stream must stay replayable"

    drain(SESSION)
    assert queue.watermark(IngestStream(PROFILE, SESSION, A)) == (2, 2)
    assert queue.watermark(IngestStream(PROFILE, SESSION, B)) == (3, 3)


def test_credits_are_applied_once_for_two_streams_of_one_profile(meta: SqliteMetaDriver) -> None:
    queue = IngestQueue(meta)
    for rollout in (A, B):
        queue.record_envelope(_record(rollout, 1))

    queue_drain_fn(queue, _Capture(credit=True))(SESSION)

    assert meta.pool_state(PROFILE).balance == 1.0
