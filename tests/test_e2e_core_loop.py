"""Bootstrap e2e — one full in-process core loop on the A2 substrate.

Proves the MVP loop end to end over REAL embedded stores (sqlite + lancedb,
synthetic embedder, stub dream LLM), without any network:

    capture turns -> score pool triggers -> dream --once (stub driver)
        -> graph nodes written + chunks marked consolidated
        -> recall lists the memories -> decay sweep runs without crashing

Also pins the daemon's boot wiring shape (the same funnel `daemon.app` builds).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mnemoseed_local.capture import (
    ScoringPipeline,
    TurnScorer,
    TurnSegmenter,
    WritingPipeline,
)
from mnemoseed_local.capture.pool import PoolEventKind, ScorePool
from mnemoseed_local.config import Config, RoleLLMConfig
from mnemoseed_local.daemon.memory import MemoryService
from mnemoseed_local.decay import DecaySweeper
from mnemoseed_local.dream import (
    DreamPipeline,
    DreamTrigger,
    FileSnapshotter,
    Merger,
    ReflectOrchestrator,
    TokenLedger,
)
from mnemoseed_local.llm import RoleRouter
from mnemoseed_local.schema.turn import HostId, IngestEvent, IngestEventType, MessageContent
from mnemoseed_local.storage.factory import Stores, build_stores
from mnemoseed_local.storage.ports import ChunkFilter, NodeFilter, Page, TurnRange

PROFILE = "default"
SESSION = "sess-e2e"

DURABLE_TURNS = (
    "我决定以后都用 pnpm 管理依赖",
    "我打算把日志系统完整迁移到时序数据库存储",
    "我坚持每次提交前都跑一遍完整的测试套件",
)


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Stores, Config]:
    """Real embedded stores over tmp_path (synthetic embedder, no downloads)."""
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
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    from mnemoseed_local.config import load_config

    config = load_config(cfg)
    config.llm = {"dream": RoleLLMConfig(role="dream", driver="stub", model="stub")}
    return build_stores(config), config


def _ingest(segmenter: TurnSegmenter, session: str, text: str, ts: float) -> None:
    segmenter.ingest(
        IngestEvent(
            host=HostId.CLAUDE_CODE,
            event=IngestEventType.USER_PROMPT,
            session_id=session,
            profile_id=PROFILE,
            ts=ts,
            content=MessageContent(text=text),
        )
    )


def test_full_core_loop(stores: tuple[Stores, Config]) -> None:
    built, config = stores
    meta = built.meta
    from mnemoseed_local.storage.ports import StoredProfile

    meta.upsert_profile(StoredProfile(profile_id=PROFILE, created_at=time.time()))

    # ---- funnel: the same wiring daemon.app._build_capture uses
    snapshotter = FileSnapshotter(store=built.vector, meta=built.meta)
    trigger = DreamTrigger(
        snapshotter=snapshotter,
        auto_trigger=False,  # FR-2.8 manual-first: dream --once runs the cycle
        purger=snapshotter.purge_snapshot,
    )
    router = RoleRouter(routes=config.llm, audit=meta, generation=lambda role: 0)
    reflector = ReflectOrchestrator(
        llm=router.resolve("dream"),
        resolve_llm=lambda: router.resolve("dream"),
        directory=snapshotter.directory,
        on_done=trigger.on_reflect_complete,
        ledger=TokenLedger(meta=meta),
    )
    merger = Merger(
        graph_main=built.graph,
        graph_isolated=None,
        meta=meta,
        on_committed=trigger.on_merge_committed,
    )
    pipeline = DreamPipeline(trigger=trigger, snapshotter=snapshotter, reflector=reflector, merger=merger)
    snapshotter.on_ready = pipeline.on_snapshot_ready

    # deterministic clock so the pool's idle window can be satisfied instantly
    wall = [1_000.0]

    def _clock() -> float:
        return wall[0]

    fired: list = []
    pool = ScorePool(clock=_clock, backend=meta, sink=fired.append)
    scoring = ScoringPipeline(scorer=TurnScorer(embedder=built.embed), pool=pool)
    writing = WritingPipeline(store=built.vector, inner=scoring, embedder=built.embed, clock=_clock)
    segmenter = TurnSegmenter(writing)

    # ---- capture: three durable turns, then settle the session (drain)
    for index, text in enumerate(DURABLE_TURNS):
        _ingest(segmenter, SESSION, text, ts=float(index))
    turn_range = segmenter.end_session(SESSION, PROFILE)
    assert turn_range == TurnRange(0, len(DURABLE_TURNS) - 1)
    # the daemon drains on /session/end (off the ingest hot path)
    outcomes = writing.drain(SESSION)
    assert outcomes, "no durable turn survived the funnel"

    # ---- score pool: advance the injected clock past the idle window, then a
    # fresh credit re-evaluates the quiet balance -> a DREAM_TRIGGER event
    # fires (balance >= 10, idle >= 5)
    wall[0] += 10.0
    events = pool.add_points(PROFILE, 0.0, turn_range)
    assert events, "score pool never reached the dream threshold"
    assert any(event.kind is PoolEventKind.DREAM_TRIGGER for event in events)
    # deliver the fired events to the trigger (the daemon's _DreamRelay.flush)
    for event in events:
        trigger.handle_event(event)
    assert trigger.status(PROFILE).pending_manual == 1

    # ---- dream --once with the stub driver
    assert trigger.dream_once(PROFILE) is True
    assert trigger.status(PROFILE).state.value in ("snapshotting", "dreaming", "merging", "idle")

    # ---- write-back: graph nodes exist, chunks marked consolidated
    nodes = built.graph.list_nodes(NodeFilter(profile_id=PROFILE), Page(limit=100)).items
    assert nodes, "dream wrote no graph nodes"
    assert any(node.node_type.value in ("DECISION", "HABIT", "PREFERENCE") for node in nodes)
    chunks = built.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(limit=100)).items
    assert chunks, "no chunks captured"
    assert all(chunk.consolidated for chunk in chunks), "chunks not marked consolidated"

    # ---- recall after the dream: consolidated chunks exit the search surface
    # design/03 §4 (A2.5 baseline fix): mark-consolidated keeps the verbatim
    # chunk as evidence but removes it from recall. A query with no extractable
    # entities skips the entity gate, so the vector track (every chunk now
    # consolidated) and the graph track (no entity seeds) both return nothing:
    # an honest empty result, never stale verbatim chunks. The evidence stays
    # reachable through the provenance channel.
    memory = MemoryService(built, config)
    result = memory.recall(profile_id=PROFILE, query="pnpm", top_k=5)
    assert result["memory"]["entries"] == [], "consolidated chunks must exit the recall surface"
    assert result["memory"]["coverage"]["vector_hits"] == 0
    assert result["memory"]["coverage"]["graph_hits"] == 0
    assert result["memory"]["coverage"]["profile_chunks"] == len(chunks)
    audited = memory.audit(profile_id=PROFILE, chunk_id=chunks[0].chunk_id)
    assert audited["target"] == {"type": "chunk", "id": chunks[0].chunk_id}
    assert "asserted_by" in audited["provenance"]

    # ---- decay sweep over the same stores must not crash
    sweeper = DecaySweeper(built, config, clock=lambda: time.time() + 86400.0)
    stats = sweeper.run_once()
    assert stats, "decay sweep produced no per-profile stats"
    assert stats[0].chunks_scanned == len(chunks)

    # ---- the trigger is back to idle after the committed dream
    assert trigger.status(PROFILE).state.value == "idle"
