"""Cursor hook lifecycle: marked-entry surgery on ``~/.cursor/hooks.json``.

Install merges one marked command entry per subscribed event into the
user-level hooks file (``{"version": 1, "hooks": {"<event>": [<handler>]}}``,
the documented shape) and writes the canonical ``mnemoseed-local`` stdio
gateway into the sibling ``~/.cursor/mcp.json`` (``mcpServers`` key shape;
create-if-absent). Only the two user-level files are ever touched — never a
project ``.cursor/`` — and the gateway entry carries no secret and pins no
profile: the gateway attaches the daemon credential and reads the profile
from its own process environment per request. Foreign handlers, foreign MCP
servers (including ``${env:...}`` interpolated values), and any other user
keys are never modified.

Marker contract (``is_ours``): a handler whose command string's argv tail is
exactly the reserved ``_hook-event --host cursor`` form — the executable part
is environment-dependent (the tool shim on PATH, else the running interpreter
via ``-m``) and is NEVER compared; the argv tail alone is the reserved
signature. The MCP entry is ours by the same structural rule
(``is_ours_mcp_entry``: no remote url plus exactly one of the two reserved
gateway argv forms). A foreign command that merely mentions mnemoseed-local
is never ours; the loose substring probe (``_looks_like_ours``) only feeds
keep-warnings. There is no hooks master switch on Cursor, so no prestate
sidecar is written and uninstall restores nothing but the removal of our own
entries (the ``version`` field written on a fresh file stays — its origin is
not tracked).

Both files are strict-loaded BEFORE either write: a non-strict or
structurally malformed file (JSONC comments, a non-object ``hooks``/
``mcpServers`` block, a non-list event value) REFUSES the whole operation
with a ``ConfigParseError`` and every byte stays untouched. All rewrites are
atomic via ``hosts.common.config``: temp file + fsync + ``os.replace`` in the
target dir, UTF-8 bytes regardless of the process locale.

Status reuses the shared /healthz probe, base-URL resolution and HookStatus
shape from ``mnemoseed_local.hosts.install``; all operations are local
filesystem work — no daemon REST writes.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from mnemoseed_local.hosts import install as shared
from mnemoseed_local.hosts.common.commands import render_command
from mnemoseed_local.hosts.common.config import ConfigParseError, load_object, write_atomic
from mnemoseed_local.hosts.install import HookStatus

#: Reserved command fragment identifying our handler entries (warnings only).
MARKER = "mnemoseed-local"

#: The six hook events registered in v1 (PRD-293's frozen subscription of
#: Cursor's agent hooks: the permission/shell/file/subagent/Tab family and
#: workspaceOpen are deliberately not subscribed).
HOOK_EVENTS = (
    "sessionStart",
    "beforeSubmitPrompt",
    "postToolUse",
    "afterAgentResponse",
    "stop",
    "sessionEnd",
)

#: The reserved argv tail each registered handler runs (the hidden verb).
TRANSFORM_ARGS = ("_hook-event", "--host", "cursor")

#: The argv form used when the tool shim is not on PATH.
MODULE_TRANSFORM_ARGS = ("-m", "mnemoseed_local.cli") + TRANSFORM_ARGS

#: Per-entry budget in seconds (the zcode handler budget's magnitude).
HANDLER_TIMEOUT_SECONDS = 10

HOOKS_FILENAME = "hooks.json"
MCP_FILENAME = "mcp.json"
HOOKS_KEY = "hooks"
VERSION_KEY = "version"
HOOKS_VERSION = 1
MCP_KEY = "mcpServers"

#: Canonical MCP server key and the reserved stdio gateway argv forms.
MCP_SERVER_NAME = "mnemoseed-local"
MCP_GATEWAY_ARGS = ("mcp",)
MCP_MODULE_GATEWAY_ARGS = ("-m", "mnemoseed_local.cli") + MCP_GATEWAY_ARGS


def config_path() -> Path:
    """The user-level Cursor hooks file."""
    return Path.home() / ".cursor" / HOOKS_FILENAME


def _mcp_target(target: Path) -> Path:
    """The MCP file is the hooks file's documented sibling under ~/.cursor."""
    return target.parent / MCP_FILENAME


def resolve_command() -> tuple[str, tuple[str, ...]]:
    """(executable, args) the registered handlers run. The installed tool shim
    wins when it resolves on PATH; otherwise the running interpreter imports
    the CLI module directly — both forms are absolute-path references."""
    shim = shutil.which("mnemoseed-local")
    if shim:
        return str(Path(shim).resolve()), TRANSFORM_ARGS
    return sys.executable, MODULE_TRANSFORM_ARGS


def transform_command() -> str:
    """The registered handler command string; its argv tail is the marker. The
    executable head renders as one literal argument per the running platform
    (the shared portable renderer)."""
    executable, args = resolve_command()
    return render_command(executable, args, windows=os.name == "nt")


def mcp_server_entry() -> dict[str, object]:
    """The canonical MCP entry: the stdio gateway verb, whose command resolves
    exactly like the handlers' — the gateway attaches the daemon credential
    per request, so the host config carries no secret and pins no profile."""
    executable, args = resolve_command()
    gateway = MCP_GATEWAY_ARGS if args == TRANSFORM_ARGS else MCP_MODULE_GATEWAY_ARGS
    return {"command": executable, "args": list(gateway)}


def _handler() -> dict[str, object]:
    return {"command": transform_command(), "timeout": HANDLER_TIMEOUT_SECONDS}


def is_ours(handler: object) -> bool:
    """Marker contract for registration/removal decisions: the command
    string's argv tail is exactly the reserved form — the command itself is
    never compared."""
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    return isinstance(command, str) and tuple(command.split())[-len(TRANSFORM_ARGS) :] == TRANSFORM_ARGS


def is_ours_mcp_entry(entry: object) -> bool:
    """Marker contract for the MCP entry: no remote url plus exactly one of
    the two reserved gateway argv forms — the command is never compared."""
    if not isinstance(entry, dict) or "url" in entry:
        return False
    args = entry.get("args")
    return isinstance(args, list) and tuple(args) in (MCP_GATEWAY_ARGS, MCP_MODULE_GATEWAY_ARGS)


def _looks_like_ours(handler: object) -> bool:
    """Loose substring probe — stale-entry WARNINGS ONLY, never decisions."""
    return isinstance(handler, dict) and MARKER in str(handler.get("command", ""))


def _hooks_map(data: dict[str, object]) -> dict[str, object] | None:
    hooks = data.get(HOOKS_KEY)
    return hooks if isinstance(hooks, dict) else None


def _event_handlers(data: dict[str, object], event: str) -> list[object] | None:
    hooks = _hooks_map(data)
    if hooks is None:
        return None
    handlers = hooks.get(event)
    return handlers if isinstance(handlers, list) else None


def _mcp_servers(data: dict[str, object]) -> dict[str, object] | None:
    servers = data.get(MCP_KEY)
    return servers if isinstance(servers, dict) else None


def _ours_mcp_present(mcp_target: Path) -> bool:
    try:
        data = load_object(mcp_target)
    except ConfigParseError:
        return False
    servers = _mcp_servers(data)
    return isinstance(servers, dict) and is_ours_mcp_entry(servers.get(MCP_SERVER_NAME))


def _require_object(target: Path, container: dict[str, object], key: str) -> dict[str, object]:
    value = container.get(key)
    if isinstance(value, dict):
        return value
    if key in container:
        raise ConfigParseError(f"{target}: {key!r} exists but is not an object — fix it manually")
    empty: dict[str, object] = {}
    container[key] = empty
    return empty


def install(path: Path | None = None, dry_run: bool = False) -> tuple[Path, bool]:
    """Merge one marked handler per event into the hooks file and the
    canonical MCP entry into the sibling mcp.json (create-if-absent); returns
    ``(path, changed)``. Both files are strict-loaded before either write, so
    a malformed file refuses the whole install byte-for-byte untouched. A dry
    run previews the change without writing."""
    target = path or config_path()
    data = load_object(target)
    mcp_target = _mcp_target(target)
    mcp_data = load_object(mcp_target)
    if not data:
        data[VERSION_KEY] = HOOKS_VERSION
    hooks = _require_object(target, data, HOOKS_KEY)
    for event in HOOK_EVENTS:
        handlers = hooks.get(event)
        if isinstance(handlers, list):
            if not any(is_ours(h) for h in handlers):
                handlers.append(_handler())
            continue
        if handlers is not None:
            raise ConfigParseError(f"{target}: hooks.{event} exists but is not a list — fix it manually")
        hooks[event] = [_handler()]
    servers = _require_object(mcp_target, mcp_data, MCP_KEY)
    servers[MCP_SERVER_NAME] = mcp_server_entry()
    hooks_changed = write_atomic(target, data, dry_run=dry_run)
    mcp_changed = write_atomic(mcp_target, mcp_data, dry_run=dry_run)
    if not dry_run:
        shared.report_onboarding(
            "cursor",
            hooks="applied" if hooks_changed else "up to date",
            mcp="applied" if mcp_changed else "up to date",
        )
    return target, hooks_changed or mcp_changed


def uninstall(path: Path | None = None, dry_run: bool = False) -> tuple[Path, bool]:
    """Remove only marker-matched handler entries and the structure-matched
    MCP entry; foreign content stays value-identical. Returns ``(path,
    existed)``."""
    target = path or config_path()
    mcp_target = _mcp_target(target)
    if not target.is_file() and not mcp_target.is_file():
        return target, False
    data = load_object(target)
    mcp_data = load_object(mcp_target)
    stale = sorted(
        {
            str(handler.get("command", ""))
            for event in HOOK_EVENTS
            for handler in _event_handlers(data, event) or []
            if isinstance(handler, dict) and _looks_like_ours(handler) and not is_ours(handler)
        }
    )
    existed = False
    for event in HOOK_EVENTS:
        handlers = _event_handlers(data, event)
        if handlers is None:
            continue
        kept = [handler for handler in handlers if not is_ours(handler)]
        if len(kept) != len(handlers):
            existed = True
            if kept:
                handlers[:] = kept
            else:
                hooks = _hooks_map(data)
                if hooks is not None:
                    del hooks[event]
    servers = _mcp_servers(mcp_data)
    if isinstance(servers, dict) and is_ours_mcp_entry(servers.get(MCP_SERVER_NAME)):
        existed = True
        del servers[MCP_SERVER_NAME]
        if not servers:
            del mcp_data[MCP_KEY]
    hooks = _hooks_map(data)
    if isinstance(hooks, dict) and not hooks:
        del data[HOOKS_KEY]
    if not existed:
        return target, False
    if target.is_file():
        write_atomic(target, data, dry_run=dry_run)
    if mcp_target.is_file():
        write_atomic(mcp_target, mcp_data, dry_run=dry_run)
    if stale:
        print(
            "note: kept foreign hook command(s) that merely mention mnemoseed-local: " + "; ".join(stale),
            file=sys.stderr,
        )
    return target, existed


def status(path: Path | None = None, base_url: str | None = None) -> HookStatus:
    """Marker-detection state + shared /healthz reachability probe.

    States: ``not-installed`` / ``installed`` (every subscribed event carries
    our entry and the MCP entry is present) / ``partial`` (some of those are
    missing — an unreadable MCP file counts as a missing entry) / ``differs``
    (the hooks file exists but does not strict-parse). Cursor has no hooks
    master switch, so there is no ``disabled`` state.
    """
    target = path or config_path()
    state = "differs"
    try:
        data = load_object(target)
    except ConfigParseError:
        data = {}
    else:
        present = sum(
            1 for event in HOOK_EVENTS if any(is_ours(h) for h in _event_handlers(data, event) or [])
        )
        if present == 0:
            state = "not-installed"
        elif present < len(HOOK_EVENTS) or not _ours_mcp_present(_mcp_target(target)):
            state = "partial"
        else:
            state = "installed"
    resolved_base = base_url if base_url is not None else shared.resolve_base_url()
    return HookStatus(
        root=target.parent,
        path=target,
        state=state,
        daemon_reachable=shared.daemon_reachable(resolved_base),
        base_url=resolved_base,
    )
