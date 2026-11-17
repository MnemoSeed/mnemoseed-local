"""Endpoint record, PID pre-filter, and unauthenticated instance handshake."""

from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path
from typing import Any

import auth_support
import httpx
import pytest

from mnemoseed_local import config as config_mod
from mnemoseed_local import endpoint


def _record(*, pid: int | None = None, instance_id: str = "instance-a") -> endpoint.EndpointRecord:
    return endpoint.EndpointRecord(
        pid=os.getpid() if pid is None else pid,
        instance_id=instance_id,
        base_url="http://127.0.0.1:8899",
        started_at=1_700_000_000.0,
    )


def _recorder(instance_id: str, seen: list[httpx.Request], *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"instance_id": instance_id, "status": "ok"})

    return httpx.MockTransport(handler)


def test_record_roundtrip_and_clean(tmp_path: Path) -> None:
    path = tmp_path / "endpoint.json"
    record = _record()
    endpoint.write_endpoint(record, path=path)

    assert endpoint.read_endpoint(path=path) == record
    endpoint.clear_endpoint(path=path)
    assert endpoint.read_endpoint(path=path) is None


def test_base_path_uses_call_time_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = config_mod.CONFIG_DIR
    try:
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        endpoint.write_endpoint(_record())
        assert (tmp_path / "endpoint.json").is_file()
        assert endpoint.read_endpoint() is not None
    finally:
        monkeypatch.setattr(config_mod, "CONFIG_DIR", original)


def test_missing_record_is_stale() -> None:
    with pytest.raises(endpoint.StaleEndpointError, match="no daemon endpoint record"):
        endpoint.resolve("http://127.0.0.1:8899", path=Path("missing-endpoint.json"))


def test_dead_pid_prefilter_rejects_before_any_request(tmp_path: Path) -> None:
    path = tmp_path / "endpoint.json"
    endpoint.write_endpoint(_record(pid=2**31 - 1), path=path)
    seen: list[httpx.Request] = []

    with pytest.raises(endpoint.StaleEndpointError, match="not running"):
        endpoint.resolve(
            "http://127.0.0.1:8899",
            path=path,
            pid_alive=lambda _: False,
            transport=_recorder("instance-a", seen),
        )
    assert seen == []


def test_forged_same_pid_different_instance_id_rejected_without_bearer(tmp_path: Path) -> None:
    """AC4: a live PID with a replaced process must never see a bearer header."""
    path = tmp_path / "endpoint.json"
    rec = _record(instance_id="instance-a")
    endpoint.write_endpoint(rec, path=path)
    seen: list[httpx.Request] = []

    with pytest.raises(endpoint.StaleEndpointError, match="instance mismatch"):
        endpoint.resolve(
            rec.base_url,
            path=path,
            pid_alive=lambda _: True,
            transport=_recorder("forged-instance", seen),
        )

    assert len(seen) == 1
    assert seen[0].url.path == "/healthz"
    assert "authorization" not in seen[0].headers


def test_verification_http_failure_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "endpoint.json"
    rec = _record()
    endpoint.write_endpoint(rec, path=path)
    seen: list[httpx.Request] = []

    with pytest.raises(endpoint.StaleEndpointError):
        endpoint.resolve(
            rec.base_url,
            path=path,
            pid_alive=lambda _: True,
            transport=_recorder("instance-a", seen, status=500),
        )
    assert "authorization" not in seen[0].headers


def test_successful_handshake_returns_verified_base_url(tmp_path: Path) -> None:
    path = tmp_path / "endpoint.json"
    rec = _record()
    endpoint.write_endpoint(rec, path=path)
    seen: list[httpx.Request] = []

    resolved = endpoint.resolve(
        rec.base_url,
        path=path,
        pid_alive=lambda _: True,
        transport=_recorder("instance-a", seen),
    )

    assert resolved == rec.base_url
    assert "authorization" not in seen[0].headers


def test_override_skips_verification_and_warns(tmp_path: Path) -> None:
    path = tmp_path / "endpoint.json"
    endpoint.write_endpoint(_record(), path=path)
    warnings: list[str] = []
    seen: list[httpx.Request] = []

    resolved = endpoint.resolve(
        "http://127.0.0.1:9999",
        override=True,
        path=path,
        transport=_recorder("forged", seen),
        warn=warnings.append,
    )

    assert resolved == "http://127.0.0.1:9999"
    assert warnings and "verification skipped" in warnings[0]
    assert seen == []


def _write_daemon_config(home: Path) -> None:
    (home / "config.toml").write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )


def test_daemon_publishes_endpoint_on_ready_and_removes_on_clean_shutdown(tmp_path: Path) -> None:
    _write_daemon_config(tmp_path)
    endpoint_path = tmp_path / "endpoint.json"

    with auth_support.boot(tmp_path) as client:
        body = client.get("/healthz").json()
        assert endpoint_path.is_file()
        record = endpoint.read_endpoint(path=endpoint_path)
        assert record is not None
        assert record.pid == os.getpid()
        assert record.instance_id == body["instance_id"]
        assert record.base_url == config_mod.load_config(tmp_path / "config.toml").baseurl.rstrip("/")
        assert record.started_at > 0
        assert isinstance(body["instance_id"], str) and body["instance_id"]

    assert not endpoint_path.exists()


def test_instance_id_is_per_start(tmp_path: Path) -> None:
    _write_daemon_config(tmp_path)
    with auth_support.boot(tmp_path) as client:
        first = client.get("/healthz").json()["instance_id"]
    with auth_support.boot(tmp_path) as client:
        second = client.get("/healthz").json()["instance_id"]
    assert first != second
    assert uuid.UUID(first) and uuid.UUID(second)


def test_explicit_override_warning_from_hook_base_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mnemoseed_local.hosts.common import daemon as hook_daemon

    seen: list[dict[str, Any]] = []

    def spy(url: str, **kwargs: Any) -> str:
        seen.append({"url": url, "override": kwargs.get("override")})
        return url

    monkeypatch.setattr(endpoint, "resolve", spy)
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", "http://127.0.0.1:9123")
    assert hook_daemon.resolve_base_url(verify=True) == "http://127.0.0.1:9123"
    assert seen == [{"url": "http://127.0.0.1:9123", "override": True}]


def test_cli_and_importer_paths_use_endpoint_resolve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from mnemoseed_local import cli, rest_client

    seen: list[dict[str, Any]] = []

    def spy(url: str, **kwargs: Any) -> str:
        seen.append({"url": url, "override": kwargs.get("override", False)})
        return url

    monkeypatch.setattr(endpoint, "resolve", spy)
    monkeypatch.delenv("MNEMOSEED_LOCAL_BASEURL", raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text('preset = "embedded"\nbaseurl = "http://127.0.0.1:8345"\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_path)

    rest_client.resolve_client(argparse.Namespace(baseurl=None))
    assert {"url": "http://127.0.0.1:8345", "override": False} in seen

    cli._import_base_url()
    assert {"url": "http://127.0.0.1:8345", "override": False} in seen
