"""Config loading: preset resolution, per-layer override, named instances,
STORAGE_MODE shortcut, and validation errors that name the offending key."""

import pytest

from mnemoseed_local.config import (
    DEFAULT_DREAM_TOKEN_BUDGET_USD,
    DEFAULT_PRESET,
    Config,
    ConfigError,
    LayerSpec,
    default_config_toml,
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


def test_dream_auto_trigger_defaults_to_false(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.dream.auto_trigger is False


def test_dream_auto_trigger_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream]\nauto_trigger = true\n')
    assert load_config(p).dream.auto_trigger is True


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


def test_dream_token_budget_usd_defaults_to_five(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.dream.token_budget_usd == DEFAULT_DREAM_TOKEN_BUDGET_USD == 5.0


def test_dream_token_budget_usd_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream]\ntoken_budget_usd = 12.5\n')
    assert load_config(p).dream.token_budget_usd == 12.5


def test_dream_token_budget_usd_negative_names_key(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, "[dream]\ntoken_budget_usd = -1\n")
    with pytest.raises(ConfigError, match=r"config\[dream.token_budget_usd\]"):
        load_config(p)


def test_dream_token_budget_usd_must_be_number(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, '[dream]\ntoken_budget_usd = "five"\n')
    with pytest.raises(ConfigError, match=r"config\[dream.token_budget_usd\]"):
        load_config(p)
