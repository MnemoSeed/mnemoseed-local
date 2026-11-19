"""Atomic multi-key config writes: POST /api/v1/config/set-many.

The console binds an LLM role by writing driver+model in one batch. The batch
must validate through the existing registry specs and generation semantics,
land all-or-nothing, and never leave a partial role table on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute, APIRouter

from mnemoseed_local.config import ROLE_ROUTE_REQUIRED_MESSAGE, load_config
from mnemoseed_local.configwrite.service import (
    ConfigWriteError,
    ConfigWriteService,
    GenerationMismatchError,
)
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver

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
