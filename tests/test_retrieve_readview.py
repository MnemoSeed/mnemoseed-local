"""PRD-448 §8.2.3 read-side lexical-shadow view: the search-only override with
copy-discipline enrichment, passthrough reads, monotonic beta, the rescue-band
fixture (N-2 obligation: shadow pushes a below-floor explicit pin over
rescue_cue_min) and the fail-closed observability counter."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterator
from pathlib import Path

import pytest

from mnemoseed_local.config import load_config
from mnemoseed_local.daemon.memory import MemoryService
from mnemoseed_local.daemon.observability import Observability
from mnemoseed_local.retrieve import readview
from mnemoseed_local.retrieve.cues import extract_cues, lexical_shadow_tokens
from mnemoseed_local.retrieve.hybrid import HybridConfig, HybridRetriever, _chunk_cue_overlap
from mnemoseed_local.retrieve.readview import RecallVectorView
from mnemoseed_local.schema.stamp import (
    EXPLICIT_PIN_SOURCE,
    ChunkStamp,
    CognitiveTier,
    Cues,
    Provenance,
)
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.factory import Stores, build_stores
from mnemoseed_local.storage.ports import ChunkFilter, Page, SearchHit, StoredProfile
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_DIM = 64
_PROFILE = "alice"
_PIN_TEXT = "atlasdb index rebuild ran clean overnight"
_QUERY = "AtlasDb index status"


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not VECTOR_DRIVERS.contains("lancedb_embedded"):
        register(VECTOR_DRIVERS)(LanceDbEmbeddedStore)
    if not GRAPH_DRIVERS.contains("sqlite_graph"):
        register(GRAPH_DRIVERS)(SqliteGraphDriver)
    if not META_DRIVERS.contains("sqlite_meta"):
        register(META_DRIVERS)(SqliteMetaDriver)
    if not EMBED_DRIVERS.contains("synthetic"):
        register(EMBED_DRIVERS)(SyntheticEmbedder)
    yield


def _chunk(
    chunk_id: str,
    text: str,
    *,
    decay: float = 1.0,
    entities: tuple[str, ...] = (),
    source: str = "manual",
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id="p1",
        cues=Cues(entities=list(entities)),
        provenance=Provenance(
            asserted_by="user" if source == EXPLICIT_PIN_SOURCE else "test-model",
            session_id=None if source == EXPLICIT_PIN_SOURCE else "s1",
            source=source,
            confidence=0.8,
            asserted_at=100.0,
        ),
        decay_weight=decay,
        score=0.5,
        ingested_at=100.0,
        turn_start=1,
        turn_end=2,
    )


def _store(tmp_path: Path) -> LanceDbEmbeddedStore:
    return LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=_DIM)


def _upsert(store: LanceDbEmbeddedStore, embedder: SyntheticEmbedder, stamp: ChunkStamp) -> None:
    embedding = embedder.embed(stamp.text)
    store.upsert_chunk(stamp, embedding.dense, embedding.sparse)


class _SpyStore:
    """Records search calls and returns the SAME hit objects every call —
    the shared-object hazard the copy discipline must survive."""

    def __init__(self, inner: LanceDbEmbeddedStore) -> None:
        self._inner = inner
        self.search_calls = 0
        self.cached: list[SearchHit] | None = None

    def search(
        self,
        dense: list[float],
        sparse: object,
        filter: ChunkFilter,
        top_k: int,
    ) -> list[SearchHit]:
        self.search_calls += 1
        if self.cached is None:
            self.cached = self._inner.search(dense, sparse, filter, top_k)
        return list(self.cached)

    def __getattr__(self, name: str):  # noqa: ANN204 - test spy
        return getattr(self._inner, name)


def _seed_pin(store: LanceDbEmbeddedStore, embedder: SyntheticEmbedder) -> ChunkStamp:
    stamp = _chunk("pin", _PIN_TEXT, decay=1.0)
    _upsert(store, embedder, stamp)
    return stamp


def test_view_enriches_without_mutating_shared_inner_stamps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    embedder = SyntheticEmbedder(dimension=_DIM)
    stamp = _seed_pin(store, embedder)
    spy = _SpyStore(store)
    view = RecallVectorView(spy)
    dense = list(embedder.embed(_QUERY).dense)
    warm = spy.search(dense, None, ChunkFilter(profile_id=_PROFILE), 10)
    inner_before = [hit.chunk.model_dump() for hit in warm]
    assert all(hit.chunk.cues.entities == [] for hit in warm)

    first = view.search(dense, None, ChunkFilter(profile_id=_PROFILE), 10)
    second = view.search(dense, None, ChunkFilter(profile_id=_PROFILE), 10)

    # enrichment: distinct stamp objects, entities grown by the shadow tokens
    for hits in (first, second):
        assert all(hit.chunk is not stamp for hit in hits)
        assert all(hit.chunk is not warm[0].chunk for hit in hits)
        entities = hits[0].chunk.cues.entities
        assert "atlasdb" in entities
        assert "rebuild" in entities
        assert entities != []
    assert [hit.chunk.model_dump() for hit in first] == [hit.chunk.model_dump() for hit in second]
    # the inner store's stamps are byte-identical after the view ran
    assert inner_before == [hit.chunk.model_dump() for hit in spy.cached]  # type: ignore[union-attr]


def test_view_similarity_is_a_passthrough(tmp_path: Path) -> None:
    store = _store(tmp_path)
    embedder = SyntheticEmbedder(dimension=_DIM)
    _seed_pin(store, embedder)
    spy = _SpyStore(store)
    view = RecallVectorView(spy)
    dense = list(embedder.embed(_QUERY).dense)
    inner_hits = spy.search(dense, None, ChunkFilter(profile_id=_PROFILE), 10)
    viewed_hits = view.search(dense, None, ChunkFilter(profile_id=_PROFILE), 10)
    assert [hit.similarity for hit in viewed_hits] == [hit.similarity for hit in inner_hits]
    assert [hit.chunk.chunk_id for hit in viewed_hits] == [hit.chunk.chunk_id for hit in inner_hits]


def test_view_reads_pass_through_unenriched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    embedder = SyntheticEmbedder(dimension=_DIM)
    stamp = _seed_pin(store, embedder)
    view = RecallVectorView(store)
    read = view.snapshot_read(ChunkFilter(profile_id=_PROFILE))
    assert [chunk.cues.entities for chunk in read] == [[]]
    paged = view.list_chunks(ChunkFilter(profile_id=_PROFILE), Page(offset=0, limit=10))
    assert [chunk.cues.entities for chunk in paged.items] == [[]]
    assert view.get_chunk(stamp.chunk_id) is not None
    assert view.distinct_profile_ids() == {_PROFILE}


def test_shadow_failure_skips_enrichment_and_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    embedder = SyntheticEmbedder(dimension=_DIM)
    _seed_pin(store, embedder)
    spy = _SpyStore(store)
    observability = Observability()
    view = RecallVectorView(spy, observability=observability)

    def boom(text: str, config: object = None) -> tuple[str, ...]:
        raise RuntimeError("shadow failure")

    monkeypatch.setattr(readview, "lexical_shadow_tokens", boom)
    dense = list(embedder.embed(_QUERY).dense)
    hits = view.search(dense, None, ChunkFilter(profile_id=_PROFILE), 10)
    assert hits == spy.cached
    assert all(hit.chunk.cues.entities == [] for hit in hits)
    assert observability.bridge_shadow_fallback_total() == 1
    view.search(dense, None, ChunkFilter(profile_id=_PROFILE), 10)
    assert observability.bridge_shadow_fallback_total() == 2


def test_view_without_observability_survives_shadow_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    embedder = SyntheticEmbedder(dimension=_DIM)
    _seed_pin(store, embedder)
    spy = _SpyStore(store)
    view = RecallVectorView(spy, observability=None)

    def boom(text: str, config: object = None) -> tuple[str, ...]:
        raise RuntimeError("shadow failure")

    monkeypatch.setattr(readview, "lexical_shadow_tokens", boom)
    dense = list(embedder.embed(_QUERY).dense)
    hits = view.search(dense, None, ChunkFilter(profile_id=_PROFILE), 10)
    assert hits == spy.cached
    assert all(hit.chunk.cues.entities == [] for hit in hits)


def test_monotonicity_shadow_never_lowers_beta() -> None:
    rng = random.Random(20261121)
    vocabulary = ("AtlasDb", "loader", "索引", "nightly", "backup", "rebuild")
    shadow_source = "atlasdb rebuild nightly archival workload storage"
    for _ in range(200):
        query_entities = tuple(rng.choice(vocabulary) for _ in range(rng.randint(1, 4)))
        stored_entities = tuple(rng.choice(vocabulary) for _ in range(rng.randint(0, 3)))
        chunk = _chunk("c", shadow_source, entities=stored_entities)
        enriched_entities = list(stored_entities)
        seen = {entity.casefold() for entity in enriched_entities}
        for token in readview.lexical_shadow_tokens(shadow_source, None):
            if token.casefold() not in seen:
                seen.add(token.casefold())
                enriched_entities.append(token)
        enriched = _chunk("c", shadow_source, entities=tuple(enriched_entities))
        query = Cues(entities=list(query_entities))
        assert _chunk_cue_overlap(query, enriched) >= _chunk_cue_overlap(query, chunk) - 1e-12


def test_rescue_band_shadow_pushes_pin_over_cue_min(tmp_path: Path) -> None:
    """N-2 obligation: a below-floor explicit pin whose stored cues are empty
    (all-lowercase text the extractor never tags) stays unserved on the raw
    store and is rescued once the view enriches the stored side."""
    store = _store(tmp_path)
    graph = SqliteGraphDriver(path=tmp_path / "graph.db")
    embedder = SyntheticEmbedder(dimension=_DIM)
    pin = _chunk("pin", _PIN_TEXT, decay=0.3, source=EXPLICIT_PIN_SOURCE)
    _upsert(store, embedder, pin)
    retriever = HybridRetriever(HybridConfig())
    cues = extract_cues(_QUERY)

    raw = retriever.recall(
        _QUERY,
        cues,
        profile_id=_PROFILE,
        vector_store=store,
        graph_store=graph,
        embedder=embedder,
    )
    assert all(candidate.id != "pin" for candidate in raw.candidates)

    viewed = retriever.recall(
        _QUERY,
        cues,
        profile_id=_PROFILE,
        vector_store=RecallVectorView(store),
        graph_store=graph,
        embedder=embedder,
    )
    assert any(candidate.id == "pin" for candidate in viewed.candidates)
    asyncio.run(store.close())
    asyncio.run(graph.close())


# ------------------------------------------------- service-level wiring oracle

_SERVICE_TOKEN = "mercurypilot"
_SERVICE_PIN_TEXT = "mercurypilot deploy log ran clean overnight"
_SERVICE_QUERY = "MercuryPilot status"


@pytest.fixture
def service_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[MemoryService, Stores]]:
    """One MemoryService over fresh embedded stores — the daemon boot wiring
    shape (build_stores + MemoryService), no HTTP surface involved."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = {_DIM}\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = {_DIM}\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config = load_config(cfg)
    stores = build_stores(config)
    stores.meta.upsert_profile(StoredProfile(profile_id=_PROFILE))
    service = MemoryService(stores, config)
    yield service, stores
    service.close()
    asyncio.run(stores.close())


def test_service_recall_wiring_rescues_shadow_only_pin(
    service_rig: tuple[MemoryService, Stores], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service-level oracle for the recall wiring (PRD-448 §8.2.3): recall must
    read the vector store through the lexical-shadow view. A below-floor
    explicit pin whose only cue-bearing token is lowercase English — invisible
    to the capture-path extractor — is rescued only through the enriched view;
    a recall wired to the raw store must miss it. The raw-store leg runs
    first: a served rescue rebinds the pin's decay over the floor, which would
    erase the rescue-band discrimination on the second leg."""
    service, stores = service_rig
    pin = _chunk("pin", _SERVICE_PIN_TEXT, decay=0.3, source=EXPLICIT_PIN_SOURCE)
    _upsert(stores.vector, stores.embed, pin)
    extracted = {entity.casefold() for entity in extract_cues(_SERVICE_PIN_TEXT).cues.entities}
    assert _SERVICE_TOKEN not in extracted
    assert _SERVICE_TOKEN in {token.casefold() for token in lexical_shadow_tokens(_SERVICE_PIN_TEXT)}

    wired_view = service._recall_view
    monkeypatch.setattr(service, "_recall_view", stores.vector)
    missed = service.recall(profile_id=_PROFILE, query=_SERVICE_QUERY)
    assert all(entry["id"] != "pin" for entry in missed["memory"]["entries"])

    monkeypatch.setattr(service, "_recall_view", wired_view)
    served = service.recall(profile_id=_PROFILE, query=_SERVICE_QUERY)
    rescued = [entry for entry in served["memory"]["entries"] if entry["id"] == "pin"]
    assert len(rescued) == 1
    assert "rescued" in rescued[0]["flags"]
