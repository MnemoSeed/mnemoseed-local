"""History import CLI (#394 S4): the top-level verb, preflights, and the
stderr-progress / stdout-report split."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import zstandard

from mnemoseed_local import cli
from mnemoseed_local.importer import runner
from mnemoseed_local.rest_client import DaemonUnavailableError

RUN_A = "c0de5e55-0000-4000-8000-000000000001"


def _write_rollout(root: Path, *, compressed: bool = False) -> Path:
    name = f"rollout-2026-09-10t10-00-00-000000-{RUN_A}.jsonl"
    path = root / "2026" / "09" / "10" / (name + ".zst" if compressed else name)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"session_id": "conv-7"}}),
        json.dumps(
            {
                "ordinal": 1,
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"text": "u0"}]},
            }
        ),
    ]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(zstandard.ZstdCompressor().compress(data) if compressed else data)
    return path


class _FakeClient:
    def __init__(self, *, unreachable: bool = False) -> None:
        self.unreachable = unreachable
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path))
        if self.unreachable and path == "/healthz":
            raise DaemonUnavailableError("daemon down")
        if path == "/session/active":
            return {"sessions": []}
        return {}

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", path))
        if path == "/session/end":
            return {"status": "settled", "new_chunk": 1, "pool_points_suppressed": 0.5}
        return {"status": "accepted"}


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    def fake_client_for(
        base_url: str,
        *,
        profile_id: str,
        actor: str,
        timeout: float,
        token: str | None = None,
    ) -> _FakeClient:
        return client

    monkeypatch.setattr(runner, "client_for", fake_client_for)


def _no_http(*args: object, **kwargs: object) -> None:
    raise AssertionError("dry-run / preflight must not issue HTTP")


def test_import_help_lists_codex_cli(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["import", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--host" in out
    assert "codex_cli" in out
    for flag in ("--source", "--profile", "--limit", "--dry-run", "--json", "--yes"):
        assert flag in out


def test_unknown_host_exits_nonzero_with_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["import", "--host", "bogus"])
    assert exc.value.code != 0
    assert "usage:" in capsys.readouterr().err


def test_dry_run_makes_zero_http_and_reports_turn_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "home"))
    root = tmp_path / "sessions"
    _write_rollout(root)
    monkeypatch.setattr(httpx, "get", _no_http)
    monkeypatch.setattr(httpx, "post", _no_http)
    assert (
        cli.main(
            [
                "import",
                "--host",
                "codex_cli",
                "--source",
                str(root),
                "--profile",
                "prof",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["turns_ingested"] == 1
    assert report["sessions_imported"] == 0
    assert not (tmp_path / "home" / "import-ledger-codex_cli.json").exists()


def test_missing_zstandard_fails_before_posts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "home"))
    root = tmp_path / "sessions"
    _write_rollout(root, compressed=True)
    monkeypatch.setattr(runner, "find_spec", lambda name: None)
    monkeypatch.setattr(httpx, "get", _no_http)
    monkeypatch.setattr(httpx, "post", _no_http)
    assert cli.main(["import", "--host", "codex_cli", "--source", str(root), "--yes"]) == 1


def test_missing_zstandard_message_names_codex_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", "http://localhost:7788")
    root = tmp_path / "sessions"
    _write_rollout(root, compressed=True)
    monkeypatch.setattr(runner, "find_spec", lambda name: None)
    monkeypatch.setattr(httpx, "get", _no_http)
    assert cli.main(["import", "--host", "codex_cli", "--source", str(root), "--yes"]) == 1
    assert "[codex]" in capsys.readouterr().err


def test_unreachable_daemon_leaves_ledger_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    root = tmp_path / "sessions"
    _write_rollout(root)
    _patch_client(monkeypatch, _FakeClient(unreachable=True))
    assert cli.main(["import", "--host", "codex_cli", "--source", str(root), "--yes"]) == 1
    assert not (home / "import-ledger-codex_cli.json").exists()


def test_non_tty_requires_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "home"))
    root = tmp_path / "sessions"
    _write_rollout(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["import", "--host", "codex_cli", "--source", str(root)]) == 1


def test_import_ledger_file_is_scoped_to_the_requested_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", "http://localhost:7788")
    root = tmp_path / "sessions"
    _write_rollout(root)
    _patch_client(monkeypatch, _FakeClient())
    assert (
        cli.main(
            [
                "import",
                "--host",
                "codex_cli",
                "--source",
                str(root),
                "--profile",
                "alice",
                "--yes",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (home / "import-ledger-codex_cli-alice.json").exists()
    assert not (home / "import-ledger-codex_cli.json").exists()


def test_progress_on_stderr_report_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", "http://localhost:7788")
    root = tmp_path / "sessions"
    _write_rollout(root)
    _patch_client(monkeypatch, _FakeClient())
    assert cli.main(["import", "--host", "codex_cli", "--source", str(root), "--yes"]) == 0
    captured = capsys.readouterr()
    assert "1/1" in captured.err
    assert "sessions_scanned: 1" in captured.out
    assert "pool_points_suppressed: 0.5" in captured.out
