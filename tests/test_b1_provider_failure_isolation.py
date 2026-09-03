"""B1 rework regressions — profile+session scoped serve, fail-closed plugin.

TDD RED phase: these tests assert the qa-implementation-verdict BLOCKER
closures through behavioral seams (real HTTP + real bundled plugin).
They must fail on the pre-rework tree for the right reasons, then go
green with the fix. The 23 tests in test_b1_provider_failure.py stay
untouched and green throughout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.turn import IngestEvent, IngestEventType
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
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
DRIVER = Path(__file__).parent / "ts_hook" / "hook_driver.mjs"

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


@pytest.fixture(autouse=True)
def _hermetic_plugin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MNEMOSEED_LOCAL_DEBUG", "MNEMOSEED_LOCAL_PROFILE_ID", "MNEMOSEED_LOCAL_BASEURL"):
        monkeypatch.delenv(var, raising=False)


def _config_toml(tmp_path: Path) -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[capture]\nauto_recall = true\n"
    )


@pytest.fixture
def b1_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


def _provider_body(session_id: str, **over: object) -> dict:
    content: dict[str, object] = {
        "provider": "openai",
        "model": "gpt-4o",
        "status": "quota",
        "reason": "provider_429_quota",
        "error_id": f"err-{session_id}",
    }
    content.update(over)
    return {
        "host": "opencode",
        "event": "provider_error",
        "session_id": session_id,
        "profile_id": PROFILE,
        "ts": time.time(),
        "content": content,
    }


# ---- 1. cross-session serve isolation --------------------------------------


def test_session_A_error_never_arms_session_B(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        resp = client.post("/ingest", json=_provider_body("sess-A"))
        assert resp.status_code == 202, resp.text
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-B"}
        ).json()
        assert data["detector_fired"] is False
        assert data["unresolved"] is False
        assert data["rule_served"] is False
        assert data["provider"] is None


def test_same_session_first_pull_arms(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        client.post("/ingest", json=_provider_body("sess-1"))
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-1"}
        ).json()
        assert data["detector_fired"] is True
        assert data["unresolved"] is True
        assert data["provider"] == "openai"


def test_same_session_consumption_second_pull_quiet(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        client.post("/ingest", json=_provider_body("sess-1"))
        first = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-1"}
        ).json()
        assert first["detector_fired"] is True
        second = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-1"}
        ).json()
        assert second["detector_fired"] is False
        assert second["unresolved"] is False


def test_session_end_clears_event_arm(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        client.post("/ingest", json=_provider_body("sess-1"))
        end = client.post("/session/end", json={"profile_id": PROFILE, "session_id": "sess-1"})
        assert end.status_code == 200, end.text
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-1"}
        ).json()
        assert data["detector_fired"] is False
        assert data["unresolved"] is False


def test_stale_event_does_not_arm_forever(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        meta = client.app.state.stores.meta
        meta.append_error_event(
            ErrorEvent(
                profile_id=PROFILE,
                signal_type=ErrorSignalType.PROVIDER_FAILURE,
                observed_at=time.time() - 7200.0,
                evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id="sess-old"),
                session_id="sess-old",
                detector_id="provider_error.v1",
                provider="openai",
                model="gpt-4o",
                status="quota",
                reason="provider_429_quota",
                retryable=0,
            )
        )
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-old"}
        ).json()
        assert data["detector_fired"] is False
        assert data["unresolved"] is False


def test_storage_filter_supports_session_scope(b1_config: Path) -> None:
    with TestClient(create_app()) as client:
        meta = client.app.state.stores.meta
        for sess in ("sess-A", "sess-B"):
            meta.append_error_event(
                ErrorEvent(
                    profile_id=PROFILE,
                    signal_type=ErrorSignalType.PROVIDER_FAILURE,
                    observed_at=time.time(),
                    evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id=sess),
                    session_id=sess,
                    detector_id="provider_error.v1",
                    provider="openai",
                    model="gpt-4o",
                    status="quota",
                    reason="provider_429_quota",
                    retryable=0,
                )
            )
        page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE, session_id="sess-A"), Page(0, 10))
        assert [e.session_id for e in page.items] == ["sess-A"]


# ---- 2/3. joined production seam: real plugin -> HTTP -> row ----------------


def _bundle(tmp_path: Path) -> Path:
    if shutil.which("npx") is None or shutil.which("node") is None:
        pytest.skip("node toolchain unavailable on this machine")
    plugin = resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts")
    out = tmp_path / "plugin.bundle.mjs"
    result = subprocess.run(
        f'npx --yes esbuild "{plugin}" --bundle --format=esm --platform=node '
        f'--outfile="{out}" --log-level=error',
        shell=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"esbuild bundle failed: {result.stderr}"
    return out


def _run(bundle: Path, scenario: str) -> dict:
    env = dict(os.environ)
    env.pop("MNEMOSEED_LOCAL_DEBUG", None)
    result = subprocess.run(
        ["node", str(DRIVER), str(bundle), scenario],
        shell=False,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, f"driver failed: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_plugin_true_429_nominates_quota_not_other(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-nomination")
    bodies = [p["body"] for p in transcript["posts"] if p["body"].get("event") == "provider_error"]
    assert bodies, "real plugin emitted no provider_error POST for the 429 fixture"
    quota = [b for b in bodies if b["content"]["status"] == "quota"]
    assert quota, f"true 429 misclassified: {[b['content']['status'] for b in bodies]}"


def test_plugin_build_error_never_nominates(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-nomination")
    bodies = [p["body"] for p in transcript["posts"] if p["body"].get("event") == "provider_error"]
    for body in bodies:
        blob = json.dumps(body["content"])
        assert "exit status" not in blob
        assert "Compilation failed" not in blob
    assert transcript["buildErrorPosts"] == 0


def test_plugin_timeout_no_status_nominates_timeout(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-nomination")
    bodies = [p["body"] for p in transcript["posts"] if p["body"].get("event") == "provider_error"]
    timeouts = [b for b in bodies if b["content"]["status"] == "timeout"]
    assert timeouts, f"timeout/no-status hang not nominated: {[b['content'] for b in bodies]}"


def test_joined_hook_body_through_ingest_persists_row(tmp_path: Path, b1_config: Path) -> None:
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-nomination")
    bodies = [p["body"] for p in transcript["posts"] if p["body"].get("event") == "provider_error"]
    assert bodies, "no captured hook POST to join through the daemon"
    body = dict(bodies[0])
    body["profile_id"] = PROFILE
    evt = IngestEvent.model_validate(body)
    assert evt.event == IngestEventType.PROVIDER_ERROR
    with TestClient(create_app()) as client:
        resp = client.post("/ingest", json=json.loads(evt.model_dump_json()))
        assert resp.status_code == 202, resp.text
        meta = client.app.state.stores.meta
        page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE), Page(0, 10))
        assert len(page.items) == 1
        row = page.items[0]
        assert row.signal_type == ErrorSignalType.PROVIDER_FAILURE
        assert row.detector_id == "provider_error.v1"
        assert row.provider == body["content"]["provider"]
        assert row.status == body["content"]["status"]
        assert row.reason


def test_secret_bearing_hook_input_leaves_no_secret_anywhere(
    tmp_path: Path, b1_config: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-secret")
    bodies = [p["body"] for p in transcript["posts"] if p["body"].get("event") == "provider_error"]
    assert bodies, "secret scenario emitted no provider_error POST"
    secrets = ("sk-live-", "Bearer ", "token=hunter2")
    with TestClient(create_app()) as client:
        with caplog.at_level("DEBUG", logger="mnemoseed_local.daemon.ingest"):
            body = dict(bodies[0])
            body["profile_id"] = PROFILE
            wire = json.dumps(body)
            for secret in secrets:
                assert secret not in wire
            resp = client.post("/ingest", json=body)
            assert resp.status_code == 202, resp.text
            meta = client.app.state.stores.meta
            page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE), Page(0, 10))
            assert len(page.items) == 1
            row = page.items[0]
            blob = json.dumps(
                {
                    "provider": row.provider,
                    "model": row.model,
                    "status": row.status,
                    "reason": row.reason,
                    "evidence": row.evidence_ptr.id,
                }
            )
            for secret in secrets:
                assert secret not in blob
            read = client.post("/memory/error_events", json={"profile_id": PROFILE}).json()
            for secret in secrets:
                assert secret not in json.dumps(read)
            hint = client.post(
                "/session/recall-pending",
                json={"profile_id": PROFILE, "session_id": row.session_id},
            ).json()
            for secret in secrets:
                assert secret not in json.dumps(hint)
    for secret in secrets:
        assert secret not in caplog.text


# ---- 4. entry-gate fixtures -------------------------------------------------


def test_entry_gate_fixtures_present_and_sanitized() -> None:
    base = Path("tests/fixtures/opencode_hook")
    expected = [
        "provider_error_429.json",
        "provider_error_timeout_no_status.json",
        "provider_build_error_negative.json",
    ]
    for name in expected:
        path = base / name
        assert path.exists(), f"missing entry-gate fixture {name}"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload.get("provenance"), f"{name} lacks provenance"
        blob = path.read_text(encoding="utf-8")
        for secret in ("sk-", "Bearer", "token=", "Authorization:"):
            assert secret not in blob, f"{name} carries a secret-shaped string"
    doc = base / "provider_error_shapes.md"
    assert doc.exists(), "missing entry-gate shape note"
