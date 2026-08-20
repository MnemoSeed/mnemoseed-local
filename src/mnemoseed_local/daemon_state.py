"""Persistent daemon on/off state: the CONFIG_DIR/daemon.off sentinel (B2.5).

Presence of the marker means the memory service is DISABLED; absence means
enabled — the install default, zero config. The marker lives outside the
config registry on purpose: off/on must persist while the daemon is absent,
and a registry key would be re-primed from stale DB rows at the next boot.
CONFIG_DIR is resolved at call time so a relocated home (test or process) is
honored.
"""

from __future__ import annotations

from pathlib import Path

_MARKER_NAME = "daemon.off"


def disabled_marker() -> Path:
    """The sentinel file path: presence = disabled, absence = enabled."""
    from mnemoseed_local.config import CONFIG_DIR

    return CONFIG_DIR / _MARKER_NAME


def is_disabled() -> bool:
    """True when the disabled marker is present (default is enabled)."""
    return disabled_marker().exists()


def set_disabled() -> Path:
    """Write the disabled marker (idempotent); returns the marker path."""
    marker = disabled_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    return marker


def set_enabled() -> None:
    """Remove the disabled marker (no-op when absent)."""
    try:
        disabled_marker().unlink()
    except FileNotFoundError:
        pass
