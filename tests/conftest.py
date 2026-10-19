"""Session-wide isolation: the reserved-port report, the sweeper drain, and the
reload guard.

Nothing here intercepts a socket. The reserved ports are watched by a static
scan of the test sources (``test_port_isolation``); the session precondition
measures them once and reports what it found, so a developer who is running the
daemon is told about it rather than blocked; and a test daemon that refused its
orderly teardown is accounted for by the session sweeper at the end.

The reload guard is armed before any test module is imported, so a test that
reloads a module defining an exception fails where the reload is written instead
of failing unrelated files later in the session.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
import reload_guard
import session_sweeper
from _ports import reserved_port_occupancy

reload_guard.activate()

logger = logging.getLogger("mnemoseed_local.tests.isolation")

_OCCUPANCY_AT_SESSION_START: dict[int, bool] = {}


@pytest.fixture(autouse=True, scope="session")
def reserved_port_report() -> Iterator[dict[int, bool]]:
    """Report the reserved ports' occupancy once per session, and refuse nothing.

    Occupancy is a fact about the host, not a defect in the suite: no test binds
    or dials a reserved port, so the session runs the same either way and only
    says what it saw.
    """
    occupancy = reserved_port_occupancy()
    _OCCUPANCY_AT_SESSION_START.clear()
    _OCCUPANCY_AT_SESSION_START.update(occupancy)
    for port in sorted(occupancy):
        logger.info("reserved port %d: %s", port, "occupied" if occupancy[port] else "free")
    yield occupancy


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Print the precondition's report where the developer running the suite sees it."""
    for port in sorted(_OCCUPANCY_AT_SESSION_START):
        state = "occupied" if _OCCUPANCY_AT_SESSION_START[port] else "free"
        terminalreporter.write_line(f"reserved port {port}: {state}")


@pytest.fixture(autouse=True, scope="session")
def session_sweep() -> Iterator[None]:
    """The session sweeper is the only authorized cleanup for a leftover daemon."""
    yield
    session_sweeper.sweep()
