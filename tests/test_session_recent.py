"""B2: the time-ordered session-resume surface (design: continue a new session
where the last one ended, by TIME not by semantic query).

Daemon ``POST /session/recent`` returns the most recent sessions' chunk tails
verbatim: newest session group first, chunks inside each group ascending
(reading order). The endpoint never guesses which session is "closed" — the
caller sees at most ``sessions`` groups and recognizes its own current one as
the still-growing newest group (preferred over an arbitrary cut).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.daemon.memory import _group_session_tails
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.schema.turn import HostId
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

PROFILE = "default"

# test_registry.py clears the driver registries wholesale; any daemon-booting
# module ordered after it must defensively re-register (test_preset_embedded
# precedent).
_DRIVERS = (
    (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
    (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
    (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
    (EMBED_DRIVERS, bge_m3_onnx.BgeM3OnnxEmbedder),
    (EMBED_DRIVERS, synthetic_embedder.SyntheticEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_registered():
    for registry, cls in _DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


# ---------------------------------------------------------------- grouping (pure)


def _stamp(chunk_id: str, session: str, ingested_at: float, text: str, turn: int = 0) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id=None,
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id=session, source="manual"),
        turn_start=turn,
        turn_end=turn,
        ingested_at=ingested_at,
    )


def test_group_session_tails_orders_groups_recent_first_and_chunks_ascending() -> None:
    """The store feeds ingested_at-desc rows: newest session group first, and
    each group's tail in ascending (reading) order."""
    chunks = [
        _stamp("c4", "s2", 40.0, "second session tail"),
        _stamp("c3", "s2", 30.0, "second session head"),
        _stamp("c2", "s1", 20.0, "first session tail"),
        _stamp("c1", "s1", 10.0, "first session head"),
    ]
    groups = _group_session_tails(chunks, per_session=20, sessions=2)
    assert [g["session_id"] for g in groups] == ["s2", "s1"]
    assert [c["text"] for c in groups[0]["chunks"]] == ["second session head", "second session tail"]
    assert [c["text"] for c in groups[1]["chunks"]] == ["first session head", "first session tail"]
    assert groups[0]["latest_at"] == 40.0


def test_group_session_tails_caps_the_tail_not_the_head() -> None:
    chunks = [
        _stamp("c3", "s1", 30.0, "newest"),
        _stamp("c2", "s1", 20.0, "middle"),
        _stamp("c1", "s1", 10.0, "oldest"),
    ]
    groups = _group_session_tails(chunks, per_session=2, sessions=5)
    assert [c["text"] for c in groups[0]["chunks"]] == ["middle", "newest"]


def test_group_session_tails_respects_the_session_cap_and_empty_input() -> None:
    chunks = [
        _stamp("c3", "s3", 30.0, "x"),
        _stamp("c2", "s2", 20.0, "x"),
        _stamp("c1", "s1", 10.0, "x"),
    ]
    groups = _group_session_tails(chunks, per_session=5, sessions=2)
    assert [g["session_id"] for g in groups] == ["s3", "s2"]
    assert _group_session_tails([], per_session=5, sessions=2) == []


# ---------------------------------------------------------------- daemon integration


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
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


def _ingest(client: TestClient, session_id: str, ts: float, text: str) -> None:
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


def test_session_recent_returns_both_session_tails_verbatim_in_order(config_path: Path) -> None:
    """The new-session seam: two drained sessions come back newest-group-first,
    chunks ascending, text verbatim — exactly what an agent needs to re-anchor
    on the previous conversation's tail."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-old", 1.0, "上次我们敲定了 verify 回退语义")
        _ingest(client, "sess-old", 2.0, "还给 gemma4:e4b 定了校验位")
        client.post("/session/end", json={"session_id": "sess-old", "profile_id": PROFILE})
        _ingest(client, "sess-new", 3.0, "现在开始做窗口守卫")
        client.post("/session/end", json={"session_id": "sess-new", "profile_id": PROFILE})

        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["profile_id"] == PROFILE
        sessions = payload["sessions"]
        assert [s["session_id"] for s in sessions] == ["sess-new", "sess-old"]
        # the verbatim channel stores turns with their role label — the prefix
        # is exactly what an agent re-anchoring on a conversation tail wants
        assert [c["text"] for c in sessions[0]["chunks"]] == ["user: 现在开始做窗口守卫"]
        assert [c["text"] for c in sessions[1]["chunks"]] == [
            "user: 上次我们敲定了 verify 回退语义",
            "user: 还给 gemma4:e4b 定了校验位",
        ]
        for group in sessions:
            assert group["latest_at"] > 0
            for chunk in group["chunks"]:
                assert chunk["chunk_id"]
                assert chunk["ingested_at"] > 0


def test_session_recent_honors_the_caps(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        for i in range(3):
            _ingest(client, f"s{i}", float(i + 1), f"turn {i}")
            client.post("/session/end", json={"session_id": f"s{i}", "profile_id": PROFILE})
        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "sessions": 1, "per_session": 5},
        )
        assert body.status_code == 200, body.text
        sessions = body.json()["sessions"]
        assert [s["session_id"] for s in sessions] == ["s2"]


def test_session_recent_empty_profile_returns_no_groups(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        assert body.json() == {"profile_id": PROFILE, "sessions": []}


def test_session_recent_rejects_out_of_range_caps(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        body = client.post("/session/recent", json={"profile_id": PROFILE, "per_session": 0})
        assert body.status_code == 422
        body = client.post("/session/recent", json={"profile_id": PROFILE, "sessions": 99})
        assert body.status_code == 422
