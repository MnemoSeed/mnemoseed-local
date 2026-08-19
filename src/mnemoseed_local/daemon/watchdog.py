"""Process-internal listener watchdog (PRD-B2.3 D2/D3).

The watchdog is a plain daemon THREAD — never an asyncio task — probing the
served host/port with raw TCP connects. A dead event loop kills asyncio tasks
with it, which is exactly the failure this component must observe; an OS
thread keeps probing no matter what the event loop is doing.

Two-state machine:

- PRE_BIND: before the first successful connect, a listener that stays refused
  past ``WATCHDOG_BOOT_GRACE_S`` fires with reason ``boot-grace`` (covers the
  deterministic boot hang where the port never binds). The grace clock starts
  at the first refused probe — ~one probe interval after the thread starts,
  not at thread start.
- ARMED: after the first successful connect, continuous refusals of at least
  ``WATCHDOG_REFUSED_GRACE_S`` fire with reason ``refused-grace``. A normal
  teardown is far faster than the grace; a hung teardown is executed after the
  grace. Any successful connect resets the refused accumulator.

A successful connect — or any non-refused error such as a timeout — counts as
alive: the bound-but-stalled loop is B6 domain, logged as a line, never a fire
reason. Firing writes a last-words line through the daemon logger, flushes the
handler chain, then calls ``os._exit(1)`` — skipping the very joins that hung.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import NoReturn

logger = logging.getLogger("mnemoseed_local.daemon")

WATCHDOG_BOOT_GRACE_S = 300.0
WATCHDOG_REFUSED_GRACE_S = 10.0
WATCHDOG_PROBE_INTERVAL_S = 1.0

_PROBE_TIMEOUT_S = 3.0
_WATCHDOG_THREAD_NAME = "mnemoseed-watchdog"
_STOP_JOIN_TIMEOUT_S = 5.0


def default_probe(host: str, port: int) -> bool:
    """Probe the listener with a raw TCP connect; True when reachable.

    ``ConnectionRefusedError`` means the listener is gone — the dead signal
    that feeds the state machine. A successful connect or any other error (a
    timeout, a network error) means the process is alive but possibly stalled;
    the stalled loop is B6 domain: logged as a line, never treated as dead.

    Measured rationale for the 3.0s probe budget: loopback refusal on filtered
    Windows hosts is delivered at ~2s latency (a filter intercepts the SYN and
    delays the RST); a 1s probe budget would race that envelope and report
    timeout (=alive) on a dead listener, so the budget must exceed it — 3.0s
    keeps refused->dead and hang->alive(B6) semantics.

    Honest boundary: a host whose firewall silently DROPS loopback SYNs (no
    RST at all, unlike this host's delayed-RST at ~2s) makes a dead listener
    indistinguishable from a stall — the probe reads alive forever and the
    watchdog is inert on such hosts. That is a documented boundary, not
    fixable in-band here.
    """
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_S):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        logger.info(
            "watchdog probe to %s:%s stalled (%s); treating as alive (B6 domain)",
            host,
            port,
            exc,
        )
        return True


def _flush_logger_chain(target: logging.Logger) -> None:
    """Flush every handler the target's propagation chain would reach."""
    current: logging.Logger | None = target
    while current is not None:
        for handler in current.handlers:
            try:
                handler.flush()
            except Exception:  # noqa: BLE001
                logger.debug("watchdog flush failed on %s", handler, exc_info=True)
        if not current.propagate:
            break
        current = current.parent


class Watchdog:
    """Probe the served listener on a daemon thread; force-exit on loss.

    Production defaults probe ``host:port`` every ``WATCHDOG_PROBE_INTERVAL_S``
    with ``os._exit(1)`` as the exit function. Every knob is injectable so the
    state machine is testable hermetically (fake probes, fire/exit recorders,
    an externally visible ``armed`` event, and a ``stop()`` that joins the
    thread).
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        boot_grace: float = WATCHDOG_BOOT_GRACE_S,
        refused_grace: float = WATCHDOG_REFUSED_GRACE_S,
        interval: float = WATCHDOG_PROBE_INTERVAL_S,
        probe: Callable[[], bool] | None = None,
        fire: Callable[[str], NoReturn] | None = None,
        exit_func: Callable[[int], NoReturn] | None = None,
        armed: threading.Event | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._boot_grace = boot_grace
        self._refused_grace = refused_grace
        self._interval = interval
        self._probe = probe or (lambda: default_probe(host, port))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._exit = exit_func or os._exit
        self._fire = fire or self._default_fire
        self.armed = armed or threading.Event()

    @property
    def is_alive(self) -> bool:
        """True while the probe thread runs."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Spawn the daemon probe thread (idempotent).

        ``stop()`` is terminal: calling ``start()`` again after a ``stop()`` is
        a silent no-op (the stop event stays set), so tests must not reuse a
        stopped watchdog for a fresh run.
        """
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=_WATCHDOG_THREAD_NAME,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop and join the thread (hermetic test cleanup).

        The thread is daemon=True, so a clean interpreter exit never waits on
        it; stop() exists so tests can tidy up deterministically.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_STOP_JOIN_TIMEOUT_S)
            self._thread = None

    def _run(self) -> None:
        refused_since: float | None = None
        while not self._stop.wait(self._interval):
            if self._probe():
                self.armed.set()
                refused_since = None
                continue
            now = time.monotonic()
            if refused_since is None:
                refused_since = now
            grace = self._refused_grace if self.armed.is_set() else self._boot_grace
            if now - refused_since >= grace:
                reason = "refused-grace" if self.armed.is_set() else "boot-grace"
                self._fire(reason)
                return

    def _default_fire(self, reason: str) -> NoReturn:
        """Last words, flush, then force-exit — the production fire path."""
        logger.critical(
            "watchdog fire (%s): listener %s:%s unreachable beyond the grace "
            "window; force-exiting the daemon with code 1 (last words)",
            reason,
            self._host,
            self._port,
        )
        _flush_logger_chain(logger)
        self._exit(1)
