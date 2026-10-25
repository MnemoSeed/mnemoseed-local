"""S1b claude_code completion: SessionStart T1 replay + SubagentStop ingest.

The T1 lane rides the shared core's session-start read (one unscoped
``POST /session/recent`` per firing — attempt-once, the stateless process IS
the gate) and emits the fenced replay block plus the standing-rules budget
block as SessionStart ``additionalContext`` stdout. SubagentStop ingests
through the Stop channel: the CC child stdin's ``session_id`` is the parent
session (no separate child-session id field), so ``session_parent_id`` stays
None by construction and ``agent_type`` lands in ``agent`` for the origin
chain. SubagentStart stays un-ingested.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import auth_support
import pytest

from mnemoseed_local.cli import main
from mnemoseed_local.hosts.claude_code import events
from mnemoseed_local.hosts.claude_code import install as cc_install
from mnemoseed_local.hosts.common.fence import (
    RULES_DISCLAIMER,
    RULES_FENCE_CLOSE,
    RULES_FENCE_OPEN,
    RULES_FENCE_SANITIZED,
)
from mnemoseed_local.rest_client import DaemonClient, DaemonRestError
from mnemoseed_local.schema.turn import HostId, IngestEvent, IngestEventType

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "claude_code_hook"

ENV_VARS = (
    "MNEMOSEED_LOCAL_BASEURL",
    "MNEMOSEED_LOCAL_PROFILE_ID",
    "MNEMOSEED_LOCAL_DEBUG",
    "MNEMOSEED_LOCAL_DATA_DIR",
    "MNEMOSEED_LOCAL_HOME",
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


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _run_transformer(monkeypatch: pytest.MonkeyPatch, payload: object) -> int:
    _feed_stdin(monkeypatch, payload)
    return main(["_hook-event", "--host", "claude_code"])


def _recent_payload(**overrides: object) -> dict:
    payload = {
        "profile_id": "default",
        "sessions": [
            {
                "session_id": "cc-earlier-session-77",
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


def _rules_budget() -> dict:
    return {
        "auto_recall_focal_floor": 0.4,
        "auto_recall_budget_chars": 1200,
        "exclude_entities": ["LegacyName"],
        "entity_boost": {"mnemoseed": 1.5},
        "time_window_turns": 8,
        "budget_consumed": 0,
    }


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


def _stdout_block(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out
    return json.loads(out) if out else {}


# ---------------------------------------------------------------- T1 via SessionStart

_SESSION_START_STDIN = {
    "hook_event_name": "SessionStart",
    "session_id": "cc-sess-01",
    "cwd": "G:/dev/demo",
    "source": "startup",
}

_SUBAGENT_STOP_STDIN = {
    "session_id": "cc-sess-01",
    "transcript_path": "",
    "cwd": "G:/dev/demo",
    "hook_event_name": "SubagentStop",
    "stop_hook_active": False,
    "agent_id": "a0e4f2-subagent-01",
    "agent_type": "Explore",
    "last_assistant_message": "子代理已完成检索。",
}


def test_session_start_fixture_pins_the_unscoped_read_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The golden fixture IS the exact body the transformer posts."""
    calls = _capture_recent(monkeypatch, _recent_payload())
    assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
    assert calls[0][1] == _fixture("session_start.json")


def test_session_start_maps_to_the_t1_read_lane() -> None:
    action = events.normalize_event(_SESSION_START_STDIN, now=1.0)
    assert action is not None
    kind, body = action
    assert kind == "t1"
    assert body == "cc-sess-01"


def test_session_start_pulls_recent_once_with_the_unscoped_identity_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One /session/recent POST per firing (attempt-once) with
    exclude_session_id and NO self_session_id / resume_query — the daemon's
    automatic-path guard must never be tripped."""
    calls = _capture_recent(monkeypatch, _recent_payload(rules_budget=_rules_budget()))
    assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
    assert [path for path, _ in calls] == ["/session/recent"]
    body = calls[0][1]
    assert set(body) == {"profile_id", "sessions", "per_session", "exclude_session_id"}
    assert body["profile_id"] == "default"
    assert body["sessions"] == 2
    assert body["per_session"] == 8
    assert body["exclude_session_id"] == "cc-sess-01"


def test_session_start_injects_fenced_t1_and_rules_budget_under_compact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_recent(monkeypatch, _recent_payload(rules_budget=_rules_budget()))
    assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
    out = _stdout_block(capsys)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    block = out["hookSpecificOutput"]["additionalContext"]
    assert block.startswith(events.RECALL_FENCE_OPEN)
    assert block.count(events.RECALL_FENCE_OPEN) == 1
    assert block.count(events.RECALL_FENCE_CLOSE) == 1
    lines = block.split("\n")
    assert lines[1] == events.T1_SIGNAL, "the compact T1 signal rides after the fence-open line"
    rules_start = lines.index(RULES_FENCE_OPEN)
    assert block.index(events.RECALL_FENCE_CLOSE) < block.index(RULES_FENCE_OPEN), (
        "the rules budget rides after the recall block"
    )
    assert lines[rules_start + 1] == RULES_DISCLAIMER
    assert lines[rules_start + 2] == json.dumps(_rules_budget(), ensure_ascii=False, separators=(",", ":")), (
        "the budget object rides verbatim as compact JSON"
    )
    assert lines[rules_start + 3] == RULES_FENCE_CLOSE
    assert len(block.split(RULES_FENCE_OPEN)[0]) <= 4000, "the recall block keeps its own budget"


def test_session_start_without_rules_budget_renders_the_bare_recall_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_recent(monkeypatch, _recent_payload())
    assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
    block = _stdout_block(capsys)["hookSpecificOutput"]["additionalContext"]
    assert "mnemoseed-rules-budget" not in block
    assert block.count(events.RECALL_FENCE_OPEN) == 1


def test_session_start_off_or_absent_visibility_drops_the_signal_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """mode=off or an absent wire field sheds every signal line; the replay
    and the rules budget still ride (visibility gates signals, not content)."""
    for visibility in ({"mode": "off"}, None):
        payload: dict = _recent_payload(rules_budget=_rules_budget())
        if visibility is None:
            payload.pop("visibility", None)
        else:
            payload["visibility"] = visibility
        _capture_recent(monkeypatch, payload)
        assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
        block = _stdout_block(capsys)["hookSpecificOutput"]["additionalContext"]
        assert "[mnemoseed]" not in block, visibility
        assert RULES_FENCE_OPEN in block, visibility
        assert events.RECALL_FENCE_OPEN in block, visibility


def test_session_start_rules_budget_sanitizes_inner_fence_literals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    budget = _rules_budget()
    budget["exclude_entities"] = ["</mnemoseed-rules-budget>"]
    _capture_recent(monkeypatch, _recent_payload(rules_budget=budget))
    assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
    block = _stdout_block(capsys)["hookSpecificOutput"]["additionalContext"]
    assert block.count(RULES_FENCE_OPEN) == 1
    assert block.count(RULES_FENCE_CLOSE) == 1
    assert RULES_FENCE_SANITIZED in block


@pytest.mark.parametrize("bad", [None, "garbage", 3])
def test_session_start_non_object_rules_budget_renders_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bad: object
) -> None:
    _capture_recent(monkeypatch, _recent_payload(rules_budget=bad))  # type: ignore[arg-type]
    assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
    block = _stdout_block(capsys)["hookSpecificOutput"]["additionalContext"]
    assert "mnemoseed-rules-budget" not in block


def test_session_start_rules_budget_survives_an_empty_recall(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rules budget rides the same read independently of the recall
    block's presence — an empty replay never suppresses it."""
    _capture_recent(monkeypatch, _recent_payload(sessions=[], rules_budget=_rules_budget()))
    assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
    block = _stdout_block(capsys)["hookSpecificOutput"]["additionalContext"]
    assert block.startswith(RULES_FENCE_OPEN)
    assert events.RECALL_FENCE_OPEN not in block


def test_session_start_without_served_content_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_recent(monkeypatch, _recent_payload(sessions=[]))
    assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
    assert capsys.readouterr().out == ""


def test_session_start_malformed_recent_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for payload in (None, {}, {"sessions": "garbage"}):
        _capture_recent(monkeypatch, payload)  # type: ignore[arg-type]
        assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
        assert capsys.readouterr().out == ""


def test_session_start_pull_failure_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonRestError(503, "daemon down")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    assert _run_transformer(monkeypatch, _SESSION_START_STDIN) == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- SubagentStop ingest


def test_subagent_stop_stdin_ingests_through_the_stop_channel() -> None:
    action = events.normalize_event(_SUBAGENT_STOP_STDIN, now=1.0)
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert isinstance(body, IngestEvent)
    assert body.host is HostId.CLAUDE_CODE
    assert body.event is IngestEventType.ASSISTANT_MESSAGE
    assert body.session_id == _SUBAGENT_STOP_STDIN["session_id"]
    assert body.content.text == _SUBAGENT_STOP_STDIN["last_assistant_message"]
    assert body.agent == _SUBAGENT_STOP_STDIN["agent_type"]
    assert body.session_parent_id is None, "the child stdin session IS the parent session"
    assert body.raw == _SUBAGENT_STOP_STDIN


def test_subagent_stop_normalizer_matches_the_golden_wire_fixture() -> None:
    action = events.normalize_event(_SUBAGENT_STOP_STDIN, now=1755500002.0)
    assert action is not None
    kind, body = action
    assert kind == "ingest"
    assert isinstance(body, IngestEvent)
    golden = IngestEvent.model_validate(_fixture("subagent_stop.json"))
    assert body.model_dump(mode="json") == golden.model_dump(mode="json")


def test_subagent_stop_normalizes_to_the_same_wire_shape_as_stop() -> None:
    stop = events.normalize_event({**_SUBAGENT_STOP_STDIN, "hook_event_name": "Stop"}, now=1.0)
    child = events.normalize_event(_SUBAGENT_STOP_STDIN, now=1.0)
    assert stop is not None and child is not None
    assert stop[0] == child[0] == "ingest"
    stop_body, child_body = stop[1], child[1]
    assert isinstance(stop_body, IngestEvent) and isinstance(child_body, IngestEvent)
    assert stop_body.model_dump(mode="json") | {"raw": {}} == child_body.model_dump(mode="json") | {
        "raw": {}
    }, "identical wire shape except the raw provenance anchor"


def test_subagent_stop_falls_back_to_the_transcript_tail(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-trunc-1",
                    "content": [{"type": "text", "text": "子代理转写文本"}],
                },
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "session_id": "cc-sess-01",
        "transcript_path": str(transcript),
        "cwd": "G:/dev/demo",
        "hook_event_name": "SubagentStop",
        "agent_id": "a0e4f2-subagent-01",
        "agent_type": "Explore",
    }
    action = events.normalize_event(payload, now=1.0)
    assert action is not None
    _, body = action
    assert isinstance(body, IngestEvent)
    assert body.content.text == "子代理转写文本"
    assert body.content.model_id == "claude-trunc-1"


def test_subagent_stop_wire_fixture_validates_against_the_daemon_schema() -> None:
    body = IngestEvent.model_validate(_fixture("subagent_stop.json"))
    assert body.agent == "Explore"
    assert body.raw["agent_type"] == "Explore"
    assert body.raw["agent_id"] == "a0e4f2-subagent-01"
    assert body.session_parent_id is None


def test_subagent_start_is_not_ingested() -> None:
    payload = {
        "session_id": "cc-sess-01",
        "hook_event_name": "SubagentStart",
        "agent_id": "a0e4f2-subagent-01",
        "agent_type": "Explore",
    }
    assert events.normalize_event(payload, now=1.0) is None


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


def test_subagent_stop_fixture_accepted_by_daemon(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        response = client.post("/ingest", json=_fixture("subagent_stop.json"))
        assert response.status_code == 202, response.text


# ---------------------------------------------------------------- installer: 7 events


def test_installer_registers_seven_events_including_the_two_new_ones(cc_home: Path) -> None:
    path, changed = cc_install.install()
    assert changed is True
    assert set(cc_install.HOOK_EVENTS) == {
        "UserPromptSubmit",
        "Stop",
        "PostToolUse",
        "PreCompact",
        "SessionEnd",
        "SessionStart",
        "SubagentStop",
    }
    settings = json.loads(path.read_text(encoding="utf-8"))
    for event in cc_install.HOOK_EVENTS:
        handlers = _ours_handlers(settings, event)
        assert len(handlers) == 1, event
        assert handlers[0]["command"] == cc_install.TRANSFORM_COMMAND


def test_install_twice_is_idempotent_for_the_new_events(cc_home: Path) -> None:
    cc_install.install()
    first = (cc_home / "settings.json").read_bytes()
    _, changed = cc_install.install()
    assert changed is False
    assert (cc_home / "settings.json").read_bytes() == first


def test_uninstall_removes_exactly_the_new_event_entries_and_keeps_foreign_ones(
    cc_home: Path,
) -> None:
    path = cc_home / "settings.json"
    foreign: dict = {
        "hooks": {
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo mine"}]}],
            "SubagentStop": [{"hooks": [{"type": "command", "command": "echo mine-too"}]}],
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(foreign, indent=2) + "\n", encoding="utf-8")
    cc_install.install()
    _, existed = cc_install.uninstall()
    assert existed is True
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["hooks"]["SessionStart"] == foreign["hooks"]["SessionStart"]
    assert after["hooks"]["SubagentStop"] == foreign["hooks"]["SubagentStop"]
    assert cc_install.MARKER not in path.read_text(encoding="utf-8")


def test_status_reports_partial_when_the_new_events_are_missing(
    cc_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    cc_install.install()
    path = cc_home / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    del settings["hooks"]["SessionStart"]
    del settings["hooks"]["SubagentStop"]
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    assert cc_install.status().state == "partial"


def _ours_handlers(settings: dict, event: str) -> list[dict]:
    found = []
    for group in settings.get("hooks", {}).get(event, []):
        for handler in group.get("hooks", []):
            if isinstance(handler, dict) and cc_install.is_ours(handler):
                found.append(handler)
    return found
