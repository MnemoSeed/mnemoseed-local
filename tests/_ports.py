"""Loopback port allocation for the test session.

One place in the suite chooses a port. The kernel hands out an ephemeral one on
an explicit bind to port zero, so no test names a port of its own and no test
can collide with the daemon a developer is running.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager

#: The ports the installed daemon owns. A test references one only where it asserts
#: what the default configuration or the documentation says.
RESERVED_PORTS = (4096, 7788)

LOOPBACK_HOST = "127.0.0.1"


def unused_loopback_port() -> int:
    """A loopback port the kernel has not handed out; the probe is closed at once."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return int(probe.getsockname()[1])


@contextmanager
def listening_loopback_socket() -> Iterator[tuple[socket.socket, int]]:
    """A bound, listening loopback socket held open for the block, and its port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((LOOPBACK_HOST, 0))
        listener.listen(128)
        yield listener, int(listener.getsockname()[1])


def reserved_port_occupancy() -> dict[int, bool]:
    """Report each reserved port's occupancy; nothing here refuses a port.

    A bind is the only measurement that needs no privilege and sends nothing, so
    the occupant is never identified and nothing is ever asked of it. A port the
    probe cannot bind is occupied; a port it binds and releases at once is free.
    """
    occupied: dict[int, bool] = {}
    for port in RESERVED_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((LOOPBACK_HOST, port))
            except OSError:
                occupied[port] = True
            else:
                occupied[port] = False
    return occupied
