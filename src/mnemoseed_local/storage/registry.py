"""Driver registry: one table per layer, named multi-instance resolution.

Each layer (vector / graph / meta / embed) owns a DriverRegistry. Drivers are
registered by class (their DriverInfo.name is the table key) and resolved at
boot through the factory. A layer may instantiate several named instances of a
driver (e.g. graph.main / graph.isolated), so resolution is always
instance-name -> driver name + params.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from mnemoseed_local.storage.ports import StorageError, UnknownDriverError

_T = TypeVar("_T", bound=type[Any])


class DriverRegistry:
    """Registered driver classes for a single layer."""

    def __init__(self, layer: str) -> None:
        self._layer = layer
        self._drivers: dict[str, type[Any]] = {}

    @property
    def layer(self) -> str:
        return self._layer

    def add(self, cls: type[Any]) -> None:
        """Register a driver class keyed by DriverInfo.name."""
        info = getattr(cls, "info", None)
        name = getattr(info, "name", None) if info is not None else None
        if not isinstance(name, str) or not name:
            raise StorageError(
                f"{self._layer} driver class {cls.__name__!r} must declare a non-empty DriverInfo.name"
            )
        if name in self._drivers:
            raise StorageError(f"duplicate {self._layer} driver name {name!r}")
        self._drivers[name] = cls

    def names(self) -> tuple[str, ...]:
        """Registered driver names, sorted."""
        return tuple(sorted(self._drivers))

    def contains(self, name: str) -> bool:
        return name in self._drivers

    def build(self, name: str, params: Mapping[str, Any]) -> Any:
        """Instantiate a registered driver with the given params."""
        cls = self._drivers.get(name)
        if cls is None:
            raise UnknownDriverError(self._layer, name, self.names())
        try:
            return cls(**params)
        except Exception as exc:
            raise StorageError(f"{self._layer} driver {name!r} failed to construct: {exc}") from exc

    def clear(self) -> None:
        """Drop all registrations (test isolation)."""
        self._drivers.clear()


VECTOR_DRIVERS = DriverRegistry("vector")
GRAPH_DRIVERS = DriverRegistry("graph")
META_DRIVERS = DriverRegistry("meta")
EMBED_DRIVERS = DriverRegistry("embed")

DRIVER_REGISTRIES: Mapping[str, DriverRegistry] = {
    "vector": VECTOR_DRIVERS,
    "graph": GRAPH_DRIVERS,
    "meta": META_DRIVERS,
    "embed": EMBED_DRIVERS,
}


def register(table: DriverRegistry) -> Callable[[_T], _T]:
    """Class decorator: register a driver by its DriverInfo.name."""

    def deco(cls: _T) -> _T:
        table.add(cls)
        return cls

    return deco
