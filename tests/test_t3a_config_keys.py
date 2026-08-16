"""T3a (A2.5 batch 3): tier / ensemble / threshold config keys — registration,
validation, tier linkage and config-file loading (design/01 §4.7 / §4.8).

The five new registry keys:
  dream.hardware_tier                standard | lite | advanced
  dream.ensemble                     off | verify | vote  (lite locks the ensemble off)
  dream.core_confidence_floor        float in [0, 1]
  dream.delta_budget_ceiling_tokens  int >= 5000
  dream.pool_forced_cap              float >= dream.core_confidence_floor

Asserted behaviors through the public configwrite surface (AC1 / AC2):
- every key is a registry key, resolved by config get, hot-applied on set;
- invalid values are rejected with a typed error naming the key;
- the lite hardware tier locks the ensemble off (rejected in BOTH write
  directions, so the invariant "lite implies ensemble == off" never breaks);
- the config file loader parses and validates the five keys.

Consumer seams (Merger / DeltaPacker / ScorePool) are tested in their own
files: test_dream_merge.py, test_dream_delta.py, test_pool.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.config import ConfigError, load_config
from mnemoseed_local.configwrite.service import (
    CONFIG_KEY_REGISTRY,
    ConfigWriteError,
    ConfigWriteService,
)

_T3A_KEYS = (
    "dream.hardware_tier",
    "dream.ensemble",
    "dream.core_confidence_floor",
    "dream.delta_budget_ceiling_tokens",
    "dream.pool_forced_cap",
)


def _config_toml(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n[dream]\n',
        encoding="utf-8",
    )
    return path


def _service(tmp_path: Path) -> tuple[ConfigWriteService, Path]:
    path = _config_toml(tmp_path)
    return ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0), path


# ---------------------------------------------------------------- registry (AC1)


def test_registry_has_five_t3a_keys() -> None:
    for key in _T3A_KEYS:
        assert key in CONFIG_KEY_REGISTRY


def test_configwrite_get_resolves_five_t3a_keys(tmp_path) -> None:
    service, _ = _service(tmp_path)
    dream = service.get()["config"]["dream"]
    assert dream["hardware_tier"] == "standard"
    assert dream["ensemble"] == "off"
    assert dream["core_confidence_floor"] == 0.0
    assert dream["delta_budget_ceiling_tokens"] == 32000
    assert dream["pool_forced_cap"] == 50.0
    assert service.get()["restart_required"] == {}


def test_configwrite_set_hot_applies_five_t3a_keys(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.hardware_tier", "lite", actor="console")
    service.set("dream.ensemble", "off", actor="console")
    service.set("dream.core_confidence_floor", 0.6, actor="console")
    service.set("dream.delta_budget_ceiling_tokens", 8192, actor="console")
    service.set("dream.pool_forced_cap", 0.8, actor="console")
    assert service._config.dream.hardware_tier == "lite"
    assert service._config.dream.ensemble == "off"
    assert service._config.dream.core_confidence_floor == 0.6
    assert service._config.dream.delta_budget_ceiling_tokens == 8192
    assert service._config.dream.pool_forced_cap == 0.8
    # persisted and round-tripped through the loader
    assert load_config(path).dream.delta_budget_ceiling_tokens == 8192
    assert load_config(path).dream.hardware_tier == "lite"


# ---------------------------------------------------------------- validation (AC1)


def test_configwrite_rejects_invalid_hardware_tier(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.hardware_tier\].*standard"):
        service.set("dream.hardware_tier", "ultra", actor="console")


def test_configwrite_accepts_each_hardware_tier(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for tier in ("standard", "lite", "advanced"):
        result = service.set("dream.hardware_tier", tier, actor="console")
        assert result["ok"] is True
        assert service._config.dream.hardware_tier == tier


def test_configwrite_rejects_invalid_ensemble_mode(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.ensemble\].*off"):
        service.set("dream.ensemble", "auto", actor="console")


def test_configwrite_rejects_core_confidence_floor_out_of_range(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in (-0.1, 1.1, "high"):
        with pytest.raises(ConfigWriteError, match=r"config\[dream\.core_confidence_floor\]"):
            service.set("dream.core_confidence_floor", bad, actor="console")


def test_configwrite_accepts_core_confidence_floor_boundaries(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for floor in (0.0, 0.7, 1.0):
        result = service.set("dream.core_confidence_floor", floor, actor="console")
        assert result["ok"] is True
    assert service._config.dream.core_confidence_floor == 1.0


def test_configwrite_rejects_delta_ceiling_below_5000(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in (4999, 0, -1, 1.5, "many"):
        with pytest.raises(ConfigWriteError, match=r"config\[dream\.delta_budget_ceiling_tokens\]"):
            service.set("dream.delta_budget_ceiling_tokens", bad, actor="console")


def test_configwrite_accepts_delta_ceiling_at_or_above_5000(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for ceiling in (5000, 8192):
        result = service.set("dream.delta_budget_ceiling_tokens", ceiling, actor="console")
        assert result["ok"] is True
    assert service._config.dream.delta_budget_ceiling_tokens == 8192


def test_configwrite_rejects_pool_forced_cap_below_floor(tmp_path) -> None:
    service, _ = _service(tmp_path)
    service.set("dream.core_confidence_floor", 0.7, actor="console")
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.pool_forced_cap\].*floor"):
        service.set("dream.pool_forced_cap", 0.5, actor="console")


def test_configwrite_accepts_pool_forced_cap_at_or_above_floor(tmp_path) -> None:
    service, _ = _service(tmp_path)
    service.set("dream.core_confidence_floor", 0.7, actor="console")
    result = service.set("dream.pool_forced_cap", 0.7, actor="console")
    assert result["ok"] is True
    assert service._config.dream.pool_forced_cap == 0.7


def test_configwrite_floor_raised_above_cap_is_rejected(tmp_path) -> None:
    service, _ = _service(tmp_path)
    service.set("dream.pool_forced_cap", 0.3, actor="console")
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.core_confidence_floor\]"):
        service.set("dream.core_confidence_floor", 0.7, actor="console")


# ---------------------------------------------------------------- tier-ensemble linkage (AC2)


def test_configwrite_lite_tier_rejects_non_off_ensemble(tmp_path) -> None:
    service, _ = _service(tmp_path)
    service.set("dream.hardware_tier", "lite", actor="console")
    for mode in ("verify", "vote"):
        with pytest.raises(ConfigWriteError, match=r"config\[dream\.ensemble\].*lite"):
            service.set("dream.ensemble", mode, actor="console")


def test_configwrite_lite_tier_accepts_off_ensemble(tmp_path) -> None:
    service, _ = _service(tmp_path)
    service.set("dream.hardware_tier", "lite", actor="console")
    result = service.set("dream.ensemble", "off", actor="console")
    assert result["ok"] is True
    assert service._config.dream.ensemble == "off"


def test_configwrite_standard_tier_accepts_non_off_ensemble(tmp_path) -> None:
    service, _ = _service(tmp_path)
    result = service.set("dream.ensemble", "verify", actor="console")
    assert result["ok"] is True
    assert service._config.dream.ensemble == "verify"


def test_configwrite_switching_to_lite_while_ensemble_non_off_is_rejected(tmp_path) -> None:
    service, _ = _service(tmp_path)
    service.set("dream.ensemble", "verify", actor="console")
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.hardware_tier\].*ensemble"):
        service.set("dream.hardware_tier", "lite", actor="console")


# ---------------------------------------------------------------- config file loading


def _write_dream(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n[dream]\n' + body,
        encoding="utf-8",
    )
    return path


def test_load_defaults_for_five_t3a_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.dream.hardware_tier == "standard"
    assert cfg.dream.ensemble == "off"
    assert cfg.dream.core_confidence_floor == 0.0
    assert cfg.dream.delta_budget_ceiling_tokens == 32000
    assert cfg.dream.pool_forced_cap == 50.0


def test_load_parses_five_t3a_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = _write_dream(
        tmp_path,
        'hardware_tier = "lite"\n'
        'ensemble = "off"\n'
        "core_confidence_floor = 0.6\n"
        "delta_budget_ceiling_tokens = 8192\n"
        "pool_forced_cap = 0.8\n",
    )
    cfg = load_config(path)
    assert cfg.dream.hardware_tier == "lite"
    assert cfg.dream.ensemble == "off"
    assert cfg.dream.core_confidence_floor == 0.6
    assert cfg.dream.delta_budget_ceiling_tokens == 8192
    assert cfg.dream.pool_forced_cap == 0.8


def test_load_rejects_bad_t3a_values_naming_the_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    for body, key in (
        ('hardware_tier = "ultra"\n', "dream.hardware_tier"),
        ('ensemble = "auto"\n', "dream.ensemble"),
        ("core_confidence_floor = 1.5\n", "dream.core_confidence_floor"),
        ("core_confidence_floor = -0.1\n", "dream.core_confidence_floor"),
        ("delta_budget_ceiling_tokens = 4000\n", "dream.delta_budget_ceiling_tokens"),
        ('delta_budget_ceiling_tokens = "eight"\n', "dream.delta_budget_ceiling_tokens"),
        ("pool_forced_cap = 0.2\ncore_confidence_floor = 0.7\n", "dream.pool_forced_cap"),
    ):
        path = _write_dream(tmp_path, body)
        with pytest.raises(ConfigError, match=rf"config\[{key}\]"):
            load_config(path)


def test_delta_ceiling_constant_is_the_default_value_source() -> None:
    from mnemoseed_local.config import DEFAULT_DREAM_DELTA_BUDGET_CEILING_TOKENS
    from mnemoseed_local.dream.delta import DELTA_BUDGET_CEILING_TOKENS

    assert DEFAULT_DREAM_DELTA_BUDGET_CEILING_TOKENS == DELTA_BUDGET_CEILING_TOKENS == 32000


# ---------------------------------------------------------------- isolated mandatory (AC2)


def test_load_rejects_floor_above_zero_without_isolated_instance(monkeypatch, tmp_path) -> None:
    """AC2: dream.core_confidence_floor > 0 requires the isolated graph
    instance — a config that sets a floor without it is rejected at load."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = tmp_path / "config.toml"
    path.write_text('preset = "embedded"\n[dream]\ncore_confidence_floor = 0.6\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"config\[dream\.core_confidence_floor\].*isolated"):
        load_config(path)


def test_load_accepts_floor_above_zero_with_isolated_instance(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = _write_dream(tmp_path, "core_confidence_floor = 0.6\n")
    assert load_config(path).dream.core_confidence_floor == 0.6


def test_configwrite_rejects_floor_above_zero_without_isolated(tmp_path) -> None:
    """AC2: the configwrite side enforces the same rule — raising the floor on
    a config without the isolated instance is rejected, never a silent
    downgrade target."""
    path = tmp_path / "config.toml"
    path.write_text('preset = "embedded"\n[dream]\n', encoding="utf-8")
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.core_confidence_floor\].*isolated"):
        service.set("dream.core_confidence_floor", 0.6, actor="console")


def test_configwrite_accepts_floor_above_zero_with_isolated(tmp_path) -> None:
    service, _ = _service(tmp_path)
    result = service.set("dream.core_confidence_floor", 0.6, actor="console")
    assert result["ok"] is True
    assert service._config.dream.core_confidence_floor == 0.6
