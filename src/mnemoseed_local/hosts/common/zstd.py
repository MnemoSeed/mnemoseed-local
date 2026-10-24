"""Lazy zstandard access for hosts that ship compressed payloads."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

_INSTALL_HINT = "install with: uv tool install --force 'mnemoseed-local[codex]'"


def require_zstandard() -> ModuleType:
    """Import zstandard on first use; a missing extra fails with guidance."""
    try:
        return import_module("zstandard")
    except ImportError as exc:
        raise RuntimeError(f"zstandard is required for this host ({_INSTALL_HINT})") from exc
