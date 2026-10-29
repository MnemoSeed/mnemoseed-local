"""Idle-session flush sweeper: the daemon's durability net for open turns.

Hosts that fire no per-reply flush leave every turn open in the segmenter
until the next turn closes it — a session that goes quiet (crash, kill,
dropped hook) keeps its exchange in RAM only. On a tick the sweeper drains
sessions idle past ``capture.idle_flush_sec`` with flush semantics: the open
turn closes, the buffered turns persist through the capture funnel, and the
session stays open and unsettled (an explicit /session/end remains the only
settle). The flush itself runs on the event loop because the TurnSegmenter is
loop-bound; the store I/O is offloaded through the shared drain lane, so a
wedged write can never stall the sweep — the following tick simply queues
behind it on the same lane. Drains are fire-and-forget on the lane, so the
sweeper keeps a failure ledger: a drain that raises is retried (bounded by
the same per-tick budget) and WARN-logged on the next tick until it succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from mnemoseed_local.capture import TurnSegmenter, WritingPipeline
from mnemoseed_local.config import Config

logger = logging.getLogger("mnemoseed_local.daemon.idle_flush")

#: Sweep cadence: bounds how long an idle turn can sit past the threshold.
IDLE_FLUSH_TICK_S = 30.0

#: Drain budget per tick (retries included) so a backlog of idle sessions
#: cannot monopolize the shared drain lane; the remainder waits for the next
#: tick, oldest first.
MAX_SESSIONS_PER_TICK = 8


class DrainSubmitter(Protocol):
    """The drain lane's submit surface (structural: keeps app.py out of here)."""

    def submit(self, fn: Callable[[str], Any], session_id: str) -> None: ...


class IdleFlushSweeper:
    """Periodically flushes and drains idle sessions (flush, never settle).

    Candidates come from the segmenter (unsettled + open turn + idle past the
    threshold, oldest first); each is flushed inline, then drained through the
    lane when one is bound, or synchronously otherwise (injection seams). The
    threshold re-reads from the live config each tick, so a configwrite change
    hot-applies.
    """

    def __init__(
        self,
        segmenter: TurnSegmenter,
        capture: WritingPipeline,
        config: Config,
        *,
        lane: DrainSubmitter | None = None,
        clock: Callable[[], float] | None = None,
        max_per_tick: int = MAX_SESSIONS_PER_TICK,
        tick_s: float = IDLE_FLUSH_TICK_S,
    ) -> None:
        self._segmenter = segmenter
        self._capture = capture
        self._config = config
        self._lane = lane
        self._clock = clock if clock is not None else time.time
        self._max_per_tick = max_per_tick
        self._tick_s = tick_s
        self._failed_lock = threading.Lock()
        self._failed: set[str] = set()

    def run_once(self) -> list[str]:
        """One sweep pass; returns the sessions whose drain was queued.

        Failed drains from earlier ticks retry first (the same per-tick budget
        applies), then fresh idle candidates, oldest first.
        """
        swept: list[str] = []
        budget = self._max_per_tick
        with self._failed_lock:
            failed = sorted(self._failed)
        for session_id in failed[:budget]:
            logger.warning("idle flush: retrying failed drain for session %s", session_id)
            if self._submit_drain(session_id):
                swept.append(session_id)
                budget -= 1
        candidates = self._segmenter.idle_sessions(
            now=self._clock(), idle_sec=self._config.capture.idle_flush_sec
        )
        for session_id, profile_id in candidates[:budget]:
            try:
                self._segmenter.flush(session_id, profile_id)
            except Exception:
                # one wedged session must not starve the rest of the sweep
                logger.warning("idle flush failed for session %s", session_id, exc_info=True)
                continue
            if self._submit_drain(session_id):
                swept.append(session_id)
        if swept:
            logger.info("idle flush: drained %d session(s): %s", len(swept), ", ".join(swept))
        return swept

    def _submit_drain(self, session_id: str) -> bool:
        """Queue one drain (lane when bound, inline otherwise); False when the
        submission itself failed (the failure ledger already holds the session
        through :meth:`_drain`)."""
        try:
            if self._lane is not None:
                self._lane.submit(self._drain, session_id)
            else:
                self._drain(session_id)
        except Exception:
            logger.warning("idle flush failed for session %s", session_id, exc_info=True)
            return False
        return True

    def _drain(self, session_id: str) -> None:
        """The lane's callable: persist the session's buffered turns while
        keeping the failure ledger true — a success clears the retry flag, a
        failure sets it (the fire-and-forget lane never reports back)."""
        try:
            self._capture.drain(session_id)
        except Exception:
            with self._failed_lock:
                self._failed.add(session_id)
            raise
        with self._failed_lock:
            self._failed.discard(session_id)

    async def run_forever(self) -> None:
        """The daemon-owned periodic loop; ticks once immediately, then sleeps
        one cadence. Cancellation is the only stop (lifespan teardown)."""
        while True:
            try:
                self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle flush sweep failed; the next tick retries")
            await asyncio.sleep(self._tick_s)
