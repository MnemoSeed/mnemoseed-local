"""LanceDbEmbeddedStore behavior: the full VectorStore surface (prd-08 appendix
B.1) over a local LanceDB directory — upsert/merge, hybrid dense+sparse search,
metadata filters, near-duplicate dual thresholds with profile isolation,
frozen-commit snapshots, consolidation, purge, weight updates, pagination.
"""

import asyncio
import math
import threading
import time
from collections.abc import Sequence
from typing import Any

import pytest

from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers import lancedb_embedded as _lancedb_embedded
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import (
    Capability,
    ChunkFilter,
    Page,
    SparseVector,
    WeightUpdate,
)
from mnemoseed_local.storage.registry import VECTOR_DRIVERS, register


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not VECTOR_DRIVERS.contains("lancedb_embedded"):
        register(VECTOR_DRIVERS)(LanceDbEmbeddedStore)
    yield


@pytest.fixture
def embedder():
    return SyntheticEmbedder(dimension=_DIM)


@pytest.fixture
def store(tmp_path):
    db = LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=_DIM)
    yield db
    asyncio.run(db.close())


_DIM = 64


def _make(
    chunk_id: str,
    text: str,
    *,
    profile_id: str = "alice",
    session: str = "s1",
    score: float = 0.0,
    decay: float = 1.0,
    entities: tuple[str, ...] = (),
    consolidated: bool = False,
    ingested_at: float = 1.0,
    turn_start: int | None = None,
    turn_end: int | None = None,
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile_id,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id="p1",
        cues=Cues(
            project="unit-tests",
            tools_used=["pytest"],
            time_bucket="diurnal",
            entities=list(entities),
        ),
        provenance=Provenance(
            asserted_by="test-model",
            session_id=session,
            source="manual",
            confidence=0.8,
            asserted_at=100.0,
        ),
        decay_weight=decay,
        score=score,
        consolidated=consolidated,
        ingested_at=ingested_at,
        turn_start=turn_start,
        turn_end=turn_end,
    )


def _write(store: LanceDbEmbeddedStore, embedder: SyntheticEmbedder, stamp: ChunkStamp) -> None:
    result = embedder.embed(stamp.text)
    store.upsert_chunk(stamp, result.dense, result.sparse)


# ---------------------------------------------------------------- basics


def test_registered_in_shared_registry():
    assert VECTOR_DRIVERS.contains("lancedb_embedded")


def test_reopen_existing_uri_does_not_recreate(tmp_path):
    """list_tables() changed shape in lancedb 0.3x; a second boot on the same
    database must open the existing table instead of erroring on create."""
    uri = tmp_path / "chunks.lance"
    first = LanceDbEmbeddedStore(uri=uri, dimensions=_DIM)
    asyncio.run(first.close())
    second = LanceDbEmbeddedStore(uri=uri, dimensions=_DIM)
    assert second.get_chunk("nope") is None
    asyncio.run(second.close())


def test_capabilities_declared(store):
    caps = store.capabilities()
    assert Capability.VECTOR_HYBRID_SEARCH in caps
    assert Capability.VECTOR_METADATA_FILTER in caps
    assert Capability.VECTOR_SNAPSHOT in caps
    assert Capability.VECTOR_HYBRID_SEARCH in LanceDbEmbeddedStore.info.capabilities
    assert "vector.hybrid_search" in {c.value for c in caps}


def test_upsert_and_get_round_trips_all_stamp_fields(store, embedder):
    stamp = _make(
        "a1",
        "alpha beta gamma",
        score=0.7,
        decay=0.6,
        entities=("math", "algebra"),
        consolidated=True,
        ingested_at=42.0,
        turn_start=3,
        turn_end=5,
    )
    _write(store, embedder, stamp)
    got = store.get_chunk("a1")
    assert got is not None
    assert got.profile_id == "alice"
    assert got.text == "alpha beta gamma"
    assert got.cognitive_tier is CognitiveTier.TIER_1
    assert got.model_id == "test-model"
    assert got.persona_id == "p1"
    assert got.cues.project == "unit-tests"
    assert got.cues.tools_used == ["pytest"]
    assert got.cues.entities == ["math", "algebra"]
    assert got.provenance.session_id == "s1"
    assert got.provenance.asserted_by == "test-model"
    assert got.score == pytest.approx(0.7)
    assert got.decay_weight == pytest.approx(0.6)
    assert got.consolidated is True
    assert got.ingested_at == pytest.approx(42.0)
    assert got.turn_start == 3
    assert got.turn_end == 5
    assert store.list_chunks(ChunkFilter(profile_id="alice"), Page(limit=10)).total == 1


def test_upsert_merges_on_same_chunk_id(store, embedder):
    first = _make("a1", "alpha beta gamma")
    _write(store, embedder, first)
    second = _make("a1", "alpha beta gamma (corrected)")
    result = embedder.embed(second.text)
    store.upsert_chunk(second, result.dense, result.sparse)
    got = store.get_chunk("a1")
    assert got is not None and got.text == "alpha beta gamma (corrected)"
    page = store.list_chunks(ChunkFilter(profile_id="alice"), Page(limit=10))
    assert page.total == 1


def test_delete_chunk(store, embedder):
    _write(store, embedder, _make("gone", "to be deleted"))
    assert store.get_chunk("gone") is not None
    store.delete_chunk("gone")
    assert store.get_chunk("gone") is None


def test_unknown_chunk_returns_none(store):
    assert store.get_chunk("does-not-exist") is None
    assert store.list_chunks(ChunkFilter(profile_id="alice"), Page(limit=10)).items == []


# ---------------------------------------------------------------- search


def test_search_ranks_exact_text_first_and_filters_profile(store, embedder):
    for chunk_id, text in [
        ("a1", "alpha beta gamma"),
        ("a2", "alpha beta delta"),
        ("a3", "rho sigma tau"),
    ]:
        _write(store, embedder, _make(chunk_id, text))
    _write(store, embedder, _make("b1", "alpha beta gamma bob", profile_id="bob"))

    query = embedder.embed("alpha beta gamma")
    hits = store.search(query.dense, query.sparse, ChunkFilter(profile_id="alice"), top_k=3)
    assert [hit.chunk.chunk_id for hit in hits] == ["a1", "a2", "a3"]
    assert any(hit.chunk.chunk_id == "a1" for hit in hits)
    assert all(hit.chunk.profile_id == "alice" for hit in hits)

    bob_hits = store.search(query.dense, query.sparse, ChunkFilter(profile_id="bob"), top_k=3)
    assert [hit.chunk.chunk_id for hit in bob_hits] == ["b1"]


def test_search_accepts_dense_only(store, embedder):
    _write(store, embedder, _make("a1", "alpha beta gamma"))
    query = embedder.embed("alpha beta gamma")
    hits = store.search(query.dense, None, ChunkFilter(profile_id="alice"), top_k=5)
    assert hits
    assert hits[0].chunk.chunk_id == "a1"


def test_search_empty_result_on_nonmatching_profile(store, embedder):
    _write(store, embedder, _make("a1", "alpha beta gamma"))
    query = embedder.embed("alpha beta gamma")
    hits = store.search(query.dense, query.sparse, ChunkFilter(profile_id="carol"), top_k=5)
    assert hits == []


def test_hybrid_sparse_path_participates_in_ranking(store):
    """With an identical dense tie, the chunk matching the query's sparse
    signature must rank above one with a disjoint sparse signature."""
    query = SparseVector((1, 2, 3), (0.7, 0.3, 0.2))
    shared = SparseVector((1, 2, 3), (0.7, 0.3, 0.2))
    disjoint = SparseVector((10, 11, 12), (0.8, 0.6, 0.4))
    dense = [1.0] + [0.0] * (_DIM - 1)  # unit first-axis vector, full dimension

    store.upsert_chunk(_make("q1", "shared lexicon"), dense, shared)
    store.upsert_chunk(_make("q2", "disjoint lexicon"), dense, disjoint)

    hits = store.search(dense, query, ChunkFilter(profile_id="alice"), top_k=2)
    assert hits[0].chunk.chunk_id == "q1"
    assert hits[1].chunk.chunk_id == "q2"
    assert hits[0].similarity > hits[1].similarity


def test_dense_only_tie_loses_sparse_discrimination(store):
    """Without sparse the identical-dense pair records an equal similarity;
    the sparse path must be what breaks the tie (hybrid vs dense-only)."""
    query = SparseVector((1, 2, 3), (0.7, 0.3, 0.2))
    shared = SparseVector((1, 2, 3), (0.7, 0.3, 0.2))
    disjoint = SparseVector((10, 11, 12), (0.8, 0.6, 0.4))
    dense = [1.0] + [0.0] * (_DIM - 1)

    store.upsert_chunk(_make("q1", "shared lexicon"), dense, shared)
    store.upsert_chunk(_make("q2", "disjoint lexicon"), dense, disjoint)

    dense_only = store.search(dense, None, ChunkFilter(profile_id="alice"), top_k=2)
    assert len({hit.similarity for hit in dense_only}) == 1
    hybrid = store.search(dense, query, ChunkFilter(profile_id="alice"), top_k=2)
    assert hybrid[0].chunk.chunk_id == "q1"


# ---------------------------------------------------------------- metadata filters


def test_search_decay_floor_filters_low_weight_chunks(store, embedder):
    _write(store, embedder, _make("kept", "strong soon reinforced", decay=0.9))
    _write(store, embedder, _make("dropped", "fading echo of yesterday", decay=0.05))
    query = embedder.embed("strong")
    hits = store.search(query.dense, query.sparse, ChunkFilter(profile_id="alice", min_decay=0.5), 5)
    assert [hit.chunk.chunk_id for hit in hits] == ["kept"]


def test_search_session_and_entity_filters(store, embedder):
    _write(store, embedder, _make("e1", "monsoon over the bay", session="s2", entities=("weather",)))
    _write(store, embedder, _make("e2", "rocket launch tower", session="s3", entities=("space",)))
    query = embedder.embed("bay")

    session_hits = store.search(
        query.dense, query.sparse, ChunkFilter(profile_id="alice", session_id="s2"), 5
    )
    assert [hit.chunk.chunk_id for hit in session_hits] == ["e1"]

    entity_hits = store.search(
        query.dense, query.sparse, ChunkFilter(profile_id="alice", entities=("space",)), 5
    )
    assert [hit.chunk.chunk_id for hit in entity_hits] == ["e2"]

    multi_entity = store.search(
        query.dense,
        query.sparse,
        ChunkFilter(profile_id="alice", entities=("space", "weather")),
        5,
    )
    assert {hit.chunk.chunk_id for hit in multi_entity} == {"e1", "e2"}


def test_search_entity_filter_tolerates_chunks_without_entity_cues(store, embedder):
    """D2 writer/reader seam: a chunk stored with NO entity cues carries
    entities_filter = '' — missing evidence, not a contradiction. The recall
    surface (entities_allow_missing=True) keeps it alongside positive matches;
    the listing/strict default (flag off) still excludes it."""
    _write(store, embedder, _make("e_noent", "rocket launch tower", session="s4", entities=()))
    _write(store, embedder, _make("e_ent", "monsoon launch pad", session="s5", entities=("space",)))
    query = embedder.embed("launch")

    strict = store.search(query.dense, query.sparse, ChunkFilter(profile_id="alice", entities=("space",)), 5)
    assert {hit.chunk.chunk_id for hit in strict} == {"e_ent"}

    tolerant = store.search(
        query.dense,
        query.sparse,
        ChunkFilter(profile_id="alice", entities=("space",), entities_allow_missing=True),
        5,
    )
    assert {hit.chunk.chunk_id for hit in tolerant} == {"e_ent", "e_noent"}


def test_search_ingested_time_window(store, embedder):
    _write(store, embedder, _make("old", "ancient text", ingested_at=100.0))
    _write(store, embedder, _make("mid", "middle text", ingested_at=200.0))
    _write(store, embedder, _make("new", "recent text", ingested_at=300.0))
    query = embedder.embed("text")

    window = ChunkFilter(profile_id="alice", ingested_after=150.0, ingested_before=250.0)
    hits = store.search(query.dense, query.sparse, window, 5)
    assert [hit.chunk.chunk_id for hit in hits] == ["mid"]

    after = store.search(query.dense, query.sparse, ChunkFilter(profile_id="alice", ingested_after=250.0), 5)
    assert [hit.chunk.chunk_id for hit in after] == ["new"]


# ---------------------------------------------------------------- near duplicate


def _write_turn_row(
    store: LanceDbEmbeddedStore, embedder: SyntheticEmbedder, chunk_id: str, start: int, end: int
) -> None:
    stamp = _make(chunk_id, "turn scoped chunk")
    row = store._to_row(stamp, [0.5] * _DIM, SparseVector((), ()))
    row["session_id"] = "conv-7"
    row["turn_start"] = start
    row["turn_end"] = end
    store._table.merge_insert("chunk_id").when_not_matched_insert_all().execute([row])


def _unit_vector(first: float, second: float = 0.0) -> list[float]:
    axis = [first, second] + [0.0] * (_DIM - 2)
    norm = math.sqrt(sum(v * v for v in axis))
    return [v / norm for v in axis]


class _LimitSpy:
    """Wraps a LanceDB query builder, recording every limit() argument of the
    vector prefilter searches (get_chunk's plain one-row lookups are skipped)."""

    def __init__(self, query: Any, limits: list[int], record: bool) -> None:
        self._query = query
        self._limits = limits
        self._record = record

    def where(self, *args: Any, **kwargs: Any) -> "_LimitSpy":
        self._query = self._query.where(*args, **kwargs)
        return self

    def metric(self, *args: Any, **kwargs: Any) -> "_LimitSpy":
        self._query = self._query.metric(*args, **kwargs)
        return self

    def limit(self, n: int) -> "_LimitSpy":
        if self._record:
            self._limits.append(n)
        self._query = self._query.limit(n)
        return self

    def offset(self, n: int) -> "_LimitSpy":
        self._query = self._query.offset(n)
        return self

    def to_list(self) -> list[dict[str, Any]]:
        return self._query.to_list()


def _spy_search(table: Any, limits: list[int]):
    real = table.search

    def spy(*args: Any, **kwargs: Any) -> _LimitSpy:
        return _LimitSpy(real(*args, **kwargs), limits, record=bool(args))

    return spy


def _fail_if_called(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("near_duplicate must not paginate the full table")


def _bulk_rows(
    store: LanceDbEmbeddedStore,
    count: int,
    prefix: str,
    dense: Sequence[float],
    text: str,
    batch: int = 500,
) -> None:
    for start in range(0, count, batch):
        rows = [
            store._to_row(_make(f"{prefix}{i:04d}", f"{text} {i}"), dense, SparseVector((), ()))
            for i in range(start, min(start + batch, count))
        ]
        store._table.merge_insert("chunk_id").when_not_matched_insert_all().execute(rows)


def test_near_duplicate_finds_self_at_high_threshold(store):
    vector = _unit_vector(1.0)
    store.upsert_chunk(_make("a1", "alpha beta gamma"), vector, SparseVector((), ()))
    found = store.near_duplicate(vector, threshold=0.99, profile_id="alice")
    assert [chunk.chunk_id for chunk in found] == ["a1"]


def test_near_duplicate_dual_thresholds(store):
    """An exactly matching vector is found at the reinforce threshold (0.9),
    and a near-miss is only found once the threshold drops to the reconcile
    level — the two-sided gating (0.9 reinforce / 0.85 reconcile)."""
    exact = _unit_vector(1.0)
    near = _unit_vector(0.95, math.sqrt(1.0 - 0.95**2))  # cos(exact, near) == 0.95
    store.upsert_chunk(_make("exact", "verbatim duplicate"), exact, SparseVector((), ()))
    store.upsert_chunk(_make("near", "echoing duplicate"), near, SparseVector((), ()))

    reinforce = store.near_duplicate(exact, threshold=0.9, profile_id="alice")
    assert {chunk.chunk_id for chunk in reinforce} == {"exact", "near"}
    tight = store.near_duplicate(exact, threshold=0.97, profile_id="alice")
    assert {chunk.chunk_id for chunk in tight} == {"exact"}
    impossible = store.near_duplicate(exact, threshold=1.5, profile_id="alice")
    assert impossible == []


def test_near_duplicate_profile_isolation(store, embedder):
    _write(store, embedder, _make("a1", "alpha beta gamma"))
    _write(store, embedder, _make("b1", "alpha beta gamma", profile_id="bob"))
    vector = embedder.embed("alpha beta gamma").dense
    only_bob = store.near_duplicate(vector, threshold=0.8, profile_id="bob")
    assert [chunk.chunk_id for chunk in only_bob] == ["b1"]
    # default full-scan finds both profiles
    every = store.near_duplicate(vector, threshold=0.8)
    assert {chunk.chunk_id for chunk in every} == {"a1", "b1"}


def test_near_duplicate_empty_when_none_similar(store, embedder):
    _write(store, embedder, _make("a1", "alpha beta gamma"))
    unrelated = embedder.embed("xylophone zephyr quarter").dense
    assert store.near_duplicate(unrelated, threshold=0.95, profile_id="alice") == []


def test_near_duplicate_empty_table(store):
    assert store.near_duplicate([0.2] * _DIM, threshold=0.9) == []


def test_near_duplicate_uses_ann_prefilter_not_full_scan(store, monkeypatch):
    """The probe pre-filters via a capped top-K scan (limit <= widened K) and
    never pages the full table, even past the old 500-row page boundary."""
    probe = _unit_vector(1.0)
    noise = _unit_vector(0.0, 1.0)
    _bulk_rows(store, 1200, "n", noise, "noise row")
    store.upsert_chunk(_make("dup", "the duplicate"), probe, SparseVector((), ()))

    limits: list[int] = []
    monkeypatch.setattr(store._table, "search", _spy_search(store._table, limits))
    monkeypatch.setattr(store, "_paginate", _fail_if_called, raising=False)

    found = store.near_duplicate(probe, threshold=0.99, profile_id="alice")
    assert [chunk.chunk_id for chunk in found] == ["dup"]
    assert limits == [50]


def test_near_duplicate_widens_prefilter_when_kth_near_threshold(store, monkeypatch):
    """When the K-th top-K candidate's dense sim is within MARGIN of the
    threshold, K widens (x4 + 50) and re-scans so a true duplicate ranked
    beyond the initial K is still caught."""
    probe = _unit_vector(1.0)
    _bulk_rows(store, 60, "w", probe, "tied duplicate")

    before = getattr(_lancedb_embedded, "near_duplicate_widenings", lambda: -1)()
    limits: list[int] = []
    monkeypatch.setattr(store._table, "search", _spy_search(store._table, limits))

    found = store.near_duplicate(probe, threshold=0.9, profile_id="alice")

    assert limits == [50, 200]
    assert _lancedb_embedded.near_duplicate_widenings() == before + 1
    assert len(found) == 60


def test_near_duplicate_no_widening_when_kth_below_margin(store, monkeypatch):
    """A candidate set whose K-th dense sim sits below threshold - MARGIN
    keeps the top-K scan at K — widening only engages near the threshold."""
    probe = _unit_vector(1.0)
    nearish = _unit_vector(0.5, math.sqrt(1.0 - 0.5**2))
    _bulk_rows(store, 50, "c", nearish, "far candidate")
    store.upsert_chunk(_make("dup", "the duplicate"), probe, SparseVector((), ()))

    before = getattr(_lancedb_embedded, "near_duplicate_widenings", lambda: -1)()
    limits: list[int] = []
    monkeypatch.setattr(store._table, "search", _spy_search(store._table, limits))

    found = store.near_duplicate(probe, threshold=0.9, profile_id="alice")

    assert limits == [50]
    assert _lancedb_embedded.near_duplicate_widenings() == before
    assert [chunk.chunk_id for chunk in found] == ["dup"]


def test_near_duplicate_tie_break_pins_chunk_id_asc(store):
    """Exact-duplicate ties order dense-desc then chunk_id asc so the caller's
    band[0] cannot silently flip identity between identical chunks."""
    vector = _unit_vector(1.0)
    store.upsert_chunk(_make("b1", "duplicate b"), vector, SparseVector((), ()))
    store.upsert_chunk(_make("a1", "duplicate a"), vector, SparseVector((), ()))

    found = store.near_duplicate(vector, threshold=0.99, profile_id="alice")
    assert [chunk.chunk_id for chunk in found] == ["a1", "b1"]


def test_near_duplicate_prefilter_ranks_by_cosine_not_l2(store, monkeypatch):
    """The top-K scan must rank by cosine, not L2: a scaled duplicate (same
    direction, larger norm) ties cos=1.0 but is farthest in L2, so only the
    cosine metric keeps it inside the K window."""
    probe = _unit_vector(1.0)
    scaled_dup = [2.0] + [0.0] * (_DIM - 1)
    lure = _unit_vector(0.8, math.sqrt(1.0 - 0.8**2))
    _bulk_rows(store, 60, "l", lure, "lure")
    store.upsert_chunk(_make("dup", "scaled duplicate"), scaled_dup, SparseVector((), ()))

    limits: list[int] = []
    monkeypatch.setattr(store._table, "search", _spy_search(store._table, limits))

    found = store.near_duplicate(probe, threshold=0.99, profile_id="alice")

    assert [chunk.chunk_id for chunk in found] == ["dup"]
    assert limits == [50]


def test_near_duplicate_sorts_dense_desc(store):
    """Hits order dense-desc so the caller's band[0] is the strongest match —
    a flip to ascending would absorb into the worst near-dup."""
    probe = _unit_vector(1.0)
    exact = [1.0] + [0.0] * (_DIM - 1)
    mid = _unit_vector(0.95, math.sqrt(1.0 - 0.95**2))
    near = _unit_vector(0.86, math.sqrt(1.0 - 0.86**2))
    store.upsert_chunk(_make("near", "weakest echo"), near, SparseVector((), ()))
    store.upsert_chunk(_make("mid", "middle echo"), mid, SparseVector((), ()))
    store.upsert_chunk(_make("exact", "verbatim"), exact, SparseVector((), ()))

    found = store.near_duplicate(probe, threshold=0.8, profile_id="alice")

    assert [chunk.chunk_id for chunk in found] == ["exact", "mid", "near"]


def test_near_duplicate_widens_when_kth_inside_margin_band(store, monkeypatch):
    """A K-th candidate whose dense sim lands inside (threshold - MARGIN,
    threshold) must widen — the boundary case between the pinned 1.0 and 0.5
    sides of the window."""
    probe = _unit_vector(1.0)
    in_band = _unit_vector(0.87, math.sqrt(1.0 - 0.87**2))
    _bulk_rows(store, 60, "ib", in_band, "in-band candidate")

    before = getattr(_lancedb_embedded, "near_duplicate_widenings", lambda: -1)()
    limits: list[int] = []
    monkeypatch.setattr(store._table, "search", _spy_search(store._table, limits))

    found = store.near_duplicate(probe, threshold=0.9, profile_id="alice")

    assert limits == [50, 200]
    assert _lancedb_embedded.near_duplicate_widenings() == before + 1
    assert found == []


def test_near_duplicate_no_widening_when_kth_just_below_band(store, monkeypatch):
    """A K-th candidate just below threshold - MARGIN must not widen — the
    guard's lower edge, not just a far-below case."""
    probe = _unit_vector(1.0)
    below_band = _unit_vector(0.83, math.sqrt(1.0 - 0.83**2))
    _bulk_rows(store, 60, "bb", below_band, "below-band candidate")

    before = getattr(_lancedb_embedded, "near_duplicate_widenings", lambda: -1)()
    limits: list[int] = []
    monkeypatch.setattr(store._table, "search", _spy_search(store._table, limits))

    found = store.near_duplicate(probe, threshold=0.9, profile_id="alice")

    assert limits == [50]
    assert _lancedb_embedded.near_duplicate_widenings() == before
    assert found == []


def test_near_duplicate_builds_stamps_without_get_chunk_roundtrip(store, monkeypatch):
    """B6 round-trip elimination: the probe already returns full rows, so a
    match must be reconstructed from those rows — never re-read through
    ``get_chunk`` per match (the pre-fix N-round-trip cost per drain)."""
    vector = _unit_vector(1.0)
    store.upsert_chunk(_make("a1", "alpha beta gamma"), vector, SparseVector((), ()))
    store.upsert_chunk(_make("a2", "alpha beta delta"), vector, SparseVector((), ()))

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("near_duplicate must not re-read matches via get_chunk")

    monkeypatch.setattr(store, "get_chunk", _explode)

    found = store.near_duplicate(vector, threshold=0.9, profile_id="alice")
    assert [chunk.chunk_id for chunk in found] == ["a1", "a2"]
    assert found[0].text == "alpha beta gamma"  # full stamp reconstructed from probe rows


def test_near_duplicate_ranked_returns_similarity_pairs(store):
    """B6 single-probe: near_duplicate_ranked returns (chunk, similarity) pairs
    sorted by similarity desc then chunk_id asc — one probe at the conflict
    threshold serves both the strong and the band near-duplicate sets."""
    exact = _unit_vector(1.0)
    near = _unit_vector(0.95, math.sqrt(1.0 - 0.95**2))
    store.upsert_chunk(_make("near", "echoing duplicate"), near, SparseVector((), ()))
    store.upsert_chunk(_make("exact", "verbatim duplicate"), exact, SparseVector((), ()))

    ranked = store.near_duplicate_ranked(exact, threshold=0.85, profile_id="alice")

    assert [chunk.chunk_id for chunk, _ in ranked] == ["exact", "near"]
    exact_sim = ranked[0][1]
    near_sim = ranked[1][1]
    assert exact_sim == pytest.approx(1.0, abs=1e-6)
    assert near_sim == pytest.approx(0.95, abs=1e-6)
    assert exact_sim >= near_sim


def test_near_duplicate_ranked_filters_below_threshold(store):
    store.upsert_chunk(_make("exact", "verbatim duplicate"), _unit_vector(1.0), SparseVector((), ()))
    unrelated = _unit_vector(0.0, 1.0)
    assert store.near_duplicate_ranked(unrelated, threshold=0.9, profile_id="alice") == []


def test_upsert_chunks_bulk_commits_all_rows(store, embedder):
    """B6 batch write: one upsert_chunks call persists every entry in a single
    merge commit (no per-turn lock/commit round-trips)."""
    a = embedder.embed("alpha beta gamma")
    b = embedder.embed("alpha beta delta")
    store.upsert_chunks(
        [
            (_make("b1", "alpha beta gamma"), a.dense, a.sparse),
            (_make("b2", "alpha beta delta"), b.dense, b.sparse),
        ]
    )
    assert store.get_chunk("b1").text == "alpha beta gamma"
    assert store.get_chunk("b2").text == "alpha beta delta"


# ---------------------------------------------------------------- snapshot / lifecycle


def test_snapshot_read_pins_committed_version(store, embedder):
    _write(store, embedder, _make("a1", "alpha beta gamma"))
    _write(store, embedder, _make("a2", "alpha beta delta"))

    pinned = store._db.open_table("chunks", version=store._table.version)
    fresh = store.snapshot_read(ChunkFilter(profile_id="alice"))
    assert {chunk.chunk_id for chunk in fresh} == {"a1", "a2"}

    # a later write bumps the version; the pinned handle stays frozen
    _write(store, embedder, _make("a9", "conflicting afterthought"))
    after_write = store.snapshot_read(ChunkFilter(profile_id="alice"))
    assert {chunk.chunk_id for chunk in after_write} == {"a1", "a2", "a9"}

    pinned_rows = pinned.search().limit(20).to_list()
    assert {row["chunk_id"] for row in pinned_rows} == {"a1", "a2"}


def test_mark_consolidated_batch(store, embedder):
    _write(store, embedder, _make("c1", "alpha beta gamma"))
    _write(store, embedder, _make("c2", "alpha beta delta"))
    _write(store, embedder, _make("c3", "rho sigma tau"))
    store.mark_consolidated(["c1", "c2"])

    materials = store.list_chunks(ChunkFilter(profile_id="alice", consolidated=True), Page(limit=10))
    assert {chunk.chunk_id for chunk in materials.items} == {"c1", "c2"}
    live = store.list_chunks(ChunkFilter(profile_id="alice"), Page(limit=10))
    assert {chunk.chunk_id for chunk in live.items} == {"c1", "c2", "c3"}


def test_mark_consolidated_empty_and_unknown(store):
    store.mark_consolidated([])
    store.mark_consolidated(["never-written"])


def test_update_weights_batch(store, embedder):
    _write(store, embedder, _make("a1", "alpha beta gamma"))
    _write(store, embedder, _make("a2", "alpha beta delta"))
    store.update_weights(
        [
            WeightUpdate(chunk_id="a1", decay_weight=0.3, reinforce_count=2),
            WeightUpdate(chunk_id="a2", last_reinforced=500.0),
        ]
    )
    first = store.get_chunk("a1")
    assert first is not None
    assert first.decay_weight == pytest.approx(0.3)
    second = store.get_chunk("a2")
    assert second is not None


def test_update_weights_partial_fields(store, embedder):
    _write(store, embedder, _make("a1", "alpha beta gamma"))
    store.update_weights([WeightUpdate(chunk_id="a1", decay_weight=0.0)])
    assert store.get_chunk("a1").decay_weight == pytest.approx(0.0)


# ---------------------------------------------------------------- chunk state


def _raw_row(store: LanceDbEmbeddedStore, chunk_id: str) -> dict:
    rows = store._table.search().where(f"chunk_id = '{chunk_id}'").limit(1).to_list()
    return rows[0] if rows else {}


def test_update_chunk_state_hit_increment_refreshes_last_hit_at(store, embedder):
    _write(store, embedder, _make("m1", "alpha beta gamma"))
    store.update_chunk_state(["m1"], hit_increment=3)
    row = _raw_row(store, "m1")
    assert row["hit_count"] == 3
    assert row["last_hit_at"] is not None and row["last_hit_at"] > 0.0


def test_update_chunk_state_sets_and_clears_reconcile_flag(store, embedder):
    _write(store, embedder, _make("m2", "alpha beta gamma"))
    store.update_chunk_state(["m2"], needs_reconcile=True)
    assert _raw_row(store, "m2")["needs_reconcile"] is True
    store.update_chunk_state(["m2"], needs_reconcile=False)
    assert _raw_row(store, "m2")["needs_reconcile"] is False


def test_update_chunk_state_batches_multiple_ids(store, embedder):
    _write(store, embedder, _make("m3", "alpha beta gamma"))
    _write(store, embedder, _make("m4", "alpha beta delta"))
    _write(store, embedder, _make("m5", "rho sigma tau"))
    store.update_chunk_state(["m3", "m4"], hit_increment=2, needs_reconcile=True)
    for cid in ("m3", "m4"):
        row = _raw_row(store, cid)
        assert row["hit_count"] == 2
        assert row["needs_reconcile"] is True
    untouched = _raw_row(store, "m5")
    assert untouched["hit_count"] == 0
    assert untouched["needs_reconcile"] is False


def test_update_chunk_state_ignores_unknown_ids_silently(store, embedder):
    _write(store, embedder, _make("m6", "alpha beta gamma"))
    store.update_chunk_state(["never-written"], hit_increment=1, needs_reconcile=True)
    row = _raw_row(store, "m6")
    assert row["hit_count"] == 0
    assert row["needs_reconcile"] is False


def test_update_chunk_state_noop_cases(store, embedder):
    _write(store, embedder, _make("m7", "alpha beta gamma"))
    store.update_chunk_state(["m7"])  # neither argument
    store.update_chunk_state([], hit_increment=1)  # empty batch
    row = _raw_row(store, "m7")
    assert row["hit_count"] == 0
    assert row["last_hit_at"] is None


def test_update_chunk_state_zero_hit_is_noop(store, embedder):
    _write(store, embedder, _make("m8", "alpha beta gamma"))
    store.update_chunk_state(["m8"], hit_increment=0)
    row = _raw_row(store, "m8")
    assert row["hit_count"] == 0
    assert row["last_hit_at"] is None


def test_purge_range_deletes_only_overlapping_turns(store, embedder):
    _write_turn_row(store, embedder, "t1", 100, 150)  # overlaps [120, 200]
    _write_turn_row(store, embedder, "t2", 300, 400)  # disjoint
    _write_turn_row(store, embedder, "t3", 130, 140)  # fully inside
    _write(store, embedder, _make("n1", "no turn bounds at all"))  # untouchable

    removed = store.purge_range("conv-7", turn_start=120, turn_end=200)
    assert removed == 2

    remaining = [
        chunk.chunk_id for chunk in store.list_chunks(ChunkFilter(profile_id="alice"), Page(limit=10)).items
    ]
    assert "t1" not in remaining
    assert "t3" not in remaining
    assert "t2" in remaining
    assert "n1" in remaining


def test_purge_range_unknown_session(store, embedder):
    _write_turn_row(store, embedder, "t1", 100, 150)
    assert store.purge_range("other-session", turn_start=0, turn_end=10_000) == 0


def test_purge_range_deletes_funnel_chunk_by_turn_window(store, embedder):
    # A funnel-written chunk (turn bounds filled by the stamp-writer) must be
    # targetable by purge_range through the public write surface, not only via
    # raw row injection.
    _write(store, embedder, _make("f1", "funnel chunk", turn_start=10, turn_end=10))
    _write(store, embedder, _make("f2", "later funnel chunk", turn_start=50, turn_end=50))
    _write(store, embedder, _make("plain", "no turn bounds"))

    assert store.purge_range("s1", turn_start=10, turn_end=10) == 1

    remaining = [
        chunk.chunk_id for chunk in store.list_chunks(ChunkFilter(profile_id="alice"), Page(limit=10)).items
    ]
    assert "f1" not in remaining
    assert "f2" in remaining
    assert "plain" in remaining


# ---------------------------------------------------------------- list / pagination


def test_list_chunks_orders_by_ingested_at_desc_and_paginates(store, embedder):
    _write(store, embedder, _make("oldest", "alpha beta gamma", ingested_at=10.0))
    _write(store, embedder, _make("middle", "alpha beta gamma", ingested_at=20.0))
    _write(store, embedder, _make("newest", "alpha beta gamma", ingested_at=30.0))

    first_page = store.list_chunks(ChunkFilter(profile_id="alice"), Page(offset=0, limit=2))
    assert [chunk.chunk_id for chunk in first_page.items] == ["newest", "middle"]
    assert first_page.total == 3
    assert first_page.offset == 0
    assert first_page.limit == 2

    second_page = store.list_chunks(ChunkFilter(profile_id="alice"), Page(offset=2, limit=2))
    assert [chunk.chunk_id for chunk in second_page.items] == ["oldest"]
    assert second_page.total == 3


def test_list_chunks_respects_filter_and_profile_isolation(store, embedder):
    _write(store, embedder, _make("a1", "alpha beta gamma", entities=("math",)))
    _write(store, embedder, _make("a2", "alpha beta delta", consolidated=True))
    _write(store, embedder, _make("b1", "alpha beta gamma", profile_id="bob"))

    math_only = store.list_chunks(ChunkFilter(profile_id="alice", entities=("math",)), Page(limit=10))
    assert [chunk.chunk_id for chunk in math_only.items] == ["a1"]

    cons_only = store.list_chunks(ChunkFilter(profile_id="alice", consolidated=True), Page(limit=10))
    assert [chunk.chunk_id for chunk in cons_only.items] == ["a2"]

    bob_only = store.list_chunks(ChunkFilter(profile_id="bob"), Page(limit=10))
    assert [chunk.chunk_id for chunk in bob_only.items] == ["b1"]


# ---------------------------------------------------------------- concurrent writes


_CONCURRENT_WORKERS = 8
_CONCURRENT_OPS = 10
_OP_LATENCY_BUDGET_SECONDS = 4.0
_WRITE_STORM_BUDGET_SECONDS = 30.0


def test_concurrent_mixed_writes_exact_counts_no_errors(tmp_path):
    """One shared store, 8 threads x 10 mixed ops (upsert + read + weight/state
    updates) must hold exact final counts, zero exceptions, and no multi-second
    per-op stall. Count/exception assertions alone cannot trip the defect:
    LanceDB falls back to directory listing when concurrent commits collide on
    latest_version_hint.json, so the data stays exact -- but the collisions
    drain lance's single background loop and turn individual ops into
    multi-second stalls, so asserting the tail per-op latency is what catches
    the un-serialized write path."""
    store = LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=_DIM)
    embedder = SyntheticEmbedder(dimension=_DIM)
    errors: list[Exception] = []
    finished: list[int] = []
    max_op: list[float] = []
    guard = threading.Lock()

    def worker(w: int) -> None:
        local_max = 0.0
        try:
            for i in range(_CONCURRENT_OPS):
                chunk_id = f"cw{w:02d}-{i:03d}"
                text = f"concurrent worker {w} op {i}"
                start = time.monotonic()
                result = embedder.embed(text)
                store.upsert_chunk(_make(chunk_id, text), result.dense, result.sparse)
                if i % 4 == 0:
                    seen = store.get_chunk(chunk_id)
                    if seen is None:
                        raise AssertionError(f"worker {w} did not read back its own write {chunk_id}")
                store.update_chunk_state([chunk_id], hit_increment=1)
                store.update_weights([WeightUpdate(chunk_id=chunk_id, decay_weight=0.5)])
                local_max = max(local_max, time.monotonic() - start)
            with guard:
                max_op.append(local_max)
                finished.append(w)
        except Exception as exc:  # pragma: no cover - failure path
            with guard:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(w,), daemon=True, name=f"lance-write-{w}")
        for w in range(_CONCURRENT_WORKERS)
    ]
    # One shared deadline across every join guards against a wedged store; the
    # discriminating assert below is the per-op tail latency, not the total.
    started = time.monotonic()
    deadline = started + _WRITE_STORM_BUDGET_SECONDS
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    elapsed = time.monotonic() - started
    stragglers = [w for w, thread in enumerate(threads) if thread.is_alive()]

    assert errors == []
    assert stragglers == [], (
        f"workers {stragglers} still writing after {elapsed:.1f}s (budget {_WRITE_STORM_BUDGET_SECONDS}s)"
    )
    assert sorted(finished) == list(range(_CONCURRENT_WORKERS))
    slowest = max(max_op)
    assert slowest <= _OP_LATENCY_BUDGET_SECONDS, (
        f"slowest mixed op took {slowest * 1000:.0f}ms (budget {_OP_LATENCY_BUDGET_SECONDS}s)"
    )
    total = store.list_chunks(ChunkFilter(profile_id="alice"), Page(limit=1_000)).total
    assert total == _CONCURRENT_WORKERS * _CONCURRENT_OPS
    for w in range(_CONCURRENT_WORKERS):
        row = _raw_row(store, f"cw{w:02d}-000")
        assert row["hit_count"] == 1
        assert row["decay_weight"] == pytest.approx(0.5)
    asyncio.run(store.close())


def test_distinct_profile_ids_covers_every_namespace(store, embedder):
    """Observational read for the doctor's unknown-profile check (#110):
    every captured namespace is enumerated across profiles."""
    assert store.distinct_profile_ids() == set()
    _write(store, embedder, _make("c-a", "alpha beta gamma", profile_id="alice"))
    _write(store, embedder, _make("c-b", "delta epsilon zeta", profile_id="bob"))
    _write(store, embedder, _make("c-c", "eta theta iota", profile_id="alice"))
    assert store.distinct_profile_ids() == {"alice", "bob"}
