"""Claude Code rollout transcript reader for history backfill.

Claude Code stores one JSON event per line under ``~/.claude/projects``.
Turns are the ``user``/``assistant`` rows' ``message.content``; the other row
types (``last-prompt``, ``mode``, ``permission-mode``, ``file-history-snapshot``,
``attachment``, ``cost-state``) are metadata and are ignored. ``isMeta`` rows
are local-command output and ``isSidechain`` rows are subagent work, so both
are excluded from conversational turns. Extracted text is kept verbatim; a
rollout has no native ordinal, so one is synthesized from the zero-based line
order and gaps from filtered rows are preserved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Default rollout root the CLI uses when ``--source`` is omitted.
DEFAULT_SOURCE = "~/.claude/projects"


@dataclass(frozen=True)
class TranscriptTurn:
    """One user/assistant message turn derived from the rollout."""

    ordinal: int
    role: str
    text: str


def iter_rollouts(root: Any) -> list[Path]:
    """Plain ``.jsonl`` rollout files under ``root``, in stable order."""
    base = Path(root).expanduser()
    if not base.is_dir():
        return []
    return sorted(path for path in base.rglob("*.jsonl") if path.is_file())


def _lines(path: Any) -> list[str]:
    if not isinstance(path, (str, Path)) or not str(path).strip():
        return []
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeError:
        return []


def _parse(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def validate_rollout(path: Any) -> None:
    """Reject an unreadable rollout path; line-level faults degrade to skipped records."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError("invalid rollout path")
    if not Path(path).expanduser().is_file():
        raise ValueError(f"not a rollout file: {path}")


def read_session_meta(path: Any) -> str | None:
    """The conversation id from the first row carrying a non-empty ``sessionId``."""
    for line in _lines(path):
        record = _parse(line)
        if record is None:
            continue
        session_id = record.get("sessionId")
        if isinstance(session_id, str) and session_id.strip():
            return session_id
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts)


def read_turns(path: Any) -> list[TranscriptTurn]:
    """Conversational turns of a rollout, with line-order ordinals ascending."""
    turns: list[TranscriptTurn] = []
    for ordinal, line in enumerate(_lines(path)):
        record = _parse(line)
        if record is None or record.get("type") not in ("user", "assistant"):
            continue
        if record.get("isMeta") or record.get("isSidechain"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role", record.get("type"))
        if role not in ("user", "assistant"):
            continue
        text = _message_text(message.get("content"))
        if not text:
            continue
        turns.append(TranscriptTurn(ordinal, role, text))
    return turns
