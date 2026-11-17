"""Missing or malformed endpoint records close every credential path."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local import cli, endpoint, rest_client
from mnemoseed_local import config as config_mod
from mnemoseed_local.hosts.common import daemon as hook_daemon


def _config(home: Path) -> None:
    (home / "config.toml").write_text(
        'preset = "embedded"\nbaseurl = "http://127.0.0.1:8899"\n', encoding="utf-8"
    )


@pytest.mark.parametrize("content", [None, "{not-json", "{}"])
def test_all_python_paths_fail_closed_without_a_valid_record(
    content: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.delenv("MNEMOSEED_LOCAL_BASEURL", raising=False)
    if content is not None:
        (tmp_path / "endpoint.json").write_text(content, encoding="utf-8")

    with pytest.raises(endpoint.StaleEndpointError):
        rest_client.resolve_client(argparse.Namespace(baseurl=None))
    with pytest.raises(endpoint.StaleEndpointError):
        cli._import_base_url()
    with pytest.raises(endpoint.StaleEndpointError):
        hook_daemon.resolve_base_url(verify=True)


def test_call_sites_request_fail_closed_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(tmp_path)
    seen: list[dict[str, Any]] = []

    def spy(url: str, **kwargs: Any) -> str:
        seen.append(
            {"url": url, "override": kwargs.get("override"), "allow_missing": kwargs.get("allow_missing")}
        )
        return url

    monkeypatch.setattr(endpoint, "resolve", spy)
    monkeypatch.delenv("MNEMOSEED_LOCAL_BASEURL", raising=False)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")

    rest_client.resolve_client(argparse.Namespace(baseurl=None))
    cli._import_base_url()
    hook_daemon.resolve_base_url(verify=True)

    assert len(seen) == 3
    assert all(call["allow_missing"] is not True for call in seen), "no path may opt into a missing record"
