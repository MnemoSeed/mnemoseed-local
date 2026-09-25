"""Dream engine: pool-event trigger, snapshot, reflect, split-write (PRD-02).

T1 ships the trigger: the per-profile lifecycle state machine that consumes
ScorePool events and requests read-only snapshots through the Snapshotter
seam. T2 ships the real snapshotter: frozen capture, atomic disk persistence,
MetaStore registration, and crash-safe idempotent recovery at the phase
boundary. T3 ships the reflection orchestrator: the de-biasing prompt template
and the deterministic offline ReflectLLM seam. T4 ships the split writer: the
Merger routing triples to the graph double-instance with idempotent write-back
and the salvage review queue, plus the DreamPipeline that drives a dream across
the reflect -> merge -> commit chain off the ingest hot path. B1 ships the
ensemble verify phase: model B judges model A's folded core triples (reject ->
reroute to isolated, never delete), falling back to A's original result with
an audit record on any verifier failure.
"""

from __future__ import annotations

from mnemoseed_local.dream.adjudicate import (  # noqa: I001 - module surface
    PAIR_ADJUDICATION_PROMPT_VERSION,
    PAIR_ADJUDICATION_SYSTEM_PROMPT,
    PAIR_SEAT_ROUTES,
    AdjudicationResult,
    Disposition,
    EndpointFate,
    EndpointObservation,
    MalformedAdjudicationInputError,
    PairAdjudicationInput,
    PairNomination,
    PairSeatDecision,
    Quality,
    ReasonCode,
    SeatActivation,
    SeatOutcome,
    SeatStatus,
    Verdict,
    activation_for,
    adjudicate,
    parse_pair_seat_output,
    render_pair_adjudication_prompt,
)
from mnemoseed_local.dream.combine import (
    COMBINE_PROMPT_VERSION,
    SINGLE_SIDE_SALVAGE_FLOOR,
    combine_results,
)
from mnemoseed_local.dream.consumer import (  # noqa: I001 - module surface
    ConsumerPolicy,
    ReconciliationConsumer,
    consumer_enabled,
)
from mnemoseed_local.dream.delta import (
    DEFAULT_DELTA_BUDGET_TOKENS,
    DELTA_BUDGET_CEILING_TOKENS,
    DELTA_BUDGET_FLOOR_TOKENS,
    DeltaPacker,
    DeltaReport,
    DeltaRequest,
    GraphDigest,
    NullGraphDigest,
    estimate_tokens,
    resolve_delta_budget,
)
from mnemoseed_local.dream.ledger import LedgerStatus, TokenLedger
from mnemoseed_local.dream.merge import (
    MergeOutcome,
    Merger,
    MergeSummary,
)
from mnemoseed_local.dream.nominate import (  # noqa: I001 - module surface
    READ_CONFLICT_KIND,
    MaterializeReport,
    after_commit,
    materialize_nominations,
    mint_generation,
    run_committed_nominations,
)
from mnemoseed_local.dream.pipeline import DreamPipeline
from mnemoseed_local.dream.prompts import (
    PROMPT_VERSION,
    ChunkBlock,
    ReflectPrompt,
    build_cache_prefix,
    build_reflect_prompt,
    ordered_chunks,
    render_chunk_block,
    render_chunk_blocks,
)
from mnemoseed_local.dream.readiness import DreamRouteReadiness
from mnemoseed_local.dream.reflect import (
    STRIP_TOKENS,
    ReflectedTriple,
    ReflectionResult,
    ReflectLLM,
    ReflectOrchestrator,
    ReflectOutcome,
    Route,
    StubReflectLLM,
    result_from_payload,
)
from mnemoseed_local.dream.repair import repair_reconciliation_audit
from mnemoseed_local.dream.snapshot import (
    FileSnapshotter,
    Snapshot,
    SnapshotChunk,
    SnapshotPhase,
    SnapshotResult,
    load_snapshot_file,
    recover_snapshots,
    resume_boundary,
    write_snapshot_file,
)
from mnemoseed_local.dream.trigger import (
    DREAM_RETRY_BASE_S,
    DREAM_RETRY_CAP_S,
    DREAM_RETRY_MAX,
    DREAM_RETRY_MULT,
    SCHEDULER_INTERVAL_S,
    DreamEligibility,
    DreamScheduler,
    DreamState,
    DreamTrigger,
    NullSnapshotter,
    Snapshotter,
    TriggerStatus,
)
from mnemoseed_local.dream.verify import (
    VERIFY_PROMPT_VERSION,
    StubVerifyLLM,
    TripleVerifier,
)

__all__ = [
    "PAIR_ADJUDICATION_PROMPT_VERSION",
    "PAIR_ADJUDICATION_SYSTEM_PROMPT",
    "PAIR_SEAT_ROUTES",
    "COMBINE_PROMPT_VERSION",
    "DELTA_BUDGET_CEILING_TOKENS",
    "DELTA_BUDGET_FLOOR_TOKENS",
    "DEFAULT_DELTA_BUDGET_TOKENS",
    "DREAM_RETRY_BASE_S",
    "DREAM_RETRY_CAP_S",
    "DREAM_RETRY_MAX",
    "DREAM_RETRY_MULT",
    "PROMPT_VERSION",
    "SINGLE_SIDE_SALVAGE_FLOOR",
    "ChunkBlock",
    "ConsumerPolicy",
    "AdjudicationResult",
    "Disposition",
    "DeltaPacker",
    "DeltaReport",
    "DeltaRequest",
    "DreamEligibility",
    "DreamPipeline",
    "DreamRouteReadiness",
    "DreamScheduler",
    "DreamState",
    "DreamTrigger",
    "EndpointFate",
    "EndpointObservation",
    "FileSnapshotter",
    "GraphDigest",
    "LedgerStatus",
    "MergeOutcome",
    "MergeSummary",
    "Merger",
    "MalformedAdjudicationInputError",
    "NullGraphDigest",
    "NullSnapshotter",
    "PairAdjudicationInput",
    "PairNomination",
    "PairSeatDecision",
    "Quality",
    "MaterializeReport",
    "READ_CONFLICT_KIND",
    "after_commit",
    "materialize_nominations",
    "mint_generation",
    "run_committed_nominations",
    "ReflectLLM",
    "ReflectOrchestrator",
    "ReflectOutcome",
    "ReflectPrompt",
    "ReflectedTriple",
    "ReflectionResult",
    "ReconciliationConsumer",
    "ReasonCode",
    "Route",
    "SCHEDULER_INTERVAL_S",
    "STRIP_TOKENS",
    "Snapshot",
    "SnapshotChunk",
    "SnapshotPhase",
    "SnapshotResult",
    "Snapshotter",
    "SeatActivation",
    "SeatOutcome",
    "SeatStatus",
    "StubReflectLLM",
    "StubVerifyLLM",
    "TokenLedger",
    "TriggerStatus",
    "TripleVerifier",
    "VERIFY_PROMPT_VERSION",
    "Verdict",
    "activation_for",
    "adjudicate",
    "build_cache_prefix",
    "build_reflect_prompt",
    "combine_results",
    "consumer_enabled",
    "estimate_tokens",
    "load_snapshot_file",
    "ordered_chunks",
    "parse_pair_seat_output",
    "recover_snapshots",
    "render_chunk_block",
    "render_chunk_blocks",
    "render_pair_adjudication_prompt",
    "repair_reconciliation_audit",
    "resolve_delta_budget",
    "resume_boundary",
    "result_from_payload",
    "write_snapshot_file",
]
