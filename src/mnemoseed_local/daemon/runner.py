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

_SNAPSHOT_MAX_SERVERS = 2
_SNAPSHOT_MAX_SOCKETS = 4
_SNAPSHOT_FIELD_BUDGET_B = 200
_SNAPSHOT_TOTAL_BUDGET_B = 2048


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


def _truncate_field(value: str) -> str:
    """Cap one snapshot string at the field budget (byte-based, never split)."""
    return value.encode("utf-8", "replace")[:_SNAPSHOT_FIELD_BUDGET_B].decode("utf-8", "replace")


def _error_token(exc: Exception) -> str:
    """Identify a socket read failure by type plus errno only.

    ``type(exc.errno) is int`` — not ``isinstance`` — so a bool masquerading as
    an errno is never recorded (bool is an int subclass but carries no errno
    meaning here).
    """
    token = type(exc).__name__
    errno = getattr(exc, "errno", None)
    if type(errno) is int:
        token += f" errno={errno}"
    return _truncate_field(token)


def _estimate_size(value: object) -> int:
    """Deterministic byte estimate over safe snapshot primitives only."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bool):
        return 4
    if isinstance(value, int):
        return 8
    if isinstance(value, dict):
        return sum(_estimate_size(key) + _estimate_size(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_estimate_size(item) for item in value)
    return 16


def _snapshot_socket(sock: object) -> tuple[dict[str, object], int]:
    """Envelope one listener socket; never raises, never records paths."""
    entry: dict[str, object] = {}
    try:
        fileno = sock.fileno()  # type: ignore[attr-defined]
    except Exception as exc:
        return ({"fd": "?", "error": _error_token(exc)}, 1)
    # type(...) is int — not isinstance — so a bool fileno (a mock artifact or a
    # hostile fd) is rejected, never recorded as an int that misleads the reader.
    entry["fd"] = fileno if type(fileno) is int else "?"
    try:
        name = sock.getsockname()  # type: ignore[attr-defined]
    except Exception as exc:
        entry["error"] = _error_token(exc)
        return (entry, 1)
    if isinstance(name, tuple) and len(name) >= 2 and isinstance(name[0], str) and isinstance(name[1], int):
        entry["host"] = _truncate_field(name[0])
        entry["port"] = name[1]
    else:
        entry["addr"] = "non-tcp"
    return (entry, 0)


def _snapshot_server(server: object) -> tuple[dict[str, object], int]:
    """Best-effort read-only snapshot of a uvicorn server's listener state.

    Returns the snapshot plus an error count (absent servers, closed sockets,
    count caps, size caps each bump it). Only safe primitives leave this
    function: bools, ints, truncated TCP host/port strings, error type names
    plus errno. Unix socket paths, configs, and object reprs never do.
    """
    errors = 0
    snapshot: dict[str, object] = {}
    try:
        snapshot["should_exit"] = bool(server.should_exit)  # type: ignore[attr-defined]
    except Exception as exc:
        errors += 1
        logger.debug("watchdog snapshot should_exit failed: %s", type(exc).__name__)
        snapshot["should_exit"] = False
    try:
        snapshot["started"] = bool(server.started)  # type: ignore[attr-defined]
    except Exception as exc:
        errors += 1
        logger.debug("watchdog snapshot started failed: %s", type(exc).__name__)
        snapshot["started"] = False
    servers = getattr(server, "servers", None)
    if servers is None:
        snapshot["state"] = "not-started"
        return (snapshot, errors)
    try:
        items = list(servers)
    except Exception as exc:
        errors += 1
        logger.debug("watchdog snapshot servers failed: %s", type(exc).__name__)
        snapshot["state"] = "error"
        return (snapshot, errors)
    if not items:
        snapshot["state"] = "empty"
        return (snapshot, errors)
    entries: list[dict[str, object]] = []
    for single in items[:_SNAPSHOT_MAX_SERVERS]:
        try:
            sockets = list(single.sockets)  # type: ignore[attr-defined]
        except Exception as exc:
            errors += 1
            logger.debug("watchdog snapshot sockets failed: %s", type(exc).__name__)
            entries.append({"sockets": [], "error": "sockets-unavailable"})
            continue
        sock_entries: list[dict[str, object]] = []
        for sock in sockets[:_SNAPSHOT_MAX_SOCKETS]:
            entry, entry_errors = _snapshot_socket(sock)
            errors += entry_errors
            sock_entries.append(entry)
        if len(sockets) > _SNAPSHOT_MAX_SOCKETS:
            errors += 1
        entries.append({"sockets": sock_entries})
    if len(items) > _SNAPSHOT_MAX_SERVERS:
        errors += 1
    snapshot["servers"] = entries
    if _estimate_size(snapshot) > _SNAPSHOT_TOTAL_BUDGET_B:
        errors += 1
        snapshot = {
            "should_exit": snapshot["should_exit"],
            "started": snapshot["started"],
            "state": "truncated",
        }
    return (snapshot, errors)


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
    watchdog = Watchdog(
        "127.0.0.1" if host in _PROBE_HOSTS else host,
        port,
        server_snapshot=lambda: _snapshot_server(server),
    )
    watchdog.start()
    # The shutdown seam behind POST /daemon/shutdown: the app module's `app`
    # object is imported eagerly so the hook is bound before server.run() loads
    # the same (cached) module via its import string.
    from functools import partial

    from mnemoseed_local.daemon import app as daemon_app

    daemon_app.app.state.shutdown_hook = partial(intentional_shutdown, watchdog, server)
    server.run()
    return 0
