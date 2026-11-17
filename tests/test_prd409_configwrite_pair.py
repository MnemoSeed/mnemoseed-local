"""Cross-field invariant for dream role routes: a single driver/model write must
never persist a route that load() would refuse (no default route)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.config import ROLE_ROUTE_REQUIRED_MESSAGE, load_config
from mnemoseed_local.configwrite.service import ConfigWriteError, ConfigWriteService

DRIVER_KEY = "dream.llm.dream.driver"
MODEL_KEY = "dream.llm.dream.model"


def _config_without_role(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\nbaseurl = "http://localhost:7788"\n[dream]\nauto_trigger = false\n',
        encoding="utf-8",
    )
    return path


def _config_with_role(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n'
        'baseurl = "http://localhost:7788"\n'
        "[dream]\n"
        "auto_trigger = false\n"
        "\n"
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    return path


def test_driver_write_without_model_is_rejected_and_file_unchanged(tmp_path: Path) -> None:
    path = _config_without_role(tmp_path)
    before = path.read_bytes()
    service = ConfigWriteService(load_config(path), None)

    with pytest.raises(ConfigWriteError) as exc:
        service.set(DRIVER_KEY, "ollama")

    assert ROLE_ROUTE_REQUIRED_MESSAGE in str(exc.value)
    assert path.read_bytes() == before
    assert "dream" not in service._config.llm


def test_model_write_without_driver_is_rejected_and_file_unchanged(tmp_path: Path) -> None:
    path = _config_without_role(tmp_path)
    before = path.read_bytes()
    service = ConfigWriteService(load_config(path), None)

    with pytest.raises(ConfigWriteError) as exc:
        service.set(MODEL_KEY, "llama3.1:8b")

    assert ROLE_ROUTE_REQUIRED_MESSAGE in str(exc.value)
    assert path.read_bytes() == before


def test_single_field_write_to_configured_role_still_succeeds(tmp_path: Path) -> None:
    path = _config_with_role(tmp_path)
    service = ConfigWriteService(load_config(path), None)

    result = service.set(DRIVER_KEY, "openai_compatible")

    assert result["ok"] is True
    assert load_config(path).llm["dream"].driver == "openai_compatible"
    assert load_config(path).llm["dream"].model == "stub"


def test_empty_value_still_rejected_by_field_validator(tmp_path: Path) -> None:
    path = _config_with_role(tmp_path)
    service = ConfigWriteService(load_config(path), None)

    with pytest.raises(ConfigWriteError):
        service.set(DRIVER_KEY, "")
