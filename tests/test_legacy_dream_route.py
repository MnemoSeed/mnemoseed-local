"""Legacy dream-route repair: the explicit, daemon-independent migration verb."""

from __future__ import annotations

import asyncio
import socket
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mnemoseed_local.cli import main
from mnemoseed_local.config import ROLE_ROUTE_REQUIRED_MESSAGE, load_config
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DRIVER = "openai"
MODEL = "gpt-4o-mini"
REPAIR = ["config", "repair-dream-route", "--driver", DRIVER, "--model", MODEL]


def _run(args: list[str]) -> int:
    try:
        return main(args)
    except SystemExit as exc:
        return int(exc.code or 0)


def _spare_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    base = tmp_path / "home"
    base.mkdir()
    config_path = base / "config.toml"
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(base))
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", base)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", config_path)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", base)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_PATH", config_path)
    monkeypatch.setattr("mnemoseed_local.storage.drivers.sqlite_meta.CONFIG_DIR", base)
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    return SimpleNamespace(base=base, path=config_path, port=_spare_port())


def _install(home: SimpleNamespace, fixture: str) -> str:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    text = text.replace("__BASEURL__", f"http://127.0.0.1:{home.port}")
    home.path.write_text(text, encoding="utf-8")
    return text


def _no_listener(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _table_keys(text: str, role: str) -> set[str]:
    return set(tomllib.loads(text).get("dream", {}).get("llm", {}).get(role, {}))


def _assert_repaired(home: SimpleNamespace, original: str, roles: tuple[str, ...]) -> None:
    cfg = load_config(home.path)
    raw = tomllib.loads(home.path.read_text(encoding="utf-8"))
    tables = raw["dream"]["llm"]
    assert set(tables) == set(roles)
    for role in roles:
        assert (cfg.llm[role].driver, cfg.llm[role].model) == (DRIVER, MODEL)
        table = tables[role]
        assert (table["driver"], table["model"]) == (DRIVER, MODEL)
        assert set(table) == _table_keys(original, role) | {"driver", "model"}


def _assert_meta_records(home: SimpleNamespace, roles: tuple[str, ...]) -> None:
    meta = SqliteMetaDriver(path=str(home.base / "meta.db"))
    try:
        for role in roles:
            assert meta.get_config(f"dream.llm.{role}.driver") is not None
            assert meta.get_config(f"dream.llm.{role}.driver").value["value"] == DRIVER
            assert meta.get_config(f"dream.llm.{role}.model").value["value"] == MODEL
    finally:
        asyncio.run(meta.close())


@pytest.mark.parametrize(
    ("fixture", "roles"),
    [
        ("legacy_driver_only.toml", ("dream",)),
        ("legacy_model_only.toml", ("dream",)),
        ("legacy_both_missing.toml", ("dream",)),
    ],
)
def test_repair_restores_each_legacy_shape(
    home: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    roles: tuple[str, ...],
) -> None:
    def _forbid_daemon(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("repair must not contact the daemon")

    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", _forbid_daemon)
    monkeypatch.setattr("mnemoseed_local.rest_client.DaemonClient.post", _forbid_daemon)
    original = _install(home, fixture)
    with pytest.raises(ValueError) as exc:
        load_config(home.path)
    assert ROLE_ROUTE_REQUIRED_MESSAGE in str(exc.value)
    assert _no_listener(home.port)

    assert _run(REPAIR) == 0
    assert _no_listener(home.port)
    _assert_repaired(home, original, roles)
    _assert_meta_records(home, roles)


def test_two_incomplete_roles_are_repaired_in_one_command(
    home: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    original = _install(home, "legacy_two_incomplete_roles.toml")
    assert _run(REPAIR) == 0
    out = capsys.readouterr().out
    assert "dream_verifier" in out
    _assert_repaired(home, original, ("dream", "dream_verifier"))
    _assert_meta_records(home, ("dream", "dream_verifier"))


def test_mid_batch_write_failure_exits_nonzero_with_a_clear_message(
    home: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mnemoseed_local.configwrite import service as service_module

    original_patch = service_module._patch_toml
    calls = {"count": 0}

    def _flaky_patch(path: Path, key_path: str, value: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            return
        original_patch(path, key_path, value)

    monkeypatch.setattr(service_module, "_patch_toml", _flaky_patch)
    _install(home, "legacy_driver_only.toml")

    assert _run(REPAIR) != 0
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "reload" in captured.err.lower() or "invalid" in captured.err.lower()
    partial = tomllib.loads(home.path.read_text(encoding="utf-8"))["dream"]["llm"]["dream"]
    assert partial.get("driver") == DRIVER
    assert "model" not in partial
    with pytest.raises(ValueError):
        load_config(home.path)


def test_detection_prints_migration_hint_without_mutating(
    home: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(home, "legacy_driver_only.toml")
    before = home.base / "config.toml"
    snapshot = {p.relative_to(home.base): p.read_bytes() for p in home.base.rglob("*") if p.is_file()}

    assert _run(["doctor"]) != 0
    out = capsys.readouterr().out
    assert ROLE_ROUTE_REQUIRED_MESSAGE in out
    assert "repair-dream-route" in out
    assert "YOUR_DRIVER" in out and "YOUR_MODEL_ID" in out
    assert before.read_bytes() == snapshot[Path("config.toml")]
    assert {p.relative_to(home.base): p.read_bytes() for p in home.base.rglob("*") if p.is_file()} == snapshot


def test_up_prints_the_same_hint_on_a_non_booting_home(
    home: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(home, "legacy_driver_only.toml")
    assert _run(["up", "--port", str(home.port)]) != 0
    err = capsys.readouterr().err
    assert "repair-dream-route" in err
    assert _no_listener(home.port)


def test_other_config_error_keeps_its_original_message(
    home: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    home.path.write_text('preset = "nonsense"\n', encoding="utf-8")
    assert _run(["doctor"]) != 0
    out = capsys.readouterr().out
    assert "unknown preset" in out
    assert "repair-dream-route" not in out
