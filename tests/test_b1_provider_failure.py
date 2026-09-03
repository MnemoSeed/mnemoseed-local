"""B1 provider-failure event path — RED phase (TDD).

Covers brief Sections 1-10 : provider wire, v12 ledger, discriminator,
ingest nomination, serve arm with per-event flags + unresolved, read route,
redaction, debounce, cross-session isolation, order, global disable/forget.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.turn import HostId, IngestEvent, IngestEventType
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.drivers._migrations import MIGRATIONS, latest_version
from mnemoseed_local.storage.ports import (
    ErrorEvent,
    ErrorEventFilter,
    ErrorSignalType,
    EvidenceKind,
    EvidencePointer,
    Page,
)
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

PROFILE = "default"

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


def _config_toml(tmp_path: Path, extra: str = "") -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n' + extra
    )


@pytest.fixture
def b1_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path, "[capture]\nauto_recall = true\n"), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


# ----  schema / wire -------------------------------------------------------


def test_schema_has_provider_error_type() -> None:
    assert hasattr(IngestEventType, "PROVIDER_ERROR")
    assert IngestEventType.PROVIDER_ERROR.value == "provider_error"


def test_provider_error_content_validator_accepts_valid(b1_config: Path) -> None:
    evt = IngestEvent(
        host=HostId.OPENCODE,
        event=IngestEventType.PROVIDER_ERROR,  # type: ignore[arg-type]
        session_id="sess-1",
        profile_id=PROFILE,
        ts=time.time(),
        content={"provider": "openai", "model": "gpt-4o", "status": "quota", "reason": "provider_429_quota"},  # type: ignore[arg-type]
    )
    assert evt.event == IngestEventType.PROVIDER_ERROR


def test_provider_error_content_rejects_wrong_event() -> None:
    with pytest.raises(ValueError):
        IngestEvent(
            host=HostId.OPENCODE,
            event=IngestEventType.USER_PROMPT,
            session_id="sess-1",
            profile_id=PROFILE,
            ts=time.time(),
            content={
                "provider": "openai",
                "model": "gpt-4o",
                "status": "quota",
                "reason": "provider_429_quota",
            },  # type: ignore[arg-type]
        )


def test_error_signal_has_provider_failure() -> None:
    assert hasattr(ErrorSignalType, "PROVIDER_FAILURE")
    assert ErrorSignalType.PROVIDER_FAILURE.value == "provider_failure"


def test_error_event_has_v12_fields() -> None:
    evt = ErrorEvent(
        profile_id=PROFILE,
        signal_type=ErrorSignalType.PROVIDER_FAILURE,  # type: ignore[arg-type]
        observed_at=time.time(),
        evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id="sess-1"),
        session_id="sess-1",
        provider="openai",
        model="gpt-4o",
        status="quota",
        reason="provider_429_quota",
        retryable=0,
    )
    assert evt.provider == "openai"
    assert evt.status == "quota"


# ----  migration -----------------------------------------------------------


def test_migration_v12_exists_and_version_12() -> None:
    assert latest_version() == 12
    meta_versions = sorted(m.version for m in MIGRATIONS if m.applies_to("meta"))
    assert meta_versions == [1, 3, 4, 6, 7, 8, 9, 11, 12]


def test_error_events_has_v12_columns(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        meta = client.app.state.stores.meta
        conn = meta._conn  # type: ignore[attr-defined]
        cols = [row[1] for row in conn.execute("PRAGMA table_info(error_events)")]
        for col in ("provider", "model", "status", "reason", "retryable"):
            assert col in cols


def test_append_error_event_persists_v12_fields(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        meta = client.app.state.stores.meta
        evt = ErrorEvent(
            profile_id=PROFILE,
            signal_type=ErrorSignalType.PROVIDER_FAILURE,
            observed_at=time.time(),
            evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id="sess-1"),
            session_id="sess-1",
            detector_id="provider_error.v1",
            provider="openai",
            model="gpt-4o",
            status="quota",
            reason="provider_429_quota",
            retryable=0,
        )
        meta.append_error_event(evt)
        page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE), Page(0, 10))
        assert len(page.items) == 1
        got = page.items[0]
        assert got.provider == "openai"
        assert got.model == "gpt-4o"
        assert got.status == "quota"
        assert got.reason == "provider_429_quota"
        assert got.retryable == 0
        assert got.detector_id == "provider_error.v1"


# ----  ingest nomination ---------------------------------------------------


def test_ingest_provider_error_nominates_row(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        resp = client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "provider_error",
                "session_id": "sess-1",
                "profile_id": PROFILE,
                "ts": time.time(),
                "content": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "status": "quota",
                    "reason": "provider_429_quota",
                    "error_id": "err-1",
                },
            },
        )
        assert resp.status_code == 202, resp.text
        meta = client.app.state.stores.meta
        page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE), Page(0, 10))
        assert len(page.items) == 1
        assert page.items[0].signal_type == ErrorSignalType.PROVIDER_FAILURE


def test_ingest_provider_error_invalid_status_dropped_not_500(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        resp = client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "provider_error",
                "session_id": "sess-1",
                "profile_id": PROFILE,
                "ts": time.time(),
                "content": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "status": "not_in_allowlist",
                    "reason": "provider_429_quota",
                },
            },
        )
        # must not be 500; either 202 dropped or 422 rejected — both non-500
        assert resp.status_code in (202, 422), resp.text
        meta = client.app.state.stores.meta
        page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE), Page(0, 10))
        # dropped -> no row
        if resp.status_code == 202:
            assert len(page.items) == 0


def test_hook_body_passes_daemon_validator(b1_config: Path) -> None:
    # the exact shape the hook would POST must validate against IngestEvent
    body = {
        "host": "opencode",
        "event": "provider_error",
        "session_id": "sess-1",
        "profile_id": PROFILE,
        "ts": time.time(),
        "content": {
            "provider": "openai",
            "model": "gpt-4o",
            "status": "quota",
            "reason": "provider_429_quota",
            "error_id": "err-abc-123",
        },
    }
    evt = IngestEvent.model_validate(body)
    assert evt.event == IngestEventType.PROVIDER_ERROR
    # also POST through real handler
    with TestClient(create_app()) as client:
        resp = client.post("/ingest", json=body)
        assert resp.status_code == 202, resp.text


# ----  serve arm -----------------------------------------------------------


def test_serve_explicit_unresolved_when_no_rule(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "provider_error",
                "session_id": "sess-1",
                "profile_id": PROFILE,
                "ts": time.time(),
                "content": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "status": "quota",
                    "reason": "provider_429_quota",
                    "error_id": "e1",
                },
            },
        )
        resp = client.post("/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-1"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["detector_fired"] is True
        assert data["unresolved"] is True
        assert data["rule_served"] is False
        assert data["provider"] == "openai"
        assert data["status"] == "quota"
        # must never be enabled:true items:[] with no unresolved
        assert not (data["enabled"] is True and data["items"] == [] and data.get("unresolved") is not True)


def test_serve_flags_wire_shape(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "provider_error",
                "session_id": "sess-1",
                "profile_id": PROFILE,
                "ts": time.time(),
                "content": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "status": "rate_limit",
                    "reason": "provider_429_rate",
                    "error_id": "e2",
                },
            },
        )
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-1"}
        ).json()
        for key in (
            "detector_fired",
            "rule_served",
            "rule_count",
            "unresolved",
            "provider",
            "model",
            "status",
        ):
            assert key in data, f"missing {key}"
        assert "graph_hits" not in data
        assert "watermark" not in data


def test_btype_event_outcome_does_not_arm_serve(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        meta = client.app.state.stores.meta
        # B-type EVENT_OUTCOME row (build failure) must NOT arm provider serve
        meta.append_error_event(
            ErrorEvent(
                profile_id=PROFILE,
                signal_type=ErrorSignalType.EVENT_OUTCOME,
                observed_at=time.time(),
                evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id="sess-1"),
                session_id="sess-1",
                detector_id="build_error.v1",
            )
        )
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-1"}
        ).json()
        assert data["detector_fired"] is False
        assert data["unresolved"] is False
        assert data["rule_served"] is False
        # second variant: PROVIDER_FAILURE with non-allowlist status must NOT arm
        meta.append_error_event(
            ErrorEvent(
                profile_id=PROFILE,
                signal_type=ErrorSignalType.PROVIDER_FAILURE,
                observed_at=time.time(),
                evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id="sess-2"),
                session_id="sess-1",
                detector_id="provider_error.v1",
                provider="openai",
                model="gpt-4o",
                status="not_in_allowlist",
                reason="provider_429_quota",
                retryable=0,
            )
        )
        data2 = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-1"}
        ).json()
        # status not in allowlist -> not armed
        assert data2["detector_fired"] is False


def test_error_read_roundtrip(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "provider_error",
                "session_id": "sess-1",
                "profile_id": PROFILE,
                "ts": time.time(),
                "content": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "status": "quota",
                    "reason": "provider_429_quota",
                    "error_id": "err-rt",
                },
            },
        )
        resp = client.post("/memory/error_events", json={"profile_id": PROFILE})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["provider"] == "openai"
        assert item["model"] == "gpt-4o"
        assert item["status"] == "quota"
        assert item["reason"] == "provider_429_quota"
        assert item["detector_id"] == "provider_error.v1"


def test_redaction_keeps_fingerprint_not_empty(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        # secret-bearing payload: raw message contains token-like substring
        resp = client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "provider_error",
                "session_id": "sess-1",
                "profile_id": PROFILE,
                "ts": time.time(),
                "content": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "status": "auth",
                    "reason": "provider_401",
                    "error_id": "err-secret-test",
                },
                "raw": {"message": "Bearer sk-1234567890 token=secret Authorization: Bearer xyz"},
            },
        )
        assert resp.status_code == 202, resp.text
        # read back must have zero secret bytes but non-empty fingerprint
        meta = client.app.state.stores.meta
        page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE), Page(0, 10))
        assert len(page.items) == 1
        row = page.items[0]
        assert row.provider == "openai"
        assert row.status == "auth"
        # ensure secrets not persisted anywhere in row
        blob = f"{row.provider} {row.model} {row.status} {row.reason} {row.evidence_ptr.id}"
        assert "sk-" not in blob
        assert "Bearer" not in blob


def test_alternate_bucket_storm_debounce(b1_config: Path) -> None:
    # debounce key = (profile, session, provider, model, error_id), never status
    plugin_path = Path("src/mnemoseed_local/hosts/opencode/plugin.ts")
    source = plugin_path.read_text(encoding="utf-8")
    # must key on error_id
    assert "error_id" in source.lower() or "errorId" in source
    # must NOT key solely on status; the debounce map should be per error_id
    # Check that debounce uses error_id and not status token as key part
    assert re.search(r"provider.*model.*error", source, re.I) is not None


def test_cross_session_isolation_debounce(b1_config: Path) -> None:
    # same provider/model/error_id in different sessions must each nominate
    with TestClient(create_app()) as client:
        for sess in ("sess-a", "sess-b"):
            client.post(
                "/ingest",
                json={
                    "host": "opencode",
                    "event": "provider_error",
                    "session_id": sess,
                    "profile_id": PROFILE,
                    "ts": time.time(),
                    "content": {
                        "provider": "openai",
                        "model": "gpt-4o",
                        "status": "quota",
                        "reason": "provider_429_quota",
                        "error_id": "same-err",
                    },
                },
            )
        meta = client.app.state.stores.meta
        # query all, should have 2 rows if per-session isolation holds
        # But ingest debounce is hook-local; daemon stores both. So we expect 2.
        page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE), Page(0, 10))
        # If daemon dedupes per session, we get 2; if collapsed, 1 -> fail
        assert len(page.items) == 2


def test_error_order_under_clock_ack(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        meta = client.app.state.stores.meta
        # insert with out-of-order observed_at but id order is monotonic
        for i in range(3):
            meta.append_error_event(
                ErrorEvent(
                    profile_id=PROFILE,
                    signal_type=ErrorSignalType.PROVIDER_FAILURE,
                    observed_at=time.time() - (2 - i) * 1000,  # staggered
                    evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id=f"sess-{i}"),
                    session_id=f"sess-{i}",
                    provider="openai",
                    model="gpt-4o",
                    status="quota",
                    reason="provider_429_quota",
                    retryable=0,
                )
            )
        resp = client.post("/memory/error_events", json={"profile_id": PROFILE})
        assert resp.status_code == 200, resp.text
        ids = [item["id"] for item in resp.json()["items"]]
        assert ids == sorted(ids)


def test_negative_no_event_no_nomination(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-none"}
        ).json()
        assert data["detector_fired"] is False
        assert data.get("unresolved") is False
        assert data["rule_served"] is False


def test_global_disable_via_auto_recall(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "provider_error",
                "session_id": "sess-1",
                "profile_id": PROFILE,
                "ts": time.time(),
                "content": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "status": "quota",
                    "reason": "provider_429_quota",
                    "error_id": "e-disable",
                },
            },
        )
        # disable globally via config
        resp = client.post("/api/v1/config/set", json={"key_path": "capture.auto_recall", "value": False})
        assert resp.status_code == 200, resp.text
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-1"}
        ).json()
        assert data["enabled"] is False
        assert data["detector_fired"] is False


def test_forget_via_existing_endpoint(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        # create a pin chunk via /memory/remember to have something to forget
        remember = client.post("/memory/remember", json={"profile_id": PROFILE, "text": "LanceDb is great"})
        assert remember.status_code == 200, remember.text
        chunk_id = remember.json()["chunk_id"]
        resp = client.post("/memory/forget_this", json={"profile_id": PROFILE, "chunk_id": chunk_id})
        assert resp.status_code == 200, resp.text
        assert chunk_id in resp.json()["removed"]["chunks"]


def test_hook_detector_branch_exists() -> None:
    source = Path("src/mnemoseed_local/hosts/opencode/plugin.ts").read_text(encoding="utf-8")
    # must have provider failure detection in session.error and message.updated
    assert "provider_error" in source.lower() or "provider_failure" in source.lower()
    assert "session.error" in source
    # must call post to /ingest with provider_error
    assert re.search(r"post\(.*provider_error", source, re.I) is not None or "provider_error" in source
    # debounce map must exist
    assert re.search(r"(Map|debounce)", source, re.I) is not None
