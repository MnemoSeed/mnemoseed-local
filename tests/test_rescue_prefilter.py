"""Route (b) rescue prefilter (design/09 §3.5, QA IMPORTANT-2): an additive
pin-class flag in the vector-track metadata so the STORAGE prefilter itself
excludes sub-floor non-pin chunks — the relaxed band never lets faded ordinary
chunks consume the vector top-K window. The joint post-filter condition in the
hybrid retriever stays as defense-in-depth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mnemoseed_local.retrieve.hybrid import ExtractedCues  # noqa: F401  (re-export check)
from mnemoseed_local.schema.stamp import (
    EXPLICIT_PIN_SOURCE,
    ChunkStamp,
    CognitiveTier,
    Cues,
    Provenance,
)
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import Capability, ChunkFilter

_DIM = 64
_PROFILE = "p1"


@pytest.fixture
def store(tmp_path: Path) -> LanceDbEmbeddedStore:
    return LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=_DIM)


def _stamp(chunk_id: str, text: str, *, source: str, decay: float) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test",
        cues=Cues(entities=["AtlasDb"]),
        provenance=Provenance(
            asserted_by="user" if source == EXPLICIT_PIN_SOURCE else "capture",
            session_id=None if source == EXPLICIT_PIN_SOURCE else "s1",
            source=source,
            confidence=1.0,
        ),
        decay_weight=decay,
    )


def _seed(store: LanceDbEmbeddedStore, stamp: ChunkStamp) -> None:
    embedding = SyntheticEmbedder(dimension=_DIM).embed(stamp.text)
    store.upsert_chunk(stamp, embedding.dense, embedding.sparse)


def test_metadata_view_carries_the_pin_flag() -> None:
    """The documented flat metadata view exposes the additive flag so every
    driver can materialize it."""
    pin = _stamp("pin", "pin body", source=EXPLICIT_PIN_SOURCE, decay=1.0)
    plain = _stamp("plain", "plain body", source="capture.session", decay=1.0)

    assert pin.metadata_filter_view()["explicit_pin"] is True
    assert plain.metadata_filter_view()["explicit_pin"] is False


def test_prefilter_two_band_search_admits_only_pins_below_main_floor(store) -> None:
    """The storage prefilter itself expresses 'relax pins only': below the main
    floor only the flashbulb pin survives the search, the faded capture chunk
    never enters the candidate window."""
    _seed(store, _stamp("aged-pin", "AtlasDb keeps the export warm", source=EXPLICIT_PIN_SOURCE, decay=0.2))
    _seed(store, _stamp("faded-plain", "AtlasDb fades like any capture", source="capture.session", decay=0.2))

    embedder = SyntheticEmbedder(dimension=_DIM)
    query = embedder.embed("AtlasDb")
    hits = store.search(
        query.dense,
        query.sparse,
        ChunkFilter(profile_id=_PROFILE, min_decay=0.4, pin_min_decay=0.15),
        top_k=10,
    )

    ids = {hit.chunk.chunk_id for hit in hits}
    assert "aged-pin" in ids
    assert "faded-plain" not in ids


def test_pin_floor_clause_covers_legacy_rows_without_the_flag(store) -> None:
    """Rows written before the denormalized flag existed carry NULL; the pin
    clause falls back to the authoritative provenance source so legacy pins
    stay rescuable while legacy non-pins stay excluded."""
    # simulate the pre-flag era: strip the column value back to NULL
    _seed(store, _stamp("legacy-pin", "AtlasDb keeps the export warm", source=EXPLICIT_PIN_SOURCE, decay=0.2))
    table = store._table
    table.update(where="chunk_id = 'legacy-pin'", values={"explicit_pin": None})

    embedder = SyntheticEmbedder(dimension=_DIM)
    query = embedder.embed("AtlasDb")
    hits = store.search(
        query.dense,
        query.sparse,
        ChunkFilter(profile_id=_PROFILE, min_decay=0.4, pin_min_decay=0.15),
        top_k=10,
    )

    assert {hit.chunk.chunk_id for hit in hits} == {"legacy-pin"}
    asyncio.run(store.close())


def test_default_filter_without_pin_floor_is_unchanged(store) -> None:
    """No ``pin_min_decay`` set -> byte-identical semantics to main: the plain
    min_decay clause applies to everyone."""
    sql = store._filter_sql(ChunkFilter(profile_id=_PROFILE, min_decay=0.4))
    assert "decay_weight >= 0.4" in sql
    assert "provenance" not in sql
    asyncio.run(store.close())


def test_capability_surface_unchanged(store) -> None:
    assert Capability.VECTOR_METADATA_FILTER in store.capabilities()
