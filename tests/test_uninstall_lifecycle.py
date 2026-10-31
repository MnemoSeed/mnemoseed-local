"""Uninstall lifecycle: one ``host`` verb covers all four adapters (``hook``
stays the documented alias), plain ``uninstall`` detects installed adapters and
prints their exact removal commands while keeping every byte of data,
``--purge`` is explicit and confirmed and removes the detected adapters before
the data home, foreign config the tool does not own stays byte-identical, a
partial failure names the owned resources that remain instead of claiming
completion, and the package itself is never uninstalled by the CLI — the exact
package-removal command is printed as the final step.

Every test runs on a hermetic home (``Path.home`` plus both ``CONFIG_DIR``
namespaces) with the daemon reachability probe stubbed at the shared seam, so
no test touches a real host config or the network.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest

import mnemoseed_local.cli as cli
from mnemoseed_local.cli import main
from mnemoseed_local.hosts import install as opencode_install

_REAL_PROBE = opencode_install.daemon_reachable

HOME_ENV_VARS = (
    "OPENCODE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "CODEX_HOME",
    "MNEMOSEED_LOCAL_HOME",
    "MNEMOSEED_LOCAL_BASEURL",
    "MNEMOSEED_LOCAL_PROFILE_ID",
)

PACKAGE_COMMAND = "uv tool uninstall mnemoseed-local"

HOST_UNINSTALL_COMMANDS = {
    "claude_code": "mnemoseed-local host uninstall --host claude_code",
    "codex_cli": "mnemoseed-local host uninstall --host codex_cli",
    "opencode": "mnemoseed-local host uninstall --host opencode",
    "zcode": "mnemoseed-local host uninstall --host zcode",
}


@pytest.fixture
def lifecycle_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic app home at its default location under the hermetic profile:
    the adapter config roots and the data home resolve inside ``tmp_path``.
    The platform is pinned off Windows so the scheduled-task leg never runs."""
    home = tmp_path / ".mnemoseed-local"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("mnemoseed_local.cli.sys.platform", "linux")
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", home / "config.toml")
    for var in HOME_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda base_url, timeout=2.0: False)
    return home


def _install_opencode() -> Path:
    path, _ = opencode_install.install_plugin()
    return path


def _install_claude_code() -> Path:
    from mnemoseed_local.hosts.claude_code import install as claude_code

    path, _ = claude_code.install()
    return path


def _install_codex_cli() -> Path:
    from mnemoseed_local.hosts.codex_cli import install as codex_cli

    path, _ = codex_cli.install()
    return path


def _install_zcode() -> Path:
    from mnemoseed_local.hosts.zcode import install as zcode

    path, _ = zcode.install()
    return path


INSTALLERS = {
    "claude_code": _install_claude_code,
    "codex_cli": _install_codex_cli,
    "opencode": _install_opencode,
    "zcode": _install_zcode,
}


def _install_all_adapters() -> None:
    for installer in INSTALLERS.values():
        installer()


def _install_data_home() -> Path:
    home = cli.CONFIG_DIR
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text('preset = "embedded"\n', encoding="utf-8")
    (home / "project-bindings.json").write_text(
        json.dumps({"version": 1, "bindings": {}}) + "\n", encoding="utf-8"
    )
    return home


def _adapter_state(host: str) -> str:
    module = importlib.import_module(opencode_install.ADAPTER_MODULES[host])
    return module.status().state


# ------------------------------------------------- unified host verb (all four)


@pytest.mark.parametrize("host", ["claude_code", "codex_cli", "opencode", "zcode"])
def test_host_verb_uninstalls_all_four_adapters(lifecycle_home: Path, host: str) -> None:
    INSTALLERS[host]()
    assert main(["host", "uninstall", "--host", host]) == 0
    assert _adapter_state(host) == "not-installed"


def test_host_verb_installs_opencode(lifecycle_home: Path) -> None:
    assert main(["host", "install", "--host", "opencode"]) == 0
    assert _adapter_state("opencode") == "match"


def test_hook_alias_still_uninstalls_opencode(lifecycle_home: Path) -> None:
    INSTALLERS["opencode"]()
    assert main(["hook", "uninstall", "opencode"]) == 0
    assert _adapter_state("opencode") == "not-installed"


# ------------------------------------------------- detection report (keep data)


def test_plain_uninstall_prints_exact_commands_and_keeps_everything(
    lifecycle_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_all_adapters()
    _install_data_home()
    assert main(["uninstall"]) == 0
    out = capsys.readouterr().out
    for command in HOST_UNINSTALL_COMMANDS.values():
        assert command in out
    for host in INSTALLERS:
        assert _adapter_state(host) != "not-installed"
    assert lifecycle_home.exists()
    assert (lifecycle_home / "project-bindings.json").exists()
    assert "uninstall --purge" in out


def test_plain_uninstall_without_adapters_or_data_reports_nothing_to_remove(
    lifecycle_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["uninstall"]) == 0
    out = capsys.readouterr().out
    assert "no host adapters detected" in out
    assert "no mnemoseed-local data directory; nothing to uninstall" in out


# ------------------------------------------------- purge: adapters, then data


def test_purge_removes_detected_adapters_then_data(
    lifecycle_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_all_adapters()
    _install_data_home()
    assert main(["uninstall", "--purge", "--yes"]) == 0
    out = capsys.readouterr().out
    for host in INSTALLERS:
        assert _adapter_state(host) == "not-installed"
    assert not lifecycle_home.exists()
    assert not (tmp_path / ".zcode" / "cli" / "mnemoseed-local.json").exists()
    assert PACKAGE_COMMAND in out


def test_purge_with_confirmation_removes(lifecycle_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    INSTALLERS["opencode"]()
    _install_data_home()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(["uninstall", "--purge"]) == 0
    assert _adapter_state("opencode") == "not-installed"
    assert not lifecycle_home.exists()


def test_purge_without_confirmation_is_refused_and_removes_nothing(
    lifecycle_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    INSTALLERS["opencode"]()
    _install_data_home()
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main(["uninstall", "--purge"]) == 0
    out = capsys.readouterr().out
    assert "purge cancelled" in out
    assert _adapter_state("opencode") != "not-installed"
    assert lifecycle_home.exists()
    assert PACKAGE_COMMAND not in out


def test_purge_with_closed_stdin_is_refused(
    lifecycle_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    INSTALLERS["opencode"]()
    _install_data_home()

    def closed_input(prompt: object) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed_input)
    assert main(["uninstall", "--purge"]) == 0
    assert "purge cancelled" in capsys.readouterr().out
    assert _adapter_state("opencode") != "not-installed"
    assert lifecycle_home.exists()


# ------------------------------------------------- foreign config stays intact


def test_purge_preserves_foreign_config_byte_identical(lifecycle_home: Path) -> None:
    home = Path.home()
    foreign_plugin = home / ".config" / "opencode" / "plugin" / "other-tool.ts"
    foreign_plugin.parent.mkdir(parents=True, exist_ok=True)
    foreign_plugin.write_bytes(b"// foreign plugin\n")
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "other-tool run"}]},
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    foreign_hooks = codex_home / "hooks.json"
    foreign_hooks.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"matcher": "x", "hooks": [{"type": "command", "command": "other"}]}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    foreign_toml = codex_home / "config.toml"
    foreign_toml.write_text('[model]\nname = "gpt-5"\n', encoding="utf-8")
    zcode_config = home / ".zcode" / "cli" / "config.json"
    zcode_config.parent.mkdir(parents=True, exist_ok=True)
    zcode_config.write_text(
        json.dumps({"mcp": {"servers": {"other": {"type": "stdio", "command": "other", "args": ["x"]}}}})
        + "\n",
        encoding="utf-8",
    )
    # foreign-only adapters are never detected, so their files must not be
    # rewritten at all; the claude settings file gains our entries below, so it
    # is checked value-level (the foreign group survives the removal)
    untouched = (foreign_plugin, foreign_hooks, foreign_toml, zcode_config)
    before = {path: path.read_bytes() for path in untouched}

    INSTALLERS["opencode"]()
    INSTALLERS["claude_code"]()
    _install_data_home()
    assert main(["uninstall", "--purge", "--yes"]) == 0

    for path, data in before.items():
        assert path.read_bytes() == data, path
    # a foreign hook group sharing a file with our entries survives the removal
    merged = json.loads(settings.read_text(encoding="utf-8"))
    commands = [handler["command"] for group in merged["hooks"]["Stop"] for handler in group["hooks"]]
    assert "other-tool run" in commands
    assert not any("mnemoseed-local" in command for command in commands)


# ------------------------------------------------- honest partial failure


def test_partial_failure_names_residual_owned_resources(
    lifecycle_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mnemoseed_local.hosts.claude_code import install as claude_code

    INSTALLERS["claude_code"]()
    INSTALLERS["opencode"]()
    _install_data_home()

    def refusing_uninstall(path: Path | None = None) -> tuple[Path, bool]:
        raise claude_code.SettingsParseError("settings.json is not strict JSON")

    monkeypatch.setattr(claude_code, "uninstall", refusing_uninstall)
    assert main(["uninstall", "--purge", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "owns resources that remain" in captured.err
    assert "claude_code" in captured.err
    assert HOST_UNINSTALL_COMMANDS["claude_code"] in captured.err
    assert PACKAGE_COMMAND not in captured.out
    assert _adapter_state("claude_code") != "not-installed"
    assert _adapter_state("opencode") == "not-installed"
    assert not lifecycle_home.exists()


# ------------------------------------------------- non-default data home safety


def test_non_default_data_home_defers_adapter_removal(
    lifecycle_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    INSTALLERS["zcode"]()
    isolated = tmp_path / "isolated-home"
    isolated.mkdir()
    (isolated / "config.toml").write_text('preset = "embedded"\n', encoding="utf-8")
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", isolated)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", isolated)

    assert main(["uninstall", "--purge", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "NOT auto-removed" in captured.out
    assert HOST_UNINSTALL_COMMANDS["zcode"] in captured.out
    assert PACKAGE_COMMAND not in captured.out
    assert _adapter_state("zcode") != "not-installed"
    assert not isolated.exists()


# ------------------------------------------------- package removal handoff


def test_package_removal_is_printed_never_claimed(
    lifecycle_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_data_home()
    assert main(["uninstall", "--purge", "--yes"]) == 0
    out = capsys.readouterr().out
    assert PACKAGE_COMMAND in out
    assert "not run by this command" in out


def test_recorded_failure_blocks_the_handoff_even_when_detection_reads_clean(
    lifecycle_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A removal that died between its own two atomic writes (the codex hooks
    file landed, the config.toml rewrite did not) leaves an owned MCP table
    that handler-based detection cannot see. The recorded failure must block
    the completion handoff on its own — a clean re-detection proves nothing."""
    from mnemoseed_local.hosts.codex_cli import install as codex_cli

    INSTALLERS["codex_cli"]()
    _install_data_home()

    real_write = codex_cli.write_text_atomic

    def failing_config_write(target: Path, text: str, **kwargs: object) -> bool:
        if target.name == codex_cli.CONFIG_FILENAME:
            raise OSError("config.toml rewrite interrupted")
        return real_write(target, text, **kwargs)

    monkeypatch.setattr(codex_cli, "write_text_atomic", failing_config_write)
    assert main(["uninstall", "--purge", "--yes"]) == 1
    captured = capsys.readouterr()
    # the defect's precondition: re-detection reads the adapter as gone
    assert _adapter_state("codex_cli") == "not-installed"
    assert "owns resources that remain" in captured.err
    assert HOST_UNINSTALL_COMMANDS["codex_cli"] in captured.err
    assert PACKAGE_COMMAND not in captured.out
    assert not lifecycle_home.exists()


def test_hidden_hook_event_fails_open_for_opencode(
    lifecycle_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hidden transformer verb accepts opencode (the dispatch table is
    shared) and fails open: empty stdout, an honest stderr note, exit 0."""
    assert main(["_hook-event", "--host", "opencode"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "opencode transformer unavailable" in captured.err


# ------------------------------------------------- detection never dials out


def test_detection_probes_never_target_the_daemon(
    lifecycle_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    INSTALLERS["opencode"]()
    urls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        urls.append(url)
        raise AssertionError("detection must not perform a live probe")

    monkeypatch.setattr(opencode_install, "daemon_reachable", _REAL_PROBE)
    monkeypatch.setattr(httpx, "get", fake_get)
    assert opencode_install.detect_installed_hosts() == ("opencode",)
    # every adapter's structural probe aims at the unroutable loopback base
    assert set(urls) == {"http://127.0.0.1:9/healthz"}
