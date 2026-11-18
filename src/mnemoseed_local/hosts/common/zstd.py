"""Lazy zstandard access for hosts that ship compressed payloads."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

from mnemoseed_local.hosts.common.config import ConfigParseError

#: The install form that works from a local checkout.
ZSTANDARD_INSTALL_COMMAND = "uv tool install --with zstandard ."

_INSTALL_HINT = f"install with: {ZSTANDARD_INSTALL_COMMAND}"


class MissingZstandardError(ConfigParseError, RuntimeError):
    """The optional reader extra is absent; nothing was read or written."""


def require_zstandard() -> ModuleType:
    """Import zstandard on first use; a missing extra fails with guidance."""
    try:
        return import_module("zstandard")
    except ImportError as exc:
        raise MissingZstandardError(f"zstandard is required for this host ({_INSTALL_HINT})") from exc


def zstandard_available() -> bool:
    """Whether the optional reader extra imports in this runtime."""
    try:
        require_zstandard()
    except MissingZstandardError:
        return False
    return True
