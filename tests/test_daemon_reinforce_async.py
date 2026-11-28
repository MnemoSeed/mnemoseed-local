"""Recall hot-path latency fixes: fire-and-forget reinforcement queue and the
index-residue TTL cache (PRD-recall-latency-fix-brief-v1).

The drilldown record (eval/2026-11-28T04-18-07Z) measured the synchronous
reinforcement write at 3,078 ms (52% of the recall wire) and the residue scan
at 1,256 ms (21%) on the real 21,894-chunk corpus. These tests pin the async
queue semantics (recall returns before reinforcement lands, close flushes,
overflow drops are counted, worker errors never surface) and the residue TTL
cache (scan-free within the TTL, invalidated by remember(), recomputed on
expiry).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.config import load_config
from mnemoseed_local.daemon.memory import _RESIDUE_SCAN_LIMIT, MemoryService
from mnemoseed_local.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.factory import build_stores
from mnemoseed_local.storage.ports import StoredProfile
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_PROFILE = "default"
_EMBEDDER = SyntheticEmbedder(dimension=64)


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


class SpyReinforcer:
    """Records record_hits invocations with start/finish timestamps."""

    def __init__(self, delay_s: float = 0.0, raise_first: bool = False) -> None:
        self.delay_s = delay_s
        self.raise_first = raise_first
        self._raised = False
        self.lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []

    def record_hits(self, chunk_ids, node_ids, *, rescued_chunk_ids=(), stamps=None) -> None:
        started = time.perf_counter()
        with self.lock:
            raise_now = self.raise_first and not self._raised
            self._raised = True
        if self.delay_s:
            time.sleep(self.delay_s)
        if raise_now:
            raise RuntimeError("simulated reinforcer failure")
        with self.lock:
            self.calls.append(
                {
                    "chunk_ids": list(chunk_ids),
                    "node_ids": list(node_ids),
                    "started_at": started,
                    "finished_at": time.perf_counter(),
                }
            )


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryService:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config = load_config(cfg)
    stores = build_stores(config)
    stores.meta.upsert_profile(StoredProfile(profile_id=_PROFILE))
    svc = MemoryService(stores, config)
    yield svc
    svc.close()


def test_recall_returns_before_reinforcement_lands(service: MemoryService) -> None:
    """The reinforcement write must not block the recall wire: with a slow
    reinforcer the recall response precedes the write's completion."""
    spy = SpyReinforcer(delay_s=1.5)
    service._reinforcer = spy
    service.remember(profile_id=_PROFILE, text="async queue probe memory")
    start = time.perf_counter()
    service.recall(profile_id=_PROFILE, query="async queue probe memory")
    elapsed = time.perf_counter() - start
    assert elapsed < 0.75, f"recall blocked on reinforcement for {elapsed:.2f}s"
    service._flush_reinforcement()
    assert len(spy.calls) == 1


def test_close_flushes_queued_hits(service: MemoryService) -> None:
    """close() drains everything still queued before the stores go away."""
    spy = SpyReinforcer(delay_s=0.05)
    service._reinforcer = spy
    service.remember(profile_id=_PROFILE, text="flush probe memory")
    service.recall(profile_id=_PROFILE, query="flush probe memory")
    service.close()
    assert len(spy.calls) >= 1


def test_flood_drops_oldest_and_counts(service: MemoryService) -> None:
    """Bursts beyond the bounded queue drop the oldest items, count them, and
    never crash the worker."""
    spy = SpyReinforcer(delay_s=0.3)
    service._reinforcer = spy
    for index in range(70):
        service._enqueue_reinforce([f"chunk-{index}"], [], rescued=(), stamps={})
    assert service._reinforce_dropped > 0
    service._flush_reinforcement()
    service.close()
    assert len(spy.calls) < 70
    assert all(call["chunk_ids"] for call in spy.calls)


def test_worker_error_is_swallowed_and_drain_continues(service: MemoryService) -> None:
    """A failing reinforcement must not kill the worker or surface to recall."""
    spy = SpyReinforcer(raise_first=True)
    service._reinforcer = spy
    service.remember(profile_id=_PROFILE, text="error swallow probe one")
    service.recall(profile_id=_PROFILE, query="error swallow probe one")
    service.remember(profile_id=_PROFILE, text="error swallow probe two")
    service.recall(profile_id=_PROFILE, query="error swallow probe two")
    service._flush_reinforcement()
    assert len(spy.calls) >= 1


def test_index_residue_scan_is_cached_within_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second recall inside the TTL must not rescan the store."""
    clock = {"now": 1_000_000.0}
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config = load_config(cfg)
    stores = build_stores(config)
    stores.meta.upsert_profile(StoredProfile(profile_id=_PROFILE))
    svc = MemoryService(stores, config, clock=lambda: clock["now"])
    original_list_chunks = stores.vector.list_chunks
    counter = {"calls": 0}

    def counting_list_chunks(*args: Any, **kwargs: Any):
        page = args[1] if len(args) > 1 else kwargs.get("page")
        if page is not None and getattr(page, "limit", None) == _RESIDUE_SCAN_LIMIT:
            counter["calls"] += 1
        return original_list_chunks(*args, **kwargs)

    monkeypatch.setattr(stores.vector, "list_chunks", counting_list_chunks)
    try:
        svc.remember(profile_id=_PROFILE, text="residue cache probe memory")
        first = svc.recall(profile_id=_PROFILE, query="residue cache probe memory")
        calls_after_first = counter["calls"]
        assert calls_after_first > 0
        second = svc.recall(profile_id=_PROFILE, query="residue cache probe memory")
        assert counter["calls"] == calls_after_first, "TTL cache miss on the second recall"
        assert first["memory"]["index_residue"]["cached"] is False
        assert second["memory"]["index_residue"]["cached"] is True
        assert second["memory"]["index_residue"]["cache_age_s"] >= 0
    finally:
        svc.close()


def test_remember_invalidates_the_residue_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new pin must be visible on the next recall despite a warm cache."""
    clock = {"now": 2_000_000.0}
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config = load_config(cfg)
    stores = build_stores(config)
    stores.meta.upsert_profile(StoredProfile(profile_id=_PROFILE))
    svc = MemoryService(stores, config, clock=lambda: clock["now"])
    original_list_chunks = stores.vector.list_chunks
    counter = {"calls": 0}

    def counting_list_chunks(*args: Any, **kwargs: Any):
        page = args[1] if len(args) > 1 else kwargs.get("page")
        if page is not None and getattr(page, "limit", None) == _RESIDUE_SCAN_LIMIT:
            counter["calls"] += 1
        return original_list_chunks(*args, **kwargs)

    monkeypatch.setattr(stores.vector, "list_chunks", counting_list_chunks)
    try:
        svc.recall(profile_id=_PROFILE, query="warm the cache")
        calls_warm = counter["calls"]
        svc.remember(profile_id=_PROFILE, text="a brand new pinned fact")
        svc.recall(profile_id=_PROFILE, query="a brand new pinned fact")
        assert counter["calls"] > calls_warm, "remember() failed to invalidate the residue cache"
    finally:
        svc.close()


def test_residue_cache_expires_after_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the TTL the scan reruns and the disclosure flips back to fresh."""
    clock = {"now": 3_000_000.0}
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config = load_config(cfg)
    stores = build_stores(config)
    stores.meta.upsert_profile(StoredProfile(profile_id=_PROFILE))
    svc = MemoryService(stores, config, clock=lambda: clock["now"])
    original_list_chunks = stores.vector.list_chunks
    counter = {"calls": 0}

    def counting_list_chunks(*args: Any, **kwargs: Any):
        page = args[1] if len(args) > 1 else kwargs.get("page")
        if page is not None and getattr(page, "limit", None) == _RESIDUE_SCAN_LIMIT:
            counter["calls"] += 1
        return original_list_chunks(*args, **kwargs)

    monkeypatch.setattr(stores.vector, "list_chunks", counting_list_chunks)
    try:
        svc.remember(profile_id=_PROFILE, text="ttl expiry probe memory")
        svc.recall(profile_id=_PROFILE, query="ttl expiry probe memory")
        calls_cached = counter["calls"]
        svc.recall(profile_id=_PROFILE, query="ttl expiry probe memory")
        assert counter["calls"] == calls_cached
        clock["now"] += 61.0
        third = svc.recall(profile_id=_PROFILE, query="ttl expiry probe memory")
        assert counter["calls"] > calls_cached, "expired cache did not recompute"
        assert third["memory"]["index_residue"]["cached"] is False
    finally:
        svc.close()
