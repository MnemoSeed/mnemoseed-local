"""Driver-agnostic contract tests for the VectorStore port (prd-08 appendix B.1).

Every method of the port gets at least one behavioral test; the suite runs
against the embedded (lancedb_embedded) driver family via the `stack` fixture.
Assertions are deliberately behavioral so a third driver that honours the same
semantics passes unchanged.
"""

from __future__ import annotations

import math

import pytest
from _support import (
    PROFILE,
    make_stamp,
    raw_chunk,
    write_turn_chunk,
)

from mnemoseed_local.storage.ports import (
    Capability,
    ChunkFilter,
    Page,
    SparseVector,
    WeightUpdate,
)

_DIM = 64


def _axis(first: float, second: float = 0.0) -> list[float]:
    """Unit vector concentrated on the first two axes (exact cosine math)."""
    axis = [first, second] + [0.0] * (_DIM - 2)
    norm = math.sqrt(sum(value * value for value in axis)) or 1.0
    return [value / norm for value in axis]


# ---------------------------------------------------------------- B.1 surface


def test_capabilities(stack) -> None:
    """VectorStore.capabilities: both contract drivers declare the full set."""
    expected = frozenset(
        {Capability.VECTOR_HYBRID_SEARCH, Capability.VECTOR_METADATA_FILTER, Capability.VECTOR_SNAPSHOT}
    )
    assert stack.vector.capabilities() == stack.vector.info.capabilities == expected


def test_upsert_get_roundtrip(stack) -> None:
    """upsert_chunk / get_chunk: every appendix A.1 stamp field survives."""
    stamp = make_stamp(
        "a1",
        "alpha beta gamma",
        score=0.7,
        decay=0.6,
        entities=("math", "algebra"),
        consolidated=True,
        ingested_at=42.0,
    )
    result = stack.embed.embed(stamp.text)
    stack.vector.upsert_chunk(stamp, result.dense, result.sparse)
    got = stack.vector.get_chunk("a1")
    assert got is not None
    assert got.chunk_id == "a1"
    assert got.profile_id == PROFILE
    assert got.text == "alpha beta gamma"
    assert got.cognitive_tier is stamp.cognitive_tier
    assert got.model_id == "contract-model"
    assert got.persona_id == "p1"
    assert got.cues.project == "contract-suite"
    assert got.cues.tools_used == ["pytest"]
    assert got.cues.entities == ["math", "algebra"]
    assert got.provenance.session_id == "s1"
    assert got.provenance.asserted_by == "contract-model"
    assert got.score == pytest.approx(0.7, abs=1e-6)
    assert got.decay_weight == pytest.approx(0.6, abs=1e-6)
    assert got.consolidated is True
    assert got.ingested_at == pytest.approx(42.0, abs=1e-6)
    assert stack.vector.get_chunk("missing") is None


def test_upsert_merges_on_same_chunk_id(stack) -> None:
    result = stack.embed.embed("alpha beta gamma")
    stack.vector.upsert_chunk(make_stamp("a1", "alpha beta gamma"), result.dense, result.sparse)
    corrected = stack.embed.embed("alpha beta gamma (corrected)")
    stack.vector.upsert_chunk(
        make_stamp("a1", "alpha beta gamma (corrected)"), corrected.dense, corrected.sparse
    )
    got = stack.vector.get_chunk("a1")
    assert got is not None and got.text == "alpha beta gamma (corrected)"
    page = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(limit=10))
    assert page.total == 1


def test_delete_chunk(stack) -> None:
    """delete_chunk: forget_this removes the shard and shrinks the listing."""
    result = stack.embed.embed("to be deleted")
    stack.vector.upsert_chunk(make_stamp("gone", "to be deleted"), result.dense, result.sparse)
    assert stack.vector.get_chunk("gone") is not None
    stack.vector.delete_chunk("gone")
    assert stack.vector.get_chunk("gone") is None
    assert stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(limit=10)).total == 0


# ---------------------------------------------------------------- search


def test_search_profile_isolation_and_metadata_filters(stack) -> None:
    a = stack.embed.embed("alpha beta gamma")
    stack.vector.upsert_chunk(
        make_stamp("a1", "alpha beta gamma", decay=0.7, entities=("math",)), a.dense, a.sparse
    )
    stack.vector.upsert_chunk(make_stamp("b1", "alpha beta gamma", profile_id="bob"), a.dense, a.sparse)

    alice = stack.vector.search(a.dense, a.sparse, ChunkFilter(profile_id=PROFILE), top_k=5)
    assert {hit.chunk.chunk_id for hit in alice} == {"a1"}
    assert stack.vector.search(a.dense, a.sparse, ChunkFilter(profile_id="carol"), top_k=5) == []

    above_floor = stack.vector.search(
        a.dense, a.sparse, ChunkFilter(profile_id=PROFILE, min_decay=0.5), top_k=5
    )
    assert {hit.chunk.chunk_id for hit in above_floor} == {"a1"}
    assert (
        stack.vector.search(a.dense, a.sparse, ChunkFilter(profile_id=PROFILE, min_decay=0.8), top_k=5) == []
    )

    by_entity = stack.vector.search(
        a.dense, a.sparse, ChunkFilter(profile_id=PROFILE, entities=("math",)), top_k=5
    )
    assert {hit.chunk.chunk_id for hit in by_entity} == {"a1"}


def test_search_ingestion_and_consolidation_filters(stack) -> None:
    points = stack.embed.embed("olde text")
    # ingestion filters are inclusive bounds (ingested_at >= after, <= before)
    stack.vector.upsert_chunk(make_stamp("old", "olde text", ingested_at=10.0), points.dense, points.sparse)
    stack.vector.upsert_chunk(make_stamp("mid", "olde text", ingested_at=150.0), points.dense, points.sparse)
    stack.vector.upsert_chunk(
        make_stamp("new", "olde text", ingested_at=250.0, consolidated=True), points.dense, points.sparse
    )

    future = stack.vector.search(
        points.dense, points.sparse, ChunkFilter(profile_id=PROFILE, ingested_after=100.0), 5
    )
    assert {hit.chunk.chunk_id for hit in future} == {"mid", "new"}

    past = stack.vector.search(
        points.dense, points.sparse, ChunkFilter(profile_id=PROFILE, ingested_before=100.0), 5
    )
    assert {hit.chunk.chunk_id for hit in past} == {"old"}

    window = stack.vector.search(
        points.dense, points.sparse, ChunkFilter(profile_id=PROFILE, ingested_before=150.0), 5
    )
    assert {hit.chunk.chunk_id for hit in window} == {"old", "mid"}

    consolidated = stack.vector.search(
        points.dense, points.sparse, ChunkFilter(profile_id=PROFILE, consolidated=True), 5
    )
    assert {hit.chunk.chunk_id for hit in consolidated} == {"new"}


def test_search_session_and_turn_window_filters(stack) -> None:
    a = stack.embed.embed("turn scoped")
    stack.vector.upsert_chunk(make_stamp("plain", "turn scoped"), a.dense, a.sparse)
    write_turn_chunk(stack, "t1", "turn scoped", "conv-7", start=130, end=170)
    write_turn_chunk(stack, "t2", "turn scoped", "conv-7", start=300, end=400)

    session = stack.vector.search(a.dense, a.sparse, ChunkFilter(profile_id=PROFILE, session_id="conv-7"), 5)
    assert {hit.chunk.chunk_id for hit in session} == {"t1", "t2"}

    window = stack.vector.search(
        a.dense,
        a.sparse,
        ChunkFilter(profile_id=PROFILE, session_id="conv-7", turn_start=120, turn_end=200),
        5,
    )
    assert {hit.chunk.chunk_id for hit in window} == {"t1"}


def test_search_hybrid_ranking_sparse_breaks_dense_tie(stack) -> None:
    """hybrid search: when dense similarity ties, the sparse leg re-ranks."""
    dense = [1.0] + [0.0] * (stack.dimension - 1)
    shared = SparseVector((1, 2, 3), (0.7, 0.3, 0.2))
    disjoint = SparseVector((10, 11, 12), (0.8, 0.6, 0.4))
    stack.vector.upsert_chunk(make_stamp("q1", "shared lexicon"), dense, shared)
    stack.vector.upsert_chunk(make_stamp("q2", "disjoint lexicon"), dense, disjoint)

    hits = stack.vector.search(dense, shared, ChunkFilter(profile_id=PROFILE), top_k=2)
    assert hits[0].chunk.chunk_id == "q1"
    assert hits[0].similarity > hits[1].similarity


def test_search_dense_only_when_sparse_absent(stack) -> None:
    """sparse=None collapses hybrid scoring to the dense leg alone."""
    dense = [1.0] + [0.0] * (stack.dimension - 1)
    shared = SparseVector((1, 2, 3), (0.7, 0.3, 0.2))
    stack.vector.upsert_chunk(make_stamp("sx", "shared lexicon"), dense, shared)
    hits = stack.vector.search(dense, None, ChunkFilter(profile_id=PROFILE), top_k=5)
    assert {hit.chunk.chunk_id for hit in hits} == {"sx"}
    assert hits[0].similarity == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------- near duplicate


def test_near_duplicate_thresholds(stack) -> None:
    """0.9 reinforce vs 0.85 reconcile gating on the shared probe."""
    exact = _axis(1.0)
    near = _axis(0.95, math.sqrt(1.0 - 0.95**2))
    stack.vector.upsert_chunk(make_stamp("exact", "verbatim duplicate"), exact, SparseVector((), ()))
    stack.vector.upsert_chunk(make_stamp("near", "echoing duplicate"), near, SparseVector((), ()))

    strict = stack.vector.near_duplicate(exact, threshold=0.99, profile_id=PROFILE)
    assert [chunk.chunk_id for chunk in strict] == ["exact"]
    relaxed = stack.vector.near_duplicate(exact, threshold=0.9, profile_id=PROFILE)
    assert {chunk.chunk_id for chunk in relaxed} == {"exact", "near"}


def test_near_duplicate_profile_scoping(stack) -> None:
    exact = _axis(1.0)
    stack.vector.upsert_chunk(make_stamp("x-alice", "duplicate alice"), exact, SparseVector((), ()))
    stack.vector.upsert_chunk(
        make_stamp("x-bob", "duplicate bob", profile_id="bob"), exact, SparseVector((), ())
    )
    only_bob = stack.vector.near_duplicate(exact, threshold=0.99, profile_id="bob")
    assert [chunk.chunk_id for chunk in only_bob] == ["x-bob"]


# ---------------------------------------------------------------- snapshot / lifecycle


def test_snapshot_read_consistent_set(stack) -> None:
    """snapshot_read returns a call-consistent read matching the filter."""
    stack.vector.upsert_chunk(
        make_stamp("a1", "snap one"), stack.text_vector("snap one"), stack.embed.embed("snap one").sparse
    )
    write_turn_chunk(stack, "t1", "snap turn", "conv-7", start=100, end=150)
    snapshot = stack.vector.snapshot_read(ChunkFilter(profile_id=PROFILE))
    assert {chunk.chunk_id for chunk in snapshot} == {"a1", "t1"}
    session = stack.vector.snapshot_read(ChunkFilter(profile_id=PROFILE, session_id="conv-7"))
    assert {chunk.chunk_id for chunk in session} == {"t1"}


def test_mark_consolidated(stack) -> None:
    result = stack.embed.embed("consolidate me")
    stack.vector.upsert_chunk(make_stamp("c1", "consolidate me"), result.dense, result.sparse)
    stack.vector.upsert_chunk(make_stamp("c2", "keep loose"), result.dense, result.sparse)
    stack.vector.mark_consolidated(["c1"])

    done = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE, consolidated=True), Page(limit=10))
    assert {chunk.chunk_id for chunk in done.items} == {"c1"}
    open_ = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE, consolidated=False), Page(limit=10))
    assert {chunk.chunk_id for chunk in open_.items} == {"c2"}


def test_purge_range_disjoint_safe(stack) -> None:
    """purge_range removes the overlapping window; disjoint turns survive."""
    write_turn_chunk(stack, "t1", "first turn", "conv-7", start=100, end=150)
    write_turn_chunk(stack, "t2", "later turn", "conv-7", start=300, end=400)
    write_turn_chunk(stack, "t3", "inside turn", "conv-7", start=130, end=140)

    purged = stack.vector.purge_range("conv-7", turn_start=120, turn_end=200)
    assert purged == 2  # t1 and t3 overlap; t2 is disjoint
    assert stack.vector.get_chunk("t1") is None
    assert stack.vector.get_chunk("t3") is None
    assert stack.vector.get_chunk("t2") is not None


def test_update_weights(stack) -> None:
    result = stack.embed.embed("weighted")
    stack.vector.upsert_chunk(make_stamp("w1", "weighted"), result.dense, result.sparse)
    stack.vector.upsert_chunk(make_stamp("w2", "weighted"), result.dense, result.sparse)
    stack.vector.update_weights(
        [WeightUpdate(chunk_id="w1", decay_weight=0.3, last_reinforced=123.0, reinforce_count=2)]
    )
    assert stack.vector.get_chunk("w1").decay_weight == pytest.approx(0.3, abs=1e-6)
    row = raw_chunk(stack, "w1")
    assert int(row["reinforce_count"]) == 2
    assert row["last_reinforced"] is not None
    assert stack.vector.get_chunk("w2").decay_weight == pytest.approx(1.0, abs=1e-6)


def test_update_chunk_state_usage_counts(stack) -> None:
    """hit_increment>0 also refreshes last_hit_at; zero and None update nothing."""
    result = stack.embed.embed("stateful")
    stack.vector.upsert_chunk(make_stamp("s1", "stateful"), result.dense, result.sparse)
    stack.vector.update_chunk_state(["s1"], hit_increment=4, needs_reconcile=True)
    row = raw_chunk(stack, "s1")
    assert int(row["hit_count"]) == 4
    assert row["last_hit_at"] is not None and float(row["last_hit_at"]) > 0.0
    assert row["needs_reconcile"] is True

    stack.vector.update_chunk_state(["s1"], hit_increment=0)
    assert int(raw_chunk(stack, "s1")["hit_count"]) == 4  # zero touches nothing

    stack.vector.update_chunk_state(["s1"], needs_reconcile=False)
    assert raw_chunk(stack, "s1")["needs_reconcile"] is False

    stack.vector.update_chunk_state(["s1"])
    assert int(raw_chunk(stack, "s1")["hit_count"]) == 4  # no args -> no-op


def test_update_chunk_state_batch_and_unknown(stack) -> None:
    result = stack.embed.embed("batch state")
    stack.vector.upsert_chunk(make_stamp("s2", "batch state"), result.dense, result.sparse)
    stack.vector.upsert_chunk(make_stamp("s3", "batch state"), result.dense, result.sparse)
    stack.vector.update_chunk_state(["s2", "s3", "ghost"], hit_increment=1, needs_reconcile=True)
    for chunk_id in ("s2", "s3"):
        row = raw_chunk(stack, chunk_id)
        assert int(row["hit_count"]) == 1
        assert row["needs_reconcile"] is True


def test_list_chunks_filter_pagination(stack) -> None:
    result = stack.embed.embed("pageable")
    stack.vector.upsert_chunk(make_stamp("g1", "pageable", ingested_at=1.0), result.dense, result.sparse)
    stack.vector.upsert_chunk(make_stamp("g2", "pageable", ingested_at=2.0), result.dense, result.sparse)
    stack.vector.upsert_chunk(make_stamp("g3", "pageable", ingested_at=3.0), result.dense, result.sparse)

    first = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(offset=0, limit=2))
    assert [chunk.chunk_id for chunk in first.items] == ["g3", "g2"]  # ingested_at DESC
    assert first.total == 3

    second = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(offset=2, limit=2))
    assert [chunk.chunk_id for chunk in second.items] == ["g1"]
    assert second.total == 3

    by_entity = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE, entities=("math",)), Page(limit=10))
    assert by_entity.total == 0  # no math-flagged chunk present


def test_list_chunks_needs_reconcile_filter(stack) -> None:
    """needs_reconcile filter (console reconcile queue, PRD-07): only the
    flagged chunk matches the True filter, and the False filter excludes it.
    Pins the driver SQL clause on both contract arms."""
    vectors = stack.embed.embed("flagged")
    stack.vector.upsert_chunk(make_stamp("r1", "flagged", ingested_at=2.0), vectors.dense, vectors.sparse)
    stack.vector.upsert_chunk(make_stamp("r2", "flagged", ingested_at=1.0), vectors.dense, vectors.sparse)
    stack.vector.update_chunk_state(["r1"], needs_reconcile=True)

    flagged = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE, needs_reconcile=True), Page(limit=10))
    assert {chunk.chunk_id for chunk in flagged.items} == {"r1"}
    assert flagged.total == 1

    clean = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE, needs_reconcile=False), Page(limit=10))
    assert {chunk.chunk_id for chunk in clean.items} == {"r2"}
    assert clean.total == 1
