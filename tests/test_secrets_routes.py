"""S5c secret-value write route: four-sink key absence, name allowlist,
credential gate and read-side wiring through one injected SecretStore.

All contracts are exercised through the HTTP surface and RoleRouter.resolve().
No OS keyring probe, no live provider call and no installed-runtime touch: the
store is injected and the config home is a temp dir.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import auth_support
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIRouter
from fastapi.testclient import TestClient

from mnemoseed_local.config import LLM_ROLES
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.daemon.secrets_routes import router as secrets_router
from mnemoseed_local.llm import RoleRouter
from mnemoseed_local.llm.registry import LLMRegistry
from mnemoseed_local.llm.types import LLMDriverInfo
from mnemoseed_local.secrets.store import SecretStore
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

KEY = "sk-live-secret-9f3a2b"
NAME = "mnemoseed/dream/dream"
RESERVED = "daemon/credential"

_REAL_DRIVERS: tuple[tuple[object, type], ...] = (
    (VECTOR_DRIVERS, LanceDbEmbeddedStore),
    (GRAPH_DRIVERS, SqliteGraphDriver),
    (META_DRIVERS, SqliteMetaDriver),
    (EMBED_DRIVERS, SyntheticEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_real_drivers_registered() -> None:
    """Re-register real storage drivers after the shared registries are cleared."""
    for registry, cls in _REAL_DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


class _MemoryStore:
    """Injected SecretStore stand-in: records writes and holds full values."""

    backend_name = "memory"

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.calls: list[tuple[str, str]] = []

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.calls.append((name, value))
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)

    def exists(self, name: str) -> bool:
        return name in self.values

    def masked_tail(self, name: str) -> str | None:
        value = self.values.get(name)
        return value[-4:] or None if value else None


class _RaisingStore(_MemoryStore):
    """Backend whose failure text carries the value, to test the str(exc) sink."""

    def set(self, name: str, value: str) -> None:
        raise RuntimeError(f"backend refused {value} for {name}")


class _SpyAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def audit_append(self, entry: Any) -> None:
        self.entries.append({"action": getattr(entry, "action", ""), "detail": getattr(entry, "detail", {})})


class _OkDriver:
    info = LLMDriverInfo(name="secret_ok")

    def __init__(self, **params: Any) -> None:
        self.params = params
        self.api_key = params.get("api_key", "")

    def check(self) -> Any:
        from mnemoseed_local.llm.types import HealthReport

        return HealthReport(ok=True)


def _registry() -> LLMRegistry:
    registry = LLMRegistry("secrets-test")
    registry.add(_OkDriver)
    return registry


def _app(store: SecretStore, audit: _SpyAudit | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(secrets_router)
    app.state.secret_store = store
    if audit is not None:
        app.state.audit = audit
    return app


def _client(store: SecretStore, audit: _SpyAudit | None = None) -> TestClient:
    return TestClient(_app(store, audit))


def _body(resp: TestClient) -> str:
    return resp.content.decode("utf-8", "replace")


def _http_paths(app: FastAPI) -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            paths.add(route.path)
        elif isinstance(route, APIRouter):
            paths.update(r.path for r in route.routes if isinstance(r, APIRoute))
        else:
            included = getattr(route, "include_context", None)
            original = getattr(included, "included_router", None) if included is not None else None
            if original is not None:
                paths.update(r.path for r in original.routes if isinstance(r, APIRoute))
    return paths


def _llm_config(home: Path) -> Path:
    cfg = home / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "openai_compatible"\n'
        'model = "test-model"\n'
        f'api_key_env = "secrets:{NAME}"\n'
        'base_url = "https://api.test/v1"\n',
        encoding="utf-8",
    )
    return cfg


def test_set_success_writes_full_value_and_returns_whitelisted_fields() -> None:
    store = _MemoryStore()
    resp = _client(store).post("/api/v1/secrets/set", json={"name": NAME, "value": KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "name", "backend"}
    assert body["ok"] is True and body["name"] == NAME
    assert store.calls == [(NAME, KEY)]
    assert store.get(NAME) == KEY


def test_four_sinks_carry_no_key_on_success(caplog: pytest.LogCaptureFixture) -> None:
    store = _MemoryStore()
    audit = _SpyAudit()
    client = _client(store, audit)
    with caplog.at_level(logging.DEBUG):
        resp = client.post("/api/v1/secrets/set", json={"name": NAME, "value": KEY})
    assert resp.status_code == 200
    assert KEY not in _body(resp)
    assert KEY[-4:] not in _body(resp)
    assert all(KEY not in json.dumps(entry) for entry in audit.entries)
    assert KEY not in caplog.text


def test_no_key_in_response_when_backend_raises_with_value() -> None:
    client = _client(_RaisingStore())
    resp = client.post("/api/v1/secrets/set", json={"name": NAME, "value": KEY})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "secret write failed"
    assert KEY not in _body(resp)


def test_malformed_body_returns_fixed_detail_without_echo() -> None:
    client = _client(_MemoryStore())
    resp = client.post("/api/v1/secrets/set", json={"name": NAME, "value": {"nested": KEY}})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid request"
    assert KEY not in _body(resp)

    raw = b'{"name":"' + NAME.encode() + b'","value":' + KEY.encode() + b",}"
    resp = client.post("/api/v1/secrets/set", content=raw, headers={"content-type": "application/json"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid request"
    assert KEY not in _body(resp)


def test_request_logging_on_still_logs_only_method_path_status(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mnemoseed_local.daemon import app as app_mod

    class _Logging:
        requests = True

    class _Cfg:
        logging = _Logging()

    store = _MemoryStore()
    app = _app(store)
    app.state.config = _Cfg()
    app.add_middleware(app_mod._request_logging_middleware, get_state=lambda: app.state)
    with caplog.at_level(logging.DEBUG):
        resp = TestClient(app).post("/api/v1/secrets/set", json={"name": NAME, "value": KEY})
    assert resp.status_code == 200
    assert KEY not in caplog.text


@pytest.mark.parametrize("role", LLM_ROLES)
def test_allowed_role_names_are_accepted(role: str) -> None:
    name = f"mnemoseed/dream/{role}"
    store = _MemoryStore()
    resp = _client(store).post("/api/v1/secrets/set", json={"name": name, "value": KEY})
    assert resp.status_code == 200
    assert store.calls == [(name, KEY)]


@pytest.mark.parametrize(
    "name",
    [
        "daemon/credential",
        "daemon.credential",
        "Daemon/Credential",
        "daemon credential",
        "daemon@credential",
        "mnemoseed/llm/x",
        "mnemoseed/dream/nope",
        "mnemoseed/dream/DREAM",
        "other",
        "",
    ],
)
def test_reserved_and_unknown_names_are_rejected(name: str) -> None:
    store = _MemoryStore()
    resp = _client(store).post("/api/v1/secrets/set", json={"name": name, "value": KEY})
    assert resp.status_code in (403, 422)
    assert store.calls == []
    assert KEY not in _body(resp)


def test_route_is_default_deny_without_credential() -> None:
    app = create_app(secret_store=_MemoryStore())
    resp = TestClient(app).post("/api/v1/secrets/set", json={"name": NAME, "value": KEY})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "daemon credential required"
    assert KEY not in _body(resp)


def test_create_app_registers_secret_route() -> None:
    app = create_app(secret_store=_MemoryStore())
    assert "/api/v1/secrets/set" in _http_paths(app)


def test_role_router_without_store_leaves_api_key_empty() -> None:
    from mnemoseed_local.config import RoleLLMConfig

    routes = {
        "dream": RoleLLMConfig(
            role="dream",
            driver="secret_ok",
            model="test-model",
            params={"api_key_env": f"secrets:{NAME}", "base_url": "https://api.test/v1"},
        )
    }
    router = RoleRouter(routes=routes, registry=_registry(), secrets=None)
    assert router.resolve("dream").api_key == ""


def test_role_router_with_injected_store_resolves_key() -> None:
    from mnemoseed_local.config import RoleLLMConfig

    routes = {
        "dream": RoleLLMConfig(
            role="dream",
            driver="secret_ok",
            model="test-model",
            params={"api_key_env": f"secrets:{NAME}", "base_url": "https://api.test/v1"},
        )
    }
    store = _MemoryStore({NAME: KEY})
    router = RoleRouter(routes=routes, registry=_registry(), secrets=store)
    assert router.resolve("dream").api_key == KEY


def test_create_app_wires_one_store_to_write_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mnemoseed_local import config as config_mod

    cfg = _llm_config(tmp_path)
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)

    credential = auth_support.seed_credential(tmp_path)
    # Restart shape (O-v4-1): the injected store already holds the value the
    # wizard wrote before the last config write, so boot's resolve sees it.
    store = _MemoryStore({NAME: KEY})
    app = create_app(secret_store=store)
    with (
        auth_support.isolated_home(tmp_path),
        TestClient(app, headers=auth_support.credential_headers(credential)) as client,
    ):
        assert app.state.secret_store is store
        assert app.state.role_router.resolve("dream").api_key == KEY
        resp = client.post(
            "/api/v1/secrets/set", json={"name": "mnemoseed/dream/dream_verifier", "value": KEY}
        )
        assert resp.status_code == 200
        assert store.calls[-1] == ("mnemoseed/dream/dream_verifier", KEY)


def test_wizard_writes_secret_before_binding_and_clears_after() -> None:
    import mnemoseed_local

    js = (Path(mnemoseed_local.__file__).parent / "console" / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index("async function wizardBind()")
    end = js.index("\nfunction ", start)
    body = js[start:end]
    secret_write = body.index('"/api/v1/secrets/set"')
    ref_push = body.index('key_path:"dream.llm.dream.api_key_env"')
    bind = body.index("writeRoleBatch(")
    clear = body.index('keyInput.value = ""')
    assert secret_write < ref_push < bind
    assert secret_write < clear


def test_bare_create_app_does_not_probe_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    import mnemoseed_local.secrets.store as store_mod

    def explode() -> Any:
        raise AssertionError("OS keyring probed by bare create_app()")

    monkeypatch.setattr(store_mod, "_probe_keyring_store", explode)
    monkeypatch.delenv("MNEMOSEED_SECRET_BACKEND", raising=False)
    app = create_app()
    assert app.state.secret_store is not None
    assert "/api/v1/secrets/set" in _http_paths(app)


def test_injected_store_skips_default_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    import mnemoseed_local.secrets.store as store_mod

    def explode() -> Any:
        raise AssertionError("OS keyring probed despite injected store")

    monkeypatch.setattr(store_mod, "_probe_keyring_store", explode)
    monkeypatch.delenv("MNEMOSEED_SECRET_BACKEND", raising=False)
    store = _MemoryStore()
    app = create_app(secret_store=store)
    assert app.state.secret_store is store


def test_lazy_default_defers_probe_until_first_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import mnemoseed_local.secrets.store as store_mod

    calls: list[int] = []

    def probe() -> Any:
        calls.append(1)
        return None

    monkeypatch.setattr(store_mod, "_probe_keyring_store", probe)
    monkeypatch.delenv("MNEMOSEED_SECRET_BACKEND", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)

    app = create_app()
    assert calls == []
    app.state.secret_store.exists(NAME)
    assert len(calls) == 1
