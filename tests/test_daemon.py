"""Trimmed A2 daemon smoke: real boot over embedded stores (synthetic embedder,
stub dream LLM), the /healthz probe, the capture -> recall -> dream -> config
-> audit loop, and the non-loopback boot refusal.

No identity/accounts/tokens: every memory route takes an explicit profile_id
and the daemon refuses a non-loopback baseurl at boot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.turn import HostId

PROFILE = "default"
SESSION = "sess-daemon"

DURABLE_TEXT = "我决定以后都用 pnpm 管理依赖"


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
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


def _boot(config_path: Path) -> TestClient:
    return TestClient(create_app())


def test_healthz_after_real_boot(config_path: Path) -> None:
    with _boot(config_path) as client:
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["preset"] == "embedded"
        assert body["migrations"]["main"] >= 1  # the meta migrations ran
        assert body["gate"]["ok"] is True
        health = client.get("/health").json()
        assert health["drivers"]["embed"] == "synthetic"


def test_non_loopback_baseurl_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\nbaseurl = "http://192.168.1.10:7788"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    app = create_app()
    with pytest.raises(RuntimeError, match="non-loopback"):
        with TestClient(app):
            pass


def _spy_scorepool(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace the daemon's ScorePool with a recording subclass so the boot
    wiring can be asserted without sleeping. The real pool still runs, so the
    daemon stays fully operational."""
    import mnemoseed_local.daemon.app as app_module
    from mnemoseed_local.capture.pool import ScorePool as RealScorePool

    seen: dict[str, object] = {}

    class _SpyPool(RealScorePool):
        def __init__(self, *args, **kwargs):
            seen["dream_threshold"] = kwargs.get("dream_threshold")
            seen["idle_window_sec"] = kwargs.get("idle_window_sec")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(app_module, "ScorePool", _SpyPool)
    return seen


def test_pool_thresholds_come_from_config(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture pool is constructed from the config keys, never fixed
    literals: dream_threshold <- dream.floor_pool_points, idle_window_sec <-
    dream.idle_min_sec (the live 900s default)."""
    seen = _spy_scorepool(monkeypatch)
    with _boot(config_path) as client:
        assert client.get("/healthz").json()["status"] == "ok"
    assert seen["dream_threshold"] == 10.0  # config.dream.floor_pool_points
    assert seen["idle_window_sec"] == 900.0  # config.dream.idle_min_sec


def test_pool_thresholds_follow_a_changed_config_on_next_boot(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config edit applies at the NEXT boot: the pool is rebuilt from the
    live config, so a new floor / idle window is in effect immediately."""
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "[dream]\nfloor_pool_points = 4.0\nidle_min_sec = 60.0\n",
        encoding="utf-8",
    )
    seen = _spy_scorepool(monkeypatch)
    with _boot(config_path) as client:
        assert client.get("/healthz").json()["status"] == "ok"
    assert seen["dream_threshold"] == 4.0
    assert seen["idle_window_sec"] == 60.0


def test_capture_recall_dream_config_audit_loop(config_path: Path) -> None:
    with _boot(config_path) as client:
        # ---- capture: one durable user turn, then settle (drain + relay)
        response = client.post(
            "/ingest",
            json={
                "host": HostId.CLAUDE_CODE.value,
                "event": "user_prompt",
                "session_id": SESSION,
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": DURABLE_TEXT},
            },
        )
        assert response.status_code == 202, response.text
        settled = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE},
        )
        assert settled.status_code == 200, settled.text

        # ---- config read + write (loopback-only, hot-apply)
        config = client.get("/api/v1/config").json()["config"]
        assert config["dream"]["auto_trigger"] is False
        result = client.post(
            "/api/v1/config/set",
            json={"key_path": "dream.floor_pool_points", "value": 4.0},
            headers={"X-MnemoSeed-Actor": "cli"},
        ).json()
        assert result["ok"] is True
        assert result["restart_required"] is False
        assert client.get("/api/v1/config").json()["config"]["dream"]["floor_pool_points"] == 4.0

        # ---- recall: the captured chunk is listable (verbatim track)
        recall = client.post(
            "/memory/recall",
            json={"profile_id": PROFILE, "query": "pnpm", "top_k": 5},
        ).json()
        assert recall["memory"]["entries"], recall

        # ---- dream --once: manual cycle, one HTTP POST
        dream = client.post(
            "/memory/dream_once",
            json={"profile_id": PROFILE},
        ).json()
        assert "launched" in dream
        status = client.post(
            "/memory/dream_status",
            json={"profile_id": PROFILE},
        ).json()
        assert status["profile_id"] == PROFILE

        # ---- audit read: the config write was attributed to the CLI surface
        audit = client.get("/api/v1/audit", params={"action": "config.set"}).json()
        assert audit["total"] >= 1
        assert any(item["actor"] == "cli" for item in audit["items"])
