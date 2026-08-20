"""Scratch eval rig (B3 T2): the production dream chain over disposable stores.

One rig = one matrix cell. The rig assembles the SAME wiring the daemon uses
(``daemon.app._build_capture``: FileSnapshotter -> DeltaPacker -> ReflectOrchestrator
-> TripleVerifier -> Merger -> DreamPipeline, graph main+isolated double
instance, one shared ledger, one live Config) over a storage stack rooted
ENTIRELY under the caller-given ``RigPaths.root`` — sqlite graph meta, lancedb
vector, synthetic embedder, a config.toml the rig itself wrote there.

Red lines (pinned by tests):

- a run NEVER touches the live config/data dirs: every CONFIG_DIR-dependent
  default is overridden with a path under root (journal dir, config file), the
  stores live under root, and ollama base_urls arrive via the cell's explicit
  route params;
- the score pool's timing is not the evaluation target: turns are ingested
  through the real WritingPipeline, drained, snapshotted over their full turn
  range, and driven through exactly one synchronous ``DreamPipeline.run``;
- turn->chunk attribution is exact: each corpus item is ingested as its own
  mini-session (one chunk per item), so metrics can tell a fact chunk from a
  noise chunk by session id, never by text guessing.

Unit tests run the rig over the deterministic stub seats; a live run only
swaps the cell's routes (``stub``/``stub_verifier`` -> ``ollama``), the rig
code path is identical.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from mnemoseed_local.capture import ScoringPipeline, TurnScorer, TurnSegmenter, WritingPipeline
from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
from mnemoseed_local.config import RoleLLMConfig, load_config
from mnemoseed_local.dream import (
    DeltaPacker,
    DreamPipeline,
    DreamTrigger,
    FileSnapshotter,
    Merger,
    ReflectOrchestrator,
    ReflectOutcome,
    TokenLedger,
    TripleVerifier,
    result_from_payload,
)
from mnemoseed_local.dream.merge import MergeOutcome, MergeSummary
from mnemoseed_local.dream.reflect import ChatLLM, ReflectionResult
from mnemoseed_local.dream.snapshot import Snapshot, load_snapshot_file
from mnemoseed_local.eval.canary import CanarySession, CanaryTurn
from mnemoseed_local.llm import RoleRouter
from mnemoseed_local.llm.types import ChatResult
from mnemoseed_local.schema.turn import HostId, IngestEvent, IngestEventType, MessageContent
from mnemoseed_local.storage.factory import Stores, build_stores
from mnemoseed_local.storage.ports import (
    AuditEntry,
    AuditFilter,
    ChunkFilter,
    GraphStore,
    NodeFilter,
    Page,
    StoredProfile,
    TurnRange,
)

logger = logging.getLogger("mnemoseed_local.eval.harness")

_EMBED_DIMENSIONS = 64


# ---------------------------------------------------------------- cell + routes


@dataclass(frozen=True)
class EvalRoute:
    """One matrix seat's LLM route (same shape as RoleLLMConfig)."""

    driver: str
    model: str
    params: tuple[tuple[str, Any], ...] = ()  # sorted pairs; frozen-hashable


def route_params(route: EvalRoute) -> dict[str, Any]:
    """Materialize the route's params as a plain dict for RoleLLMConfig."""
    return dict(route.params)


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("_", text).strip("_").lower() or "x"


@dataclass(frozen=True)
class EvalCell:
    """One matrix cell: reflect seat × ensemble mode × verifier seat × tier params."""

    reflect: EvalRoute
    ensemble: str = "off"  # off | verify (vote is a B5 mechanism, see PRD-B3)
    verifier: EvalRoute | None = None
    delta_budget_tokens: int = 32000
    core_confidence_floor: float = 0.0

    @property
    def cell_id(self) -> str:
        """Deterministic slug distinguishing every cell parameter that matters."""
        parts = [_slug(self.reflect.model), self.ensemble]
        if self.verifier is not None:
            parts.append(_slug(self.verifier.model))
        parts.append(f"d{self.delta_budget_tokens}")
        parts.append(f"f{self.core_confidence_floor:g}")
        return "+".join(parts)


# ---------------------------------------------------------------- collapse guard (B4a)
#
# RCA (PRD-B3 "B4 前置排查"): the matrix reflect seat (think=False, no
# seed/temperature) makes qwen3.5:9b emit a literal ``[]`` ~67% of the time
# (ollama eval_count=2) — accepted as a "legit empty extraction" it hardens
# into a deterministic-looking zero-recall cell. The classifier below pins the
# exact fingerprint; the reflect retry loop turns it into a typed retry, and
# the per-seat fixed seed (matrix.py) is the actual recovery.

#: The verbatim collapse output (RCA fingerprint: content='[]').
_COLLAPSE_TEXT = "[]"

#: The completion-count boundary of the collapse fingerprint (ollama
#: eval_count=2 for a bare ``[]``; any whitespace/formatting pushes it above).
#: A well-formed empty extraction under normal token counts never matches.
COLLAPSE_MAX_COMPLETION_TOKENS = 2


class ReflectCollapseError(Exception):
    """The reflect seat returned the sampling-collapse fingerprint: a verbatim
    empty JSON array with a tiny completion count. The reflect retry loop
    treats it as any other typed failure; the pinned seed is the recovery."""


def is_reflect_collapse(
    response: str | ChatResult, *, max_completion_tokens: int = COLLAPSE_MAX_COMPLETION_TOKENS
) -> bool:
    """True only for the collapse fingerprint: a verbatim ``[]`` text with a
    tiny provider-reported completion count. Plain-text responses (no usage
    fingerprint) and well-formed empty arrays under normal token counts are
    never collapses."""
    if not isinstance(response, ChatResult):
        return False
    usage = response.usage
    if usage is None or usage.completion_tokens is None:
        return False
    return response.text.strip() == _COLLAPSE_TEXT and usage.completion_tokens <= max_completion_tokens


class _CollapseGuard:
    """Wraps a resolved reflect seat: raises ``ReflectCollapseError`` on the
    collapse fingerprint (verbatim ``[]`` with completion <= 2) so the reflect
    retry loop engages, and records per-run collapse attempts / recovery for
    the report surface. The fingerprint is classified regardless of whether
    the empty array was legitimate; only a well-formed empty extraction under
    normal token counts passes through."""

    def __init__(self, llm: ChatLLM, *, max_completion_tokens: int = COLLAPSE_MAX_COMPLETION_TOKENS) -> None:
        self._llm = llm
        self._max_completion_tokens = max_completion_tokens
        self.run_collapse_attempts: int = 0
        self.run_recovered: bool = False

    def reset_run(self) -> None:
        self.run_collapse_attempts = 0
        self.run_recovered = False

    def chat(self, *, system: str, user: str) -> str | ChatResult:
        response = self._llm.chat(system=system, user=user)
        if isinstance(response, ChatResult) and is_reflect_collapse(
            response, max_completion_tokens=self._max_completion_tokens
        ):
            self.run_collapse_attempts += 1
            usage = response.usage
            assert usage is not None and usage.completion_tokens is not None
            raise ReflectCollapseError(
                f"reflect collapse fingerprint: literal {_COLLAPSE_TEXT!r} "
                f"with completion_tokens={usage.completion_tokens}"
            )
        if self.run_collapse_attempts > 0:
            self.run_recovered = True
        return response


# ---------------------------------------------------------------- run read-back


@dataclass(frozen=True)
class RecordedChunk:
    """One verbatim chunk read back from the rig's vector store."""

    chunk_id: str
    session_id: str | None
    turn_start: int | None
    turn_end: int | None
    text: str
    consolidated: bool


@dataclass(frozen=True)
class RecordedNode:
    """One graph node read back after merge, flattened to the eval surface."""

    node_id: str
    graph: str  # "main" | "isolated"
    subject: str
    predicate: str
    object: str
    polarity: str
    confidence: float
    chunk_ids: tuple[str, ...]


@dataclass(frozen=True)
class CellRun:
    """Everything one cell's dream run produced, read back from the stores."""

    cell_id: str
    profile_id: str
    merge_committed: bool
    merge_summary: MergeSummary | None
    reflect_outcome: ReflectOutcome | None
    reflect_result: ReflectionResult | None  # the journaled (post-verify) result
    core_nodes: tuple[RecordedNode, ...]
    isolated_nodes: tuple[RecordedNode, ...]
    chunks: tuple[RecordedChunk, ...]
    audit: tuple[AuditEntry, ...]
    token_usage: int  # tokens metered during THIS run (monthly-counter delta)
    duration_s: float
    # turn index (material order) -> the mini-session id that carried it;
    # chunk.session_id reversed through this tuple gives exact attribution.
    turn_sessions: tuple[str, ...] = ()
    # B4a collapse surface: how many reflect attempts hit the collapse
    # fingerprint (max_retries=2 -> at most 3), and whether one recovered.
    reflect_collapse_attempts: int = 0
    reflect_recovered: bool = False
    # the reflect seat's pinned sampling seed (None when the route carries no
    # seed, e.g. stub seats or --no-seat-seed runs).
    seat_seed: int | None = None


# ---------------------------------------------------------------- rig paths


@dataclass(frozen=True)
class RigPaths:
    """Every file the rig may ever create lives under ``root``."""

    root: Path
    config_name: str = "config.toml"
    journal_name: str = "dreams"
    stores_name: str = "stores"

    @property
    def config_path(self) -> Path:
        return self.root / self.config_name

    @property
    def journal_dir(self) -> Path:
        return self.root / self.journal_name

    @property
    def stores_dir(self) -> Path:
        return self.root / self.stores_name


# ---------------------------------------------------------------- recording seams


class _RecordingMerger(Merger):
    """Merger that keeps its last typed outcome for the read-back surface."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.last_outcome: MergeOutcome | None = None

    def merge(self, snapshot: Snapshot, result: ReflectionResult) -> MergeOutcome:
        self.last_outcome = super().merge(snapshot, result)
        return self.last_outcome


class _RecordingReflector(ReflectOrchestrator):
    """Reflector that keeps its last typed outcome (cost telemetry) plus the
    collapse-guard counters for the current run (B4a report surface)."""

    def __init__(self, *, collapse_guard: _CollapseGuard | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.last_outcome: ReflectOutcome | None = None
        self.last_collapse_attempts: int = 0
        self.last_reflect_recovered: bool = False
        self._collapse_guard = collapse_guard

    def reflect(self, snapshot: Snapshot) -> ReflectOutcome:
        if self._collapse_guard is not None:
            self._collapse_guard.reset_run()
        self.last_outcome = super().reflect(snapshot)
        if self._collapse_guard is not None:
            self.last_collapse_attempts = self._collapse_guard.run_collapse_attempts
            self.last_reflect_recovered = self._collapse_guard.run_recovered
        return self.last_outcome


# ---------------------------------------------------------------- the rig


_CONFIG_TEMPLATE = """\
preset = "embedded"
[storage.vector]
uri = "{vector_uri}"
dimensions = {dimensions}
[storage.graph]
path = "{graph_main}"
[storage.graph.instances.isolated]
driver = "sqlite_graph"
path = "{graph_isolated}"
[storage.meta]
path = "{meta}"
[storage.embed]
driver = "synthetic"
dimension = {dimensions}
"""


class EvalRig:
    """One matrix cell's disposable dream rig.

    Constructor assembles everything (store build + funnel wiring); each
    ``run_canary`` / ``run_turns`` call then feeds one material through the
    full dream chain and reads the stores back. Reuse across materials is the
    intended shape (matrix cells share their rig), so ``close()`` is explicit.
    """

    def __init__(
        self,
        paths: RigPaths,
        cell: EvalCell,
        *,
        profile_id: str = "canary",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.paths = paths
        self.cell = cell
        self.profile_id = profile_id
        # idempotent over the artifacts this rig owns: a reused root must not
        # re-ingest onto a prior run's stores/journal under the shared profile.
        for p in (paths.stores_dir, paths.journal_dir):
            if p.exists():
                shutil.rmtree(p)
        paths.config_path.unlink(missing_ok=True)
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.stores_dir.mkdir(parents=True, exist_ok=True)
        paths.journal_dir.mkdir(parents=True, exist_ok=True)

        paths.config_path.write_text(
            _CONFIG_TEMPLATE.format(
                vector_uri=(paths.stores_dir / "chunks.lance").as_posix(),
                graph_main=(paths.stores_dir / "cortex.db").as_posix(),
                graph_isolated=(paths.stores_dir / "isolated.db").as_posix(),
                meta=(paths.stores_dir / "meta.db").as_posix(),
                dimensions=_EMBED_DIMENSIONS,
            ),
            encoding="utf-8",
        )
        config = load_config(paths.config_path)
        # In-memory cell overlay (never written back): the two dream roles ride
        # the cell's routes; dream flags ride the cell's tier params.
        routes: dict[str, RoleLLMConfig] = {
            "dream": RoleLLMConfig(
                role="dream",
                driver=cell.reflect.driver,
                model=cell.reflect.model,
                params=route_params(cell.reflect),
            )
        }
        verifier_route = cell.verifier or cell.reflect
        routes["dream_verifier"] = RoleLLMConfig(
            role="dream_verifier",
            driver=verifier_route.driver,
            model=verifier_route.model,
            params=route_params(verifier_route),
        )
        config.llm = routes
        config.dream = replace(
            config.dream,
            ensemble=cell.ensemble,
            core_confidence_floor=cell.core_confidence_floor,
        )
        self._config = config

        self._stores = build_stores(config)
        self._stores.meta.upsert_profile(StoredProfile(profile_id=profile_id, created_at=time.time()))
        isolated = self._stores.instances.get("graph", {}).get("isolated")
        if isolated is None:  # pragma: no cover - config template always declares it
            raise RuntimeError("rig config lost the isolated graph instance")

        snapshotter = FileSnapshotter(
            store=self._stores.vector, meta=self._stores.meta, directory=paths.journal_dir
        )
        trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=False, purger=snapshotter.purge_snapshot)
        router = RoleRouter(routes=config.llm, audit=self._stores.meta, generation=lambda role: 0)
        ledger = TokenLedger(meta=self._stores.meta)
        verifier = TripleVerifier(
            llm=router.resolve("dream_verifier"),
            config=config,
            meta=self._stores.meta,
            ledger=ledger,
        )
        collapse_guard = _CollapseGuard(router.resolve("dream"))
        reflector = _RecordingReflector(
            llm=collapse_guard,
            collapse_guard=collapse_guard,
            # B4a: cap the reflect retry lane at 3 total attempts (default 3
            # retries = 4 attempts is wasteful against a deterministic collapse).
            max_retries=2,
            sleep=sleep,
            directory=snapshotter.directory,
            packer=DeltaPacker(budget_tokens=cell.delta_budget_tokens),
            on_done=trigger.on_reflect_complete,
            on_run_started=lambda run_id, model: self._stores.meta.update_dream_run_model(run_id, model),
            ledger=ledger,
            verifier=verifier,
        )
        self._collapse_guard = collapse_guard
        merger = _RecordingMerger(
            graph_main=self._stores.graph,
            graph_isolated=cast(GraphStore, isolated),
            meta=self._stores.meta,
            on_committed=trigger.on_merge_committed,
            config=config,
        )
        pipeline = DreamPipeline(trigger=trigger, snapshotter=snapshotter, reflector=reflector, merger=merger)
        snapshotter.on_ready = pipeline.on_snapshot_ready
        self._pipeline = pipeline

        scoring = ScoringPipeline(
            scorer=TurnScorer(embedder=self._stores.embed),
        )
        self._writing = WritingPipeline(store=self._stores.vector, inner=scoring, embedder=self._stores.embed)
        self._segmenter = TurnSegmenter(self._writing)
        self._snapshotter = snapshotter
        self._trigger = trigger
        self._reflector = reflector
        self._merger = merger
        self._ledger = ledger

    # ------------------------------------------------------------ running materials

    def run_canary(self, session: CanarySession) -> CellRun:
        """Feed one canary session through the dream chain (one mini-session
        per corpus turn, so chunk attribution stays exact)."""
        return self.run_turns(session.turns, session_id=session.session_id, profile_id=self.profile_id)

    def run_turns(
        self,
        turns: tuple[CanaryTurn, ...] | list[CanaryTurn],
        *,
        session_id: str,
        profile_id: str | None = None,
    ) -> CellRun:
        """Feed corpus turns (role-mapped to ingest events), settle, drain,
        snapshot the full range, run the dream once, and read everything back."""
        profile = profile_id or self.profile_id
        turn_sessions = tuple(f"{session_id}-s{index:02d}" for index in range(len(turns)))
        # ledger baseline: the report should measure THIS material's tokens,
        # not the rig's cumulative month (a rig is reused across materials).
        usage_before = self._ledger.usage(profile)
        started = time.perf_counter()
        for index, (turn, mini_session) in enumerate(zip(turns, turn_sessions, strict=True)):
            self._segmenter.ingest(
                IngestEvent(
                    host=HostId.GENERIC,
                    event=(
                        IngestEventType.USER_PROMPT
                        if turn.role == "user"
                        else IngestEventType.ASSISTANT_MESSAGE
                    ),
                    session_id=mini_session,
                    profile_id=profile,
                    ts=1_700_000_000.0 + index,
                    content=MessageContent(text=turn.text),
                )
            )
            self._segmenter.end_session(mini_session, profile)
            self._writing.drain(mini_session)
        if turns:
            # Production M1 shape (FR-2.8): a pool event held as pending-manual,
            # then one `dream --once` cycle. Driving through the trigger keeps
            # the full state machine path (TRIGGERED -> DREAMING -> MERGING ->
            # finish) exactly as the daemon runs it — including the safe clear,
            # which only fires on a merge-commit seen by the trigger.
            self._trigger.handle_event(
                PoolEvent(
                    kind=PoolEventKind.DREAM_TRIGGER,
                    profile_id=profile,
                    turn_range=TurnRange(0, 0),
                    balance=0.0,
                    fired_at=1_700_000_000.0,
                )
            )
            self._trigger.dream_once(profile)
        duration_s = time.perf_counter() - started
        usage_delta = self._ledger.usage(profile) - usage_before
        return self._read_back(profile, duration_s, turn_sessions, usage_delta)

    def run_snapshot(self, snapshot: Snapshot) -> CellRun:
        """Replay a frozen material snapshot through a FRESH reflect/verify/
        merge on this cell (the B1-harness shape, made durable).

        The caller hands in a snapshot whose run journal phases were already
        reset (``materials.fresh_replay``) — chunk stamps ride verbatim so
        tier/origin are never re-derived, but the dream itself re-runs under
        THIS cell's seats. The scratch vector store holds no chunk copies for
        replay materials; the read-back's ``chunks`` therefore come from the
        snapshot itself (``consolidated=False``), and attribution fields stay
        empty (``turn_sessions=()``): recall/pollution metrics are canary-only.
        """
        profile = snapshot.profile_id
        self._stores.meta.upsert_profile(StoredProfile(profile_id=profile, created_at=time.time()))
        usage_before = self._ledger.usage(profile)
        started = time.perf_counter()
        self._snapshotter.adopt(snapshot)
        self._trigger.resume(profile, snapshot.turn_range)
        self._pipeline.run(snapshot)
        duration_s = time.perf_counter() - started
        usage_delta = self._ledger.usage(profile) - usage_before
        run = self._read_back(profile, duration_s, (), usage_delta)
        # replay attribution: surface the material chunks on the read-back
        return replace(
            run,
            chunks=tuple(
                RecordedChunk(
                    chunk_id=chunk.chunk_id,
                    session_id=chunk.session_id,
                    turn_start=chunk.turn_start,
                    turn_end=chunk.turn_end,
                    text=chunk.text,
                    consolidated=False,
                )
                for chunk in snapshot.chunks
            ),
        )

    # ------------------------------------------------------------ read-back

    def _read_back(
        self, profile: str, duration_s: float, turn_sessions: tuple[str, ...], usage_delta: int
    ) -> CellRun:
        core_nodes = self._read_nodes(self._stores.graph, profile, "main")
        isolated_store = self._stores.instances["graph"]["isolated"]
        isolated_nodes = self._read_nodes(cast(GraphStore, isolated_store), profile, "isolated")
        chunks = tuple(
            sorted(
                (
                    RecordedChunk(
                        chunk_id=chunk.chunk_id,
                        session_id=chunk.provenance.session_id,
                        turn_start=chunk.turn_start,
                        turn_end=chunk.turn_end,
                        text=chunk.text,
                        consolidated=chunk.consolidated,
                    )
                    for chunk in self._stores.vector.list_chunks(
                        ChunkFilter(profile_id=profile), Page(limit=1000)
                    ).items
                ),
                key=lambda c: (c.session_id or "", c.chunk_id),
            )
        )
        audit = tuple(
            sorted(
                self._stores.meta.audit_query(AuditFilter(), Page(limit=1000)).items,
                key=lambda a: (a.action, a.actor, str(sorted(a.detail.items()))),
            )
        )
        active = self._snapshotter.active(profile)
        reflect_result = None
        if active is not None:
            journaled = load_snapshot_file(self.paths.journal_dir / f"{active.snapshot_id}.json")
            if journaled is not None:
                reflect_result = result_from_payload(journaled.reflect_result)
        merge_outcome = self._merger.last_outcome
        return CellRun(
            cell_id=self.cell.cell_id,
            profile_id=profile,
            merge_committed=bool(merge_outcome.committed) if merge_outcome is not None else False,
            merge_summary=merge_outcome.summary if merge_outcome is not None else None,
            reflect_outcome=self._reflector.last_outcome,
            reflect_result=reflect_result,
            core_nodes=core_nodes,
            isolated_nodes=isolated_nodes,
            chunks=chunks,
            audit=audit,
            token_usage=usage_delta,
            duration_s=duration_s,
            turn_sessions=turn_sessions,
            reflect_collapse_attempts=self._reflector.last_collapse_attempts,
            reflect_recovered=self._reflector.last_reflect_recovered,
            seat_seed=dict(self.cell.reflect.params).get("seed"),
        )

    def _read_nodes(self, graph: GraphStore, profile: str, name: str) -> tuple[RecordedNode, ...]:
        nodes = graph.list_nodes(NodeFilter(profile_id=profile), Page(limit=1000)).items
        recorded: list[RecordedNode] = []
        for node in nodes:
            chunk_ids: tuple[str, ...] = ()
            for event in node.provenance.history:
                if event.action == "created":
                    chunk_ids = tuple(str(c) for c in event.detail.get("chunk_ids", []))
                    break
            recorded.append(
                RecordedNode(
                    node_id=node.node_id,
                    graph=name,
                    subject=str(node.props.get("subject", "")),
                    predicate=str(node.props.get("predicate", "")),
                    object=str(node.props.get("object", "")),
                    polarity=str(node.props.get("polarity", "positive")),
                    confidence=float(node.confidence),
                    chunk_ids=chunk_ids,
                )
            )
        recorded.sort(key=lambda n: n.node_id)
        return tuple(recorded)

    # ------------------------------------------------------------ lifecycle

    @property
    def stores(self) -> Stores:
        """Read access for metrics/debug (never mutated outside the dream)."""
        return self._stores

    def close(self) -> None:
        """Release the embedded store handles (sqlite/lancedb)."""
        asyncio.run(self._stores.close())
