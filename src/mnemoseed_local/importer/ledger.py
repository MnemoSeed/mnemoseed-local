"""Client-side resume ledger for history imports.

This ledger keeps the client-side resume floor: one entry per rollout, advanced
only after a 200 from ``/session/end``. The daemon persists its own seen/applied
watermarks and absorbs re-posted ordinals. The conversation id is lineage
metadata only — session identity is the run uuid in the rollout filename.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mnemoseed_local.config import live_home_dir
from mnemoseed_local.hosts.common.config import write_atomic

logger = logging.getLogger(__name__)

_LEGACY_MIGRATION_PROFILE = "default"


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
    """Atomically written ledger at ``<home>/import-ledger-<host>-<profile>.json``."""

    def __init__(
        self,
        host: str,
        profile_id: str = _LEGACY_MIGRATION_PROFILE,
        *,
        home: Path | None = None,
    ) -> None:
        self.host = host
        self.profile_id = profile_id
        directory = home if home is not None else live_home_dir()
        self.path = directory / f"import-ledger-{host}-{profile_id}.json"
        self._legacy_path = directory / f"import-ledger-{host}.json"
        self._data = self._load()

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "host": self.host, "sessions": {}}

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("import ledger %s could not be read (%s); starting empty", path, exc)
            return None
        try:
            data = json.loads(raw)
        except ValueError as exc:
            logger.warning("import ledger %s is corrupt (%s); starting empty", path, exc)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
            logger.warning("import ledger %s has an invalid shape; starting empty", path)
            return None
        return data

    def _load(self) -> dict[str, Any]:
        data = self._read(self.path)
        if data is not None:
            return data
        if (
            self.profile_id == _LEGACY_MIGRATION_PROFILE
            and not self.path.exists()
            and self._legacy_path.exists()
        ):
            legacy = self._read(self._legacy_path)
            if legacy is not None:
                write_atomic(self.path, legacy)
                return legacy
        return self._empty()

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
