"""Persistent daemon on/off state: the CONFIG_DIR/daemon.off sentinel (B2.5).

Presence of the marker means the memory service is DISABLED; absence means
enabled — the install default, zero config. The marker lives outside the
config registry on purpose: off/on must persist while the daemon is absent,
and a registry key would be re-primed from stale DB rows at the next boot.
CONFIG_DIR is resolved at call time so a relocated home (test or process) is
honored.
"""

from __future__ import annotations

import os
import time
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

_MARKER_NAME = "daemon.off"
_COORDINATION_NAME = "daemon.coordination.lock"
_COORDINATION_TIMEOUT_S = 30.0


class CoordinationTimeout(TimeoutError):
    """The short-lived daemon start/stop coordination window expired."""


class _CoordinationLock:
    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _unlock_file(self._handle)
        self._handle.close()


def acquire_daemon_coordination(
    timeout_s: float = _COORDINATION_TIMEOUT_S,
) -> _CoordinationLock:
    """Acquire the process-independent on/off transition lock.

    The lock is deliberately separate from the logon bootstrap mutex. It spans
    only the final marker decision through bind/readiness (or shutdown), and
    is released before the daemon continues running.
    """
    from mnemoseed_local.config import CONFIG_DIR

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / _COORDINATION_NAME
    handle = path.open("a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            _lock_file(handle)
            return _CoordinationLock(handle)
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise CoordinationTimeout(
                    f"timed out after {timeout_s:g}s waiting for daemon coordination"
                ) from None
            time.sleep(0.05)


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    import importlib

    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    with suppress(OSError):
        import importlib

        handle.seek(0)
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def disabled_marker() -> Path:
    """The sentinel file path: presence = disabled, absence = enabled."""
    from mnemoseed_local.config import CONFIG_DIR

    return CONFIG_DIR / _MARKER_NAME


def is_disabled() -> bool:
    """True when the disabled marker is present (default is enabled)."""
    return disabled_marker().exists()


def set_disabled() -> Path:
    """Write the disabled marker (idempotent); returns the marker path."""
    marker = disabled_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    return marker


def set_enabled() -> None:
    """Remove the disabled marker (no-op when absent)."""
    try:
        disabled_marker().unlink()
    except FileNotFoundError:
        pass
