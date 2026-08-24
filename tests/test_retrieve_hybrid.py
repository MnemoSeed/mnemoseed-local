"""PRD-03 FR-3.3 / FR-3.4 / FR-3.14: dual-track retrieval + fusion rerank.

Behavioral contract over the real embedded drivers (LanceDB + sqlite-graph +
synthetic embedder), mirroring the retrieval-sequence-diagram: the vector track
returns semantic neighbors within the cue-entity overlap and a decay floor; the
graph track returns the 2-hop entity-subgraph; the fusion rerank produces one
ranked union with a per-candidate score breakdown. Determinism is contractual:
no clocks, no randomness, no network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from mnemoseed_local.retrieve.cues import ExtractedCues, Intent, extract_cues
from mnemoseed_local.retrieve.hybrid import (
    Candidate,
    HybridConfig,
    HybridRecall,
    HybridRetriever,
    ScoreBreakdown,
)
from mnemoseed_local.schema.graph import Edge, GraphNode, NodeType, RelType
from mnemoseed_local.schema.stamp import EXPLICIT_PIN_SOURCE, ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import ChunkFilter
from mnemoseed_local.storage.registry import GRAPH_DRIVERS, VECTOR_DRIVERS, register

_DIM = 64
_PROFILE = "alice"
_CUE_ENTITY_WEIGHT = 0.6  # beta-internal entity component (documented in hybrid.py)


@dataclass
class _Stack:
    vector: LanceDbEmbeddedStore
    graph: SqliteGraphDriver
    embed: SyntheticEmbedder


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not VECTOR_DRIVERS.contains("lancedb_embedded"):
        register(VECTOR_DRIVERS)(LanceDbEmbeddedStore)
    if not GRAPH_DRIVERS.contains("sqlite_graph"):
        register(GRAPH_DRIVERS)(SqliteGraphDriver)
    yield


@pytest.fixture
def stack(tmp_path):
    db = _Stack(
        vector=LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=_DIM),
        graph=SqliteGraphDriver(path=tmp_path / "graph.db"),
        embed=SyntheticEmbedder(dimension=_DIM),
    )
    yield db
    asyncio.run(db.vector.close())
    asyncio.run(db.graph.close())


# ------------------------------------------------------------ builder helpers


def _chunk(
    chunk_id: str,
    text: str,
    *,
    decay: float = 1.0,
    host: str | None = None,
    project: str | None = None,
    time_bucket: str | None = None,
    entities: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    consolidated: bool = False,
    profile: str = _PROFILE,
    source: str = "manual",
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id="p1",
        cues=Cues(
            project=project,
            host=host,
            tools_used=list(tools),
            time_bucket=time_bucket,
            entities=list(entities),
        ),
        provenance=Provenance(
            asserted_by="user" if source == EXPLICIT_PIN_SOURCE else "test-model",
            session_id=None if source == EXPLICIT_PIN_SOURCE else "s1",
            source=source,
            confidence=0.8,
            asserted_at=100.0,
        ),
        decay_weight=decay,
        score=0.5,
        consolidated=consolidated,
        ingested_at=1.0,
        turn_start=1,
        turn_end=2,
    )


def _write(stack: _Stack, stamp: ChunkStamp) -> None:
    result = stack.embed.embed(stamp.text)
    stack.vector.upsert_chunk(stamp, result.dense, result.sparse)


def _props(node_type: NodeType) -> dict:
    if node_type is NodeType.PREFERENCE:
        return {
            "domain": "coding",
            "statement": "s",
            "valence": 0.5,
            "prior_width": 0.3,
            "trait_anchor": "a",
            "evidence_chain": [],
        }
    if node_type is NodeType.EPISODE:
        return {"summary": "s", "session_ref": "x"}
    if node_type is NodeType.HABIT:
        return {"statement": "h"}
    if node_type is NodeType.DECISION:
        return {"statement": "d"}
    if node_type is NodeType.CONSTRAINT:
        return {"rule": "r", "severity": "high"}
    return {}


def _node(
    node_id: str,
    node_type: NodeType,
    entities: tuple[str, ...],
    *,
    decay: float = 1.0,
    profile: str = _PROFILE,
) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=profile,
        node_type=node_type,
        entities=list(entities),
        props=_props(node_type),
        decay_weight=decay,
        confidence=0.7,
        provenance=Provenance(asserted_by="test-model", source="x", session_id="s1"),
        valid_from=100.0,
    )


def _edge(stack: _Stack, src: str, dst: str, *, profile: str = _PROFILE) -> None:
    stack.graph.add_edge(Edge(src=src, dst=dst, rel=RelType.HAS, profile_id=profile, created_at=1.0))


def _query_cues(
    entities: tuple[str, ...] = (),
    *,
    host: str | None = None,
    project: str | None = None,
    time_bucket: str | None = None,
    tools: tuple[str, ...] = (),
) -> ExtractedCues:
    return ExtractedCues(
        cues=Cues(
            entities=list(entities),
            host=host,
            project=project,
            time_bucket=time_bucket,
            tools_used=list(tools),
        ),
        intent=Intent.RECALL,
    )


def _recall(
    stack: _Stack,
    text: str,
    cues: ExtractedCues,
    config: HybridConfig | None = None,
) -> HybridRecall:
    return HybridRetriever(config).recall(
        text,
        cues,
        profile_id=_PROFILE,
        vector_store=stack.vector,
        graph_store=stack.graph,
        embedder=stack.embed,
    )


def _chunk_candidates(result: HybridRecall) -> list[Candidate]:
    return [c for c in result.candidates if c.kind == "chunk"]


def _graph_candidates(result: HybridRecall) -> list[Candidate]:
    return [c for c in result.candidates if c.kind == "graph"]


# ------------------------------------------------------------ vector track


def test_vector_track_returns_semantic_neighbors_filtered_by_cue_entities(stack) -> None:
    _write(stack, _chunk("c_mem", "the LanceDb loader caches vectors", decay=0.9, entities=("LanceDb",)))
    _write(stack, _chunk("c_other", "the university library at noon", decay=0.9, entities=("University",)))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    ids = {c.id for c in _chunk_candidates(result)}
    assert "c_mem" in ids
    assert "c_other" not in ids


def test_entity_filter_blocks_token_overlap_without_cue_entity(stack) -> None:
    _write(stack, _chunk("c_ent", "the LanceDb retrieval loader", decay=0.9, entities=("LanceDb",)))
    _write(stack, _chunk("c_noent", "the LanceDb retrieval loader too", decay=0.9, entities=("Cache",)))
    result = _recall(stack, "lancedb retrieval loader", _query_cues(("LanceDb",)))
    ids = {c.id for c in _chunk_candidates(result)}
    assert "c_ent" in ids
    assert "c_noent" not in ids


def test_vector_track_keeps_chunks_missing_entity_cues(stack) -> None:
    """D2 (live-drain finding): chunks whose stored entity cues are EMPTY —
    written before the daemon filled them, or written by paths with nothing to
    extract — mean 'no entity evidence', never a contradiction. The entity
    gate must only exclude positive mismatches."""
    _write(stack, _chunk("c_untagged", "the LanceDb loader caches vectors", decay=0.9, entities=()))
    _write(stack, _chunk("c_tagged", "the LanceDb retrieval loader", decay=0.9, entities=("LanceDb",)))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    ids = {c.id for c in _chunk_candidates(result)}
    assert "c_untagged" in ids
    assert "c_tagged" in ids


def test_vector_track_excludes_decay_below_floor(stack) -> None:
    _write(stack, _chunk("c_high", "LanceDb loader", decay=0.9, entities=("LanceDb",)))
    _write(stack, _chunk("c_low", "LanceDb loader", decay=0.2, entities=("LanceDb",)))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    ids = {c.id for c in _chunk_candidates(result)}
    assert "c_high" in ids
    assert "c_low" not in ids


# ---------------------------------------------------- cue-driven rescue band


def test_rescue_band_admits_pin_with_strong_cue_below_main_floor(stack) -> None:
    """design/09 §3.5 (cue-driven rescue): a flashbulb pin whose weight faded
    into the rescue band [rescue_floor, 0.4) still enters the pool when the
    query's cue match is strong — encoding specificity: strong cues retrieve
    weak traces. The candidate carries the rescued marker."""
    config = HybridConfig(rescue_min_decay=0.25, rescue_cue_min=0.3)
    _write(
        stack,
        _chunk("pin", "LanceDb loader", decay=0.3, entities=("LanceDb",), source=EXPLICIT_PIN_SOURCE),
    )
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)), config=config)
    rescued = [c for c in _chunk_candidates(result) if c.id == "pin"]
    assert len(rescued) == 1
    assert rescued[0].rescued is True


def test_rescue_band_never_admits_plain_chunks_below_main_floor(stack) -> None:
    """The relaxation is pin-class only: an ordinary chunk below the main floor
    stays excluded no matter how well the cue matches (status quo preserved)."""
    config = HybridConfig(rescue_min_decay=0.25, rescue_cue_min=0.3)
    _write(stack, _chunk("plain", "LanceDb loader", decay=0.3, entities=("LanceDb",)))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)), config=config)
    assert _chunk_candidates(result) == []


def test_prefilter_expresses_relax_pins_only_in_storage(stack) -> None:
    """Route (b) prefilter (design/09 §3.5): the retriever hands the store a
    TWO-BAND window — main floor for everyone, relaxed floor for explicit pins
    only — so faded non-pin chunks are excluded at the storage layer and never
    consume vector top-K slots; the joint post-filter stays defense-in-depth."""
    config = HybridConfig(rescue_min_decay=0.25, rescue_cue_min=0.3)
    captured: list[ChunkFilter] = []

    class _SpyStore:
        def __init__(self, inner: LanceDbEmbeddedStore) -> None:
            self._inner = inner

        def search(self, dense, sparse, filter, top_k):  # noqa: A002 - port signature
            captured.append(filter)
            return self._inner.search(dense, sparse, filter, top_k)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    _write(stack, _chunk("faded", "LanceDb loader", decay=0.3, entities=("LanceDb",)))
    _write(
        stack,
        _chunk("aged", "LanceDb loader", decay=0.3, entities=("LanceDb",), source=EXPLICIT_PIN_SOURCE),
    )
    _write(stack, _chunk("healthy", "LanceDb loader", decay=0.9, entities=("LanceDb",)))

    result = HybridRetriever(config).recall(
        "lancedb loader",
        _query_cues(("LanceDb",)),
        profile_id=_PROFILE,
        vector_store=_SpyStore(stack.vector),
        graph_store=stack.graph,
        embedder=stack.embed,
    )

    assert len(captured) == 1
    assert captured[0].min_decay == config.min_decay
    assert captured[0].pin_min_decay == config.rescue_min_decay
    # behavioral: the faded non-pin is gone while both legitimate rows survive
    by_id = {c.id: c for c in _chunk_candidates(result)}
    assert set(by_id) == {"aged", "healthy"}
    assert by_id["aged"].rescued is True
    assert by_id["healthy"].rescued is False


def test_rescue_band_requires_the_cue_minimum(stack) -> None:
    """Below the main floor a pin must clear the cue-match threshold: a partial
    entity overlap (one of two query entities stored) scores β_entity = 0.3 and
    is gated by a 0.4 minimum, admitted at 0.2."""
    partial_overlap = 0.6 * (1 / 2)  # beta-internal entity component, 1-of-2 query entities
    config = HybridConfig(rescue_min_decay=0.25, rescue_cue_min=0.4)
    _write(
        stack,
        _chunk("pin", "LanceDb loader", decay=0.3, entities=("LanceDb",), source=EXPLICIT_PIN_SOURCE),
    )
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb", "Cache")), config=config)
    assert [c.id for c in _chunk_candidates(result) if c.rescued] == []

    permissive = HybridConfig(rescue_min_decay=0.25, rescue_cue_min=partial_overlap)
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb", "Cache")), config=permissive)
    assert [c.id for c in _chunk_candidates(result) if c.rescued] == ["pin"]


def test_below_rescue_floor_pin_stays_dead(stack) -> None:
    """The dead zone holds: below the rescue floor even a perfect-cue pin never
    enters the pool (it remains reachable only as index residue)."""
    config = HybridConfig(rescue_min_decay=0.25, rescue_cue_min=0.3)
    _write(
        stack,
        _chunk("deep", "LanceDb loader", decay=0.1, entities=("LanceDb",), source=EXPLICIT_PIN_SOURCE),
    )
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)), config=config)
    assert _chunk_candidates(result) == []


def test_rescued_candidate_never_ranks_above_a_normal_one(stack) -> None:
    """Enforced rank discipline (design/09 §3.5): a rescued candidate sorts
    after ALL normal candidates regardless of its fused score — being fished
    back must not let a weak trace displace decay-healthy memories."""
    from mnemoseed_local.retrieve.hybrid import _merge

    strong_rescued = Candidate(
        kind="chunk",
        id="rescued-high",
        source="vector",
        item=None,
        score=10.0,
        breakdown=ScoreBreakdown(
            semantic=1.0,
            cue_overlap=0.6,
            decay_weight=0.2,
            graph_centrality=0.0,
            cooccurrence=0.0,
            total=10.0,
        ),
        rescued=True,
    )
    modest_normal = Candidate(
        kind="chunk",
        id="normal-low",
        source="vector",
        item=None,
        score=1.0,
        breakdown=ScoreBreakdown(
            semantic=0.5,
            cue_overlap=0.6,
            decay_weight=0.9,
            graph_centrality=0.0,
            cooccurrence=0.0,
            total=1.0,
        ),
        rescued=False,
    )
    merged = _merge([strong_rescued], [modest_normal])

    assert [candidate.id for candidate in merged.candidates] == ["normal-low", "rescued-high"]


def test_vector_track_excludes_consolidated_chunks(stack) -> None:
    """AC1: a chunk the dream merge marked consolidated exits the recall
    surface — retained as evidence, never re-surfaced as a candidate — while
    unconsolidated chunks keep appearing (status quo)."""
    _write(
        stack,
        _chunk("c_merged", "the LanceDb loader caches vectors", decay=0.9, entities=("LanceDb",)),
    )
    stack.vector.mark_consolidated(["c_merged"])
    _write(
        stack,
        _chunk("c_fresh", "the LanceDb loader caches vectors fresh", decay=0.9, entities=("LanceDb",)),
    )
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    ids = {c.id for c in _chunk_candidates(result)}
    assert "c_fresh" in ids
    assert "c_merged" not in ids


def test_consolidated_chunk_not_duplicated_by_graph_node(stack) -> None:
    """AC2: one fact kept both as a merged chunk (consolidated) and its graph
    node double — recall surfaces the graph node only, never the chunk."""
    _write(
        stack,
        _chunk("c_digest", "prefers vim for edits", decay=0.9, entities=("LanceDb",)),
    )
    stack.vector.mark_consolidated(["c_digest"])
    stack.graph.upsert_node(_node("g_digest", NodeType.PREFERENCE, ("LanceDb",), decay=0.9))
    result = _recall(stack, "prefers vim for edits", _query_cues(("LanceDb",)))
    ids = {c.id for c in result.candidates}
    assert "g_digest" in ids
    assert "c_digest" not in ids


def test_vector_track_scopes_to_profile(stack) -> None:
    _write(stack, _chunk("alice_mem", "LanceDb loader", decay=0.9, entities=("LanceDb",)))
    _write(
        stack,
        _chunk("bob_mem", "LanceDb loader", decay=0.9, entities=("LanceDb",), profile="bob"),
    )
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    ids = {c.id for c in _chunk_candidates(result)}
    assert "alice_mem" in ids
    assert "bob_mem" not in ids


def test_vector_track_caps_at_config_top_k(stack) -> None:
    for i in range(25):
        _write(stack, _chunk(f"cap-{i:02d}", "LanceDb loader", decay=0.9, entities=("LanceDb",)))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    assert len(_chunk_candidates(result)) == HybridConfig().vector_top_k


# ------------------------------------------------------------ graph track


def test_graph_track_traverses_two_hops_with_decay_floor(stack) -> None:
    stack.graph.upsert_node(_node("lan", NodeType.PREFERENCE, ("LanceDb",), decay=0.9))
    stack.graph.upsert_node(_node("mid", NodeType.EPISODE, ("cache",), decay=0.9))
    stack.graph.upsert_node(_node("deep", NodeType.HABIT, ("retrieval",), decay=0.9))
    stack.graph.upsert_node(_node("weak", NodeType.EPISODE, (), decay=0.3))
    _edge(stack, "lan", "mid")
    _edge(stack, "mid", "deep")
    _edge(stack, "mid", "weak")
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    ids = {c.id for c in _graph_candidates(result)}
    assert {"lan", "mid", "deep"} <= ids
    assert "weak" not in ids


def test_graph_track_scopes_to_profile(stack) -> None:
    stack.graph.upsert_node(_node("a_lan", NodeType.PREFERENCE, ("LanceDb",), decay=0.9))
    stack.graph.upsert_node(_node("b_lan", NodeType.PREFERENCE, ("LanceDb",), decay=0.9, profile="bob"))
    stack.graph.upsert_node(_node("b_guard", NodeType.EPISODE, (), decay=0.9, profile="bob"))
    _edge(stack, "b_lan", "b_guard", profile="bob")
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    ids = {c.id for c in _graph_candidates(result)}
    assert "a_lan" in ids
    assert not {"b_lan", "b_guard"} & ids


def test_graph_track_empty_cue_entities_yields_no_candidates(stack) -> None:
    stack.graph.upsert_node(_node("lan", NodeType.PREFERENCE, ("LanceDb",), decay=0.9))
    result = _recall(stack, "lancedb loader", _query_cues())
    assert result.graph_hits == 0
    assert _graph_candidates(result) == []


def test_graph_track_caps_at_config_top_k(stack) -> None:
    stack.graph.upsert_node(_node("hub", NodeType.PREFERENCE, ("LanceDb",), decay=0.9))
    for i in range(24):
        stack.graph.upsert_node(_node(f"leaf-{i:02d}", NodeType.EPISODE, (), decay=0.9))
        _edge(stack, "hub", f"leaf-{i:02d}")
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    assert len(_graph_candidates(result)) == HybridConfig().graph_top_k


# ------------------------------------------------------------ fusion rerank


def test_merged_ranking_has_breakdown_per_candidate(stack) -> None:
    _write(stack, _chunk("c1", "the LanceDb loader", decay=0.9, host="cursor", entities=("LanceDb",)))
    stack.graph.upsert_node(_node("g1", NodeType.PREFERENCE, ("LanceDb",), decay=0.9))
    stack.graph.upsert_node(_node("g2", NodeType.EPISODE, (), decay=0.9))
    _edge(stack, "g1", "g2")
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",), host="cursor"))
    candidates = result.candidates
    assert len(candidates) >= 3
    ranked = sorted(candidates, key=lambda c: (-c.score, c.kind, c.id))
    assert [c.id for c in candidates] == [c.id for c in ranked]
    for cand in candidates:
        assert cand.score == cand.breakdown.total
        assert isinstance(cand.breakdown, ScoreBreakdown)
    chunk_breakdown = next(c for c in candidates if c.kind == "chunk").breakdown
    assert chunk_breakdown.semantic > 0.0
    assert chunk_breakdown.graph_centrality == 0.0
    assert chunk_breakdown.cooccurrence == 0.0
    for cand in _graph_candidates(result):
        assert cand.breakdown.semantic == 0.0


def test_candidate_payload_and_kind(stack) -> None:
    _write(stack, _chunk("ck", "LanceDb loader", decay=0.9, entities=("LanceDb",)))
    stack.graph.upsert_node(_node("gn", NodeType.PREFERENCE, ("LanceDb",), decay=0.9))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    for cand in _chunk_candidates(result):
        assert cand.source == "vector"
        assert isinstance(cand.item, ChunkStamp)
    for cand in _graph_candidates(result):
        assert cand.source == "graph"
        assert isinstance(cand.item, GraphNode)


def test_weights_reorder_union(stack) -> None:
    _write(stack, _chunk("the_chunk", "LanceDb retrieval loader", decay=0.9, entities=("LanceDb",)))
    stack.graph.upsert_node(_node("hub", NodeType.PREFERENCE, ("LanceDb",), decay=0.9))
    stack.graph.upsert_node(_node("n1", NodeType.EPISODE, (), decay=0.5))
    stack.graph.upsert_node(_node("n2", NodeType.EPISODE, (), decay=0.5))
    _edge(stack, "hub", "n1")
    _edge(stack, "hub", "n2")
    decay_dominant = HybridConfig(
        weight_semantic=0.0,
        weight_cue_overlap=0.0,
        weight_decay=2.0,
        weight_centrality=2.0,
        centrality_saturation=2,
    )
    semantic_only = HybridConfig(
        weight_semantic=4.0,
        weight_cue_overlap=0.0,
        weight_decay=0.0,
        weight_centrality=0.0,
        centrality_saturation=2,
    )
    result_decay = _recall(stack, "lancedb retrieval loader", _query_cues(("LanceDb",)), decay_dominant)
    result_semantic = _recall(stack, "lancedb retrieval loader", _query_cues(("LanceDb",)), semantic_only)
    assert result_decay.candidates[0].kind == "graph"
    assert result_semantic.candidates[0].kind == "chunk"


def test_cooccurrence_term_not_in_use_flag(stack) -> None:
    _write(stack, _chunk("ck", "LanceDb loader", decay=0.9, entities=("LanceDb",)))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    assert result.cooccurrence_term is False


# ------------------------------------------------------------ weak situational cues


def test_situational_context_match_raises_cue_overlap_bounded(stack) -> None:
    _write(stack, _chunk("c_none", "LanceDb loader config", decay=0.9, host="vim", entities=("LanceDb",)))
    _write(stack, _chunk("c_match", "LanceDb loader config", decay=0.9, host="cursor", entities=("LanceDb",)))
    result = _recall(stack, "lancedb loader config", _query_cues(("LanceDb",), host="cursor"))
    match = next(c for c in _chunk_candidates(result) if c.id == "c_match")
    none = next(c for c in _chunk_candidates(result) if c.id == "c_none")
    delta = match.breakdown.cue_overlap - none.breakdown.cue_overlap
    assert delta > 0.0
    assert delta <= 0.2  # bounded, low-weight contribution
    assert result.candidates.index(match) < result.candidates.index(none)


def test_situational_context_never_hard_filters(stack) -> None:
    _write(stack, _chunk("c_only", "LanceDb loader", decay=0.9, host="vim", entities=("LanceDb",)))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",), host="cursor"))
    assert any(c.id == "c_only" for c in _chunk_candidates(result))


def test_cue_overlap_blend_is_documented_shape(stack) -> None:
    _write(
        stack,
        _chunk("ck", "LanceDb loader", decay=0.9, host="cursor", entities=("LanceDb",), tools=("pytest",)),
    )
    result = _recall(
        stack,
        "lancedb loader",
        _query_cues(("LanceDb", "BgeM3"), host="cursor", tools=("pytest",)),
    )
    cand = next(c for c in _chunk_candidates(result) if c.id == "ck")
    # 0.6 * (1/2 entity) + 0.25 * (1/1 tool) + 0.15 * (1/1 host) == 0.7
    assert abs(cand.breakdown.cue_overlap - 0.7) < 1e-9


def test_tool_cue_overlap_activates_on_stored_tool_names(stack) -> None:
    """Option C repair: capture now fills chunk.cues.tools_used, so the stored
    side of the β_tool=0.25 overlap term is no longer always empty — a chunk
    carrying the queried tool name outranks an otherwise identical one."""
    _write(
        stack,
        _chunk("c_tool", "the LanceDb loader", decay=0.9, entities=("LanceDb",), tools=("bash",)),
    )
    _write(stack, _chunk("c_plain", "the LanceDb loader", decay=0.9, entities=("LanceDb",)))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",), tools=("bash",)))
    tool_chunk = next(c for c in _chunk_candidates(result) if c.id == "c_tool")
    plain_chunk = next(c for c in _chunk_candidates(result) if c.id == "c_plain")
    # 0.25 * (1/1 tool overlap) vs 0.25 * 0 for the tool-less chunk
    assert abs(tool_chunk.breakdown.cue_overlap - plain_chunk.breakdown.cue_overlap - 0.25) < 1e-9
    assert result.candidates.index(tool_chunk) < result.candidates.index(plain_chunk)


def test_tool_cue_overlap_fires_via_extract_cues_query_path(stack) -> None:
    """IMPORTANT-1 e2e: capture now filters stored tool names through
    _is_tool_name, so only query-matchable names persist. This walks the REAL
    query path — the tool name is extracted by extract_cues (not hand-built
    cues) — and proves the β_tool=0.25 overlap term fires on a matchable name
    (snake_case, backticked like a real query)."""
    _write(
        stack,
        _chunk("c_tool", "the LanceDb loader", decay=0.9, entities=("LanceDb",), tools=("run_tests",)),
    )
    _write(stack, _chunk("c_plain", "the LanceDb loader", decay=0.9, entities=("LanceDb",)))
    query_text = "the `run_tests` loader for LanceDb"
    query = extract_cues(query_text)
    assert "run_tests" in query.cues.tools_used  # the real extractor sees the tool
    result = _recall(stack, query_text, query)
    tool_chunk = next(c for c in _chunk_candidates(result) if c.id == "c_tool")
    plain_chunk = next(c for c in _chunk_candidates(result) if c.id == "c_plain")
    # 0.25 * (1/1 tool overlap) vs 0.25 * 0 for the tool-less chunk
    assert abs(tool_chunk.breakdown.cue_overlap - plain_chunk.breakdown.cue_overlap - 0.25) < 1e-9
    assert result.candidates.index(tool_chunk) < result.candidates.index(plain_chunk)


def test_entity_overlap_is_casefolded(stack) -> None:
    # "BgeM3" stored vs "bgeM3" queried: the store prefilter passes via the
    # exact-cased "LanceDb", the overlap score folds case on both entities.
    _write(stack, _chunk("ck", "LanceDb loader", decay=0.9, entities=("LanceDb", "BgeM3")))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb", "bgeM3")))
    cand = next(c for c in _chunk_candidates(result) if c.id == "ck")
    assert abs(cand.breakdown.cue_overlap - _CUE_ENTITY_WEIGHT) < 1e-9


# ------------------------------------------------------------ determinism


def test_identical_inputs_identical_output(stack) -> None:
    _write(stack, _chunk("c1", "LanceDb loader", decay=0.9, host="cursor", entities=("LanceDb",)))
    stack.graph.upsert_node(_node("g1", NodeType.PREFERENCE, ("LanceDb",), decay=0.9))
    stack.graph.upsert_node(_node("g2", NodeType.EPISODE, (), decay=0.9))
    _edge(stack, "g1", "g2")
    cues = _query_cues(("LanceDb",), host="cursor")
    first = _recall(stack, "lancedb loader", cues)
    second = _recall(stack, "lancedb loader", cues)
    assert first == second
    assert [c.id for c in first.candidates] == [c.id for c in second.candidates]


def test_tie_break_by_stable_id(stack) -> None:
    _write(stack, _chunk("chunk-b", "LanceDb loader", decay=0.9, entities=("LanceDb",)))
    _write(stack, _chunk("chunk-a", "LanceDb loader", decay=0.9, entities=("LanceDb",)))
    result = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    ids = [c.id for c in _chunk_candidates(result)]
    assert ids[0] == "chunk-a"
    assert ids[1] == "chunk-b"


# ------------------------------------------------------------ config surface


def test_default_config_documented_defaults() -> None:
    config = HybridConfig()
    assert config.vector_top_k == 20
    assert config.graph_top_k == 20
    assert config.min_decay == 0.4
    assert config.weight_semantic == 1.0
    assert config.weight_cue_overlap == 1.0
    assert config.weight_decay == 0.8
    assert config.weight_centrality == 0.5
    assert config.centrality_saturation == 8
