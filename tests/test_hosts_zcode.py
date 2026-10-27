"""#272 S2a: ZCode host hook adapter (PRD-272 §3.2/§5).

Golden fixtures under ``tests/fixtures/zcode_hook/`` pin the payload shapes
the ``_hook-event`` transformer consumes (the Claude-Code-compatible dialect
working contract from PRD-272 §9.2). Transformer tests pin: zcode capture
attribution on /ingest, the T1 session-start replay lane (POST
/session/recent -> fenced additionalContext stdout on SessionStart), the T2
mid-session recall lane (acked user ingest -> recall-pending pull ->
additionalContext), the #273 visibility vocabulary, and the zero-stdout red
line on every other path. Lifecycle tests pin the marked-entry surgery on
``~/.zcode/cli/config.json`` (``type: process`` entries under
``hooks.events.<Event>``, the ``hooks.enabled`` pre-state record, the
``mcp.servers`` entry) with foreign entries preserved byte-for-byte in value.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mnemoseed_local.cli import main
from mnemoseed_local.hosts.claude_code import events as cc_events
from mnemoseed_local.hosts.zcode import events
from mnemoseed_local.hosts.zcode import install as zc_install
from mnemoseed_local.rest_client import (
    DaemonClient,
    DaemonRestError,
    DaemonUnavailableError,
)
from mnemoseed_local.schema.turn import HostId, IngestEvent, IngestEventType

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zcode_hook"

ENV_VARS = (
    "MNEMOSEED_LOCAL_BASEURL",
    "MNEMOSEED_LOCAL_PROFILE_ID",
    "MNEMOSEED_LOCAL_DEBUG",
    "MNEMOSEED_LOCAL_DATA_DIR",
    "MNEMOSEED_LOCAL_HOME",
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def zc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic ZCode config root: <tmp home>/.zcode/cli."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path / ".zcode"


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
    return main(["_hook-event", "--host", "zcode"])


def _pull_payload(**overrides: object) -> dict:
    payload = {
        "enabled": True,
        "items": [{"kind": "chunk", "id": "c1", "text": "中段关键线索"}],
        "non_focal_above_floor": 0,
        "budget_chars": events.RECALL_PULL_MAX_CHARS,
        "slot_consumed": False,
    }
    payload.update(overrides)
    return payload


def _capture_pull(monkeypatch: pytest.MonkeyPatch, payload: dict) -> list[tuple[str, dict]]:
    """Route DaemonClient.post so /session/recall-pending answers `payload`."""
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        if path == "/session/recall-pending":
            return payload
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    return calls


def _recent_payload(**overrides: object) -> dict:
    payload = {
        "profile_id": "default",
        "sessions": [
            {
                "session_id": "zc-earlier-session-77",
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


# ---------------------------------------------------------------- golden fixtures -> wire (§5.1)


@pytest.mark.parametrize(
    "name,event_type",
    [
        ("user_prompt", IngestEventType.USER_PROMPT),
        ("tool_use", IngestEventType.TOOL_USE),
        ("stop", IngestEventType.ASSISTANT_MESSAGE),
    ],
)
def test_capture_fixtures_ingest_with_zcode_attribution(name: str, event_type: IngestEventType) -> None:
    fixture = _fixture(name)
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert isinstance(body, IngestEvent)
    assert body.host is HostId.ZCODE
    assert body.event is event_type
    assert body.session_id == fixture["session_id"]
    assert body.profile_id == "default"
    assert body.raw == fixture


def test_user_prompt_fixture_carries_the_verbatim_prompt() -> None:
    fixture = _fixture("user_prompt")
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.text == fixture["prompt"]


def test_tool_use_fixture_carries_the_tool_content() -> None:
    fixture = _fixture("tool_use")
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.tool_name == "Bash"
    assert body.content.input == {"command": "uv run pytest -q"}
    assert body.content.output == "938 passed, 5 skipped"


def test_stop_fixture_carries_the_assistant_text() -> None:
    fixture = _fixture("stop")
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.text == fixture["last_assistant_message"]


def test_normalized_wire_bodies_validate_against_the_daemon_schema() -> None:
    for name in ("user_prompt", "tool_use", "stop"):
        action = events.normalize_event(_fixture(name), now=1.0)
        assert action is not None
        kind, body = action
        assert kind == "ingest"
        assert isinstance(body, IngestEvent)
        IngestEvent.model_validate(body.model_dump(mode="json"))


def test_session_start_fixture_maps_to_the_t1_read_lane() -> None:
    """SessionStart never ingests: it is the T1 read lane keyed by session."""
    fixture = _fixture("session_start")
    action = events.normalize_event(fixture, now=1.0)
    assert action is not None
    kind, body = action
    assert kind != "ingest"
    assert kind == "t1"
    assert body == fixture["session_id"]


def test_zcode_signal_vocabulary_matches_the_claude_code_bytes() -> None:
    """The #273 signal word list is one fixed vocabulary across hosts."""
    assert events.T2_SIGNAL == cc_events.T2_SIGNAL
    assert events.CAPTURE_SIGNAL == cc_events.CAPTURE_SIGNAL
    assert events.T2_SIGNAL == "[mnemoseed] recalled related memories"
    assert events.CAPTURE_SIGNAL == "[mnemoseed] captured this session"


def test_events_module_reexports_the_shared_core() -> None:
    for name in (
        "POST_TIMEOUT_SECONDS",
        "RECALL_BUDGET_KEY",
        "RECALL_DISCLAIMER",
        "RECALL_FENCE_CLOSE",
        "RECALL_FENCE_OPEN",
        "RECALL_FENCE_SANITIZED",
        "RECALL_PULL_MAX_CHARS",
        "RECALL_PULL_TIMEOUT_SECONDS",
        "build_t2_context",
        "capture_count_line",
        "profile_id",
        "recall_count_line",
        "sanitize_recall_text",
        "visibility_mode",
    ):
        assert hasattr(events, name), name


# ---------------------------------------------------------------- T1 session-start replay (§5.3)


def test_session_start_pulls_recent_once_with_the_legacy_identity_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The T1 pull posts /session/recent exactly once (attempt-once per
    firing) with exclude_session_id and NO self_session_id / resume_query —
    the daemon's automatic-path guard must never be tripped."""
    calls = _capture_recent(monkeypatch, _recent_payload())
    assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
    assert [path for path, _ in calls] == ["/session/recent"]
    body = calls[0][1]
    assert set(body) == {"profile_id", "sessions", "per_session", "exclude_session_id"}
    assert body["profile_id"] == "default"
    assert body["sessions"] == 2
    assert body["per_session"] == 8
    assert body["exclude_session_id"] == "zc-sess-01"


def test_session_start_injects_the_fenced_replay_under_compact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_recent(monkeypatch, _recent_payload())
    assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    block = out["hookSpecificOutput"]["additionalContext"]
    assert block.startswith(events.RECALL_FENCE_OPEN)
    assert block.endswith(events.RECALL_FENCE_CLOSE)
    assert block.count("<mnemoseed-memory-recall>") == 1
    lines = block.split("\n")
    assert lines[1] == events.T1_SIGNAL, "the compact T1 signal rides after the fence-open line"
    assert lines[2].startswith("The block below is an automatic memory replay")
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


def test_session_start_verbose_adds_the_exact_count_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_recent(monkeypatch, _recent_payload(visibility={"mode": "verbose"}))
    assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
    out = json.loads(capsys.readouterr().out)
    block = out["hookSpecificOutput"]["additionalContext"]
    assert "[mnemoseed] 1 sessions, 2 memories, 10 chars" in block


def test_session_start_off_or_absent_visibility_drops_the_signal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for visibility in ({"mode": "off"}, None):
        payload: dict = _recent_payload()
        if visibility is None:
            payload.pop("visibility", None)
        else:
            payload["visibility"] = visibility
        _capture_recent(monkeypatch, payload)
        assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
        out = json.loads(capsys.readouterr().out)
        block = out["hookSpecificOutput"]["additionalContext"]
        assert "[mnemoseed]" not in block, visibility


def test_session_start_pull_failure_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonRestError(503, "daemon down")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    assert _run_transformer(monkeypatch, _fixture("session_start")) == 0
    assert capsys.readouterr().out == ""


def test_t1_signal_sheds_first_under_budget_pressure() -> None:
    """The compact signal is purely additive: when it does not fit, the shed
    result is byte-identical to the no-signal build."""
    sessions = [{"session_id": "s-1", "latest_at": 1.0, "chunks": [{"chunk_id": "c1", "text": "z" * 300}]}]
    base = events.build_t1_context(sessions, 4000)
    assert base is not None
    with_signal = events.build_t1_context(sessions, len(base), signal=events.T1_SIGNAL)
    assert with_signal == base, "the shed signal must restore the bare block"
    assert events.T1_SIGNAL not in with_signal


def test_t1_below_the_slice_floor_drops_whole_fail_closed() -> None:
    sessions = [{"session_id": "s-1", "latest_at": 1.0, "chunks": [{"chunk_id": "c1", "text": "z" * 500}]}]
    assert events.build_t1_context(sessions, 200) is None


def test_t1_boundary_chunk_keeps_its_tail_slice() -> None:
    chunks = [
        {"chunk_id": "c1", "text": "b" * 3900},
        {"chunk_id": "c2", "text": "a" * 100},
    ]
    sessions = [{"session_id": "s-1", "latest_at": 1.0, "chunks": chunks}]
    block = events.build_t1_context(sessions, 4000)
    assert block is not None
    assert "\naaa" in block, "the newer chunk renders whole"
    assert "\n…bbb" in block, "the boundary chunk keeps its newest tail slice"
    assert len(block) <= 4000


def test_t1_block_never_exceeds_the_budget() -> None:
    chunks = [{"chunk_id": f"c{i}", "text": "字" * 300} for i in range(30)]
    sessions = [{"session_id": f"zc-old-{i}", "latest_at": float(i), "chunks": chunks} for i in range(3)]
    block = events.build_t1_context(sessions, 4000)
    assert block is not None
    assert len(block) <= 4000


def test_t1_inner_fence_literals_are_sanitized() -> None:
    """TA-5: served text carrying the fence markers must not break the block."""
    sessions = [
        {
            "session_id": "s-1",
            "latest_at": 1.0,
            "chunks": [{"chunk_id": "c1", "text": "see <mnemoseed-memory-recall> inside"}],
        }
    ]
    block = events.build_t1_context(sessions, 4000)
    assert block is not None
    assert block.count("<mnemoseed-memory-recall>") == 1
    assert block.count("</mnemoseed-memory-recall>") == 1
    assert "‹mnemoseed-memory-recall›" in block


# ---------------------------------------------------------------- T2 mid-session recall (§5.3)


def test_user_prompt_injects_t2_after_the_ack(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _capture_pull(monkeypatch, _pull_payload(visibility={"mode": "compact", "capture": False}))
    assert _run_transformer(monkeypatch, _fixture("user_prompt")) == 0
    assert [path for path, _ in calls] == ["/ingest", "/session/recall-pending"]
    pull_body = calls[1][1]
    assert pull_body == {
        "profile_id": "default",
        "session_id": "zc-sess-01",
        "seen_chunk_ids": [],
    }
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    block = out["hookSpecificOutput"]["additionalContext"]
    assert "中段关键线索" in block
    assert block.count("<mnemoseed-memory-recall>") == 1
    assert events.T2_SIGNAL in block


def test_user_prompt_off_or_absent_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """mode=off or an absent wire field: zero injection, zero stdout — the
    byte-level no-op holds even with a pending capture signal."""
    for visibility in ({"mode": "off", "capture": True}, None):
        payload: dict = _pull_payload(enabled=False, items=[])
        if visibility is not None:
            payload["visibility"] = visibility
        _capture_pull(monkeypatch, payload)
        assert _run_transformer(monkeypatch, _fixture("user_prompt")) == 0
        assert capsys.readouterr().out == "", visibility


def test_capture_only_signal_renders_under_compact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """enabled=false + a pending capture signal is a stdout payload (mode !=
    off) — the claude_code stdout red line's revision."""
    _capture_pull(
        monkeypatch, _pull_payload(enabled=False, items=[], visibility={"mode": "compact", "capture": True})
    )
    assert _run_transformer(monkeypatch, _fixture("user_prompt")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["additionalContext"] == events.CAPTURE_SIGNAL


def test_ingest_rejection_never_pulls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        if path == "/ingest":
            raise DaemonRestError(409, "already settled")
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    assert _run_transformer(monkeypatch, _fixture("user_prompt")) == 0
    assert [path for path, _ in calls] == ["/ingest"]
    assert capsys.readouterr().out == ""


def test_t2_budget_follows_the_wire_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_pull(monkeypatch, _pull_payload(budget_chars=200))
    assert _run_transformer(monkeypatch, _fixture("user_prompt")) == 0
    out = json.loads(capsys.readouterr().out)
    block = out["hookSpecificOutput"]["additionalContext"]
    wrapper = (
        len(events.RECALL_FENCE_OPEN) + 1 + len(events.RECALL_DISCLAIMER) + 1 + len(events.RECALL_FENCE_CLOSE)
    )
    assert len(block) <= 200 + wrapper


def test_stop_and_tool_lanes_stay_capture_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _capture_pull(monkeypatch, _pull_payload())
    for name in ("stop", "tool_use"):
        assert _run_transformer(monkeypatch, _fixture(name)) == 0
        assert capsys.readouterr().out == ""
    assert [path for path, _ in calls] == ["/ingest", "/ingest"]


# ---------------------------------------------------------------- stdin defense (§5.4)


def test_malformed_stdin_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_transformer(monkeypatch, "{not json") == 0
    assert capsys.readouterr().out == ""
    assert captured_posts == []


def test_non_object_stdin_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_transformer(monkeypatch, "[]") == 0
    assert capsys.readouterr().out == ""
    assert captured_posts == []


def test_unknown_event_and_identityless_payloads_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run_transformer(monkeypatch, {"session_id": "s1", "hook_event_name": "Notification", "text": "x"})
        == 0
    )
    assert _run_transformer(monkeypatch, {"hook_event_name": "UserPromptSubmit", "prompt": "x"}) == 0
    assert _run_transformer(monkeypatch, {"session_id": "  ", "hook_event_name": "Stop"}) == 0
    assert capsys.readouterr().out == ""
    assert captured_posts == []


def test_blank_prompt_is_dropped_silently(
    monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    assert (
        _run_transformer(
            monkeypatch, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "   "}
        )
        == 0
    )
    assert captured_posts == []


def test_all_lanes_fail_open_when_the_daemon_is_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonUnavailableError("connection refused")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    for name in ("session_start", "user_prompt", "tool_use", "stop"):
        assert _run_transformer(monkeypatch, _fixture(name)) == 0, name
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- Stop / PostToolUse normalization


def _stop_payload(**overrides: object) -> dict:
    payload = {
        "session_id": "zc-sess-01",
        "cwd": "G:/dev/demo",
        "hook_event_name": "Stop",
        "transcript_path": "",
        "stop_hook_active": False,
        "last_assistant_message": "失败原因是测试里的时钟断言没有冻结。",
    }
    payload.update(overrides)
    return payload


def test_stop_takes_the_model_from_a_cc_dialect_transcript_tail(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"user","message":{"role":"user"}}',
                '{"type":"assistant","message":{"model":"glm-4.7","content":[]}}',
                "not-json-garbage",
            ]
        ),
        encoding="utf-8",
    )
    action = events.normalize_event(_stop_payload(transcript_path=str(transcript)), now=2.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.model_id == "glm-4.7"
    assert body.content.text == _stop_payload()["last_assistant_message"]


def test_stop_without_payload_text_falls_back_to_the_transcript_tail(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"model":"glm-4.7",'
        '"content":[{"type":"text","text":"来自转录尾部的回复"}]}}\n',
        encoding="utf-8",
    )
    action = events.normalize_event(
        _stop_payload(transcript_path=str(transcript), last_assistant_message=None), now=2.5
    )
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.model_id == "glm-4.7"
    assert body.content.text == "来自转录尾部的回复"


def test_stop_transcript_failures_degrade_to_a_model_less_event(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("totally not json\n{\n", encoding="utf-8")
    for transcript_path in (str(garbage), str(tmp_path / "missing.jsonl"), None, 42):
        action = events.normalize_event(_stop_payload(transcript_path=transcript_path), now=3.0)
        assert action is not None, repr(transcript_path)
        _, body = action
        assert isinstance(body, IngestEvent)
        assert body.content.model_id is None
        assert body.content.text == _stop_payload()["last_assistant_message"]


def test_stop_never_settles_the_session() -> None:
    action = events.normalize_event(_stop_payload(), now=7.0)
    assert action is not None
    kind, _ = action
    assert kind == "ingest"
    assert events.ENDPOINTS[kind] == "/ingest"


def test_post_tool_use_caps_the_output_and_flattens_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events, "MAX_TOOL_OUTPUT_CHARS", 20)
    payload = {
        "session_id": "zc-sess-01",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "uv run pytest -q"},
        "tool_response": {"stdout": "938 passed, 5 skipped", "success": True},
    }
    action = events.normalize_event(payload, now=4.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.output.endswith(events.TOOL_TRUNCATION_MARKER)
    assert events.MAX_TOOL_OUTPUT_CHARS >= len(body.content.output) - len(events.TOOL_TRUNCATION_MARKER)


def test_post_tool_use_prefers_stderr_when_stdout_absent() -> None:
    payload = {
        "session_id": "zc-sess-01",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {},
        "tool_response": {"stderr": "warning: deprecated flag"},
    }
    action = events.normalize_event(payload, now=4.5)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.output == "warning: deprecated flag"
    assert body.content.input == {}


def test_post_tool_use_without_a_tool_name_is_dropped() -> None:
    payload = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_response": {}}
    assert events.normalize_event(payload, now=4.6) is None


# ---------------------------------------------------------------- installer lifecycle (§5.5)


FOREIGN_CONFIG = {
    "theme": "dark",
    "hooks": {
        "enabled": False,
        "timeoutMs": 30000,
        "events": {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "process", "command": "C:/tools/foreign.exe", "args": ["log"]}],
                },
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": "my-own-hook --flag"}]},
            ],
        },
    },
    "mcp": {
        "servers": {
            "本地服务器": {"type": "stdio", "command": "npx", "args": ["-y", "some/server"]},
        }
    },
}


def _seed_config(path: Path, payload: dict | None = None) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload if payload is not None else FOREIGN_CONFIG, indent=2) + "\n"
    path.write_text(body, encoding="utf-8")
    return payload if payload is not None else FOREIGN_CONFIG


def _handlers_of(config: dict, event: str) -> list[dict]:
    found = []
    for group in config.get("hooks", {}).get("events", {}).get(event, []):
        for handler in group.get("hooks", []):
            if isinstance(handler, dict) and zc_install.is_ours(handler):
                found.append(handler)
    return found


@pytest.fixture
def stable_command(monkeypatch: pytest.MonkeyPatch) -> tuple[str, tuple[str, ...]]:
    resolved = (str(Path(__file__).resolve()), zc_install.TRANSFORM_ARGS)
    monkeypatch.setattr(zc_install, "transform_command", lambda: resolved)
    return resolved


def test_install_writes_the_canonical_hooks_and_mcp_config(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    path, changed = zc_install.install()
    assert changed is True
    assert path == zc_home / "cli" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["hooks"]["enabled"] is True
    assert set(config["hooks"]["events"]) == set(zc_install.HOOK_EVENTS)
    command, args = stable_command
    for event in zc_install.HOOK_EVENTS:
        handlers = _handlers_of(config, event)
        assert len(handlers) == 1, event
        handler = handlers[0]
        assert handler["type"] == "process"
        assert handler["command"] == command
        assert handler["args"] == list(args)
        assert handler["timeoutMs"] == zc_install.HANDLER_TIMEOUT_MS
    assert config["mcp"]["servers"]["mnemoseed-local"] == {
        "type": "stdio",
        "command": command,
        "args": ["mcp"],
    }
    assert Path(handler["command"]).is_absolute()


def test_install_records_the_enabled_pre_state_sidecar(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    path = zc_home / "cli" / "config.json"
    _seed_config(path, {"hooks": {"enabled": False}})
    zc_install.install()
    sidecar = json.loads((zc_home / "cli" / "mnemoseed-local.json").read_text(encoding="utf-8"))
    assert sidecar[zc_install.PRESTATE_KEY] is False


def test_repair_install_keeps_the_first_recorded_prestate(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    """First record wins: a repair install (e.g. over the legacy HTTP entry)
    must not clobber the pre-state with the already-flipped switch."""
    path = zc_home / "cli" / "config.json"
    sidecar = path.parent / zc_install.PRESTATE_FILENAME
    legacy = {
        "hooks": {
            "enabled": True,
            "events": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "process",
                                "command": "C:/old/mnemoseed-local.exe",
                                "args": list(zc_install.TRANSFORM_ARGS),
                            }
                        ]
                    }
                ]
            },
        },
        "mcp": {"servers": {"mnemoseed-local": {"type": "http", "url": "http://localhost:7788/mcp"}}},
    }
    _seed_config(path, legacy)
    sidecar.write_text(json.dumps({zc_install.PRESTATE_KEY: False}), encoding="utf-8")
    zc_install.install()
    assert json.loads(sidecar.read_text(encoding="utf-8"))[zc_install.PRESTATE_KEY] is False
    _, existed = zc_install.uninstall()
    assert existed is True
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["hooks"]["enabled"] is False


def test_install_is_idempotent_byte_identical(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    path = zc_home / "cli" / "config.json"
    _seed_config(path)
    zc_install.install()
    first = path.read_bytes()
    _, changed = zc_install.install()
    assert changed is False
    assert path.read_bytes() == first


def test_install_never_mutates_foreign_entries(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    path = zc_home / "cli" / "config.json"
    _seed_config(path)
    zc_install.install()
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["theme"] == FOREIGN_CONFIG["theme"]
    assert config["hooks"]["timeoutMs"] == FOREIGN_CONFIG["hooks"]["timeoutMs"]
    assert config["hooks"]["events"]["PostToolUse"][0] == FOREIGN_CONFIG["hooks"]["events"]["PostToolUse"][0]
    assert config["hooks"]["events"]["Stop"][0] == FOREIGN_CONFIG["hooks"]["events"]["Stop"][0]
    assert config["mcp"]["servers"]["本地服务器"] == FOREIGN_CONFIG["mcp"]["servers"]["本地服务器"]


def test_uninstall_removes_exactly_our_entries(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    path = zc_home / "cli" / "config.json"
    _seed_config(path)
    original_foreign = json.loads(json.dumps(FOREIGN_CONFIG))
    zc_install.install()
    _, existed = zc_install.uninstall()
    assert existed is True
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["theme"] == original_foreign["theme"]
    assert config["hooks"]["timeoutMs"] == original_foreign["hooks"]["timeoutMs"]
    assert config["hooks"]["events"]["PostToolUse"] == original_foreign["hooks"]["events"]["PostToolUse"]
    assert config["hooks"]["events"]["Stop"] == original_foreign["hooks"]["events"]["Stop"]
    assert config["mcp"]["servers"]["本地服务器"] == original_foreign["mcp"]["servers"]["本地服务器"]
    assert "mnemoseed" not in path.read_text(encoding="utf-8")
    assert not (zc_home / "cli" / "mnemoseed-local.json").is_file()


def test_uninstall_on_a_fresh_install_restores_an_empty_config(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    zc_install.install()
    _, existed = zc_install.uninstall()
    assert existed is True
    path = zc_home / "cli" / "config.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize(
    ("pre_state", "expected_after"),
    [
        (False, False),
        (None, "absent"),
        (True, True),
    ],
)
def test_uninstall_restores_the_enabled_pre_state(
    zc_home: Path,
    stable_command: tuple[str, tuple[str, ...]],
    pre_state: bool | None,
    expected_after: bool | str,
) -> None:
    path = zc_home / "cli" / "config.json"
    hooks: dict = {"events": {}}
    if pre_state is not None:
        hooks["enabled"] = pre_state
    _seed_config(path, {"hooks": hooks})
    zc_install.install()
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["hooks"]["enabled"] is True, "install must flip the switch on explicitly"
    zc_install.uninstall()
    config = json.loads(path.read_text(encoding="utf-8"))
    if expected_after == "absent":
        assert "enabled" not in config.get("hooks", {})
    else:
        assert config["hooks"]["enabled"] is expected_after


def test_uninstall_without_a_sidecar_leaves_enabled_untouched(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    """A lost pre-state record is honest unknown: our entries go, the switch
    the user may have flipped deliberately stays."""
    path = zc_home / "cli" / "config.json"
    _seed_config(path)
    zc_install.install()
    (zc_home / "cli" / "mnemoseed-local.json").unlink()
    _, existed = zc_install.uninstall()
    assert existed is True
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["hooks"]["enabled"] is True


def test_uninstall_without_install_reports_absent(zc_home: Path) -> None:
    _, existed = zc_install.uninstall()
    assert existed is False


def test_uninstall_leaves_a_foreign_shaped_mcp_entry_under_our_name(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    """Structure recognition, not the server key alone: an entry we did not
    write, even under our own key, is user data and stays."""
    path = zc_home / "cli" / "config.json"
    _seed_config(path)
    zc_install.install()
    config = json.loads(path.read_text(encoding="utf-8"))
    foreign_shape = {"type": "stdio", "command": "C:/tools/unrelated.exe", "args": ["serve"]}
    config["mcp"]["servers"]["mnemoseed-local"] = foreign_shape
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    _, existed = zc_install.uninstall()
    assert existed is True
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["mcp"]["servers"]["mnemoseed-local"] == foreign_shape


def test_dry_run_previews_without_writing(zc_home: Path, stable_command: tuple[str, tuple[str, ...]]) -> None:
    path = zc_home / "cli" / "config.json"
    original = _seed_config(path)
    _, would_change = zc_install.install(dry_run=True)
    assert would_change is True
    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert not (zc_home / "cli" / "mnemoseed-local.json").is_file()
    _, changed = zc_install.install()
    assert changed is True


def test_malformed_config_refuses_instead_of_corrupting(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    path = zc_home / "cli" / "config.json"
    cases = [
        '{\n  // user annotation\n  "model": "opus"\n}\n',
        "{broken",
        json.dumps({"hooks": []}),
        json.dumps({"hooks": {"events": "garbage"}}),
        json.dumps({"hooks": {"events": {"Stop": "garbage"}}}),
        json.dumps({"hooks": {"enabled": "yes"}}),
        json.dumps({"mcp": []}),
        json.dumps({"mcp": {"servers": "garbage"}}),
    ]
    for original in cases:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original, encoding="utf-8")
        with pytest.raises(zc_install.ConfigParseError):
            zc_install.install()
        assert path.read_text(encoding="utf-8") == original


def test_foreign_process_entry_mentioning_the_marker_is_never_ours(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    path = zc_home / "cli" / "config.json"
    foreign = {
        "hooks": {
            "enabled": True,
            "events": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "process",
                                "command": "C:/tools/mnemoseed-local-helper.exe",
                                "args": ["watch"],
                            }
                        ]
                    }
                ]
            },
        }
    }
    _seed_config(path, foreign)
    _, changed = zc_install.install()
    assert changed is True
    config = json.loads(path.read_text(encoding="utf-8"))
    assert len(_handlers_of(config, "Stop")) == 1
    foreign_group = config["hooks"]["events"]["Stop"][0]
    assert foreign_group["hooks"][0]["args"] == ["watch"]

    _, existed = zc_install.uninstall()
    assert existed is True
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["hooks"]["events"]["Stop"] == [foreign_group]
    assert "mnemoseed-local-helper" in capsys.readouterr().err


def test_status_tracks_the_install_states(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    assert zc_install.status().state == "not-installed"
    zc_install.install()
    assert zc_install.status().state == "installed"
    path = zc_home / "cli" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["hooks"]["enabled"] = False
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    assert zc_install.status().state == "disabled"
    config["hooks"]["enabled"] = True
    del config["hooks"]["events"]["Stop"]
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    assert zc_install.status().state == "partial"
    zc_install.install()
    config = json.loads(path.read_text(encoding="utf-8"))
    del config["mcp"]["servers"]["mnemoseed-local"]
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    assert zc_install.status().state == "partial"
    zc_install.install()
    config = json.loads(path.read_text(encoding="utf-8"))
    config["mcp"]["servers"]["mnemoseed-local"] = {"type": "http", "url": "http://localhost:7788/mcp"}
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    assert zc_install.status().state == "partial"


def test_status_reports_the_config_path_and_daemon_probe(
    zc_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_probe(base_url: str, timeout: float = 2.0) -> bool:
        seen["base_url"] = base_url
        return True

    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", fake_probe)
    info = zc_install.status()
    assert info.daemon_reachable is True
    assert info.path == zc_home / "cli" / "config.json"
    assert seen["base_url"] == "http://localhost:7788"


def test_transform_command_resolves_an_absolute_tool_reference() -> None:
    command, args = zc_install.transform_command()
    assert Path(command).is_absolute()
    assert args[-3:] == ("_hook-event", "--host", "zcode")


@pytest.mark.parametrize(
    ("resolved", "expected_args"),
    [
        ((str(Path(__file__).resolve()), zc_install.TRANSFORM_ARGS), ["mcp"]),
        ((sys.executable, zc_install.MODULE_TRANSFORM_ARGS), ["-m", "mnemoseed_local.cli", "mcp"]),
    ],
)
def test_mcp_server_entry_resolves_like_the_handler(
    resolved: tuple[str, tuple[str, ...]],
    expected_args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(zc_install, "transform_command", lambda: resolved)
    assert zc_install.mcp_server_entry() == {
        "type": "stdio",
        "command": resolved[0],
        "args": expected_args,
    }


def test_mcp_server_entry_resolves_an_absolute_command() -> None:
    entry = zc_install.mcp_server_entry()
    assert Path(str(entry["command"])).is_absolute()
    assert tuple(entry["args"]) in (zc_install.MCP_GATEWAY_ARGS, zc_install.MCP_MODULE_GATEWAY_ARGS)


def test_utf8_foreign_content_survives_the_roundtrip(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]]
) -> None:
    path = zc_home / "cli" / "config.json"
    foreign = {
        "mcp": {
            "servers": {
                "本地服务器": {"type": "stdio", "command": "C:/工具/服务器.exe"},
                "其他网关": {"type": "stdio", "command": "C:/工具/其他.exe", "args": ["mcp"]},
            }
        },
        "hooks": {
            "enabled": False,
            "events": {"Stop": [{"hooks": [{"type": "command", "command": "我的钩子 --标记"}]}]},
        },
    }
    _seed_config(path, foreign)
    zc_install.install()
    zc_install.uninstall()
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["mcp"]["servers"] == foreign["mcp"]["servers"]
    assert config["hooks"]["events"] == foreign["hooks"]["events"]


def test_installed_mcp_entry_spawns_a_working_gateway(
    zc_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: the entry install() writes is a spawnable stdio gateway
    that answers initialize and tools/list without a daemon."""
    resolved = (sys.executable, zc_install.MODULE_TRANSFORM_ARGS)
    monkeypatch.setattr(zc_install, "transform_command", lambda: resolved)
    zc_install.install()
    path = zc_home / "cli" / "config.json"
    entry = json.loads(path.read_text(encoding="utf-8"))["mcp"]["servers"]["mnemoseed-local"]
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05",'
        '"capabilities":{},"clientInfo":{"name":"install-acceptance","version":"1"}}}\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
    )
    env = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
    env["MNEMOSEED_LOCAL_HOME"] = str(tmp_path)
    proc = subprocess.Popen(
        [entry["command"], *entry["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        out, err = proc.communicate(requests, timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("the gateway did not answer the handshake within 10s")
    responses: dict[object, dict] = {}
    for line in out.splitlines():
        if line.strip():
            message = json.loads(line)
            responses[message["id"]] = message
    assert responses[1]["result"]["serverInfo"]["name"] == "mnemoseed-local", err
    tools = responses[2]["result"]["tools"]
    assert isinstance(tools, list) and tools, err


def test_host_cli_lifecycle_wiring(
    zc_home: Path, stable_command: tuple[str, tuple[str, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["host", "install", "--host", "zcode"]) == 0
    assert "applied" in capsys.readouterr().out
    assert (zc_home / "cli" / "config.json").is_file()

    assert main(["host", "status", "--host", "zcode"]) == 0
    out = capsys.readouterr().out
    assert "installed" in out

    assert main(["host", "uninstall", "--host", "zcode"]) == 0
    assert "applied" in capsys.readouterr().out


# ---------------------------------------------------------------- hostile-locale subprocess oracle (B2.10)


class _VisibilityServer:
    """Loopback POST recorder standing in for the daemon in subprocess tests."""

    def __init__(self, served: dict) -> None:
        self.requests: list[tuple[str, bytes]] = []
        outer = self
        self.served = served

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.requests.append((self.path, body))
                payload = json.dumps(
                    outer.served if self.path == "/session/recall-pending" else {},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_process_level_stdin_roundtrips_utf8_under_a_hostile_locale(tmp_path: Path) -> None:
    """Subprocess-level UTF-8 oracle: real bytes piped into the REAL verb under
    PYTHONIOENCODING=gbk; the posted prompt text and the stdout payload must
    both survive the hostile stdin/stdout text layers."""
    served = _pull_payload(visibility={"mode": "compact", "capture": True})
    server = _VisibilityServer(served)
    try:
        prompt = "帮我查一下这个 flaky 测试为什么偶尔失败。"
        payload = _fixture("user_prompt") | {"prompt": prompt}
        env = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
        env["MNEMOSEED_LOCAL_BASEURL"] = server.url
        env["MNEMOSEED_LOCAL_HOME"] = str(tmp_path)
        env["PYTHONIOENCODING"] = "gbk"
        result = subprocess.run(
            [sys.executable, "-m", "mnemoseed_local.cli", "_hook-event", "--host", "zcode"],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=120,
            env=env,
            shell=False,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        posted = json.loads(server.requests[0][1].decode("utf-8"))
        assert posted["host"] == "zcode"
        assert posted["content"]["text"] == prompt
        out = json.loads(result.stdout.decode("utf-8"))
        payload_text = out["hookSpecificOutput"]["additionalContext"]
        capture_line, _, fence_block = payload_text.partition("\n")
        assert capture_line == events.CAPTURE_SIGNAL
        assert "中段关键线索" in fence_block
        assert prompt not in payload_text
    finally:
        server.close()
