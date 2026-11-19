"""LLM provider HTTP surface: catalog, probe, models and local detection.

Read-only/transient routes for the console provider wizard. The probe builds an
ad-hoc driver, calls ``check()`` and drops it: it never touches the RoleRouter
cache and never persists, audits or logs the API key. Error detail is reduced to
a whitelist so a driver's transport text (which may carry the full request URL
or the key itself) cannot reach any response sink.
"""

from __future__ import annotations

import os
from typing import Any, cast
from urllib.parse import parse_qsl, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request

from mnemoseed_local.llm.providers import PROVIDER_PRESETS
from mnemoseed_local.llm.registry import LLM_DRIVERS, LLMRegistry
from mnemoseed_local.llm.types import HealthReport, LLMError

router = APIRouter(prefix="/api/v1/llm", tags=["llm"])

_OLLAMA_DEFAULT = "http://127.0.0.1:11434"
_REDACTED = "[REDACTED]"
_CATALOG_PRESET_FIELDS = (
    "id",
    "display",
    "protocol",
    "base_url",
    "auth_methods",
    "model_list_source",
    "curated_models",
    "docs_url",
    "token_param",
)


def _registry(request: Request) -> LLMRegistry:
    return cast(LLMRegistry, getattr(request.app.state, "llm_registry", LLM_DRIVERS))


def _ollama_client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def _string_values(value: Any) -> set[str]:
    """Collect every non-empty string carried in a request value."""
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, dict):
        found: set[str] = set()
        for item in value.values():
            found.update(_string_values(item))
        return found
    if isinstance(value, (list, tuple)):
        found = set()
        for item in value:
            found.update(_string_values(item))
        return found
    return set()


def _secret_values(body: dict[str, Any]) -> frozenset[str]:
    """Treat every request string as key material: a driver may reflect any
    field back into a sink, including a query value embedded in base_url."""
    values = _string_values(body)
    params = body.get("params")
    base_url = params.get("base_url") if isinstance(params, dict) else None
    if isinstance(base_url, str):
        values.update(_string_values(parse_qsl(urlparse(base_url).query, keep_blank_values=True)))
    return frozenset(values)


def _scrub(value: str, secrets: frozenset[str]) -> str:
    for secret in secrets:
        value = value.replace(secret, _REDACTED)
    return value


def _safe_report(report: Any, secrets: frozenset[str]) -> dict[str, Any]:
    """Whitelisted probe response: status and model ids only, never raw
    driver error text (which can carry request bytes or the key)."""
    detail = report.detail if isinstance(getattr(report, "detail", None), dict) else {}
    raw_models = detail.get("models")
    models = (
        [_scrub(m, secrets) for m in raw_models if isinstance(m, str)] if isinstance(raw_models, list) else []
    )
    ok = bool(getattr(report, "ok", False))
    return {"ok": ok, "status": "ok" if ok else "unavailable", "models": models}


def _build_params(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    driver = body.get("driver")
    params = body.get("params") or {}
    if not isinstance(driver, str) or not driver:
        raise HTTPException(status_code=422, detail="body.driver must be a non-empty string")
    if not isinstance(params, dict):
        raise HTTPException(status_code=422, detail="body.params must be an object")
    return driver, dict(params)


@router.get("/catalog")
def catalog(request: Request) -> dict[str, Any]:
    presets = [
        {field: _preset_value(preset, field) for field in _CATALOG_PRESET_FIELDS}
        for preset in PROVIDER_PRESETS
    ]
    return {"presets": presets, "drivers": list(_registry(request).names())}


def _preset_value(preset: Any, field: str) -> Any:
    value = getattr(preset, field)
    return list(value) if isinstance(value, tuple) else value


@router.post("/probe")
def probe(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    driver_name, params = _build_params(body)
    secrets = _secret_values(body)
    try:
        driver = _registry(request).build(driver_name, params)
    except LLMError:
        raise HTTPException(status_code=422, detail="provider probe failed") from None
    try:
        report = driver.check()
    except Exception:
        report = HealthReport(ok=False)
    return _safe_report(report, secrets)


@router.post("/models")
def models(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    driver_name, params = _build_params(body)
    secrets = _secret_values(body)
    try:
        driver = _registry(request).build(driver_name, params)
    except LLMError:
        raise HTTPException(status_code=422, detail="provider model list failed") from None
    try:
        report = driver.check()
    except Exception:
        report = HealthReport(ok=False)
    result = _safe_report(report, secrets)
    preset_id = body.get("preset")
    if isinstance(preset_id, str):
        for preset in PROVIDER_PRESETS:
            if preset.id == preset_id:
                for name in preset.curated_models:
                    if name not in result["models"]:
                        result["models"].insert(0, name)
                break
    return {"ok": result["ok"], "models": result["models"]}


@router.post("/detect-local")
def detect_local() -> dict[str, Any]:
    base_url = os.environ.get("OLLAMA_HOST") or _OLLAMA_DEFAULT
    url = base_url.rstrip("/") + "/api/tags"
    empty: dict[str, Any] = {"available": False, "models": [], "base_url": base_url}
    try:
        with _ollama_client(2.0) as client:
            response = client.get(url)
        if response.status_code != 200:
            return empty
        payload = response.json()
    except httpx.HTTPError, ValueError:
        return empty
    data = payload.get("models") if isinstance(payload, dict) else None
    names = [m.get("name") for m in data if isinstance(m, dict)] if isinstance(data, list) else []
    found = [name for name in names if isinstance(name, str)]
    return {"available": True, "models": found, "base_url": base_url}
