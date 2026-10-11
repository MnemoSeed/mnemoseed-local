"""The test-daemon self-teardown capability.

A test daemon runs on its own ephemeral port, boots through the product's up
path so the watchdog is armed and the shutdown hook is injected before the
listener opens, answers /healthz, and then answers POST /daemon/shutdown and
leaves. The daemon's own watchdog is disarmed on an intentional shutdown, so it
is inert here by design: the backstop for a child that refuses to leave is the
Watchdog the fixture holds outside the child, whose exit function is the session
sweeper. No path in the harness terminates a process where it sits.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import _test_daemon
import pytest
import session_sweeper
from _ports import RESERVED_PORTS
from _test_daemon import daemon_for_test

from mnemoseed_local.rest_client import DaemonClient

TESTS = Path(__file__).resolve().parent
#: The files that own the test-daemon lifecycle. The session sweeper is the
#: only one of them allowed to signal a process.
TEARDOWN_OWNERS = ("_test_daemon.py", "session_sweeper.py")
INLINE_TERMINATION = ("proc.kill(", "os.kill(", "taskkill", "TerminateProcess")


def test_a_test_daemon_answers_healthz_then_shuts_itself_down_and_exits(tmp_path: Path) -> None:
    with daemon_for_test(tmp_path) as daemon:
        assert DaemonClient(base_url=daemon.base_url).get("/healthz")["status"] == "ok"

    assert daemon.shutdown_answer == {"ok": True, "status": "shutting_down"}
    assert daemon.exit_code == 0, "a test daemon must exit when it is done"


def test_a_test_daemon_runs_the_product_up_path_in_an_isolated_home(tmp_path: Path) -> None:
    with daemon_for_test(tmp_path) as daemon:
        assert daemon.port not in RESERVED_PORTS
        assert daemon.argv[1:4] == ("-m", "mnemoseed_local.cli", "up")
        assert "--port" in daemon.argv and str(daemon.port) in daemon.argv
        assert daemon.environment["MNEMOSEED_LOCAL_HOME"] == str(daemon.home)
        assert (daemon.home / "config.toml").is_file()


def test_the_session_sweeper_terminates_a_registered_process() -> None:
    """The authorized cleanup has teeth: a registered process does not come back."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session_sweeper.register(proc.pid)
    try:
        assert proc.poll() is None
        session_sweeper.sweep()
        assert proc.wait(timeout=10.0) != 0, "the swept process must not exit cleanly on its own"
    finally:
        if proc.poll() is None:  # pragma: no cover - the sweep above must have taken it
            session_sweeper.sweep()


def test_a_child_that_never_answered_is_registered_for_the_session_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness failure before the child is owned accounts for it instead of
    stranding it: the spawned process is the registered one, and the failure the
    harness raised still reaches the test."""
    pids: list[int] = []
    real_popen = _test_daemon.subprocess.Popen

    def _recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        proc = real_popen(*args, **kwargs)
        pids.append(proc.pid)
        return proc

    def _explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("simulated harness failure before the backstop")

    monkeypatch.setattr(_test_daemon.subprocess, "Popen", _recording_popen)
    monkeypatch.setattr(_test_daemon, "Watchdog", _explode)

    with pytest.raises(AssertionError, match="simulated harness failure before the backstop"):
        with daemon_for_test(tmp_path):
            pass

    assert pids == list(session_sweeper.outstanding())
    session_sweeper.sweep()


def test_no_test_daemon_path_terminates_a_process_inline() -> None:
    """The sweeper signals a process; nothing in the harness kills one where it sits."""
    offenders: dict[str, list[str]] = {}
    for name in TEARDOWN_OWNERS:
        text = (TESTS / name).read_text(encoding="utf-8")
        tokens = [token for token in INLINE_TERMINATION if token in text]
        if tokens:
            offenders[name] = tokens

    assert offenders == {"session_sweeper.py": ["os.kill("]}
