"""Merge write-back + salvage queue (PRD-02 T4; FR-2.3 / FR-2.4 / AC-2 /
AC-3 / NFR-2.3).

The Merger consumes a T3 ReflectionResult together with its Snapshot and
commits it to the graph double-instance: ``core`` routes to the main graph
(``graph.main``), ``isolated`` and ``salvage`` route to the isolated named
instance (``graph.isolated``). A tier-3-evidenced triple is NEVER written to the
main graph even if an upstream route says "core" — this engine-side re-check is
the last line of defense behind T3's route re-derivation (anti-backflow,
design/02 section 4). Salvage triples are additionally enqueued into the
salvage re-view channel by appending an audit record (actor=profile,
action="salvage_queued") to the append-only audit log, which survives daemon
restarts with no new table.

Write-back idempotency (NFR-2.3): before writing, the Merger probes the target
graph for an existing node over the same (subject, predicate, object) triple
via the port's ``find_same_predicate``. An exact object match reinforces the
existing node in place — confidence / last_reinforced / reinforce_count update
and a "reinforced" provenance event is appended, the source chain is never
rewritten (append-only provenance). Otherwise a new deterministic-content-hash
node is created. Re-running a merge — crash + ``resume_merge``, or a second
boot — therefore never duplicates graph rows or salvage entries.

T3b (design/01 §4.8): the isolated graph instance is MANDATORY. A missing
``graph_isolated`` with any triple whose effective route needs it (a
floor-downgraded core triple or a salvage triple) fails the pass TYPED before
the first write — atomic, no partial commit, no stranded tier-3 rows. The
isolated requirement is enforced upstream too: config load and configwrite
reject a config with a non-zero floor and no isolated instance, and the daemon
refuses to boot.

Completion: after every triple of the pass commits, the Merger fires the
``on_committed`` seam exactly once (wired to trigger.on_merge_committed, which
runs the safe-clear purger). Any failure returns a typed MergeOutcome with no
completion callback; the snapshot stays journaled for resume_merge. The Merger
never raises into the daemon.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mnemoseed_local.dream.reflect import ReflectedTriple, ReflectionResult, Route
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotPhase
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import CognitiveTier, Provenance, ProvenanceEvent
from mnemoseed_local.storage.ports import AuditEntry, AuditFilter, GraphStore, MetaStore, Page, TurnRange

if TYPE_CHECKING:
    from mnemoseed_local.config import Config

logger = logging.getLogger("mnemoseed_local.dream.merge")

_SALVAGE_ACTION = "salvage_queued"

_NODE_TYPE_BY_PREDICATE: dict[str, NodeType] = {
    "prefers": NodeType.PREFERENCE,
    "has_habit": NodeType.HABIT,
    "decided": NodeType.DECISION,
}


@dataclass(frozen=True)
class MergeSummary:
    """What one merge pass wrote, per route, for observability and audit."""

    profile_id: str
    snapshot_id: str
    turn_range: TurnRange
    written: int  # total node write ops (created + reinforced)
    created: int
    reinforced: int
    core: int
    isolated: int
    salvage: int  # salvage triples enqueued for the re-view channel
    deflected: int  # anti-backflow drops (tier-3 evidence claimed "core")


@dataclass(frozen=True)
class MergeOutcome:
    """Typed result of one merge pass. ``ok`` is always set; ``committed`` is
    True exactly when the completion seam fired (every triple wrote back)."""

    ok: bool
    summary: MergeSummary | None = None
    error: str | None = None
    committed: bool = False
    skipped: bool = False  # marker gate: merge had already completed


class Merger:
    """Write back a ReflectionResult across the graph double-instance.

    ``graph_main`` is the primary "main" graph; ``graph_isolated`` is the
    mandatory isolated named instance (design/01 §4.8): floor-downgraded core
    and salvage triples route here, never to main. A missing isolated instance
    with any triple needing it fails the pass atomically before the first
    write (never strands, never pollutes main). ``meta`` carries the salvage
    queue in the append-only audit log.
    """

    def __init__(
        self,
        *,
        graph_main: GraphStore,
        graph_isolated: GraphStore | None,
        meta: MetaStore,
        on_committed: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
        config: Config | None = None,
    ) -> None:
        self._graph_main = graph_main
        self._graph_isolated = graph_isolated
        self._meta = meta
        self._on_committed = on_committed
        self._clock = clock
        self._config = config

    def _confidence_floor(self) -> float:
        """The core-confidence floor (dream.core_confidence_floor, T3a): read
        LIVE from the bound Config at every merge, so a configwrite change
        applies to the next merge of this same instance — no daemon restart."""
        if self._config is None:
            return 0.0
        return self._config.dream.core_confidence_floor

    def merge(self, snapshot: Snapshot, result: ReflectionResult) -> MergeOutcome:
        """Commit one reflection result. Never raises; a failure degrades into
        a typed outcome and the snapshot stays journaled for resume_merge."""
        if SnapshotPhase.MERGE_DONE.value in snapshot.phases:
            return MergeOutcome(ok=True, skipped=True)
        try:
            summary = self._commit_triples(snapshot, result)
        except Exception as exc:  # noqa: BLE001 - typed outcome, never a daemon raise
            logger.warning("merge failed for %s: %s", snapshot.profile_id, exc)
            return MergeOutcome(ok=False, error=str(exc))
        if self._on_committed is not None:
            self._on_committed(snapshot.profile_id)
        return MergeOutcome(ok=True, summary=summary, committed=True)

    # ------------------------------------------------------------ routing

    def _is_downgrade_candidate(self, triple: ReflectedTriple, floor: float) -> bool:
        """A core triple the confidence floor would route to isolated.

        Mirrors the loop's downgrade condition exactly, so the atomic pre-pass
        and the routing loop can never drift: a tier-3 core triple is deflected
        by the anti-backflow gate BEFORE the floor is consulted, so it is never
        a downgrade candidate.
        """
        return (
            triple.route is Route.CORE
            and triple.confidence < floor
            and not any(t is CognitiveTier.TIER_3 for t in triple.tiers)
        )

    def _effective_route(self, triple: ReflectedTriple, floor: float) -> Route | None:
        """The route this triple actually commits under, after both engine
        gates: the anti-backflow deflection first (a tier-3 core triple never
        reaches main — effective route None), then the floor downgrade (a
        below-floor core triple reroutes to ISOLATED)."""
        if any(t is CognitiveTier.TIER_3 for t in triple.tiers) and triple.route is Route.CORE:
            return None
        if self._is_downgrade_candidate(triple, floor):
            return Route.ISOLATED
        return triple.route

    def _needs_isolated(self, triple: ReflectedTriple, floor: float) -> bool:
        """Whether this triple's effective route requires the isolated graph."""
        route = self._effective_route(triple, floor)
        return route is Route.ISOLATED or route is Route.SALVAGE

    def _commit_triples(self, snapshot: Snapshot, result: ReflectionResult) -> MergeSummary:
        created = 0
        reinforced = 0
        core = 0
        isolated = 0
        salvage = 0
        deflected = 0
        floor = self._confidence_floor()
        # D-T3a-1 (extended T3b): the no-isolated failure is ATOMIC. Any triple
        # whose EFFECTIVE route needs the isolated graph — a floor-downgraded
        # core triple or a salvage triple — with no isolated instance fails
        # typed BEFORE the first write: no partial commit, no stranded rows, no
        # main-graph pollution. (Tier-3 core triples are deflected first, so
        # they never count as needing isolated.)
        if self._graph_isolated is None and any(
            self._needs_isolated(triple, floor) for triple in result.triples
        ):
            raise ValueError(
                "the 'isolated' graph instance is required (floor downgrade or "
                "salvage) but not configured; add storage.graph.instances.isolated"
            )
        for triple in result.triples:
            route = self._effective_route(triple, floor)
            if route is None:
                deflected += 1
                logger.warning(
                    "anti-backflow: tier-3 triple (%s, %s, %s) deflected from the main graph",
                    triple.subject,
                    triple.predicate,
                    triple.object,
                )
                continue
            if route is Route.CORE:
                target = self._graph_main
                core += 1
            else:
                # The atomic pre-pass above guarantees the downgrade / salvage
                # target exists; the else branch never strands.
                assert self._graph_isolated is not None
                target = self._graph_isolated
                isolated += 1
            if self._write_triple(target, snapshot, triple):
                created += 1
            else:
                reinforced += 1
            if triple.route is Route.SALVAGE:
                self._enqueue_salvage(snapshot, triple)
                salvage += 1
        return MergeSummary(
            profile_id=snapshot.profile_id,
            snapshot_id=snapshot.snapshot_id,
            turn_range=snapshot.turn_range,
            written=created + reinforced,
            created=created,
            reinforced=reinforced,
            core=core,
            isolated=isolated,
            salvage=salvage,
            deflected=deflected,
        )

    # ------------------------------------------------------------ idempotent write

    def _write_triple(self, graph: GraphStore, snapshot: Snapshot, triple: ReflectedTriple) -> bool:
        """Write one triple idempotently; returns True when a node was created,
        False when an existing identical triple was reinforced in place."""
        existing = self._find_same(graph, snapshot.profile_id, triple)
        if existing is not None:
            graph.upsert_node(self._reinforced(existing, snapshot, triple))
            return False
        graph.upsert_node(self._new_node(snapshot, triple))
        return True

    @staticmethod
    def _find_same(graph: GraphStore, profile_id: str, triple: ReflectedTriple) -> GraphNode | None:
        """Probe the port's same-(subject,predicate) reader and match on the
        object (casefold) — the (subject, predicate, object) dedup key."""
        wanted = triple.object.casefold()
        for node in graph.find_same_predicate(triple.subject, triple.predicate, profile_id):
            if str(node.props.get("object", "")).casefold() == wanted:
                return node
        return None

    def _new_node(self, snapshot: Snapshot, triple: ReflectedTriple) -> GraphNode:
        now = self._clock()
        node_type = _NODE_TYPE_BY_PREDICATE.get(triple.predicate.casefold(), NodeType.USER)
        return GraphNode(
            node_id=_content_id(snapshot.profile_id, triple),
            profile_id=snapshot.profile_id,
            node_type=node_type,
            entities=[triple.object] if triple.object else [],
            props=_payload(triple, node_type),
            confidence=triple.confidence,
            cognitive_tier=max((int(t) for t in triple.tiers), default=1),
            provenance=_build_provenance(snapshot, triple, now),
            created_at=now,
            updated_at=now,
            last_reinforced=now,
            reinforce_count=1,
        )

    def _reinforced(self, existing: GraphNode, snapshot: Snapshot, triple: ReflectedTriple) -> GraphNode:
        """Same node_id + version, re-upserted in place by the driver: only the
        reinforcement fields and the provenance history move, the version chain
        is untouched, the source chain is never rewritten."""
        now = self._clock()
        history = list(existing.provenance.history)
        history.append(
            ProvenanceEvent(
                at=now,
                action="reinforced",
                actor="dream-engine",
                detail={"snapshot_id": snapshot.snapshot_id, "chunks": list(triple.chunk_ids)},
            )
        )
        provenance = existing.provenance.model_copy(update={"history": history})
        return existing.model_copy(
            update={
                "confidence": min(0.95, max(existing.confidence, triple.confidence)),
                "last_reinforced": now,
                "reinforce_count": existing.reinforce_count + 1,
                "updated_at": now,
                "provenance": provenance,
            }
        )

    # ------------------------------------------------------------ salvage queue

    def _enqueue_salvage(self, snapshot: Snapshot, triple: ReflectedTriple) -> None:
        """Append the salvage entry to the audit log (the durable, restart-safe
        queue). Dedup is independent of the graph-node write, so a crash between
        node write and enqueue can never lose the entry; a re-run finds the
        existing audit row and skips."""
        if self._salvage_exists(snapshot.profile_id, triple):
            return
        self._meta.audit_append(
            AuditEntry(
                actor=snapshot.profile_id,
                action=_SALVAGE_ACTION,
                detail={
                    "subject": triple.subject,
                    "predicate": triple.predicate,
                    "object": triple.object,
                    "confidence": triple.confidence,
                    "chunk_ids": list(triple.chunk_ids),
                    "snapshot_id": snapshot.snapshot_id,
                    "turn_range": {"start": snapshot.turn_range.start, "end": snapshot.turn_range.end},
                },
                at=self._clock(),
            )
        )

    def _salvage_exists(self, profile_id: str, triple: ReflectedTriple) -> bool:
        page = Page(limit=200)
        while True:
            result = self._meta.audit_query(AuditFilter(actor=profile_id, action=_SALVAGE_ACTION), page)
            for entry in result.items:
                detail = entry.detail
                if (
                    str(detail.get("subject", "")).casefold() == triple.subject.casefold()
                    and str(detail.get("predicate", "")).casefold() == triple.predicate.casefold()
                    and str(detail.get("object", "")).casefold() == triple.object.casefold()
                ):
                    return True
            consumed = page.offset + len(result.items)
            if result.total <= consumed:
                return False
            page = Page(offset=consumed, limit=page.limit)


# ---------------------------------------------------------------- helpers


def _content_id(profile_id: str, triple: ReflectedTriple) -> str:
    """Deterministic content-hash node id: the same triple always maps to the
    same node, an extra idempotency insurance on top of find_same_predicate."""
    raw = "\x00".join((profile_id, triple.subject, triple.predicate, triple.object, triple.polarity))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _payload(triple: ReflectedTriple, node_type: NodeType) -> dict[str, Any]:
    base = {
        "subject": triple.subject,
        "predicate": triple.predicate,
        "object": triple.object,
        "polarity": triple.polarity,
    }
    if node_type is NodeType.PREFERENCE:
        return {
            **base,
            "domain": "",
            "statement": triple.object,
            "valence": 0.5,
            "prior_width": 0.3,
            "trait_anchor": "",
            "evidence_chain": list(triple.chunk_ids),
        }
    if node_type is NodeType.HABIT:
        return {**base, "statement": triple.object}
    if node_type is NodeType.DECISION:
        return {**base, "statement": triple.object}
    return {**base, "name": triple.subject}


def _build_provenance(snapshot: Snapshot, triple: ReflectedTriple, now: float) -> Provenance:
    """Provenance from the evidence chunk stamps: asserted_by is the union of
    the stamps' sources, source_ref pins snapshot_id + turn_range, confidence
    is carried across, and the history opens with the "created" event."""
    stamps = {c.chunk_id: c.to_stamp() for c in snapshot.chunks}
    asserted: list[str] = []
    session_ids: list[str] = []
    for chunk_id in triple.chunk_ids:
        stamp = stamps.get(chunk_id)
        if stamp is None:
            continue
        who = stamp.provenance.asserted_by.strip()
        if who and who not in asserted:
            asserted.append(who)
        sid = stamp.provenance.session_id
        if sid and sid not in session_ids:
            session_ids.append(sid)
    return Provenance(
        asserted_by=",".join(asserted) or "dream-engine",
        session_id=session_ids[0] if session_ids else None,
        source=(f"dream:{snapshot.snapshot_id}:turns:{snapshot.turn_range.start}-{snapshot.turn_range.end}"),
        confidence=triple.confidence,
        asserted_at=now,
        history=[
            ProvenanceEvent(
                at=now,
                action="created",
                actor="dream-engine",
                detail={"chunk_ids": list(triple.chunk_ids), "snapshot_id": snapshot.snapshot_id},
            )
        ],
    )
