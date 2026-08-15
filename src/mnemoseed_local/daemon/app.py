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
    DreamPipeline,
    DreamScheduler,
    DreamTrigger,
    FileSnapshotter,
    Merger,
    ReflectOrchestrator,
    TokenLedger,
    resume_boundary,
)
from mnemoseed_local.llm import RoleRouter
from mnemoseed_local.llm.types import (
    ChatResult,
    DreamLLM,
    HealthReport,
    LLMDriverInfo,
    LLMError,
    LLMUnavailable,
)
from mnemoseed_local.schema.stamp import CognitiveTier
from mnemoseed_local.schema.turn import Turn
from mnemoseed_local.storage.factory import Stores, build_stores
from mnemoseed_local.storage.ports import (
    AuditFilter,
    CapabilityIssue,
    GraphStore,
    Page,
)

logger = logging.getLogger("mnemoseed_local.daemon")

# The single dream role the reflect boundary runs (A2 MVP): the role stays a
# pipeline-internal param so a future deep/short split can re-open it.
_REFLECT_ROLE = "dream"

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
    """Per-write encoding context on the serving path (FR-1.6)."""
    return WriteContext(
        profile_id=turn.profile_id,
        host=turn.host.value,
        cognitive_tier=CognitiveTier.TIER_1,
    )


class _DreamRelay:
    """Deferred dream-event delivery off the scoring hot path.

    The ScorePool fires dream events while the ScoringPipeline is still scoring
    a drained session — before the WritingPipeline has persisted that session's
    chunks to the vector store. A dream launched at that instant would capture
    an empty snapshot and its safe-clear would purge nothing. The relay instead
    collects the fired events and, once the drain wrote the chunks (the daemon
    flushes after ``WritingPipeline.drain``), hands them to the trigger in
    order. Manual-first (FR-2.8) is untouched: with auto_trigger=False the relay
    simply delivers events the trigger records as pending-manual.
    """

    def __init__(self, trigger: DreamTrigger) -> None:
        self._trigger = trigger
        self._pending: deque[PoolEvent] = deque()

    def handle(self, event: PoolEvent) -> None:
        """ScorePool sink seam: buffer a fired dream event during the drain."""
        self._pending.append(event)

    def flush(self) -> None:
        """Deliver buffered events to the trigger, in fire order."""
        while self._pending:
            self._trigger.handle_event(self._pending.popleft())


def _build_capture(
    stores: Stores, config: Config, configwrite: ConfigWriteService
) -> tuple[WritingPipeline, DreamTrigger, DreamPipeline, _DreamRelay, RoleRouter]:
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
        logger.warning(
            "no 'isolated' graph instance configured; tier-3 output is stranded "
            "(the salvage review channel still captures the entry)"
        )
    router = RoleRouter(
        routes=config.llm,
        audit=stores.meta,
        generation=configwrite.generation_for,
        secrets=None,
    )

    def _resolve_dream_llm() -> DreamLLM:
        return _build_dream_llm(router)

    reflector = ReflectOrchestrator(
        llm=_resolve_dream_llm(),
        resolve_llm=_resolve_dream_llm,
        directory=snapshotter.directory,
        on_done=trigger.on_reflect_complete,
        on_unavailable=_reflect_unavailable,
        on_run_started=lambda run_id, model: stores.meta.update_dream_run_model(run_id, model),
        ledger=TokenLedger(meta=stores.meta, budget_usd=config.dream.token_budget_usd),
    )
    merger = Merger(
        graph_main=stores.graph,
        graph_isolated=graph_isolated,
        meta=stores.meta,
        on_committed=trigger.on_merge_committed,
    )
    pipeline = DreamPipeline(trigger=trigger, snapshotter=snapshotter, reflector=reflector, merger=merger)
    relay = _DreamRelay(trigger)
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
    # balance, so the scheduler cannot double-service the same window.
    pool = ScorePool(
        clock=time.monotonic,
        backend=stores.meta,
        sink=relay.handle,
        dream_threshold=config.dream.floor_pool_points,
        idle_window_sec=config.dream.idle_min_sec,
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
        app.state.dream_relay,
        app.state.role_router,
    ) = _build_capture(stores, config, app.state.configwrite)
    app.state.segmenter = TurnSegmenter(app.state.capture)
    # Memory surface (T4): one retrieval engine whose track executor is shut
    # down in teardown, before the stores close.
    app.state.memory = MemoryService(stores, config)
    # Decay sweep (PRD-04 FR-4.1 / FR-4.4): the daemon-owned background loop
    # over the live config (λ / interval / enabled re-read each tick).
    app.state.decay = DecaySweeper(stores, config)
    app.state.decay_task = asyncio.create_task(app.state.decay.run_forever())
    # A2 dream schedule (FR-2.1 / FR-2.4): the score-pool trigger rules
    # (floor+idle / hard deadline) are re-read from the config each tick, so a
    # configwrite change hot-applies without a restart.
    app.state.scheduler = DreamScheduler(stores, config, trigger=app.state.dream)
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
    for task_name in ("decay_task", "scheduler_task"):
        task = getattr(app.state, task_name, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    app.state.memory.close()
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
