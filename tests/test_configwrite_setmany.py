"""Atomic multi-key config writes: POST /api/v1/config/set-many.

The console binds an LLM role by writing driver+model in one batch. The batch
must validate through the existing registry specs and generation semantics,
land all-or-nothing, and never leave a partial role table on disk.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute, APIRouter

from mnemoseed_local.config import ROLE_ROUTE_REQUIRED_MESSAGE, load_config
from mnemoseed_local.configwrite import service as configwrite_module
from mnemoseed_local.configwrite.service import (
    ConfigWriteError,
    ConfigWriteService,
    GenerationMismatchError,
    _file_fingerprint,
)
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import AuditFilter, Page

DRIVER_KEY = "dream.llm.dream.driver"
MODEL_KEY = "dream.llm.dream.model"
PAIR = [(DRIVER_KEY, "ollama"), (MODEL_KEY, "llama3.1:8b")]


def _fresh_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\nbaseurl = "http://localhost:7788"\n[dream]\nauto_trigger = false\n',
        encoding="utf-8",
    )
    return path


def _service(tmp_path: Path, *, meta: bool = False) -> tuple[ConfigWriteService, Path]:
    path = _fresh_config(tmp_path)
    store = SqliteMetaDriver(path=str(tmp_path / "meta.db")) if meta else None
    return ConfigWriteService(load_config(path), store), path


def _audit_actions(meta: SqliteMetaDriver, action: str) -> list[Any]:
    return meta.audit_query(AuditFilter(action=action), Page(limit=100)).items


def _fault_second_record(monkeypatch: pytest.MonkeyPatch, meta: SqliteMetaDriver) -> None:
    """Fail the second versioned record (after its file patch) to simulate a
    mid-batch store fault."""
    real_set_config = meta.set_config
    records = {"count": 0}

    def fault(key: str, value: dict[str, Any]) -> int:
        if not key.startswith("__"):
            records["count"] += 1
            if records["count"] == 2:
                raise RuntimeError("simulated meta fault")
        return real_set_config(key, value)

    monkeypatch.setattr(meta, "set_config", fault)


def test_set_many_atomic_success_pairs_role_and_reload_valid(tmp_path: Path) -> None:
    service, path = _service(tmp_path, meta=True)
    result = service.set_many(PAIR)
    assert result["ok"] is True
    assert [a["key_path"] for a in result["applied"]] == [DRIVER_KEY, MODEL_KEY]
    assert all(isinstance(a["version_id"], int) for a in result["applied"])
    assert all(a["restart_required"] is False for a in result["applied"])
    assert result["generation"] == 2
    assert service.generation == 2
    assert service.generation_for("dream") == 2
    cfg = load_config(path)
    assert cfg.llm["dream"].driver == "ollama"
    assert cfg.llm["dream"].model == "llama3.1:8b"


def test_set_many_driver_only_role_is_rejected_with_no_file_change(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ConfigWriteError) as exc:
        service.set_many([(DRIVER_KEY, "ollama")])
    assert ROLE_ROUTE_REQUIRED_MESSAGE in str(exc.value)
    assert path.read_bytes() == before
    assert "[dream.llm.dream]" not in path.read_text(encoding="utf-8")
    assert "dream" not in service._config.llm
    assert service.generation == 0


def test_set_many_model_only_role_is_rejected_with_no_file_change(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ConfigWriteError):
        service.set_many([(MODEL_KEY, "llama3.1:8b")])
    assert path.read_bytes() == before
    assert service.generation == 0


def test_set_many_base_url_only_role_is_rejected_with_no_file_change(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ConfigWriteError) as exc:
        service.set_many([("dream.llm.dream.base_url", "http://example.test/v1")])
    assert ROLE_ROUTE_REQUIRED_MESSAGE in str(exc.value)
    assert path.read_bytes() == before
    assert service.generation == 0


def test_set_many_full_bind_with_extra_fields_succeeds(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    result = service.set_many([*PAIR, ("dream.llm.dream.base_url", "http://example.test/v1")])
    assert result["ok"] is True
    assert len(result["applied"]) == 3
    assert load_config(path).llm["dream"].params["base_url"] == "http://example.test/v1"


def test_set_many_bad_second_update_leaves_no_partial_first_write(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ConfigWriteError) as exc:
        service.set_many([(DRIVER_KEY, "ollama"), ("dream.no_such_key", 1)])
    assert "dream.no_such_key" in str(exc.value)
    assert path.read_bytes() == before
    assert "dream" not in service._config.llm
    assert service.generation == 0


def test_set_many_stale_generation_is_rejected_before_any_write(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.auto_trigger", True, actor="test")
    before = path.read_bytes()
    with pytest.raises(GenerationMismatchError):
        service.set_many(PAIR, expected_generation=0)
    assert path.read_bytes() == before
    assert service.generation == 1


def test_set_many_file_write_fault_mid_batch_restores_file_and_live_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landing fault on a middle write must leave the file, the live config
    and the generation counters exactly as before the batch."""
    service, path = _service(tmp_path, meta=True)
    before = path.read_bytes()
    llm_before = deepcopy(service._config.llm)
    raw_before = deepcopy(service._config.raw)
    real_patch = configwrite_module._patch_toml
    writes = {"count": 0}

    def fault_on_second_write(patch_path: Path, key_path: str, value: Any) -> None:
        writes["count"] += 1
        if writes["count"] == 2:
            raise OSError("simulated landing fault")
        real_patch(patch_path, key_path, value)

    monkeypatch.setattr(configwrite_module, "_patch_toml", fault_on_second_write)
    with pytest.raises(OSError, match="simulated landing fault"):
        service.set_many([*PAIR, ("dream.llm.dream.base_url", "http://example.test/v1")])
    assert path.read_bytes() == before
    assert "[dream.llm.dream]" not in path.read_text(encoding="utf-8")
    assert service._config.llm == llm_before
    assert service._config.raw == raw_before
    assert service.generation == 0
    assert service.generation_for("dream") == 0
    assert service._last_fingerprint() == _file_fingerprint(path)
    assert load_config(path).llm == {}
    rollback_audits = _audit_actions(service._meta, "config_batch_rollback")
    assert len(rollback_audits) == 1
    assert rollback_audits[0].detail["keys"] == [DRIVER_KEY]


def test_set_many_meta_fault_mid_batch_restores_file_and_live_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A versioned-record fault on a middle write rolls the whole batch back:
    the file patch that already landed is undone with the live config, the
    fingerprint baseline tracks the restored file, and the rollback is audited."""
    service, path = _service(tmp_path, meta=True)
    before = path.read_bytes()
    llm_before = deepcopy(service._config.llm)
    raw_before = deepcopy(service._config.raw)
    _fault_second_record(monkeypatch, service._meta)
    with pytest.raises(RuntimeError, match="simulated meta fault"):
        service.set_many([*PAIR, ("dream.llm.dream.base_url", "http://example.test/v1")])
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert "[dream.llm.dream]" not in path.read_text(encoding="utf-8")
    assert service._config.llm == llm_before
    assert service._config.raw == raw_before
    assert service.generation == 0
    assert service.generation_for("dream") == 0
    assert service._last_fingerprint() == _file_fingerprint(path)
    assert load_config(path).llm == {}
    rollback_audits = _audit_actions(service._meta, "config_batch_rollback")
    assert len(rollback_audits) == 1
    assert rollback_audits[0].detail["keys"] == [DRIVER_KEY]


def test_set_many_meta_fault_then_boot_reconcile_does_not_resurrect_role_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A faulted batch must not survive its own rollback: driving the SAME boot
    path (load_config + reconcile_boot) against the restored state rewrites
    nothing from the batch, keeps the file loadable and leaves no partial role
    table."""
    service, path = _service(tmp_path, meta=True)
    before = path.read_bytes()
    _fault_second_record(monkeypatch, service._meta)
    with pytest.raises(RuntimeError, match="simulated meta fault"):
        service.set_many(PAIR)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert service._last_fingerprint() == _file_fingerprint(path)
    boot_service = ConfigWriteService(load_config(path), service._meta, clock=lambda: 1_700_000_000.0)
    result = boot_service.reconcile_boot()
    assert result["ok"] is True
    assert result["mirror_rewritten"] == []
    # the landed key's phantom+compensating records make the one-shot import
    # skip it; the faulted key was never recorded and imports like every
    # never-written key
    assert "dream.llm.dream.driver" not in result["keys_updated"]
    assert "dream.llm.dream.model" in result["keys_updated"]
    assert load_config(path).llm == {}
    assert boot_service._config.llm == {}


def test_set_many_meta_fault_then_boot_reconcile_does_not_resurrect_non_role_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same oracle for a plain (non-role) registry key: the faulted value
    must not re-emerge from the settings DB on the next boot."""
    service, path = _service(tmp_path, meta=True)
    before = path.read_bytes()
    _fault_second_record(monkeypatch, service._meta)
    with pytest.raises(RuntimeError, match="simulated meta fault"):
        service.set_many([("dream.auto_trigger", True), ("decay.sweep_interval_s", 3600.0)])
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert service._last_fingerprint() == _file_fingerprint(path)
    boot_service = ConfigWriteService(load_config(path), service._meta, clock=lambda: 1_700_000_000.0)
    result = boot_service.reconcile_boot()
    assert result["ok"] is True
    assert result["mirror_rewritten"] == []
    assert "dream.auto_trigger" not in result["keys_updated"]
    assert load_config(path).dream.auto_trigger is False
    assert boot_service._config.dream.auto_trigger is False


def test_console_bind_endpoint_is_registered_and_used_by_app_js() -> None:
    from mnemoseed_local.daemon.app import create_app

    paths: set[str] = set()
    for route in create_app().routes:
        if isinstance(route, APIRoute):
            paths.add(route.path)
        elif isinstance(route, APIRouter):
            paths.update(r.path for r in route.routes if isinstance(r, APIRoute))
        else:
            included = getattr(route, "include_context", None)
            original = getattr(included, "included_router", None) if included is not None else None
            if original is not None:
                paths.update(r.path for r in original.routes if isinstance(r, APIRoute))
    assert "/api/v1/config/set-many" in paths
    app_js = (
        Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local" / "console" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert 'fetchJson("/api/v1/config/set-many"' in app_js
