from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / "install.ps1").read_text(encoding="utf-8")
BOOTSTRAP = (ROOT / "scripts" / "windows-logon.ps1").read_text(encoding="utf-8")
TASK_REGISTRAR = ROOT / "scripts" / "windows-logon-task.ps1"
BOOTSTRAP_FIXTURE = ROOT / "tests" / "windows_autostart_fixture.ps1"
TASK_FIXTURE = ROOT / "tests" / "windows_task_fixture.ps1"
INSTALL_HELPERS = ROOT / "scripts" / "windows-install-helpers.ps1"
INSTALL_OPERATION_FIXTURE = ROOT / "tests" / "windows_install_operation_fixture.ps1"
HELPER_RESOLUTION_FIXTURE = ROOT / "tests" / "windows_helper_resolution_fixture.ps1"
INSTALL_BOOTSTRAP_FIXTURE = ROOT / "tests" / "windows_install_bootstrap_fixture.ps1"
PWSH = os.environ.get("PWSH", "pwsh")
TEST_USER = "mnemoseed-test-user"

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows autostart tests require Windows"),
    pytest.mark.skipif(
        shutil.which(PWSH) is None and not Path(PWSH).exists(),
        reason="Windows PowerShell tests require pwsh",
    ),
]


def run_fixture(command: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    local_app_data = str(tmp_path / "LocalAppData")
    return subprocess.run(
        command,
        env={
            **os.environ,
            "MNEMOSEED_LOCAL_HOME": str(tmp_path / "home"),
            "USERNAME": TEST_USER,
            "LOCALAPPDATA": local_app_data,
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def task_fixture_command(
    tmp_path: Path, scenario: str, *, result: Path | None = None, uninstall: bool = False
) -> list[str]:
    command = [
        PWSH,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(TASK_FIXTURE),
        "-TaskScript",
        str(TASK_REGISTRAR),
        "-Bootstrap",
        str(ROOT / "scripts" / "windows-logon.ps1"),
        "-User",
        TEST_USER,
        "-Scenario",
        scenario,
        "-Result",
        str(result or tmp_path / "task-result.json"),
    ]
    if uninstall:
        command.append("-Uninstall")
    return command


def run_bootstrap(tmp_path: Path, scenario: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            PWSH,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(BOOTSTRAP_FIXTURE),
            "-Bootstrap",
            str(ROOT / "scripts" / "windows-logon.ps1"),
            "-Scenario",
            scenario,
            "-Result",
            str(tmp_path / "result.txt"),
        ],
        env={**os.environ, "MNEMOSEED_LOCAL_HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def run_task(tmp_path: Path, scenario: str, *, uninstall: bool = False) -> subprocess.CompletedProcess[str]:
    return run_fixture(task_fixture_command(tmp_path, scenario, uninstall=uninstall), tmp_path)


def result_text(tmp_path: Path) -> str:
    result = tmp_path / "result.txt"
    return result.read_text(encoding="utf-8") if result.exists() else ""


def test_off_before_start_does_not_probe_or_launch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "daemon.off").touch()
    completed = run_bootstrap(tmp_path, "off-before")

    assert completed.returncode == 0
    assert "launch" not in result_text(tmp_path)
    assert "Ollama" not in completed.stderr


def test_off_during_ollama_wait_does_not_launch_daemon(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "off-during")

    assert completed.returncode == 0
    assert result_text(tmp_path) == "marked"
    assert "launch" not in result_text(tmp_path)


def test_healthy_existing_daemon_is_idempotent(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "healthy")

    assert completed.returncode == 0
    assert "launch" not in result_text(tmp_path)


def test_healthy_response_without_listener_pid_is_rejected(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "unknown-healthy")

    assert completed.returncode == 1
    assert "no inspectable listener PID" in completed.stderr
    assert "launch" not in result_text(tmp_path)


def test_listener_pid_must_be_stable_around_health(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "unstable-pid")

    assert completed.returncode == 1
    assert "changed PID" in completed.stderr
    assert "launch" not in result_text(tmp_path)


def test_ollama_timeout_diagnoses_and_starts_memory_daemon(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "ollama-timeout")

    assert completed.returncode == 0
    assert "Ollama API" in completed.stderr
    assert "starting the MnemoSeed daemon anyway" in completed.stderr
    assert "OllamaTimeoutSeconds = 30" in BOOTSTRAP
    assert "launch" in result_text(tmp_path)


def test_unknown_port_owner_is_rejected(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "unknown-port")

    assert completed.returncode == 1
    assert "unknown owner" in completed.stderr
    assert "launch" not in result_text(tmp_path)


def test_unrelated_executable_with_mnemoseed_label_is_rejected(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "foreign-label")

    assert completed.returncode == 1
    assert "unknown owner" in completed.stderr
    assert "launch" not in result_text(tmp_path)


def test_mismatched_executable_is_rejected_even_with_exact_command_shape(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "mismatched-executable")

    assert completed.returncode == 1
    assert "unknown owner" in completed.stderr
    assert "launch" not in result_text(tmp_path)


def test_mnemoseed_owned_unhealthy_listener_is_rejected(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "mnemoseed-owned-unhealthy")

    assert completed.returncode == 1
    assert "health check failed" in completed.stderr
    assert "launch" not in result_text(tmp_path)


def test_bootstrap_never_starts_ollama_or_pulls_a_model(tmp_path: Path) -> None:
    completed = run_bootstrap(tmp_path, "healthy")

    assert completed.returncode == 0
    assert "Start-Process" not in BOOTSTRAP
    assert "ollama serve" not in BOOTSTRAP.lower()
    assert "ollama pull" not in BOOTSTRAP.lower()
    assert "pull" not in result_text(tmp_path)


def test_repeat_trigger_is_serialized_by_named_mutex(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = tmp_path / "result.txt"
    env = {**os.environ, "MNEMOSEED_LOCAL_HOME": str(home)}
    command = [
        PWSH,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(BOOTSTRAP_FIXTURE),
        "-Bootstrap",
        str(ROOT / "scripts" / "windows-logon.ps1"),
        "-Scenario",
        "launch",
        "-Result",
        str(result),
    ]
    first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not result.exists():
            time.sleep(0.05)
        assert result.exists(), "first trigger did not reach the launcher"
        second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15, check=False)
        first_stdout, first_stderr = first.communicate(timeout=15)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)

    assert first.returncode == 0, first_stdout + first_stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert result.read_text(encoding="utf-8").count("launch") == 1


@pytest.mark.parametrize("scenario", ["missing", "drift"])
def test_installer_task_registration_repairs_missing_or_owned_drift_task(
    tmp_path: Path, scenario: str
) -> None:
    result = tmp_path / "task-result.json"
    completed = run_fixture(task_fixture_command(tmp_path, scenario, result=result), tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    registration = result.read_text(encoding="utf-8")
    expected_force = "true" if scenario == "drift" else "false"
    assert f'"Force":{expected_force}' in registration
    assert '"Execute":"powershell.exe"' in registration


def test_unknown_installer_task_is_refused_without_force(tmp_path: Path) -> None:
    result = tmp_path / "task-result.json"
    completed = run_fixture(task_fixture_command(tmp_path, "malformed", result=result), tmp_path)

    assert completed.returncode == 1
    assert "not provably owned" in completed.stderr
    assert not result.exists()


def test_foreign_same_principal_task_is_refused_without_force(tmp_path: Path) -> None:
    result = tmp_path / "task-result.json"
    completed = run_fixture(task_fixture_command(tmp_path, "foreign", result=result), tmp_path)

    assert completed.returncode == 1
    assert "not provably owned" in completed.stderr
    assert not result.exists()

    result = tmp_path / "task-result.json"
    completed = run_fixture(task_fixture_command(tmp_path, "correct", result=result), tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "already registered" in completed.stdout
    assert not result.exists()


def test_foreign_same_metadata_powershell_payload_is_refused_without_force(
    tmp_path: Path,
) -> None:
    completed = run_task(tmp_path, "foreign-payload")

    assert completed.returncode == 1
    assert "not provably owned" in completed.stderr
    assert not (tmp_path / "task-result.json").exists()


def test_product_identified_task_with_settings_drift_is_repaired(tmp_path: Path) -> None:
    completed = run_task(tmp_path, "owned-settings-drift")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = (tmp_path / "task-result.json").read_text(encoding="utf-8")
    assert '"Force":true' in result
    assert "urn:mnemoseed-local:task:windows-logon-daemon:v1" in result


def test_uninstall_removes_only_exact_owned_task_path(tmp_path: Path) -> None:
    completed = run_task(tmp_path, "uninstall-owned", uninstall=True)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = (tmp_path / "task-result.json").read_text(encoding="utf-8")
    assert '"Uninstalled":true' in result
    assert "\\MnemoSeedLocal\\" in result


def test_uninstall_refuses_foreign_task_at_owned_path(tmp_path: Path) -> None:
    completed = run_task(tmp_path, "uninstall-foreign", uninstall=True)

    assert completed.returncode == 1
    assert "not provably owned" in completed.stderr
    assert not (tmp_path / "task-result.json").exists()


def test_uninstall_refuses_ambiguous_same_name_task_outside_owned_path(
    tmp_path: Path,
) -> None:
    completed = run_task(tmp_path, "uninstall-ambiguous", uninstall=True)

    assert completed.returncode == 1
    assert "outside" in completed.stderr
    assert not (tmp_path / "task-result.json").exists()


def test_installer_does_not_manage_ollama_startup() -> None:
    lowered = (INSTALL + INSTALL_HELPERS.read_text(encoding="utf-8")).lower()
    assert "migratelegacyollamatask" in lowered
    assert "register-scheduledtask" not in lowered
    assert "start-process" not in lowered
    assert "start-process" not in lowered
    assert "native" in lowered
    assert "tray" in lowered


def run_legacy_migration(tmp_path: Path, scenario: str) -> subprocess.CompletedProcess[str]:
    return run_fixture(
        [
            PWSH,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "tests" / "windows_install_migration_fixture.ps1"),
            "-TaskScript",
            str(TASK_REGISTRAR),
            "-Bootstrap",
            str(ROOT / "scripts" / "windows-logon.ps1"),
            "-User",
            TEST_USER,
            "-LocalAppData",
            str(tmp_path / "LocalAppData"),
            "-Scenario",
            scenario,
            "-Result",
            str(tmp_path / "migration-result.json"),
        ],
        tmp_path,
    )


def run_install_operation(tmp_path: Path, scenario: str) -> subprocess.CompletedProcess[str]:
    return run_fixture(
        [
            PWSH,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(INSTALL_OPERATION_FIXTURE),
            "-Helpers",
            str(INSTALL_HELPERS),
            "-TaskScript",
            str(TASK_REGISTRAR),
            "-Bootstrap",
            str(ROOT / "scripts" / "windows-logon.ps1"),
            "-User",
            TEST_USER,
            "-LocalAppData",
            str(tmp_path / "LocalAppData"),
            "-Scenario",
            scenario,
            "-Result",
            str(tmp_path / "operation-events.json"),
        ],
        tmp_path,
    )


@pytest.mark.parametrize("scenario", ["owned", "missing"])
def test_installer_operation_migrates_then_registers_daemon(tmp_path: Path, scenario: str) -> None:
    completed = run_install_operation(tmp_path, scenario)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    events = (tmp_path / "operation-events.json").read_text(encoding="utf-8")
    assert '"Call":"lookup"' in events
    assert '"Call":"unregister"' in events if scenario == "owned" else '"Call":"unregister"' not in events
    assert events.rfind('"Migrate":true') < events.rfind('"Migrate":false')


@pytest.mark.parametrize("scenario", ["foreign", "drifted"])
def test_installer_operation_does_not_register_after_rejected_migration(
    tmp_path: Path, scenario: str
) -> None:
    completed = run_install_operation(tmp_path, scenario)
    assert completed.returncode != 0
    assert "preserved" in completed.stdout.lower() + completed.stderr.lower()
    assert '"Call":"invoke"' not in (tmp_path / "operation-events.json").read_text(encoding="utf-8")


def test_installer_removes_only_exact_legacy_ollama_task(tmp_path: Path) -> None:
    completed = run_legacy_migration(tmp_path, "owned")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"Unregistered":true' in (tmp_path / "migration-result.json").read_text()


@pytest.mark.parametrize("scenario", ["foreign", "drifted"])
def test_installer_preserves_foreign_or_drifted_ollama_task(tmp_path: Path, scenario: str) -> None:
    completed = run_legacy_migration(tmp_path, scenario)
    assert completed.returncode != 0
    assert "manual" in completed.stderr.lower()
    assert not (tmp_path / "migration-result.json").exists()


@pytest.mark.parametrize(
    ("scenario", "success", "downloads"),
    [
        ("success", True, 2),
        ("invalid", False, 2),
        ("second-download-fails", False, 2),
        ("siblings", True, 0),
    ],
)
def test_installer_helper_resolution_behavior(
    tmp_path: Path, scenario: str, success: bool, downloads: int
) -> None:
    import json

    completed = subprocess.run(
        [
            PWSH,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(HELPER_RESOLUTION_FIXTURE),
            "-Helpers",
            str(INSTALL_HELPERS),
            "-Scenario",
            scenario,
            "-Root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["Result"]["Success"] is success
    if success:
        assert payload["Result"]["DownloadCount"] == downloads
    else:
        assert payload["StagingExists"] is False


def test_remote_installer_resolves_and_validates_helpers() -> None:
    assert "Initialize-MnemoSeedHelperModule" in INSTALL
    assert "windows-install-helpers.ps1" in INSTALL
    assert "Resolve-MnemoSeedTaskHelpers" in INSTALL
    assert "Invoke-MnemoSeedTaskInstallation" in INSTALL


def test_installer_bootstrap_executes_extracted_functions_with_empty_script_root(
    tmp_path: Path,
) -> None:
    import json

    completed = subprocess.run(
        [
            PWSH,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(INSTALL_BOOTSTRAP_FIXTURE),
            "-Installer",
            str(ROOT / "install.ps1"),
            "-HelperModule",
            str(INSTALL_HELPERS),
            "-LogonScript",
            str(ROOT / "scripts" / "windows-logon.ps1"),
            "-TaskScript",
            str(TASK_REGISTRAR),
            "-User",
            TEST_USER,
            "-Scenario",
            "remote-success",
            "-Root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["Success"] is True
    assert payload["StagingExists"] is False
    assert any(event.endswith("windows-install-helpers.ps1") for event in payload["Events"])
    assert any(event.endswith("windows-logon.ps1") for event in payload["Events"])
    assert any(event.endswith("windows-logon-task.ps1") for event in payload["Events"])
    assert "lookup:OllamaHeadlessServe" in payload["Events"]
    assert "register:MnemoSeedLocalDaemon" in payload["Events"]


@pytest.mark.parametrize("scenario", ["local-siblings", "module-syntax-failure", "partial-download"])
def test_installer_bootstrap_precedence_and_failure_cleanup(tmp_path: Path, scenario: str) -> None:
    import json

    completed = subprocess.run(
        [
            PWSH,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(INSTALL_BOOTSTRAP_FIXTURE),
            "-Installer",
            str(ROOT / "install.ps1"),
            "-HelperModule",
            str(INSTALL_HELPERS),
            "-LogonScript",
            str(ROOT / "scripts" / "windows-logon.ps1"),
            "-TaskScript",
            str(TASK_REGISTRAR),
            "-User",
            TEST_USER,
            "-Scenario",
            scenario,
            "-Root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["StagingExists"] is False
    if scenario == "local-siblings":
        assert payload["Success"] is True
        assert not any(event.startswith("download:") for event in payload["Events"])
    else:
        assert payload["Success"] is False
        assert not any(event.startswith("register:") for event in payload["Events"])


def test_install_dry_run_documents_exact_legacy_migration(tmp_path: Path) -> None:
    completed = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "install.ps1"), "-DryRun"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "remove only the exact root OllamaHeadlessServe task" in completed.stdout
    assert "preserve and reject a same-name foreign/drifted task" in completed.stdout


def test_daemon_task_contract_is_explicit() -> None:
    assert TASK_REGISTRAR.exists()
    assert "Invoke-MnemoSeedTaskInstallation -TaskScript $MnemoSeedTaskRegistration" in INSTALL
    assert "Invoke-MnemoSeedTaskInstallation -TaskScript $MnemoSeedTaskRegistration" in INSTALL
    task_text = TASK_REGISTRAR.read_text(encoding="utf-8")
    assert "-AtLogOn" in task_text
    assert "-MultipleInstances IgnoreNew" in task_text
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in task_text
    assert "-Hidden" in task_text
    assert "-WindowStyle Hidden" in task_text
    assert "daemon.off" in BOOTSTRAP
    assert "RestartCount" in task_text
    assert "RestartInterval" in task_text
    assert "urn:mnemoseed-local:task:windows-logon-daemon:v1" in task_text
    assert "[string]$TaskIdentity" in BOOTSTRAP
    assert "$TaskIdentity -ne $TaskIdentityUri" in BOOTSTRAP
