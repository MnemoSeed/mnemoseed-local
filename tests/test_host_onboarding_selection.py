"""Host onboarding selection for the install orchestrators (#301).

Pins: an installer never auto-wires a host adapter — wiring happens only
through an explicit ``--hosts``/``-Hosts`` selection (each named key validated
before any step runs; ``all`` is refused on purpose) or through the interactive
offer that runs only from an empty selection and only when a terminal can
answer it. Installed-host detection probes the host CLIs on PATH and the
adapters' own config directories, a non-interactive session without a selection
wires nothing and reports what it detected, the next steps name the exact
per-host commands, and every selected host reports its hooks, MCP and trust
outcomes on separate lines, so Claude Code's hooks-only installer can never
read as a complete onboarding and repeat installs stay idempotent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from mnemoseed_local.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]

SH_TEXT = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
PS1_TEXT = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")

#: The adapter keys an installer may wire, in registry order.
HOST_KEYS = ("claude_code", "codex_cli", "opencode", "zcode")

#: The exact manual wiring commands a no-selection install prints.
NEXT_STEP_COMMANDS = (
    "mnemoseed-local hook install opencode",
    "mnemoseed-local host install --host claude_code",
    "mnemoseed-local host install --host codex_cli",
    "mnemoseed-local host install --host zcode",
)

PWSH = os.environ.get("PWSH", "pwsh")

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="a POSIX sh runner is required")
needs_pwsh = pytest.mark.skipif(
    sys.platform != "win32" or (shutil.which(PWSH) is None and not Path(PWSH).exists()),
    reason="Windows with pwsh is required",
)

#: Test double for the Windows task registrar: declares the splatted parameter
#: contract and performs nothing.
FAKE_TASK_SCRIPT = (
    "param(\n"
    "    [string]$BootstrapSource, [string]$TaskName, [string]$User, [switch]$MigrateLegacyOllamaTask\n"
    ")\n"
)


class _Installer:
    """A hermetic installer runner plus the shim log of its CLI invocations."""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]],
        run_file: Callable[..., subprocess.CompletedProcess[str]],
        log: Path,
    ) -> None:
        self.run = run
        self.run_file = run_file
        self.log = log

    def shim_lines(self) -> list[str]:
        if not self.log.is_file():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def adapter_lines(self) -> list[str]:
        return [line for line in self.shim_lines() if line.startswith(("hook", "host"))]


def _make_sh_installer(
    tmp_path: Path, *, cli_fail_arg: str | None = None, clean_hosts: bool = False
) -> _Installer:
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    log = tmp_path / "shim.log"
    shim_body = '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SHIM_LOG"\nexit 0\n'
    if cli_fail_arg is not None:
        shim_body = (
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$SHIM_LOG"\n'
            f'case "$*" in *"{cli_fail_arg}"*) exit 3 ;; esac\n'
            "exit 0\n"
        )
    for name in ("mnemoseed-local", "uv"):
        shim = shim_dir / name
        shim.write_text(shim_body, encoding="utf-8")
        shim.chmod(0o755)
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["MNEMOSEED_LOCAL_HOME"] = str(tmp_path / "mnemo-home")
    env["SHIM_LOG"] = str(log)
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    if clean_hosts:
        for name in ("CODEX_HOME", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"):
            env.pop(name, None)
        env["PATH"] = os.pathsep.join([str(shim_dir), str(Path(shutil.which("sh")).parent)])

    def run(*args: str, stdin_data: str | None = None) -> subprocess.CompletedProcess[str]:
        stdin_kwargs = {"input": stdin_data} if stdin_data is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(
            ["sh", str(REPO_ROOT / "install.sh"), *args],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            **stdin_kwargs,
        )

    return _Installer(run, run, log)


def _make_pwsh_installer(tmp_path: Path, *, clean_hosts: bool = False) -> _Installer:
    """A full-run sandbox: the installer is copied verbatim into a tree whose
    helper siblings and PATH-resolved CLI/uv are test doubles, so every step
    runs without touching the real user profile or any host config."""
    staging = tmp_path / "install-tree"
    (staging / "scripts").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "install.ps1", staging / "install.ps1")
    shutil.copyfile(
        REPO_ROOT / "scripts" / "windows-install-helpers.ps1",
        staging / "scripts" / "windows-install-helpers.ps1",
    )
    (staging / "scripts" / "windows-logon.ps1").write_text(
        "# test double: never executed\n", encoding="utf-8"
    )
    (staging / "scripts" / "windows-logon-task.ps1").write_text(FAKE_TASK_SCRIPT, encoding="utf-8")
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    log = tmp_path / "shim.log"
    shim_body = '@echo off\r\n>>"%SHIM_LOG%" echo %*\r\nexit /b 0\r\n'
    for name in ("mnemoseed-local", "uv"):
        (shim_dir / f"{name}.cmd").write_text(shim_body, encoding="utf-8", newline="")
    home = tmp_path / "user-home"
    home.mkdir()
    (tmp_path / "temp").mkdir()
    env = dict(os.environ)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["MNEMOSEED_LOCAL_HOME"] = str(home / ".mnemoseed-local")
    env["SHIM_LOG"] = str(log)
    env["TEMP"] = str(tmp_path / "temp")
    env["TMP"] = str(tmp_path / "temp")
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    if clean_hosts:
        for name in ("CODEX_HOME", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"):
            env.pop(name, None)
        env["PATH"] = os.pathsep.join(
            [str(shim_dir), str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")]
        )

    def run_file(installer: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(installer), *args],
            cwd=str(tmp_path),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    return _Installer(lambda *args: run_file(staging / "install.ps1", *args), run_file, log)


@pytest.fixture
def sh_install(tmp_path: Path) -> _Installer:
    return _make_sh_installer(tmp_path)


@pytest.fixture
def sh_install_clean(tmp_path: Path) -> _Installer:
    """A machine with no host CLIs on PATH, no host config dirs, no TTY."""
    return _make_sh_installer(tmp_path, clean_hosts=True)


@pytest.fixture
def sh_install_with_failing_codex(tmp_path: Path) -> _Installer:
    """The CLI double exits non-zero only for the codex_cli adapter install."""
    return _make_sh_installer(tmp_path, cli_fail_arg="codex_cli")


@pytest.fixture
def sh_install_with_failing_opencode(tmp_path: Path) -> _Installer:
    """The CLI double exits non-zero only for the opencode adapter install."""
    return _make_sh_installer(tmp_path, cli_fail_arg="hook install opencode")


@pytest.fixture
def ps1_install(tmp_path: Path) -> _Installer:
    return _make_pwsh_installer(tmp_path)


@pytest.fixture
def ps1_install_clean(tmp_path: Path) -> _Installer:
    """A machine with no host CLIs on PATH, no host config dirs, no TTY."""
    return _make_pwsh_installer(tmp_path, clean_hosts=True)


def _extract_function(text: str, header: str) -> str:
    """The source of one top-level function, from its header to its ``}``."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _run_pwsh_script(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _run_sh_fragment(script: str, answer: str) -> subprocess.CompletedProcess[str]:
    """A POSIX fragment runner; stdin stays binary because Windows text-mode
    input would CRLF-translate the answer and corrupt the menu token."""
    completed = subprocess.run(
        ["sh", "-c", script],
        input=(answer + "\n").encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    completed.stdout = completed.stdout.decode("utf-8", "replace")
    completed.stderr = completed.stderr.decode("utf-8", "replace")
    return completed


# ---------------------------------------------------------------- string-level


def test_posix_installer_selects_hosts_explicitly_and_refuses_pre_write() -> None:
    assert "HOSTS=''" in SH_TEXT
    assert "--hosts)" in SH_TEXT
    assert "--hosts=*" in SH_TEXT
    assert 'die "--hosts requires a value' in SH_TEXT
    assert "validate_host_list" in SH_TEXT
    assert "|".join(HOST_KEYS) in SH_TEXT
    assert "'all' is not accepted" in SH_TEXT
    validation_call = SH_TEXT.index('HOST_SELECTED="$(validate_host_list "$HOSTS")"')
    assert validation_call < SH_TEXT.index("# --- dry-run plan")
    assert validation_call < SH_TEXT.index("# --- step 1: ollama")
    assert "[7/8] host adapter wiring" in SH_TEXT
    assert "[7/8] OpenCode host adapter hook" not in SH_TEXT
    assert "for _host in $HOST_SELECTED" in SH_TEXT


def test_windows_installer_selects_hosts_explicitly_and_refuses_pre_write() -> None:
    assert "[string]$Hosts = ''" in PS1_TEXT
    assert "function Get-HostSelection" in PS1_TEXT
    assert "$HostSelection = Get-HostSelection $Hosts" in PS1_TEXT
    assert "|".join(HOST_KEYS) in PS1_TEXT
    assert "'all' is not accepted" in PS1_TEXT
    validation_call = PS1_TEXT.index("$HostSelection = Get-HostSelection $Hosts")
    assert validation_call < PS1_TEXT.index("# --- dry-run plan")
    assert validation_call < PS1_TEXT.index("# --- step 1: ollama")
    assert "[9/10] host adapter wiring" in PS1_TEXT
    assert "[9/10] OpenCode host adapter hook" not in PS1_TEXT
    assert "foreach ($hostKey in $HostSelection)" in PS1_TEXT
    assert "adapter install failed" in PS1_TEXT
    assert "adapter install failed" in SH_TEXT


def test_no_selection_prints_the_exact_per_host_commands_as_next_steps() -> None:
    for text in (SH_TEXT, PS1_TEXT):
        assert "no host adapter was wired" in text
        for command in NEXT_STEP_COMMANDS:
            assert command in text


def test_host_selection_stays_orthogonal_to_the_setup_mode() -> None:
    assert "MODE='capture-recall'" in SH_TEXT
    assert "--mode)" in SH_TEXT
    sh_step = SH_TEXT[SH_TEXT.index("[7/8] host adapter wiring") : SH_TEXT.index("# --- step 8")]
    assert "MODE" not in sh_step
    assert "[ValidateSet('CaptureRecall', 'Refinement')]" in PS1_TEXT
    ps1_step = PS1_TEXT[PS1_TEXT.index("[9/10] host adapter wiring") : PS1_TEXT.index("# --- step 8")]
    assert "$Mode" not in ps1_step
    assert "$CliMode" not in ps1_step


def test_dry_run_plan_reports_the_host_selection() -> None:
    assert "[7] host adapter wiring" in SH_TEXT
    assert "no host adapter wiring (no --hosts value" in SH_TEXT
    assert "'    plan:  would run `mnemoseed-local hook install opencode`'" in SH_TEXT
    assert "mnemoseed-local host install --host $_host" in SH_TEXT
    assert "[9] host adapter wiring" in PS1_TEXT
    assert "no host adapter wiring (no -Hosts value" in PS1_TEXT
    assert "mnemoseed-local host install --host $hostKey" in PS1_TEXT


def test_installers_detect_installed_hosts_via_cli_or_config_dir() -> None:
    assert "detect_installed_hosts() {" in SH_TEXT
    assert "host_config_dir" in SH_TEXT
    for pair in ("claude_code:claude", "codex_cli:codex", "opencode:opencode", "zcode:zcode"):
        assert pair in SH_TEXT, pair
    for marker in (
        '"$HOME/.claude"',
        '"$HOME/.codex"',
        '"$HOME/.config/opencode"',
        '"$HOME/.zcode"',
        "CODEX_HOME",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
    ):
        assert marker in SH_TEXT, marker
    assert SH_TEXT.index("detect_installed_hosts() {") < SH_TEXT.index("# --- dry-run plan")
    for name in (
        "Test-HostConfigDir",
        "Get-InstalledHosts",
        "ConvertFrom-HostMenuAnswer",
        "Select-HostsInteractively",
    ):
        marker = f"function {name}"
        assert marker in PS1_TEXT, name
        assert PS1_TEXT.index(marker) < PS1_TEXT.index("# --- dry-run plan"), name
    for marker in (
        "'.claude'",
        "'.codex'",
        "'.config\\opencode'",
        "'.zcode'",
        "CODEX_HOME",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
    ):
        assert marker in PS1_TEXT, marker


def test_installers_offer_an_interactive_selection_only_from_an_empty_selection() -> None:
    sh_step = SH_TEXT[SH_TEXT.index("[7/8] host adapter wiring") : SH_TEXT.index("# --- step 8")]
    sh_empty = sh_step[sh_step.index('if [ -z "$HOST_SELECTED" ]; then') :]
    assert "[ -t 0 ]" in sh_empty
    assert "offer_installed_host_selection" in sh_empty
    ps_step = PS1_TEXT[PS1_TEXT.index("[9/10] host adapter wiring") : PS1_TEXT.index("# --- step 8")]
    ps_empty = ps_step[ps_step.index("if ($HostSelection.Count -eq 0) {") :]
    assert "Select-HostsInteractively" in ps_empty
    for text in (SH_TEXT, PS1_TEXT):
        assert "wire which hosts? comma-separated numbers, empty to skip" in text
        assert "nothing selected: no host adapter was wired" in text


def test_installers_report_detected_hosts_without_wiring_when_not_interactive() -> None:
    for text in (SH_TEXT, PS1_TEXT):
        assert "non-interactive session: nothing was wired" in text
        assert "detection: no installed host found (host CLIs on PATH or their config dirs)" in text


def test_dry_run_plan_probes_the_detected_hosts() -> None:
    sh_plan = SH_TEXT[SH_TEXT.index("# --- dry-run plan") : SH_TEXT.index("# --- step 1: ollama")]
    assert "probe: detected installed hosts:" in sh_plan
    assert "probe: no installed host detected (host CLIs on PATH or their config dirs)" in sh_plan
    ps_plan = PS1_TEXT[PS1_TEXT.index("# --- dry-run plan") : PS1_TEXT.index("# --- step 1: ollama")]
    assert "probe: detected installed hosts:" in ps_plan
    assert "probe: no installed host detected (host CLIs on PATH or their config dirs)" in ps_plan


# ---------------------------------------------------------------- posix behavior


@needs_sh
def test_posix_install_without_a_host_selection_wires_nothing(sh_install: _Installer) -> None:
    completed = sh_install.run()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert sh_install.adapter_lines() == []
    assert "skipped: no --hosts value given - no host adapter was wired" in completed.stdout
    for command in NEXT_STEP_COMMANDS:
        assert command in completed.stdout
    assert "installation complete:" in completed.stdout


@needs_sh
def test_posix_install_runs_exactly_the_selected_adapters(sh_install: _Installer) -> None:
    completed = sh_install.run("--hosts", "codex_cli,zcode")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = sh_install.shim_lines()
    assert "host install --host codex_cli" in lines
    assert "host install --host zcode" in lines
    assert lines.index("host install --host codex_cli") < lines.index("host install --host zcode")
    assert "hook install opencode" not in lines
    assert "host install --host claude_code" not in lines
    assert "host codex_cli: running: mnemoseed-local host install --host codex_cli" in completed.stdout
    assert "host zcode: running: mnemoseed-local host install --host zcode" in completed.stdout
    assert "detected installed hosts" not in completed.stdout


@needs_sh
def test_posix_install_refuses_an_unknown_host_before_any_step(sh_install: _Installer) -> None:
    completed = sh_install.run("--hosts", "codex_cli,bogus")
    assert completed.returncode != 0
    assert "unknown host 'bogus'" in completed.stderr
    assert sh_install.shim_lines() == []


@needs_sh
def test_posix_install_refuses_hosts_all(sh_install: _Installer) -> None:
    completed = sh_install.run("--hosts", "all")
    assert completed.returncode != 0
    assert "unknown host 'all'" in completed.stderr
    assert "'all' is not accepted" in completed.stderr
    assert sh_install.shim_lines() == []


@needs_sh
def test_posix_install_accepts_the_equals_form_and_only_exact_keys(sh_install: _Installer) -> None:
    completed = sh_install.run("--hosts=opencode")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert sh_install.shim_lines().count("hook install opencode") == 1
    sh_install.log.unlink()
    for value in ("codex_cli,", ",", "codex_cli, zcode"):
        refused = sh_install.run("--hosts", value)
        assert refused.returncode != 0, value
        assert "unknown host" in refused.stderr, value
        assert not sh_install.log.exists(), value


@needs_sh
def test_posix_install_host_failure_is_non_fatal_for_the_other_hosts(
    sh_install_with_failing_codex: _Installer,
) -> None:
    completed = sh_install_with_failing_codex.run("--hosts", "codex_cli,zcode")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = sh_install_with_failing_codex.shim_lines()
    assert "host install --host codex_cli" in lines
    assert "host install --host zcode" in lines
    assert lines.index("host install --host codex_cli") < lines.index("host install --host zcode")
    assert "the codex_cli adapter install failed" in completed.stderr
    assert "retry manually with: mnemoseed-local host install --host codex_cli" in completed.stderr
    assert "installation complete:" in completed.stdout


@needs_sh
def test_posix_opencode_adapter_failure_is_non_fatal(
    sh_install_with_failing_opencode: _Installer,
) -> None:
    completed = sh_install_with_failing_opencode.run("--hosts", "opencode")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert sh_install_with_failing_opencode.adapter_lines() == ["hook install opencode"]
    assert "the opencode adapter install failed" in completed.stderr
    assert "retry manually with: mnemoseed-local hook install opencode" in completed.stderr
    assert "installation complete:" in completed.stdout


@needs_sh
def test_posix_install_deduplicates_the_selection(sh_install: _Installer) -> None:
    completed = sh_install.run("--hosts", "codex_cli,codex_cli,zcode")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = sh_install.shim_lines()
    assert lines.count("host install --host codex_cli") == 1
    assert "host install --host zcode" in lines


@needs_sh
def test_posix_dry_run_refuses_an_unknown_host_and_plans_the_selection(sh_install: _Installer) -> None:
    refused = sh_install.run("--dry-run", "--hosts", "bogus")
    assert refused.returncode != 0
    assert "unknown host 'bogus'" in refused.stderr
    assert sh_install.shim_lines() == []
    planned = sh_install.run("--dry-run", "--hosts", "codex_cli,zcode")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert "would run `mnemoseed-local host install --host codex_cli`" in planned.stdout
    assert "would run `mnemoseed-local host install --host zcode`" in planned.stdout
    assert "hook install opencode" not in planned.stdout


@needs_sh
def test_posix_install_reports_detected_hosts_and_wires_nothing_non_interactively(
    sh_install_clean: _Installer, tmp_path: Path
) -> None:
    (tmp_path / ".claude").mkdir()
    completed = sh_install_clean.run()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "detected installed hosts: claude_code" in completed.stdout
    assert "non-interactive session: nothing was wired" in completed.stdout
    assert sh_install_clean.adapter_lines() == []
    for command in NEXT_STEP_COMMANDS:
        assert command in completed.stdout
    assert "installation complete:" in completed.stdout


@needs_sh
def test_posix_piped_stdin_never_shows_the_menu_and_wires_nothing(
    sh_install_clean: _Installer, tmp_path: Path
) -> None:
    (tmp_path / ".claude").mkdir()
    completed = sh_install_clean.run(stdin_data="")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "wire which hosts?" not in completed.stdout
    assert "wire which hosts?" not in completed.stderr
    assert sh_install_clean.adapter_lines() == []
    assert "non-interactive session: nothing was wired" in completed.stdout
    assert "installation complete:" in completed.stdout


@needs_sh
def test_posix_install_reports_no_detected_host_on_a_clean_machine(sh_install_clean: _Installer) -> None:
    completed = sh_install_clean.run()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "detection: no installed host found (host CLIs on PATH or their config dirs)" in completed.stdout
    assert sh_install_clean.adapter_lines() == []
    assert "installation complete:" in completed.stdout


@needs_sh
def test_posix_dry_run_probes_the_detected_hosts(sh_install_clean: _Installer, tmp_path: Path) -> None:
    planned = sh_install_clean.run("--dry-run")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert "probe: no installed host detected (host CLIs on PATH or their config dirs)" in planned.stdout
    assert sh_install_clean.shim_lines() == []
    (tmp_path / ".claude").mkdir()
    detected = sh_install_clean.run("--dry-run")
    assert detected.returncode == 0, detected.stdout + detected.stderr
    assert "probe: detected installed hosts: claude_code" in detected.stdout
    assert sh_install_clean.shim_lines() == []


@needs_sh
def test_posix_explicit_hosts_selection_bypasses_detection(
    sh_install_clean: _Installer, tmp_path: Path
) -> None:
    (tmp_path / ".claude").mkdir()
    completed = sh_install_clean.run("--hosts", "zcode")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert sh_install_clean.adapter_lines() == ["host install --host zcode"]
    assert "detected installed hosts" not in completed.stdout
    assert "detection: no installed host found" not in completed.stdout


@needs_sh
def test_posix_menu_selection_maps_menu_numbers_onto_detected_keys() -> None:
    prelude = (
        _extract_function(SH_TEXT, "menu_select_hosts() {")
        + "\n"
        + _extract_function(SH_TEXT, "offer_installed_host_selection() {")
        + "\n"
    )
    shown = _run_sh_fragment(prelude + "offer_installed_host_selection claude_code codex_cli", "2")
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert "detected installed hosts:" in shown.stderr
    assert "1) claude_code" in shown.stderr
    assert "2) codex_cli" in shown.stderr
    assert "wire which hosts? comma-separated numbers, empty to skip" in shown.stderr
    assert shown.stdout == "codex_cli\n"
    cases = {
        "": "",
        "1,3": "claude_code\nopencode\n",
        "1, 3": "claude_code\nopencode\n",
        "1 3": "claude_code\nopencode\n",
        "bogus": "",
        "2,2": "codex_cli\n",
        "9": "",
        "0": "",
    }
    for answer, expected in cases.items():
        parsed = _run_sh_fragment(
            prelude + "offer_installed_host_selection claude_code codex_cli opencode zcode", answer
        )
        assert parsed.returncode == 0, (answer, parsed.stdout + parsed.stderr)
        assert parsed.stdout.endswith(expected), (answer, parsed.stdout)


@needs_sh
def test_posix_offer_capture_sees_only_the_selection_never_the_menu() -> None:
    prelude = (
        _extract_function(SH_TEXT, "menu_select_hosts() {")
        + "\n"
        + _extract_function(SH_TEXT, "offer_installed_host_selection() {")
        + "\n"
    )
    shown = _run_sh_fragment(
        prelude
        + 'RESULT="$(offer_installed_host_selection claude_code codex_cli)"\n'
        + "printf 'CAPTURED:%s|' \"$RESULT\"",
        "2",
    )
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert shown.stdout == "CAPTURED:codex_cli|"
    assert "detected installed hosts:" in shown.stderr
    assert "1) claude_code" in shown.stderr
    assert "wire which hosts? comma-separated numbers, empty to skip" in shown.stderr


# ---------------------------------------------------------------- windows behavior


@needs_pwsh
def test_windows_install_without_a_host_selection_wires_nothing(ps1_install: _Installer) -> None:
    completed = ps1_install.run()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert ps1_install.adapter_lines() == []
    assert "skipped: no -Hosts value given - no host adapter was wired" in completed.stdout
    for command in NEXT_STEP_COMMANDS:
        assert command in completed.stdout
    assert "installation complete:" in completed.stdout


@needs_pwsh
def test_windows_install_runs_exactly_the_selected_adapters(ps1_install: _Installer) -> None:
    completed = ps1_install.run("-Hosts", "codex_cli,zcode")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = ps1_install.shim_lines()
    assert "host install --host codex_cli" in lines
    assert "host install --host zcode" in lines
    assert lines.index("host install --host codex_cli") < lines.index("host install --host zcode")
    assert "hook install opencode" not in lines
    assert "host install --host claude_code" not in lines
    assert "host codex_cli: running: mnemoseed-local host install --host codex_cli" in completed.stdout
    assert "host zcode: running: mnemoseed-local host install --host zcode" in completed.stdout
    assert "detected installed hosts" not in completed.stdout


@needs_pwsh
def test_windows_install_refuses_an_unknown_host_before_any_step(ps1_install: _Installer) -> None:
    completed = ps1_install.run_file(REPO_ROOT / "install.ps1", "-Hosts", "codex_cli,bogus")
    assert completed.returncode != 0
    assert "unknown host 'bogus'" in completed.stderr
    assert ps1_install.shim_lines() == []
    for value in ("codex_cli,", ",", "codex_cli, zcode"):
        refused = ps1_install.run_file(REPO_ROOT / "install.ps1", "-Hosts", value)
        assert refused.returncode != 0, value
        assert "unknown host" in refused.stderr, value
        assert ps1_install.shim_lines() == [], value


@needs_pwsh
def test_windows_install_refuses_hosts_all(ps1_install: _Installer) -> None:
    completed = ps1_install.run_file(REPO_ROOT / "install.ps1", "-Hosts", "all")
    assert completed.returncode != 0
    assert "unknown host 'all'" in completed.stderr
    assert "'all' is not accepted" in completed.stderr
    assert ps1_install.shim_lines() == []
    uppercased = ps1_install.run_file(REPO_ROOT / "install.ps1", "-Hosts", "CODEX_CLI")
    assert uppercased.returncode != 0
    assert "unknown host 'CODEX_CLI'" in uppercased.stderr
    assert ps1_install.shim_lines() == []


@needs_pwsh
def test_windows_dry_run_plans_no_wiring_without_a_selection(ps1_install: _Installer) -> None:
    completed = ps1_install.run_file(REPO_ROOT / "install.ps1", "-DryRun")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "no host adapter wiring (no -Hosts value" in completed.stdout
    assert "hook install opencode" not in completed.stdout
    assert ps1_install.shim_lines() == []


@needs_pwsh
def test_windows_dry_run_plans_exactly_the_selected_hosts(ps1_install: _Installer) -> None:
    completed = ps1_install.run_file(REPO_ROOT / "install.ps1", "-DryRun", "-Hosts", "codex_cli,zcode")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "would run `mnemoseed-local host install --host codex_cli`" in completed.stdout
    assert "would run `mnemoseed-local host install --host zcode`" in completed.stdout
    assert "hook install opencode" not in completed.stdout
    assert ps1_install.shim_lines() == []
    deduplicated = ps1_install.run_file(
        REPO_ROOT / "install.ps1", "-DryRun", "-Hosts", "codex_cli,codex_cli,zcode"
    )
    assert deduplicated.returncode == 0, deduplicated.stdout + deduplicated.stderr
    assert deduplicated.stdout.count("would run `mnemoseed-local host install --host codex_cli`") == 1


@needs_pwsh
def test_windows_install_reports_detected_hosts_and_wires_nothing_non_interactively(
    ps1_install_clean: _Installer, tmp_path: Path
) -> None:
    (tmp_path / "user-home" / ".claude").mkdir()
    completed = ps1_install_clean.run()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "detected installed hosts: claude_code" in completed.stdout
    assert "non-interactive session: nothing was wired" in completed.stdout
    assert ps1_install_clean.adapter_lines() == []
    for command in NEXT_STEP_COMMANDS:
        assert command in completed.stdout
    assert "installation complete:" in completed.stdout


@needs_pwsh
def test_windows_install_reports_no_detected_host_on_a_clean_machine(ps1_install_clean: _Installer) -> None:
    completed = ps1_install_clean.run()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "detection: no installed host found (host CLIs on PATH or their config dirs)" in completed.stdout
    assert ps1_install_clean.adapter_lines() == []
    assert "installation complete:" in completed.stdout


@needs_pwsh
def test_windows_host_detection_functions_report_cli_and_config_dir_hits(tmp_path: Path) -> None:
    functions = "\n".join(
        _extract_function(PS1_TEXT, f"function {name}")
        for name in ("Test-CommandExists", "Test-HostConfigDir", "Get-InstalledHosts")
    )
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    cli_path = tmp_path / "cli-path"
    cli_path.mkdir()
    (cli_path / "zcode.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8", newline="")
    homes: dict[str, Path] = {}
    home_layouts = {
        "h0": (),
        "h1": (".claude",),
        "h2": (".zcode",),
        "h3": (".claude", ".zcode"),
        "h4": (),
    }
    for name, dirs in home_layouts.items():
        home = tmp_path / name
        home.mkdir()
        for config_dir in dirs:
            (home / config_dir).mkdir()
        homes[name] = home
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    opencode_root = tmp_path / "opencode-root"
    opencode_root.mkdir()
    xdg_root = tmp_path / "xdg-root"
    (xdg_root / "opencode").mkdir(parents=True)
    lines = [
        functions,
        "",
        "$env:CODEX_HOME = $null",
        "$env:OPENCODE_CONFIG_DIR = $null",
        "$env:XDG_CONFIG_HOME = $null",
        "",
    ]
    phases: list[tuple[str, Path, Path, list[str]]] = [
        ("A", homes["h0"], empty_path, []),
        ("B", homes["h1"], empty_path, []),
        ("C", homes["h2"], empty_path, []),
        ("D", homes["h3"], empty_path, []),
        ("E", homes["h4"], cli_path, []),
        ("F", homes["h4"], empty_path, [f"$env:CODEX_HOME = '{codex_home}'"]),
        (
            "G",
            homes["h4"],
            empty_path,
            ["$env:CODEX_HOME = $null", f"$env:OPENCODE_CONFIG_DIR = '{opencode_root}'"],
        ),
        (
            "H",
            homes["h4"],
            empty_path,
            ["$env:OPENCODE_CONFIG_DIR = $null", f"$env:XDG_CONFIG_HOME = '{xdg_root}'"],
        ),
    ]
    for label, home, path, setup in phases:
        lines.extend(setup)
        lines.append(f"$env:USERPROFILE = '{home}'")
        lines.append(f"$env:PATH = '{path}'")
        lines.append(f'"PHASE-{label}:" + (@(Get-InstalledHosts) -join ",")')
    script = tmp_path / "detect-fragment.ps1"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    completed = _run_pwsh_script(script)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    expected = {
        "PHASE-A:": "",
        "PHASE-B:": "claude_code",
        "PHASE-C:": "zcode",
        "PHASE-D:": "claude_code,zcode",
        "PHASE-E:": "zcode",
        "PHASE-F:": "codex_cli",
        "PHASE-G:": "opencode",
        "PHASE-H:": "opencode",
    }
    for prefix, keys in expected.items():
        line = next(row for row in completed.stdout.splitlines() if row.startswith(prefix))
        assert line == prefix + keys, (prefix, line, completed.stdout)


@needs_pwsh
def test_windows_menu_answer_maps_numbers_onto_detected_keys(tmp_path: Path) -> None:
    parse = _extract_function(PS1_TEXT, "function ConvertFrom-HostMenuAnswer")
    cases = {
        "R1": ("2", "codex_cli"),
        "R2": ("1,3", "claude_code,opencode"),
        "R3": ("", ""),
        "R4": ("bogus", ""),
        "R5": ("9", ""),
        "R6": ("2,2", "codex_cli"),
        "R7": ("0", ""),
        "R8": ("2, 4", "codex_cli,zcode"),
    }
    lines = [parse, "", "$detected = @('claude_code', 'codex_cli', 'opencode', 'zcode')"]
    for label, (answer, _expected) in cases.items():
        call = "ConvertFrom-HostMenuAnswer '" + answer + "' $detected"
        lines.append('"' + label + ':" + ((' + call + ') -join ",")')
    script = tmp_path / "parse-fragment.ps1"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    completed = _run_pwsh_script(script)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    for label, (_answer, expected) in cases.items():
        line = next(row for row in completed.stdout.splitlines() if row.startswith(label + ":"))
        assert line == label + ":" + expected, (label, line, completed.stdout)


# ---------------------------------------------------------------- per-host outcome reporting


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_claude_code_install_reports_hooks_only_and_an_incomplete_onboarding(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["host", "install", "--host", "claude_code"]) == 0
    out = capsys.readouterr().out
    assert "host claude_code: hooks: applied" in out
    assert "host claude_code: mcp: not registered by this installer" in out
    assert "incomplete" in out
    settings = json.loads((isolated_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "hooks" in settings
    assert "mcpServers" not in settings
    assert "mcp" not in settings


def test_codex_cli_install_reports_hooks_mcp_and_pending_trust_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert main(["host", "install", "--host", "codex_cli"]) == 0
    out = capsys.readouterr().out
    assert "host codex_cli: hooks: applied" in out
    assert "host codex_cli: mcp: applied" in out
    assert "host codex_cli: trust: pending" in out
    assert "/hooks" in out
    assert (codex_home / "hooks.json").is_file()
    assert "[mcp_servers.mnemoseed]" in (codex_home / "config.toml").read_text(encoding="utf-8")


def test_zcode_install_reports_hooks_and_mcp_separately_with_no_trust_step(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["host", "install", "--host", "zcode"]) == 0
    out = capsys.readouterr().out
    assert "host zcode: hooks: applied" in out
    assert "host zcode: mcp: applied" in out
    assert "host zcode: trust:" not in out
    data = json.loads((isolated_home / ".zcode" / "cli" / "config.json").read_text(encoding="utf-8"))
    assert data["hooks"]["enabled"] is True
    assert data["mcp"]["servers"]["mnemoseed-local"]["type"] == "stdio"


def test_opencode_install_reports_the_plugin_hook_and_a_manual_mcp_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opencode_root = tmp_path / "opencode-root"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(opencode_root))
    assert main(["hook", "install", "opencode"]) == 0
    out = capsys.readouterr().out
    assert "host opencode: hooks: applied" in out
    assert "host opencode: mcp: manual step" in out
    assert (opencode_root / "plugin" / "mnemoseed-local.ts").is_file()


def test_repeat_install_reports_up_to_date_and_leaves_the_bytes_untouched(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_home = isolated_home / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    opencode_root = isolated_home / "opencode-root"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(opencode_root))
    for args in (
        ["host", "install", "--host", "codex_cli"],
        ["host", "install", "--host", "zcode"],
        ["host", "install", "--host", "claude_code"],
        ["hook", "install", "opencode"],
    ):
        assert main(args) == 0
    capsys.readouterr()
    snapshots = {
        "codex_hooks": (codex_home / "hooks.json").read_bytes(),
        "codex_config": (codex_home / "config.toml").read_bytes(),
        "zcode": (isolated_home / ".zcode" / "cli" / "config.json").read_bytes(),
        "claude": (isolated_home / ".claude" / "settings.json").read_bytes(),
        "opencode": (opencode_root / "plugin" / "mnemoseed-local.ts").read_bytes(),
    }
    for args in (
        ["host", "install", "--host", "codex_cli"],
        ["host", "install", "--host", "zcode"],
        ["host", "install", "--host", "claude_code"],
        ["hook", "install", "opencode"],
    ):
        assert main(args) == 0
    out = capsys.readouterr().out
    for line in (
        "host codex_cli: hooks: up to date",
        "host codex_cli: mcp: up to date",
        "host zcode: hooks: up to date",
        "host zcode: mcp: up to date",
        "host claude_code: hooks: up to date",
        "host opencode: hooks: up to date",
    ):
        assert line in out
    assert snapshots["codex_hooks"] == (codex_home / "hooks.json").read_bytes()
    assert snapshots["codex_config"] == (codex_home / "config.toml").read_bytes()
    assert snapshots["zcode"] == (isolated_home / ".zcode" / "cli" / "config.json").read_bytes()
    assert snapshots["claude"] == (isolated_home / ".claude" / "settings.json").read_bytes()
    assert snapshots["opencode"] == (opencode_root / "plugin" / "mnemoseed-local.ts").read_bytes()


def test_selecting_two_hosts_leaves_the_other_adapters_untouched(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(isolated_home / "codex-home"))
    assert main(["host", "install", "--host", "codex_cli"]) == 0
    assert main(["host", "install", "--host", "zcode"]) == 0
    assert (isolated_home / "codex-home" / "hooks.json").is_file()
    assert (isolated_home / ".zcode" / "cli" / "config.json").is_file()
    assert not (isolated_home / ".claude" / "settings.json").exists()
    assert not (isolated_home / ".config" / "opencode" / "plugin").exists()
