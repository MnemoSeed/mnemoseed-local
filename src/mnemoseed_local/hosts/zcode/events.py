"""ZCode hook stdin -> daemon REST normalization (transformer lane).

ZCode pipes one JSON object per hook firing into
``mnemoseed-local _hook-event --host zcode`` (registered by
``hosts.zcode.install`` as ``type: process`` entries under
``hooks.events.<Event>`` in ``~/.zcode/cli/config.json``). The stdin payload
dialect is the Claude-Code-compatible working contract pinned by
``tests/fixtures/zcode_hook/`` (PRD-272 §9.2); every field read is defensive.
Event map v1 (the four subscribed events):

- ``SessionStart``      -> the T1 read lane: one ``POST /session/recent``,
  rendered as the fenced replay block and emitted as
  ``hookSpecificOutput.additionalContext`` stdout (attempt-once per firing —
  the stateless process IS the gate); never an ingest;
- ``UserPromptSubmit``  -> ``user_prompt`` /ingest, then the T2 pull after the
  ACK (ack-implies-ready) with the served selection emitted as
  ``additionalContext`` stdout;
- ``PostToolUse``       -> ``tool_use`` /ingest (output capped);
- ``Stop``              -> ``assistant_message`` /ingest — flush semantics
  only, NEVER a session settle; the payload's ``last_assistant_message`` is
  the primary text, with the transcript JSONL tail (Claude-Code dialect,
  defensive) supplying the model id and the text fallback.

The wire ``visibility`` field is the only rendering authority (#273): absent
or off means no signal line and zero stdout. ZCode parses hook stdout as JSON
under a strict schema, so the emitted object carries only the documented
``hookSpecificOutput.{hookEventName, additionalContext}`` keys.

RED LINE: stdout stays EMPTY on every path EXCEPT a served T1/T2 block or a
visibility signal payload when the wire mode is not off. Failures land in the
opt-in stderr debug lane; a dropped or malformed event is exit 0.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from mnemoseed_local.hosts.common.daemon import (
    POST_TIMEOUT_SECONDS,
    debug,
    hook_client,
    profile_id,
)
from mnemoseed_local.hosts.common.daemon import (
    RECALL_BUDGET_KEY as RECALL_BUDGET_KEY,
)
from mnemoseed_local.hosts.common.daemon import (
    RECALL_PULL_MAX_CHARS as RECALL_PULL_MAX_CHARS,
)
from mnemoseed_local.hosts.common.daemon import (
    RECALL_PULL_TIMEOUT_SECONDS as RECALL_PULL_TIMEOUT_SECONDS,
)
from mnemoseed_local.hosts.common.fence import (
    RECALL_DISCLAIMER as RECALL_DISCLAIMER,
)
from mnemoseed_local.hosts.common.fence import (
    RECALL_FENCE_CLOSE as RECALL_FENCE_CLOSE,
)
from mnemoseed_local.hosts.common.fence import (
    RECALL_FENCE_OPEN as RECALL_FENCE_OPEN,
)
from mnemoseed_local.hosts.common.fence import (
    RECALL_FENCE_SANITIZED as RECALL_FENCE_SANITIZED,
)
from mnemoseed_local.hosts.common.payload import additional_context_payload
from mnemoseed_local.hosts.common.recall import (
    CAPTURE_SIGNAL as CAPTURE_SIGNAL,
)
from mnemoseed_local.hosts.common.recall import (
    MAX_TOOL_OUTPUT_CHARS,
    TOOL_TRUNCATION_MARKER,
    inject_recall_context,
    pull_session_tails,
    session_start_block,
    tool_output_text,
    transcript_tail,
)
from mnemoseed_local.hosts.common.recall import (
    T1_SIGNAL as T1_SIGNAL,
)
from mnemoseed_local.hosts.common.recall import (
    T2_SIGNAL as T2_SIGNAL,
)
from mnemoseed_local.hosts.common.recall import (
    build_t1_context as build_t1_context,
)
from mnemoseed_local.hosts.common.recall import (
    capture_count_line as capture_count_line,
)
from mnemoseed_local.hosts.common.recall import (
    recall_count_line as recall_count_line,
)
from mnemoseed_local.hosts.common.recall import (
    sanitize_recall_text as sanitize_recall_text,
)
from mnemoseed_local.hosts.common.t2 import build_t2_context as build_t2_context
from mnemoseed_local.hosts.common.visibility import visibility_mode as visibility_mode
from mnemoseed_local.schema.turn import (
    HostId,
    IngestEvent,
    IngestEventType,
    MessageContent,
    ToolContent,
)

#: kind -> daemon REST endpoint.
ENDPOINTS = {"ingest": "/ingest"}

#: The hook event that carries the T1 read lane (no ingest, no settle).
T1_HOOK_EVENT = "SessionStart"

#: The hook event that carries the T2 additionalContext formback.
INJECTION_HOOK_EVENT = "UserPromptSubmit"

Action = tuple[str, Any]

Normalizer = Callable[[dict[str, Any], str, float], Action | None]

_NORMALIZERS: dict[str, Normalizer] = {}


def _normalizer(name: str) -> Callable[[Normalizer], Normalizer]:
    def register(fn: Normalizer) -> Normalizer:
        _NORMALIZERS[name] = fn
        return fn

    return register


def normalize_event(payload: dict[str, Any], *, now: float) -> Action | None:
    """Map one ZCode hook payload to a daemon action; ``None`` means drop."""
    name = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not isinstance(name, str) or not isinstance(session_id, str) or not session_id.strip():
        return None
    normalizer = _NORMALIZERS.get(name)
    return normalizer(payload, session_id, now) if normalizer else None


def _ingest(
    event: IngestEventType,
    payload: dict[str, Any],
    session_id: str,
    now: float,
    content: MessageContent | ToolContent,
) -> Action:
    body = IngestEvent(
        host=HostId.ZCODE,
        event=event,
        session_id=session_id,
        profile_id=profile_id(),
        ts=now,
        content=content,
        raw=payload,
    )
    return ("ingest", body)


@_normalizer("UserPromptSubmit")
def _user_prompt(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return _ingest(IngestEventType.USER_PROMPT, payload, session_id, now, content=MessageContent(text=prompt))


@_normalizer("Stop")
def _stop(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    model, transcript_text = transcript_tail(payload.get("transcript_path"))
    text = payload.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        text = transcript_text
    return _ingest(
        IngestEventType.ASSISTANT_MESSAGE,
        payload,
        session_id,
        now,
        content=MessageContent(text=text, model_id=model),
    )


@_normalizer("PostToolUse")
def _tool_use(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    tool_input = payload.get("tool_input")
    output = tool_output_text(payload.get("tool_response"))
    if len(output) > MAX_TOOL_OUTPUT_CHARS:
        output = output[:MAX_TOOL_OUTPUT_CHARS] + TOOL_TRUNCATION_MARKER
    return _ingest(
        IngestEventType.TOOL_USE,
        payload,
        session_id,
        now,
        content=ToolContent(
            tool_name=tool_name,
            input=tool_input if isinstance(tool_input, dict) else {},
            output=output,
        ),
    )


@_normalizer("SessionStart")
def _session_start(payload: dict[str, Any], session_id: str, now: float) -> Action:
    return ("t1", session_id)


def transform_event(raw: str) -> int:
    """One hook stdin JSON object -> daemon POSTs (the registered dispatch entry).

    Zero stdout on EVERY path EXCEPT a served T1 replay block or a served T2
    pull / visibility signal payload (see the module red line). Fire-and-
    forget: failures land in the opt-in stderr debug lane; a dropped or
    malformed event is exit 0.
    """
    try:
        event = json.loads(raw)
    except ValueError:
        debug("dropped malformed stdin payload")
        return 0
    if not isinstance(event, dict):
        debug("dropped non-object stdin payload")
        return 0
    try:
        action = normalize_event(event, now=time.time())
    except Exception as exc:  # noqa: BLE001 - tolerant-by-contract lane
        debug(f"normalization failed: {exc}")
        return 0
    if action is None:
        debug(f"dropped unmapped or identity-less event {event.get('hook_event_name')!r}")
        return 0
    kind, body = action
    client = hook_client(timeout=POST_TIMEOUT_SECONDS)
    try:
        if kind == "t1":
            block = session_start_block(pull_session_tails(client, str(body)))
            if block is not None:
                print(json.dumps(additional_context_payload(block, T1_HOOK_EVENT)))
            return 0
        client.post(ENDPOINTS[kind], body.model_dump(mode="json"))
        # only an ACKED user ingest parks the focal slot, so the pull runs
        # strictly after the 2xx (ack-implies-ready); a served selection is
        # emitted as additionalContext — the ONE stdout this lane may write
        if isinstance(body, IngestEvent) and body.event is IngestEventType.USER_PROMPT:
            block = inject_recall_context(body.session_id, client)
            if block is not None:
                print(json.dumps(additional_context_payload(block, INJECTION_HOOK_EVENT)))
    except Exception as exc:  # noqa: BLE001 - fire-and-forget contract
        debug(f"{kind} failed: {exc}")
    return 0
