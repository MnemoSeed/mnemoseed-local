"""PRD-03 FR-3.5 / FR-3.6 / FR-3.8 / FR-3.13: context assembly.

Behavioral contract over the real embedded drivers (LanceDB + sqlite-graph +
sqlite-meta + synthetic embedder): the budget gate drops over-budget tails and
reports dropped_count; conflict-flagged graph candidates are admitted as
atomic pairs with explicit marking; the Freshness Guard demotes graph
candidates whose cortex conclusions have unconsolidated contradicting chunks
(newer than the pool watermark) and attaches truncated evidence; zero
qualifying candidates yield an honest empty result with a coverage report.
Determinism is contractual: identical inputs produce identical packages.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from mnemoseed_local.dream.delta import estimate_tokens
from mnemoseed_local.retrieve.assemble import (
    AssembleConfig,
    AssembledContext,
    AssembledEntry,
    Assembler,
    CoverageReport,
    EntryFlag,
)
from mnemoseed_local.retrieve.cues import ExtractedCues, Intent
from mnemoseed_local.retrieve.hybrid import (
    Candidate,
    HybridConfig,
    HybridRecall,
    HybridRetriever,
    ScoreBreakdown,
)
from mnemoseed_local.schema.graph import Edge, GraphNode, NodeType, RelType
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import TurnRange
from mnemoseed_local.storage.registry import GRAPH_DRIVERS, META_DRIVERS, VECTOR_DRIVERS, register

_DIM = 64
_PROFILE = "alice"


@dataclass
class _Stack:
    vector: LanceDbEmbeddedStore
    graph: SqliteGraphDriver
    meta: SqliteMetaDriver
    embed: SyntheticEmbedder


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not VECTOR_DRIVERS.contains("lancedb_embedded"):
        register(VECTOR_DRIVERS)(LanceDbEmbeddedStore)
    if not GRAPH_DRIVERS.contains("sqlite_graph"):
        register(GRAPH_DRIVERS)(SqliteGraphDriver)
    if not META_DRIVERS.contains("sqlite_meta"):
        register(META_DRIVERS)(SqliteMetaDriver)
    yield


@pytest.fixture
def stack(tmp_path):
    db = _Stack(
        vector=LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=_DIM),
        graph=SqliteGraphDriver(path=tmp_path / "graph.db"),
        meta=SqliteMetaDriver(path=tmp_path / "meta.db"),
        embed=SyntheticEmbedder(dimension=_DIM),
    )
    yield db
    asyncio.run(db.vector.close())
    asyncio.run(db.graph.close())
    asyncio.run(db.meta.close())


# ------------------------------------------------------------ builder helpers


def _chunk(
    chunk_id: str,
    text: str,
    *,
    decay: float = 1.0,
    turn_start: int = 1,
    turn_end: int = 2,
    ingested_at: float = 1.0,
    entities: tuple[str, ...] = (),
    profile: str = _PROFILE,
    consolidated: bool = False,
    session_id: str | None = "s1",
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id="p1",
        cues=Cues(entities=list(entities)),
        provenance=Provenance(
            asserted_by="test-model",
            session_id=session_id,
            source="manual",
            confidence=0.8,
            asserted_at=100.0,
        ),
        decay_weight=decay,
        score=0.5,
        consolidated=consolidated,
        ingested_at=ingested_at,
        turn_start=turn_start,
        turn_end=turn_end,
    )


def _write(stack: _Stack, stamp: ChunkStamp) -> None:
    result = stack.embed.embed(stamp.text)
    stack.vector.upsert_chunk(stamp, result.dense, result.sparse)


def _props(node_type: NodeType, statement: str | None = None) -> dict:
    if node_type is NodeType.PREFERENCE:
        return {
            "domain": "coding",
            "statement": statement or "s",
            "valence": 0.5,
            "prior_width": 0.3,
            "trait_anchor": "a",
            "evidence_chain": [],
        }
    if node_type is NodeType.EPISODE:
        return {"summary": statement or "s", "session_ref": "x"}
    if node_type is NodeType.HABIT:
        return {"statement": statement or "h"}
    if node_type is NodeType.DECISION:
        return {"statement": statement or "d"}
    return {}


def _node(
    node_id: str,
    node_type: NodeType = NodeType.PREFERENCE,
    entities: tuple[str, ...] = (),
    *,
    decay: float = 1.0,
    statement: str | None = None,
    conflict_flag: bool = False,
    conflict_group: str | None = None,
    pending_consolidation: bool = False,
    profile: str = _PROFILE,
) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=profile,
        node_type=node_type,
        entities=list(entities),
        props=_props(node_type, statement),
        decay_weight=decay,
        confidence=0.7,
        conflict_flag=conflict_flag,
        conflict_group=conflict_group,
        pending_consolidation=pending_consolidation,
        provenance=Provenance(asserted_by="test-model", source="x", session_id="s1"),
        valid_from=100.0,
    )


def _edge(stack: _Stack, src: str, dst: str, *, profile: str = _PROFILE) -> None:
    stack.graph.add_edge(Edge(src=src, dst=dst, rel=RelType.HAS, profile_id=profile, created_at=1.0))


def _query_cues(entities: tuple[str, ...] = ()) -> ExtractedCues:
    return ExtractedCues(
        cues=Cues(entities=list(entities)),
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


def _assemble(
    stack: _Stack,
    recall: HybridRecall,
    config: AssembleConfig | None = None,
) -> AssembledContext:
    return Assembler(config).assemble(
        recall,
        profile_id=_PROFILE,
        meta_store=stack.meta,
        vector_store=stack.vector,
        graph_store=stack.graph,
    )


# ------------------------------------------------------------ budget gate


def test_budget_gate_exact_boundary_admits_and_drops_tail(stack) -> None:
    # Fully deterministic ranking: three same-score graph candidates, id order.
    text_a = "alpha bravo charlie delta"
    text_b = "echo foxtrot golf hotel india"
    text_c = "juliet kilo lima mike november"
    for node_id, text in (("g_a", text_a), ("g_b", text_b), ("g_c", text_c)):
        stack.graph.upsert_node(_node(node_id, statement=text, entities=("LanceDb",)))
    budget = estimate_tokens(text_a) + estimate_tokens(text_b)  # exact boundary
    result = _assemble(
        stack,
        _recall(stack, "editor memory", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=3, budget_tokens=budget),
    )
    assert [entry.id for entry in result.entries] == ["g_a", "g_b"]
    assert result.tokens_used == budget
    assert result.dropped_count == 1
    assert result.coverage.pool_size == 3


def test_budget_gate_drops_sixth_candidate(stack) -> None:
    # Identical text => identical embedding => id-order tie-break is decisive.
    for i in range(6):
        _write(stack, _chunk(f"c{i}", "LanceDb loader cache", entities=("LanceDb",)))
    result = _assemble(stack, _recall(stack, "lancedb loader", _query_cues(("LanceDb",))))
    assert [entry.id for entry in result.entries] == [f"c{i}" for i in range(5)]
    assert result.dropped_count == 1
    assert result.coverage.pool_size == 6


def test_budget_gate_reports_dropped_count_never_silent(stack) -> None:
    _write(stack, _chunk("c_fit", "LanceDb loader", entities=("LanceDb",)))
    _write(stack, _chunk("c_other", "LanceDb loader again", entities=("LanceDb",)))
    result = _assemble(
        stack,
        _recall(stack, "lancedb loader", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=1, budget_tokens=800),
    )
    assert len(result.entries) == 1
    assert result.dropped_count == 1


def test_budget_estimator_counts_cjk_characters_tokens(stack) -> None:
    # 100 CJK chars estimate to 100 tokens under the reused estimator; a naive
    # len//4 inline surrogate would round them to 25 and admit both halves.
    whole = "梦" * 100
    _write(stack, _chunk("c_a", whole, entities=("LanceDb",)))
    _write(stack, _chunk("c_b", whole, entities=("LanceDb",)))
    result = _assemble(
        stack,
        _recall(stack, "lancedb loader", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=2, budget_tokens=150),
    )
    assert [entry.id for entry in result.entries] == ["c_a"]
    assert result.entries[0].tokens == 100
    assert result.tokens_used == 100
    assert result.dropped_count == 1
    assert result.coverage.pool_size == 2


# ------------------------------------------------------------ conflict pairing


def _conflict_nodes(stack: _Stack, group: str) -> None:
    stack.graph.upsert_node(
        _node(
            "g_p1", statement="prefers vim", entities=("LanceDb",), conflict_flag=True, conflict_group=group
        )
    )
    stack.graph.upsert_node(
        _node(
            "g_p2", statement="prefers emacs", entities=("LanceDb",), conflict_flag=True, conflict_group=group
        )
    )


def test_conflict_pair_returns_both_sides_marked(stack) -> None:
    _conflict_nodes(stack, "cg-vs-editor")
    result = _assemble(stack, _recall(stack, "editor memory", _query_cues(("LanceDb",))))
    entries = {entry.id: entry for entry in result.entries}
    assert set(entries) == {"g_p1", "g_p2"}
    for entry in entries.values():
        assert EntryFlag.CONFLICT_PAIR in entry.flags
        assert entry.conflict_group == "cg-vs-editor"
    assert result.dropped_count == 0


def test_conflict_pair_not_fitting_marks_omitted_sibling(stack) -> None:
    stack.graph.upsert_node(
        _node(
            "g_a",
            statement="alpha bravo charlie delta",
            entities=("LanceDb",),
            conflict_flag=True,
            conflict_group="cg-x",
        )
    )
    stack.graph.upsert_node(
        _node(
            "g_b",
            statement="echo foxtrot golf hotel india",
            entities=("LanceDb",),
            conflict_flag=True,
            conflict_group="cg-x",
        )
    )
    one_side = estimate_tokens("alpha bravo charlie delta")
    result = _assemble(
        stack,
        _recall(stack, "editor memory", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=5, budget_tokens=one_side),
    )
    assert [entry.id for entry in result.entries] == ["g_a"]
    entry = result.entries[0]
    assert EntryFlag.CONFLICT_OMITTED in entry.flags
    assert entry.conflict_group == "cg-x"
    assert result.dropped_count == 1  # the sibling is dropped, never silently


def test_conflict_pair_not_fitting_at_all_drops_whole_group(stack) -> None:
    stack.graph.upsert_node(
        _node(
            "g_a",
            statement="alpha bravo charlie delta",
            entities=("LanceDb",),
            conflict_flag=True,
            conflict_group="cg-x",
        )
    )
    stack.graph.upsert_node(
        _node(
            "g_b",
            statement="echo foxtrot golf hotel india",
            entities=("LanceDb",),
            conflict_flag=True,
            conflict_group="cg-x",
        )
    )
    tiny = estimate_tokens("alpha bravo charlie delta") // 2  # not even one member fits
    result = _assemble(
        stack,
        _recall(stack, "editor memory", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=5, budget_tokens=tiny),
    )
    assert result.entries == ()
    assert result.dropped_count == 2


def test_conflict_lone_survivor_marks_omitted_not_pair(stack) -> None:
    # Only one side shares the query cue, so the sibling never reaches the
    # pool; the survivor must not claim a pair that was never returned.
    stack.graph.upsert_node(
        _node(
            "g_win", statement="prefers vim", entities=("LanceDb",), conflict_flag=True, conflict_group="cg-x"
        )
    )
    stack.graph.upsert_node(
        _node(
            "g_lost",
            statement="prefers emacs",
            entities=("UnrelatedThing",),
            conflict_flag=True,
            conflict_group="cg-x",
        )
    )
    result = _assemble(stack, _recall(stack, "editor memory", _query_cues(("LanceDb",))))
    assert [entry.id for entry in result.entries] == ["g_win"]
    entry = result.entries[0]
    assert entry.conflict_group == "cg-x"
    assert EntryFlag.CONFLICT_OMITTED in entry.flags
    assert EntryFlag.CONFLICT_PAIR not in entry.flags
    assert result.dropped_count == 0  # the absent sibling is not a dropped drop


# ------------------------------------------------------------ freshness guard


def _freshness_stack(stack: _Stack) -> None:
    """graph-scores are fully deterministic: g_main holds the top slot until
    the guard demotes it below g_alt; f_new is the only fresh fragment."""
    stack.meta.advance_watermark(_PROFILE, TurnRange(start=0, end=5))
    stack.graph.upsert_node(
        _node("g_main", statement="alpha bravo charlie delta", entities=("LanceDb",), decay=0.9)
    )
    stack.graph.upsert_node(_node("g_alt", statement="prefers stable editor config", entities=("Editor",)))
    for i in range(6):
        stack.graph.upsert_node(_node(f"g_n{i}", NodeType.EPISODE, (), statement=f"neighbor fact {i}"))
        _edge(stack, "g_alt", f"g_n{i}")
    _edge(stack, "g_main", "g_alt")


def _fresh_chunk(stack: _Stack, chunk_id: str = "f_new") -> None:
    _write(
        stack,
        _chunk(
            chunk_id,
            "recently switched to emacs and never looked back",
            decay=0.3,
            turn_start=9,
            turn_end=10,
            ingested_at=50.0,
            entities=("LanceDb",),
        ),
    )


def test_freshness_demotion_changes_membership(stack) -> None:
    _freshness_stack(stack)
    before = _assemble(
        stack,
        _recall(stack, "lancedb loader", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=1, budget_tokens=800),
    )
    assert [entry.id for entry in before.entries] == ["g_main"]  # cortex node holds the top slot
    _fresh_chunk(stack)
    after = _assemble(
        stack,
        _recall(stack, "lancedb loader", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=1, budget_tokens=800),
    )
    assert [entry.id for entry in after.entries] == ["g_alt"]  # demotion dropped it out of the cut
    assert stack.graph.get_node("g_main").pending_consolidation is True
    assert stack.graph.get_node("g_alt").pending_consolidation is False
    assert after.coverage.pending_marked == 1
    assert after.coverage.fresh_evidence_chunks >= 1
    assert after.dropped_count == after.coverage.pool_size - 1


def test_freshness_marks_pending_with_demoted_score_and_evidence(stack) -> None:
    _freshness_stack(stack)
    _fresh_chunk(stack)
    result = _assemble(
        stack,
        _recall(stack, "lancedb loader", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=5, budget_tokens=800),
    )
    entry = next(entry for entry in result.entries if entry.id == "g_main")
    assert EntryFlag.PENDING_CONSOLIDATION in entry.flags
    assert EntryFlag.FRESH_EVIDENCE in entry.flags
    assert entry.recent_evidence == ("recently switched to emacs and never looked back",)
    assert entry.score == pytest.approx(0.8 * 1.3825, abs=1e-9)
    assert stack.graph.get_node("g_main").pending_consolidation is True


def test_freshness_evidence_capped_and_truncated(stack) -> None:
    _freshness_stack(stack)
    for i, ingested in enumerate((50.0, 40.0, 30.0)):
        _write(
            stack,
            _chunk(
                f"f{i}",
                "sample fresh fragment text for consolidation passage " * 4,
                decay=0.3,
                turn_start=9 + i,
                turn_end=10 + i,
                ingested_at=ingested,
                entities=("LanceDb",),
            ),
        )
    result = _assemble(
        stack,
        _recall(stack, "lancedb loader", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=5, budget_tokens=800, evidence_cap=2, evidence_max_chars=20),
    )
    entry = next(entry for entry in result.entries if entry.id == "g_main")
    assert len(entry.recent_evidence) == 2
    assert all(len(snippet) == 20 for snippet in entry.recent_evidence)
    assert result.coverage.fresh_evidence_chunks == 3


def test_freshness_fragment_count_deduped_across_rerun_passes(stack) -> None:
    # Three same-entity graph candidates force the cut to rerun (each pass
    # demotes the next candidate); the one fresh fragment must count once.
    stack.meta.advance_watermark(_PROFILE, TurnRange(start=0, end=5))
    for suffix in ("x1", "x2", "x3"):
        stack.graph.upsert_node(
            _node(f"g_{suffix}", statement=f"prefers config {suffix}", entities=("LanceDb",))
        )
    _fresh_chunk(stack)
    result = _assemble(
        stack,
        _recall(stack, "lancedb loader", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=2, budget_tokens=800),
    )
    assert result.coverage.fresh_evidence_chunks == 1
    assert result.coverage.pending_marked == 3
    assert result.coverage.pool_size == 3


def test_freshness_probe_excludes_consolidated_chunks(stack) -> None:
    """design/03 §4 + QA defect 1: the Freshness Guard probe targets
    UNconsolidated fragments — a chunk the dream merge marked consolidated must
    not re-surface as 'fresh unconsolidated evidence' (pending_consolidation
    marking / recent_evidence attachment), even though its turn range is past
    the watermark."""
    stack.meta.advance_watermark(_PROFILE, TurnRange(start=0, end=5))
    stack.graph.upsert_node(
        _node("g_cons", statement="prefers vim for edits", entities=("LanceDb",), decay=0.9)
    )
    _write(
        stack,
        _chunk(
            "c_cons",
            "digested fragment the dream already folded into the graph",
            decay=0.5,
            turn_start=9,
            turn_end=10,
            ingested_at=50.0,
            entities=("LanceDb",),
            consolidated=True,
        ),
    )
    result = _assemble(stack, _recall(stack, "lancedb loader", _query_cues(("LanceDb",))))
    assert result.coverage.fresh_evidence_chunks == 0  # never fresh evidence again
    assert result.coverage.pending_marked == 0
    assert stack.graph.get_node("g_cons").pending_consolidation is False
    entry = next(entry for entry in result.entries if entry.id == "g_cons")
    assert EntryFlag.PENDING_CONSOLIDATION not in entry.flags
    assert EntryFlag.FRESH_EVIDENCE not in entry.flags
    assert entry.recent_evidence == ()


def test_pre_existing_pending_flag_surfaces_without_probe(stack) -> None:
    stack.meta.advance_watermark(_PROFILE, TurnRange(start=0, end=5))
    stack.graph.upsert_node(
        _node("g_busy", statement="prefers vim", entities=("LanceDb",), pending_consolidation=True)
    )
    result = _assemble(stack, _recall(stack, "lancedb loader", _query_cues(("LanceDb",))))
    entry = next(entry for entry in result.entries if entry.id == "g_busy")
    assert EntryFlag.PENDING_CONSOLIDATION in entry.flags


# ------------------------------------------------------------ rank discipline


def _pool_candidate(chunk_id: str, text: str, *, score: float, rescued: bool = False) -> Candidate:
    breakdown = ScoreBreakdown(
        semantic=score,
        cue_overlap=0.0,
        decay_weight=0.0,
        graph_centrality=0.0,
        cooccurrence=0.0,
        total=score,
    )
    return Candidate(
        kind="chunk",
        id=chunk_id,
        source="vector",
        item=_chunk(chunk_id, text),
        score=score,
        breakdown=breakdown,
        rescued=rescued,
    )


def test_rank_discipline_surfaces_in_served_order(stack) -> None:
    """design/09 §3.5 at the serving surface: the assembler re-applies the
    retriever's rank discipline, so a rescued candidate with the highest fused
    score still renders after lower-scored normal candidates."""
    strong_rescued = _pool_candidate("c_rescued", "rescued pin text", score=2.0, rescued=True)
    modest_normal = _pool_candidate("c_normal", "normal memory text", score=1.0)
    recall = HybridRecall(candidates=[strong_rescued, modest_normal], vector_hits=2, graph_hits=0)

    result = _assemble(stack, recall)

    ids = [entry.id for entry in result.entries]
    assert ids == ["c_normal", "c_rescued"]
    assert EntryFlag.RESCUED in result.entries[1].flags
    assert result.entries[1].score == pytest.approx(2.0)


def test_top_k_truncation_never_drops_normal_for_rescued(stack) -> None:
    """Truncation inherits discipline: with a single slot the normal candidate
    keeps it and the higher-scoring rescued one is the honest drop."""
    strong_rescued = _pool_candidate("c_rescued", "rescued pin text", score=2.0, rescued=True)
    modest_normal = _pool_candidate("c_normal", "normal memory text", score=1.0)
    recall = HybridRecall(candidates=[strong_rescued, modest_normal], vector_hits=2, graph_hits=0)

    result = _assemble(stack, recall, AssembleConfig(top_k=1))

    assert [entry.id for entry in result.entries] == ["c_normal"]
    assert result.dropped_count == 1


# ------------------------------------------------------------ honest empty


def test_honest_empty_reports_coverage(stack) -> None:
    result = _assemble(stack, _recall(stack, "nothing here", _query_cues(("Nope",))))
    assert result.entries == ()
    assert result.dropped_count == 0
    assert result.tokens_used == 0
    assert isinstance(result.coverage, CoverageReport)
    assert result.coverage.pool_size == 0
    assert result.coverage.vector_hits == 0
    assert result.coverage.graph_hits == 0
    assert result.coverage.profile_chunks == 0
    assert result.coverage.watermark is None


def test_honest_empty_when_everything_dropped(stack) -> None:
    _write(stack, _chunk("c_big", "x" * 400, entities=("LanceDb",)))
    result = _assemble(
        stack,
        _recall(stack, "lancedb loader", _query_cues(("LanceDb",))),
        AssembleConfig(top_k=5, budget_tokens=10),
    )
    assert result.entries == ()
    assert result.dropped_count == 1
    assert result.coverage.pool_size == 1


# ------------------------------------------------------------ determinism


def test_identical_inputs_identical_package(stack) -> None:
    _conflict_nodes(stack, "cg-vs-editor")
    _freshness_stack(stack)
    recall = _recall(stack, "lancedb loader", _query_cues(("LanceDb",)))
    first = _assemble(stack, recall)
    second = _assemble(stack, recall)
    assert first == second
    assert [entry.id for entry in first.entries] == [entry.id for entry in second.entries]


# ------------------------------------------------------------ entry provenance


def test_chunk_entries_carry_verbatim_session_and_ingested_at(stack) -> None:
    _write(stack, _chunk("c_src", "session provenance fact", session_id="sess-9", ingested_at=42.0))
    result = _assemble(stack, _recall(stack, "session provenance", _query_cues(("LanceDb",))))
    entry = next(entry for entry in result.entries if entry.kind == "chunk")
    assert entry.session_id == "sess-9"
    assert entry.ingested_at == 42.0
    assert entry.valid_from is None


def test_graph_entries_carry_null_session_provenance_with_valid_from(stack) -> None:
    """Graph nodes aggregate many sessions: their source attribution stays the
    honest null instead of borrowing the node's updated_at as an ingest time,
    while the version chain's assertion-time valid_from is served as a fact."""
    stack.graph.upsert_node(_node("g_src", statement="graph conclusion", entities=("LanceDb",)))
    stored = stack.graph.get_node("g_src")
    assert stored is not None
    result = _assemble(stack, _recall(stack, "graph conclusion", _query_cues(("LanceDb",))))
    entry = next(entry for entry in result.entries if entry.kind == "graph")
    assert entry.session_id is None
    assert entry.ingested_at is None
    assert entry.valid_from == pytest.approx(stored.valid_from)


def test_assembled_entry_defaults_provenance_to_null() -> None:
    entry = AssembledEntry(kind="chunk", id="c", source="s", text="t", score=1.0, tokens=1, flags=())
    assert entry.session_id is None
    assert entry.ingested_at is None
    assert entry.valid_from is None
    assert entry.asserted_by is None
    assert entry.needs_reconcile is False


def test_chunk_entries_carry_asserted_by_and_needs_reconcile(stack) -> None:
    """R2 provenance-trust: a chunk entry surfaces who asserted it and the
    storage needs_reconcile flag (additive provenance on the assembled entry)."""
    cid = "c_prov"
    _write(stack, _chunk(cid, "reconcile provenance fact", entities=("LanceDb",), session_id="sess-9"))
    stack.vector.update_chunk_state([cid], needs_reconcile=True)
    result = _assemble(stack, _recall(stack, "reconcile provenance fact", _query_cues(("LanceDb",))))
    entry = next(entry for entry in result.entries if entry.kind == "chunk" and entry.id == cid)
    assert entry.asserted_by == "test-model"
    assert entry.needs_reconcile is True


# ------------------------------------------------------------ read-side conflict flag


def _read_conflict_nodes(
    stack: _Stack,
    a_id: str,
    a_statement: str,
    b_id: str,
    b_statement: str,
    *,
    entity: str = "LanceDb",
) -> None:
    stack.graph.upsert_node(_node(a_id, statement=a_statement, entities=(entity,)))
    stack.graph.upsert_node(_node(b_id, statement=b_statement, entities=(entity,)))


def test_read_conflict_flags_pair_as_reciprocal_evidence_pointers(stack) -> None:
    _read_conflict_nodes(
        stack,
        "rc_a",
        "Alice was born in berlin",
        "rc_b",
        "Alice was born in paris",
    )
    result = _assemble(stack, _recall(stack, "alice birthplace", _query_cues(("LanceDb",))))
    entries = {entry.id: entry for entry in result.entries}
    assert set(entries) == {"rc_a", "rc_b"}
    for entry in entries.values():
        assert EntryFlag.READ_CONFLICT in entry.flags
    ga, gb = stack.graph.get_node("rc_a"), stack.graph.get_node("rc_b")
    assert ga is not None and gb is not None
    assert ga.read_conflict_id == "rc_b"
    assert gb.read_conflict_id == "rc_a"


def test_read_conflict_raises_without_mutating_confidence_or_text(stack) -> None:
    _read_conflict_nodes(
        stack,
        "rc_a",
        "Alice was born in berlin",
        "rc_b",
        "Alice was born in paris",
    )
    before_a = stack.graph.get_node("rc_a")
    before_b = stack.graph.get_node("rc_b")
    assert before_a is not None and before_b is not None
    pre_conf_a = before_a.confidence
    pre_conf_b = before_b.confidence
    pre_text_a = before_a.props["statement"]
    pre_text_b = before_b.props["statement"]

    _assemble(stack, _recall(stack, "alice birthplace", _query_cues(("LanceDb",))))

    after_a = stack.graph.get_node("rc_a")
    after_b = stack.graph.get_node("rc_b")
    assert after_a is not None and after_b is not None
    assert after_a.confidence == pre_conf_a
    assert after_b.confidence == pre_conf_b
    assert after_a.props["statement"] == pre_text_a
    assert after_b.props["statement"] == pre_text_b


def test_read_conflict_under_flags_ambiguous_or_identical_statements(stack) -> None:
    # Dissimilar facts about the same entity are complementary, not a
    # contradiction: below the frame-similarity floor, no flag.
    _read_conflict_nodes(
        stack,
        "rc_c",
        "Alice is a software engineer",
        "rc_d",
        "Alice enjoys classical opera",
    )
    result = _assemble(stack, _recall(stack, "alice profile", _query_cues(("LanceDb",))))
    assert not any(EntryFlag.READ_CONFLICT in entry.flags for entry in result.entries)
    assert stack.graph.get_node("rc_c").read_conflict_id is None
    assert stack.graph.get_node("rc_d").read_conflict_id is None

    # Identical statements agree (a near-duplicate, not a contradiction): no flag.
    _read_conflict_nodes(
        stack,
        "rc_e",
        "Alice pronouns are they them",
        "rc_f",
        "Alice pronouns are they them",
    )
    result = _assemble(stack, _recall(stack, "alice pronouns", _query_cues(("LanceDb",))))
    assert not any(EntryFlag.READ_CONFLICT in entry.flags for entry in result.entries)
    assert stack.graph.get_node("rc_e").read_conflict_id is None
    assert stack.graph.get_node("rc_f").read_conflict_id is None


# ------------------------------------------------- read-conflict boundary families
#
# The QA-flagged regions: near-identical agreements (tense, typo, date-update)
# must NEVER be flagged even though character similarity is high; divergent
# opposite assertions (city/year/name, explicit negation) MUST be flagged even
# when they share a wording frame. The detector keys on token divergence, not on
# raw character overlap.

_NEAR_AGREEMENT = [
    # (id, statement_a, statement_b, entity) — the shared subject is the entity.
    ("na_tense", "Alice lives in Rome", "Alice lived in Rome", "Alice"),
    ("na_typo", "Alice moved to Paris in 2019", "Alice mosved to Paris in 2019", "Alice"),
    ("na_date", "Alice was born in 1985", "Alice was born in 1987", "Alice"),
]

_DIVERGENT_OPPOSITE = [
    # (id, statement_a, statement_b, entity) — the shared subject is the entity.
    (
        "dv_city",
        "Alice grew up in London, England",
        "Alice grew up in Paris, France",
        "Alice",
    ),
    (
        "dv_year",
        "Bob was born in 1985",
        "Bob was born in 2001",
        "Bob",
    ),
    (
        "dv_name",
        "Carol's current name is Carol Smith",
        "Carol's current name is Carol Jones",
        "Carol",
    ),
    (
        "dv_negation",
        "Dana enjoys classical opera",
        "Dana does not enjoy classical opera",
        "Dana",
    ),
]


def test_read_conflict_near_agreement_family_never_flags(stack) -> None:
    for node_id, text_a, text_b, entity in _NEAR_AGREEMENT:
        a_id, b_id = f"{node_id}_a", f"{node_id}_b"
        _read_conflict_nodes(stack, a_id, text_a, b_id, text_b, entity=entity)
        result = _assemble(stack, _recall(stack, "alice fact", _query_cues((entity,))))
        entries = {entry.id: entry for entry in result.entries}
        assert {a_id, b_id} <= set(entries), (text_a, text_b)
        for entry in entries.values():
            assert EntryFlag.READ_CONFLICT not in entry.flags, (text_a, text_b)
        _assert_unflagged(stack, a_id)
        _assert_unflagged(stack, b_id)


def test_read_conflict_divergent_opposite_family_flags(stack) -> None:
    for node_id, text_a, text_b, entity in _DIVERGENT_OPPOSITE:
        a_id, b_id = f"{node_id}_a", f"{node_id}_b"
        _read_conflict_nodes(stack, a_id, text_a, b_id, text_b, entity=entity)
        result = _assemble(stack, _recall(stack, "alice fact", _query_cues((entity,))))
        entries = {entry.id: entry for entry in result.entries}
        assert {a_id, b_id} <= set(entries), (text_a, text_b)
        for entry in entries.values():
            assert EntryFlag.READ_CONFLICT in entry.flags, (text_a, text_b)
        _assert_reciprocal(stack, a_id, b_id)


def test_read_conflict_year_divergence_flags_with_subject_entity(stack) -> None:
    # Regression: when the shared subject (Alice) is registered as the node's
    # entity, the divergent value pair must still flag — the subject mention
    # stays in the frame comparison, so "born" + "Alice" form the shared frame
    # and 1985/2001 read as a same-frame value divergence.
    _read_conflict_nodes(
        stack,
        "rdv_a",
        "Alice was born in 1985",
        "rdv_b",
        "Alice was born in 2001",
        entity="Alice",
    )
    result = _assemble(stack, _recall(stack, "alice fact", _query_cues(("Alice",))))
    entries = {entry.id: entry for entry in result.entries}
    assert {"rdv_a", "rdv_b"} <= set(entries)
    for entry in entries.values():
        assert EntryFlag.READ_CONFLICT in entry.flags
    _assert_reciprocal(stack, "rdv_a", "rdv_b")


def test_read_conflict_contrast_is_mutation_sensitive(stack) -> None:
    # The SAME one-token edit: a char-similar (tense/typo) variant must NOT flag,
    # a char-distinct divergent value variant MUST. Proves the detector keys on
    # lexical divergence, not on pair membership or a similarity band.
    _read_conflict_nodes(
        stack,
        "ct_a",
        "Alice lives in Rome",
        "ct_b",
        "Alice lived in Rome",
        entity="CueContrast",
    )
    result = _assemble(stack, _recall(stack, "alice fact", _query_cues(("CueContrast",))))
    assert {e.id for e in result.entries} >= {"ct_a", "ct_b"}
    assert not any(EntryFlag.READ_CONFLICT in e.flags for e in result.entries)

    _read_conflict_nodes(
        stack,
        "cv_a",
        "Alice lives in Rome",
        "cv_b",
        "Alice lives in Paris",
        entity="CueContrast2",
    )
    result = _assemble(stack, _recall(stack, "alice fact", _query_cues(("CueContrast2",))))
    assert {e.id for e in result.entries} >= {"cv_a", "cv_b"}
    for entry in result.entries:
        if entry.id in {"cv_a", "cv_b"}:
            assert EntryFlag.READ_CONFLICT in entry.flags
    _assert_reciprocal(stack, "cv_a", "cv_b")


def test_read_conflict_stopword_entity_subject_force_kept_flags(stack) -> None:
    # Regression: an entity that collides with a STOPWORD (e.g. "Will") is a real
    # subject mention; it must stay in the content surface. Dropping it would
    # collapse a same-frame value divergence ("lives in Rome"/"lives in Paris")
    # down to a single bare predicate token and hide the contradiction.
    _read_conflict_nodes(
        stack,
        "sw_a",
        "Will lives in Rome",
        "sw_b",
        "Will lives in Paris",
        entity="Will",
    )
    result = _assemble(stack, _recall(stack, "will location", _query_cues(("Will",))))
    entries = {entry.id: entry for entry in result.entries}
    assert {"sw_a", "sw_b"} <= set(entries)
    for entry in entries.values():
        assert EntryFlag.READ_CONFLICT in entry.flags
    _assert_reciprocal(stack, "sw_a", "sw_b")


def test_read_conflict_stopword_entity_near_agreement_never_flags(stack) -> None:
    # A stopword-colliding subject must not turn tense/typo/date corrections
    # into contradictions once it is force-kept in the frame.
    cases = [
        ("sw_na", "Will lives in Rome", "Will lived in Rome"),
        ("sw_ty", "Will moved to Paris in 2019", "Will mosved to Paris in 2019"),
        ("sw_dt", "Will was born in 1985", "Will was born in 1987"),
    ]
    for prefix, text_a, text_b in cases:
        a_id, b_id = f"{prefix}_a", f"{prefix}_b"
        _read_conflict_nodes(stack, a_id, text_a, b_id, text_b, entity="Will")
        result = _assemble(stack, _recall(stack, "will fact", _query_cues(("Will",))))
        entries = {entry.id: entry for entry in result.entries}
        assert {a_id, b_id} <= set(entries), (text_a, text_b)
        assert not any(EntryFlag.READ_CONFLICT in entry.flags for entry in result.entries), (text_a, text_b)
        _assert_unflagged(stack, a_id)
        _assert_unflagged(stack, b_id)


def test_read_conflict_stopword_entity_complementary_never_flags(stack) -> None:
    # A lone shared subject mention stays complementary even when it names a
    # stopword: only the subject is force-kept, other stopwords still drop.
    _read_conflict_nodes(
        stack,
        "sw_c_a",
        "Will is a software engineer",
        "sw_c_b",
        "Will enjoys classical opera",
        entity="Will",
    )
    result = _assemble(stack, _recall(stack, "will profile", _query_cues(("Will",))))
    assert not any(EntryFlag.READ_CONFLICT in entry.flags for entry in result.entries)
    _assert_unflagged(stack, "sw_c_a")
    _assert_unflagged(stack, "sw_c_b")


def _assert_unflagged(stack: _Stack, node_id: str) -> None:
    node = stack.graph.get_node(node_id)
    assert node is not None and node.read_conflict_id is None, node_id


def _assert_reciprocal(stack: _Stack, a_id: str, b_id: str) -> None:
    a, b = stack.graph.get_node(a_id), stack.graph.get_node(b_id)
    assert a is not None and b is not None
    assert a.read_conflict_id == b_id, a_id
    assert b.read_conflict_id == a_id, b_id
