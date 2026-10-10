"""Session-scoped registry for a test daemon that refused its orderly teardown.

The test-daemon harness is the only writer: a child that has not exited inside
the bounded wait is registered here instead of being terminated where it sits.
The sweep runs at the end of the session, takes the same signal the product
would be given by hand, and logs every step. Nothing else in the suite stops a
process.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

logger = logging.getLogger("mnemoseed_local.tests.session_sweeper")

_lock = threading.Lock()
_outstanding: set[int] = set()


def register(pid: int) -> None:
    """Register a test-daemon process the session must account for."""
    with _lock:
        _outstanding.add(pid)
    logger.warning("registered test daemon pid %d for the session sweep", pid)


def outstanding() -> tuple[int, ...]:
    """The registered processes, oldest first."""
    with _lock:
        return tuple(sorted(_outstanding))


def sweep(code: int = 1) -> None:
    """Terminate every registered process, then forget them.

    This is also the exit seam the test-context Watchdog is handed: the daemon's
    own watchdog is disarmed on an intentional shutdown, so it cannot back a
    child that refuses to leave, and nothing here waits on one.
    """
    for pid in outstanding():
        logger.warning("sweeping test daemon pid %d (code %d)", pid, code)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            logger.warning("sweep could not signal pid %d; it may already be gone", pid)
    _forget()


def _forget() -> None:
    with _lock:
        _outstanding.clear()
