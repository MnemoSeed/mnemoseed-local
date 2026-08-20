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
from mnemoseed_local.schema.turn import (
    FlushRequest,
    IngestEvent,
    IngestEventType,
    MessageContent,
    SessionEndRequest,
)
from mnemoseed_local.storage.ports import TurnRange
from mnemoseed_local.util.daemon_executor import DaemonExecutor

router = APIRouter()

logger = logging.getLogger("mnemoseed_local.daemon.ingest")

# B2.1 T2 focal scan pool (F2 根治 D4): a module-level daemon-thread singleton,
# NEVER closed — scan threads die with the process (watchdog/announcer
# precedent), so a wedged scan can never block interpreter exit.
scan_executor = DaemonExecutor(max_workers=2, thread_name_prefix="mnemoseed-scan")


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(event: IngestEvent, request: Request) -> dict[str, Any]:
    segmenter: TurnSegmenter = request.app.state.segmenter
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
        config = getattr(request.app.state, "config", None)
        memory = getattr(request.app.state, "memory", None)
        if memory is not None and config is not None and config.capture.auto_recall:
            try:
                await asyncio.wrap_future(
                    scan_executor.submit(
                        memory.note_user_prompt,
                        event.profile_id,
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
    # Guarded so injection seams that bind a drain-less pipeline keep working.
    drain = getattr(getattr(request.app.state, "capture", None), "drain", None)
    if drain is not None:
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
