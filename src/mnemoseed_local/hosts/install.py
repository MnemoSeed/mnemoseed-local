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

B2.6 bundle switch: the install surface is a FILE COPY (A3-pinned:
byte-identical write, three-state status, one-file uninstall), so the switch
is a RENAME to ``*.ts.disabled`` — the host's glob matches only ``.ts``/``.js``,
so the renamed file stops loading without being deleted (research doc §1/§5).
The alternative — an opencode.json plugin-array tuple ``["spec", {enabled:false}]``
— would (a) break the A3-pinned file-based install/status/uninstall contract
with JSON surgery and (b) deliver ``options: null`` to a dir-scanned copy
(B2.6 probe round 1), so the tuple cannot reach the installed file without
double-loading it. The shipped plugin still honors the tuple switch for users
who register the array form manually (probe round 2 confirmed the options
tuple is delivered).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import httpx

#: Plugin filename as written under the host's auto-discovery directory.
PLUGIN_FILENAME = "mnemoseed-local.ts"

#: Disabled rename suffix (B2.6 switch): the host's plugin glob matches only
#: ``*.ts``/``*.js``, so ``mnemoseed-local.ts.disabled`` stops loading.
DISABLED_SUFFIX = ".disabled"

#: Daemon base URL the shipped plugin posts to when no env override is set.
DEFAULT_BASE_URL = "http://localhost:7788"

#: Probe budget for the status check (mirrors the plugin's request timeout).
PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class HookStatus:
    """Result of the status check.

    ``state`` is one of ``"not-installed"`` / ``"match"`` (installed file is
    byte-identical to the shipped plugin) / ``"differs"`` (installed but not
    the shipped bytes) / ``"disabled"`` (renamed to ``*.ts.disabled``).
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


def disabled_path(root: Path | None = None) -> Path:
    """The plugin's disabled (renamed) path under the config root."""
    return Path(str(target_path(root)) + DISABLED_SUFFIX)


def install_plugin(root: Path | None = None) -> tuple[Path, bool]:
    """Write the shipped plugin into the config root's auto-discovery dir.

    Returns ``(path, changed)``: ``changed`` is False when the installed file
    was already byte-identical (idempotent overwrite). Install means enabled:
    a stale ``*.ts.disabled`` remnant is cleared so status/uninstall always
    see one coherent state.
    """
    path = target_path(root)
    source = plugin_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    disabled = disabled_path(root)
    remnant = disabled.is_file()
    if remnant:
        disabled.unlink()
    if path.is_file() and path.read_bytes() == source:
        return path, remnant
    path.write_bytes(source)
    return path, True


def uninstall_plugin(root: Path | None = None) -> tuple[Path, bool]:
    """Remove the installed plugin file — and a disabled remnant if present
    (uninstall means fully gone). Returns ``(path, existed)`` where existed
    covers either form; never touches anything else under the config root."""
    path = target_path(root)
    disabled = disabled_path(root)
    existed = path.is_file() or disabled.is_file()
    if path.is_file():
        path.unlink()
    if disabled.is_file():
        disabled.unlink()
    return path, existed


def disable_plugin(root: Path | None = None) -> tuple[Path, bool]:
    """Rename the installed plugin to ``*.ts.disabled`` (B2.6 switch): the
    host's glob stops loading it at the next startup. Returns ``(path,
    changed)``; changed is False when nothing active was installed (a stale
    remnant alone is not re-disabled). Atomic via ``Path.replace`` (os.replace)
    — the host's glob matches only ``.ts``/``.js``, so the renamed file stops
    loading without being deleted."""
    path = target_path(root)
    disabled = disabled_path(root)
    try:
        path.replace(disabled)
    except FileNotFoundError:
        return path, False
    except (FileExistsError, OSError):
        try:
            if disabled.exists():
                disabled.unlink()
        except OSError:
            pass
        try:
            path.replace(disabled)
        except FileNotFoundError:
            return path, False
        except (FileExistsError, OSError):
            return path, False
    return path, True


def enable_plugin(root: Path | None = None) -> tuple[Path, bool]:
    """Rename the disabled plugin back to the active name (re-enable).
    Returns ``(path, changed)``; changed is False when there is nothing
    disabled to restore. Atomic via ``Path.replace`` — handles the active
    file already existing (concurrent install) by overwriting atomically,
    with an unlink-retry fallback for Windows file-lock races."""
    path = target_path(root)
    disabled = disabled_path(root)
    try:
        disabled.replace(path)
    except FileNotFoundError:
        return path, False
    except (FileExistsError, OSError):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        try:
            disabled.replace(path)
        except FileNotFoundError:
            return path, False
        except (FileExistsError, OSError):
            return path, False
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
    disabled = disabled_path(resolved_root)
    if not path.is_file():
        try:
            is_disabled = disabled.is_file()
        except OSError:
            is_disabled = False
        state = "disabled" if is_disabled else "not-installed"
    else:
        try:
            data = path.read_bytes()
        except (FileNotFoundError, PermissionError, OSError):
            # transient race: file disappeared or locked between is_file and read
            try:
                is_disabled = disabled.is_file()
            except OSError:
                is_disabled = False
            if is_disabled:
                state = "disabled"
            else:
                # re-check if path still exists; if not, treat as not-installed else differs
                try:
                    state = "not-installed" if not path.is_file() else "differs"
                except OSError:
                    state = "not-installed"
        else:
            state = "match" if data == plugin_bytes() else "differs"
    resolved_base = base_url if base_url is not None else resolve_base_url()
    return HookStatus(
        root=resolved_root,
        path=path,
        state=state,
        daemon_reachable=daemon_reachable(resolved_base),
        base_url=resolved_base,
    )
