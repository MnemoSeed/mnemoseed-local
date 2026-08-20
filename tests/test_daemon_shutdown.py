"""PRD-B2.5 endpoint: POST /daemon/shutdown (respond-then-exit).

With the injected seam the handler answers 200 immediately and the hook
(watchdog disarm + graceful teardown) runs only AFTER the response is flushed;
a TestClient boot never injects the seam, so the same route answers 503 with a
clear message — never 500.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app


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


def test_shutdown_endpoint_responds_then_runs_hook(config_path: Path) -> None:
    """Seam present: 200 + the exact body first, the hook runs only AFTER the
    response was flushed, and the shutdown is audited with the resolved actor.

    The hook blocks until the test releases it: if the handler ran the hook
    before flushing the response, the POST could never complete while the hook
    was blocked — the ordering is pinned by construction, not by timing luck.
    """
    app = create_app()
    hook_entered = threading.Event()
    release_hook = threading.Event()
    hook_done = threading.Event()
    calls: list[str] = []

    def _fake_hook() -> None:
        calls.append("enter")
        hook_entered.set()
        release_hook.wait(5.0)
        calls.append("exit")
        hook_done.set()

    app.state.shutdown_hook = _fake_hook
    with TestClient(app) as client:
        result: dict[str, Any] = {}

        def _post() -> None:
            result["resp"] = client.post("/daemon/shutdown", headers={"X-MnemoSeed-Actor": "cli"})

        thread = threading.Thread(target=_post, daemon=True)
        thread.start()
        try:
            assert hook_entered.wait(5.0), "the shutdown hook never ran"
            thread.join(timeout=5.0)
            assert not thread.is_alive(), "the 200 response was not flushed before the hook ran"
            resp = result["resp"]
            assert resp.status_code == 200
            assert resp.json() == {"ok": True, "status": "shutting_down"}
        finally:
            release_hook.set()
        assert hook_done.wait(5.0), "the hook did not finish after release"
        audit = client.get("/api/v1/audit").json()
        entries = [e for e in audit["items"] if e["action"] == "daemon_shutdown"]
        assert entries, "the intentional shutdown was not audited"
        assert entries[-1]["actor"] == "cli"
    assert calls == ["enter", "exit"], "the hook must run exactly once"


def test_shutdown_endpoint_without_seam_is_503(config_path: Path) -> None:
    """No seam (TestClient boot never injects the hook): a clear 503, never
    500, and no shutdown hook may exist on the app."""
    with TestClient(create_app()) as client:
        resp = client.post("/daemon/shutdown")
        assert resp.status_code == 503
        detail = resp.json().get("detail", "")
        assert "shutdown hook" in detail, f"unclear 503 detail: {detail!r}"


def test_intentional_shutdown_disarms_before_requesting_shutdown() -> None:
    """The pinned order behind POST /daemon/shutdown: disarm() must run BEFORE
    request_shutdown() — uvicorn closes the listener before lifespan teardown,
    so an armed watchdog would misfire os._exit(1) on an intentional drain
    longer than the refused grace."""
    from mnemoseed_local.daemon.runner import intentional_shutdown

    calls: list[str] = []

    class _FakeWatchdog:
        def disarm(self) -> None:
            calls.append("disarm")

    class _FakeServer:
        def request_shutdown(self) -> None:
            calls.append("request_shutdown")

    intentional_shutdown(_FakeWatchdog(), _FakeServer())
    assert calls == ["disarm", "request_shutdown"]
