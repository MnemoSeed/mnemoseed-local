"""Per-settle funnel accounting: a concurrent drain must not pollute another
settle's response counts."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import auth_support
from fastapi.testclient import TestClient

from mnemoseed_local.rest_client import ACTOR_HEADER
from mnemoseed_local.schema.turn import HostId

pytest_plugins = ["test_credit_pool_suppression"]

PROFILE = "default"


def _ingest(client: TestClient, session: str, text: str) -> None:
    response = client.post(
        "/ingest",
        json={
            "host": HostId.CLAUDE_CODE.value,
            "event": "user_prompt",
            "session_id": session,
            "profile_id": PROFILE,
            "ts": 1.0,
            "content": {"text": text},
        },
        headers={ACTOR_HEADER: "cli"},
    )
    assert response.status_code == 202, response.text


def test_two_overlapping_settles_count_only_their_own_turns(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "sess-a", "first distinct historical prompt about indexing")
        _ingest(client, "sess-a", "second distinct historical prompt about retention")
        _ingest(client, "sess-b", "third distinct historical prompt about eviction")

        original_flush = client.app.state.dream_relay.flush
        gate = asyncio.Event()
        entered = threading.Event()
        calls = 0

        async def gated_flush() -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await gate.wait()
            else:
                gate.set()
            return await original_flush()

        client.app.state.dream_relay.flush = gated_flush
        results: dict[str, dict[str, Any]] = {}

        def settle(session: str) -> None:
            response = client.post("/session/end", json={"session_id": session, "profile_id": PROFILE})
            assert response.status_code == 200, response.text
            results[session] = response.json()

        first = threading.Thread(target=settle, args=("sess-a",))
        first.start()
        assert entered.wait(10)
        settle("sess-b")
        first.join(10)
        assert not first.is_alive()

        assert results["sess-a"]["new_chunk"] == 2
        assert results["sess-b"]["new_chunk"] == 1
