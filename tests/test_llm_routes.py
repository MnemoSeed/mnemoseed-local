"""S5 LLM HTTP surface: catalog / probe / models / detect-local.

Every contract is exercised through the HTTP surface with an injectable ad-hoc
driver registry and MockTransport. No daemon boot, no live provider API, and no
API key material in any of the four sinks (response, audit, daemon.log and
exception text).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mnemoseed_local.llm import routes as llm_routes
from mnemoseed_local.llm.registry import LLMRegistry
from mnemoseed_local.llm.types import HealthReport, LLMDriverInfo

KEY = "sk-live-secret-9f3a2b"
KEY_QUERY_URL = f"https://api.test/v1?api_key={KEY}&model=gpt-x"


class _OkDriver:
    info = LLMDriverInfo(name="ok_probe")

    def __init__(self, **params: Any) -> None:
        self.params = params

    def check(self) -> HealthReport:
        return HealthReport(ok=True, detail={"models": ["gpt-test"]})


class _EchoDriver:
    """Ad-hoc driver that echoes request bytes back through check() detail.

    Stands in for a real driver whose transport error text carries the full
    request URL (base_url + query) and api key; the route must scrub it.
    """

    info = LLMDriverInfo(name="echo_probe")

    def __init__(self, **params: Any) -> None:
        self.params = params

    def check(self) -> HealthReport:
        key = self.params.get("api_key", "")
        base = self.params.get("base_url", "")
        return HealthReport(
            ok=False,
            detail={"error": f"auth failed for {key} at {base}", "models": [key] if key else []},
        )


def _echoed(**params: Any) -> list[str]:
    return [value for value in params.values() if isinstance(value, str) and value]


class _ParamEchoDriver:
    """Ad-hoc driver reflecting every request string value into check() detail."""

    info = LLMDriverInfo(name="param_echo_probe")

    def __init__(self, **params: Any) -> None:
        self.params = params

    def check(self) -> HealthReport:
        echoed = _echoed(**self.params)
        return HealthReport(ok=False, detail={"error": " ".join(echoed), "models": echoed})


class _RaiseDriver:
    """Ad-hoc driver whose constructor raises with the key in its message."""

    info = LLMDriverInfo(name="raise_probe")

    def __init__(self, **params: Any) -> None:
        raise ValueError(f"bad base_url {params.get('base_url', '')}")

    def check(self) -> HealthReport:
        return HealthReport(ok=True)


class _ParamRaiseDriver:
    """Ad-hoc driver whose constructor raises with every request string."""

    info = LLMDriverInfo(name="param_raise_probe")

    def __init__(self, **params: Any) -> None:
        raise ValueError("driver failed: " + " ".join(_echoed(**params)))

    def check(self) -> HealthReport:
        return HealthReport(ok=True)


class _SpyAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def audit_append(self, entry: Any) -> None:
        self.entries.append({"action": getattr(entry, "action", ""), "detail": getattr(entry, "detail", {})})


def _registry(*drivers: type[Any]) -> LLMRegistry:
    reg = LLMRegistry("llm-test")
    for driver in drivers:
        reg.add(driver)
    return reg


def _app(registry: LLMRegistry, audit: _SpyAudit | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(llm_routes.router)
    app.state.llm_registry = registry
    if audit is not None:
        app.state.audit = audit
    return app


def _client(registry: LLMRegistry, audit: _SpyAudit | None = None) -> TestClient:
    return TestClient(_app(registry, audit))


def _body(resp: TestClient) -> str:
    return resp.content.decode("utf-8", "replace")


def test_catalog_lists_presets_and_registered_drivers() -> None:
    resp = _client(_registry(_OkDriver)).get("/api/v1/llm/catalog")
    assert resp.status_code == 200
    body = resp.json()
    ids = {p["id"] for p in body["presets"]}
    assert {"openai", "anthropic", "custom"} <= ids
    assert "ok_probe" in body["drivers"]


def test_probe_returns_ok_health_report() -> None:
    resp = _client(_registry(_OkDriver)).post(
        "/api/v1/llm/probe",
        json={"driver": "ok_probe", "params": {"api_key": KEY, "base_url": "https://api.test/v1"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "gpt-test" in body["models"]


def test_probe_no_key_in_response_when_driver_echoes_key() -> None:
    resp = _client(_registry(_EchoDriver)).post(
        "/api/v1/llm/probe",
        json={"driver": "echo_probe", "params": {"api_key": KEY, "base_url": "https://api.test/v1"}},
    )
    assert KEY not in _body(resp)


def test_probe_no_key_in_response_when_key_embedded_in_base_url_query() -> None:
    resp = _client(_registry(_EchoDriver)).post(
        "/api/v1/llm/probe",
        json={"driver": "echo_probe", "params": {"api_key": "", "base_url": KEY_QUERY_URL}},
    )
    assert KEY not in _body(resp)


def test_probe_no_key_in_exception_text_when_constructor_raises() -> None:
    class _State:
        llm_registry = _registry(_RaiseDriver)

    class _Req:
        app = type("_App", (), {"state": _State()})()

    with pytest.raises(HTTPException) as excinfo:
        llm_routes.probe(  # type: ignore[arg-type]
            _Req(), {"driver": "raise_probe", "params": {"api_key": KEY, "base_url": KEY_QUERY_URL}}
        )
    assert KEY not in str(excinfo.value)


def test_probe_no_key_in_audit_or_daemon_log(caplog: pytest.LogCaptureFixture) -> None:
    audit = _SpyAudit()
    client = _client(_registry(_EchoDriver), audit)
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/api/v1/llm/probe",
            json={"driver": "echo_probe", "params": {"api_key": KEY, "base_url": KEY_QUERY_URL}},
        )
    assert all(KEY not in json.dumps(e) for e in audit.entries)
    assert KEY not in caplog.text


@pytest.mark.parametrize(
    "params",
    [
        {"api_key": KEY, "base_url": "https://api.test/v1"},
        {"api_key": "", "base_url": KEY_QUERY_URL},
        {"api_key": "benign", "base_url": "https://api.test/v1", "model": KEY},
    ],
    ids=["api_key_field", "base_url_query", "model_field"],
)
def test_probe_no_key_in_any_sink_for_every_request_string(
    params: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    audit = _SpyAudit()
    client = _client(_registry(_ParamEchoDriver), audit)
    with caplog.at_level(logging.DEBUG):
        resp = client.post("/api/v1/llm/probe", json={"driver": "param_echo_probe", "params": params})
    assert resp.status_code == 200
    assert KEY not in _body(resp)
    assert all(KEY not in json.dumps(e) for e in audit.entries)
    assert KEY not in caplog.text

    class _State:
        llm_registry = _registry(_ParamRaiseDriver)

    class _Req:
        app = type("_App", (), {"state": _State()})()

    with pytest.raises(HTTPException) as excinfo:
        llm_routes.probe(  # type: ignore[arg-type]
            _Req(), {"driver": "param_raise_probe", "params": params}
        )
    assert KEY not in str(excinfo.value)


def test_probe_unknown_driver_is_refused_without_key() -> None:
    resp = _client(_registry(_OkDriver)).post(
        "/api/v1/llm/probe",
        json={"driver": "no_such_driver", "params": {"api_key": KEY, "base_url": KEY_QUERY_URL}},
    )
    assert resp.status_code in (400, 404, 422)
    assert KEY not in _body(resp)


def test_probe_never_uses_role_router_cache() -> None:
    """An ad-hoc driver works with no configured dream route; probe does not
    depend on RoleRouter state, so it cannot mutate its cache."""
    resp = _client(_registry(_OkDriver)).post(
        "/api/v1/llm/probe",
        json={
            "driver": "ok_probe",
            "params": {"api_key": KEY, "model": "m", "base_url": "https://api.test/v1"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_models_returns_scrubbed_model_list() -> None:
    resp = _client(_registry(_EchoDriver)).post(
        "/api/v1/llm/models",
        json={
            "preset": "openai",
            "driver": "echo_probe",
            "params": {"api_key": KEY, "base_url": "https://api.test/v1"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["models"], list)
    assert KEY not in _body(resp)
    assert "gpt-4o" in body["models"]


def test_detect_local_uses_ollama_host_and_mock_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"models": [{"name": "heretic-9b"}]})

    def fake_client(timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:15434")
    monkeypatch.setattr(llm_routes, "_ollama_client", fake_client)
    resp = _client(_registry()).post("/api/v1/llm/detect-local", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert "heretic-9b" in body["models"]
    assert "15434" in seen["url"]


def test_detect_local_unreachable_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_client(timeout: float) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline")

        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(llm_routes, "_ollama_client", fake_client)
    resp = _client(_registry()).post("/api/v1/llm/detect-local", json={})
    assert resp.status_code == 200
    assert resp.json()["available"] is False
