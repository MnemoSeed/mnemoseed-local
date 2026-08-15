"""Decay configuration (config.py [decay] table + configwrite registry keys).

The engine's tunables are real config: ``decay.enabled``,
``decay.sweep_interval_s``, ``decay.min_apply_delta`` and
``decay.lambda_per_type`` parse from config.toml, are writable through the
ConfigWriteService registry (live-apply without a restart), survive the
surgical TOML patch, and are imported by the DB-primary boot reconcile exactly
like the dream keys.
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
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import AuditFilter, Page


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _base_toml(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    _write(
        path,
        'preset = "embedded"\nbaseurl = "http://localhost:7788"\n\n[decay]\nenabled = true\n',
    )
    return path


def _service(tmp_path: Path, *, meta: SqliteMetaDriver | None = None) -> tuple[ConfigWriteService, Path]:
    path = _base_toml(tmp_path)
    return (
        ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0),
        path,
    )


# ---------------------------------------------------------------- config load


def test_decay_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.decay.enabled is True
    assert cfg.decay.sweep_interval_s == 86400.0  # NFR-4.1: once daily
    assert cfg.decay.min_apply_delta == 0.01
    assert cfg.decay.lambda_per_type["EPISODE"] == pytest.approx(0.03)


def test_decay_table_parses(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = tmp_path / "config.toml"
    _write(
        path,
        'preset = "embedded"\n'
        "[decay]\n"
        "enabled = false\n"
        "sweep_interval_s = 3600.0\n"
        "min_apply_delta = 0.0\n"
        'lambda_per_type = {"EPISODE" = 0.05}\n',
    )
    cfg = load_config(path)
    assert cfg.decay.enabled is False
    assert cfg.decay.sweep_interval_s == 3600.0
    assert cfg.decay.min_apply_delta == 0.0
    # the file's map is carried verbatim (partial maps resolve at sweep time
    # through lambda_for's default fallback)
    assert cfg.decay.lambda_per_type == {"EPISODE": 0.05}


def test_decay_lambda_subtable_form_parses(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = tmp_path / "config.toml"
    _write(
        path,
        'preset = "embedded"\n[decay]\n[decay.lambda_per_type]\nEPISODE = 0.05\n',
    )
    assert load_config(path).decay.lambda_per_type == {"EPISODE": 0.05}


def test_decay_bad_values_name_the_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    for body, key in (
        ('[decay]\nenabled = "yes"\n', "decay.enabled"),
        ("[decay]\nsweep_interval_s = -1\n", "decay.sweep_interval_s"),
        ('[decay]\nmin_apply_delta = "tiny"\n', "decay.min_apply_delta"),
        ('[decay]\nlambda_per_type = {"EPISODE" = 0}\n', "decay.lambda_per_type.EPISODE"),
        ('[decay]\nlambda_per_type = {"BOGUS" = 0.1}\n', "decay.lambda_per_type.BOGUS"),
    ):
        path = tmp_path / "config.toml"
        _write(path, 'preset = "embedded"\n' + body)
        with pytest.raises(ConfigError, match=rf"config\[{key}\]"):
            load_config(path)


# ---------------------------------------------------------------- configwrite registry


def test_configwrite_registry_has_decay_keys() -> None:
    for key in (
        "decay.enabled",
        "decay.sweep_interval_s",
        "decay.min_apply_delta",
        "decay.lambda_per_type",
    ):
        assert key in CONFIG_KEY_REGISTRY


def test_configwrite_set_decay_flag_live_applies_and_patches(tmp_path) -> None:
    service, path = _service(tmp_path)
    result = service.set("decay.enabled", False, actor="console")
    assert result["ok"] is True
    assert result["restart_required"] is False  # hot-apply, no restart
    assert service._config.decay.enabled is False
    text = path.read_text(encoding="utf-8")
    assert "enabled = false" in text


def test_configwrite_set_interval_and_delta_live_apply(tmp_path) -> None:
    service, _path = _service(tmp_path)
    service.set("decay.sweep_interval_s", 3600.0, actor="console")
    service.set("decay.min_apply_delta", 0.05, actor="console")
    assert service._config.decay.sweep_interval_s == 3600.0
    assert service._config.decay.min_apply_delta == 0.05


def test_configwrite_set_lambda_map_patches_and_roundtrips(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    service, path = _service(tmp_path)
    service.set("decay.lambda_per_type", {"EPISODE": 0.05}, actor="console")
    assert service._config.decay.lambda_per_type == {"EPISODE": 0.05}
    # the surgical patch wrote the inline TOML map and the file reloads cleanly
    assert 'lambda_per_type = {"EPISODE" = 0.05}' in path.read_text(encoding="utf-8")
    assert load_config(path).decay.lambda_per_type == {"EPISODE": 0.05}


def test_configwrite_lambda_map_validation(tmp_path) -> None:
    service, _path = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match="BOGUS"):
        service.set("decay.lambda_per_type", {"BOGUS": 0.1}, actor="console")
    with pytest.raises(ConfigWriteError, match="must be a positive number"):
        service.set("decay.lambda_per_type", {"EPISODE": 0}, actor="console")
    with pytest.raises(ConfigWriteError, match="must be an object"):
        service.set("decay.lambda_per_type", [0.1], actor="console")


def test_configwrite_lambda_set_cleans_legacy_subtable(tmp_path, monkeypatch) -> None:
    """A hand-written ``[decay.lambda_per_type]`` sub-table is replaced by the
    canonical inline form on write, so the regenerated file never double-defines
    the key (a duplicate table + inline pair would fail the next load)."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = tmp_path / "config.toml"
    _write(
        path,
        'preset = "embedded"\n[decay]\nenabled = true\n[decay.lambda_per_type]\nEPISODE = 0.05\n',
    )
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1.0)
    service.set("decay.lambda_per_type", {"EPISODE": 0.2}, actor="console")
    text = path.read_text(encoding="utf-8")
    assert text.count("lambda_per_type") == 1  # the stale sub-table is gone
    assert load_config(path).decay.lambda_per_type == {"EPISODE": 0.2}


def test_configwrite_set_records_version_and_audits(tmp_path) -> None:
    meta = SqliteMetaDriver(path=str(tmp_path / "meta.db"))
    service, _path = _service(tmp_path, meta=meta)
    result = service.set("decay.enabled", False, actor="cli")
    assert isinstance(result["version_id"], int)
    assert meta.get_config("decay.enabled") is not None
    entries = meta.audit_query(AuditFilter(action="config.set"), Page(limit=10)).items
    assert entries[-1].actor == "cli"
    assert entries[-1].detail["key_path"] == "decay.enabled"


def test_reconcile_boot_imports_decay_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    meta = SqliteMetaDriver(path=str(tmp_path / "meta.db"))
    service, _path = _service(tmp_path, meta=meta)
    service.reconcile_boot()
    assert meta.get_config("decay.enabled") is not None
    assert meta.get_config("decay.sweep_interval_s") is not None
    assert meta.get_config("decay.min_apply_delta") is not None
    assert meta.get_config("decay.lambda_per_type") is not None
    entries = meta.audit_query(AuditFilter(action="config_import"), Page(limit=10)).items
    assert "decay.enabled" in entries[-1].detail["keys_imported"]
