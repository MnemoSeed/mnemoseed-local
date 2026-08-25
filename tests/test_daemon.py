"""Trimmed A2 daemon smoke: real boot over embedded stores (synthetic embedder,
stub dream LLM), the /healthz probe, the capture -> recall -> dream -> config
-> audit loop, and the non-loopback boot refusal.

No identity/accounts/tokens: every memory route takes an explicit profile_id
and the daemon refuses a non-loopback baseurl at boot.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
from mnemoseed_local.daemon.app import DreamWorker, create_app
from mnemoseed_local.dream import DreamTrigger, SnapshotResult, load_snapshot_file
from mnemoseed_local.schema.turn import HostId
from mnemoseed_local.storage.ports import TurnRange

PROFILE = "default"
SESSION = "sess-daemon"

DURABLE_TEXT = "我决定以后都用 pnpm 管理依赖"
PLAIN_TEXT = "我决定构建脚本一律用 uv 启动服务"


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


def _boot(config_path: Path) -> TestClient:
    return TestClient(create_app())


def test_healthz_after_real_boot(config_path: Path) -> None:
    with _boot(config_path) as client:
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["preset"] == "embedded"
        assert body["migrations"]["main"] >= 1  # the meta migrations ran
        assert body["gate"]["ok"] is True
        health = client.get("/health").json()
        assert health["drivers"]["embed"] == "synthetic"


def test_shutdown_releases_the_global_daemon_log_handler(config_path: Path) -> None:
    """Lifespan teardown must release the process-global named daemon.log
    handler it attached: a released handler keeps daemon.log deletable and
    stops a torn-down boot from bleeding into a later boot's logs."""
    target = logging.getLogger("mnemoseed_local")

    def named_handlers() -> list[logging.Handler]:
        return [h for h in target.handlers if getattr(h, "name", None) == "daemon.log"]

    for handler in named_handlers():  # isolate from handlers earlier boots left attached
        target.removeHandler(handler)
    with _boot(config_path):
        assert named_handlers(), "the lifespan never attached the daemon.log handler"
    assert not named_handlers(), "shutdown kept the global daemon.log handler attached"


def test_ingest_scan_runs_on_daemon_pool(config_path: Path) -> None:
    """B2.1 T2 focal scan runs on the daemon scan pool (F2 根治 D4): a
    user_prompt ingest spawns a daemon mnemoseed-scan worker and never any
    AnyIO worker thread — a non-daemon pool thread would outlive the process
    (the F2 join-hang shape). A mutant reintroducing anyio.to_thread spawns an
    AnyIO worker thread and fails the new-names assert."""
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "[capture]\nauto_recall = true\n",
        encoding="utf-8",
    )
    with _boot(config_path) as client:
        before = {t.name for t in threading.enumerate()}
        response = client.post(
            "/ingest",
            json={
                "host": HostId.CLAUDE_CODE.value,
                "event": "user_prompt",
                "session_id": SESSION,
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": DURABLE_TEXT},
            },
        )
        assert response.status_code == 202, response.text
        new_names = {t.name for t in threading.enumerate()} - before
    scan_workers = [t for t in threading.enumerate() if t.name.startswith("mnemoseed-scan-")]
    assert scan_workers, "no mnemoseed-scan daemon worker spawned by the ingest"
    assert all(t.daemon for t in scan_workers), "scan workers must be daemon threads"
    assert not any(name.startswith("AnyIO worker thread") for name in new_names), (
        f"the scan ran on a non-daemon AnyIO worker: {new_names}"
    )


def test_non_loopback_baseurl_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\nbaseurl = "http://192.168.1.10:7788"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    app = create_app()
    with pytest.raises(RuntimeError, match="non-loopback"):
        with TestClient(app):
            pass


def test_boot_refuses_without_isolated_graph_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: the isolated graph instance is mandatory at boot — a config without
    it refuses startup with a clear error (and a fix hint), never a silent
    downgrade that strands tier-3 output."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    app = create_app()
    with pytest.raises(RuntimeError, match="isolated"):
        with TestClient(app):
            pass


def _spy_scorepool(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace the daemon's ScorePool with a recording subclass so the boot
    wiring can be asserted without sleeping. The real pool still runs, so the
    daemon stays fully operational."""
    import mnemoseed_local.daemon.app as app_module
    from mnemoseed_local.capture.pool import ScorePool as RealScorePool

    seen: dict[str, object] = {}

    class _SpyPool(RealScorePool):
        def __init__(self, *args, **kwargs):
            seen["dream_threshold"] = kwargs.get("dream_threshold")
            seen["idle_window_sec"] = kwargs.get("idle_window_sec")
            seen["forced_cap"] = kwargs.get("forced_cap")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(app_module, "ScorePool", _SpyPool)
    return seen


def test_pool_thresholds_come_from_config(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture pool is constructed from the config keys, never fixed
    literals: dream_threshold <- dream.floor_pool_points, idle_window_sec <-
    dream.idle_min_sec (the live 900s default), forced_cap <- the 50.0 default
    of dream.pool_forced_cap."""
    seen = _spy_scorepool(monkeypatch)
    with _boot(config_path) as client:
        assert client.get("/healthz").json()["status"] == "ok"
    assert seen["dream_threshold"] == 10.0  # config.dream.floor_pool_points
    assert seen["idle_window_sec"] == 900.0  # config.dream.idle_min_sec
    assert seen["forced_cap"] == 50.0  # config.dream.pool_forced_cap


def test_pool_thresholds_follow_a_changed_config_on_next_boot(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config edit applies at the NEXT boot: the pool is rebuilt from the
    live config, so a new floor / idle window / forced cap is in effect
    immediately."""
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "[dream]\nfloor_pool_points = 4.0\nidle_min_sec = 60.0\npool_forced_cap = 20.0\n",
        encoding="utf-8",
    )
    seen = _spy_scorepool(monkeypatch)
    with _boot(config_path) as client:
        assert client.get("/healthz").json()["status"] == "ok"
    assert seen["dream_threshold"] == 4.0
    assert seen["idle_window_sec"] == 60.0
    assert seen["forced_cap"] == 20.0


def test_dream_outcome_seam_wired_to_scheduler(config_path: Path) -> None:
    """A2.5 T1 backoff wiring: the dream pipeline's outcome seam is bound to the
    scheduler, so a REAL reflect/merge failure reports back into the retry
    state (the report travels through the worker thread, never the event loop's
    trigger path)."""
    from mnemoseed_local.dream import DreamScheduler

    with _boot(config_path) as client:
        app = client.app
        seam = app.state.dream_pipeline.on_outcome
        assert seam is not None
        assert getattr(seam, "__self__", None) is app.state.scheduler
        assert getattr(seam, "__func__", None) is DreamScheduler.report_outcome


def test_capture_recall_dream_config_audit_loop(config_path: Path) -> None:
    with _boot(config_path) as client:
        # ---- capture: one durable user turn, then settle (drain + relay)
        response = client.post(
            "/ingest",
            json={
                "host": HostId.CLAUDE_CODE.value,
                "event": "user_prompt",
                "session_id": SESSION,
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": DURABLE_TEXT},
            },
        )
        assert response.status_code == 202, response.text
        settled = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE},
        )
        assert settled.status_code == 200, settled.text

        # ---- config read + write (loopback-only, hot-apply)
        config = client.get("/api/v1/config").json()["config"]
        assert config["dream"]["auto_trigger"] is True
        result = client.post(
            "/api/v1/config/set",
            json={"key_path": "dream.floor_pool_points", "value": 4.0},
            headers={"X-MnemoSeed-Actor": "cli"},
        ).json()
        assert result["ok"] is True
        assert result["restart_required"] is False
        assert client.get("/api/v1/config").json()["config"]["dream"]["floor_pool_points"] == 4.0

        # ---- recall: the captured chunk is listable (verbatim track)
        recall = client.post(
            "/memory/recall",
            json={"profile_id": PROFILE, "query": "pnpm", "top_k": 5},
        ).json()
        assert recall["memory"]["entries"], recall

        # ---- dream --once: manual cycle, one HTTP POST
        dream = client.post(
            "/memory/dream_once",
            json={"profile_id": PROFILE},
        ).json()
        assert "launched" in dream
        status = client.post(
            "/memory/dream_status",
            json={"profile_id": PROFILE},
        ).json()
        assert status["profile_id"] == PROFILE

        # ---- audit read: the config write was attributed to the CLI surface
        audit = client.get("/api/v1/audit", params={"action": "config.set"}).json()
        assert audit["total"] >= 1
        assert any(item["actor"] == "cli" for item in audit["items"])


def test_daemon_write_context_fills_entity_cues_from_turn_text() -> None:
    """D2 writer fill (live-drain finding): capture-written chunks must carry
    the entity cues the recall-side entity gate reads — the /memory/remember
    path already extracts cues from the pinned text; the funnel path must not
    write entity-less stamps, or every entity-bearing query excludes them."""
    from mnemoseed_local.daemon.app import _daemon_write_context
    from mnemoseed_local.schema.turn import Turn, TurnRole, TurnStep

    turn = Turn(
        turn_index=0,
        session_id="s1",
        profile_id=PROFILE,
        host=HostId.OPENCODE,
        started_at=1.0,
        steps=[
            TurnStep(role=TurnRole.USER, content="以后调试 MnemoSeed capture 链路一律先检查缓存"),
        ],
    )

    ctx = _daemon_write_context(turn)

    assert "MnemoSeed" in ctx.entities


def test_daemon_write_context_fills_tool_cues_from_turn_steps() -> None:
    """Option C (encoding specificity R4): capture must store the tool cues the
    retrieval side matches on. Only query-matchable names (the _is_tool_name
    shapes the query extractor recognises: camelCase/snake_case/kebab/MCP)
    travel into WriteContext.tools_used — casefold-deduped, first-occurrence
    order — so the hybrid β_tool overlap term is no longer dead code, and a
    common lowercase host name (bash) is never stored verbatim where the query
    side could not match it."""
    from mnemoseed_local.daemon.app import _daemon_write_context
    from mnemoseed_local.schema.turn import Turn, TurnRole, TurnStep

    turn = Turn(
        turn_index=0,
        session_id="s1",
        profile_id=PROFILE,
        host=HostId.OPENCODE,
        started_at=1.0,
        steps=[
            TurnStep(role=TurnRole.USER, content="跑一遍测试"),
            TurnStep(role=TurnRole.TOOL, tool_name="bash"),  # not matchable -> filtered
            TurnStep(role=TurnRole.TOOL, tool_name="runTests"),  # camelCase
            TurnStep(role=TurnRole.TOOL, tool_name="RunTests"),  # casefold duplicate
            TurnStep(role=TurnRole.TOOL, tool_name="run_tests"),  # snake_case
            TurnStep(role=TurnRole.TOOL, tool_name="run-tests"),  # kebab
            TurnStep(role=TurnRole.TOOL, tool_name="github__create_issue__create"),  # MCP
            TurnStep(role=TurnRole.TOOL),  # nameless TOOL step -> skipped
            TurnStep(role=TurnRole.ASSISTANT, content="全绿"),
        ],
    )

    ctx = _daemon_write_context(turn)

    assert ctx.tools_used == (
        "runTests",
        "run_tests",
        "run-tests",
        "github__create_issue__create",
    )


def test_daemon_write_context_caps_tool_cues_at_retrieval_budget() -> None:
    """The capture-side fill shares the retrieval cue budget (tools_cap), so a
    tool-heavy turn never stores more names than the query side can match."""
    from mnemoseed_local.daemon.app import _daemon_write_context
    from mnemoseed_local.retrieve.cues import CueConfig
    from mnemoseed_local.schema.turn import Turn, TurnRole, TurnStep

    cap = CueConfig().tools_cap
    turn = Turn(
        turn_index=0,
        session_id="s1",
        profile_id=PROFILE,
        host=HostId.OPENCODE,
        started_at=1.0,
        steps=[TurnStep(role=TurnRole.TOOL, tool_name=f"tool-{i:02d}") for i in range(cap + 3)],
    )

    ctx = _daemon_write_context(turn)

    assert len(ctx.tools_used) == cap
    assert ctx.tools_used == tuple(f"tool-{i:02d}" for i in range(cap))


def test_daemon_write_context_carries_turn_origin_agent() -> None:
    """The serving write context propagates the segmenter's turn-level origin
    attribution so the stamp writer can land it on its own inert column."""
    from mnemoseed_local.daemon.app import _daemon_write_context
    from mnemoseed_local.schema.turn import Turn, TurnRole, TurnStep

    turn = Turn(
        turn_index=0,
        session_id="s1",
        profile_id=PROFILE,
        host=HostId.OPENCODE,
        started_at=1.0,
        origin_agent="build",
        steps=[TurnStep(role=TurnRole.USER, content="以后都用 pnpm")],
    )

    ctx = _daemon_write_context(turn)

    assert ctx.origin_agent == "build"


def test_capture_recall_with_entity_bearing_query(config_path: Path) -> None:
    """D2 end-to-end on the serving surface: a durable turn drained into the
    store must be reachable by a query whose cues extract entities (the
    scenario that 100%-missed before the fix)."""
    with _boot(config_path) as client:
        response = client.post(
            "/ingest",
            json={
                "host": HostId.CLAUDE_CODE.value,
                "event": "user_prompt",
                "session_id": SESSION,
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": "以后调试 MnemoSeed capture 链路一律先检查缓存完整性"},
            },
        )
        assert response.status_code == 202, response.text
        settled = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE},
        )
        assert settled.status_code == 200, settled.text

        recall = client.post(
            "/memory/recall",
            json={"profile_id": PROFILE, "query": "MnemoSeed 缓存完整性", "top_k": 5},
        ).json()
        assert recall["memory"]["entries"], recall


def test_capture_stamps_epoch_domain_timestamps(config_path: Path) -> None:
    """D3 serving-surface assertion: a turn drained through the daemon must
    land epoch-domain ``ingested_at`` so the decay sweeper's epoch baseline
    never misreads it (live observed: monotonic stamp -> sweep zeroed the
    fresh chunk)."""
    import lancedb

    with _boot(config_path) as client:
        response = client.post(
            "/ingest",
            json={
                "host": HostId.CLAUDE_CODE.value,
                "event": "user_prompt",
                "session_id": SESSION,
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": DURABLE_TEXT},
            },
        )
        assert response.status_code == 202, response.text
        settled = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE},
        )
        assert settled.status_code == 200, settled.text

        db = lancedb.connect(str(config_path.parent / "chunks.lance"))
        arrow = db.open_table("chunks").to_arrow()
        ingested_at = float(arrow.column("ingested_at").to_pylist()[-1])
        assert abs(ingested_at - time.time()) < 300


def test_recall_entries_carry_session_provenance_and_iso_ingested_at(config_path: Path) -> None:
    """Chunk recall entries report their verbatim session id and an ISO-8601
    UTC ingest time (never a raw epoch float) — the comparative structure the
    time-comparison surface needs."""
    _ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")

    with _boot(config_path) as client:
        response = client.post(
            "/ingest",
            json={
                "host": HostId.CLAUDE_CODE.value,
                "event": "user_prompt",
                "session_id": SESSION,
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": DURABLE_TEXT},
            },
        )
        assert response.status_code == 202, response.text
        settled = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE},
        )
        assert settled.status_code == 200, settled.text

        recall = client.post(
            "/memory/recall",
            json={"profile_id": PROFILE, "query": "pnpm", "top_k": 5},
        ).json()
        entries = recall["memory"]["entries"]
        chunk_entries = [entry for entry in entries if entry["kind"] == "chunk"]
        assert chunk_entries, entries
        for entry in chunk_entries:
            assert entry["session_id"] == SESSION
            assert _ISO.fullmatch(entry["ingested_at"])


def test_recall_entries_carry_origin_agent_and_host(config_path: Path) -> None:
    """B2.9 recall read-back: chunk entries expose the inert origin agent label
    and the encoding host; a session captured without an agent reports
    consistent nulls (with the host still present) instead of a guessed label."""
    with _boot(config_path) as client:
        attributed = client.post(
            "/ingest",
            json={
                "host": HostId.OPENCODE.value,
                "event": "user_prompt",
                "session_id": SESSION,
                "profile_id": PROFILE,
                "ts": 1.0,
                "agent": "build",
                "content": {"text": DURABLE_TEXT},
            },
        )
        assert attributed.status_code == 202, attributed.text
        settled = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE},
        )
        assert settled.status_code == 200, settled.text

        recall = client.post(
            "/memory/recall",
            json={"profile_id": PROFILE, "query": "pnpm", "top_k": 5},
        ).json()
        chunk_entries = [e for e in recall["memory"]["entries"] if e["kind"] == "chunk"]
        assert chunk_entries, recall
        for entry in chunk_entries:
            assert entry["origin_agent"] == "build"
            assert entry["host"] == HostId.OPENCODE.value

        # The unattributed twin: every recall entry of a plain session renders
        # origin_agent null while the encoding host stays present.
        plain = client.post(
            "/ingest",
            json={
                "host": HostId.CLAUDE_CODE.value,
                "event": "user_prompt",
                "session_id": f"{SESSION}-plain",
                "profile_id": PROFILE,
                "ts": 2.0,
                "content": {"text": PLAIN_TEXT},
            },
        )
        assert plain.status_code == 202, plain.text
        plain_settled = client.post(
            "/session/end",
            json={"session_id": f"{SESSION}-plain", "profile_id": PROFILE},
        )
        assert plain_settled.status_code == 200, plain_settled.text

        plain_recall = client.post(
            "/memory/recall",
            json={"profile_id": PROFILE, "query": "uv", "top_k": 5},
        ).json()
        plain_entries = [
            e
            for e in plain_recall["memory"]["entries"]
            if e["kind"] == "chunk" and e["session_id"] == f"{SESSION}-plain"
        ]
        assert plain_entries, plain_recall
        for entry in plain_entries:
            assert entry["origin_agent"] is None
            assert entry["host"] == HostId.CLAUDE_CODE.value


# ---------------------------------------------------------------- B6 (W-C): drain off the event loop


def test_settle_and_flush_drain_on_dedicated_daemon_thread(config_path: Path) -> None:
    """B6 (W-C): the capture drain runs on a dedicated mnemoseed-drain daemon
    thread, never on the event loop — a store write inside drain() must show up
    as a mnemoseed-drain-* thread name (the pre-fix drain ran inline on the
    loop, blocking every endpoint for its whole duration). Both drain sites
    (/session/end and /flush) use the lane, and the lane worker is a daemon
    thread never registered in the interpreter's atexit join set (F2 根治)."""
    from concurrent.futures import thread as cf_thread

    with _boot(config_path) as client:
        app = client.app
        names: list[str] = []
        original_drain = app.state.capture.drain

        def recording_drain(session_id: str) -> None:
            names.append(threading.current_thread().name)
            original_drain(session_id)

        app.state.capture.drain = recording_drain
        _ingest_turn(client, SESSION, 1.0, DURABLE_TEXT)
        settled = client.post("/session/end", json={"session_id": SESSION, "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        flushed = client.post("/flush", json={"session_id": SESSION, "profile_id": PROFILE})
        assert flushed.status_code == 200, flushed.text
        assert names, "the drain never ran"
        assert all(name.startswith("mnemoseed-drain-") for name in names), (
            f"the drain ran on the event loop: {names}"
        )
        drain_threads = [t for t in threading.enumerate() if t.name.startswith("mnemoseed-drain-")]
        assert drain_threads, "no mnemoseed-drain worker spawned"
        assert all(t.daemon for t in drain_threads), "drain workers must be daemon threads"
        assert all(t not in cf_thread._threads_queues for t in drain_threads), (
            "the drain worker must never join the interpreter's atexit join set"
        )


def test_drain_exception_propagates_to_the_ack(config_path: Path) -> None:
    """B6 (W-C): the ack is the drain's completion — a failing drain surfaces
    its exception into the response path exactly like the pre-fix synchronous
    raise (an honest error on both drain sites, never a swallowed success)."""
    with _boot(config_path) as client:
        calls = {"n": 0}

        def broken_drain(session_id: str) -> None:
            del session_id
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("drain exploded")

        client.app.state.capture.drain = broken_drain
        _ingest_turn(client, "sess-raise-settle", 1.0, DURABLE_TEXT)
        with pytest.raises(RuntimeError, match="drain exploded"):
            client.post("/session/end", json={"session_id": "sess-raise-settle", "profile_id": PROFILE})
        _ingest_turn(client, "sess-raise-flush", 2.0, DURABLE_TEXT)
        with pytest.raises(RuntimeError, match="drain exploded"):
            client.post("/flush", json={"session_id": "sess-raise-flush", "profile_id": PROFILE})
    # the aborted settle never pruned: teardown re-drains the buffered session
    # and must not blow up teardown for a handler-side failure (calls 3+ stay
    # healthy)


# ---------------------------------------------------------------- T1a: dream off the event loop


def _ingest_turn(client: TestClient, session_id: str, ts: float, text: str) -> None:
    response = client.post(
        "/ingest",
        json={
            "host": HostId.CLAUDE_CODE.value,
            "event": "user_prompt",
            "session_id": session_id,
            "profile_id": PROFILE,
            "ts": ts,
            "content": {"text": text},
        },
    )
    assert response.status_code == 202, response.text


def _wait_dream_idle(client: TestClient, timeout: float = 10.0) -> dict:
    """Poll /memory/dream_status until the profile's dream returns to idle."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.post("/memory/dream_status", json={"profile_id": PROFILE}).json()
        if body.get("state") == "idle":
            return body
        time.sleep(0.05)
    raise AssertionError(f"dream never returned to idle; last status: {body}")


def _wait_audit_total(client: TestClient, action: str, minimum: int, timeout: float = 3.0) -> int:
    """Poll /api/v1/audit until ``action`` has at least ``minimum`` rows.

    The dream worker flips the trigger to idle at merge-commit, while the
    dream_committed audit row is appended afterwards on the completion path —
    a single-shot query can observe committed-but-not-yet-audited.
    """
    deadline = time.monotonic() + timeout
    total = 0
    while time.monotonic() < deadline:
        total = client.get("/api/v1/audit", params={"action": action}).json()["total"]
        if total >= minimum:
            return total
        time.sleep(0.05)
    raise AssertionError(f"audit {action!r} never reached {minimum}; last total: {total}")


# ---------------------------------------------------------------- B1 T3: ensemble verify (daemon wiring)


def _enable_ensemble_verify(config_path: Path, *, verifier_table: str) -> None:
    # one [dream] table only (TOML); auto_trigger stays off because this is a
    # MANUAL dream_once scenario (the near-zero floor + idle makes the single
    # captured turn fire the pool event dream_once consumes)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '[dream]\nauto_trigger = false\nensemble = "verify"\nfloor_pool_points = 0.1\nidle_min_sec = 0.0\n'
        + verifier_table,
        encoding="utf-8",
    )


def test_ensemble_verify_judges_the_run_and_audits_the_verdict(config_path: Path) -> None:
    """B1 T3 daemon integration: with dream.ensemble=verify the dream_verifier
    role materializes per run, judges the reflected core triples, and audits
    the verdict — the run itself still commits (verification is a layer, never
    a gate)."""
    _enable_ensemble_verify(
        config_path,
        verifier_table='[dream.llm.dream_verifier]\ndriver = "stub_verifier"\nmodel = "stubjudge"\n',
    )
    with _boot(config_path) as client:
        _ingest_turn(client, SESSION, 1.0, DURABLE_TEXT)
        settled = client.post("/session/end", json={"session_id": SESSION, "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        dream = client.post("/memory/dream_once", json={"profile_id": PROFILE}).json()
        assert dream["launched"] is True
        _wait_dream_idle(client)

        verified = client.get("/api/v1/audit", params={"action": "ensemble_verified"}).json()
        assert verified["total"] == 1, verified
        detail = verified["items"][0]["detail"]
        assert detail["verifier_model"] == "stubjudge"
        assert detail["judged"] >= 1  # the stub-extracted decided marker was judged
        assert detail["rejected"] == 0  # every stub triple is evidence-backed
        # the judged run committed (no fallback, no blocked merge)
        committed = client.get("/api/v1/audit", params={"action": "dream_committed"}).json()
        assert committed["total"] >= 1
        fallback = client.get("/api/v1/audit", params={"action": "ensemble_verify_fallback"}).json()
        assert fallback["total"] == 0
        # triple audit surface 1: the router materialized the judge role
        configured = client.get("/api/v1/audit", params={"action": "llm_role_configured"}).json()
        assert any(item["detail"].get("role") == "dream_verifier" for item in configured["items"])


def test_ensemble_verify_broken_judge_route_falls_back_and_audits(config_path: Path) -> None:
    """B1 T3 (design/01 decision 1): a verifier route that cannot materialize
    degrades typed — the dream ships A's original result, the merge commits,
    and the fallback lands an audit record with the reason."""
    _enable_ensemble_verify(
        config_path,
        verifier_table='[dream.llm.dream_verifier]\ndriver = "no_such_driver"\nmodel = "gone"\n',
    )
    with _boot(config_path) as client:
        _ingest_turn(client, SESSION, 1.0, DURABLE_TEXT)
        settled = client.post("/session/end", json={"session_id": SESSION, "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        dream = client.post("/memory/dream_once", json={"profile_id": PROFILE}).json()
        assert dream["launched"] is True
        _wait_dream_idle(client)

        fallback = client.get("/api/v1/audit", params={"action": "ensemble_verify_fallback"}).json()
        assert fallback["total"] == 1, fallback
        assert fallback["items"][0]["detail"]["reason"] == "llm_unavailable"
        committed = client.get("/api/v1/audit", params={"action": "dream_committed"}).json()
        assert committed["total"] >= 1
        verified = client.get("/api/v1/audit", params={"action": "ensemble_verified"}).json()
        assert verified["total"] == 0


def _enable_ensemble_vote(config_path: Path) -> None:
    # auto_trigger stays off because this is a MANUAL dream_once scenario
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '[dream]\nauto_trigger = false\nensemble = "vote"\nfloor_pool_points = 0.1\nidle_min_sec = 0.0\n'
        + '[dream.llm.dream_vote]\ndriver = "stub"\nmodel = "stub"\n',
        encoding="utf-8",
    )


def test_ensemble_vote_runs_both_seats_and_combines(config_path: Path, tmp_path: Path) -> None:
    """B5 vote daemon wiring (BLOCKER-1): with dream.ensemble=vote the daemon
    must wire the pipeline's live mode AND the vote seat LLM. A dream then runs
    seat A, seat B, and the combiner — journaling BOTH per-seat results and a
    combine-done marker. Before the fix the mode/vote_llm wiring was missing, so
    a vote dream silently ran the single-model reflect path (dead config)."""
    _enable_ensemble_vote(config_path)
    with _boot(config_path) as client:
        _ingest_turn(client, SESSION, 1.0, DURABLE_TEXT)
        settled = client.post("/session/end", json={"session_id": SESSION, "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        dream = client.post("/memory/dream_once", json={"profile_id": PROFILE}).json()
        assert dream["launched"] is True
        _wait_dream_idle(client)

        committed = client.get("/api/v1/audit", params={"action": "dream_committed"}).json()
        assert committed["total"] >= 1

    # the journal (marked MERGE_DONE, never deleted) must carry BOTH seat
    # results and the combine marker — proof the vote chain actually ran
    journal = list(tmp_path.glob("dreams/*.json"))
    assert journal, "no dream journal written"
    snap = load_snapshot_file(journal[0])
    assert snap is not None
    assert snap.vote_results is not None
    assert "a" in snap.vote_results and "b" in snap.vote_results
    phases = set(snap.phases)
    assert "combine_done" in phases
    assert "merge_done" in phases


def test_healthz_and_ingest_stay_responsive_during_dream(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: a dream must never freeze the daemon surface.

    A stub dream LLM that sleeps ~2s per reflect call runs inside the dream
    chain; /healthz and /ingest must answer well under 500ms while that dream
    is in flight. The pre-fix daemon ran the whole snapshot->reflect->merge
    chain synchronously on the event loop, blocking both endpoints for the
    entire dream.
    """
    # lower the pool floor so the single captured turn fires a dream event;
    # auto_trigger stays off because the launched dream must be the MANUAL one
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "[dream]\nauto_trigger = false\nfloor_pool_points = 0.1\nidle_min_sec = 0.0\n",
        encoding="utf-8",
    )
    # swap the reflect stub for a sleeping subclass (the daemon resolves the
    # dream LLM at boot and re-resolves per run through the same module seam)
    import mnemoseed_local.dream.reflect as reflect_module

    real_chat = reflect_module.StubReflectLLM.chat

    class _SlowStubLLM(reflect_module.StubReflectLLM):
        def chat(self, *, system: str, user: str) -> str:
            time.sleep(2.0)
            return real_chat(self, system=system, user=user)

    monkeypatch.setattr(reflect_module, "StubReflectLLM", _SlowStubLLM)

    with _boot(config_path) as client:
        _ingest_turn(client, SESSION, 1.0, DURABLE_TEXT)
        settled = client.post("/session/end", json={"session_id": SESSION, "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text

        # launch the manual dream in the background: its ~2s reflect keeps the
        # dream in flight while the main thread probes the live surface
        outcome: dict[str, object] = {}

        def _dream_once() -> None:
            body = client.post("/memory/dream_once", json={"profile_id": PROFILE}).json()
            outcome["launched"] = body.get("launched")

        thread = threading.Thread(target=_dream_once, daemon=True)
        thread.start()
        time.sleep(0.1)  # let the worker reach the reflect sleep

        latencies: list[float] = []
        deadline = time.monotonic() + 3.0
        probe = 0
        while time.monotonic() < deadline:
            started = time.perf_counter()
            healthz = client.get("/healthz")
            latencies.append(time.perf_counter() - started)
            assert healthz.status_code == 200
            started = time.perf_counter()
            _ingest_turn(client, f"probe-{probe}", 2.0 + probe, DURABLE_TEXT)
            latencies.append(time.perf_counter() - started)
            probe += 1
            time.sleep(0.1)

        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the manual dream never completed"
        assert outcome.get("launched") is True
        assert max(latencies) < 0.5, f"surface blocked during the dream: {latencies}"


def test_manual_dream_while_scheduled_trigger_in_flight_runs_reflect_once(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: a manual dream arriving while a scheduled (auto) dream is in flight
    never double-runs the same snapshot.

    The worker serializes the dream chain (at most one dream at a time) and
    the trigger's overlap guard rejects the manual launch, so the reflect seam
    executes exactly once per scheduled window and the manual run reports
    ``launched: false``.
    """
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "[dream]\nauto_trigger = true\nfloor_pool_points = 0.1\nidle_min_sec = 0.0\n",
        encoding="utf-8",
    )
    import mnemoseed_local.dream.reflect as reflect_module

    real_chat = reflect_module.StubReflectLLM.chat
    calls: list[float] = []

    class _CountingStubLLM(reflect_module.StubReflectLLM):
        def chat(self, *, system: str, user: str) -> str:
            calls.append(time.perf_counter())
            time.sleep(0.4)
            return real_chat(self, system=system, user=user)

    monkeypatch.setattr(reflect_module, "StubReflectLLM", _CountingStubLLM)

    with _boot(config_path) as client:
        # session 1 settles -> pool fires -> the relay flush hands the event to
        # the worker, which auto-launches the first dream (scheduled side)
        _ingest_turn(client, SESSION, 1.0, DURABLE_TEXT)
        settled = client.post("/session/end", json={"session_id": SESSION, "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        # session 2 fires a second event for the same profile while the first
        # dream is in flight (the overflow queue absorbs it, one dream at a time)
        # distinct durable text: an identical sentence would score as a
        # session-repetition (disposable) and never reach the pool
        _ingest_turn(client, "sess-b", 2.0, "我决定把 CI 从 GitHub Actions 迁移到自建服务器")
        settled_b = client.post("/session/end", json={"session_id": "sess-b", "profile_id": PROFILE})
        assert settled_b.status_code == 200, settled_b.text
        # the manual trigger arrives while a dream is in flight: rejected
        manual = client.post("/memory/dream_once", json={"profile_id": PROFILE}).json()
        assert manual["launched"] is False
        _wait_dream_idle(client)
        # exactly one reflect per scheduled window, executed sequentially
        assert len(calls) == 2, f"expected one reflect per scheduled window, saw {len(calls)}"
        assert calls[1] - calls[0] >= 0.4, "dream chains overlapped; the worker did not serialize"


def test_config_set_auto_trigger_off_stops_launches_without_restart(config_path: Path) -> None:
    """Hot-apply contract (FR-2.8): with the daemon live under the shipped
    auto-trigger ON default, flipping ``dream.auto_trigger`` to False through
    the configwrite surface must stop automatic launches on the very next pool
    event — the running daemon never needs a restart for the switch."""
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "[dream]\nauto_trigger = true\nfloor_pool_points = 0.1\nidle_min_sec = 0.0\n",
        encoding="utf-8",
    )
    with _boot(config_path) as client:
        # baseline: while auto_trigger is true, a settled turn auto-launches
        _ingest_turn(client, SESSION, 1.0, DURABLE_TEXT)
        settled = client.post("/session/end", json={"session_id": SESSION, "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        _wait_dream_idle(client)
        committed_before = _wait_audit_total(client, "dream_committed", minimum=1)

        # the runtime off-switch: every reporting surface agrees it is off
        result = client.post(
            "/api/v1/config/set",
            json={"key_path": "dream.auto_trigger", "value": False},
            headers={"X-MnemoSeed-Actor": "cli"},
        ).json()
        assert result["ok"] is True
        assert result["restart_required"] is False
        assert client.get("/api/v1/config").json()["config"]["dream"]["auto_trigger"] is False

        # drive another pool event: held as pending_manual, never launched
        # (distinct text: an identical sentence scores as repetition and never
        # reaches the pool)
        _ingest_turn(client, "sess-b", 2.0, "我决定把 CI 从 GitHub Actions 迁移到自建服务器")
        settled_b = client.post("/session/end", json={"session_id": "sess-b", "profile_id": PROFILE})
        assert settled_b.status_code == 200, settled_b.text

        status: dict[str, Any] = {}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            status = client.post("/memory/dream_status", json={"profile_id": PROFILE}).json()
            if status.get("pending_manual", 0) >= 1 or status.get("state") != "idle":
                break
            time.sleep(0.05)
        assert status.get("pending_manual", 0) >= 1, f"the event was never delivered: {status}"
        assert status.get("state") == "idle", f"a dream launched after the switch-off: {status}"
        committed_after = client.get("/api/v1/audit", params={"action": "dream_committed"}).json()["total"]
        assert committed_after == committed_before, "a dream launched after the switch-off"


class _SlowSnapshotter:
    """Snapshotter whose request blocks on the worker thread, keeping a manual
    dream in flight so the queue can hold a second job behind it."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.entered = threading.Event()

    def request(self, profile_id: str, turn_range: TurnRange) -> SnapshotResult:
        del profile_id, turn_range
        self.entered.set()
        time.sleep(self._delay)
        return SnapshotResult(snapshot=None, ok=True)


def _manual_event() -> PoolEvent:
    """One pool-fired event held as pending_manual for a manual dream run."""
    return PoolEvent(
        kind=PoolEventKind.DREAM_TRIGGER,
        profile_id=PROFILE,
        turn_range=TurnRange(start=0, end=0),
        balance=10.0,
        fired_at=1.0,
    )


async def _wait_snapshot_entered(snapshotter: _SlowSnapshotter, limit: float = 2.0) -> None:
    """Block until the worker reached the snapshotter (in a worker thread)."""
    entered = await asyncio.to_thread(snapshotter.entered.wait, limit)
    assert entered, "manual job never reached the snapshotter"


async def test_worker_stop_resolves_queued_manual_job() -> None:
    """A manual job still queued at shutdown never launched: its future
    resolves False in finite time.

    The pre-fix stop() cancelled the consumer task and never touched the
    queue, leaving the queued job's pending future unresolved forever.
    """
    snapshotter = _SlowSnapshotter(0.5)
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=False)
    trigger.handle_event(_manual_event())
    trigger.handle_event(_manual_event())
    worker = DreamWorker(trigger)
    worker.start()
    first = asyncio.create_task(worker.submit_dream_once(PROFILE))
    await _wait_snapshot_entered(snapshotter)
    second = asyncio.create_task(worker.submit_dream_once(PROFILE))
    await asyncio.sleep(0.05)  # the second job lands in the queue behind the first
    await asyncio.wait_for(worker.stop(), timeout=2.0)
    # the in-flight chain ran to completion during executor shutdown
    assert await asyncio.wait_for(first, timeout=1.0) is True
    # the queued job was never launched: resolved False, not left pending
    assert await asyncio.wait_for(second, timeout=1.0) is False


async def test_worker_stop_waits_for_in_flight_manual_job() -> None:
    """An in-flight manual job's future resolves with the real outcome after
    the chain completes: stop() drains the chain instead of aborting it."""
    snapshotter = _SlowSnapshotter(0.5)
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=False)
    trigger.handle_event(_manual_event())
    worker = DreamWorker(trigger)
    worker.start()
    job = asyncio.create_task(worker.submit_dream_once(PROFILE))
    await _wait_snapshot_entered(snapshotter)
    started = time.monotonic()
    await asyncio.wait_for(worker.stop(), timeout=2.0)
    elapsed = time.monotonic() - started
    # stop() waited for the remaining chain instead of returning immediately
    assert elapsed >= 0.2, f"stop() returned before the chain finished: {elapsed:.3f}s"
    assert await asyncio.wait_for(job, timeout=1.0) is True


async def test_worker_stop_returns_in_finite_time_with_pending_jobs() -> None:
    """stop() itself returns in finite time with both an in-flight and a
    queued manual job pending, and every pending future resolves."""
    snapshotter = _SlowSnapshotter(0.5)
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=False)
    trigger.handle_event(_manual_event())
    trigger.handle_event(_manual_event())
    worker = DreamWorker(trigger)
    worker.start()
    first = asyncio.create_task(worker.submit_dream_once(PROFILE))
    await _wait_snapshot_entered(snapshotter)
    second = asyncio.create_task(worker.submit_dream_once(PROFILE))
    await asyncio.sleep(0.05)
    await asyncio.wait_for(worker.stop(), timeout=2.0)
    assert await asyncio.wait_for(first, timeout=1.0) is True
    assert await asyncio.wait_for(second, timeout=1.0) is False
