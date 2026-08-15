"""Storage layer: ports & adapters.

Importing this package pulls in the built-in drivers (registration side effect)
so ``build_stores`` can resolve them by name.
"""

from mnemoseed_local.storage import drivers  # noqa: F401

__all__ = ["drivers"]
