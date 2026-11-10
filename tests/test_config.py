"""Config loading: preset resolution, per-layer override, named instances,
STORAGE_MODE shortcut, and validation errors that name the offending key."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mnemoseed_local.config import (
    DEFAULT_PRESET,
    Config,
    ConfigError,
    LayerSpec,
    default_config_toml,
    live_home_dir,
    load_config,
)


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _layer_spec(layer: str, driver: str) -> LayerSpec:
    return LayerSpec(layer=layer, driver=driver)


def test_default_config_is_embedded(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.preset == DEFAULT_PRESET
    assert cfg.layer_instances("vector")["main"].driver == "lancedb_embedded"
    assert cfg.layer_instances("graph")["main"].driver == "sqlite_graph"
    assert cfg.layer_instances("meta")["main"].driver == "sqlite_meta"
    assert cfg.layer_instances("embed")["main"].driver == "bge_m3_onnx"


def test_storage_mode_env_overrides_file_preset(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    _write(p, 'preset = "custom"\n')
    monkeypatch.setenv("STORAGE_MODE", "embedded")
    cfg = load_config(p)
    assert cfg.preset == "embedded"
    assert cfg.layer_instances("vector")["main"].driver == "lancedb_embedded"


def test_storage_mode_does_not_override_explicit_layer(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    _write(p, '[storage.embed]\ndriver = "synthetic"\n')
    monkeypatch.setenv("STORAGE_MODE", "embedded")
    assert load_config(p).layer_instances("embed")["main"].driver == "synthetic"


def test_storage_mode_invalid_names_env_key(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "nope")
    with pytest.raises(ConfigError, match=r"config\[STORAGE_MODE\]"):
        load_config(tmp_path / "missing.toml")


def test_config_file_invalid_preset_names_preset_key(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "nope"\n')
    with pytest.raises(ConfigError, match=r"config\[preset\]"):
        load_config(p)


def test_layer_override_wins_over_preset(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, '[storage.embed]\ndriver = "synthetic"\n')
    cfg = load_config(p)
    assert cfg.layer_instances("embed")["main"].driver == "synthetic"
    assert cfg.layer_instances("meta")["main"].driver == "sqlite_meta"  # preset fallback


def test_named_multi_instance(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        "[storage.graph]\n"
        "[storage.graph.instances.isolated]\n"
        'driver = "sqlite_graph"\n'
        'path = "~/.mnemoseed-local/isolated.db"\n',
    )
    graph = load_config(p).layer_instances("graph")
    assert set(graph) == {"main", "isolated"}
    assert graph["main"].driver == "sqlite_graph"
    assert graph["main"].params == {}
    assert graph["isolated"].driver == "sqlite_graph"
    assert graph["isolated"].params == {"path": "~/.mnemoseed-local/isolated.db"}


def test_instance_driver_wins_over_layer_driver(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        "[storage.embed]\n"
        'driver = "bge_m3_onnx"\n'
        "[storage.embed.instances.main]\n"
        'driver = "synthetic"\n'
        "[storage.embed.instances.isolated]\n"
        'driver = "synthetic"\n',
    )
    embed = load_config(p).layer_instances("embed")
    assert embed["main"].driver == "synthetic"
    assert embed["isolated"].driver == "synthetic"


def test_custom_preset_requires_explicit_layer(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "custom"\n[storage.graph]\ndriver = "sqlite_graph"\n')
    cfg = load_config(p)
    assert cfg.layer_instances("graph")["main"].driver == "sqlite_graph"
    with pytest.raises(ConfigError, match=r"config\[storage.vector.driver\]"):
        cfg.layer_instances("vector")


def test_unknown_storage_layer_names_key(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, '[storage.brain]\ndriver = "x"\n')
    with pytest.raises(ConfigError, match=r"config\[storage.brain\]"):
        load_config(p)


def test_bad_layer_driver_type_names_key(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, "[storage.vector]\ndriver = 42\n")
    with pytest.raises(ConfigError, match=r"config\[storage.vector.driver\]"):
        load_config(p)


def test_bad_instance_driver_type_names_key(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, "[storage.graph.instances.isolated]\ndriver = 7\n")
    with pytest.raises(ConfigError, match=r"config\[storage.graph.instances.isolated.driver\]"):
        load_config(p)


def test_bad_storage_type_names_key(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'storage = "not-a-table"\n')
    with pytest.raises(ConfigError, match=r"config\[storage\]"):
        load_config(p)


def test_params_passthrough(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        '[storage.vector]\ndriver = "lancedb_embedded"\nuri = "/data/vector.lance"\nregion = "eu"\n',
    )
    params = load_config(p).layer_instances("vector")["main"].params
    assert params == {"uri": "/data/vector.lance", "region": "eu"}


def test_default_toml_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, default_config_toml())
    assert load_config(p).preset == DEFAULT_PRESET


def test_programmatic_config_resolves_without_file(tmp_path):
    cfg = Config(
        preset="embedded",
        storage={"vector": _layer_spec("vector", "lancedb_embedded")},
    )
    assert cfg.layer_instances("vector")["main"].driver == "lancedb_embedded"
    assert cfg.preset == "embedded"
    assert cfg.layer_instances("graph")["main"].driver == "sqlite_graph"


def test_unknown_layer_resolution_names_key():
    with pytest.raises(ConfigError, match=r"config\[storage.unknown\]"):
        Config().layer_instances("unknown")


_AUTO_FLAG_KEYS: dict[str, tuple[str, str]] = {
    "dream": ("auto_trigger", "floor_pool_points = 10.0"),
    "capture": ("auto_recall", "auto_recall_focal_floor = 0.5"),
}


def _auto_flag(cfg: Config, table: str) -> bool:
    """The auto flag a table carries (dream or capture)."""
    return cfg.dream.auto_trigger if table == "dream" else cfg.capture.auto_recall


@pytest.mark.parametrize("table", ["dream", "capture"])
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("missing-file", True),
        ("missing-table", True),
        ("empty-table", True),
        ("missing-key", True),
        ("explicit-true", True),
        ("explicit-false", False),
    ],
)
def test_auto_flag_load_matrix(tmp_path, monkeypatch, table, scenario, expected):
    """An absent table or absent key resolves ON; an explicit value is preserved."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    key, sibling = _AUTO_FLAG_KEYS[table]
    other_table = "capture" if table == "dream" else "dream"
    body = {
        "missing-file": None,
        "missing-table": f'preset = "embedded"\n[{other_table}]\n',
        "empty-table": f"[{table}]\n",
        "missing-key": f"[{table}]\n{sibling}\n",
        "explicit-true": f"[{table}]\n{key} = true\n",
        "explicit-false": f"[{table}]\n{key} = false\n",
    }[scenario]
    path = tmp_path / "config.toml"
    if body is not None:
        _write(path, body)
    assert _auto_flag(load_config(path), table) is expected


def test_dream_table_must_be_a_table(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'dream = "nope"\n')
    with pytest.raises(ConfigError, match=r"config\[dream\]"):
        load_config(p)


def test_dream_auto_trigger_must_be_boolean(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, '[dream]\nauto_trigger = "yes"\n')
    with pytest.raises(ConfigError, match=r"config\[dream.auto_trigger\]"):
        load_config(p)


def test_dream_token_budget_usd_load_raises_deprecation_error(tmp_path, monkeypatch):
    """AC1: the USD budget concept is removed (design/01 §4.1); a config that
    still carries dream.token_budget_usd fails with a deprecation error naming
    the key — a stale file can never silently run a budgetless engine."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream]\ntoken_budget_usd = 5.0\n')
    with pytest.raises(ConfigError, match=r"config\[dream\.token_budget_usd\].*deprecated and removed"):
        load_config(p)


def test_dream_config_has_no_token_budget_field(tmp_path, monkeypatch):
    """AC1: the resolved DreamConfig exposes no token_budget_usd field."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert not hasattr(cfg.dream, "token_budget_usd")


def test_default_config_toml_omits_token_budget_usd() -> None:
    """AC1: the init template carries no budget key (the template is the doc
    for the removed FR-2.5b cap)."""
    assert "token_budget_usd" not in default_config_toml()


def test_default_config_toml_writes_the_isolated_graph_instance(tmp_path, monkeypatch) -> None:
    """AC2: the init template writes an ACTIVE storage.graph.instances.isolated
    table (not a comment), so a fresh init resolves the isolated instance."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, default_config_toml())
    cfg = load_config(p)
    assert "isolated" in cfg.layer_instances("graph")


def test_default_config_toml_has_no_active_dream_route() -> None:
    """Fresh installs ship with NO dream route: the init template carries only
    a commented field reference for the role tables and points at the console
    wizard / config set as the write paths — never an active route key, never
    a baked-in model."""
    import tomllib

    template = default_config_toml()
    parsed = tomllib.loads(template)
    assert "llm" not in parsed.get("dream", {})
    assert "qwen3.5" not in template
    assert "gemma4" not in template
    assert "ollama pull" not in template
    assert "http://localhost:7788/#/config" in template


def test_default_config_toml_documents_the_role_fields(tmp_path, monkeypatch) -> None:
    """The commented field reference documents every writable role field and
    both seats (dream + verifier) so ensemble opt-ins discover their route."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    template = default_config_toml()
    fields = ("driver", "model", "base_url", "api_key_env", "max_tokens", "think", "num_ctx", "num_predict")
    for field in fields:
        assert field in template
    assert "dream_verifier" in template
    # a load of the template alone yields no routes
    p = tmp_path / "config.toml"
    _write(p, template)
    assert load_config(p).llm == {}


def test_no_factory_dream_route_exists(tmp_path, monkeypatch) -> None:
    """The factory route table is empty: a fresh load resolves no route, and a
    deliberate explicit route (with both driver and model) still wins."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    from mnemoseed_local.config import DEFAULT_LLM_ROUTES

    assert DEFAULT_LLM_ROUTES == {}
    cfg = load_config(tmp_path / "missing-config.toml")
    assert cfg.llm == {}
    p = tmp_path / "config.toml"
    _write(
        p,
        '[dream.llm.dream]\ndriver = "ollama"\nmodel = "qwen3.5:9b"\nthink = true\nnum_ctx = 36864\n',
    )
    dream = load_config(p).llm["dream"]
    assert dream.driver == "ollama"
    assert dream.model == "qwen3.5:9b"
    assert dream.params["think"] is True
    assert dream.params["num_ctx"] == 36864


# ---------------------------------------------------------------- [logging] (B2.12)


def test_logging_requests_defaults_off(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.logging.requests is False


def test_logging_requests_parses_true(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, "[logging]\nrequests = true\n")
    assert load_config(p).logging.requests is True


def test_logging_requests_rejects_non_bool(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, '[logging]\nrequests = "yes"\n')
    with pytest.raises(ConfigError, match=r"config\[logging.requests\]"):
        load_config(p)


# ---------------------------------------------------------------- profiles (#109)


def test_profiles_agent_bindings_parse(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, '[profiles]\nagent_bindings = { planner = "research" }\n')
    cfg = load_config(p)
    assert cfg.profiles.agent_bindings == {"planner": "research"}
    assert cfg.profiles.persona_for("planner") == "research"


def test_profiles_default_is_empty_and_unbound_agents_resolve_to_none(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.profiles.agent_bindings == {}
    assert cfg.profiles.persona_for("planner") is None
    assert cfg.profiles.persona_for(None) is None


@pytest.mark.parametrize(
    ("table", "bad_value"),
    [
        ('agent_bindings = { planner = "  " }', '"  "'),
        ('agent_bindings = { "" = "research" }', '""'),
        ("agent_bindings = { planner = 7 }", "7"),
        ("agent_bindings = 3", "3"),
    ],
)
def test_profiles_rejects_malformed_agent_bindings(tmp_path, table, bad_value):
    p = tmp_path / "config.toml"
    _write(p, f'preset = "embedded"\n[profiles]\n{table}\n')
    with pytest.raises(ConfigError, match=r"config\[profiles\.agent_bindings\]"):
        load_config(p)


def test_profiles_profile_for_resolves_bound_and_unbound(tmp_path, monkeypatch):
    """#130 routing: profile_for is the write-path isolation helper — same
    map as persona_for, None when unbound, and persona_for stays an alias so
    the two surfaces cannot drift."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, '[profiles]\nagent_bindings = { planner = "research", worker = "work" }\n')
    cfg = load_config(p)
    assert cfg.profiles.profile_for("planner") == "research"
    assert cfg.profiles.profile_for("worker") == "work"
    assert cfg.profiles.profile_for("ghost") is None
    assert cfg.profiles.profile_for(None) is None
    # persona_for is the neutral carrier alias — same result, never diverges
    assert cfg.profiles.persona_for("planner") == cfg.profiles.profile_for("planner")
    assert cfg.profiles.persona_for(None) is None


def test_profiles_profile_for_empty_map_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.profiles.profile_for("planner") is None
    assert cfg.profiles.profile_for(None) is None


def test_live_home_dir_resolves_override_and_blank_means_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("MNEMOSEED_LOCAL_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")
    installed = tmp_path / "user-home" / ".mnemoseed-local"
    assert live_home_dir() == installed
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", "")
    assert live_home_dir() == installed
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", "   ")
    assert live_home_dir() == installed
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "home"))
    assert live_home_dir() == tmp_path / "home"


def test_config_dir_ignores_a_blank_live_home_env(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    installed = home / ".mnemoseed-local"
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "MNEMOSEED_LOCAL_HOME": "",
    }
    blank = subprocess.run(
        [sys.executable, "-c", "import mnemoseed_local.config as c; print(c.CONFIG_DIR)"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert Path(blank.stdout.strip()) == installed

    override = tmp_path / "override-home"
    explicit = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mnemoseed_local.config as c; print(c.CONFIG_DIR)",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "MNEMOSEED_LOCAL_HOME": str(override),
        },
        cwd=str(tmp_path),
    )
    assert Path(explicit.stdout.strip()) == override
