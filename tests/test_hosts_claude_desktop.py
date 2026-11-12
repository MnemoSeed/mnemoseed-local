"""Claude Desktop host adapter: the MCP server entry in the app's own config.

The config is ``claude_desktop_config.json`` inside the directory the app really
reads: an explicit override, then the MSIX package directory on Windows, then the
platform's classic path. Resolution is by directory existence, because a package
directory that exists while its config file is not yet written is the live
surface — judging by file existence alone writes a config the app never reads.
Every rewrite is strict-JSON surgery that refuses rather than trusting a file it
cannot parse, keeps foreign keys byte-for-byte, and lands atomically.

Every test runs on a hermetic surface: the override names a temp file, or a fake
platform layout stands in, so no test can reach a real Claude Desktop config.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from mnemoseed_local.cli import main
from mnemoseed_local.hosts import install as shared
from mnemoseed_local.hosts.claude_desktop import install as cd_install
from mnemoseed_local.mcp_gateway import instructions

EXISTING_CONFIG = {
    "preferences": {
        "quickEntryShortcut": "Alt+Space",
        "allowedWorkspaces": ["G:/Development"],
    },
    "globalShortcut": "Ctrl+Alt+C",
    "mcpServers": {
        "foreign-server": {"command": "C:/tools/foreign.exe", "args": ["serve"]},
    },
}

#: The unroutable base the hermetic local home points at, so a status call can
#: never reach a daemon that happens to be running on this machine.
UNROUTABLE_BASE_URL = "http://127.0.0.1:9"

HERMETIC_ENV_VARS = ("MNEMOSEED_CLAUDE_DESKTOP_CONFIG", "LOCALAPPDATA", "APPDATA", "XDG_CONFIG_HOME")


def _write_config(target: Path, payload: object, *, text: str | None = None) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    body = text if text is not None else json.dumps(payload, indent=2) + "\n"
    target.write_text(body, encoding="utf-8")
    return hashlib.md5(target.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits the config override from the ambient environment."""
    monkeypatch.delenv(cd_install.CONFIG_OVERRIDE_ENV, raising=False)


@pytest.fixture
def cd_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic surface: the explicit override names a temp config file, the local
    home is a temp directory aimed at an unroutable base, and no real user
    directory is reachable from the adapter."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for var in HERMETIC_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    local_home = tmp_path / "mnemoseed-home"
    local_home.mkdir()
    (local_home / "config.toml").write_text(
        f'baseurl = "{UNROUTABLE_BASE_URL}"\npreset = "embedded"\n', encoding="utf-8"
    )
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", local_home)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", local_home / "config.toml")
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(local_home))
    target = tmp_path / "Claude" / cd_install.CONFIG_FILENAME
    monkeypatch.setenv(cd_install.CONFIG_OVERRIDE_ENV, str(target))
    return target


@pytest.fixture
def installed_executable(monkeypatch: pytest.MonkeyPatch) -> str:
    """The executable-resolution seam: the write path under test, with the
    installed-runtime gate answered instead of guessed."""
    resolved = str(Path(sys.executable).resolve())
    monkeypatch.setattr(cd_install, "resolve_executable", lambda: resolved)
    return resolved


@pytest.fixture
def windows_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A fake Windows layout: the MSIX root and the classic config directory."""
    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setattr(cd_install, "_platform", lambda: "windows")
    return local / cd_install.MSIX_ROOT_NAME, roaming / "Claude"


# ---------------------------------------------------------------- merge semantics


def test_install_merges_only_the_mcp_entry(cd_surface: Path, installed_executable: str) -> None:
    _write_config(cd_surface, EXISTING_CONFIG)

    target, changed = cd_install.install()

    assert target == cd_surface
    assert changed is True
    written = json.loads(cd_surface.read_text(encoding="utf-8"))
    assert written["mcpServers"]["mnemoseed"] == {"command": installed_executable, "args": ["mcp"]}
    assert written["preferences"] == EXISTING_CONFIG["preferences"]
    assert written["globalShortcut"] == EXISTING_CONFIG["globalShortcut"]
    assert written["mcpServers"]["foreign-server"] == EXISTING_CONFIG["mcpServers"]["foreign-server"]


def test_install_is_idempotent(cd_surface: Path, installed_executable: str) -> None:
    _write_config(cd_surface, EXISTING_CONFIG)
    cd_install.install()
    before = cd_surface.read_bytes()

    _, changed = cd_install.install()

    assert changed is False
    assert cd_surface.read_bytes() == before


def test_install_creates_the_config_when_absent(cd_surface: Path, installed_executable: str) -> None:
    assert not cd_surface.exists()

    _, changed = cd_install.install()

    assert changed is True
    assert json.loads(cd_surface.read_text(encoding="utf-8")) == {
        "mcpServers": {"mnemoseed": {"command": installed_executable, "args": ["mcp"]}}
    }
    assert cd_surface.parent.is_dir()


def test_the_written_command_is_an_absolute_path_with_the_gateway_args(
    cd_surface: Path, installed_executable: str
) -> None:
    """A chat app spawns the command itself: the entry carries an absolute path
    and the single gateway argument, never a bare executable name."""
    cd_install.install()

    entry = json.loads(cd_surface.read_text(encoding="utf-8"))["mcpServers"]["mnemoseed"]
    assert Path(entry["command"]).is_absolute()
    assert Path(entry["command"]).is_file()
    assert entry["args"] == ["mcp"]


# ---------------------------------------------------------------- refusal


def test_non_strict_json_refuses_and_leaves_the_file_untouched(
    cd_surface: Path, installed_executable: str
) -> None:
    digest = _write_config(cd_surface, None, text='{\n  // user annotation\n  "preferences": {}\n}\n')

    with pytest.raises(cd_install.ConfigParseError):
        cd_install.install()

    assert hashlib.md5(cd_surface.read_bytes()).hexdigest() == digest


def test_a_malformed_mcp_servers_value_is_refused_not_overwritten(
    cd_surface: Path, installed_executable: str
) -> None:
    digest = _write_config(cd_surface, {"mcpServers": ["not", "an", "object"], "preferences": {}})

    with pytest.raises(cd_install.ConfigParseError):
        cd_install.install()

    assert hashlib.md5(cd_surface.read_bytes()).hexdigest() == digest


def test_an_empty_config_file_installs_cleanly(cd_surface: Path, installed_executable: str) -> None:
    _write_config(cd_surface, None, text="")

    _, changed = cd_install.install()

    assert changed is True
    assert "mnemoseed" in json.loads(cd_surface.read_text(encoding="utf-8"))["mcpServers"]


def test_the_cli_reports_a_refusal_instead_of_a_traceback(
    cd_surface: Path, installed_executable: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(cd_surface, None, text="{broken")

    assert main(["host", "install", "--host", "claude_desktop"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "manually" in captured.err


# ---------------------------------------------------------------- the executable resolver


def test_a_development_run_refuses_the_install(cd_surface: Path) -> None:
    """The real predicate on the real tree: this suite runs from a checkout, and
    a checkout's build interpreter is not a command Claude Desktop can spawn."""
    assert cd_install._installed_runtime() is False

    with pytest.raises(cd_install.ClaudeDesktopConfigError) as refusal:
        cd_install.install()

    assert "uv tool install" in str(refusal.value)
    assert not cd_surface.exists()


def test_a_frozen_bundle_is_an_installed_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert cd_install._installed_runtime() is True
    assert cd_install.resolve_executable() == str(Path(sys.executable).resolve())


# ---------------------------------------------------------------- status


def test_status_tracks_not_installed_installed_and_stale(cd_surface: Path, installed_executable: str) -> None:
    assert cd_install.status().state == "not-installed"

    cd_install.install()
    assert cd_install.status().state == "installed"

    written = json.loads(cd_surface.read_text(encoding="utf-8"))
    written["mcpServers"]["mnemoseed"]["command"] = "C:/gone/away/python.exe"
    _write_config(cd_surface, written)
    assert cd_install.status().state == "stale"


def test_stale_is_repaired_by_reinstalling(
    cd_surface: Path, installed_executable: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stale state is the only automatic detector for an entry that points at
    a removed environment; re-running install is the whole repair."""
    _write_config(cd_surface, EXISTING_CONFIG)
    cd_install.install()
    written = json.loads(cd_surface.read_text(encoding="utf-8"))
    written["mcpServers"]["mnemoseed"]["command"] = str(cd_surface.parent / "old-venv" / "python.exe")
    _write_config(cd_surface, written)
    assert cd_install.status().state == "stale"

    assert main(["host", "install", "--host", "claude_desktop"]) == 0

    assert cd_install.status().state == "installed"
    assert "mcp: applied" in capsys.readouterr().out


def test_unreadable_and_stale_never_cross_report(cd_surface: Path, installed_executable: str) -> None:
    """A file that cannot be parsed is ``unreadable`` whatever its bytes look
    like; a parsable file whose command differs is ``stale``. Neither state is
    ever the other's verdict."""
    _write_config(cd_surface, None, text='{"mcpServers": {"mnemoseed": {"command": "old/python.exe"')
    assert cd_install.status().state == "unreadable"

    _write_config(cd_surface, {"mcpServers": {"mnemoseed": {"command": "old/python.exe", "args": ["mcp"]}}})
    assert cd_install.status().state == "stale"

    _write_config(cd_surface, {"mcpServers": {"mnemoseed": {"url": "http://127.0.0.1:7788/mcp"}}})
    assert cd_install.status().state == "unreadable"

    _write_config(cd_surface, {"mcpServers": "not an object"})
    assert cd_install.status().state == "unreadable"


def test_status_keeps_the_shared_probe_and_base_url(
    cd_surface: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_probe(base_url: str, timeout: float = 2.0) -> bool:
        seen["base_url"] = base_url
        return True

    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", fake_probe)
    info = cd_install.status()
    assert info.daemon_reachable is True
    assert seen["base_url"] == "http://localhost:7788"
    assert info.path == cd_surface


# ---------------------------------------------------------------- uninstall


def test_uninstall_removes_only_our_entry(cd_surface: Path, installed_executable: str) -> None:
    _write_config(cd_surface, EXISTING_CONFIG)
    cd_install.install()

    target, existed = cd_install.uninstall()

    assert (target, existed) == (cd_surface, True)
    written = json.loads(cd_surface.read_text(encoding="utf-8"))
    assert "mnemoseed" not in written["mcpServers"]
    assert written["mcpServers"]["foreign-server"] == EXISTING_CONFIG["mcpServers"]["foreign-server"]
    assert written["preferences"] == EXISTING_CONFIG["preferences"]
    assert cd_install.status().state == "not-installed"


def test_uninstall_drops_the_servers_key_when_it_empties(cd_surface: Path, installed_executable: str) -> None:
    cd_install.install()

    cd_install.uninstall()

    assert "mcpServers" not in json.loads(cd_surface.read_text(encoding="utf-8"))


def test_uninstall_on_an_absent_file_reports_nothing(cd_surface: Path) -> None:
    target, existed = cd_install.uninstall()

    assert (target, existed) == (cd_surface, False)
    assert not cd_surface.exists()


# ---------------------------------------------------------------- path resolution


def test_the_override_wins_over_every_platform_candidate(
    cd_surface: Path, windows_layout: tuple[Path, Path]
) -> None:
    msix_root, classic = windows_layout
    (msix_root / "claude_pkg").mkdir(parents=True)
    classic.mkdir(parents=True)

    assert cd_install.config_path() == cd_surface


def test_the_msix_package_directory_wins_on_windows(
    windows_layout: tuple[Path, Path],
) -> None:
    """The package directory is the surface the app reads even before a config
    file exists there."""
    msix_root, classic = windows_layout
    package = msix_root / "claude_pzs8sxrjxfjjc"
    package.mkdir(parents=True)
    classic.mkdir(parents=True)

    assert cd_install.config_path() == package / cd_install.CONFIG_FILENAME


def test_the_newest_package_directory_wins_when_several_exist(
    windows_layout: tuple[Path, Path],
) -> None:
    msix_root, _ = windows_layout
    older = msix_root / "claude_older"
    newer = msix_root / "claude_newer"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    os.utime(older, (1_600_000_000, 1_600_000_000))
    os.utime(newer, (1_700_000_000, 1_700_000_000))

    assert cd_install.config_path() == newer / cd_install.CONFIG_FILENAME


def test_several_package_directories_with_a_config_refuse_with_guidance(
    windows_layout: tuple[Path, Path], installed_executable: str
) -> None:
    """Two live config files mean the surface cannot be decided: no guess, no
    write, and an explicit pointer at the override."""
    msix_root, _ = windows_layout
    first = msix_root / "claude_one"
    second = msix_root / "claude_two"
    _write_config(first / cd_install.CONFIG_FILENAME, {"preferences": {"first": True}})
    _write_config(second / cd_install.CONFIG_FILENAME, {"preferences": {"second": True}})

    with pytest.raises(cd_install.ClaudeDesktopConfigError) as refusal:
        cd_install.install()

    assert cd_install.CONFIG_OVERRIDE_ENV in str(refusal.value)
    assert json.loads((first / cd_install.CONFIG_FILENAME).read_text(encoding="utf-8")) == {
        "preferences": {"first": True}
    }
    assert json.loads((second / cd_install.CONFIG_FILENAME).read_text(encoding="utf-8")) == {
        "preferences": {"second": True}
    }


def test_the_classic_path_is_the_fallback_when_nothing_exists(
    windows_layout: tuple[Path, Path],
) -> None:
    _, classic = windows_layout

    assert cd_install.config_path() == classic / cd_install.CONFIG_FILENAME


@pytest.mark.parametrize(
    ("platform", "relative"),
    [
        ("macos", Path("Library") / "Application Support" / "Claude"),
        ("linux", Path(".config") / "Claude"),
    ],
)
def test_the_platform_classic_paths_resolve_under_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, relative: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv(cd_install.CONFIG_OVERRIDE_ENV, raising=False)
    monkeypatch.setattr(cd_install, "_platform", lambda: platform)

    assert cd_install.config_path() == home / relative / cd_install.CONFIG_FILENAME


# ---------------------------------------------------------------- adapter registration


def test_the_adapter_answers_the_host_verb(cd_surface: Path, installed_executable: str) -> None:
    assert shared.ADAPTER_MODULES["claude_desktop"] == "mnemoseed_local.hosts.claude_desktop.install"

    assert main(["host", "status", "--host", "claude_desktop"]) == 0
    assert main(["host", "install", "--host", "claude_desktop"]) == 0

    assert cd_surface.is_file()

    assert main(["host", "uninstall", "--host", "claude_desktop"]) == 0
    assert cd_install.status().state == "not-installed"


def test_the_host_verb_reports_the_stale_state_through_the_cli(
    cd_surface: Path, installed_executable: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(
        cd_surface,
        {
            "preferences": {"quickEntryShortcut": "Alt+Space"},
            "mcpServers": {"mnemoseed": {"command": "C:/gone/away/python.exe", "args": ["mcp"]}},
        },
    )

    assert main(["host", "status", "--host", "claude_desktop"]) == 0

    out = capsys.readouterr().out
    assert "host: stale" in out
    assert str(cd_surface) in out


def test_the_status_json_carries_the_shared_observations(
    cd_surface: Path, installed_executable: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hook-less host has no ingest to acknowledge and no host-attributed
    capture to find, so those observations stay honestly not-observed/unknown
    instead of being dropped or faked; the manual trust step does not apply."""
    _write_config(cd_surface, EXISTING_CONFIG)

    assert main(["host", "status", "--host", "claude_desktop", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["host"] == "claude_desktop"
    assert payload["structure"]["state"] == "not-installed"
    assert payload["ingest_ack"]["state"] in {"not_observed", "unknown"}
    assert payload["capture_evidence"]["state"] in {"none", "unknown"}
    assert payload["replay_evidence"]["state"] in {"not_observed", "unknown"}
    assert payload["trust"] == {
        "state": "not_required",
        "detail": "no mnemoseed hooks registered for this host",
    }


def test_the_hidden_transformer_verb_fails_open_for_this_host(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Claude Desktop has no hook stdin surface, so the registered choice stays
    inert: the verb fails open rather than blocking anything."""
    assert main(["_hook-event", "--host", "claude_desktop"]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unavailable" in captured.err


# ---------------------------------------------------------------- install output


def test_install_prints_the_five_onboarding_items(
    cd_surface: Path, installed_executable: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(cd_surface, EXISTING_CONFIG)

    assert main(["host", "install", "--host", "claude_desktop"]) == 0

    out = capsys.readouterr().out
    assert "mcp: applied" in out
    assert "close Claude Desktop" in out and "restart" in out
    assert instructions.gateway_text("claude_chat") in out
    assert "ask a question only memory can answer" in out
    assert "no capture hook" in out
    for forbidden in ("memory active", "activated", "will call"):
        assert forbidden not in out.lower()


def test_install_reports_up_to_date_on_a_second_run(
    cd_surface: Path, installed_executable: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cd_install.install()
    capsys.readouterr()

    assert main(["host", "install", "--host", "claude_desktop"]) == 0

    assert "mcp: up to date" in capsys.readouterr().out
