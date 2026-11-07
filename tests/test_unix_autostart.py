"""Unix login autostart: the POSIX installer registers the frozen systemd user
unit (Linux) and the frozen LaunchAgent plist (macOS) after the CLI install and
before init, mirroring the Windows register-without-start semantics (enable
without ``--now``, no launchctl bootstrap), owning its same-path files by the
identity marker (foreign content fails closed), degrading honestly where no
systemd user manager or gui domain exists, resolving the CLI to an absolute
path (a bare command word or a quoted/spaced path is refused), and planning the
step in ``--dry-run`` with zero side effects.

The unit and plist content is pinned exactly against the design freeze, and
the syntax hooks run ``systemd-analyze verify`` / ``plutil -lint`` on the
generated artifacts wherever those validators exist (a runner-level syntax
proof only; real-host runtime behavior is owned by the verification round).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_macos_install_paths import (
    CI_TEXT,
    _decode,
    _failure_report,
    _make_installer,
    _msys_path,
    _shim_environment,
    _shim_log_lines,
    _write_shim,
    needs_sh,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SH_TEXT = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")

SPAN_BEGIN = "# --- unix autostart: shared generation"
SPAN_END = "# --- unix autostart end ---"
SPAN = SH_TEXT[SH_TEXT.index(SPAN_BEGIN) : SH_TEXT.index(SPAN_END)]

MARKER = "mnemoseed-local:autostart:unix-daemon:v1"
UNIT_RELATIVE = Path(".config") / "systemd" / "user" / "mnemoseed-local.service"
PLIST_RELATIVE = Path("Library") / "LaunchAgents" / "mnemoseed-local-daemon.plist"
FAKE_CLI = "/fake/bin/mnemoseed-local"

UNIT_DEFAULT = (
    "[Unit]\n"
    "Description=MnemoSeed Local memory daemon (user session)\n"
    f"# {MARKER}\n"
    "ConditionPathExists=!%h/.mnemoseed-local/daemon.off\n"
    "\n"
    "[Service]\n"
    f"ExecStart={FAKE_CLI} up\n"
    "Restart=no\n"
    "\n"
    "[Install]\n"
    "WantedBy=default.target\n"
)

PLIST_GATE = (
    'if [ -f "${MNEMOSEED_LOCAL_HOME:-$HOME/.mnemoseed-local}/daemon.off" ]; then '
    f"exit 0; fi; exec '{FAKE_CLI}' up"
)


def _plist_text(*, env_dict: str | None, data_home: str) -> str:
    env_block = "" if env_dict is None else env_dict
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        f"<!-- {MARKER} -->\n"
        "<dict>\n"
        f"{env_block}"
        "    <key>Label</key>\n"
        "    <string>mnemoseed-local-daemon</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        "        <string>/bin/sh</string>\n"
        "        <string>-c</string>\n"
        f"        <string>{PLIST_GATE}</string>\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{data_home}/launchd.out.log</string>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>{data_home}/launchd.out.log</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


PLIST_ENV_BLOCK = (
    "    <key>EnvironmentVariables</key>\n"
    "    <dict>\n"
    "        <key>MNEMOSEED_LOCAL_HOME</key>\n"
    "        <string>{data_home}</string>\n"
    "    </dict>\n"
)


def _autostart_prelude(shim_dir: Path, os_name: str = "Linux") -> str:
    """The span's functions with the top-level helpers they rely on, a
    controlled OS, and a controlled AUTOSTART_POINTER; set -eu matches the
    installer's execution context (a die inside a command substitution must
    abort, not swallow into an empty assignment)."""
    return (
        "set -eu\n"
        "die() { printf '%s\\n' \"install.sh: error: $*\" >&2; exit 1; }\n"
        'have() { command -v "$1" >/dev/null 2>&1; }\n'
        "AUTOSTART_POINTER=''\n"
        'UV_BIN_DIR="$HOME/.local/bin"\n'
        f"OS_NAME='{os_name}'\n" + SPAN
    )


def _emit_fragment(call: str, *, home: Path | None = None, data_home: Path | None = None) -> str:
    prelude = "set -eu\n"
    if home is not None:
        prelude += f"HOME='{home.as_posix()}'\n"
    if data_home is not None:
        prelude += f"MNEMOSEED_LOCAL_HOME='{data_home.as_posix()}'\n"
    return prelude + SPAN + f"\n{call}\n"


def _skip_if_resolution_dirty(resolved: str) -> None:
    """Registration tests need a resolvable CLI path; on hosts whose tmp paths
    carry whitespace or quotes the refusal fires by design, and those refusals
    are pinned by their own tests."""
    if any(character.isspace() or character == "'" for character in resolved):
        pytest.skip("this host's tmp path is refusal-dirty (whitespace or quote)")


def _run_span(
    tmp_path: Path, script: str, env: dict[str, str], stdin_data: bytes | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a fragment from a file: the span plus its call exceeds the ~8KB
    command-line argument this runner's sh can carry, and a file is also the
    same shape the CI syntax steps source."""
    path = tmp_path / "fragment.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    stdin_kwargs = {"input": stdin_data} if stdin_data is not None else {"stdin": subprocess.DEVNULL}
    completed = subprocess.run(["sh", str(path)], env=env, capture_output=True, timeout=60, **stdin_kwargs)
    return _decode(completed)


# ---------------------------------------------------------------- installer wiring


def test_autostart_step_sits_between_the_cli_install_and_init() -> None:
    assert (
        SH_TEXT.index("[3/9] mnemoseed-local CLI")
        < SH_TEXT.index("[4/9] login autostart")
        < SH_TEXT.index("[5/9] init")
    )


def test_step_count_is_nine_everywhere() -> None:
    assert "/8]" not in SH_TEXT
    for step in ("[1/9]", "[4/9] login autostart", "[5/9] init", "[9/9] doctor (final re-check)"):
        assert step in SH_TEXT


def test_dry_run_plans_the_autostart_step_between_cli_and_init() -> None:
    assert (
        SH_TEXT.index("[3] mnemoseed-local CLI")
        < SH_TEXT.index("[4] login autostart")
        < SH_TEXT.index("[5] init")
    )
    assert "plan_autostart" in SH_TEXT


def test_registration_is_enable_only_and_never_starts_the_daemon() -> None:
    assert "systemctl --user enable mnemoseed-local.service" in SH_TEXT
    assert "enable --now" not in SH_TEXT
    assert "--now" not in SPAN
    bootstrap_lines = [line for line in SPAN.splitlines() if "bootstrap" in line]
    assert bootstrap_lines, "the no-bootstrap registration must be stated"
    assert all("no launchctl bootstrap" in line for line in bootstrap_lines)


def test_next_steps_name_the_native_status_pointers() -> None:
    assert "systemctl --user status mnemoseed-local" in SH_TEXT
    assert "launchctl print gui/" in SH_TEXT
    assert "mnemoseed-local-daemon" in SH_TEXT


# ---------------------------------------------------------------- frozen unit content


def test_unit_emit_matches_the_frozen_default_form() -> None:
    assert f"AUTOSTART_MARKER='{MARKER}'" in SPAN
    emit = SPAN[SPAN.index("emit_systemd_unit() {") :]
    assert "[Unit]" in emit
    assert "Description=MnemoSeed Local memory daemon (user session)" in emit
    assert '"# $AUTOSTART_MARKER"' in emit
    assert "ConditionPathExists=!%h/.mnemoseed-local/daemon.off" in emit
    assert "Restart=no" in emit
    assert "WantedBy=default.target" in emit
    assert "ExecStart=$1 up" in emit
    assert "Environment=MNEMOSEED_LOCAL_HOME=" in emit


@needs_sh
def test_unit_emit_default_home_exact(tmp_path: Path) -> None:
    _shim_dir, env = _shim_environment(tmp_path)
    env.pop("MNEMOSEED_LOCAL_HOME")
    completed = _run_span(
        tmp_path, _emit_fragment(f"emit_systemd_unit '{FAKE_CLI}'", home=Path(env["HOME"])), env
    )
    assert completed.returncode == 0, _failure_report(completed, env)
    assert completed.stdout == UNIT_DEFAULT


@needs_sh
def test_unit_emit_baked_override_exact(tmp_path: Path) -> None:
    _shim_dir, env = _shim_environment(tmp_path)
    data_home = tmp_path / "data-home"
    env["MNEMOSEED_LOCAL_HOME"] = str(data_home)
    completed = _run_span(tmp_path, _emit_fragment(f"emit_systemd_unit '{FAKE_CLI}'"), env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert completed.stdout == UNIT_DEFAULT.replace(
        "ConditionPathExists=!%h/.mnemoseed-local/daemon.off",
        f"ConditionPathExists=!{data_home}/daemon.off",
    ).replace("[Service]\n", f"[Service]\nEnvironment=MNEMOSEED_LOCAL_HOME={data_home}\n")


@needs_sh
def test_unit_emit_override_bakes_gate_and_env_to_the_same_home(tmp_path: Path) -> None:
    _shim_dir, env = _shim_environment(tmp_path)
    data_home = tmp_path / "data-home"
    env["MNEMOSEED_LOCAL_HOME"] = str(data_home)
    completed = _run_span(tmp_path, _emit_fragment(f"emit_systemd_unit '{FAKE_CLI}'"), env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert f"ConditionPathExists=!{data_home}/daemon.off" in completed.stdout
    assert f"Environment=MNEMOSEED_LOCAL_HOME={data_home}" in completed.stdout
    assert "%h" not in completed.stdout


# ---------------------------------------------------------------- frozen plist content


@needs_sh
def test_plist_emit_default_home_exact(tmp_path: Path) -> None:
    _shim_dir, env = _shim_environment(tmp_path)
    env.pop("MNEMOSEED_LOCAL_HOME")
    # the expected home is read from the shell's own view of $HOME: MSYS sh
    # presents an inherited Windows HOME in its POSIX form
    probe = _run_span(tmp_path, 'printf "%s\\n" "$HOME"\n', env)
    assert probe.returncode == 0, _failure_report(probe, env)
    data_home = probe.stdout.strip() + "/.mnemoseed-local"
    completed = _run_span(tmp_path, _emit_fragment(f"emit_launchagent_plist '{FAKE_CLI}'"), env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert completed.stdout == _plist_text(env_dict=None, data_home=data_home)


@needs_sh
def test_plist_emit_baked_override_exact(tmp_path: Path) -> None:
    _shim_dir, env = _shim_environment(tmp_path)
    data_home = tmp_path / "data-home"
    env["MNEMOSEED_LOCAL_HOME"] = str(data_home)
    completed = _run_span(tmp_path, _emit_fragment(f"emit_launchagent_plist '{FAKE_CLI}'"), env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert completed.stdout == _plist_text(
        env_dict=PLIST_ENV_BLOCK.format(data_home=data_home), data_home=str(data_home)
    )


@needs_sh
def test_plist_has_no_keepalive_and_uses_the_runtime_gate(tmp_path: Path) -> None:
    _shim_dir, env = _shim_environment(tmp_path)
    env.pop("MNEMOSEED_LOCAL_HOME")
    completed = _run_span(tmp_path, _emit_fragment(f"emit_launchagent_plist '{FAKE_CLI}'"), env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert "KeepAlive" not in completed.stdout
    assert "RunAtLoad" in completed.stdout
    assert "${MNEMOSEED_LOCAL_HOME:-$HOME/.mnemoseed-local}/daemon.off" in completed.stdout


@needs_sh
def test_plist_emit_escapes_the_xml_specials(tmp_path: Path) -> None:
    _shim_dir, env = _shim_environment(tmp_path)
    env.pop("MNEMOSEED_LOCAL_HOME")
    tricky = "/opt/a&b<c>d"
    completed = _run_span(
        tmp_path, _emit_fragment(f"emit_launchagent_plist '{tricky}'", home=Path(env["HOME"])), env
    )
    assert completed.returncode == 0, _failure_report(completed, env)
    assert "exec '/opt/a&amp;b&lt;c&gt;d' up" in completed.stdout
    assert "<c>" not in completed.stdout


# ---------------------------------------------------------------- CLI resolution


@needs_sh
def test_cli_resolution_prefers_command_v(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("mnemoseed-local",))
    _skip_if_resolution_dirty(str(shim_dir / "mnemoseed-local"))
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "resolve_autostart_cli\n", env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert completed.stdout == f"{shim_dir / 'mnemoseed-local'}\n"


@needs_sh
def test_cli_resolution_falls_back_to_the_uv_tool_shim_dir(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    fallback = Path(env["HOME"]) / ".local" / "bin" / "mnemoseed-local"
    _skip_if_resolution_dirty(str(fallback))
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback.chmod(0o755)
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "resolve_autostart_cli\n", env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert completed.stdout == f"{fallback}\n"


@needs_sh
def test_cli_resolution_fails_closed_when_unresolvable(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "resolve_autostart_cli\n", env)
    assert completed.returncode != 0, _failure_report(completed, env)
    assert "cannot resolve the mnemoseed-local CLI path" in completed.stderr


@needs_sh
def test_cli_resolution_refuses_a_single_quoted_path(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    quoted_home = tmp_path / "home's dir"
    quoted_home.mkdir()
    env["HOME"] = str(quoted_home)
    fallback = quoted_home / ".local" / "bin" / "mnemoseed-local"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback.chmod(0o755)
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "resolve_autostart_cli\n", env)
    assert completed.returncode != 0, _failure_report(completed, env)
    assert "refusing the CLI path" in completed.stderr
    assert "single quote or whitespace" in completed.stderr


@needs_sh
def test_cli_resolution_refuses_a_path_with_whitespace(tmp_path: Path) -> None:
    spaced = tmp_path / "space dir"
    spaced.mkdir()
    shim = spaced / "mnemoseed-local"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)
    shim_dir, env = _shim_environment(tmp_path)
    env["PATH"] = f"{_msys_path(spaced)}:{env['PATH']}"
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "resolve_autostart_cli\n", env)
    assert completed.returncode != 0, _failure_report(completed, env)
    assert "refusing the CLI path" in completed.stderr
    assert "single quote or whitespace" in completed.stderr


# ---------------------------------------------------------------- Linux registration


@needs_sh
def test_linux_registration_writes_the_unit_enables_and_never_starts(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("systemctl", "mnemoseed-local"))
    _skip_if_resolution_dirty(str(shim_dir / "mnemoseed-local"))
    completed = _run_span(
        tmp_path,
        _autostart_prelude(shim_dir)
        + "install_linux_autostart\n"
        + "printf '%s\\n' \"POINTER=$AUTOSTART_POINTER\"\n",
        env,
    )
    assert completed.returncode == 0, _failure_report(completed, env)
    unit = Path(env["HOME"]) / UNIT_RELATIVE
    assert unit.is_file()
    text = unit.read_text(encoding="utf-8")
    assert MARKER in text
    assert f"ConditionPathExists=!{env['MNEMOSEED_LOCAL_HOME']}/daemon.off" in text
    log = _shim_log_lines(env)
    assert "--user daemon-reload" in log
    assert "--user enable mnemoseed-local.service" in log
    assert not any("--now" in line for line in log)
    assert "registered autostart unit" in completed.stdout
    assert "POINTER=systemctl --user status mnemoseed-local" in completed.stdout
    leftovers = [p.name for p in unit.parent.iterdir() if p.name != unit.name]
    assert leftovers == []


@needs_sh
def test_linux_re_registration_rewrites_atomically_and_idempotently(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("systemctl", "mnemoseed-local"))
    _skip_if_resolution_dirty(str(shim_dir / "mnemoseed-local"))
    prelude = _autostart_prelude(shim_dir)
    first = _run_span(tmp_path, prelude + "install_linux_autostart\n", env)
    assert first.returncode == 0, _failure_report(first, env)
    unit = Path(env["HOME"]) / UNIT_RELATIVE
    content = unit.read_text(encoding="utf-8")
    second = _run_span(tmp_path, prelude + "install_linux_autostart\n", env)
    assert second.returncode == 0, _failure_report(second, env)
    assert unit.read_text(encoding="utf-8") == content
    assert content.count(MARKER) == 1
    log = _shim_log_lines(env)
    assert log.count("--user daemon-reload") == 2
    assert log.count("--user enable mnemoseed-local.service") == 2


@needs_sh
def test_linux_foreign_unit_fails_closed_untouched(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("systemctl",))
    unit = Path(env["HOME"]) / UNIT_RELATIVE
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\nDescription=someone else's unit\n", encoding="utf-8")
    before = unit.read_bytes()
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "install_linux_autostart\n", env)
    assert completed.returncode != 0, _failure_report(completed, env)
    assert "not provably owned by MnemoSeed" in completed.stderr
    assert "identity marker missing" in completed.stderr
    assert unit.read_bytes() == before
    log = _shim_log_lines(env)
    assert not any("daemon-reload" in line or " enable" in line for line in log)


@needs_sh
def test_linux_without_systemctl_degrades_with_a_note_and_continues(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "install_linux_autostart\n", env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert "login autostart needs a systemd user session" in completed.stderr
    assert "skipped" in completed.stderr
    assert not (Path(env["HOME"]) / UNIT_RELATIVE).exists()


@needs_sh
def test_linux_without_a_user_manager_degrades_with_a_note_and_continues(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("systemctl",))
    _write_shim(shim_dir, "systemctl", "#!/bin/sh\nexit 3\n")
    env["XDG_RUNTIME_DIR"] = str(tmp_path / "empty-xdg")
    (tmp_path / "empty-xdg").mkdir()
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "install_linux_autostart\n", env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert "login autostart needs a systemd user session" in completed.stderr
    assert not (Path(env["HOME"]) / UNIT_RELATIVE).exists()
    assert _shim_log_lines(env) == []


@needs_sh
def test_linux_manager_probe_accepts_a_live_user_manager_socket(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("systemctl", "mnemoseed-local"))
    _skip_if_resolution_dirty(str(shim_dir / "mnemoseed-local"))
    # is-system-running answers non-running (the degraded branch) while every
    # real operation succeeds: the private socket keeps the probe positive
    _write_shim(
        shim_dir,
        "systemctl",
        '#!/bin/sh\ncase "$*" in *is-system-running*) exit 1 ;; esac\nexit 0\n',
    )
    xdg = tmp_path / "xdg"
    (xdg / "systemd" / "private").mkdir(parents=True)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "install_linux_autostart\n", env)
    assert completed.returncode == 0, _failure_report(completed, env)
    assert (Path(env["HOME"]) / UNIT_RELATIVE).is_file()


@needs_sh
@pytest.mark.parametrize(
    ("failing_step", "message"),
    [
        ("daemon-reload", "'systemctl --user daemon-reload' failed"),
        ("enable mnemoseed-local.service", "'systemctl --user enable mnemoseed-local.service' failed"),
    ],
)
def test_linux_manager_operation_failure_fails_closed(
    tmp_path: Path, failing_step: str, message: str
) -> None:
    """With the user manager present, a failed reload or enable is a hard
    refusal (rc non-zero), never a note-and-continue."""
    shim_dir, env = _shim_environment(tmp_path, ("systemctl", "mnemoseed-local"))
    _skip_if_resolution_dirty(str(shim_dir / "mnemoseed-local"))
    _write_shim(
        shim_dir,
        "systemctl",
        '#!/bin/sh\ncase "$*" in *is-system-running*) exit 0 ;; esac\n'
        f'case "$*" in *"{failing_step}"*) exit 4 ;; esac\nexit 0\n',
    )
    completed = _run_span(tmp_path, _autostart_prelude(shim_dir) + "install_linux_autostart\n", env)
    assert completed.returncode != 0, _failure_report(completed, env)
    assert message in completed.stderr


# ---------------------------------------------------------------- macOS registration


@needs_sh
def test_darwin_registration_writes_the_plist_without_bootstrap(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("launchctl", "mnemoseed-local"))
    _skip_if_resolution_dirty(str(shim_dir / "mnemoseed-local"))
    completed = _run_span(
        tmp_path,
        _autostart_prelude(shim_dir, "Darwin")
        + "install_darwin_autostart\n"
        + "printf '%s\\n' \"POINTER=$AUTOSTART_POINTER\"\n",
        env,
    )
    assert completed.returncode == 0, _failure_report(completed, env)
    plist = Path(env["HOME"]) / PLIST_RELATIVE
    assert plist.is_file()
    text = plist.read_text(encoding="utf-8")
    assert f"<!-- {MARKER} -->" in text
    assert "<key>Label</key>" in text
    assert "mnemoseed-local-daemon</string>" in text
    assert "<key>RunAtLoad</key>" in text
    log = _shim_log_lines(env)
    assert not any("bootstrap" in line for line in log)
    assert "registered autostart plist" in completed.stdout
    assert "POINTER=launchctl print gui/" in completed.stdout
    assert "mnemoseed-local-daemon" in completed.stdout.split("POINTER=")[1]


@needs_sh
def test_darwin_re_registration_rewrites_atomically_and_idempotently(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("launchctl", "mnemoseed-local"))
    _skip_if_resolution_dirty(str(shim_dir / "mnemoseed-local"))
    prelude = _autostart_prelude(shim_dir, "Darwin")
    first = _run_span(tmp_path, prelude + "install_darwin_autostart\n", env)
    assert first.returncode == 0, _failure_report(first, env)
    plist = Path(env["HOME"]) / PLIST_RELATIVE
    content = plist.read_text(encoding="utf-8")
    second = _run_span(tmp_path, prelude + "install_darwin_autostart\n", env)
    assert second.returncode == 0, _failure_report(second, env)
    assert plist.read_text(encoding="utf-8") == content
    assert content.count(MARKER) == 1
    leftovers = [p.name for p in plist.parent.iterdir() if p.name != plist.name]
    assert leftovers == []


@needs_sh
def test_darwin_foreign_plist_fails_closed_untouched(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("launchctl",))
    plist = Path(env["HOME"]) / PLIST_RELATIVE
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist><dict><key>Label</key><string>other</string></dict></plist>\n", encoding="utf-8")
    before = plist.read_bytes()
    completed = _run_span(
        tmp_path, _autostart_prelude(shim_dir, "Darwin") + "install_darwin_autostart\n", env
    )
    assert completed.returncode != 0, _failure_report(completed, env)
    assert "not provably owned by MnemoSeed" in completed.stderr
    assert plist.read_bytes() == before


@needs_sh
def test_darwin_absent_gui_domain_notes_and_still_registers(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("launchctl", "mnemoseed-local"))
    _skip_if_resolution_dirty(str(shim_dir / "mnemoseed-local"))
    _write_shim(shim_dir, "launchctl", "#!/bin/sh\nexit 3\n")
    completed = _run_span(
        tmp_path, _autostart_prelude(shim_dir, "Darwin") + "install_darwin_autostart\n", env
    )
    assert completed.returncode == 0, _failure_report(completed, env)
    assert "launchd gui domain is not available in this session" in completed.stderr
    assert (Path(env["HOME"]) / PLIST_RELATIVE).is_file()


# ---------------------------------------------------------------- platform dispatch


@needs_sh
def test_unsupported_platform_notes_and_skips(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    completed = _run_span(
        tmp_path, _autostart_prelude(shim_dir, "MINGW64_NT-10.0-26200") + "install_unix_autostart\n", env
    )
    assert completed.returncode == 0, _failure_report(completed, env)
    assert "login autostart is not available on this platform" in completed.stderr
    assert not (Path(env["HOME"]) / UNIT_RELATIVE).exists()
    assert not (Path(env["HOME"]) / PLIST_RELATIVE).exists()


# ---------------------------------------------------------------- dry run and full runs


@needs_sh
def test_full_run_dry_run_plans_autostart_with_zero_side_effects(tmp_path: Path) -> None:
    run, env = _make_installer(tmp_path)
    completed = run("--dry-run")
    assert completed.returncode == 0, _failure_report(completed, env)
    assert "[4] login autostart" in completed.stdout
    # the plan content is unconditional (it shows what the step writes on a
    # manager-present machine); each host asserts its own OS branch's block
    if sys.platform == "win32":
        # the host OS is neither Linux nor Darwin: the plan honestly skips
        assert "unsupported platform" in completed.stdout
    elif sys.platform == "darwin":
        assert "mnemoseed-local-daemon.plist" in completed.stdout
        assert "RunAtLoad" in completed.stdout
        assert "no launchctl bootstrap" in completed.stdout
    else:
        assert "mnemoseed-local.service" in completed.stdout
        assert "WantedBy=default.target" in completed.stdout
        assert "Restart=no" in completed.stdout
    home = Path(env["HOME"])
    assert not (home / UNIT_RELATIVE).exists()
    assert not (home / PLIST_RELATIVE).exists()
    assert _shim_log_lines(env) == []


@needs_sh
@pytest.mark.skipif(sys.platform != "linux", reason="the Linux full run needs a Linux host")
def test_full_run_registers_the_linux_unit(tmp_path: Path) -> None:
    run, env = _make_installer(tmp_path, extra_shims=("systemctl",))
    _skip_if_resolution_dirty(str(tmp_path / "shims" / "mnemoseed-local"))
    completed = run()
    assert completed.returncode == 0, _failure_report(completed, env)
    unit = Path(env["HOME"]) / UNIT_RELATIVE
    assert unit.is_file()
    text = unit.read_text(encoding="utf-8")
    assert MARKER in text
    log = _shim_log_lines(env)
    assert "--user enable mnemoseed-local.service" in log
    assert "systemctl --user status mnemoseed-local" in completed.stdout


@needs_sh
@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS full run needs a macOS host")
def test_full_run_writes_the_darwin_plist(tmp_path: Path) -> None:
    run, env = _make_installer(tmp_path)
    _skip_if_resolution_dirty(str(tmp_path / "shims" / "mnemoseed-local"))
    completed = run()
    assert completed.returncode == 0, _failure_report(completed, env)
    plist = Path(env["HOME"]) / PLIST_RELATIVE
    assert plist.is_file()
    assert MARKER in plist.read_text(encoding="utf-8")
    assert "launchctl print gui/" in completed.stdout


@needs_sh
@pytest.mark.skipif(sys.platform != "win32", reason="the unsupported-platform note needs a non-Unix host")
def test_full_run_on_an_unsupported_platform_skips_autostart(tmp_path: Path) -> None:
    run, env = _make_installer(tmp_path)
    completed = run()
    assert completed.returncode == 0, _failure_report(completed, env)
    assert "login autostart is not available on this platform" in completed.stderr
    assert not (Path(env["HOME"]) / UNIT_RELATIVE).exists()
    assert not (Path(env["HOME"]) / PLIST_RELATIVE).exists()


# ---------------------------------------------------------------- syntax validation hooks


@needs_sh
def test_generated_unit_passes_systemd_analyze_when_available(tmp_path: Path) -> None:
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze is not available on this host")
    _shim_dir, env = _shim_environment(tmp_path)
    fake = tmp_path / "fake-cli" / "mnemoseed-local"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    env.pop("MNEMOSEED_LOCAL_HOME")
    default = _run_span(tmp_path, _emit_fragment(f"emit_systemd_unit '{fake.as_posix()}'"), env)
    assert default.returncode == 0, _failure_report(default, env)
    override_home = tmp_path / "data-home"
    baked = _run_span(
        tmp_path, _emit_fragment(f"emit_systemd_unit '{fake.as_posix()}'", data_home=override_home), env
    )
    assert baked.returncode == 0, _failure_report(baked, env)
    for name, text in (("default.service", default.stdout), ("override.service", baked.stdout)):
        unit = tmp_path / name
        unit.write_text(text, encoding="utf-8", newline="\n")
        verified = subprocess.run(
            [analyze, "verify", str(unit)], capture_output=True, text=True, timeout=60, check=False
        )
        assert verified.returncode == 0, verified.stdout + verified.stderr


@needs_sh
def test_generated_plist_passes_plutil_lint_when_available(tmp_path: Path) -> None:
    lint = shutil.which("plutil")
    if lint is None:
        pytest.skip("plutil is not available on this host")
    _shim_dir, env = _shim_environment(tmp_path)
    env.pop("MNEMOSEED_LOCAL_HOME")
    default = _run_span(
        tmp_path, _emit_fragment(f"emit_launchagent_plist '{FAKE_CLI}'", home=Path(env["HOME"])), env
    )
    assert default.returncode == 0, _failure_report(default, env)
    override_home = tmp_path / "data-home"
    baked = _run_span(
        tmp_path, _emit_fragment(f"emit_launchagent_plist '{FAKE_CLI}'", data_home=override_home), env
    )
    assert baked.returncode == 0, _failure_report(baked, env)
    for name, text in (("default.plist", default.stdout), ("override.plist", baked.stdout)):
        plist = tmp_path / name
        plist.write_text(text, encoding="utf-8", newline="\n")
        linted = subprocess.run(
            [lint, "-lint", str(plist)], capture_output=True, text=True, timeout=60, check=False
        )
        assert linted.returncode == 0, linted.stdout + linted.stderr


# ---------------------------------------------------------------- CI wiring


def test_ci_smoke_carries_the_syntax_validation() -> None:
    smoke = CI_TEXT[CI_TEXT.index("  install-script-smoke:") : CI_TEXT.index("  release-install-verify:")]
    assert "runner.os == 'Linux'" in smoke
    assert "systemd-analyze verify" in smoke
    assert "runner.os == 'macOS'" in smoke
    assert "plutil -lint" in smoke
