"""CLI surface (A2 MVP): init writes a working config, verb dispatch returns
proper exit codes, config ops are loopback-only, and uninstall --purge stays
scoped to the app's own config home."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mnemoseed_local.cli import build_parser, main, models_contain
from mnemoseed_local.config import default_config_toml, load_config
from mnemoseed_local.hardware import recommended_tier
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
        # The healthy fixture also reports the default dream model as pulled,
        # so the A3 "dream model" check passes alongside the "dream llm" probe.
        return HealthReport(ok=True, detail={"models": ["qwen3.5:9b"]})


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
    test wrote there. The hardware probes are patched too, so no real
    nvidia-smi / RAM lookup leaks the host's hardware into the report."""
    monkeypatch.setattr("mnemoseed_local.storage.factory.build_stores", lambda config: _FakeDoctorStores())
    monkeypatch.setattr("mnemoseed_local.llm.RoleRouter", _FakeDoctorRouter)
    monkeypatch.setattr("mnemoseed_local.hardware.probe_max_vram_gb", lambda: 0.0)
    monkeypatch.setattr("mnemoseed_local.hardware.probe_ram_gb", lambda: None)


#: Every doctor fixture carries the mandatory isolated graph instance (AC2): a
#: missing instance is itself a FAILING doctor check, so the healthy-fixture
#: tests include it and only the dedicated missing-isolated test omits it.
_ISOLATED_BLOCK = '[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n'


def test_doctor_reports_default_delta_ceiling_window_gap_on_factory_num_ctx(
    cli_home: Path, monkeypatch, capsys
) -> None:
    """D4 compounding: the factory route now ships num_ctx=16384, so the
    ollama 4096 silent-squeeze trap can no longer pass unnoticed; with the
    32000 delta ceiling still in place the doctor check FAILS honestly with
    the actionable hint (tier-coherent pairing lives on
    dream.delta_budget_ceiling_tokens — never in the driver)."""
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text('preset = "embedded"\n' + _ISOLATED_BLOCK, encoding="utf-8")
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "ctx window" in out
    assert "num_ctx=16384" in out
    assert "lower the delta ceiling or raise num_ctx" in out


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


# ------------------------------------------- A3 T5: doctor "dream model" check


def _write_doctor_config(cli_home: Path, extra: str = "") -> None:
    cli_home.mkdir(parents=True, exist_ok=True)
    # these tests exercise the model/tier checks, not the ctx window — give
    # the config a window-coherent num_ctx so the honest D4 default (factory
    # 16384 vs 32000 delta ceiling) doesn't wash the target check's outcome;
    # when `extra` opens the role table itself, the key lands inside it
    # instead of declaring the table twice
    window = "num_ctx = 40000\n"
    if "[dream.llm.dream]" not in extra:
        window = "[dream.llm.dream]\n" + window
    (cli_home / "config.toml").write_text(
        'preset = "embedded"\n' + _ISOLATED_BLOCK + extra + window,
        encoding="utf-8",
    )


def _router_class(report: HealthReport) -> type:
    """A RoleRouter stub whose dream LLM probe returns the given report."""

    class _ReportLLM:
        def check(self) -> HealthReport:
            return report

    class _ReportRouter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def resolve(self, role: str) -> _ReportLLM:
            del role
            return _ReportLLM()

    return _ReportRouter


def test_doctor_dream_model_present(cli_home: Path, monkeypatch, capsys) -> None:
    _write_doctor_config(cli_home)
    _mock_doctor_backend(cli_home, monkeypatch)
    report = HealthReport(ok=True, detail={"models": ["qwen3.5:9b", "bge-m3:latest"]})
    monkeypatch.setattr("mnemoseed_local.llm.RoleRouter", _router_class(report))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "[  ok] dream model: model 'qwen3.5:9b' present" in out


def test_doctor_dream_model_missing_fails_with_pull_hint(cli_home: Path, monkeypatch, capsys) -> None:
    """A pulled-list without the configured model is a FAIL with the pull hint
    — never a silent pull (A3 model-missing UX)."""
    _write_doctor_config(cli_home)
    _mock_doctor_backend(cli_home, monkeypatch)
    report = HealthReport(ok=True, detail={"models": ["someone-else:7b"]})
    monkeypatch.setattr("mnemoseed_local.llm.RoleRouter", _router_class(report))
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "model 'qwen3.5:9b' not pulled; run: ollama pull qwen3.5:9b" in out


def test_doctor_dream_model_server_unreachable(cli_home: Path, monkeypatch, capsys) -> None:
    """Server down is a FAIL with a start-ollama hint only (no pull advice)."""
    _write_doctor_config(cli_home)
    _mock_doctor_backend(cli_home, monkeypatch)
    report = HealthReport(ok=False, detail={"error": "connection refused"})
    monkeypatch.setattr("mnemoseed_local.llm.RoleRouter", _router_class(report))
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "ollama server unreachable (connection refused); start ollama first" in out
    assert "ollama pull" not in out


def test_doctor_dream_model_skips_non_ollama_route(cli_home: Path, monkeypatch, capsys) -> None:
    """Non-ollama dream routes skip the check (same precedent as ctx window)."""
    _write_doctor_config(
        cli_home,
        '[dream.llm.dream]\ndriver = "openai_compatible"\nmodel = "gpt-4o-mini"\n',
    )
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "dream model" in out
    assert "not ollama" in out
    assert "model presence check skipped" in out


def test_doctor_report_lists_new_checks_in_order(cli_home: Path, monkeypatch, capsys) -> None:
    _write_doctor_config(cli_home)
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "dream model" in out
    assert "hardware tier" in out
    assert out.index("dream llm") < out.index("dream model")
    # the informational tier line is config-only, before the storage assembly
    assert out.index("hardware tier") < out.index("storage")


# ------------------------------------------- B1 T3: doctor "ensemble verifier" check


class _PerRoleLLM:
    def __init__(self, report: HealthReport) -> None:
        self._report = report

    def check(self) -> HealthReport:
        return self._report


def _per_role_router_class(reports: dict[str, HealthReport]) -> type:
    """A RoleRouter stub resolving each role to its own canned health probe
    (the dream route and the verify judging seat probe different models)."""

    class _PerRoleRouter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def resolve(self, role: str) -> _PerRoleLLM:
            return _PerRoleLLM(reports[role])

    return _PerRoleRouter


def _verify_line(out: str) -> str:
    return next(line for line in out.splitlines() if "ensemble verifier" in line)


def test_doctor_verifier_check_skips_when_ensemble_off(cli_home: Path, monkeypatch, capsys) -> None:
    """The verifier judging seat is dormant with the ensemble off (factory
    default): the check reports the skip, never a false alarm."""
    _write_doctor_config(cli_home)  # default ensemble = "off"
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 0
    assert "skipped" in _verify_line(capsys.readouterr().out)


def test_doctor_verifier_present_when_verify_on(cli_home: Path, monkeypatch, capsys) -> None:
    _write_doctor_config(cli_home, '[dream]\nensemble = "verify"\n')
    _mock_doctor_backend(cli_home, monkeypatch)
    monkeypatch.setattr(
        "mnemoseed_local.llm.RoleRouter",
        _per_role_router_class(
            {
                "dream": HealthReport(ok=True, detail={"models": ["qwen3.5:9b"]}),
                "dream_verifier": HealthReport(ok=True, detail={"models": ["gemma4:e4b"]}),
            }
        ),
    )
    assert main(["doctor"]) == 0
    assert "[  ok] ensemble verifier: model 'gemma4:e4b' present" in capsys.readouterr().out


def test_doctor_verifier_missing_model_fails_with_pull_hint(cli_home: Path, monkeypatch, capsys) -> None:
    """Opted-in verify with a missing judge model is a FAIL with the pull hint
    — never a silent pull (the runtime fallback is the safety net, doctor is
    the honest report)."""
    _write_doctor_config(cli_home, '[dream]\nensemble = "verify"\n')
    _mock_doctor_backend(cli_home, monkeypatch)
    monkeypatch.setattr(
        "mnemoseed_local.llm.RoleRouter",
        _per_role_router_class(
            {
                "dream": HealthReport(ok=True, detail={"models": ["qwen3.5:9b"]}),
                "dream_verifier": HealthReport(ok=True, detail={"models": ["qwen3.5:9b"]}),
            }
        ),
    )
    assert main(["doctor"]) == 1
    line = _verify_line(capsys.readouterr().out)
    assert "model 'gemma4:e4b' not pulled; run: ollama pull gemma4:e4b" in line


def test_doctor_verifier_server_unreachable_fails_without_pull_advice(
    cli_home: Path, monkeypatch, capsys
) -> None:
    _write_doctor_config(cli_home, '[dream]\nensemble = "verify"\n')
    _mock_doctor_backend(cli_home, monkeypatch)
    monkeypatch.setattr(
        "mnemoseed_local.llm.RoleRouter",
        _per_role_router_class(
            {
                "dream": HealthReport(ok=True, detail={"models": ["qwen3.5:9b"]}),
                "dream_verifier": HealthReport(ok=False, detail={"error": "connection refused"}),
            }
        ),
    )
    assert main(["doctor"]) == 1
    line = _verify_line(capsys.readouterr().out)
    assert "ollama server unreachable (connection refused); start ollama first" in line
    assert "ollama pull" not in line


def test_doctor_verifier_skips_non_ollama_verifier_route(cli_home: Path, monkeypatch, capsys) -> None:
    """A non-ollama verifier route skips the inventory check (same precedent
    as the dream model and ctx-window checks: the provider's model inventory
    is out of doctor's reach)."""
    _write_doctor_config(
        cli_home,
        '[dream]\nensemble = "verify"\n'
        '[dream.llm.dream_verifier]\ndriver = "openai_compatible"\nmodel = "judge"\n',
    )
    _mock_doctor_backend(cli_home, monkeypatch)
    assert main(["doctor"]) == 0
    line = _verify_line(capsys.readouterr().out)
    assert "not ollama" in line
    assert "skipped" in line


# ------------------------------------------- A3 T5: model name normalization


@pytest.mark.parametrize(
    ("models", "configured", "expected"),
    [
        (["qwen3.5:9b"], "qwen3.5:9b", True),  # tag matches exactly
        (["qwen3.5:9b"], "qwen3.5:7b", False),  # a different tag misses
        (["qwen3.5:9b"], "qwen3.5", False),  # tagless config never matches a pinned tag
        (["qwen3.5:latest"], "qwen3.5", True),  # tagless config matches name:latest
        (["qwen3.5"], "qwen3.5", True),  # bare server entry, tagless config
        (["qwen3.5"], "qwen3.5:latest", True),  # server entry eliding the default :latest tag
        (["qwen3.5:9b"], "qwen3.5:latest", False),  # latest config never matches a pinned tag
        (["qwen3.5:9b", "bge-m3:latest"], "bge-m3", True),
        ([], "qwen3.5:9b", False),  # empty inventory misses
    ],
)
def test_models_contain_name_normalization(models, configured, expected) -> None:
    assert models_contain(models, configured) is expected


# ------------------------------------------- A3 T5: doctor "hardware tier"


@pytest.mark.parametrize(
    ("vram", "ram", "expected"),
    [
        (21.9, None, "standard"),  # just below the advanced VRAM floor
        (22.0, None, "advanced"),  # VRAM >= 22 GiB
        (6.9, 8.0, "lite"),  # below both floors
        (7.0, None, "standard"),  # VRAM >= 7 qualifies even with unknown RAM
        (None, 29.9, "lite"),  # just below the RAM floor
        (None, 30.0, "standard"),  # RAM >= 30 GiB alone qualifies
        (6.9, 30.0, "standard"),  # the RAM leg qualifies without a GPU
        (None, None, "lite"),  # both probes unknown degrades to lite
    ],
)
def test_recommended_tier_boundaries(vram, ram, expected) -> None:
    assert recommended_tier(vram, ram) == expected


def test_doctor_hardware_tier_pinned_format(cli_home: Path, monkeypatch, capsys) -> None:
    """The tier line is a pinned machine-readable contract for the install
    script: probe seams are injected, the exact format is regex-pinned."""
    _write_doctor_config(cli_home)
    _mock_doctor_backend(cli_home, monkeypatch)
    monkeypatch.setattr("mnemoseed_local.hardware.probe_max_vram_gb", lambda: 12.0)
    monkeypatch.setattr("mnemoseed_local.hardware.probe_ram_gb", lambda: 32.0)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    line = next(item for item in out.splitlines() if "hardware tier" in item)
    detail = line.split("hardware tier: ", 1)[1]
    assert re.fullmatch(
        r'^recommended tier "(standard|lite|advanced)" \(vram=\d+GB, ram=\d+GB\); current tier "\w+"$',
        detail,
    )
    assert 'recommended tier "standard" (vram=12GB, ram=32GB); current tier "standard"' in out


def test_doctor_hardware_tier_mismatch_is_hint_only(cli_home: Path, monkeypatch, capsys) -> None:
    """Recommended != current is a hint, never a FAIL (check stays ok)."""
    _write_doctor_config(cli_home, '[dream]\nhardware_tier = "lite"\n')
    _mock_doctor_backend(cli_home, monkeypatch)
    monkeypatch.setattr("mnemoseed_local.hardware.probe_max_vram_gb", lambda: 24.0)
    monkeypatch.setattr("mnemoseed_local.hardware.probe_ram_gb", lambda: 64.0)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert 'recommended tier "advanced" (vram=24GB, ram=64GB); current tier "lite"' in out
    assert "all checks passed" in out


# ------------------------------------------- A3 T5: up start check


def _write_up_config(cli_home: Path, extra: str = "") -> None:
    cli_home.mkdir(parents=True, exist_ok=True)
    (cli_home / "config.toml").write_text('preset = "embedded"\n' + extra, encoding="utf-8")


def _fake_up_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace the storage stack and the daemon runner with call counters, so
    no real daemon boots during cmd_up tests."""
    calls = {"build_stores": 0, "run_server": 0}

    class _FakeUpStores:
        async def close(self) -> None:
            pass

    def _fake_build_stores(config: object) -> _FakeUpStores:
        calls["build_stores"] += 1
        return _FakeUpStores()

    def _fake_run_server(host: str, port: int) -> int:
        calls["run_server"] += 1
        return 0

    monkeypatch.setattr("mnemoseed_local.storage.factory.build_stores", _fake_build_stores)
    monkeypatch.setattr("mnemoseed_local.daemon.runner.run_server", _fake_run_server)
    return calls


def test_up_refuses_to_boot_when_dream_model_missing(cli_home: Path, monkeypatch, capsys) -> None:
    """Model missing: rc 1 + pull hint, and the whole path spawns no
    subprocess (cli must never silently `ollama pull`)."""
    import subprocess

    def _no_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("the up path must never spawn a subprocess")

    _write_up_config(cli_home)
    calls = _fake_up_runtime(monkeypatch)
    monkeypatch.setattr(
        "mnemoseed_local.cli._dream_model_check",
        lambda config: (False, "model 'qwen3.5:9b' not pulled; run: ollama pull qwen3.5:9b"),
    )
    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
    assert main(["up"]) == 1
    err = capsys.readouterr().err
    assert "ollama pull qwen3.5:9b" in err
    assert "error: dream model 'qwen3.5:9b' not pulled" in err
    assert calls == {"build_stores": 0, "run_server": 0}


def test_up_missing_model_message_comes_from_the_real_check(cli_home: Path, monkeypatch, capsys) -> None:
    """Same evaluation as doctor, here through the deep router seam: the exact
    pinned stderr line proves the shared helper wired the real report."""
    _write_up_config(cli_home)
    calls = _fake_up_runtime(monkeypatch)
    monkeypatch.setattr(
        "mnemoseed_local.llm.RoleRouter",
        _router_class(HealthReport(ok=True, detail={"models": []})),
    )
    assert main(["up"]) == 1
    err = capsys.readouterr().err
    assert "error: dream model 'qwen3.5:9b' not pulled; run: ollama pull qwen3.5:9b" in err
    assert calls == {"build_stores": 0, "run_server": 0}


def test_up_refuses_to_boot_when_ollama_unreachable(cli_home: Path, monkeypatch, capsys) -> None:
    _write_up_config(cli_home)
    calls = _fake_up_runtime(monkeypatch)
    monkeypatch.setattr(
        "mnemoseed_local.llm.RoleRouter",
        _router_class(HealthReport(ok=False, detail={"error": "connection refused"})),
    )
    assert main(["up"]) == 1
    err = capsys.readouterr().err
    assert "ollama" in err
    assert "unreachable" in err
    assert "start ollama" in err
    assert "ollama pull" not in err
    assert calls == {"build_stores": 0, "run_server": 0}


def test_up_proceeds_when_dream_model_present(cli_home: Path, monkeypatch, capsys) -> None:
    _write_up_config(cli_home)
    calls = _fake_up_runtime(monkeypatch)
    monkeypatch.setattr(
        "mnemoseed_local.cli._dream_model_check",
        lambda config: (True, "model 'qwen3.5:9b' present"),
    )
    assert main(["up"]) == 0
    assert calls == {"build_stores": 1, "run_server": 1}
    assert "daemon on http://127.0.0.1:7788" in capsys.readouterr().out


def test_up_skips_model_preflight_for_non_ollama_route(cli_home: Path, monkeypatch, capsys) -> None:
    _write_up_config(
        cli_home,
        '[dream.llm.dream]\ndriver = "openai_compatible"\nmodel = "gpt-4o-mini"\n',
    )
    calls = _fake_up_runtime(monkeypatch)
    consulted: list[object] = []

    def _boom(config: object) -> tuple[bool, str]:
        consulted.append(config)
        raise AssertionError("a non-ollama route must not consult the model check")

    monkeypatch.setattr("mnemoseed_local.cli._dream_model_check", _boom)
    assert main(["up"]) == 0
    assert consulted == []
    assert calls == {"build_stores": 1, "run_server": 1}


# ------------------------------------------- A3 T5: init guidance


def test_init_prints_next_steps_guidance(cli_home: Path, capsys) -> None:
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "next steps:" in out
    assert "  1. mnemoseed-local doctor   (self-check incl. hardware tier)" in out
    assert "  2. ollama pull qwen3.5:9b   (dream model, first time only)" in out
    assert "  3. mnemoseed-local up" in out
