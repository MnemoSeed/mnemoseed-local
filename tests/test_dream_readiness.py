from __future__ import annotations

from types import SimpleNamespace

from mnemoseed_local.cli import models_contain
from mnemoseed_local.config import RoleLLMConfig
from mnemoseed_local.dream.readiness import DreamRouteReadiness


def _route(driver: str = "ollama", model: str = "qwen3.5:9b") -> RoleLLMConfig:
    return RoleLLMConfig(
        role="dream",
        driver=driver,
        model=model,
        params={"base_url": "http://127.0.0.1:11434"},
    )


def test_ollama_unavailable_is_reported_without_pull(monkeypatch) -> None:
    def _fail(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("httpx.get", _fail)
    readiness = DreamRouteReadiness({"dream": _route()})

    assert readiness.refresh() is False
    status = readiness.status()
    assert status["ready"] is False
    assert "connection refused" in status["detail"]
    assert "pull" not in status["detail"]


def test_ollama_model_presence_is_required(monkeypatch) -> None:
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"models": [{"name": "other:7b"}]},
    )
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: response)
    readiness = DreamRouteReadiness({"dream": _route()})

    assert readiness.refresh() is False
    assert "not present" in readiness.status()["detail"]


def test_non_ollama_route_is_not_gated() -> None:
    readiness = DreamRouteReadiness({"dream": _route("openai_compatible")})

    assert readiness.refresh() is True
    assert readiness.status()["ready"] is True


def test_model_matcher_is_shared_with_doctor_semantics() -> None:
    assert models_contain(["qwen:8b"], "qwen:8b")
    assert models_contain(["qwen"], "qwen:latest")
    assert models_contain(["qwen:latest"], "qwen")
    assert not models_contain(["qwen:latest"], "qwen:8b")
