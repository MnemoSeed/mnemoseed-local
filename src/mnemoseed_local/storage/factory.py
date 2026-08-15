"""Storage factory: resolves drivers from Config and runs the startup gate.

Boot sequence per layer: resolve named instances from config, instantiate the
registered driver for each name, then run the appendix C capability gate over
the resolved stack. HARD findings refuse startup (CapabilityStartupError lists
the missing capabilities); DEGRADE findings log an explicit warning. No path is
silent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

from mnemoseed_local.config import LAYER_TYPES, Config
from mnemoseed_local.storage.ports import (
    CapabilityStartupError,
    Embedder,
    GraphStore,
    MetaStore,
    StorageError,
    Store,
    ValidationReport,
    VectorStore,
    validate_capabilities,
)
from mnemoseed_local.storage.registry import DRIVER_REGISTRIES

logger = logging.getLogger(__name__)


@dataclass
class Stores:
    """The fully resolved, per-layer named storage stack plus its gate report."""

    instances: dict[str, dict[str, Store]]
    report: ValidationReport

    def _primary(self, kind: str) -> Store:
        named = self.instances[kind]
        store = named.get("main")
        if store is None:
            raise StorageError(f"layer {kind!r} resolved to no instance named 'main'")
        return store

    @property
    def vector(self) -> VectorStore:
        return cast(VectorStore, self._primary("vector"))

    @property
    def graph(self) -> GraphStore:
        return cast(GraphStore, self._primary("graph"))

    @property
    def meta(self) -> MetaStore:
        return cast(MetaStore, self._primary("meta"))

    @property
    def embed(self) -> Embedder:
        return cast(Embedder, self._primary("embed"))

    def layer(self, kind: str) -> dict[str, Store]:
        """All named instances of one layer (e.g. graph.main / graph.isolated)."""
        return self.instances[kind]

    def instance(self, kind: str, name: str) -> Store:
        """One named instance of one layer."""
        try:
            return self.instances[kind][name]
        except KeyError as exc:
            available = sorted(self.instances[kind])
            raise StorageError(
                f"no {kind} instance named {name!r} (available: {', '.join(available) or 'none'})"
            ) from exc

    async def close(self) -> None:
        for named in self.instances.values():
            for store in named.values():
                closer = getattr(store, "close", None)
                if closer is not None:
                    await closer()


def build_stores(config: Config) -> Stores:
    """Resolve every layer's named driver instances and run the capability gate."""
    resolved: dict[str, dict[str, Store]] = {}
    for kind in LAYER_TYPES:
        registry = DRIVER_REGISTRIES[kind]
        built: dict[str, Store] = {}
        for name, spec in config.layer_instances(kind).items():
            built[name] = registry.build(spec.driver, spec.params)
        resolved[kind] = built

    report = validate_capabilities(resolved)
    for issue in report.degradations:
        logger.warning(
            "capability degradation - %s.%s driver %r lacks %s (%s): %s",
            issue.layer,
            issue.instance,
            issue.driver,
            issue.capability.value,
            issue.feature,
            issue.behavior,
        )
    if report.hard_missing:
        raise CapabilityStartupError(report.hard_missing)

    return Stores(instances=resolved, report=report)
