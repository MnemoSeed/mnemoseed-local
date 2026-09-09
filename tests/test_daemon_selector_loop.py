"""Windows listener-loss root fix: uvicorn must run on a SelectorEventLoop.

On Windows the daemon's sole listener died while the server object stayed
healthy (should_exit=False, started=True, fd=-1): the IOCP accept path drops
the listening socket on abortive peer closes and never re-arms accept, so the
server runs with a closed listener. A SelectorEventLoop avoids that path.

Hermeticity: these tests never boot a daemon, never arm the watchdog, never
touch the home directory, and never call os._exit — the run_server wiring test
replaces uvicorn.Config, the server, the announcer thread, the watchdog, and
the daemon app module with recording fakes.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import types
from typing import Any

import pytest
import uvicorn

import mnemoseed_local.daemon as daemon_package
import mnemoseed_local.daemon.runner as runner


def test_windows_selects_selector_event_loop_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows-selected factory builds a SelectorEventLoop with zero args."""
    monkeypatch.setattr(sys, "platform", "win32")
    selected = runner.select_uvicorn_loop()
    assert not isinstance(selected, str)
    loop = selected()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_non_windows_keeps_uvicorn_auto_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Windows the uvicorn loop selection is untouched (auto behavior)."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert runner.select_uvicorn_loop() == "auto"


class _RecordingConfig:
    """Stand-in for uvicorn.Config that records its construction kwargs."""

    instances: list[_RecordingConfig] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs: dict[str, object] = dict(kwargs)
        _RecordingConfig.instances.append(self)


class _FakeServer:
    """Stand-in for MnemoseedServer: records its config, never serves."""

    instances: list[_FakeServer] = []

    def __init__(self, config: object) -> None:
        self.config = config
        self.ran = False
        _FakeServer.instances.append(self)

    def announce_ready(self, host: str, port: int) -> None:
        del host, port

    def run(self) -> None:
        self.ran = True


class _FakeThread:
    """Stand-in for the announcer thread: records its target, never starts."""

    instances: list[_FakeThread] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.kwargs: dict[str, object] = dict(kwargs)
        _FakeThread.instances.append(self)

    def start(self) -> None:
        pass


class _FakeWatchdog:
    """Stand-in for Watchdog: records its wiring, never probes or exits."""

    instances: list[_FakeWatchdog] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs: dict[str, object] = dict(kwargs)
        _FakeWatchdog.instances.append(self)

    def start(self) -> None:
        pass


def _install_run_server_fakes(monkeypatch: pytest.MonkeyPatch) -> Any:
    _RecordingConfig.instances.clear()
    _FakeServer.instances.clear()
    _FakeThread.instances.clear()
    _FakeWatchdog.instances.clear()
    real_get_loop_factory = uvicorn.Config.get_loop_factory
    monkeypatch.setattr(uvicorn, "Config", _RecordingConfig)
    monkeypatch.setattr(runner, "MnemoseedServer", _FakeServer)
    monkeypatch.setattr(threading, "Thread", _FakeThread)
    monkeypatch.setattr(runner, "Watchdog", _FakeWatchdog)
    stub_app = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "mnemoseed_local.daemon.app", stub_app)
    monkeypatch.setattr(daemon_package, "app", stub_app, raising=False)

    def _forbidden_exit(code: int) -> Any:
        raise AssertionError(f"os._exit must never fire in the wiring test (code={code})")

    monkeypatch.setattr(os, "_exit", _forbidden_exit)
    return real_get_loop_factory


@pytest.mark.parametrize(
    ("platform", "expect_selector"),
    [("win32", True), ("linux", False), ("darwin", False)],
)
def test_run_server_passes_selected_loop_into_uvicorn_config(
    monkeypatch: pytest.MonkeyPatch, platform: str, expect_selector: bool
) -> None:
    """run_server() hands the platform-selected loop value to uvicorn.Config,
    and the recorded value resolves through the installed uvicorn's own
    get_loop_factory to a factory building the expected loop."""
    real_get_loop_factory = uvicorn.Config.get_loop_factory
    _install_run_server_fakes(monkeypatch)
    monkeypatch.setattr(sys, "platform", platform)

    assert runner.run_server("127.0.0.1", 7788) == 0

    assert len(_RecordingConfig.instances) == 1
    recorded = _RecordingConfig.instances[0].kwargs.get("loop")
    assert recorded == runner.select_uvicorn_loop()
    assert len(_FakeServer.instances) == 1
    assert _FakeServer.instances[0].ran
    assert _FakeWatchdog.instances, "the watchdog wiring must stay armed"

    resolved = real_get_loop_factory(types.SimpleNamespace(loop=recorded, use_subprocess=False))
    if expect_selector:
        assert callable(resolved)
        loop = resolved()
        try:
            assert isinstance(loop, asyncio.SelectorEventLoop)
        finally:
            loop.close()
    else:
        assert recorded == "auto"
