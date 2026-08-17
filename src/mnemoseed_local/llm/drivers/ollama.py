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

#: Ollama /api/chat option names the driver forwards from role params into the
#: request body's ``options`` field (design/01 §4.8). Only whitelisted keys are
#: forwarded, so non-option params (base_url, timeout, ...) never leak into
#: options. num_ctx / num_predict are the required members (the doctor
#: ctx-window check and the generation cap); the rest are the documented
#: sampling/runtime options.
_OLLAMA_OPTION_KEYS: frozenset[str] = frozenset(
    {
        "num_ctx",
        "num_predict",
        "num_keep",
        "num_batch",
        "num_gpu",
        "num_thread",
        "num_gqa",
        "seed",
        "temperature",
        "top_k",
        "top_p",
        "min_p",
        "tfs_z",
        "typical_p",
        "repeat_last_n",
        "repeat_penalty",
        "presence_penalty",
        "frequency_penalty",
        "mirostat",
        "mirostat_tau",
        "mirostat_eta",
        "penalize_newline",
        "stop",
        "numa",
        "main_gpu",
        "low_vram",
        "f16_kv",
        "vocab_only",
        "use_mmap",
        "use_mlock",
    }
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
        model: str = "qwen3.5:9b",
        timeout: float = 60.0,
        think: bool | None = None,
        **kwargs: Any,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = ""
        self.model = model
        self.timeout = float(timeout)
        self.think = think
        self.params: dict[str, Any] = kwargs
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def chat(self, *, system: str, user: str) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        # D4 standalone top-level seam (NOT an "options" member): thinking is
        # pure parasitism for the dream reflect's structured extraction — a
        # thinking model can burn the whole num_predict budget on internal
        # thinking and leave message.content EMPTY (72s/zero-JSON-per-attempt
        # observed live on qwen3.5:9b; think=false takes 2.5s and returns a
        # valid JSON array).
        if self.think is not None:
            payload["think"] = bool(self.think)
        # Options seam (design/01 §4.8): role params configure the request
        # options (num_ctx, num_predict, ...). Only configured option params
        # are forwarded, so an unconfigured route keeps the exact legacy
        # request body (no empty "options" key, byte-identical serialization).
        options = self._ollama_options()
        if options:
            payload["options"] = options
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

    def _ollama_options(self) -> dict[str, Any]:
        """The whitelisted ollama options present in the role params."""
        return {key: self.params[key] for key in _OLLAMA_OPTION_KEYS if key in self.params}

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
    """Ollama's native /api/chat reports token counts at the RESPONSE ROOT
    (prompt_eval_count / eval_count), never inside a "usage" key — the driver's
    previous nested-only read silently dropped every provider token (verified
    against live ollama 0.32). A {"usage": {...}} body stays honored for nested
    shapes some middleware emits."""
    prompt = body.get("prompt_eval_count")
    completion = body.get("eval_count")
    if prompt is not None or completion is not None:
        return Usage(
            prompt_tokens=int(prompt) if prompt is not None else None,
            completion_tokens=int(completion) if completion is not None else None,
        )
    data = body.get("usage")
    if not isinstance(data, dict):
        return None
    return Usage(
        prompt_tokens=data.get("prompt_eval_count", data.get("prompt_tokens")),
        completion_tokens=data.get("eval_count", data.get("completion_tokens")),
    )
