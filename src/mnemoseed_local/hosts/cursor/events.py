"""Cursor hook stdin -> daemon REST normalization (transformer lane).

Cursor pipes one JSON object per hook firing into
``mnemoseed-local _hook-event --host cursor`` (registered by
``hosts.cursor.install`` as command entries under ``hooks.<event>`` in
``~/.cursor/hooks.json``). The stdin payload dialect is Cursor's documented
agent-hook schema pinned by ``tests/fixtures/cursor_hook/`` (PRD-293); every
field read is defensive. Event map v1 (the six subscribed events):

- ``sessionStart``      -> the T1 read lane: one ``POST /session/recent``,
  rendered as the fenced replay block and emitted as the FLAT
  ``{"additional_context": ...}`` stdout (the native Cursor output shape —
  never Claude's nested ``hookSpecificOutput`` envelope); attempt-once per
  firing — the stateless process IS the gate; never an ingest;
- ``beforeSubmitPrompt`` -> ``user_prompt`` /ingest, capture-only; the
  documented ``{"continue": true}`` allow rides EVERY recognized firing and
  never waits on the daemon, the binding outcome, or the prompt's content;
- ``postToolUse``       -> ``tool_use`` /ingest (output capped, the event's
  real ``cwd`` preferred by the binding gate);
- ``afterAgentResponse`` -> ``assistant_message`` /ingest (the assistant text
  arrives here — the ``stop`` input carries none);
- ``stop``              -> ``POST /flush`` ONLY (flush semantics, never a
  settle and never an ingest);
- ``sessionEnd``        -> ``POST /session/end`` (the settle forwards
  session_id/profile_id/ts only — the host's ``reason`` has no wire field and
  is not forwarded; this lane persists nothing).

There is NO T2 mid-session injection on this host: ``beforeSubmitPrompt`` has
no additionalContext output channel, so ``pull_pending_recall`` /
``build_t2_context`` are deliberately not wired — recall on Cursor is T1 +
the MCP gateway only, and per-turn capture signals have no vehicle either.

The session identity dialect is ``conversation_id`` (cross-turn stable; the
docs pin ``session_id == conversation_id`` where the field exists).
``origin_agent`` stays null — Cursor reports no agent field.

RED LINE (Cursor version): stdout carries ONLY (a) the flat
``{"additional_context": ...}`` object on a SERVED sessionStart T1 read and
(b) ``{"continue": true}`` on every recognized beforeSubmitPrompt firing —
never exit 2, never ``failClosed``, never ``followup_message`` (it would be
auto-submitted as a user message). Every other path stays at zero stdout
(empty stdout is the documented quickstart-legal shape); failures land in
the opt-in stderr debug lane and every drop is exit 0.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from mnemoseed_local.hosts.common.daemon import (
    POST_TIMEOUT_SECONDS,
    debug,
    enforce_project_binding,
    hook_client,
    profile_id,
)
from mnemoseed_local.hosts.common.recall import (
    MAX_TOOL_OUTPUT_CHARS,
    TOOL_TRUNCATION_MARKER,
    pull_session_tails,
    session_start_block,
    tool_output_text,
)
from mnemoseed_local.schema.turn import (
    FlushRequest,
    HostId,
    IngestEvent,
    IngestEventType,
    MessageContent,
    SessionEndRequest,
    ToolContent,
)

#: kind -> daemon REST endpoint.
ENDPOINTS = {"ingest": "/ingest", "flush": "/flush", "session_end": "/session/end"}

#: The submit event whose firing must always carry the explicit allow.
SUBMIT_EVENT = "beforeSubmitPrompt"

Action = tuple[str, Any]

Normalizer = Callable[[dict[str, Any], str, float], Action | None]

_NORMALIZERS: dict[str, Normalizer] = {}


def _normalizer(name: str) -> Callable[[Normalizer], Normalizer]:
    def register(fn: Normalizer) -> Normalizer:
        _NORMALIZERS[name] = fn
        return fn

    return register


def additional_context_payload(block: str) -> dict[str, Any]:
    """The flat native stdout shape Cursor's sessionStart output documents."""
    return {"additional_context": block}


def submit_allow_payload() -> dict[str, Any]:
    """The documented explicit allow for a beforeSubmitPrompt firing."""
    return {"continue": True}


def cwd_proxy(event: dict[str, Any]) -> object:
    """The binding gate's cwd input for one Cursor event: the event's real
    ``cwd`` when it carries one (tool events), else the single-root
    ``workspace_roots[0]`` proxy. A multi-root workspace supplies no cwd and
    walks the gate's honest no-cwd path."""
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd
    roots = event.get("workspace_roots")
    if isinstance(roots, list) and len(roots) == 1:
        root = roots[0]
        if isinstance(root, str) and root.strip():
            return root
    return None


def normalize_event(payload: dict[str, Any], *, now: float) -> Action | None:
    """Map one Cursor hook payload to a daemon action; ``None`` means drop."""
    name = payload.get("hook_event_name")
    session_id = payload.get("conversation_id")
    if not isinstance(name, str) or not isinstance(session_id, str) or not session_id.strip():
        return None
    normalizer = _NORMALIZERS.get(name)
    return normalizer(payload, session_id, now) if normalizer else None


def _ingest_body(
    event: IngestEventType,
    payload: dict[str, Any],
    session_id: str,
    now: float,
    content: MessageContent | ToolContent,
) -> IngestEvent:
    return IngestEvent(
        host=HostId.CURSOR,
        event=event,
        session_id=session_id,
        profile_id=profile_id(),
        ts=now,
        content=content,
        raw=payload,
    )


def _ingest(
    event: IngestEventType,
    payload: dict[str, Any],
    session_id: str,
    now: float,
    content: MessageContent | ToolContent,
) -> Action:
    return ("ingest", _ingest_body(event, payload, session_id, now, content))


def _model_id_of(payload: dict[str, Any]) -> str | None:
    for key in ("model_id", "model"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


@_normalizer("sessionStart")
def _session_start(payload: dict[str, Any], session_id: str, now: float) -> Action:
    return ("t1", session_id)


@_normalizer("beforeSubmitPrompt")
def _user_prompt(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return _ingest(IngestEventType.USER_PROMPT, payload, session_id, now, content=MessageContent(text=prompt))


@_normalizer("postToolUse")
def _tool_use(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    tool_input = payload.get("tool_input")
    output = tool_output_text(payload.get("tool_output"))
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


@_normalizer("afterAgentResponse")
def _assistant_message(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return _ingest(
        IngestEventType.ASSISTANT_MESSAGE,
        payload,
        session_id,
        now,
        content=MessageContent(text=text, model_id=_model_id_of(payload)),
    )


@_normalizer("stop")
def _stop(payload: dict[str, Any], session_id: str, now: float) -> Action:
    # flush-only: persist the buffered turns WITHOUT settling; the session's
    # settle belongs to sessionEnd
    return ("flush", FlushRequest(session_id=session_id, profile_id=profile_id()))


@_normalizer("sessionEnd")
def _session_end(payload: dict[str, Any], session_id: str, now: float) -> Action:
    return ("session_end", SessionEndRequest(session_id=session_id, profile_id=profile_id(), ts=now))


def transform_event(raw: str) -> int:
    """One hook stdin JSON object -> daemon POSTs (the registered dispatch entry).

    Zero stdout on EVERY path EXCEPT a served T1 replay block or the explicit
    allow of a recognized submit firing (see the module red line). Fire-and-
    forget: failures land in the opt-in stderr debug lane; a dropped or
    malformed event is exit 0, never exit 2. The project binding gate runs
    first: a refused event is dropped with a visible stderr refusal and no
    daemon write.
    """
    try:
        event = json.loads(raw)
    except ValueError:
        debug("dropped malformed stdin payload")
        return 0
    if not isinstance(event, dict):
        debug("dropped non-object stdin payload")
        return 0
    if event.get("hook_event_name") == SUBMIT_EVENT:
        # the allow never waits on the daemon or the binding outcome
        print(json.dumps(submit_allow_payload()))
    if not enforce_project_binding(cwd_proxy(event)):
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
                print(json.dumps(additional_context_payload(block)))
            return 0
        client.post(ENDPOINTS[kind], body.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 - fire-and-forget contract
        debug(f"{kind} failed: {exc}")
    return 0
