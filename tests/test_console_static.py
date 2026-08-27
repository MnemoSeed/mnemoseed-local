"""Console C-1 T1: the same-origin static mount at "/" is registered last, so
it serves the console without shadowing any daemon route.

Starlette matches routes in registration order: mounting "/" before the
routers would swallow /healthz, /memory/* and /api/v1/*. These regression
tests pin that ordering.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.routing import Mount

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


def _console_mounts() -> list[Mount]:
    return [route for route in create_app().routes if isinstance(route, Mount) and route.name == "console"]


def test_console_static_mount_registered_at_root() -> None:
    mounts = _console_mounts()
    assert mounts, "no 'console' StaticFiles mount at '/'"
    assert mounts[0].path in {"", "/"}, "the console mount is not rooted at '/'"
    assert isinstance(mounts[0].app, StaticFiles), "the console mount is not a StaticFiles app"


def test_console_mount_does_not_shadow_routes(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        assert _console_mounts(), "the console mount was not registered"

        healthz = client.get("/healthz")
        assert healthz.status_code == 200, healthz.text
        assert healthz.json()["status"] == "ok"

        audit = client.get("/api/v1/audit")
        assert audit.status_code == 200, audit.text
        assert "items" in audit.json()

        recall = client.post("/memory/recall", json={})
        assert recall.status_code == 422, recall.text

        # Only .gitkeep exists and html=True: the missing index.html answers 404
        # from the StaticFiles mount, proving "/" now belongs to the console.
        root = client.get("/")
        assert root.status_code == 404, root.text
