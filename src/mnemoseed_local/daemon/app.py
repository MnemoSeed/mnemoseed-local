"""MnemoSeed Local daemon — FastAPI core, A2 local MVP trim.

Boot sequence: load config (loopback-only baseurl) -> build stores (drivers run
their schema migrations at construction) -> capability gate -> serve. The
surface is deliberately small:

- capture write endpoints (/ingest, /session/end, /flush),
- recall/read endpoints (/memory/*),
- the manual dream surface (/memory/dream_once),
- config get/set/rollback over ConfigWriteService (/api/v1/config),
- /healthz liveness + /api/v1/audit read.

NO identity/accounts/tokens: the daemon binds loopback by default and a
non-loopback baseurl is rejected at boot; every memory route takes an explicit
profile_id (never guessed, D5 isolation).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future as ConcurrentFuture
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, status

from mnemoseed_local import __version__
from mnemoseed_local.capture import (
    ScoringPipeline,
    StrippingPipeline,
    TurnScorer,
    TurnSegmenter,
    WritingPipeline,
)
from mnemoseed_local.capture.pool import PoolEvent, ScorePool
from mnemoseed_local.capture.stamper import WriteContext
from mnemoseed_local.config import Config, load_config
from mnemoseed_local.configwrite.routes import router as configwrite_router
from mnemoseed_local.configwrite.service import ConfigWriteService
from mnemoseed_local.daemon.actor import resolve_actor
from mnemoseed_local.daemon.ingest import router as ingest_router
from mnemoseed_local.daemon.memory import MemoryService
from mnemoseed_local.daemon.memory import router as memory_router
from mnemoseed_local.daemon.observability import Observability
from mnemoseed_local.decay import DecaySweeper
from mnemoseed_local.decay.rebuild import rebuild_pin_weights
from mnemoseed_local.dream import (
    DeltaPacker,
    DreamPipeline,
    DreamScheduler,
    DreamTrigger,
    FileSnapshotter,
    Merger,
    ReflectOrchestrator,
    Snapshot,
    TokenLedger,
    TripleVerifier,
    resume_boundary,
)
from mnemoseed_local.dream.pipeline import ExtractFailure, RunCompletion
from mnemoseed_local.llm import RoleRouter
from mnemoseed_local.llm.types import (
    ChatResult,
    DreamLLM,
    HealthReport,
    LLMDriverInfo,
    LLMError,
    LLMUnavailable,
)
from mnemoseed_local.retrieve.cues import CueConfig, _is_tool_name, extract_cues
from mnemoseed_local.schema.stamp import CognitiveTier
from mnemoseed_local.schema.turn import Turn, TurnRole
from mnemoseed_local.storage.factory import Stores, build_stores
from mnemoseed_local.storage.ports import (
    AuditEntry,
    AuditFilter,
    CapabilityIssue,
    GraphStore,
    Page,
)
from mnemoseed_local.util.daemon_executor import DaemonExecutor

logger = logging.getLogger("mnemoseed_local.daemon")

# The dream role the reflect boundary runs (A2 MVP), and the B1 ensemble
# verify judging seat (design/01 decision 1): roles stay pipeline-internal
# params so a future deep/short split can re-open them. The B5 vote seat B's
# independent generator role (dream_vote) has no factory route; the daemon
# falls back to the dream_verifier judging route (still a distinct model from
# A) when it is unconfigured, so vote never degenerates into a same-model pass.
_REFLECT_ROLE = "dream"
_VERIFIER_ROLE = "dream_verifier"
_VOTE_ROLE = "dream_vote"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# F2 根治 (PRD-B2.3 append) + B6 (W-C): the bounded stop waits for a wedged
# in-flight dream chain and for the capture drain lane. Worst-case teardown
# budget: dream stop wait (5s) + retriever close (2s) + drain stop wait (2s)
# + store close (~1s) ≈ 10s total — a ~1s margin under the watchdog's
# refused-grace kill window (~10s grace + 1s probe interval).
DREAM_STOP_TIMEOUT_S = 5.0
# DRAIN_STOP_TIMEOUT_S is a WHOLE-QUEUE budget on the single drain worker:
# every buffered session competes for the same 2s window, so a large backlog
# can push tail sessions past the deadline and abandon them. Accepted boundary:
# the host-side B2.2 replay re-posts an abandoned tail on the next session, so
# a graceful restart never permanently loses data (no per-drain knob, KISS).
DRAIN_STOP_TIMEOUT_S = 2.0


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


class _UnavailableLLM:
    """Boot-safe deferred dream LLM (FR-2.6).

    Used when the configured dream route cannot be materialized at boot
    (unknown driver name, or a driver construction failure): boot must never
    crash on a broken route. Dreams stay capture-only — ``chat`` degrades
    through the typed ``LLMUnavailable`` path the reflect boundary already
    handles, and ``check`` reports the reason without raising.
    """

    info: ClassVar[LLMDriverInfo] = LLMDriverInfo(
        name="unavailable",
        description="deferred route: the configured dream driver failed to build",
    )

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system
        raise LLMUnavailable(self._reason)

    def check(self) -> HealthReport:
        return HealthReport(ok=False, detail={"error": self._reason})


def _build_dream_llm(router: RoleRouter) -> DreamLLM:
    """Materialize the dream-pipeline LLM from the configured routes.

    Resolution performs no network I/O (drivers construct lazy HTTP clients),
    so boot stays fast with or without keys. A route that cannot be built
    (unknown driver, bad params) degrades typed to a deferred LLM instead of
    crashing boot; the reflect boundary then reports ``llm_unavailable`` and
    the snapshot stays journaled (FR-2.6).
    """
    try:
        return router.resolve(_REFLECT_ROLE)
    except LLMError as exc:
        logger.warning(
            "dream route unavailable at boot (%s); dreams degrade to capture-only until the route is fixed",
            exc,
        )
        return _UnavailableLLM(str(exc))


def _build_verifier_llm(router: RoleRouter) -> DreamLLM:
    """Materialize the ensemble verify judging seat (B1), same degrade-typed
    boot discipline as the dream route. A broken verifier route never crashes
    boot and never blocks a dream: the verify phase falls back to the
    unverified reflect result with an audit record (design/01 decision 1)."""
    try:
        return router.resolve(_VERIFIER_ROLE)
    except LLMError as exc:
        logger.warning(
            "verifier route unavailable at boot (%s); ensemble verify falls back to A's original reflect",
            exc,
        )
        return _UnavailableLLM(str(exc))


def _build_vote_llm(router: RoleRouter) -> DreamLLM:
    """Materialize the B5 vote seat B's generator route (independent of A).

    Vote is a second full generation over the same delta, so seat B needs its
    own route — never A's model and never the cheaper judge model re-used as a
    generator. The dedicated ``dream_vote`` route has no factory default, so it
    is commonly unconfigured: fall back to the ``dream_verifier`` judging route
    (still a distinct model from A) with a warning, so vote degrades to an
    independent-seat pass rather than a same-model duplicate or a crash. A
    broken dedicated route degrades the same way.
    """
    try:
        return router.resolve(_VOTE_ROLE)
    except LLMError as exc:
        logger.warning(
            "vote route unavailable (%s); seat B falls back to the dream_verifier route",
            exc,
        )
        return _build_verifier_llm(router)


def _reflect_unavailable(reason: str) -> None:
    """FR-2.6: log each typed provider outage the reflect boundary refuses."""
    logger.warning("dream reflect model unavailable: %s", reason)


@dataclass(frozen=True)
class HealthSnapshot:
    """Boot-time snapshot served by /healthz (captured once, served many)."""

    started_at: float
    preset: str
    stores: dict[str, dict[str, str]]
    migrations: dict[str, int]
    gate_ok: bool
    degradations: list[dict[str, str]]
    hard_missing: list[dict[str, str]]


def _issue_payload(issue: CapabilityIssue) -> dict[str, str]:
    return {
        "capability": issue.capability.value,
        "severity": issue.severity.value,
        "layer": issue.layer,
        "instance": issue.instance,
        "driver": issue.driver,
        "feature": issue.feature,
        "behavior": issue.behavior,
    }


def _stores_payload(stores: Stores) -> dict[str, dict[str, str]]:
    return {
        kind: {name: store.info.name for name, store in named.items()}
        for kind, named in stores.instances.items()
    }


def _migrations_payload(stores: Stores) -> dict[str, int]:
    """Meta schema version per named instance — proof the migrations ran."""
    versions: dict[str, int] = {}
    for name, store in stores.instances.get("meta", {}).items():
        getter = getattr(store, "schema_version", None)
        if getter is not None:
            versions[name] = int(getter())
    return versions


def _turn_tool_names(turn: Turn) -> tuple[str, ...]:
    """Tool-name cues from the turn's TOOL steps (encoding specificity R4).

    First-occurrence order, casefold-deduped (matching the retrieval-side
    overlap semantics), capped at the retrieval cue budget so a tool-heavy turn
    never stores more names than the query side can match. Only names the
    query-side classifier recognises (``_is_tool_name``: camelCase/snake_case/
    kebab/MCP) are stored — a common lowercase host name like ``bash`` would
    otherwise be stored verbatim but never matchable by the real extractor.
    """
    cap = CueConfig().tools_cap
    seen: set[str] = set()
    names: list[str] = []
    for step in turn.steps:
        if step.role is TurnRole.TOOL and step.tool_name and _is_tool_name(step.tool_name):
            key = step.tool_name.casefold()
            if key in seen:
                continue
            if not _is_tool_name(step.tool_name):
                continue
            seen.add(key)
            names.append(step.tool_name)
            if len(names) >= cap:
                break
    return tuple(names)


def _daemon_write_context(turn: Turn) -> WriteContext:
    """Per-write encoding context on the serving path (FR-1.6).

    Entity cues are extracted from the turn's user/assistant text — the same
    corpus that stamps the chunk — mirroring the /memory/remember path, which
    extracts cues from the pinned text. Without this fill every capture chunk
    reads as no-entity-evidence to the recall-side entity gate, and every
    entity-bearing query silently excludes the whole capture surface (D2).
    Tool-name cues come from the turn's TOOL steps (Option C): the retrieval
    β_tool overlap term is dead code unless capture stores the names it
    matches on.
    """
    text = " ".join(
        step.content
        for step in turn.steps
        if step.role in (TurnRole.USER, TurnRole.ASSISTANT) and step.content
    )
    entities = tuple(extract_cues(text).cues.entities) if text else ()
    return WriteContext(
        profile_id=turn.profile_id,
        host=turn.host.value,
        cognitive_tier=CognitiveTier.TIER_1,
        origin_agent=turn.origin_agent,
        entities=entities,
        tools_used=_turn_tool_names(turn),
    )


@dataclass(frozen=True)
class _DreamJob:
    """One unit of dream work for the worker thread.

    Exactly one of ``event`` (a fired pool event), ``profile_id`` (a manual
    ``dream_once`` run), or ``pipeline``+``snapshot`` (a boot-recovery resume)
    is set. ``future`` resolves the manual launch decision back to the awaiting
    /memory/dream_once caller.
    """

    event: PoolEvent | None = None
    profile_id: str | None = None
    future: asyncio.Future[bool] | None = None
    pipeline: DreamPipeline | None = None
    snapshot: Snapshot | None = None

    def run(self, trigger: DreamTrigger, config: Config | None = None) -> bool | None:
        """Execute on the worker thread; returns the manual launch decision."""
        if self.pipeline is not None and self.snapshot is not None:
            # Boot-recovery replay: no new LLM evidence, so the oversized-delta
            # parking guard must not arm on replayed verdicts (#97/#99).
            self.pipeline.run(self.snapshot, counts_toward_parking=False)
            return None
        if self.event is not None:
            if config is not None:
                # hot-apply seam: re-read the shared Config per delivery so a
                # configwrite auto_trigger flip reaches this event live
                trigger.set_auto_trigger(config.dream.auto_trigger)
            trigger.handle_event(self.event)
            return None
        if self.profile_id is not None:
            return trigger.dream_once(self.profile_id)
        return None


class DreamWorker:
    """Runs the dream chain off the event loop on one dedicated thread.

    The snapshot -> reflect -> merge -> safe-clear chain is synchronous and used
    to run on the app event loop, freezing every other endpoint for its whole
    duration. The worker hands each dream job to a ``DaemonExecutor`` with
    ``max_workers=1``: at most one dream is ever in flight (by construction),
    the event loop only submits jobs and reads ``trigger.status()`` snapshots,
    and all trigger mutations happen on the worker thread. The worker thread is
    a daemon thread never registered with the interpreter's atexit join set, so
    a wedged chain cannot hold the process hostage (F2 根治). ``stop()`` drains
    an in-flight dream with a bounded wait before the stores close during
    lifespan teardown, abandoning a wedged chain instead of joining it.
    """

    def __init__(
        self,
        trigger: DreamTrigger,
        *,
        config: Config | None = None,
        stop_timeout: float = DREAM_STOP_TIMEOUT_S,
    ) -> None:
        self._trigger = trigger
        self._config = config
        self._stop_timeout = stop_timeout
        self._executor = DaemonExecutor(max_workers=1, thread_name_prefix="mnemoseed-dream")
        self._queue: asyncio.Queue[_DreamJob] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._inflight: _DreamJob | None = None  # the job whose chain is running
        self._inflight_cf: ConcurrentFuture[bool | None] | None = None
        # Deferred boot-recovery resumes: the scheduler's first tick waits until
        # every queued resume completed, so it never emits a duplicate dream
        # over a range the resume is about to consolidate.
        self._resume_pending = 0
        self._resume_drained = asyncio.Event()
        self._resume_drained.set()

    def start(self) -> None:
        """Create the consumer task (idempotent; called from lifespan)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def submit_event(self, event: PoolEvent) -> None:
        """Queue one fired pool event for delivery on the worker thread."""
        await self._queue.put(_DreamJob(event=event, profile_id=None, future=None))

    def enqueue_event(self, event: PoolEvent) -> None:
        """Synchronous event submission (the scheduler tick path)."""
        self._queue.put_nowait(_DreamJob(event=event, profile_id=None, future=None))

    def enqueue_resume(self, pipeline: DreamPipeline, snapshot: Snapshot) -> None:
        """Queue one boot-recovery resume for the worker.

        The scheduler gate tracks the count: each resume completion decrements
        it and the drain event fires at zero, so the scheduler's first tick
        waits for the whole journaled recovery window to drain."""
        self._resume_pending += 1
        self._resume_drained.clear()
        self._queue.put_nowait(_DreamJob(pipeline=pipeline, snapshot=snapshot))

    @property
    def resume_drained(self) -> asyncio.Event:
        """Set once every deferred boot-recovery resume completed; the
        scheduler awaits it before its first tick."""
        return self._resume_drained

    async def submit_dream_once(self, profile_id: str) -> bool:
        """Run exactly one manual cycle; awaits the worker's launch decision."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        await self._queue.put(_DreamJob(event=None, profile_id=profile_id, future=future))
        return await future

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            self._inflight = job
            try:
                cf = self._executor.submit(job.run, self._trigger, self._config)
                self._inflight_cf = cf
                result = await asyncio.wrap_future(cf)
            except Exception:
                logger.exception("dream worker job failed; the trigger state stays consistent")
                result = None
            finally:
                if job.pipeline is not None and job.snapshot is not None:
                    self._resume_pending -= 1
                    if self._resume_pending <= 0:
                        self._resume_pending = 0
                        self._resume_drained.set()
            self._inflight = None
            self._inflight_cf = None
            if job.future is not None and not job.future.done():
                job.future.set_result(result is True)

    async def stop(self) -> None:
        """Cancel the consumer, drain pending manual jobs, then wait bounded
        for the in-flight chain before shutdown.

        Queued jobs never launched: their pending futures resolve False here.
        The in-flight chain is awaited for at most ``stop_timeout`` WITHOUT
        jamming the event loop (the pre-fix shutdown(wait=True) blocked the
        loop for the whole wedge); on timeout the chain is ABANDONED — the
        journaled snapshot guarantees re-recovery on the next boot, and the
        lancedb/sqlite writes are atomic, so abandoning never corrupts state.
        The abandoned worker stays a daemon thread and dies with the process.
        The pending future then resolves with the real launch decision (or
        False on failure or abandon), so a /memory/dream_once caller never
        hangs on a dream that will never happen.
        """
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # queued manual jobs were never launched: resolve them as not-launched
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            future = job.future
            if future is not None and not future.done():
                future.set_result(False)
        # bounded wait on the in-flight chain without jamming the loop
        inflight_cf = self._inflight_cf
        if inflight_cf is not None:
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(inflight_cf),
                    timeout=self._stop_timeout,
                )
            except TimeoutError:
                pass  # abandoned: the chain is wedged in unbounded store I/O
            except asyncio.CancelledError:
                pass  # the chain never launched (cancelled while queued)
        self._executor.close(timeout=0)
        inflight = self._inflight
        if inflight is not None and inflight.future is not None and not inflight.future.done():
            inflight.future.set_result(self._inflight_launched())
        self._inflight = None
        self._inflight_cf = None

    def _inflight_launched(self) -> bool:
        """The real launch decision of the drained in-flight chain (False on
        any failure or abandon — the job never actually launched)."""
        cf = self._inflight_cf
        if cf is None or not cf.done():
            return False
        try:
            return cf.result() is True
        except Exception:
            return False


class DrainLane:
    """Runs the capture drain off the event loop on one dedicated daemon thread.

    ``WritingPipeline.drain`` is store I/O that used to run inline on the app
    loop, blocking every endpoint for its whole duration. The lane executes each
    drain on a ``DaemonExecutor`` with ``max_workers=1`` (per-session FIFO for
    free; the store write lock serializes anyway). The handler awaits the drain
    future, so the ack still means completed-applied; ``stop()`` completes every
    queued drain with a bounded wait before the stores close and abandons a
    wedged one on the deadline — the abandoned worker stays a daemon thread and
    dies with the process (F2 根治 semantics: teardown never hangs).
    """

    def __init__(
        self,
        *,
        stop_timeout: float = DRAIN_STOP_TIMEOUT_S,
    ) -> None:
        self._stop_timeout = stop_timeout
        self._executor = DaemonExecutor(max_workers=1, thread_name_prefix="mnemoseed-drain")
        self._pending: dict[str, ConcurrentFuture[Any]] = {}
        self._lock = threading.Lock()

    def submit(self, fn: Callable[[str], Any], session_id: str) -> None:
        """Queue one drain without awaiting (teardown submits all, then stops)."""
        future = self._executor.submit(fn, session_id)
        with self._lock:
            self._pending[session_id] = future

    async def drain(self, fn: Callable[[str], Any], session_id: str) -> None:
        """Queue one drain and await its completion (the ack = applied)."""
        future = self._executor.submit(fn, session_id)
        with self._lock:
            self._pending[session_id] = future
        try:
            await asyncio.wrap_future(future)
        finally:
            with self._lock:
                if self._pending.get(session_id) is future:
                    self._pending.pop(session_id, None)

    async def stop(self) -> tuple[int, int, int]:
        """Complete every queued drain with a bounded wait, then close the lane.

        Returns (completed, failed, abandoned): the drains that finished, the
        ones that raised (a teardown-time data-loss event with no awaiting
        handler — logged per session with the exception), and the ones wedged
        past the deadline (never joined — the daemon worker dies with the
        process; the host-side B2.2 replay absorbs their undrained turns).
        Close is terminal: no drain can be submitted after stop.
        """
        deadline = time.monotonic() + self._stop_timeout
        completed = 0
        failed: list[str] = []
        abandoned: list[str] = []
        while True:
            with self._lock:
                pending = dict(self._pending)
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                abandoned.extend(pending)
                break
            wrapped = {session_id: asyncio.wrap_future(future) for session_id, future in pending.items()}
            for afut in wrapped.values():
                # retrieve a wrapper's exception on completion so a late-failing
                # abandoned drain never leaves a 'never retrieved' loop warning
                afut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            await asyncio.wait(wrapped.values(), timeout=remaining)
            for session_id, afut in wrapped.items():
                if not afut.done():
                    continue
                with self._lock:
                    if self._pending.get(session_id) is pending[session_id]:
                        self._pending.pop(session_id, None)
                exc = afut.exception()
                if exc is not None:
                    failed.append(session_id)
                    logger.warning(
                        "teardown: drain failed for session %s: %s",
                        session_id,
                        exc,
                    )
                else:
                    completed += 1
        if abandoned:
            logger.warning(
                "teardown: abandoned %d wedged drain(s): %s — the undrained "
                "turns are absorbed by the B2.2 host-side replay",
                len(abandoned),
                ", ".join(abandoned),
            )
        self._executor.close(timeout=0)
        return completed, len(failed), len(abandoned)


class _DreamRelay:
    """Deferred dream-event delivery off the scoring hot path.

    The ScorePool fires dream events while the ScoringPipeline is still scoring
    a drained session — before the WritingPipeline has persisted that session's
    chunks to the vector store. A dream launched at that instant would capture
    an empty snapshot and its safe-clear would purge nothing. The relay instead
    collects the fired events and, once the drain wrote the chunks (the daemon
    flushes after ``WritingPipeline.drain``), hands them to the worker in
    order. Manual-first (FR-2.8) is untouched: with auto_trigger=False the
    worker simply delivers events the trigger records as pending-manual.
    """

    def __init__(self, worker: DreamWorker) -> None:
        self._worker = worker
        self._pending: deque[PoolEvent] = deque()

    def handle(self, event: PoolEvent) -> None:
        """ScorePool sink seam: buffer a fired dream event during the drain."""
        self._pending.append(event)

    async def flush(self) -> None:
        """Hand buffered events to the worker, in fire order."""
        while self._pending:
            await self._worker.submit_event(self._pending.popleft())


class _WorkerTriggerForwarder:
    """Scheduler -> worker seam: hands a scheduler-emitted event to the dream
    worker instead of calling the trigger inline, so trigger mutations stay on
    the worker thread (the scheduler tick runs on the event loop)."""

    def __init__(self, worker: DreamWorker) -> None:
        self._worker = worker

    def handle_event(self, event: PoolEvent) -> None:
        self._worker.enqueue_event(event)


def _audit_extract_failure(meta: Any, failure: ExtractFailure) -> None:
    """Observation log for failed dream extractions: one classified audit row
    per attempt, best-effort — an audit surface fault never breaks the dream
    pipeline."""
    try:
        meta.audit_append(
            AuditEntry(
                actor="dream",
                action="dream_extract_failed",
                detail={
                    "profile_id": failure.profile_id,
                    "stage": failure.stage,
                    "failure_class": failure.failure_class,
                    "detail": failure.detail,
                    "tokens": failure.tokens,
                },
                at=time.time(),
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract-failure audit failed for %s: %s", failure.profile_id, exc)


def _build_capture(
    stores: Stores, config: Config, configwrite: ConfigWriteService
) -> tuple[
    WritingPipeline,
    DreamTrigger,
    DreamPipeline,
    DreamWorker,
    _DreamRelay,
    RoleRouter,
    list[tuple[DreamPipeline, Snapshot]],
    ScorePool,
]:
    """Serving capture funnel: strip -> score -> pool -> stamp/write over the
    resolved storage stack. /ingest stays submit-only; the funnel drains on
    /session/end (v1 drain trigger, off the /ingest hot path).

    Journaled-snapshot recovery is split by cost: the O(1) classification and
    trigger bookkeeping (recover / adopt / resume routing) run synchronously
    here, but the expensive pipeline.run reflect/merge chain is deferred as
    RESUME jobs for the dream worker, so the port binds fast and the watchdog
    PRE_BIND window covers only true hangs. The deferred (pipeline, snapshot)
    pairs are returned for the worker to enqueue in order after start.
    """
    snapshotter = FileSnapshotter(store=stores.vector, meta=stores.meta)
    trigger = DreamTrigger(
        snapshotter=snapshotter,
        auto_trigger=config.dream.auto_trigger,
        purger=snapshotter.purge_snapshot,
    )
    graph_isolated = cast(GraphStore | None, stores.instances.get("graph", {}).get("isolated"))
    if graph_isolated is None:
        # T3b (design/01 §4.8): the isolated graph instance is MANDATORY. The
        # config surface already enforces it (load-time and configwrite both
        # reject a floor config without it) — this boot-time hard stop is the
        # last line of defense so the daemon never serves a dream that could
        # strand tier-3 output or fail a floor downgrade mid-pass.
        raise RuntimeError(
            "no 'isolated' graph instance configured; add a "
            '[storage.graph.instances.isolated] table (driver = "sqlite_graph") '
            "or run 'mnemoseed init' to write the template"
        )
    router = RoleRouter(
        routes=config.llm,
        audit=stores.meta,
        generation=configwrite.generation_for,
        secrets=None,
    )

    def _resolve_dream_llm() -> DreamLLM:
        return _build_dream_llm(router)

    def _resolve_verifier_llm() -> DreamLLM:
        return _build_verifier_llm(router)

    def _resolve_vote_llm() -> DreamLLM:
        return _build_vote_llm(router)

    ledger = TokenLedger(meta=stores.meta)
    verifier = TripleVerifier(
        llm=_resolve_verifier_llm(),
        resolve_llm=_resolve_verifier_llm,
        config=config,
        meta=stores.meta,
        ledger=ledger,
    )

    reflector = ReflectOrchestrator(
        llm=_resolve_dream_llm(),
        resolve_llm=_resolve_dream_llm,
        directory=snapshotter.directory,
        packer=DeltaPacker(config=config),
        on_done=trigger.on_reflect_complete,
        on_unavailable=_reflect_unavailable,
        on_run_started=lambda run_id, model: stores.meta.update_dream_run_model(run_id, model),
        ledger=ledger,
        verifier=verifier,
        # B5 vote: seat B is a second full generator over the same delta, wired
        # to its OWN route (dream_vote) so the two seats carry an independent
        # signal — never A's model. Unconfigured, it falls back to the
        # dream_verifier judging route (still a distinct model from A), so vote
        # never degenerates into a same-model duplicate or a crash.
        vote_llm=_build_vote_llm(router),
        resolve_vote_llm=_resolve_vote_llm,
        # Batched reflection (#99): 0 keeps the legacy single-pack reflect;
        # a positive cap slices oversized backlogs into model-sized batches.
        batch_max_tokens=config.dream.reflect_batch_max_tokens or None,
    )
    merger = Merger(
        graph_main=stores.graph,
        graph_isolated=graph_isolated,
        meta=stores.meta,
        on_committed=trigger.on_merge_committed,
        config=config,
    )

    def _record_run_completion(completion: RunCompletion) -> None:
        """The user-facing dream log: complete the run row (finish + metered
        tokens) and land a queryable audit entry. Best-effort — a journal
        surface failure never disturbs a committed merge."""
        try:
            stores.meta.finish_dream_run(
                completion.run_id,
                finished_at=completion.finished_at,
                tokens=completion.tokens,
                cost=0.0,
                dropped_count=0,
            )
            stores.meta.audit_append(
                AuditEntry(
                    actor="dream",
                    action="dream_committed",
                    detail={
                        "run_id": completion.run_id,
                        "profile_id": completion.profile_id,
                        "duration_s": round(completion.finished_at - completion.started_at, 1),
                        "tokens": completion.tokens,
                    },
                    at=completion.finished_at,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("run-completion journal failed for %s: %s", completion.run_id, exc)

    pipeline = DreamPipeline(
        trigger=trigger,
        snapshotter=snapshotter,
        reflector=reflector,
        merger=merger,
        on_run_committed=_record_run_completion,
        on_extract_failed=lambda failure: _audit_extract_failure(stores.meta, failure),
        # B5 vote: the live ensemble mode ("off" | "verify" | "vote") read off
        # the config each run, so the pipeline dispatches the vote dual-seat
        # chain when the user opted in (configwrite changes hot-apply).
        mode=lambda: config.dream.ensemble,
    )
    worker = DreamWorker(trigger, config=config)
    relay = _DreamRelay(worker)
    snapshotter.on_ready = pipeline.on_snapshot_ready
    deferred_resumes: list[tuple[DreamPipeline, Snapshot]] = []
    for snapshot in snapshotter.recover():
        snapshotter.adopt(snapshot)
        boundary = resume_boundary(snapshot)
        if boundary == "reflect":
            trigger.resume(snapshot.profile_id, snapshot.turn_range)
        elif boundary == "merge":
            trigger.resume_merge(snapshot.profile_id, snapshot.turn_range)
        elif boundary in ("reflect_b", "combine"):
            # B5 vote resume: a mid-vote crash left seat B / the combiner
            # un-run. Resume into DREAMING (like "reflect") so the deferred
            # resume's merge-commit fires the safe-clear; resume_merge would
            # wrongly skip the B/combine work this boundary still needs.
            trigger.resume(snapshot.profile_id, snapshot.turn_range)
        deferred_resumes.append((pipeline, snapshot))
    # The capture pool self-fires at the SAME configured floor/idle keys the
    # scheduler reads (dream.floor_pool_points / dream.idle_min_sec): never a
    # fixed literal. The scheduler stays the authority — a pool fire drains the
    # balance, so the scheduler cannot double-service the same window. The
    # forced cap (dream.pool_forced_cap, T3a) is read live through the bound
    # Config, so a configwrite change applies to the SAME pool instance.
    pool = ScorePool(
        clock=time.monotonic,
        backend=stores.meta,
        sink=relay.handle,
        dream_threshold=config.dream.floor_pool_points,
        idle_window_sec=config.dream.idle_min_sec,
        forced_cap=config.dream.pool_forced_cap,
        config=config,
    )
    for profile_id, state in stores.meta.pool_states().items():
        pool.restore(profile_id, state.balance, state.watermark)
    scoring = ScoringPipeline(
        scorer=TurnScorer(embedder=stores.embed),
        pool=pool,
    )
    return (
        WritingPipeline(
            store=stores.vector,
            inner=scoring,
            embedder=stores.embed,
            context=_daemon_write_context,
        ),
        trigger,
        pipeline,
        worker,
        relay,
        router,
        deferred_resumes,
        pool,
    )


def _attach_daemon_log_handler() -> None:
    """Attach the durable daemon.log FileHandler to the ``mnemoseed_local``
    logger (PRD-B2.3 D4), idempotent across repeated lifespan entries in one
    process. The handler flushes per emit, so the file is readable while the
    daemon is alive; the boot/teardown stage lines and the watchdog last-words
    all land here through the daemon logger's propagation. The logger's level
    is lifted to INFO so the info-graded stage lines are not dropped before the
    handler sees them. The handler is a process-global: exactly one live boot
    per process is the supported embedding shape, and its teardown releases
    the handler again (``_release_daemon_log_handler``)."""
    target = logging.getLogger("mnemoseed_local")
    for handler in target.handlers:
        if getattr(handler, "name", None) == "daemon.log":
            return
    # Resolve CONFIG_DIR at call time, not import time, so a test (or a
    # process that relocated its home) monkeypatching config.CONFIG_DIR is
    # honored — otherwise every TestClient boot in the suite writes the real
    # user home's daemon.log (QA I-1).
    from mnemoseed_local.config import CONFIG_DIR

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(CONFIG_DIR / "daemon.log", encoding="utf-8")
    handler.name = "daemon.log"
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    target.addHandler(handler)
    target.setLevel(logging.INFO)


def _release_daemon_log_handler() -> None:
    """Teardown counterpart of ``_attach_daemon_log_handler``: close and drop
    the named handler so a torn-down boot never keeps its daemon.log open
    (undeletable log) or bleeds into a later boot's file. Assumes one live
    boot per process — the shape pinned at the attach site."""
    target = logging.getLogger("mnemoseed_local")
    for handler in list(target.handlers):
        if getattr(handler, "name", None) == "daemon.log":
            target.removeHandler(handler)
            handler.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    host = urlparse(config.baseurl).hostname
    if not _is_loopback_host(host):
        raise RuntimeError(
            f"daemon refuses non-loopback baseurl {config.baseurl!r}: the local MVP "
            "is localhost-only (no identity/accounts/tokens)"
        )
    _attach_daemon_log_handler()
    logger.info(
        "daemon boot: pid=%d version=%s preset=%s port=%s",
        os.getpid(),
        __version__,
        config.preset,
        urlparse(config.baseurl).port or 7788,
    )
    stores = build_stores(config)
    app.state.config = config
    app.state.stores = stores
    # Since-boot observability counters (B2.12): a fresh instance per boot so
    # the counts are honest since-boot signals for the doctor surface.
    app.state.observability = Observability()
    # ConfigWriteService: the daemon's single config writer behind every
    # CLI settings change; boot reconciliation (E1-4 DB-primary) imports the
    # registry keys once and then lets the DB win over a hand-edited file.
    app.state.configwrite = ConfigWriteService(config, stores.meta)
    app.state.configwrite.reconcile_boot()
    (
        app.state.capture,
        app.state.dream,
        app.state.dream_pipeline,
        app.state.dream_worker,
        app.state.dream_relay,
        app.state.role_router,
        deferred_resumes,
        app.state.score_pool,
    ) = _build_capture(stores, config, app.state.configwrite)
    app.state.segmenter = TurnSegmenter(app.state.capture)
    # B6 (W-C): the drain lane runs WritingPipeline.drain off the event loop on
    # one dedicated daemon thread. Per-lifespan (drains are recoverable-critical,
    # unlike the never-closed scan singleton); teardown stops it before the
    # stores close.
    app.state.drain_lane = DrainLane()
    # Dream chain thread (T1a): the capture/memory surface never blocks on a
    # dream. Started here so the worker consumes events from boot onward.
    app.state.dream_worker.start()
    # Deferred journaled recovery: the pipeline.run chain is enqueued as RESUME
    # jobs in order AFTER the worker starts, so the expensive reflect/merge runs
    # on the worker thread and the port binds fast. The scheduler waits for
    # these to drain before its first tick.
    for resume_pipeline, resume_snapshot in deferred_resumes:
        app.state.dream_worker.enqueue_resume(resume_pipeline, resume_snapshot)
    # Memory surface (T4): one retrieval engine whose track executor is shut
    # down in teardown, before the stores close.
    app.state.memory = MemoryService(stores, config)
    # Retention redesign one-time migration (design/09 §4.1): existing pin
    # chunks recompute their effective weight under the flashbulb λ from their
    # own reinforcement baseline — deterministic, idempotent, marker-gated.
    rebuild_pin_weights(stores, config)
    # Decay sweep (PRD-04 FR-4.1 / FR-4.4): the daemon-owned background loop
    # over the live config (λ / interval / enabled re-read each tick).
    app.state.decay = DecaySweeper(stores, config)
    app.state.decay_task = asyncio.create_task(app.state.decay.run_forever())
    # A2 dream schedule (FR-2.1 / FR-2.4): the score-pool trigger rules
    # (floor+idle / hard deadline) are re-read from the config each tick, so a
    # configwrite change hot-applies without a restart. The scheduler emits
    # through the worker forwarder so every trigger mutation stays on the
    # worker thread.
    scheduler_trigger = cast(DreamTrigger, _WorkerTriggerForwarder(app.state.dream_worker))
    app.state.scheduler = DreamScheduler(
        stores,
        config,
        trigger=scheduler_trigger,
        resume_drain=app.state.dream_worker.resume_drained,
        # fire-time drain through the live pool: the persisted gauge files into
        # the lifetime ledger and the in-process gauge resets together, so a
        # later credit can never resurrect drained points
        drain=app.state.score_pool.drain,
    )
    # A2.5 T1 backoff wiring: the dream pipeline reports every attempt's outcome
    # (reflect/merge ok or error) back to the scheduler, so a failed dream
    # re-arms its fired fingerprint and re-fires on the exponential backoff. The
    # pipeline runs on the dream worker thread; the scheduler drains the report
    # on its next tick (event loop), so no trigger state is ever mutated inline.
    app.state.dream_pipeline.on_outcome = app.state.scheduler.report_outcome
    app.state.scheduler_task = asyncio.create_task(app.state.scheduler.run_forever())
    app.state.health = HealthSnapshot(
        started_at=time.perf_counter(),
        preset=config.preset,
        stores=_stores_payload(stores),
        migrations=_migrations_payload(stores),
        gate_ok=stores.report.ok,
        degradations=[_issue_payload(i) for i in stores.report.degradations],
        hard_missing=[_issue_payload(i) for i in stores.report.hard_missing],
    )
    if stores.report.ok:
        logger.info("storage stack ready, all required capabilities present")
    else:
        for deg in stores.report.missing:
            logger.warning("degraded: %s - %s", deg.feature, deg.behavior)
    yield
    # Stop the background loops before the stores close (lifecycle order).
    logger.info("teardown: stop background loops")
    for task_name in ("decay_task", "scheduler_task"):
        task = getattr(app.state, task_name, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    logger.info("teardown: close memory")
    app.state.memory.close()
    # Stop the dream worker before the drain lane: its in-flight chain releases
    # the shared storage stack earlier, shrinking the drain-abandon risk (B6 W-C).
    # New event submissions stopped with the cancelled scheduler above.
    logger.info("teardown: stop dream worker")
    await app.state.dream_worker.stop()
    # QA-4: drain the capture lane BEFORE anything closes — restart mid-reply
    # must not lose the last exchange. Open turns close off the hot path, then
    # every buffered session drains into the stores they belong to; the drains
    # run on the drain lane thread and teardown waits for ALL of them (bounded)
    # before the stores close (B6 W-C).
    logger.info("teardown: drain capture lane")
    segmenter = getattr(app.state, "segmenter", None)
    capture = getattr(app.state, "capture", None)
    drain = getattr(capture, "drain", None)
    lane = getattr(app.state, "drain_lane", None)
    if segmenter is not None and drain is not None and capture is not None:
        segmenter.flush_all()
        for session_id in capture.sessions():
            if lane is not None:
                lane.submit(drain, session_id)
            else:
                drain(session_id)
        if lane is not None:
            completed, failed, abandoned = await lane.stop()
            logger.info(
                "teardown: drain lane complete (%d drained, %d failed, %d abandoned)",
                completed,
                failed,
                abandoned,
            )
    logger.info("teardown: close stores")
    try:
        await stores.close()
    finally:
        logger.info("teardown: complete")
        _release_daemon_log_handler()


def _request_logging_middleware(app: Any, get_state: Callable[[], Any]) -> Any:
    """Pure-ASGI [logging] requests middleware (B2.12): one INFO line per
    request (method + path + status), paths only — never bodies. A plain
    ASGI wrapper, deliberately not BaseHTTPMiddleware: the extra task-group
    scheduling hops would reorder respond-then-run seams (/daemon/shutdown).
    Off by default; a single boolean read per request when off."""

    async def _entry(scope: Any, receive: Any, send: Any) -> None:
        config = getattr(get_state(), "config", None)
        if scope["type"] != "http" or config is None or not config.logging.requests:
            await app(scope, receive, send)
            return
        status = {"code": 0}

        async def _send(message: Any) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await app(scope, receive, _send)
        logger.info("%s %s -> %d", scope["method"], scope["path"], status["code"])

    return _entry


def create_app() -> FastAPI:
    app = FastAPI(title="MnemoSeed Local", version=__version__, lifespan=lifespan)
    # Capture intake lives per-app state; F1 (StrippingPipeline) drains the
    # same pipeline instance the /ingest router hands turns to, on the
    # consumer side of the seam so the HTTP path stays O(1).
    app.state.capture = StrippingPipeline()
    app.state.segmenter = TurnSegmenter(app.state.capture)
    app.state.observability = Observability()
    app.include_router(ingest_router)
    app.include_router(memory_router)
    app.include_router(configwrite_router)
    # B2.12 request-level observability ([logging] requests, default OFF).
    app.add_middleware(_request_logging_middleware, get_state=lambda: app.state)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        snap: HealthSnapshot = app.state.health
        elapsed_ms = (time.perf_counter() - snap.started_at) * 1000.0
        return {
            "status": "ok",
            "uptime_ms": round(elapsed_ms, 3),
            "preset": snap.preset,
            "stores": snap.stores,
            "migrations": snap.migrations,
            "gate": {
                "ok": snap.gate_ok,
                "degradations": snap.degradations,
                "hard_missing": snap.hard_missing,
            },
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        stores: Stores = app.state.stores
        return {
            "status": "ok",
            "version": __version__,
            "preset": app.state.config.preset,
            "drivers": {
                "vector": stores.vector.info.name,
                "graph": stores.graph.info.name,
                "meta": stores.meta.info.name,
                "embed": stores.embed.info.name,
            },
        }

    @app.post("/mcp/handshake")
    async def mcp_handshake(request: Request) -> dict[str, Any]:
        """MCP-gateway startup beacon (B2.12): the stdio server announces
        itself when a client connects it. Loopback-trusted like every other
        surface (no tokens in the local MVP); purely observational."""
        observability: Observability | None = getattr(request.app.state, "observability", None)
        if observability is not None:
            observability.note_mcp_handshake()
        return {"ok": True}

    @app.get("/api/v1/observability")
    async def observability_read() -> dict[str, Any]:
        """Since-boot activity counters for the doctor surface (B2.12)."""
        observability: Observability | None = getattr(app.state, "observability", None)
        if observability is None:
            return {"boot_started_at": 0.0, "capture_ingest_count": 0, "mcp_handshake_count": 0}
        return observability.snapshot()

    @app.get("/api/v1/audit")
    async def audit_read(
        request: Request,
        actor: str | None = None,
        action: str | None = None,
        since: float | None = None,
        until: float | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Append-only audit read (who/what/when across every write surface)."""
        meta = app.state.stores.meta
        page = meta.audit_query(
            AuditFilter(actor=actor, action=action, since=since, until=until),
            Page(offset=max(0, offset), limit=max(1, min(500, limit))),
        )
        return {
            "items": [
                {
                    "id": entry.id,
                    "actor": entry.actor,
                    "action": entry.action,
                    "detail": entry.detail,
                    "at": entry.at,
                }
                for entry in page.items
            ],
            "total": page.total,
            "offset": page.offset,
            "limit": page.limit,
        }

    @app.post("/daemon/shutdown")
    async def daemon_shutdown(request: Request) -> dict[str, Any]:
        """Intentional shutdown (B2.5): answer 200 first, then run the
        injected shutdown hook after the response is flushed. Only run_server
        injects the hook (it alone arms the watchdog), so a TestClient boot
        answers 503 with a clear message instead of a 500."""
        hook = getattr(request.app.state, "shutdown_hook", None)
        if hook is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="no shutdown hook: the daemon was not started via run_server",
            )
        actor = resolve_actor(request)
        try:
            request.app.state.stores.meta.audit_append(
                AuditEntry(actor=actor, action="daemon_shutdown", detail={}, at=time.time())
            )
        except Exception:  # pragma: no cover - journaling is best-effort
            logger.warning("daemon_shutdown audit failed; shutdown proceeds", exc_info=True)

        async def _run_hook() -> None:
            await asyncio.sleep(0)  # yield: the 200 response flushes first
            hook()

        asyncio.create_task(_run_hook())
        return {"ok": True, "status": "shutting_down"}

    return app


app = create_app()
