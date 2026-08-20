"""PRD-B2.3 S1: daemon watchdog + durable daemon.log pins.

The S1 shipped surface (this batch):

- daemon/watchdog.py — a daemon THREAD (never an asyncio task: a dead event
  loop kills asyncio tasks with it, which is exactly the failure the watchdog
  must observe) probing the served listener with raw TCP connects and
  force-exiting the process after a grace window (PRE_BIND boot grace /
  ARMED refused grace).
- runner.py arms the watchdog inside run_server() only — create_app() and
  TestClient boots NEVER arm it.
- app.py attaches a durable FileHandler writing CONFIG_DIR/daemon.log at
  lifespan startup, plus boot/teardown stage lines.
- F2 根治 (PRD-B2.3 append): DreamWorker/HybridRetriever/ingest run on
  DaemonExecutor daemon threads (never registered with the interpreter's
  atexit join), stop() bounds its wait and abandons wedged chains, and the
  fire path dumps all thread stacks into daemon.log before the forced exit.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
from mnemoseed_local.daemon.app import DreamWorker, create_app
from mnemoseed_local.daemon.watchdog import _PROBE_TIMEOUT_S, Watchdog, default_probe
from mnemoseed_local.dream import DreamTrigger, SnapshotResult
from mnemoseed_local.storage.ports import TurnRange

PROFILE = "default"
WATCHDOG_THREAD_NAME = "mnemoseed-watchdog"
DAEMON_LOG_NAME = "daemon.log"


def _config_text(tmp_path: Path) -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
    )


def _detach_daemon_log_handler() -> None:
    """Remove and close any attached daemon.log FileHandler so the suite stays
    hermetic (a boot elsewhere in the process may have attached one)."""
    target = logging.getLogger("mnemoseed_local")
    for handler in list(target.handlers):
        if getattr(handler, "name", None) == DAEMON_LOG_NAME:
            target.removeHandler(handler)
            handler.close()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bootable embedded config whose CONFIG_DIR lands in tmp_path, so the
    app's daemon.log FileHandler never touches the real home; the handler is
    detached again in the finalizer."""
    _detach_daemon_log_handler()
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_text(tmp_path), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    yield cfg
    _detach_daemon_log_handler()


def _boot() -> TestClient:
    return TestClient(create_app())


# ---------------------------------------------------------------- never armed


def test_create_app_and_testclient_never_arm_watchdog(config_path: Path) -> None:
    """Only run_server() arms the watchdog: a TestClient boot of create_app()
    must never leave a mnemoseed-watchdog thread behind (the module-level boot
    discipline, app.py:714)."""
    with _boot() as client:
        assert client.get("/healthz").json()["status"] == "ok"
        names = [t.name for t in threading.enumerate()]
        assert WATCHDOG_THREAD_NAME not in names


# ---------------------------------------------------------------- PRE_BIND


def test_pre_bind_boot_grace_fires() -> None:
    """PRE_BIND: a listener that never accepts connections (probe always
    refused) past WATCHDOG_BOOT_GRACE_S fires with the boot-grace reason."""
    fired: list[str] = []
    fired_event = threading.Event()

    def _record_fire(reason: str) -> None:
        fired.append(reason)
        fired_event.set()

    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=0.2,
        interval=0.02,
        probe=lambda: False,
        fire=_record_fire,
    )
    watchdog.start()
    try:
        assert fired_event.wait(3.0), "watchdog never fired in PRE_BIND"
        assert fired and "boot" in fired[0]
        assert len(fired) == 1  # exactly one fire, then the loop stops
    finally:
        watchdog.stop()


# ---------------------------------------------------------------- ARMED


def test_armed_refused_grace_fires_on_real_socket() -> None:
    """ARMED: against a real listener the watchdog arms on the first real TCP
    connect; once the listener disappears, the real production default probe
    sees the dead listener (refused at ~2s latency on filtered Windows hosts)
    and the refused grace fires with the refused-grace reason."""
    # The accept loop drains the queue so every probe handshake completes while
    # the listener is open, and closes the listener ITSELF when told to stop —
    # on Windows a socket must be closed from the thread using it.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    stop_accept = threading.Event()

    def _accept_loop() -> None:
        listener.settimeout(0.05)
        while not stop_accept.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue  # poll interval; re-check stop_accept
            except OSError:
                break
            conn.close()
        try:
            listener.close()
        except OSError:
            pass

    accept_thread = threading.Thread(target=_accept_loop, daemon=True)
    accept_thread.start()

    # The production default probe must distinguish dead from alive on this
    # host: a live listener answers fast, and the closed port is refused at
    # ~2s latency on filtered Windows hosts (the probe budget must exceed that
    # envelope or the refusal races the timeout and reads as alive).
    live_started = time.perf_counter()
    assert default_probe("127.0.0.1", port) is True, "live listener must probe as alive"
    assert time.perf_counter() - live_started < 1.0, "live probe must answer fast"

    fired: list[str] = []
    fired_event = threading.Event()
    armed = threading.Event()

    def _record_fire(reason: str) -> None:
        fired.append(reason)
        fired_event.set()

    watchdog = Watchdog(
        "127.0.0.1",
        port,
        boot_grace=5.0,  # long: arming must happen before any grace matters
        refused_grace=0.2,
        interval=0.05,
        fire=_record_fire,
        armed=armed,
    )
    watchdog.start()
    try:
        assert armed.wait(3.0), "watchdog never armed against the live listener"
        stop_accept.set()  # the accept loop closes the listener within 50ms
        # sibling assertion: the real probe sees the closed listener as dead
        # within the probe budget plus a margin (the refusal lands at ~2s)
        deadline = time.monotonic() + _PROBE_TIMEOUT_S + 1.0
        while time.monotonic() < deadline:
            if not default_probe("127.0.0.1", port):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("default_probe never saw the closed listener as dead")
        # refused_grace=0.2: the fire lands on the first refused probe (~2s)
        assert fired_event.wait(10.0), "watchdog never fired after the listener closed"
        assert fired and "refused" in fired[0]
        assert len(fired) == 1
    finally:
        stop_accept.set()
        listener.close()
        watchdog.stop()
        accept_thread.join(timeout=2.0)


def test_armed_flapping_under_grace_does_not_fire() -> None:
    """ARMED: short refused runs that recover before the refused grace must
    never fire — every successful connect resets the refused accumulator."""
    fired: list[str] = []
    armed = threading.Event()
    armed.set()  # already armed: the PRE_BIND phase is skipped
    script = (False, False, True)  # refused, refused, success — repeats
    state = {"i": 0}

    def _flapping_probe() -> bool:
        value = script[state["i"] % len(script)]
        state["i"] += 1
        return value

    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=0.05,
        refused_grace=0.1,  # 2 refused probes at 0.02s stay well under this
        interval=0.02,
        probe=_flapping_probe,
        fire=lambda reason: fired.append(reason),
        armed=armed,
    )
    watchdog.start()
    try:
        time.sleep(0.5)
        assert fired == []
    finally:
        watchdog.stop()


def test_pre_bind_alive_within_grace_does_not_fire() -> None:
    """PRE_BIND: a listener that answers from the start arms quickly and never
    fires — a successful connect is always alive."""
    fired: list[str] = []
    armed = threading.Event()

    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=0.1,
        interval=0.02,
        probe=lambda: True,
        fire=lambda reason: fired.append(reason),
        armed=armed,
    )
    watchdog.start()
    try:
        time.sleep(0.3)  # several probe intervals
        assert fired == []
        assert armed.is_set()  # the first success armed it
    finally:
        watchdog.stop()


# ------------------------------------------------------- disarm (B2.5)


def test_armed_disarm_never_fires_on_real_closed_listener() -> None:
    """ARMED + disarm: an intentionally disarmed watchdog must NOT fire when
    its real listener closes — disarm sets the stop event only, so the probe
    loop exits on its next interval instead of accumulating refusals past the
    refused grace (a not-disarmed watchdog fires on the closed listener)."""
    # The accept loop drains the queue while the listener is open and closes
    # the listener ITSELF when told to stop (Windows socket ownership).
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    stop_accept = threading.Event()

    def _accept_loop() -> None:
        listener.settimeout(0.05)
        while not stop_accept.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue  # poll interval; re-check stop_accept
            except OSError:
                break
            conn.close()
        try:
            listener.close()
        except OSError:
            pass

    accept_thread = threading.Thread(target=_accept_loop, daemon=True)
    accept_thread.start()

    assert default_probe("127.0.0.1", port) is True, "live listener must probe as alive"

    fired: list[str] = []
    armed = threading.Event()

    watchdog = Watchdog(
        "127.0.0.1",
        port,
        boot_grace=5.0,  # long: arming must happen before any grace matters
        refused_grace=0.2,
        interval=0.05,
        fire=lambda reason: fired.append(reason),
        armed=armed,
    )
    watchdog.start()
    try:
        assert armed.wait(3.0), "watchdog never armed against the live listener"
        watchdog.disarm()
        stop_accept.set()  # the accept loop closes the listener within 50ms
        # Well beyond refused_grace (0.2s) plus the ~2s refused probe latency:
        # a not-disarmed watchdog would have fired inside this window.
        time.sleep(5.0)
        assert fired == [], "a disarmed watchdog must never fire"
        assert not watchdog.is_alive, "the probe loop must exit after disarm"
    finally:
        stop_accept.set()
        listener.close()
        watchdog.stop()
        accept_thread.join(timeout=2.0)


def test_disarm_stops_the_probe_loop_without_fire() -> None:
    """disarm is distinct from stop(): it only sets the stop event, so the
    loop ends on its next interval — no join, no probe, no fire (a probe that
    would fire immediately if the loop kept running)."""
    fired: list[str] = []
    armed = threading.Event()
    armed.set()  # already armed: the PRE_BIND phase is skipped

    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=5.0,
        refused_grace=0.1,
        interval=0.02,
        probe=lambda: False,  # a live loop would fire within the refused grace
        fire=lambda reason: fired.append(reason),
        armed=armed,
    )
    watchdog.start()
    try:
        assert armed.is_set()
        watchdog.disarm()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and watchdog.is_alive:
            time.sleep(0.05)
        assert not watchdog.is_alive, "the probe loop must exit after disarm"
        assert fired == []
    finally:
        watchdog.stop()


# ---------------------------------------------------------------- fire path


def test_fire_writes_last_words_and_flushes_before_exit(log_home: Path) -> None:
    """The default fire path: last-words line through the daemon logger, flush
    of the handler chain, THEN the exit function — injecting exit_func while
    keeping the real default fire path is the honest sequencing pin. log_home
    redirects CONFIG_DIR so the fire path's forensic dump lands in tmp_path
    (QA I-1: never the real user home)."""
    _detach_daemon_log_handler()
    daemon_logger = logging.getLogger("mnemoseed_local.daemon")
    records: list[logging.LogRecord] = []
    flush_calls: list[bool] = []

    class _FlushRecorderHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

        def flush(self) -> None:
            flush_calls.append(True)
            super().flush()

    handler = _FlushRecorderHandler()
    daemon_logger.addHandler(handler)
    captured: dict[str, object] = {}
    exit_calls: list[int] = []

    def _record_exit(code: int) -> None:
        exit_calls.append(code)
        captured["records_at_exit"] = list(records)
        captured["flush_calls_at_exit"] = len(flush_calls)

    watchdog = Watchdog("127.0.0.1", 7788, exit_func=_record_exit)
    try:
        watchdog._default_fire("boot-grace")  # the real fire path, exit injected
    finally:
        daemon_logger.removeHandler(handler)
        handler.close()

    assert exit_calls == [1]  # os._exit(1) in production
    at_exit = captured["records_at_exit"]
    assert at_exit, "the last-words record was not captured before the exit ran"
    assert "boot-grace" in at_exit[-1].getMessage()
    assert captured["flush_calls_at_exit"] >= 1, "the handler chain was not flushed"


def test_watchdog_fire_dumps_all_thread_stacks_to_daemon_log(log_home: Path) -> None:
    """F2 根治 D5: the fire path's forensic dump — last-words line lands in
    daemon.log BEFORE the dump header; the dump carries the watchdog's own
    frame AND a live foreign thread's frame (all_threads=True proof; faulthandler
    names threads by id on this platform, so the frame's function name is the
    discriminator); the exit function ran exactly once. CONFIG_DIR is resolved
    at call time, so the dump follows the relocated home. Mutants: dump
    omitted, all_threads=False, a dump failure blocking the exit, or an
    import-time-cached CONFIG_DIR all fail this pin."""
    from mnemoseed_local.daemon.app import _attach_daemon_log_handler

    _attach_daemon_log_handler()
    exit_calls: list[int] = []
    foreign_hold = threading.Event()
    foreign = threading.Thread(
        target=_foreign_pin_wedge, args=(foreign_hold,), name="mnemoseed-foreign-pin", daemon=True
    )
    foreign.start()
    watchdog = Watchdog("127.0.0.1", 7788, exit_func=lambda code: exit_calls.append(code))
    try:
        watchdog._default_fire("boot-grace")
    finally:
        foreign_hold.set()
        foreign.join(timeout=2.0)

    assert exit_calls == [1], "the exit function must run exactly once"
    text = (log_home / DAEMON_LOG_NAME).read_text(encoding="utf-8")
    assert "watchdog fire (boot-grace)" in text, "the last-words line never reached daemon.log"
    dump_header = next(line for line in text.splitlines() if "forensic dump" in line)
    assert text.index("watchdog fire (boot-grace)") < text.index(dump_header), (
        "the last-words line must land before the dump header"
    )
    assert "boot-grace" in dump_header
    assert threading.current_thread().name in dump_header, "the dump header must name its thread"
    assert "_foreign_pin_wedge" in text, "the foreign thread's frame is missing (all_threads=False?)"
    assert "_default_fire" in text, "the watchdog's own frame is missing from the dump"


def test_watchdog_fire_dump_failure_never_blocks_exit(
    log_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2 根治 D5: the fire path is NEVER blocked by a dump failure — a
    raising faulthandler dump still ends in the exit function running exactly
    once (the watchdog must always get its os._exit(1) out)."""
    exit_calls: list[int] = []

    def _explode(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("simulated dump failure")

    monkeypatch.setattr(faulthandler, "dump_traceback", _explode)
    watchdog = Watchdog("127.0.0.1", 7788, exit_func=lambda code: exit_calls.append(code))
    watchdog._default_fire("boot-grace")
    assert exit_calls == [1], "a dump failure must never block the fire exit"


def test_watchdog_fire_debug_log_failure_never_blocks_exit(
    log_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NIT-1: the dump-failure debug line itself can raise (a broken handler,
    a closed logger). The exit sits in a finally over the whole last-words/dump
    sequence, so ANY raise path — including the failure-path debug log —
    still runs the exit exactly once."""
    exit_calls: list[int] = []

    def _explode(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("simulated dump failure")

    def _debug_explode(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("simulated debug-log failure")

    monkeypatch.setattr(faulthandler, "dump_traceback", _explode)
    monkeypatch.setattr(logging.getLogger("mnemoseed_local.daemon"), "debug", _debug_explode)
    watchdog = Watchdog("127.0.0.1", 7788, exit_func=lambda code: exit_calls.append(code))
    watchdog._default_fire("boot-grace")
    assert exit_calls == [1], "the exit must run exactly once even when the debug log raises"


# ------------------------------------------------------- root mechanism (F2 根治)


def _foreign_pin_wedge(hold: threading.Event) -> None:
    """Live foreign-thread target for the fire-path dump pin (its frame must
    appear in the all_threads dump)."""
    hold.wait()


class _BlockForeverSnapshotter:
    """Snapshotter whose request blocks on a threading.Event inside the worker
    thread, keeping a manual dream in flight forever (the PRD-B2.3 D2 repro
    shape, re-used by the F2 根治 flip pin)."""

    def __init__(self, block: threading.Event) -> None:
        self._block = block
        self.entered = threading.Event()

    def request(self, profile_id: str, turn_range: TurnRange) -> SnapshotResult:
        del profile_id, turn_range
        self.entered.set()
        self._block.wait()  # blocks the worker thread until the test releases it
        return SnapshotResult(snapshot=None, ok=True)


def _manual_event() -> PoolEvent:
    return PoolEvent(
        kind=PoolEventKind.DREAM_TRIGGER,
        profile_id=PROFILE,
        turn_range=TurnRange(start=0, end=0),
        balance=10.0,
        fired_at=1.0,
    )


async def test_worker_stop_bounded_abandons_wedged_inflight_job() -> None:
    """F2 根治 flip pin: DreamWorker.stop() returns in bounded time while an
    in-flight job is wedged forever inside the snapshotter.

    The pre-fix stop() jammed the event loop inside executor.shutdown(wait=True)
    for the whole wedge; the fixed stop() waits at most ``stop_timeout`` WITHOUT
    jamming the loop, then ABANDONS the wedged chain (journaled snapshots
    guarantee re-recovery on the next boot; lancedb/sqlite writes are atomic).
    The pending manual future resolves False and the wedged worker thread stays
    alive as a daemon thread — abandoned, never joined. Mutants: unbounded join
    hangs the wait_for; a True resolution on abandon fails the future assert.
    """
    block = threading.Event()
    snapshotter = _BlockForeverSnapshotter(block)
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=False)
    trigger.handle_event(_manual_event())
    worker = DreamWorker(trigger, stop_timeout=0.2)
    worker.start()
    job = asyncio.create_task(worker.submit_dream_once(PROFILE))
    entered = await asyncio.to_thread(snapshotter.entered.wait, 2.0)
    assert entered, "the manual job never reached the snapshotter"
    started = time.monotonic()
    await asyncio.wait_for(worker.stop(), timeout=3.0)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"stop() was not bounded: {elapsed:.3f}s"
    assert await asyncio.wait_for(job, timeout=1.0) is False
    worker_thread = next(
        (t for t in threading.enumerate() if t.name.startswith("mnemoseed-dream-")),
        None,
    )
    assert worker_thread is not None, "no dream worker thread"
    assert worker_thread.is_alive(), "the wedged worker was joined, not abandoned"


class _QuickSnapshotter:
    """Snapshotter that returns immediately (a healthy in-flight dream)."""

    def request(self, profile_id: str, turn_range: TurnRange) -> SnapshotResult:
        del profile_id, turn_range
        return SnapshotResult(snapshot=None, ok=True)


async def test_daemon_worker_threads_are_daemon_and_unregistered() -> None:
    """The dream worker thread is a daemon thread NEVER registered in
    ``concurrent.futures.thread._threads_queues`` — the F2 root-cause shape:
    TPE workers are non-daemon and joined by the interpreter's atexit hook, so
    a wedged worker keeps the process alive forever. A TPE-backed worker fails
    both asserts."""
    from concurrent.futures import thread as cf_thread

    trigger = DreamTrigger(snapshotter=_QuickSnapshotter(), auto_trigger=False)
    trigger.handle_event(_manual_event())
    worker = DreamWorker(trigger)
    worker.start()
    job = asyncio.create_task(worker.submit_dream_once(PROFILE))
    assert await asyncio.wait_for(job, timeout=2.0) is True
    try:
        dream_threads = [t for t in threading.enumerate() if t.name.startswith("mnemoseed-dream-")]
        assert dream_threads, "no dream worker thread"
        assert all(t.daemon for t in dream_threads), "the dream worker must be a daemon thread"
        assert all(t not in cf_thread._threads_queues for t in dream_threads), (
            "the dream worker must never join the interpreter's atexit join set"
        )
    finally:
        await worker.stop()


# ------------------------------------------------ B6 W-C: drain lane teardown


def test_teardown_drain_telemetry_precedes_stores_close(log_home: Path) -> None:
    """B6 (W-C): teardown logs the drain-lane completion BEFORE the stores
    close — the completed-before-close telemetry pin (a mutant that closes the
    stores first, or omits the bounded drain wait, fails the order assert)."""
    session = "sess-telemetry"
    log_file = log_home / "daemon.log"
    with _boot() as client:
        response = client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "user_prompt",
                "session_id": session,
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": "关停前最后一句"},
            },
        )
        assert response.status_code == 202, response.text
    final_text = log_file.read_text(encoding="utf-8")
    assert "teardown: drain capture lane" in final_text
    assert "teardown: drain lane complete (1 drained, 0 failed, 0 abandoned)" in final_text
    assert final_text.index("teardown: drain lane complete") < final_text.index("teardown: close stores"), (
        "the drain-lane completion must be logged before the stores close"
    )


def test_teardown_abandons_wedged_drain_in_bounded_time(config_path: Path) -> None:
    """B6 (W-C): a drain wedged in store I/O hits the drain stop bound and
    teardown completes anyway — the wedged worker is abandoned (daemon thread,
    dies with the process), never joined, and the abandoned-drain warning names
    the session (F2-rootfix semantics: teardown never hangs)."""
    session = "sess-wedge"
    block = threading.Event()
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "user_prompt",
                "session_id": session,
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": "楔死前最后一句"},
            },
        )
        assert response.status_code == 202, response.text

        def wedged_drain(session_id: str) -> None:
            del session_id
            block.wait(5.0)  # outlives the 2s drain stop budget

        app.state.capture.drain = wedged_drain
        started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < 4.0, f"teardown was not bounded: {elapsed:.3f}s"
    log_text = (config_path.parent / "daemon.log").read_text(encoding="utf-8")
    assert "teardown: drain lane complete (0 drained, 0 failed, 1 abandoned)" in log_text
    assert f"abandoned 1 wedged drain(s): {session}" in log_text
    assert "B2.2" in log_text, "the abandoned-drain warning must note the host-side replay absorb"
    wedge_thread = next(
        (t for t in threading.enumerate() if t.name.startswith("mnemoseed-drain-")),
        None,
    )
    assert wedge_thread is not None, "no drain worker thread"
    assert wedge_thread.is_alive(), "the wedged drain was joined, not abandoned"


def test_teardown_reports_failed_drain_with_session_and_exception(config_path: Path) -> None:
    """B6 (W-C) QA IMPORTANT-1: a teardown-submitted drain that raises is a
    data-loss event with no awaiting handler — stop() must log a WARNING naming
    the session and the exception, count it as failed (never as drained), and
    still complete teardown."""
    session = "sess-fail"
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "user_prompt",
                "session_id": session,
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": "失败前最后一句"},
            },
        )
        assert response.status_code == 202, response.text

        def failing_drain(session_id: str) -> None:
            del session_id
            raise RuntimeError("teardown drain exploded")

        app.state.capture.drain = failing_drain
    log_text = (config_path.parent / "daemon.log").read_text(encoding="utf-8")
    assert "teardown: drain lane complete (0 drained, 1 failed, 0 abandoned)" in log_text
    assert f"drain failed for session {session}" in log_text
    assert "teardown drain exploded" in log_text


# ------------------------------------------------------------- daemon.log pins


@pytest.fixture
def log_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bootable home under tmp_path: MNEMOSEED_LOCAL_HOME honors CONFIG_DIR,
    and the app's daemon.log FileHandler lands inside it (detached after)."""
    _detach_daemon_log_handler()
    (tmp_path / "config.toml").write_text(_config_text(tmp_path), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path))
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    yield tmp_path
    _detach_daemon_log_handler()


def test_daemon_log_durable_filehandler_pins(log_home: Path) -> None:
    """The durable daemon.log: exists under CONFIG_DIR, holds the boot line
    (with pid) while the process is alive, gains the teardown stage lines after
    shutdown, and is attached exactly once across repeated in-process boots."""
    log_file = log_home / "daemon.log"
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        # readable while the daemon is alive: the FileHandler flushes per emit
        assert log_file.exists()
        live_text = log_file.read_text(encoding="utf-8")
        assert "daemon boot:" in live_text
        assert f"daemon boot: pid={os.getpid()}" in live_text

    # after teardown the stage lines landed in the same file
    final_text = log_file.read_text(encoding="utf-8")
    assert "teardown: drain capture lane" in final_text
    assert "teardown: complete" in final_text

    # a second in-process boot must not double-attach the handler
    with TestClient(app) as client:
        assert client.get("/healthz").json()["status"] == "ok"
    target = logging.getLogger("mnemoseed_local")
    attached = [h for h in target.handlers if getattr(h, "name", None) == DAEMON_LOG_NAME]
    assert len(attached) == 1
    # the boot line carries the full identity payload, not just the pid
    boot_line = next(line for line in live_text.splitlines() if "daemon boot:" in line)
    assert f"pid={os.getpid()}" in boot_line
    assert "version=" in boot_line
    assert "preset=embedded" in boot_line
    assert "port=7788" in boot_line


# ------------------------------------------- QA I-1: no real-home pollution


def test_daemon_log_handler_follows_patched_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA I-1: the daemon.log FileHandler must resolve CONFIG_DIR at attach
    time — a monkeypatched mnemoseed_local.config.CONFIG_DIR must redirect the
    log away from the real user home, or every TestClient boot in the suite
    pollutes ~/.mnemoseed-local/daemon.log (empirically confirmed)."""
    _detach_daemon_log_handler()
    (tmp_path / "config.toml").write_text(_config_text(tmp_path), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    app = create_app()
    try:
        with TestClient(app) as client:
            assert client.get("/healthz").json()["status"] == "ok"
        with TestClient(app) as client:
            assert client.get("/healthz").json()["status"] == "ok"
        target = logging.getLogger("mnemoseed_local")
        attached = [h for h in target.handlers if getattr(h, "name", None) == DAEMON_LOG_NAME]
        assert len(attached) == 1
        base = str(attached[0].baseFilename)
        assert base.startswith(str(tmp_path)), f"handler writes to {base!r}, not the tmp home"
    finally:
        _detach_daemon_log_handler()


# ------------------------------------------- QA I-2: B6 stall red line


class _RecordHandler(logging.Handler):
    """Handler that collects emitted records (for B6 log-line pins)."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


def test_default_probe_stall_returns_alive_and_logs_b6(monkeypatch) -> None:
    """QA I-2: the B6 red line — a stalled probe (timeout, not refused) must
    read as ALIVE and emit the stall log line, never as dead. A mutant
    default_probe returning False on any OSError must fail this pin."""
    records: list[logging.LogRecord] = []
    daemon_logger = logging.getLogger("mnemoseed_local.daemon")
    handler = _RecordHandler(records)
    daemon_logger.addHandler(handler)

    def _stall(*args: object, **kwargs: object) -> socket.socket:
        del args, kwargs
        raise TimeoutError("simulated stalled bound loop (B6 domain)")

    monkeypatch.setattr(socket, "create_connection", _stall)
    try:
        assert default_probe("127.0.0.1", 7788) is True
    finally:
        daemon_logger.removeHandler(handler)
        handler.close()
    assert any("B6" in r.getMessage() for r in records), "the stall must be logged"


def test_armed_stalled_probe_never_fires() -> None:
    """QA I-2: a probe that behaves like a stalled-but-bound loop (always reads
    alive, as the default probe does for timeouts) must keep an ARMED watchdog
    alive — several intervals without a single refused reading, no fire."""
    fired: list[str] = []
    armed = threading.Event()
    armed.set()  # already armed: the PRE_BIND phase is skipped
    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=0.05,
        refused_grace=0.1,
        interval=0.02,
        probe=lambda: True,  # stalled-bound behavior: timeouts map to alive
        fire=lambda reason: fired.append(reason),
        armed=armed,
    )
    watchdog.start()
    try:
        time.sleep(0.3)  # several probe intervals
        assert fired == []
    finally:
        watchdog.stop()
