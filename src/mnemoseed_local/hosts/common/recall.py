"""Shared T1/T2 recall lanes and defensive payload helpers for host hooks.

T1 (session start): one bounded ``POST /session/recent`` read rendered as the
fenced replay block (fence + disclaimer + ``<session-tail>`` groups), the
block shape byte-matching the opencode plugin's ``buildRecallInjection``.
T2 (mid-session): after a UserPromptSubmit ingest ACKs (ack-implies-ready),
the bounded ``POST /session/recall-pending`` pull renders the fenced block
via ``build_t2_context``. Both lanes read the daemon's wire ``visibility``
field as the ONLY rendering authority: absent or off means no signal line.

RED LINE (per host transformer): stdout stays EMPTY on every path EXCEPT a
served T1/T2 block or a visibility signal payload when the wire mode is not
off. Failures stay in the opt-in stderr debug lane, never stdout.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mnemoseed_local.hosts.common.daemon import (
    RECALL_BUDGET_KEY,
    RECALL_PULL_MAX_CHARS,
    debug,
    pull_pending_recall,
)
from mnemoseed_local.hosts.common.fence import (
    RECALL_DISCLAIMER,
    RECALL_FENCE_CLOSE,
    RECALL_FENCE_OPEN,
    sanitize_recall_text,
)
from mnemoseed_local.hosts.common.t2 import build_t2_context
from mnemoseed_local.hosts.common.visibility import (
    capture_count_line,
    recall_count_line,
    visibility_mode,
)
from mnemoseed_local.rest_client import DaemonClient

#: The fixed, neutral, non-directive signal vocabulary (byte-identical across
#: hosts; the source-level oracle pins every side together).
T1_SIGNAL = "[mnemoseed] replayed context from a previous session"
T2_SIGNAL = "[mnemoseed] recalled related memories"
CAPTURE_SIGNAL = "[mnemoseed] captured this session"

#: Per-response cap for PostToolUse tool output.
MAX_TOOL_OUTPUT_CHARS = 8000

TOOL_TRUNCATION_MARKER = "[... truncated]"

#: T1 session-start read shape (mirrors the opencode plugin's session tail).
T1_SESSIONS = 2
T1_PER_SESSION = 8
T1_MAX_INJECT_CHARS = 4000
T1_MIN_SLICE_CHARS = 200

_SESSION_TAIL_CLOSE = "</session-tail>"


def t1_count_line(sessions: int, memories: int, chars: int) -> str:
    """The verbose T1 count line (appended after the compact signal line)."""
    return f"[mnemoseed] {sessions} sessions, {memories} memories, {chars} chars"


def t1_signal_of(mode: str | None, sessions: int, memories: int, chars: int) -> str | None:
    """The prebuilt T1 signal text under the wire visibility mode."""
    if mode not in ("compact", "verbose"):
        return None
    if mode == "verbose":
        return T1_SIGNAL + "\n" + t1_count_line(sessions, memories, chars)
    return T1_SIGNAL


def t2_signal_of(served: dict[str, Any], items: list[dict[str, Any]]) -> str | None:
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


def capture_signal_of(served: dict[str, Any] | None) -> str | None:
    """The pending capture signal text of a pull payload, None when silent.

    compact renders the once-per-session line; verbose renders the capture
    count line; anything else (including off and a zero count) is silent.
    """
    if visibility_mode(served) is None or served is None:
        return None
    visibility = served["visibility"]
    if not isinstance(visibility, dict):
        return None
    capture = visibility.get("capture")
    if capture is True:
        return CAPTURE_SIGNAL
    if isinstance(capture, int) and not isinstance(capture, bool) and capture > 0:
        return capture_count_line(capture)
    return None


def inject_recall_context(session_id: str, client: DaemonClient) -> str | None:
    """The T2 injection action: pull + build, fail-open.

    Runs only after the ingest ACK (the daemon parked the focal slot before the
    2xx — ``ack-implies-ready``); the daemon's own ``enabled``/``items``/
    ``slot_consumed`` semantics are the arm-clearing authority, so a non-empty
    serve returns the block while an empty/disabled/failed pull falls through
    to the capture signal (a bare line when the wire mode renders and the
    latch is pending) and ``None`` when nothing is to say.
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
            signal = t2_signal_of(served, items)
            block = build_t2_context(items, item_budget, signal=signal)
            if block is not None:
                parts.append(block)
    capture = capture_signal_of(served)
    if capture is not None:
        parts.insert(0, capture)
    if not parts:
        return None
    return "\n".join(parts)


def pull_session_tails(client: DaemonClient, session_id: str) -> dict[str, Any] | None:
    """The ONE bounded T1 read: ``POST /session/recent`` (2s budget, fail-open).

    The body carries ``exclude_session_id`` and deliberately NO
    ``self_session_id`` and NO ``resume_query``: a session-start firing has no
    prompt to scope from, and sending both identity fields without a query
    would trip the daemon's automatic-path fail-closed guard — the legacy
    unscoped replay is the honest shape for a stateless session-start hook.
    """
    body = {
        "profile_id": client.profile_id,
        "sessions": T1_SESSIONS,
        "per_session": T1_PER_SESSION,
        "exclude_session_id": session_id,
    }
    try:
        data = client.post("/session/recent", body)
    except Exception as exc:  # noqa: BLE001 - fail-open read lane
        debug(f"session/recent pull failed: {exc}")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        debug("session/recent malformed")
        return None
    return data


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _session_tail_id(group: dict[str, Any]) -> str:
    """The header's short id: the session_id's last dash-segment (long session
    ids are unreadable noise in the injected block)."""
    session_id = group.get("session_id")
    session_id = session_id if isinstance(session_id, str) else ""
    dash = session_id.rfind("-")
    return session_id[dash + 1 :] if dash >= 0 else session_id


def _iso_ended(group: dict[str, Any]) -> str:
    latest = group.get("latest_at")
    if isinstance(latest, (int, float)) and not isinstance(latest, bool):
        try:
            return datetime.fromtimestamp(latest, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def _session_tail_header(group: dict[str, Any]) -> str:
    started = ""
    window = group.get("window")
    if isinstance(window, dict) and group.get("window_truncated") is not True:
        first = window.get("first")
        if isinstance(first, str) and first:
            started = f' started="{_escape_attr(first)}"'
    return (
        f'<session-tail id="{_escape_attr(_session_tail_id(group))}"'
        f' ended="{_escape_attr(_iso_ended(group))}"{started}>'
    )


def _build_t1_base(sessions: Any, budget: int) -> tuple[str, int, int, int] | None:
    """(block, group count, included chunk count, chars) of the bare T1 block.

    Accrue newest-first (groups in payload order, chunks within a group
    reversed) against a char budget whose final assembled block must stay
    within ``budget`` INCLUDING the fence, the disclaimer line and the group
    headers — the wrapper is accounted up front. A boundary chunk keeps only
    its newest tail slice (prefixed with the "…" marker) when the leftover can
    still hold ``T1_MIN_SLICE_CHARS``; otherwise it is dropped along with
    everything older. Returns None when nothing rendered or the budget leaves
    no slice room.
    """
    if not isinstance(sessions, list):
        return None
    fixed = len(RECALL_FENCE_OPEN) + 1 + len(RECALL_DISCLAIMER) + 1 + len(RECALL_FENCE_CLOSE)
    remaining = budget - fixed
    if remaining < T1_MIN_SLICE_CHARS:
        return None
    lines = [RECALL_FENCE_OPEN, RECALL_DISCLAIMER]
    committed_group = False
    group_count = 0
    memories = 0
    chars = 0
    for group in sessions:
        if not isinstance(group, dict):
            continue
        chunks = group.get("chunks")
        newest_first = list(reversed(chunks)) if isinstance(chunks, list) else []
        if not newest_first:
            continue
        header = _session_tail_header(group)
        group_fixed = len(header) + 1 + len(_SESSION_TAIL_CLOSE) + 1
        if group_fixed > remaining:
            break
        remaining -= group_fixed
        rendered: list[str] = []
        got_chunk = False
        for chunk in newest_first:
            raw = chunk.get("text") if isinstance(chunk, dict) else None
            text = sanitize_recall_text(raw if isinstance(raw, str) else "")
            if not text:
                continue
            if len(text) + 1 <= remaining:
                rendered.append(text)
                remaining -= len(text) + 1
                chars += len(text)
                memories += 1
                got_chunk = True
                continue
            slice_budget = remaining - 2  # reserve the "…" marker and the newline
            if slice_budget < T1_MIN_SLICE_CHARS:
                break
            sliced = "…" + text[-slice_budget:]
            rendered.append(sliced)
            remaining = 0
            chars += len(sliced) + 1
            memories += 1
            got_chunk = True
            break
        if not got_chunk:
            break
        lines.append(header)
        lines.extend(reversed(rendered))  # collected newest-first, rendered ascending
        lines.append(_SESSION_TAIL_CLOSE)
        group_count += 1
        committed_group = True
    if not committed_group:
        return None
    lines.append(RECALL_FENCE_CLOSE)
    return "\n".join(lines), group_count, memories, chars


def _with_signal_after_fence(block: str, signal: str) -> str:
    if not block.startswith(RECALL_FENCE_OPEN):
        return block
    rest = block[len(RECALL_FENCE_OPEN) + 1 :]
    return RECALL_FENCE_OPEN + "\n" + signal + "\n" + rest


def build_t1_context(sessions: Any, budget: int, signal: str | None = None) -> str | None:
    """The fenced T1 replay block; the visibility signal sheds whole when it
    does not fit, restoring the bare no-signal build byte-for-byte."""
    base = _build_t1_base(sessions, budget)
    if base is None:
        return None
    block = base[0]
    if signal:
        candidate = _with_signal_after_fence(block, signal)
        if len(candidate) <= budget:
            return candidate
    return block


def session_start_block(data: dict[str, Any] | None) -> str | None:
    """The T1 injection block of a served /session/recent payload.

    The signal text derives from the payload's own ``visibility`` mode and the
    base build's counts; when the signal does not fit the block budget it
    sheds and the bare block is served.
    """
    base = _build_t1_base(data.get("sessions") if isinstance(data, dict) else None, T1_MAX_INJECT_CHARS)
    if base is None:
        return None
    block, group_count, memories, chars = base
    signal = t1_signal_of(visibility_mode(data), group_count, memories, chars)
    if signal is None:
        return block
    candidate = _with_signal_after_fence(block, signal)
    return candidate if len(candidate) <= T1_MAX_INJECT_CHARS else block


def tool_output_text(response: Any) -> str:
    """Flatten a PostToolUse tool_response into the captured text."""
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


def transcript_tail(path: Any) -> tuple[str | None, str]:
    """(model, assistant text) of the newest assistant entry in a JSONL
    transcript. Every failure mode (missing/garbage file, unexpected shapes)
    degrades to ``(None, "")`` — a model-less, empty-text event still beats a
    dropped one. The record shape is the Claude-Code transcript dialect; a
    transcript in any other shape degrades the same way.
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
