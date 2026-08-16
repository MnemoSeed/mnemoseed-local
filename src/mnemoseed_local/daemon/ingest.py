"""Daemon capture surface: POST /ingest and POST /session/end.

Receives Tier 1 host hook events (design/06 2.5), segments them into Turns,
and hands them to the CapturePipeline seam for the F1-F3 funnel (later tasks).
Profile identity is the payload's explicit profile_id only — the daemon never
guesses identity. Token auth lands with PRD-06; only the shape is reserved.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from mnemoseed_local.capture import (
    ProfileMismatchError,
    SessionSettledError,
    SessionUnknownError,
    TurnSegmenter,
)
from mnemoseed_local.schema.turn import FlushRequest, IngestEvent, SessionEndRequest

router = APIRouter()


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(event: IngestEvent, request: Request) -> dict[str, Any]:
    segmenter: TurnSegmenter = request.app.state.segmenter
    try:
        segmenter.ingest(event)
    except ProfileMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SessionSettledError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {
        "status": "accepted",
        "session_id": event.session_id,
        "profile_id": event.profile_id,
        "event": event.event.value,
    }


@router.post("/session/end")
async def session_end(req: SessionEndRequest, request: Request) -> dict[str, Any]:
    segmenter: TurnSegmenter = request.app.state.segmenter
    try:
        turn_range = segmenter.end_session(req.session_id, req.profile_id)
    except SessionUnknownError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProfileMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # v1 drain trigger: no scheduled drain exists yet, so settlement is the
    # natural off-HTTP-path moment to move buffered turns through the F2-F4
    # funnel. /ingest stays submit-only (the client never waits on writes).
    # Guarded so injection seams that bind a drain-less pipeline keep working.
    drain = getattr(getattr(request.app.state, "capture", None), "drain", None)
    if drain is not None:
        drain(req.session_id)
    # The dream chain then runs off the hot path, AFTER the drain persisted the
    # chunks: the ScorePool relay buffered any fired dream events during scoring
    # and flushing here hands them to the worker, so a launched dream's snapshot
    # actually contains the turns that scored it (no empty-capture race). The
    # flush only enqueues — the dream chain itself runs on the worker thread.
    relay = getattr(request.app.state, "dream_relay", None)
    if relay is not None:
        await relay.flush()
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
    drain-less pipeline working.
    """
    segmenter: TurnSegmenter = request.app.state.segmenter
    try:
        closed = segmenter.flush(req.session_id, req.profile_id)
    except SessionUnknownError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProfileMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    drain = getattr(getattr(request.app.state, "capture", None), "drain", None)
    if drain is not None:
        drain(req.session_id)
    return {
        "status": "flushed",
        "session_id": req.session_id,
        "profile_id": req.profile_id,
        "closed_turns": closed,
    }
