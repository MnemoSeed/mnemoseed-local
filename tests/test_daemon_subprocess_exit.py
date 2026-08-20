"""F2 根治 (PRD-B2.3 append) root-mechanism pins: daemon workers must never
block interpreter exit.

The F2 root cause (PRD-B2.3 根因定案): ThreadPoolExecutor workers are
non-daemon threads registered in the interpreter's atexit join set
(``threading._shutdown``), so a wedged worker keeps the process alive forever.
DaemonExecutor workers are plain daemon threads on a queue — they are never
registered anywhere and die with the process. These subprocess pins prove the
mechanism at process granularity: the DaemonExecutor child exits cleanly while
a worker is wedged; the ThreadPoolExecutor child (the old mechanism, kept as
red documentation) hangs and must be killed. The DaemonExecutor unit pins
cover the bounded-close contract: queued (not-started) work is drained into
the close wait, never left silently running after close returns.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from mnemoseed_local.util.daemon_executor import DaemonExecutor

_SRC = Path(__file__).resolve().parents[1] / "src"

GREEN_SCRIPT = textwrap.dedent(
    """
    import threading

    from mnemoseed_local.util.daemon_executor import DaemonExecutor

    block = threading.Event()
    executor = DaemonExecutor(max_workers=1, thread_name_prefix="mnemoseed-subprocess")
    executor.submit(block.wait)
    # The main thread exits while a worker is wedged inside block.wait(): the
    # daemon worker must not block interpreter exit (the F2 root cause).
    """
)

RED_TPE_SCRIPT = textwrap.dedent(
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    block = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mnemoseed-tpe")
    executor.submit(block.wait)
    # The main thread exits while the non-daemon TPE worker is wedged: the
    # interpreter's atexit join (_threads_queues) hangs forever — the old
    # mechanism this batch replaces.
    """
)


def _run_child(script: str, timeout: float) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def test_daemon_executor_worker_never_blocks_interpreter_exit() -> None:
    """GREEN pin: a process whose DaemonExecutor worker is wedged in a forever
    block still exits with rc 0 — daemon workers die with the process instead
    of holding it alive. A mutant that joins workers (or spawns non-daemon
    threads) hangs the child and fails this pin."""
    result = _run_child(GREEN_SCRIPT, timeout=3)
    assert result.returncode == 0, result.stderr


def test_threadpool_worker_blocks_interpreter_exit_red_documented() -> None:
    """RED documentation pin: the identical shape on ThreadPoolExecutor hangs
    interpreter exit and must be killed — the exact F2 mechanism this batch
    replaces. The pin is red by documentation: it fails only if Python ever
    changes TPE's daemon/join semantics."""
    with pytest.raises(subprocess.TimeoutExpired):
        _run_child(RED_TPE_SCRIPT, timeout=2)


# ------------------------------------------------------- bounded close (NIT-2)


def test_daemon_executor_close_abandons_queued_jobs_not_runs_them_late() -> None:
    """NIT-2: close() must not leave queued (not-started) work silently running
    after it returns. A wedged worker holds the only slot while a second job
    sits queued; close(timeout small) returns bounded, and once the worker is
    released the queued job must be ABANDONED (never run post-close) — the
    pre-fix close only waited the in-flight future, leaving the queued item
    ahead of the sentinels to execute after close returned."""
    block = threading.Event()
    executor = DaemonExecutor(max_workers=1, thread_name_prefix="mnemoseed-close")
    first = executor.submit(block.wait)
    ran: list[str] = []
    second = executor.submit(lambda: ran.append("second"))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not first.running():
        time.sleep(0.01)
    assert first.running(), "the first job never started on the worker"
    assert not second.done(), "the second job must still be queued"

    started = time.monotonic()
    close_thread = threading.Thread(target=lambda: executor.close(timeout=0.2), daemon=True)
    close_thread.start()
    close_thread.join(timeout=1.0)
    elapsed = time.monotonic() - started
    assert not close_thread.is_alive(), "close() hung past the bound (unbounded join?)"
    assert elapsed < 1.0, f"close() was not bounded: {elapsed:.3f}s"

    # release the wedged worker: it must finish the first job and exit, never
    # pick up the drained queued job
    block.set()
    assert first.result(timeout=1.0) is True  # Event.wait() returns True on set
    time.sleep(0.2)  # grace: a post-close run of the queued job would land here
    assert ran == [], "the queued job ran after close returned (silently running post-close)"
    assert not second.done(), "the abandoned queued future must stay pending"


def test_daemon_executor_submit_racing_close_is_lock_ordered() -> None:
    """NIT-2: a submit racing close is lock-ordered — it either lands before
    close drains the queue (its future is waited or abandoned) or raises
    RuntimeError. Never a foreign exception and never a silent post-close run."""
    executor = DaemonExecutor(max_workers=2, thread_name_prefix="mnemoseed-race")
    submitted: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _submit() -> None:
        try:
            executor.submit(lambda: None)
        except RuntimeError:
            with lock:
                submitted.append(False)
        except BaseException as exc:  # pragma: no cover - foreign failures fail the pin
            with lock:
                errors.append(exc)
        else:
            with lock:
                submitted.append(True)

    threads = [threading.Thread(target=_submit, daemon=True) for _ in range(8)]
    closer = threading.Thread(target=lambda: executor.close(timeout=0.05), daemon=True)
    for thread in threads:
        thread.start()
    closer.start()
    for thread in threads:
        thread.join(timeout=3.0)
    closer.join(timeout=3.0)
    assert errors == [], f"a racing submit raised a foreign exception: {errors}"
    assert submitted, "no submit observed the racing close at all"
    assert all(isinstance(value, bool) for value in submitted)
