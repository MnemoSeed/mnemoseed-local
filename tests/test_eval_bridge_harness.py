"""PRD-448 W1 bridge harness: rig mechanics under the synthetic embedder —
seeding through the real ingest funnel, the stored-cue oracle derivation,
per-candidate similarity measurement consistency, wire recall shape, model
staging contract and isolation pins (brief §4.1/§6 W1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _ports import RESERVED_PORTS

from mnemoseed_local.eval.bridge_harness import BridgeRig, cue_entities, stage_model_cache
from mnemoseed_local.eval.bridge_materials import PROBE_ZH_NATURAL, bridge_corpus, bridge_probes
from mnemoseed_local.eval.rig_freshness import RigRootNotFresh
from mnemoseed_local.retrieve.cues import extract_cues
from mnemoseed_local.retrieve.hybrid import HybridConfig

# test_registry.py clears the driver registries wholesale; any daemon-booting
# module ordered after it must defensively re-register (test_recall_pending
# precedent).
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
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


def _synthetic_rig(root: Path) -> BridgeRig:
    return BridgeRig(root, embed_driver="synthetic", embed_dimension=64)


def test_rig_boots_fresh_and_seeds_the_full_corpus(tmp_path: Path) -> None:
    with _synthetic_rig(tmp_path / "rig") as rig:
        labels = rig.seed_corpus()
        assert set(labels) == {chunk.label for chunk in bridge_corpus()}
        chunks = rig.stored_chunks()
        assert len(chunks) == len(bridge_corpus())
        for chunk in bridge_corpus():
            stored = next(item for item in chunks if item.chunk_id == labels[chunk.label])
            assert list(stored.cues.entities) == list(extract_cues(chunk.text).cues.entities)
            assert f"user: {chunk.text}" == stored.text


def test_measure_probe_reports_consistent_per_candidate_similarities(tmp_path: Path) -> None:
    with _synthetic_rig(tmp_path / "rig") as rig:
        labels = rig.seed_corpus()
        measurement = rig.measure_probe(PROBE_ZH_NATURAL)
        assert {c.chunk_id for c in measurement.candidates} == set(labels.values())
        floor = HybridConfig().min_similarity
        for candidate in measurement.candidates:
            recovered = 2.0 * candidate.fused_similarity - candidate.dense_similarity
            assert abs(candidate.sparse_similarity - recovered) < 1e-6
            assert candidate.above_floor == (candidate.fused_similarity >= floor)


def test_wire_recall_serves_shape_and_budget_metrics(tmp_path: Path) -> None:
    with _synthetic_rig(tmp_path / "rig") as rig:
        rig.seed_corpus()
        memory = rig.recall(PROBE_ZH_NATURAL)["memory"]
        for key in ("entries", "dropped_count", "budget_tokens", "tokens_used", "coverage"):
            assert key in memory
        assert memory["budget_tokens"] == 800
        coverage = memory["coverage"]
        for key in (
            "vector_hits",
            "graph_hits",
            "pool_size",
            "profile_chunks",
            "floor_dropped",
            "dedup_collapsed",
        ):
            assert key in coverage
        assert all(entry["kind"] == "chunk" for entry in memory["entries"])
        assert all("score" in entry and "id" in entry for entry in memory["entries"])


def test_rig_port_is_never_a_reserved_port(tmp_path: Path) -> None:
    rig = _synthetic_rig(tmp_path / "rig")
    port = int(rig.base_url.rsplit(":", 1)[1])
    assert port not in RESERVED_PORTS


def test_rig_refuses_a_non_fresh_root(tmp_path: Path) -> None:
    root = tmp_path / "rig"
    root.mkdir()
    (root / "config.toml").write_text('preset = "embedded"\n', encoding="utf-8")
    with pytest.raises(RigRootNotFresh):
        _synthetic_rig(root)


def test_probe_cue_output_matches_the_real_extractor() -> None:
    for probe in bridge_probes():
        assert list(cue_entities(probe.text)) == list(extract_cues(probe.text).cues.entities)


def test_model_staging_copies_artifacts_and_hashes(tmp_path: Path) -> None:
    source = tmp_path / "installed" / "models" / "bge-m3"
    source.mkdir(parents=True)
    (source / "model_quantized.onnx").write_bytes(b"model-bytes")
    (source / "model_quantized.onnx.complete").write_text("11", encoding="utf-8")
    (source / "tokenizer.json").write_bytes(b"tokenizer-bytes")
    (source / "sparse_linear.pt").write_bytes(b"sparse-bytes")
    target = tmp_path / "home" / "models" / "bge-m3"
    staging = stage_model_cache(target, source=source)
    assert staging.mode == "copied"
    staged = {entry.name: entry for entry in staging.files}
    assert set(staged) == {"model_quantized.onnx", "tokenizer.json", "sparse_linear.pt"}
    assert staged["model_quantized.onnx"].bytes == len(b"model-bytes")
    assert all(entry.sha256 for entry in staging.files)
    assert (target / "model_quantized.onnx").read_bytes() == b"model-bytes"
    assert (target / "model_quantized.onnx.complete").exists()


def test_model_staging_falls_back_to_download_when_no_cache(tmp_path: Path) -> None:
    staging = stage_model_cache(tmp_path / "home" / "models" / "bge-m3", source=tmp_path / "missing")
    assert staging.mode == "download"
    assert staging.files == ()
    assert staging.source is None
