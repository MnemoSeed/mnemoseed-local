"""Durable ingest wiring and boot replay (PRD-409 S4).

A 202 must survive a hard kill, an applied event must never be replayed, and
the seen/applied split must be independent of the client import ledger.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import auth_support
import httpx
import pytest
from _ports import unused_loopback_port
from fastapi.testclient import TestClient
from test_import_core import RUN_A, _rollout_path, _source, _write_rollout

from mnemoseed_local.daemon import app as app_module
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.daemon.ingest_queue import IngestQueue
from mnemoseed_local.importer.ledger import ImportLedger
from mnemoseed_local.importer.runner import run_import
from mnemoseed_local.rest_client import ACTOR_HEADER
from mnemoseed_local.schema.turn import (
    HostId,
    IngestEvent,
    IngestEventType,
    MessageContent,
)
from mnemoseed_local.storage.ports import (
    ChunkFilter,
    IngestStream,
    Page,
)

pytest_plugins = ["test_credit_pool_suppression"]

PROFILE = "default"
SESSION = "sess-409-recovery"
TEXT = "a durable user turn that must survive a hard kill"
DAEMON_LOG_NAME = "daemon.log"


def _detach_daemon_log_handler() -> None:
    target = logging.getLogger("mnemoseed_local")
    for handler in list(target.handlers):
        if getattr(handler, "name", None) == DAEMON_LOG_NAME:
            target.removeHandler(handler)
            handler.close()


@asynccontextmanager
async def _kill_lifespan(app: Any) -> Any:
    """Boot fully, then exit without the daemon's graceful drain."""
    cm = app_module.lifespan(app)
    await cm.__aenter__()
    try:
        yield
    finally:
        _detach_daemon_log_handler()


@contextmanager
def _killed_daemon(home: Path) -> Any:
    credential = auth_support.seed_credential(home)
    app = create_app()
    app.router.lifespan_context = _kill_lifespan
    with auth_support.isolated_home(home), auth_support.live_server(app, unused_loopback_port()) as base_url:
        headers = auth_support.credential_headers(credential)
        with httpx.Client(base_url=base_url, headers=headers, timeout=10.0) as client:
            yield client


def _event(ordinal: int, text: str = TEXT, session_id: str = SESSION) -> IngestEvent:
    return IngestEvent(
        host=HostId.OPENCODE,
        event=IngestEventType.USER_PROMPT,
        session_id=session_id,
        profile_id=PROFILE,
        ts=float(ordinal) + 1.0,
        content=MessageContent(text=text),
        transcript_ordinal=ordinal,
    )


def _chunk_count(client: TestClient) -> int:
    return int(
        client.app.state.stores.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(limit=1000)).total
    )


def test_hard_kill_after_202_replays_the_turn_on_boot(config_path: Path, tmp_path: Path) -> None:
    home = tmp_path
    with _killed_daemon(home) as client:
        response = client.post("/ingest", json=_event(0).model_dump(mode="json"))
        assert response.status_code == 202
        assert response.json().get("deduped") is not True
        with sqlite3.connect(home / "meta.db") as conn:
            row = conn.execute(
                "SELECT seen_ordinal, applied_ordinal FROM ingest_watermarks "
                "WHERE profile_id = ? AND session_id = ? AND rollout_id = ''",
                (PROFILE, SESSION),
            ).fetchone()
        assert row == (0, -1)
    with auth_support.boot(home) as client:
        assert _chunk_count(client) >= 1
        assert client.app.state.ingest_queue.watermark(IngestStream(PROFILE, SESSION, "")) == (0, 0)


def test_seen_but_unapplied_event_is_replayed(config_path: Path, tmp_path: Path) -> None:
    home = tmp_path
    with auth_support.boot(home) as client:
        queue: IngestQueue = client.app.state.ingest_queue
        queue.record(_event(3), "hook")
        assert queue.watermark(IngestStream(PROFILE, SESSION, "")) == (3, -1)
    with auth_support.boot(home) as client:
        assert _chunk_count(client) >= 1
        assert client.app.state.ingest_queue.watermark(IngestStream(PROFILE, SESSION, "")) == (3, 3)


def test_replay_of_applied_events_does_not_double_write_or_credit(config_path: Path, tmp_path: Path) -> None:
    home = tmp_path
    with auth_support.boot(home) as client:
        for ordinal in range(3):
            response = client.post("/ingest", json=_event(ordinal).model_dump(mode="json"))
            assert response.status_code == 202
        settle = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE},
        )
        assert settle.status_code == 200
        chunks = _chunk_count(client)
        balance = client.app.state.stores.meta.pool_state(PROFILE).balance
        assert chunks >= 1
        assert balance > 0.0
    with auth_support.boot(home) as client:
        assert _chunk_count(client) == chunks
        assert client.app.state.stores.meta.pool_state(PROFILE).balance == balance


class _Adapter:
    def __init__(self, client: TestClient) -> None:
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {ACTOR_HEADER: "cli"}

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.get(path, params=params, headers=self._headers())
        assert response.status_code < 400
        return response.json() if response.content else {}

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.post(path, json=body, headers=self._headers())
        assert response.status_code < 400
        return response.json() if response.content else {}


def test_settled_rollout_rerun_is_deduped_without_new_chunks(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    root = tmp_path / "sessions"
    _write_rollout(
        _rollout_path(root, RUN_A),
        conversation_id="conv-409",
        turns=((0, "user", "first durable historical prompt"), (1, "assistant", "a distinct reply")),
    )
    with auth_support.boot(home) as client:
        first = run_import(
            source=_source(),
            root=root,
            profile_id=PROFILE,
            client=_Adapter(client),
            ledger=ImportLedger("codex_cli", home=home),
        )
        chunks = _chunk_count(client)
        assert first.new_chunk >= 1
    # A fresh client ledger means the importer re-posts every ordinal. The
    # daemon's durable seen watermark must answer deduped for all of them.
    fresh_ledger_home = tmp_path / "fresh-ledger"
    fresh_ledger_home.mkdir()
    with auth_support.boot(home) as client:
        second = run_import(
            source=_source(),
            root=root,
            profile_id=PROFILE,
            client=_Adapter(client),
            ledger=ImportLedger("codex_cli", home=fresh_ledger_home),
        )
        assert second.turns_ingested == 2
        assert second.turns_deduped == 2
        assert second.new_chunk == 0
        assert _chunk_count(client) == chunks


def test_recover_pending_is_explicit_before_the_idle_sweeper(config_path: Path, tmp_path: Path) -> None:
    source = (Path(app_module.__file__).parent / "app.py").read_text(encoding="utf-8")
    assert "recover_pending(" in source
    assert source.index("recover_pending(") < source.index("idle_flush_task = asyncio.create_task")
