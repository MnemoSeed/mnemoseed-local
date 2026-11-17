"""GET /session/active (#394 S2): a read-only view of the daemon's live capture
buffers so a history importer can skip sessions still in flight."""

from __future__ import annotations

from pathlib import Path

import auth_support
import pytest
from _ports import unused_loopback_port
from fastapi.testclient import TestClient

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
_TEXT = "我决定以后都用 pnpm 管理依赖"

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


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'baseurl = "http://127.0.0.1:{unused_loopback_port()}"\n'
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


def _ingest(client: TestClient, session_id: str, ts: float = 1.0) -> None:
    response = client.post(
        "/ingest",
        json={
            "host": HostId.CLAUDE_CODE.value,
            "event": "user_prompt",
            "session_id": session_id,
            "profile_id": PROFILE,
            "ts": ts,
            "content": {"text": _TEXT},
        },
    )
    assert response.status_code == 202, response.text


def test_active_lists_only_sessions_still_in_the_capture_buffers(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "sess-live", 1.0)
        _ingest(client, "sess-live", 2.0)
        _ingest(client, "sess-settled", 3.0)
        settled = client.post("/session/end", json={"session_id": "sess-settled", "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        response = client.get("/session/active", params={"profile_id": PROFILE})
        assert response.status_code == 200, response.text
        assert set(response.json()["sessions"]) == {"sess-live"}


def test_active_returns_the_buffer_snapshot_without_a_profile_filter(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        client.app.state.capture.sessions = lambda: ("sess-a", "sess-b")
        response = client.get("/session/active", params={"profile_id": PROFILE})
        assert response.status_code == 200, response.text
        assert response.json() == {"sessions": ["sess-a", "sess-b"]}


def test_active_is_read_only(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "sess-live", 1.0)
        _ingest(client, "sess-live", 2.0)
        before = client.app.state.capture.sessions()
        response = client.get("/session/active", params={"profile_id": PROFILE})
        assert response.status_code == 200, response.text
        assert client.app.state.capture.sessions() == before


def test_active_requires_the_profile_query_param(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        assert client.get("/session/active").status_code == 422


def test_active_refuses_a_caller_without_the_credential(config_path: Path) -> None:
    with auth_support.unauthenticated(config_path.parent) as client:
        response = client.get("/session/active", params={"profile_id": PROFILE})
        assert response.status_code == 401
