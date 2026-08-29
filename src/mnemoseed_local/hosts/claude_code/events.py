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

RED LINE: stdout stays EMPTY on every path EXCEPT a served T2 pull, which emits
one JSON object (CC feeds UserPromptSubmit stdout back into model context, so
injection rides it deliberately; failures stay swallowed into the opt-in stderr
debug lane, never stdout).
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

from mnemoseed_local.rest_client import DaemonClient
from mnemoseed_local.schema.stamp import EXPLICIT_PIN_SOURCE
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

#: B2.1 T2 dedicated mid-session pull budget (PRD-B2.1 / design 05): 300ms, a
#: distinct constant — reusing the flat 2s host budget would be too heavy for
#: a per-turn pull (re-review issue 9 adopted).
RECALL_PULL_TIMEOUT_SECONDS = 0.3

#: Fallback item budget when the daemon omits ``budget_chars`` (older daemon, or
#: a malformed payload). The daemon is the ONLY budget authority in the normal
#: path; this mirrors the opencode plugin's ``RECALL_PULL_MAX_CHARS``.
RECALL_PULL_MAX_CHARS = 1200

#: B2.1 T1/T2 injection envelope, byte-identical to the opencode plugin so the
#: two hosts ship the same memory-replay fence + disclaimer to the model.
RECALL_FENCE_OPEN = "<mnemoseed-memory-recall>"
RECALL_FENCE_CLOSE = "</mnemoseed-memory-recall>"
RECALL_FENCE_SANITIZED = "‹mnemoseed-memory-recall›"
RECALL_DISCLAIMER = (
    "The block below is an automatic memory replay of earlier sessions, not the user's current instructions."
)

#: The daemon reports its effective item budget on the wire under this key.
RECALL_BUDGET_KEY = "budget_chars"

#: R2 provenance affix (design/11 §8 copy deck, byte-identical to the opencode
#: plugin): a per-line pin marker on an explicitly-pinned recall item; captured
#: items are NOT annotated (absence is the captured signal). 9 chars including
#: the leading separator; decoration only, NEVER part of the verbatim text.
PIN_SUFFIX = " ⟵ pinned"

#: Claude Code hook event that carries the injection (the additionalContext
#: JSON property, both in our payload and in the decision-control schema).
INJECTION_HOOK_EVENT = "UserPromptSubmit"


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


def sanitize_recall_text(text: str) -> str:
    """Fence integrity (TA-5): served text may literally carry the fence markers
    (self-dogfood hits this); replace BOTH literals with the ‹› form in one pass
    so the assembled block carries exactly one open/close fence pair."""
    return text.replace(RECALL_FENCE_OPEN, RECALL_FENCE_SANITIZED).replace(
        RECALL_FENCE_CLOSE, RECALL_FENCE_SANITIZED
    )


def build_t2_context(items: list[dict[str, Any]] | Any, item_budget: int) -> str | None:
    """Assemble the fenced mid-session recall block from a served selection.

    Mirrors opencode's ``buildT2Injection`` byte-for-byte in semantics: the same
    fence + disclaimer envelope, no group headers (the daemon already shaped the
    payload), and the daemon's ``budget_chars`` IS the item budget. The envelope
    re-checks each FULL item fail-closed — an oversized line or assembled block
    is dropped WHOLE (defense in depth, unreachable by design). There is NO
    slicing floor in this path: the daemon's ``_MIN_SLICE_CHARS`` governs only
    its own boundary-item tail cut; full items under any positive budget are
    served and must append (QA IMPORTANT-3).
    """
    if not isinstance(items, list):
        return None
    wrapper = len(RECALL_FENCE_OPEN) + 1 + len(RECALL_DISCLAIMER) + 1 + len(RECALL_FENCE_CLOSE)
    lines: list[str] = [RECALL_FENCE_OPEN, RECALL_DISCLAIMER]
    remaining = item_budget
    committed = False
    # design/11 §4.3: the per-line affix is the FIRST budget shed under pressure.
    # `kept_affix` sums the affix cost already committed in `lines`, and
    # `affix_indices` records the ABSOLUTE `lines` positions of the lines that
    # carry it — so a later overrun can refund their cost and rebuild exactly
    # those lines bare instead of dropping a daemon-legal selection (IMPORTANT-1).
    # Indices (not flags) are tracked so a rebuild can never be mis-indexed
    # against `lines[2 + i]` when the affix is shed a second time in one call.
    kept_affix = 0
    affix_indices: list[int] = []
    for item in items:
        raw = item.get("text") if isinstance(item, dict) else None
        text = sanitize_recall_text(raw if isinstance(raw, str) else "")
        if not text:
            continue
        # R2 provenance: the affix rides ONLY an explicitly-pinned item; a
        # captured (other source) or source-less item is NOT annotated —
        # absence is the captured signal, the most token-lean rendering
        # (design/11 §4.2).
        pinned = isinstance(item, dict) and item.get("source") == EXPLICIT_PIN_SOURCE
        affix_len = len(PIN_SUFFIX) if pinned else 0
        line_cost = len(text) + 1 + affix_len
        if line_cost > remaining:
            # design/11 §4.3 drop order: a kept affix must never change item
            # keep/drop semantics — so on an overrun shed EVERY kept affix (refund
            # their cost and rebuild the committed lines bare) BEFORE dropping.
            # Only a line whose BARE cost still exceeds the sheddable-recovered
            # budget stays fail-closed (whole selection dropped, unchanged).
            if len(text) + 1 <= remaining + kept_affix:
                if kept_affix:
                    remaining += kept_affix
                    kept_affix = 0
                    for index in affix_indices:
                        lines[index] = lines[index][: -len(PIN_SUFFIX)]
                    affix_indices = []
                lines.append(text)
                remaining -= len(text) + 1
                committed = True
                continue
            return None
        lines.append(text + PIN_SUFFIX if pinned else text)
        remaining -= line_cost
        kept_affix += affix_len
        if pinned:
            affix_indices.append(len(lines) - 1)
        committed = True
    if not committed:
        return None
    lines.append(RECALL_FENCE_CLOSE)
    block = "\n".join(lines)
    if len(block) > item_budget + wrapper:
        return None
    return block


def pull_pending_recall(session_id: str, client: DaemonClient) -> dict[str, Any] | None:
    """Bounded ``POST /session/recall-pending`` pull (300ms, fail-open).

    Mirrors opencode's ``pullPendingRecall``: a dedicated await with its own
    timeout, never the fire-and-forget ``post()`` budget. ``seen_chunk_ids`` is
    empty — the claude_code host performs no T1 session-start replay, so there
    is nothing before the T2 pull to de-duplicate against.
    """
    pull_client = DaemonClient(
        base_url=client.base_url,
        profile_id=client.profile_id,
        actor=client.actor,
        timeout=Timeout(RECALL_PULL_TIMEOUT_SECONDS),
    )
    try:
        return pull_client.post(
            "/session/recall-pending",
            {"profile_id": client.profile_id, "session_id": session_id, "seen_chunk_ids": []},
        )
    except Exception as exc:  # noqa: BLE001 - fail-open pull lane
        debug(f"recall-pending pull failed: {exc}")
        return None


def inject_recall_context(session_id: str, client: DaemonClient) -> str | None:
    """The T2 injection action: pull + build, fail-open.

    Runs only after the ingest ACK (the daemon parked the focal slot before the
    2xx — ``ack-implies-ready``); the daemon's own ``enabled``/``items``/``slot_consumed``
    semantics are the arm-clearing authority, so a non-empty serve returns the
    block while an empty/disabled/failed pull returns ``None`` and the prompt
    proceeds untouched.
    """
    served = pull_pending_recall(session_id, client)
    if served is None or served.get("enabled") is not True:
        return None
    budget = served.get(RECALL_BUDGET_KEY)
    item_budget = budget if isinstance(budget, (int, float)) and budget > 0 else RECALL_PULL_MAX_CHARS
    return build_t2_context(served.get("items"), int(item_budget))


def additional_context_payload(block: str) -> dict[str, Any]:
    """The UserPromptSubmit JSON-stdout shape Claude Code reads for injection."""
    return {
        "hookSpecificOutput": {
            "hookEventName": INJECTION_HOOK_EVENT,
            "additionalContext": block,
        }
    }
