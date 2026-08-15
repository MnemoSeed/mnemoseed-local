"""AC-4 degradation demos: every missing-capability path is explicit, never silent.

Three subset combinations from appendix C:
(b) a vector driver without vector.snapshot -> startup degradation warning and
    snapshot_read degrades to a turn-range logical read, warning again;
(c) a meta driver without meta.transaction -> HARD gate, startup refusal;
(d) a graph driver without graph.edge_list -> startup degradation warning.

The wrapper drivers below are what the gate actually sees; the real backends
underneath are the embedded drivers, so the tests stay offline.
"""

from __future__ import annotations

import logging

import pytest
from _support import PROFILE, make_stamp, run

from mnemoseed_local.config import Config, LayerSpec
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.factory import CapabilityStartupError, build_stores
from mnemoseed_local.storage.ports import Capability, ChunkFilter, DriverInfo, Page
from mnemoseed_local.storage.registry import (
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_DIM = 64


# ---------------------------------------------------------- degrade wrapper (b)


class NoSnapshotVector:
    """A vector driver that silently dropped vector.snapshot from its caps."""

    info = DriverInfo(
        name="no_snapshot_vector",
        capabilities=frozenset({Capability.VECTOR_HYBRID_SEARCH, Capability.VECTOR_METADATA_FILTER}),
        description="contract wrapper: lancedb without the snapshot capability",
    )

    def __init__(self, **kwargs) -> None:
        self._inner = LanceDbEmbeddedStore(**kwargs)

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def snapshot_read(self, filter: ChunkFilter):
        logging.getLogger("mnemoseed_local.degradation.no_snapshot").warning(
            "capability degradation - vector.main driver %r lacks vector.snapshot "
            "(dream-engine snapshot isolation): snapshot degrades to turn-range "
            "logical isolation",
            self.info.name,
        )
        return self._inner.list_chunks(filter, Page(limit=1 << 20)).items


def test_vector_snapshot_degrade_to_logical_read_at_startup_and_use(caplog, tmp_path) -> None:
    """Dropping vector.snapshot is a DEGRADE: warned at boot, snapshot becomes
    a logical read and still warns when invoked."""
    if not VECTOR_DRIVERS.contains(NoSnapshotVector.info.name):
        register(VECTOR_DRIVERS)(NoSnapshotVector)
    config = Config(
        preset="custom",
        storage={
            "vector": LayerSpec(
                "vector",
                driver="no_snapshot_vector",
                params={"uri": str(tmp_path / "chunks.lance"), "dimensions": _DIM},
            ),
            "graph": LayerSpec("graph", driver="sqlite_graph", params={"path": str(tmp_path / "graph.db")}),
            "meta": LayerSpec("meta", driver="sqlite_meta", params={"path": str(tmp_path / "meta.db")}),
            "embed": LayerSpec("embed", driver="synthetic", params={"dimension": _DIM}),
        },
    )
    with caplog.at_level(logging.WARNING):
        stores = build_stores(config)  # startup passes (degrade, not hard)
    assert any(
        "capability degradation" in r.message and "vector.snapshot" in r.message for r in caplog.records
    ), "startup must log the snapshot degradation"

    stores.vector.upsert_chunk(
        make_stamp("s1", "snap one"),
        stores.embed.embed("snap one").dense,
        stores.embed.embed("snap one").sparse,
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        snapshot = stores.vector.snapshot_read(ChunkFilter(profile_id=PROFILE))
    assert {chunk.chunk_id for chunk in snapshot} == {"s1"}
    assert any(
        "capability degradation" in r.message and "logical isolation" in r.message for r in caplog.records
    ), "the degraded snapshot path itself must warn (no silent fallback)"
    run(stores.close())


# ---------------------------------------------------------- hard gate wrapper (c)


class NoTransactionMeta:
    """A meta driver missing the hard meta.transaction capability."""

    info = DriverInfo(
        name="no_transaction_meta",
        capabilities=frozenset({Capability.META_CONCURRENT_READERS}),
        description="contract wrapper: sqlite_meta without the transaction capability",
    )

    def __init__(self, **kwargs) -> None:
        self._inner = SqliteMetaDriver(**kwargs)

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_meta_transaction_missing_refuses_startup(caplog, tmp_path) -> None:
    """Dropping meta.transaction is HARD: build_stores raises, nothing boots."""
    if not META_DRIVERS.contains(NoTransactionMeta.info.name):
        register(META_DRIVERS)(NoTransactionMeta)
    config = Config(
        preset="custom",
        storage={
            "vector": LayerSpec(
                "vector",
                driver="lancedb_embedded",
                params={"uri": str(tmp_path / "chunks.lance"), "dimensions": _DIM},
            ),
            "graph": LayerSpec("graph", driver="sqlite_graph", params={"path": str(tmp_path / "graph.db")}),
            "meta": LayerSpec(
                "meta", driver="no_transaction_meta", params={"path": str(tmp_path / "meta.db")}
            ),
            "embed": LayerSpec("embed", driver="synthetic", params={"dimension": _DIM}),
        },
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(CapabilityStartupError, match="meta.transaction"):
            build_stores(config)
    assert not any("capability degradation" in r.message for r in caplog.records), (
        "a HARD miss must not be logged as a degradation; refusing is the only path"
    )


# ------------------------------------------------- degrade wrapper (d): graph.edge_list


class NoEdgeListGraph:
    """A graph driver that dropped the GRAPH_EDGE_LIST bulk-edge capability."""

    info = DriverInfo(
        name="no_edge_list_graph",
        capabilities=frozenset(
            {
                Capability.GRAPH_TRAVERSE_2HOP,
                Capability.GRAPH_VERSION_CHAIN,
                Capability.GRAPH_COOCCURRENCE_EDGES,
            }
        ),
        description="contract wrapper: sqlite_graph without the bulk edge listing",
    )

    def __init__(self, **kwargs) -> None:
        self._inner = SqliteGraphDriver(**kwargs)

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_graph_edge_list_missing_degrades_with_startup_warning(caplog, tmp_path) -> None:
    """Appendix C v1.1: a graph driver without GRAPH_EDGE_LIST still boots but
    the startup gate logs the console-graph degradation warning (bulk edge view
    unavailable; per-node traversal is the fallback, never a fake bulk read)."""
    if not GRAPH_DRIVERS.contains(NoEdgeListGraph.info.name):
        register(GRAPH_DRIVERS)(NoEdgeListGraph)
    config = Config(
        preset="custom",
        storage={
            "vector": LayerSpec(
                "vector",
                driver="lancedb_embedded",
                params={"uri": str(tmp_path / "chunks.lance"), "dimensions": _DIM},
            ),
            "graph": LayerSpec(
                "graph",
                driver="no_edge_list_graph",
                params={"path": str(tmp_path / "graph.db")},
            ),
            "meta": LayerSpec("meta", driver="sqlite_meta", params={"path": str(tmp_path / "meta.db")}),
            "embed": LayerSpec("embed", driver="synthetic", params={"dimension": _DIM}),
        },
    )
    with caplog.at_level(logging.WARNING):
        stores = build_stores(config)  # startup passes (degrade, not hard)
    assert any(
        "capability degradation" in r.message and "graph.edge_list" in r.message for r in caplog.records
    ), "startup must log the graph.edge_list degradation"
    assert stores.report.ok, "a degrade, not a hard miss, so the stack stays bootable"
    run(stores.close())
