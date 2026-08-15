"""Contract-suite fixtures.

Two jobs: re-register the real drivers (test_registry, which shares the module
registries, clears them at teardown — the import-time @register fires only
once), and provide the parametrized `stack` fixture that runs every contract
test against the embedded driver family.
"""

from __future__ import annotations

import asyncio

import pytest
from _support import ContractStack, build_embedded

from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_REAL_DRIVERS: tuple[tuple[object, type], ...] = (
    (VECTOR_DRIVERS, LanceDbEmbeddedStore),
    (GRAPH_DRIVERS, SqliteGraphDriver),
    (META_DRIVERS, SqliteMetaDriver),
    (EMBED_DRIVERS, SyntheticEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_real_drivers_registered() -> None:
    """Re-register any driver test_registry cleared from the shared registries."""
    for registry, cls in _REAL_DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


@pytest.fixture
def stack(tmp_path) -> ContractStack:
    """The embedded contract stack (lancedb + sqlite graph + sqlite meta + synthetic)."""
    built = build_embedded(tmp_path)
    yield built
    asyncio.run(built.close())
