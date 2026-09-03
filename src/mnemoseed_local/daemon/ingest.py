"""Daemon capture surface: POST /ingest and POST /session/end.

Receives Tier 1 host hook events (design/06 2.5), segments them into Turns,
and hands them to the CapturePipeline seam for the F1-F3 funnel (later tasks).
Profile identity is the payload's explicit profile_id only — the daemon never
guesses identity. Token auth lands with PRD-06; only the shape is reserved.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from mnemoseed_local.capture import (
    ProfileMismatchError,
    SessionSettledError,
    SessionUnknownError,
    TurnSegmenter,
)
from mnemoseed_local.config import Config
from mnemoseed_local.daemon.actor import resolve_actor
from mnemoseed_local.daemon.observability import Observability
from mnemoseed_local.schema.turn import (
    FlushRequest,
    IngestEvent,
    IngestEventType,
    MessageContent,
    ProviderErrorContent,
    SessionEndRequest,
)
from mnemoseed_local.storage.ports import (
    ErrorEvent,
    ErrorSignalType,
    EvidenceKind,
    EvidencePointer,
    TurnRange,
)
from mnemoseed_local.util.daemon_executor import DaemonExecutor

router = APIRouter()

logger = logging.getLogger("mnemoseed_local.daemon.ingest")

# B2.1 T2 focal scan pool (F2 根治 D4): a module-level daemon-thread singleton,
# NEVER closed — scan threads die with the process (watchdog/announcer
# precedent), so a wedged scan can never block interpreter exit.
scan_executor = DaemonExecutor(max_workers=2, thread_name_prefix="mnemoseed-scan")


def _observability(request: Request) -> Observability | None:
    return getattr(request.app.state, "observability", None)


def _effective_ingest_profile(event: IngestEvent, config: Config | None) -> str:
    """Effective profile for capture-side routing (#130).

    When the ingest's origin agent is bound, the effective profile is the
    bound profile, otherwise the wire profile_id. Uses the live config's
    profile_for helper so archived-does-not-unbind.
    """
    if config is not None and event.agent:
        bound: str | None = config.profiles.profile_for(event.agent)
        if bound is not None:
            return bound
    return event.profile_id


_PROVIDER_ALLOWLIST = frozenset(
    {"quota", "rate_limit", "auth", "model_unavailable", "timeout", "overloaded", "other_provider"}
)
_REASON_RE = __import__("re").compile(r"^provider_[a-z0-9_]+$")
_STATUS_RETRYABLE: dict[str, int] = {
    "quota": 0,
    "rate_limit": 1,
    "auth": 0,
    "model_unavailable": 0,
    "timeout": 1,
    "overloaded": 1,
    "other_provider": 0,
}
_SECRET_SUBSTRINGS = ("sk-", "Bearer", "Authorization", "token=", "key=", "secret")


def _redact_safe_id(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return ""
    low = v.lower()
    for s in _SECRET_SUBSTRINGS:
        if s.lower() in low:
            return ""
    # allow only safe chars; URL or credential shaped -> empty
    if "://" in v or "/" in v and v.count("/") > 1:
        # model split contains one slash, provider alone must not contain //
        # keep simple: if contains url-like, drop
        if "http" in low:
            return ""
    # bare id pattern
    if not __import__("re").match(r"^[A-Za-z0-9._/-]+$", v):
        return ""
    if len(v) > 64:
        return ""
    return v


def _handle_provider_error(event: IngestEvent, effective_profile: str, request: Request) -> bool:
    """Validate and persist a provider_error nomination. Returns True if handled."""
    content = event.content
    if not isinstance(content, ProviderErrorContent):
        return False
    provider = _redact_safe_id(content.provider)
    if not provider:
        logger.debug("provider_error dropped: empty provider after redaction")
        return True
    status = content.status.strip()
    if status not in _PROVIDER_ALLOWLIST:
        logger.debug("provider_error dropped: status %r not in allowlist", status)
        return True
    reason = content.reason.strip()
    if not reason or len(reason) > 64 or _REASON_RE.match(reason) is None:
        logger.debug("provider_error dropped: invalid reason %r", reason)
        return True
    model = _redact_safe_id(content.model) if content.model else None
    # retryable derived
    retryable = _STATUS_RETRYABLE.get(status)
    # build ledger row
    stores = getattr(request.app.state, "stores", None)
    meta = getattr(stores, "meta", None) if stores is not None else None
    if meta is None:
        meta = getattr(request.app.state, "memory", None)
        # fallback: try stores via memory
        if meta is not None and hasattr(meta, "_stores"):
            meta = meta._stores.meta  # type: ignore[attr-defined]
        else:
            logger.warning("provider_error no meta store available")
            return True
    evt = ErrorEvent(
        profile_id=effective_profile,
        signal_type=ErrorSignalType.PROVIDER_FAILURE,
        observed_at=event.ts,
        evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id=event.session_id),
        session_id=event.session_id,
        detector_id="provider_error.v1",
        provider=provider,
        model=model,
        status=status,
        reason=reason,
        retryable=retryable,
    )
    try:
        meta.append_error_event(evt)  # type: ignore[union-attr]
    except Exception:
        logger.warning("provider_error append failed", exc_info=True)
    return True


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(event: IngestEvent, request: Request) -> dict[str, Any]:
    segmenter: TurnSegmenter = request.app.state.segmenter
    observability = _observability(request)
    config = getattr(request.app.state, "config", None)
    effective_profile = _effective_ingest_profile(event, config)
    if observability is not None:
        # B2.12: capture-hook activity (vs other actors) feeds the doctor's
        # registered-but-never-connected check; every sighting feeds the
        # first-sighting profile hygiene. Observational only. Sight the
        # effective profile for bound agents.
        if resolve_actor(request) == "hook":
            observability.note_capture_ingest()
        observability.note_profile_sighting(effective_profile)
    # B1 provider-error nomination path — never falls into focal scan
    if event.event is IngestEventType.PROVIDER_ERROR:
        _handle_provider_error(event, effective_profile, request)
        return {
            "status": "accepted",
            "session_id": event.session_id,
            "profile_id": event.profile_id,
            "event": event.event.value,
        }
    try:
        segmenter.ingest(event)
    except ProfileMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SessionSettledError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # B2.1 T2 (PRD-B2.1): user prompts run the embedding-free focal scan and
    # park the pending-recall selection BEFORE the 202 answers (ack-implies-
    # ready). The scan is store I/O, so it runs in a worker thread; the
    # TurnSegmenter itself stays on the event loop (it is not thread-safe —
    # a sync-def handler would threadpool it into a real race).
    if (
        event.event is IngestEventType.USER_PROMPT
        and isinstance(event.content, MessageContent)
        and event.content.text
    ):
        memory = getattr(request.app.state, "memory", None)
        if memory is not None and config is not None and config.capture.auto_recall:
            try:
                await asyncio.wrap_future(
                    scan_executor.submit(
                        memory.note_user_prompt,
                        effective_profile,
                        event.session_id,
                        event.content.text,
                    )
                )
            except Exception:  # pragma: no cover - the scan must never fail ingest
                logger.warning("focal scan failed; ingest proceeds", exc_info=True)
    return {
        "status": "accepted",
        "session_id": event.session_id,
        "profile_id": event.profile_id,
        "event": event.event.value,
    }


@router.post("/session/end")
async def session_end(req: SessionEndRequest, request: Request) -> dict[str, Any]:
    observability = _observability(request)
    if observability is not None:
        # settlement has no agent — sight the wire profile_id (effective is
        # resolved at capture drain/pending sweep, not here)
        observability.note_profile_sighting(req.profile_id)
    segmenter: TurnSegmenter = request.app.state.segmenter
    try:
        turn_range = segmenter.end_session(req.session_id, req.profile_id)
    except SessionUnknownError:
        # B2.1 T2 NIT-5b: a settle for a session the segmenter never captured
        # (never ingested, or a fresh hook process) is still TERMINAL for the
        # pending-recall lifecycle — the memory seams below must run and the
        # fire-and-forget hook must not swallow a 404 silently. Answer a 200
        # no-op settle (zero turns) instead of rejecting.
        turn_range = TurnRange(start=0, end=-1)
    except ProfileMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # v1 drain trigger: no scheduled drain exists yet, so settlement is the
    # natural off-HTTP-path moment to move buffered turns through the F2-F4
    # funnel. /ingest stays submit-only (the client never waits on writes).
    # B6 (W-C): the drain runs on the daemon drain lane thread, so the settle
    # never blocks the event loop for the whole store write; the response still
    # waits for the drain future (ack = completed-applied). Guarded so injection
    # seams that bind a drain-less pipeline keep working.
    lane = getattr(request.app.state, "drain_lane", None)
    drain = getattr(getattr(request.app.state, "capture", None), "drain", None)
    if drain is not None:
        if lane is not None:
            await lane.drain(drain, req.session_id)
        else:
            drain(req.session_id)
    # QA-5: the turns are persisted by the drain, so the settled session can
    # hand its buffers back (same guarded-seam pattern as drain).
    prune = getattr(getattr(request.app.state, "capture", None), "prune_settled", None)
    if prune is not None:
        prune(req.session_id)
    # The dream chain then runs off the hot path, AFTER the drain persisted the
    # chunks: the ScorePool relay buffered any fired dream events during scoring
    # and flushing here hands them to the worker, so a launched dream's snapshot
    # actually contains the turns that scored it (no empty-capture race). The
    # flush only enqueues — the dream chain itself runs on the worker thread.
    relay = getattr(request.app.state, "dream_relay", None)
    if relay is not None:
        await relay.flush()
    # B2.1 T2: the terminal settle drops the session's pending-recall slot and
    # seen-set — a pull after the settle finds nothing to serve. Runs for
    # fresh and repeat settles alike (the guarded seam keeps injection-only
    # test harnesses working).
    memory = getattr(request.app.state, "memory", None)
    if memory is not None:
        memory.end_session(req.profile_id, req.session_id)
    return {
        "status": "settled",
        "session_id": req.session_id,
        "profile_id": req.profile_id,
        "turns": turn_range.end - turn_range.start + 1,
        "turn_range": turn_range,
    }


@router.post("/flush")
async def flush(req: FlushRequest, request: Request) -> dict[str, Any]:
    """PreCompact rescue (design/06 4, FR-6.3): close the in-flight turn and
    drain it off the hot path WITHOUT settling the session.

    Closure alone keeps the session ingestable; the subsequent /session/end
    still settles and drains any turns opened after the flush. The same
    guarded-drain spine as /session/end keeps injection seams that bind a
    drain-less pipeline working; the drain runs on the daemon drain lane thread
    and the response waits for it (ack = completed-applied, B6 W-C).
    """
    observability = _observability(request)
    if observability is not None:
        # flush has no agent — sight the wire profile_id
        observability.note_profile_sighting(req.profile_id)
    segmenter: TurnSegmenter = request.app.state.segmenter
    try:
        closed = segmenter.flush(req.session_id, req.profile_id)
    except SessionUnknownError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProfileMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    lane = getattr(request.app.state, "drain_lane", None)
    drain = getattr(getattr(request.app.state, "capture", None), "drain", None)
    if drain is not None:
        if lane is not None:
            await lane.drain(drain, req.session_id)
        else:
            drain(req.session_id)
    return {
        "status": "flushed",
        "session_id": req.session_id,
        "profile_id": req.profile_id,
        "closed_turns": closed,
    }
