"""Claude Code hook lifecycle: marked-entry surgery on ``~/.claude/settings.json``.

Unlike file-copy hosts (opencode auto-discovers a plugin directory), Claude
Code owns a single user settings file. Install therefore MERGES idempotent,
marker-matched handler entries into ``hooks.{UserPromptSubmit,Stop,
PostToolUse,PreCompact,SessionEnd}``; foreign entries are never touched.
Marker contract (``is_ours``): the command is exactly ``TRANSFORM_COMMAND``
or starts with the reserved prefix ``MARKER + " "`` — a foreign command that
merely mentions mnemoseed-local is never ours; the loose substring probe
(``_looks_like_ours``) only feeds keep-warnings.

disable/enable flip a ``disabled`` flag on our entries only (CC has no glob
discovery to rename out of). Official CC docs specify plain JSON for settings
files, so a non-strict or structurally malformed file (JSONC comments, a
non-object ``hooks`` block) REFUSES with manual-edit guidance instead of
silently rewriting user data; the file stays byte-for-byte untouched. All
rewrites are atomic: temp file + fsync + ``os.replace`` in the target dir, so
a crash mid-write can never destroy the user's settings.

Status reuses the shared /healthz probe, base-URL resolution and HookStatus
shape from ``mnemoseed_local.hosts.install``; all operations are local
filesystem work — no daemon REST writes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from mnemoseed_local.hosts import install as shared
from mnemoseed_local.hosts.install import HookStatus

#: Reserved command prefix identifying our handler entries.
MARKER = "mnemoseed-local"

#: The five hook events we register in v1.
HOOK_EVENTS = ("UserPromptSubmit", "Stop", "PostToolUse", "PreCompact", "SessionEnd")

#: Command each registered handler runs (CC pipes the event JSON via stdin).
TRANSFORM_COMMAND = "mnemoseed-local _hook-event --host claude_code"

_HANDLER_TIMEOUT_SECONDS = 10


class SettingsParseError(Exception):
    """settings.json is not strict JSON with the expected structure; untouched."""


def settings_path() -> Path:
    """The user-level Claude Code settings file."""
    return Path.home() / ".claude" / "settings.json"


def _handler() -> dict[str, object]:
    return {
        "type": "command",
        "command": TRANSFORM_COMMAND,
        "timeout": _HANDLER_TIMEOUT_SECONDS,
    }


def is_ours(handler: object) -> bool:
    """Marker contract for registration/removal decisions."""
    if not isinstance(handler, dict):
        return False
    command = str(handler.get("command", ""))
    return command == TRANSFORM_COMMAND or command.startswith(MARKER + " ")


def _looks_like_ours(handler: object) -> bool:
    """Loose substring probe — stale-entry WARNINGS ONLY, never decisions."""
    return isinstance(handler, dict) and MARKER in str(handler.get("command", ""))


def _load(target: Path) -> dict[str, object]:
    """Strict-JSON load; blank/absent files read as empty settings. utf-8-sig
    tolerates a BOM-prefixed file (stripped on the next atomic rewrite)."""
    if not target.is_file():
        return {}
    text = target.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SettingsParseError(
            f"{target} is not strict JSON ({exc}); Claude Code documents plain "
            "JSON settings, so this tool never rewrites commented files — fix "
            "it manually (add our hook command) or remove the comments, then retry"
        ) from exc
    if not isinstance(data, dict):
        raise SettingsParseError(f"{target} does not contain a JSON object")
    return data


def _serialize(data: dict[str, object]) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_new(target: Path, data: dict[str, object]) -> bool:
    payload = _serialize(data)
    if target.is_file() and target.read_bytes() == payload:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return True


def _hooks_map(data: dict[str, object]) -> dict[str, object] | None:
    hooks = data.get("hooks")
    return hooks if isinstance(hooks, dict) else None


def _event_groups(data: dict[str, object], event: str) -> list[object] | None:
    hooks = _hooks_map(data)
    if hooks is None:
        return None
    groups = hooks.get(event)
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


def install(path: Path | None = None) -> tuple[Path, bool]:
    """Merge one marked handler group per event; returns ``(path, changed)``."""
    target = path or settings_path()
    data = _load(target)
    hooks = _hooks_map(data)
    if hooks is None:
        if "hooks" in data:
            # a malformed foreign value must be refused, never overwritten
            raise SettingsParseError(f'{target}: "hooks" exists but is not an object — fix it manually')
        hooks = {}
        data["hooks"] = hooks
    for event in HOOK_EVENTS:
        existing = hooks.get(event)
        if isinstance(existing, list):
            already = any(is_ours(h) for g in existing for h in _group_handlers(g) or [])
            if not already:
                existing.append({"hooks": [_handler()]})
            continue
        if existing is not None:
            # a malformed foreign value must be refused, never overwritten
            raise SettingsParseError(f"{target}: hooks.{event} exists but is not a list — fix it manually")
        hooks[event] = [{"hooks": [_handler()]}]
    return target, _write_new(target, data)


def uninstall(path: Path | None = None) -> tuple[Path, bool]:
    """Remove only marker-matched entries; foreign hooks stay bit-for-bit."""
    target = path or settings_path()
    if not target.is_file():
        return target, False
    data = _load(target)
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
                hooks = _hooks_map(data)
                if hooks is not None:
                    del hooks[event]
    if existed:
        _write_new(target, data)
    if stale:
        print(
            "note: kept foreign hook command(s) that merely mention mnemoseed-local: " + "; ".join(stale),
            file=sys.stderr,
        )
    return target, existed


def disable(path: Path | None = None) -> tuple[Path, bool]:
    """Set ``disabled: true`` on our entries only; returns ``(path, changed)``."""
    return _toggle_disabled(path, True)


def enable(path: Path | None = None) -> tuple[Path, bool]:
    """Remove the ``disabled`` flag from our entries; returns ``(path, changed)``."""
    return _toggle_disabled(path, False)


def _toggle_disabled(path: Path | None, disabled: bool) -> tuple[Path, bool]:
    target = path or settings_path()
    if not target.is_file():
        return target, False
    data = _load(target)
    changed = False
    for _, _, handler in _iter_ours(data):
        if disabled:
            if handler.get("disabled") is not True:
                handler["disabled"] = True
                changed = True
        elif "disabled" in handler:
            del handler["disabled"]
            changed = True
    if changed:
        _write_new(target, data)
    return target, changed


def status(path: Path | None = None, base_url: str | None = None) -> HookStatus:
    """Marker-detection state + shared /healthz reachability probe.

    States: ``not-installed`` / ``installed`` / ``disabled`` (every marked
    entry flagged) / ``partial`` (some events lack our entry, or flags are
    mixed) / ``differs`` (settings exist but do not strict-parse).
    """
    target = path or settings_path()
    state = "differs"
    try:
        data = _load(target)
    except SettingsParseError:
        data = {}
    else:
        ours_by_event: dict[str, list[dict[str, object]]] = {event: [] for event in HOOK_EVENTS}
        for event, _, handler in _iter_ours(data):
            ours_by_event[event].append(handler)
        present = sum(1 for ours in ours_by_event.values() if ours)
        all_disabled = sum(
            1
            for ours in ours_by_event.values()
            if ours and all(handler.get("disabled") is True for handler in ours)
        )
        if present == 0:
            state = "not-installed"
        elif present < len(HOOK_EVENTS):
            state = "partial"
        elif all_disabled == len(HOOK_EVENTS):
            state = "disabled"
        elif all_disabled:
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
