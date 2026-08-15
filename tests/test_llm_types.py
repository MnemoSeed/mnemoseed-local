"""DreamLLM port types (PRD-02 T6; FR-2.14 / design/02 §6-§7).

The port contract in one file: ``ChatResult`` carries text + provider-reported
usage + model + driver; ``check()`` returns a ``HealthReport`` that never
raises; and every failure a driver can surface (transport, auth, missing route,
unknown driver, oauth-not-built) is a typed ``LLMError`` subclass so callers
degrade for real (FR-2.6) instead of catching strings.
"""

from __future__ import annotations

from mnemoseed_local.llm import (
    ChatResult,
    HealthReport,
    LLMDriverInfo,
    LLMError,
    LLMRouteError,
    LLMUnavailable,
    OAuthNotImplemented,
    UnknownLLMDriverError,
    Usage,
)


def test_chat_result_carries_text_usage_model_driver() -> None:
    usage = Usage(prompt_tokens=10, completion_tokens=3)
    result = ChatResult(text="[]", usage=usage, model="claude-sonnet-5", driver="anthropic")
    assert result.text == "[]"
    assert result.usage is usage
    assert result.model == "claude-sonnet-5"
    assert result.driver == "anthropic"


def test_chat_result_optional_fields_default() -> None:
    result = ChatResult(text="hi")
    assert result.usage is None
    assert result.model == ""
    assert result.driver == ""


def test_usage_defaults_all_none() -> None:
    usage = Usage()
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.cache_read_input_tokens is None
    assert usage.cache_creation_input_tokens is None


def test_usage_carries_provider_cache_legs() -> None:
    usage = Usage(
        prompt_tokens=5,
        completion_tokens=2,
        cache_read_input_tokens=42,
        cache_creation_input_tokens=7,
    )
    assert usage.cache_read_input_tokens == 42
    assert usage.cache_creation_input_tokens == 7


def test_health_report_carries_ok_and_detail() -> None:
    report = HealthReport(ok=False, detail={"status": "not_configured"})
    assert report.ok is False
    assert report.detail["status"] == "not_configured"


def test_error_hierarchy_supports_typed_degradation() -> None:
    # FR-2.6: every unavailable branch is reachable through one typed base.
    assert issubclass(LLMUnavailable, LLMError)
    assert issubclass(OAuthNotImplemented, LLMUnavailable)  # stub chats degrade like 401s
    assert issubclass(LLMRouteError, LLMError)
    assert issubclass(UnknownLLMDriverError, LLMError)


def test_unknown_llm_driver_error_names_available() -> None:
    exc = UnknownLLMDriverError("llm", "nope", ["ollama"])
    assert str(exc) == "unknown llm driver 'nope' (available: ollama)"
    assert (
        str(UnknownLLMDriverError("llm", "nope", []))
        == "unknown llm driver 'nope' (no llm drivers registered)"
    )


def test_llm_driver_info_minimal_surface() -> None:
    info = LLMDriverInfo(name="anthropic")
    assert info.name == "anthropic"
    assert info.description == ""
