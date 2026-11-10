"""Anthropic driver: request/response mappings over httpx MockTransport (PRD-B7 S2).

Same contract as the sibling driver tests: no live network; each test pins the
wire shape it sends (path, headers, body) and the typed degradation it surfaces
(``LLMUnavailable`` on transport/auth failure, FR-2.6; ``check()`` never
raises). Error text must never carry the api key.
"""

from __future__ import annotations

import json
import traceback
from typing import Any

import httpx
import pytest

from mnemoseed_local.llm import HealthReport, LLMUnavailable, Usage
from mnemoseed_local.llm.drivers.anthropic import AnthropicLLM
from mnemoseed_local.llm.drivers.openai_compatible import OpenAICompatibleLLM
from mnemoseed_local.llm.registry import LLM_DRIVERS

API_KEY = "sk-ant-test-1234abcdef"


def _client(base_url: str, handler: Any) -> httpx.Client:
    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler), timeout=5.0)


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def _driver(handler: Any, **kwargs: Any) -> AnthropicLLM:
    params: dict[str, Any] = {
        "base_url": "https://api.anthropic.test",
        "api_key": API_KEY,
        "model": "claude-sonnet-5",
    }
    params.update(kwargs)
    llm = AnthropicLLM(**params)
    llm._client = _client(params["base_url"], handler)
    return llm


def _assert_no_key_material(excinfo: pytest.ExceptionInfo[LLMUnavailable]) -> None:
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in "".join(traceback.format_exception(excinfo.value))


# ---------------------------------------------------------------- chat request shape


def test_anthropic_no_network_at_construction() -> None:
    llm = AnthropicLLM(api_key=API_KEY, model="claude-sonnet-5")
    assert llm.model == "claude-sonnet-5"
    assert llm.max_tokens == 4096


def test_anthropic_requires_base_url_and_model() -> None:
    with pytest.raises(ValueError, match="base_url"):
        AnthropicLLM(base_url="", api_key="k", model="m")
    with pytest.raises(ValueError, match="model"):
        AnthropicLLM(api_key="k", model="")


def test_anthropic_chat_sends_messages_request() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers.get("x-api-key") == API_KEY
        assert request.headers.get("anthropic-version") == "2023-06-01"
        sent["body"] = _body(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "content": [{"type": "text", "text": "[]"}],
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        )

    llm = _driver(handler)
    result = llm.chat(system="sys", user="usr")
    assert result.text == "[]"
    assert result.model == "claude-sonnet-5"
    assert result.driver == "anthropic"
    assert sent["body"]["model"] == "claude-sonnet-5"
    # system is a top-level parameter, never a message (a system-in-messages
    # regression breaks this exact-equality pin on the single-user message list).
    assert sent["body"]["system"] == "sys"
    assert sent["body"]["messages"] == [{"role": "user", "content": "usr"}]
    assert sent["body"]["max_tokens"] == 4096
    assert result.usage is not None
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 4
    assert result.usage.cache_read_input_tokens is None


def test_anthropic_max_tokens_override_honored() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = _body(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    llm = _driver(handler, max_tokens=777)
    llm.chat(system="s", user="u")
    assert sent["body"]["max_tokens"] == 777


def test_anthropic_no_api_key_sends_no_x_api_key_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-api-key" not in request.headers
        assert request.headers.get("anthropic-version") == "2023-06-01"
        return httpx.Response(200, json={"content": [{"type": "text", "text": "x"}]})

    llm = AnthropicLLM(base_url="https://api.anthropic.test", api_key="", model="m")
    llm._client = _client("https://api.anthropic.test", handler)
    llm.chat(system="s", user="u")


# ---------------------------------------------------------------- response decode


def test_anthropic_chat_joins_multiple_text_blocks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "part one"},
                    {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
                    {"type": "text", "text": "part two"},
                ]
            },
        )

    llm = _driver(handler)
    result = llm.chat(system="s", user="u")
    assert result.text == "part one\npart two"


def test_anthropic_usage_maps_input_output_and_cache_legs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "x"}],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 42,
                    "cache_creation_input_tokens": 7,
                },
            },
        )

    llm = _driver(handler)
    result = llm.chat(system="s", user="u")
    assert result.usage == Usage(
        prompt_tokens=5,
        completion_tokens=2,
        cache_read_input_tokens=42,
        cache_creation_input_tokens=7,
    )


@pytest.mark.parametrize("garbage", [{"foo": "bar"}, ["nope"]])
def test_anthropic_garbage_shape_degrades_to_empty_text(garbage: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=garbage)

    llm = _driver(handler)
    result = llm.chat(system="s", user="u")
    assert result.text == ""
    assert result.usage is None


# ---------------------------------------------------------------- typed failures


def test_anthropic_auth_error_maps_like_openai_compatible() -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            401,
            json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
        )

    with pytest.raises(LLMUnavailable, match="HTTP 401") as excinfo:
        _driver(deny).chat(system="s", user="u")
    _assert_no_key_material(excinfo)

    openai_llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="bad", model="m")
    openai_llm._client = _client("https://llm.test/v1", deny)
    with pytest.raises(LLMUnavailable, match="HTTP 401"):
        openai_llm.chat(system="s", user="u")


def test_anthropic_server_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"type": "error"})

    with pytest.raises(LLMUnavailable, match="HTTP 500") as excinfo:
        _driver(handler).chat(system="s", user="u")
    _assert_no_key_material(excinfo)


def test_anthropic_timeout_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(LLMUnavailable, match="timed out") as excinfo:
        _driver(handler).chat(system="s", user="u")
    _assert_no_key_material(excinfo)


def test_anthropic_malformed_json_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"<not-json>")

    with pytest.raises(LLMUnavailable) as excinfo:
        _driver(handler).chat(system="s", user="u")
    _assert_no_key_material(excinfo)


# ---------------------------------------------------------------- check()


def test_anthropic_check_reports_reachable_and_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers.get("anthropic-version") == "2023-06-01"
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-5"}, {"id": "claude-haiku-4"}]})

    llm = _driver(handler)
    report = llm.check()
    assert isinstance(report, HealthReport)
    assert report.ok is True
    assert report.detail["models"] == ["claude-sonnet-5", "claude-haiku-4"]


def test_anthropic_check_never_raises_on_non_object_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=["unexpected"])

    llm = _driver(handler)
    report = llm.check()
    assert report.ok is True
    assert report.detail["models"] == []


def test_anthropic_check_reports_unreachable_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, json={})

    llm = _driver(handler)
    report = llm.check()
    assert report.ok is False
    assert "503" in report.detail["error"]


# ---------------------------------------------------------------- registration


def test_anthropic_registers_into_llm_drivers() -> None:
    assert LLM_DRIVERS.contains("anthropic")
    assert "anthropic" in LLM_DRIVERS.names()
    assert "anthropic" in {info.name for info in LLM_DRIVERS.catalog()}


def test_anthropic_builds_via_registry() -> None:
    llm = LLM_DRIVERS.build(
        "anthropic",
        {"base_url": "https://api.anthropic.test", "api_key": API_KEY, "model": "claude-sonnet-5"},
    )
    assert llm.model == "claude-sonnet-5"
    assert llm.max_tokens == 4096
