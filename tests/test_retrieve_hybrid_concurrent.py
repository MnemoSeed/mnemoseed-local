"""PRD-03 T2.5: hybrid dual-track retrieval runs the two tracks concurrently.

The sequential T2 path stays as the deterministic reference; recall() issues the
vector and graph tracks on separate worker threads over thread-safe stores. The
merge is order-insensitive so the concurrent output must be byte-identical to
the sequential reference. Equivalence, forced overlap, and multi-thread recall
storms over the SAME driver instances prove both the orchestration and the
thread-safe sqlite connections.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

import pytest

from mnemoseed_local.retrieve.cues import ExtractedCues, Intent
from mnemoseed_local.retrieve.hybrid import HybridRecall, HybridRetriever
from mnemoseed_local.schema.graph import Edge, GraphNode, NodeType, RelType
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import ChunkFilter, NodeFilter, Page
from mnemoseed_local.storage.registry import GRAPH_DRIVERS, VECTOR_DRIVERS, register

_DIM = 64
_PROFILE = "alice"
_WORKER_COUNT = 8
_PER_WORKER = 20


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
    entities: tuple[str, ...] = (),
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id="p1",
        cues=Cues(project=project, host=host, time_bucket="diurnal", entities=list(entities)),
        provenance=Provenance(asserted_by="test-model", session_id="s1", source="manual", asserted_at=100.0),
        decay_weight=decay,
        score=0.5,
        consolidated=False,
        ingested_at=1.0,
        turn_start=1,
        turn_end=2,
    )


def _write(stack: _Stack, stamp: ChunkStamp) -> None:
    result = stack.embed.embed(stamp.text)
    stack.vector.upsert_chunk(stamp, result.dense, result.sparse)


def _node(node_id: str, entities: tuple[str, ...], *, decay: float = 1.0) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=_PROFILE,
        node_type=NodeType.PREFERENCE,
        entities=list(entities),
        props={
            "domain": "coding",
            "statement": "s",
            "valence": 0.5,
            "prior_width": 0.3,
            "trait_anchor": "a",
            "evidence_chain": [],
        },
        decay_weight=decay,
        confidence=0.7,
        provenance=Provenance(asserted_by="test-model", source="x", session_id="s1"),
        valid_from=100.0,
    )


def _edge(stack: _Stack, src: str, dst: str) -> None:
    stack.graph.add_edge(Edge(src=src, dst=dst, rel=RelType.HAS, profile_id=_PROFILE, created_at=1.0))


def _query_cues(entities: tuple[str, ...] = (), *, host: str | None = None) -> ExtractedCues:
    return ExtractedCues(cues=Cues(entities=list(entities), host=host), intent=Intent.RECALL)


def _seed(stack: _Stack) -> None:
    _write(
        stack,
        _chunk("c1", "the LanceDb loader caches vectors", decay=0.9, host="cursor", entities=("LanceDb",)),
    )
    _write(
        stack, _chunk("c2", "lancedb hybrid search reranks sparse", decay=0.9, entities=("LanceDb", "Sparse"))
    )
    _write(
        stack, _chunk("c3", "sqlite graph stores the cortex nodes", decay=0.8, entities=("Sqlite", "Graph"))
    )
    _write(stack, _chunk("c4", "unrelated university library at noon", decay=0.9, entities=("University",)))
    for i in range(6):
        _write(stack, _chunk(f"c-extra-{i}", "the LanceDb loader pipeline", decay=0.7, entities=("LanceDb",)))
    stack.graph.upsert_node(_node("hub", ("LanceDb",), decay=0.9))
    for i in range(12):
        stack.graph.upsert_node(_node(f"leaf-{i:02d}", ("lancedb",), decay=0.9))
        _edge(stack, "hub", f"leaf-{i:02d}")
    stack.graph.upsert_node(_node("weak", (), decay=0.3))
    _edge(stack, "hub", "weak")


def _canonical(result: HybridRecall) -> bytes:
    """Byte-stable serialization of a recall: two runs must match exactly."""
    rows = [
        (
            candidate.kind,
            candidate.id,
            candidate.source,
            candidate.score,
            candidate.breakdown.semantic,
            candidate.breakdown.cue_overlap,
            candidate.breakdown.decay_weight,
            candidate.breakdown.graph_centrality,
            candidate.breakdown.cooccurrence,
            candidate.breakdown.total,
            candidate.item.model_dump_json(),
        )
        for candidate in result.candidates
    ]
    return repr((result.vector_hits, result.graph_hits, result.cooccurrence_term, rows)).encode("utf-8")


def _recall(stack: _Stack, text: str, cues: ExtractedCues) -> HybridRecall:
    return HybridRetriever().recall(
        text,
        cues,
        profile_id=_PROFILE,
        vector_store=stack.vector,
        graph_store=stack.graph,
        embedder=stack.embed,
    )


def _run(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# ------------------------------------------------------------ equivalence


def test_concurrent_recall_byte_identical_to_sequential_reference(stack) -> None:
    _seed(stack)
    text = "lancedb loader"
    cues = _query_cues(("LanceDb",), host="cursor")
    retriever = HybridRetriever()
    concurrent = retriever.recall(
        text,
        cues,
        profile_id=_PROFILE,
        vector_store=stack.vector,
        graph_store=stack.graph,
        embedder=stack.embed,
    )
    sequential = retriever._recall_sequential(
        text,
        cues,
        profile_id=_PROFILE,
        vector_store=stack.vector,
        graph_store=stack.graph,
        embedder=stack.embed,
    )
    assert concurrent.vector_hits == sequential.vector_hits
    assert concurrent.graph_hits == sequential.graph_hits
    assert _canonical(concurrent) == _canonical(sequential)


def test_concurrent_recall_deterministic_across_calls(stack) -> None:
    _seed(stack)
    text = "lancedb loader"
    cues = _query_cues(("LanceDb",), host="cursor")
    first = _canonical(_recall(stack, text, cues))
    for _ in range(5):
        assert _canonical(_recall(stack, text, cues)) == first


# ------------------------------------------------------------ overlap


_BARRIER_TIMEOUT = 10.0


class _BrokenBarrier(Exception):
    """One track never entered: a sequential run blocks instead of completing."""


class _TrackProbe:
    """Delegating store wrapper that logs first-call entry/exit per track.

    The barrier forces both tracks to be inside their first port call before
    either returns: a sequential implementation blocks its first caller until a
    partner that never starts, so the barrier times out and the test fails
    cleanly instead of hanging. Interleaving is asserted without any sleep.
    """

    def __init__(
        self, real, side: str, barrier: threading.Barrier, events: list, lock: threading.Lock
    ) -> None:
        self._real = real
        self._side = side
        self._barrier = barrier
        self._events = events
        self._lock = lock

    def _mark(self, stage: str) -> None:
        with self._lock:
            self._events.append((stage, self._side, threading.get_ident(), time.perf_counter()))

    def _await_partner(self) -> None:
        try:
            self._barrier.wait(timeout=_BARRIER_TIMEOUT)
        except threading.BrokenBarrierError as exc:  # pragma: no cover - sequential runs only
            raise _BrokenBarrier("second track never started; retrieval is sequential") from exc

    def capabilities(self):
        return self._real.capabilities()

    def search(self, dense, sparse, filter, top_k):
        self._mark("entry")
        self._await_partner()
        try:
            return self._real.search(dense, sparse, filter, top_k)
        finally:
            self._mark("exit")

    def list_nodes(self, filter, page):
        self._mark("entry")
        self._await_partner()
        try:
            return self._real.list_nodes(filter, page)
        finally:
            self._mark("exit")

    def traverse(self, node_id, depth=2, filter=None):
        return self._real.traverse(node_id, depth=depth, filter=filter)


def test_concurrent_tracks_overlap_in_wall_time(stack) -> None:
    _seed(stack)
    events: list[tuple[str, str, int, float]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    vector_probe = _TrackProbe(stack.vector, "vector", barrier, events, lock)
    graph_probe = _TrackProbe(stack.graph, "graph", barrier, events, lock)

    HybridRetriever().recall(
        "lancedb loader",
        _query_cues(("LanceDb",)),
        profile_id=_PROFILE,
        vector_store=vector_probe,
        graph_store=graph_probe,
        embedder=stack.embed,
    )

    entries = [event for event in events if event[0] == "entry"]
    exits = [event for event in events if event[0] == "exit"]
    assert len(entries) == 2 and len(exits) == 2
    # both tracks are in flight at once: every entry precedes every exit...
    stage_order = [event[0] for event in events]
    assert stage_order[:2] == ["entry", "entry"]
    assert stage_order[2:] == ["exit", "exit"]
    # ...and they occupy distinct worker threads (a single worker would deadlock)
    entry_threads = {tid for _stage, _side, tid, _at in entries}
    assert len(entry_threads) == 2
    # wall-time spans overlap: each track entered before the other returned
    by_side = {
        side: {event[0]: event[3] for event in events if event[1] == side} for side in ("vector", "graph")
    }
    assert by_side["vector"]["entry"] < by_side["graph"]["exit"]
    assert by_side["graph"]["entry"] < by_side["vector"]["exit"]


# ------------------------------------------------------------ multi-thread storm


class _BlockingSearchStore:
    """Vector-store wrapper whose search blocks on an event, wedging one track
    (the F2 根治 wedged-worker shape for the retriever)."""

    def __init__(self, real, block: threading.Event) -> None:
        self._real = real
        self._block = block
        self.entered = threading.Event()

    def capabilities(self):
        return self._real.capabilities()

    def search(self, dense, sparse, filter, top_k):
        self.entered.set()
        self._block.wait()
        return self._real.search(dense, sparse, filter, top_k)

    def list_chunks(self, filter, page):
        return self._real.list_chunks(filter, page)


def test_retriever_close_bounded_when_track_wedged(stack) -> None:
    """F2 根治 D3: close() must return in bounded time while one track is
    wedged forever inside the vector store — the bounded wait abandons the
    wedged worker instead of joining it (the pre-fix shutdown(wait=True)
    jammed forever)."""
    _seed(stack)
    block = threading.Event()
    blocking = _BlockingSearchStore(stack.vector, block)
    retriever = HybridRetriever(close_timeout=0.2)
    recall_thread = threading.Thread(
        target=retriever.recall,
        kwargs={
            "query_text": "lancedb loader",
            "cues": _query_cues(("LanceDb",)),
            "profile_id": _PROFILE,
            "vector_store": blocking,
            "graph_store": stack.graph,
            "embedder": stack.embed,
        },
        daemon=True,
    )
    recall_thread.start()
    assert blocking.entered.wait(3.0), "the vector track never entered the blocked search"
    started = time.monotonic()
    close_thread = threading.Thread(target=retriever.close, daemon=True)
    close_thread.start()
    close_thread.join(timeout=1.0)
    elapsed = time.monotonic() - started
    assert not close_thread.is_alive(), "close() hung past the bound (unbounded join?)"
    assert elapsed < 1.0, f"close() was not bounded: {elapsed:.3f}s"
    # the wedged worker is abandoned, never joined: the recall thread stays
    # blocked on its result and the worker thread remains alive
    assert recall_thread.is_alive(), "the wedged track was joined, not abandoned"


def test_recall_after_close_raises_runtime_error(stack) -> None:
    """Docstring-only contract pin (hybrid.py): ``recall`` after ``close``
    raises the executor's RuntimeError instead of deadlocking — the retriever
    never serves after teardown."""
    _seed(stack)
    retriever = HybridRetriever()
    retriever.close()
    with pytest.raises(RuntimeError):
        retriever.recall(
            "lancedb loader",
            _query_cues(("LanceDb",)),
            profile_id=_PROFILE,
            vector_store=stack.vector,
            graph_store=stack.graph,
            embedder=stack.embed,
        )


def test_recalls_from_eight_threads_do_not_corrupt_state(stack) -> None:
    _seed(stack)
    text = "lancedb loader"
    cues = _query_cues(("LanceDb",), host="cursor")
    retriever = HybridRetriever()
    baseline = _canonical(_recall(stack, text, cues))

    barrier = threading.Barrier(_WORKER_COUNT)
    counts: dict[bytes, int] = {}
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            barrier.wait()
            local: dict[bytes, int] = {}
            for _ in range(_PER_WORKER):
                output = _canonical(
                    retriever.recall(
                        text,
                        cues,
                        profile_id=_PROFILE,
                        vector_store=stack.vector,
                        graph_store=stack.graph,
                        embedder=stack.embed,
                    )
                )
                local[output] = local.get(output, 0) + 1
            with lock:
                for output, count in local.items():
                    counts[output] = counts.get(output, 0) + count
        except Exception as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)

    _run([threading.Thread(target=worker, args=(i,)) for i in range(_WORKER_COUNT)])

    assert errors == []
    assert set(counts) == {baseline}
    assert counts[baseline] == _WORKER_COUNT * _PER_WORKER
    # the stores were not corrupted by the storm: a fresh recall matches, and the
    # underlying row counts are untouched (recall is read-only).
    assert _canonical(_recall(stack, text, cues)) == baseline
    assert stack.graph.list_nodes(NodeFilter(profile_id=_PROFILE), Page(0, 1_000)).total == 14
    assert stack.vector.list_chunks(ChunkFilter(profile_id=_PROFILE), Page(0, 1_000)).total == 10
