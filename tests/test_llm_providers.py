"""Provider preset catalog: pinned values, shape invariants, immutability."""

import dataclasses

import pytest

from mnemoseed_local.llm.providers import PROVIDER_PRESETS, ProviderPreset

EXPECTED_IDS = {
    "openai",
    "anthropic",
    "google",
    "deepseek",
    "moonshot",
    "xai",
    "openrouter",
    "nvidia",
    "custom",
}

EXPECTED_PROTOCOL_BASE_URL = {
    "openai": ("openai_compatible", "https://api.openai.com/v1"),
    "anthropic": ("anthropic", "https://api.anthropic.com"),
    "google": (
        "openai_compatible",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    ),
    "deepseek": ("openai_compatible", "https://api.deepseek.com/v1"),
    "moonshot": ("openai_compatible", "https://api.moonshot.ai/v1"),
    "xai": ("openai_compatible", "https://api.x.ai/v1"),
    "openrouter": ("openai_compatible", "https://openrouter.ai/api/v1"),
    "nvidia": ("openai_compatible", "https://integrate.api.nvidia.com/v1"),
    "custom": ("openai_compatible", None),
}


def by_id(preset_id: str) -> ProviderPreset:
    matches = [p for p in PROVIDER_PRESETS if p.id == preset_id]
    assert len(matches) == 1, f"expected exactly one preset {preset_id!r}"
    return matches[0]


def test_exactly_nine_unique_expected_ids() -> None:
    ids = [p.id for p in PROVIDER_PRESETS]
    assert len(PROVIDER_PRESETS) == len(EXPECTED_IDS)
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_IDS


def test_preset_order_is_prd_table_order() -> None:
    assert tuple(p.id for p in PROVIDER_PRESETS) == (
        "openai",
        "anthropic",
        "google",
        "deepseek",
        "moonshot",
        "xai",
        "openrouter",
        "nvidia",
        "custom",
    )


@pytest.mark.parametrize("preset_id", sorted(EXPECTED_IDS))
def test_protocol_and_base_url_pinned(preset_id: str) -> None:
    preset = by_id(preset_id)
    protocol, base_url = EXPECTED_PROTOCOL_BASE_URL[preset_id]
    assert preset.protocol == protocol
    assert preset.base_url == base_url


def test_auth_methods_rules() -> None:
    for preset in PROVIDER_PRESETS:
        assert "api_key" in preset.auth_methods
        has_oauth = "oauth" in preset.auth_methods
        assert has_oauth == (preset.id == "openai")


def test_token_param_rules() -> None:
    for preset in PROVIDER_PRESETS:
        expected = "max_completion_tokens" if preset.id == "openai" else "max_tokens"
        assert preset.token_param == expected


def test_curated_models_matches_model_list_source() -> None:
    for preset in PROVIDER_PRESETS:
        if preset.id == "custom":
            assert preset.curated_models == ()
            continue
        if "curated" in preset.model_list_source:
            assert preset.curated_models, preset.id
        else:
            assert preset.curated_models == (), preset.id
    assert by_id("custom").curated_models == ()
    assert by_id("anthropic").model_list_source == "api"
    assert by_id("openai").model_list_source == "curated+api"
    assert by_id("custom").model_list_source == "curated"


def test_curated_model_ids_wellformed() -> None:
    for preset in PROVIDER_PRESETS:
        for model in preset.curated_models:
            assert isinstance(model, str)
            assert model
            assert model == model.strip()
            assert not any(c.isspace() for c in model)


def test_presets_frozen() -> None:
    preset = by_id("openai")
    assert dataclasses.is_dataclass(preset)
    with pytest.raises(dataclasses.FrozenInstanceError):
        preset.display = "mutated"  # type: ignore[misc]


def test_display_and_docs_url_nonempty() -> None:
    for preset in PROVIDER_PRESETS:
        assert preset.display
        assert preset.docs_url.startswith("https://")
        assert preset.model_list_source in {"curated", "api", "curated+api"}
        assert set(preset.auth_methods) <= {"api_key", "oauth"}
