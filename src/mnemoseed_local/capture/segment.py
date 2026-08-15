"""Turn segmentation — hook event stream to structured Turns.

One in-memory state machine per session_id. Turn boundaries are anchored by
explicit user_prompt events (Claude Code / Codex / Gemini). Hosts without a
user-prompt hook (Cursor: afterAgentResponse + postToolUse only) are
segmented on response boundaries: a second assistant_message after an
assistant step closes the preceding turn.
"""

from __future__ import annotations

import time
from typing import NoReturn

from mnemoseed_local.capture.pipeline import CapturePipeline
from mnemoseed_local.schema.turn import (
    IngestEvent,
    IngestEventType,
    MessageContent,
    ToolContent,
    Turn,
    TurnRole,
    TurnStep,
)
from mnemoseed_local.storage.ports import TurnRange


class CaptureError(Exception):
    """Base error for the capture intake paths."""


class SessionUnknownError(CaptureError):
    """/session/end for a session that never posted to /ingest."""


class SessionSettledError(CaptureError):
    """/ingest after /session/end already settled the session."""


class ProfileMismatchError(CaptureError):
    """A payload's profile_id conflicts with the session's bound profile."""


class _SessionState:
    """Per-session segmentation state (bound profile, open turn, indices)."""

    def __init__(self, session_id: str, profile_id: str) -> None:
        self.session_id = session_id
        self.profile_id = profile_id
        self.turn_index = 0
        self.open_turn: Turn | None = None
        self.turn_range: TurnRange | None = None

    def ingest(self, event: IngestEvent, pipeline: CapturePipeline) -> None:
        if event.profile_id != self.profile_id:
            raise ProfileMismatchError(
                f"session {self.session_id!r} is bound to profile {self.profile_id!r}, "
                f"got {event.profile_id!r}"
            )
        if self.turn_range is not None:
            raise SessionSettledError(
                f"session {self.session_id!r} already settled; no further ingest accepted"
            )
        if event.event is IngestEventType.USER_PROMPT:
            content = event.content
            if not isinstance(content, MessageContent):
                self._invalid(event)
            turn = self._start_turn(event, pipeline)
            turn.steps.append(TurnStep(role=TurnRole.USER, content=content.text))
        elif event.event is IngestEventType.ASSISTANT_MESSAGE:
            content = event.content
            if not isinstance(content, MessageContent):
                self._invalid(event)
            open_turn = self.open_turn
            if open_turn is None or any(step.role is TurnRole.ASSISTANT for step in open_turn.steps):
                open_turn = self._start_turn(event, pipeline)
            open_turn.steps.append(TurnStep(role=TurnRole.ASSISTANT, content=content.text))
            if content.model_id:
                open_turn.model_id = content.model_id
        else:  # TOOL_USE
            content = event.content
            if not isinstance(content, ToolContent):
                self._invalid(event)
            tool_turn = self.open_turn
            if tool_turn is None:
                tool_turn = self._start_turn(event, pipeline)
            tool_turn.steps.append(
                TurnStep(
                    role=TurnRole.TOOL,
                    content=content.output,
                    tool_name=content.tool_name,
                    tool_input=content.input,
                )
            )

    def end(self, profile_id: str, pipeline: CapturePipeline) -> TurnRange:
        if profile_id != self.profile_id:
            raise ProfileMismatchError(
                f"session {self.session_id!r} is bound to profile {self.profile_id!r}, got {profile_id!r}"
            )
        if self.turn_range is not None:
            return self.turn_range
        if self.open_turn is not None:
            self._close_open_turn(time.time(), pipeline)
        self.turn_range = TurnRange(start=0, end=self.turn_index - 1)
        pipeline.end_session(self.session_id, self.turn_range)
        return self.turn_range

    def flush(self, profile_id: str, pipeline: CapturePipeline) -> int:
        """Close the in-flight turn (if any) without settling the session.

        The closed turn is handed to the pipeline (and drained by the caller)
        but ``turn_range`` stays unset, so the session keeps accepting further
        input and the eventual :meth:`end` still settles it. Returns the number
        of turns closed by this call (0 for an already-flushed or settled
        session).
        """
        if profile_id != self.profile_id:
            raise ProfileMismatchError(
                f"session {self.session_id!r} is bound to profile {self.profile_id!r}, got {profile_id!r}"
            )
        if self.turn_range is not None or self.open_turn is None:
            return 0
        self._close_open_turn(time.time(), pipeline)
        return 1

    def _start_turn(self, event: IngestEvent, pipeline: CapturePipeline) -> Turn:
        if self.open_turn is not None:
            self._close_open_turn(event.ts, pipeline)
        turn = Turn(
            turn_index=self.turn_index,
            session_id=self.session_id,
            profile_id=self.profile_id,
            host=event.host,
            started_at=event.ts,
            importance_hint=event.importance_hint,
        )
        self.turn_index += 1
        self.open_turn = turn
        return turn

    def _close_open_turn(self, ended_at: float, pipeline: CapturePipeline) -> Turn:
        turn = self.open_turn
        if turn is None:  # pragma: no cover - only called with an open turn
            raise CaptureError("internal: no open turn to close")
        turn.ended_at = ended_at
        turn.closed = True
        self.open_turn = None
        pipeline.submit_turn(turn)
        return turn

    @staticmethod
    def _invalid(event: IngestEvent) -> NoReturn:
        raise CaptureError(
            f"event/content shape mismatch for event {event.event.value} (host {event.host.value})"
        )


class TurnSegmenter:
    """Splits each session's hook stream into Turns and hands them downstream."""

    def __init__(self, pipeline: CapturePipeline) -> None:
        self._pipeline = pipeline
        self._sessions: dict[str, _SessionState] = {}

    def ingest(self, event: IngestEvent) -> None:
        state = self._sessions.get(event.session_id)
        if state is None:
            state = _SessionState(event.session_id, event.profile_id)
            self._sessions[event.session_id] = state
        state.ingest(event, self._pipeline)

    def end_session(self, session_id: str, profile_id: str) -> TurnRange:
        state = self._sessions.get(session_id)
        if state is None:
            raise SessionUnknownError(f"session {session_id!r} not captured; nothing to settle")
        return state.end(profile_id, self._pipeline)

    def flush(self, session_id: str, profile_id: str) -> int:
        """PreCompact rescue: hand the in-flight turn to the pipeline without
        settling the session (design/06 4). Returns the closed-turn count."""
        state = self._sessions.get(session_id)
        if state is None:
            raise SessionUnknownError(f"session {session_id!r} not captured; nothing to flush")
        return state.flush(profile_id, self._pipeline)
