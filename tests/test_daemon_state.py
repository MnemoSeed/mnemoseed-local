from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from mnemoseed_local import daemon_state
from mnemoseed_local.daemon_state import CoordinationTimeout, acquire_daemon_coordination


class _FakeHandle:
    def __init__(self, *, lock_error: bool = False, unlock_error: bool = False) -> None:
        self.fileno_value = 37
        self.seeks: list[int] = []
        self.closed = False
        self._lock_error = lock_error
        self._unlock_error = unlock_error

    def seek(self, offset: int, whence: int = 0) -> int:
        self.seeks.append(offset)
        return offset

    def fileno(self) -> int:
        return self.fileno_value

    def close(self) -> None:
        self.closed = True


def _lock_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: str,
    constants: dict[str, int],
) -> tuple[_FakeHandle, SimpleNamespace, list[str]]:
    handle = _FakeHandle()
    module = SimpleNamespace(**constants)
    imported: list[str] = []
    monkeypatch.setattr(daemon_state.os, "name", platform)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: imported.append(name) or module,
    )
    return handle, module, imported


def test_lock_file_uses_windows_nonblocking_byte_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle, module, imported = _lock_module(
        monkeypatch,
        platform="nt",
        constants={"LK_NBLCK": 1, "LK_UNLCK": 2},
    )
    calls: list[tuple[int, int, int]] = []
    module.locking = lambda *args: calls.append(args)  # type: ignore[attr-defined]

    daemon_state._lock_file(handle)

    assert handle.seeks == [0]
    assert imported == ["msvcrt"]
    assert calls == [(37, 1, 1)]


def test_unlock_file_uses_windows_unlock_and_suppresses_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle, module, imported = _lock_module(
        monkeypatch,
        platform="nt",
        constants={"LK_NBLCK": 1, "LK_UNLCK": 2},
    )
    calls: list[tuple[int, int, int]] = []
    module.locking = lambda *args: calls.append(args)  # type: ignore[attr-defined]

    daemon_state._unlock_file(handle)
    assert calls == [(37, 2, 1)]
    assert handle.seeks == [0]
    assert imported == ["msvcrt"]

    calls.clear()
    module.locking = lambda *args: (_ for _ in ()).throw(OSError)  # type: ignore[attr-defined]
    daemon_state._unlock_file(handle)


def test_lock_file_uses_posix_nonblocking_exclusive_flock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle, module, imported = _lock_module(
        monkeypatch,
        platform="posix",
        constants={"LOCK_EX": 2, "LOCK_NB": 4, "LOCK_UN": 8},
    )
    calls: list[tuple[int, int]] = []
    module.flock = lambda *args: calls.append(args)  # type: ignore[attr-defined]

    daemon_state._lock_file(handle)

    assert handle.seeks == [0]
    assert imported == ["fcntl"]
    assert calls == [(37, 6)]


def test_unlock_file_uses_posix_unlock_and_suppresses_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle, module, imported = _lock_module(
        monkeypatch,
        platform="posix",
        constants={"LOCK_EX": 2, "LOCK_NB": 4, "LOCK_UN": 8},
    )
    calls: list[tuple[int, int]] = []
    module.flock = lambda *args: calls.append(args)  # type: ignore[attr-defined]

    daemon_state._unlock_file(handle)
    assert calls == [(37, 8)]
    assert handle.seeks == [0]
    assert imported == ["fcntl"]

    calls.clear()
    module.flock = lambda *args: (_ for _ in ()).throw(OSError)  # type: ignore[attr-defined]
    daemon_state._unlock_file(handle)


def test_acquire_appends_sentinel_and_release_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    unlocks: list[BinaryIO] = []
    monkeypatch.setattr(daemon_state, "_lock_file", lambda handle: None)
    monkeypatch.setattr(daemon_state, "_unlock_file", unlocks.append)

    lock = acquire_daemon_coordination(timeout_s=0)
    handle = lock._handle

    assert (tmp_path / "daemon.coordination.lock").read_bytes() == b"0"
    assert not handle.closed
    lock.release()
    lock.release()

    assert unlocks == [handle]
    assert handle.closed


def test_acquire_timeout_closes_handle_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[BinaryIO] = []
    original_open = Path.open

    def tracking_open(path: Path, *args: object, **kwargs: object) -> BinaryIO:
        handle = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(handle)
        return handle

    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(daemon_state, "_lock_file", lambda handle: (_ for _ in ()).throw(OSError))
    monkeypatch.setattr(daemon_state.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(daemon_state.time, "sleep", lambda seconds: None)

    with pytest.raises(CoordinationTimeout, match="timed out after 0s"):
        acquire_daemon_coordination(timeout_s=0)

    assert len(opened) == 1
    assert opened[0].closed
