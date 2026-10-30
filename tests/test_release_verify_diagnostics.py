"""Release-verify diagnostics: pass records observe preserved state and count fake-daemon requests."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release_install_verifier", Path(__file__).resolve().parents[1] / "scripts" / "verify_release_install.py"
)
assert _SPEC is not None and _SPEC.loader is not None
release = sys.modules.get(_SPEC.name)
if release is None:
    release = importlib.util.module_from_spec(_SPEC)
    sys.modules[_SPEC.name] = release
    _SPEC.loader.exec_module(release)

_OVERWRITE_CONFIG = """\
preset = "embedded"

[dream]
auto_trigger = true
"""


def _context(tmp_path: Path) -> Any:
    tool_dir = tmp_path / "tools"
    bin_dir = tmp_path / "tool-bin"
    bin_name = "Scripts" if os.name == "nt" else "bin"
    python_name = "python.exe" if os.name == "nt" else "python"
    return release.Context(
        repo_root=tmp_path / "checkout",
        work_root=tmp_path / "work",
        uv=tmp_path / "uv",
        version="0.2.0",
        dist_dir=tmp_path / "dist",
        wheel=tmp_path / "dist" / "artifact.whl",
        sdist=tmp_path / "dist" / "artifact.tar.gz",
        tool_dir=tool_dir,
        tool_bin=bin_dir,
        tool_python=tool_dir / release.PROJECT / bin_name / python_name,
        cli=bin_dir / "mnemoseed-local",
    )


def _ping_healthz(baseurl: str) -> None:
    """One direct loopback GET /healthz, the request a host status check sends to the daemon."""
    parts = urlsplit(baseurl)
    connection = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=10)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200
        response.read()
    finally:
        connection.close()


@dataclass
class ConfigFalse:
    context: Any
    verifier: Any
    config_file: Path
    overwrite_on_init: bool = False


@pytest.fixture
def config_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConfigFalse:
    context = _context(tmp_path)
    verifier = release.Verifier(tmp_path / "report.json")
    config_file = context.work_root / "config-explicit-false" / ".mnemoseed-local" / "config.toml"
    probe = ConfigFalse(context=context, verifier=verifier, config_file=config_file)

    def run(argv: Sequence[object], **kwargs: object) -> Any:
        command = [str(item) for item in argv]
        if command[:2] == [str(context.installed_cli), "init"]:
            if probe.overwrite_on_init:
                config_file.write_text(_OVERWRITE_CONFIG, encoding="utf-8", newline="\n")
            output = "config already exists; keeping the current bytes\n"
            return release.Run(argv=command, exit_code=1, stdout=output, stderr="", duration_s=0.0)
        assert command[:3] == [str(context.installed_python), "-I", "-c"]
        data = tomllib.loads(config_file.read_text(encoding="utf-8"))
        payload = {
            "version": context.version,
            "config_path": str(config_file),
            "preset": str(data.get("preset", "")),
            "baseurl": str(data.get("baseurl", "")),
            "dream_auto_trigger": False,
            "capture_auto_recall": False,
            "isolated_graph": True,
        }
        return release.Run(
            argv=command,
            exit_code=0,
            stdout=json.dumps(payload) + "\n",
            stderr="",
            duration_s=0.0,
        )

    monkeypatch.setattr(verifier, "run", run)
    return probe


def test_fake_healthz_counts_requests_received(tmp_path: Path) -> None:
    fake = release._FakeHealthz()
    port = fake.start()
    try:
        assert port not in release.FORBIDDEN_PORTS
        _ping_healthz(f"http://127.0.0.1:{port}")
        _ping_healthz(f"http://127.0.0.1:{port}")
        assert fake.requests_received == 2
    finally:
        fake.stop()


def test_explicit_false_pass_observes_preserved_bytes(config_false: ConfigFalse) -> None:
    release.proof_config_explicit_false(config_false.verifier, config_false.context)
    records = config_false.verifier.report["assertions"]
    byte_check = next(
        record for record in records if record.get("check") == "the explicit-false config bytes are unchanged"
    )
    assert byte_check["ok"] is True
    expected_detail = f"config.toml bytes preserved ({config_false.config_file.stat().st_size})"
    assert byte_check["detail"] == expected_detail
    assert "config.toml was rewritten" not in json.dumps(config_false.verifier.report)


def test_explicit_false_failure_keeps_rewrite_wording(config_false: ConfigFalse) -> None:
    config_false.overwrite_on_init = True
    with pytest.raises(release.Failure) as raised:
        release.proof_config_explicit_false(config_false.verifier, config_false.context)
    assert "config.toml was rewritten" in str(raised.value)


@dataclass
class CodexSandbox:
    context: Any
    verifier: Any
    corrupt_mnemoseed_on_install: bool = False


@pytest.fixture
def codex_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexSandbox:
    context = _context(tmp_path)
    verifier = release.Verifier(tmp_path / "report.json")
    sandbox = CodexSandbox(context=context, verifier=verifier)
    home = context.work_root / "codex"
    codex_config = home / "codex-home" / "config.toml"
    hooks_file = home / "codex-home" / "hooks.json"
    mnemoseed_config = home / "mnemoseed-home" / "config.toml"
    shim = str(context.installed_cli.resolve())
    marker_command = f"{shim} {release.HOOK_TAIL}"
    status_count = {"runs": 0}

    def baseurl() -> str:
        data = tomllib.loads(mnemoseed_config.read_text(encoding="utf-8"))
        return str(data["baseurl"])

    def write_installed_files() -> None:
        hooks: dict[str, Any] = {"hooks": {}}
        for event in release.HOOK_EVENTS:
            groups: list[dict[str, Any]] = []
            if event == "Stop":
                groups.append(release._FOREIGN_HOOKS["hooks"]["Stop"][0])
            groups.append({"matcher": "*", "hooks": [{"type": "command", "command": marker_command}]})
            hooks["hooks"][event] = groups
        hooks["third_party"] = {"keep": True}
        hooks_file.write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8", newline="\n")
        codex_config.write_text(
            f'model = "gpt-5-codex"\n\n[mcp_servers.mnemoseed]\n'
            f'command = "{Path(shim).as_posix()}"\nargs = ["mcp"]\n',
            encoding="utf-8",
            newline="\n",
        )

    def restore_foreign_files() -> None:
        codex_config.write_text(release._CODEX_FOREIGN_CONFIG, encoding="utf-8", newline="\n")
        hooks_file.write_text(
            json.dumps(release._FOREIGN_HOOKS, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def run(argv: Sequence[object], **kwargs: object) -> Any:
        command = [str(item) for item in argv]
        if any(release.HOOK_TAIL in part for part in command):
            return release.Run(argv=command, exit_code=0, stdout="", stderr="", duration_s=0.0)
        assert command[:2] == [str(context.installed_cli), "host"]
        action = command[2]
        if action == "status":
            url = baseurl()
            _ping_healthz(url)
            installed = status_count["runs"] == 1
            status_count["runs"] += 1
            state = (
                "host: installed, trust unverified (review and approve the hooks in Codex via /hooks)"
                if installed
                else "host: not-installed"
            )
            output = f"{state}\ndaemon: reachable at {url}\n"
            return release.Run(argv=command, exit_code=0, stdout=output, stderr="", duration_s=0.0)
        if action == "install":
            write_installed_files()
            if sandbox.corrupt_mnemoseed_on_install:
                touched = mnemoseed_config.read_text(encoding="utf-8") + "# touched\n"
                mnemoseed_config.write_text(touched, encoding="utf-8", newline="\n")
            output = "hooks installed; review and trust them in Codex via /hooks\n"
            return release.Run(argv=command, exit_code=0, stdout=output, stderr="", duration_s=0.0)
        assert action == "uninstall"
        restore_foreign_files()
        return release.Run(argv=command, exit_code=0, stdout="hooks removed\n", stderr="", duration_s=0.0)

    monkeypatch.setattr(verifier, "run", run)
    return sandbox


def test_codex_sandbox_records_health_requests_for_each_status_check(codex_sandbox: CodexSandbox) -> None:
    release.proof_codex_sandbox(codex_sandbox.verifier, codex_sandbox.context)
    counts = codex_sandbox.verifier.report["health_requests"]
    assert counts == {"before_install": 1, "after_install": 1, "after_uninstall": 1}
    assert codex_sandbox.verifier.report["health_requests_note"] == "per-checkpoint delta, not cumulative"


def test_codex_sandbox_failure_keeps_changed_wording(codex_sandbox: CodexSandbox) -> None:
    codex_sandbox.corrupt_mnemoseed_on_install = True
    with pytest.raises(release.Failure) as raised:
        release.proof_codex_sandbox(codex_sandbox.verifier, codex_sandbox.context)
    assert "config.toml changed" in str(raised.value)
