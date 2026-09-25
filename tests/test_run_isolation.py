"""Run-isolation oracles for the Windows-only test surface and the live daemon port."""

from __future__ import annotations

import importlib.util
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

TESTS = Path(__file__).resolve().parent
AUTOSTART_SUITE = TESTS / "test_windows_autostart.py"
AUTOSTART_FIXTURE = TESTS / "windows_autostart_fixture.ps1"
LIVE_DAEMON_PORT = 7788
AMBIENT_FIXTURES = (
    (TESTS / "windows_task_fixture.ps1", "USERNAME", "User"),
    (TESTS / "windows_install_bootstrap_fixture.ps1", "USERNAME", "User"),
    (TESTS / "windows_install_migration_fixture.ps1", "USERNAME", "User"),
    (TESTS / "windows_install_migration_fixture.ps1", "LOCALAPPDATA", "LocalAppData"),
    (TESTS / "windows_install_operation_fixture.ps1", "USERNAME", "User"),
    (TESTS / "windows_install_operation_fixture.ps1", "LOCALAPPDATA", "LocalAppData"),
)
REAL_PROBE_CALLS = ("Get-NetTCPConnection", "Invoke-RestMethod", "Test-NetConnection", "TcpClient")


def _child_env(tmp_path: Path) -> dict[str, str]:
    inherited = [entry for entry in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if entry]
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(tmp_path), *inherited]),
        "MNEMOSEED_LOCAL_HOME": str(home),
        # Points the suite's own PowerShell lookup at a file that exists, so a
        # skip in the child can only come from the host-platform guard.
        "PWSH": sys.executable,
    }


def _autostart_skip_conditions(platform: str) -> tuple[object, ...]:
    """Evaluate the suite's own skip conditions for a simulated host platform.

    PowerShell availability is held open with an interpreter path so that only
    the host-platform condition can decide the outcome. The conditions are read
    from the module instead of faking the platform for a real run, because the
    standard library branches on ``sys.platform`` itself.
    """
    spec = importlib.util.spec_from_file_location(f"_autostart_for_{platform}", AUTOSTART_SUITE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "platform", platform), mock.patch.dict(os.environ, {"PWSH": sys.executable}):
        spec.loader.exec_module(module)

    marks = module.pytestmark if isinstance(module.pytestmark, list) else [module.pytestmark]
    return tuple(
        mark.args[0] if mark.args else mark.kwargs.get("condition") for mark in marks if mark.name == "skipif"
    )


def test_autostart_suite_runs_nothing_on_a_non_windows_platform(tmp_path: Path) -> None:
    """The Windows PowerShell suite is a no-op wherever Windows itself is absent."""
    (tmp_path / "host_platform.py").write_text("import sys\n\nsys.platform = 'linux'\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(AUTOSTART_SUITE),
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "-p",
            "host_platform",
        ],
        env=_child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    report = completed.stdout + completed.stderr

    skipped = re.search(r"(\d+) skipped", report)
    assert completed.returncode == 0, report
    assert skipped is not None and int(skipped.group(1)) > 0, report
    assert " passed" not in report, report
    assert " failed" not in report, report
    assert " error" not in report, report


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_autostart_suite_is_skipped_wherever_windows_is_absent(platform: str) -> None:
    conditions = _autostart_skip_conditions(platform)

    assert conditions, "the suite declares no skip condition"
    assert any(conditions), platform


def test_autostart_suite_still_runs_on_windows() -> None:
    """The host guard must skip off Windows only, never disable the suite."""
    assert not any(_autostart_skip_conditions("win32"))


def test_autostart_suite_guard_follows_the_host_platform() -> None:
    """A host-sensitive condition must exist, not only a PowerShell check."""
    assert _autostart_skip_conditions("win32") != _autostart_skip_conditions("linux")


def test_test_session_refuses_the_live_daemon_port() -> None:
    """No test may bind or dial the port the installed daemon owns."""
    with socket.socket() as probe, pytest.raises(OSError) as caught:
        probe.bind(("127.0.0.1", LIVE_DAEMON_PORT))

    assert str(LIVE_DAEMON_PORT) in str(caught.value)


def test_autostart_fixture_intercepts_every_daemon_probe() -> None:
    """The fixture answers the port and health probes itself, so the shipped
    fallbacks that read a real port are never reached from a test."""
    fixture = AUTOSTART_FIXTURE.read_text(encoding="utf-8")

    assert re.search(r"PortProbe\s*=\s*\$portProbe", fixture)
    assert re.search(r"HealthProbe\s*=\s*\$healthProbe", fixture)
    for real_probe in REAL_PROBE_CALLS:
        assert real_probe not in fixture, real_probe


@pytest.mark.parametrize(
    ("fixture", "ambient", "parameter"),
    AMBIENT_FIXTURES,
    ids=[f"{path.name}:{ambient}" for path, ambient, _ in AMBIENT_FIXTURES],
)
def test_windows_fixtures_take_identity_from_the_caller(fixture: Path, ambient: str, parameter: str) -> None:
    """A fixture that reads an ambient identity variable binds an empty string
    wherever the host does not define it, so the caller supplies both values."""
    text = fixture.read_text(encoding="utf-8")

    assert re.search(rf"\[string\]\${parameter}\b", text), parameter
    assert re.search(rf"\[Parameter\(Mandatory\s*=\s*\$true\)\]\s*\[string\]\${parameter}\b", text), parameter
    assert f"$env:{ambient}" not in text, ambient
