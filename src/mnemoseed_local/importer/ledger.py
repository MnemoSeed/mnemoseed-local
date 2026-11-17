"""Client-side resume ledger for history imports.

The daemon's watermark is in memory and is popped at settle, so the durable
resume point lives here: one entry per rollout, advanced only after a 200 from
``/session/end``. The conversation id is lineage metadata only — session
identity is the run uuid in the rollout filename.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mnemoseed_local.config import live_home_dir
from mnemoseed_local.hosts.common.config import write_atomic


@dataclass
class LedgerEntry:
    path: str
    size: int
    mtime: float
    session_id: str
    conversation_id: str | None
    settled_through_ordinal: int
    status: str
    outcomes: dict[str, int | float] = field(default_factory=dict)


class ImportLedger:
    """Atomically written ledger at ``<home>/import-ledger-<host>.json``."""

    def __init__(self, host: str, *, home: Path | None = None) -> None:
        self.host = host
        self.path = (home if home is not None else live_home_dir()) / f"import-ledger-{host}.json"
        self._data = self._load()

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "host": self.host, "sessions": {}}

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return self._empty()
        return data if isinstance(data, dict) and isinstance(data.get("sessions"), dict) else self._empty()

    def get(self, session_id: str) -> LedgerEntry | None:
        raw = self._data["sessions"].get(session_id)
        if not isinstance(raw, dict):
            return None
        return LedgerEntry(
            path=str(raw.get("path", "")),
            size=int(raw.get("size", 0)),
            mtime=float(raw.get("mtime", 0.0)),
            session_id=str(raw.get("session_id", "")),
            conversation_id=raw.get("conversation_id"),
            settled_through_ordinal=int(raw.get("settled_through_ordinal", -1)),
            status=str(raw.get("status", "")),
            outcomes=dict(raw.get("outcomes") or {}),
        )

    def record(self, entry: LedgerEntry) -> None:
        """Advance a session's resume point; only a settled entry may land."""
        if entry.status != "settled":
            raise ValueError("the ledger records only settled sessions")
        self._data["sessions"][entry.session_id] = asdict(entry)
        write_atomic(self.path, self._data)
