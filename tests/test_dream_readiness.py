from __future__ import annotations

from types import SimpleNamespace

from mnemoseed_local.cli import models_contain
from mnemoseed_local.config import RoleLLMConfig
from mnemoseed_local.dream.readiness import DreamRouteReadiness
from mnemoseed_local.llm.types import HealthReport


def _route(driver: str = "ollama", model: str = "qwen3.5:9b") -> RoleLLMConfig:
    return RoleLLMConfig(
        role="dream",
        driver=driver,
        model=model,
        params={"base_url": "http://127.0.0.1:11434"},
    )


class _ProbeSpy:
    """A probe-driver double that counts its check() invocations."""

    def __init__(self, report: HealthReport) -> None:
        self.report = report
        self.calls = 0

    def check(self) -> HealthReport:
        self.calls += 1
        return self.report


def _spy_probe(spy: _ProbeSpy):
    return lambda route: spy


def test_unconfigured_route_is_not_ready_and_names_it() -> None:
    """A fresh install has no dream route: readiness is honestly False (never
    the old lie), and /healthz dream_route surfaces driver/model null."""
    readiness = DreamRouteReadiness({})

    assert readiness.refresh() is False
    status = readiness.status()
    assert status["ready"] is False
    assert status["driver"] is None
    assert status["model"] is None
    assert status["detail"] == "no dream model configured"
    assert status["checked_at"] is None


def test_unconfigured_route_stays_not_ready_across_refreshes() -> None:
    readiness = DreamRouteReadiness({})
    assert readiness.refresh() is False
    assert readiness.refresh() is False
    assert readiness.is_ready() is False


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


def test_ollama_ready_when_model_is_present(monkeypatch) -> None:
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"models": [{"name": "qwen3.5:9b"}]},
    )
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: response)
    readiness = DreamRouteReadiness({"dream": _route()})

    assert readiness.refresh() is True
    status = readiness.status()
    assert status["ready"] is True
    assert status["driver"] == "ollama"
    assert "present" in status["detail"]


def test_cloud_route_is_probed_and_ready(monkeypatch) -> None:
    """A configured cloud route is probed through the driver's check() — no
    auto-ready shortcut, no ollama /api/tags call."""
    spy = _ProbeSpy(HealthReport(ok=True, detail={"models": ["deepseek-chat"]}))
    monkeypatch.setattr("mnemoseed_local.dream.readiness._probe_driver", _spy_probe(spy))
    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no ollama probe")))
    readiness = DreamRouteReadiness({"dream": _route("openai_compatible", "deepseek-chat")})

    assert readiness.refresh() is True
    status = readiness.status()
    assert status["ready"] is True
    assert status["driver"] == "openai_compatible"
    assert status["model"] == "deepseek-chat"
    assert "provider API ready" in status["detail"]
    assert "deepseek-chat" in status["detail"]
    assert spy.calls == 1


def test_cloud_route_probe_failure_is_not_ready(monkeypatch) -> None:
    spy = _ProbeSpy(HealthReport(ok=False, detail={"error": "HTTP 401"}))
    monkeypatch.setattr("mnemoseed_local.dream.readiness._probe_driver", _spy_probe(spy))
    readiness = DreamRouteReadiness({"dream": _route("openai_compatible", "deepseek-chat")})

    assert readiness.refresh() is False
    status = readiness.status()
    assert status["ready"] is False
    assert "HTTP 401" in status["detail"]


def test_cloud_probe_is_ttl_throttled(monkeypatch) -> None:
    """Within the TTL window a second refresh() reuses the cached verdict —
    the 60s scheduler tick and the event gate never hammer the provider."""
    spy = _ProbeSpy(HealthReport(ok=True, detail={"models": ["deepseek-chat"]}))
    monkeypatch.setattr("mnemoseed_local.dream.readiness._probe_driver", _spy_probe(spy))
    now = 1_000.0
    readiness = DreamRouteReadiness(
        {"dream": _route("openai_compatible", "deepseek-chat")},
        clock=lambda: now,
    )

    assert readiness.refresh() is True
    assert readiness.refresh() is True
    assert readiness.refresh() is True
    assert spy.calls == 1  # two refreshes inside the window suppressed

    now += 61.0  # past the 60s TTL
    assert readiness.refresh() is True
    assert spy.calls == 2


def test_cloud_probe_reprobes_after_a_failure_once_the_ttl_elapses(monkeypatch) -> None:
    spy = _ProbeSpy(HealthReport(ok=False, detail={"error": "HTTP 503"}))
    monkeypatch.setattr("mnemoseed_local.dream.readiness._probe_driver", _spy_probe(spy))
    now = 1_000.0
    readiness = DreamRouteReadiness(
        {"dream": _route("openai_compatible", "deepseek-chat")},
        clock=lambda: now,
    )

    assert readiness.refresh() is False
    assert spy.calls == 1
    now += 61.0
    assert readiness.refresh() is False
    assert spy.calls == 2


def test_unmaterializable_cloud_route_is_a_failed_probe_never_a_raise(monkeypatch) -> None:
    """refresh() runs on the worker and scheduler paths: a route that cannot
    even materialize (unknown driver) is a not-ready verdict with the error
    detail — never an exception out of the gate."""
    readiness = DreamRouteReadiness({"dream": _route("no_such_driver", "m")})

    assert readiness.refresh() is False
    status = readiness.status()
    assert status["ready"] is False
    assert "no_such_driver" in status["detail"]


def test_model_matcher_is_shared_with_doctor_semantics() -> None:
    assert models_contain(["qwen:8b"], "qwen:8b")
    assert models_contain(["qwen"], "qwen:latest")
    assert models_contain(["qwen:latest"], "qwen")
    assert not models_contain(["qwen:latest"], "qwen:8b")
