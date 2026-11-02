"""Cursor host adapter (PRD-293): the snake-case dialect transformer and the
two-file installer surgery.

Golden fixtures under ``tests/fixtures/cursor_hook/`` pin the payload shapes
the ``_hook-event --host cursor`` transformer consumes (Cursor's documented
agent-hook input schema; the key sets are whitelist-strict along the
``test_s0_host_contracts`` precedent). The frozen event map: ``sessionStart``
-> the T1 read lane (flat ``{"additional_context": ...}`` stdout),
``beforeSubmitPrompt`` -> ``user_prompt`` ingest plus the exact
``{"continue": true}`` allow, ``postToolUse`` -> ``tool_use`` ingest,
``afterAgentResponse`` -> ``assistant_message`` ingest, ``stop`` -> /flush
only (the payload carries no text), ``sessionEnd`` -> /session/end settle.
There is NO T2 mid-session injection (``beforeSubmitPrompt`` has no
additionalContext channel): recall on Cursor is T1 + the MCP gateway only.
The binding gate walks the cwd proxy: the event's real cwd when it carries
one, else ``workspace_roots[0]`` in a single-root workspace, else the gate's
honest no-cwd path. The installer merges marked entries into
``~/.cursor/hooks.json`` (argv-tail marker, the command never compared) and
the canonical stdio gateway into the sibling ``~/.cursor/mcp.json`` (no
secret, no pinned profile); Cursor has no hooks master switch, so status
knows no ``disabled`` state.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import auth_support
import pytest
from _ports import unused_loopback_port

from mnemoseed_local.cli import HOOK_HOSTS, main
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.hosts import install as shared
from mnemoseed_local.hosts.common import project_binding as pb
from mnemoseed_local.hosts.common.commands import render_command
from mnemoseed_local.hosts.common.daemon import RESTART_INSTRUCTION
from mnemoseed_local.hosts.common.fence import (
    RECALL_DISCLAIMER,
    RECALL_FENCE_CLOSE,
    RECALL_FENCE_OPEN,
)
from mnemoseed_local.hosts.common.recall import T1_SIGNAL
from mnemoseed_local.hosts.cursor import events
from mnemoseed_local.hosts.cursor import install as cur_install
from mnemoseed_local.rest_client import DaemonClient, DaemonRestError, DaemonUnavailableError
from mnemoseed_local.schema.turn import FlushRequest, HostId, IngestEvent, IngestEventType, SessionEndRequest
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "cursor_hook"

ENV_VARS = (
    "MNEMOSEED_LOCAL_BASEURL",
    "MNEMOSEED_LOCAL_PROFILE_ID",
    "MNEMOSEED_LOCAL_DEBUG",
    "MNEMOSEED_LOCAL_DATA_DIR",
    "MNEMOSEED_LOCAL_HOME",
)

SESSION = "cur-conv-01"

# Cursor's documented agent-hook input schema (PRD-293, official docs fetched
# 2026-10-31): the common fields ride every event; each event adds its own.
# Whitelist-strict: exact key-set equality, so any extra key goes red.
CURSOR_COMMON_FIELDS = (
    "hook_event_name",
    "conversation_id",
    "generation_id",
    "model",
    "workspace_roots",
    "transcript_path",
)
CURSOR_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "sessionStart": ("session_id", "is_background_agent", "composer_mode"),
    "beforeSubmitPrompt": ("prompt", "attachments"),
    "postToolUse": ("tool_name", "tool_input", "tool_output", "cwd", "duration"),
    "afterAgentResponse": ("text",),
    "stop": ("status", "loop_count"),
    "sessionEnd": (
        "session_id",
        "reason",
        "duration_ms",
        "is_background_agent",
        "final_status",
        "error_message",
    ),
}

SUBMIT_ALLOW = '{"continue": true}\n'


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def cur_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic Cursor config root: <tmp home>/.cursor."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path / ".cursor"


@pytest.fixture
def captured_posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Record DaemonClient.post calls instead of hitting the network."""
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    return calls


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _run_transformer(monkeypatch: pytest.MonkeyPatch, payload: object) -> int:
    _feed_stdin(monkeypatch, payload)
    return main(["_hook-event", "--host", "cursor"])


def _recent_payload(**overrides: object) -> dict:
    payload = {
        "profile_id": "default",
        "sessions": [
            {
                "session_id": "cur-earlier-session-77",
                "latest_at": 1761000000.0,
                "window": {
                    "first": "2026-10-20T10:00:00+00:00",
                    "latest": "2026-10-20T11:30:00+00:00",
                },
                "window_truncated": False,
                "active": False,
                "seconds_since_last_activity": 120.0,
                "chunks": [
                    {"chunk_id": "c1", "text": "第一条线索"},
                    {"chunk_id": "c2", "text": "第二条线索"},
                ],
            }
        ],
        "self_window": None,
        "guidance": "",
        "selection": "unscoped",
        "visibility": {"mode": "compact"},
    }
    payload.update(overrides)
    return payload


def _capture_recent(monkeypatch: pytest.MonkeyPatch, payload: dict) -> list[tuple[str, dict]]:
    """Route DaemonClient.post so /session/recent answers `payload`."""
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        if path == "/session/recent":
            return payload
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    return calls


# ---------------------------------------------------------------- golden fixtures (whitelist-strict)


def _snake(event: str) -> str:
    return {
        "sessionStart": "session_start",
        "beforeSubmitPrompt": "before_submit_prompt",
        "postToolUse": "post_tool_use",
        "afterAgentResponse": "after_agent_response",
        "stop": "stop",
        "sessionEnd": "session_end",
    }[event]


@pytest.mark.parametrize("event", tuple(CURSOR_EVENT_FIELDS))
def test_fixtures_are_single_line_json(event: str) -> None:
    raw = (FIXTURE_DIR / f"{_snake(event)}.json").read_text(encoding="utf-8")
    assert "\n" not in raw.strip()
    json.loads(raw)


@pytest.mark.parametrize("event", tuple(CURSOR_EVENT_FIELDS))
def test_cursor_fixture_matches_the_documented_allowlist(event: str) -> None:
    payload = _fixture(_snake(event))
    assert payload["hook_event_name"] == event
    # Whitelist-strict: exact key-set equality, so any extra key goes red.
    assert set(payload) == set(CURSOR_COMMON_FIELDS) | set(CURSOR_EVENT_FIELDS[event])
    assert isinstance(payload["conversation_id"], str) and payload["conversation_id"].strip()
    assert isinstance(payload["generation_id"], str) and payload["generation_id"].strip()
    assert isinstance(payload["model"], str) and payload["model"].strip()
    assert isinstance(payload["workspace_roots"], list) and payload["workspace_roots"]
    assert all(isinstance(root, str) and root.strip() for root in payload["workspace_roots"])
    assert isinstance(payload["transcript_path"], str) and payload["transcript_path"].strip()
    if event == "sessionStart":
        assert payload["session_id"] == payload["conversation_id"]
        assert payload["is_background_agent"] is False
        assert isinstance(payload["composer_mode"], str) and payload["composer_mode"].strip()
    elif event == "beforeSubmitPrompt":
        assert isinstance(payload["prompt"], str) and payload["prompt"].strip()
        assert isinstance(payload["attachments"], list)
    elif event == "postToolUse":
        assert isinstance(payload["tool_name"], str) and payload["tool_name"].strip()
        assert isinstance(payload["tool_input"], dict)
        assert isinstance(payload["tool_output"], str) and payload["tool_output"].strip()
        assert isinstance(payload["cwd"], str) and payload["cwd"].strip()
        assert isinstance(payload["duration"], (int, float)) and not isinstance(payload["duration"], bool)
    elif event == "afterAgentResponse":
        assert isinstance(payload["text"], str) and payload["text"].strip()
    elif event == "stop":
        assert payload["status"] in ("completed", "aborted", "error")
        assert isinstance(payload["loop_count"], int) and not isinstance(payload["loop_count"], bool)
    else:  # sessionEnd
        assert payload["session_id"] == payload["conversation_id"]
        assert payload["reason"] in ("completed", "aborted", "error", "window_close", "user_close")
        assert isinstance(payload["duration_ms"], int) and not isinstance(payload["duration_ms"], bool)
        assert payload["is_background_agent"] is False
        assert payload["final_status"] in ("completed", "aborted", "error")
        assert payload["error_message"] is None


# ---------------------------------------------------------------- event map (normalization)


@pytest.mark.parametrize(
    "name,event_type",
    [
        ("before_submit_prompt", IngestEventType.USER_PROMPT),
        ("post_tool_use", IngestEventType.TOOL_USE),
        ("after_agent_response", IngestEventType.ASSISTANT_MESSAGE),
    ],
)
def test_capture_fixtures_ingest_with_cursor_attribution(name: str, event_type: IngestEventType) -> None:
    fixture = _fixture(name)
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert isinstance(body, IngestEvent)
    assert body.host is HostId.CURSOR
    assert body.event is event_type
    # the conversation_id dialect: Cursor's cross-turn session identity
    assert body.session_id == fixture["conversation_id"]
    assert body.profile_id == "default"
    assert body.agent is None
    assert body.raw == fixture


def test_session_start_fixture_maps_to_the_t1_read_lane() -> None:
    """sessionStart never ingests: it is the T1 read lane keyed by the
    conversation id (the docs pin session_id == conversation_id)."""
    fixture = _fixture("session_start")
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    kind, body = action
    assert kind == "t1"
    assert body == fixture["conversation_id"]


def test_stop_fixture_maps_to_flush_only() -> None:
    """The Stop combo is split: Cursor's stop carries no text (assistant text
    rode the earlier afterAgentResponse), so stop is the flush lane only."""
    fixture = _fixture("stop")
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    kind, body = action
    assert kind == "flush"
    assert isinstance(body, FlushRequest)
    assert body.session_id == fixture["conversation_id"]
    assert body.profile_id == "default"


def test_session_end_fixture_maps_to_the_settle() -> None:
    fixture = _fixture("session_end")
    action = events.normalize_event(fixture, now=2.0)
    assert action is not None
    kind, body = action
    assert kind == "session_end"
    assert isinstance(body, SessionEndRequest)
    assert body.session_id == fixture["conversation_id"]
    assert body.profile_id == "default"
    assert body.ts == 2.0
    # the wire model has no reason field; the host's reason rides raw only
    assert "reason" not in body.model_dump(mode="json")


def test_events_module_endpoint_map_is_frozen() -> None:
    assert events.ENDPOINTS == {"ingest": "/ingest", "flush": "/flush", "session_end": "/session/end"}


def test_assistant_message_lifts_the_model_from_the_common_schema() -> None:
    fixture = _fixture("after_agent_response")
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.model_id == fixture["model"]
    dual = dict(fixture, model_id="glm-4.7")
    action = events.normalize_event(dual, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.model_id == "glm-4.7", "an explicit model_id wins over the model field"
    modelless = dict(fixture, model=None)
    action = events.normalize_event(modelless, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.model_id is None


def test_tool_use_flattens_the_output_and_caps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture("post_tool_use")
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.tool_name == "Terminal"
    assert body.content.input == {"command": "uv run pytest -q"}
    assert body.content.output == "938 passed, 5 skipped"
    monkeypatch.setattr(events, "MAX_TOOL_OUTPUT_CHARS", 20)
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.output.endswith(events.TOOL_TRUNCATION_MARKER)
    loose = dict(fixture, tool_input="not a dict", tool_output={"stdout": "out", "stderr": ""})
    action = events.normalize_event(loose, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.input == {}
    assert body.content.output == "out"


def test_normalized_wire_bodies_validate_against_the_daemon_schema() -> None:
    for name in ("before_submit_prompt", "post_tool_use", "after_agent_response"):
        action = events.normalize_event(_fixture(name), now=1.0)
        assert action is not None
        kind, body = action
        assert kind == "ingest"
        assert isinstance(body, IngestEvent)
        IngestEvent.model_validate(body.model_dump(mode="json"))


def test_unsubscribed_cursor_events_are_dropped() -> None:
    """The permission/shell/file/subagent/Tab family is not subscribed: any
    such firing is an unmapped event (fail-open, zero stdout, no posts)."""
    for event in (
        "preToolUse",
        "postToolUseFailure",
        "beforeShellExecution",
        "afterShellExecution",
        "beforeReadFile",
        "afterFileEdit",
        "subagentStart",
        "subagentStop",
        "preCompact",
        "afterAgentThought",
        "beforeTabFileRead",
        "workspaceOpen",
    ):
        payload = dict(_fixture("post_tool_use"), hook_event_name=event)
        assert events.normalize_event(payload, now=1.0) is None, event


def test_identityless_payloads_are_dropped() -> None:
    for payload in (
        {"hook_event_name": "beforeSubmitPrompt", "prompt": "x"},
        {"conversation_id": "  ", "hook_event_name": "stop"},
        {"conversation_id": SESSION},
    ):
        assert events.normalize_event(payload, now=1.0) is None, payload


# ---------------------------------------------------------------- T1 session-start replay (flat stdout)


def test_session_start_pulls_recent_once_with_the_legacy_identity_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_recent(monkeypatch, _recent_payload())
    assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
    assert [path for path, _ in calls] == ["/session/recent"]
    body = calls[0][1]
    assert set(body) == {"profile_id", "sessions", "per_session", "exclude_session_id"}
    assert body["profile_id"] == "default"
    assert body["exclude_session_id"] == SESSION


def test_session_start_injects_the_flat_additional_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The served T1 block rides Cursor's FLAT native stdout shape — exactly
    one key, never Claude's nested hookSpecificOutput envelope."""
    _capture_recent(monkeypatch, _recent_payload())
    assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
    raw = capsys.readouterr().out
    assert "hookSpecificOutput" not in raw
    out = json.loads(raw)
    assert set(out) == {"additional_context"}
    block = out["additional_context"]
    assert block.startswith(RECALL_FENCE_OPEN)
    assert block.endswith(RECALL_FENCE_CLOSE)
    assert block.count("<mnemoseed-memory-recall>") == 1
    lines = block.split("\n")
    assert lines[1] == T1_SIGNAL, "the compact T1 signal rides after the fence-open line"
    assert RECALL_DISCLAIMER in block
    assert '<session-tail id="77" ' in block, "the header id is the last dash segment"
    assert block.index("第一条线索") < block.index("第二条线索"), "chunks render ascending"
    assert len(block) <= 4000


def test_session_start_without_served_content_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_recent(monkeypatch, _recent_payload(sessions=[]))
    assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
    assert capsys.readouterr().out == ""


def test_session_start_malformed_recent_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for payload in (None, {}, {"sessions": "garbage"}):
        _capture_recent(monkeypatch, payload)  # type: ignore[arg-type]
        assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
        assert capsys.readouterr().out == ""


def test_session_start_off_or_absent_visibility_drops_the_signal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wire visibility mode is the only signal authority, unchanged."""
    for visibility in ({"mode": "off"}, None):
        payload: dict = _recent_payload()
        if visibility is None:
            payload.pop("visibility", None)
        else:
            payload["visibility"] = visibility
        _capture_recent(monkeypatch, payload)
        assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
        out = json.loads(capsys.readouterr().out)
        assert "[mnemoseed]" not in out["additional_context"], visibility


def test_session_start_pull_failure_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonRestError(503, "daemon down")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- beforeSubmitPrompt: the explicit allow


def test_submit_ingests_then_prints_exactly_the_allow(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_transformer(monkeypatch, _fixture("before_submit_prompt")) == 0
    assert capsys.readouterr().out == SUBMIT_ALLOW
    assert [path for path, _ in captured_posts] == ["/ingest"]
    body = captured_posts[0][1]
    assert body["host"] == "cursor"
    assert body["event"] == "user_prompt"
    assert body["session_id"] == SESSION
    assert body["content"]["text"] == _fixture("before_submit_prompt")["prompt"]


def test_submit_never_pulls_the_t2_lane(
    monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    """No T2 passive injection on Cursor: the prompt ingest is capture-only —
    no /session/recall-pending pull ever rides beforeSubmitPrompt."""
    assert _run_transformer(monkeypatch, _fixture("before_submit_prompt")) == 0
    assert [path for path, _ in captured_posts] == ["/ingest"]


def test_submit_allow_rides_every_recognized_firing(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The documented explicit allow never depends on the binding outcome, the
    event's identity, the prompt's content, or the daemon's health — the empty
    stdout semantics for beforeSubmitPrompt are undocumented and never relied
    on."""
    cases: list[dict[str, Any]] = [
        dict(_fixture("before_submit_prompt"), prompt="   "),
        {"hook_event_name": "beforeSubmitPrompt", "prompt": "x"},
    ]
    for payload in cases:
        captured_posts.clear()
        assert _run_transformer(monkeypatch, payload) == 0, payload
        assert capsys.readouterr().out == SUBMIT_ALLOW, payload
        assert captured_posts == [], payload


def test_submit_allow_rides_a_daemon_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonUnavailableError("connection refused")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    assert _run_transformer(monkeypatch, _fixture("before_submit_prompt")) == 0
    assert capsys.readouterr().out == SUBMIT_ALLOW


def test_malformed_stdin_prints_nothing_and_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a PARSED object can be identified as a submit firing; malformed or
    non-object stdin stays on the zero-stdout red line."""
    for raw in ("{not json", "[]", '"text"'):
        assert _run_transformer(monkeypatch, raw) == 0, raw
        assert capsys.readouterr().out == "", raw
    assert captured_posts == []


# ---------------------------------------------------------------- stop = flush only / sessionEnd = settle


def test_stop_posts_only_the_flush(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_transformer(monkeypatch, _fixture("stop")) == 0
    assert capsys.readouterr().out == ""
    assert [path for path, _ in captured_posts] == ["/flush"]
    body = captured_posts[0][1]
    assert body["session_id"] == SESSION
    assert body["profile_id"] == "default"
    assert "assistant" not in json.dumps(captured_posts), "stop must not ingest any text"


def test_session_end_posts_only_the_settle(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_transformer(monkeypatch, _fixture("session_end")) == 0
    assert capsys.readouterr().out == ""
    assert [path for path, _ in captured_posts] == ["/session/end"]
    body = captured_posts[0][1]
    assert set(body) == {"session_id", "profile_id", "ts"}
    assert body["session_id"] == SESSION
    assert body["profile_id"] == "default"


def test_tool_and_assistant_lanes_stay_capture_only(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("post_tool_use", "after_agent_response"):
        assert _run_transformer(monkeypatch, _fixture(name)) == 0, name
        assert capsys.readouterr().out == ""
    assert [path for path, _ in captured_posts] == ["/ingest", "/ingest"]


def test_all_lanes_fail_open_when_the_daemon_is_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonUnavailableError("connection refused")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    for name in ("session_start", "post_tool_use", "after_agent_response", "stop", "session_end"):
        assert _run_transformer(monkeypatch, _fixture(name)) == 0, name
        assert capsys.readouterr().out == ""
    assert _run_transformer(monkeypatch, _fixture("before_submit_prompt")) == 0
    assert capsys.readouterr().out == SUBMIT_ALLOW


# ---------------------------------------------------------------- binding gate: the cwd proxy


@pytest.fixture
def gate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    return home


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True)
    return path


def test_single_root_workspace_proxies_through_the_gate(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, "alpha")
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "alpha")
    payload = dict(_fixture("after_agent_response"), workspace_roots=[str(root)])
    assert events.transform_event(json.dumps(payload)) == 0
    assert [path for path, _ in captured_posts] == ["/ingest"]
    assert captured_posts[0][1]["profile_id"] == "alpha"


def test_tool_event_prefers_its_real_cwd(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    elsewhere = _mkdir(tmp_path / "elsewhere")
    pb.bind(gate_home, root, "alpha")
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "alpha")
    payload = dict(_fixture("post_tool_use"), workspace_roots=[str(elsewhere)], cwd=str(root))
    assert events.transform_event(json.dumps(payload)) == 0
    assert [path for path, _ in captured_posts] == ["/ingest"]


def test_multiroot_workspace_walks_the_honest_no_cwd_path_when_unbound(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    """Multiroot feeds the gate no cwd; with nothing bound anywhere the
    explicit env selection is the honored manual path and capture proceeds."""
    root_a = _mkdir(tmp_path / "proj-a")
    root_b = _mkdir(tmp_path / "proj-b")
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "alpha")
    payload = dict(_fixture("after_agent_response"), workspace_roots=[str(root_a), str(root_b)])
    assert events.transform_event(json.dumps(payload)) == 0
    assert [path for path, _ in captured_posts] == ["/ingest"]
    assert captured_posts[0][1]["profile_id"] == "alpha"


def test_multiroot_workspace_is_refused_when_roots_are_bound(
    gate_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, "alpha")
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "alpha")
    payload = dict(
        _fixture("after_agent_response"), workspace_roots=[str(root), str(_mkdir(tmp_path / "other"))]
    )
    assert events.transform_event(json.dumps(payload)) == 0
    assert captured_posts == [], "a refused event makes no daemon write"
    err = capsys.readouterr().err
    assert "event dropped" in err
    assert "project run" in err
    assert RESTART_INSTRUCTION in err


def test_binding_refusal_is_visible_and_writes_nothing(
    gate_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, "alpha")
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "beta")
    payload = dict(_fixture("after_agent_response"), workspace_roots=[str(root)])
    assert events.transform_event(json.dumps(payload)) == 0
    assert captured_posts == []
    err = capsys.readouterr().err
    assert "event dropped" in err
    assert "alpha" in err and "beta" in err


def test_binding_refused_submit_still_prints_the_allow(
    gate_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, "alpha")
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "beta")
    payload = dict(_fixture("before_submit_prompt"), workspace_roots=[str(root)])
    assert events.transform_event(json.dumps(payload)) == 0
    assert captured_posts == []
    captured = capsys.readouterr()
    assert captured.out == SUBMIT_ALLOW
    assert "event dropped" in captured.err


def test_legacy_flow_without_a_profile_env_stays_the_default_entry(
    gate_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    pb.bindings_path(gate_home).write_bytes(b"{ broken ")
    monkeypatch.setenv("MNEMOSEED_LOCAL_DEBUG", "1")
    assert events.transform_event(json.dumps(_fixture("after_agent_response"))) == 0
    assert [path for path, _ in captured_posts] == ["/ingest"]
    assert captured_posts[0][1]["profile_id"] == "default"
    assert "unbound" in capsys.readouterr().err
    assert pb.bindings_path(gate_home).read_bytes() == b"{ broken "


# ---------------------------------------------------------------- installer surgery


FOREIGN_HOOKS = {
    "version": 1,
    "hooks": {
        "stop": [{"command": "my-own-hook --flag", "timeout": 5}],
        "postToolUse": [{"command": "C:/tools/mnemoseed-local-helper.exe watch"}],
    },
    "theme": "dark",
}
FOREIGN_MCP = {
    "mcpServers": {
        "本地服务器": {"command": "npx", "args": ["-y", "some/server", "${env:LOCAL_TOKEN}"]},
    }
}


def _seed(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _handlers_of(config: dict, event: str) -> list[dict]:
    found = []
    for handler in config.get("hooks", {}).get(event, []):
        if isinstance(handler, dict) and cur_install.is_ours(handler):
            found.append(handler)
    return found


@pytest.fixture
def stable_command(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Pin both resolution surfaces: the handler command string and the MCP
    entry derive from the same resolver."""
    resolved = (str(Path(__file__).resolve()), cur_install.MODULE_TRANSFORM_ARGS)
    monkeypatch.setattr(cur_install, "resolve_command", lambda: resolved)
    executable = resolved[0]
    return executable, cur_install.transform_command()


def test_install_writes_the_canonical_hooks_and_mcp_files(
    cur_home: Path, stable_command: tuple[str, str]
) -> None:
    executable, command = stable_command
    path, changed = cur_install.install()
    assert changed is True
    assert path == cur_home / "hooks.json"
    hooks = _read(path)
    assert hooks["version"] == 1
    assert set(hooks["hooks"]) == set(cur_install.HOOK_EVENTS)
    for event in cur_install.HOOK_EVENTS:
        handlers = _handlers_of(hooks, event)
        assert len(handlers) == 1, event
        handler = handlers[0]
        assert set(handler) == {"command", "timeout"}
        assert handler["command"] == command
        assert handler["timeout"] == cur_install.HANDLER_TIMEOUT_SECONDS
        assert cur_install.is_ours(handler)
    assert not (cur_home / "mnemoseed-local.json").is_file(), "no prestate sidecar exists for Cursor"
    mcp = _read(cur_home / "mcp.json")
    assert mcp["mcpServers"]["mnemoseed-local"] == {
        "command": executable,
        "args": ["-m", "mnemoseed_local.cli", "mcp"],
    }


def test_registered_command_ends_with_the_reserved_argv_tail() -> None:
    command = cur_install.transform_command()
    assert command.split()[-3:] == ["_hook-event", "--host", "cursor"]
    assert cur_install.is_ours({"command": command})
    executable, args = cur_install.resolve_command()
    assert Path(executable).is_absolute()
    assert args[-3:] == ("_hook-event", "--host", "cursor")


def test_mcp_entry_carries_no_secret_and_pins_no_profile() -> None:
    entry = cur_install.mcp_server_entry()
    assert Path(str(entry["command"])).is_absolute()
    assert tuple(entry["args"]) in (
        cur_install.MCP_GATEWAY_ARGS,
        cur_install.MCP_MODULE_GATEWAY_ARGS,
    )
    assert "token" not in json.dumps(entry).lower()
    assert "profile" not in json.dumps(entry).lower()


def test_install_is_idempotent_byte_identical(cur_home: Path, stable_command: tuple[str, str]) -> None:
    path = cur_home / "hooks.json"
    _seed(path, FOREIGN_HOOKS)
    _seed(cur_home / "mcp.json", FOREIGN_MCP)
    cur_install.install()
    first_hooks = path.read_bytes()
    first_mcp = (cur_home / "mcp.json").read_bytes()
    _, changed = cur_install.install()
    assert changed is False
    assert path.read_bytes() == first_hooks
    assert (cur_home / "mcp.json").read_bytes() == first_mcp


def test_install_never_mutates_foreign_entries(cur_home: Path, stable_command: tuple[str, str]) -> None:
    _seed(cur_home / "hooks.json", FOREIGN_HOOKS)
    _seed(cur_home / "mcp.json", FOREIGN_MCP)
    cur_install.install()
    hooks = _read(cur_home / "hooks.json")
    assert hooks["theme"] == FOREIGN_HOOKS["theme"]
    # foreign entries on subscribed events keep their exact position and value
    assert hooks["hooks"]["stop"][0] == FOREIGN_HOOKS["hooks"]["stop"][0]
    assert hooks["hooks"]["postToolUse"][0] == FOREIGN_HOOKS["hooks"]["postToolUse"][0]
    assert _read(cur_home / "mcp.json")["mcpServers"]["本地服务器"] == FOREIGN_MCP["mcpServers"]["本地服务器"]


def test_marker_mentioning_foreign_command_is_never_ours(
    cur_home: Path, stable_command: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(cur_home / "hooks.json", FOREIGN_HOOKS)
    _, changed = cur_install.install()
    assert changed is True
    hooks = _read(cur_home / "hooks.json")
    assert len(_handlers_of(hooks, "postToolUse")) == 1
    _, existed = cur_install.uninstall()
    assert existed is True
    hooks = _read(cur_home / "hooks.json")
    assert hooks["hooks"]["postToolUse"] == FOREIGN_HOOKS["hooks"]["postToolUse"]
    assert "mnemoseed-local-helper" in capsys.readouterr().err


def test_uninstall_removes_exactly_our_entries(cur_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_path = cur_home / "hooks.json"
    mcp_path = cur_home / "mcp.json"
    _seed(hooks_path, FOREIGN_HOOKS)
    _seed(mcp_path, FOREIGN_MCP)
    cur_install.install()
    _, existed = cur_install.uninstall()
    assert existed is True
    assert _read(hooks_path) == FOREIGN_HOOKS
    assert _read(mcp_path) == FOREIGN_MCP
    remaining = [
        handler
        for event in cur_install.HOOK_EVENTS
        for handler in _read(hooks_path)["hooks"].get(event, [])
        if cur_install.is_ours(handler)
    ]
    assert remaining == []
    assert "_hook-event --host cursor" not in hooks_path.read_text(encoding="utf-8")


def test_uninstall_on_a_fresh_install_leaves_the_schema_version(
    cur_home: Path, stable_command: tuple[str, str]
) -> None:
    """No prestate sidecar exists, so the schema version the installer wrote on
    the fresh file is the only remnant — our entries and containers all go."""
    cur_install.install()
    _, existed = cur_install.uninstall()
    assert existed is True
    assert _read(cur_home / "hooks.json") == {"version": 1}
    assert _read(cur_home / "mcp.json") == {}


def test_uninstall_without_install_reports_absent(cur_home: Path) -> None:
    _, existed = cur_install.uninstall()
    assert existed is False
    assert not (cur_home / "hooks.json").is_file()
    assert not (cur_home / "mcp.json").is_file()


def test_uninstall_cleans_an_mcp_only_remnant(cur_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_path = cur_home / "hooks.json"
    _seed(hooks_path, FOREIGN_HOOKS)
    cur_install.install()
    hooks = _read(hooks_path)
    del hooks["hooks"]
    hooks_path.write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
    _, existed = cur_install.uninstall()
    assert existed is True
    assert _read(hooks_path) == {"version": 1, "theme": "dark"}
    assert _read(cur_home / "mcp.json") == {}


def test_uninstall_leaves_a_foreign_shaped_mcp_entry_under_our_name(
    cur_home: Path, stable_command: tuple[str, str]
) -> None:
    _seed(cur_home / "hooks.json", FOREIGN_HOOKS)
    cur_install.install()
    mcp_path = cur_home / "mcp.json"
    mcp = _read(mcp_path)
    foreign_shape = {"command": "C:/tools/unrelated.exe", "args": ["serve"]}
    mcp["mcpServers"]["mnemoseed-local"] = foreign_shape
    mcp_path.write_text(json.dumps(mcp, indent=2) + "\n", encoding="utf-8")
    _, existed = cur_install.uninstall()
    assert existed is True
    assert _read(mcp_path)["mcpServers"]["mnemoseed-local"] == foreign_shape


def test_dry_run_previews_without_writing(cur_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_path = cur_home / "hooks.json"
    _seed(hooks_path, FOREIGN_HOOKS)
    _seed(cur_home / "mcp.json", FOREIGN_MCP)
    _, would_change = cur_install.install(dry_run=True)
    assert would_change is True
    assert _read(hooks_path) == FOREIGN_HOOKS
    assert _read(cur_home / "mcp.json") == FOREIGN_MCP
    _, existed = cur_install.uninstall()
    assert existed is False


def test_malformed_hooks_file_refuses_before_either_write(cur_home: Path) -> None:
    hooks_path = cur_home / "hooks.json"
    mcp_path = cur_home / "mcp.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    # uninstall refuses a non-strict file; a valid-JSON structural oddity with
    # nothing of ours to remove is an honest no-op (the zcode precedent)
    cases = [
        ('{\n  // user annotation\n  "theme": "dark"\n}\n', True),
        ("{broken", True),
        (json.dumps({"hooks": []}), False),
        (json.dumps({"hooks": {"stop": "garbage"}}), False),
    ]
    for original, uninstall_raises in cases:
        hooks_path.write_text(original, encoding="utf-8")
        _seed(mcp_path, FOREIGN_MCP)
        before_hooks = hooks_path.read_bytes()
        before_mcp = mcp_path.read_bytes()
        with pytest.raises(cur_install.ConfigParseError):
            cur_install.install()
        assert hooks_path.read_bytes() == before_hooks
        assert mcp_path.read_bytes() == before_mcp
        if uninstall_raises:
            with pytest.raises(cur_install.ConfigParseError):
                cur_install.uninstall()
        else:
            assert cur_install.uninstall() == (hooks_path, False)
        assert hooks_path.read_bytes() == before_hooks
        assert mcp_path.read_bytes() == before_mcp


def test_malformed_mcp_file_refuses_before_the_hooks_write(
    cur_home: Path, stable_command: tuple[str, str]
) -> None:
    hooks_path = cur_home / "hooks.json"
    mcp_path = cur_home / "mcp.json"
    _seed(hooks_path, FOREIGN_HOOKS)
    mcp_path.write_text("{broken mcp\n", encoding="utf-8")
    before_hooks = hooks_path.read_bytes()
    before_mcp = mcp_path.read_bytes()
    with pytest.raises(cur_install.ConfigParseError):
        cur_install.install()
    assert hooks_path.read_bytes() == before_hooks
    assert mcp_path.read_bytes() == before_mcp
    with pytest.raises(cur_install.ConfigParseError):
        cur_install.uninstall()
    assert hooks_path.read_bytes() == before_hooks


def test_spaced_executable_roundtrips_through_the_marker(
    cur_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spaced executable renders as one literal argument per the running
    platform; the argv tail — the marker — survives write and recognition."""
    resolved = ("C:/Program Files/Python313/python.exe", cur_install.MODULE_TRANSFORM_ARGS)
    monkeypatch.setattr(cur_install, "resolve_command", lambda: resolved)
    _, changed = cur_install.install()
    assert changed is True
    hooks = _read(cur_home / "hooks.json")
    command = _handlers_of(hooks, "stop")[0]["command"]
    tail = " ".join(resolved[1])
    if os.name == "nt":
        assert command == f"& '{resolved[0]}' {tail}"
    else:
        assert command == f"'{resolved[0]}' {tail}"
    assert cur_install.is_ours({"command": command})


def test_render_command_posix_branch_quotes_the_head_as_one_shell_word() -> None:
    head = "C:/Program Files/Python313/python.exe"
    tail = ("-m", "mnemoseed_local.cli", "_hook-event", "--host", "cursor")
    command = render_command(head, tail, windows=False)
    assert not command.startswith("&")
    assert command == f"'{head}' -m mnemoseed_local.cli _hook-event --host cursor"
    assert shlex.split(command) == [head, *tail]


def test_render_command_posix_branch_roundtrips_an_embedded_single_quote() -> None:
    head = "C:/Users/O'Brien/tools/py.exe"
    tail = ("_hook-event", "--host", "cursor")
    command = render_command(head, tail, windows=False)
    assert shlex.split(command) == [head, *tail]


def test_render_command_windows_branch_uses_the_call_operator_and_escapes_quotes() -> None:
    tail = ("_hook-event", "--host", "cursor")
    head = "C:/Program Files/Python313/python.exe"
    assert render_command(head, tail, windows=True) == f"& '{head}' _hook-event --host cursor"
    quoted = "C:/Users/O'Brien/tools/py.exe"
    escaped = render_command(quoted, tail, windows=True)
    assert escaped == "& 'C:/Users/O''Brien/tools/py.exe' _hook-event --host cursor"


def test_render_command_leaves_a_plain_head_bare_on_posix() -> None:
    head = "C:/tools/mnemoseed-local.exe"
    tail = ("_hook-event", "--host", "cursor")
    assert render_command(head, tail, windows=False) == f"{head} _hook-event --host cursor"


def test_registered_command_renders_portably_per_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor installer renders through the shared portable helper: a
    spaced executable stays one literal argument on the running platform, and
    the POSIX branch never emits the PowerShell call operator."""
    spaced = "C:/Program Files/Python313/python.exe"
    monkeypatch.setattr(cur_install, "resolve_command", lambda: (spaced, cur_install.MODULE_TRANSFORM_ARGS))
    tail = " ".join(cur_install.MODULE_TRANSFORM_ARGS)
    if os.name == "nt":
        assert cur_install.transform_command() == f"& '{spaced}' {tail}"
    monkeypatch.setattr(os, "name", "posix")
    posix_command = cur_install.transform_command()
    assert not posix_command.startswith("& ")
    assert posix_command == f"'{spaced}' {tail}"


def test_status_tracks_the_install_states(
    cur_home: Path, stable_command: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    assert cur_install.status().state == "not-installed"
    cur_install.install()
    assert cur_install.status().state == "installed"
    path = cur_home / "hooks.json"
    hooks = _read(path)
    del hooks["hooks"]["stop"]
    path.write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
    assert cur_install.status().state == "partial"
    cur_install.install()
    mcp_path = cur_home / "mcp.json"
    mcp = _read(mcp_path)
    del mcp["mcpServers"]["mnemoseed-local"]
    mcp_path.write_text(json.dumps(mcp, indent=2) + "\n", encoding="utf-8")
    assert cur_install.status().state == "partial"
    cur_install.install()
    mcp = _read(mcp_path)
    mcp["mcpServers"]["mnemoseed-local"] = {"url": "http://127.0.0.1:9/mcp"}
    mcp_path.write_text(json.dumps(mcp, indent=2) + "\n", encoding="utf-8")
    assert cur_install.status().state == "partial"
    path.write_text("{broken\n", encoding="utf-8")
    assert cur_install.status().state == "differs"


def test_status_reports_the_config_path_and_daemon_probe(
    cur_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_probe(base_url: str, timeout: float = 2.0) -> bool:
        seen["base_url"] = base_url
        return True

    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", fake_probe)
    info = cur_install.status()
    assert info.daemon_reachable is True
    assert info.path == cur_home / "hooks.json"
    assert info.root == cur_home
    assert seen["base_url"] == "http://localhost:7788"


def test_utf8_foreign_content_survives_the_roundtrip(cur_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_path = cur_home / "hooks.json"
    foreign = {
        "version": 1,
        "hooks": {"stop": [{"command": "我的钩子 --标记", "timeout": 3}]},
        "备注": "用户注释",
    }
    _seed(hooks_path, foreign)
    _seed(cur_home / "mcp.json", FOREIGN_MCP)
    cur_install.install()
    cur_install.uninstall()
    assert _read(hooks_path) == foreign
    assert _read(cur_home / "mcp.json") == FOREIGN_MCP


def test_host_cli_lifecycle_wiring(
    cur_home: Path, stable_command: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["host", "install", "--host", "cursor"]) == 0
    assert "applied" in capsys.readouterr().out
    assert (cur_home / "hooks.json").is_file()
    assert (cur_home / "mcp.json").is_file()

    assert main(["host", "status", "--host", "cursor"]) == 0
    out = capsys.readouterr().out
    assert "installed" in out

    assert main(["host", "uninstall", "--host", "cursor"]) == 0
    assert "applied" in capsys.readouterr().out


# ---------------------------------------------------------------- dispatch derivation (the one-line registry)


def test_cursor_registers_once_in_the_shared_adapter_registry() -> None:
    assert shared.ADAPTER_MODULES["cursor"] == "mnemoseed_local.hosts.cursor.install"
    binding = HOOK_HOSTS["cursor"]
    assert binding.events == "mnemoseed_local.hosts.cursor.events"
    assert binding.install == "mnemoseed_local.hosts.cursor.install"
    assert binding.events_entry == "transform_event"
    assert callable(events.transform_event)


def test_hook_events_cover_exactly_the_frozen_subscription() -> None:
    assert cur_install.HOOK_EVENTS == (
        "sessionStart",
        "beforeSubmitPrompt",
        "postToolUse",
        "afterAgentResponse",
        "stop",
        "sessionEnd",
    )
    assert set(events._NORMALIZERS) == set(cur_install.HOOK_EVENTS)


# ---------------------------------------------------------------- real-daemon E2E (spare port, isolated home)


def _config_toml(home: Path) -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[capture]\nauto_recall = true\n"
    )


# test_registry.py clears the driver registries wholesale; any daemon-booting
# module ordered after it must defensively re-register (test_preset_embedded
# precedent).
_DRIVERS = (
    (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
    (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
    (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
    (EMBED_DRIVERS, bge_m3_onnx.BgeM3OnnxEmbedder),
    (EMBED_DRIVERS, synthetic_embedder.SyntheticEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_registered() -> Iterator[None]:
    for registry, cls in _DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[tuple[str, str, Path]]:
    """The real daemon: isolated config home, spare port, live uvicorn server."""
    home = tmp_path / "daemon-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(_config_toml(home), encoding="utf-8")
    with auth_support.isolated_home(home):
        credential = auth_support.seed_credential(home)
        with auth_support.live_server(create_app(), unused_loopback_port()) as base_url:
            yield base_url, credential, home


class _Recorder:
    """Loopback POST recorder standing in for the daemon in subprocess tests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.requests.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"


def _subprocess_env(base_url: str, daemon_home: Path, profile: str | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNEMOSEED_LOCAL_")}
    env["MNEMOSEED_LOCAL_BASEURL"] = base_url
    env["MNEMOSEED_LOCAL_HOME"] = str(daemon_home)
    if profile is not None:
        env["MNEMOSEED_LOCAL_PROFILE_ID"] = profile
    return env


def _fire(
    payload: dict[str, Any], base_url: str, daemon_home: Path, profile: str | None = None
) -> subprocess.CompletedProcess[bytes]:
    """One golden stdin event through the real registered transformer process."""
    return subprocess.run(
        [sys.executable, "-m", "mnemoseed_local.cli", "_hook-event", "--host", "cursor"],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        timeout=120,
        env=_subprocess_env(base_url, daemon_home, profile),
        shell=False,
    )


def _fire_text(payload: dict[str, Any], base_url: str, daemon_home: Path) -> str:
    """One event through the real process; returns the raw stdout with the
    child's platform newline normalized away."""
    result = _fire(payload, base_url, daemon_home)
    return result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def test_cursor_payloads_reach_a_real_daemon_through_the_registered_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subprocess-level oracle: the registered transformer verb posts the real
    capture payloads to a live loopback recorder — host attribution, the
    conversation_id identity, the exact allow on stdout, and zero daemon
    writes with a visible refusal on a binding mismatch."""
    daemon_home = tmp_path / "daemon-home"
    daemon_home.mkdir()
    (daemon_home / "config.toml").write_text(_config_toml(daemon_home), encoding="utf-8")
    root = _mkdir(tmp_path / "proj")
    pb.bind(daemon_home, root, "alpha")

    recorder = _Recorder()
    try:
        base_url = recorder.url
        payload = dict(_fixture("before_submit_prompt"), workspace_roots=[str(root)])
        done = _fire(payload, base_url, daemon_home, profile="alpha")
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
        # the child's text layer writes the platform newline; the payload line
        # itself is the oracle (the in-process tests pin the exact bytes)
        assert done.stdout.decode("utf-8", "replace").strip() == SUBMIT_ALLOW.strip()
        posted = [json.loads(body) for path, body in recorder.requests if path == "/ingest"]
        assert len(posted) == 1
        assert posted[0]["host"] == "cursor"
        assert posted[0]["session_id"] == SESSION
        assert posted[0]["profile_id"] == "alpha"
        assert posted[0]["content"]["text"] == _fixture("before_submit_prompt")["prompt"]

        # a refused multiroot firing: visible refusal on stderr, no daemon write
        recorder.requests.clear()
        refused = dict(payload, workspace_roots=[str(root), str(_mkdir(tmp_path / "other"))])
        refused_run = _fire(refused, base_url, daemon_home, profile="alpha")
        assert refused_run.returncode == 0
        assert recorder.requests == []
        assert "event dropped" in refused_run.stderr.decode("utf-8", "replace")
    finally:
        recorder.close()


def test_cursor_host_chain_end_to_end_on_a_real_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    daemon: tuple[str, str, Path],
) -> None:
    """Full chain on a real embedded daemon: install (fake ~/.cursor) ->
    capture -> flush durability -> settle -> T1 replay -> uninstall restoring
    the foreign configs exactly. Recall on Cursor is T1 + MCP only: the prompt
    lane prints the allow and never a recall block."""
    base_url, credential, daemon_home = daemon
    client = DaemonClient(base_url=base_url, token=credential)
    host_home = tmp_path / "host-home"
    monkeypatch.setattr(Path, "home", lambda: host_home)
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", base_url)
    hooks_path = host_home / ".cursor" / "hooks.json"
    mcp_path = host_home / ".cursor" / "mcp.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    _seed(hooks_path, FOREIGN_HOOKS)
    _seed(mcp_path, FOREIGN_MCP)

    assert main(["host", "install", "--host", "cursor"]) == 0
    capsys.readouterr()
    installed = _read(hooks_path)
    assert set(installed["hooks"]) == set(cur_install.HOOK_EVENTS)
    assert _read(mcp_path)["mcpServers"]["mnemoseed-local"] == cur_install.mcp_server_entry()
    assert main(["host", "status", "--host", "cursor"]) == 0
    status_out = capsys.readouterr().out
    assert "host: installed" in status_out
    assert "reachable" in status_out

    def fire(payload: dict[str, Any]) -> str:
        """One event through the real process; returns the raw stdout."""
        return _fire_text(payload, base_url, daemon_home)

    # session alpha: prompt -> tool -> assistant -> stop flush
    assert fire(_fixture("before_submit_prompt")) == SUBMIT_ALLOW
    for name in ("post_tool_use", "after_agent_response", "stop"):
        assert fire(_fixture(name)) == ""

    chunks = _export(client)["chunks"]
    assert chunks, "the stop flush must persist the buffered turn"
    assert all(chunk["provenance"]["source"] == "cursor-chat" for chunk in chunks)
    assert all(chunk["provenance"]["session_id"] == SESSION for chunk in chunks)
    joined = "\n".join(chunk["text"] for chunk in chunks)
    assert f"user: {_fixture('before_submit_prompt')['prompt']}" in joined
    assert f"assistant: {_fixture('after_agent_response')['text']}" in joined

    # session beta: T1 replay on the flat envelope; the prompt lane stays allow-only
    beta = "cur-conv-02"
    start = fire(_replace_session(_fixture("session_start"), beta))
    assert start, "a served T1 replay must reach stdout"
    replay = json.loads(start)
    assert set(replay) == {"additional_context"}
    t1 = replay["additional_context"]
    assert t1.startswith(RECALL_FENCE_OPEN) and t1.endswith(RECALL_FENCE_CLOSE)
    assert t1.splitlines()[1] == T1_SIGNAL
    assert _fixture("after_agent_response")["text"] in t1
    assert len(t1) <= 4000

    assert fire(_replace_session(_fixture("before_submit_prompt"), beta)) == SUBMIT_ALLOW
    for name in ("post_tool_use", "after_agent_response", "stop", "session_end"):
        assert fire(_replace_session(_fixture(name), beta)) == ""

    chunks_after = _export(client)["chunks"]
    assert all(chunk["provenance"]["session_id"] in (SESSION, beta) for chunk in chunks_after)
    beta_text = "\n".join(
        chunk["text"] for chunk in chunks_after if chunk["provenance"]["session_id"] == beta
    )
    assert f"user: {BETA_PROMPT}" in beta_text, "the settled beta session persisted"
    assert f"assistant: {BETA_REPLY}" in beta_text

    # uninstall restores the foreign configs exactly, idempotently
    assert main(["host", "uninstall", "--host", "cursor"]) == 0
    assert "applied" in capsys.readouterr().out
    assert _read(hooks_path) == FOREIGN_HOOKS
    assert _read(mcp_path) == FOREIGN_MCP
    assert "_hook-event --host cursor" not in hooks_path.read_text(encoding="utf-8")
    assert main(["host", "uninstall", "--host", "cursor"]) == 0
    assert "no change" in capsys.readouterr().out
    assert _read(hooks_path) == FOREIGN_HOOKS


BETA_PROMPT = "WatermarkReplay 的水位幂等还有哪些边界没测？"
BETA_REPLY = "水位幂等由 daemon 水位去重保证，复放不会重复入库。"
BETA_TOOL_OUTPUT = "12 passed in 0.42s"


def _replace_session(fixture: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Session beta's golden payload: its own identity and its own texts, so
    the daemon's near-duplicate guard never confounds it with session alpha."""
    payload = dict(fixture)
    payload["conversation_id"] = session_id
    if "session_id" in payload:
        payload["session_id"] = session_id
    payload["transcript_path"] = payload["transcript_path"].replace(SESSION, session_id)
    if payload.get("hook_event_name") == "beforeSubmitPrompt":
        payload["prompt"] = BETA_PROMPT
    elif payload.get("hook_event_name") == "afterAgentResponse":
        payload["text"] = BETA_REPLY
    elif payload.get("hook_event_name") == "postToolUse":
        payload["tool_output"] = BETA_TOOL_OUTPUT
    return payload


def _export(client: DaemonClient) -> dict[str, Any]:
    reply = client.post("/memory/export", {"profile_id": "default"})
    assert isinstance(reply, dict) and "chunks" in reply, reply
    return reply
