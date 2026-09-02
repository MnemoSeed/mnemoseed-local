"""Decay sweep loop (PRD-04 decay engine, design/01 stage ⑤).

One ``run_once`` pass sweeps every due profile's unreinforced graph nodes and
verbatim chunks through the FR-4.1 curve, writes batch weight updates through
the existing storage ports, persists a per-profile resume cursor in the meta
store (crash-safe idempotent re-sweep), and lands exactly ONE ``decay_sweep``
audit entry per profile sweep (actor=daemon; never per-node noise).
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from pathlib import Path

import pytest

from mnemoseed_local.config import Config, DecayConfig, load_config
from mnemoseed_local.decay.sweeper import DecaySweeper
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.factory import build_stores
from mnemoseed_local.storage.ports import AuditFilter, Page, StoredProfile, WeightUpdate
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_DAY = 86400.0
_PROFILE = "p1"
_CURSOR_KEY = "__decay__cursor"


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


# ---------------------------------------------------------------- fixtures


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


def _stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, object]:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config = load_config(_config_toml(tmp_path))
    stores = build_stores(config)
    return config, stores


def _embedder() -> SyntheticEmbedder:
    return SyntheticEmbedder(dimension=64)


def _seed_profile(stores: object, profile: str = _PROFILE) -> None:
    stores.meta.upsert_profile(StoredProfile(profile_id=profile))


def _pref_node(
    node_id: str,
    *,
    profile: str = _PROFILE,
    last_reinforced: float,
    confidence: float = 1.0,
    decay_weight: float = 1.0,
    node_type: NodeType = NodeType.PREFERENCE,
    never_decay: bool = False,
) -> GraphNode:
    props: dict[str, object] = {
        "domain": "coding",
        "statement": "dark mode",
        "valence": 0.8,
        "prior_width": 0.3,
        "trait_anchor": "anima-1",
        "evidence_chain": [],
    }
    if node_type is not NodeType.PREFERENCE:
        props = {"summary": "seed episode", "session_ref": "s1"}
    return GraphNode(
        node_id=node_id,
        profile_id=profile,
        node_type=node_type,
        entities=["ui"],
        props=props,
        confidence=confidence,
        decay_weight=decay_weight,
        never_decay=never_decay,
        last_reinforced=last_reinforced,
        provenance=Provenance(asserted_by="user", source="manual", confidence=confidence),
    )


def _chunk(
    chunk_id: str,
    *,
    profile: str = _PROFILE,
    ingested_at: float,
    last_reinforced: float | None = None,
    confidence: float = 1.0,
    decay_weight: float = 1.0,
    consolidated: bool = False,
    source: str = "manual",
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile,
        text="seed text",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=["ui"]),
        provenance=Provenance(asserted_by="user", source=source, confidence=confidence),
        decay_weight=decay_weight,
        ingested_at=ingested_at,
        last_reinforced=last_reinforced,
        consolidated=consolidated,
    )


def _seed_chunk(stores: object, stamp: ChunkStamp) -> None:
    embed = _embedder()
    vector = embed.embed(stamp.text)
    stores.vector.upsert_chunk(stamp, vector.dense, vector.sparse)


def _seed_chunks_batch(stores: object, stamps: list[ChunkStamp]) -> None:
    """Seed many chunks in ONE merge commit (the capture-drain batch path)."""
    embed = _embedder()
    entries = []
    for stamp in stamps:
        vector = embed.embed(stamp.text)
        entries.append((stamp, vector.dense, vector.sparse))
    stores.vector.upsert_chunks(entries)


def _audit_entries(stores: object, action: str) -> list[object]:
    return stores.meta.audit_query(AuditFilter(action=action), Page(limit=100)).items


async def _wait_for_audit(stores: object, action: str, budget: float = 0.5) -> None:
    """Block (wall-clock polling) until the ``action`` audit is visible on the
    main thread's connection.

    The sweep and its WAL commit run on the decay worker thread; a fresh
    main-thread connection sees a committed write only after the bounded
    cross-thread propagation window. The audit is the last write of a pass, so
    once it is visible every earlier write of the same pass is committed too.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while True:
        if _audit_entries(stores, action):
            return
        if loop.time() >= deadline:
            raise AssertionError(f"{action} audit not visible within {budget:.0f}s")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- sweep behavior


def test_sweep_decays_old_nodes_and_keeps_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-1 engine unit: a 60-day-unreinforced preference sinks through the
    curve (λ=0.005 → exp(-0.3)); a just-reinforced one stays untouched."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("old", last_reinforced=now - 60 * _DAY))
    stores.graph.upsert_node(_pref_node("fresh", last_reinforced=now))

    stats = sweeper.run_once()

    assert len(stats) == 1
    assert stats[0].nodes_scanned == 2
    assert stats[0].nodes_updated == 1
    assert stats[0].max_drop == pytest.approx(1.0 - math.exp(-0.3), abs=1e-6)
    assert stores.graph.get_node("old").decay_weight == pytest.approx(math.exp(-0.3), abs=1e-6)
    assert stores.graph.get_node("fresh").decay_weight == pytest.approx(1.0, abs=1e-9)


def test_sweep_decays_old_chunks_and_keeps_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The verbatim channel decays too (design/01 stage ⑤ applies to Hot): a
    60-day-old chunk sinks via the chunk λ (0.03 → exp(-1.8)); a fresh one
    stays at 1.0."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    _seed_chunk(stores, _chunk("old-c", ingested_at=now - 60 * _DAY))
    _seed_chunk(stores, _chunk("fresh-c", ingested_at=now))

    stats = sweeper.run_once()

    assert stats[0].chunks_scanned == 2
    assert stats[0].chunks_updated == 1
    assert stores.vector.get_chunk("old-c").decay_weight == pytest.approx(math.exp(-1.8), abs=1e-6)
    assert stores.vector.get_chunk("fresh-c").decay_weight == pytest.approx(1.0, abs=1e-9)


def test_sweep_uses_last_reinforced_baseline_over_ingested_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reinforcement is an EVENT: the sweep's baseline is last_reinforced, not
    the original ingestion time. A chunk ingested 100 days ago but reinforced
    30 days ago decays as 30 days old, not 100 (FR-4.2 ordering)."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    _seed_chunk(
        stores,
        _chunk("r", ingested_at=now - 100 * _DAY, last_reinforced=now - 30 * _DAY),
    )
    sweeper.run_once()
    expected = math.exp(-0.03 * 30.0)
    assert stores.vector.get_chunk("r").decay_weight == pytest.approx(expected, abs=1e-6)


def test_sweep_consolidated_chunks_decay_three_times_faster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """design/03 §4: a consolidated chunk (post-dream merge marker) decays at
    3x the chunk λ — the evidence scene's value diminishes once the gist is in
    the graph; an unconsolidated sibling follows the base chunk rate."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    _seed_chunk(stores, _chunk("plain", ingested_at=now - 60 * _DAY))
    _seed_chunk(stores, _chunk("merged", ingested_at=now - 60 * _DAY, consolidated=True))

    stats = sweeper.run_once()

    assert stores.vector.get_chunk("plain").decay_weight == pytest.approx(math.exp(-0.03 * 60.0), abs=1e-6)
    assert stores.vector.get_chunk("merged").decay_weight == pytest.approx(
        math.exp(-0.03 * 3.0 * 60.0), abs=1e-6
    )
    assert stats[0].chunks_scanned == 2
    assert stats[0].chunks_updated == 2


def test_sweep_never_decays_pinned_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-4.4 never-decay whitelist: a pinned node (user pin flips never_decay)
    keeps its weight even when unreinforced for years."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("pin", last_reinforced=now - 400 * _DAY, never_decay=True))
    stores.graph.upsert_node(_pref_node("plain", last_reinforced=now - 400 * _DAY))

    stats = sweeper.run_once()

    assert stores.graph.get_node("pin").decay_weight == pytest.approx(1.0, abs=1e-9)
    assert stores.graph.get_node("plain").decay_weight < 1.0
    assert stats[0].nodes_scanned == 2
    assert stats[0].nodes_updated == 1


def test_sweep_pin_chunks_decay_at_the_flashbulb_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """design/09 §3.1 (flashbulb class): a chunk whose provenance.source is the
    explicit-pin marker resolves its λ from the "pin" tier — preference pace
    (0.005, ~139-day half-life) instead of the verbatim-chunk rate; an
    ordinary sibling keeps fading at the chunk rate."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    _seed_chunk(stores, _chunk("pin-c", ingested_at=now - 60 * _DAY, source="memory.remember"))
    _seed_chunk(stores, _chunk("plain-c", ingested_at=now - 60 * _DAY))

    sweeper.run_once()

    assert stores.vector.get_chunk("pin-c").decay_weight == pytest.approx(math.exp(-0.005 * 60.0), abs=1e-6)
    assert stores.vector.get_chunk("plain-c").decay_weight == pytest.approx(math.exp(-0.03 * 60.0), abs=1e-6)


def test_sweep_pin_lambda_honors_the_explicit_map_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin tier is a first-class λ key: an explicit ``lambda_per_type["pin"]``
    entry wins over the design default, exactly like the other tiers."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    config.decay = DecayConfig(lambda_per_type={"pin": 0.02})
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    _seed_chunk(stores, _chunk("pin-c", ingested_at=now - 60 * _DAY, source="memory.remember"))

    sweeper.run_once()

    assert stores.vector.get_chunk("pin-c").decay_weight == pytest.approx(math.exp(-0.02 * 60.0), abs=1e-6)


def test_sweep_excludes_tombstoned_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleted/tombstoned nodes are invisible to the current-revision read and
    are never touched by the sweep."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("zombie", last_reinforced=now - 60 * _DAY))
    stores.graph.tombstone("zombie", deleted_at=now - 10 * _DAY)

    stats = sweeper.run_once()

    assert stats[0].nodes_scanned == 0
    assert stats[0].nodes_updated == 0
    assert len(_audit_entries(stores, "decay_sweep")) == 1


def test_sweep_min_apply_delta_skips_dumb_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """min_apply_delta: sub-threshold drops never reach the write port."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    config.decay = DecayConfig(min_apply_delta=0.1, lambda_per_type={"PREFERENCE": 0.01})
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    # 5 days at λ=0.01 -> drop ≈ 0.049 (below the 0.1 floor): skipped
    stores.graph.upsert_node(_pref_node("small", last_reinforced=now - 5 * _DAY))
    # 30 days at λ=0.01 -> drop ≈ 0.259 (above the floor): written
    stores.graph.upsert_node(_pref_node("big", last_reinforced=now - 30 * _DAY))

    stats = sweeper.run_once()

    assert stats[0].nodes_updated == 1
    assert stores.graph.get_node("small").decay_weight == pytest.approx(1.0, abs=1e-9)
    assert stores.graph.get_node("big").decay_weight == pytest.approx(math.exp(-0.3), abs=1e-6)


def test_sweep_is_monotonic_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sweep is a TREND: it may only lower weights. A decayed node whose
    recomputed target lies above its current weight stays where it is (only a
    reinforcement EVENT raises)."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("low", last_reinforced=now - 60 * _DAY, decay_weight=0.3))
    sweeper.run_once()
    assert stores.graph.get_node("low").decay_weight == pytest.approx(0.3, abs=1e-9)


def test_sweep_confidence_ceiling_binds_the_curve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-4.1 base_confidence: the curve is confidence-scaled and the sweep
    never lifts a weight above the recomputed ceiling."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    # confidence 0.7, 60 days, preference λ=0.005 -> 0.7 × exp(-0.3)
    stores.graph.upsert_node(_pref_node("c70", last_reinforced=now - 60 * _DAY, confidence=0.7))
    sweeper.run_once()
    expected = 0.7 * math.exp(-0.3)
    assert stores.graph.get_node("c70").decay_weight == pytest.approx(expected, abs=1e-6)


def test_sweep_profile_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D5 isolation: each profile's sweep pass only touches its own data and
    produces its own audit entry."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores, _PROFILE)
    _seed_profile(stores, "p2")
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("a1", last_reinforced=now - 60 * _DAY))
    stores.graph.upsert_node(_pref_node("b1", profile="p2", last_reinforced=now - 60 * _DAY))

    stats = sweeper.run_once()

    assert {s.profile_id for s in stats} == {_PROFILE, "p2"}
    for stat in stats:
        assert stat.nodes_scanned == 1
        assert stat.nodes_updated == 1
    entries = _audit_entries(stores, "decay_sweep")
    assert len(entries) == 2
    assert {e.detail["profile_id"] for e in entries} == {_PROFILE, "p2"}
    assert stores.graph.get_node("a1").decay_weight == pytest.approx(math.exp(-0.3), abs=1e-6)
    assert stores.graph.get_node("b1").decay_weight == pytest.approx(math.exp(-0.3), abs=1e-6)


# ---------------------------------------------------------------- cursor / resume


def test_sweep_cursor_resumes_and_skips_freshly_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cursor persists last-swept timestamps in meta; a profile inside its
    interval is skipped, and it becomes due again once the interval elapses."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("old", last_reinforced=now - 60 * _DAY))

    first = sweeper.run_once()
    assert len(first) == 1

    again = sweeper.run_once()
    assert again == []  # still inside the interval: nothing due

    cursor = stores.meta.get_config(_CURSOR_KEY)
    assert cursor is not None
    assert cursor.value["profiles"][_PROFILE] == pytest.approx(now)

    clock[0] = now + 2 * _DAY
    due = sweeper.run_once()
    assert len(due) == 1
    assert due[0].nodes_scanned == 1
    assert len(_audit_entries(stores, "decay_sweep")) == 2


def test_sweep_crash_before_cursor_write_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash-safe resume: a crash after the weight writes but before the cursor
    update re-sweeps the same profile; the recomputation is deterministic, so
    the second pass applies nothing (delta floor) and never double-writes."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("old", last_reinforced=now - 60 * _DAY))
    sweeper.run_once()
    expected = math.exp(-0.3)

    # simulate the crash window: weights persisted, cursor lost
    stores.meta.set_config(_CURSOR_KEY, {"profiles": {}})
    clock[0] = now + 2 * _DAY
    stats = sweeper.run_once()

    assert stats[0].nodes_scanned == 1
    assert stats[0].nodes_updated == 0  # target recomputed to the same value
    assert stores.graph.get_node("old").decay_weight == pytest.approx(expected, abs=1e-6)


def test_sweep_without_profiles_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No profiles -> no pass, no audit noise."""
    config, stores = _stack(tmp_path, monkeypatch)
    sweeper = DecaySweeper(stores, config, clock=lambda: 1_800_000_000.0)
    assert sweeper.run_once() == []
    assert _audit_entries(stores, "decay_sweep") == []


def test_sweep_disabled_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """decay.enabled=false (hot-applied or at boot): run_once performs nothing."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    config.decay = DecayConfig(enabled=False)
    sweeper = DecaySweeper(stores, config, clock=lambda: 1_800_000_000.0)
    assert sweeper.run_once() == []
    assert _audit_entries(stores, "decay_sweep") == []


# ---------------------------------------------------------------- hot-apply (F2)


def test_hot_applied_lambda_affects_the_next_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A λ change through the config writer reaches the NEXT sweep without a
    restart: the sweeper resolves λ from the live config at sweep time."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("old", last_reinforced=now - 60 * _DAY))

    sweeper.run_once()
    assert stores.graph.get_node("old").decay_weight == pytest.approx(math.exp(-0.005 * 60.0), abs=1e-6)

    # the config writer's apply step replaces the live DecayConfig (same object
    # the sweeper holds), then the clock advances past the interval
    config.decay = DecayConfig(lambda_per_type={"PREFERENCE": 0.05})
    clock[0] = now + 2 * _DAY
    sweeper.run_once()

    assert stores.graph.get_node("old").decay_weight == pytest.approx(math.exp(-0.05 * 62.0), abs=1e-6)


# ---------------------------------------------------------------- observability


def test_sweep_audits_one_entry_per_profile_with_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observability: per-sweep stats (scanned/updated/max-drop) land in ONE
    audit entry per profile sweep, actor=daemon — never per-node noise."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("n1", last_reinforced=now - 60 * _DAY))
    stores.graph.upsert_node(_pref_node("n2", last_reinforced=now - 60 * _DAY))
    _seed_chunk(stores, _chunk("c1", ingested_at=now - 60 * _DAY))

    stats = sweeper.run_once()

    assert stats[0].nodes_scanned == 2
    assert stats[0].nodes_updated == 2
    assert stats[0].chunks_scanned == 1
    assert stats[0].chunks_updated == 1
    entries = _audit_entries(stores, "decay_sweep")
    assert len(entries) == 1  # one entry, not per-node
    entry = entries[0]
    assert entry.actor == "daemon"
    assert entry.detail["profile_id"] == _PROFILE
    assert entry.detail["nodes_scanned"] == 2
    assert entry.detail["nodes_updated"] == 2
    assert entry.detail["chunks_scanned"] == 1
    assert entry.detail["chunks_updated"] == 1
    assert entry.detail["max_drop"] > 0
    assert sweeper.last_stats() == stats


# ---------------------------------------------------------------- daemon loop


@pytest.mark.asyncio
async def test_run_forever_ticks_and_stops_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon-owned loop ticks once immediately, then sleeps; cancellation
    stops it cleanly with no pending task left behind.

    The sweep and its WAL commit run on the decay worker thread, so the audit
    is only visible to this thread's connection after the bounded cross-thread
    propagation window. We poll for the audit (the last write of the pass)
    instead of asserting synchronously — once it is visible the weight write,
    which precedes it, is guaranteed committed too.
    """
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    config.decay = DecayConfig(sweep_interval_s=0.01)
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    stores.graph.upsert_node(_pref_node("old", last_reinforced=now - 60 * _DAY))

    task = asyncio.create_task(sweeper.run_forever())
    await asyncio.sleep(0.02)
    await _wait_for_audit(stores, "decay_sweep")  # proves a full tick completed
    assert stores.graph.get_node("old").decay_weight < 1.0  # ...and it decayed

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.0)
    assert task.cancelled() is True


# ---------------------------------------------------------------- concurrency (P-003)


def test_sweep_weight_updates_complete_under_concurrent_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-003 regression: a sweep must complete while a concurrent writer
    (the boot dream/scheduler worker) commits to the same vector table.

    The naive per-row ``table.update`` in ``update_weights`` issued one native
    commit per chunk, serializing at ~100ms/chunk on Windows and deadlocking
    LanceDB's background loop once a writer thread interleaved commits. The
    batched single-commit fix must finish well inside the budget with a live
    writer driving the same table. A hung sweep surfaced here as a timeout in
    a dedicated worker thread (never a pytest hang): the deadline fires and
    the test fails instead of blocking the suite.
    """
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    # A few hundred unreinforced chunks exceed one read page and force the
    # write batch through the shared weight port (the >2000-row live case).
    _seed_chunks_batch(stores, [_chunk(f"c{i:04d}", ingested_at=now - 90 * _DAY) for i in range(240)])

    stop = threading.Event()

    def writer_loop() -> None:
        idx = 0
        while not stop.is_set():
            _seed_chunks_batch(stores, [_chunk(f"w{idx % 20}", ingested_at=now)])
            idx += 1

    writer = threading.Thread(target=writer_loop, daemon=True)
    writer.start()
    done = threading.Event()
    result: list[object] = []

    def sweep() -> None:
        try:
            result.append(sweeper.run_once())
        finally:
            done.set()

    sweeper_thread = threading.Thread(target=sweep, daemon=True)
    sweeper_thread.start()
    budget = 8.0  # fixed path ~0.2s; the per-row regression burned ~100ms/chunk (240 -> >20s)
    t0 = time.time()
    finished = done.wait(budget)
    elapsed = time.time() - t0
    stop.set()
    writer.join(timeout=0.0)

    assert finished, f"sweep did not complete within {budget}s under a concurrent writer (P-003); "
    f"took {elapsed:.1f}s"
    assert elapsed < budget
    assert result[0][0].chunks_updated == 240
    assert stores.vector.get_chunk("c0000").decay_weight == pytest.approx(math.exp(-0.03 * 90.0), abs=1e-6)


# ---------------------------------------------------------------- mutation hardening (BLOCKER-2)


def test_update_weights_empty_and_all_none_are_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty or all-None WeightUpdate batches must be a no-op (no commit, no version bump)."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    _seed_chunk(stores, _chunk("c1", ingested_at=now - 60 * _DAY))
    version_before = stores.vector._table.version  # type: ignore[attr-defined]
    chunk_before = stores.vector.get_chunk("c1")
    assert chunk_before is not None
    stores.vector.update_weights([])
    stores.vector.update_weights([WeightUpdate(chunk_id="missing")])
    stores.vector.update_weights(
        [WeightUpdate(chunk_id="c1", decay_weight=None, last_reinforced=None, reinforce_count=None)]
    )
    assert stores.vector._table.version == version_before  # type: ignore[attr-defined]
    chunk_after = stores.vector.get_chunk("c1")
    assert chunk_after is not None
    assert chunk_after.decay_weight == pytest.approx(chunk_before.decay_weight)


def test_update_weights_duplicate_chunk_id_last_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate chunk_ids in one batch resolve to the last entry (parity with sequential updates)."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    _seed_chunk(stores, _chunk("dup", ingested_at=now))
    stores.vector.update_weights(
        [
            WeightUpdate(chunk_id="dup", decay_weight=0.2),
            WeightUpdate(chunk_id="dup", decay_weight=0.8),
        ]
    )
    assert stores.vector.get_chunk("dup").decay_weight == pytest.approx(0.8)  # type: ignore[union-attr]
    stores.vector.update_weights(
        [
            WeightUpdate(chunk_id="dup", decay_weight=0.9),
            WeightUpdate(chunk_id="dup", decay_weight=0.1),
        ]
    )
    assert stores.vector.get_chunk("dup").decay_weight == pytest.approx(0.1)  # type: ignore[union-attr]


def test_update_weights_preserves_untouched_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A decay-only batch must not null last_reinforced or reinforce_count."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    _seed_chunk(stores, _chunk("c1", ingested_at=now))
    stores.vector.update_weights(
        [WeightUpdate(chunk_id="c1", decay_weight=0.5, last_reinforced=12345.0, reinforce_count=7)]
    )
    baseline = stores.vector.get_chunk("c1")
    assert baseline is not None
    assert baseline.last_reinforced == pytest.approx(12345.0)
    assert (
        stores.vector._table.search()
        .where("chunk_id = 'c1'")
        .limit(1)
        .to_list()[0][  # type: ignore[attr-defined]
            "reinforce_count"
        ]
        == 7
    )
    stores.vector.update_weights([WeightUpdate(chunk_id="c1", decay_weight=0.1)])
    after = stores.vector.get_chunk("c1")
    assert after is not None
    assert after.decay_weight == pytest.approx(0.1)
    assert after.last_reinforced == pytest.approx(12345.0)
    row = stores.vector._table.search().where("chunk_id = 'c1'").limit(1).to_list()[0]  # type: ignore[attr-defined]
    assert row["reinforce_count"] == 7


def test_update_weights_mixed_signatures_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mixed-signature batches must split into uniform merge tables so no column is nulled."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    _seed_chunks_batch(
        stores,
        [_chunk("c1", ingested_at=now), _chunk("c2", ingested_at=now), _chunk("c3", ingested_at=now)],
    )
    stores.vector.update_weights(
        [
            WeightUpdate(chunk_id="c1", decay_weight=0.11),
            WeightUpdate(chunk_id="c2", last_reinforced=9999.0),
            WeightUpdate(chunk_id="c3", decay_weight=0.22, last_reinforced=8888.0, reinforce_count=7),
        ]
    )
    c1 = stores.vector.get_chunk("c1")
    c2 = stores.vector.get_chunk("c2")
    c3 = stores.vector.get_chunk("c3")
    assert c1 is not None and c1.decay_weight == pytest.approx(0.11)
    assert c2 is not None and c2.last_reinforced == pytest.approx(9999.0)
    assert c2.decay_weight == pytest.approx(1.0)
    assert c3 is not None and c3.decay_weight == pytest.approx(0.22)
    assert c3.last_reinforced == pytest.approx(8888.0)
    row3 = stores.vector._table.search().where("chunk_id = 'c3'").limit(1).to_list()[0]  # type: ignore[attr-defined]
    assert row3["reinforce_count"] == 7
    row1 = stores.vector._table.search().where("chunk_id = 'c1'").limit(1).to_list()[0]  # type: ignore[attr-defined]
    assert row1["reinforce_count"] == 0
    assert c1.last_reinforced is not None


@pytest.mark.asyncio
async def test_sweep_does_not_block_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Decay writes must not block the event loop (P-003 class elimination).

    Heartbeats are collected from t=0 (before the sweep task even runs) and the
    sweep interval is shrunk so the blocking write recurs inside the measured
    window. Either alone catches the round-1 BLOCKER-1 regression (inline
    ``run_once`` on the loop wedges the heartbeat cadence); together they make
    the oracle fail-fast on that exact revert.
    """
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    now = 1_800_000_000.0
    clock = [now]
    _seed_chunks_batch(stores, [_chunk(f"c{i:04d}", ingested_at=now - 90 * _DAY) for i in range(5)])
    config.decay = DecayConfig(sweep_interval_s=0.02)
    sweeper = DecaySweeper(stores, config, clock=lambda: clock[0])
    original_update = stores.vector.update_weights

    def blocking_update(updates):  # type: ignore[no-untyped-def]
        time.sleep(0.35)
        return original_update(updates)

    monkeypatch.setattr(stores.vector, "update_weights", blocking_update)
    loop = asyncio.get_running_loop()
    start = loop.time()
    task = asyncio.create_task(sweeper.run_forever())
    beats: list[float] = []
    while loop.time() - start < 0.8:
        beats.append(loop.time())
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sweeper.close(timeout=0)
    gaps = [beats[i + 1] - beats[i] for i in range(len(beats) - 1)]
    assert beats, "heartbeat never ticked"
    assert max(gaps) < 0.12, f"event loop blocked: max gap {max(gaps):.3f}s (beats={beats[:5]})"
