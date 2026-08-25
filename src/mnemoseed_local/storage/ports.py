"""Storage port interfaces, capability flags, and the startup gate.

The storage layer is ports-and-adapters: four port interfaces (VectorStore,
GraphStore, MetaStore, Embedder) with a fixed method surface (prd-08 appendix B),
a driver registry behind each port (named multi-instance per layer), and a
capability gate that runs against the resolved config at daemon boot.

Backends are not interchangeable: every driver honestly declares its capability
set, and missing capabilities produce explicit degradations or a refused startup
— never silent failures.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, Field

from mnemoseed_local.schema.graph import Edge, GraphNode, NodeType
from mnemoseed_local.schema.stamp import ChunkStamp

# ---------------------------------------------------------------- data types


class RecallRule(BaseModel):
    """One standing constraint attached to a chunk (B2.7 Scheme 2-lite).

    ``value``'s shape is decided by ``kind``: ``exclude_entities`` is a list of
    entity names, ``entity_boost`` is ``[entity, coefficient]`` (the single
    ``value`` slot carries both as strings), ``focal_floor``/``budget_chars``/
    ``time_window`` are numeric. ``ttl_turns`` decays daemon-side per turn
    (>0); 0 means permanent. ``scope`` selects the aggregation surface.
    """

    kind: Literal["focal_floor", "budget_chars", "exclude_entities", "entity_boost", "time_window"]
    value: float | str | list[str]
    ttl_turns: int = 0
    scope: Literal["profile", "session", "global"] = "session"
    session_id: str | None = None


class RulesBudgetBlock(BaseModel):
    """The standing-constraint budget the daemon serves on /session/recent.

    The daemon is the only budget authority: the hook passes the block through
    verbatim (never interprets it). ``exclude_entities`` is the union of the
    matching rules; ``entity_boost`` takes the max coefficient per entity;
    ``budget_consumed`` is the daemon-side T2 char count at call time.
    """

    auto_recall_focal_floor: float
    auto_recall_budget_chars: int
    exclude_entities: list[str] = Field(default_factory=list)
    entity_boost: dict[str, float] = Field(default_factory=dict)
    time_window_turns: int | None = None
    budget_consumed: int = 0


@dataclass(frozen=True)
class SparseVector:
    """Structured sparse vector: parallel index/value pairs (never a dense array).

    The bge-m3 sparse output is ~250k dimensions with few non-zero entries.
    """

    indices: tuple[int, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.values):
            raise ValueError("sparse indices and values must be parallel")


@dataclass(frozen=True)
class Page:
    """Pagination cursor for filtered list reads."""

    offset: int = 0
    limit: int = 50


@dataclass(frozen=True)
class PageResult[T]:
    """One page of a filtered list read."""

    items: list[T]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True)
class WeightUpdate:
    """Bulk vector-weight update (decay / reinforcement write-back)."""

    chunk_id: str
    decay_weight: float | None = None
    last_reinforced: float | None = None
    reinforce_count: int | None = None


@dataclass(frozen=True)
class ChunkFilter:
    """Metadata filter for vector reads. profile_id is always explicit.

    ``entities_allow_missing`` is the recall-surface reading of the entity
    gate (D2): a chunk with NO stored entity cues is absence of evidence, not
    a contradiction, so it stays matchable. The default (strict exact-tag
    matching) governs listing/audit surfaces.
    """

    profile_id: str
    min_decay: float = 0.0
    # design/09 §3.5 route (b): when set BELOW ``min_decay``, explicit-pin
    # chunks floor at this value instead — the two-band rescue admission lives
    # in the storage prefilter itself, so sub-floor non-pin chunks never enter
    # the search window. Drivers without the pin flag treat it as absent.
    pin_min_decay: float | None = None
    ingested_after: float | None = None
    ingested_before: float | None = None
    session_id: str | None = None
    turn_start: int | None = None
    turn_end: int | None = None
    entities: tuple[str, ...] = ()
    consolidated: bool | None = None
    needs_reconcile: bool | None = None  # console reconcile-queue filter (PRD-07)
    entities_allow_missing: bool = False
    rules_not_null: bool = False  # B2.7: only chunks carrying rules (rules_json IS NOT NULL)


@dataclass(frozen=True)
class SearchHit:
    """One hybrid-search result (similarity feeds downstream scoring)."""

    chunk: ChunkStamp
    similarity: float


@dataclass(frozen=True)
class NodeFilter:
    """Filter for graph reads. profile_id is always explicit."""

    profile_id: str
    node_type: NodeType | None = None
    entities: tuple[str, ...] = ()
    min_decay: float = 0.0


class EdgeKind(StrEnum):
    """Bulk edge-list kind vocabulary (prd-08 appendix B.2 v1.1)."""

    RELATION = "relation"
    COOCCURRENCE = "cooccurrence"


@dataclass(frozen=True)
class EdgeEntry:
    """One bulk edge-list item (prd-08 appendix B.2 ``list_edges``).

    ``kind`` collapses the rel vocabulary to the two document values:
    ``co_occurred`` is COOCCURRENCE, every other rel is RELATION.
    """

    edge_id: str
    src: str
    dst: str
    kind: EdgeKind
    weight: float
    created_at: float


@dataclass(frozen=True)
class EdgeFilter:
    """Filter for bulk edge reads (prd-08 appendix B.2 v1.1).

    profile_id is always explicit (D5 isolation). ``node_types`` / ``tier``
    restrict the edge's endpoints: both ends must be current nodes
    (valid_to IS NULL) whose type / cognitive_tier matches, so hiding a node
    type also hides every edge touching it. The current-revision endpoint
    restriction is ALWAYS applied — even with no type/tier filter, an edge
    whose endpoint is tombstoned or superseded never leaks. The time window
    and min_weight apply to the edge row itself (created_at / weight).
    """

    profile_id: str
    node_types: tuple[NodeType, ...] = ()
    created_after: float | None = None
    created_before: float | None = None
    tier: int | None = None
    min_weight: float = 0.0


@dataclass(frozen=True)
class GraphWeightUpdate:
    """One entry in a batch decay recompute."""

    node_id: str
    decay_weight: float


@dataclass(frozen=True)
class TimelineEvent:
    """One entry in a version-chain timeline playback."""

    when: float
    version: int
    summary: str


@dataclass(frozen=True)
class TurnRange:
    """Structured turn boundary (snapshot scoping, safe purge, pool events)."""

    start: int
    end: int


@dataclass(frozen=True)
class PoolState:
    """Score-pool split balances: the pending gauge and the lifetime ledger.

    ``balance`` is only the current pending gauge (points that can still
    trigger a dream); ``filed_points_total`` accumulates every fired point and
    never triggers again.
    """

    balance: float = 0.0
    watermark: TurnRange | None = None
    filed_points_total: float = 0.0


@dataclass(frozen=True)
class StoredProfile:
    """Profile record (identity namespace for D5 isolation)."""

    profile_id: str
    display_name: str = ""
    created_at: float = 0.0
    archived: bool = False


@dataclass(frozen=True)
class StoredUser:
    """Owner/user account row (PRD-06 FR-6.1a)."""

    user_id: str
    username: str
    password_hash: str = ""
    role: str = "owner"
    created_at: float = 0.0


@dataclass(frozen=True)
class Token:
    """Issued credential for a profile."""

    token_id: str
    profile_id: str
    scopes: Sequence[str] = ()
    issued_at: float = 0.0
    expires_at: float | None = None
    revoked: bool = False
    # One-shot bearer secret materialized only at issue time (PRD-06 FR-6.1b):
    # the value rides back to the caller exactly once and is never persisted —
    # only its sha256 digest lands in ``tokens.token_hash``. Every other read
    # path returns this field empty.
    token_secret: str = ""


@dataclass(frozen=True)
class AuditEntry:
    """One append-only audit record."""

    actor: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)
    at: float = 0.0
    id: int | None = None


@dataclass(frozen=True)
class AuditFilter:
    """Filter for the counted, paginated audit read."""

    actor: str | None = None
    action: str | None = None
    since: float | None = None
    until: float | None = None


@dataclass(frozen=True)
class ConfigEntry:
    """One versioned config value."""

    key: str
    value: dict[str, Any]
    version: int
    updated_at: float


@dataclass(frozen=True)
class DreamRun:
    """Dream-engine run record (console panel + idempotent recovery)."""

    run_id: str = ""
    session_id: str | None = None
    turn_range: TurnRange | None = None
    model_id: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    tokens: int = 0
    cost: float = 0.0
    interrupted: bool = False
    dropped_count: int = 0


@dataclass(frozen=True)
class DreamRunFilter:
    """Filter for dream-run history reads."""

    session_id: str | None = None
    since: float | None = None
    until: float | None = None
    interrupted: bool | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    """Embedder output; sparse is absent when the driver lacks the capability."""

    dense: Sequence[float]
    sparse: SparseVector | None = None


@dataclass(frozen=True)
class DriverInfo:
    """Driver identity and static capability declaration (registry entry)."""

    name: str
    capabilities: frozenset[Capability]
    description: str = ""


# ---------------------------------------------------------------- capabilities


class Capability(StrEnum):
    """Minimal declared capability set (prd-08 FR-8.6, frozen exact list).

    The list is extensible; the validation mechanism is what is frozen.
    """

    VECTOR_HYBRID_SEARCH = "vector.hybrid_search"
    VECTOR_METADATA_FILTER = "vector.metadata_filter"
    VECTOR_SNAPSHOT = "vector.snapshot"
    GRAPH_TRAVERSE_2HOP = "graph.traverse_2hop"
    GRAPH_VERSION_CHAIN = "graph.version_chain"
    GRAPH_COOCCURRENCE_EDGES = "graph.cooccurrence_edges"
    GRAPH_EDGE_LIST = "graph.edge_list"
    META_TRANSACTION = "meta.transaction"
    META_CONCURRENT_READERS = "meta.concurrent_readers"
    EMBED_LOCAL_INFERENCE = "embed.local_inference"
    EMBED_BATCH = "embed.batch"
    EMBED_SPARSE_OUTPUT = "embed.sparse_output"

    @property
    def layer(self) -> str:
        """Owning layer ("vector" / "graph" / "meta" / "embed")."""
        return self.value.split(".", 1)[0]


class ValidationSeverity(StrEnum):
    """Startup-gate severity from the prd-08 degradation table."""

    HARD = "hard"  # refuse startup, list the missing capabilities
    DEGRADE = "degrade"  # pass startup with an explicit logged warning


@dataclass(frozen=True)
class CapabilityPolicy:
    """How a missing capability behaves at the startup gate (appendix C)."""

    capability: Capability
    severity: ValidationSeverity
    feature: str
    behavior: str


@dataclass(frozen=True)
class CapabilityIssue:
    """One concrete gate finding, bound to a resolved driver instance."""

    capability: Capability
    severity: ValidationSeverity
    layer: str
    instance: str
    driver: str
    feature: str
    behavior: str


@dataclass
class ValidationReport:
    """Startup-gate result for the resolved storage stack."""

    ok: bool
    hard_missing: list[CapabilityIssue] = field(default_factory=list)
    degradations: list[CapabilityIssue] = field(default_factory=list)

    @property
    def missing(self) -> list[CapabilityIssue]:
        """Every gated issue, hard first — the daemon logs over this list."""
        return [*self.hard_missing, *self.degradations]


DEGRADATION_TABLE: tuple[CapabilityPolicy, ...] = (
    # hard requirements — refuse startup (appendix C)
    CapabilityPolicy(
        capability=Capability.META_TRANSACTION,
        severity=ValidationSeverity.HARD,
        feature="score pool and watermark atomicity",
        behavior="atomic pool_add / advance_watermark are a hard dependency; startup refused",
    ),
    CapabilityPolicy(
        capability=Capability.GRAPH_VERSION_CHAIN,
        severity=ValidationSeverity.HARD,
        feature="reconcile and as_of bi-temporal queries",
        behavior="version-chain replay and as_of are a hard dependency; startup refused",
    ),
    CapabilityPolicy(
        capability=Capability.VECTOR_METADATA_FILTER,
        severity=ValidationSeverity.HARD,
        feature="profile isolation and freshness guard",
        behavior="profile_id isolation and ingested_at filtering are a hard dependency; startup refused",
    ),
    # degradations — pass startup with an explicit warning (appendix C)
    CapabilityPolicy(
        capability=Capability.EMBED_SPARSE_OUTPUT,
        severity=ValidationSeverity.DEGRADE,
        feature="hybrid retrieval sparse path",
        behavior="no sparse vectors produced; retrieval degrades to dense-only with a quality warning",
    ),
    CapabilityPolicy(
        capability=Capability.VECTOR_HYBRID_SEARCH,
        severity=ValidationSeverity.DEGRADE,
        feature="hybrid retrieval",
        behavior="hybrid retrieval degrades to dense-only, retrieval quality warning",
    ),
    CapabilityPolicy(
        capability=Capability.VECTOR_SNAPSHOT,
        severity=ValidationSeverity.DEGRADE,
        feature="dream-engine snapshot isolation",
        behavior="dream snapshot degrades to turn-range logical isolation, isolation strength warning",
    ),
    CapabilityPolicy(
        capability=Capability.GRAPH_COOCCURRENCE_EDGES,
        severity=ValidationSeverity.DEGRADE,
        feature="rerank co-occurrence term",
        behavior="rerank drops the epsilon co-occurrence term, retrieval quality warning",
    ),
    CapabilityPolicy(
        capability=Capability.GRAPH_EDGE_LIST,
        severity=ValidationSeverity.DEGRADE,
        feature="console Graph View bulk edge list",
        behavior=(
            "console graph degrades to per-node edge fetching via traverse(), "
            "console-graph performance warning"
        ),
    ),
    CapabilityPolicy(
        capability=Capability.META_CONCURRENT_READERS,
        severity=ValidationSeverity.DEGRADE,
        feature="console concurrent reads",
        behavior="console reads serialize on a single reader, concurrency performance warning",
    ),
    CapabilityPolicy(
        capability=Capability.EMBED_BATCH,
        severity=ValidationSeverity.DEGRADE,
        feature="batch vectorization",
        behavior="embedding runs one text at a time, throughput warning",
    ),
)


# ---------------------------------------------------------------- errors


class StorageError(Exception):
    """Base storage-layer error."""


class OwnerConflictError(StorageError):
    """A second owner account was attempted (FR-6.1a single-user hard limit).

    Raised by ``MetaStore.create_owner`` when the atomic check inside its
    transaction finds an owner already committed, or when a write constraint
    (users.username UNIQUE) rejects the insert -- never a bare IntegrityError.
    """


class UnknownDriverError(StorageError):
    """A driver name that no registered driver provides."""

    def __init__(self, layer: str, driver: str, available: Sequence[str]) -> None:
        if available:
            message = f"unknown {layer} driver {driver!r} (available: {', '.join(available)})"
        else:
            message = f"unknown {layer} driver {driver!r} (no {layer} drivers registered)"
        super().__init__(message)


class CapabilityStartupError(StorageError):
    """The startup gate refused to boot because hard capabilities are missing."""

    def __init__(self, missing: Sequence[CapabilityIssue]) -> None:
        entries = [
            f"  - {issue.layer}.{issue.instance} driver {issue.driver!r} lacks "
            f"{issue.capability.value} ({issue.feature}): {issue.behavior}"
            for issue in missing
        ]
        super().__init__("storage capability gate failed; missing capabilities:\n" + "\n".join(entries))


# ---------------------------------------------------------------- ports


class VectorStore(Protocol):
    """Hippocampus: verbatim shard storage plus metadata-filtered search."""

    info: ClassVar[DriverInfo]

    def capabilities(self) -> frozenset[Capability]:
        raise NotImplementedError

    def upsert_chunk(
        self,
        chunk: ChunkStamp,
        dense: Sequence[float],
        sparse: SparseVector | None = None,
    ) -> None:
        raise NotImplementedError

    def upsert_chunks(
        self,
        entries: Sequence[tuple[ChunkStamp, Sequence[float], SparseVector | None]],
    ) -> None:
        """Bulk upsert of many chunks in one commit (B6 drain batch write).

        The capture drain can flush an entire session's new chunks in a single
        store commit instead of one lock/commit round-trip per turn. Each entry
        is ``(chunk, dense, sparse)`` mirroring ``upsert_chunk``.
        """
        raise NotImplementedError

    def get_chunk(self, chunk_id: str) -> ChunkStamp | None:
        raise NotImplementedError

    def delete_chunk(self, chunk_id: str) -> None:
        raise NotImplementedError

    def search(
        self,
        dense: Sequence[float],
        sparse: SparseVector | None,
        filter: ChunkFilter,
        top_k: int,
    ) -> list[SearchHit]:
        raise NotImplementedError

    def near_duplicate(self, vector: Sequence[float], threshold: float, profile_id: str) -> list[ChunkStamp]:
        """Near-duplicate probe within one profile (D5 isolation is explicit).

        ``profile_id`` scopes the probe to a single profile — same isolation
        contract as ChunkFilter. The capture path (FR-1.8) must never scan
        another profile's chunks when deciding reinforce/reconcile/new.
        """
        raise NotImplementedError

    def near_duplicate_ranked(
        self, vector: Sequence[float], threshold: float, profile_id: str
    ) -> list[tuple[ChunkStamp, float]]:
        """Near-duplicate probe returning ``(chunk, similarity)`` pairs.

        Same isolation contract as ``near_duplicate``; the pairs are sorted by
        similarity desc then chunk_id asc. The capture drain path (B6) probes
        once at the conflict threshold and partitions the result into the
        strong (>= reinforce) and band (>= conflict) near-duplicate sets,
        halving the ANN searches per turn on the drain hot path.
        """
        raise NotImplementedError

    def snapshot_read(self, filter: ChunkFilter) -> list[ChunkStamp]:
        raise NotImplementedError

    def mark_consolidated(self, chunk_ids: Sequence[str]) -> None:
        raise NotImplementedError

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        raise NotImplementedError

    def update_weights(self, updates: Sequence[WeightUpdate]) -> None:
        raise NotImplementedError

    def update_chunk_state(
        self,
        chunk_ids: Sequence[str],
        hit_increment: int | None = None,
        needs_reconcile: bool | None = None,
    ) -> None:
        """Batch-write per-chunk usage counters and the reconcile flag.

        Port-level semantics so every vector driver behaves identically:

        - ``hit_increment`` is added verbatim to each chunk's ``hit_count``.
          Only a positive value also refreshes ``last_hit_at`` to the current
          time; zero touches nothing.
        - ``needs_reconcile`` sets (True) or clears (False) the flag.
        - Unknown ``chunk_id`` values are ignored silently (no error); callers
          may pass ids that were concurrently deleted (e.g. purged turns).
        - Passing neither argument updates nothing.

        Consumers: retrieval hit counting, and capture FR-1.8 where the
        0.85-0.9 near-duplicate band marks chunks needs_reconcile.
        """
        raise NotImplementedError

    def list_chunks(self, filter: ChunkFilter, page: Page) -> PageResult[ChunkStamp]:
        raise NotImplementedError


class GraphStore(Protocol):
    """Cortex: consolidated structured long-term memory with version chains."""

    info: ClassVar[DriverInfo]

    def capabilities(self) -> frozenset[Capability]:
        raise NotImplementedError

    def upsert_node(self, node: GraphNode) -> None:
        raise NotImplementedError

    def get_node(self, node_id: str) -> GraphNode | None:
        raise NotImplementedError

    def list_nodes(self, filter: NodeFilter, page: Page) -> PageResult[GraphNode]:
        raise NotImplementedError

    def add_edge(self, edge: Edge) -> None:
        raise NotImplementedError

    def bump_cooccurrence(self, node_a: str, node_b: str, profile_id: str) -> None:
        raise NotImplementedError

    def traverse(self, node_id: str, depth: int = 2, filter: NodeFilter | None = None) -> list[GraphNode]:
        raise NotImplementedError

    def find_same_predicate(self, subject: str, predicate: str, profile_id: str) -> list[GraphNode]:
        raise NotImplementedError

    def set_flags(self, nodes: Sequence[str], flags: Sequence[GraphFlag]) -> None:
        raise NotImplementedError

    def clear_flags(self, nodes: Sequence[str], flags: Sequence[GraphFlag]) -> None:
        raise NotImplementedError

    def invalidate(self, node_id: str, valid_to: float) -> None:
        raise NotImplementedError

    def tombstone(self, node_id: str, deleted_at: float | None = None) -> bool:
        """Delete a node for good (GDPR right-to-erasure, design/03 storage-layer erasure).

        The current revision is closed at ``deleted_at`` and a ``deleted``
        provenance event is appended to that revision's version-chain payload;
        nothing is physically removed, so the chain survives for audit and
        as_of historical replay while every current-revision read (get / list /
        traverse / future as_of) stops seeing the node. Returns False when the
        node has no current revision to tombstone.
        """
        raise NotImplementedError

    def append_version(self, node: GraphNode, *, invalidate_at: float | None = None) -> None:
        raise NotImplementedError

    def versions(self, node_id: str) -> list[GraphNode]:
        raise NotImplementedError

    def diff(self, version_a: str, version_b: str) -> dict[str, Any]:
        raise NotImplementedError

    def timeline(self, node_id: str) -> list[TimelineEvent]:
        raise NotImplementedError

    def as_of(self, timestamp: float, filter: NodeFilter) -> list[GraphNode]:
        raise NotImplementedError

    def batch_update_weights(self, updates: Sequence[GraphWeightUpdate]) -> None:
        raise NotImplementedError

    def query_intentions(self, status: IntentionStatus, due_before: float) -> list[GraphNode]:
        raise NotImplementedError

    def list_edges(self, filter: EdgeFilter, page: Page) -> PageResult[EdgeEntry]:
        """Bulk edge listing backing the console Graph View (v1.1 amendment).

        Filter fields: profile_id / endpoint node types / created time window /
        cognitive tier / min edge weight; paginated with a stable order
        (created_at desc, edge id asc). Each item returns edge id, endpoints,
        kind (relation | cooccurrence), weight, timestamps.
        """
        raise NotImplementedError


class MetaStore(Protocol):
    """Metadata: profiles, tokens, score pool, watermarks, config, audit."""

    info: ClassVar[DriverInfo]

    def capabilities(self) -> frozenset[Capability]:
        raise NotImplementedError

    def pool_add(self, profile_id: str, points: float, turn_range: TurnRange) -> None:
        raise NotImplementedError

    def pool_state(self, profile_id: str) -> PoolState:
        raise NotImplementedError

    def pool_credit(self, profile_id: str, balance: float, turn_range: TurnRange) -> None:
        """Set a profile's persisted pool row absolutely (balance + watermark).

        Used by the capture ScorePool after every state change, mirroring the
        in-process ledger into the per-profile table without touching the
        fired-event window.
        """
        raise NotImplementedError

    def pool_drain(self, profile_id: str, turn_range: TurnRange) -> float:
        """Atomically file a fired dream's points out of the pending gauge.

        One transaction moves the whole persisted balance into the lifetime
        ``filed_points_total`` ledger and resets the gauge to 0, so the same
        points can never trigger twice. The watermark columns are untouched.
        Returns the filed amount.
        """
        raise NotImplementedError

    def pool_states(self) -> dict[str, PoolState]:
        raise NotImplementedError

    def advance_watermark(self, profile_id: str, turn_range: TurnRange) -> None:
        raise NotImplementedError

    def upsert_profile(self, profile: StoredProfile) -> None:
        raise NotImplementedError

    def get_profile(self, profile_id: str) -> StoredProfile | None:
        raise NotImplementedError

    def delete_profile(self, profile_id: str) -> None:
        raise NotImplementedError

    def list_profiles(self) -> list[StoredProfile]:
        raise NotImplementedError

    def archive_profile(self, profile_id: str, archived: bool) -> None:
        """Set the profile's archived flag (console FR-7.3 profile archive).

        Rename never touches the flag (upsert updates display_name only); the
        flag is an explicit archive/unarchive action.
        """
        raise NotImplementedError

    def issue_token(
        self,
        profile_id: str,
        scopes: Sequence[str],
        expires_at: float | None = None,
    ) -> Token:
        """Issue a fresh profile token; ``token_secret`` is set only here."""
        raise NotImplementedError

    def revoke_token(self, token_id: str) -> None:
        raise NotImplementedError

    def authenticate_token(self, secret: str) -> Token | None:
        """Resolve a bearer secret to its live token (revoked/expired => None).

        The secret is hashed and matched against ``tokens.token_hash``; the
        returned Token never carries ``token_secret``.
        """
        raise NotImplementedError

    # ------------------------------------------------------------ users (FR-6.1a)

    def create_user(self, user: StoredUser) -> None:
        raise NotImplementedError

    def create_owner(self, owner: StoredUser, profile: StoredProfile, audit: AuditEntry) -> None:
        """Create the single owner + default profile + audit in ONE transaction.

        FR-6.1a exact-once is enforced inside the transaction, never as a
        check-then-insert: concurrent setups serialize on the write lock
        (sqlite BEGIN IMMEDIATE; postgres advisory xact lock), re-read the owner
        count after the wait, and raise ``OwnerConflictError`` for every loser.
        The users ``username`` UNIQUE constraint is a final backstop -- any
        IntegrityError on the way in is translated to the same typed conflict,
        never a bare IntegrityError. All three writes commit together or none do.
        """
        raise NotImplementedError

    def get_user_by_username(self, username: str) -> StoredUser | None:
        raise NotImplementedError

    def count_users(self) -> int:
        raise NotImplementedError

    def list_users(self) -> list[StoredUser]:
        raise NotImplementedError

    def update_user_password(self, user_id: str, password_hash: str) -> None:
        raise NotImplementedError

    def get_config(self, key: str, version: int | None = None) -> ConfigEntry | None:
        raise NotImplementedError

    def set_config(self, key: str, value: dict[str, Any]) -> int:
        raise NotImplementedError

    def rollback_config(self, key: str, version: int) -> None:
        raise NotImplementedError

    def audit_append(self, entry: AuditEntry) -> None:
        raise NotImplementedError

    def audit_query(self, filter: AuditFilter, page: Page) -> PageResult[AuditEntry]:
        raise NotImplementedError

    def record_dream_run(self, run: DreamRun) -> str:
        raise NotImplementedError

    def list_dream_runs(self, filter: DreamRunFilter, page: Page) -> PageResult[DreamRun]:
        raise NotImplementedError

    def update_dream_run_model(self, run_id: str, model_id: str) -> None:
        """F2: record the model pinned at reflect run start on an existing run.

        A dream run is registered at snapshot capture, before the route is
        resolved; the reflect boundary pins the model at run start and writes
        it back here. Unknown run ids are a silent no-op (the run is always
        registered first).
        """
        raise NotImplementedError

    def finish_dream_run(
        self,
        run_id: str,
        *,
        finished_at: float,
        tokens: int,
        cost: float,
        dropped_count: int,
    ) -> None:
        """The dream log surface: complete a run row with finish time, metered
        tokens and cost at merge commit. Unknown run ids are a silent no-op
        (same contract as update_dream_run_model)."""
        raise NotImplementedError

    def add_token_usage(self, profile_id: str, year_month: str, tokens: int) -> None:
        """FR-2.5b: atomically increment a profile's monthly dream-token counter."""
        raise NotImplementedError

    def token_usage(self, profile_id: str, year_month: str) -> int:
        """FR-2.5b: read a profile's monthly dream-token counter (0 when none)."""
        raise NotImplementedError

    def schema_version(self) -> int:
        raise NotImplementedError

    def migrate(self, target: int | None = None) -> int:
        raise NotImplementedError


class Embedder(Protocol):
    """Vectorization provider."""

    info: ClassVar[DriverInfo]
    dimension: int

    def capabilities(self) -> frozenset[Capability]:
        raise NotImplementedError

    def embed(self, text: str) -> EmbeddingResult:
        raise NotImplementedError

    def embed_batch(self, texts: Sequence[str]) -> list[EmbeddingResult]:
        raise NotImplementedError


class GraphFlag(StrEnum):
    """Updatable graph workflow flags (prd-08 appendix A.2)."""

    NEEDS_RECONCILE = "needs_reconcile"
    PENDING_CONSOLIDATION = "pending_consolidation"
    CONFLICT_GROUP = "conflict_group"
    PERIPHERAL_GAPS = "peripheral_gaps"


class IntentionStatus(StrEnum):
    """Prospective-memory node lifecycle."""

    PENDING = "pending"
    FIRED = "fired"
    CANCELLED = "cancelled"


# Any resolved driver instance is one of the four ports.
Store = VectorStore | GraphStore | MetaStore | Embedder


# ---------------------------------------------------------------- validation


def validate_capabilities(instances: Mapping[str, Mapping[str, Store]]) -> ValidationReport:
    """Run the appendix C gate over resolved driver instances.

    Only capabilities present in DEGRADATION_TABLE are gated; the remaining
    declared flags (graph.traverse_2hop, embed.local_inference) are not part of
    the startup criteria. HARD findings refuse startup, DEGRADE findings log a
    warning. No path is silent.
    """
    issues: list[CapabilityIssue] = []
    for layer, named in instances.items():
        for instance_name, store in named.items():
            declared = store.capabilities()
            for policy in DEGRADATION_TABLE:
                if policy.capability.layer != layer:
                    continue
                if policy.capability not in declared:
                    issues.append(
                        CapabilityIssue(
                            capability=policy.capability,
                            severity=policy.severity,
                            layer=layer,
                            instance=instance_name,
                            driver=store.info.name,
                            feature=policy.feature,
                            behavior=policy.behavior,
                        )
                    )
    hard = [i for i in issues if i.severity is ValidationSeverity.HARD]
    degradable = [i for i in issues if i.severity is ValidationSeverity.DEGRADE]
    return ValidationReport(ok=not hard, hard_missing=hard, degradations=degradable)
