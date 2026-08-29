"""B2.10: Claude Code host hook adapter (issue #106, PRD-B2.10).

Golden fixtures under ``tests/fixtures/claude_code_hook/`` pin the exact JSON
bodies the ``_hook-event`` transformer posts: each must validate against the
daemon's pydantic wire models AND round-trip through the real daemon surface
(mirrors the opencode fixture contract). Lifecycle tests pin the idempotent
marked-entry merge into ``~/.claude/settings.json`` (foreign entries never
touched), and the stdout-silence oracle pins the transformer's zero-stdout
discipline on success AND failure paths (UserPromptSubmit stdout leaks into
model context).
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

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from mnemoseed_local.cli import main
from mnemoseed_local.daemon.actor import resolve_actor
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.hosts.claude_code import events
from mnemoseed_local.hosts.claude_code import install as cc_install
from mnemoseed_local.rest_client import DaemonClient, DaemonRestError, DaemonUnavailableError
from mnemoseed_local.schema.turn import (
    FlushRequest,
    HostId,
    IngestEvent,
    IngestEventType,
    SessionEndRequest,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "claude_code_hook"

INGEST_FIXTURES = [
    "user_prompt.json",
    "user_prompt_agent.json",
    "assistant_message.json",
    "assistant_message_model_less.json",
    "tool_use.json",
]

ENV_VARS = (
    "MNEMOSEED_LOCAL_BASEURL",
    "MNEMOSEED_LOCAL_PROFILE_ID",
    "MNEMOSEED_LOCAL_DEBUG",
    "MNEMOSEED_LOCAL_DATA_DIR",
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def cc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic Claude Code config root: <tmp home>/.claude."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path / ".claude"


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


# ---------------------------------------------------------------- golden fixtures


@pytest.mark.parametrize(
    "name",
    INGEST_FIXTURES + ["session_end.json", "flush.json"],
)
def test_fixtures_are_single_line_json(name: str) -> None:
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    assert len(text.splitlines()) == 1


@pytest.mark.parametrize("name", INGEST_FIXTURES)
def test_ingest_fixtures_validate(name: str) -> None:
    event = IngestEvent.model_validate(_fixture(name))
    assert event.host is HostId.CLAUDE_CODE


def test_session_end_fixture_validates() -> None:
    SessionEndRequest.model_validate(_fixture("session_end.json"))


def test_flush_fixture_validates() -> None:
    FlushRequest.model_validate(_fixture("flush.json"))


def test_agent_fixture_carries_the_canonical_field_and_plain_fixtures_stay_none() -> None:
    attributed = IngestEvent.model_validate(_fixture("user_prompt_agent.json"))
    assert attributed.agent == "Explore"
    for name in INGEST_FIXTURES:
        if name != "user_prompt_agent.json":
            assert IngestEvent.model_validate(_fixture(name)).agent is None


# ---------------------------------------------------------------- golden <-> daemon e2e


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Embedded single-process daemon config (same harness as test_daemon)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


@pytest.mark.parametrize("name", INGEST_FIXTURES)
def test_ingest_fixtures_accepted_by_daemon(config_path: Path, name: str) -> None:
    with TestClient(create_app()) as client:
        response = client.post("/ingest", json=_fixture(name))
        assert response.status_code == 202, response.text


def test_session_end_fixture_accepted_by_daemon(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        opened = client.post("/ingest", json=_fixture("user_prompt.json"))
        assert opened.status_code == 202, opened.text
        settled = client.post("/session/end", json=_fixture("session_end.json"))
        assert settled.status_code == 200, settled.text
        assert settled.json()["status"] == "settled"


def test_flush_fixture_accepted_by_daemon(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        opened = client.post("/ingest", json=_fixture("user_prompt.json"))
        assert opened.status_code == 202, opened.text
        flushed = client.post("/flush", json=_fixture("flush.json"))
        assert flushed.status_code == 200, flushed.text
        assert flushed.json()["status"] == "flushed"


# ---------------------------------------------------------------- settings.json merge lifecycle


FOREIGN_SETTINGS = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "permissions": {"allow": ["Bash(uv run pytest:*)"]},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo foreign-pre"}]},
        ],
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "my-own-hook --flag"}]},
        ],
    },
}


def _seed_foreign_settings(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(FOREIGN_SETTINGS, indent=2) + "\n", encoding="utf-8")
    return FOREIGN_SETTINGS


def _ours_handlers(settings: dict, event: str) -> list[dict]:
    found = []
    for group in settings.get("hooks", {}).get(event, []):
        for handler in group.get("hooks", []):
            if isinstance(handler, dict) and cc_install.is_ours(handler):
                found.append(handler)
    return found


def test_install_creates_marked_entries_for_all_five_events(cc_home: Path) -> None:
    path, changed = cc_install.install()
    assert changed is True
    assert path == cc_home / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    for event in cc_install.HOOK_EVENTS:
        handlers = _ours_handlers(settings, event)
        assert len(handlers) == 1, event
        assert handlers[0]["type"] == "command"


def test_install_twice_leaves_settings_byte_identical(cc_home: Path) -> None:
    path = cc_home / "settings.json"
    _seed_foreign_settings(path)
    cc_install.install()
    first = path.read_bytes()
    _, changed = cc_install.install()
    assert changed is False
    assert path.read_bytes() == first


def test_install_never_mutates_foreign_entries(cc_home: Path) -> None:
    path = cc_home / "settings.json"
    _seed_foreign_settings(path)
    cc_install.install()
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["permissions"] == FOREIGN_SETTINGS["permissions"]
    assert settings["hooks"]["PreToolUse"] == FOREIGN_SETTINGS["hooks"]["PreToolUse"]
    foreign_groups = [
        group
        for group in settings["hooks"]["UserPromptSubmit"]
        if not any(cc_install.MARKER in str(h.get("command", "")) for h in group.get("hooks", []))
    ]
    assert foreign_groups == FOREIGN_SETTINGS["hooks"]["UserPromptSubmit"]


def test_uninstall_preserves_foreign_entries_exactly_and_removes_all_markers(cc_home: Path) -> None:
    path = cc_home / "settings.json"
    _seed_foreign_settings(path)
    original_foreign_hooks = {
        event: json.loads(json.dumps(FOREIGN_SETTINGS["hooks"][event]))
        for event in ("PreToolUse", "UserPromptSubmit")
    }
    cc_install.install()
    _, existed = cc_install.uninstall()
    assert existed is True
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["$schema"] == FOREIGN_SETTINGS["$schema"]
    assert after["permissions"] == FOREIGN_SETTINGS["permissions"]
    for event, expected in original_foreign_hooks.items():
        assert after["hooks"][event] == expected
    assert cc_install.MARKER not in path.read_text(encoding="utf-8")


def test_uninstall_without_install_reports_absent(cc_home: Path) -> None:
    _, existed = cc_install.uninstall()
    assert existed is False


def test_disable_flags_only_our_entries_and_enable_restores_them(cc_home: Path) -> None:
    path = cc_home / "settings.json"
    _seed_foreign_settings(path)
    cc_install.install()

    _, disabled_changed = cc_install.disable()
    assert disabled_changed is True
    settings = json.loads(path.read_text(encoding="utf-8"))
    for event in cc_install.HOOK_EVENTS:
        assert all(h.get("disabled") is True for h in _ours_handlers(settings, event))
    assert settings["hooks"]["PreToolUse"] == FOREIGN_SETTINGS["hooks"]["PreToolUse"]

    _, enabled_changed = cc_install.enable()
    assert enabled_changed is True
    settings = json.loads(path.read_text(encoding="utf-8"))
    for event in cc_install.HOOK_EVENTS:
        assert all("disabled" not in h for h in _ours_handlers(settings, event))
    assert settings["hooks"]["PreToolUse"] == FOREIGN_SETTINGS["hooks"]["PreToolUse"]

    # idempotence: a second enable over an enabled file reports no change
    assert cc_install.enable()[1] is False
    assert cc_install.disable()[1] is True


def test_status_tracks_installed_disabled_partial_states(
    cc_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    assert cc_install.status().state == "not-installed"
    cc_install.install()
    assert cc_install.status().state == "installed"
    cc_install.disable()
    assert cc_install.status().state == "disabled"
    cc_install.enable()
    path = cc_home / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    del settings["hooks"]["SessionEnd"]
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    assert cc_install.status().state == "partial"


def test_status_reuses_the_shared_healthz_probe(cc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_probe(base_url: str, timeout: float = 2.0) -> bool:
        seen["base_url"] = base_url
        return True

    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", fake_probe)
    info = cc_install.status()
    assert info.daemon_reachable is True
    assert seen["base_url"] == "http://localhost:7788"


def test_jsonc_or_broken_settings_refuse_instead_of_corrupting(cc_home: Path) -> None:
    """Official CC docs specify plain JSON settings; a tolerant rewrite would
    silently strip user comments, so non-strict files refuse with manual-edit
    guidance and stay byte-for-byte untouched."""
    path = cc_home / "settings.json"
    original = '{\n  // user annotation\n  "model": "opus"\n}\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(original, encoding="utf-8")
    with pytest.raises(cc_install.SettingsParseError):
        cc_install.install()
    assert path.read_text(encoding="utf-8") == original


def test_empty_settings_file_installs_cleanly(cc_home: Path) -> None:
    path = cc_home / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    _, changed = cc_install.install()
    assert changed is True
    assert len(_ours_handlers(json.loads(path.read_text(encoding="utf-8")), "Stop")) == 1


# ---------------------------------------------------------------- event normalization


def _stop_payload(**overrides: object) -> dict:
    payload = {
        "session_id": "cc-sess-01",
        "transcript_path": "",
        "cwd": "G:/dev/demo",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "时钟断言需要注入。",
    }
    payload.update(overrides)
    return payload


def test_user_prompt_maps_to_ingest_and_lifts_the_agent_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNEMOSEED_LOCAL_PROFILE_ID", raising=False)
    payload = {
        "session_id": "cc-sess-01",
        "cwd": "G:/dev/demo",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "第一条提示",
        "agent_type": "Explore",
        "agent_id": "agent-77f",
    }
    action = events.normalize_event(payload, now=1.0)
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert isinstance(body, IngestEvent)
    assert body.host is HostId.CLAUDE_CODE
    assert body.event.value == "user_prompt"
    assert body.content.text == "第一条提示"
    assert body.agent == "Explore"
    assert body.raw["agent_id"] == "agent-77f"


def test_stop_maps_to_assistant_ingest_with_model_from_transcript_tail(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"user","message":{"role":"user"}}',
                '{"type":"assistant","message":{"model":"claude-opus-4-1","content":[]}}',
                "not-json-garbage",
                '{"type":"assistant","message":{"model":"claude-sonnet-4-5","content":[]}}',
                "",
            ]
        ),
        encoding="utf-8",
    )
    action = events.normalize_event(_stop_payload(transcript_path=str(transcript)), now=2.0)
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert body.event.value == "assistant_message"
    assert body.content.model_id == "claude-sonnet-4-5"
    assert body.content.text == "时钟断言需要注入。"


def test_transcript_parse_failure_degrades_to_model_less_assistant_event(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("totally not json\n{\n", encoding="utf-8")
    for transcript_path in (str(garbage), str(tmp_path / "missing.jsonl"), None, 42):
        action = events.normalize_event(
            _stop_payload(transcript_path=transcript_path, last_assistant_message="仍要到达"),
            now=3.0,
        )
        assert action is not None, repr(transcript_path)
        kind, body = action
        assert kind == "ingest"
        assert body.event.value == "assistant_message"
        assert body.content.model_id is None
        assert body.content.text == "仍要到达"


def test_post_tool_use_maps_tool_content_and_caps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "MAX_TOOL_OUTPUT_CHARS", 20)
    payload = {
        "session_id": "cc-sess-01",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "uv run pytest -q"},
        "tool_response": {"stdout": "938 passed, 5 skipped", "success": True},
        "tool_use_id": "toolu_01ABC",
    }
    action = events.normalize_event(payload, now=4.0)
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert body.event.value == "tool_use"
    assert body.content.tool_name == "Bash"
    assert body.content.input == {"command": "uv run pytest -q"}
    assert body.content.output.endswith(events.TOOL_TRUNCATION_MARKER)
    assert events.MAX_TOOL_OUTPUT_CHARS >= len(body.content.output) - len(events.TOOL_TRUNCATION_MARKER)


def test_pre_compact_maps_to_flush_and_session_end_maps_to_settle() -> None:
    flush = events.normalize_event(
        {"session_id": "cc-sess-01", "hook_event_name": "PreCompact", "trigger": "auto"}, now=5.0
    )
    assert flush is not None
    flush_kind, flush_body = flush
    assert flush_kind == "flush"
    assert isinstance(flush_body, FlushRequest)
    assert flush_body.session_id == "cc-sess-01"

    settle = events.normalize_event(
        {"session_id": "cc-sess-01", "hook_event_name": "SessionEnd", "reason": "clear"}, now=6.0
    )
    assert settle is not None
    settle_kind, settle_body = settle
    assert settle_kind == "session_end"
    assert isinstance(settle_body, SessionEndRequest)
    assert settle_body.ts == 6.0


def test_stop_never_settles_the_session() -> None:
    """Regression pin: Stop is flush-semantics territory (segmenter closes the
    turn naturally); it must produce an /ingest assistant event and NEVER a
    /session/end action."""
    action = events.normalize_event(_stop_payload(), now=7.0)
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert body.event.value == "assistant_message"
    assert events.ENDPOINTS[kind] == "/ingest"


def test_subagent_lifecycle_events_are_not_ingested_in_v1() -> None:
    for name in ("SubagentStart", "SubagentStop"):
        payload = {"session_id": "cc-sess-01", "hook_event_name": name, "agent_type": "Explore"}
        assert events.normalize_event(payload, now=8.0) is None, name


def test_unknown_events_and_identity_less_payloads_are_dropped_silently() -> None:
    assert events.normalize_event({"hook_event_name": "Notification"}, now=9.0) is None
    assert events.normalize_event({"hook_event_name": "UserPromptSubmit", "prompt": "x"}, now=9.0) is None


def test_profile_binding_follows_env_then_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNEMOSEED_LOCAL_PROFILE_ID", raising=False)
    assert events.profile_id() == "default"
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "team-alpha")
    assert events.profile_id() == "team-alpha"


def test_blank_agent_stays_honest_null_through_the_transformer(
    monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    """A whitespace-only agent label must arrive at the daemon as an explicit
    null (honest unknown), never a stored blank (#105 semantics)."""
    monkeypatch.delenv("MNEMOSEED_LOCAL_PROFILE_ID", raising=False)
    _feed_stdin(
        monkeypatch,
        {
            "session_id": "cc-sess-01",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "hello",
            "agent": "   ",
        },
    )
    assert main(["_hook-event", "--host", "claude_code"]) == 0
    assert len(captured_posts) == 2, "an acked user_prompt also pulls recall-pending"
    path, body = captured_posts[0]
    assert path == "/ingest"
    assert body["agent"] is None
    assert captured_posts[1][0] == "/session/recall-pending"


# ---------------------------------------------------------------- transformer CLI: stdout-silence oracle


def _run_transformer(monkeypatch: pytest.MonkeyPatch, payload: object) -> int:
    _feed_stdin(monkeypatch, payload)
    return main(["_hook-event", "--host", "claude_code"])


def test_transformer_stdout_stays_silent_on_success(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run_transformer(
            monkeypatch,
            {"session_id": "cc-sess-01", "hook_event_name": "UserPromptSubmit", "prompt": "hi"},
        )
        == 0
    )
    result = capsys.readouterr()
    assert result.out == ""
    assert [path for path, _ in captured_posts] == ["/ingest", "/session/recall-pending"]


def test_transformer_stdout_stays_silent_when_the_daemon_is_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonUnavailableError("connection refused")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    assert (
        _run_transformer(
            monkeypatch, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
        )
        == 0
    )
    assert capsys.readouterr().out == ""


def test_transformer_survives_malformed_stdin_silently(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_transformer(monkeypatch, "{not json") == 0
    assert capsys.readouterr().out == ""
    assert captured_posts == []


def test_session_end_posts_fire_and_forget_without_draining(
    monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    """SessionEnd maps to /session/end only; the POST must not block on the
    daemon's drain (CC gives SessionEnd hooks a shared 1.5s budget)."""
    assert (
        _run_transformer(
            monkeypatch, {"session_id": "s1", "hook_event_name": "SessionEnd", "reason": "clear"}
        )
        == 0
    )
    assert [path for path, _ in captured_posts] == ["/session/end"]


def test_stop_pipeline_posts_only_to_ingest(
    monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    assert _run_transformer(monkeypatch, _stop_payload()) == 0
    assert [path for path, _ in captured_posts] == ["/ingest"]


# ---------------------------------------------------------------- CLI wiring


def test_hook_verb_accepts_claude_code_host(
    cc_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: True)
    assert main(["hook", "install", "claude_code"]) == 0
    out = capsys.readouterr().out
    assert "installed hook" in out
    assert (cc_home / "settings.json").is_file()

    assert main(["hook", "status", "claude_code"]) == 0
    out = capsys.readouterr().out
    assert "installed" in out
    assert "daemon: reachable" in out

    assert main(["hook", "disable", "claude_code"]) == 0
    assert "disabled hook" in capsys.readouterr().out
    assert main(["hook", "enable", "claude_code"]) == 0
    assert "enabled hook" in capsys.readouterr().out

    assert main(["hook", "uninstall", "claude_code"]) == 0
    assert "uninstalled hook" in capsys.readouterr().out


def test_hook_install_prints_manual_edit_guidance_on_unparsable_settings(
    cc_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = cc_home / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert main(["hook", "install", "claude_code"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "manually" in captured.err


# ---------------------------------------------------------------- QA hardening oracles


def test_non_dict_hooks_block_is_refused_not_overwritten(cc_home: Path) -> None:
    """A malformed top-level hooks value is user data: refuse untouched, never
    silently replace it with our map (mirrors the event-level refusal)."""
    path = cc_home / "settings.json"
    for bad_hooks in ([], "string"):
        original = json.dumps({"model": "opus", "hooks": bad_hooks}, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original, encoding="utf-8")
        with pytest.raises(cc_install.SettingsParseError):
            cc_install.install()
        assert path.read_text(encoding="utf-8") == original


def test_foreign_command_mentioning_the_marker_is_never_ours(
    cc_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A foreign command that merely CONTAINS "mnemoseed-local" must not be
    mistaken for our entry (no registration skip, no uninstall deletion); the
    loose substring probe may only feed a keep-warning on stderr."""
    path = cc_home / "settings.json"
    foreign_command = "tail -f ~/logs/mnemoseed-local-audit.log"
    foreign_group = {"hooks": [{"type": "command", "command": foreign_command}]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [foreign_group]}}, indent=2) + "\n", encoding="utf-8"
    )

    _, changed = cc_install.install()
    assert changed is True
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert len(_ours_handlers(settings, "UserPromptSubmit")) == 1
    assert foreign_group in settings["hooks"]["UserPromptSubmit"]

    _, existed = cc_install.uninstall()
    assert existed is True
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["hooks"]["UserPromptSubmit"] == [foreign_group]
    assert foreign_command in capsys.readouterr().err


def test_settings_rewrite_survives_a_mid_write_failure(
    cc_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewrites must be temp-file + fsync + os.replace atomic: a crash after
    the bytes hit the disk leaves the previous settings file fully intact and
    no temp litter behind."""
    path = cc_home / "settings.json"
    _seed_foreign_settings(path)
    cc_install.install()
    before = path.read_bytes()

    def exploding_fsync(fd: int) -> None:
        raise OSError("disk went away mid-write")

    monkeypatch.setattr(os, "fsync", exploding_fsync)
    with pytest.raises(OSError):
        cc_install.disable()
    assert path.read_bytes() == before
    assert [p.name for p in cc_home.iterdir() if p.name.endswith(".tmp")] == []


def test_transformer_survives_schema_validation_failures_silently(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A payload that maps but fails wire validation must exit 0 with zero
    stdout through the debug lane — never a traceback-exit inside CC."""
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "   ")
    assert (
        _run_transformer(
            monkeypatch,
            {"session_id": "cc-sess-01", "hook_event_name": "UserPromptSubmit", "prompt": "hi"},
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured_posts == []


def test_session_end_uses_tight_per_phase_timeout_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """SessionEnd runs against CC's shared ~1.5s teardown clock while
    /session/end drains server-side, so its POST gets per-phase limits; every
    other lane keeps the flat 2s budget."""
    seen: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {}

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        seen["timeout"] = kwargs.get("timeout")
        seen["timeouts"] = seen.get("timeouts", []) + [kwargs.get("timeout")]
        return FakeResponse()

    monkeypatch.setattr("mnemoseed_local.rest_client.httpx.post", fake_post)
    _feed_stdin(monkeypatch, {"session_id": "s1", "hook_event_name": "SessionEnd", "reason": "clear"})
    assert main(["_hook-event", "--host", "claude_code"]) == 0
    timeout = seen["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == timeout.write == timeout.read == timeout.pool == 0.5

    seen.clear()
    _feed_stdin(monkeypatch, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    assert main(["_hook-event", "--host", "claude_code"]) == 0
    ingest_timeout = seen["timeouts"][0]
    assert ingest_timeout == events.POST_TIMEOUT_SECONDS
    pull_timeout = seen["timeouts"][1]
    assert isinstance(pull_timeout, httpx.Timeout)
    assert (
        pull_timeout.connect
        == pull_timeout.write
        == pull_timeout.read
        == pull_timeout.pool
        == events.RECALL_PULL_TIMEOUT_SECONDS
    ), "the mid-session pull must use its dedicated 300ms budget, never the flat 2s"


def test_stop_without_last_assistant_message_falls_back_to_transcript_text(tmp_path: Path) -> None:
    """Mutation pin for the text fallback: a Stop payload without
    last_assistant_message takes the assistant TEXT BLOCKS from the transcript
    tail (alongside the model id), so the event still carries verbatim content."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"model":"claude-opus-4-1",'
        '"content":[{"type":"text","text":"来自转录尾部的回复"}]}}\n',
        encoding="utf-8",
    )
    action = events.normalize_event(
        _stop_payload(transcript_path=str(transcript), last_assistant_message=None),
        now=2.5,
    )
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert body.content.model_id == "claude-opus-4-1"
    assert body.content.text == "来自转录尾部的回复"


def test_tool_output_prefers_stderr_when_stdout_absent() -> None:
    payload = {
        "session_id": "cc-sess-01",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {},
        "tool_response": {"stderr": "warning: deprecated flag", "success": False},
    }
    action = events.normalize_event(payload, now=4.5)
    assert action is not None
    _, body = action
    assert body.content.output == "warning: deprecated flag"


def test_bom_prefixed_settings_still_parse_and_install(cc_home: Path) -> None:
    path = cc_home / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\ufeff" + json.dumps({"model": "opus"}) + "\n", encoding="utf-8")
    _, changed = cc_install.install()
    assert changed is True
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert len(_ours_handlers(settings, "Stop")) == 1


def test_daemon_trusts_the_hook_actor() -> None:
    request = Request({"type": "http", "headers": [(b"x-mnemoseed-actor", b"hook")]})
    assert resolve_actor(request) == "hook"


class _CaptureServer:
    """Loopback POST recorder standing in for the daemon in subprocess tests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append((self.path, self.rfile.read(length)))
                payload = b"{}"
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


def test_process_level_stdin_roundtrips_utf8_and_stays_silent() -> None:
    """Subprocess-level UTF-8 oracle: real bytes piped into the REAL verb.
    The child's hostile stdin text layer is forced via PYTHONIOENCODING=gbk,
    so any sys.stdin.read()-based decode corrupts non-ASCII prompts — the
    transformer must decode raw stdin bytes as UTF-8 itself."""
    server = _CaptureServer()
    try:
        prompt = "帮我查一下这个 flaky 测试为什么偶尔失败。"
        payload = {
            "session_id": "cc-sess-01",
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "cwd": "G:/dev/demo",
        }
        env = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
        env["MNEMOSEED_LOCAL_BASEURL"] = server.url
        env["PYTHONIOENCODING"] = "gbk"
        result = subprocess.run(
            [sys.executable, "-m", "mnemoseed_local.cli", "_hook-event", "--host", "claude_code"],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=120,
            env=env,
            shell=False,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        assert result.stdout == b""
        assert [path for path, _ in server.requests][0] == "/ingest"
        assert [path for path, _ in server.requests][1] == "/session/recall-pending"
        posted = json.loads(server.requests[0][1].decode("utf-8"))
        assert posted["content"]["text"] == prompt
    finally:
        server.close()


# ---------------------------------------------------------------- B2.1 T2 mid-session recall injection


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
    """Route DaemonClient.post so /session/recall-pending answers `payload`; the
    transient pull client shares the same class surface as the ingest client."""
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        if path == "/session/recall-pending":
            return payload
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    return calls


# ---- mapping: a normalized UserPromptSubmit action is the injection point


def test_user_prompt_action_is_the_injection_point() -> None:
    """normalize_event maps UserPromptSubmit to the ingest action, and that
    action is the recognized mid-session injection point (the daemon parks the
    focal slot synchronously before the ingest 2xx — ack-implies-ready)."""
    action = events.normalize_event(
        {
            "session_id": "cc-sess-01",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "继续",
        },
        now=1.0,
    )
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert body.event is IngestEventType.USER_PROMPT
    assert events.action_injectable(kind, body) is True
    assert events.injection_session_id(body) == "cc-sess-01"

    stop = events.normalize_event(_stop_payload(), now=2.0)
    assert stop is not None
    assert events.action_injectable(stop[0], stop[1]) is False
    assert events.injection_session_id(stop[1]) is None

    flush = events.normalize_event({"session_id": "cc-sess-01", "hook_event_name": "PreCompact"}, now=3.0)
    assert flush is not None
    assert events.action_injectable(flush[0], flush[1]) is False
    assert events.injection_session_id(flush[1]) is None


# ---- the injection action: pull + budget semantics


def test_injection_pulls_recall_pending_and_formats_budget_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injection action pulls /session/recall-pending with the wire
    budget_chars as the item budget (never a hardcoded cap) and assembles the
    fenced additionalContext envelope. A selection whose item cost lands inside
    (1200 - wrapper, 2000] is STILL appended — block length pinned exactly:
    wrapper(156) + item(1902) = 2058 (QA BLOCKER-1 mirror)."""
    item_text = "x" * 1901  # line cost 1902
    calls = _capture_pull(
        monkeypatch,
        _pull_payload(
            items=[{"kind": "chunk", "id": "c1", "text": item_text}],
            budget_chars=2000,
        ),
    )
    client = DaemonClient(base_url="http://daemon", profile_id="default", actor="hook")
    block = events.inject_recall_context("cc-sess-01", client)
    assert calls, "the injection must POST /session/recall-pending"
    path, body = calls[0]
    assert path == "/session/recall-pending"
    assert body == {
        "profile_id": "default",
        "session_id": "cc-sess-01",
        "seen_chunk_ids": [],
    }
    assert block is not None
    assert block.startswith(events.RECALL_FENCE_OPEN)
    assert block.count("<mnemoseed-memory-recall>") == 1
    assert block.endswith(events.RECALL_FENCE_CLOSE)
    assert len(block) == 2058, f"wrapper(156)+item(1902) must land exactly: {len(block)}"


def test_injection_below_slice_floor_still_appends(monkeypatch: pytest.MonkeyPatch) -> None:
    """QA IMPORTANT-3 mirror: the daemon is the sole budget authority across the
    WHOLE positive-int range — budget_chars=150 (below the T1 slice floor) is
    daemon-legal; a full item that fits IS served and appended. Block length
    pinned: wrapper(156) + item(101) = 257. The T2 injection path must not
    re-impose a slicing floor it does not own."""
    item_text = "y" * 100  # line cost 101
    _capture_pull(
        monkeypatch,
        _pull_payload(
            items=[{"kind": "chunk", "id": "c1", "text": item_text}],
            budget_chars=150,
        ),
    )
    block = events.inject_recall_context(
        "cc-sess-01", DaemonClient(base_url="http://daemon", profile_id="default")
    )
    assert block is not None
    assert len(block) == 257, f"wrapper(156)+item(101) must land exactly: {len(block)}"


def test_injection_oversized_item_drops_whole_block_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook re-checks each item fail-closed against the wire budget_chars: a
    line that alone exceeds the budget drops the WHOLE selection (defense in
    depth, unreachable by design) — nothing is appended."""
    item_text = "z" * 1201  # line cost 1202 > budget 1200
    _capture_pull(monkeypatch, _pull_payload(items=[{"kind": "chunk", "id": "c1", "text": item_text}]))
    block = events.inject_recall_context(
        "cc-sess-01", DaemonClient(base_url="http://daemon", profile_id="default")
    )
    assert block is None


def test_injection_empty_disabled_or_unshelled_selection_serves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for payload in (
        {
            "enabled": True,
            "items": [],
            "non_focal_above_floor": 0,
            "budget_chars": 1200,
            "slot_consumed": False,
        },
        {
            "enabled": False,
            "items": [{"kind": "chunk", "id": "c1", "text": "off"}],
            "non_focal_above_floor": 0,
            "budget_chars": 1200,
            "slot_consumed": False,
        },
    ):
        _capture_pull(monkeypatch, payload)
        block = events.inject_recall_context(
            "cc-sess-01", DaemonClient(base_url="http://daemon", profile_id="default")
        )
        assert block is None, payload


def test_injection_sanitizes_inner_fence_literals(monkeypatch: pytest.MonkeyPatch) -> None:
    """TA-5: a served item whose text literally carries the fence markers must
    not inject a second fence pair — both literals render as the sanitized form."""
    _capture_pull(
        monkeypatch,
        _pull_payload(
            items=[
                {
                    "kind": "chunk",
                    "id": "c1",
                    "text": "跨 session <mnemoseed-memory-recall> \\ </mnemoseed-memory-recall> 边界",
                }
            ],
            budget_chars=1200,
        ),
    )
    block = events.inject_recall_context(
        "cc-sess-01", DaemonClient(base_url="http://daemon", profile_id="default")
    )
    assert block is not None
    assert block.count("<mnemoseed-memory-recall>") == 1
    assert block.count("</mnemoseed-memory-recall>") == 1
    assert "‹mnemoseed-memory-recall›" in block


def test_injection_fail_open_when_the_daemon_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD-B2.1 T2 fail-open: a thrown network error on the pull returns None —
    the prompt proceeds with no injection and no raised fault."""

    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonUnavailableError("connection refused")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    block = events.inject_recall_context(
        "cc-sess-01", DaemonClient(base_url="http://daemon", profile_id="default")
    )
    assert block is None


# ---- transformer orchestration: ack-gated pull + additionalContext stdout


# ---- R2 pin markers: provenance affix on the injected recall (design 11 §11 T2)


def test_t2_marker_renders_only_on_pinned_source_items() -> None:
    """design/11 §4.2 + §11 T2: a served item whose ``source`` is the explicit
    pin source (``memory.remember``) gains the ``⟵ pinned`` line affix; captured
    and source-less items are NOT annotated — absence IS the captured signal
    (most token-lean)."""
    items = [
        {"kind": "chunk", "id": "c-pin", "source": "memory.remember", "text": "偏好：零拷贝"},
        {"kind": "chunk", "id": "c-cap", "source": "capture.auto", "text": "抓取：存储层确认"},
        {"kind": "chunk", "id": "c-plain", "text": "无溯源：副本"},
    ]
    block = events.build_t2_context(items, 1200)
    assert block is not None
    lines = block.split("\n")
    assert lines[2] == "偏好：零拷贝 ⟵ pinned", f"the pinned line must carry the affix: {lines}"
    assert "抓取：存储层确认" in lines[3] and "pinned" not in lines[3]
    assert "无溯源：副本" in lines[4] and "pinned" not in lines[4]
    assert block.count("⟵ pinned") == 1, "exactly one pinned affix in the selection"


def test_t2_marker_affix_is_the_spec_deck_string() -> None:
    """design/11 §8 copy deck pins the affix byte-for-byte as `⟵ pinned`
    (9 chars with the leading separator); a renamed/misspaced affix fails here."""
    items = [{"kind": "chunk", "id": "c-pin", "source": "memory.remember", "text": "x"}]
    block = events.build_t2_context(items, 1200)
    assert block is not None
    assert " ⟵ pinned" in block
    assert len(" ⟵ pinned") == 9


def test_t2_marker_budget_pressure_drops_the_affix_not_the_item() -> None:
    """design/11 §4.3 drop order: under a tight item budget the per-line affix
    is shed FIRST (the marker is a disposable reinforcement signal), the bare
    verbatim line still appends, and the block-level fail-closed check holds —
    the block lands at exactly budget + wrapper."""
    text = "z" * 149  # bare line cost 150 == budget 150; with affix 159 > 150
    items = [{"kind": "chunk", "id": "c-pin-bnd", "source": "memory.remember", "text": text}]
    block = events.build_t2_context(items, 150)
    assert block is not None, "shedding the affix must keep the item appended"
    assert "pinned" not in block, "the affix must be shed under pressure"
    assert text in block, "the bare verbatim line must survive"
    wrapper = len(events.RECALL_FENCE_OPEN) + 1 + len(events.RECALL_DISCLAIMER) + 1
    wrapper += len(events.RECALL_FENCE_CLOSE)
    assert len(block) == 150 + wrapper


def test_t2_marker_never_relaxes_the_fail_closed_oversized_drop() -> None:
    """design/11 §4.3: the affix must never let an otherwise-oversized item in —
    a line that exceeds the budget even bare still drops the WHOLE selection
    fail-closed (defense in depth, unchanged)."""
    text = "w" * 1201  # bare line cost 1202 > budget 1200
    items = [{"kind": "chunk", "id": "c-over", "source": "memory.remember", "text": text}]
    assert events.build_t2_context(items, 1200) is None


def test_t2_marker_attribution_survives_a_boundary_sliced_item() -> None:
    """design/11 §4.3: the daemon may tail-slice a boundary item before serving;
    the hook must keep the affix attribution correct on the received (already
    sliced) text — source-based, unaffected by the slice."""
    sliced = "…" + "乙" * 30
    items = [{"kind": "chunk", "id": "c-slice", "source": "memory.remember", "text": sliced}]
    block = events.build_t2_context(items, 1200)
    assert block is not None
    assert sliced + " ⟵ pinned" in block, f"sliced pinned text must keep its affix: {block}"


def test_t2_marker_sheds_kept_affixes_before_a_multi_item_overrun_drop() -> None:
    """IMPORTANT-1 (QA, exact repro): a kePT affix on an EARLY committed pinned
    line must NEVER squeeze out a LATER captured line. Budget 100, [pinned A(50)
    +affix, captured B(41)]: A fits affixed (60<=100, remaining 41→40) then
    B(42)>40 → the old code returned None (whole drop) even though BARE A+B=93
    <=100. design/11 §4.3: shed the affix BEFORE dropping an item — the daemon-
    legal selection must be SERVED with A's affix shed, still fail-closed only
    when the bare total exceeds budget."""
    a = "a" * 50  # bare line cost 51; affixed 60
    b = "b" * 41  # bare line cost 42
    items = [
        {"kind": "chunk", "id": "c-pin-a", "source": "memory.remember", "text": a},
        {"kind": "chunk", "id": "c-cap-b", "source": "capture.auto", "text": b},
    ]
    block = events.build_t2_context(items, 100)
    assert block is not None, "the affix must be shed so the bare selection is served, not dropped"
    assert "pinned" not in block, f"all kept affixes must be shed under pressure: {block}"
    assert a in block, f"the bare pinned line must survive: {block}"
    assert b in block, f"the later captured line must survive: {block}"


def test_t2_marker_post_shed_accounting_uses_bare_not_affixed_cost() -> None:
    """NIT-2 mutation guard for the post-shed accounting: the shed branch MUST
    decrement `remaining` by the BARE line cost. A mutation to `remaining -=
    affixed line_cost` (which still carries the shed affix) must fail HERE —
    Budget 100, [captured Z(89)→rem 10, pinned X(5): affixed 15>10∪bare 6<=10 →
    shed rem 10-6=4, captured Y(3): cost 4<=4 served]. If the shed branch used
    the affixed 15, remaining would go -5 and Y(4) would overrun → None."""
    z = "z" * 89  # bare line cost 90
    x = "x" * 5  # bare line cost 6; affixed 15
    y = "y" * 3  # bare line cost 4
    items = [
        {"kind": "chunk", "id": "c-cap-z", "source": "capture.auto", "text": z},
        {"kind": "chunk", "id": "c-pin-x", "source": "memory.remember", "text": x},
        {"kind": "chunk", "id": "c-cap-y", "source": "capture.auto", "text": y},
    ]
    block = events.build_t2_context(items, 100)
    assert block is not None, "the post-shed bare accounting must fit the trailing item"
    assert z in block and x in block and y in block, "all three lines must be served"


def test_t2_marker_two_sheds_keep_prior_bare_line_intact() -> None:
    """BLOCKER (QA, verbatim corruption): when the affix is shed a SECOND time in
    one call, every line that still carried a kept affix is rebuilt bare. The old
    `affix_flags` reset + re-enumeration (`lines[2 + i]`) sliced 9 chars off the
    WRONG already-bare committed line after the first shed. Exact repro — Budget
    2400, [pinned P1("a"*1800), pinned P2("b"*582), pinned P3("c"*6), pinned
    P4("e"*1)]: P1 (affixed 1810<=2400) commits, P2 (affixed 592>rem 590) sheds
    P1's affix, P3 (affixed 16<=rem 16) commits a fresh kept affix, then P4
    (affixed 11>rem 0) sheds it a SECOND time. (a) P1's line must be UNCHANGED at
    length 1800 (never 1800->1791), (b) P3's freshly-kept affix is shed, (c) the
    bare selection (total 2393<=2400) is served, not None."""
    a = "a" * 1800
    b = "b" * 582
    c = "c" * 6
    e = "e" * 1
    items = [
        {"kind": "chunk", "id": "c-p1", "source": "memory.remember", "text": a},
        {"kind": "chunk", "id": "c-p2", "source": "memory.remember", "text": b},
        {"kind": "chunk", "id": "c-p3", "source": "memory.remember", "text": c},
        {"kind": "chunk", "id": "c-p4", "source": "memory.remember", "text": e},
    ]
    block = events.build_t2_context(items, 2400)
    assert block is not None, "the bare selection must be served, not dropped"
    lines = block.split("\n")
    assert lines[2] == a, (
        "the earlier committed bare line must be byte-identical (no 1800->1791 "
        f"corruption) — got length {len(lines[2])}"
    )
    assert "pinned" not in block, f"every kept affix must be shed: {block}"
    assert c in block, "the mid-selection decorated item must be served bare"
    assert e in block, "the trailing item must be served"


def test_transformer_emits_additional_context_only_on_a_served_pull(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A UserPromptSubmit that ack'd its ingest pulls recall-pending and, on a
    served selection, emits the fenced block as hookSpecificOutput.additionalContext
    — the ONLY stdout this transformer may produce."""
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        if path == "/session/recall-pending":
            return _pull_payload(budget_chars=200)
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    assert (
        _run_transformer(
            monkeypatch, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
        )
        == 0
    )
    assert [path for path, _ in calls] == ["/ingest", "/session/recall-pending"]
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    block = out["hookSpecificOutput"]["additionalContext"]
    assert "中段关键线索" in block
    assert block.count("<mnemoseed-memory-recall>") == 1


def test_transformer_never_pulls_before_the_ingest_ack(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PRD-B2.1 T2 / D8 (ack-implies-ready): a UserPromptSubmit whose ingest is
    rejected (non-2xx -> no slot parked) must NOT pull — zero injection, silent."""

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonRestError(409, "already settled")

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    assert (
        _run_transformer(
            monkeypatch, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
        )
        == 0
    )
    assert capsys.readouterr().out == ""


def test_transformer_fail_open_when_pull_fails_keeps_stdout_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        if path == "/session/recall-pending":
            raise DaemonUnavailableError("daemon went away mid-pull")
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    assert (
        _run_transformer(
            monkeypatch, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
        )
        == 0
    )
    assert [path for path, _ in calls] == ["/ingest", "/session/recall-pending"]
    assert capsys.readouterr().out == ""


def test_stop_and_flush_never_emit_additional_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the UserPromptSubmit injection point may write additionalContext;
    Stop (assistant ingest) and PreCompact (flush) keep the zero-stdout lane."""
    for payload in (
        _stop_payload(),
        {"session_id": "s1", "hook_event_name": "PreCompact", "trigger": "auto"},
    ):
        assert _run_transformer(monkeypatch, payload) == 0
        assert capsys.readouterr().out == ""


# ---- install wiring: UserPromptSubmit carries a synchronous transformer command


def test_install_registers_user_prompt_submit_as_the_injection_surface(
    cc_home: Path,
) -> None:
    """The registered UserPromptSubmit handler must run the transformer command
    synchronously (no async) — Claude Code delivers a SYNCHRONOUS hook's
    additionalContext on the CURRENT turn; an async UserPromptSubmit would push
    it to the next turn and break the same-turn T2 injection."""
    cc_install.install()
    settings = json.loads((cc_home / "settings.json").read_text(encoding="utf-8"))
    handlers = _ours_handlers(settings, "UserPromptSubmit")
    assert len(handlers) == 1
    assert handlers[0]["command"] == cc_install.TRANSFORM_COMMAND
    assert handlers[0].get("async") is not True
