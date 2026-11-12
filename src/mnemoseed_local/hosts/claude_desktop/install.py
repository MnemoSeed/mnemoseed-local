"""Claude Desktop lifecycle: the ``mcpServers.mnemoseed`` entry in
``claude_desktop_config.json``.

The app reads that file from a directory resolved by DIRECTORY existence, in
this order: ``MNEMOSEED_CLAUDE_DESKTOP_CONFIG`` (an explicit file override for
tests and CI), then the Windows MSIX package directory under
``%LOCALAPPDATA%\\Claude-Data\\<pkg>\\``, then the platform classic path
(``%APPDATA%\\Claude\\``, ``~/Library/Application Support/Claude/``,
``~/.config/Claude/``). File existence would be the wrong test: an installed
package whose config has not been generated yet is the live surface, and writing
elsewhere produces a file the app never reads. Several package directories are
resolved by newest config mtime, and two of them already carrying a config file
refuse the operation instead of guessing.

Merge semantics: strict JSON, only ``mcpServers.mnemoseed`` is written
(``{"command": <absolute path>, "args": ["mcp"]}``), every other key survives
untouched, and a file that does not strict-parse or whose ``mcpServers`` is not
an object refuses with manual-edit guidance and stays byte-for-byte unchanged.
Rewrites are atomic (``hosts.common.config.write_atomic``).

The command is the running interpreter's absolute path, written only from an
installed runtime: a development checkout refuses, because pointing the app at a
build environment writes an entry it cannot spawn. Status reuses the shared
/healthz probe and HookStatus shape; all operations are local filesystem work.

The app owns account-level personal preferences that live in the cloud, so this
installer cannot write the chat-side protocol text — it prints the paste-ready
block instead, which is the boundary rather than a gap.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from mnemoseed_local.hosts import install as shared
from mnemoseed_local.hosts.common.config import ConfigParseError, load_object, require_object, write_atomic
from mnemoseed_local.hosts.install import HookStatus
from mnemoseed_local.mcp_gateway import instructions

#: Explicit config-file override: how a test or CI run picks the file to operate on.
CONFIG_OVERRIDE_ENV = "MNEMOSEED_CLAUDE_DESKTOP_CONFIG"

#: The app's own config filename, inside the resolved directory.
CONFIG_FILENAME = "claude_desktop_config.json"

#: The MSIX data root under ``%LOCALAPPDATA%``; each package gets a child.
MSIX_ROOT_NAME = "Claude-Data"

#: The classic config directory beneath the platform's config root.
CLASSIC_DIR_NAME = "Claude"

#: The one container and the one entry this adapter owns.
MCP_KEY = "mcpServers"
MCP_SERVER_NAME = "mnemoseed"

#: The stdio gateway verb the app spawns.
GATEWAY_ARGS = ("mcp",)

#: What the user is told when the surface cannot be decided or written.
DEV_RUN_GUIDANCE = (
    "this is a development run: Claude Desktop needs an installed CLI to spawn, and a build "
    "environment's interpreter is not one. Install the tool (`uv tool install mnemoseed-local`) "
    "and run this command from that install"
)


class ClaudeDesktopConfigError(ConfigParseError):
    """This adapter refuses to write: the config surface is undecidable (several
    package directories already carry a config file) or the running interpreter
    is not an installed runtime. Nothing is written."""


def _platform() -> str:
    """The platform whose Claude Desktop layout applies."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _classic_dir() -> Path:
    """This platform's classic config directory (created on write when absent)."""
    platform = _platform()
    if platform == "windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / CLASSIC_DIR_NAME
    if platform == "macos":
        return Path.home() / "Library" / "Application Support" / CLASSIC_DIR_NAME
    return Path.home() / ".config" / CLASSIC_DIR_NAME


def _msix_package_dirs() -> list[Path]:
    """The Windows MSIX package directories, in a stable order (empty elsewhere)."""
    if _platform() != "windows":
        return []
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    try:
        children = [child for child in (Path(local) / MSIX_ROOT_NAME).iterdir() if child.is_dir()]
    except OSError:
        return []
    return sorted(children, key=str)


def _package_age(package: Path) -> float:
    """The newest evidence of activity in a package directory: its config file's
    mtime when it has one, else the directory's own."""
    for candidate in (package / CONFIG_FILENAME, package):
        try:
            return candidate.stat().st_mtime
        except OSError:
            continue
    return 0.0


def _select_package_dir(packages: Sequence[Path]) -> Path:
    """One package directory out of several: the one that already carries a
    config file is the live surface; two of them are undecidable, so the
    operation refuses rather than writing into a directory the app may ignore."""
    configured = [package for package in packages if (package / CONFIG_FILENAME).is_file()]
    if len(configured) > 1:
        raise ClaudeDesktopConfigError(
            f"{len(configured)} Claude Desktop package directories already hold "
            f"{CONFIG_FILENAME} ({'; '.join(str(package) for package in configured)}): this tool "
            f"cannot tell which one the app reads. Point {CONFIG_OVERRIDE_ENV} at the file to use, "
            "or remove the stale one"
        )
    if configured:
        return configured[0]
    return max(packages, key=lambda package: (_package_age(package), str(package)))


def config_dir() -> Path:
    """The directory the app reads its config from (see the module docstring)."""
    packages = _msix_package_dirs()
    if packages:
        return _select_package_dir(packages)
    return _classic_dir()


def config_path() -> Path:
    """The config file to operate on: the override when set, else the resolved
    directory's config filename."""
    override = os.environ.get(CONFIG_OVERRIDE_ENV)
    if override:
        return Path(override)
    return config_dir() / CONFIG_FILENAME


def _installed_runtime() -> bool:
    """Whether this process is a packaged distribution rather than a development
    checkout: a bundler's frozen runtime, or an interpreter whose package is
    installed outside a ``src/`` layout."""
    if getattr(sys, "frozen", False):
        return True
    return Path(__file__).resolve().parents[3].name != "src"


def resolve_executable() -> str:
    """The absolute interpreter path a Desktop entry can spawn."""
    if not _installed_runtime():
        raise ClaudeDesktopConfigError(DEV_RUN_GUIDANCE)
    return str(Path(sys.executable).resolve())


def mcp_entry(executable: str) -> dict[str, object]:
    """The one entry this adapter writes."""
    return {"command": executable, "args": list(GATEWAY_ARGS)}


def _report(target: Path, executable: str, changed: bool) -> None:
    """The install output: what was written, when to restart the app, the
    paste-ready chat protocol, how to verify it, and the boundary that the chat
    surface has no capture hook."""
    shared.report_onboarding(
        "claude_desktop",
        hooks="not applicable - Claude Desktop has no hook surface",
        mcp="applied" if changed else "up to date",
    )
    print(f"host claude_desktop: config: {target}")
    print(f"host claude_desktop: command: {executable} {' '.join(GATEWAY_ARGS)}")
    print(
        "host claude_desktop: next: close Claude Desktop first, then restart it - a running app "
        "can rewrite this config from memory on exit and drop the new entry"
    )
    print(
        "host claude_desktop: verify: after the restart, ask a question only memory can answer "
        "(for example: what did we decide about the deployment script last week?) and check that "
        "the answer quotes remembered content"
    )
    print(
        "host claude_desktop: note: this chat surface has no capture hook - memory is written "
        "only when the assistant calls the tools itself"
    )
    print(
        "host claude_desktop: paste into personal preferences (Settings > Profile) - those "
        "preferences are account-level and this tool cannot write them:"
    )
    print(instructions.gateway_text("claude_chat"))


def install(path: Path | None = None, *, executable: str | None = None) -> tuple[Path, bool]:
    """Merge the MCP entry into the app's config; returns ``(path, changed)``."""
    resolved = executable if executable is not None else resolve_executable()
    target = path or config_path()
    data = load_object(target)
    servers = require_object(target, data, MCP_KEY)
    servers[MCP_SERVER_NAME] = mcp_entry(resolved)
    changed = write_atomic(target, data)
    _report(target, resolved, changed)
    return target, changed


def uninstall(path: Path | None = None) -> tuple[Path, bool]:
    """Remove only ``mcpServers.mnemoseed``; the container key goes when it
    empties. Foreign servers and every other key survive untouched."""
    target = path or config_path()
    if not target.is_file():
        return target, False
    data = load_object(target)
    servers = data.get(MCP_KEY)
    if not isinstance(servers, dict):
        if MCP_KEY in data:
            raise ClaudeDesktopConfigError(
                f'{target}: "mcpServers" exists but is not an object - fix it manually'
            )
        return target, False
    if MCP_SERVER_NAME not in servers:
        return target, False
    del servers[MCP_SERVER_NAME]
    if not servers:
        del data[MCP_KEY]
    write_atomic(target, data)
    return target, True


def _current_command() -> str | None:
    """The command this adapter would write now, or None when the running
    interpreter is not an installed runtime and cannot be compared against."""
    try:
        return resolve_executable()
    except ClaudeDesktopConfigError:
        return None


def status(path: Path | None = None, base_url: str | None = None) -> HookStatus:
    """The entry's state plus the shared /healthz reachability probe.

    States: ``not-installed`` / ``installed`` / ``stale`` (the configured command
    is not the one this surface would write now, which is how an entry pointing
    at a removed environment shows up) / ``unreadable`` (the file, or the
    mnemoseed entry inside it, cannot be parsed into the expected shape). A parse
    failure is never reported as ``stale`` and a command mismatch never as
    ``unreadable``.
    """
    target = path or config_path()
    try:
        data = load_object(target)
    except ConfigParseError:
        state = "unreadable"
    else:
        servers = data.get(MCP_KEY)
        if servers is not None and not isinstance(servers, dict):
            state = "unreadable"
        else:
            entry = (servers or {}).get(MCP_SERVER_NAME)
            command = entry.get("command") if isinstance(entry, dict) else None
            if entry is None:
                state = "not-installed"
            elif not isinstance(command, str) or not command.strip():
                state = "unreadable"
            else:
                current = _current_command()
                state = "stale" if current is not None and command != current else "installed"
    resolved_base = base_url if base_url is not None else shared.resolve_base_url()
    return HookStatus(
        root=target.parent,
        path=target,
        state=state,
        daemon_reachable=shared.daemon_reachable(resolved_base),
        base_url=resolved_base,
    )
