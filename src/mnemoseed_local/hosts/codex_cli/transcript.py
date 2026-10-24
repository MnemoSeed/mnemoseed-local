"""Codex rollout transcript reader: the facts source behind the hook clock.

Hook stdin only marks time; the rollout transcript at ``transcript_path`` is
the facts source. Every line carries ``{ordinal, timestamp, type, payload}``;
conversation turns are the ``response_item`` records whose payload type is
``message`` with a ``user``/``assistant`` role (``developer`` and the
reasoning/tool payloads are not conversation turns). The compressed
``.jsonl.zst`` form lazily imports the optional ``zstandard`` extra
(``mnemoseed-local[codex]``) and raises with the install command when it is
absent; every other failure (missing file, bad bytes, broken records)
degrades to no turns — a dropped differential beats a raised hook.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mnemoseed_local.hosts.common.zstd import require_zstandard


@dataclass(frozen=True)
class TranscriptTurn:
    """One user/assistant message turn derived from the rollout."""

    ordinal: int
    role: str
    text: str


def _line_ordinal(record: dict[str, Any], fallback: int) -> int:
    ordinal = record.get("ordinal")
    if isinstance(ordinal, int) and not isinstance(ordinal, bool) and ordinal >= 0:
        return ordinal
    return fallback


def _message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _read_lines(path: Path) -> list[str]:
    if path.suffix == ".zst":
        data = path.read_bytes()
        zstd = require_zstandard()
        reader: Any = zstd.ZstdDecompressor().stream_reader(io.BytesIO(data))
        with reader:
            text: str = reader.read().decode("utf-8")
        return text.splitlines()
    return path.read_text(encoding="utf-8").splitlines()


def read_turns(path: Any) -> list[TranscriptTurn]:
    """All user/assistant message turns of a rollout transcript, ascending."""
    if not isinstance(path, str) or not path.strip():
        return []
    try:
        lines = _read_lines(Path(path).expanduser())
    except (OSError, ValueError):
        return []
    turns: list[TranscriptTurn] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(payload)
        if not text.strip():
            continue
        turns.append(TranscriptTurn(_line_ordinal(record, index), role, text))
    return turns


def anchor_ordinal(turns: list[TranscriptTurn], prompt: str) -> int | None:
    """The ordinal of the newest user turn carrying this prompt, None when the
    transcript does not (yet) show it — the event then ingests without
    participating in the watermark."""
    wanted = prompt.strip()
    for turn in reversed(turns):
        if turn.role == "user" and turn.text.strip() == wanted:
            return turn.ordinal
    return None
