"""Ownership oracles for the isolated Atlas verifier's process teardown.

The verifier starts a launcher, so the processes it owns are the ones it started
plus the descendants those processes create. The oracles here pin that cleanup
reclaims that whole tree — including a descendant whose launcher already left —
and that it never reaches a process this run did not start.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _ports import LOOPBACK_HOST, unused_loopback_port

TESTS = Path(__file__).resolve().parent
HARNESS = TESTS.parent / "scripts" / "atlas_verify.py"
LAUNCHER = TESTS / "atlas_teardown_launcher.py"
FIXTURE = TESTS / "atlas_teardown_fixture.py"
READY_TIMEOUT_S = 60.0
REAP_TIMEOUT_S = 30.0
POLL_S = 0.1
HEARTBEAT_PERIOD_S = 1.0
#: Ways of reaching a process other than through a handle or a recorded pid.
IMAGE_SELECTION_PRIMITIVES = ("taskkill", "/IM", "Get-Process", "ProcessName", "tasklist", "wmic")


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    """The harness module under test, loaded from its own path."""
    spec = importlib.util.spec_from_file_location("atlas_verify_under_test", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def await_text(path: Path, timeout: float = READY_TIMEOUT_S) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        time.sleep(POLL_S)
    raise AssertionError(f"the run-owned tree never wrote {path}")


def await_tree_ready(workdir: Path) -> int:
    """The grandchild's pid, once the grandchild has bound its port and beaten."""
    grandchild = int(await_text(workdir / "grandchild.pid"))
    await_text(workdir / "heartbeat")
    return grandchild


def await_heartbeat_frozen(workdir: Path, timeout: float = REAP_TIMEOUT_S) -> None:
    """The grandchild is gone once its heartbeat stops advancing."""
    heartbeat = workdir / "heartbeat"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        before = heartbeat.read_text(encoding="utf-8")
        time.sleep(HEARTBEAT_PERIOD_S)
        if heartbeat.read_text(encoding="utf-8") == before:
            return
    raise AssertionError("the run-owned grandchild outlived the teardown")


def await_port_release(port: int, timeout: float = REAP_TIMEOUT_S) -> None:
    """The port the grandchild bound is free once this run can bind it again."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            try:
                probe.bind((LOOPBACK_HOST, port))
            except OSError:
                time.sleep(POLL_S)
            else:
                return
    raise AssertionError(f"the run-owned listener still holds port {port}")


def launched_tree(harness: ModuleType, workdir: Path, port: int) -> Any:
    """Own a launcher tree the way the verifier does, and wait for the launcher to leave."""
    workdir.mkdir(exist_ok=True)
    daemon = harness.spawn_owned_daemon(
        [sys.executable, str(LAUNCHER), str(workdir), LOOPBACK_HOST, str(port)],
        env=os.environ.copy(),
        cwd=str(workdir),
    )
    await_tree_ready(workdir)
    assert daemon.process.wait(timeout=REAP_TIMEOUT_S) == 0, "the launcher did not leave first"
    return daemon


def test_descendants_of_a_launcher_that_left_are_reaped(harness: ModuleType, tmp_path: Path) -> None:
    """The core oracle: the launcher is gone, and its grandchild still must be reclaimed."""
    port = unused_loopback_port()
    workdir = tmp_path / "tree"
    daemon = launched_tree(harness, workdir, port)
    grandchild_pid = int(await_text(workdir / "grandchild.pid"))

    reported = harness.stop_owned_daemon(daemon, f"http://{LOOPBACK_HOST}:{port}")

    assert daemon.pid in reported
    if os.name == "nt":
        assert grandchild_pid in reported
    await_heartbeat_frozen(workdir)
    await_port_release(port)


def test_unrelated_processes_are_never_selected_for_cleanup(harness: ModuleType, tmp_path: Path) -> None:
    """A process this run did not start is never a cleanup target."""
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], cwd=str(tmp_path))
    try:
        port = unused_loopback_port()
        daemon = launched_tree(harness, tmp_path / "tree", port)

        reported = harness.stop_owned_daemon(daemon, f"http://{LOOPBACK_HOST}:{port}")

        assert unrelated.pid not in reported
        assert unrelated.poll() is None, "cleanup took a process this run did not start"
    finally:
        unrelated.kill()
        unrelated.wait(timeout=REAP_TIMEOUT_S)


@pytest.mark.parametrize("mode", ["completed", "raised", "killed"])
def test_every_verifier_exit_path_reaps_the_run_owned_tree(tmp_path: Path, mode: str) -> None:
    """Completion, failure and a kill from outside each leave no descendant behind."""
    if mode == "killed" and os.name != "nt":
        pytest.skip("a killed verifier unwinds nothing; only a Windows job object reclaims its tree")
    port = unused_loopback_port()
    workdir = tmp_path / f"tree-{mode}"
    verifier = subprocess.Popen(
        [sys.executable, str(FIXTURE), str(workdir), str(LAUNCHER), str(port), mode],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        grandchild_pid = await_tree_ready(workdir)
        assert verifier.poll() is None
        if mode == "killed":
            verifier.kill()
        verifier.wait(timeout=REAP_TIMEOUT_S)
        output = verifier.stdout.read() if verifier.stdout is not None else ""

        await_heartbeat_frozen(workdir)
        await_port_release(port)
        if mode != "killed":
            assert "Run-owned PIDs:" in output, output
        if mode == "raised":
            assert verifier.returncode != 0, output
        if os.name == "nt" and mode != "killed":
            assert str(grandchild_pid) in output, output
    finally:
        if verifier.poll() is None:
            verifier.kill()
            verifier.wait(timeout=REAP_TIMEOUT_S)


def test_teardown_reaches_no_process_through_its_image() -> None:
    """Selection through an executable image would reach processes this run never started."""
    source = HARNESS.read_text(encoding="utf-8")

    for primitive in IMAGE_SELECTION_PRIMITIVES:
        assert primitive not in source, primitive
