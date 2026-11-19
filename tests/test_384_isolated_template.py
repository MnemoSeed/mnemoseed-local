"""The isolated-graph path from the shipped template must follow the live home."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from mnemoseed_local.config import default_config_toml, load_config
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver


def _isolated_path(template: str) -> str:
    return tomllib.loads(template)["storage"]["graph"]["instances"]["isolated"]["path"]


def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    live_home = tmp_path / "isolated-home"
    real_home = tmp_path / "real-home"
    live_home.mkdir()
    real_home.mkdir()
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(live_home))
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("USERPROFILE", str(real_home))
    config_path = live_home / "config.toml"
    config_path.write_text(default_config_toml(), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    return live_home, real_home


def test_template_isolated_path_is_home_relative() -> None:
    path = _isolated_path(default_config_toml())
    assert not path.startswith("~")
    assert not Path(path).is_absolute()


def test_copied_template_resolves_isolated_db_under_the_temp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_home, real_home = _isolated_home(tmp_path, monkeypatch)
    cfg = load_config(live_home / "config.toml")
    isolated = cfg.layer_instances("graph")["isolated"]
    resolved = Path(os.path.expanduser(isolated.params["path"]))
    assert resolved == live_home / "isolated.db"
    assert not resolved.is_relative_to(real_home)


def test_template_boot_never_creates_the_real_isolated_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_home, real_home = _isolated_home(tmp_path, monkeypatch)
    cfg = load_config(live_home / "config.toml")
    params = cfg.layer_instances("graph")["isolated"].params
    SqliteGraphDriver(**params)
    assert (live_home / "isolated.db").is_file()
    assert not (real_home / ".mnemoseed-local" / "isolated.db").exists()


def test_explicit_tilde_and_absolute_paths_stay_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[storage.graph.instances.isolated]\n"
        'driver = "sqlite_graph"\n'
        'path = "~/.mnemoseed-local/isolated.db"\n',
        encoding="utf-8",
    )
    assert load_config(config_path).layer_instances("graph")["isolated"].params == {
        "path": "~/.mnemoseed-local/isolated.db"
    }
