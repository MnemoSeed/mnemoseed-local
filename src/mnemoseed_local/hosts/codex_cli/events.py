"""Codex CLI hook stdin -> daemon REST normalization (transformer lane).

Codex pipes one JSON object per hook firing into
``mnemoseed-local _hook-event --host codex_cli`` (registered by
``hosts.codex_cli.install`` as ``type: command`` entries in the user-level
``hooks.json``). The stdin contract is the official hooks documentation,
pinned by ``tests/fixtures/codex_hook/``; the rollout transcript at
``transcript_path`` is the facts source and the hooks are only the clock.
Event map v1 (the four subscribed events):

- ``SessionStart``      -> the T1 read lane: one ``POST /session/recent``,
  rendered as the fenced replay block and emitted as
  ``hookSpecificOutput.additionalContext`` (attempt-once per firing — the
  stateless process IS the gate); never an ingest;
- ``UserPromptSubmit``  -> one ``user_prompt`` /ingest carrying the prompt
  field and the transcript ordinal of the matching user turn, then the T2
  pull after the ACK (ack-implies-ready) with the served selection emitted
  as ``additionalContext``;
- ``Stop``              -> the transcript differential: every assistant turn
  after the last user turn (the local lower bound — user turns ride their
  own UserPromptSubmit firings), each as one /ingest with its ordinal; the
  first 202's ``ack_ordinal`` lifts the intra-process send floor so turns
  the daemon already holds are not re-sent;
- ``SessionEnd``        -> the same differential, then one
  ``POST /session/end`` settle.

The daemon owns the per-session high watermark (PRD-272 §9.3): it skips
ordinals at or below it, so same-state replay stores nothing twice. The
wire ``visibility`` field is the only rendering authority (#273): absent or
off means no signal line and zero stdout.

RED LINE: stdout stays EMPTY on every path EXCEPT a served T1/T2 block or a
visibility signal payload when the wire mode is not off. Failures land in
the opt-in stderr debug lane — except a missing ``zstandard`` extra, which
is a setup fault and answers once on stderr with the install command. A
dropped or malformed event is exit 0.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from mnemoseed_local.hosts.codex_cli.transcript import TranscriptTurn, anchor_ordinal, read_turns
from mnemoseed_local.hosts.common.daemon import (
    POST_TIMEOUT_SECONDS as POST_TIMEOUT_SECONDS,
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
from mnemoseed_local.hosts.common.daemon import (
    debug as debug,
)
from mnemoseed_local.hosts.common.daemon import (
    hook_client as hook_client,
)
from mnemoseed_local.hosts.common.daemon import (
    profile_id as profile_id,
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
    T1_SIGNAL as T1_SIGNAL,
)
from mnemoseed_local.hosts.common.recall import (
    T2_SIGNAL as T2_SIGNAL,
)
from mnemoseed_local.hosts.common.recall import (
    inject_recall_context,
    pull_session_tails,
    session_start_block,
)
from mnemoseed_local.schema.turn import (
    HostId,
    IngestEvent,
    IngestEventType,
    MessageContent,
    SessionEndRequest,
)

#: The hook event that carries the T1 read lane (no ingest, no settle).
T1_HOOK_EVENT = "SessionStart"

#: The hook event that carries the T2 additionalContext formback.
INJECTION_HOOK_EVENT = "UserPromptSubmit"

#: Hook events that run the transcript differential (SessionEnd adds the settle).
DIFFERENTIAL_EVENTS = ("Stop", "SessionEnd")


def _transcript_turns(payload: dict[str, Any]) -> list[TranscriptTurn]:
    try:
        return read_turns(payload.get("transcript_path"))
    except RuntimeError as exc:  # the missing optional extra is a setup fault
        print(f"mnemoseed-local: {exc}", file=sys.stderr)
        return []


def _ingest_body(
    event_type: IngestEventType,
    payload: dict[str, Any],
    session_id: str,
    text: str,
    ordinal: int | None,
    *,
    model_id: str | None = None,
) -> IngestEvent:
    return IngestEvent(
        host=HostId.CODEX_CLI,
        event=event_type,
        session_id=session_id,
        profile_id=profile_id(),
        ts=time.time(),
        content=MessageContent(text=text, model_id=model_id),
        transcript_ordinal=ordinal,
        raw=payload,
    )


def _ack_ordinal(reply: Any) -> int | None:
    ack = reply.get("ack_ordinal") if isinstance(reply, dict) else None
    if isinstance(ack, int) and not isinstance(ack, bool):
        return ack
    return None


def _t1_lane(session_id: str) -> None:
    client = hook_client(timeout=POST_TIMEOUT_SECONDS)
    block = session_start_block(pull_session_tails(client, session_id))
    if block is not None:
        print(json.dumps(additional_context_payload(block, T1_HOOK_EVENT)))


def _user_prompt_lane(payload: dict[str, Any], session_id: str) -> None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        debug("dropped prompt-less UserPromptSubmit")
        return
    turns = _transcript_turns(payload)
    client = hook_client(timeout=POST_TIMEOUT_SECONDS)
    body = _ingest_body(
        IngestEventType.USER_PROMPT, payload, session_id, prompt, anchor_ordinal(turns, prompt)
    )
    client.post("/ingest", body.model_dump(mode="json"))
    # only an ACKED user ingest parks the focal slot, so the pull runs
    # strictly after the 2xx; a served selection is the ONE stdout this
    # lane may write
    block = inject_recall_context(session_id, client)
    if block is not None:
        print(json.dumps(additional_context_payload(block, INJECTION_HOOK_EVENT)))


def _differential_lane(payload: dict[str, Any], session_id: str, *, settle: bool) -> None:
    turns = _transcript_turns(payload)
    floor = -1
    for turn in turns:
        if turn.role == "user":
            floor = turn.ordinal
    model = payload.get("model")
    model_id = model if isinstance(model, str) and model else None
    client = hook_client(timeout=POST_TIMEOUT_SECONDS)
    ack: int | None = None
    for turn in turns:
        if turn.ordinal <= floor:
            continue
        if ack is not None and turn.ordinal <= ack:
            continue
        body = _ingest_body(
            IngestEventType.ASSISTANT_MESSAGE,
            payload,
            session_id,
            turn.text,
            turn.ordinal,
            model_id=model_id,
        )
        reply = client.post("/ingest", body.model_dump(mode="json"))
        if ack is None:
            ack = _ack_ordinal(reply)
    if settle:
        client.post(
            "/session/end",
            SessionEndRequest(session_id=session_id, profile_id=profile_id(), ts=time.time()).model_dump(
                mode="json"
            ),
        )


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
    name = event.get("hook_event_name")
    session_id = event.get("session_id")
    if not isinstance(name, str) or not isinstance(session_id, str) or not session_id.strip():
        debug("dropped identity-less payload")
        return 0
    try:
        if name == "SessionStart":
            _t1_lane(session_id)
        elif name == "UserPromptSubmit":
            _user_prompt_lane(event, session_id)
        elif name in DIFFERENTIAL_EVENTS:
            _differential_lane(event, session_id, settle=name == "SessionEnd")
        else:
            debug(f"dropped unmapped event {name!r}")
    except Exception as exc:  # noqa: BLE001 - fire-and-forget contract
        debug(f"{name} failed: {exc}")
    return 0
