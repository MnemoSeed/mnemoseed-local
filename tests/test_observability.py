"""B2.12 observability: authenticated-request activity counts (per audit actor),
the since-boot activity counters (capture-hook ingest vs authenticated gateway
traffic), the request-level logging toggle ([logging] requests), and
first-sighting profile hygiene.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import auth_support
import pytest
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
SESSION = "sess-obs"

SECRET_BODY_TEXT = "机密请求体内容绝不能进日志"

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


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture: str) -> Path:
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
        'model = "stub"\n' + capture,
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _write_config(tmp_path, monkeypatch, "")


@pytest.fixture
def recall_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with capture.auto_recall on (serves are real injections)."""
    return _write_config(tmp_path, monkeypatch, "[capture]\nauto_recall = true\n")


def _boot(config_path: Path) -> Any:
    return auth_support.boot(config_path.parent)


def _ingest_body(session: str = SESSION, profile: str = PROFILE) -> dict:
    return {
        "host": HostId.OPENCODE.value,
        "event": "user_prompt",
        "session_id": session,
        "profile_id": profile,
        "ts": 1.0,
        "content": {"text": SECRET_BODY_TEXT},
    }


# ---------------------------------------------------------------- authenticated activity counts


def test_authenticated_requests_are_counted_per_actor(config_path: Path) -> None:
    """Every request the credential gate admits is counted against its audit
    actor, so gateway traffic is visible without any counter an unauthenticated
    caller could move. The reading request counts as the actor that reads."""
    with _boot(config_path) as client:
        assert client.get("/api/v1/audit").status_code == 200
        client.post("/ingest", json=_ingest_body(), headers={"X-MnemoSeed-Actor": "hook"})
        assert client.get("/api/v1/config", headers={"X-MnemoSeed-Actor": "cli"}).status_code == 200
        body = client.get("/api/v1/observability").json()

    assert body["authenticated_actor_counts"] == {"console": 2, "hook": 1, "cli": 1}


def test_authenticated_counts_reset_per_boot(config_path: Path) -> None:
    with _boot(config_path) as client:
        assert client.get("/api/v1/audit").status_code == 200
        assert client.get("/api/v1/observability").json()["authenticated_actor_counts"] == {"console": 2}
    with _boot(config_path) as client:
        assert client.get("/api/v1/observability").json()["authenticated_actor_counts"] == {"console": 1}


def test_a_refused_request_moves_no_counter(config_path: Path) -> None:
    """A request the gate refuses never reaches the counters: the writable
    surface stays behind the credential, whatever path it names."""
    with _boot(config_path) as client:
        wrong = auth_support.credential_headers("a-credential-that-was-never-issued")
        assert client.get("/api/v1/audit", headers=wrong).status_code == 401
        assert client.post("/ingest", json=_ingest_body(), headers=wrong).status_code == 401
        body = client.get("/api/v1/observability").json()

    assert body["authenticated_actor_counts"] == {"console": 1}


def test_the_retired_beacon_path_is_no_writable_counter(config_path: Path) -> None:
    """The startup beacon is gone: its path serves no counter to a credentialed
    caller and stays refused for everyone else."""
    with _boot(config_path) as client:
        assert client.post("/mcp/handshake", json={}).status_code == 405
    with auth_support.unauthenticated(config_path.parent) as stranger:
        assert stranger.post("/mcp/handshake", json={}).status_code == 401


# ---------------------------------------------------------------- capture-hook activity counter


def test_capture_ingest_counter_counts_only_hook_actor(config_path: Path) -> None:
    with _boot(config_path) as client:
        client.post("/ingest", json=_ingest_body(), headers={"X-MnemoSeed-Actor": "hook"})
        assert client.get("/api/v1/observability").json()["capture_ingest_count"] == 1
        # a CLI/console ingest is not capture-HOOK activity
        client.post("/ingest", json=_ingest_body())
        client.post("/ingest", json=_ingest_body(), headers={"X-MnemoSeed-Actor": "cli"})
        assert client.get("/api/v1/observability").json()["capture_ingest_count"] == 1
        # reverse assertion: the counter read refuses a caller with no credential
        refused = client.get("/api/v1/observability", headers={"Authorization": "Bearer not-the-credential"})
        assert refused.status_code == 401


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


# ---------------------------------------------------------------- T2 recall counters


def test_recall_counters_default_to_zero(config_path: Path) -> None:
    """The three T2 counters start at zero on a fresh boot."""
    with _boot(config_path) as client:
        obs = client.get("/api/v1/observability").json()
        assert obs["recall_injection_count"] == 0
        assert obs["recall_injection_chars_total"] == 0
        assert obs["recall_pending_served_count"] == 0


def _ingest_prompt(client: TestClient, session_id: str, ts: float, text: str) -> None:
    response = client.post(
        "/ingest",
        json={
            "host": "opencode",
            "event": "user_prompt",
            "session_id": session_id,
            "profile_id": PROFILE,
            "ts": ts,
            "content": {"text": text},
        },
    )
    assert response.status_code == 202, response.text


def test_recall_injection_counters_increment_on_real_serve(recall_config_path: Path) -> None:
    """Ingest a session with entities that trigger a focal scan, then pull
    recall_pending — the observability counters increment by the expected amounts."""
    with _boot(recall_config_path) as client:
        # settle an old session that has entity-bearing chunks to serve
        _ingest_prompt(client, "sess-old", 1.0, "LanceDb 是向量存储层")
        client.post("/session/end", json={"session_id": "sess-old", "profile_id": PROFILE})
        # fresh session prompt triggers focal scan
        _ingest_prompt(client, "sess-new", 2.0, "LanceDb 现在处于什么阶段")
        obs_before = client.get("/api/v1/observability").json()
        assert obs_before["recall_injection_count"] == 0
        assert obs_before["recall_injection_chars_total"] == 0
        assert obs_before["recall_pending_served_count"] == 0
        # pull — this is the injection serve
        pull = client.post(
            "/session/recall-pending",
            json={"profile_id": PROFILE, "session_id": "sess-new"},
        )
        assert pull.status_code == 200, pull.text
        payload = pull.json()
        assert payload["enabled"] is True
        assert len(payload["items"]) > 0, "expected at least one focal item served"
        served_chars = sum(len(item["text"]) + 1 for item in payload["items"])
        obs_after = client.get("/api/v1/observability").json()
        assert obs_after["recall_injection_count"] == 1
        assert obs_after["recall_injection_chars_total"] == served_chars
        assert obs_after["recall_pending_served_count"] == 1


def test_recall_counters_are_cumulative_across_multiple_serves(recall_config_path: Path) -> None:
    """Multiple serve events accumulate correctly in both counters."""
    with _boot(recall_config_path) as client:
        # set up old sessions for focal serve
        _ingest_prompt(client, "sess-old", 1.0, "MnemoSeed 的记忆系统使用 LanceDb")
        client.post("/session/end", json={"session_id": "sess-old", "profile_id": PROFILE})
        _ingest_prompt(client, "sess-old2", 1.1, "LanceDb 的向量维度是 64")
        client.post("/session/end", json={"session_id": "sess-old2", "profile_id": PROFILE})

        # serve 1
        _ingest_prompt(client, "sess-a", 2.0, "LanceDb 是什么")
        pull_a = client.post(
            "/session/recall-pending",
            json={"profile_id": PROFILE, "session_id": "sess-a"},
        )
        assert pull_a.status_code == 200
        chars_a = sum(len(item["text"]) + 1 for item in pull_a.json()["items"])
        client.post("/session/end", json={"session_id": "sess-a", "profile_id": PROFILE})

        # serve 2
        _ingest_prompt(client, "sess-b", 3.0, "MnemoSeed 使用 LanceDb 吗")
        pull_b = client.post(
            "/session/recall-pending",
            json={"profile_id": PROFILE, "session_id": "sess-b"},
        )
        assert pull_b.status_code == 200
        chars_b = sum(len(item["text"]) + 1 for item in pull_b.json()["items"])

        obs = client.get("/api/v1/observability").json()
        assert obs["recall_injection_count"] == 2
        assert obs["recall_injection_chars_total"] == chars_a + chars_b
        assert obs["recall_pending_served_count"] == 2


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


# ---------------------------------------------------------------- capture evidence states (host status)


def test_capture_evidence_states_from_explicit_values() -> None:
    """The persisted-capture state maps explicit evidence onto the honest
    vocabulary: absence, in-window presence, out-of-window presence."""
    from mnemoseed_local.daemon.observability import (
        CAPTURE_EVIDENCE_NONE,
        CAPTURE_EVIDENCE_RECENT,
        CAPTURE_EVIDENCE_STALE,
        capture_evidence_status,
    )

    window = 600.0
    now = 1_000_000.0

    absent = capture_evidence_status(last_persisted_at=None, window_s=window, now=now)
    assert absent.state == CAPTURE_EVIDENCE_NONE
    assert absent.last_persisted_at is None
    assert absent.window_s == window

    recent = capture_evidence_status(last_persisted_at=now - 30.0, window_s=window, now=now)
    assert recent.state == CAPTURE_EVIDENCE_RECENT

    boundary = capture_evidence_status(last_persisted_at=now - window, window_s=window, now=now)
    assert boundary.state == CAPTURE_EVIDENCE_RECENT

    stale = capture_evidence_status(last_persisted_at=now - window - 1.0, window_s=window, now=now)
    assert stale.state == CAPTURE_EVIDENCE_STALE
    assert stale.last_persisted_at == now - window - 1.0

    future = capture_evidence_status(last_persisted_at=now + 5.0, window_s=window, now=now)
    assert future.state == CAPTURE_EVIDENCE_RECENT


def test_capture_evidence_line_is_one_computed_line() -> None:
    from mnemoseed_local.daemon.observability import capture_evidence_status

    line = capture_evidence_status(last_persisted_at=None, window_s=600.0, now=1.0).line()
    assert line.startswith("capture: none (")
    assert line.endswith(")")


def test_since_boot_counter_maps_onto_observed_not_observed_unknown() -> None:
    """A counter that moved, one that has not, and one that could not be read
    (None — unknown, never zero)."""
    from mnemoseed_local.daemon.observability import (
        OBSERVATION_NOT_OBSERVED,
        OBSERVATION_OBSERVED,
        OBSERVATION_UNKNOWN,
        since_boot_observation,
    )

    observed = since_boot_observation(
        label="ingest ack",
        count=3,
        observed="3 hook ingests acknowledged since boot",
        not_observed="no hook ingest acknowledged since boot",
        unknown="the since-boot counters were unreadable",
    )
    assert observed.state == OBSERVATION_OBSERVED
    assert observed.count == 3

    clear = since_boot_observation(
        label="ingest ack",
        count=0,
        observed="observed",
        not_observed="no hook ingest acknowledged since boot",
        unknown="unreadable",
    )
    assert clear.state == OBSERVATION_NOT_OBSERVED
    assert clear.count == 0

    unknown = since_boot_observation(
        label="ingest ack",
        count=None,
        observed="observed",
        not_observed="clear",
        unknown="the since-boot counters were unreadable",
    )
    assert unknown.state == OBSERVATION_UNKNOWN
    assert unknown.count is None

    assert observed.line().startswith("ingest ack: observed (")
    assert clear.line().startswith("ingest ack: not_observed (")
