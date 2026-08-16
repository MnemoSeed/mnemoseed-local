"""CLI surface (A2 MVP): init writes a working config, verb dispatch returns
proper exit codes, config ops are loopback-only, and uninstall --purge stays
scoped to the app's own config home."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.cli import build_parser, main
from mnemoseed_local.config import default_config_toml, load_config
from mnemoseed_local.llm.types import HealthReport


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".mnemoseed-local"
    # Both namespaces are patched here, not in per-test helpers: cli.py binds
    # CONFIG_DIR/CONFIG_PATH at import time (its own module-level names) while
    # load_config()/cmd_doctor read the config-module names, so a doctor run
    # needs both patched to the same hermetic home.
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", home / "config.toml")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    return home


def test_init_writes_a_working_config(cli_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == 0
    config_path = cli_home / "config.toml"
    assert config_path.exists()
    cfg = load_config(config_path)
    assert cfg.preset == "embedded"
    assert cfg.baseurl == "http://localhost:7788"
    # the ollama dream driver is the default (model aligned on qwen3.5:9b, AC3)
    assert cfg.llm["dream"].driver == "ollama"
    assert cfg.llm["dream"].model == "qwen3.5:9b"
    # the A2 schedule trigger keys are present
    assert cfg.dream.floor_pool_points == 10.0
    assert cfg.dream.idle_min_sec == 900.0
    assert cfg.dream.hard_deadline_sec == 86400.0
    # AC2: the init template writes the mandatory isolated graph instance
    assert "isolated" in cfg.layer_instances("graph")


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


# ---------------------------------------------------------------- doctor


class _FakeDoctorStores:
    def close(self) -> None:
        pass


class _FakeDoctorLLM:
    def check(self) -> HealthReport:
        return HealthReport(ok=True, detail={})


class _FakeDoctorRouter:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def resolve(self, role: str) -> _FakeDoctorLLM:
        del role
        return _FakeDoctorLLM()


def _mock_doctor_backend(cli_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run cmd_doctor offline: no storage assembly and no ollama network probe.
    The hermetic config home is already patched by the cli_home fixture (both
    the cli and the config module namespaces), so doctor loads the file the
    test wrote there."""
    monkeypatch.setattr("mnemoseed_local.storage.factory.build_stores", lambda config: _FakeDoctorStores())
    monkeypatch.setattr("mnemoseed_local.llm.RoleRouter", _FakeDoctorRouter)


#: Every doctor fixture carries the mandatory isolated graph instance (AC2): a
#: missing instance is itself a FAILING doctor check, so the healthy-fixture
#: tests include it and only the dedicated missing-isolated test omits it.
_ISOLATED_BLOCK = '[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n'


def test_doctor_hints_when_num_ctx_unset(cli_home: Path, monkeypatch, capsys) -> None:
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text('preset = "embedded"\n' + _ISOLATED_BLOCK, encoding="utf-8")
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "all checks passed" in out
    assert "num_ctx is not configured" in out


def test_doctor_reports_small_num_ctx_with_fix_hint(cli_home: Path, monkeypatch, capsys) -> None:
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text(
        'preset = "embedded"\n' + _ISOLATED_BLOCK + "[dream.llm.dream]\nnum_ctx = 1024\n",
        encoding="utf-8",
    )
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "ctx window" in out
    assert "num_ctx=1024" in out
    assert "lower the delta ceiling or raise num_ctx" in out


def test_doctor_ctx_window_passes_when_num_ctx_fits(cli_home: Path, monkeypatch, capsys) -> None:
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text(
        'preset = "embedded"\n' + _ISOLATED_BLOCK + "[dream.llm.dream]\nnum_ctx = 40000\n",
        encoding="utf-8",
    )
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "ctx window" in out
    assert "all checks passed" in out


def test_doctor_rejects_non_integer_num_predict(cli_home: Path, monkeypatch, capsys) -> None:
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text(
        'preset = "embedded"\n'
        + _ISOLATED_BLOCK
        + '[dream.llm.dream]\nnum_ctx = 40000\nnum_predict = "lots"\n',
        encoding="utf-8",
    )
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "num_predict must be an integer" in out


def test_doctor_delta_component_follows_config_ceiling(cli_home: Path, monkeypatch, capsys) -> None:
    """QA handoff: the doctor formula's delta component reads the
    dream.delta_budget_ceiling_tokens config key — lowering the ceiling makes
    a previously-failing window fit, WITHOUT touching num_ctx."""
    from mnemoseed_local.dream.delta import estimate_tokens
    from mnemoseed_local.dream.prompts import build_cache_prefix

    prefix = estimate_tokens(build_cache_prefix())
    margin = 2048  # DREAM_MARGIN_TOKENS_DEFAULT (num_predict unset)
    num_ctx = prefix + 5000 + margin + 16  # fits a 5000 ceiling, NOT the 32000 default
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text(
        'preset = "embedded"\n' + _ISOLATED_BLOCK + "[dream]\n"
        "delta_budget_ceiling_tokens = 5000\n"
        "[dream.llm.dream]\n"
        f"num_ctx = {num_ctx}\n",
        encoding="utf-8",
    )
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "all checks passed" in out

    # the same window with the 32000 default ceiling fails the check
    (cli_home / "config.toml").write_text(
        f'preset = "embedded"\n{_ISOLATED_BLOCK}[dream.llm.dream]\nnum_ctx = {num_ctx}\n',
        encoding="utf-8",
    )
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "lower the delta ceiling or raise num_ctx" in out


def test_doctor_skips_ctx_window_check_for_non_ollama_route(cli_home: Path, monkeypatch, capsys) -> None:
    """AC5: the ctx-window check (and its num_ctx hints) apply to the ollama
    driver only; a non-ollama route is skipped even with a tiny num_ctx."""
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text(
        'preset = "embedded"\n' + _ISOLATED_BLOCK + "[dream.llm.dream]\n"
        'driver = "openai_compatible"\n'
        "num_ctx = 1024\n",  # far too small: would fail the ollama check
        encoding="utf-8",
    )
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "all checks passed" in out
    assert "not ollama" in out
    assert "lower the delta ceiling" not in out


def test_doctor_reports_missing_isolated_instance_with_fix_hint(cli_home: Path, monkeypatch, capsys) -> None:
    """AC2: doctor hard-checks the isolated graph instance — a config without
    it is a FAIL with a fix hint, never a silent downgrade."""
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text('preset = "embedded"\n', encoding="utf-8")
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "isolated" in out
    assert "storage.graph.instances.isolated" in out
