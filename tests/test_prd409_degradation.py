"""D5 retry matrix and clear CLI/MCP degradation when the daemon is down."""

from __future__ import annotations

import pytest

from mnemoseed_local import cli
from mnemoseed_local.mcp_gateway import reliable_client
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
