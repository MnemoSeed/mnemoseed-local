"""Session-wide isolation guards for the test run."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

RESERVED_PORTS = frozenset({7788, 4096})


class ReservedPortError(OSError):
    """Raised when a test reaches for a port reserved for the installed daemon."""


def _port_of(address: object) -> int | None:
    if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[1], int):
        return address[1]
    return None


def _reject_reserved(port: int | None) -> None:
    if port in RESERVED_PORTS:
        raise ReservedPortError(f"tests must not use the reserved port {port}")


@pytest.fixture(autouse=True, scope="session")
def reserved_ports_stay_unused() -> Iterator[None]:
    """Fail any test that binds or dials a port the installed daemon owns."""
    real_bind = socket.socket.bind
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def bind(self: socket.socket, address: object) -> None:
        _reject_reserved(_port_of(address))
        real_bind(self, address)

    def connect(self: socket.socket, address: object) -> None:
        _reject_reserved(_port_of(address))
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        _reject_reserved(_port_of(address))
        return real_connect_ex(self, address)

    def create_connection(address: object, *args: object, **kwargs: object) -> socket.socket:
        _reject_reserved(_port_of(address))
        return real_create_connection(address, *args, **kwargs)  # type: ignore[arg-type]

    socket.socket.bind = bind
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    try:
        yield
    finally:
        socket.socket.bind = real_bind
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        socket.create_connection = real_create_connection
