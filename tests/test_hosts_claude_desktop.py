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
import subprocess
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

HERMETIC_ENV_VARS = (
    "MNEMOSEED_LOCAL_BASEURL",
    "MNEMOSEED_CLAUDE_DESKTOP_CONFIG",
    "LOCALAPPDATA",
    "APPDATA",
    "XDG_CONFIG_HOME",
)


def _rpc(msg_id: int, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
    message: dict[str, object] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _write_config(target: Path, payload: object, *, text: str | None = None) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    body = text if text is not None else json.dumps(payload, indent=2) + "\n"
    target.write_text(body, encoding="utf-8")
    return hashlib.md5(target.read_bytes()).hexdigest()


def _write_raw_config(target: Path, payload: bytes) -> str:
    """A config file whose bytes are not decodable as UTF-8."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return hashlib.md5(payload).hexdigest()


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
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", UNROUTABLE_BASE_URL)
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
    """The command-resolution seam: the write path under test, with the
    installed-runtime gate answered instead of guessed."""
    resolved = str(Path(sys.executable).resolve())
    monkeypatch.setattr(cd_install, "resolve_command", lambda: (resolved, ["mcp"]))
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


def test_a_malformed_mnemoseed_entry_is_refused_not_replaced(
    cd_surface: Path, installed_executable: str
) -> None:
    """The entry is the same user data as the container that holds it: a shape
    this tool does not own refuses with manual guidance instead of being
    overwritten, exactly as status calls that shape ``unreadable``."""
    digest = _write_config(
        cd_surface,
        {"mcpServers": {"mnemoseed": "not an object", "foreign-server": {"command": "x"}}},
    )

    with pytest.raises(cd_install.ConfigParseError):
        cd_install.install()

    assert hashlib.md5(cd_surface.read_bytes()).hexdigest() == digest
    assert json.loads(cd_surface.read_text(encoding="utf-8"))["mcpServers"]["mnemoseed"] == "not an object"


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


def test_a_non_utf8_config_is_refused_with_guidance_and_never_a_traceback(
    cd_surface: Path, installed_executable: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bytes that are not UTF-8 are a parse failure like any other: the CLI
    reports the manual guidance, exits 1, and the file stays untouched."""
    digest = _write_raw_config(cd_surface, '{"preferences": {"note": "caf\xe9"}}\n'.encode("latin-1"))

    assert main(["host", "install", "--host", "claude_desktop"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not UTF-8" in captured.err
    assert "manually" in captured.err
    assert "Traceback" not in captured.err
    assert hashlib.md5(cd_surface.read_bytes()).hexdigest() == digest


def test_a_non_utf8_config_reports_unreadable(cd_surface: Path) -> None:
    _write_raw_config(cd_surface, b'{"mcpServers": {"mnemoseed": "caf\xe9"}}\n')

    assert cd_install.status().state == "unreadable"


def test_a_config_held_open_by_the_running_app_is_reported_with_guidance(
    cd_surface: Path,
    installed_executable: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A write the app holds open is a user-facing refusal: exit 1, the OS
    error, the close-the-app guidance, and no traceback."""
    _write_config(cd_surface, EXISTING_CONFIG)

    def locked(*args: object, **kwargs: object) -> bool:
        raise PermissionError("the file is held by another process")

    monkeypatch.setattr(cd_install, "write_atomic", locked)

    assert main(["host", "install", "--host", "claude_desktop"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "held by another process" in captured.err
    assert "close Claude Desktop first" in captured.err
    assert json.loads(cd_surface.read_text(encoding="utf-8")) == EXISTING_CONFIG


# ---------------------------------------------------------------- the executable resolver


def test_a_development_run_refuses_the_install(cd_surface: Path) -> None:
    """The real predicate on the real tree: this suite runs from a checkout, and
    a checkout's build interpreter is not a command Claude Desktop can spawn."""
    assert cd_install._installed_runtime() is False

    with pytest.raises(cd_install.ClaudeDesktopConfigError) as refusal:
        cd_install.install()

    assert "uv tool install" in str(refusal.value)
    assert not cd_surface.exists()


def test_a_frozen_bundle_runs_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert cd_install._installed_runtime() is True
    assert cd_install.resolve_command() == (str(Path(sys.executable).resolve()), ["mcp"])


def _fake_installed_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A controlled interpreter location with the installed-runtime gate
    answered, so the resolution under test builds the command itself."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    interpreter = scripts / "python.exe"
    interpreter.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(cd_install, "_installed_runtime", lambda: True)
    return interpreter


def test_the_installed_command_is_the_console_script_beside_the_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented install route is a tool/venv layout: the app spawns the
    launcher beside the interpreter, because the interpreter alone would run
    ``python mcp``, which is not a program."""
    interpreter = _fake_installed_interpreter(tmp_path, monkeypatch)
    launcher = interpreter.with_name(cd_install.GATEWAY_SCRIPT_NAME)
    launcher.write_text("", encoding="utf-8")

    assert cd_install.resolve_command() == (str(launcher), ["mcp"])


def test_the_installed_command_falls_back_to_the_module_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed layout without a launcher still yields a spawnable command:
    the interpreter running the CLI module with the gateway verb."""
    interpreter = _fake_installed_interpreter(tmp_path, monkeypatch)

    assert cd_install.resolve_command() == (
        str(interpreter),
        ["-m", "mnemoseed_local.cli", "mcp"],
    )


def test_the_resolved_command_completes_an_mcp_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command this adapter writes is the command the app will spawn: it is
    run here for real and answers an ``initialize`` handshake. Only the
    installed-runtime gate is answered — the resolution under test builds the
    command itself, with no resolver seam patched."""
    monkeypatch.setattr(cd_install, "_installed_runtime", lambda: True)
    command, args = cd_install.resolve_command()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        f'baseurl = "{UNROUTABLE_BASE_URL}"\npreset = "embedded"\n', encoding="utf-8"
    )

    completed = subprocess.run(
        [command, *args],
        input=json.dumps(_rpc(1, "initialize")) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "MNEMOSEED_LOCAL_HOME": str(home)},
        timeout=60.0,
    )

    assert completed.returncode == 0, completed.stderr
    answered = json.loads(completed.stdout.splitlines()[0])
    assert answered["result"]["serverInfo"]["name"] == "mnemoseed-local"
    assert answered["result"]["instructions"] == instructions.gateway_text()


def test_the_module_form_completes_an_mcp_handshake(tmp_path: Path) -> None:
    """The fallback command is spawnable for real, not just a tuple: the CLI
    module runs the gateway and answers the handshake under a temp home aimed
    at an unroutable base."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        f'baseurl = "{UNROUTABLE_BASE_URL}"\npreset = "embedded"\n', encoding="utf-8"
    )

    completed = subprocess.run(
        [sys.executable, *cd_install.MODULE_ARGS, "mcp"],
        input=json.dumps(_rpc(1, "initialize")) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "MNEMOSEED_LOCAL_HOME": str(home), "MNEMOSEED_LOCAL_BASEURL": UNROUTABLE_BASE_URL},
        timeout=60.0,
    )

    assert completed.returncode == 0, completed.stderr
    answered = json.loads(completed.stdout.splitlines()[0])
    assert answered["result"]["serverInfo"]["name"] == "mnemoseed-local"
    assert answered["result"]["instructions"] == instructions.gateway_text()


# ---------------------------------------------------------------- status


def test_status_tracks_not_installed_installed_and_stale(cd_surface: Path, installed_executable: str) -> None:
    assert cd_install.status().state == "not-installed"

    cd_install.install()
    assert cd_install.status().state == "installed"

    written = json.loads(cd_surface.read_text(encoding="utf-8"))
    written["mcpServers"]["mnemoseed"]["command"] = "C:/gone/away/python.exe"
    _write_config(cd_surface, written)
    assert cd_install.status().state == "stale"


def test_status_reports_stale_when_only_the_arguments_differ(
    cd_surface: Path, installed_executable: str
) -> None:
    """The stored entry is the command/argument pair: a matching command with
    arguments this surface would not write is not the entry it would write."""
    cd_install.install()
    written = json.loads(cd_surface.read_text(encoding="utf-8"))
    written["mcpServers"]["mnemoseed"]["args"] = ["--stdio"]
    _write_config(cd_surface, written)

    assert cd_install.status().state == "stale"


def test_a_development_checkout_reports_installed_when_it_cannot_compare(cd_surface: Path) -> None:
    """Outside an installed runtime there is no command to compare against, so
    a non-empty entry honestly reads ``installed`` rather than a state that
    would pretend to know."""
    _write_config(
        cd_surface, {"mcpServers": {"mnemoseed": {"command": "C:/somewhere/cli.exe", "args": ["mcp"]}}}
    )

    assert cd_install._installed_runtime() is False
    assert cd_install.status().state == "installed"


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


def test_a_null_mnemoseed_entry_is_unreadable_and_untouched(cd_surface: Path) -> None:
    """A present key with a null value is the same refusal the write side
    raises: status reports it ``unreadable`` instead of reading it as absent,
    so detection sees the host as present."""
    digest = _write_config(cd_surface, {"mcpServers": {"mnemoseed": None}})

    assert cd_install.status().state == "unreadable"
    assert shared._adapter_present("claude_desktop") is True
    assert hashlib.md5(cd_surface.read_bytes()).hexdigest() == digest


def test_a_null_mcp_servers_container_is_unreadable_and_untouched(cd_surface: Path) -> None:
    digest = _write_config(cd_surface, {"mcpServers": None, "preferences": {}})

    assert cd_install.status().state == "unreadable"
    assert hashlib.md5(cd_surface.read_bytes()).hexdigest() == digest


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
    assert info.base_url == UNROUTABLE_BASE_URL
    assert seen["base_url"] == UNROUTABLE_BASE_URL
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


def test_the_side_that_already_carries_a_config_wins(windows_layout: tuple[Path, Path]) -> None:
    """A package directory can exist while the app the user actually runs is
    the classic install: the side that already holds a config file is the live
    surface, whichever side it is."""
    msix_root, classic = windows_layout
    (msix_root / "claude_pkg").mkdir(parents=True)
    _write_config(classic / cd_install.CONFIG_FILENAME, {"preferences": {"classic": True}})

    assert cd_install.config_path() == classic / cd_install.CONFIG_FILENAME


def test_a_config_on_both_sides_refuses_with_guidance(
    windows_layout: tuple[Path, Path], installed_executable: str
) -> None:
    """Two live config files mean the surface cannot be decided: no guess, no
    write, and an explicit pointer at the override."""
    msix_root, classic = windows_layout
    package = msix_root / "claude_pkg"
    _write_config(package / cd_install.CONFIG_FILENAME, {"preferences": {"msix": True}})
    _write_config(classic / cd_install.CONFIG_FILENAME, {"preferences": {"classic": True}})

    with pytest.raises(cd_install.ClaudeDesktopConfigError) as refusal:
        cd_install.install()

    assert cd_install.CONFIG_OVERRIDE_ENV in str(refusal.value)
    assert json.loads((package / cd_install.CONFIG_FILENAME).read_text(encoding="utf-8")) == {
        "preferences": {"msix": True}
    }
    assert json.loads((classic / cd_install.CONFIG_FILENAME).read_text(encoding="utf-8")) == {
        "preferences": {"classic": True}
    }


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
