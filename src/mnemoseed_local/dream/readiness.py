"""Read-only readiness gate for the configured dream route."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

import httpx

from mnemoseed_local.config import RoleLLMConfig


def models_contain(models: list[str], configured: str) -> bool:
    """Match Ollama model names with the same semantics as doctor."""
    if ":" not in configured:
        wanted = {configured, f"{configured}:latest"}
    else:
        name, _, tag = configured.partition(":")
        wanted = {configured, name} if tag == "latest" else {configured}
    return any(model in wanted for model in models)


class DreamRouteReadiness:
    """Track whether the configured dream route can accept a new dream.

    The daemon remains available while this is false. The scheduler and score
    pool use the cached result to defer emission without draining pending work.
    """

    def __init__(
        self,
        routes: Mapping[str, RoleLLMConfig],
        *,
        timeout_s: float = 1.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._routes = routes
        self._timeout_s = timeout_s
        self._clock = clock
        route = self._route()
        self._ready = route is None or route.driver != "ollama"
        self._detail = "not checked" if not self._ready else "not an Ollama route"
        self._checked_at: float | None = None

    def _route(self) -> RoleLLMConfig | None:
        return self._routes.get("dream")

    def is_ready(self) -> bool:
        return self._ready

    def refresh(self) -> bool:
        route = self._route()
        if route is None or route.driver != "ollama":
            self._ready = True
            self._detail = "not an Ollama route"
            self._checked_at = self._clock()
            return True

        base_url = str(route.params.get("base_url", "http://localhost:11434")).rstrip("/")
        try:
            response = httpx.get(f"{base_url}/api/tags", timeout=self._timeout_s)
            response.raise_for_status()
            body = response.json()
            models = body.get("models", []) if isinstance(body, dict) else []
            names = {str(item.get("name")) for item in models if isinstance(item, dict) and item.get("name")}
            if not models_contain(sorted(names), route.model):
                self._ready = False
                self._detail = f"Ollama model {route.model!r} is not present; pull it explicitly when ready"
            else:
                self._ready = True
                self._detail = f"Ollama API ready; model {route.model!r} is present"
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self._ready = False
            self._detail = f"Ollama API unavailable at {base_url}: {exc}"
        self._checked_at = self._clock()
        return self._ready

    def status(self) -> dict[str, object]:
        route = self._route()
        return {
            "ready": self._ready,
            "driver": route.driver if route is not None else None,
            "model": route.model if route is not None else None,
            "detail": self._detail,
            "checked_at": self._checked_at,
        }
