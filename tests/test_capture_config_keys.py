"""B2.1 T2 (A2.5 batch): the capture auto-recall config keys — registration,
validation, hot-apply and config-file loading (design/01 §4.6, PRD-B2.1 T2
D5).

The three new registry keys:
  capture.auto_recall               bool (default False — the pipeline is opt-in)
  capture.auto_recall_focal_floor   float in (0, 1]  (the focal decay floor)
  capture.auto_recall_budget_chars  positive int    (the injection budget)

Asserted behaviors through the public configwrite surface:
- every key is a registry key, resolved by config get, hot-applied on set;
- invalid values are rejected with a typed error naming the key;
- the config file loader parses and validates the three keys;
- the shipped default config documents the [capture] table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.config import ConfigError, default_config_toml, load_config
from mnemoseed_local.configwrite.service import (
    CONFIG_KEY_REGISTRY,
    ConfigWriteError,
    ConfigWriteService,
)

_CAPTURE_KEYS = (
    "capture.auto_recall",
    "capture.auto_recall_focal_floor",
    "capture.auto_recall_budget_chars",
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


def test_registry_has_three_capture_keys() -> None:
    for key in _CAPTURE_KEYS:
        assert key in CONFIG_KEY_REGISTRY


def test_configwrite_get_resolves_three_capture_keys(tmp_path) -> None:
    service, _ = _service(tmp_path)
    capture = service.get()["config"]["capture"]
    assert capture["auto_recall"] is False
    assert capture["auto_recall_focal_floor"] == 0.4
    assert capture["auto_recall_budget_chars"] == 1200
    assert service.get()["restart_required"] == {}


def test_configwrite_set_hot_applies_three_capture_keys(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("capture.auto_recall", True, actor="console")
    service.set("capture.auto_recall_focal_floor", 0.6, actor="console")
    service.set("capture.auto_recall_budget_chars", 800, actor="console")
    assert service._config.capture.auto_recall is True
    assert service._config.capture.auto_recall_focal_floor == 0.6
    assert service._config.capture.auto_recall_budget_chars == 800
    # persisted and round-tripped through the loader
    assert load_config(path).capture.auto_recall is True
    assert load_config(path).capture.auto_recall_budget_chars == 800


# ---------------------------------------------------------------- validation (AC1)


def test_configwrite_rejects_non_bool_auto_recall(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in ("yes", 1, "true"):
        with pytest.raises(ConfigWriteError, match=r"config\[capture\.auto_recall\]"):
            service.set("capture.auto_recall", bad, actor="console")


def test_configwrite_rejects_focal_floor_out_of_range(tmp_path) -> None:
    """The focal floor is positive and at most 1 — 0.0 (the dream floor's
    boundary) is NOT accepted here: a floor of zero would make every decayed
    chunk focal."""
    service, _ = _service(tmp_path)
    for bad in (0.0, 1.1, -0.1, "high"):
        with pytest.raises(ConfigWriteError, match=r"config\[capture\.auto_recall_focal_floor\]"):
            service.set("capture.auto_recall_focal_floor", bad, actor="console")


def test_configwrite_accepts_focal_floor_boundaries(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for floor in (0.1, 0.4, 1.0):
        result = service.set("capture.auto_recall_focal_floor", floor, actor="console")
        assert result["ok"] is True
    assert service._config.capture.auto_recall_focal_floor == 1.0


def test_configwrite_rejects_budget_chars_non_positive(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in (0, -1, 1.5, "many"):
        with pytest.raises(ConfigWriteError, match=r"config\[capture\.auto_recall_budget_chars\]"):
            service.set("capture.auto_recall_budget_chars", bad, actor="console")


def test_configwrite_accepts_positive_budget_chars(tmp_path) -> None:
    service, _ = _service(tmp_path)
    result = service.set("capture.auto_recall_budget_chars", 600, actor="console")
    assert result["ok"] is True
    assert service._config.capture.auto_recall_budget_chars == 600


# ---------------------------------------------------------------- config file loading


def _write_capture(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n[dream]\n'
        "[capture]\n" + body,
        encoding="utf-8",
    )
    return path


def test_load_defaults_for_three_capture_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.capture.auto_recall is False
    assert cfg.capture.auto_recall_focal_floor == 0.4
    assert cfg.capture.auto_recall_budget_chars == 1200


def test_load_parses_three_capture_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = _write_capture(
        tmp_path,
        "auto_recall = true\nauto_recall_focal_floor = 0.6\nauto_recall_budget_chars = 800\n",
    )
    cfg = load_config(path)
    assert cfg.capture.auto_recall is True
    assert cfg.capture.auto_recall_focal_floor == 0.6
    assert cfg.capture.auto_recall_budget_chars == 800


def test_load_rejects_bad_capture_values_naming_the_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    for body, key in (
        ('auto_recall = "yes"\n', "capture.auto_recall"),
        ("auto_recall_focal_floor = 1.5\n", "capture.auto_recall_focal_floor"),
        ("auto_recall_focal_floor = 0.0\n", "capture.auto_recall_focal_floor"),
        ("auto_recall_budget_chars = 0\n", "capture.auto_recall_budget_chars"),
        ('auto_recall_budget_chars = "many"\n', "capture.auto_recall_budget_chars"),
    ):
        path = _write_capture(tmp_path, body)
        with pytest.raises(ConfigError, match=rf"config\[{key}\]"):
            load_config(path)


def test_default_config_toml_documents_the_capture_table() -> None:
    toml = default_config_toml()
    assert "[capture]" in toml
    assert "auto_recall" in toml
    assert "auto_recall_focal_floor" in toml
    assert "auto_recall_budget_chars" in toml
