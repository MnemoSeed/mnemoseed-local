"""MCP gateway skeleton (A3 T3, design/01 §4.5): newline-delimited JSON-RPC
2.0 handshake, tools/list + tools/call against a stubbed daemon client,
structured isError on daemon failures, and garbage-line robustness — the
stdin loop never dies.
"""

from __future__ import annotations

import io
import json
import sys
import threading
from typing import Any

import pytest

from mnemoseed_local import __version__, cli
from mnemoseed_local.mcp_gateway import server
from mnemoseed_local.rest_client import DaemonRestError, DaemonUnavailableError


class StubClient:
    """DaemonClient double: records posts, returns a canned payload or raises.
    Handshake beacons are recorded separately so tool-call assertions stay
    readable."""

    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.profile_id = "default"
        self.payload = payload if payload is not None else {"ok": True}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.handshakes: list[tuple[str, dict[str, Any] | None]] = []

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        (self.handshakes if path == "/mcp/handshake" else self.calls).append((path, body))
        if self.error is not None:
            raise self.error
        return self.payload


def _request(msg_id: int, method: str, params: dict[str, Any] | None = None) -> str:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def _notification(method: str, params: dict[str, Any] | None = None) -> str:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def run_gateway(lines: list[str], client: StubClient) -> tuple[int, list[dict[str, Any]]]:
    """Feed ``lines`` through serve() over StringIO; return (exit code, decoded responses)."""
    stdin = io.StringIO("".join(line + "\n" for line in lines))
    stdout = io.StringIO()
    code = server.serve(stdin=stdin, stdout=stdout, client=client)  # type: ignore[arg-type]
    return code, [json.loads(raw) for raw in stdout.getvalue().splitlines() if raw.strip()]


# ---------------------------------------------------------------- handshake


def test_initialize_result_shape() -> None:
    _, responses = run_gateway(
        [
            _request(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "opencode", "version": "0"},
                },
            )
        ],
        StubClient(),
    )
    assert len(responses) == 1
    response = responses[0]
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    result = response["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "mnemoseed-local"
    assert result["serverInfo"]["version"] == __version__
    assert "tools" in result["capabilities"]


def test_initialize_accepts_any_client_protocol_version_and_reports_ours() -> None:
    _, responses = run_gateway(
        [_request(1, "initialize", {"protocolVersion": "9999-01-01", "capabilities": {}})],
        StubClient(),
    )
    assert responses[0]["result"]["protocolVersion"] == server.PROTOCOL_VERSION


def test_initialized_notification_emits_nothing() -> None:
    _, responses = run_gateway(
        [_request(1, "initialize"), _notification("notifications/initialized")],
        StubClient(),
    )
    assert len(responses) == 1
    assert responses[0]["id"] == 1


def test_ping_returns_empty_result() -> None:
    _, responses = run_gateway([_request(9, "ping")], StubClient())
    assert responses == [{"jsonrpc": "2.0", "id": 9, "result": {}}]


def test_tools_list_has_exactly_the_five_tools_with_valid_schemas() -> None:
    _, responses = run_gateway([_request(2, "tools/list")], StubClient())
    tools = responses[0]["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "recall",
        "remember",
        "dream_once",
        "recent_sessions",
        "session_windows",
    }
    for tool in tools:
        assert tool["description"]
        schema = tool["inputSchema"]
        assert isinstance(schema, dict)
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
    recall = next(tool for tool in tools if tool["name"] == "recall")
    assert recall["inputSchema"]["required"] == ["query"]
    assert recall["inputSchema"]["properties"]["query"]["type"] == "string"
    assert recall["inputSchema"]["properties"]["top_k"]["type"] == "integer"
    remember = next(tool for tool in tools if tool["name"] == "remember")
    assert remember["inputSchema"]["required"] == ["text"]
    # B2: the time-ordered resume surface takes no required arguments
    recent = next(tool for tool in tools if tool["name"] == "recent_sessions")
    assert "required" not in recent["inputSchema"]
    assert recent["inputSchema"]["properties"]["n_sessions"]["type"] == "integer"
    assert recent["inputSchema"]["properties"]["n_per_session"]["type"] == "integer"
    # the session time-window surface takes no required arguments
    windows = next(tool for tool in tools if tool["name"] == "session_windows")
    assert "required" not in windows["inputSchema"]
    assert windows["inputSchema"]["properties"]["n_sessions"]["type"] == "integer"


# ---------------------------------------------------------------- tools/call


def test_call_recall_passes_arguments_and_returns_payload_json() -> None:
    payload = {"memory": {"entries": [{"kind": "fact", "text": "x", "score": 0.9}], "coverage": {}}}
    client = StubClient(payload=payload)
    _, responses = run_gateway(
        [_request(3, "tools/call", {"name": "recall", "arguments": {"query": "x", "top_k": 3}})],
        client,
    )
    result = responses[0]["result"]
    assert result["isError"] is False
    content = result["content"]
    assert content[0]["type"] == "text"
    assert json.loads(content[0]["text"]) == payload
    assert client.calls == [("/memory/recall", {"profile_id": "default", "query": "x", "top_k": 3})]


def test_call_recall_omits_top_k_when_absent() -> None:
    client = StubClient(payload={"memory": {"entries": []}})
    run_gateway(
        [_request(3, "tools/call", {"name": "recall", "arguments": {"query": "x"}})],
        client,
    )
    assert client.calls == [("/memory/recall", {"profile_id": "default", "query": "x"})]


def test_call_remember_positive() -> None:
    payload = {"outcome": "stored", "chunk_id": "c-1"}
    client = StubClient(payload=payload)
    _, responses = run_gateway(
        [_request(4, "tools/call", {"name": "remember", "arguments": {"text": "prefers uv"}})],
        client,
    )
    assert responses[0]["result"]["isError"] is False
    assert json.loads(responses[0]["result"]["content"][0]["text"]) == payload
    assert client.calls == [("/memory/remember", {"profile_id": "default", "text": "prefers uv"})]


def test_call_dream_once_positive() -> None:
    payload = {"launched": True, "state": "running"}
    client = StubClient(payload=payload)
    _, responses = run_gateway(
        [_request(5, "tools/call", {"name": "dream_once", "arguments": {}})],
        client,
    )
    assert responses[0]["result"]["isError"] is False
    assert json.loads(responses[0]["result"]["content"][0]["text"]) == payload
    assert client.calls == [("/memory/dream_once", {"profile_id": "default"})]


def test_call_dream_once_without_arguments_key() -> None:
    client = StubClient(payload={"launched": False, "state": "idle"})
    code, responses = run_gateway(
        [_request(5, "tools/call", {"name": "dream_once"})],
        client,
    )
    assert code == 0
    assert responses[0]["result"]["isError"] is False
    assert client.calls == [("/memory/dream_once", {"profile_id": "default"})]


def test_call_recent_sessions_maps_arguments_to_the_daemon_endpoint() -> None:
    """B2: recent_sessions proxies to POST /session/recent with the wire key
    names (n_sessions -> sessions, n_per_session -> per_session)."""
    payload = {"profile_id": "default", "sessions": [{"session_id": "s1", "latest_at": 2.0, "chunks": []}]}
    client = StubClient(payload=payload)
    _, responses = run_gateway(
        [
            _request(
                8,
                "tools/call",
                {"name": "recent_sessions", "arguments": {"n_sessions": 3, "n_per_session": 5}},
            )
        ],
        client,
    )
    assert responses[0]["result"]["isError"] is False
    assert json.loads(responses[0]["result"]["content"][0]["text"]) == payload
    assert client.calls == [("/session/recent", {"profile_id": "default", "sessions": 3, "per_session": 5})]


def test_call_recent_sessions_without_arguments_sends_profile_only() -> None:
    client = StubClient(payload={"profile_id": "default", "sessions": []})
    _, responses = run_gateway(
        [_request(8, "tools/call", {"name": "recent_sessions"})],
        client,
    )
    assert responses[0]["result"]["isError"] is False
    assert client.calls == [("/session/recent", {"profile_id": "default"})]


def test_call_session_windows_maps_arguments_to_the_daemon_endpoint() -> None:
    """session_windows proxies to POST /session/windows with the wire key
    name (n_sessions -> sessions) and passes the payload through untouched."""
    payload = {
        "profile_id": "default",
        "sessions": [
            {
                "session_id": "s1",
                "window": {"first": "2026-08-20T00:00:00Z", "latest": "2026-08-20T00:01:00Z"},
                "chunk_count": 2,
                "active": True,
                "window_truncated": False,
            }
        ],
    }
    client = StubClient(payload=payload)
    _, responses = run_gateway(
        [
            _request(
                8,
                "tools/call",
                {"name": "session_windows", "arguments": {"n_sessions": 7}},
            )
        ],
        client,
    )
    assert responses[0]["result"]["isError"] is False
    text = responses[0]["result"]["content"][0]["text"]
    assert text == json.dumps(payload, ensure_ascii=False, default=str)
    assert json.loads(text) == payload
    assert client.calls == [("/session/windows", {"profile_id": "default", "sessions": 7})]


def test_call_session_windows_without_arguments_sends_profile_only() -> None:
    client = StubClient(payload={"profile_id": "default", "sessions": []})
    _, responses = run_gateway(
        [_request(8, "tools/call", {"name": "session_windows"})],
        client,
    )
    assert responses[0]["result"]["isError"] is False
    assert json.loads(responses[0]["result"]["content"][0]["text"]) == {
        "profile_id": "default",
        "sessions": [],
    }
    assert client.calls == [("/session/windows", {"profile_id": "default"})]


def test_call_session_windows_daemon_unreachable_is_structured_error() -> None:
    client = StubClient(error=DaemonUnavailableError("cannot reach http://localhost:7788"))
    _, responses = run_gateway(
        [_request(6, "tools/call", {"name": "session_windows", "arguments": {}}), _request(7, "ping")],
        client,
    )
    assert len(responses) == 2
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "cannot reach" in result["content"][0]["text"]
    assert responses[1]["id"] == 7


def test_unknown_tool_is_structured_error_and_loop_survives() -> None:
    _, responses = run_gateway(
        [_request(6, "tools/call", {"name": "bogus", "arguments": {}}), _request(7, "ping")],
        StubClient(),
    )
    assert len(responses) == 2
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "unknown tool" in result["content"][0]["text"]
    assert responses[1]["result"] == {}


def test_daemon_unreachable_is_structured_error_and_loop_survives() -> None:
    client = StubClient(error=DaemonUnavailableError("cannot reach http://localhost:7788"))
    _, responses = run_gateway(
        [_request(6, "tools/call", {"name": "recall", "arguments": {"query": "x"}}), _request(7, "ping")],
        client,
    )
    assert len(responses) == 2
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "cannot reach" in result["content"][0]["text"]
    assert responses[1]["id"] == 7


def test_daemon_rest_error_is_structured_error() -> None:
    client = StubClient(error=DaemonRestError(422, "query too long"))
    _, responses = run_gateway(
        [_request(6, "tools/call", {"name": "remember", "arguments": {"text": "x"}})],
        client,
    )
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "422" in result["content"][0]["text"]


# ---------------------------------------------------------------- robustness


def test_garbage_line_without_id_is_dropped_and_loop_survives() -> None:
    code, responses = run_gateway(
        ["this is not json", _request(8, "ping")],
        StubClient(),
    )
    assert code == 0
    assert responses == [{"jsonrpc": "2.0", "id": 8, "result": {}}]


def test_non_dict_json_line_is_dropped_without_crash() -> None:
    code, responses = run_gateway(["[1, 2, 3]", _request(8, "ping")], StubClient())
    assert code == 0
    assert responses == [{"jsonrpc": "2.0", "id": 8, "result": {}}]


def test_garbage_line_with_salvageable_id_gets_parse_error() -> None:
    _, responses = run_gateway(
        ['{"jsonrpc": "2.0", "id": 7, "method": '],
        StubClient(),
    )
    assert len(responses) == 1
    response = responses[0]
    assert response["id"] == 7
    assert response["error"]["code"] == -32700
    assert "result" not in response


def test_unknown_method_with_id_gets_method_not_found() -> None:
    _, responses = run_gateway([_request(8, "resources/list")], StubClient())
    assert responses[0]["error"]["code"] == -32601
    assert responses[0]["id"] == 8


def test_unknown_method_without_id_is_dropped_and_loop_survives() -> None:
    code, responses = run_gateway(
        [_notification("resources/list"), _request(8, "ping")],
        StubClient(),
    )
    assert code == 0
    assert responses == [{"jsonrpc": "2.0", "id": 8, "result": {}}]


def test_notifications_cancelled_is_ignored() -> None:
    code, responses = run_gateway(
        [
            _notification("notifications/cancelled", {"requestId": 1, "reason": "user"}),
            _request(8, "ping"),
        ],
        StubClient(),
    )
    assert code == 0
    assert responses == [{"jsonrpc": "2.0", "id": 8, "result": {}}]


def test_empty_lines_are_skipped() -> None:
    code, responses = run_gateway(["", "   ", _request(8, "ping")], StubClient())
    assert code == 0
    assert responses == [{"jsonrpc": "2.0", "id": 8, "result": {}}]


def test_eof_exits_cleanly() -> None:
    code, responses = run_gateway([], StubClient())
    assert code == 0
    assert responses == []


# ---------------------------------------------------------------- handshake beacon (B2.12)


def test_serve_beacons_the_daemon_once_on_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway announces itself to the daemon exactly once when it starts
    serving a client — the observability signal doctor needs to tell a
    registered-but-never-connected MCP server from a working one."""
    seen: list[StubClient] = []
    beacons = threading.Event()

    def fake_beacon(client: StubClient) -> None:
        seen.append(client)
        beacons.set()

    monkeypatch.setattr(server.daemon_state, "is_disabled", lambda: False)
    monkeypatch.setattr(server, "send_handshake_beacon", fake_beacon)
    client = StubClient()
    code, responses = run_gateway([_request(1, "ping")], client)
    assert code == 0
    assert responses[0]["id"] == 1
    assert beacons.wait(5), "serve() never fired the handshake beacon"
    assert seen == [client]


def test_beacon_failure_never_breaks_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire-and-forget discipline: an exploding beacon must not disturb the
    stdin loop — the MCP surface keeps answering."""

    def exploding_beacon(client: StubClient) -> None:
        raise RuntimeError("beacon channel down")

    monkeypatch.setattr(server, "send_handshake_beacon", exploding_beacon)
    code, responses = run_gateway([_request(7, "ping")], StubClient())
    assert code == 0
    assert responses == [{"jsonrpc": "2.0", "id": 7, "result": {}}]


def test_beacon_skipped_when_daemon_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user-disabled service gets no loopback traffic from the gateway."""
    fired = threading.Event()

    def fake_beacon(client: StubClient) -> None:
        fired.set()

    monkeypatch.setattr(server.daemon_state, "is_disabled", lambda: True)
    monkeypatch.setattr(server, "send_handshake_beacon", fake_beacon)
    code, responses = run_gateway([_request(3, "ping")], StubClient())
    assert code == 0
    assert responses[0]["id"] == 3
    assert not fired.wait(0.5), "the beacon must not fire while the service is disabled"


def test_send_handshake_beacon_swallows_an_unreachable_daemon() -> None:
    """A dead daemon must be swallowed quietly — the beacon is best-effort."""
    from mnemoseed_local.rest_client import DaemonClient

    server.send_handshake_beacon(DaemonClient(base_url="http://127.0.0.1:1", timeout=1.0))


# ---------------------------------------------------------------- client seam


def test_build_client_marks_actor_mcp() -> None:
    client = server.build_client()
    assert client.actor == "mcp"
    assert client.profile_id


# ---------------------------------------------------------------- CLI wiring


def test_cli_mcp_verb_runs_the_stdio_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    client = StubClient(payload={"memory": {"entries": []}})
    monkeypatch.setattr(server, "build_client", lambda args=None: client)
    stdin = io.StringIO(_request(1, "initialize") + "\n")
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    code = cli.main(["mcp"])
    assert code == 0
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(responses) == 1
    assert responses[0]["result"]["serverInfo"]["name"] == "mnemoseed-local"


# ---------------------------------------------------------------- stdio lane encoding (live finding)


def test_serve_forces_utf8_and_untranslated_newlines_on_stdio_lanes() -> None:
    """Live finding (2026-08-18 go-live smoke): on a host whose stdio locale is
    not UTF-8 (Windows cp936), the gateway wrote responses in the host
    codepage — any non-ASCII payload (Chinese memory text, em-dash in a
    description) arrived MOJIBAKE'd, and the tools/list handshake itself broke
    the client's UTF-8 decode. The gateway must force UTF-8 + untranslated \\n
    on BOTH lanes regardless of the host locale."""
    raw_in = io.BytesIO((_request(1, "tools/list") + "\n").encode("utf-8"))
    raw_out = io.BytesIO()
    # stand-ins for host-locale pipe streams (cp936: the zh-CN Windows trap)
    stdin = io.TextIOWrapper(raw_in, encoding="cp936")
    stdout = io.TextIOWrapper(raw_out, encoding="cp936", newline="")
    code = server.serve(stdin=stdin, stdout=stdout, client=StubClient())
    assert code == 0
    stdout.flush()
    wire = raw_out.getvalue()
    assert b"\r\n" not in wire  # no CRLF translation on the wire
    text = wire.decode("utf-8")  # the whole frame decodes as UTF-8
    assert "—" in text  # the B2 em-dash survives (mojibake would have died on decode)
