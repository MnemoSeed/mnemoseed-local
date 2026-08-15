"""Reinforcement event side (PRD-04 FR-4.2, design/01 stage ⑤).

The counterpart of the decay sweep: where the sweep is a TREND (only lowers
weights from the ``last_reinforced`` baseline), a retrieval usage event is an
EVENT — it refreshes ``last_reinforced`` and rebounds ``decay_weight`` toward
the 1.0 ceiling, bounded. Behavioral contract over the real embedded drivers:

- a retrieval hit refreshes last_reinforced and applies the rebound (≤ 1.0);
- a hit on an item below the candidate floor never rebounds (sunk memories are
  only resurrected by the explicit revival path, FR-4.3);
- usage counters still increment alongside the reinforcement;
- a fresh hit makes the item decay-neutral for the next sweep interval (the
  ordering contract: the sweep following a hit leaves the weight unchanged).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from mnemoseed_local.config import Config, load_config
from mnemoseed_local.decay import Reinforcer
from mnemoseed_local.decay.sweeper import DecaySweeper
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
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

_DAY = 86400.0
_PROFILE = "p1"
_CURSOR_KEY = "__decay__cursor"
_NOW = 1_800_000_000.0


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


def _seed_profile(stores: object) -> None:
    stores.meta.upsert_profile(StoredProfile(profile_id=_PROFILE))


def _chunk(
    chunk_id: str,
    *,
    ingested_at: float,
    last_reinforced: float | None = None,
    decay_weight: float = 1.0,
    consolidated: bool = False,
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text="seed text",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=["ui"]),
        provenance=Provenance(asserted_by="user", source="manual", confidence=1.0),
        decay_weight=decay_weight,
        ingested_at=ingested_at,
        last_reinforced=last_reinforced,
        consolidated=consolidated,
    )


def _seed_chunk(stores: object, stamp: ChunkStamp) -> None:
    embedder = SyntheticEmbedder(dimension=64)
    vector = embedder.embed(stamp.text)
    stores.vector.upsert_chunk(stamp, vector.dense, vector.sparse)


def _pref_node(
    node_id: str,
    *,
    last_reinforced: float,
    decay_weight: float = 1.0,
) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=_PROFILE,
        node_type=NodeType.PREFERENCE,
        entities=["ui"],
        props={
            "domain": "coding",
            "statement": "dark mode",
            "valence": 0.8,
            "prior_width": 0.3,
            "trait_anchor": "anima-1",
            "evidence_chain": [],
        },
        confidence=1.0,
        decay_weight=decay_weight,
        last_reinforced=last_reinforced,
        provenance=Provenance(asserted_by="user", source="manual", confidence=1.0),
    )


def _raw_chunk(stores: object, chunk_id: str) -> dict[str, object]:
    """Raw lancedb row: usage counters are hidden on the ChunkStamp read path."""
    from mnemoseed_local.storage.drivers.lancedb_embedded import _escape

    rows = stores.vector._table.search().where(f"chunk_id = {_escape(chunk_id)}").limit(1).to_list()
    return rows[0] if rows else {}


def _reinforcer(stores: object) -> Reinforcer:
    return Reinforcer(stores, clock=lambda: _NOW)


# ---------------------------------------------------------------- chunk hits


def test_chunk_hit_refreshes_last_reinforced_and_rebounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-4.2 event: one chunk hit refreshes last_reinforced and rebounds the
    weight by the reinforcement bonus (bounded at 1.0), hit_count included."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    _seed_chunk(
        stores,
        _chunk("c1", ingested_at=_NOW - 10 * _DAY, last_reinforced=_NOW - 10 * _DAY, decay_weight=0.7),
    )

    _reinforcer(stores).record_hits(["c1"], [])

    stored = stores.vector.get_chunk("c1")
    assert stored.last_reinforced == pytest.approx(_NOW)
    assert stored.decay_weight == pytest.approx(0.8)
    raw = _raw_chunk(stores, "c1")
    assert raw["hit_count"] == 1
    assert raw.get("last_hit_at") is not None


def test_chunk_hit_rebound_is_bounded_at_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rebound never exceeds 1.0 even when the bonus would overshoot."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    _seed_chunk(stores, _chunk("c2", ingested_at=_NOW, last_reinforced=_NOW, decay_weight=0.95))

    _reinforcer(stores).record_hits(["c2"], [])

    assert stores.vector.get_chunk("c2").decay_weight == pytest.approx(1.0)


def test_below_floor_chunk_hit_counts_but_never_rebounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hit on a sunk chunk (decay_weight below the 0.4 floor) counts the usage
    but does not rebound and does not refresh last_reinforced (FR-4.3: sunk
    memories are only resurrected by the explicit revival path)."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    _seed_chunk(
        stores,
        _chunk(
            "c3",
            ingested_at=_NOW - 10 * _DAY,
            last_reinforced=_NOW - 10 * _DAY,
            decay_weight=0.2,
        ),
    )

    _reinforcer(stores).record_hits(["c3"], [])

    stored = stores.vector.get_chunk("c3")
    assert stored.decay_weight == pytest.approx(0.2)
    assert stored.last_reinforced == pytest.approx(_NOW - 10 * _DAY)
    assert _raw_chunk(stores, "c3")["hit_count"] == 1


# ---------------------------------------------------------------- node hits


def test_node_hit_refreshes_last_reinforced_and_rebounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-4.2 event over the graph track: a node hit refreshes last_reinforced,
    rebounds the weight, and bumps the node's own usage counters."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    stores.graph.upsert_node(_pref_node("n1", last_reinforced=_NOW - 10 * _DAY, decay_weight=0.7))

    _reinforcer(stores).record_hits([], ["n1"])

    node = stores.graph.get_node("n1")
    assert node is not None
    assert node.last_reinforced == pytest.approx(_NOW)
    assert node.decay_weight == pytest.approx(0.8)
    assert node.hit_count == 1
    assert node.last_hit_at == pytest.approx(_NOW)


def test_below_floor_node_hit_counts_but_never_rebounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same sunk-memory guard on the graph side."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    stores.graph.upsert_node(_pref_node("n2", last_reinforced=_NOW - 10 * _DAY, decay_weight=0.2))

    _reinforcer(stores).record_hits([], ["n2"])

    node = stores.graph.get_node("n2")
    assert node is not None
    assert node.decay_weight == pytest.approx(0.2)
    assert node.last_reinforced == pytest.approx(_NOW - 10 * _DAY)
    assert node.hit_count == 1


def test_unknown_hit_ids_are_ignored_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire-and-forget semantics: ids that vanished (concurrent purge) are a
    silent no-op, never an error."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    _reinforcer(stores).record_hits(["never-written"], ["never-written-node"])


# ---------------------------------------------------- ordering: sweep follows a hit


def test_sweep_after_chunk_hit_leaves_weight_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering contract: a fresh hit makes the chunk decay-neutral for the next
    sweep interval — the sweep following the hit leaves the weight exactly at
    the post-hit value (last_reinforced refreshed, so the trend skips it)."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    sweeper = DecaySweeper(stores, config, clock=lambda: _NOW)
    _seed_chunk(stores, _chunk("c6", ingested_at=_NOW - 100 * _DAY, last_reinforced=_NOW - 10 * _DAY))

    sweeper.run_once()
    decayed = stores.vector.get_chunk("c6").decay_weight
    assert decayed == pytest.approx(math.exp(-0.03 * 10.0), abs=1e-6)

    _reinforcer(stores).record_hits(["c6"], [])
    after_hit = stores.vector.get_chunk("c6").decay_weight
    assert after_hit == pytest.approx(min(1.0, decayed + 0.1))

    # crash-window pattern: the cursor is cleared so the next pass re-scans
    stores.meta.set_config(_CURSOR_KEY, {"profiles": {}})
    sweeper.run_once()

    assert stores.vector.get_chunk("c6").decay_weight == pytest.approx(after_hit)
    assert stores.vector.get_chunk("c6").last_reinforced == pytest.approx(_NOW)


def test_sweep_after_node_hit_leaves_weight_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering contract over the graph side: the sweep after a node hit leaves
    the rebounded weight untouched."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    sweeper = DecaySweeper(stores, config, clock=lambda: _NOW)
    stores.graph.upsert_node(_pref_node("n6", last_reinforced=_NOW - 60 * _DAY))

    sweeper.run_once()
    decayed = stores.graph.get_node("n6").decay_weight
    assert decayed == pytest.approx(math.exp(-0.005 * 60.0), abs=1e-6)

    _reinforcer(stores).record_hits([], ["n6"])
    after_hit = stores.graph.get_node("n6").decay_weight
    assert after_hit == pytest.approx(min(1.0, decayed + 0.1))

    stores.meta.set_config(_CURSOR_KEY, {"profiles": {}})
    sweeper.run_once()

    node = stores.graph.get_node("n6")
    assert node.decay_weight == pytest.approx(after_hit)
    assert node.last_reinforced == pytest.approx(_NOW)
