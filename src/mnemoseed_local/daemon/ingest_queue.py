"""Durable ingest queue over the meta store.

Every accepted capture event is written here before its 202. The seen watermark
follows the pre-ack advance; the applied watermark follows a successful drain.
Boot replay reads the difference and hands the raw envelopes back to the
existing segmenter.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence

from mnemoseed_local.schema.turn import IngestEvent
from mnemoseed_local.storage.ports import (
    IngestAppliedEvent,
    IngestRecord,
    IngestStream,
    MetaStore,
    PoolCreditOp,
)

EVENT_KIND = "event"
SESSION_END_KIND = "session_end"
FLUSH_KIND = "flush"
INGEST_RETAINED_ROWS = 512


def derive_ingest_event_id(
    profile_id: str,
    session_id: str,
    rollout_id: str | None,
    ordinal: int,
    content: str,
) -> str:
    """Hash the stream identity, ordinal, and canonical content into one id."""
    canonical = json.dumps(
        [profile_id, session_id, rollout_id or "", ordinal, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IngestQueue:
    """The durable queue surface shared by the ingest route and boot recovery."""

    def __init__(self, meta: MetaStore) -> None:
        self._meta = meta

    @property
    def meta(self) -> MetaStore:
        return self._meta

    def record_envelope(self, record: IngestRecord) -> None:
        self._meta.ingest_record(record)

    def record(self, event: IngestEvent, actor: str) -> str:
        event_id = derive_ingest_event_id(
            event.profile_id,
            event.session_id,
            event.transcript_rollout_id,
            event.transcript_ordinal if event.transcript_ordinal is not None else -1,
            json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        )
        envelope = IngestRecord(
            event_id=event_id,
            profile_id=event.profile_id,
            session_id=event.session_id,
            rollout_id=event.transcript_rollout_id or "",
            ordinal=event.transcript_ordinal if event.transcript_ordinal is not None else -1,
            kind=EVENT_KIND,
            payload=json.dumps(event.model_dump(mode="json"), separators=(",", ":")),
            actor=actor,
            host=event.host.value,
            received_at=event.ts,
        )
        self._meta.ingest_record(envelope)
        return event_id

    def record_marker(self, kind: str, profile_id: str, session_id: str, rollout_id: str | None) -> str:
        event_id = derive_ingest_event_id(profile_id, session_id, rollout_id, -1, kind)
        record = IngestRecord(
            event_id=event_id,
            profile_id=profile_id,
            session_id=session_id,
            rollout_id=rollout_id or "",
            ordinal=-1,
            kind=kind,
            payload=json.dumps({"kind": kind}, separators=(",", ":")),
            actor="daemon",
            host="daemon",
            received_at=0.0,
        )
        self._meta.ingest_record(record)
        return event_id

    def record_non_turn(self, event: IngestEvent, actor: str) -> None:
        """Persist and apply an event that never becomes a Turn."""
        event_id = self.record(event, actor)
        stream = IngestStream(event.profile_id, event.session_id, event.transcript_rollout_id or "")
        ordinal = event.transcript_ordinal if event.transcript_ordinal is not None else -1
        self.apply(
            stream,
            (IngestAppliedEvent(event_id, ordinal, 0.0),),
            credits=(),
            at=time.time(),
        )

    def record_session_marker(self, kind: str, profile_id: str, session_id: str) -> str:
        pending = self.pending(session_id=session_id)
        rollout_id = pending[-1].rollout_id if pending else ""
        return self.record_marker(kind, profile_id, session_id, rollout_id)

    def seen(self, profile_id: str, session_id: str, rollout_id: str | None, ordinal: int) -> bool:
        seen, _ = self._meta.ingest_watermarks(IngestStream(profile_id, session_id, rollout_id or ""))
        return seen >= ordinal

    def watermark(self, stream: IngestStream) -> tuple[int, int]:
        return self._meta.ingest_watermarks(stream)

    def advance_seen(self, stream: IngestStream, ordinal: int) -> int:
        return self._meta.ingest_seen(stream.profile_id, stream.session_id, stream.rollout_id, ordinal)

    def pending(
        self, *, profile_id: str | None = None, session_id: str | None = None
    ) -> tuple[IngestRecord, ...]:
        return self._meta.ingest_pending(profile_id=profile_id, session_id=session_id)

    def apply(
        self,
        stream: IngestStream,
        applications: Sequence[IngestAppliedEvent],
        *,
        credits: Sequence[PoolCreditOp] = (),
        at: float,
    ) -> None:
        self._meta.ingest_apply(
            stream=stream,
            applications=applications,
            credits=credits,
            at=at,
        )

    def prune(self, *, keep: int) -> int:
        return self._meta.ingest_prune(keep=keep)
