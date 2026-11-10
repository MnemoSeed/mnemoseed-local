"""Read-only readiness gate for the configured dream route.

Three honest states: no configured route (not ready — dreams defer until a
model is bound), an ollama route (probed via ``/api/tags`` + model-tag
presence), and a cloud route (probed through the driver's ``check()``, TTL
throttled so the scheduler tick and the event gate never hammer the provider).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

import httpx

from mnemoseed_local.config import RoleLLMConfig
from mnemoseed_local.llm.types import DreamLLM, LLMError

#: Display/probe fallback when an ollama route omits base_url (mirrors the
#: driver constructor's default).
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"

#: How long a cloud probe's verdict is reused before the next provider call.
#: The scheduler ticks every 60s; each dream event also consults the cached
#: gate, so without a TTL one pending dream could probe the provider per event.
CLOUD_PROBE_TTL_S = 60.0

#: The honest not-ready detail for a fresh install (surfaced by /healthz and
#: the manual-dream reason).
NO_ROUTE_DETAIL = "no dream model configured"


def models_contain(models: list[str], configured: str) -> bool:
    """Match Ollama model names with the same semantics as doctor."""
    if ":" not in configured:
        wanted = {configured, f"{configured}:latest"}
    else:
        name, _, tag = configured.partition(":")
        wanted = {configured, name} if tag == "latest" else {configured}
    return any(model in wanted for model in models)


def _probe_driver(route: RoleLLMConfig) -> DreamLLM:
    """An isolated one-shot probe driver built from the role config through the
    llm registry: its own cache, never the daemon router's state or audit."""
    from mnemoseed_local.llm import RoleRouter

    return RoleRouter(routes={route.role: route}).resolve(route.role)


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
        probe_ttl_s: float = CLOUD_PROBE_TTL_S,
    ) -> None:
        self._routes = routes
        self._timeout_s = timeout_s
        self._clock = clock
        self._probe_ttl_s = probe_ttl_s
        route = self._route()
        self._ready = False
        self._detail = NO_ROUTE_DETAIL if route is None else "not checked"
        self._checked_at: float | None = None

    def _route(self) -> RoleLLMConfig | None:
        return self._routes.get("dream")

    def is_ready(self) -> bool:
        return self._ready

    def refresh(self) -> bool:
        route = self._route()
        if route is None:
            self._ready = False
            self._detail = NO_ROUTE_DETAIL
            self._checked_at = None
            return False
        if route.driver == "ollama":
            return self._refresh_ollama(route)
        return self._refresh_probed(route)

    def _refresh_ollama(self, route: RoleLLMConfig) -> bool:
        base_url = str(route.params.get("base_url", OLLAMA_DEFAULT_BASE_URL)).rstrip("/")
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

    def _refresh_probed(self, route: RoleLLMConfig) -> bool:
        now = self._clock()
        if self._checked_at is not None and now - self._checked_at < self._probe_ttl_s:
            return self._ready
        try:
            driver = _probe_driver(route)
        except LLMError as exc:
            # a route that cannot even materialize (unknown driver, bad params)
            # is a failed probe, never an exception out of refresh()
            self._ready = False
            self._detail = f"provider probe failed ({exc})"
            self._checked_at = now
            return False
        report = driver.check()
        if report.ok:
            self._ready = True
            self._detail = f"provider API ready ({route.driver}); model {route.model!r} reachable"
        else:
            self._ready = False
            self._detail = f"provider probe failed ({report.detail.get('error', 'unknown error')})"
        self._checked_at = now
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
