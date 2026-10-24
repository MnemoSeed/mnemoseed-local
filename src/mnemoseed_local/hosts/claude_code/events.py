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

B2.1 T2 mid-session recall injection (the claude_code half of the R2 trust
surface): after a ``UserPromptSubmit`` ingest ACKs (the daemon parked the focal
slot synchronously before answering 2xx — ack-implies-ready), the transformer
pulls ``POST /session/recall-pending`` (300ms, fail-open) and, when the daemon
serves a selection, emits it as ``hookSpecificOutput.additionalContext`` JSON
stdout. Claude Code injects that string alongside the submitted prompt — the
SINGLE channel this transformer may write to stdout, and only on a served pull.
Budget semantics mirror the opencode plugin's ``buildT2Injection``: the daemon's
wire ``budget_chars`` is the item budget (never a hardcoded cap), the envelope
re-checks each full item fail-closed (an oversized item drops the WHOLE
selection), and there is NO slicing floor in this path — the daemon is the sole
budget authority across the whole positive-int range.

RED LINE: stdout stays EMPTY on every path EXCEPT (a) a served T2 pull, which
emits one JSON object (CC feeds UserPromptSubmit stdout back into model context,
so injection rides it deliberately; failures stay swallowed into the opt-in
stderr debug lane, never stdout), and (b) a visibility signal payload when
the daemon's wire mode is not off — which may be a bare capture signal line
without any served selection.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from httpx import Timeout
from pydantic import BaseModel

from mnemoseed_local.hosts.common import payload
from mnemoseed_local.hosts.common.daemon import (
    POST_TIMEOUT_SECONDS,
    RECALL_BUDGET_KEY,
    RECALL_PULL_MAX_CHARS,
    debug,
    hook_client,
    profile_id,
    pull_pending_recall,
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
from mnemoseed_local.hosts.common.t2 import build_t2_context, sanitize_recall_text
from mnemoseed_local.hosts.common.visibility import (
    VISIBILITY_KEY,
    capture_count_line,
    recall_count_line,
    visibility_mode,
)
from mnemoseed_local.rest_client import DaemonClient
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

#: conversation visibility: the fixed, neutral, non-directive signal
#: vocabulary (byte-identical to the opencode plugin; the source-level oracle
#: in tests pins both sides together). Rendering is gated on the daemon's wire
#: ``visibility`` field; a field that is absent or anything but compact/verbose
#: means off (old daemon x new hook stays byte-identical).
T2_SIGNAL = "[mnemoseed] recalled related memories"
CAPTURE_SIGNAL = "[mnemoseed] captured this session"

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


def capture_signal_of(served: dict[str, Any] | None) -> str | None:
    """The pending capture signal text of a pull payload, None when silent.

    compact renders the once-per-session line; verbose renders the capture
    count line; anything else (including off and a zero count) is silent.
    """
    if visibility_mode(served) is None or served is None:
        return None
    visibility = served[VISIBILITY_KEY]
    if not isinstance(visibility, dict):
        return None
    capture = visibility.get("capture")
    if capture is True:
        return CAPTURE_SIGNAL
    if isinstance(capture, int) and not isinstance(capture, bool) and capture > 0:
        return capture_count_line(capture)
    return None


def _t2_signal_of(served: dict[str, Any], items: list[dict[str, Any]]) -> str | None:
    """The prebuilt T2 signal text under the payload's visibility mode.

    compact: the bare signal line; verbose: the signal line plus the count line
    (memories = served items, chars = their sanitized text bytes — exact, since
    the T2 build is all-or-nothing).
    """
    mode = visibility_mode(served)
    if mode is None:
        return None
    if mode != "verbose":
        return T2_SIGNAL
    texts = [
        sanitize_recall_text(str(item.get("text")))
        for item in items
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text")
    ]
    chars = sum(len(text) for text in texts)
    return T2_SIGNAL + "\n" + recall_count_line(len(texts), chars)


def inject_recall_context(session_id: str, client: DaemonClient) -> str | None:
    """The T2 injection action: pull + build, fail-open.

    Runs only after the ingest ACK (the daemon parked the focal slot before the
    2xx — ``ack-implies-ready``); the daemon's own ``enabled``/``items``/``slot_consumed``
    semantics are the arm-clearing authority, so a non-empty serve returns the
    block while an empty/disabled/failed pull falls through to the capture
    signal (a bare line when the wire mode renders and the latch is pending) and
    ``None`` when nothing is to say.
    """
    served = pull_pending_recall(session_id, client)
    if served is None:
        return None
    parts: list[str] = []
    if served.get("enabled") is True:
        budget = served.get(RECALL_BUDGET_KEY)
        item_budget = (
            int(budget) if isinstance(budget, (int, float)) and budget > 0 else RECALL_PULL_MAX_CHARS
        )
        items = served.get("items")
        if isinstance(items, list) and items:
            signal = _t2_signal_of(served, items)
            block = build_t2_context(items, item_budget, signal=signal)
            if block is not None:
                parts.append(block)
    capture = capture_signal_of(served)
    if capture is not None:
        parts.insert(0, capture)
    if not parts:
        return None
    return "\n".join(parts)


def additional_context_payload(block: str) -> dict[str, Any]:
    """The UserPromptSubmit JSON-stdout shape Claude Code reads for injection."""
    return payload.additional_context_payload(block, INJECTION_HOOK_EVENT)


def transform_event(raw: str) -> int:
    """One hook stdin JSON object -> daemon POSTs (the registered dispatch entry).

    Zero stdout on EVERY path EXCEPT a served T2 pull or a visibility signal
    payload (see the module red line). Fire-and-forget: failures land in the
    opt-in stderr debug lane; a dropped or malformed event is exit 0.
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
        client.post(ENDPOINTS[kind], body.model_dump(mode="json"))
        # only an ACKED user ingest parks the focal slot, so the pull runs
        # strictly after the 2xx (ack-implies-ready); a served selection is
        # emitted as additionalContext — the ONE stdout this lane may write
        session_id = injection_session_id(body)
        if session_id is not None:
            block = inject_recall_context(session_id, client)
            if block is not None:
                print(json.dumps(additional_context_payload(block)))
    except Exception as exc:  # noqa: BLE001 - fire-and-forget contract
        debug(f"{kind} post failed: {exc}")
    return 0
