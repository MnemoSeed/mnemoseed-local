"""Daemon launcher: runs uvicorn over the FastAPI app and reports readiness.

``mnemoseed up`` (and the embedded single-process path) call :func:`run_server`.
The server subclass exposes two programmatic additions over stock uvicorn: a
``ready`` event that fires once the app serves a 200 on ``/healthz``, and
:meth:`MnemoseedServer.request_shutdown` so an embedded caller or a test can
stop the daemon without a signal. Signal handling is inherited from uvicorn, so
Ctrl+C still shuts the lifespan down cleanly (stores close) on the main thread.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx
import uvicorn

from mnemoseed_local.daemon.watchdog import Watchdog

logger = logging.getLogger("mnemoseed_local.daemon")

_ANNOUNCE_TIMEOUT = 120.0
_POLL_INTERVAL = 0.2
_PROBE_HOSTS = ("0.0.0.0", "::")


class MnemoseedServer(uvicorn.Server):
    """uvicorn server with a readiness event and programmatic shutdown."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self._ready = threading.Event()
        self._announced = False

    @property
    def ready(self) -> threading.Event:
        """Set once ``/healthz`` returned 200 during a boot announce pass."""
        return self._ready

    def request_shutdown(self) -> None:
        """Ask the run loop to stop; lifespan teardown (stores close) runs."""
        self.should_exit = True

    def announce_ready(self, host: str, port: int) -> None:
        """Poll /healthz in a loop and mark the server ready when green.

        Runs on a daemon thread so a slow first boot (model download on first
        run, Postgres backoff in the docker preset) never blocks the run loop.
        Reads the ``_announced`` flag so the ready line prints exactly once.
        """
        probe_host = "127.0.0.1" if host in _PROBE_HOSTS else host
        url = f"http://{probe_host}:{port}/healthz"
        deadline = time.monotonic() + _ANNOUNCE_TIMEOUT
        while time.monotonic() < deadline and not self.should_exit:
            if self._announced:
                return
            try:
                response = httpx.get(url, timeout=1.0)
                if response.status_code == 200:
                    logger.info("healthy at %s", url)
                    self._announced = True
                    self._ready.set()
                    return
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_INTERVAL)


def intentional_shutdown(watchdog: Watchdog, server: MnemoseedServer) -> None:
    """The POST /daemon/shutdown seam: disarm the watchdog BEFORE the listener
    closes, then ask the server for a graceful teardown. Order pinned: uvicorn
    closes the listener before lifespan teardown, so an armed watchdog would
    misfire os._exit(1) on an intentional drain longer than the refused grace."""
    watchdog.disarm()
    server.request_shutdown()


def run_server(host: str, port: int) -> int:
    """Boot the daemon app and block until shutdown; returns the exit code.

    The app is referenced by import string so uvicorn imports it lazily after
    config.load() — the compose ``core`` image and the ``up`` command both land
    here.
    """
    config = uvicorn.Config(
        "mnemoseed_local.daemon.app:app",
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = MnemoseedServer(config)
    announcer = threading.Thread(
        target=server.announce_ready,
        args=(host, port),
        daemon=True,
        name="mnemoseed-announce",
    )
    announcer.start()
    # PRD-B2.3 D2: arm the watchdog before the run loop. The probe host
    # resolves the same way announce_ready does (a wildcard bind is probed via
    # the loopback), and the thread force-exits the process if the listener is
    # lost beyond a grace window — including a hung teardown, whose joins this
    # exit skips. The watchdog thread is daemon=True, so a clean shutdown never
    # waits on it.
    watchdog = Watchdog("127.0.0.1" if host in _PROBE_HOSTS else host, port)
    watchdog.start()
    # The shutdown seam behind POST /daemon/shutdown: the app module's `app`
    # object is imported eagerly so the hook is bound before server.run() loads
    # the same (cached) module via its import string.
    from functools import partial

    from mnemoseed_local.daemon import app as daemon_app

    daemon_app.app.state.shutdown_hook = partial(intentional_shutdown, watchdog, server)
    server.run()
    return 0
