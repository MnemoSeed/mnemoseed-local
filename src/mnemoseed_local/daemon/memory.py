"""Daemon memory surface (PRD-03 T4): the /memory endpoints, A2 local trim.

Router + service seam over the retrieval engine and the storage ports
(NO identity/accounts/tokens in the local MVP — profile_id is explicit in
every request, never guessed, D5 isolation):

- POST /memory/recall       - CueExtractor -> HybridRetriever -> Assembler, with
                              envelope cues, top_k / budget overrides, the
                              honest-empty CoverageReport (FR-3.13), conflict
                              pairing / pending-consolidation / fresh-evidence
                              markers, and fire-and-forget usage events that both
                              count the hit (FR-3.7) and reinforce it (FR-4.2).
- POST /memory/remember     - explicit user pin; provenance asserts
                              ``asserted_by="user"`` with source
                              ``EXPLICIT_PIN_SOURCE``; identical re-pins reinforce
                              the existing chunk instead of duplicating it.
- POST /memory/audit        - provenance + version chain + relevant audit rows.
- POST /memory/timeline     - per-node version replay, else profile-wide paging.
- POST /memory/export       - stable paged JSON dump including provenance.
- POST /memory/forget_this  - GDPR deletion (design/03 storage-layer erasure):
                              chunk rows are deleted, graph nodes are tombstoned
                              (version chain preserved), the audit trail records
                              exactly what was removed.
- POST /memory/dream_once   - the /dream command HTTP surface (FR-2.8
                              manual-first): run exactly one manual dream cycle
                              and read the trigger's observability.
- POST /memory/reinforce    - B2.1 T3 consumption evidence: the hook attests
                              that an injected slice was cited by the assistant;
                              the Reinforcer applies its FR-4.2 rebound.

Determinism: no clocks except reading the live timestamp where the semantic
contract is timestamp-based (forget/deletion time, remember ``asserted_at``,
usage-event ``last_hit_at``); no randomness; no network.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, Any, Self

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from mnemoseed_local.capture.stamper import ConsistencyVerdict, NearDuplicateChecker, WriteConfig
from mnemoseed_local.config import Config
from mnemoseed_local.daemon.actor import resolve_actor
from mnemoseed_local.decay import Reinforcer
from mnemoseed_local.dream import DreamTrigger, TriggerStatus

if TYPE_CHECKING:
    from mnemoseed_local.daemon.app import DreamWorker
from mnemoseed_local.retrieve.assemble import (
    AssembledContext,
    AssembledEntry,
    Assembler,
)
from mnemoseed_local.retrieve.cues import CueExtractor
from mnemoseed_local.retrieve.hybrid import HybridRetriever
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Provenance, ProvenanceEvent
from mnemoseed_local.schema.turn import ProfileRef
from mnemoseed_local.storage.drivers._time import iso8601_utc
from mnemoseed_local.storage.factory import Stores
from mnemoseed_local.storage.ports import (
    AuditEntry,
    AuditFilter,
    ChunkFilter,
    NodeFilter,
    Page,
    RecallRule,
    RulesBudgetBlock,
    VectorStore,
)

logger = logging.getLogger("mnemoseed_local.daemon.memory")

# Explicit-pin provenance source marker (FR-3.1). A /memory/remember write is
# asserted by the user and never merges into the capture provenance channel.
EXPLICIT_PIN_SOURCE = "memory.remember"

# B2.1 T2 (design/01 §4.6, PRD-B2.1): the mid-session auto-recall focal scan.
# NON_FOCAL_FLOOR mirrors the default focal floor: decay-healthy chunks the
# focal scan did NOT select are reported as the T4 weak-association probe
# (TA-4: weak associations are never injected — counted, never served).
# _MIN_SLICE_CHARS mirrors the T1 budget semantics (a boundary slice below it
# drops the whole item along with everything older).
NON_FOCAL_FLOOR = 0.4
_MIN_SLICE_CHARS = 200
_SCAN_PAGE_LIMIT = 50

# Bounded per-session window scan: the exact first/latest over a session's
# rows. A scan that returns the limit may have missed older rows, so the
# consumer reports window_truncated (never a page-visible approximation).
SESSION_WINDOW_SCAN_LIMIT = 2000


class MemoryNotFoundError(Exception):
    """The requested memory target does not exist for this profile."""


# ---------------------------------------------------------------- wire models

NonBlankText = Annotated[str, Field(min_length=1, pattern=r".*\S.*")]


class RecallRequest(BaseModel):
    profile_id: ProfileRef
    query: NonBlankText
    # Envelope cues ride through as weak rerank context (FR-3.14), never as a
    # candidate filter.
    host: str | None = None
    project: str | None = None
    time_bucket: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    budget: int | None = Field(default=None, ge=1)


class RememberRequest(BaseModel):
    profile_id: ProfileRef
    text: NonBlankText
    rules: list[RecallRule] | None = None


class AuditRequest(BaseModel):
    profile_id: ProfileRef
    node_id: str | None = None
    chunk_id: str | None = None

    @model_validator(mode="after")
    def _has_target(self) -> Self:
        if self.node_id is None and self.chunk_id is None:
            raise ValueError("audit requires a node_id or a chunk_id target")
        return self


class TimelineRequest(BaseModel):
    profile_id: ProfileRef
    node_id: str | None = None


class ExportRequest(BaseModel):
    profile_id: ProfileRef
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=500)


class ForgetRequest(BaseModel):
    profile_id: ProfileRef
    chunk_id: str | None = None
    node_id: str | None = None
    entity: str | None = None

    @model_validator(mode="after")
    def _has_target(self) -> Self:
        if self.chunk_id is None and self.node_id is None and self.entity is None:
            raise ValueError("forget_this requires a chunk_id, node_id, or entity target")
        return self


class DreamRequest(BaseModel):
    """Request body for the /dream command surface (FR-2.8 manual-first)."""

    profile_id: ProfileRef


class SessionRecentRequest(BaseModel):
    """Request body for POST /session/recent (B2 time-ordered resume)."""

    profile_id: ProfileRef
    sessions: int = Field(default=2, ge=1, le=5)
    per_session: int = Field(default=20, ge=1, le=100)
    exclude_session_id: str | None = None
    self_session_id: str | None = None


class SessionWindowsRequest(BaseModel):
    """Request body for POST /session/windows (B2 time-window surface)."""

    profile_id: ProfileRef
    sessions: int = Field(default=3, ge=1, le=10)


class RecallPendingRequest(BaseModel):
    """Request body for POST /session/recall-pending (B2.1 T2 mid-session).

    ``seen_chunk_ids`` are the chunk ids the caller already holds (the T1
    session-start injection); the daemon merges them into its selection so a
    chunk the caller saw is never re-served (D2). The cap mirrors the hook's
    T1 tail size (16 ids).
    """

    profile_id: ProfileRef
    session_id: str
    seen_chunk_ids: list[str] = Field(default_factory=list, max_length=16)


class ReinforceRequest(BaseModel):
    """Request body for POST /memory/reinforce (B2.1 T3 consumption evidence).

    ``chunk_ids``/``node_ids`` are the exact store keys whose usage the hook's
    consumption guard attested; unknown ids are tolerated silently by the
    Reinforcer (concurrently purged targets never fail the caller).
    """

    profile_id: ProfileRef
    chunk_ids: list[str] = Field(default_factory=list, max_length=64)
    node_ids: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _has_target(self) -> Self:
        if not self.chunk_ids and not self.node_ids:
            raise ValueError("reinforce requires at least one chunk_id or node_id target")
        return self


def _discover_session_ids(
    chunks: Sequence[ChunkStamp],
    *,
    sessions: int,
    exclude_session_id: str | None = None,
) -> list[str]:
    """Newest-first distinct-session discovery over an ingested_at-desc page.

    First-seen order over the newest-first walk is recency order; the shared
    "?" group (chunks without a session label) is never excluded by an
    exact-match exclusion."""
    ids: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        if exclude_session_id is not None and chunk.provenance.session_id == exclude_session_id:
            continue
        session_id = chunk.provenance.session_id or "?"
        if session_id in seen:
            continue
        seen.add(session_id)
        ids.append(session_id)
        if len(ids) >= sessions:
            break
    return ids


@dataclass(frozen=True)
class SessionWindow:
    """Exact per-session chunk window from a bounded full scan.

    ``chunk_count`` is None for the shared "?" group, whose unlabeled rows are
    not addressable by a session-scoped scan — the honest unknown, never a
    fabricated zero."""

    session_id: str
    first: float | None
    latest: float | None
    chunk_count: int | None
    window_truncated: bool


def _scan_session_window(
    vector: VectorStore,
    *,
    profile_id: str,
    session_id: str,
) -> SessionWindow:
    """One bounded per-session scan: the true first/latest over the session's
    rows, or an empty window when the session holds no chunks."""
    if session_id == "?":
        return SessionWindow(session_id, None, None, None, False)
    page = vector.list_chunks(
        ChunkFilter(profile_id=profile_id, session_id=session_id),
        Page(offset=0, limit=SESSION_WINDOW_SCAN_LIMIT),
    )
    items = page.items
    if not items:
        return SessionWindow(session_id, None, None, 0, False)
    ordered = sorted(items, key=lambda chunk: chunk.ingested_at)
    return SessionWindow(
        session_id=session_id,
        first=ordered[0].ingested_at,
        latest=ordered[-1].ingested_at,
        chunk_count=len(items),
        window_truncated=page.total > SESSION_WINDOW_SCAN_LIMIT,
    )


def _window_iso(window: SessionWindow) -> dict[str, str] | None:
    """ISO-8601 UTC rendering of an exact window; null when it has no chunks
    or a non-positive (epoch-leak) bound."""
    first = window.first
    if first is None or first <= 0:
        return None
    latest = window.latest
    if latest is None or latest <= 0:
        return None
    return {"first": iso8601_utc(first), "latest": iso8601_utc(latest)}


def _group_session_tails(
    chunks: list[ChunkStamp],
    *,
    per_session: int,
    sessions: int,
    exclude_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Group ingested_at-DESC chunks into per-session tails (B2 semantics).

    Newest session group first (first-seen order over the newest-first page is
    exactly recency order); each group's tail is its LAST ``per_session``
    chunks, listed ascending — the reading order. The endpoint never guesses
    which session is "closed": at most ``sessions`` groups come back and the
    caller recognizes its own current one as the still-growing newest group.
    ``exclude_session_id`` is a filter applied BEFORE grouping (the caller's
    own session must never be echoed back to it): the session cap counts
    SURVIVOR groups, and the shared "?" group (chunks without a session label)
    is never excluded."""
    session_ids = _discover_session_ids(chunks, sessions=sessions, exclude_session_id=exclude_session_id)
    by_session: dict[str, list[ChunkStamp]] = {session_id: [] for session_id in session_ids}
    for chunk in chunks:
        if exclude_session_id is not None and chunk.provenance.session_id == exclude_session_id:
            continue
        session_id = chunk.provenance.session_id or "?"
        if session_id not in by_session:
            continue  # a session beyond the cap; the discovery walk ordered the rest
        by_session[session_id].append(chunk)
    payload: list[dict[str, Any]] = []
    for session_id in session_ids:
        chunks_desc = by_session[session_id]
        tail = chunks_desc[:per_session]
        tail.reverse()  # ascending: the tail in reading order
        payload.append(
            {
                "session_id": session_id,
                "latest_at": chunks_desc[0].ingested_at,
                "chunks": [
                    {
                        "chunk_id": chunk.chunk_id,
                        "text": chunk.text,
                        "ingested_at": chunk.ingested_at,
                        "turn_start": chunk.turn_start,
                        "turn_end": chunk.turn_end,
                    }
                    for chunk in tail
                ],
            }
        )
    return payload


# ---------------------------------------------------------------- service


class MemoryService:
    """Daemon-owned memory engine: leverages the retrieval + storage ports."""

    def __init__(self, stores: Stores, config: Config) -> None:
        self._stores = stores
        self._config = config
        self._cues = CueExtractor()
        self._retriever = HybridRetriever()
        self._assembler = Assembler()
        # FR-4.2 event side: retrieval usage becomes a reinforcement event
        # (baseline refresh + bounded rebound), the counterpart of the sweep.
        self._reinforcer = Reinforcer(stores)
        # B2.1 T2 pending-recall state: per-session parked selection + seen
        # set. The lock serializes the atomic serve=mark-seen against
        # concurrent pulls — MemoryService is shared by the async ingest lane
        # (focal scan in a worker thread) and the threadpool route handlers.
        self._pending_slots: dict[tuple[str, str], list[dict[str, str]]] = {}
        self._pending_non_focal: dict[tuple[str, str], int] = {}
        self._seen_chunk_ids: dict[tuple[str, str], set[str]] = {}
        # QA BLOCKER-2: per-key CONSUMED TOMBSTONE, distinct from the slot. A
        # serve pops the slot and sets the tombstone; a later pull (the retry
        # after a lost response) finds no slot but the tombstone and answers
        # slot_consumed:true with an empty selection — the hook clears its arm
        # on exactly that, so the retry loop cannot pull into the void forever.
        # Cleared on /session/end with the rest of the lifecycle state; new
        # scans may park fresh slots while it stands (slot and tombstone
        # coexist, a fresh serve returns items + slot_consumed:true).
        self._pending_consumed: dict[tuple[str, str], bool] = {}
        # NIT-5: per-session monotonic scan sequence + settlement epoch. A scan
        # captures both under the lock at start and only parks its selection on
        # write-back if neither changed — a stale scan can never overwrite a
        # newer scan's slot, and a scan started before /session/end can never
        # re-park a selection afterwards (the settle is terminal).
        self._scan_seq: dict[tuple[str, str], int] = {}
        self._session_epoch: dict[tuple[str, str], int] = {}
        self._pending_lock = threading.Lock()
        # B2.7: per-session daemon-side T2 char count (served items), reported
        # as ``rules_budget.budget_consumed`` — the hook only reads it.
        self._budget_consumed: dict[tuple[str, str], int] = {}

    @property
    def retriever(self) -> HybridRetriever:
        return self._retriever

    def close(self) -> None:
        """Release the retrieval engine (T4 lifecycle fix): the daemon owns the
        HybridRetriever and shuts its track executor down on teardown so worker
        threads never outlive the process."""
        self._retriever.close()

    # ------------------------------------------------------------ recall

    def recall(
        self,
        *,
        profile_id: str,
        query: str,
        host: str | None = None,
        project: str | None = None,
        time_bucket: str | None = None,
        top_k: int | None = None,
        budget: int | None = None,
    ) -> dict[str, Any]:
        """Full recall path: cues -> dual-track pool -> budgeted context."""
        extracted = self._cues.extract(query, host=host, project=project, time_bucket=time_bucket)
        recall_result = self._retriever.recall(
            query,
            extracted,
            profile_id=profile_id,
            vector_store=self._stores.vector,
            graph_store=self._stores.graph,
            embedder=self._stores.embed,
        )
        assembler = self._assembler
        if top_k is not None or budget is not None:
            base = assembler.config
            assembler = Assembler(
                replace(
                    base,
                    top_k=top_k if top_k is not None else base.top_k,
                    budget_tokens=budget if budget is not None else base.budget_tokens,
                )
            )
        context = assembler.assemble(
            recall_result,
            profile_id=profile_id,
            meta_store=self._stores.meta,
            vector_store=self._stores.vector,
            graph_store=self._stores.graph,
        )
        self._record_hits(context)
        return {"memory": self._memory_payload(context)}

    def _record_hits(self, context: AssembledContext) -> None:
        """FR-3.7 usage events + FR-4.2 reinforcement: fire-and-forget, never
        failing or blocking recall.

        Only items that made the context package are counted and reinforced (a
        hit means the recalled memory). The raw store write is best-effort by
        design; the Reinforcer additionally refreshes ``last_reinforced`` and
        rebounds ``decay_weight`` (bounded at 1.0) for every above-floor hit.
        """
        chunk_ids = [entry.id for entry in context.entries if entry.kind == "chunk"]
        node_ids = [entry.id for entry in context.entries if entry.kind == "graph"]
        if not chunk_ids and not node_ids:
            return
        try:
            self._reinforcer.record_hits(chunk_ids, node_ids)
        except Exception:  # pragma: no cover - usage accounting must not fail recall
            logger.warning("usage-event write failed; recall proceeds", exc_info=True)

    def _memory_payload(self, context: AssembledContext) -> dict[str, Any]:
        coverage = context.coverage
        watermark = coverage.watermark
        return {
            "entries": [self._entry_payload(entry) for entry in context.entries],
            "dropped_count": context.dropped_count,
            "budget_tokens": context.budget_tokens,
            "tokens_used": context.tokens_used,
            "coverage": {
                "vector_hits": coverage.vector_hits,
                "graph_hits": coverage.graph_hits,
                "pool_size": coverage.pool_size,
                "profile_chunks": coverage.profile_chunks,
                "watermark": (
                    {"start": watermark.start, "end": watermark.end} if watermark is not None else None
                ),
                "fresh_evidence_chunks": coverage.fresh_evidence_chunks,
                "pending_marked": coverage.pending_marked,
            },
        }

    @staticmethod
    def _entry_payload(entry: AssembledEntry) -> dict[str, Any]:
        return {
            "kind": entry.kind,
            "id": entry.id,
            "source": entry.source,
            "text": entry.text,
            "score": entry.score,
            "tokens": entry.tokens,
            "flags": [flag.value for flag in entry.flags],
            "conflict_group": entry.conflict_group,
            "recent_evidence": list(entry.recent_evidence),
            "session_id": entry.session_id,
            "ingested_at": iso8601_utc(entry.ingested_at) if entry.ingested_at is not None else None,
        }

    # ------------------------------------------------------------ remember

    def remember(
        self,
        *,
        profile_id: str,
        text: str,
        actor: str = "console",
        rules: Sequence[RecallRule] | None = None,
    ) -> dict[str, Any]:
        """Write an explicit user pin, mirroring the StampWriter's dual-branch
        near-duplicate flow: a strong consistent hit reinforces in place, a
        conflict flags needs_reconcile, anything else becomes a new chunk.
        Provenance is append-only; the explicit-pin source is never rewritten.
        B2.7: every branch persists the standing ``rules`` on the chunk
        (``rules_json``) — the driver merges them by identity on a re-upsert, so
        a near-duplicate re-pin never drops rules."""
        now = time.time()
        rules_dicts = [rule.model_dump() for rule in rules] if rules else []
        extracted = self._cues.extract(text)
        vector = self._stores.vector
        embedded = self._stores.embed.embed(text)
        config = WriteConfig()
        stamp = ChunkStamp(
            chunk_id=uuid.uuid4().hex,
            profile_id=profile_id,
            text=text,
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="user",
            cues=extracted.cues,
            provenance=Provenance(
                asserted_by="user",
                session_id=None,
                source=EXPLICIT_PIN_SOURCE,
                confidence=1.0,
                asserted_at=now,
                history=[ProvenanceEvent(action="created", actor="user", at=now)],
            ),
            decay_weight=1.0,
            score=1.0,
            ingested_at=now,
            rules=rules_dicts,
        )
        strong = vector.near_duplicate(embedded.dense, config.reinforce_threshold, profile_id=profile_id)
        band = vector.near_duplicate(embedded.dense, config.conflict_threshold, profile_id=profile_id)
        if not band:
            vector.upsert_chunk(stamp, embedded.dense, embedded.sparse)
            self._audit(
                profile_id, "remember", {"chunk_id": stamp.chunk_id, "profile_id": profile_id}, actor=actor
            )
            return {"outcome": "new_chunk", "chunk_id": stamp.chunk_id}
        hit = band[0]
        strong_ids = {chunk.chunk_id for chunk in strong}
        verdict = NearDuplicateChecker().check(stamp.text, hit.text)
        if hit.chunk_id in strong_ids and verdict is ConsistencyVerdict.CONSISTENT:
            rebound = min(1.0, hit.decay_weight + config.reinforce_bonus)
            hit_emb = self._stores.embed.embed(hit.text)
            vector.upsert_chunk(
                hit.model_copy(
                    update={"decay_weight": rebound, "last_reinforced": now, "rules": rules_dicts}
                ),
                hit_emb.dense,
                hit_emb.sparse,
            )
            self._audit(
                profile_id,
                "remember",
                {"chunk_id": hit.chunk_id, "profile_id": profile_id, "outcome": "reinforced"},
                actor=actor,
            )
            return {"outcome": "reinforced", "chunk_id": hit.chunk_id}
        if verdict is ConsistencyVerdict.CONFLICT:
            hit_emb = self._stores.embed.embed(hit.text)
            vector.upsert_chunk(
                hit.model_copy(update={"rules": rules_dicts}),
                hit_emb.dense,
                hit_emb.sparse,
            )
            vector.update_chunk_state([hit.chunk_id], needs_reconcile=True)
            self._audit(
                profile_id,
                "remember",
                {"chunk_id": hit.chunk_id, "profile_id": profile_id, "outcome": "needs_reconcile"},
                actor=actor,
            )
            return {"outcome": "needs_reconcile", "chunk_id": hit.chunk_id}
        vector.upsert_chunk(stamp, embedded.dense, embedded.sparse)
        self._audit(
            profile_id, "remember", {"chunk_id": stamp.chunk_id, "profile_id": profile_id}, actor=actor
        )
        return {"outcome": "new_chunk", "chunk_id": stamp.chunk_id}

    # ------------------------------------------------------------ audit

    def audit(
        self,
        *,
        profile_id: str,
        node_id: str | None = None,
        chunk_id: str | None = None,
    ) -> dict[str, Any]:
        """Provenance + version chain for one target, plus relevant audit rows."""
        if chunk_id is not None:
            chunk = self._stores.vector.get_chunk(chunk_id)
            if chunk is None:
                raise MemoryNotFoundError(f"chunk {chunk_id!r} not found")
            return {
                "target": {"type": "chunk", "id": chunk_id},
                "provenance": chunk.provenance.model_dump(),
                "versions": [],
                "audit": self._relevant_audit(profile_id, chunk_id),
            }
        chain = self._stores.graph.versions(node_id) if node_id is not None else []
        if not chain:
            raise MemoryNotFoundError(f"node {node_id!r} not found")
        return {
            "target": {"type": "node", "id": node_id},
            "provenance": chain[-1].provenance.model_dump(),
            "versions": [version.model_dump() for version in chain],
            "audit": self._relevant_audit(profile_id, node_id if node_id is not None else "?"),
        }

    def _relevant_audit(self, profile_id: str, target_id: str) -> list[dict[str, Any]]:
        """Audit rows referencing ``target_id`` (client-side filter: the port's
        AuditFilter carries no target or profile dimension)."""
        page = self._stores.meta.audit_query(AuditFilter(), Page(offset=0, limit=200))
        relevant: list[dict[str, Any]] = []
        for entry in page.items:
            detail = entry.detail or {}
            if target_id not in self._audit_targets(detail):
                continue
            if profile_id not in (detail.get("profile_id") or (profile_id,)) and entry.actor != "capture":
                # Keep rows whose detail omits a profile (system-level) but drop
                # rows that name a different profile explicitly (D5 isolation).
                continue
            relevant.append(
                {
                    "actor": entry.actor,
                    "action": entry.action,
                    "detail": detail,
                    "at": entry.at,
                    "id": entry.id,
                }
            )
        return relevant

    @staticmethod
    def _audit_targets(detail: dict[str, Any]) -> tuple[str, ...]:
        targets: list[Any] = []
        for key in ("chunk_id", "node_id"):
            if detail.get(key):
                targets.append(detail[key])
        for key in ("chunks", "nodes"):
            values = detail.get(key)
            if isinstance(values, list):
                targets.extend(values)
        return tuple(str(target) for target in targets)

    # ------------------------------------------------------------ timeline

    def timeline(self, *, profile_id: str, node_id: str | None = None) -> dict[str, Any]:
        """Per-node version replay, else a profile-wide recent-first listing."""
        if node_id is not None:
            version_events = self._stores.graph.timeline(node_id)
            if not version_events:
                raise MemoryNotFoundError(f"node {node_id!r} not found")
            return {
                "events": [
                    {"when": event.when, "version": event.version, "summary": event.summary}
                    for event in version_events
                ]
            }
        chunk_page = self._stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id), Page(offset=0, limit=100)
        )
        node_page = self._stores.graph.list_nodes(
            NodeFilter(profile_id=profile_id), Page(offset=0, limit=100)
        )
        events: list[dict[str, Any]] = [
            {
                "when": chunk.ingested_at,
                "kind": "chunk",
                "id": chunk.chunk_id,
                "version": None,
                "summary": chunk.text,
            }
            for chunk in chunk_page.items
        ]
        for node in node_page.items:
            summary = node.props.get("statement")
            events.append(
                {
                    "when": node.updated_at,
                    "kind": "node",
                    "id": node.node_id,
                    "version": node.version,
                    "summary": summary if isinstance(summary, str) and summary else node.node_id,
                }
            )
        events.sort(key=lambda event: event["when"], reverse=True)
        return {"events": events}

    # ------------------------------------------------------------ session resume (B2)

    def session_recent(
        self,
        *,
        profile_id: str,
        per_session: int = 20,
        sessions: int = 2,
        exclude_session_id: str | None = None,
        self_session_id: str | None = None,
        active_sessions: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """B2 time-ordered resume: the newest sessions' verbatim chunk tails.

        Read-only over the store's ingested_at-desc listing (lancedb driver
        guarantee); the page window is a pragmatic guardrail against very long
        sessions diluting the older group out of view. ``exclude_session_id``
        widens the page by one session's worth of chunks so the caller's own
        session can be filtered out without starving the survivor groups.
        Every group carries its exact per-session window; ``self_window`` is
        the caller-named session's window when it has chunks, else null."""
        limit = min(2000, (sessions + (1 if exclude_session_id else 0)) * per_session * 4)
        page = self._stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id), Page(offset=0, limit=limit)
        )
        groups = _group_session_tails(
            page.items,
            per_session=per_session,
            sessions=sessions,
            exclude_session_id=exclude_session_id,
        )
        for group in groups:
            window = _scan_session_window(
                self._stores.vector, profile_id=profile_id, session_id=group["session_id"]
            )
            group["window"] = _window_iso(window)
            group["window_truncated"] = window.window_truncated
        self_window: dict[str, Any] | None = None
        if self_session_id:
            window = _scan_session_window(
                self._stores.vector, profile_id=profile_id, session_id=self_session_id
            )
            if window.first is not None:
                self_window = {
                    "session_id": self_session_id,
                    "window": _window_iso(window),
                    "chunk_count": window.chunk_count,
                    "active": self_session_id in active_sessions,
                }
        result: dict[str, Any] = {
            "profile_id": profile_id,
            "sessions": groups,
            "self_window": self_window,
        }
        rules_budget = self._build_rules_budget(
            profile_id=profile_id, session_id=self_session_id, per_session=per_session
        )
        if rules_budget is not None:
            result["rules_budget"] = rules_budget.model_dump()
        return result

    def _build_rules_budget(
        self,
        *,
        profile_id: str,
        session_id: str | None,
        per_session: int,
    ) -> RulesBudgetBlock | None:
        """B2.7 Scheme 3: aggregate the standing rules the caller's session may
        rely on. Only scope=session for THIS session + scope=profile + scope=
        global participate; another session's session-scoped rules never leak.
        Returns None when no applicable rule exists (absent semantics — the
        caller omits the ``rules_budget`` key)."""
        page = self._stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id, rules_not_null=True),
            Page(offset=0, limit=1000),
        )
        exclude: list[str] = []
        boost: dict[str, float] = {}
        seen_exclude: set[str] = set()
        found = False
        for chunk in page.items:
            for rule_dict in chunk.rules:
                rule = RecallRule(**rule_dict)
                if not self._rule_in_scope(rule, session_id):
                    continue
                found = True
                if rule.kind == "exclude_entities":
                    for excluded in rule.value if isinstance(rule.value, list) else []:
                        if excluded not in seen_exclude:
                            seen_exclude.add(excluded)
                            exclude.append(excluded)
                elif rule.kind == "entity_boost":
                    entity, coefficient = self._entity_boost_value(rule.value)
                    if entity is not None:
                        boost[entity] = max(boost.get(entity, 0.0), coefficient)
        if not found:
            return None
        return RulesBudgetBlock(
            auto_recall_focal_floor=self._config.capture.auto_recall_focal_floor,
            auto_recall_budget_chars=self._config.capture.auto_recall_budget_chars,
            exclude_entities=exclude,
            entity_boost=boost,
            time_window_turns=per_session,
            budget_consumed=self._budget_consumed.get((profile_id, session_id or ""), 0),
        )

    @staticmethod
    def _rule_in_scope(rule: RecallRule, session_id: str | None) -> bool:
        if rule.scope != "session":
            return True
        return session_id is not None and rule.session_id == session_id

    @staticmethod
    def _entity_boost_value(value: float | str | list[str]) -> tuple[str | None, float]:
        """Decode an entity_boost rule's ``value`` (``[entity, coefficient]``)."""
        if isinstance(value, list) and len(value) >= 2:
            try:
                return str(value[0]), float(value[1])
            except (TypeError, ValueError):
                return None, 0.0
        return None, 0.0

    def session_windows(
        self,
        *,
        profile_id: str,
        sessions: int = 3,
        active_sessions: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """The exact per-session time-window surface: each discovered session's
        true first/latest from a bounded full scan, its chunk count, the
        live-capture active flag, and the scan-limit truncation marker."""
        page = self._stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id), Page(offset=0, limit=SESSION_WINDOW_SCAN_LIMIT)
        )
        result: list[dict[str, Any]] = []
        for session_id in _discover_session_ids(page.items, sessions=sessions):
            window = _scan_session_window(self._stores.vector, profile_id=profile_id, session_id=session_id)
            result.append(
                {
                    "session_id": session_id,
                    "window": _window_iso(window),
                    "chunk_count": window.chunk_count,
                    "active": session_id in active_sessions,
                    "window_truncated": window.window_truncated,
                }
            )
        return {"profile_id": profile_id, "sessions": result}

    # ------------------------------------------------------------ B2.1 T2 mid-session recall

    def note_user_prompt(self, profile_id: str, session_id: str, text: str) -> None:
        """Run the embedding-free focal scan for one user prompt and park the
        budgeted selection as the session's pending slot (D1/D3/D4).

        Called from the /ingest handler BEFORE it answers 202 (ack-implies-
        ready): the pull that follows the ack finds the slot already filled.
        The scan reads the stores outside the lock (only the seen-set is
        snapshotted under it); the pull's serve=mark-seen is atomic under the
        lock and filters against the merged seen-set, so a stale scan can
        never resurrect a served item.
        """
        key = (profile_id, session_id)
        with self._pending_lock:
            seen = set(self._seen_chunk_ids.get(key, ()))
            seq = self._scan_seq.get(key, 0) + 1
            self._scan_seq[key] = seq
            epoch = self._session_epoch.get(key, 0)
        items, non_focal = self._focal_scan(profile_id, session_id, text, seen)
        with self._pending_lock:
            # NIT-5a: only the LAST scan to start may park — a scan that
            # captured its sequence before a newer scan wrote must not
            # overwrite the newer selection. NIT-5b: a settle bumps the epoch,
            # so a scan started before /session/end cannot re-park afterwards.
            if self._session_epoch.get(key, 0) != epoch:
                return
            if self._scan_seq.get(key, 0) != seq:
                return
            self._pending_slots[key] = items
            self._pending_non_focal[key] = non_focal

    def _focal_scan(
        self,
        profile_id: str,
        session_id: str,
        text: str,
        seen: set[str],
    ) -> tuple[list[dict[str, str]], int]:
        """Embedding-free focal selection (D3): cue entities anchor a metadata
        read over the vector and graph stores at the focal decay floor; the
        requesting session and the daemon-seen ids are excluded; the budget
        admits greedily by decay (tie newest-first: ingested_at ms desc, then
        turn_start desc) with the T1 slice semantics (D4). Returns (items,
        non_focal_above_floor)."""
        entities = tuple(self._cues.extract(text).cues.entities)
        query_folded = {str.casefold(e) for e in entities}
        if not entities:
            # no entity anchor: nothing can be focal; the weak-association
            # probe still reports the decay-healthy population
            return [], self._non_focal_count(profile_id, session_id, query_folded, seen)
        floor = self._config.capture.auto_recall_focal_floor
        page = self._stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id, entities=entities, min_decay=floor),
            Page(0, _SCAN_PAGE_LIMIT),
        )
        # decay, recency-stamp, turn_start, kind, id, text — sorted greedily
        # by decay desc then newest-first (D4): stamp desc, then turn_start
        # desc. The stamp is quantized to millisecond precision: the graph
        # driver persists updated_at through an ISO8601 ms round-trip
        # (storage/drivers/_time.py), so a chunk ingested_at with sub-ms noise
        # and a node updated_at from the same instant only tie when compared
        # at the coarser (representable) precision. The turn_start third key
        # breaks the same-stamp tie in reading order; nodes carry the -1
        # sentinel (they have no turn window) so a chunk always precedes a
        # node on a full tie (chunks first).
        candidates: list[tuple[float, float, int, str, str, str]] = []
        for chunk in page.items:
            if chunk.provenance.session_id == session_id:
                continue  # the requesting session never sees its own chunks
            if chunk.chunk_id in seen:
                continue  # daemon-seen ids are excluded at scan time
            stored = {str.casefold(e) for e in chunk.cues.entities}
            if not stored & query_folded:
                continue  # the casefold authority (mirror of the Freshness probe)
            candidates.append(
                (
                    chunk.decay_weight,
                    round(chunk.ingested_at, 3),
                    chunk.turn_start if chunk.turn_start is not None else -1,
                    "chunk",
                    chunk.chunk_id,
                    chunk.text,
                )
            )
        node_page = self._stores.graph.list_nodes(
            NodeFilter(profile_id=profile_id, entities=entities, min_decay=floor),
            Page(0, _SCAN_PAGE_LIMIT),
        )
        for node in node_page.items:
            statement = node.props.get("statement")
            if not isinstance(statement, str) or not statement:
                continue
            stored = {str.casefold(e) for e in node.entities}
            if not stored & query_folded:
                continue
            # Sentinel -1: node has no turn_start. -1 and 0 are equivalent for
            # chunks-first (both ≤ any chunk turn_start ≥ 0); -1 is canonical.
            candidates.append(
                (node.decay_weight, round(node.updated_at, 3), -1, "node", node.node_id, statement)
            )
        budget = self._config.capture.auto_recall_budget_chars
        items: list[dict[str, str]] = []
        remaining = budget
        for _decay, _stamp, _turn_start, kind, candidate_id, text in sorted(
            candidates, key=lambda c: (-c[0], -c[1], -c[2])
        ):
            cost = len(text) + 1
            if cost <= remaining:
                items.append({"kind": kind, "id": candidate_id, "text": text})
                remaining -= cost
                continue
            slice_budget = remaining - 2  # the "…" marker and the newline (T1)
            if slice_budget < _MIN_SLICE_CHARS:
                break  # the boundary item is dropped ALONG WITH everything older
            items.append({"kind": kind, "id": candidate_id, "text": "…" + text[-slice_budget:]})
            remaining = 0
            break
        return items, self._non_focal_count(profile_id, session_id, query_folded, seen)

    def _non_focal_count(
        self, profile_id: str, session_id: str, query_folded: set[str], seen: set[str]
    ) -> int:
        """The T4 weak-association probe: decay-healthy chunks (>= the
        NON_FOCAL_FLOOR) outside the requesting session that the focal scan
        did NOT select — the entity-blind / entity-miss population the
        mid-session recall can never serve. Nodes are excluded (consolidated
        graph entries are a different population); the probe is a bounded,
        per-scan observation, never a selection."""
        page = self._stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id, min_decay=NON_FOCAL_FLOOR),
            Page(0, _SCAN_PAGE_LIMIT),
        )
        floor = self._config.capture.auto_recall_focal_floor
        count = 0
        for chunk in page.items:
            if chunk.provenance.session_id == session_id:
                continue
            if chunk.chunk_id in seen:
                continue
            stored = {str.casefold(e) for e in chunk.cues.entities}
            if stored & query_folded and chunk.decay_weight >= floor:
                continue  # a focal candidate (selected or budget-dropped) is not "non-focal"
            count += 1
        return count

    def recall_pending(self, profile_id: str, session_id: str, seen_chunk_ids: list[str]) -> dict[str, Any]:
        """Serve the pending slot (D6): the caller's seen ids (its T1-injected
        flat list, <=16) join the daemon's SERVED-ids set as the pull-time
        exclusion — the merged set selects the candidates; only a NON-EMPTY
        serve consumes the slot and persists the served ids (serve =
        mark-seen, the stale-scan safety net). An empty serve (everything
        excluded) leaves the slot untouched so a fresh pull can still consume
        it — D8's "empty pull keeps the flag armed" depends on exactly this.
        The whole exchange is atomic under the lock.

        ``budget_chars`` reports the daemon's effective item budget (the
        selection cap the hook must respect when re-checking the block).
        ``slot_consumed`` reports whether the slot is/was consumed: true for a
        serve that consumed it, and — via the consumed tombstone (QA
        BLOCKER-2) — true for a retry pull after an earlier serve, so the hook
        clears its arm instead of pulling an empty slot forever. NIT-4: the
        serve+mark is gated on ``enabled`` — a config-off pull consumes
        nothing and marks nothing, so a later flip-on still serves the parked
        selection. NIT-6: a pull for a session with no slot never materializes
        any lifecycle state."""
        key = (profile_id, session_id)
        enabled = self._config.capture.auto_recall
        budget = self._config.capture.auto_recall_budget_chars
        if not enabled:
            return {
                "enabled": False,
                "items": [],
                "non_focal_above_floor": 0,
                "budget_chars": budget,
                "slot_consumed": False,
            }
        with self._pending_lock:
            served = self._seen_chunk_ids.get(key)
            excluded = (served if served is not None else frozenset()) | set(seen_chunk_ids)
            slot = self._pending_slots.get(key)
            items = [item for item in slot if item["id"] not in excluded] if slot is not None else []
            if items:
                if served is None:
                    served = set()
                    self._seen_chunk_ids[key] = served
                served.update(item["id"] for item in items)
                self._pending_slots.pop(key, None)
                non_focal = self._pending_non_focal.pop(key, 0)
                self._pending_consumed[key] = True  # the serve leaves its tombstone
                # B2.7: accrue the daemon-side T2 char count (budget_consumed).
                self._budget_consumed[key] = self._budget_consumed.get(key, 0) + sum(
                    len(item["text"]) + 1 for item in items
                )
                slot_consumed = True
            elif slot is not None:
                # D6 empty serve: the slot survives so a fresh pull can still
                # consume it; nothing is marked, the tombstone is untouched.
                non_focal = self._pending_non_focal.get(key, 0)
                slot_consumed = False
            else:
                # No slot: a consumed tombstone means an earlier serve already
                # took it (the hook's clearing signal); otherwise this session
                # never had anything to serve (or was settled) — false.
                non_focal = 0
                slot_consumed = self._pending_consumed.get(key, False)
        return {
            "enabled": True,
            "items": items,
            "non_focal_above_floor": non_focal,
            "budget_chars": budget,
            "slot_consumed": slot_consumed,
        }

    def end_session(self, profile_id: str, session_id: str) -> None:
        """Drop a settled session's pending slot, probe count, seen-set and
        consumed tombstone (D6 + QA BLOCKER-2): /session/end is the terminal
        signal — a pull after the settle finds nothing to serve, the tombstone
        is gone, and the seen-set stops accumulating. The settlement epoch is
        bumped under the lock (NIT-5b): a scan started BEFORE the settle can
        never re-park a slot afterwards."""
        key = (profile_id, session_id)
        with self._pending_lock:
            self._pending_slots.pop(key, None)
            self._pending_non_focal.pop(key, None)
            self._seen_chunk_ids.pop(key, None)
            self._pending_consumed.pop(key, None)
            self._budget_consumed.pop(key, None)
            self._scan_seq.pop(key, None)
            self._session_epoch[key] = self._session_epoch.get(key, 0) + 1

    # ------------------------------------------------------------ reinforce (B2.1 T3)

    def reinforce(
        self,
        *,
        profile_id: str,
        chunk_ids: list[str],
        node_ids: list[str],
    ) -> dict[str, Any]:
        """T3 consumption-evidence reinforcement (B2.1): apply the Reinforcer's
        FR-4.2 rebound to the attested chunk/node usage. Best-effort like the
        recall accounting path — a store fault must never fail the hook's
        fire-and-forget POST, and unknown ids are ignored silently.

        ``profile_id`` is deliberately NOT forwarded: target resolution is
        profile-agnostic by design (the ids are unguessable store keys and the
        usage is attested server-side by the hook's citation guard, so there
        is no cross-profile guessing surface to defend against)."""
        del profile_id  # the Reinforcer resolves targets store-side
        try:
            self._reinforcer.record_hits(chunk_ids, node_ids)
        except Exception:  # pragma: no cover - usage accounting must not fail the caller
            logger.warning("reinforce event write failed; consumption guard proceeds", exc_info=True)
        return {"status": "ok"}

    # ------------------------------------------------------------ export

    def export(self, *, profile_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Stable paged profile dump including provenance (schema-tagged)."""
        chunk_page = self._stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id), Page(offset=offset, limit=limit)
        )
        node_page = self._stores.graph.list_nodes(
            NodeFilter(profile_id=profile_id), Page(offset=offset, limit=limit)
        )
        return {
            "schema": "mnemoseed.memory.export/1",
            "profile_id": profile_id,
            "chunks": [chunk.model_dump() for chunk in chunk_page.items],
            "nodes": [node.model_dump() for node in node_page.items],
            "paging": {
                "chunk_total": chunk_page.total,
                "node_total": node_page.total,
                "offset": offset,
                "limit": limit,
            },
        }

    # ------------------------------------------------------------ forget_this

    def forget_this(
        self,
        *,
        profile_id: str,
        chunk_id: str | None = None,
        node_id: str | None = None,
        entity: str | None = None,
        actor: str = "console",
    ) -> dict[str, Any]:
        """GDPR deletion (design/03 storage-layer erasure). Chunks are physically
        deleted; graph nodes are tombstoned so their full version chains stay
        reachable through the store layer (versions / audit / timeline) while
        every current read stops seeing them."""
        removed_chunks: list[str] = []
        removed_nodes: list[str] = []
        if chunk_id is not None:
            if self._stores.vector.get_chunk(chunk_id) is None:
                raise MemoryNotFoundError(f"chunk {chunk_id!r} not found")
            self._stores.vector.delete_chunk(chunk_id)
            removed_chunks.append(chunk_id)
        if node_id is not None:
            if self._stores.graph.get_node(node_id) is None:
                raise MemoryNotFoundError(f"node {node_id!r} not found")
            self._stores.graph.tombstone(node_id, deleted_at=time.time())
            removed_nodes.append(node_id)
        if entity is not None:
            for chunk in self._stores.vector.list_chunks(
                ChunkFilter(profile_id=profile_id, entities=(entity,)), Page(offset=0, limit=1000)
            ).items:
                self._stores.vector.delete_chunk(chunk.chunk_id)
                removed_chunks.append(chunk.chunk_id)
            for node in self._stores.graph.list_nodes(
                NodeFilter(profile_id=profile_id, entities=(entity,)), Page(offset=0, limit=1000)
            ).items:
                self._stores.graph.tombstone(node.node_id, deleted_at=time.time())
                removed_nodes.append(node.node_id)
        self._audit(
            profile_id,
            "forget_this",
            {"chunks": removed_chunks, "nodes": removed_nodes, "profile_id": profile_id},
            actor=actor,
        )
        return {"removed": {"chunks": removed_chunks, "nodes": removed_nodes}}

    # ------------------------------------------------------------ plumbing

    def _audit(self, profile_id: str, action: str, detail: dict[str, Any], *, actor: str = "console") -> None:
        self._stores.meta.audit_append(AuditEntry(actor=actor, action=action, detail=detail, at=time.time()))


# ---------------------------------------------------------------- router

# A2 local trim: NO identity gate — the daemon is localhost-only by default and
# every route takes an explicit profile_id. Audit attribution comes from the
# X-MnemoSeed-Actor header (cli|console), default "console".
router = APIRouter()


def _route_404(exc: MemoryNotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/memory/recall")
def memory_recall(req: RecallRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    return service.recall(
        profile_id=req.profile_id,
        query=req.query,
        host=req.host,
        project=req.project,
        time_bucket=req.time_bucket,
        top_k=req.top_k,
        budget=req.budget,
    )


@router.post("/memory/remember")
def memory_remember(req: RememberRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    return service.remember(
        profile_id=req.profile_id,
        text=req.text,
        actor=resolve_actor(request),
        rules=req.rules,
    )


@router.post("/memory/audit")
def memory_audit(req: AuditRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    try:
        return service.audit(profile_id=req.profile_id, node_id=req.node_id, chunk_id=req.chunk_id)
    except MemoryNotFoundError as exc:
        raise _route_404(exc) from exc


@router.post("/memory/timeline")
def memory_timeline(req: TimelineRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    try:
        return service.timeline(profile_id=req.profile_id, node_id=req.node_id)
    except MemoryNotFoundError as exc:
        raise _route_404(exc) from exc


@router.post("/memory/export")
def memory_export(req: ExportRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    return service.export(profile_id=req.profile_id, offset=req.offset, limit=req.limit)


@router.post("/session/recent")
def session_recent(req: SessionRecentRequest, request: Request) -> dict[str, Any]:
    """B2 (PRD-B2): time-ordered session tails for the resume seam — the
    "continue where the last conversation ended" surface, verbatim chunks,
    newest session group first, tails ascending. ``exclude_session_id`` lets
    the session-start injection read skip the caller's own session."""
    service: MemoryService = request.app.state.memory
    sessions = getattr(getattr(request.app.state, "capture", None), "sessions", None)
    active = frozenset(sessions()) if sessions is not None else frozenset()
    return service.session_recent(
        profile_id=req.profile_id,
        per_session=req.per_session,
        sessions=req.sessions,
        exclude_session_id=req.exclude_session_id,
        self_session_id=req.self_session_id,
        active_sessions=active,
    )


@router.post("/session/windows")
def session_windows(req: SessionWindowsRequest, request: Request) -> dict[str, Any]:
    """Exact per-session chunk windows for the time-comparison surface: the
    daemon supplies the structure, the consumer decides which window a mtime
    belongs to."""
    service: MemoryService = request.app.state.memory
    sessions = getattr(getattr(request.app.state, "capture", None), "sessions", None)
    active = frozenset(sessions()) if sessions is not None else frozenset()
    return service.session_windows(profile_id=req.profile_id, sessions=req.sessions, active_sessions=active)


@router.post("/session/recall-pending")
def session_recall_pending(req: RecallPendingRequest, request: Request) -> dict[str, Any]:
    """B2.1 T2 (PRD-B2.1): the mid-session auto-recall pull — serve the focal
    selection parked by the session's most recent user prompt (ack-implies-
    ready), merging the caller's seen ids so chunks it already holds are
    never re-served."""
    service: MemoryService = request.app.state.memory
    return service.recall_pending(
        profile_id=req.profile_id,
        session_id=req.session_id,
        seen_chunk_ids=req.seen_chunk_ids,
    )


@router.post("/memory/reinforce")
def memory_reinforce(req: ReinforceRequest, request: Request) -> dict[str, Any]:
    """B2.1 T3 (PRD-B2.1): consumption-evidence reinforcement — the hook's
    consumption guard attests that an injected slice was actually cited by the
    assistant, and this endpoint turns that attestation into a real usage
    event (TA-6: being injected is not being used)."""
    service: MemoryService = request.app.state.memory
    return service.reinforce(
        profile_id=req.profile_id,
        chunk_ids=req.chunk_ids,
        node_ids=req.node_ids,
    )


@router.post("/memory/forget_this")
def memory_forget_this(req: ForgetRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    try:
        return service.forget_this(
            profile_id=req.profile_id,
            chunk_id=req.chunk_id,
            node_id=req.node_id,
            entity=req.entity,
            actor=resolve_actor(request),
        )
    except MemoryNotFoundError as exc:
        raise _route_404(exc) from exc


# ------------------------------------------------------------ /dream surface


def _trigger_payload(status: TriggerStatus) -> dict[str, Any]:
    """Serialized trigger observability (state, pending depths, ranges)."""
    last = status.last_event
    current = status.current_range
    return {
        "profile_id": status.profile_id,
        "state": status.state.value,
        "pending_queue": status.pending_queue,
        "pending_manual": status.pending_manual,
        "last_event": (
            {
                "kind": last.kind.value,
                "profile_id": last.profile_id,
                "turn_range": {"start": last.turn_range.start, "end": last.turn_range.end},
                "fired_at": last.fired_at,
            }
            if last is not None
            else None
        ),
        "current_range": ({"start": current.start, "end": current.end} if current is not None else None),
    }


@router.post("/memory/dream_once")
async def memory_dream_once(req: DreamRequest, request: Request) -> dict[str, Any]:
    """One manual dream cycle (FR-2.8 ``dream --once``).

    The launch decision is made on the dream worker thread (the /memory surface
    must never block on the snapshot -> reflect -> merge chain); the handler
    awaits the worker's decision so the response keeps the same ``launched`` /
    ``state`` contract the synchronous path had.
    """
    worker: DreamWorker = request.app.state.dream_worker
    launched = await worker.submit_dream_once(req.profile_id)
    trigger: DreamTrigger = request.app.state.dream
    status = trigger.status(req.profile_id)
    payload = _trigger_payload(status)
    payload["launched"] = launched
    return payload


@router.post("/memory/dream_status")
async def memory_dream_status(req: DreamRequest, request: Request) -> dict[str, Any]:
    trigger: DreamTrigger = request.app.state.dream
    return _trigger_payload(trigger.status(req.profile_id))
