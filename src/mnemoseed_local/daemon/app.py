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
import time
from collections import deque
from collections.abc import AsyncIterator
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

from fastapi import FastAPI, Request

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
from mnemoseed_local.daemon.ingest import router as ingest_router
from mnemoseed_local.daemon.memory import MemoryService
from mnemoseed_local.daemon.memory import router as memory_router
from mnemoseed_local.decay import DecaySweeper
from mnemoseed_local.dream import (
    DeltaPacker,
    DreamPipeline,
    DreamScheduler,
    DreamTrigger,
    FileSnapshotter,
    Merger,
    ReflectOrchestrator,
    TokenLedger,
    TripleVerifier,
    resume_boundary,
)
from mnemoseed_local.dream.pipeline import RunCompletion
from mnemoseed_local.llm import RoleRouter
from mnemoseed_local.llm.types import (
    ChatResult,
    DreamLLM,
    HealthReport,
    LLMDriverInfo,
    LLMError,
    LLMUnavailable,
)
from mnemoseed_local.retrieve.cues import extract_cues
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

logger = logging.getLogger("mnemoseed_local.daemon")

# The dream role the reflect boundary runs (A2 MVP), and the B1 ensemble
# verify judging seat (design/01 decision 1): roles stay pipeline-internal
# params so a future deep/short split can re-open them.
_REFLECT_ROLE = "dream"
_VERIFIER_ROLE = "dream_verifier"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


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


def _daemon_write_context(turn: Turn) -> WriteContext:
    """Per-write encoding context on the serving path (FR-1.6).

    Entity cues are extracted from the turn's user/assistant text — the same
    corpus that stamps the chunk — mirroring the /memory/remember path, which
    extracts cues from the pinned text. Without this fill every capture chunk
    reads as no-entity-evidence to the recall-side entity gate, and every
    entity-bearing query silently excludes the whole capture surface (D2).
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
        entities=entities,
    )


@dataclass(frozen=True)
class _DreamJob:
    """One unit of dream work for the worker thread.

    Exactly one of ``event`` (a fired pool event) and ``profile_id`` (a manual
    ``dream_once`` run) is set. ``future`` resolves the manual launch decision
    back to the awaiting /memory/dream_once caller.
    """

    event: PoolEvent | None
    profile_id: str | None
    future: asyncio.Future[bool] | None

    def run(self, trigger: DreamTrigger) -> bool | None:
        """Execute on the worker thread; returns the manual launch decision."""
        if self.event is not None:
            trigger.handle_event(self.event)
            return None
        if self.profile_id is not None:
            return trigger.dream_once(self.profile_id)
        return None


class DreamWorker:
    """Runs the dream chain off the event loop on one dedicated thread.

    The snapshot -> reflect -> merge -> safe-clear chain is synchronous and used
    to run on the app event loop, freezing every other endpoint for its whole
    duration. The worker hands each dream job to a ``ThreadPoolExecutor`` with
    ``max_workers=1``: at most one dream is ever in flight (by construction),
    the event loop only submits jobs and reads ``trigger.status()`` snapshots,
    and all trigger mutations happen on the worker thread. ``stop()`` drains an
    in-flight dream before the stores close during lifespan teardown.
    """

    def __init__(self, trigger: DreamTrigger) -> None:
        self._trigger = trigger
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mnemoseed-dream")
        self._queue: asyncio.Queue[_DreamJob] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._inflight: _DreamJob | None = None  # the job whose chain is running
        self._inflight_cf: ConcurrentFuture[bool | None] | None = None

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
                cf = self._executor.submit(job.run, self._trigger)
                self._inflight_cf = cf
                result = await asyncio.wrap_future(cf)
            except Exception:
                logger.exception("dream worker job failed; the trigger state stays consistent")
                result = None
            self._inflight = None
            self._inflight_cf = None
            if job.future is not None and not job.future.done():
                job.future.set_result(result is True)

    async def stop(self) -> None:
        """Cancel the consumer, then drain pending manual jobs before shutdown.

        Queued jobs never launched: their pending futures resolve False here.
        The in-flight job's chain runs to completion during executor shutdown;
        its pending future then resolves with the real launch decision (or
        False on failure), so a /memory/dream_once caller never hangs on a
        dream that will never happen.
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
        # let the in-flight chain finish, then resolve its pending future with
        # the real outcome (the chain ran to completion on the worker thread)
        self._executor.shutdown(wait=True)
        inflight = self._inflight
        self._inflight = None
        if inflight is not None and inflight.future is not None and not inflight.future.done():
            inflight.future.set_result(self._inflight_launched())
        self._inflight_cf = None

    def _inflight_launched(self) -> bool:
        """The real launch decision of the drained in-flight chain (False on
        any failure — the job never actually launched)."""
        cf = self._inflight_cf
        if cf is None:
            return False
        try:
            return cf.result() is True
        except Exception:
            return False


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


def _build_capture(
    stores: Stores, config: Config, configwrite: ConfigWriteService
) -> tuple[WritingPipeline, DreamTrigger, DreamPipeline, DreamWorker, _DreamRelay, RoleRouter]:
    """Serving capture funnel: strip -> score -> pool -> stamp/write over the
    resolved storage stack. /ingest stays submit-only; the funnel drains on
    /session/end (v1 drain trigger, off the /ingest hot path).
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
    )
    worker = DreamWorker(trigger)
    relay = _DreamRelay(worker)
    snapshotter.on_ready = pipeline.on_snapshot_ready
    for snapshot in snapshotter.recover():
        snapshotter.adopt(snapshot)
        boundary = resume_boundary(snapshot)
        if boundary == "reflect":
            trigger.resume(snapshot.profile_id, snapshot.turn_range)
        elif boundary == "merge":
            trigger.resume_merge(snapshot.profile_id, snapshot.turn_range)
        pipeline.run(snapshot)
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
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    host = urlparse(config.baseurl).hostname
    if not _is_loopback_host(host):
        raise RuntimeError(
            f"daemon refuses non-loopback baseurl {config.baseurl!r}: the local MVP "
            "is localhost-only (no identity/accounts/tokens)"
        )
    stores = build_stores(config)
    app.state.config = config
    app.state.stores = stores
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
    ) = _build_capture(stores, config, app.state.configwrite)
    app.state.segmenter = TurnSegmenter(app.state.capture)
    # Dream chain thread (T1a): the capture/memory surface never blocks on a
    # dream. Started here so the worker consumes events from boot onward.
    app.state.dream_worker.start()
    # Memory surface (T4): one retrieval engine whose track executor is shut
    # down in teardown, before the stores close.
    app.state.memory = MemoryService(stores, config)
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
    app.state.scheduler = DreamScheduler(stores, config, trigger=scheduler_trigger)
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
    # QA-4: drain the capture lane BEFORE anything closes — restart mid-reply
    # must not lose the last exchange. Open turns close off the hot path, then
    # every buffered session drains into the stores they belong to.
    segmenter = getattr(app.state, "segmenter", None)
    capture = getattr(app.state, "capture", None)
    drain = getattr(capture, "drain", None)
    if segmenter is not None and drain is not None and capture is not None:
        segmenter.flush_all()
        for session_id in capture.sessions():
            drain(session_id)
    # Stop the background loops before the stores close (lifecycle order).
    for task_name in ("decay_task", "scheduler_task"):
        task = getattr(app.state, task_name, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    app.state.memory.close()
    # Drain an in-flight dream before the stores close (the chain uses the
    # storage stack); new event submissions stop with the cancelled scheduler.
    await app.state.dream_worker.stop()
    await stores.close()


def create_app() -> FastAPI:
    app = FastAPI(title="MnemoSeed Local", version=__version__, lifespan=lifespan)
    # Capture intake lives per-app state; F1 (StrippingPipeline) drains the
    # same pipeline instance the /ingest router hands turns to, on the
    # consumer side of the seam so the HTTP path stays O(1).
    app.state.capture = StrippingPipeline()
    app.state.segmenter = TurnSegmenter(app.state.capture)
    app.include_router(ingest_router)
    app.include_router(memory_router)
    app.include_router(configwrite_router)

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

    return app


app = create_app()
