"""Boot replay and drain application for the durable ingest queue."""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Callable
from typing import Any

from mnemoseed_local.capture import (
    ProfileMismatchError,
    SessionSettledError,
    SessionUnknownError,
    TurnSegmenter,
)
from mnemoseed_local.daemon.ingest_queue import (
    EVENT_KIND,
    FLUSH_KIND,
    INGEST_RETAINED_ROWS,
    SESSION_END_KIND,
    IngestQueue,
)
from mnemoseed_local.schema.turn import IngestEvent
from mnemoseed_local.storage.ports import IngestAppliedEvent, IngestStream, PoolCreditOp


def queue_drain_fn(
    queue: IngestQueue,
    capture: Any,
    *,
    credit_pool: bool = True,
) -> Callable[[str], Any]:
    """Wrap a capture drain so a successful one applies every pending event.

    Pool mutations are collected into ``credit_ops`` and committed with the
    applied markers in one meta transaction. The chunk write happens in the
    vector store before that transaction.
    """

    def drain(session_id: str) -> Any:
        try:
            parameters = inspect.signature(capture.drain).parameters
            accepts_ops = "credit_ops" in parameters or any(
                param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()
            )
        except TypeError, ValueError:
            accepts_ops = False
        pending = queue.pending(session_id=session_id)
        credit_ops: list[PoolCreditOp] = []
        if credit_pool and accepts_ops:
            outcomes = capture.drain(session_id, credit_ops=credit_ops)
        elif credit_pool:
            outcomes = capture.drain(session_id)
        else:
            outcomes = capture.drain(session_id, credit_pool=False)
        streams: dict[IngestStream, list[Any]] = {}
        for record in pending:
            streams.setdefault(record.stream, []).append(record)
        credited_profiles: set[str] = set()
        for stream, records in streams.items():
            applications = tuple(
                IngestAppliedEvent(record.event_id, record.ordinal, 0.0) for record in records
            )
            credits: tuple[PoolCreditOp, ...] = ()
            if stream.profile_id not in credited_profiles:
                credits = tuple(op for op in credit_ops if op.profile_id == stream.profile_id)
                credited_profiles.add(stream.profile_id)
            queue.apply(stream, applications, credits=credits, at=time.time())
        for profile_id in {op.profile_id for op in credit_ops} - credited_profiles:
            queue.apply(
                IngestStream(profile_id, session_id, ""),
                (),
                credits=tuple(op for op in credit_ops if op.profile_id == profile_id),
                at=time.time(),
            )
        return outcomes

    return drain


def recover_pending(
    queue: IngestQueue,
    segmenter: TurnSegmenter,
    drain: Callable[[str], Any],
) -> tuple[str, ...]:
    """Feed seen-but-unapplied envelopes to the segmenter in insertion order.

    Marker rows restore settled/flush state. Each touched session is closed and
    drained, which applies its pending events through ``drain``.
    """
    pending = queue.pending()
    streams: dict[str, IngestStream] = {}
    settled: set[str] = set()
    for record in pending:
        streams[record.session_id] = record.stream
        if record.kind == EVENT_KIND:
            event = IngestEvent.model_validate(json.loads(record.payload))
            try:
                segmenter.ingest(event)
            except ProfileMismatchError, SessionSettledError:
                pass
        elif record.kind == SESSION_END_KIND:
            settled.add(record.session_id)
        elif record.kind == FLUSH_KIND:
            try:
                segmenter.flush(record.session_id, record.profile_id)
            except SessionUnknownError:
                pass
    for session_id, stream in streams.items():
        if session_id in settled:
            try:
                segmenter.end_session(session_id, stream.profile_id)
            except SessionUnknownError:
                pass
        else:
            try:
                segmenter.flush(session_id, stream.profile_id)
            except SessionUnknownError:
                pass
        drain(session_id)
    queue.prune(keep=INGEST_RETAINED_ROWS)
    return tuple(streams)
