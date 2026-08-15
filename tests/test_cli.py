"""CLI surface (A2 MVP): init writes a working config, verb dispatch returns
proper exit codes, config ops are loopback-only, and uninstall --purge stays
scoped to the app's own config home."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.cli import build_parser, main
from mnemoseed_local.config import default_config_toml, load_config


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".mnemoseed-local"
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_PATH", home / "config.toml")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    return home


def test_init_writes_a_working_config(cli_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == 0
    config_path = cli_home / "config.toml"
    assert config_path.exists()
    cfg = load_config(config_path)
    assert cfg.preset == "embedded"
    assert cfg.baseurl == "http://localhost:7788"
    # the ollama dream driver is the default
    assert cfg.llm["dream"].driver == "ollama"
    assert cfg.llm["dream"].model == "llama3.1:8b"
    # the A2 schedule trigger keys are present
    assert cfg.dream.floor_pool_points == 10.0
    assert cfg.dream.idle_min_sec == 900.0
    assert cfg.dream.hard_deadline_sec == 86400.0


def test_init_refuses_to_overwrite_without_force(cli_home: Path, capsys) -> None:
    assert main(["init"]) == 0
    assert main(["init"]) == 1
    assert "already exists" in capsys.readouterr().out


def test_init_force_overwrites(cli_home: Path) -> None:
    assert main(["init"]) == 0
    (cli_home / "config.toml").write_text('preset = "embedded"\n', encoding="utf-8")
    assert main(["init", "--force"]) == 0
    assert load_config(cli_home / "config.toml").baseurl == "http://localhost:7788"


def test_default_config_toml_parses_clean() -> None:
    import tomllib

    tomllib.loads(default_config_toml())


def test_recall_against_a_down_daemon_is_a_clean_error(capsys) -> None:
    code = main(["recall", "pnpm", "--baseurl", "http://127.0.0.1:1"])
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_unknown_command_prints_help(capsys) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["bogus"])


def test_config_get_requires_loopback(capsys) -> None:
    code = main(["config", "get", "--baseurl", "http://10.0.0.5:7788"])
    assert code == 1
    assert "loopback-only" in capsys.readouterr().err


def test_uninstall_without_purge_keeps_data(cli_home: Path, capsys) -> None:
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text('preset = "embedded"\n', encoding="utf-8")
    assert main(["uninstall"]) == 0
    assert cli_home.exists()
    assert "purge" in capsys.readouterr().out


def test_uninstall_purge_deletes_only_the_config_home(cli_home: Path, monkeypatch, capsys) -> None:
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text('preset = "embedded"\n', encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(["uninstall", "--purge"]) == 0
    assert not cli_home.exists()
    assert "data dir deleted" in capsys.readouterr().out
