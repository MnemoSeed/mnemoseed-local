"""Codex CLI hook lifecycle: marked-entry surgery on the sandbox CODEX_HOME.

The user-level ``hooks.json`` follows the Claude-Code-compatible shape Codex
consumes natively: a top-level ``hooks`` object maps each event to matcher
groups holding ``type: command`` handlers. Install keeps exactly one canonical
handler per subscribed event inside that container, and ``config.toml`` gains
the ``[mcp_servers.mnemoseed]`` stdio gateway entry.

Marker contract (``is_ours``): a handler whose command ends with the reserved
``_hook-event --host codex_cli`` argv tail — either the shim form or the
``-m mnemoseed_local.cli`` module form. The executable is environment
dependent, so the argv tail, not the path, is the signature; a foreign command
that merely mentions mnemoseed-local is never ours (the loose substring probe
only feeds keep-warnings). Handler commands render per platform: a PowerShell
call operator with a single-quoted literal on Windows, POSIX shell quoting
elsewhere.

Surgery rewrites only marker-owned handlers, harvesting both the nested
container and the historical flat root shape. Foreign groups, their matcher and
metadata fields, foreign handler values and the whole ``config.toml`` text stay
untouched: our canonical group is appended, and a group left without handlers
survives only when it carries metadata of its own.

Both files validate before either write lands: strict JSON/TOML parse first,
then the container/known-event/group schema, so an unexpected shape refuses
with ``ConfigParseError`` and both files keep their bytes. The ``config.toml``
rewrite swaps the line span of our canonical table and re-verifies the parse
before it is stored; every rewrite is atomic (temp file + fsync +
``os.replace``) and UTF-8 regardless of the process locale, and
``install(dry_run=True)`` previews without writing.

Status is structural: a complete canonical install reports trust unverified,
because Codex asks the user to review the hooks once inside ``/hooks``. This
tool never reads or writes a trust record, and daemon reachability is reported
apart from the structural state.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tomllib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from mnemoseed_local.hosts import install as shared
from mnemoseed_local.hosts.common.config import ConfigParseError, load_object, write_atomic, write_text_atomic
from mnemoseed_local.hosts.install import HookStatus

#: Reserved command fragment identifying our handler entries.
MARKER = "mnemoseed-local"

#: The four hook events we register in v1 (the official Codex hooks docs).
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")

#: The reserved argv tail each registered handler runs (the hidden verb).
TRANSFORM_ARGS = ("_hook-event", "--host", "codex_cli")

SHIM_TAIL = " ".join(TRANSFORM_ARGS)

#: The argv tail of the module form used when the tool shim is not on PATH.
MODULE_TAIL = " ".join(("-m", "mnemoseed_local.cli") + TRANSFORM_ARGS)

HOOKS_FILENAME = "hooks.json"
CONFIG_FILENAME = "config.toml"

#: The top-level object mapping events to matcher groups.
HOOKS_KEY = "hooks"

#: Our MCP server entry: the stdio gateway (canonical: exactly these keys).
MCP_SERVER_NAME = "mnemoseed"
MCP_TABLE_HEADER = f"[mcp_servers.{MCP_SERVER_NAME}]"
MCP_COMMAND_KEY = "command"
MCP_ARGS_KEY = "args"
MCP_GATEWAY_ARGS = ("mcp",)

#: Structural status of a complete canonical install: the host still asks the
#: user to approve the hooks once.
TRUST_UNVERIFIED = "installed, trust unverified (review and approve the hooks in Codex via /hooks)"


def codex_home() -> Path:
    """The sandbox CODEX_HOME override, else the user-level Codex home."""
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def hooks_path() -> Path:
    """The user-level Codex hooks file."""
    return codex_home() / HOOKS_FILENAME


def config_path() -> Path:
    """The user-level Codex config file."""
    return codex_home() / CONFIG_FILENAME


def transform_executable() -> str:
    """The absolute executable reference the registered handlers and the MCP
    entry run: the installed tool shim resolved on PATH, else the running
    interpreter."""
    shim = shutil.which("mnemoseed-local")
    return str(Path(shim).resolve()) if shim else sys.executable


def render_command(head: str, tail: Sequence[str], *, windows: bool) -> str:
    """One handler command line: ``head`` stays a single literal argument — a
    PowerShell single-quoted string after the call operator on Windows, a
    POSIX-quoted shell word elsewhere."""
    if windows:
        literal = "'" + head.replace("'", "''") + "'"
        return " ".join(["&", literal, *tail])
    return " ".join([shlex.quote(head), *tail])


def transform_command() -> str:
    """The handler command string: the executable plus the reserved argv tail
    (the shim form when ``mnemoseed-local`` is on PATH, else the module form)."""
    tail = (
        TRANSFORM_ARGS if shutil.which("mnemoseed-local") else ("-m", "mnemoseed_local.cli", *TRANSFORM_ARGS)
    )
    return render_command(transform_executable(), tail, windows=os.name == "nt")


def is_ours(handler: object) -> bool:
    """Marker contract for registration/removal decisions."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    if command == MODULE_TAIL or command.endswith(" " + MODULE_TAIL):
        return True
    head, sep, _ = command.rpartition(" " + SHIM_TAIL)
    return bool(sep) and "-m" not in head.split()


def _looks_like_ours(handler: object) -> bool:
    """Loose substring probe — stale-entry WARNINGS ONLY, never decisions."""
    return isinstance(handler, dict) and MARKER in str(handler.get("command", ""))


def _handler() -> dict[str, object]:
    return {"type": "command", "command": transform_command()}


def _group_handlers(group: object) -> list[object] | None:
    if not isinstance(group, dict):
        return None
    handlers = group.get(HOOKS_KEY)
    return handlers if isinstance(handlers, list) else None


def _iter_group_handlers(groups: object) -> Iterator[tuple[dict[str, object], dict[str, object]]]:
    """Yield ``(matcher group, handler)`` for every object handler in a list."""
    if not isinstance(groups, list):
        return
    for group in groups:
        if not isinstance(group, dict):
            continue
        for handler in _group_handlers(group) or []:
            if isinstance(handler, dict):
                yield group, handler


def _strip_event(groups: list[object]) -> bool:
    """Drop marker-owned handlers from one event's group list; a group left
    without handlers survives only when it carries metadata of its own."""
    changed = False
    kept: list[object] = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        handlers = _group_handlers(group)
        if handlers is None:
            kept.append(group)
            continue
        remaining = [handler for handler in handlers if not is_ours(handler)]
        if len(remaining) == len(handlers):
            kept.append(group)
            continue
        changed = True
        if not remaining and set(group) == {HOOKS_KEY}:
            continue
        group[HOOKS_KEY] = remaining
        kept.append(group)
    groups[:] = kept
    return changed


def _strip_event_in(holder: dict[str, object], event: str) -> bool:
    """Strip one event of one holder (nested container or flat root); an event
    left without groups goes with them."""
    groups = holder.get(event)
    if not isinstance(groups, list):
        return False
    changed = _strip_event(groups)
    if changed and not groups:
        del holder[event]
    return changed


def _strip_own(data: dict[str, object], container: dict[str, object] | None) -> bool:
    """Remove every marker-owned handler from both shapes; an emptied nested
    container goes too. Returns whether the document would change."""
    changed = False
    for holder in [container, data] if container is not None else [data]:
        for event in HOOK_EVENTS:
            changed = _strip_event_in(holder, event) or changed
    if container is not None and not container and changed:
        data.pop(HOOKS_KEY, None)
    return changed


def _is_canonical(groups: object, handler: dict[str, object]) -> bool:
    """Whether one event already carries exactly our canonical handler alone,
    in an unfiltered group holding no keys besides its handlers."""
    own = [(group, entry) for group, entry in _iter_group_handlers(groups) if is_ours(entry)]
    return (
        len(own) == 1
        and own[0][1] == handler
        and set(own[0][0]) == {HOOKS_KEY}
        and _group_handlers(own[0][0]) == [handler]
    )


def _merge_events(data: dict[str, object], container: dict[str, object]) -> bool:
    """Strip marker-owned handlers from both shapes, then keep one canonical
    group per event inside the nested container."""
    handler = _handler()
    changed = False
    for event in HOOK_EVENTS:
        changed = _strip_event_in(data, event) or changed
        if _is_canonical(container.get(event), handler):
            continue
        changed = _strip_event_in(container, event) or changed
        existing = container.get(event)
        groups: list[object] = existing if isinstance(existing, list) else []
        container[event] = groups
        groups.append({HOOKS_KEY: [handler]})
        changed = True
    return changed


def _loose_commands(data: dict[str, object], container: dict[str, object] | None) -> list[str]:
    """Commands that merely mention the marker — never decisions, only notes."""
    found: set[str] = set()
    for holder in [container, data] if container is not None else [data]:
        for event in HOOK_EVENTS:
            for _, handler in _iter_group_handlers(holder.get(event)):
                if _looks_like_ours(handler) and not is_ours(handler):
                    found.add(str(handler.get("command", "")))
    return sorted(found)


# ------------------------------------------------------------ schema validation


def _validate_groups(target: Path, event: str, groups: object) -> None:
    """One event's value must be matcher groups of object handlers."""
    if not isinstance(groups, list):
        raise ConfigParseError(f"{target}: {event} is not a list of matcher groups — fix it manually")
    for group in groups:
        if not isinstance(group, dict):
            raise ConfigParseError(
                f"{target}: {event} holds a matcher group that is not an object — fix it manually"
            )
        handlers = group.get(HOOKS_KEY)
        if HOOKS_KEY in group and not isinstance(handlers, list):
            raise ConfigParseError(
                f"{target}: {event} holds a group whose hooks is not a list — fix it manually"
            )
        for handler in handlers or []:
            if not isinstance(handler, dict):
                raise ConfigParseError(
                    f"{target}: {event} holds a hook handler that is not an object — fix it manually"
                )


def _validate(target: Path, data: dict[str, object]) -> dict[str, object] | None:
    """Refuse an unexpected container / known-event / group shape; returns the
    nested ``hooks`` container when the file already carries one."""
    container = data.get(HOOKS_KEY)
    if HOOKS_KEY in data and not isinstance(container, dict):
        raise ConfigParseError(f"{target}: {HOOKS_KEY} is not an object of hook events — fix it manually")
    for event in HOOK_EVENTS:
        if isinstance(container, dict) and event in container:
            _validate_groups(target, event, container[event])
        if event in data:
            _validate_groups(target, event, data[event])
    return container if isinstance(container, dict) else None


# ------------------------------------------------------------ config.toml marker-block surgery


def _read_config(target: Path) -> str:
    """The config text (BOM-tolerant); absent files read as empty."""
    if not target.is_file():
        return ""
    try:
        return target.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigParseError(f"{target} is not UTF-8 ({exc}); fix it manually, then retry") from exc


def _load_config(target: Path) -> str:
    """Strict tomllib parse of the config text; refusal keeps it untouched."""
    text = _read_config(target)
    if not text:
        return ""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigParseError(f"{target} is not valid TOML ({exc}); fix it manually, then retry") from exc
    return text


def _parsed(text: str) -> dict[str, Any]:
    return tomllib.loads(text) if text else {}


def _verify_toml(target: Path, text: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - belt-and-braces
        raise ConfigParseError(f"{target}: the rewrite does not parse ({exc}); file left untouched") from exc


def _require_replaceable(target: Path, parsed: dict[str, Any]) -> None:
    """A table under our name must be ours to manage: exactly the canonical
    keys, or absent. Anything richer belongs to the user and refuses."""
    servers = parsed.get("mcp_servers")
    if not isinstance(servers, dict) or MCP_SERVER_NAME not in servers:
        return
    entry = servers[MCP_SERVER_NAME]
    if not isinstance(entry, dict):
        raise ConfigParseError(f"{target}: mcp_servers.{MCP_SERVER_NAME} is not a table — fix it manually")
    if set(entry) - {MCP_COMMAND_KEY, MCP_ARGS_KEY}:
        raise ConfigParseError(
            f"{target}: {MCP_TABLE_HEADER} carries keys this tool does not manage — fix it manually"
        )


def _canonical_mcp_block() -> str:
    command = transform_executable()
    return (
        f"{MCP_TABLE_HEADER}\n"
        f"{MCP_COMMAND_KEY} = {json.dumps(command)}\n"
        f"{MCP_ARGS_KEY} = {json.dumps(list(MCP_GATEWAY_ARGS))}\n"
    )


def _table_span(lines: list[str]) -> tuple[int, int] | None:
    """(start, end) line span of our table: the header line through the line
    before the next top-level table header."""
    start = next((i for i, line in enumerate(lines) if line.rstrip("\r\n") == MCP_TABLE_HEADER), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not lines[end].startswith("["):
        end += 1
    return start, end


def _replace_table(text: str, block: str) -> tuple[str, bool]:
    """Swap our table's line span for the canonical block (append when the
    header is absent); the rest of the file stays byte-identical."""
    lines = text.splitlines(keepends=True)
    span = _table_span(lines)
    canonical = block.splitlines(keepends=True)
    if span is None:
        head = text if not text or text.endswith("\n") else text + "\n"
        return head + block, True
    start, end = span
    if lines[start:end] == canonical:
        return text, False
    return "".join(lines[:start] + canonical + lines[end:]), True


def _remove_table(text: str) -> tuple[str, bool]:
    """Drop our table's line span; the rest of the file stays byte-identical."""
    lines = text.splitlines(keepends=True)
    span = _table_span(lines)
    if span is None:
        return text, False
    start, end = span
    return "".join(lines[:start] + lines[end:]), True


# ------------------------------------------------------------ lifecycle


def install(dry_run: bool = False) -> tuple[Path, bool]:
    """Merge one canonical handler group per event into hooks.json and the
    canonical MCP entry into config.toml; returns ``(hooks path, changed)``.
    Both targets validate before either writes; a dry run previews without
    writing."""
    target = hooks_path()
    data = load_object(target)
    container = _validate(target, data)
    config = config_path()
    text = _load_config(config)
    _require_replaceable(config, _parsed(text))
    new_config, mcp_changed = _replace_table(text, _canonical_mcp_block())
    if mcp_changed:
        _verify_toml(config, new_config)
    if container is None:
        container = {}
        data[HOOKS_KEY] = container
    hooks_changed = _merge_events(data, container)
    if hooks_changed:
        hooks_changed = write_atomic(target, data, dry_run=dry_run)
    if mcp_changed:
        mcp_changed = write_text_atomic(config, new_config, dry_run=dry_run)
    if not dry_run:
        shared.report_onboarding(
            "codex_cli",
            hooks="applied" if hooks_changed else "up to date",
            mcp="applied" if mcp_changed else "up to date",
            trust="pending - review and approve the hooks in Codex via /hooks (manual, never automated)",
        )
    return target, hooks_changed or mcp_changed


def uninstall(dry_run: bool = False) -> tuple[Path, bool]:
    """Remove only marker-owned hook entries and the canonical MCP table;
    foreign groups, metadata, handlers and config text stay value-identical.
    Returns ``(hooks path, existed)``."""
    target = hooks_path()
    data = load_object(target)
    container = _validate(target, data)
    loose = _loose_commands(data, container)
    config = config_path()
    text = _load_config(config)
    _require_replaceable(config, _parsed(text))
    new_config, removed = _remove_table(text)
    if removed:
        _verify_toml(config, new_config)
    hooks_changed = _strip_own(data, container)
    if hooks_changed:
        write_atomic(target, data, dry_run=dry_run)
    if removed:
        write_text_atomic(config, new_config, dry_run=dry_run)
    if loose:
        print(
            "note: kept foreign hook command(s) that merely mention mnemoseed-local: " + "; ".join(loose),
            file=sys.stderr,
        )
    return target, hooks_changed or removed


# ------------------------------------------------------------ status


def _mcp_present(parsed: dict[str, Any]) -> bool:
    servers = parsed.get("mcp_servers")
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, dict) else None
    return entry == {MCP_COMMAND_KEY: transform_executable(), MCP_ARGS_KEY: list(MCP_GATEWAY_ARGS)}


def _has_own(groups: object) -> bool:
    return any(is_ours(handler) for _, handler in _iter_group_handlers(groups))


def _flat_own(data: dict[str, object]) -> bool:
    """Marker-owned handlers in the flat root shape."""
    return any(_has_own(data.get(event)) for event in HOOK_EVENTS)


def _any_own(data: dict[str, object], container: dict[str, object] | None) -> bool:
    if _flat_own(data):
        return True
    return container is not None and any(_has_own(container.get(event)) for event in HOOK_EVENTS)


def _structural_state(target: Path) -> str:
    """The structural install state alone; daemon reachability is reported apart."""
    try:
        data = load_object(target)
        container = _validate(target, data)
        parsed = _parsed(_load_config(config_path()))
    except ConfigParseError:
        return "malformed"
    handler = _handler()
    complete = (
        isinstance(container, dict)
        and not _flat_own(data)
        and _mcp_present(parsed)
        and all(_is_canonical(container.get(event), handler) for event in HOOK_EVENTS)
    )
    if complete:
        return TRUST_UNVERIFIED
    return "partial" if _any_own(data, container) else "not-installed"


def status(path: Path | None = None, base_url: str | None = None) -> HookStatus:
    """Structural state of the sandbox Codex home plus the shared /healthz
    reachability probe.

    States: ``not-installed`` / ``partial`` (a known event lacks our canonical
    handler, the shape is legacy flat, or the MCP entry is missing or drifted) /
    ``malformed`` (hooks.json or config.toml does not parse or has an unexpected
    shape) / the trust-unverified state of a complete canonical install.
    """
    target = path or hooks_path()
    resolved_base = base_url if base_url is not None else shared.resolve_base_url()
    return HookStatus(
        root=codex_home(),
        path=target,
        state=_structural_state(target),
        daemon_reachable=shared.daemon_reachable(resolved_base),
        base_url=resolved_base,
    )
