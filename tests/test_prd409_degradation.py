"""D5 retry matrix and clear CLI/MCP degradation when the daemon is down."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from mnemoseed_local import cli
from mnemoseed_local.mcp_gateway import reliable_client, server
from mnemoseed_local.rest_client import DaemonUnavailableError


def test_cli_down_error_is_actionable_not_a_bare_refusal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli._client_error(
        DaemonUnavailableError("cannot reach http://hook.test: [Errno 111] Connection refused")
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "mnemoseed-local up" in err, "the CLI must tell the owner how to start the daemon"
    assert "not running" in err.lower() or "disabled" in err.lower()


def test_mcp_down_hint_contract_is_unchanged() -> None:
    assert "{base_url}" in reliable_client.DAEMON_DOWN_HINT
    assert "mnemoseed-local up" in reliable_client.DAEMON_DOWN_HINT
    assert reliable_client.RETRY_TIMEOUT_SECONDS == 1.5


class _DownClient:
    profile_id = "default"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        del path, body
        raise self.error


def test_daemon_down_is_entry_unavailable_not_capability_absence() -> None:
    client = _DownClient(DaemonUnavailableError("cannot reach http://127.0.0.1:1"))
    result = server.call_tool(client, "recall", {"query": "x"})
    failure = result["entry_failure"]
    assert result["isError"] is True
    assert failure["kind"] == "entry_unavailable"
    assert failure["kind"] not in {"absent", "capability_absent", "index_unavailable"}
    assert failure["alternate"] == "stdio gateway: mnemoseed-local mcp"
    assert "cannot reach" in result["content"][0]["text"]
    datetime.fromisoformat(failure["observed_at"])
