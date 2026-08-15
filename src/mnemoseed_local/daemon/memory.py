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

Determinism: no clocks except reading the live timestamp where the semantic
contract is timestamp-based (forget/deletion time, remember ``asserted_at``,
usage-event ``last_hit_at``); no randomness; no network.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from typing import Annotated, Any, Self

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from mnemoseed_local.capture.stamper import ConsistencyVerdict, NearDuplicateChecker, WriteConfig
from mnemoseed_local.config import Config
from mnemoseed_local.daemon.actor import resolve_actor
from mnemoseed_local.decay import Reinforcer
from mnemoseed_local.dream import DreamTrigger, TriggerStatus
from mnemoseed_local.retrieve.assemble import (
    AssembledContext,
    AssembledEntry,
    Assembler,
)
from mnemoseed_local.retrieve.cues import CueExtractor
from mnemoseed_local.retrieve.hybrid import HybridRetriever
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Provenance, ProvenanceEvent
from mnemoseed_local.schema.turn import ProfileRef
from mnemoseed_local.storage.factory import Stores
from mnemoseed_local.storage.ports import (
    AuditEntry,
    AuditFilter,
    ChunkFilter,
    NodeFilter,
    Page,
    WeightUpdate,
)

logger = logging.getLogger("mnemoseed_local.daemon.memory")

# Explicit-pin provenance source marker (FR-3.1). A /memory/remember write is
# asserted by the user and never merges into the capture provenance channel.
EXPLICIT_PIN_SOURCE = "memory.remember"


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
        }

    # ------------------------------------------------------------ remember

    def remember(self, *, profile_id: str, text: str, actor: str = "console") -> dict[str, Any]:
        """Write an explicit user pin, mirroring the StampWriter's dual-branch
        near-duplicate flow: a strong consistent hit reinforces in place, a
        conflict flags needs_reconcile, anything else becomes a new chunk.
        Provenance is append-only; the explicit-pin source is never rewritten.
        """
        now = time.time()
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
            vector.update_weights([WeightUpdate(hit.chunk_id, decay_weight=rebound, last_reinforced=now)])
            self._audit(
                profile_id,
                "remember",
                {"chunk_id": hit.chunk_id, "profile_id": profile_id, "outcome": "reinforced"},
                actor=actor,
            )
            return {"outcome": "reinforced", "chunk_id": hit.chunk_id}
        if verdict is ConsistencyVerdict.CONFLICT:
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
    return service.remember(profile_id=req.profile_id, text=req.text, actor=resolve_actor(request))


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

    ``async def`` so the whole snapshot -> reflect -> merge -> safe-clear chain
    runs on the app event-loop thread: the daemon's sqlite connections are bound
    to that thread and refuse cross-thread use.
    """
    trigger: DreamTrigger = request.app.state.dream
    launched = trigger.dream_once(req.profile_id)
    status = trigger.status(req.profile_id)
    payload = _trigger_payload(status)
    payload["launched"] = launched
    return payload


@router.post("/memory/dream_status")
async def memory_dream_status(req: DreamRequest, request: Request) -> dict[str, Any]:
    trigger: DreamTrigger = request.app.state.dream
    return _trigger_payload(trigger.status(req.profile_id))
