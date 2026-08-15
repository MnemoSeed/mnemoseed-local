"""OpenAI-compatible chat driver (PRD-02 T6; design/02 §6).

First payload driver for the short_increment role: talks any OpenAI-compatible
``/chat/completions`` endpoint over HTTP with a Bearer key. Construction
performs no network I/O (httpx clients are lazy; tests swap ``_client`` for a
MockTransport one). Transport or auth failure inside ``chat`` raises the typed
``LLMUnavailable`` (FR-2.6); ``check()`` never raises.
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
class OpenAICompatibleLLM:
    """Chat via any OpenAI-compatible ``/chat/completions`` endpoint."""

    info = LLMDriverInfo(
        name="openai_compatible",
        description="chat via an OpenAI-compatible /chat/completions endpoint (Bearer key)",
    )

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int = 2048,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> None:
        if not base_url:
            raise ValueError("openai_compatible requires a non-empty 'base_url'")
        if not model:
            raise ValueError("openai_compatible requires a non-empty 'model'")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.params: dict[str, Any] = kwargs
        # Auth rides on each request (not the client): tests swap ``_client`` for
        # a MockTransport one, and the header must survive that swap.
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def chat(self, *, system: str, user: str) -> ChatResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
        }
        try:
            response = self._client.post("/chat/completions", json=payload, headers=self._headers)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailable(f"openai_compatible chat failed: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailable(f"openai_compatible chat failed: {exc}") from exc
        text = _first_choice_text(body)
        usage = _usage_from(body.get("usage"))
        return ChatResult(text=text, usage=usage, model=self.model, driver="openai_compatible")

    def check(self) -> HealthReport:
        try:
            response = self._client.get("/models", headers=self._headers)
            if response.status_code != 200:
                return HealthReport(
                    ok=False,
                    detail={"error": f"GET /models returned HTTP {response.status_code}"},
                )
            models = _id_list(response.json().get("data"))
            return HealthReport(ok=True, detail={"models": models})
        except (httpx.HTTPError, ValueError) as exc:
            return HealthReport(ok=False, detail={"error": str(exc)})


def _first_choice_text(body: Any) -> str:
    """First assistant message text from an OpenAI-shaped completion body."""
    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def _id_list(data: Any) -> list[Any]:
    """Flatten OpenAI/Anthropic model catalogs to their id fields."""
    return [m.get("id") for m in data or [] if isinstance(m, dict)]


def _usage_from(data: Any) -> Usage | None:
    if not isinstance(data, dict):
        return None
    return Usage(
        prompt_tokens=data.get("prompt_tokens"),
        completion_tokens=data.get("completion_tokens"),
    )
