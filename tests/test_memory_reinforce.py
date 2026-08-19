"""B2.1 T3: the consumption-evidence reinforcement endpoint (POST
/memory/reinforce).

The hook's consumption guard attests that injected content was actually cited
by the assistant; the daemon counts that as a real usage event through the
existing Reinforcer (FR-4.2 rebound, TA-6: being injected is not being used).
Unknown ids are tolerated silently (concurrently purged targets never fail the
caller); both target lists empty is a validation error; the chunk side's
observable anchor is ``last_reinforced``, the node side's is ``hit_count``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import Provenance
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


def _ingest_and_settle(client: TestClient, session_id: str, ts: float, text: str) -> None:
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
    settled = client.post("/session/end", json={"session_id": session_id, "profile_id": PROFILE})
    assert settled.status_code == 200, settled.text


def test_reinforce_chunk_path_marks_last_reinforced(config_path: Path) -> None:
    """The hook's attested chunk usage lands on the store: the exact chunks it
    named come back with a refreshed ``last_reinforced`` baseline."""
    app = create_app()
    with TestClient(app) as client:
        _ingest_and_settle(client, "sess-a", 1.0, "消费证据守卫验证")
        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        chunk_ids = [c["chunk_id"] for g in body.json()["sessions"] for c in g["chunks"]]
        assert chunk_ids

        response = client.post(
            "/memory/reinforce",
            json={"profile_id": PROFILE, "chunk_ids": chunk_ids, "node_ids": []},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok"}

        for chunk_id in chunk_ids:
            chunk = app.state.stores.vector.get_chunk(chunk_id)
            assert chunk is not None
            assert chunk.last_reinforced is not None, "a hit must refresh the reinforcement baseline"


def test_reinforce_node_path_counts_hit(config_path: Path) -> None:
    """The graph side of a consumption event counts the usage (FR-3.7
    hit_count) and refreshes the node's baseline through the full-node port."""
    app = create_app()
    with TestClient(app) as client:
        node = GraphNode(
            profile_id=PROFILE,
            node_type=NodeType.PREFERENCE,
            props={
                "domain": "test",
                "statement": "偏好测试",
                "valence": 0.5,
                "prior_width": 0.5,
                "trait_anchor": "anchor",
                "evidence_chain": [],
            },
            provenance=Provenance(asserted_by="user", source="test"),
        )
        app.state.stores.graph.upsert_node(node)
        response = client.post(
            "/memory/reinforce",
            json={"profile_id": PROFILE, "chunk_ids": [], "node_ids": [node.node_id]},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok"}
        refreshed = app.state.stores.graph.get_node(node.node_id)
        assert refreshed is not None
        assert refreshed.hit_count == 1


def test_reinforce_unknown_ids_is_silently_tolerated(config_path: Path) -> None:
    """A concurrently-purged target must never fail the caller: unknown ids
    are ignored silently by the Reinforcer (standing contract)."""
    with TestClient(create_app()) as client:
        response = client.post(
            "/memory/reinforce",
            json={"profile_id": PROFILE, "chunk_ids": ["no-such-chunk"], "node_ids": []},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok"}


def test_reinforce_rejects_empty_targets_and_oversized_lists(config_path: Path) -> None:
    """Both target lists empty is a validation error (422); each list is
    capped at 64 ids."""
    with TestClient(create_app()) as client:
        empty = client.post(
            "/memory/reinforce",
            json={"profile_id": PROFILE, "chunk_ids": [], "node_ids": []},
        )
        assert empty.status_code == 422
        too_many = client.post(
            "/memory/reinforce",
            json={"profile_id": PROFILE, "chunk_ids": [str(i) for i in range(65)], "node_ids": []},
        )
        assert too_many.status_code == 422
