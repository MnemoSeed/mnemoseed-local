"""The embedded preset fully resolves: vector / graph / meta / embed layers map
to the M0 drivers and the resolved stack passes the appendix C capability gate
with no hard findings and no degradations.
"""

import asyncio

import pytest

from mnemoseed_local.config import Config, LayerSpec
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.factory import build_stores
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_DRIVERS = (
    (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
    (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
    (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
    (EMBED_DRIVERS, bge_m3_onnx.BgeM3OnnxEmbedder),
    (EMBED_DRIVERS, synthetic_embedder.SyntheticEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_registered():
    for registry, cls in _DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


def _embedded_config(tmp_path):
    return Config(
        preset="embedded",
        storage={
            "vector": LayerSpec(
                layer="vector",
                params={"uri": str(tmp_path / "chunks.lance"), "dimensions": 64},
            ),
            "graph": LayerSpec(layer="graph", params={"path": str(tmp_path / "cortex.db")}),
            "meta": LayerSpec(layer="meta", params={"path": str(tmp_path / "meta.db")}),
            "embed": LayerSpec(layer="embed", params={"model_dir": str(tmp_path / "models")}),
        },
    )


def test_preset_names_map_to_m0_drivers():
    config = Config(preset="embedded")
    assert config.layer_instances("vector")["main"].driver == "lancedb_embedded"
    assert config.layer_instances("graph")["main"].driver == "sqlite_graph"
    assert config.layer_instances("meta")["main"].driver == "sqlite_meta"
    assert config.layer_instances("embed")["main"].driver == "bge_m3_onnx"


def test_preset_embedded_builds_and_passes_gate(tmp_path):
    stores = build_stores(_embedded_config(tmp_path))
    assert stores.report.ok
    assert stores.report.hard_missing == []
    assert stores.report.degradations == []
    assert stores.vector.info.name == "lancedb_embedded"
    assert stores.graph.info.name == "sqlite_graph"
    assert stores.meta.info.name == "sqlite_meta"
    assert stores.embed.info.name == "bge_m3_onnx"
    # construction never downloads the model — inference loads it lazily
    asyncio.run(stores.close())


def test_preset_embedded_vector_writes_and_reads(tmp_path):
    stores = build_stores(_embedded_config(tmp_path))
    synthetic = synthetic_embedder.SyntheticEmbedder(dimension=64)
    stamp = ChunkStamp(
        chunk_id="p1c1",
        profile_id="alice",
        text="embedded preset round trip",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test",
        cues=Cues(),
        provenance=Provenance(asserted_by="test", source="integration"),
    )
    result = synthetic.embed(stamp.text)
    stores.vector.upsert_chunk(stamp, result.dense, result.sparse)
    got = stores.vector.get_chunk("p1c1")
    assert got is not None and got.text == "embedded preset round trip"
    try:
        asyncio.run(stores.close())
    finally:
        pass
