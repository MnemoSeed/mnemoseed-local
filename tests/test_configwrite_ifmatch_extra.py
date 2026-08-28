"""HTTP If-Match oracle gaps: 409 detail, W/ handling, rollback lock."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mnemoseed_local.config import load_config
from mnemoseed_local.configwrite.routes import router as configwrite_router
from mnemoseed_local.configwrite.service import ConfigWriteService
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver


def _config_toml(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "# MnemoSeed configuration\n"
        'preset = "embedded"\n'
        'baseurl = "http://localhost:7788"\n'
        "\n"
        "[dream]\n"
        "\n"
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    return path


def _client(tmp_path: Path, *, with_meta: bool = False) -> tuple[TestClient, ConfigWriteService]:
    path = _config_toml(tmp_path)
    config = load_config(path)
    meta = SqliteMetaDriver(path=str(tmp_path / "meta.db")) if with_meta else None
    service = ConfigWriteService(config, meta, clock=lambda: 1_700_000_000.0)
    app = FastAPI()
    app.state.config = config  # type: ignore[attr-defined]
    app.state.configwrite = service  # type: ignore[attr-defined]
    app.include_router(configwrite_router)
    return TestClient(app), service


def test_if_match_empty_returns_409(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    cur = service.generation
    resp = client.post(
        "/api/v1/config/set",
        json={"key_path": "dream.auto_trigger", "value": True},
        headers={"If-Match": ""},
    )
    assert resp.status_code == 409
    assert str(cur) in str(resp.json().get("detail", ""))


def test_if_match_whitespace_only_returns_409(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    cur = service.generation
    resp = client.post(
        "/api/v1/config/set",
        json={"key_path": "dream.auto_trigger", "value": True},
        headers={"If-Match": "   "},
    )
    assert resp.status_code == 409
    assert str(cur) in str(resp.json().get("detail", ""))


def test_if_match_abc_returns_409(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    cur = service.generation
    resp = client.post(
        "/api/v1/config/set",
        json={"key_path": "dream.auto_trigger", "value": True},
        headers={"If-Match": "abc"},
    )
    assert resp.status_code == 409
    assert str(cur) in str(resp.json().get("detail", ""))


def test_if_match_w_prefix_handled(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    gen = client.get("/api/v1/config").json()["generation"]
    resp = client.post(
        "/api/v1/config/set",
        json={"key_path": "dream.auto_trigger", "value": True},
        headers={"If-Match": f"W/{gen}"},
    )
    assert resp.status_code == 200


def test_if_match_w_quoted_handled(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    gen = client.get("/api/v1/config").json()["generation"]
    resp = client.post(
        "/api/v1/config/set",
        json={"key_path": "dream.auto_trigger", "value": True},
        headers={"If-Match": f'W/"{gen}"'},
    )
    assert resp.status_code == 200


def test_if_match_inner_spaces_handled(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    gen = client.get("/api/v1/config").json()["generation"]
    resp = client.post(
        "/api/v1/config/set",
        json={"key_path": "dream.auto_trigger", "value": True},
        headers={"If-Match": f" {gen} "},
    )
    assert resp.status_code == 200


def test_rollback_bumps_generation(tmp_path: Path) -> None:
    client, service = _client(tmp_path, with_meta=True)
    first = service.set("dream.auto_trigger", True, actor="console")["version_id"]
    gen_before = client.get("/api/v1/config").json()["generation"]
    resp = client.post("/api/v1/config/rollback", json={"version_id": first})
    assert resp.status_code == 200
    gen_after = client.get("/api/v1/config").json()["generation"]
    assert gen_after == gen_before + 1


def test_failed_write_does_not_bump_generation(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    service.set("dream.auto_trigger", True, actor="console")
    cur = service.generation
    assert cur == 1
    resp = client.post(
        "/api/v1/config/set",
        json={"key_path": "dream.auto_trigger", "value": False},
        headers={"If-Match": "0"},
    )
    assert resp.status_code == 409
    assert service.generation == cur
    assert client.get("/api/v1/config").json()["generation"] == cur


def test_concurrent_writes_exactly_one_200_one_409(tmp_path: Path) -> None:
    path = _config_toml(tmp_path)
    config = load_config(path)
    service = ConfigWriteService(config, None, clock=lambda: 1_700_000_000.0)
    app = FastAPI()
    app.state.config = config  # type: ignore[attr-defined]
    app.state.configwrite = service  # type: ignore[attr-defined]
    app.include_router(configwrite_router)
    client = TestClient(app)

    gen = client.get("/api/v1/config").json()["generation"]
    assert gen == 0

    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def do_write() -> None:
        barrier.wait()
        resp = client.post(
            "/api/v1/config/set",
            json={"key_path": "dream.auto_trigger", "value": True},
            headers={"If-Match": str(gen)},
        )
        with lock:
            results.append(resp.status_code)

    t1 = threading.Thread(target=do_write)
    t2 = threading.Thread(target=do_write)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(results) == [200, 409]


def test_rollback_with_stale_if_match_returns_409(tmp_path: Path) -> None:
    client, service = _client(tmp_path, with_meta=True)
    first = service.set("dream.auto_trigger", True, actor="console")["version_id"]
    service.set("dream.auto_trigger", False, actor="console")
    cur = service.generation
    assert cur == 2
    resp = client.post(
        "/api/v1/config/rollback",
        json={"version_id": first},
        headers={"If-Match": "0"},
    )
    assert resp.status_code == 409
    assert str(cur) in str(resp.json().get("detail", ""))


def test_concurrent_rollbacks_exactly_one_200_one_409(tmp_path: Path) -> None:
    path = _config_toml(tmp_path)
    config = load_config(path)
    meta = SqliteMetaDriver(path=str(tmp_path / "meta.db"))
    service = ConfigWriteService(config, meta, clock=lambda: 1_700_000_000.0)
    first = service.set("dream.auto_trigger", True, actor="console")["version_id"]
    service.set("dream.auto_trigger", False, actor="console")
    app = FastAPI()
    app.state.config = config  # type: ignore[attr-defined]
    app.state.configwrite = service  # type: ignore[attr-defined]
    app.include_router(configwrite_router)
    client = TestClient(app)

    gen = client.get("/api/v1/config").json()["generation"]
    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def do_rollback() -> None:
        barrier.wait()
        resp = client.post(
            "/api/v1/config/rollback",
            json={"version_id": first},
            headers={"If-Match": str(gen)},
        )
        with lock:
            results.append(resp.status_code)

    t1 = threading.Thread(target=do_rollback)
    t2 = threading.Thread(target=do_rollback)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(results) == [200, 409]
