"""Driver registry, named multi-instance, and the appendix C capability gate.

Every test registers throwaway fake drivers into the module-level registries
(real drivers ship in later milestones); an autouse fixture clears them.
"""

import pytest

from mnemoseed_local.config import Config, LayerSpec, _InstanceOverride
from mnemoseed_local.storage.factory import build_stores
from mnemoseed_local.storage.ports import (
    Capability,
    CapabilityStartupError,
    DriverInfo,
    Embedder,
    GraphStore,
    MetaStore,
    StorageError,
    UnknownDriverError,
    ValidationSeverity,
    VectorStore,
    validate_capabilities,
)
from mnemoseed_local.storage.registry import (
    DRIVER_REGISTRIES,
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

FULL_VECTOR = frozenset(
    {Capability.VECTOR_HYBRID_SEARCH, Capability.VECTOR_METADATA_FILTER, Capability.VECTOR_SNAPSHOT}
)
FULL_GRAPH = frozenset(
    {
        Capability.GRAPH_VERSION_CHAIN,
        Capability.GRAPH_COOCCURRENCE_EDGES,
        Capability.GRAPH_TRAVERSE_2HOP,
        Capability.GRAPH_EDGE_LIST,
    }
)
FULL_META = frozenset({Capability.META_TRANSACTION, Capability.META_CONCURRENT_READERS})
FULL_EMBED = frozenset(
    {Capability.EMBED_LOCAL_INFERENCE, Capability.EMBED_BATCH, Capability.EMBED_SPARSE_OUTPUT}
)


@pytest.fixture(autouse=True)
def _clear_registries():
    for registry in DRIVER_REGISTRIES.values():
        registry.clear()
    yield
    for registry in DRIVER_REGISTRIES.values():
        registry.clear()


class FakeVector(VectorStore):
    info = DriverInfo(name="fake_vector", capabilities=FULL_VECTOR)

    def capabilities(self):
        return self.info.capabilities


class FakeGraph(GraphStore):
    info = DriverInfo(name="fake_graph", capabilities=FULL_GRAPH)

    def __init__(self, **params):
        self.params = params

    def capabilities(self):
        return self.info.capabilities


class FakeMeta(MetaStore):
    info = DriverInfo(name="fake_meta", capabilities=FULL_META)

    def capabilities(self):
        return self.info.capabilities


class FakeEmbed(Embedder):
    info = DriverInfo(name="fake_embed", capabilities=FULL_EMBED)
    dimension = 8

    def capabilities(self):
        return self.info.capabilities


def _register_all() -> None:
    register(VECTOR_DRIVERS)(FakeVector)
    register(GRAPH_DRIVERS)(FakeGraph)
    register(META_DRIVERS)(FakeMeta)
    register(EMBED_DRIVERS)(FakeEmbed)


def _full_config() -> Config:
    return Config(
        preset="embedded",
        storage={
            "vector": LayerSpec(layer="vector", driver="fake_vector"),
            "graph": LayerSpec(layer="graph", driver="fake_graph"),
            "meta": LayerSpec(layer="meta", driver="fake_meta"),
            "embed": LayerSpec(layer="embed", driver="fake_embed"),
        },
    )


def test_capability_enum_is_exactly_fr_8_6_set():
    expected = {
        "vector.hybrid_search",
        "vector.metadata_filter",
        "vector.snapshot",
        "graph.traverse_2hop",
        "graph.version_chain",
        "graph.cooccurrence_edges",
        "graph.edge_list",
        "meta.transaction",
        "meta.concurrent_readers",
        "embed.local_inference",
        "embed.batch",
        "embed.sparse_output",
    }
    assert {c.value for c in Capability} == expected
    assert len(list(Capability)) == 12


def test_register_via_decorator():
    register(VECTOR_DRIVERS)(FakeVector)
    assert VECTOR_DRIVERS.contains("fake_vector")
    assert "fake_vector" in VECTOR_DRIVERS.names()


def test_register_duplicate_name_rejected():
    register(VECTOR_DRIVERS)(FakeVector)
    with pytest.raises(StorageError, match="duplicate"):
        register(VECTOR_DRIVERS)(FakeVector)


def test_register_requires_info_name():
    class NoInfo:
        pass

    with pytest.raises(StorageError, match="must declare a non-empty DriverInfo.name"):
        VECTOR_DRIVERS.add(NoInfo)


def test_registry_resolution_builds_all_layers():
    _register_all()
    stores = build_stores(_full_config())
    assert stores.vector.info.name == "fake_vector"
    assert stores.graph.info.name == "fake_graph"
    assert stores.meta.info.name == "fake_meta"
    assert stores.embed.info.name == "fake_embed"
    assert stores.report.ok is True
    assert stores.report.missing == []


def test_named_multi_instance():
    _register_all()
    cfg = _full_config()
    cfg.storage["graph"] = LayerSpec(
        layer="graph",
        driver="fake_graph",
        instances={"isolated": _InstanceOverride(driver="fake_graph", params={"namespace": "tier3"})},
    )
    stores = build_stores(cfg)
    main = stores.graph
    isolated = stores.instance("graph", "isolated")
    assert main.params == {}
    assert isolated.params == {"namespace": "tier3"}
    with pytest.raises(StorageError, match="no graph instance named 'missing'"):
        stores.instance("graph", "missing")


def test_unknown_driver_error_names_driver_and_layer():
    register(VECTOR_DRIVERS)(FakeVector)
    cfg = Config(
        preset="embedded",
        storage={"vector": LayerSpec(layer="vector", driver="no_such_driver")},
    )
    with pytest.raises(
        UnknownDriverError,
        match=r"unknown vector driver 'no_such_driver' \(available: fake_vector\)",
    ):
        build_stores(cfg)


def test_sparse_output_missing_degrades_with_warning(caplog):
    _register_all()

    class NoSparseEmbed(FakeEmbed):
        info = DriverInfo(
            name="fake_embed_nosparse",
            capabilities=FULL_EMBED - {Capability.EMBED_SPARSE_OUTPUT},
        )

    register(EMBED_DRIVERS)(NoSparseEmbed)
    cfg = _full_config()
    cfg.storage["embed"] = LayerSpec(layer="embed", driver="fake_embed_nosparse")

    stores = build_stores(cfg)
    assert stores.report.ok is True
    assert not stores.report.hard_missing
    missing = {i.capability for i in stores.report.degradations}
    assert Capability.EMBED_SPARSE_OUTPUT in missing
    assert any("capability degradation" in r.message for r in caplog.records)


def test_meta_transaction_missing_hard_refuses(caplog):
    _register_all()

    class NoTransactionMeta(MetaStore):
        info = DriverInfo(
            name="fake_meta_notx",
            capabilities=frozenset({Capability.META_CONCURRENT_READERS}),
        )

        def capabilities(self):
            return self.info.capabilities

    register(META_DRIVERS)(NoTransactionMeta)
    cfg = _full_config()
    cfg.storage["meta"] = LayerSpec(layer="meta", driver="fake_meta_notx")

    with pytest.raises(CapabilityStartupError) as excinfo:
        build_stores(cfg)
    message = str(excinfo.value)
    assert "capability gate failed" in message
    assert "meta.transaction" in message
    assert "fake_meta_notx" in message


def test_validate_unit_hard_and_degrade_paths():
    class LimitedVector(VectorStore):
        info = DriverInfo(name="limited_vector", capabilities=frozenset({Capability.VECTOR_SNAPSHOT}))

        def capabilities(self):
            return self.info.capabilities

    class LimitedEmbed(Embedder):
        info = DriverInfo(name="limited_embed", capabilities=frozenset({Capability.EMBED_LOCAL_INFERENCE}))
        dimension = 8

        def capabilities(self):
            return self.info.capabilities

    report = validate_capabilities(
        {
            "vector": {"main": LimitedVector()},
            "graph": {"main": FakeGraph()},
            "meta": {"main": FakeMeta()},
            "embed": {"main": LimitedEmbed()},
        }
    )
    # metadata_filter is hard and missing -> not ok
    assert report.ok is False
    assert {i.capability for i in report.hard_missing} == {Capability.VECTOR_METADATA_FILTER}
    degrade = {i.capability for i in report.degradations}
    assert Capability.EMBED_SPARSE_OUTPUT in degrade
    assert Capability.EMBED_BATCH in degrade
    assert all(i.severity is ValidationSeverity.HARD for i in report.hard_missing)
    assert all(i.severity is ValidationSeverity.DEGRADE for i in report.degradations)
    # declared but not part of the gate (appendix C has no row for these)
    gated = {i.capability for i in report.missing}
    assert Capability.GRAPH_TRAVERSE_2HOP not in gated
    assert Capability.EMBED_LOCAL_INFERENCE not in gated


def test_report_missing_combines_hard_and_degrade():
    report = validate_capabilities(
        {
            "vector": {"main": FakeVector()},
            "graph": {"main": FakeGraph()},
            "meta": {"main": FakeMeta()},
            "embed": {"main": FakeEmbed()},
        }
    )
    assert report.ok is True
    assert report.missing == []
    assert report.hard_missing == []
    assert report.degradations == []
