"""LLM driver registry (PRD-02 T6; FR-2.14).

One table for the whole llm layer, mirroring the storage registry idiom:
drivers register by class (identified by ``LLMDriverInfo.name``), resolve by
kind, and construction errors stay typed under ``LLMError``. The storage
registry is deliberately not reused — its identity contract (a Capability
frozenset per driver) and error types (StorageError / UnknownDriverError) are
storage semantics — so this is the typed LLM analog, and importing
``mnemoseed_local.llm`` registers the built-in drivers through the same import
side effect the storage package uses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from mnemoseed_local.llm.types import LLMDriverInfo, LLMError, UnknownLLMDriverError

_D = TypeVar("_D", bound=type[Any])


class LLMRegistry:
    """Named driver registry for one layer (always "llm" in practice)."""

    def __init__(self, layer: str) -> None:
        self._layer = layer
        self._drivers: dict[str, type[Any]] = {}

    @property
    def layer(self) -> str:
        """The layer this registry serves (used in unknown-driver messages)."""
        return self._layer

    def add(self, driver: type[Any]) -> None:
        """Register one driver class, keyed by its LLMDriverInfo.name."""
        info = getattr(driver, "info", None)
        if not isinstance(info, LLMDriverInfo) or not info.name:
            raise LLMError(f"{driver.__name__} must declare a non-empty LLMDriverInfo.name to register")
        if info.name in self._drivers:
            raise LLMError(f"duplicate {self._layer} driver registration for {info.name!r}")
        self._drivers[info.name] = driver

    def names(self) -> tuple[str, ...]:
        """Registered driver names, deterministic sorted order."""
        return tuple(sorted(self._drivers))

    def contains(self, name: str) -> bool:
        """True when a driver with this name is registered."""
        return name in self._drivers

    def catalog(self) -> tuple[LLMDriverInfo, ...]:
        """Registered driver metadata, deterministic name order (the API
        ``/api/v1/llm/routes`` surface shows the built-in driver catalog)."""
        infos: list[LLMDriverInfo] = []
        for name in self.names():
            info = self._drivers[name].info
            if isinstance(info, LLMDriverInfo):
                infos.append(info)
        return tuple(infos)

    def build(self, name: str, params: dict[str, Any] | None = None) -> Any:
        """Construct one driver instance. Construction failures are typed:
        unknown names raise ``UnknownLLMDriverError``; a driver constructor
        raising wraps into ``LLMError`` (callers degrade, never crash)."""
        params = params or {}
        driver = self._drivers.get(name)
        if driver is None:
            raise UnknownLLMDriverError(self._layer, name, tuple(self._drivers))
        try:
            return driver(**params)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"failed to construct {self._layer} driver {name!r}: {exc}") from exc

    def clear(self) -> None:
        """Drop every registration (test isolation / reload paths)."""
        self._drivers.clear()


def register(table: LLMRegistry) -> Callable[[_D], _D]:
    """Decorator registering a driver class into a registry at import time."""

    def wrapper(driver: _D) -> _D:
        table.add(driver)
        return driver

    return wrapper


# The one LLM driver table; built-ins register into it when the package
# (mnemoseed_local.llm or mnemoseed_local.llm.drivers) is imported.
LLM_DRIVERS = LLMRegistry("llm")
