"""S0 contract pinning for the #272 host adapters.

Golden stdin fixtures under ``tests/fixtures/zcode_hook/`` and
``tests/fixtures/codex_hook/`` pin the payload shapes the adapters build
against, plus the additive ``zcode`` HostId value and its stamper prefix.
Allowlist provenance is explicit: Codex shapes come from the official hooks
documentation (developers.openai.com/codex/hooks, fetched 2026-10-24, fetched
independently after a QA correction of the earlier field list); ZCode stdin
field names are the Claude-Code-dialect hypothesis from PRD-272 §3.2 — the
official zcode-guide 0.3.0 skills pin registration, matcher and output
semantics but no stdin payload fields, so S2a confirms the dialect with one
real capture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_stamper import _Clock, _FakeVectorStore

from mnemoseed_local.capture.scorer import (
    Durability,
    DurabilityResult,
    ScoreComponents,
    ScoredTurn,
)
from mnemoseed_local.capture.stamper import StampWriter, WriteContext, WriteOutcomeKind
from mnemoseed_local.schema.turn import HostId, IngestEvent, Turn, TurnRole, TurnStep
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder

FIXTURE_DIR = Path(__file__).parent / "fixtures"

ZCODE_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")
CODEX_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")

# ZCode stdin field allowlists: Claude-Code-compatible dialect (verify on first
# live capture).
ZCODE_DIALECT_FIELDS: dict[str, tuple[str, ...]] = {
    "SessionStart": ("hook_event_name", "session_id", "cwd", "source"),
    "UserPromptSubmit": ("hook_event_name", "session_id", "cwd", "prompt"),
    "PostToolUse": (
        "hook_event_name",
        "session_id",
        "cwd",
        "tool_name",
        "tool_input",
        "tool_response",
        "tool_use_id",
    ),
    "Stop": (
        "hook_event_name",
        "session_id",
        "cwd",
        "transcript_path",
        "stop_hook_active",
        "last_assistant_message",
    ),
}

# Official Codex hooks docs (developers.openai.com/codex/hooks, fetched
# 2026-10-24): every command hook receives the five common fields on stdin;
# permission_mode rides nine events (SessionEnd NOT among them); turn-scoped
# events add turn_id; SessionStart adds source, UserPromptSubmit adds prompt,
# Stop adds stop_hook_active/last_assistant_message, SessionEnd adds reason.
# Tool/subagent event additions (tool_name/agent_id/...) stay off these
# fixtures — only the four lifecycle events are subscribed.
CODEX_COMMON_FIELDS = (
    "session_id",
    "transcript_path",
    "cwd",
    "hook_event_name",
    "model",
)
CODEX_PERMISSION_MODES = ("default", "acceptEdits", "plan", "dontAsk", "bypassPermissions")
CODEX_START_SOURCES = ("startup", "resume", "clear", "compact", "fork")
CODEX_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "SessionStart": ("permission_mode", "source"),
    "UserPromptSubmit": ("permission_mode", "turn_id", "prompt"),
    "Stop": ("permission_mode", "turn_id", "stop_hook_active", "last_assistant_message"),
    "SessionEnd": ("reason",),
}


def _snake(event: str) -> str:
    return {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt",
        "PostToolUse": "tool_use",
        "Stop": "stop",
        "SessionEnd": "session_end",
    }[event]


def _fixture(host: str, event: str) -> dict:
    path = FIXTURE_DIR / f"{host}_hook" / f"{_snake(event)}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------- HostId additive


def test_zcode_enum_value_is_purely_additive() -> None:
    assert [(member.name, member.value) for member in HostId] == [
        ("CLAUDE_CODE", "claude_code"),
        ("CURSOR", "cursor"),
        ("CODEX_CLI", "codex_cli"),
        ("GEMINI_CLI", "gemini_cli"),
        ("OPENCODE", "opencode"),
        ("WINDSURF", "windsurf"),
        ("ZCODE", "zcode"),
        ("GENERIC", "generic"),
    ]


def test_zcode_host_validates_on_the_ingest_wire() -> None:
    event = IngestEvent.model_validate(
        {
            "host": "zcode",
            "event": "user_prompt",
            "session_id": "zc-sess-01",
            "profile_id": "default",
            "ts": 0.0,
            "content": {"text": "hi"},
        }
    )
    assert event.host is HostId.ZCODE


def test_stamper_derives_zcode_chat_source_prefix() -> None:
    turn = Turn(
        turn_index=0,
        session_id="zc-sess-01",
        profile_id="prof-main",
        host=HostId.ZCODE,
        model_id="test-model",
        started_at=0.0,
        steps=[TurnStep(role=TurnRole.USER, content="hello")],
    )
    scored = ScoredTurn(
        turn=turn,
        importance=5.0,
        components=ScoreComponents(arousal=5.0, novelty=5.0, causal_chain=2.0),
        durability=DurabilityResult(
            durability=Durability.DURABLE,
            confidence=0.85,
            reasons=["pref-marker"],
        ),
        emotion=None,
        causal_reasons=[],
        features={},
    )
    store = _FakeVectorStore()
    outcome = StampWriter(store=store, embedder=SyntheticEmbedder(), clock=_Clock()).write(
        scored, WriteContext(profile_id="prof-main")
    )
    assert outcome.kind is WriteOutcomeKind.NEW_CHUNK
    assert store.chunks[outcome.chunk_id].provenance.source == "zcode-chat"


# ------------------------------------------------------------ ZCode fixtures


@pytest.mark.parametrize(
    "host,event", [(h, e) for h, events in (("zcode", ZCODE_EVENTS), ("codex", CODEX_EVENTS)) for e in events]
)
def test_fixtures_are_single_line_json(host: str, event: str) -> None:
    path = FIXTURE_DIR / f"{host}_hook" / f"{_snake(event)}.json"
    raw = path.read_text(encoding="utf-8")
    assert "\n" not in raw.strip()
    json.loads(raw)


@pytest.mark.parametrize("event", ZCODE_EVENTS)
def test_zcode_fixture_matches_dialect_allowlist(event: str) -> None:
    payload = _fixture("zcode", event)
    assert payload["hook_event_name"] == event
    assert isinstance(payload["session_id"], str) and payload["session_id"].strip()
    assert set(payload) == set(ZCODE_DIALECT_FIELDS[event])
    for key in ("hook_event_name", "session_id", "cwd"):
        assert isinstance(payload[key], str)
    if event == "SessionStart":
        assert isinstance(payload["source"], str)
    elif event == "UserPromptSubmit":
        assert isinstance(payload["prompt"], str) and payload["prompt"].strip()
    elif event == "PostToolUse":
        assert isinstance(payload["tool_name"], str) and payload["tool_name"].strip()
        assert isinstance(payload["tool_input"], dict)
        assert isinstance(payload["tool_use_id"], str) and payload["tool_use_id"].strip()
    else:  # Stop
        assert isinstance(payload["transcript_path"], str) and payload["transcript_path"].strip()
        assert isinstance(payload["stop_hook_active"], bool)
        assert (
            isinstance(payload["last_assistant_message"], str) and payload["last_assistant_message"].strip()
        )


# ------------------------------------------------------------ Codex fixtures


@pytest.mark.parametrize("event", CODEX_EVENTS)
def test_codex_fixture_matches_documented_allowlist(event: str) -> None:
    payload = _fixture("codex", event)
    assert payload["hook_event_name"] == event
    # Whitelist-strict: exact key-set equality, so any extra key goes red.
    assert set(payload) == set(CODEX_COMMON_FIELDS) | set(CODEX_EVENT_FIELDS[event])
    for key in CODEX_COMMON_FIELDS:
        assert isinstance(payload[key], str)
    assert payload["session_id"].strip()
    assert payload["transcript_path"].strip()
    assert payload["transcript_path"].endswith(".jsonl.zst")
    assert payload["model"].strip()
    if "permission_mode" in payload:
        assert payload["permission_mode"] in CODEX_PERMISSION_MODES
    if event == "SessionStart":
        assert payload["source"] in CODEX_START_SOURCES
    elif event == "UserPromptSubmit":
        assert isinstance(payload["turn_id"], str) and payload["turn_id"].strip()
        assert isinstance(payload["prompt"], str) and payload["prompt"].strip()
    elif event == "Stop":
        assert isinstance(payload["turn_id"], str) and payload["turn_id"].strip()
        assert isinstance(payload["stop_hook_active"], bool)
        assert (
            isinstance(payload["last_assistant_message"], str) and payload["last_assistant_message"].strip()
        )
    else:  # SessionEnd
        assert payload["reason"] == "other"
