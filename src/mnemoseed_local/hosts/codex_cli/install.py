"""Codex CLI hook lifecycle: marked-entry surgery on the sandbox CODEX_HOME.

The user-level ``hooks.json`` follows the Claude-Code-compatible shape Codex
consumes natively (per-event matcher groups holding ``type: command``
handlers); install merges one marked handler per subscribed event, and
``config.toml`` gains the ``[mcp_servers.mnemoseed]`` stdio gateway entry.
Marker contract (``is_ours``): a handler whose command ends with the
reserved ``_hook-event --host codex_cli`` argv tail — either the shim form
or the full ``-m`` module form. The executable is environment-dependent, so
the argv tail, not the path, is the signature; a foreign command that merely
mentions mnemoseed-local is never ours (the loose substring probe only feeds
keep-warnings).

config.toml surgery is marker-block text surgery: a strict ``tomllib`` parse
first (a broken file refuses untouched), then the exact canonical table
block is appended or removed and the rewrite is re-verified before any
bytes land — user comments and foreign tables stay byte-identical. A
``[mcp_servers.mnemoseed]`` table carrying keys this tool does not manage
refuses with manual-edit guidance instead of being overwritten. All
rewrites are atomic (temp file + fsync + ``os.replace`` in the target dir),
UTF-8 bytes regardless of the process locale; ``--dry-run`` is module-level
(``install(dry_run=True)``).

The hooks trust gate is Codex-side: after install the user must review and
trust the hooks once via ``/hooks``, and Codex records the content hash
under ``[hooks.state]`` in config.toml. Status reports the three honest
states: no ``trusted_hash`` entries -> installed but not yet trusted;
entries present but none matching the sha256 of the current hooks.json
bytes -> stale trust (the file changed; re-trust via /hooks); a match ->
trusted. A mismatch always advises the re-trust, which is the safe
direction.

Status reuses the shared /healthz probe, base-URL resolution and HookStatus
shape from ``mnemoseed_local.hosts.install``; all operations are local
filesystem work — no daemon REST writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tomllib
from collections.abc import Iterator
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

HOOKS_KEY = "hooks"
STATE_KEY = "state"
TRUSTED_HASH_KEY = "trusted_hash"

#: Our MCP server entry: the stdio gateway (canonical: exactly these keys).
MCP_SERVER_NAME = "mnemoseed"
MCP_TABLE_HEADER = f"[mcp_servers.{MCP_SERVER_NAME}]"
MCP_COMMAND_KEY = "command"
MCP_ARGS_KEY = "args"
MCP_GATEWAY_ARGS = ("mcp",)

#: The honest trust states (exact status strings).
TRUST_PENDING = "installed, not yet trusted"
TRUST_STALE = "stale trust (hooks changed; re-trust via /hooks)"


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


def transform_command() -> str:
    """The handler command string (the executable plus the reserved argv)."""
    module_form = ("-m", "mnemoseed_local.cli") + TRANSFORM_ARGS
    tail = TRANSFORM_ARGS if shutil.which("mnemoseed-local") else module_form
    return _join_command([transform_executable(), *tail])


def _join_command(argv: list[str]) -> str:
    head, *tail = argv
    joined = f'"{head}"' if " " in head else head
    return " ".join([joined, *tail])


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
    handlers = group.get("hooks")
    return handlers if isinstance(handlers, list) else None


def _iter_ours(data: dict[str, object]) -> Iterator[tuple[str, dict[str, object], dict[str, object]]]:
    """Yield ``(event, matcher group, handler)`` for every marker-matched entry."""
    for event in HOOK_EVENTS:
        groups = data.get(event)
        if not isinstance(groups, list):
            continue
        for group in groups:
            handlers = _group_handlers(group)
            if handlers is None or not isinstance(group, dict):
                continue
            for handler in handlers:
                if isinstance(handler, dict) and is_ours(handler):
                    yield event, group, handler


def _merge_hooks(target: Path, data: dict[str, object], dry_run: bool) -> bool:
    """Merge one marked handler group per event; returns whether bytes moved."""
    for event in HOOK_EVENTS:
        groups = data.get(event)
        if isinstance(groups, list):
            if not any(is_ours(h) for g in groups for h in _group_handlers(g) or []):
                groups.append({"hooks": [_handler()]})
            continue
        if groups is not None:
            raise ConfigParseError(f"{target}: {event} exists but is not a list — fix it manually")
        data[event] = [{"hooks": [_handler()]}]
    return write_atomic(target, data, dry_run=dry_run)


def _loose_ours(data: dict[str, object]) -> list[str]:
    """Commands that merely mention the marker — never decisions, only notes."""
    found = set()
    for event in HOOK_EVENTS:
        groups = data.get(event)
        if not isinstance(groups, list):
            continue
        for group in groups:
            for handler in _group_handlers(group) or []:
                if isinstance(handler, dict) and _looks_like_ours(handler) and not is_ours(handler):
                    found.add(str(handler.get("command", "")))
    return sorted(found)


def _strip_hooks(target: Path, dry_run: bool = False) -> bool:
    """Remove only marker-matched entries; foreign hooks stay value-identical."""
    if not target.is_file():
        return False
    data = load_object(target)
    loose = _loose_ours(data)
    existed = False
    # materialize first: removals mutate the containers being iterated
    for event, group, handler in list(_iter_ours(data)):
        existed = True
        handlers = _group_handlers(group) or []
        handlers.remove(handler)
        if not handlers:
            groups = data.get(event)
            if isinstance(groups, list):
                groups.remove(group)
                if not groups:
                    del data[event]
    if existed:
        write_atomic(target, data, dry_run=dry_run)
    if loose:
        print(
            "note: kept foreign hook command(s) that merely mention mnemoseed-local: " + "; ".join(loose),
            file=sys.stderr,
        )
    return existed


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
    """Merge one marked handler group per event into hooks.json and the
    canonical MCP entry into config.toml; returns ``(hooks path, changed)``.
    Both targets validate before either writes; a dry run previews without
    writing."""
    target = hooks_path()
    data = load_object(target)
    config = config_path()
    text = _load_config(config)
    _require_replaceable(config, _parsed(text))
    new_config, mcp_changed = _replace_table(text, _canonical_mcp_block())
    if mcp_changed:
        _verify_toml(config, new_config)
    hooks_changed = _merge_hooks(target, data, dry_run)
    if mcp_changed:
        write_text_atomic(config, new_config, dry_run=dry_run)
    if hooks_changed and not dry_run:
        print("next: review and trust the hooks once inside Codex via /hooks")
    return target, hooks_changed or mcp_changed


def uninstall(dry_run: bool = False) -> tuple[Path, bool]:
    """Remove only marker-matched hook entries and the canonical MCP table;
    foreign hooks, MCP entries, and any other config bytes stay value-
    identical. Returns ``(hooks path, existed)``."""
    target = hooks_path()
    config = config_path()
    text = _load_config(config)
    _require_replaceable(config, _parsed(text))
    new_config, removed = _remove_table(text)
    if removed:
        _verify_toml(config, new_config)
    existed = _strip_hooks(target, dry_run=dry_run)
    if removed:
        write_text_atomic(config, new_config, dry_run=dry_run)
    return target, existed or removed


# ------------------------------------------------------------ status + trust


def _content_digest(hooks_file: Path) -> str | None:
    try:
        return hashlib.sha256(hooks_file.read_bytes()).hexdigest()
    except OSError:
        return None


def _trusted_hashes(target: Path) -> set[str]:
    """Every ``trusted_hash`` Codex recorded under ``[hooks.state]``; an
    unreadable or absent config means nothing was trusted yet."""
    try:
        parsed = _parsed(_read_config(target))
    except (OSError, ValueError):
        return set()
    hooks = parsed.get(HOOKS_KEY)
    state = hooks.get(STATE_KEY) if isinstance(hooks, dict) else None
    if not isinstance(state, dict):
        return set()
    found: set[str] = set()
    for record in state.values():
        if isinstance(record, dict):
            digest = record.get(TRUSTED_HASH_KEY)
            if isinstance(digest, str) and digest:
                found.add(digest)
    return found


def _trust_state(hooks_file: Path) -> str:
    """The three honest trust states from config.toml's ``[hooks.state]``."""
    entries = _trusted_hashes(config_path())
    if not entries:
        return TRUST_PENDING
    digest = _content_digest(hooks_file)
    return "installed" if digest is not None and digest in entries else TRUST_STALE


def _mcp_present(parsed: dict[str, Any]) -> bool:
    servers = parsed.get("mcp_servers")
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, dict) else None
    return entry == {MCP_COMMAND_KEY: transform_executable(), MCP_ARGS_KEY: list(MCP_GATEWAY_ARGS)}


def status(path: Path | None = None, base_url: str | None = None) -> HookStatus:
    """Marker-detection state + shared /healthz reachability probe.

    States: ``not-installed`` / ``partial`` (some event lacks our entry, or
    the MCP entry is missing or drifted) / ``differs`` (hooks.json exists but
    does not strict-parse) / the three trust states of a full install.
    """
    target = path or hooks_path()
    state = "differs"
    try:
        data = load_object(target)
    except ConfigParseError:
        data = {}
    else:
        present = sum(1 for event in HOOK_EVENTS if _has_ours(data, event))
        if present == 0:
            state = "not-installed"
        elif present < len(HOOK_EVENTS) or not _mcp_entry_live():
            state = "partial"
        else:
            state = _trust_state(target)
    resolved_base = base_url if base_url is not None else shared.resolve_base_url()
    return HookStatus(
        root=codex_home(),
        path=target,
        state=state,
        daemon_reachable=shared.daemon_reachable(resolved_base),
        base_url=resolved_base,
    )


def _mcp_entry_live() -> bool:
    """The canonical MCP entry as the file stands now; an unreadable config
    reads as absent (a read-only probe never raises)."""
    try:
        parsed = _parsed(_read_config(config_path()))
    except ConfigParseError:
        return False
    return _mcp_present(parsed)


def _has_ours(data: dict[str, object], event: str) -> bool:
    groups = data.get(event)
    if not isinstance(groups, list):
        return False
    return any(isinstance(h, dict) and is_ours(h) for g in groups for h in _group_handlers(g) or [])
