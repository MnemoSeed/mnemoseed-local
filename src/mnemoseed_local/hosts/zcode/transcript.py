"""zcode SQLite history reader (design 14 §2).

Each ``session`` row is exposed as a stat-able stub path
``<root>/.mnemoseed-rollouts/<session-id>.zcode``; readers decode the session
id from the stem and open ``<root>/db.sqlite`` read-only. The native
``sequence`` on ``message`` is the turn ordinal. zcode stores conversational
text exclusively in ``type=="text"`` ``part`` rows — including user text — so
reasoning, tool, and other part types are excluded. All session ids, including
``sess_subagent_agent_*``, are imported; provenance lives in the stamp.
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
DEFAULT_SOURCE = "~/.zcode/cli/db"

_DB_NAME = "db.sqlite"
_STUB_DIR = ".mnemoseed-rollouts"
_STUB_SUFFIX = ".zcode"
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class ZcodeTranscriptError(Exception):
    """The zcode history source could not be enumerated safely."""


@dataclass(frozen=True)
class TranscriptTurn:
    """One user/assistant message turn from a session."""

    ordinal: int
    role: str
    text: str


def _open_db(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise ValueError(f"zcode database not found or unreadable: {db}")
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)


def _valid_session_id(session_id: Any) -> bool:
    """Only plain ids may become stub file names; path syntax is never a name."""
    return isinstance(session_id, str) and _SESSION_ID_RE.fullmatch(session_id) is not None


def _decode(path: Any) -> tuple[Path, str]:
    handle = Path(path)
    if handle.parent.name != _STUB_DIR:
        raise ValueError(f"not a zcode rollout handle: {path}")
    session_id = handle.stem
    if not session_id:
        raise ValueError("zcode rollout handle has no session id")
    return handle.parent.parent / _DB_NAME, session_id


def _loads(raw: Any) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except TypeError, ValueError:
        return None
    return data if isinstance(data, dict) else None


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
        rows = con.execute("SELECT id, time_created FROM session ORDER BY time_created, id").fetchall()
    stub_dir = base / _STUB_DIR
    if create_stubs:
        try:
            stub_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ZcodeTranscriptError(
                f"cannot create zcode rollout stub directory {stub_dir}: {exc}"
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
                raise ZcodeTranscriptError(f"cannot create zcode rollout stub {handle}: {exc}") from exc
        handles.append(handle)
    return handles


def read_session_meta(path: Any) -> str | None:
    """The session id encoded in the handle, used as conversation lineage."""
    try:
        _db, session_id = _decode(path)
    except TypeError, ValueError:
        return None
    return session_id


def read_turns(path: Any) -> list[TranscriptTurn]:
    """User/assistant turns of a session in ascending native ``sequence`` order."""
    db, session_id = _decode(path)
    with closing(_open_db(db)) as con:
        rows = con.execute(
            "SELECT m.sequence, m.data, p.data FROM message m "
            "JOIN part p ON p.message_id = m.id "
            "WHERE m.session_id = ? ORDER BY m.sequence, p.sequence",
            (session_id,),
        ).fetchall()

    grouped: dict[int, tuple[str, list[str]]] = {}
    for sequence, raw_message, raw_part in rows:
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            continue
        entry = grouped.get(sequence)
        if entry is None:
            message = _loads(raw_message)
            role = message.get("role") if message else None
            if role not in ("user", "assistant"):
                continue
            entry = (role, [])
            grouped[sequence] = entry
        part = _loads(raw_part)
        if part is None or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            entry[1].append(text)

    turns: list[TranscriptTurn] = []
    for sequence, (role, pieces) in grouped.items():
        text = "".join(pieces)
        if text.strip():
            turns.append(TranscriptTurn(sequence, role, text))
    return turns


def validate_rollout(path: Any) -> None:
    """Refuse a missing, unreadable, or schema-incompatible database."""
    db, _session_id = _decode(path)
    with closing(_open_db(db)) as con:
        con.execute("SELECT 1 FROM session LIMIT 1").fetchone()
        con.execute("SELECT 1 FROM message LIMIT 1").fetchone()
        con.execute("SELECT 1 FROM part LIMIT 1").fetchone()
