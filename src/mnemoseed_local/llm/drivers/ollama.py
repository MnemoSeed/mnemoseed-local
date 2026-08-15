"""Ollama chat driver (PRD-02 T6; FR-2.7).

A role routed to ``driver = "ollama"`` talks Ollama's native ``/api/chat``
endpoint: no API key, no auth header, ``stream=false`` for a single response.
The default model is a <=14B quantized tag from config (design/02 §6 / FR-2.7);
the PRD's 70B default line is intentionally not used here — that spec conflict
is resolved toward the 14B default and flagged in the T6 report.
"""

from __future__ import annotations

from typing import Any

import httpx

from mnemoseed_local.llm.registry import LLM_DRIVERS, register
from mnemoseed_local.llm.types import (
    ChatResult,
    HealthReport,
    LLMDriverInfo,
    LLMUnavailable,
    Usage,
)


@register(LLM_DRIVERS)
class OllamaLLM:
    """Local Ollama chat via the native /api/chat endpoint (offline, no key)."""

    info = LLMDriverInfo(
        name="ollama",
        description="local Ollama chat via the native /api/chat endpoint (no API key; offline track)",
    )

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1:8b",
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = ""
        self.model = model
        self.timeout = float(timeout)
        self.params: dict[str, Any] = kwargs
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def chat(self, *, system: str, user: str) -> ChatResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        try:
            response = self._client.post("/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailable(f"ollama chat failed: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailable(f"ollama chat failed: {exc}") from exc
        message = body.get("message")
        text = message.get("content", "") if isinstance(message, dict) else ""
        return ChatResult(text=text, usage=_usage_from(body), model=self.model, driver="ollama")

    def check(self) -> HealthReport:
        try:
            response = self._client.get("/api/tags")
            if response.status_code != 200:
                return HealthReport(
                    ok=False,
                    detail={"error": f"GET /api/tags returned HTTP {response.status_code}"},
                )
            models = [m.get("name") for m in response.json().get("models") or [] if isinstance(m, dict)]
            return HealthReport(ok=True, detail={"models": models})
        except (httpx.HTTPError, ValueError) as exc:
            return HealthReport(ok=False, detail={"error": str(exc)})


def _usage_from(body: Any) -> Usage | None:
    data = body.get("usage")
    if not isinstance(data, dict):
        return None
    return Usage(
        prompt_tokens=data.get("prompt_eval_count"),
        completion_tokens=data.get("eval_count"),
    )
