"""Provider preset catalog: static cloud-provider rows for the setup wizard.

Pure data, zero I/O: presets prefill config fields and tell the wizard where a
provider's model list comes from. The local Ollama entry is not a preset — it
comes from runtime detection.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    """One cloud provider's prefill row."""

    id: str
    display: str
    protocol: str
    base_url: str | None
    auth_methods: tuple[str, ...]
    model_list_source: str
    curated_models: tuple[str, ...]
    docs_url: str
    token_param: str


PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="openai",
        display="OpenAI",
        protocol="openai_compatible",
        base_url="https://api.openai.com/v1",
        auth_methods=("api_key", "oauth"),
        model_list_source="curated+api",
        curated_models=(
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "o3",
            "o4-mini",
        ),
        docs_url="https://platform.openai.com/docs",
        token_param="max_completion_tokens",
    ),
    ProviderPreset(
        id="anthropic",
        display="Anthropic",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        auth_methods=("api_key",),
        model_list_source="api",
        curated_models=(),
        docs_url="https://docs.anthropic.com",
        token_param="max_tokens",
    ),
    ProviderPreset(
        id="google",
        display="Google",
        protocol="openai_compatible",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        auth_methods=("api_key",),
        model_list_source="curated+api",
        curated_models=(
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
        ),
        docs_url="https://ai.google.dev/gemini-api/docs",
        token_param="max_tokens",
    ),
    ProviderPreset(
        id="deepseek",
        display="DeepSeek",
        protocol="openai_compatible",
        base_url="https://api.deepseek.com/v1",
        auth_methods=("api_key",),
        model_list_source="api",
        curated_models=(),
        docs_url="https://api-docs.deepseek.com",
        token_param="max_tokens",
    ),
    ProviderPreset(
        id="moonshot",
        display="Moonshot",
        protocol="openai_compatible",
        base_url="https://api.moonshot.ai/v1",
        auth_methods=("api_key",),
        model_list_source="api",
        curated_models=(),
        docs_url="https://platform.moonshot.ai/docs",
        token_param="max_tokens",
    ),
    ProviderPreset(
        id="xai",
        display="xAI",
        protocol="openai_compatible",
        base_url="https://api.x.ai/v1",
        auth_methods=("api_key",),
        model_list_source="api",
        curated_models=(),
        docs_url="https://docs.x.ai",
        token_param="max_tokens",
    ),
    ProviderPreset(
        id="openrouter",
        display="OpenRouter",
        protocol="openai_compatible",
        base_url="https://openrouter.ai/api/v1",
        auth_methods=("api_key",),
        model_list_source="curated+api",
        curated_models=(
            "openai/gpt-4o-mini",
            "anthropic/claude-sonnet-4",
            "google/gemini-2.0-flash-001",
            "deepseek/deepseek-chat",
            "meta-llama/llama-3.3-70b-instruct",
        ),
        docs_url="https://openrouter.ai/docs",
        token_param="max_tokens",
    ),
    ProviderPreset(
        id="nvidia",
        display="NVIDIA",
        protocol="openai_compatible",
        base_url="https://integrate.api.nvidia.com/v1",
        auth_methods=("api_key",),
        model_list_source="curated+api",
        curated_models=(
            "meta/llama-3.3-70b-instruct",
            "deepseek-ai/deepseek-r1",
            "qwen/qwen2.5-72b-instruct",
        ),
        docs_url="https://docs.api.nvidia.com",
        token_param="max_tokens",
    ),
    ProviderPreset(
        id="custom",
        display="Custom",
        protocol="openai_compatible",
        base_url=None,
        auth_methods=("api_key",),
        model_list_source="curated",
        curated_models=(),
        docs_url="https://platform.openai.com/docs/api-reference",
        token_param="max_tokens",
    ),
)
