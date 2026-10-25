"""Claude Code hook stdin -> daemon REST normalization (transformer lane).

Claude Code pipes one JSON object per hook firing into
``mnemoseed-local _hook-event --host claude_code`` (registered by
``hosts.claude_code.install``). Event map v1:

- ``SessionStart``    -> the T1 read lane: one ``POST /session/recent`` (the
  legacy unscoped identity shape — no ``self_session_id``, no
  ``resume_query``), rendered as the fenced replay block plus the standing
  rules budget block and emitted as ``hookSpecificOutput.additionalContext``
  stdout (attempt-once per firing — the stateless process IS the gate);
  never an ingest;
- ``UserPromptSubmit`` -> ``user_prompt`` /ingest (``agent`` lifted when the
  payload carries one; blank stays an honest null via the wire validator);
- ``Stop``            -> ``assistant_message`` /ingest — flush semantics only,
  NEVER a session settle; the model id comes from the transcript JSONL tail
  and any parse failure degrades to a model-less event, never a drop;
- ``SubagentStop``    -> ``assistant_message`` /ingest through the same Stop
  normalizer: payload text with the transcript-tail fallback, and the
  subagent ``agent_type`` lifted into ``agent`` for the origin chain. The CC
  child stdin carries no separate child-session id — ``session_id`` IS the
  parent session, so ``session_parent_id`` stays None by construction;
- ``PostToolUse``     -> ``tool_use`` /ingest (output capped);
- ``PreCompact``      -> POST /flush;
- ``SessionEnd``      -> POST /session/end, fire-and-forget without awaiting
  the daemon's drain (CC gives SessionEnd hooks a shared ~1.5s budget; the
  daemon's teardown flush is the backstop);
- ``SubagentStart`` is NOT ingested: a bare start signal carries no turn
  content (the subagent call itself is captured by PostToolUse-on-Task), and
  mapping it would add an event kind.

B2.1 T2 mid-session recall injection (the claude_code half of the R2 trust
surface): after a ``UserPromptSubmit`` ingest ACKs (the daemon parked the focal
slot synchronously before answering 2xx — ack-implies-ready), the transformer
pulls ``POST /session/recall-pending`` (300ms, fail-open) and, when the daemon
serves a selection, emits it as ``hookSpecificOutput.additionalContext`` JSON
stdout. Budget semantics mirror the opencode plugin's ``buildT2Injection``: the daemon's
wire ``budget_chars`` is the item budget (never a hardcoded cap), the envelope
re-checks each full item fail-closed (an oversized item drops the WHOLE
selection), and there is NO slicing floor in this path — the daemon is the sole
budget authority across the whole positive-int range.

RED LINE: stdout stays EMPTY on every path EXCEPT (a) a served T2 pull, which
emits one JSON object (CC feeds UserPromptSubmit stdout back into model context,
so injection rides it deliberately; failures stay swallowed into the opt-in
stderr debug lane, never stdout), (b) a visibility signal payload when
the daemon's wire mode is not off — which may be a bare capture signal line
without any served selection, and (c) a served T1 replay on SessionStart (the
fenced block plus the standing rules budget block, one JSON object).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from httpx import Timeout
from pydantic import BaseModel

from mnemoseed_local.hosts.common import payload
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
from mnemoseed_local.hosts.common.fence import (
    sanitize_recall_text as sanitize_recall_text,
)
from mnemoseed_local.hosts.common.recall import (
    CAPTURE_SIGNAL as CAPTURE_SIGNAL,
)
from mnemoseed_local.hosts.common.recall import (
    MAX_TOOL_OUTPUT_CHARS,
    TOOL_TRUNCATION_MARKER,
    inject_recall_context,
    pull_session_tails,
    session_start_injection,
    tool_output_text,
    transcript_tail,
)
from mnemoseed_local.hosts.common.recall import (
    T1_SIGNAL as T1_SIGNAL,
)
from mnemoseed_local.hosts.common.recall import (
    T2_SIGNAL as T2_SIGNAL,
)
from mnemoseed_local.hosts.common.t2 import build_t2_context as build_t2_context
from mnemoseed_local.hosts.common.visibility import (
    capture_count_line as capture_count_line,
)
from mnemoseed_local.hosts.common.visibility import (
    recall_count_line as recall_count_line,
)
from mnemoseed_local.hosts.common.visibility import (
    visibility_mode as visibility_mode,
)
from mnemoseed_local.schema.turn import (
    FlushRequest,
    HostId,
    IngestEvent,
    IngestEventType,
    SessionEndRequest,
)

#: kind -> daemon REST endpoint (kind names are the transformer's action tags).
ENDPOINTS = {"ingest": "/ingest", "flush": "/flush", "session_end": "/session/end"}

#: Per-phase cap for SessionEnd posts: CC enforces a shared ~1.5s teardown
#: clock on SessionEnd hooks while /session/end drains server-side, so the
#: call must give up fast.
SESSION_END_TIMEOUT_SECONDS = 0.5

#: Claude Code hook event that carries the injection (the additionalContext
#: JSON property, both in our payload and in the decision-control schema).
INJECTION_HOOK_EVENT = "UserPromptSubmit"

#: The hook event that carries the T1 session-start replay (no ingest).
T1_HOOK_EVENT = "SessionStart"

Action = tuple[str, Any]

Normalizer = Callable[[dict[str, Any], str, float], Action | None]

_NORMALIZERS: dict[str, Normalizer] = {}


def _normalizer(name: str) -> Callable[[Normalizer], Normalizer]:
    def register(fn: Normalizer) -> Normalizer:
        _NORMALIZERS[name] = fn
        return fn

    return register


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
@_normalizer("SubagentStop")
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
        content={"text": text, "model_id": model},
        agent=_agent_of(payload),
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
        content={
            "tool_name": tool_name,
            "input": tool_input if isinstance(tool_input, dict) else {},
            "output": output,
        },
    )


@_normalizer("PreCompact")
def _pre_compact(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    return ("flush", FlushRequest(session_id=session_id, profile_id=profile_id()))


@_normalizer("SessionEnd")
def _session_end(payload: dict[str, Any], session_id: str, now: float) -> Action | None:
    return ("session_end", SessionEndRequest(session_id=session_id, profile_id=profile_id(), ts=now))


@_normalizer("SessionStart")
def _session_start(payload: dict[str, Any], session_id: str, now: float) -> Action:
    return ("t1", session_id)


def action_injectable(kind: str, body: BaseModel) -> bool:
    """True only for an acked UserPromptSubmit ingest — the T2 injection point.

    Claude Code has no separate ``chat.system.transform``-style hook to pull on:
    ``UserPromptSubmit`` both ingests AND carries the ``additionalContext``
    formback, so the injection rides the same normalized action. Every other
    kind (assistant ingest, flush, session_end) stays a pure capture route.
    """
    return kind == "ingest" and isinstance(body, IngestEvent) and body.event is IngestEventType.USER_PROMPT


def injection_session_id(body: BaseModel) -> str | None:
    """The session the served selection belongs to, or ``None`` off the lane."""
    if isinstance(body, IngestEvent) and body.event is IngestEventType.USER_PROMPT:
        return body.session_id
    return None


def endpoint_budget(kind: str) -> float | Timeout:
    """POST budget per action kind (see SESSION_END_TIMEOUT_SECONDS)."""
    if kind == "session_end":
        return Timeout(SESSION_END_TIMEOUT_SECONDS)
    return POST_TIMEOUT_SECONDS


def transform_event(raw: str) -> int:
    """One hook stdin JSON object -> daemon POSTs (the registered dispatch entry).

    Zero stdout on EVERY path EXCEPT a served T1 replay, a served T2 pull, or
    a visibility signal payload (see the module red line). Fire-and-forget:
    failures land in the opt-in stderr debug lane; a dropped or malformed
    event is exit 0.
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
    client = hook_client(timeout=endpoint_budget(kind))
    try:
        if kind == "t1":
            block = session_start_injection(pull_session_tails(client, str(body)))
            if block is not None:
                print(json.dumps(payload.additional_context_payload(block, T1_HOOK_EVENT)))
            return 0
        client.post(ENDPOINTS[kind], body.model_dump(mode="json"))
        # only an ACKED user ingest parks the focal slot, so the pull runs
        # strictly after the 2xx (ack-implies-ready); a served selection is
        # emitted as additionalContext — the ONE stdout the T2 lane may write
        session_id = injection_session_id(body)
        if session_id is not None:
            block = inject_recall_context(session_id, client)
            if block is not None:
                print(json.dumps(payload.additional_context_payload(block, INJECTION_HOOK_EVENT)))
    except Exception as exc:  # noqa: BLE001 - fire-and-forget contract
        debug(f"{kind} post failed: {exc}")
    return 0
