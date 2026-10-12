"""The session precondition REPORT: measured occupancy and its terminal echo.

The frozen invariant pairs a static source scan with a session precondition:
nothing in the suite binds a reserved port, and what the host already holds is
measured once per session and reported rather than refused. These oracles drive
the measurement against a port that is really bound and a port that is really
free, then pin the terminal summary to exactly that measured state, so neither
the measurement nor its echo can be turned into a no-op silently.
"""

from __future__ import annotations

import _ports
import pytest
from _ports import (
    RESERVED_PORTS,
    listening_loopback_socket,
    reserved_port_occupancy,
    unused_loopback_port,
)


class _TerminalRecorder:
    """The one reporter seam the summary hook writes through."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.lines: list[str] = []

    def write_line(self, line: str) -> None:
        self.lines.append(line)


def test_the_probe_reports_a_bound_port_occupied_and_a_free_one_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The occupancy probe measures a bind: a port held by a live listener is
    occupied, a port the kernel handed out and released is free."""
    with listening_loopback_socket() as (_, occupied_port):
        free_port = unused_loopback_port()
        monkeypatch.setattr(_ports, "RESERVED_PORTS", (occupied_port, free_port))

        assert reserved_port_occupancy() == {occupied_port: True, free_port: False}


def test_the_terminal_summary_echoes_the_measured_occupancy(
    pytestconfig: pytest.Config, reserved_port_report: dict[int, bool]
) -> None:
    """The summary hook prints exactly the state the session precondition
    measured, one sorted line per reserved port, through pytest's own hook."""
    reporter = _TerminalRecorder(pytestconfig)

    pytestconfig.pluginmanager.hook.pytest_terminal_summary(
        terminalreporter=reporter, exitstatus=0, config=pytestconfig
    )

    assert reporter.lines == [
        f"reserved port {port}: {'occupied' if reserved_port_report[port] else 'free'}"
        for port in sorted(reserved_port_report)
    ]
    assert set(reserved_port_report) == set(RESERVED_PORTS)
