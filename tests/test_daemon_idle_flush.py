"""Idle-session flush sweeper: the daemon's durability net for open turns.

A session whose last ingest went idle past the threshold still holds an
unflushed open turn in RAM (hosts that fire no per-reply flush). The sweeper
drains it with flush semantics — the turn persists, the session stays open
and unsettled — bounded to N sessions per tick, oldest activity first, one
failing session's drain never stops the sweep, and a drain that fails inside
the bound lane retries at WARN on the next tick until it persists.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from mnemoseed_local.capture import ScoringPipeline, TurnScorer, TurnSegmenter, WritingPipeline
from mnemoseed_local.config import CaptureConfig, ConfigError, load_config
from mnemoseed_local.configwrite.service import CONFIG_KEY_REGISTRY, ConfigWriteService
from mnemoseed_local.daemon.app import DrainLane
from mnemoseed_local.daemon.idle_flush import IdleFlushSweeper
from mnemoseed_local.schema.turn import HostId, IngestEvent, IngestEventType, MessageContent
from mnemoseed_local.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.factory import build_stores
from mnemoseed_local.storage.ports import ChunkFilter, Page, StoredProfile
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

PROFILE = "default"

_IDLE_PROMPT = "把部署脚本迁移到 systemd 定时器并写回滚步骤"
_FRESH_PROMPT = "刚提交的一个全新问题"


@pytest.fixture(autouse=True)
def _ensure_real_drivers() -> None:
    """test_daemon clears the shared registries; re-register the real drivers."""
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)


def _config_toml(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def funnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, object, WritingPipeline, TurnSegmenter, list[float]]:
    """Real embedded stores + the daemon capture funnel + an injectable clock."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config = load_config(_config_toml(tmp_path))
    stores = build_stores(config)
    stores.meta.upsert_profile(StoredProfile(profile_id=PROFILE))
    clock = [1_800_000_000.0]
    scoring = ScoringPipeline(scorer=TurnScorer(embedder=stores.embed))
    writing = WritingPipeline(
        store=stores.vector, inner=scoring, embedder=stores.embed, clock=lambda: clock[0]
    )
    segmenter = TurnSegmenter(writing)
    return stores, config, writing, segmenter, clock


def _prompt(segmenter: TurnSegmenter, session: str, ts: float, text: str) -> None:
    segmenter.ingest(
        IngestEvent(
            host=HostId.ZCODE,
            event=IngestEventType.USER_PROMPT,
            session_id=session,
            profile_id=PROFILE,
            ts=ts,
            content=MessageContent(text=text),
        )
    )


def _chunk_texts(stores: object) -> list[str]:
    return [
        chunk.text
        for chunk in stores.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(limit=50)).items  # type: ignore[attr-defined]
    ]


# ---------------------------------------------------------------- sweep behavior


def test_idle_sweeper_drains_an_idle_session_without_settling(funnel) -> None:
    """A session idle past the threshold has its open turn flushed and drained
    into the stores; a fresh session stays untouched; the swept session keeps
    accepting ingest (flush semantics, never a settle)."""
    stores, config, writing, segmenter, clock = funnel
    now = clock[0]
    config.capture = CaptureConfig(idle_flush_sec=300.0)
    sweeper = IdleFlushSweeper(segmenter, writing, config, clock=lambda: clock[0])
    _prompt(segmenter, "sess-idle", now - 400.0, _IDLE_PROMPT)
    _prompt(segmenter, "sess-fresh", now - 5.0, _FRESH_PROMPT)

    swept = sweeper.run_once()

    assert swept == ["sess-idle"]
    texts = _chunk_texts(stores)
    assert any("systemd" in text for text in texts), "the idle session's turn was never persisted"
    assert not any("全新问题" in text for text in texts), "a fresh session must not be swept"
    assert writing.settled("sess-idle") is None
    _prompt(segmenter, "sess-idle", now, "扫完之后的下一问")
    assert segmenter.flush("sess-idle", PROFILE) == 1


def test_idle_sweep_is_bounded_and_oldest_first(funnel) -> None:
    """At most N sessions drain per tick, oldest activity first; the rest wait
    for the next tick. Sessions are inserted NEWEST first, so insertion order
    is the reverse of idle order and the assertion proves the last_ts sort."""
    stores, config, writing, segmenter, clock = funnel
    now = clock[0]
    config.capture = CaptureConfig(idle_flush_sec=100.0)
    sweeper = IdleFlushSweeper(segmenter, writing, config, clock=lambda: clock[0], max_per_tick=2)
    _prompt(segmenter, "old-3", now - 200.0, _IDLE_PROMPT)
    _prompt(segmenter, "old-2", now - 300.0, _IDLE_PROMPT)
    _prompt(segmenter, "old-1", now - 400.0, _IDLE_PROMPT)

    assert sweeper.run_once() == ["old-1", "old-2"]
    assert sweeper.run_once() == ["old-3"]


def test_one_failed_drain_never_stops_the_sweep(
    funnel, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing session's drain is logged and skipped; the remaining idle
    sessions still drain on the same tick."""
    stores, config, writing, segmenter, clock = funnel
    now = clock[0]
    config.capture = CaptureConfig(idle_flush_sec=100.0)
    sweeper = IdleFlushSweeper(segmenter, writing, config, clock=lambda: clock[0])
    _prompt(segmenter, "sess-dead", now - 400.0, _IDLE_PROMPT)
    _prompt(segmenter, "sess-alive", now - 300.0, _IDLE_PROMPT)
    original_drain = writing.drain

    def flaky(session_id: str) -> object:
        if session_id == "sess-dead":
            raise RuntimeError("store wedged")
        return original_drain(session_id)

    monkeypatch.setattr(writing, "drain", flaky)
    with caplog.at_level(logging.WARNING, logger="mnemoseed_local.daemon.idle_flush"):
        swept = sweeper.run_once()

    assert swept == ["sess-alive"]
    assert any("sess-dead" in record.message for record in caplog.records)


def test_already_flushed_sessions_are_never_reswept(funnel) -> None:
    """A swept (or Stop-flushed) session holds no open turn: the next tick
    leaves it alone instead of churning no-op drains."""
    stores, config, writing, segmenter, clock = funnel
    now = clock[0]
    config.capture = CaptureConfig(idle_flush_sec=100.0)
    sweeper = IdleFlushSweeper(segmenter, writing, config, clock=lambda: clock[0])
    _prompt(segmenter, "sess-done", now - 400.0, _IDLE_PROMPT)
    assert sweeper.run_once() == ["sess-done"]

    clock[0] = now + 600.0
    assert sweeper.run_once() == []


# ---------------------------------------------------------------- bound lane


class RecordingLane:
    """Minimal DrainSubmitter: records submits; the recorded drains execute on
    demand, after the fire-and-forget submit has already returned (a drain
    exception is swallowed exactly like an unread future)."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self._pending: list[tuple[Callable[[str], object], str]] = []

    def submit(self, fn: Callable[[str], object], session_id: str) -> None:
        self.submitted.append(session_id)
        self._pending.append((fn, session_id))

    def run_pending(self) -> None:
        pending, self._pending = self._pending, []
        for fn, session_id in pending:
            try:
                fn(session_id)
            except Exception:
                pass


def test_sweeper_queues_drains_through_the_bound_lane(funnel) -> None:
    """The production drain line: the bound lane receives every drained
    session in sweep order, and the lane-executed drain persists the chunks."""
    stores, config, writing, segmenter, clock = funnel
    now = clock[0]
    config.capture = CaptureConfig(idle_flush_sec=100.0)
    lane = RecordingLane()
    sweeper = IdleFlushSweeper(segmenter, writing, config, lane=lane, clock=lambda: clock[0])
    _prompt(segmenter, "sess-b", now - 300.0, _IDLE_PROMPT)
    _prompt(segmenter, "sess-a", now - 400.0, _IDLE_PROMPT)

    assert sweeper.run_once() == ["sess-a", "sess-b"]
    assert lane.submitted == ["sess-a", "sess-b"]

    lane.run_pending()
    assert any("systemd" in text for text in _chunk_texts(stores))


def test_failed_lane_drain_is_retried_on_the_next_tick(
    funnel, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A drain that fails inside the lane (fire-and-forget) becomes a WARN
    retry on the next tick and persists once the store recovers; a session the
    sweeper already flushed is recoverable by no other path."""
    stores, config, writing, segmenter, clock = funnel
    now = clock[0]
    config.capture = CaptureConfig(idle_flush_sec=100.0)
    lane = RecordingLane()
    sweeper = IdleFlushSweeper(segmenter, writing, config, lane=lane, clock=lambda: clock[0])
    _prompt(segmenter, "sess-a", now - 400.0, _IDLE_PROMPT)
    original_drain = writing.drain
    attempts = {"sess-a": 0}

    def flaky(session_id: str) -> object:
        if session_id == "sess-a":
            attempts["sess-a"] += 1
            if attempts["sess-a"] == 1:
                raise RuntimeError("store wedged")
        return original_drain(session_id)

    monkeypatch.setattr(writing, "drain", flaky)

    with caplog.at_level(logging.WARNING, logger="mnemoseed_local.daemon.idle_flush"):
        assert sweeper.run_once() == ["sess-a"]
        lane.run_pending()
        # the flushed turn is invisible to idle selection now: only the
        # failure ledger can still recover it
        assert sweeper.run_once() == ["sess-a"]
        assert any("retrying failed drain for session sess-a" in record.message for record in caplog.records)
        lane.run_pending()

    assert any("systemd" in text for text in _chunk_texts(stores))
    assert sweeper.run_once() == []


# ---------------------------------------------------------------- daemon loop


async def test_run_forever_ticks_and_stops_cleanly(funnel) -> None:
    """The daemon-owned loop ticks once immediately, drains the idle session,
    and cancels cleanly with no pending task left behind."""
    stores, config, writing, segmenter, clock = funnel
    config.capture = CaptureConfig(idle_flush_sec=60.0)
    sweeper = IdleFlushSweeper(segmenter, writing, config, clock=lambda: clock[0], tick_s=0.01)
    _prompt(segmenter, "sess-idle", clock[0] - 100.0, _IDLE_PROMPT)

    task = asyncio.create_task(sweeper.run_forever())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while not _chunk_texts(stores):
        if loop.time() >= deadline:
            task.cancel()
            pytest.fail("the sweeper never drained the idle session")
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.0)
    assert task.cancelled() is True


# ---------------------------------------------------------------- drain lane logging


def _exploding_drain(session_id: str) -> object:
    raise RuntimeError("lane drain exploded")


async def test_drain_lane_submit_logs_a_failed_drain(caplog: pytest.LogCaptureFixture) -> None:
    """The submit done-callback logs a drain exception the moment it resolves:
    fire-and-forget submitters (the sweeper) never read the future."""
    lane = DrainLane()
    try:
        with caplog.at_level(logging.WARNING, logger="mnemoseed_local.daemon"):
            lane.submit(_exploding_drain, "sess-x")
            deadline = time.monotonic() + 2.0
            while not any("sess-x" in record.message for record in caplog.records):
                if time.monotonic() >= deadline:
                    pytest.fail("the lane never logged the failed drain")
                await asyncio.sleep(0.01)
            assert any("drain failed for session sess-x" in record.message for record in caplog.records)
    finally:
        await lane.stop()


# ---------------------------------------------------------------- config key


def _write_key_toml(tmp_path: Path, line: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n[capture]\n' + line + "\n",
        encoding="utf-8",
    )
    return path


def test_capture_idle_flush_sec_roundtrips_through_the_loader(tmp_path: Path) -> None:
    config = load_config(_write_key_toml(tmp_path, "idle_flush_sec = 120"))
    assert config.capture.idle_flush_sec == 120.0


def test_capture_idle_flush_sec_defaults_to_ten_minutes(tmp_path: Path) -> None:
    config = load_config(_config_toml(tmp_path))
    assert config.capture.idle_flush_sec == 600.0


@pytest.mark.parametrize("raw", ["0", "-5", '"soon"'])
def test_capture_idle_flush_sec_rejects_non_positive_values(tmp_path: Path, raw: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write_key_toml(tmp_path, f"idle_flush_sec = {raw}"))
    assert "capture.idle_flush_sec" in str(excinfo.value)


def test_capture_idle_flush_sec_is_a_registry_key_with_hot_apply(tmp_path: Path) -> None:
    """The knob is first-class like its [capture] siblings: registry-visible,
    resolved by config get, hot-applied on set, persisted to the file."""
    path = _write_key_toml(tmp_path, "idle_flush_sec = 120")
    assert "capture.idle_flush_sec" in CONFIG_KEY_REGISTRY
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0)

    service.set("capture.idle_flush_sec", 90, actor="test")

    assert service.get()["config"]["capture"]["idle_flush_sec"] == 90.0
    assert load_config(path).capture.idle_flush_sec == 90.0
