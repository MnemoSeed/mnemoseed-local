"""B2 time-window surface: POST /session/windows returns exact per-session
chunk windows — the true first/latest ingested_at from a bounded full scan
(never a page-visible approximation), the chunk count, the live-capture
active flag, and the scan-limit truncation marker. The shared '?' group
(unlabeled pins) is included with active=false.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.schema.turn import HostId
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.drivers._time import iso8601_utc
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

PROFILE = "default"

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")

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


def _write_chunk(
    client: TestClient,
    chunk_id: str,
    session_id: str | None,
    ingested_at: float,
    text: str,
) -> None:
    stores = client.app.state.stores
    stamp = ChunkStamp(
        chunk_id=chunk_id,
        profile_id=PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id=session_id, source="manual"),
        ingested_at=ingested_at,
    )
    result = stores.embed.embed(text)
    stores.vector.upsert_chunk(stamp, result.dense, result.sparse)


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


def test_session_windows_exact_windows_newest_first_with_question_group(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        _write_chunk(client, "c1", "s-old", 1.0, "old turn one")
        _write_chunk(client, "c2", "s-old", 2.0, "old turn two")
        _write_chunk(client, "c3", "s-old", 3.0, "old turn three")
        _write_chunk(client, "c4", "s-new", 4.0, "new turn one")
        _write_chunk(client, "c5", "s-new", 5.0, "new turn two")
        _write_chunk(client, "c6", None, 0.5, "manual pin")

        body = client.post("/session/windows", json={"profile_id": PROFILE, "sessions": 5})
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["profile_id"] == PROFILE
        sessions = payload["sessions"]
        assert [s["session_id"] for s in sessions] == ["s-new", "s-old", "?"]

        new_entry = sessions[0]
        assert new_entry["window"] == {"first": iso8601_utc(4.0), "latest": iso8601_utc(5.0)}
        assert new_entry["chunk_count"] == 2
        assert new_entry["active"] is False
        assert new_entry["window_truncated"] is False

        old_entry = sessions[1]
        assert old_entry["window"] == {"first": iso8601_utc(1.0), "latest": iso8601_utc(3.0)}
        assert old_entry["chunk_count"] == 3
        assert old_entry["active"] is False
        assert old_entry["window_truncated"] is False

        question_entry = sessions[2]
        assert question_entry["session_id"] == "?"
        assert question_entry["active"] is False
        assert question_entry["window"] is None
        assert question_entry["chunk_count"] is None
        assert question_entry["window_truncated"] is False

        for entry in sessions:
            for value in (entry["window"] or {}).values():
                assert _ISO.fullmatch(value), value


def test_session_windows_active_flags_only_unsettled_session(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        _ingest(client, "sess-active", 1.0, "buffered session turn")
        flushed = client.post("/flush", json={"session_id": "sess-active", "profile_id": PROFILE})
        assert flushed.status_code == 200, flushed.text
        _ingest(client, "sess-done", 2.0, "settled session turn")
        settled = client.post("/session/end", json={"session_id": "sess-done", "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text

        body = client.post("/session/windows", json={"profile_id": PROFILE, "sessions": 5})
        assert body.status_code == 200, body.text
        sessions = body.json()["sessions"]
        assert [s["session_id"] for s in sessions] == ["sess-done", "sess-active"]
        assert sessions[0]["active"] is False  # settled and pruned from the capture lane
        assert sessions[1]["active"] is True  # still buffered in the capture lane
        assert sessions[0]["window_truncated"] is False
        assert sessions[1]["window_truncated"] is False


def test_session_windows_truncated_when_session_exceeds_scan_limit(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mnemoseed_local.daemon.memory.SESSION_WINDOW_SCAN_LIMIT", 3)
    with TestClient(create_app()) as client:
        for i in range(1, 5):
            _write_chunk(client, f"big-{i}", "s-big", float(i), f"big turn {i}")
        _write_chunk(client, "small-1", "s-small", 5.0, "small turn")

        body = client.post("/session/windows", json={"profile_id": PROFILE, "sessions": 5})
        assert body.status_code == 200, body.text
        sessions = body.json()["sessions"]
        assert [s["session_id"] for s in sessions] == ["s-small", "s-big"]
        small = sessions[0]
        assert small["window_truncated"] is False
        assert small["window"] == {"first": iso8601_utc(5.0), "latest": iso8601_utc(5.0)}
        big = sessions[1]
        assert big["chunk_count"] == 3
        assert big["window_truncated"] is True
        assert big["window"] == {"first": iso8601_utc(2.0), "latest": iso8601_utc(4.0)}
        for entry in sessions:
            for value in (entry["window"] or {}).values():
                assert _ISO.fullmatch(value), value


def test_session_windows_exactly_at_scan_limit_is_not_truncated(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session with exactly SESSION_WINDOW_SCAN_LIMIT rows is complete: the
    truncation flag is true only when the scan-limit rows are exceeded."""
    monkeypatch.setattr("mnemoseed_local.daemon.memory.SESSION_WINDOW_SCAN_LIMIT", 3)
    with TestClient(create_app()) as client:
        for i in range(1, 4):
            _write_chunk(client, f"c{i}", "s-exact", float(i), f"turn {i}")

        body = client.post("/session/windows", json={"profile_id": PROFILE, "sessions": 5})
        assert body.status_code == 200, body.text
        entry = body.json()["sessions"][0]
        assert entry["session_id"] == "s-exact"
        assert entry["chunk_count"] == 3
        assert entry["window_truncated"] is False
        assert entry["window"] == {"first": iso8601_utc(1.0), "latest": iso8601_utc(3.0)}


def test_session_windows_non_positive_epoch_window_is_null(config_path: Path) -> None:
    """A zero or negative first epoch has no meaningful timestamp; the window
    renders as honest null instead of the 1970-01-01 epoch-leak trap."""
    with TestClient(create_app()) as client:
        _write_chunk(client, "c0", "s-zero", 0.0, "zero epoch")
        _write_chunk(client, "cneg", "s-neg", -5.0, "negative epoch")
        _write_chunk(client, "cpos", "s-pos", 5.0, "positive epoch")

        body = client.post("/session/windows", json={"profile_id": PROFILE, "sessions": 5})
        assert body.status_code == 200, body.text
        sessions = body.json()["sessions"]
        by_id = {entry["session_id"]: entry for entry in sessions}
        assert by_id["s-zero"]["window"] is None
        assert by_id["s-neg"]["window"] is None
        assert by_id["s-pos"]["window"] == {"first": iso8601_utc(5.0), "latest": iso8601_utc(5.0)}


def test_session_windows_rejects_out_of_range_sessions(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        body = client.post("/session/windows", json={"profile_id": PROFILE, "sessions": 0})
        assert body.status_code == 422
        body = client.post("/session/windows", json={"profile_id": PROFILE, "sessions": 11})
        assert body.status_code == 422


def test_session_windows_empty_profile_and_default_cap(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        empty = client.post("/session/windows", json={"profile_id": PROFILE})
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"profile_id": PROFILE, "sessions": []}

        for i in range(4):
            _write_chunk(client, f"c{i}", f"s{i}", float(i + 1), f"turn {i}")
        default = client.post("/session/windows", json={"profile_id": PROFILE})
        assert [s["session_id"] for s in default.json()["sessions"]] == ["s3", "s2", "s1"]
