"""The test-daemon lifecycle: a real daemon on its own port that tears itself down.

The child runs ``mnemoseed-local up`` — the product's own start path — so the
watchdog is armed and the shutdown hook is injected before the listener opens.
Readiness waits on the child's own /healthz through the daemon client, so the
same credential seam a future boundary adds is the one this fixture rides; the
teardown asks the child's POST /daemon/shutdown and then asserts that the
process is actually gone.

The daemon's own watchdog is disarmed on an intentional shutdown, so it is inert
here by design. The backstop is the Watchdog this context holds outside the
child, whose exit function is the session sweeper: a child that refuses to leave
is registered with the sweeper, never terminated where it sits.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import session_sweeper
from _ports import LOOPBACK_HOST, unused_loopback_port

from mnemoseed_local.cli import _OFF_POLL_TIMEOUT_S
from mnemoseed_local.credential import DaemonCredentialError, read_credential_at
from mnemoseed_local.daemon.watchdog import Watchdog, default_probe
from mnemoseed_local.rest_client import DaemonClient, DaemonRestError, DaemonUnavailableError

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_REPO_ROOT / "src")
_READY_TIMEOUT_S = 60.0
#: The actor a test daemon's audit entries are attributed to.
TEST_ACTOR = "test"
#: The product gives up on a drain after ``_OFF_POLL_TIMEOUT_S``; the child must
#: be gone well inside that, so the bound is the product's own, doubled.
_EXIT_TIMEOUT_S = 2 * _OFF_POLL_TIMEOUT_S
_SHUTDOWN_ANSWER = {"ok": True, "status": "shutting_down"}


def _embedded_config_text(workdir: Path, port: int) -> str:
    """A bootable embedded config whose every store lands in the caller's temp dir."""
    return (
        f'baseurl = "http://{LOOPBACK_HOST}:{port}"\n'
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(workdir / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(workdir / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(workdir / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(workdir / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
    )


@dataclass
class DaemonUnderTest:
    """A daemon child on its own loopback port, and what its teardown observed."""

    proc: subprocess.Popen[str]
    base_url: str
    port: int
    home: Path
    argv: tuple[str, ...]
    environment: dict[str, str]
    health: dict[str, Any] | None = None
    shutdown_answer: dict[str, Any] | None = None
    exit_code: int | None = None
    backstop: Watchdog | None = field(default=None, repr=False)


@contextmanager
def daemon_for_test(tmp_path: Path) -> Iterator[DaemonUnderTest]:
    """Boot a daemon child on an ephemeral port and tear it down through the product."""
    port = unused_loopback_port()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(_embedded_config_text(tmp_path, port), encoding="utf-8")
    # The interpreter directly, not through a virtualenv launcher: a launcher
    # stands a trampoline in front of the real process, and the pid that waits
    # and signals must be the one that serves the listener. The editable install
    # the launcher would have found is handed over as a path entry.
    argv = (
        sys._base_executable,
        "-m",
        "mnemoseed_local.cli",
        "up",
        "--host",
        LOOPBACK_HOST,
        "--port",
        str(port),
    )
    environment = {
        **os.environ,
        "MNEMOSEED_LOCAL_HOME": str(home),
        "PYTHONPATH": os.pathsep.join(
            filter(None, [_SRC, sysconfig.get_paths()["purelib"], os.environ.get("PYTHONPATH", "")])
        ),
    }
    proc = subprocess.Popen(
        list(argv),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    daemon = DaemonUnderTest(
        proc=proc,
        base_url=f"http://{LOOPBACK_HOST}:{port}",
        port=port,
        home=home,
        argv=argv,
        environment=environment,
    )
    # The child is owned from the moment it exists: a failure on the path to
    # readiness still accounts for it instead of stranding it.
    try:
        daemon.backstop = Watchdog(LOOPBACK_HOST, port, probe=default_probe, exit_func=session_sweeper.sweep)
        daemon.backstop.start()
        _await_readiness(daemon)
        yield daemon
    finally:
        if daemon.backstop is not None:
            daemon.backstop.stop()
        _tear_down(daemon)


def _await_readiness(daemon: DaemonUnderTest) -> None:
    """Wait for the child's own /healthz to answer, and record what it said."""
    client = DaemonClient(base_url=daemon.base_url, timeout=1.0)
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while True:
        if daemon.proc.poll() is not None:
            raise AssertionError(f"the daemon child exited before readiness (code {daemon.proc.returncode})")
        try:
            health = client.get("/healthz")
        except (DaemonRestError, DaemonUnavailableError):
            if time.monotonic() >= deadline:
                raise AssertionError("the daemon child never answered /healthz") from None
            time.sleep(0.1)
        else:
            daemon.health = health
            return


def _tear_down(daemon: DaemonUnderTest) -> None:
    """Ask the child to stop, then assert that it really left.

    A child that never answered is registered for the session sweep instead of
    being waited on: nothing orderly can be asked of a listener that never
    opened, and an unaccounted-for process is the defect this avoids.
    """
    if daemon.proc.poll() is None:
        if daemon.health is None:
            session_sweeper.register(daemon.proc.pid)
        else:
            answer = _shutdown_client(daemon).post("/daemon/shutdown")
            assert answer == _SHUTDOWN_ANSWER, answer
            daemon.shutdown_answer = answer
            _assert_exited(daemon)


def _shutdown_client(daemon: DaemonUnderTest) -> DaemonClient:
    """The child's own client, carrying the credential the child issued.

    The credential lives in the child's isolated home, so it is read from that
    path — never from the config home of the process running the test.
    """
    try:
        credential = read_credential_at(daemon.home)
    except DaemonCredentialError as exc:
        raise AssertionError(f"the daemon child issued no credential: {exc}") from None
    return DaemonClient(base_url=daemon.base_url, actor=TEST_ACTOR, token=credential)


def _assert_exited(daemon: DaemonUnderTest) -> None:
    """Assert the child is gone; register it with the sweeper when it refuses.

    A refusal is a defect, so the test fails and the process is registered for
    the session sweep instead of being terminated where it sits.
    """
    try:
        daemon.exit_code = daemon.proc.wait(timeout=_EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        session_sweeper.register(daemon.proc.pid)
        raise AssertionError(
            f"the daemon child (pid {daemon.proc.pid}) refused to exit within "
            f"{_EXIT_TIMEOUT_S:g}s; registered with the session sweeper"
        ) from None
    assert daemon.exit_code == 0, daemon.exit_code
