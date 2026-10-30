"""ZCode hook lifecycle: marked-entry surgery on ``~/.zcode/cli/config.json``.

The official ZCode hooks surface is the top-level ``hooks`` block shaped
``{ enabled?, timeoutMs?, maxOutputBytes?, events: { <Event>: [ { matcher?,
hooks: [...] } ] } }``; configuration-file hooks are DISABLED by default, so
install registers four ``type: process`` entries (one per subscribed event,
no matcher = match every firing) and explicitly flips ``hooks.enabled: true``
while recording the pre-state in a sidecar file next to the config — uninstall
rolls the switch back ONLY from that record (a lost record is honest unknown
and leaves the switch untouched). Install also writes the canonical
``mcp.servers["mnemoseed-local"]`` entry — a ``type: stdio`` registration of
the CLI ``mcp`` gateway, whose command resolves like the handlers' and which
attaches the daemon credential itself per request, so no secret lives in the
host config; foreign hooks groups, foreign MCP servers, and any other user
keys are never touched.

Marker contract (``is_ours``): a ``type: process`` handler whose ``args`` is
exactly one of the two reserved argv forms running the hidden
``_hook-event --host zcode`` verb — the command itself is environment-
dependent (the tool shim on PATH, else the running interpreter via ``-m``),
so the argv, not the path, is the reserved signature. The MCP entry is ours
by the same structural rule (``is_ours_mcp_entry``: ``type: stdio`` plus the
reserved gateway argv). A foreign command that merely mentions mnemoseed-local
is never ours; the loose substring probe (``_looks_like_ours``) only feeds
keep-warnings.

A non-strict or structurally malformed config (JSONC comments, a non-object
``hooks``/``events``/``mcp`` block, a non-boolean ``enabled``) REFUSES with a
``ConfigParseError`` instead of silently rewriting user data; the file stays
byte-for-byte untouched. All rewrites are atomic via
``hosts.common.config``: temp file + fsync + ``os.replace`` in the target
dir, UTF-8 bytes regardless of the process locale.

Status reuses the shared /healthz probe, base-URL resolution and HookStatus
shape from ``mnemoseed_local.hosts.install``; all operations are local
filesystem work — no daemon REST writes.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

from mnemoseed_local.hosts import install as shared
from mnemoseed_local.hosts.common.config import ConfigParseError, load_object, write_atomic
from mnemoseed_local.hosts.install import HookStatus

#: Reserved command fragment identifying our handler entries.
MARKER = "mnemoseed-local"

#: The four hook events we register in v1 (ZCode's seven documented events).
#: No SessionEnd: ZCode fires none and sessions stay open across turns —
#: durability rides Stop's flush and the daemon's idle-session sweep.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")

#: The reserved argv tail each registered handler runs (the hidden verb).
TRANSFORM_ARGS = ("_hook-event", "--host", "zcode")

#: The argv form used when the tool shim is not on PATH.
MODULE_TRANSFORM_ARGS = ("-m", "mnemoseed_local.cli") + TRANSFORM_ARGS

#: Process-handler budget (mirrors the claude_code handler timeout).
HANDLER_TIMEOUT_MS = 10000

HOOKS_KEY = "hooks"
ENABLED_KEY = "enabled"
EVENTS_KEY = "events"
MCP_KEY = "mcp"
MCP_SERVERS_KEY = "servers"

#: Canonical MCP server key and the reserved stdio gateway argv forms.
MCP_SERVER_NAME = "mnemoseed-local"
MCP_GATEWAY_ARGS = ("mcp",)
MCP_MODULE_GATEWAY_ARGS = ("-m", "mnemoseed_local.cli") + MCP_GATEWAY_ARGS

#: Sidecar next to the config recording the ``hooks.enabled`` pre-state.
PRESTATE_FILENAME = "mnemoseed-local.json"
PRESTATE_KEY = "hooks_enabled"


def config_path() -> Path:
    """The user-level ZCode CLI config file."""
    return Path.home() / ".zcode" / "cli" / "config.json"


def transform_command() -> tuple[str, tuple[str, ...]]:
    """(executable, args) the registered handlers run. The installed tool shim
    wins when it resolves on PATH; otherwise the running interpreter imports
    the CLI module directly — both forms are absolute-path references."""
    shim = shutil.which("mnemoseed-local")
    if shim:
        return str(Path(shim).resolve()), TRANSFORM_ARGS
    return sys.executable, MODULE_TRANSFORM_ARGS


def mcp_server_entry() -> dict[str, object]:
    """The canonical MCP entry: the stdio gateway verb, whose executable
    resolves exactly like the handlers' — the gateway attaches the daemon
    credential per request, so the host config carries no secret."""
    command, handler_args = transform_command()
    args = MCP_GATEWAY_ARGS if handler_args == TRANSFORM_ARGS else MCP_MODULE_GATEWAY_ARGS
    return {"type": "stdio", "command": command, "args": list(args)}


def _handler() -> dict[str, object]:
    command, args = transform_command()
    return {
        "type": "process",
        "command": command,
        "args": list(args),
        "timeoutMs": HANDLER_TIMEOUT_MS,
    }


def is_ours(handler: object) -> bool:
    """Marker contract for registration/removal decisions."""
    if not isinstance(handler, dict) or handler.get("type") != "process":
        return False
    args = handler.get("args")
    return isinstance(args, list) and tuple(args) in (TRANSFORM_ARGS, MODULE_TRANSFORM_ARGS)


def is_ours_mcp_entry(entry: object) -> bool:
    """Marker contract for the MCP entry: ``type: stdio`` plus exactly one of
    the two reserved gateway argv forms — the command is never compared."""
    if not isinstance(entry, dict) or entry.get("type") != "stdio":
        return False
    args = entry.get("args")
    return isinstance(args, list) and tuple(args) in (MCP_GATEWAY_ARGS, MCP_MODULE_GATEWAY_ARGS)


def _looks_like_ours(handler: object) -> bool:
    """Loose substring probe — stale-entry WARNINGS ONLY, never decisions."""
    return isinstance(handler, dict) and MARKER in str(handler.get("command", ""))


def _hooks_map(data: dict[str, object]) -> dict[str, object] | None:
    hooks = data.get(HOOKS_KEY)
    return hooks if isinstance(hooks, dict) else None


def _events_map(data: dict[str, object]) -> dict[str, object] | None:
    hooks = _hooks_map(data)
    if hooks is None:
        return None
    events = hooks.get(EVENTS_KEY)
    return events if isinstance(events, dict) else None


def _event_groups(data: dict[str, object], event: str) -> list[object] | None:
    events = _events_map(data)
    if events is None:
        return None
    groups = events.get(event)
    return groups if isinstance(groups, list) else None


def _group_handlers(group: object) -> list[object] | None:
    if not isinstance(group, dict):
        return None
    handlers = group.get("hooks")
    return handlers if isinstance(handlers, list) else None


def _iter_ours(
    data: dict[str, object],
) -> Iterator[tuple[str, dict[str, object], dict[str, object]]]:
    """Yield ``(event, matcher group, handler)`` for every marker-matched entry."""
    for event in HOOK_EVENTS:
        groups = _event_groups(data, event)
        if groups is None:
            continue
        for group in groups:
            handlers = _group_handlers(group)
            if handlers is None or not isinstance(group, dict):
                continue
            for handler in handlers:
                if isinstance(handler, dict) and is_ours(handler):
                    yield event, group, handler


def _mcp_servers(data: dict[str, object]) -> dict[str, object] | None:
    mcp = data.get(MCP_KEY)
    if not isinstance(mcp, dict):
        return None
    servers = mcp.get(MCP_SERVERS_KEY)
    return servers if isinstance(servers, dict) else None


def _ours_mcp_present(data: dict[str, object]) -> bool:
    servers = _mcp_servers(data)
    return isinstance(servers, dict) and is_ours_mcp_entry(servers.get(MCP_SERVER_NAME))


def _prestate_path(target: Path) -> Path:
    return target.parent / PRESTATE_FILENAME


def _read_prestate(target: Path) -> tuple[bool, bool | None]:
    """(found, value) of the recorded ``hooks.enabled`` pre-state; ``None``
    means the key was absent when install flipped it."""
    path = _prestate_path(target)
    if not path.is_file():
        return False, None
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return False, None
    if not isinstance(record, dict) or PRESTATE_KEY not in record:
        return False, None
    value = record[PRESTATE_KEY]
    if value is None or isinstance(value, bool):
        return True, value
    return False, None


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
    """Merge one marked handler group per event, flip ``hooks.enabled: true``
    (recording the pre-state — the first record wins on reinstall), and write
    the canonical MCP entry; returns ``(path, changed)``. A dry run previews
    the change without writing."""
    target = path or config_path()
    data = load_object(target)
    hooks = _require_object(target, data, HOOKS_KEY)
    enabled_pre = hooks.get(ENABLED_KEY)
    if enabled_pre is not None and not isinstance(enabled_pre, bool):
        raise ConfigParseError(f"{target}: hooks.{ENABLED_KEY} is not a boolean — fix it manually")
    events = _require_object(target, hooks, EVENTS_KEY)
    for event in HOOK_EVENTS:
        groups = events.get(event)
        if isinstance(groups, list):
            already = any(is_ours(h) for g in groups for h in _group_handlers(g) or [])
            if not already:
                groups.append({"hooks": [_handler()]})
            continue
        if groups is not None:
            raise ConfigParseError(
                f"{target}: hooks.events.{event} exists but is not a list — fix it manually"
            )
        groups = [{"hooks": [_handler()]}]
        events[event] = groups
    hooks[ENABLED_KEY] = True
    mcp = _require_object(target, data, MCP_KEY)
    servers = _require_object(target, mcp, MCP_SERVERS_KEY)
    servers[MCP_SERVER_NAME] = mcp_server_entry()
    changed = write_atomic(target, data, dry_run=dry_run)
    if changed and not dry_run:
        found, _ = _read_prestate(target)
        if not found:
            write_atomic(_prestate_path(target), {PRESTATE_KEY: enabled_pre})
    if not dry_run:
        shared.report_onboarding(
            "zcode",
            hooks="applied" if changed else "up to date",
            mcp="applied" if changed else "up to date",
        )
    return target, changed


def uninstall(path: Path | None = None, dry_run: bool = False) -> tuple[Path, bool]:
    """Remove only marker-matched entries, our structure-matched MCP entry,
    and — per the recorded pre-state — the ``hooks.enabled`` flip; foreign
    hooks and MCP entries stay value-identical. Returns ``(path, existed)``."""
    target = path or config_path()
    if not target.is_file():
        return target, False
    data = load_object(target)
    stale = sorted(
        {
            str(handler.get("command", ""))
            for event in HOOK_EVENTS
            for group in _event_groups(data, event) or []
            for handler in _group_handlers(group) or []
            if isinstance(handler, dict) and _looks_like_ours(handler) and not is_ours(handler)
        }
    )
    existed = False
    # materialize first: removals mutate the containers being iterated
    for event, group, handler in list(_iter_ours(data)):
        existed = True
        handlers = _group_handlers(group) or []
        handlers.remove(handler)
        if not handlers:
            groups = _event_groups(data, event) or []
            groups.remove(group)
            if not groups:
                events = _events_map(data)
                if events is not None:
                    del events[event]
    if not existed:
        return target, False
    events = _events_map(data)
    hooks = _hooks_map(data)
    if isinstance(events, dict) and isinstance(hooks, dict):
        if not events:
            del hooks[EVENTS_KEY]
        found, value = _read_prestate(target)
        if found and value is None:
            hooks.pop(ENABLED_KEY, None)
        elif found and value is False:
            hooks[ENABLED_KEY] = False
        if not hooks:
            del data[HOOKS_KEY]
    servers = _mcp_servers(data)
    if isinstance(servers, dict) and is_ours_mcp_entry(servers.get(MCP_SERVER_NAME)):
        del servers[MCP_SERVER_NAME]
        mcp = data.get(MCP_KEY)
        if isinstance(mcp, dict):
            if not servers:
                del mcp[MCP_SERVERS_KEY]
            if not mcp:
                del data[MCP_KEY]
    write_atomic(target, data, dry_run=dry_run)
    if not dry_run:
        try:
            _prestate_path(target).unlink()
        except OSError:
            pass
    if stale:
        print(
            "note: kept foreign hook command(s) that merely mention mnemoseed-local: " + "; ".join(stale),
            file=sys.stderr,
        )
    return target, existed


def status(path: Path | None = None, base_url: str | None = None) -> HookStatus:
    """Marker-detection state + shared /healthz reachability probe.

    States: ``not-installed`` / ``installed`` (all four events + the MCP
    entry + the switch on) / ``disabled`` (entries present, switch off) /
    ``partial`` (some event lacks our entry, or the MCP entry is missing) /
    ``differs`` (config exists but does not strict-parse).
    """
    target = path or config_path()
    state = "differs"
    try:
        data = load_object(target)
    except ConfigParseError:
        data = {}
    else:
        ours_by_event: dict[str, list[dict[str, object]]] = {event: [] for event in HOOK_EVENTS}
        for event, _, handler in _iter_ours(data):
            ours_by_event[event].append(handler)
        present = sum(1 for ours in ours_by_event.values() if ours)
        hooks = _hooks_map(data)
        enabled = isinstance(hooks, dict) and hooks.get(ENABLED_KEY) is True
        if present == 0:
            state = "not-installed"
        elif not enabled:
            state = "disabled"
        elif present < len(HOOK_EVENTS) or not _ours_mcp_present(data):
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
