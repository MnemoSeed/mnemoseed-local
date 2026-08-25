"""B2.12 observability: MCP-gateway handshake beacon surface, since-boot
activity counters (capture-hook ingest vs MCP handshakes), the request-level
logging toggle ([logging] requests), and first-sighting profile hygiene.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.turn import HostId

PROFILE = "default"
SESSION = "sess-obs"

SECRET_BODY_TEXT = "机密请求体内容绝不能进日志"


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


def _ingest_body(session: str = SESSION, profile: str = PROFILE) -> dict:
    return {
        "host": HostId.OPENCODE.value,
        "event": "user_prompt",
        "session_id": session,
        "profile_id": profile,
        "ts": 1.0,
        "content": {"text": SECRET_BODY_TEXT},
    }


# ---------------------------------------------------------------- handshake beacon surface


def test_mcp_handshake_endpoint_records_since_boot(config_path: Path) -> None:
    with _boot(config_path) as client:
        assert client.get("/api/v1/observability").json()["mcp_handshake_count"] == 0
        first = client.post("/mcp/handshake", json={"profile_id": PROFILE})
        assert first.status_code == 200
        assert first.json() == {"ok": True}
        client.post("/mcp/handshake", json={})
        body = client.get("/api/v1/observability").json()
        assert body["mcp_handshake_count"] == 2


def test_handshake_counters_reset_per_boot(config_path: Path) -> None:
    with _boot(config_path) as client:
        client.post("/mcp/handshake", json={})
    with _boot(config_path) as client:
        assert client.get("/api/v1/observability").json()["mcp_handshake_count"] == 0


# ---------------------------------------------------------------- capture-hook activity counter


def test_capture_ingest_counter_counts_only_hook_actor(config_path: Path) -> None:
    with _boot(config_path) as client:
        client.post("/ingest", json=_ingest_body(), headers={"X-MnemoSeed-Actor": "hook"})
        assert client.get("/api/v1/observability").json()["capture_ingest_count"] == 1
        # a CLI/console ingest is not capture-HOOK activity
        client.post("/ingest", json=_ingest_body())
        client.post("/ingest", json=_ingest_body(), headers={"X-MnemoSeed-Actor": "cli"})
        assert client.get("/api/v1/observability").json()["capture_ingest_count"] == 1


# ---------------------------------------------------------------- first-sighting hygiene (#110)


def test_first_sighting_logged_once_per_profile(config_path: Path, caplog) -> None:
    with _boot(config_path) as client, caplog.at_level(logging.INFO):
        client.post("/ingest", json=_ingest_body(profile="typo-id"))
        client.post("/ingest", json=_ingest_body(profile="typo-id"))
        client.post("/ingest", json=_ingest_body(profile=PROFILE))
    sightings = [r for r in caplog.records if "first sighting" in r.getMessage()]
    assert len(sightings) == 1, caplog.messages
    assert "typo-id" in sightings[0].getMessage()


def test_default_profile_never_logs_a_first_sighting(config_path: Path, caplog) -> None:
    with _boot(config_path) as client, caplog.at_level(logging.INFO):
        client.post("/ingest", json=_ingest_body(profile=PROFILE))
    assert not [r for r in caplog.records if "first sighting" in r.getMessage()]


# ---------------------------------------------------------------- request logging toggle


def test_request_logging_off_by_default_stays_silent(config_path: Path, caplog) -> None:
    with _boot(config_path) as client, caplog.at_level(logging.INFO):
        client.get("/healthz")
        client.post("/ingest", json=_ingest_body())
    assert not any("GET /healthz" in r.getMessage() for r in caplog.records)


def test_request_logging_on_emits_method_path_status_without_bodies(config_path: Path, caplog) -> None:
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "[logging]\nrequests = true\n",
        encoding="utf-8",
    )
    with _boot(config_path) as client, caplog.at_level(logging.INFO):
        client.get("/healthz")
        client.post("/ingest", json=_ingest_body())
    messages = [r.getMessage() for r in caplog.records]
    assert any("GET /healthz" in m for m in messages), messages
    assert any("POST /ingest" in m for m in messages), messages
    assert not any(SECRET_BODY_TEXT in m for m in messages), "request bodies must never reach the log"
