"""Anthropic native chat driver (PRD-B7 S2; PRD-B7 §3.2).

Talks Anthropic's native ``/v1/messages`` endpoint: ``x-api-key`` +
``anthropic-version`` headers, ``system`` as a top-level parameter (not a
message), and a required per-request ``max_tokens``. Construction performs no
network I/O (httpx clients are lazy; tests swap ``_client`` for a
MockTransport one). Transport or auth failure inside ``chat`` raises the typed
``LLMUnavailable`` (FR-2.6); ``check()`` never raises.
"""

from __future__ import annotations

from typing import Any

import httpx

from mnemoseed_local.llm.drivers.openai_compatible import _id_list
from mnemoseed_local.llm.registry import LLM_DRIVERS, register
from mnemoseed_local.llm.types import (
    ChatResult,
    HealthReport,
    LLMDriverInfo,
    LLMUnavailable,
    Usage,
)


@register(LLM_DRIVERS)
class AnthropicLLM:
    """Chat via Anthropic's native /v1/messages endpoint."""

    info = LLMDriverInfo(
        name="anthropic",
        description="chat via the Anthropic native /v1/messages endpoint (x-api-key)",
    )

    def __init__(
        self,
        base_url: str = "https://api.anthropic.com",
        api_key: str = "",
        model: str = "",
        max_tokens: int = 4096,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> None:
        if not base_url:
            raise ValueError("anthropic requires a non-empty 'base_url'")
        if not model:
            raise ValueError("anthropic requires a non-empty 'model'")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.params: dict[str, Any] = kwargs
        # Headers ride on each request (tests swap ``_client`` for a
        # MockTransport one, and the header must survive that swap).
        self._headers: dict[str, str] = {"anthropic-version": "2023-06-01"}
        if api_key:
            self._headers["x-api-key"] = api_key
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def chat(self, *, system: str, user: str) -> ChatResult:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            response = self._client.post("/v1/messages", json=payload, headers=self._headers)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailable(f"anthropic chat failed: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailable(f"anthropic chat failed: {exc}") from exc
        if not isinstance(body, dict):
            body = {}
        return ChatResult(
            text=_text_from(body),
            usage=_usage_from(body.get("usage")),
            model=self.model,
            driver="anthropic",
        )

    def check(self) -> HealthReport:
        try:
            response = self._client.get("/v1/models", headers=self._headers)
            if response.status_code != 200:
                return HealthReport(
                    ok=False,
                    detail={"error": f"GET /v1/models returned HTTP {response.status_code}"},
                )
            body = response.json()
            models = _id_list(body.get("data") if isinstance(body, dict) else None)
            return HealthReport(ok=True, detail={"models": models})
        except (httpx.HTTPError, ValueError) as exc:
            return HealthReport(ok=False, detail={"error": str(exc)})


def _text_from(body: dict[str, Any]) -> str:
    """Reply text from an Anthropic /v1/messages body: the text blocks of
    content[], joined in order; non-text blocks (tool_use, thinking, ...) and
    malformed entries never contribute."""
    blocks = body.get("content") or []
    return "\n".join(
        block["text"]
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _usage_from(data: Any) -> Usage | None:
    if not isinstance(data, dict):
        return None
    return Usage(
        prompt_tokens=data.get("input_tokens"),
        completion_tokens=data.get("output_tokens"),
        cache_read_input_tokens=data.get("cache_read_input_tokens"),
        cache_creation_input_tokens=data.get("cache_creation_input_tokens"),
    )
