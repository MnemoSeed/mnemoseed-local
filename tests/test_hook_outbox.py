"""D5 hook outbox: one bounded POST, spool on failure, ordered flush on recovery.

A hook firing while the daemon is down must not silently lose the turn and
must stay inside the hook's time budget. These tests drive the public
``post_capture`` surface from hosts/common/daemon.py through a client whose
POST is scripted to fail/recover, plus every host's ``transform_event`` entry.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local import config as config_mod
from mnemoseed_local.hosts.common import daemon as hook_daemon
from mnemoseed_local.rest_client import DaemonClient, DaemonUnavailableError


@pytest.fixture
def outbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The outbox resolves its path from the call-time config home."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("MNEMOSEED_LOCAL_DEBUG", raising=False)
    monkeypatch.delenv("MNEMOSEED_LOCAL_BASEURL", raising=False)
    monkeypatch.delenv("MNEMOSEED_LOCAL_PROFILE_ID", raising=False)
    return tmp_path


def _client() -> DaemonClient:
    return DaemonClient(base_url="http://hook.test")


def _post(client: DaemonClient | None, path: str, body: dict, *, host: str) -> None:
    """Drive post_capture as a host guard does: the re-raised failure is caught."""
    try:
        hook_daemon.post_capture(client, path, body, host=host)
    except DaemonUnavailableError:
        pass


def _lines(home: Path) -> list[dict[str, Any]]:
    path = home / hook_daemon.OUTBOX_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_down_time_spools_one_line_and_warns_without_debug(
    outbox_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def down(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonUnavailableError("connection refused")

    monkeypatch.setattr(DaemonClient, "post", down)
    _post(_client(), "/ingest", {"ordinal": 7}, host="codex_cli")

    spooled = _lines(outbox_home)
    assert len(spooled) == 1
    assert spooled[0]["path"] == "/ingest"
    assert spooled[0]["body"] == {"ordinal": 7}
    assert spooled[0]["host"] == "codex_cli"
    assert isinstance(spooled[0]["ts"], (int, float))
    err = capsys.readouterr().err
    assert "mnemoseed-local:" in err
    assert "spooled" in err.lower(), "the warning must be visible with DEBUG off"


def test_single_post_attempt_no_mcp_style_retry(outbox_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def down(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        attempts.append(path)
        raise DaemonUnavailableError("connection refused")

    monkeypatch.setattr(DaemonClient, "post", down)
    _post(_client(), "/ingest", {"ordinal": 1}, host="zcode")
    assert attempts == ["/ingest"], "a failed hook POST is attempted exactly once"


def test_flushed_lines_precede_the_current_post_in_order(
    outbox_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    delivered: list[int] = []
    state = {"down": True}

    def maybe(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        if state["down"]:
            raise DaemonUnavailableError("down")
        delivered.append(int((body or {})["ordinal"]))
        return {}

    monkeypatch.setattr(DaemonClient, "post", maybe)
    client = _client()
    _post(client, "/ingest", {"ordinal": 1}, host="zcode")
    _post(client, "/ingest", {"ordinal": 2}, host="zcode")
    assert delivered == []
    assert len(_lines(outbox_home)) == 2

    state["down"] = False
    _post(client, "/ingest", {"ordinal": 3}, host="zcode")
    assert delivered == [1, 2, 3], "the spool must flush in order before the new turn"
    assert _lines(outbox_home) == [], "a fully flushed spool is emptied"


def test_ac5_twenty_down_turns_recover_exactly_once(
    outbox_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    delivered: list[int] = []
    state = {"down": True}

    def maybe(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        if state["down"]:
            raise DaemonUnavailableError("down")
        delivered.append(int((body or {})["ordinal"]))
        return {}

    monkeypatch.setattr(DaemonClient, "post", maybe)
    client = _client()
    for ordinal in range(20):
        _post(client, "/ingest", {"ordinal": ordinal}, host="codex_cli")

    assert len(_lines(outbox_home)) == 20
    assert "spooled" in capsys.readouterr().err.lower()

    state["down"] = False
    _post(client, "/ingest", {"ordinal": 20}, host="codex_cli")
    assert delivered == list(range(21)), "each turn reaches the store exactly once, in order"
    assert _lines(outbox_home) == []


def test_spool_write_is_not_debug_gated(outbox_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_DEBUG", "")
    monkeypatch.setattr(
        DaemonClient,
        "post",
        lambda self, path, body=None: (_ for _ in ()).throw(DaemonUnavailableError("down")),
    )
    _post(_client(), "/ingest", {"ordinal": 9}, host="cursor")
    assert len(_lines(outbox_home)) == 1, "the spool is written even with the debug lane off"


def test_retention_drops_oldest_and_warns(
    outbox_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hook_daemon, "OUTBOX_MAX_BYTES", 240)
    monkeypatch.setattr(
        DaemonClient,
        "post",
        lambda self, path, body=None: (_ for _ in ()).throw(DaemonUnavailableError("down")),
    )
    client = _client()
    for ordinal in range(6):
        _post(client, "/ingest", {"ordinal": ordinal, "pad": "x" * 30}, host="zcode")

    kept = _lines(outbox_home)
    assert len(kept) < 6, "the oldest lines are dropped once the byte cap is crossed"
    assert [entry["body"]["ordinal"] for entry in kept] == sorted(
        entry["body"]["ordinal"] for entry in kept
    ), "survivors stay in order (oldest dropped first)"
    assert "overflow" in capsys.readouterr().err.lower() or "dropped" in capsys.readouterr().err.lower()


def test_age_pruning_drops_stale_lines(outbox_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = outbox_home / hook_daemon.OUTBOX_FILENAME
    stale = {
        "ts": time.time() - hook_daemon.OUTBOX_MAX_AGE_SECONDS - 10,
        "host": "zcode",
        "path": "/ingest",
        "body": {"ordinal": -1},
    }
    fresh = {"ts": time.time(), "host": "zcode", "path": "/ingest", "body": {"ordinal": 1}}
    path.write_text(json.dumps(stale) + "\n" + json.dumps(fresh) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        DaemonClient,
        "post",
        lambda self, path, body=None: (_ for _ in ()).throw(DaemonUnavailableError("down")),
    )
    _post(_client(), "/ingest", {"ordinal": 2}, host="zcode")
    assert [entry["body"]["ordinal"] for entry in _lines(outbox_home)] == [1, 2]


# ---------------------------------------------------------------- host routing


@pytest.mark.parametrize(
    ("host", "module", "payload"),
    [
        (
            "codex_cli",
            "mnemoseed_local.hosts.codex_cli.events",
            {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "hi"},
        ),
        (
            "claude_code",
            "mnemoseed_local.hosts.claude_code.events",
            {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "hi"},
        ),
        (
            "cursor",
            "mnemoseed_local.hosts.cursor.events",
            {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "s", "prompt": "hi"},
        ),
        (
            "zcode",
            "mnemoseed_local.hosts.zcode.events",
            {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "hi"},
        ),
    ],
)
def test_every_host_routes_its_failed_post_through_the_spool(
    host: str,
    module: str,
    payload: dict,
    outbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    def down(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonUnavailableError("connection refused")

    monkeypatch.setattr(DaemonClient, "post", down)
    events = importlib.import_module(module)
    assert events.transform_event(json.dumps(payload)) == 0
    spooled = _lines(outbox_home)
    assert spooled, f"{host} did not spool its failed turn"
    assert any(entry["path"] == "/ingest" and entry["host"] == host for entry in spooled)
