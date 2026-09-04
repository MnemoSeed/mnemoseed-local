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
    shutdown, is attached exactly once across repeated in-process boots, and
    is released again by each shutdown."""
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
    # each shutdown releases the process-global handler again
    target = logging.getLogger("mnemoseed_local")
    attached = [h for h in target.handlers if getattr(h, "name", None) == DAEMON_LOG_NAME]
    assert len(attached) == 0
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
        # a boot after a released shutdown re-attaches against the same root
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


# ------------------------------------------- Rev 3 diagnostics (TDD RED batch)


def test_rev3_probe_result_single_call_and_bool_compat(monkeypatch) -> None:
    """Rev3-A: _run calls the probe callable exactly once per round; the
    production default_probe_result issues exactly one socket.create_connection
    per call; legacy bool fakes keep their semantics (False dead, True alive)."""
    from mnemoseed_local.daemon import watchdog as wd

    calls: list[int] = []
    fired: list[str] = []
    armed = threading.Event()
    armed.set()

    def _counting_probe():
        calls.append(1)
        return False

    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=5.0,
        refused_grace=0.05,
        interval=0.02,
        probe=_counting_probe,
        fire=lambda reason: fired.append(reason),
        armed=armed,
    )
    watchdog.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not fired:
            time.sleep(0.01)
        assert fired == ["refused-grace"]
        total = watchdog.probe_total
        assert total >= 1
        assert len(calls) == total, "each round must call the probe exactly once"
    finally:
        watchdog.stop()

    connect_calls: list[tuple] = []

    def _spy(*args: object, **kwargs: object):
        connect_calls.append((args, kwargs))
        raise ConnectionRefusedError("rev3 refused")

    monkeypatch.setattr(socket, "create_connection", _spy)
    result = wd.default_probe_result("127.0.0.1", 1)
    assert result.alive is False
    assert result.kind == "refused"
    assert len(connect_calls) == 1, "production probe must connect exactly once"

    assert wd.default_probe("127.0.0.1", 1) is False
    assert len(connect_calls) == 2


def test_rev3_winerror64_classified_alive_other_oserror(monkeypatch) -> None:
    """Rev3-A: WinError 64 reads as alive/other_oserror; refused stays dead;
    timeout stays alive/timeout."""
    from mnemoseed_local.daemon import watchdog as wd

    def _win64(*args: object, **kwargs: object):
        del args, kwargs
        exc = OSError("network name deleted")
        exc.winerror = 64  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(socket, "create_connection", _win64)
    result = wd.default_probe_result("127.0.0.1", 1)
    assert result.alive is True
    assert result.kind == "other_oserror"

    def _refused(*args: object, **kwargs: object):
        del args, kwargs
        raise ConnectionRefusedError("gone")

    monkeypatch.setattr(socket, "create_connection", _refused)
    result = wd.default_probe_result("127.0.0.1", 1)
    assert result.alive is False
    assert result.kind == "refused"

    def _timeout(*args: object, **kwargs: object):
        del args, kwargs
        raise TimeoutError("stalled")

    monkeypatch.setattr(socket, "create_connection", _timeout)
    result = wd.default_probe_result("127.0.0.1", 1)
    assert result.alive is True
    assert result.kind == "timeout"


def test_rev3_dump_header_flush_before_traceback(log_home: Path, monkeypatch) -> None:
    """Rev3-RB1: _default_fire writes header then flush() then
    faulthandler.dump_traceback (buffer order pin); no fsync is issued."""
    import faulthandler as _fh

    order: list[str] = []
    real_dump = _fh.dump_traceback

    def _spy_dump(*args: object, **kwargs: object) -> None:
        order.append("traceback")
        return real_dump(*args, **kwargs)

    monkeypatch.setattr(_fh, "dump_traceback", _spy_dump)
    monkeypatch.setattr(os, "fsync", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fsync forbidden")))

    orig_open = open

    class _SpyFile:
        def __init__(self, raw):
            self._raw = raw

        def write(self, data):
            order.append("write")
            return self._raw.write(data)

        def flush(self):
            order.append("flush")
            return self._raw.flush()

        def __enter__(self):
            self._raw.__enter__()
            return self

        def __exit__(self, *args):
            return self._raw.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._raw, name)

    def _spy_open(*args: object, **kwargs: object):
        raw = orig_open(*args, **kwargs)
        if isinstance(raw, str):
            return raw
        try:
            if getattr(raw, "writable", lambda: False)():
                return _SpyFile(raw)
        except Exception:
            pass
        return raw

    import builtins

    monkeypatch.setattr(builtins, "open", _spy_open)
    exit_calls: list[int] = []
    watchdog = Watchdog("127.0.0.1", 7788, exit_func=lambda code: exit_calls.append(code))
    watchdog._default_fire("boot-grace")
    assert exit_calls == [1]
    assert "write" in order and "flush" in order and "traceback" in order
    assert order.index("write") < order.index("flush") < order.index("traceback")


def test_rev3_dump_flush_failure_still_exits_once(log_home: Path, monkeypatch) -> None:
    """Rev3 flush-bomb: a raising dump.flush() must (a) still let the
    faulthandler traceback execute (the flush failure is contained to the
    inner flush — it must not skip the dump), (b) bump instrumentation_errors,
    and (c) still exit(1) exactly once. A mutant that lets the flush raise
    escape, or that skips the dump on flush failure, fails (a)/(c)."""
    import builtins
    import faulthandler as _fh

    dump_calls: list[bool] = []
    real_dump = _fh.dump_traceback

    def _spy_dump(*args: object, **kwargs: object) -> None:
        dump_calls.append(True)
        return real_dump(*args, **kwargs)

    monkeypatch.setattr(_fh, "dump_traceback", _spy_dump)

    orig_open = open

    class _FlushBomb:
        def __init__(self, raw):
            self._raw = raw

        def write(self, data):
            return self._raw.write(data)

        def flush(self):
            raise OSError("simulated flush failure")

        def __enter__(self):
            self._raw.__enter__()
            return self

        def __exit__(self, *args):
            return self._raw.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._raw, name)

    def _bomb_open(*args: object, **kwargs: object):
        raw = orig_open(*args, **kwargs)
        try:
            if getattr(raw, "writable", lambda: False)():
                return _FlushBomb(raw)
        except Exception:
            pass
        return raw

    monkeypatch.setattr(builtins, "open", _bomb_open)
    exit_calls: list[int] = []
    watchdog = Watchdog("127.0.0.1", 7788, exit_func=lambda code: exit_calls.append(code))
    watchdog._default_fire("boot-grace")
    assert dump_calls, "faulthandler.traceback must still run after the flush failure"
    assert watchdog.instrumentation_errors >= 1, "the flush failure must bump instrumentation_errors"
    assert exit_calls == [1]


def test_rev3_snapshot_shapes_and_caps() -> None:
    """Rev3-RB1/D: not-started/None/empty/closed/error envelopes; at most
    2 servers x 4 sockets; only safe primitives; field <=200B; total <=2KB."""
    from mnemoseed_local.daemon.runner import _snapshot_server

    class _FakeSock:
        def __init__(self, fd=7, name=("127.0.0.1", 9999)):
            self._fd = fd
            self._name = name

        def fileno(self):
            return self._fd

        def getsockname(self):
            return self._name

    class _FakeServer:
        def __init__(self, servers):
            self.servers = servers
            self.should_exit = False
            self.started = False

    class _FakeListener:
        """One listening object exposing .sockets like asyncio.Server."""

        def __init__(self, sockets):
            self.sockets = sockets

    class _NoAttr:
        pass

    snap, errors = _snapshot_server(_NoAttr())
    assert snap.get("state") == "not-started"

    class _NoneServers:
        servers = None

    snap, _ = _snapshot_server(_NoneServers())
    assert snap.get("state") == "not-started"

    class _Empty:
        servers = []

    snap, _ = _snapshot_server(_Empty())
    assert snap.get("state") == "empty"

    # There (1): the cap pin must use legitimate server objects (each carrying
    # should_exit/started) so the error accounting is exactly the SLICING caps —
    # nothing from absent attributes. 3 servers x 6 sockets each: exactly 2
    # servers are kept, each listing exactly 4 sockets, and errors == 3
    # (1 server cap + 2 socket caps). Removing the socket slicing (would emit
    # 6 sockets) or the server slicing (would emit 3 servers) fails below.
    servers = [_FakeListener([_FakeSock(fd=i) for i in range(6)]) for _ in range(3)]
    snap, errors = _snapshot_server(_FakeServer(servers))
    listed = snap.get("servers")
    assert isinstance(listed, list) and len(listed) == 2
    assert all(len(entry.get("sockets", [])) == 4 for entry in listed)
    assert sum(len(e.get("sockets", [])) for e in listed) == 8  # 2 x 4
    assert errors == 3, f"1 server cap + 2 socket caps must total 3, got {errors}"

    class _ClosedSock:
        def fileno(self):
            raise OSError("closed")

        def getsockname(self):
            raise OSError("closed")

    snap, errors = _snapshot_server(_FakeServer([_FakeListener([_ClosedSock()])]))
    assert errors >= 1
    text = repr(snap)
    for banned in ("ssl", "config", "keyfile", "header", "payload", "profile", "session"):
        assert banned not in text.lower()

    class _UnixSock:
        def fileno(self):
            return 9

        def getsockname(self):
            return "/tmp/secret-user.sock"

    snap, _ = _snapshot_server(_FakeServer([_FakeListener([_UnixSock()])]))
    assert "secret-user" not in repr(snap), "unix paths must never be recorded"

    def _sizes(value) -> int:
        if isinstance(value, str):
            return len(value.encode("utf-8"))
        if isinstance(value, dict):
            return sum(_sizes(k) + _sizes(v) for k, v in value.items())
        if isinstance(value, list):
            return sum(_sizes(v) for v in value)
        return 8

    big_servers = [_FakeListener([_FakeSock(fd=3, name=("x" * 5000, 1))])]
    snap, _ = _snapshot_server(_FakeServer(big_servers))
    assert _sizes(snap) <= 2048


def test_rev3_snapshot_raise_counts_and_fire_survives() -> None:
    """Rev3-RB2: a raising snapshot callable bumps snapshot_errors, emits a
    debug line, and the fire still happens."""
    records: list[logging.LogRecord] = []
    daemon_logger = logging.getLogger("mnemoseed_local.daemon")

    class _Rec(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Rec()
    daemon_logger.addHandler(handler)
    old_level = daemon_logger.level
    daemon_logger.setLevel(logging.DEBUG)
    fired: list[str] = []
    fired_event = threading.Event()

    def _boom():
        raise RuntimeError("snapshot exploded")

    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=0.05,
        refused_grace=0.05,
        interval=0.02,
        probe=lambda: False,
        fire=lambda reason: (fired.append(reason), fired_event.set()),
        server_snapshot=_boom,
    )
    watchdog.start()
    try:
        assert fired_event.wait(3.0), "fire must survive a snapshot failure"
        assert fired == ["boot-grace"]
        assert watchdog.snapshot_errors >= 1
        assert any("snapshot" in r.getMessage().lower() for r in records), "a debug line is required"
    finally:
        daemon_logger.setLevel(old_level)
        daemon_logger.removeHandler(handler)
        handler.close()
        watchdog.stop()


def test_rev3_stats_bounded_no_unbounded_latency() -> None:
    """Rev3-RB3: stats are fixed bounded scalars; NO instance attribute may be a
    list or deque regardless of its field name, and the module source contains NO
    deque/p50/history import or field. The collection-scan predicate must ALSO
    catch an arbitrary ``self._history = []`` mutant (applied here to a synthetic
    object carrying the mutant, since the live watchdog must stay clean)."""
    import collections
    import inspect

    from mnemoseed_local.daemon import watchdog as wd

    def _unbounded(items: dict[str, object]) -> list[str]:
        return [name for name, value in items.items() if isinstance(value, (list, collections.deque))]

    fired: list[str] = []
    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=5.0,
        refused_grace=5.0,
        interval=0.01,
        probe=lambda: True,
        fire=lambda reason: fired.append(reason),
    )
    watchdog.start()
    try:
        time.sleep(0.3)
        assert fired == []
        assert watchdog.probe_total >= 3
        assert watchdog.success_count == watchdog.probe_total
        assert watchdog.last_latency_ms >= 0.0
        assert watchdog.max_latency_ms >= watchdog.last_latency_ms
        for name in ("probe_total", "last_latency_ms", "max_latency_ms"):
            assert isinstance(getattr(watchdog, name), (int, float)), name
        # The live watchdog carries no unbounded collection attr at all.
        assert _unbounded(vars(watchdog)) == [], f"unbounded collection attr: {_unbounded(vars(watchdog))}"
        # The same scan must reject an arbitrary _history=[] mutant, whatever
        # its field name: a synthetic instance carrying that mutant is caught.
        mutant = {"_history": [0.0, 1.0]}
        assert _unbounded(mutant) == ["_history"], "the scan failed to flag an arbitrary list-attr mutant"
        # A deque carrying a latency history is likewise caught (name-independent).
        mutant_deque = {"deque_latencies": collections.deque([1.0])}
        assert _unbounded(mutant_deque), "a deque attr must be flagged too"
    finally:
        watchdog.stop()
    # Module source: no deque/p50/history import or field anywhere.
    src = inspect.getsource(wd)
    for banned in ("deque", "p50", "history"):
        assert banned not in src, f"watchdog module references {banned!r}"


def test_rev3_blocked_probe_disarm_never_fires() -> None:
    """Rev3-B2 first stop-check: disarm while the probe is blocked; the
    released False reading past grace must not fire."""
    release = threading.Event()
    entered = threading.Event()
    fired: list[str] = []
    armed = threading.Event()
    armed.set()

    def _blocked():
        entered.set()
        assert release.wait(5.0), "probe was never released"
        return False

    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=5.0,
        refused_grace=0.0,
        interval=0.02,
        probe=_blocked,
        fire=lambda reason: fired.append(reason),
        armed=armed,
    )
    watchdog.start()
    try:
        assert entered.wait(3.0)
        watchdog.disarm()
        time.sleep(0.1)  # let the stop event land while the probe is blocked
        release.set()
        time.sleep(0.4)  # well past refused_grace
        assert fired == []
        assert watchdog.refused_window_start is None, "no refused bookkeeping may run after disarm"
    finally:
        release.set()
        watchdog.stop()


def test_rev3_disarm_during_snapshot_never_fires() -> None:
    """Rev3-B2 second stop-check: the snapshot callable disarms (stop lands
    between the grace decision and _fire); the fire must be skipped."""
    fired: list[str] = []
    armed = threading.Event()
    armed.set()
    holder: dict[str, Watchdog] = {}

    def _disarming_snapshot():
        holder["wd"].disarm()
        return ({"state": "empty"}, 0)

    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=5.0,
        refused_grace=0.0,
        interval=0.02,
        probe=lambda: False,
        fire=lambda reason: fired.append(reason),
        armed=armed,
        server_snapshot=_disarming_snapshot,
    )
    holder["wd"] = watchdog
    watchdog.start()
    try:
        time.sleep(0.5)
        assert fired == [], "a stop arriving before _fire must suppress the fire"
    finally:
        watchdog.stop()


def test_rev3_grace_reasons_are_exact() -> None:
    """Rev3: the fire reason strings are exact (==), and the PRE_BIND and
    ARMED reasons differ."""
    fired: list[str] = []
    fired_event = threading.Event()

    def _record(reason: str) -> None:
        fired.append(reason)
        fired_event.set()

    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=0.05,
        interval=0.01,
        probe=lambda: False,
        fire=_record,
    )
    watchdog.start()
    try:
        assert fired_event.wait(3.0)
        assert fired[0] == "boot-grace"
    finally:
        watchdog.stop()

    fired2: list[str] = []
    armed = threading.Event()
    armed.set()
    watchdog2 = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=5.0,
        refused_grace=0.05,
        interval=0.01,
        probe=lambda: False,
        fire=lambda reason: fired2.append(reason),
        armed=armed,
    )
    watchdog2.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not fired2:
            time.sleep(0.01)
        assert fired2 and fired2[0] == "refused-grace"
    finally:
        watchdog2.stop()


def test_rev3_wall_clock_grace_not_probe_count(monkeypatch) -> None:
    """Rev3 (9): the grace is WALL-CLOCK, not probe-count — proven deterministically
    with a controlled monotonic clock and event-paced probes, no long sleeps.
    The clock advances far past grace across only a FEW refused probes; a
    probe-count mutant (which fires after ~grace/interval probes) could never
    have fired, so the fire assertion fails it."""
    from mnemoseed_local.daemon import watchdog as wd

    clock = {"t": 0.0}

    def _mono() -> float:
        clock["t"] += 0.5
        return clock["t"]

    monkeypatch.setattr(wd.time, "monotonic", _mono)

    release = threading.Event()
    entered = threading.Event()
    released: dict[str, int] = {"n": 0}

    def _paced_probe() -> bool:
        entered.set()  # signal: the loop is about to run a probe
        assert release.wait(5.0), "probe never released by the test"
        release.clear()
        released["n"] += 1
        return False  # always refused

    fired: list[str] = []
    watchdog = Watchdog(
        "127.0.0.1",
        1,
        boot_grace=0.25,
        refused_grace=5.0,  # the ARMED grace is irrelevant here (PRE_BIND)
        interval=0.001,
        probe=_paced_probe,
        fire=lambda reason: fired.append(reason),
    )
    watchdog.start()
    try:
        # Pace a handful of probes; monotonic jumps 0.5s per read, so the wall
        # clock crosses boot_grace (0.25s) after just two refused probes.
        for _ in range(20):
            if fired:
                break
            assert entered.wait(3.0), "probe never entered"
            release.set()
            entered.clear()
            time.sleep(0.02)  # let the fire decision complete on the thread
        assert fired and fired[0] == "boot-grace", "wall-clock grace must fire"
        # A probe-count mutant needs ~grace/interval probes; only the paced few
        # have run, so it could not have fired yet.
        assert released["n"] <= 5, f"probe-count mutant killed at {released['n']} probes"
    finally:
        release.set()  # unblock any in-flight probe so stop() can join
        watchdog.stop()


def test_rev3_fire_summary_complete_and_private(log_home: Path) -> None:
    """Rev3-G: the fire summary carries host/port/reason/elapsed/counts/
    latencies/armed/counters and never privacy words."""
    exit_calls: list[int] = []
    watchdog = Watchdog("127.0.0.1", 7711, exit_func=lambda code: exit_calls.append(code))
    try:
        watchdog._default_fire("refused-grace")
    except TypeError:
        pass
    assert exit_calls == [1]
    text = (log_home / DAEMON_LOG_NAME).read_text(encoding="utf-8")
    summary_lines = [line for line in text.splitlines() if "watchdog summary" in line]
    assert summary_lines, "the fire must log a summary line"
    summary = summary_lines[-1]
    for required in ("127.0.0.1", "7711", "refused-grace", "probe_total", "snapshot_errors"):
        assert required in summary, f"summary missing {required}"
    lowered = summary.lower()
    for banned in (
        "text",
        "content",
        "chunk",
        "header",
        "ssl",
        "path",
        "payload",
        "profile",
        "session",
        "turn",
    ):
        assert banned not in lowered, f"summary leaks {banned}"


def test_rev3_run_fire_path_fault_composition_exits_once(log_home: Path, monkeypatch) -> None:
    """Rev3 (4): the REAL fire path driven through _run — a raising snapshot
    callable AND a raising dump-traceback together must bump snapshot_errors and
    instrumentation_errors, emit a debug line for the snapshot failure, and still
    end in exactly one exit(1). The exit() comes from the actual _run fire loop
    (not a direct _default_fire call), pinning the whole sequence."""
    import faulthandler as _fh

    records: list[logging.LogRecord] = []
    daemon_logger = logging.getLogger("mnemoseed_local.daemon")

    class _Rec(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Rec()
    daemon_logger.addHandler(handler)
    old_level = daemon_logger.level
    daemon_logger.setLevel(logging.DEBUG)

    def _dump_boom(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("simulated dump failure")

    def _snap_boom() -> tuple[dict[str, object], int]:
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(_fh, "dump_traceback", _dump_boom)
    exit_calls: list[int] = []
    try:
        # Use the REAL default fire path (no injected `fire`); inject only the
        # exit function so the thread's fire lands in a recording recorder.
        watchdog = Watchdog(
            "127.0.0.1",
            1,
            boot_grace=0.05,
            refused_grace=0.05,
            interval=0.01,
            probe=lambda: False,
            server_snapshot=_snap_boom,
            exit_func=lambda code: exit_calls.append(code),
        )
        watchdog.start()
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not exit_calls:
                time.sleep(0.01)
            assert exit_calls == [1], "the full-fire composition must exit exactly once"
            assert watchdog.snapshot_errors >= 1, "the snapshot raise must be counted"
            assert watchdog.instrumentation_errors >= 1, "the dump failure must be counted"
            assert any("snapshot" in r.getMessage().lower() for r in records), (
                "a snapshot-failure debug line is required"
            )
        finally:
            watchdog.stop()
    finally:
        daemon_logger.setLevel(old_level)
        daemon_logger.removeHandler(handler)
        handler.close()


def test_rev3_invalid_snapshot_shape_through_run_exits_once(log_home: Path) -> None:
    """Rev3 (5): a server_snapshot returning an INVALID SHAPE (([not-dict],
    not-int)) must, through the real _run fire loop, land in a safe error state
    (snapshot == {'state': 'error'}), bump snapshot_errors, emit the shape-invalid
    debug line, and still reach exactly one fire/exit — the probe thread must
    NOT die silently."""
    records: list[logging.LogRecord] = []
    daemon_logger = logging.getLogger("mnemoseed_local.daemon")

    class _Rec(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Rec()
    daemon_logger.addHandler(handler)
    old_level = daemon_logger.level
    daemon_logger.setLevel(logging.DEBUG)

    def _bad_shape() -> tuple[list[object], str]:
        return ([{"not": "a dict"}], "not-an-int")

    exit_calls: list[int] = []
    try:
        watchdog = Watchdog(
            "127.0.0.1",
            1,
            boot_grace=0.05,
            refused_grace=0.05,
            interval=0.01,
            probe=lambda: False,
            server_snapshot=_bad_shape,
            exit_func=lambda code: exit_calls.append(code),
        )
        watchdog.start()
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not exit_calls:
                time.sleep(0.01)
            assert exit_calls == [1], "an invalid snapshot shape must not block the exit"
            assert watchdog.snapshot_errors >= 1, "the invalid shape must bump snapshot_errors"
            assert watchdog._last_summary.get("snapshot") == {"state": "error"}, (
                "the invalid shape must degrade to a safe error state"
            )
            assert any("shape invalid" in r.getMessage() for r in records), (
                "a shape-invalid debug line is required"
            )
        finally:
            watchdog.stop()
    finally:
        daemon_logger.setLevel(old_level)
        daemon_logger.removeHandler(handler)
        handler.close()


def test_rev3_snapshot_rejects_bool_fd_and_errno() -> None:
    """Rev3 (7): safe-integer handling in runner must use `type(x) is int`, so
    a bool fd (isinstance True, but not an int) or a bool errno is rejected,
    never recorded."""
    from mnemoseed_local.daemon.runner import _error_token, _snapshot_socket

    class _BoolFilenoSock:
        def fileno(self) -> bool:
            return True  # a bool masquerading as fd

        def getsockname(self):
            return ("127.0.0.1", 9999)

    entry, errors = _snapshot_socket(_BoolFilenoSock())
    assert entry.get("fd") == "?", "a bool fileno must be rejected, not recorded as int"
    assert entry.get("host") == "127.0.0.1", "a rejected fd must not lose the rest of the envelope"
    assert errors == 0, "rejecting a bool fd is not an error, just a drop"

    err = OSError("boom")
    err.errno = True  # type: ignore[assignment]  # bool errno
    token = _error_token(err)
    assert "errno" not in token, f"a bool errno must not be recorded: {token}"
    assert token == "OSError", token
