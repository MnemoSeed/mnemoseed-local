"""Boot-recovery deferral (roadmap: boot 同步 dream 恢复挪出启动路径).

The journaled-snapshot recovery used to run the FULL dream chain synchronously
inside lifespan startup, before the port binds. These pins lock the deferred
shape: recovery classification and trigger bookkeeping stay synchronous in
``_build_capture``, the pipeline.run chain moves to the dream worker as a
RESUME job, and the scheduler waits for the deferred resumes to drain before
its first tick (a tick during the recovery window would emit a dream for the
still-unconsolidated profile and queue a duplicate behind the resume).

Four pins:

- startup purity: by the time the TestClient enters (lifespan done), the
  dream LLM has completed ZERO calls and the port bound fast;
- the deferred resume eventually runs and commits the journaled snapshot;
- the scheduler gate: no dream may be queued while a resume is blocked;
- the exception path: a resume job that raises still releases the drain
  signal (try/finally), so the scheduler starts;
- the drain timeout: a wedged resume executor must not stall the scheduler —
  after the bounded timeout it logs a warning and ticks anyway.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

import mnemoseed_local.dream.reflect as reflect_module
from mnemoseed_local.config import load_config
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.dream import (
    DreamPipeline,
    Snapshot,
    SnapshotChunk,
    SnapshotPhase,
    load_snapshot_file,
    resume_boundary,
    write_snapshot_file,
)
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.factory import build_stores
from mnemoseed_local.storage.ports import TurnRange

PROFILE = "default"
SNAP_ID = "boot-recover-1"
CHUNK_ID = "chunk-boot-1"
RANGE = TurnRange(0, 3)
DURABLE_TEXT = "我决定以后都用 pnpm 管理依赖"

DAEMON_LOG_NAME = "daemon.log"


def _detach_daemon_log_handler() -> None:
    """Remove and close any attached daemon.log FileHandler so the suite stays
    hermetic (a boot elsewhere in the process may have attached one)."""
    import logging

    target = logging.getLogger("mnemoseed_local")
    for handler in list(target.handlers):
        if getattr(handler, "name", None) == DAEMON_LOG_NAME:
            target.removeHandler(handler)
            handler.close()


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


# ---------------------------------------------------------------- seeding


def _stamp(chunk_id: str = CHUNK_ID) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=PROFILE,
        text=DURABLE_TEXT,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="stub",
        cues=Cues(entities=["pnpm"]),
        provenance=Provenance(
            asserted_by="user",
            session_id="sess-boot-recovery",
            source="sess-boot-recovery",
            asserted_at=1.0,
        ),
        ingested_at=1.0,
        turn_start=RANGE.start,
        turn_end=RANGE.end,
    )


def _seed_journal(tmp_path: Path) -> Snapshot:
    """A journaled reflect-boundary snapshot: the daemon's crash left the
    snapshot captured but the reflect pass never ran."""
    snap = Snapshot(
        snapshot_id=SNAP_ID,
        profile_id=PROFILE,
        turn_range=RANGE,
        chunks=(SnapshotChunk.from_stamp(_stamp()),),
        created_at=1.0,
        phases=frozenset({SnapshotPhase.SNAPSHOT_DONE.value}),
    )
    write_snapshot_file(tmp_path / "dreams", snap)
    return snap


def _seed_stores(tmp_path: Path, *, pool_balance: float = 0.0) -> None:
    """The same chunk rows a crashed daemon left behind: the vector row plus
    the profile's persisted score-pool balance (0 = no pool row)."""
    config = load_config()
    stores = build_stores(config)
    try:
        stores.vector.upsert_chunk(_stamp(), [0.0] * 64)
        if pool_balance > 0.0:
            stores.meta.pool_credit(PROFILE, pool_balance, RANGE)
    finally:
        asyncio.run(stores.close())


# ---------------------------------------------------------------- stub LLMs


class _BlockingStub(reflect_module.StubReflectLLM):
    """Counting stub whose chat blocks on a release event: the resume's
    reflect stays in flight until the test lets it through."""

    entered: ClassVar[list[float]] = []
    completed: ClassVar[list[float]] = []
    release: ClassVar[threading.Event] = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.entered = []
        cls.completed = []
        cls.release = threading.Event()

    def chat(self, *, system: str, user: str) -> str:
        _BlockingStub.entered.append(time.perf_counter())
        _BlockingStub.release.wait(10.0)
        result = super().chat(system=system, user=user)
        _BlockingStub.completed.append(time.perf_counter())
        return result


class _CountingStub(reflect_module.StubReflectLLM):
    """Fast counting stub: every completed chat records a timestamp."""

    entered: ClassVar[list[float]] = []
    completed: ClassVar[list[float]] = []

    @classmethod
    def reset(cls) -> None:
        cls.entered = []
        cls.completed = []

    def chat(self, *, system: str, user: str) -> str:
        _CountingStub.entered.append(time.perf_counter())
        result = super().chat(system=system, user=user)
        _CountingStub.completed.append(time.perf_counter())
        return result


# ---------------------------------------------------------------- helpers


class _BootState:
    """Cross-thread probe holder for one boot-under-test."""

    def __init__(self) -> None:
        self.client: TestClient | None = None
        self.entered: int = 0
        self.completed: int = 0
        self.completed_after_release: int = 0
        self.completed_later: int = 0
        self.committed_total: int = 0
        self.committed_run_id: str | None = None
        self.drained: bool = False
        self.status_after_drain: dict[str, Any] = {}
        self.enter_done = threading.Event()
        self.finished = threading.Event()


def _wait_dream_idle(client: TestClient, timeout: float = 10.0) -> dict[str, Any]:
    """Poll /memory/dream_status until the profile's dream returns to idle."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.post("/memory/dream_status", json={"profile_id": PROFILE}).json()
        if body.get("state") == "idle":
            return body
        time.sleep(0.05)
    raise AssertionError(f"dream never returned to idle; last status: {body}")


def _journal_state(tmp_path: Path) -> Snapshot | None:
    return load_snapshot_file(tmp_path / "dreams" / f"{SNAP_ID}.json")


# ---------------------------------------------------------------- pins


def test_boot_defers_journaled_recovery_off_the_startup_path(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red 1 + Red 2: the journaled recovery's dream chain runs on the dream
    worker, never inside lifespan. The port binds with zero completed LLM
    calls, and the deferred resume completes the reflect -> merge -> commit
    chain after boot."""
    _BlockingStub.reset()
    monkeypatch.setattr(reflect_module, "StubReflectLLM", _BlockingStub)
    _seed_journal(config_path.parent)
    _seed_stores(config_path.parent)

    state = _BootState()

    def _run_boot() -> None:
        with _boot() as client:
            state.entered = len(_BlockingStub.entered)
            state.completed = len(_BlockingStub.completed)
            state.enter_done.set()
            _BlockingStub.release.wait(10.0)  # hold the daemon open
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and len(_BlockingStub.completed) < 1:
                time.sleep(0.05)
            state.completed_after_release = len(_BlockingStub.completed)
            committed: dict[str, Any] | None = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                committed = client.get("/api/v1/audit", params={"action": "dream_committed"}).json()
                if committed["total"] >= 1:
                    break
                time.sleep(0.05)
            assert committed is not None
            state.committed_total = committed["total"]
            state.committed_run_id = committed["items"][0]["detail"]["run_id"] if committed["items"] else None
        state.finished.set()

    thread = threading.Thread(target=_run_boot, daemon=True)
    thread.start()
    try:
        assert state.enter_done.wait(5.0), (
            "lifespan ran the journaled dream chain inline: the port never bound"
        )
        assert state.entered <= 1, "a second dream launched during the blocked window"
        assert state.completed == 0, "the dream chain ran synchronously during startup"
    finally:
        _BlockingStub.release.set()
    assert state.finished.wait(10.0), "the daemon teardown never completed"
    thread.join(timeout=5.0)

    assert state.completed_after_release == 1, "the deferred resume never ran"
    assert state.committed_total >= 1, "the resume's merge never committed"
    assert state.committed_run_id == SNAP_ID
    on_disk = _journal_state(config_path.parent)
    assert on_disk is not None and resume_boundary(on_disk) is None

    import lancedb

    db = lancedb.connect(str(config_path.parent / "chunks.lance"))
    rows = db.open_table("chunks").to_arrow().to_pylist()
    chunk_row = [row for row in rows if row["chunk_id"] == CHUNK_ID]
    assert chunk_row and chunk_row[0]["consolidated"] is True


def test_deferred_resume_runs_and_merges_after_boot(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferred RESUME job runs the journaled snapshot through the chain
    after boot: the reflect chat fires and the merge commits the run."""
    _CountingStub.reset()
    monkeypatch.setattr(reflect_module, "StubReflectLLM", _CountingStub)
    _seed_journal(config_path.parent)
    _seed_stores(config_path.parent)

    with _boot() as client:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and len(_CountingStub.completed) < 1:
            time.sleep(0.05)
        assert len(_CountingStub.completed) >= 1, "the deferred resume never ran"
        _wait_dream_idle(client)
        committed = client.get("/api/v1/audit", params={"action": "dream_committed"}).json()
        assert committed["total"] >= 1
        assert committed["items"][0]["detail"]["run_id"] == SNAP_ID

    on_disk = _journal_state(config_path.parent)
    assert on_disk is not None and resume_boundary(on_disk) is None


def _vote_payload() -> dict[str, Any]:
    """A minimal seat reflection payload the combiner can consume (empty triples
    is a valid all-noise seat). It carries the consumed chunk id so the merge's
    safe-clear marks that exact row consolidated."""
    return {
        "snapshot_id": SNAP_ID,
        "profile_id": PROFILE,
        "turn_range": {"start": RANGE.start, "end": RANGE.end},
        "prompt_version": "v1",
        "model_id": "stub",
        "triples": [],
        "conflicts": [],
        "delta_overflow": [],
        "consumed_chunk_ids": [CHUNK_ID],
    }


def _seed_vote_combine_journal(tmp_path: Path) -> Snapshot:
    """A journaled snapshot at the vote COMBINE boundary: A and B both ran and
    their payloads are persisted, but the combiner never folded them and the
    single merge never committed — the daemon must resume at combine, merge,
    and safe-clear the range."""
    snap = (
        Snapshot(
            snapshot_id=SNAP_ID,
            profile_id=PROFILE,
            turn_range=RANGE,
            chunks=(SnapshotChunk.from_stamp(_stamp()),),
            created_at=1.0,
            phases=frozenset(
                {
                    SnapshotPhase.SNAPSHOT_DONE.value,
                    SnapshotPhase.REFLECT_A_DONE.value,
                    SnapshotPhase.REFLECT_B_DONE.value,
                }
            ),
        )
        .with_vote_seat("a", _vote_payload())
        .with_vote_seat("b", _vote_payload())
    )
    write_snapshot_file(tmp_path / "dreams", snap)
    return snap


def test_vote_combine_boundary_resume_consolidates_chunks(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER-1: a crash-resume at the vote COMBINE boundary must resume the
    trigger (DREAMING + current_range) so the deferred combine -> merge ->
    safe-clear fires. Before the fix the 'combine' boundary fell through with no
    trigger.resume(), so on_merge_committed no-op'd and the merged chunks were
    never marked consolidated (double-representation + stale search surface)."""
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '[dream]\nauto_trigger = false\nensemble = "vote"\nfloor_pool_points = 0.1\nidle_min_sec = 0.0\n'
        + '[dream.llm.dream_vote]\ndriver = "stub"\nmodel = "stub"\n',
        encoding="utf-8",
    )
    _seed_vote_combine_journal(config_path.parent)
    _seed_stores(config_path.parent)

    with _boot() as client:
        deadline = time.monotonic() + 10.0
        committed: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            committed = client.get("/api/v1/audit", params={"action": "dream_committed"}).json()
            if committed["total"] >= 1:
                break
            time.sleep(0.05)
        assert committed is not None and committed["total"] >= 1, "the vote-combine resume never committed"
        _wait_dream_idle(client)

    on_disk = _journal_state(config_path.parent)
    assert on_disk is not None and resume_boundary(on_disk) is None

    import lancedb

    db = lancedb.connect(str(config_path.parent / "chunks.lance"))
    rows = db.open_table("chunks").to_arrow().to_pylist()
    chunk_row = [row for row in rows if row["chunk_id"] == CHUNK_ID]
    assert chunk_row and chunk_row[0]["consolidated"] is True


def _seed_vote_b_journal(tmp_path: Path) -> Snapshot:
    """A journaled snapshot at the vote REFLECT_B boundary: A ran and its
    payload is persisted, but seat B never generated — the daemon must resume
    at reflect_b, run B, then combine -> merge, and safe-clear the range."""
    snap = Snapshot(
        snapshot_id=SNAP_ID,
        profile_id=PROFILE,
        turn_range=RANGE,
        chunks=(SnapshotChunk.from_stamp(_stamp()),),
        created_at=1.0,
        phases=frozenset(
            {
                SnapshotPhase.SNAPSHOT_DONE.value,
                SnapshotPhase.REFLECT_A_DONE.value,
            }
        ),
    ).with_vote_seat("a", _vote_payload())
    write_snapshot_file(tmp_path / "dreams", snap)
    return snap


def test_vote_reflect_b_boundary_resume_runs_b_and_consolidates(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER-1: a crash-resume at the vote REFLECT_B boundary must resume the
    trigger (DREAMING + current_range) so the deferred resume runs seat B, then
    combine -> merge -> safe-clear. Before the fix the 'reflect_b' boundary fell
    through with no trigger.resume(), so on_merge_committed no-op'd and the
    merged chunks were never marked consolidated."""
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '[dream]\nauto_trigger = false\nensemble = "vote"\nfloor_pool_points = 0.1\nidle_min_sec = 0.0\n'
        + '[dream.llm.dream_vote]\ndriver = "stub"\nmodel = "stub"\n',
        encoding="utf-8",
    )
    _seed_vote_b_journal(config_path.parent)
    _seed_stores(config_path.parent)

    with _boot() as client:
        deadline = time.monotonic() + 10.0
        committed: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            committed = client.get("/api/v1/audit", params={"action": "dream_committed"}).json()
            if committed["total"] >= 1:
                break
            time.sleep(0.05)
        assert committed is not None and committed["total"] >= 1, "the vote reflect_b resume never committed"
        _wait_dream_idle(client)

    on_disk = _journal_state(config_path.parent)
    assert on_disk is not None and resume_boundary(on_disk) is None

    import lancedb

    db = lancedb.connect(str(config_path.parent / "chunks.lance"))
    rows = db.open_table("chunks").to_arrow().to_pylist()
    chunk_row = [row for row in rows if row["chunk_id"] == CHUNK_ID]
    assert chunk_row and chunk_row[0]["consolidated"] is True


def test_scheduler_waits_for_deferred_resumes_before_first_tick(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin (scheduler gate): the scheduler's first tick waits for the deferred
    resumes to drain. A tick during the blocked recovery window would emit a
    dream for the still-unconsolidated profile, queueing a duplicate behind
    the resume; once the resume drains (and safe-clears), no second dream may
    run."""
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "[dream]\nauto_trigger = true\nfloor_pool_points = 0.1\nidle_min_sec = 0.0\n",
        encoding="utf-8",
    )
    _BlockingStub.reset()
    monkeypatch.setattr(reflect_module, "StubReflectLLM", _BlockingStub)
    _seed_journal(config_path.parent)
    _seed_stores(config_path.parent, pool_balance=10.0)

    state = _BootState()

    def _run_boot() -> None:
        with _boot() as client:
            state.client = client
            state.entered = len(_BlockingStub.entered)
            state.enter_done.set()
            _BlockingStub.release.wait(10.0)  # hold the resume blocked
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and len(_BlockingStub.completed) < 1:
                time.sleep(0.05)
            state.completed_after_release = len(_BlockingStub.completed)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not client.app.state.dream_worker.resume_drained.is_set():
                time.sleep(0.05)
            state.drained = client.app.state.dream_worker.resume_drained.is_set()
            time.sleep(1.0)  # the scheduler's first post-drain tick
            state.completed_later = len(_BlockingStub.completed)
            state.status_after_drain = client.post(
                "/memory/dream_status", json={"profile_id": PROFILE}
            ).json()
        state.finished.set()

    thread = threading.Thread(target=_run_boot, daemon=True)
    thread.start()
    try:
        assert state.enter_done.wait(5.0), "lifespan never bound the port"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(_BlockingStub.entered) < 1:
            time.sleep(0.02)
        assert len(_BlockingStub.entered) >= 1, "the deferred resume never reached the LLM"
        assert state.client is not None, "the daemon never bound"
        status = state.client.post("/memory/dream_status", json={"profile_id": PROFILE}).json()
        assert status["pending_queue"] == 0, f"scheduler ticked before the resume drained: {status}"
        assert status["state"] == "dreaming", status
    finally:
        _BlockingStub.release.set()
    assert state.finished.wait(10.0), "the daemon teardown never completed"
    thread.join(timeout=5.0)

    assert state.completed_after_release == 1, "the deferred resume never ran"
    assert state.drained is True, "the drain signal never set after the resume"
    assert state.completed_later == 1, "a duplicate dream ran over the resumed range"
    assert state.status_after_drain["pending_queue"] == 0


def test_scheduler_starts_even_when_a_resume_job_raises(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin (exception drain): a resume job that raises must still decrement
    the drain counter (try/finally) — otherwise the scheduler would await the
    drain event forever and never tick."""
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "[dream]\nauto_trigger = true\nfloor_pool_points = 0.1\nidle_min_sec = 0.0\n",
        encoding="utf-8",
    )
    _seed_journal(config_path.parent)
    _seed_stores(config_path.parent, pool_balance=10.0)

    real_run = DreamPipeline.run

    def _raising_run(self: DreamPipeline, snapshot: Snapshot) -> None:
        if snapshot.snapshot_id == SNAP_ID:
            raise RuntimeError("resume exploded (exception-drain pin)")
        real_run(self, snapshot)

    monkeypatch.setattr(DreamPipeline, "run", _raising_run)

    with _boot() as client:
        worker = client.app.state.dream_worker
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not worker.resume_drained.is_set():
            time.sleep(0.05)
        assert worker.resume_drained.is_set(), "the drain signal never set after the failing resume"
        # the scheduler's first tick runs once drained: the failed resume left
        # the profile in flight, so the emitted dream queues behind it
        deadline = time.monotonic() + 5.0
        status: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status = client.post("/memory/dream_status", json={"profile_id": PROFILE}).json()
            if status["pending_queue"] >= 1:
                break
            time.sleep(0.05)
        assert status is not None and status["pending_queue"] >= 1, (
            "the scheduler never ticked after the drain signal"
        )


# ---------------------------------------------------------------- drain timeout (IMPORTANT-1)


class _EmptyMeta:
    """MetaStore-shaped double with no profiles (the scheduler ticks over it)."""

    def list_profiles(self) -> list[Any]:
        return []

    def pool_state(self, profile_id: str) -> Any:
        del profile_id
        return type("P", (), {"balance": 0.0})()

    def pool_states(self) -> dict[str, Any]:
        return {}

    def audit_append(self, entry: Any) -> None:
        del entry


class _EmptyVector:
    """VectorStore-shaped double with no chunks."""

    def list_chunks(self, filter: Any, page: Any) -> Any:
        del filter, page
        return type("R", (), {"items": [], "total": 0})()


class _EmptyStores:
    vector = _EmptyVector()
    meta = _EmptyMeta()


def test_scheduler_ticks_after_resume_drain_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """IMPORTANT-1: a wedged resume executor must not stall the scheduler
    forever. When the drain event never sets, the scheduler waits only the
    bounded timeout, logs a warning, and ticks anyway."""
    from mnemoseed_local.config import Config, DreamConfig
    from mnemoseed_local.dream import DreamScheduler

    never_drain = asyncio.Event()  # never set: the resume executor is wedged
    scheduler = DreamScheduler(
        _EmptyStores(),
        Config(dream=DreamConfig()),
        resume_drain=never_drain,
        resume_drain_timeout_s=0.05,
    )

    ticks: list[float] = []
    ticked = asyncio.Event()
    real_tick = scheduler.tick

    def _recording_tick() -> list[Any]:
        ticks.append(time.perf_counter())
        ticked.set()
        return real_tick()

    monkeypatch.setattr(scheduler, "tick", _recording_tick)

    import logging

    async def _run() -> None:
        with caplog.at_level(logging.WARNING, logger="mnemoseed_local.dream.trigger"):
            task = asyncio.create_task(scheduler.run_forever())
            try:
                await asyncio.wait_for(ticked.wait(), timeout=5.0)
                assert len(ticks) >= 1, "the scheduler never ticked after the drain timeout"
                assert caplog.text, "no warning was logged for the drain timeout"
                assert "resume drain timed out" in caplog.text
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(_run())
