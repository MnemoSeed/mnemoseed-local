"""Install / uninstall / status for the OpenCode host hook (A3 T2).

OpenCode's global config root resolves as:

1. ``OPENCODE_CONFIG_DIR`` when set (the host honors the same override);
2. else ``XDG_CONFIG_HOME/opencode``;
3. else ``~/.config/opencode`` — via ``Path.home() / ".config"``, so Windows
   converges on the same ``%USERPROFILE%\\.config\\opencode`` layout.

The plugin file lands at ``<root>/plugin/mnemoseed-local.ts``: OpenCode
auto-discovers ``{plugin,plugins}/*.{ts,js}`` under the config root at
startup. Install overwrites byte-identically (idempotent); uninstall removes
only that one file; status reports the install state plus a daemon
reachability probe. All operations are local — no daemon REST writes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import httpx

#: Plugin filename as written under the host's auto-discovery directory.
PLUGIN_FILENAME = "mnemoseed-local.ts"

#: Daemon base URL the shipped plugin posts to when no env override is set.
DEFAULT_BASE_URL = "http://localhost:7788"

#: Probe budget for the status check (mirrors the plugin's request timeout).
PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class HookStatus:
    """Result of the status check.

    ``state`` is one of ``"not-installed"`` / ``"match"`` (installed file is
    byte-identical to the shipped plugin) / ``"differs"`` (installed but not
    the shipped bytes).
    """

    root: Path
    path: Path
    state: str
    daemon_reachable: bool
    base_url: str


def resolve_config_root() -> Path:
    """Resolve the OpenCode global config root (see the module docstring)."""
    override = os.environ.get("OPENCODE_CONFIG_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "opencode"


def resolve_base_url() -> str:
    """Daemon base URL the hook posts to (env override, else the default)."""
    return (os.environ.get("MNEMOSEED_LOCAL_BASEURL") or DEFAULT_BASE_URL).rstrip("/")


def plugin_bytes() -> bytes:
    """The shipped plugin source (wheel package data)."""
    return resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts").read_bytes()


def target_path(root: Path | None = None) -> Path:
    """The plugin's install path under the (resolved or given) config root."""
    return (root if root is not None else resolve_config_root()) / "plugin" / PLUGIN_FILENAME


def install_plugin(root: Path | None = None) -> tuple[Path, bool]:
    """Write the shipped plugin into the config root's auto-discovery dir.

    Returns ``(path, changed)``: ``changed`` is False when the installed file
    was already byte-identical (idempotent overwrite).
    """
    path = target_path(root)
    source = plugin_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == source:
        return path, False
    path.write_bytes(source)
    return path, True


def uninstall_plugin(root: Path | None = None) -> tuple[Path, bool]:
    """Remove the installed plugin file. Returns ``(path, existed)``; never
    touches anything else under the config root."""
    path = target_path(root)
    if not path.is_file():
        return path, False
    path.unlink()
    return path, True


def daemon_reachable(base_url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """GET <base>/healthz with a tight budget; any error means unreachable."""
    try:
        httpx.get(f"{base_url.rstrip('/')}/healthz", timeout=timeout)
    except Exception:
        return False
    return True


def hook_status(root: Path | None = None, base_url: str | None = None) -> HookStatus:
    """Report install state (bytes vs shipped plugin) + daemon reachability."""
    resolved_root = root if root is not None else resolve_config_root()
    path = target_path(resolved_root)
    if not path.is_file():
        state = "not-installed"
    elif path.read_bytes() == plugin_bytes():
        state = "match"
    else:
        state = "differs"
    resolved_base = base_url if base_url is not None else resolve_base_url()
    return HookStatus(
        root=resolved_root,
        path=path,
        state=state,
        daemon_reachable=daemon_reachable(resolved_base),
        base_url=resolved_base,
    )
