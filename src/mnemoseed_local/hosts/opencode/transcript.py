"""opencode SQLite history reader (design 14 §2).

Each ``session_v2`` row is exposed as a stat-able stub path
``<root>/.mnemoseed-rollouts/<session-id>.opencode``; readers decode the
session id from the stem and open ``<root>/opencode.db`` read-only so the WAL
is visible. The native ``seq`` is the turn ordinal. Only ``type=="text"``
assistant content parts and the user ``text`` field become conversational
turns; reasoning and tool parts are excluded.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Default source directory the CLI uses when ``--source`` is omitted.
DEFAULT_SOURCE = "~/.local/share/opencode"

_DB_NAME = "opencode.db"
_STUB_DIR = ".mnemoseed-rollouts"
_STUB_SUFFIX = ".opencode"
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class OpencodeTranscriptError(Exception):
    """The opencode history source could not be enumerated safely."""


@dataclass(frozen=True)
class TranscriptTurn:
    """One user/assistant message turn from a session."""

    ordinal: int
    role: str
    text: str


def _open_db(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise ValueError(f"opencode database not found or unreadable: {db}")
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)


def _valid_session_id(session_id: Any) -> bool:
    """Only plain ids may become stub file names; path syntax is never a name."""
    return isinstance(session_id, str) and _SESSION_ID_RE.fullmatch(session_id) is not None


def _decode(path: Any) -> tuple[Path, str]:
    handle = Path(path)
    if handle.parent.name != _STUB_DIR:
        raise ValueError(f"not an opencode rollout handle: {path}")
    session_id = handle.stem
    if not session_id:
        raise ValueError("opencode rollout handle has no session id")
    return handle.parent.parent / _DB_NAME, session_id


def iter_rollouts(root: Any, *, create_stubs: bool = True) -> list[Path]:
    """Per-session handles, ordered by creation time then id.

    Stub files are created only when requested; a dry import names the same
    handles without touching the source directory.
    """
    base = Path(root).expanduser()
    db = base / _DB_NAME
    if not db.is_file():
        return []
    with closing(_open_db(db)) as con:
        rows = con.execute("SELECT id, time_created FROM session_v2 ORDER BY time_created, id").fetchall()
    stub_dir = base / _STUB_DIR
    if create_stubs:
        try:
            stub_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OpencodeTranscriptError(
                f"cannot create opencode rollout stub directory {stub_dir}: {exc}"
            ) from exc
    handles: list[Path] = []
    for session_id, _created in rows:
        if not _valid_session_id(session_id):
            continue
        handle = stub_dir / f"{session_id}{_STUB_SUFFIX}"
        if create_stubs:
            try:
                handle.touch(exist_ok=True)
            except OSError as exc:
                raise OpencodeTranscriptError(f"cannot create opencode rollout stub {handle}: {exc}") from exc
        handles.append(handle)
    return handles


def read_session_meta(path: Any) -> str | None:
    """The session id encoded in the handle, used as conversation lineage."""
    try:
        _db, session_id = _decode(path)
    except TypeError, ValueError:
        return None
    return session_id


def _message_text(role: str, data: dict[str, Any]) -> str:
    if role == "user":
        text = data.get("text")
        return text if isinstance(text, str) else ""
    content = data.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        part["text"]
        for part in content
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
    )


def read_turns(path: Any) -> list[TranscriptTurn]:
    """User/assistant turns of a session in ascending native ``seq`` order."""
    db, session_id = _decode(path)
    with closing(_open_db(db)) as con:
        rows = con.execute(
            "SELECT type, seq, data FROM session_message WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    turns: list[TranscriptTurn] = []
    for role, seq, raw in rows:
        if role not in ("user", "assistant") or not isinstance(seq, int) or isinstance(seq, bool):
            continue
        try:
            data = json.loads(raw)
        except TypeError, ValueError:
            continue
        if not isinstance(data, dict):
            continue
        text = _message_text(role, data)
        if text.strip():
            turns.append(TranscriptTurn(seq, role, text))
    return turns


def validate_rollout(path: Any) -> None:
    """Refuse a missing, unreadable, or schema-incompatible database."""
    db, _session_id = _decode(path)
    with closing(_open_db(db)) as con:
        con.execute("SELECT 1 FROM session_v2 LIMIT 1").fetchone()
        con.execute("SELECT 1 FROM session_message LIMIT 1").fetchone()
