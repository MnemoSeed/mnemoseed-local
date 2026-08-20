"""GatewayClient retry + honest-error behavior (PRD-B2.3 D5/D6).

S2 covers the ``reliable_client.GatewayClient`` wrapper: at most one fast
retry that fires ONLY when the first ``DaemonUnavailableError`` is caused by
an ``httpx.ConnectError`` (loopback refused = daemon-restart window), plus
the honest message shapes for refused-after-retry and timeout. Rest errors,
timeout causes and cause-less unavailability never retry.
"""

from __future__ import annotations

import io
import json
from typing import Any

import httpx

from mnemoseed_local.mcp_gateway import server
from mnemoseed_local.mcp_gateway.reliable_client import GatewayClient
from mnemoseed_local.rest_client import DaemonRestError, DaemonUnavailableError


class ScriptedClient:
    """DaemonClient double: replays a script of ``("ok", payload)`` /
    ``("raise", exception)`` steps and records every ``post``."""

    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.profile_id = "default"
        self.base_url = "http://localhost:7788"
        self.script = list(script)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, body))
        step = self.script.pop(0)
        kind, value = step
        if kind == "raise":
            raise value
        return value


def _refused_error() -> DaemonUnavailableError:
    """A DaemonUnavailableError whose __cause__ is an httpx.ConnectError."""
    try:
        raise DaemonUnavailableError("cannot reach http://localhost:7788") from httpx.ConnectError(
            "connection refused"
        )
    except DaemonUnavailableError as exc:
        return exc


def _timeout_error() -> DaemonUnavailableError:
    """A DaemonUnavailableError whose __cause__ is an httpx timeout."""
    try:
        raise DaemonUnavailableError("cannot reach http://localhost:7788") from httpx.ReadTimeout("timed out")
    except DaemonUnavailableError as exc:
        return exc


def _request(msg_id: int, method: str, params: dict[str, Any] | None = None) -> str:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def run_gateway(lines: list[str], client: ScriptedClient) -> tuple[int, list[dict[str, Any]]]:
    stdin = io.StringIO("".join(line + "\n" for line in lines))
    stdout = io.StringIO()
    code = server.serve(stdin=stdin, stdout=stdout, client=client)  # type: ignore[arg-type]
    return code, [json.loads(raw) for raw in stdout.getvalue().splitlines() if raw.strip()]


def _call(name: str, arguments: dict[str, Any]) -> str:
    return _request(1, "tools/call", {"name": name, "arguments": arguments})


# ---------------------------------------------------------------- retry rule


def test_refused_then_success_retries_once_with_identical_bodies() -> None:
    payload = {"memory": {"entries": []}}
    client = ScriptedClient([("raise", _refused_error()), ("ok", payload)])
    _, responses = run_gateway([_call("recall", {"query": "x"})], client)
    result = responses[0]["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == payload
    assert len(client.calls) == 2
    assert client.calls[0] == client.calls[1] == ("/memory/recall", {"profile_id": "default", "query": "x"})


def test_refused_twice_reports_down_hint_and_retries_exactly_once() -> None:
    client = ScriptedClient([("raise", _refused_error()), ("raise", _refused_error())])
    _, responses = run_gateway([_call("recall", {"query": "x"})], client)
    result = responses[0]["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "cannot reach" in text
    assert "mnemoseed-local up" in text
    assert len(client.calls) == 2


def test_timeout_never_retries_and_reports_timeout_hint() -> None:
    client = ScriptedClient([("raise", _timeout_error())])
    _, responses = run_gateway([_call("recall", {"query": "x"})], client)
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "timed out" in result["content"][0]["text"]
    assert len(client.calls) == 1


def test_rest_error_never_retries_and_passthrough_keeps_422() -> None:
    client = ScriptedClient([("raise", DaemonRestError(422, "query too long"))])
    _, responses = run_gateway([_call("remember", {"text": "x"})], client)
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "422" in result["content"][0]["text"]
    assert len(client.calls) == 1


def test_causeless_unavailable_never_retries_and_passthrough() -> None:
    client = ScriptedClient([("raise", DaemonUnavailableError("cannot reach http://localhost:7788"))])
    _, responses = run_gateway([_call("recall", {"query": "x"})], client)
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "cannot reach" in result["content"][0]["text"]
    assert len(client.calls) == 1


def test_success_path_issues_exactly_one_post() -> None:
    payload = {"outcome": "stored", "chunk_id": "c-1"}
    client = ScriptedClient([("ok", payload)])
    _, responses = run_gateway([_call("remember", {"text": "prefers uv"})], client)
    result = responses[0]["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == payload
    assert client.calls == [("/memory/remember", {"profile_id": "default", "text": "prefers uv"})]


def test_refused_retry_preserves_exact_request_bodies_per_tool() -> None:
    payload = {"memory": {"entries": []}}
    remember = ScriptedClient([("raise", _refused_error()), ("ok", {"outcome": "stored"})])
    _, _ = run_gateway([_call("remember", {"text": "prefers uv"})], remember)
    assert len(remember.calls) == 2
    assert (
        remember.calls[0]
        == remember.calls[1]
        == (
            "/memory/remember",
            {"profile_id": "default", "text": "prefers uv"},
        )
    )
    recall = ScriptedClient([("raise", _refused_error()), ("ok", payload)])
    _, _ = run_gateway([_call("recall", {"query": "x", "top_k": 3})], recall)
    assert len(recall.calls) == 2
    assert (
        recall.calls[0]
        == recall.calls[1]
        == (
            "/memory/recall",
            {"profile_id": "default", "query": "x", "top_k": 3},
        )
    )


def test_ping_and_tools_list_never_touch_the_client() -> None:
    client = ScriptedClient([])
    _, responses = run_gateway([_request(9, "ping"), _request(2, "tools/list")], client)
    assert responses[0] == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == {
        "recall",
        "remember",
        "dream_once",
        "recent_sessions",
        "session_windows",
    }
    assert client.calls == []


def test_wrap_is_idempotent() -> None:
    client = ScriptedClient([])
    wrapped = GatewayClient.wrap(client)
    assert isinstance(wrapped, GatewayClient)
    assert GatewayClient.wrap(wrapped) is wrapped
