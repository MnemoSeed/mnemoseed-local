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
handler chain, dumps every thread's stack into daemon.log as a forensic
artifact, then calls ``os._exit(1)`` — skipping the very joins that hung.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace as _replace_result
from typing import Literal, NoReturn

logger = logging.getLogger("mnemoseed_local.daemon")

WATCHDOG_BOOT_GRACE_S = 300.0
WATCHDOG_REFUSED_GRACE_S = 10.0
WATCHDOG_PROBE_INTERVAL_S = 1.0

_PROBE_TIMEOUT_S = 3.0
_WATCHDOG_THREAD_NAME = "mnemoseed-watchdog"
_STOP_JOIN_TIMEOUT_S = 5.0

ProbeKind = Literal["success", "refused", "timeout", "other_oserror"]


@dataclass(frozen=True)
class ProbeResult:
    """One probe outcome; fire logic reads only ``alive``."""

    alive: bool
    kind: ProbeKind
    latency_ms: float = 0.0


def default_probe_result(host: str, port: int) -> ProbeResult:
    """Probe the listener with a single raw TCP connect.

    ``ConnectionRefusedError`` means the listener is gone — the dead signal
    that feeds the state machine. A successful connect or any other error (a
    timeout, a network error including WinError 64) means the process is alive
    but possibly stalled; the stalled loop is B6 domain: logged as a line,
    never treated as dead. No timing here — ``Watchdog._run`` stamps latency
    around its single probe call.
    """
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_S):
            return ProbeResult(alive=True, kind="success")
    except ConnectionRefusedError:
        return ProbeResult(alive=False, kind="refused")
    # socket.timeout is an alias of TimeoutError; one branch covers both.
    except TimeoutError as exc:
        logger.info(
            "watchdog probe to %s:%s stalled (%s); treating as alive (B6 domain)",
            host,
            port,
            exc,
        )
        return ProbeResult(alive=True, kind="timeout")
    except OSError as exc:
        logger.info(
            "watchdog probe to %s:%s stalled (%s); treating as alive (B6 domain)",
            host,
            port,
            exc,
        )
        return ProbeResult(alive=True, kind="other_oserror")


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
    return default_probe_result(host, port).alive


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
        probe: Callable[[], bool | ProbeResult] | None = None,
        fire: Callable[[str], NoReturn] | None = None,
        exit_func: Callable[[int], NoReturn] | None = None,
        armed: threading.Event | None = None,
        server_snapshot: Callable[[], tuple[dict[str, object], int]] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._boot_grace = boot_grace
        self._refused_grace = refused_grace
        self._interval = interval
        self._probe = probe or (lambda: default_probe_result(host, port))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._exit = exit_func or os._exit
        self._fire = fire or self._default_fire
        self.armed = armed or threading.Event()
        self._server_snapshot = server_snapshot
        self.probe_total = 0
        self.success_count = 0
        self.refused_count = 0
        self.timeout_count = 0
        self.other_oserror_count = 0
        self.last_latency_ms = 0.0
        self.max_latency_ms = 0.0
        self.refused_window_start: float | None = None
        self.snapshot_errors = 0
        self.instrumentation_errors = 0
        self._last_summary: dict[str, object] = {}

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

    def disarm(self) -> None:
        """Disarm before an intentional shutdown: set the stop event only, no
        join — the probe loop exits on its next interval and the daemon thread
        dies with the process. Distinct from stop(): the shutting-down path
        must never wait on the probe thread."""
        self._stop.set()

    def _safe_debug(self, message: str, *args: object) -> None:
        """Log a debug line without ever raising (the only bare pass)."""
        try:
            logger.debug(message, *args, exc_info=True)
        except Exception:  # noqa: BLE001
            pass

    def _collect_snapshot(self) -> dict[str, object]:
        """Run the optional server snapshot; failures only bump the counter."""
        if self._server_snapshot is None:
            return {"state": "unknown"}
        try:
            snapshot, errors = self._server_snapshot()
        except Exception:
            self.snapshot_errors += 1
            self._safe_debug("watchdog snapshot failed")
            return {"state": "error"}
        if not isinstance(snapshot, dict) or not isinstance(errors, int):
            self.snapshot_errors += 1
            self._safe_debug("watchdog snapshot shape invalid")
            return {"state": "error"}
        self.snapshot_errors += errors
        return snapshot

    def _build_summary(
        self, reason: str, refused_since: float, snapshot: dict[str, object]
    ) -> dict[str, object]:
        """Assemble the fire summary from safe scalars only."""
        return {
            "host": self._host,
            "port": self._port,
            "reason": reason,
            "elapsed": time.monotonic() - refused_since,
            "probe_total": self.probe_total,
            "success_count": self.success_count,
            "refused_count": self.refused_count,
            "timeout_count": self.timeout_count,
            "other_oserror_count": self.other_oserror_count,
            "last_latency_ms": self.last_latency_ms,
            "max_latency_ms": self.max_latency_ms,
            "armed": self.armed.is_set(),
            "snapshot": snapshot,
            "snapshot_errors": self.snapshot_errors,
            "instrumentation_errors": self.instrumentation_errors,
        }

    def _minimal_summary(self, reason: str) -> dict[str, object]:
        """Safe summary for direct fire calls that never ran the probe loop."""
        return {
            "host": self._host,
            "port": self._port,
            "reason": reason,
            "elapsed": 0.0,
            "probe_total": self.probe_total,
            "success_count": self.success_count,
            "refused_count": self.refused_count,
            "timeout_count": self.timeout_count,
            "other_oserror_count": self.other_oserror_count,
            "last_latency_ms": self.last_latency_ms,
            "max_latency_ms": self.max_latency_ms,
            "armed": self.armed.is_set(),
            "snapshot": {"state": "unknown"},
            "snapshot_errors": self.snapshot_errors,
            "instrumentation_errors": self.instrumentation_errors,
        }

    @staticmethod
    def _format_summary(summary: dict[str, object]) -> str:
        """Render the summary as one log line of safe scalars."""
        return (
            "watchdog summary: "
            f"host={summary.get('host')} port={summary.get('port')} "
            f"reason={summary.get('reason')} elapsed={summary.get('elapsed')} "
            f"probe_total={summary.get('probe_total')} "
            f"success_count={summary.get('success_count')} "
            f"refused_count={summary.get('refused_count')} "
            f"timeout_count={summary.get('timeout_count')} "
            f"other_oserror_count={summary.get('other_oserror_count')} "
            f"last_latency_ms={summary.get('last_latency_ms')} "
            f"max_latency_ms={summary.get('max_latency_ms')} "
            f"armed={summary.get('armed')} snapshot={summary.get('snapshot')} "
            f"snapshot_errors={summary.get('snapshot_errors')} "
            f"instrumentation_errors={summary.get('instrumentation_errors')}"
        )

    def _run(self) -> None:
        refused_since: float | None = None
        while not self._stop.wait(self._interval):
            start = time.monotonic()
            try:
                raw = self._probe()
            except Exception:
                self.instrumentation_errors += 1
                self._safe_debug("watchdog probe raised; treating as alive")
                continue
            latency_ms = (time.monotonic() - start) * 1000.0
            if isinstance(raw, ProbeResult):
                probed = _replace_result(raw, latency_ms=latency_ms)
            elif isinstance(raw, bool):
                probed = ProbeResult(
                    alive=raw,
                    kind="success" if raw else "refused",
                    latency_ms=latency_ms,
                )
            else:
                self.instrumentation_errors += 1
                self._safe_debug("watchdog probe shape invalid; treating as alive")
                continue
            self.probe_total += 1
            if probed.kind == "success":
                self.success_count += 1
            elif probed.kind == "refused":
                self.refused_count += 1
            elif probed.kind == "timeout":
                self.timeout_count += 1
            else:
                self.other_oserror_count += 1
            self.last_latency_ms = probed.latency_ms
            if probed.latency_ms > self.max_latency_ms:
                self.max_latency_ms = probed.latency_ms
            if self._stop.is_set():
                return
            if probed.alive:
                if refused_since is not None:
                    refused_since = None
                    self.refused_window_start = None
                    logger.info(
                        "watchdog refused window ended on %s:%s",
                        self._host,
                        self._port,
                    )
                self.armed.set()
                continue
            now = time.monotonic()
            if refused_since is None:
                refused_since = now
                self.refused_window_start = now
                logger.info(
                    "watchdog refused window started on %s:%s",
                    self._host,
                    self._port,
                )
            grace = self._refused_grace if self.armed.is_set() else self._boot_grace
            if now - refused_since >= grace:
                reason = "refused-grace" if self.armed.is_set() else "boot-grace"
                self._last_summary = self._build_summary(reason, refused_since, self._collect_snapshot())
                if self._stop.is_set():
                    return
                self._fire(reason)
                return

    def _default_fire(self, reason: str) -> NoReturn:
        """Last words, forensic dump, then force-exit — the production fire path.

        The dump is a forensic artifact (F2 根治 D5): every thread's stack
        lands in ``CONFIG_DIR/daemon.log`` (append, no line cap) so the hung
        teardown's players are on disk after the process is gone. CONFIG_DIR is
        resolved at call time — a relocated home is honored. The header and
        summary are flushed before the traceback lands, so the on-disk order
        is header-then-stacks. The exit runs in a finally over the whole
        sequence, so ANY raise — including from the failure-path debug log
        itself — still exits. Boundary: a BLOCKING (not raising) CONFIG_DIR
        open/mkdir, e.g. a stalled network share, can still defer the exit;
        that hang is out of band here.
        """
        try:
            summary = dict(self._last_summary)
            if summary.get("reason") != reason:
                summary = self._minimal_summary(reason)
            summary_line = self._format_summary(summary)
            logger.critical(
                "watchdog fire (%s): listener %s:%s unreachable beyond the grace "
                "window; force-exiting the daemon with code 1 (last words) %s",
                reason,
                self._host,
                self._port,
                summary_line,
            )
            _flush_logger_chain(logger)
            try:
                from mnemoseed_local.config import CONFIG_DIR

                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                with open(CONFIG_DIR / "daemon.log", "a", encoding="utf-8") as dump:
                    dump.write(
                        f"\n--- watchdog forensic dump ({reason}) at "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())} on "
                        f"thread {threading.current_thread().name} ---\n"
                    )
                    dump.write(summary_line + "\n")
                    try:
                        dump.flush()
                    except Exception:
                        self.instrumentation_errors += 1
                        self._safe_debug("watchdog dump flush failed; fire proceeds")
                    try:
                        faulthandler.dump_traceback(file=dump, all_threads=True)
                    except Exception:
                        self.instrumentation_errors += 1
                        self._safe_debug("watchdog forensic dump failed; fire proceeds")
            except Exception:
                self.instrumentation_errors += 1
                self._safe_debug("watchdog forensic dump failed; fire proceeds")
        finally:
            self._exit(1)
