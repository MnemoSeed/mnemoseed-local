"""Console C-1 T2: POST /memory/atlas + VectorStore.get_dense (CHUNKS-ONLY PCA)."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.graph import GraphNode, NodeType, Provenance
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues


def _config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 8\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 8\n'
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


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _config_path(tmp_path, monkeypatch)
    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _make_chunk(
    profile_id: str,
    text: str,
    dense: list[float],
    decay: float = 0.8,
    ingested_at: float | None = None,
) -> ChunkStamp:
    now = ingested_at if ingested_at is not None else time.time()
    return ChunkStamp(
        chunk_id=uuid.uuid4().hex[:8],
        profile_id=profile_id,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test",
        cues=Cues(entities=["e1", "e2"], project="p"),
        provenance=Provenance(
            asserted_by="test",
            session_id="s1",
            source="session://s1",
            confidence=0.9,
            asserted_at=now,
        ),
        decay_weight=decay,
        score=0.5,
        ingested_at=now,
    )


def _insert_chunk(client: TestClient, profile_id: str, chunk: ChunkStamp, dense: list[float]) -> None:
    stores = client.app.state.stores  # type: ignore[attr-defined]
    stores.vector.upsert_chunk(chunk, dense, None)


def _insert_node(client: TestClient, profile_id: str, node_id: str | None = None) -> str:
    stores = client.app.state.stores  # type: ignore[attr-defined]
    nid = node_id or uuid.uuid4().hex[:8]
    node = GraphNode(
        profile_id=profile_id,
        node_id=nid,
        node_type=NodeType.DECISION,
        entities=["e1"],
        props={"statement": "a" * 200},
        provenance=Provenance(
            asserted_by="test",
            source="session://s1",
            confidence=0.9,
            asserted_at=time.time(),
        ),
        valid_from=time.time(),
        updated_at=time.time(),
    )
    stores.graph.upsert_node(node)
    return nid


# ------------------------------------------------------------------ tests


def test_atlas_empty_profile(client: TestClient) -> None:
    resp = client.post("/memory/atlas", json={"profile_id": "default"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["window_truncated"] is False
    assert data["positions"] is None
    assert data["algo"] == "unavailable"


def test_atlas_limit_over_500_is_422(client: TestClient) -> None:
    resp = client.post("/memory/atlas", json={"profile_id": "default", "limit": 501})
    assert resp.status_code == 422, resp.text


def test_atlas_x_normalized_and_z_is_one_minus_decay(client: TestClient) -> None:
    now = time.time()
    chunks = []
    for i in range(3):
        dense = [float(i), float(i * 2), 0, 0, 0, 0, 0, 0]
        c = _make_chunk("default", f"text {i} " + "x" * 10, dense, decay=0.2 + i * 0.1, ingested_at=now + i)
        chunks.append((c, dense))
    for c, dense in chunks:
        _insert_chunk(client, "default", c, dense)

    resp = client.post("/memory/atlas", json={"profile_id": "default", "limit": 10})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["algo"] == "pca"
    assert data["positions"] is not None
    xs = [v[0] for v in data["positions"].values()]
    assert all(-1.0 - 1e-6 <= x <= 1.0 + 1e-6 for x in xs), xs
    # z == 1 - decay_weight
    for item in data["items"]:
        if item["kind"] == "chunk":
            pos = data["positions"].get(item["id"])
            if pos is not None:
                expected_z = 1.0 - item["decay_weight"]
                assert pos[2] == pytest.approx(expected_z, abs=1e-6)


def test_atlas_fewer_than_two_chunks_returns_null_unavailable(client: TestClient) -> None:
    c = _make_chunk("default", "single chunk text", [1, 0, 0, 0, 0, 0, 0, 0], decay=0.9)
    _insert_chunk(client, "default", c, [1, 0, 0, 0, 0, 0, 0, 0])
    resp = client.post("/memory/atlas", json={"profile_id": "default"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["positions"] is None
    assert data["algo"] == "unavailable"


def test_atlas_total_is_honest_sum_and_window_truncated(client: TestClient) -> None:
    for i in range(3):
        c = _make_chunk("default", f"chunk {i}", [float(i), 0, 0, 0, 0, 0, 0, 0])
        _insert_chunk(client, "default", c, [float(i), 0, 0, 0, 0, 0, 0, 0])
    _insert_node(client, "default")
    _insert_node(client, "default")

    # total should be 5 (3 chunks +2 nodes)
    resp = client.post("/memory/atlas", json={"profile_id": "default", "limit": 10})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 5
    assert data["window_truncated"] is False
    assert len(data["items"]) == 5

    # window truncated when limit smaller than total
    resp2 = client.post("/memory/atlas", json={"profile_id": "default", "limit": 2})
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["total"] == 5
    assert data2["window_truncated"] is True
    assert len(data2["items"]) == 2


def test_atlas_node_ids_absent_from_positions(client: TestClient) -> None:
    for i in range(2):
        c = _make_chunk("default", f"chunk {i}", [float(i), float(i + 1), 0, 0, 0, 0, 0, 0])
        _insert_chunk(client, "default", c, [float(i), float(i + 1), 0, 0, 0, 0, 0, 0])
    nid = _insert_node(client, "default")

    resp = client.post("/memory/atlas", json={"profile_id": "default", "limit": 10})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["positions"] is not None
    assert nid not in data["positions"]
    # positions only contains chunk ids
    for pid in data["positions"]:
        assert any(item["id"] == pid and item["kind"] == "chunk" for item in data["items"])


def test_atlas_text_head_truncated(client: TestClient) -> None:
    long_text = "a" * 500
    c = _make_chunk("default", long_text, [1, 0, 0, 0, 0, 0, 0, 0])
    _insert_chunk(client, "default", c, [1, 0, 0, 0, 0, 0, 0, 0])
    resp = client.post("/memory/atlas", json={"profile_id": "default"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert len(items[0]["text_head"]) <= 120


def test_get_dense_missing_ids_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_path(tmp_path, monkeypatch)
    app = create_app()
    with TestClient(app) as tc:
        stores = tc.app.state.stores  # type: ignore[attr-defined]
        c = _make_chunk("default", "hello", [1, 0, 0, 0, 0, 0, 0, 0])
        stores.vector.upsert_chunk(c, [1, 0, 0, 0, 0, 0, 0, 0], None)
        result = stores.vector.get_dense([c.chunk_id, "missing-id"])
        assert c.chunk_id in result
        assert "missing-id" not in result
        assert result[c.chunk_id] == pytest.approx([1, 0, 0, 0, 0, 0, 0, 0])
