"""PRD-448 read-side lexical-shadow view over the vector store.

``RecallVectorView`` implements the ``VectorStore`` port over a real store and
overrides ``search()`` only: the inner search runs untouched, then each
returned stamp is enriched with ``lexical_shadow_tokens`` of its own verbatim
text under the copy discipline — every enriched hit carries a fresh
``ChunkStamp`` built with ``model_copy``; inner-store objects are never
mutated and nothing is persisted. The stored side of the cue-overlap join
grows, so the beta entity component is monotonic non-decreasing and a
below-floor explicit pin can cross the rescue band (encoding specificity:
strong cues retrieve weak traces).

All other port methods pass through unchanged — ``snapshot_read`` stays
unenriched so the freshness probe is byte-stable. The view is stateless and
constructed once per service lifetime; shadow failures degrade fail-closed
(the original hits return, the observability counter moves, recall proceeds).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Protocol

from mnemoseed_local.retrieve.cues import CueConfig, lexical_shadow_tokens
from mnemoseed_local.schema.stamp import ChunkStamp
from mnemoseed_local.storage.ports import (
    Capability,
    ChunkFilter,
    DriverInfo,
    Page,
    PageResult,
    SearchHit,
    SparseVector,
    VectorStore,
    WeightUpdate,
)


class ShadowFallbackSink(Protocol):
    """The observability surface the view needs (structurally satisfied)."""

    def note_bridge_shadow_fallback(self) -> None: ...


class RecallVectorView:
    """Read-side shadow-enrichment decorator over one vector store."""

    info: ClassVar[DriverInfo] = DriverInfo(
        name="recall_vector_view",
        capabilities=frozenset(),
        description="read-side lexical-shadow view over a vector store",
    )

    def __init__(
        self,
        inner: VectorStore,
        *,
        observability: ShadowFallbackSink | None = None,
        cue_config: CueConfig | None = None,
    ) -> None:
        self._inner = inner
        self._observability = observability
        self._cue_config = cue_config

    @property
    def inner(self) -> VectorStore:
        return self._inner

    # ------------------------------------------------------------ the override

    def search(
        self,
        dense: Sequence[float],
        sparse: SparseVector | None,
        filter: ChunkFilter,
        top_k: int,
    ) -> list[SearchHit]:
        hits = self._inner.search(dense, sparse, filter, top_k)
        try:
            enriched = [SearchHit(chunk=self._enriched(hit.chunk), similarity=hit.similarity) for hit in hits]
        except Exception:
            if self._observability is not None:
                self._observability.note_bridge_shadow_fallback()
            return hits
        return enriched

    def _enriched(self, chunk: ChunkStamp) -> ChunkStamp:
        entities = list(chunk.cues.entities)
        seen = {entity.casefold() for entity in entities}
        for token in lexical_shadow_tokens(chunk.text, self._cue_config):
            folded = token.casefold()
            if folded not in seen:
                seen.add(folded)
                entities.append(token)
        return chunk.model_copy(update={"cues": chunk.cues.model_copy(update={"entities": entities})})

    # ------------------------------------------------------------ passthrough

    def capabilities(self) -> frozenset[Capability]:
        return self._inner.capabilities()

    def upsert_chunk(
        self,
        chunk: ChunkStamp,
        dense: Sequence[float],
        sparse: SparseVector | None = None,
    ) -> None:
        self._inner.upsert_chunk(chunk, dense, sparse)

    def upsert_chunks(
        self,
        entries: Sequence[tuple[ChunkStamp, Sequence[float], SparseVector | None]],
    ) -> None:
        self._inner.upsert_chunks(entries)

    def get_chunk(self, chunk_id: str) -> ChunkStamp | None:
        return self._inner.get_chunk(chunk_id)

    def delete_chunk(self, chunk_id: str) -> None:
        self._inner.delete_chunk(chunk_id)

    def near_duplicate(self, vector: Sequence[float], threshold: float, profile_id: str) -> list[ChunkStamp]:
        return self._inner.near_duplicate(vector, threshold, profile_id)

    def near_duplicate_ranked(
        self, vector: Sequence[float], threshold: float, profile_id: str
    ) -> list[tuple[ChunkStamp, float]]:
        return self._inner.near_duplicate_ranked(vector, threshold, profile_id)

    def snapshot_read(self, filter: ChunkFilter) -> list[ChunkStamp]:
        return self._inner.snapshot_read(filter)

    def mark_consolidated(self, chunk_ids: Sequence[str]) -> None:
        self._inner.mark_consolidated(chunk_ids)

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        return self._inner.purge_range(session_id, turn_start, turn_end)

    def update_weights(self, updates: Sequence[WeightUpdate]) -> None:
        self._inner.update_weights(updates)

    def update_chunk_state(
        self,
        chunk_ids: Sequence[str],
        hit_increment: int | None = None,
        needs_reconcile: bool | None = None,
    ) -> None:
        self._inner.update_chunk_state(
            chunk_ids, hit_increment=hit_increment, needs_reconcile=needs_reconcile
        )

    def list_chunks(self, filter: ChunkFilter, page: Page) -> PageResult[ChunkStamp]:
        return self._inner.list_chunks(filter, page)

    def get_dense(self, chunk_ids: Sequence[str]) -> dict[str, list[float]]:
        return self._inner.get_dense(chunk_ids)

    def distinct_profile_ids(self) -> set[str]:
        return self._inner.distinct_profile_ids()

    async def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if closer is not None:
            await closer()
