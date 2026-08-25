"""Claude Code hook stdin -> daemon REST normalization (transformer lane).

Claude Code pipes one JSON object per hook firing into
``mnemoseed-local _hook-event --host claude_code`` (registered by
``hosts.claude_code.install``). Event map v1:

- ``UserPromptSubmit`` -> ``user_prompt`` /ingest (``agent`` lifted when the
  payload carries one; blank stays an honest null via the wire validator);
- ``Stop``            -> ``assistant_message`` /ingest — flush semantics only,
  NEVER a session settle; the model id comes from the transcript JSONL tail
  and any parse failure degrades to a model-less event, never a drop;
- ``PostToolUse``     -> ``tool_use`` /ingest (output capped);
- ``PreCompact``      -> POST /flush;
- ``SessionEnd``      -> POST /session/end, fire-and-forget without awaiting
  the daemon's drain (CC gives SessionEnd hooks a shared ~1.5s budget; the
  daemon's teardown flush is the backstop);
- ``SubagentStart``/``SubagentStop`` are NOT ingested in v1 (the raw payload
  records agent info for later adoption).

RED LINE: stdout stays EMPTY on every path — CC feeds UserPromptSubmit stdout
back into model context. Failures are swallowed into the opt-in stderr debug
lane (``MNEMOSEED_LOCAL_DEBUG``), mirroring the shipped opencode plugin.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from httpx import Timeout
from pydantic import BaseModel

from mnemoseed_local.schema.turn import (
    FlushRequest,
    HostId,
    IngestEvent,
    IngestEventType,
    SessionEndRequest,
)

#: kind -> daemon REST endpoint (kind names are the transformer's action tags).
ENDPOINTS = {"ingest": "/ingest", "flush": "/flush", "session_end": "/session/end"}

#: Fire-and-forget POST budget (mirrors the opencode plugin's 2s discipline).
POST_TIMEOUT_SECONDS = 2.0

#: Per-phase cap for SessionEnd posts: CC enforces a shared ~1.5s teardown
#: clock on SessionEnd hooks while /session/end drains server-side, so the
#: call must give up fast (residual worst-case noted in the PRD boundary).
SESSION_END_TIMEOUT_SECONDS = 0.5


def endpoint_budget(kind: str) -> float | Timeout:
    """POST budget per action kind (see SESSION_END_TIMEOUT_SECONDS)."""
    if kind == "session_end":
        return Timeout(SESSION_END_TIMEOUT_SECONDS)
    return POST_TIMEOUT_SECONDS


MAX_TOOL_OUTPUT_CHARS = 8000

TOOL_TRUNCATION_MARKER = "[... truncated]"

Action = tuple[str, BaseModel]

Normalizer = Callable[[dict[str, Any], str, float], Action | None]

_NORMALIZERS: dict[str, Normalizer] = {}


def _normalizer(name: str) -> Callable[[Normalizer], Normalizer]:
    def register(fn: Normalizer) -> Normalizer:
        _NORMALIZERS[name] = fn
        return fn

    return register


def debug_enabled() -> bool:
    return bool(os.environ.get("MNEMOSEED_LOCAL_DEBUG"))


def debug(message: str) -> None:
    """Opt-in failure lane: stderr only (never stdout), silent unless enabled."""
    if debug_enabled():
        print(f"mnemoseed-local: {message}", file=sys.stderr)


def profile_id() -> str:
    """Profile binding convention: env override or the default profile."""
    return os.environ.get("MNEMOSEED_LOCAL_PROFILE_ID") or "default"


def normalize_event(payload: dict[str, Any], *, now: float) -> Action | None:
    """Map one CC hook payload to a daemon action; ``None`` means drop."""
    name = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not isinstance(name, str) or not isinstance(session_id, str) or not session_id.strip():
        return None
    normalizer = _NORMALIZERS.get(name)
    return normalizer(payload, session_id, now) if normalizer else None


def _ingest(
    event: IngestEventType, payload: dict[str, Any], session_id: str, now: float, **extra: Any
) -> Action:
    body = IngestEvent(
        host=HostId.CLAUDE_CODE,
        event=event,
        session_id=session_id,
        profile_id=profile_id(),
        ts=now,
        raw=payload,
        **extra,
    )
    return ("ingest", body)


def _agent_of(payload: dict[str, Any]) -> str | None:
    value = payload.get("agent") or payload.get("agent_type")
    if isinstance(value, str):
        return value.strip() or None
    return None


@_normalizer("UserPromptSubmit")
def _user_prompt(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return _ingest(
        IngestEventType.USER_PROMPT,
        payload,
        session_id,
        now,
        content={"text": prompt},
        agent=_agent_of(payload),
    )


@_normalizer("Stop")
def _stop(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    model, transcript_text = _transcript_tail(payload.get("transcript_path"))
    text = payload.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        text = transcript_text
    return _ingest(
        IngestEventType.ASSISTANT_MESSAGE,
        payload,
        session_id,
        now,
        content={"text": text, "model_id": model},
    )


def _transcript_tail(path: Any) -> tuple[str | None, str]:
    """(model, assistant text) of the newest assistant entry in the JSONL.

    Every failure mode (missing/garbage file, unexpected shapes) degrades to
    ``(None, "")`` — a model-less, empty-text event still beats a dropped one.
    """
    if not isinstance(path, str) or not path:
        return None, ""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return None, ""
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        blocks = message.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return (model if isinstance(model, str) and model else None), text
    return None, ""


@_normalizer("PostToolUse")
def _tool_use(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    tool_input = payload.get("tool_input")
    output = _tool_output_text(payload.get("tool_response"))
    if len(output) > MAX_TOOL_OUTPUT_CHARS:
        output = output[:MAX_TOOL_OUTPUT_CHARS] + TOOL_TRUNCATION_MARKER
    return _ingest(
        IngestEventType.TOOL_USE,
        payload,
        session_id,
        now,
        content={
            "tool_name": tool_name,
            "input": tool_input if isinstance(tool_input, dict) else {},
            "output": output,
        },
    )


def _tool_output_text(response: Any) -> str:
    if response is None or isinstance(response, str):
        return response or ""
    if isinstance(response, dict):
        for key in ("stdout", "stderr", "content", "output"):
            value = response.get(key)
            # An empty-string stdout must not shadow a meaningful later key.
            if isinstance(value, str) and value:
                return value
        return json.dumps(response, ensure_ascii=False)
    return str(response)


@_normalizer("PreCompact")
def _pre_compact(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    return ("flush", FlushRequest(session_id=session_id, profile_id=profile_id()))


@_normalizer("SessionEnd")
def _session_end(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    return ("session_end", SessionEndRequest(session_id=session_id, profile_id=profile_id(), ts=now))
