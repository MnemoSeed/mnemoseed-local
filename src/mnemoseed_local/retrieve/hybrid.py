"""Hybrid dual-track retrieval and fusion rerank (PRD-03 FR-3.3 / FR-3.4 / FR-3.14).

Two independent retrieval tracks produce one ranked candidate pool:

- Vector track (FR-3.3): semantic neighbors from the store, embedded through
  the ``Embedder`` port, restricted to the cue-entity overlap when the store
  declares ``vector.metadata_filter``, and floored at ``min_decay``.
- Graph track (FR-3.3): entity-subgraph 2-hop traversal seeded from every node
  carrying any query cue entity; the pool is decay-floored at the candidate
  boundary and the track caps at ``graph_top_k`` decay-weighted entries. Seeds
  are not decay-filtered so a never-decay constraint reachable only through a
  decayed fact can still surface; sub-floor nodes never enter the pool.

The fusion rerank (FR-3.4) scores every candidate as
``alpha*semantic + beta*cue_overlap + gamma*decay_weight + delta*graph_centrality``.
Each component is normalized to [0, 1], so the weights express relative
importance and alpha/beta/gamma/delta need not sum to 1. The epsilon
co-occurrence term is not in use: the port exposes no cheap edge read, the
breakdown field stays 0.0, and ``HybridRecall.cooccurrence_term`` reports the
omission.

The two tracks read independent stores and merge through an order-insensitive
stable sort, so their evaluation order cannot affect the result: ``recall``
issues them concurrently on a two-worker executor and the output is
byte-identical to the sequential reference ``_recall_sequential``. The embedded
sqlite drivers keep one connection per thread, so parallel track reads never
share a handle. The executor is cached per retriever (threads spawn lazily on
the first recall) and the interpreter joins its idle workers at exit, so no
thread outlives the process. Deterministic: no clocks, no randomness, no
network; ties break by (kind, id).

Situational weak cues (FR-3.14): extracted host/project/time_bucket never
filter candidates; they feed the beta term as a low-weight blended component
(entity 0.6 / tool 0.25 / context 0.15). Graph candidates carry only the entity
component of beta (GraphNode stores no situational fields) and a zero semantic
term; a chunk candidate's semantic term is the store similarity clamped to
[0, 1]. Entity overlap folds case; the store metadata prefilter matches on the
raw stored entities (embedded driver) so a chunk whose only entity match is
case-differing may be cut before scoring.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from mnemoseed_local.retrieve.cues import ExtractedCues
from mnemoseed_local.schema.graph import GraphNode
from mnemoseed_local.schema.stamp import ChunkStamp, Cues
from mnemoseed_local.storage.ports import (
    Capability,
    ChunkFilter,
    Embedder,
    GraphStore,
    NodeFilter,
    Page,
    VectorStore,
)

# beta-internal component weights (FR-3.14: context stays a low-weight cue)
_BETA_ENTITY_WEIGHT = 0.6
_BETA_TOOL_WEIGHT = 0.25
_BETA_CONTEXT_WEIGHT = 0.15

_SEED_PAGE_LIMIT = 10_000


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class HybridConfig:
    """Tunable fusion weights and candidate-pool floors (FR-3.4).

    Defaults: alpha=1.0, beta=1.0, gamma=0.8, delta=0.5; candidate-pool decay
    floor 0.4 (design item 2: decay_weight < 0.4 never enters the pool);
    per-track pools capped at 20; graph 1-hop degree centrality saturates at 8
    neighbors.
    """

    weight_semantic: float = 1.0
    weight_cue_overlap: float = 1.0
    weight_decay: float = 0.8
    weight_centrality: float = 0.5
    min_decay: float = 0.4
    vector_top_k: int = 20
    graph_top_k: int = 20
    centrality_saturation: int = 8


# ---------------------------------------------------------------- output types


@dataclass(frozen=True)
class ScoreBreakdown:
    """Per-candidate fused-score components (transparency for debugging and
    the later top-k / dropped_count assembly)."""

    semantic: float
    cue_overlap: float
    decay_weight: float
    graph_centrality: float
    cooccurrence: float
    total: float


@dataclass(frozen=True)
class Candidate:
    """One fused candidate: a chunk hit (kind="chunk") or graph node (kind="graph")."""

    kind: str
    id: str
    source: str
    item: ChunkStamp | GraphNode
    score: float
    breakdown: ScoreBreakdown


@dataclass(frozen=True)
class HybridRecall:
    """Ranked fused union plus per-track accounting."""

    candidates: list[Candidate]
    vector_hits: int
    graph_hits: int
    cooccurrence_term: bool = False


# ---------------------------------------------------------------- engine


class HybridRetriever:
    """Deterministic dual-track retrieval with fusion rerank."""

    def __init__(self, config: HybridConfig | None = None) -> None:
        self._config = config if config is not None else HybridConfig()
        # Cached two-worker executor: threads spawn lazily on the first recall
        # and stay for the retriever's lifetime (bounded sqlite handles), and
        # the interpreter's atexit hook joins idle workers at exit.
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mnemoseed-track")

    @property
    def config(self) -> HybridConfig:
        return self._config

    def close(self) -> None:
        """Release the track executor.

        The daemon owns the retriever lifecycle (T4): shutdown the cached
        two-worker executor on teardown so worker threads and their sqlite
        handles never outlive the process. Idempotent; ``recall`` after close
        raises the executor's RuntimeError instead of deadlocking.
        """
        self._executor.shutdown(wait=True)

    def recall(
        self,
        query_text: str,
        cues: ExtractedCues,
        *,
        profile_id: str,
        vector_store: VectorStore,
        graph_store: GraphStore,
        embedder: Embedder,
    ) -> HybridRecall:
        """Rank the merged vector+graph pool; the two tracks run concurrently."""
        config = self._config
        query = cues.cues
        vector_future = self._executor.submit(
            self._vector_track,
            query_text,
            query,
            profile_id,
            vector_store,
            embedder,
            config,
        )
        graph_future = self._executor.submit(
            self._graph_track,
            query,
            profile_id,
            graph_store,
            config,
        )
        return _merge(vector_future.result(), graph_future.result())

    def _recall_sequential(
        self,
        query_text: str,
        cues: ExtractedCues,
        *,
        profile_id: str,
        vector_store: VectorStore,
        graph_store: GraphStore,
        embedder: Embedder,
    ) -> HybridRecall:
        """Reference path: both tracks on the calling thread (T2 semantics).

        Kept for the byte-equivalence tests and as a deterministic fallback;
        ``recall`` runs exactly the same tracks on worker threads.
        """
        config = self._config
        query = cues.cues
        vector_candidates = self._vector_track(query_text, query, profile_id, vector_store, embedder, config)
        graph_candidates = self._graph_track(query, profile_id, graph_store, config)
        return _merge(vector_candidates, graph_candidates)

    # ----------------------------------------------------------- vector track

    def _vector_track(
        self,
        query_text: str,
        query: Cues,
        profile_id: str,
        vector_store: VectorStore,
        embedder: Embedder,
        config: HybridConfig,
    ) -> list[Candidate]:
        entities = tuple(query.entities)
        embedding = embedder.embed(query_text)
        supports_filter = Capability.VECTOR_METADATA_FILTER in vector_store.capabilities()
        filter_entities = entities if supports_filter and entities else ()
        hits = vector_store.search(
            embedding.dense,
            embedding.sparse,
            ChunkFilter(
                profile_id=profile_id,
                min_decay=config.min_decay,
                entities=filter_entities,
                # Merged chunks are the fact's retained evidence scene, never
                # fresh recall surface: the dream merge marks them consolidated
                # (design/03 §4, same semantics as the Freshness Guard probe).
                consolidated=False,
            ),
            config.vector_top_k,
        )
        candidates: list[Candidate] = []
        for hit in hits:
            chunk = hit.chunk
            if chunk.decay_weight < config.min_decay:
                continue
            if entities and not _entity_overlap(entities, chunk.cues.entities):
                continue
            semantic = max(0.0, min(1.0, float(hit.similarity)))
            breakdown = _breakdown(
                semantic=semantic,
                cue_overlap=_chunk_cue_overlap(query, chunk),
                decay_weight=chunk.decay_weight,
                graph_centrality=0.0,
                config=config,
            )
            candidates.append(
                Candidate(
                    kind="chunk",
                    id=chunk.chunk_id,
                    source="vector",
                    item=chunk,
                    score=breakdown.total,
                    breakdown=breakdown,
                )
            )
        return candidates

    # ------------------------------------------------------------ graph track

    def _graph_track(
        self,
        query: Cues,
        profile_id: str,
        graph_store: GraphStore,
        config: HybridConfig,
    ) -> list[Candidate]:
        entities = tuple(query.entities)
        if not entities:
            return []
        seeds = self._seed_nodes(graph_store, profile_id, entities)
        seen: dict[str, GraphNode] = {}
        for seed in sorted(seeds, key=lambda node: node.node_id):
            for node in graph_store.traverse(
                seed.node_id,
                depth=2,
                filter=NodeFilter(profile_id=profile_id),
            ):
                if node.decay_weight >= config.min_decay:
                    seen[node.node_id] = node
        pool = sorted(seen.values(), key=lambda node: (-node.decay_weight, node.node_id))[
            : config.graph_top_k
        ]
        candidates: list[Candidate] = []
        for node in pool:
            breakdown = _breakdown(
                semantic=0.0,
                cue_overlap=_graph_cue_overlap(entities, node),
                decay_weight=node.decay_weight,
                graph_centrality=_graph_centrality(graph_store, profile_id, node, config),
                config=config,
            )
            candidates.append(
                Candidate(
                    kind="graph",
                    id=node.node_id,
                    source="graph",
                    item=node,
                    score=breakdown.total,
                    breakdown=breakdown,
                )
            )
        return candidates

    def _seed_nodes(
        self,
        graph_store: GraphStore,
        profile_id: str,
        entities: tuple[str, ...],
    ) -> list[GraphNode]:
        result = graph_store.list_nodes(
            NodeFilter(profile_id=profile_id, entities=entities),
            Page(offset=0, limit=_SEED_PAGE_LIMIT),
        )
        return list(result.items)


# ---------------------------------------------------------------- helpers


def _sort_key(candidate: Candidate) -> tuple[float, str, str]:
    """Rank order: score descending, then a stable (kind, id) tie-break."""
    return (-candidate.score, candidate.kind, candidate.id)


def _merge(
    vector_candidates: Sequence[Candidate],
    graph_candidates: Sequence[Candidate],
) -> HybridRecall:
    """Fuse both track pools into one order-insensitive ranked recall."""
    merged = sorted([*vector_candidates, *graph_candidates], key=_sort_key)
    return HybridRecall(
        candidates=merged,
        vector_hits=len(vector_candidates),
        graph_hits=len(graph_candidates),
    )


def _breakdown(
    *,
    semantic: float,
    cue_overlap: float,
    decay_weight: float,
    graph_centrality: float,
    config: HybridConfig,
) -> ScoreBreakdown:
    total = (
        config.weight_semantic * semantic
        + config.weight_cue_overlap * cue_overlap
        + config.weight_decay * decay_weight
        + config.weight_centrality * graph_centrality
    )
    return ScoreBreakdown(
        semantic=semantic,
        cue_overlap=cue_overlap,
        decay_weight=decay_weight,
        graph_centrality=graph_centrality,
        cooccurrence=0.0,
        total=total,
    )


def _graph_centrality(
    graph_store: GraphStore,
    profile_id: str,
    node: GraphNode,
    config: HybridConfig,
) -> float:
    """Graph centrality proxy: 1-hop degree (port surface), saturated to [0, 1]."""
    hop1 = graph_store.traverse(node.node_id, depth=1, filter=NodeFilter(profile_id=profile_id))
    degree = max(0, len(hop1) - 1)
    return min(1.0, degree / max(1, config.centrality_saturation))


def _fold(value: str) -> str:
    return value.casefold()


def _entity_overlap(query_entities: tuple[str, ...], stored_entities: Sequence[str]) -> bool:
    query = {_fold(entity) for entity in query_entities}
    return any(_fold(entity) in query for entity in stored_entities)


def _ratio(query_items: Sequence[str], stored_items: Sequence[str]) -> float:
    if not query_items:
        return 0.0
    query = {_fold(item) for item in query_items}
    stored = {_fold(item) for item in stored_items}
    return len(query & stored) / len(query)


def _chunk_cue_overlap(query: Cues, chunk: ChunkStamp) -> float:
    entity = _ratio(query.entities, chunk.cues.entities)
    tool = _ratio(query.tools_used, chunk.cues.tools_used)
    context = _context_match(query, chunk.cues.host, chunk.cues.project, chunk.cues.time_bucket)
    return _BETA_ENTITY_WEIGHT * entity + _BETA_TOOL_WEIGHT * tool + _BETA_CONTEXT_WEIGHT * context


def _graph_cue_overlap(query_entities: tuple[str, ...], node: GraphNode) -> float:
    return _BETA_ENTITY_WEIGHT * _ratio(query_entities, node.entities)


def _context_match(
    query: Cues,
    host: str | None,
    project: str | None,
    time_bucket: str | None,
) -> float:
    present = 0
    matched = 0
    if query.host is not None:
        present += 1
        if query.host == host:
            matched += 1
    if query.project is not None:
        present += 1
        if query.project == project:
            matched += 1
    if query.time_bucket is not None:
        present += 1
        if query.time_bucket == time_bucket:
            matched += 1
    return matched / present if present else 0.0
