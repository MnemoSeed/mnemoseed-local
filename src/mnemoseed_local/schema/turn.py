"""Turn schema — the structured unit the capture funnel consumes.

Host hook payloads (design/06 2.5) are normalized into Turn structures by the
daemon /ingest segmentation (01 Stage ①). A Turn groups one user prompt with
the resulting assistant text and tool-call sequence; turn boundaries are
anchored by user prompts (hosts without a prompt hook are segmented on
response boundaries, see capture/segment.py).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, Field, model_validator

# Required identity field on every wire model. min_length=1 rejects the empty
# string; the pattern rejects whitespace-only values. A blank profile_id must
# never reach the vector drivers, where it would silently widen
# near_duplicate probes and store filters to whole-table scans.
ProfileRef = Annotated[str, Field(min_length=1, pattern=r".*\S.*")]


class HostId(StrEnum):
    """Host that produced the hook event (design/06 2.1 / 2.5)."""

    CLAUDE_CODE = "claude_code"
    CURSOR = "cursor"
    CODEX_CLI = "codex_cli"
    GEMINI_CLI = "gemini_cli"
    OPENCODE = "opencode"
    WINDSURF = "windsurf"
    GENERIC = "generic"  # any script / mnemoseed CLI direct path


class IngestEventType(StrEnum):
    """Normalized hook event kinds /ingest accepts."""

    USER_PROMPT = "user_prompt"  # Claude Code/Codex UserPromptSubmit
    ASSISTANT_MESSAGE = "assistant_message"  # CC Stop / Cursor afterAgentResponse / Gemini Stop
    TOOL_USE = "tool_use"  # CC/Cursor PostToolUse / OpenCode tool.execute.after


class TurnRole(StrEnum):
    """Role of one structured step inside a turn."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageContent(BaseModel):
    """Text-bearing event content (user_prompt / assistant_message)."""

    text: str
    model_id: str | None = None  # producing model; assistant replies only


class ToolContent(BaseModel):
    """Tool-call event content (tool_use)."""

    tool_name: str
    input: dict[str, Any]
    output: str = ""


class IngestEvent(BaseModel):
    """Envelope POSTed to /ingest by a Tier 1 host hook (design/06 2.5).

    ``profile_id`` is required and never guessed — token auth is a later
    PRD-06 concern. ``raw`` carries the host's untouched payload as a
    provenance anchor for ingest-time inspection; segmentation consumes only
    the canonical fields, so verbatim text lives in ``content.text`` / tool
    ``output`` and, downstream, the chunk text.
    """

    host: HostId
    event: IngestEventType
    session_id: str
    profile_id: ProfileRef
    ts: float
    content: MessageContent | ToolContent
    importance_hint: float | None = Field(default=None, ge=0.0, le=1.0)  # FR-1.9
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _content_matches_event(self) -> Self:
        if self.event is IngestEventType.TOOL_USE:
            if not isinstance(self.content, ToolContent):
                raise ValueError("event 'tool_use' requires content as {tool_name, input, output}")
        elif not isinstance(self.content, MessageContent):
            raise ValueError(f"event '{self.event.value}' requires content as {{text, model_id}}")
        return self


class SessionEndRequest(BaseModel):
    """Request body for /session/end (session settlement)."""

    session_id: str
    profile_id: ProfileRef
    ts: float | None = None  # host-stamped end time; server stamps when absent


class FlushRequest(BaseModel):
    """Request body for /flush (PreCompact live-site rescue, design/06 4).

    Closes the in-flight turn and drains it without settling the session, so a
    mid-session context compaction never loses the turn that was open. Unlike
    settlement, /flush leaves the session ingestable; the eventual
    /session/end still settles and drains the rest.
    """

    session_id: str
    profile_id: ProfileRef


class TurnStep(BaseModel):
    """One ordered step inside a Turn (verbatim text preserved)."""

    role: TurnRole
    content: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None


class Turn(BaseModel):
    """One structured turn handed to the capture funnel (F1-F3).

    ``closed`` flips once the boundary is known (a later user prompt or the
    session end); only closed turns are handed to the pipeline.
    """

    turn_index: int
    session_id: str
    profile_id: ProfileRef
    host: HostId
    model_id: str | None = None
    started_at: float
    ended_at: float | None = None
    closed: bool = False
    importance_hint: float | None = Field(default=None, ge=0.0, le=1.0)  # FR-1.9
    steps: list[TurnStep] = Field(default_factory=list)
