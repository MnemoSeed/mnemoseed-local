"""T4a — recall harness: materials factory, the T2-pipeline rig (HTTP
/ingest -> /session/recall-pending -> hook pull -> /memory/reinforce), the
B4b isolation contract (run-id namespace + idempotent rig) and the
needle-mechanics end-to-end evidence.

Materials factory pins: 24 points (bilingual x 4 classes x 3 lengths),
noise coverage (entity-miss / entity-collision / needle-collision), the
decay layering (facts 1.0, aged noise below the floor sweep, needle
collision mid-band), temporal inversion (facts stored last), the
needle-collision shared-window invariant, deterministic regeneration and
the reply-template needle mechanics.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.eval.recall_harness import (
    RecallRig,
    build_needle_registry,
    cited_chunk_ids,
    needles_of,
    normalize_recall_text,
)
from mnemoseed_local.eval.recall_materials import (
    RecallNoiseKind,
    recall_materials,
)
from mnemoseed_local.eval.recall_metrics import score_recall
from mnemoseed_local.retrieve.cues import extract_cues

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


# ---------------------------------------------------------------- materials factory


def test_materials_factory_covers_the_full_24_point_grid() -> None:
    materials = recall_materials()
    assert len(materials) == 24
    assert len({m.point_id for m in materials}) == 24
    assert {m.language for m in materials} == {"en", "zh"}
    assert {m.fact_class for m in materials} == {"prefers", "has_habit", "decided", "believes"}
    assert {m.length_band for m in materials} == {"short", "medium", "long"}
    combos = {(m.language, m.fact_class, m.length_band) for m in materials}
    assert len(combos) == 24  # 2 x 4 x 3 full grid, one point per combo


def test_materials_length_bands_are_respected() -> None:
    for band, lo, hi in (("short", 32, 60), ("medium", 150, 320), ("long", 600, 1000)):
        for material in [m for m in recall_materials() if m.length_band == band]:
            length = len(normalize_recall_text(material.fact_text))
            assert lo <= length <= hi, (material.point_id, length)


def test_materials_deterministic_and_seed_sensitive() -> None:
    assert recall_materials(7) == recall_materials(7)
    assert recall_materials(7) != recall_materials(8)


def test_materials_each_point_carries_all_three_noise_kinds() -> None:
    expected = {
        RecallNoiseKind.ENTITY_MISS,
        RecallNoiseKind.ENTITY_COLLISION,
        RecallNoiseKind.NEEDLE_COLLISION,
    }
    for material in recall_materials():
        assert {noise.kind for noise in material.noise} == expected, material.point_id


def test_materials_entity_miss_noise_never_carries_the_entity() -> None:
    for material in recall_materials():
        miss = next(n for n in material.noise if n.kind is RecallNoiseKind.ENTITY_MISS)
        assert material.entity.casefold() not in miss.text.casefold(), material.point_id
        stored = {e.casefold() for e in extract_cues(miss.text).cues.entities}
        assert material.entity.casefold() not in stored, material.point_id


def test_materials_entity_is_extractable_from_cue_and_fact() -> None:
    for material in recall_materials():
        cue_entities = {e.casefold() for e in extract_cues(material.cue_turn).cues.entities}
        fact_entities = {e.casefold() for e in extract_cues(material.fact_text).cues.entities}
        assert material.entity.casefold() in cue_entities, material.point_id
        assert material.entity.casefold() in fact_entities, material.point_id


def test_materials_needle_collision_shares_the_head_needle() -> None:
    for material in recall_materials():
        collision = next(n for n in material.noise if n.kind is RecallNoiseKind.NEEDLE_COLLISION)
        fact_needles = needles_of(material.fact_text)
        collision_needles = needles_of(collision.text)
        assert fact_needles and collision_needles, material.point_id
        # the fact and its needle-collision chunk share the 24-char head window
        assert fact_needles[0] == collision_needles[0], material.point_id


def test_materials_reply_templates_needle_mechanics() -> None:
    for material in recall_materials():
        names = [template.name for template in material.reply_templates]
        assert set(names) == {"cite", "stray", "no_cite", "paraphrase"}
        assert names.count("cite") == 5  # citation mass dilutes the structural detector error
        support = material.support_text
        registry = build_needle_registry(
            [
                {"id": "fact", "text": material.fact_text},
                {"id": "fact_support", "text": support},
                {
                    "id": "collision",
                    "text": next(
                        n.text for n in material.noise if n.kind is RecallNoiseKind.NEEDLE_COLLISION
                    ),
                },
            ]
        )
        by_name = {t.name: t.text for t in material.reply_templates}
        cited = cited_chunk_ids(by_name["cite"], registry)
        assert {"fact", "fact_support"} <= set(cited), material.point_id
        stray = cited_chunk_ids(by_name["stray"], registry)
        assert "collision" in stray, material.point_id  # accidental other-chunk needle
        assert cited_chunk_ids(by_name["no_cite"], registry) == []
        assert cited_chunk_ids(by_name["paraphrase"], registry) == []
        # genuine citations always point at the two fact turns; the stray is the false one
        for template in material.reply_templates:
            assert set(template.references) <= {"fact", "fact_support"}, material.point_id
            if template.name == "no_cite":
                assert template.references == ()
            if template.name in ("cite", "stray", "paraphrase"):
                assert set(template.references) == {"fact", "fact_support"}, material.point_id


def test_materials_decay_layering_pins_the_floor_envelope() -> None:
    """Facts at full weight; aged interference below the whole floor sweep;
    the needle-collision pair mid-band so the floor axis gates it."""
    for material in recall_materials():
        labels = [label for label, _ in material.stored_turns]
        decays = dict(zip(labels, material.turn_decays, strict=True))
        assert decays["fact"] == 1.0, material.point_id
        assert decays["fact_support"] == 1.0, material.point_id
        assert decays["entity_miss"] < 0.4, material.point_id
        assert decays["entity_collision"] < 0.4, material.point_id
        assert decays["needle_collision"] == 0.45, material.point_id


def test_materials_temporal_inversion_stores_facts_last() -> None:
    """The age tie-break must not make serve order trivially predictable:
    interference turns are stored first, the fact turns come LAST (newest)."""
    for material in recall_materials():
        labels = [label for label, _ in material.stored_turns]
        assert labels[:3] == ["entity_miss", "entity_collision", "needle_collision"], material.point_id
        assert labels[3:] == ["fact", "fact_support"], material.point_id


def test_materials_support_turn_is_a_referenceable_fact() -> None:
    """The second fact turn carries the entity (focal-matchable) and is long
    enough to own a needle the citing replies can fire verbatim."""
    for material in recall_materials():
        assert material.entity.casefold() in material.support_text.casefold(), material.point_id
        stored_entities = {e.casefold() for e in extract_cues(material.support_text).cues.entities}
        assert material.entity.casefold() in stored_entities, material.point_id
        assert needles_of(material.support_text), material.point_id


# ---------------------------------------------------------------- the rig


def test_rig_boots_a_daemon_under_its_root(tmp_path: Path) -> None:
    with RecallRig(tmp_path / "rig") as rig:
        client = rig.client
        assert client.app.state.config.capture.auto_recall is True
        assert client.app.state.config.capture.auto_recall_focal_floor == 0.4
        assert client.app.state.config.capture.auto_recall_budget_chars == 1200
    # the daemon's whole world lives under the rig root
    assert list(tmp_path.iterdir()) == [tmp_path / "rig"]
    assert (tmp_path / "rig" / "config.toml").exists()


def test_rig_full_pipeline_serves_reinforces_and_reads_evidence(tmp_path: Path) -> None:
    material = next(m for m in recall_materials() if m.language == "en" and m.length_band == "short")
    with RecallRig(tmp_path / "rig") as rig:
        run = rig.run_material(material)
        # the cue anchored 3 focal candidates at floor 0.4: both facts + the
        # mid-band needle-collision chunk (the aged noises decay below 0.4)
        assert run.candidate_pool == 3
        # only the needle-collision chunk is serveable noise now
        assert run.noise_pool == 1
        # budget 1200 admits every short chunk
        assert len(run.served) == 3
        assert run.served_noise == 1
        # the aged chunks fall under the non-focal floor too: nothing counted
        assert run.non_focal_above_floor == 0
        assert run.budget_chars == 1200
        # the needle mechanics end-to-end: the shared head needle false-fires
        # the collision on every fact citation; both facts reinforce
        ids = rig.chunk_ids_by_label(material)
        assert set(run.reinforced) == {
            ids["fact"],
            ids["fact_support"],
            ids["needle_collision"],
        }
        # the consumption evidence read back from last_reinforced matches
        assert set(rig.consumption_evidence()) == set(run.reinforced)


def test_rig_applies_the_material_decay_layering(tmp_path: Path) -> None:
    """The rig writes each stored turn's declared decay into the vector store
    BEFORE the cue scan runs — that layering is what makes the floor axis
    discriminative.

    Post-run weight reads are rebound-shifted for REINFORCED chunks (every
    citation steps +0.1 toward 1.0), so the never-reinforced aged chunks are
    asserted numerically while the needle-collision layering is proven
    behaviorally: at floor 0.5 it must be gated out of the candidate pool
    (an unapplied write would leave it at the 1.0 ingest stamp and in-pool).
    """
    from mnemoseed_local.storage.ports import ChunkFilter, Page

    material = next(m for m in recall_materials() if m.language == "en" and m.length_band == "short")
    with RecallRig(tmp_path / "rig") as rig:
        run = rig.run_material(material)
        page = rig.client.app.state.stores.vector.list_chunks(ChunkFilter(profile_id="t4a"), Page(0, 100))
        weight_by_id = {chunk.chunk_id: chunk.decay_weight for chunk in page.items}
        weights = {
            label: weight_by_id[chunk_id] for label, chunk_id in rig.chunk_ids_by_label(material).items()
        }
    assert weights["entity_miss"] == pytest.approx(0.35)
    assert weights["entity_collision"] == pytest.approx(0.35)
    # the aged chunks stay below the whole sweep: never focal, never counted
    assert run.candidate_pool == 3 and run.noise_pool == 1  # nc in-pool at floor 0.4
    with RecallRig(tmp_path / "rig-high", focal_floor=0.5) as high:
        high_run = high.run_material(recall_materials()[0])
    assert high_run.candidate_pool == 2  # facts only: nc decayed below 0.5
    assert high_run.noise_pool == 0
    assert high_run.served_noise == 0


def test_rig_pipeline_metrics_are_hand_computable(tmp_path: Path) -> None:
    material = next(m for m in recall_materials() if m.language == "en" and m.length_band == "short")
    with RecallRig(tmp_path / "rig") as rig:
        run = rig.run_material(material)
        metrics = score_recall(run)
        # served 3 of 3 candidates
        assert metrics.recall_at_k == (1 / 3, 1.0, 1.0, 1.0)
        # both facts are genuinely referenced among the 3 served
        assert metrics.precision_at_k == (2 / 3, 2 / 3, 2 / 3, 2 / 3)
        # the single serveable noise candidate is served: floor-fp saturated
        assert metrics.floor_fp == 1.0
        # the shared head needle adds one false reinforcement per citation;
        # 6 citations x 1 over 5x3 + 3 reinforcements = 6/18
        assert metrics.detector_fp == pytest.approx(1 / 3)
        # only the paraphrase misses: 2 unreferenced needles of 14 references
        assert metrics.fn_rate == pytest.approx(1 / 7)


def test_rig_same_root_twice_is_idempotent(tmp_path: Path) -> None:
    """B4b contract: a reused rig root must not accumulate store state — the
    second construction wipes and serves the identical picture."""
    from mnemoseed_local.storage.ports import ChunkFilter, Page

    material = next(m for m in recall_materials() if m.language == "en" and m.length_band == "short")
    root = tmp_path / "rig"
    with RecallRig(root) as rig:
        first = rig.run_material(material)
        first_total = rig.client.app.state.stores.vector.list_chunks(
            ChunkFilter(profile_id="t4a"), Page(0, 100)
        ).total
    with RecallRig(root) as rig:
        second = rig.run_material(material)
        second_total = rig.client.app.state.stores.vector.list_chunks(
            ChunkFilter(profile_id="t4a"), Page(0, 100)
        ).total
    assert first.candidate_pool == second.candidate_pool == 3
    assert first.served_noise == second.served_noise == 1
    assert first.non_focal_above_floor == second.non_focal_above_floor == 0
    # no store may carry more than one run's worth of chunks (fresh wipe):
    # the 2 fact turns + 3 noises (session A) and the cue/reply turn (B)
    assert first_total == second_total == 6


def test_rig_run_id_namespace_isolates_concurrent_runs(tmp_path: Path) -> None:
    """B4b run-id namespace: two rigs under root/runs/<id>/<point> never
    share a store — each sees its own 3-candidate pool."""
    material = next(m for m in recall_materials() if m.language == "en" and m.length_band == "short")
    root = tmp_path / "runs" / "abc123"
    with RecallRig(root / material.point_id) as rig:
        run = rig.run_material(material)
    assert run.candidate_pool == 3
    assert run.served_noise == 1


def test_rig_sequential_materials_do_not_leak(tmp_path: Path) -> None:
    """Within-run isolation: a second material through a fresh rig on the
    same parent root must see exactly its own candidates — no cross-material
    leakage via a shared profile."""
    en = next(m for m in recall_materials() if m.language == "en" and m.length_band == "short")
    zh = next(m for m in recall_materials() if m.language == "zh" and m.length_band == "short")
    assert en.point_id != zh.point_id
    with RecallRig(tmp_path / "runs" / "run1" / en.point_id) as rig:
        run_en = rig.run_material(en)
    with RecallRig(tmp_path / "runs" / "run2" / zh.point_id) as rig:
        run_zh = rig.run_material(zh)
    assert run_en.candidate_pool == 3
    assert run_zh.candidate_pool == 3
    # the entity names never cross over
    assert set(run_en.served) & set(run_zh.served) == set()
