"""Host-agnostic history import runner.

One rollout file is one import session keyed by the run uuid in its filename;
``session_meta.session_id`` is conversation lineage only. Every turn goes
through the hook ingest envelope with its real ordinal, the batch settles once
with pool credit off, and the resume ledger advances only on that settle's 200.
"""

from __future__ import annotations

import importlib
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol

from mnemoseed_local.importer.ledger import ImportLedger, LedgerEntry
from mnemoseed_local.rest_client import DaemonClient, client_for
from mnemoseed_local.schema.turn import (
    HostId,
    IngestEvent,
    IngestEventType,
    MessageContent,
    SessionEndRequest,
)

#: Settle can drain thousands of turns; the hook's short budget cannot wait.
IMPORT_SETTLE_TIMEOUT_SECONDS = 900.0

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class ImportPreflightError(Exception):
    """A setup or reachability precondition failed before any ingest."""


class TranscriptTurnLike(Protocol):
    ordinal: int
    role: str
    text: str


@dataclass(frozen=True)
class RolloutSource:
    host: str
    default_source: str
    iter_rollouts: Callable[[Any], list[Path]]
    read_session_meta: Callable[[Any], str | None]
    read_turns: Callable[[Any], Sequence[TranscriptTurnLike]]
    validate_rollout: Callable[[Any], None]


@dataclass
class ImportReport:
    sessions_scanned: int = 0
    sessions_imported: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    turns_ingested: int = 0
    turns_deduped: int = 0
    new_chunk: int = 0
    reinforced: int = 0
    needs_reconcile: int = 0
    pool_points_suppressed: float = 0.0
    elapsed_s: float = 0.0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def load_source(host: str, module_path: str) -> RolloutSource:
    """Resolve a registry module path into the readers the runner calls."""
    module = importlib.import_module(module_path)
    return RolloutSource(
        host=host,
        default_source=str(module.DEFAULT_SOURCE),
        iter_rollouts=module.iter_rollouts,
        read_session_meta=module.read_session_meta,
        read_turns=module.read_turns,
        validate_rollout=module.validate_rollout,
    )


def build_client(base_url: str, profile_id: str) -> DaemonClient:
    """The import client: CLI actor (never hook) and the long settle budget."""
    return client_for(
        base_url,
        profile_id=profile_id,
        actor="cli",
        timeout=IMPORT_SETTLE_TIMEOUT_SECONDS,
    )


def preflight_zstandard() -> None:
    """Fail before the first ingest when the compressed-rollout extra is absent."""
    if find_spec("zstandard") is None:
        raise ImportPreflightError(
            "zstandard is required to read compressed rollouts "
            "(install with: uv tool install --force 'mnemoseed-local[codex]')"
        )


def preflight(client: DaemonClient) -> None:
    """Local setup check first, then one read-only daemon reachability probe."""
    preflight_zstandard()
    client.get("/healthz")


def _run_uuid(path: Path) -> str:
    matches = _UUID_RE.findall(path.stem)
    return matches[-1] if matches else path.stem


def _ingest_body(
    *,
    host: HostId,
    event: IngestEventType,
    session_id: str,
    profile_id: str,
    text: str,
    ordinal: int,
    raw: dict[str, Any],
) -> IngestEvent:
    """The hook envelope, built from the schema without importing a host module."""
    return IngestEvent(
        host=host,
        event=event,
        session_id=session_id,
        profile_id=profile_id,
        ts=time.time(),
        content=MessageContent(text=text),
        transcript_ordinal=ordinal,
        raw=raw,
    )


def run_import(
    *,
    source: RolloutSource,
    root: Path,
    profile_id: str,
    client: DaemonClient | None,
    ledger: ImportLedger,
    limit: int | None = None,
    dry_run: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ImportReport:
    started = time.monotonic()
    report = ImportReport()
    active: set[str] = set()
    if not dry_run and client is not None:
        active = set(client.get("/session/active", params={"profile_id": profile_id}).get("sessions", []))
    rollouts = source.iter_rollouts(root)
    if limit is not None:
        rollouts = rollouts[:limit]
    total = len(rollouts)
    for index, path in enumerate(rollouts, start=1):
        report.sessions_scanned += 1

        def note(count: int, new_chunk: int, idx: int = index, rollout: Path = path) -> None:
            if progress is not None:
                progress(
                    f"{idx}/{total} {rollout.name} turns={count} new_chunk={new_chunk} "
                    f"elapsed={time.monotonic() - started:.1f}"
                )

        try:
            source.validate_rollout(str(path))
            conversation_id = source.read_session_meta(str(path))
            turns = sorted(source.read_turns(str(path)), key=lambda turn: int(turn.ordinal))
        except RuntimeError:
            raise
        except Exception:
            report.skip("corrupt")
            note(0, 0)
            continue
        if conversation_id is None:
            report.skip("no-meta")
            note(0, 0)
            continue
        if not turns:
            report.skip("zero-turn")
            note(0, 0)
            continue
        session_id = _run_uuid(path)
        if session_id in active or conversation_id in active:
            report.skip("active")
            note(0, 0)
            continue
        record = ledger.get(session_id)
        floor = record.settled_through_ordinal if record is not None else -1
        fresh = [turn for turn in turns if int(turn.ordinal) > floor]
        if record is not None and not fresh:
            report.skip("already-imported")
            note(0, 0)
            continue
        if dry_run:
            report.turns_ingested += len(fresh)
            note(len(fresh), 0)
            continue
        if client is None:
            raise ImportPreflightError("no daemon client for a non-dry import")
        settled_through = floor
        for turn in fresh:
            ordinal = int(turn.ordinal)
            body = _ingest_body(
                host=HostId(source.host),
                event=IngestEventType.USER_PROMPT
                if turn.role == "user"
                else IngestEventType.ASSISTANT_MESSAGE,
                session_id=session_id,
                profile_id=profile_id,
                text=turn.text,
                ordinal=ordinal,
                raw={
                    "source": "history-import",
                    "host": source.host,
                    "rollout": str(path),
                    "conversation_id": conversation_id,
                },
            )
            reply = client.post("/ingest", body.model_dump(mode="json"))
            report.turns_ingested += 1
            ack = reply.get("ack_ordinal") if isinstance(reply, dict) else None
            if isinstance(ack, int) and not isinstance(ack, bool) and ack > ordinal:
                report.turns_deduped += 1
            settled_through = max(settled_through, ordinal)
        settle = client.post(
            "/session/end",
            SessionEndRequest(
                session_id=session_id,
                profile_id=profile_id,
                ts=time.time(),
                credit_pool=False,
            ).model_dump(mode="json"),
        )
        ledger.record(
            LedgerEntry(
                path=str(path),
                size=path.stat().st_size,
                mtime=path.stat().st_mtime,
                session_id=session_id,
                conversation_id=conversation_id,
                settled_through_ordinal=settled_through,
                status="settled",
                outcomes={
                    "turns_ingested": len(fresh),
                    "new_chunk": int(settle.get("new_chunk", 0)),
                    "reinforced": int(settle.get("reinforced", 0)),
                    "needs_reconcile": int(settle.get("needs_reconcile", 0)),
                    "pool_points_suppressed": float(settle.get("pool_points_suppressed", 0.0)),
                },
            )
        )
        report.sessions_imported += 1
        report.new_chunk += int(settle.get("new_chunk", 0))
        report.reinforced += int(settle.get("reinforced", 0))
        report.needs_reconcile += int(settle.get("needs_reconcile", 0))
        report.pool_points_suppressed += float(settle.get("pool_points_suppressed", 0.0))
        note(len(fresh), int(settle.get("new_chunk", 0)))
    report.elapsed_s = time.monotonic() - started
    return report
