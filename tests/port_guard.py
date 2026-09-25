"""Reserved-port guard shared by the test session and by every child it starts."""

from __future__ import annotations

import socket
from typing import Any

RESERVED_PORTS = frozenset({7788, 4096})
GUARD_MARKER = "_mnemoseed_reserved_ports"
ORIGINALS = "_mnemoseed_reserved_port_originals"
_MODULE_PATCH = "create_connection"


class ReservedPortError(OSError):
    """Raised when a test reaches for a port reserved for the installed daemon."""


def installed_ports() -> frozenset[int]:
    """The ports this process refuses; empty when the guard is not installed."""
    return frozenset(getattr(socket, GUARD_MARKER, ()))


def guarded_operations() -> frozenset[str]:
    """The socket entry points this process refuses a reserved port on."""
    return frozenset(getattr(socket, ORIGINALS, ()))


def patched_originals() -> dict[str, Any]:
    """The callables the guard replaced, for a caller to stand a recorder in for."""
    return getattr(socket, ORIGINALS, {})


def _port_of(address: object) -> int | None:
    if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[1], int):
        return address[1]
    return None


def _reject_reserved(port: int | None) -> None:
    if port in RESERVED_PORTS:
        raise ReservedPortError(f"tests must not use the reserved port {port}")


def _original(name: str) -> Any:
    return getattr(socket, ORIGINALS)[name]


def _patched_target(name: str) -> Any:
    """Where a patched name lives: the socket class, or the socket module."""
    return socket if name == _MODULE_PATCH else socket.socket


def install() -> None:
    """Refuse a reserved port on every socket this process builds."""
    if installed_ports():
        return

    def bind(self: socket.socket, address: object) -> None:
        _reject_reserved(_port_of(address))
        _original("bind")(self, address)

    def connect(self: socket.socket, address: object) -> None:
        _reject_reserved(_port_of(address))
        _original("connect")(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        _reject_reserved(_port_of(address))
        return int(_original("connect_ex")(self, address))

    def sendto(self: socket.socket, data: object, address: object, *args: object) -> int:
        _reject_reserved(_port_of(address))
        return int(_original("sendto")(self, data, address, *args))

    def create_connection(address: object, *args: object, **kwargs: object) -> socket.socket:
        _reject_reserved(_port_of(address))
        return _original(_MODULE_PATCH)(address, *args, **kwargs)

    patches: dict[str, Any] = {
        "bind": bind,
        "connect": connect,
        "connect_ex": connect_ex,
        "sendto": sendto,
        _MODULE_PATCH: create_connection,
    }
    setattr(socket, ORIGINALS, {name: getattr(_patched_target(name), name) for name in patches})
    for name, patch in patches.items():
        setattr(_patched_target(name), name, patch)
    setattr(socket, GUARD_MARKER, RESERVED_PORTS)


def uninstall() -> None:
    """Restore the socket surface this process started with."""
    originals: dict[str, Any] | None = getattr(socket, ORIGINALS, None)
    if not originals:
        return
    for name, original in originals.items():
        setattr(_patched_target(name), name, original)
    delattr(socket, ORIGINALS)
    delattr(socket, GUARD_MARKER)
