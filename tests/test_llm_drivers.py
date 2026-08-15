"""LLM drivers: request/response mappings over httpx MockTransport (PRD-02 T6).

All network access lives behind the driver boundary and is exercised through
``httpx.MockTransport`` (or a loopback stub server for the oauth concurrency
case) — no live third-party network anywhere. Each driver test pins the wire
shape it sends (path, headers, body) and the typed degradation it surfaces
``LLMUnavailable`` on transport/auth failure (FR-2.6).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mnemoseed_local.llm import LLMUnavailable
from mnemoseed_local.llm.drivers.ollama import OllamaLLM
from mnemoseed_local.llm.drivers.openai_compatible import OpenAICompatibleLLM
from mnemoseed_local.llm.registry import LLM_DRIVERS


def _client(base_url: str, handler: Any) -> httpx.Client:
    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler), timeout=5.0)


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


# ---------------------------------------------------------------- openai_compatible


def test_openai_no_network_at_construction() -> None:
    llm = OpenAICompatibleLLM(base_url="http://127.0.0.1:1/v1", api_key="k", model="m")
    assert llm.model == "m"


def test_openai_requires_base_url_and_model() -> None:
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatibleLLM(base_url="", api_key="k", model="m")
    with pytest.raises(ValueError, match="model"):
        OpenAICompatibleLLM(base_url="http://x/v1", api_key="k", model="")


def test_openai_chat_sends_chat_completions_request() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # base_url "…/v1" + "/chat/completions" joins with the prefix on the wire
        assert request.url.path == "/v1/chat/completions"
        assert request.headers.get("authorization") == "Bearer sk-test"
        body = _body(request)
        sent["body"] = body
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "[]"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="sk-test", model="gpt-5.6-terra")
    llm._client = _client("https://llm.test/v1", handler)
    result = llm.chat(system="sys", user="usr")
    assert result.text == "[]"
    assert result.model == "gpt-5.6-terra"
    assert result.driver == "openai_compatible"
    assert sent["body"]["model"] == "gpt-5.6-terra"
    assert sent["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert sent["body"]["max_tokens"] == 2048
    assert result.usage is not None
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 4
    assert result.usage.cache_read_input_tokens is None


def test_openai_no_api_key_sends_no_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    llm.chat(system="s", user="u")


def test_openai_auth_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="bad", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")


def test_openai_server_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="k", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")


def test_openai_transport_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("refused")

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="k", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    with pytest.raises(LLMUnavailable, match="refused"):
        llm.chat(system="s", user="u")


def test_openai_check_reports_reachable_and_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="k", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    report = llm.check()
    assert report.ok is True
    assert report.detail["models"] == ["m1", "m2"]


def test_openai_check_reports_unreachable_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, json={})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="k", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    report = llm.check()
    assert report.ok is False
    assert "503" in report.detail["error"]


# ---------------------------------------------------------------- ollama (offline track)


def test_ollama_chat_sends_native_api_chat() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        assert "authorization" not in request.headers  # offline: no key, no auth header
        sent["body"] = _body(request)
        return httpx.Response(
            200,
            json={
                "model": "llama3.1:8b",
                "message": {"role": "assistant", "content": "[]"},
                "usage": {"prompt_eval_count": 9, "eval_count": 3},
            },
        )

    llm = OllamaLLM(base_url="http://127.0.0.1:11434")
    llm._client = _client("http://127.0.0.1:11434", handler)
    result = llm.chat(system="sys", user="usr")
    assert result.text == "[]"
    assert result.driver == "ollama"
    assert sent["body"]["model"] == "llama3.1:8b"
    assert sent["body"]["stream"] is False
    assert sent["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert result.usage is not None
    assert result.usage.prompt_tokens == 9
    assert result.usage.completion_tokens == 3


def test_ollama_check_reports_tags() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5:7b"}]})

    llm = OllamaLLM(base_url="http://127.0.0.1:11434")
    llm._client = _client("http://127.0.0.1:11434", handler)
    report = llm.check()
    assert report.ok is True
    assert report.detail["models"] == ["llama3.1:8b", "qwen2.5:7b"]


def test_ollama_server_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "model not found"})

    llm = OllamaLLM(base_url="http://127.0.0.1:11434")
    llm._client = _client("http://127.0.0.1:11434", handler)
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")


# ---------------------------------------------------------------- registration side effect


def test_importing_drivers_registers_the_local_set() -> None:
    for name in ("ollama", "openai_compatible", "stub"):
        assert LLM_DRIVERS.contains(name)
