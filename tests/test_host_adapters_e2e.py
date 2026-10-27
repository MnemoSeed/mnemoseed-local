"""Host adapter E2E on a real embedded daemon (PRD-272 §5.10).

Each Python host (zcode, codex_cli) walks one full chain against a real
daemon booted in an isolated ``MNEMOSEED_LOCAL_HOME`` on a spare loopback
port, with the host config root sandboxed (``Path.home`` / ``CODEX_HOME``):

installer (the real ``host install`` verb) -> golden hook stdin events piped
through the REAL transformer process (``python -m mnemoseed_local.cli
_hook-event --host <host>``, the registered hook command shape) -> daemon-side
capture with host attribution and provenance asserted over public read
surfaces (``/memory/export``, the T2 serve) -> T1 session-start replay with
fence + signal line -> T2 recall after the user-ingest ack -> uninstall
restoring the host config exactly (idempotent).

The zcode chain uses the mid-session ``/flush`` rescue (zcode has no
SessionEnd); the codex chain proves the transcript-watermark idempotency on
the wire: a repeated Stop firing over one transcript state and the
SessionEnd replay leave exactly one stored turn, and a post-settle replay
fails open. The codex rollout is a real ``.jsonl.zst`` (zstandard, the dev
dependency) so the production decompression path is exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import auth_support
import pytest
from _ports import unused_loopback_port

from mnemoseed_local.cli import main
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.hosts.common.fence import RECALL_DISCLAIMER, RECALL_FENCE_CLOSE, RECALL_FENCE_OPEN
from mnemoseed_local.hosts.common.recall import CAPTURE_SIGNAL, T1_SIGNAL, T2_SIGNAL
from mnemoseed_local.rest_client import DaemonClient
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

PROFILE = "default"

ZC_SESSION_A = "zcode-e2e-alpha"
ZC_SESSION_B = "zcode-e2e-beta"
ZC_PROMPT_A = "上一轮我们把 FlakyClockHelper 的断言改成了注入时钟"
ZC_REPLY_A = "FlakyClockHelper 已改为注入时钟，用例稳定了。"
ZC_PROMPT_B = "FlakyClockHelper 现在还会偶发失败吗？帮我查一下。"

CX_SESSION_A = "codex-e2e-alpha"
CX_SESSION_B = "codex-e2e-beta"
CX_PROMPT_A = "把 WatermarkReplay 的水位幂等查清楚"
CX_REPLY_A = "WatermarkReplay 的重放由 daemon 水位去重，零重复入库。"
CX_PROMPT_B = "WatermarkReplay 还有哪些边界没测？"

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


@contextmanager
def _daemon(home: Path) -> Iterator[tuple[str, str]]:
    """The real daemon: isolated config home, spare port, live uvicorn server."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(_config_toml(home), encoding="utf-8")
    with auth_support.isolated_home(home):
        credential = auth_support.seed_credential(home)
        app = create_app()
        with auth_support.live_server(app, unused_loopback_port()) as base_url:
            yield base_url, credential


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[tuple[str, str, Path]]:
    home = tmp_path / "daemon-home"
    with _daemon(home) as (base_url, credential):
        yield base_url, credential, home


def _client(base_url: str, credential: str) -> DaemonClient:
    return DaemonClient(base_url=base_url, token=credential)


def _hook_env(base_url: str, daemon_home: Path, codex_home: Path | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNEMOSEED_LOCAL_")}
    env["MNEMOSEED_LOCAL_BASEURL"] = base_url
    env["MNEMOSEED_LOCAL_HOME"] = str(daemon_home)
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env


def _fire_hook(
    host: str, payload: dict[str, Any], base_url: str, daemon_home: Path, codex_home: Path | None = None
) -> dict[str, Any] | None:
    """One golden stdin event through the real transformer process.

    Returns the parsed stdout JSON payload (None when stdout stayed empty,
    the red-line default); a non-zero exit is a chain failure.
    """
    result = subprocess.run(
        [sys.executable, "-m", "mnemoseed_local.cli", "_hook-event", "--host", host],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        timeout=120,
        env=_hook_env(base_url, daemon_home, codex_home),
        shell=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    out = result.stdout.decode("utf-8", "replace").strip()
    return json.loads(out) if out else None


def _export(client: DaemonClient) -> dict[str, Any]:
    reply = client.post("/memory/export", {"profile_id": PROFILE})
    assert isinstance(reply, dict) and "chunks" in reply, reply
    return reply


def _flush(client: DaemonClient, session_id: str) -> None:
    reply = client.post("/flush", {"session_id": session_id, "profile_id": PROFILE})
    assert reply.get("status") == "flushed", reply


# ---------------------------------------------------------------- zcode chain


ZC_FOREIGN_CONFIG = {
    "theme": "dark",
    "hooks": {
        "enabled": False,
        "events": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-hook --flag"}]}]},
    },
    "mcp": {"servers": {"本地服务器": {"type": "stdio", "command": "npx", "args": ["-y", "some/server"]}}},
}


def _zc_payload(session_id: str, name: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"session_id": session_id, "cwd": "G:/dev/demo", "hook_event_name": name}
    payload.update(fields)
    return payload


def test_zcode_host_chain_end_to_end_on_a_real_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    daemon: tuple[str, str, Path],
) -> None:
    base_url, credential, daemon_home = daemon
    client = _client(base_url, credential)
    host_home = tmp_path / "host-home"
    monkeypatch.setattr(Path, "home", lambda: host_home)
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", base_url)
    config_path = host_home / ".zcode" / "cli" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    seeded = json.dumps(ZC_FOREIGN_CONFIG, indent=2, ensure_ascii=False) + "\n"
    config_path.write_text(seeded, encoding="utf-8")

    assert main(["host", "install", "--host", "zcode"]) == 0
    capsys.readouterr()
    installed = json.loads(config_path.read_text(encoding="utf-8"))
    assert installed["hooks"]["enabled"] is True
    assert set(installed["hooks"]["events"]) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
    assert installed["mcp"]["servers"]["mnemoseed-local"] == {
        "type": "http",
        "url": "http://localhost:7788/mcp",
    }
    assert (host_home / ".zcode" / "cli" / "mnemoseed-local.json").is_file()
    assert main(["host", "status", "--host", "zcode"]) == 0
    status = capsys.readouterr().out
    assert "host: installed" in status
    assert "reachable" in status

    # session alpha: user -> tool -> assistant tail through the real transformer
    first = _fire_hook(
        "zcode",
        _zc_payload(ZC_SESSION_A, "UserPromptSubmit", prompt=ZC_PROMPT_A),
        base_url,
        daemon_home,
    )
    assert first is not None
    assert first["hookSpecificOutput"]["additionalContext"] == CAPTURE_SIGNAL
    for payload in (
        _zc_payload(
            ZC_SESSION_A,
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "uv run pytest -q tests/test_decay_model.py"},
            tool_response={"stdout": "12 passed in 0.42s"},
        ),
        _zc_payload(
            ZC_SESSION_A,
            "Stop",
            transcript_path="",
            stop_hook_active=False,
            last_assistant_message=ZC_REPLY_A,
        ),
    ):
        assert _fire_hook("zcode", payload, base_url, daemon_home) is None

    # mid-session durability without a settle (zcode has no SessionEnd)
    _flush(client, ZC_SESSION_A)
    chunks = _export(client)["chunks"]
    assert chunks, "a flushed zcode turn must be stored"
    assert all(chunk["provenance"]["source"] == "zcode-chat" for chunk in chunks)
    assert all(chunk["provenance"]["session_id"] == ZC_SESSION_A for chunk in chunks)
    joined = "\n".join(chunk["text"] for chunk in chunks)
    assert f"user: {ZC_PROMPT_A}" in joined
    assert f"assistant: {ZC_REPLY_A}" in joined
    assert any("FlakyClockHelper" in chunk["cues"]["entities"] for chunk in chunks)

    # recall-surface provenance probe
    probe = client.post(
        "/ingest",
        {
            "host": "zcode",
            "event": "user_prompt",
            "session_id": "zcode-e2e-probe",
            "profile_id": PROFILE,
            "ts": 1.0,
            "content": {"text": "FlakyClockHelper 的修复是谁做的？"},
        },
    )
    assert probe.get("status") == "accepted", probe
    served = client.post(
        "/session/recall-pending",
        {"profile_id": PROFILE, "session_id": "zcode-e2e-probe", "seen_chunk_ids": []},
    )
    assert served["items"], served
    assert served["items"][0]["source"] == "zcode-chat"

    # session beta: T1 replay with fence + signal line, then T2 after the ack
    replay = _fire_hook(
        "zcode",
        _zc_payload(ZC_SESSION_B, "SessionStart", source="startup"),
        base_url,
        daemon_home,
    )
    assert replay is not None, "a served T1 replay must reach stdout"
    assert replay["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    t1 = replay["hookSpecificOutput"]["additionalContext"]
    assert t1.startswith(RECALL_FENCE_OPEN) and t1.endswith(RECALL_FENCE_CLOSE)
    assert t1.count(RECALL_FENCE_OPEN) == 1 and t1.count(RECALL_FENCE_CLOSE) == 1
    assert t1.splitlines()[1] == T1_SIGNAL
    assert RECALL_DISCLAIMER in t1
    assert '<session-tail id="alpha"' in t1
    assert ZC_REPLY_A in t1
    assert len(t1) <= 4000

    recall = _fire_hook(
        "zcode",
        _zc_payload(ZC_SESSION_B, "UserPromptSubmit", prompt=ZC_PROMPT_B),
        base_url,
        daemon_home,
    )
    assert recall is not None, "a served T2 recall must reach stdout"
    t2 = recall["hookSpecificOutput"]["additionalContext"]
    assert t2.splitlines()[0] == CAPTURE_SIGNAL
    assert T2_SIGNAL in t2
    assert t2.count(RECALL_FENCE_OPEN) == 1 and t2.count(RECALL_FENCE_CLOSE) == 1
    assert ZC_PROMPT_A in t2

    for payload in (
        _zc_payload(ZC_SESSION_B, "PostToolUse", tool_name="Bash", tool_input={}, tool_response={}),
        _zc_payload(
            ZC_SESSION_B,
            "Stop",
            transcript_path="",
            stop_hook_active=False,
            last_assistant_message="收到。",
        ),
    ):
        assert _fire_hook("zcode", payload, base_url, daemon_home) is None

    # uninstall restores the config exactly, idempotently
    assert main(["host", "uninstall", "--host", "zcode"]) == 0
    assert "applied" in capsys.readouterr().out
    assert json.loads(config_path.read_text(encoding="utf-8")) == ZC_FOREIGN_CONFIG
    assert "mnemoseed" not in config_path.read_text(encoding="utf-8")
    assert not (host_home / ".zcode" / "cli" / "mnemoseed-local.json").is_file()
    assert main(["host", "uninstall", "--host", "zcode"]) == 0
    assert "no change" in capsys.readouterr().out
    assert json.loads(config_path.read_text(encoding="utf-8")) == ZC_FOREIGN_CONFIG


# ---------------------------------------------------------------- codex chain


CX_FOREIGN_GROUP = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-own-hook --flag"}]}
CX_FOREIGN_HOOKS = {"hooks": {"Stop": [CX_FOREIGN_GROUP]}}
CX_FOREIGN_CONFIG_TOML = (
    "# user tuning\n"
    'model = "gpt-5-codex"\n'
    "\n"
    "[mcp_servers.other]\n"
    'command = "npx"\n'
    'args = ["-y", "some/server"]\n'
)


def _rollout_line(ordinal: int, kind: str, payload: dict[str, Any]) -> str:
    return json.dumps(
        {"ordinal": ordinal, "timestamp": "2026-10-24T10:00:00.000Z", "type": kind, "payload": payload},
        ensure_ascii=False,
    )


def _message_line(ordinal: int, role: str, text: str) -> str:
    return _rollout_line(
        ordinal,
        "response_item",
        {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        },
    )


def _write_rollout(path: Path, lines: list[str]) -> Path:
    zstandard = pytest.importorskip("zstandard")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstandard.ZstdCompressor().compress("\n".join(lines).encode("utf-8")))
    return path


def _cx_payload(session_id: str, name: str, transcript: Path, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": "G:/dev/demo",
        "hook_event_name": name,
        "model": "gpt-5-codex",
    }
    payload.update(fields)
    return payload


def test_codex_host_chain_end_to_end_on_a_real_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    daemon: tuple[str, str, Path],
) -> None:
    base_url, credential, daemon_home = daemon
    client = _client(base_url, credential)
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", base_url)
    hooks_path = codex_home / "hooks.json"
    config_path = codex_home / "config.toml"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(CX_FOREIGN_HOOKS, indent=2) + "\n", encoding="utf-8")
    config_path.write_text(CX_FOREIGN_CONFIG_TOML, encoding="utf-8")

    assert main(["host", "install", "--host", "codex_cli"]) == 0
    capsys.readouterr()
    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert set(installed) == {"hooks"}
    container = installed["hooks"]
    assert set(container) == {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}
    assert container["Stop"][0] == CX_FOREIGN_HOOKS["hooks"]["Stop"][0], "the foreign group survives install"
    assert main(["host", "status", "--host", "codex_cli"]) == 0
    status_out = capsys.readouterr().out
    assert "unverified" in status_out
    assert "/hooks" in status_out
    assert "[mcp_servers.mnemoseed]" in config_path.read_text(encoding="utf-8")

    rollout_a = _write_rollout(
        codex_home / "sessions" / "2026" / "10" / "24" / "rollout-alpha.jsonl.zst",
        [
            _rollout_line(0, "session_meta", {"type": "session_meta", "id": CX_SESSION_A}),
            _message_line(1, "user", CX_PROMPT_A),
            _rollout_line(2, "event_msg", {"type": "token_count"}),
            _message_line(3, "assistant", CX_REPLY_A),
        ],
    )
    rollout_b = _write_rollout(
        codex_home / "sessions" / "2026" / "10" / "24" / "rollout-beta.jsonl.zst",
        [
            _rollout_line(0, "session_meta", {"type": "session_meta", "id": CX_SESSION_B}),
            _message_line(1, "user", CX_PROMPT_B),
        ],
    )

    # session alpha: empty daemon T1 stays silent; first capture rides the signal
    assert (
        _fire_hook(
            "codex_cli",
            _cx_payload(CX_SESSION_A, "SessionStart", rollout_a, permission_mode="default", source="startup"),
            base_url,
            daemon_home,
            codex_home,
        )
        is None
    )
    acked = _fire_hook(
        "codex_cli",
        _cx_payload(
            CX_SESSION_A,
            "UserPromptSubmit",
            rollout_a,
            permission_mode="default",
            turn_id="turn-0001",
            prompt=CX_PROMPT_A,
        ),
        base_url,
        daemon_home,
        codex_home,
    )
    assert acked is not None
    assert acked["hookSpecificOutput"]["additionalContext"] == CAPTURE_SIGNAL
    stop_payload = _cx_payload(
        CX_SESSION_A,
        "Stop",
        rollout_a,
        permission_mode="default",
        turn_id="turn-0001",
        stop_hook_active=False,
        last_assistant_message=CX_REPLY_A,
    )
    assert _fire_hook("codex_cli", stop_payload, base_url, daemon_home, codex_home) is None
    assert _export(client)["paging"]["chunk_total"] == 0, "the ingest path stays submit-only"
    # the same transcript state fires again: the watermark keeps the daemon clean
    assert _fire_hook("codex_cli", stop_payload, base_url, daemon_home, codex_home) is None

    assert (
        _fire_hook(
            "codex_cli",
            _cx_payload(CX_SESSION_A, "SessionEnd", rollout_a, reason="other"),
            base_url,
            daemon_home,
            codex_home,
        )
        is None
    )
    chunks = _export(client)["chunks"]
    assert all(chunk["provenance"]["source"] == "codex_cli-chat" for chunk in chunks), chunks
    assert all(chunk["provenance"]["session_id"] == CX_SESSION_A for chunk in chunks)
    assert len(chunks) == 1, "the repeated firing and the settle replay must not duplicate the turn"
    stored = chunks[0]["text"]
    assert stored == f"user: {CX_PROMPT_A}\nassistant: {CX_REPLY_A}", stored
    assert stored.count("assistant: ") == 1

    # a post-settle replay fails open: exit 0, no stdout, nothing stored
    assert _fire_hook("codex_cli", stop_payload, base_url, daemon_home, codex_home) is None
    assert _export(client)["paging"]["chunk_total"] == 1

    # session beta: T1 replay, T2 recall, then the terminal settle
    replay = _fire_hook(
        "codex_cli",
        _cx_payload(CX_SESSION_B, "SessionStart", rollout_b, permission_mode="default", source="startup"),
        base_url,
        daemon_home,
        codex_home,
    )
    assert replay is not None, "a served T1 replay must reach stdout"
    assert replay["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    t1 = replay["hookSpecificOutput"]["additionalContext"]
    assert t1.startswith(RECALL_FENCE_OPEN) and t1.endswith(RECALL_FENCE_CLOSE)
    assert t1.count(RECALL_FENCE_OPEN) == 1 and t1.count(RECALL_FENCE_CLOSE) == 1
    assert t1.splitlines()[1] == T1_SIGNAL
    assert RECALL_DISCLAIMER in t1
    assert '<session-tail id="alpha"' in t1
    assert CX_PROMPT_A in t1
    assert len(t1) <= 4000

    recall = _fire_hook(
        "codex_cli",
        _cx_payload(
            CX_SESSION_B,
            "UserPromptSubmit",
            rollout_b,
            permission_mode="default",
            turn_id="turn-0001",
            prompt=CX_PROMPT_B,
        ),
        base_url,
        daemon_home,
        codex_home,
    )
    assert recall is not None, "a served T2 recall must reach stdout"
    t2 = recall["hookSpecificOutput"]["additionalContext"]
    assert t2.splitlines()[0] == CAPTURE_SIGNAL
    assert T2_SIGNAL in t2
    assert t2.count(RECALL_FENCE_OPEN) == 1 and t2.count(RECALL_FENCE_CLOSE) == 1
    assert CX_PROMPT_A in t2

    assert (
        _fire_hook(
            "codex_cli",
            _cx_payload(
                CX_SESSION_B,
                "Stop",
                rollout_b,
                permission_mode="default",
                turn_id="turn-0001",
                stop_hook_active=False,
                last_assistant_message=None,
            ),
            base_url,
            daemon_home,
            codex_home,
        )
        is None
    )
    assert (
        _fire_hook(
            "codex_cli",
            _cx_payload(CX_SESSION_B, "SessionEnd", rollout_b, reason="other"),
            base_url,
            daemon_home,
            codex_home,
        )
        is None
    )
    final = _export(client)
    assert final["paging"]["chunk_total"] == 2
    assert all(chunk["provenance"]["source"] == "codex_cli-chat" for chunk in final["chunks"])

    # uninstall removes exactly our entries; foreign bytes survive untouched
    assert main(["host", "uninstall", "--host", "codex_cli"]) == 0
    assert "applied" in capsys.readouterr().out
    assert json.loads(hooks_path.read_text(encoding="utf-8")) == CX_FOREIGN_HOOKS
    assert config_path.read_text(encoding="utf-8") == CX_FOREIGN_CONFIG_TOML
    assert main(["host", "status", "--host", "codex_cli"]) == 0
    assert "host: not-installed" in capsys.readouterr().out
    assert main(["host", "uninstall", "--host", "codex_cli"]) == 0
    assert "no change" in capsys.readouterr().out
