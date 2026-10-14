"""The loopback HTTP MCP transport: the /mcp contract surface.

The in-proc tests boot the real app through ``auth_support`` and pin the
request envelope — the credential gate, the per-request profile header, the
JSON-RPC shapes, and the method/path refusals. The real-daemon tests boot a
daemon child through ``tests/_test_daemon.py`` so the tools/call self-call leg
crosses a real loopback socket.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import auth_support
import httpx
import pytest
from _ports import unused_loopback_port
from _test_daemon import TEST_ACTOR, daemon_for_test

from mnemoseed_local import __version__
from mnemoseed_local.credential import read_credential_at
from mnemoseed_local.daemon.auth import AUTH_DETAIL
from mnemoseed_local.mcp_gateway.http_transport import (
    MALFORMED_MESSAGE_DETAIL,
    MCP_PATH,
    MISSING_PROFILE_DETAIL,
    PROFILE_HEADER,
)
from mnemoseed_local.mcp_gateway.server import PROTOCOL_VERSION
from mnemoseed_local.rest_client import ACTOR_HEADER, DaemonClient

TOOL_NAMES = {"recall", "remember", "supersede", "dream_once", "recent_sessions", "session_windows"}

#: The tools/call round trip must finish far below the self-call's own 30s
#: client timeout; an inline (on-loop) dispatch stalls past it.
TOOL_CALL_BOUND_S = 15.0


def _config_text(home: Path) -> str:
    return (
        f'baseurl = "http://127.0.0.1:{unused_loopback_port()}"\n'
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        '[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated config home holding a bootable embedded config."""
    (tmp_path / "config.toml").write_text(_config_text(tmp_path), encoding="utf-8")
    return tmp_path


def _rpc(msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _daemon_client(daemon: Any) -> DaemonClient:
    """A credentialed client onto the daemon child, marked as the test surface."""
    return DaemonClient(base_url=daemon.base_url, actor=TEST_ACTOR, token=read_credential_at(daemon.home))


def _post_mcp(
    daemon: Any,
    payload: dict[str, Any],
    *,
    profile: str | None = "default",
    actor: str | None = "mcp",
    timeout: float = 60.0,
) -> httpx.Response:
    """One MCP request onto the daemon child's HTTP surface."""
    headers: dict[str, str] = {"Authorization": f"Bearer {read_credential_at(daemon.home)}"}
    if profile is not None:
        headers[PROFILE_HEADER] = profile
    if actor is not None:
        headers[ACTOR_HEADER] = actor
    return httpx.post(
        f"{daemon.base_url}{MCP_PATH}",
        json=payload,
        headers=headers,
        timeout=timeout,
    )


# ---------------------------------------------------------------- the contract surface (in-proc)


def test_uncredentialed_post_is_refused_with_the_constant_detail(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        response = client.post(MCP_PATH, json=_rpc(1, "initialize"), headers={PROFILE_HEADER: "default"})

    assert response.status_code == 401
    assert response.json() == {"detail": AUTH_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"


def test_missing_or_blank_profile_header_is_a_constant_400(home: Path) -> None:
    with auth_support.boot(home) as client:
        absent = client.post(MCP_PATH, json=_rpc(1, "initialize"))
        blank = client.post(MCP_PATH, json=_rpc(2, "initialize"), headers={PROFILE_HEADER: "   "})
        declared_actor = client.post(
            MCP_PATH,
            json=_rpc(3, "initialize"),
            headers={PROFILE_HEADER: "default", ACTOR_HEADER: "cli"},
        )

    for response in (absent, blank):
        assert response.status_code == 400
        assert response.json() == {"detail": MISSING_PROFILE_DETAIL}
    assert declared_actor.status_code == 200


def test_initialize_answers_the_gateway_shape(home: Path) -> None:
    with auth_support.boot(home) as client:
        response = client.post(
            MCP_PATH,
            json=_rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
            headers={PROFILE_HEADER: "default"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    result = payload["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"] == {"name": "mnemoseed-local", "version": __version__}
    assert "tools" in result["capabilities"]


def test_tools_list_is_the_six_gateway_tools(home: Path) -> None:
    with auth_support.boot(home) as client:
        response = client.post(MCP_PATH, json=_rpc(2, "tools/list"), headers={PROFILE_HEADER: "default"})

    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == TOOL_NAMES


def test_get_and_delete_are_refused_405(home: Path) -> None:
    with auth_support.boot(home) as client:
        for method in ("get", "delete"):
            response = client.request(method, MCP_PATH, headers={PROFILE_HEADER: "default"})

        assert response.status_code == 405
        assert "POST" in response.headers["allow"]


def test_the_trailing_slash_path_is_not_the_route(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        assert client.post(MCP_PATH + "/", json=_rpc(1, "initialize")).status_code == 401
    with auth_support.boot(home) as client:
        read = client.get(MCP_PATH + "/")
        posted = client.post(MCP_PATH + "/", json=_rpc(1, "initialize"), headers={PROFILE_HEADER: "default"})

    # Admitted, then matched by no route: the console mount answers, and it
    # refuses the POST verb the MCP surface would need.
    assert read.status_code == 404
    assert posted.status_code == 405


def test_malformed_and_non_object_bodies_are_a_constant_400(home: Path) -> None:
    with auth_support.boot(home) as client:
        unparseable = client.post(MCP_PATH, content=b"{not json", headers={PROFILE_HEADER: "default"})
        non_object = client.post(MCP_PATH, json=[1, 2, 3], headers={PROFILE_HEADER: "default"})

    for response in (unparseable, non_object):
        assert response.status_code == 400
        assert response.json() == {"detail": MALFORMED_MESSAGE_DETAIL}


def test_a_notification_gets_no_jsonrpc_answer(home: Path) -> None:
    with auth_support.boot(home) as client:
        response = client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={PROFILE_HEADER: "default"},
        )

    assert response.status_code == 202
    assert response.content == b""


def test_an_unknown_method_keeps_the_jsonrpc_error_envelope(home: Path) -> None:
    with auth_support.boot(home) as client:
        response = client.post(MCP_PATH, json=_rpc(7, "resources/list"), headers={PROFILE_HEADER: "default"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32601
    assert payload["id"] == 7


def test_tools_call_against_a_down_daemon_is_a_structured_error(home: Path) -> None:
    with auth_support.boot(home) as client:
        response = client.post(
            MCP_PATH,
            json=_rpc(4, "tools/call", {"name": "recall", "arguments": {"query": "x"}}),
            headers={PROFILE_HEADER: "default"},
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "cannot reach" in result["content"][0]["text"]


# ---------------------------------------------------------------- the real-daemon surface


def test_tool_call_round_trip_on_a_real_daemon(tmp_path: Path) -> None:
    with daemon_for_test(tmp_path) as daemon:
        remembered = _post_mcp(
            daemon,
            _rpc(1, "tools/call", {"name": "remember", "arguments": {"text": "http transport probe fact"}}),
        )
        assert remembered.status_code == 200
        stored = remembered.json()["result"]
        assert stored["isError"] is False
        assert json.loads(stored["content"][0]["text"])["outcome"] == "new_chunk"

        recalled = _post_mcp(
            daemon,
            _rpc(2, "tools/call", {"name": "recall", "arguments": {"query": "http transport probe fact"}}),
        )
        assert recalled.status_code == 200
        entries = recalled.json()["result"]
        assert entries["isError"] is False
        assert "http transport probe fact" in json.dumps(json.loads(entries["content"][0]["text"]))


def test_tool_call_audits_the_gateway_actor_and_ignores_the_declared_one(tmp_path: Path) -> None:
    with daemon_for_test(tmp_path) as daemon:
        answered = _post_mcp(
            daemon,
            _rpc(1, "tools/call", {"name": "remember", "arguments": {"text": "attributed pin"}}),
            actor="hook",  # a client-declared actor the surface must never trust
        )
        assert answered.json()["result"]["isError"] is False

        client = _daemon_client(daemon)
        gateway_rows = client.get("/api/v1/audit", params={"actor": "mcp", "action": "remember"})["items"]
        declared_rows = client.get("/api/v1/audit", params={"actor": "hook", "action": "remember"})["items"]

    assert [row["actor"] for row in gateway_rows] == ["mcp"]
    assert declared_rows == []


def test_two_concurrent_clients_stay_profile_isolated(tmp_path: Path) -> None:
    with daemon_for_test(tmp_path) as daemon:
        bodies = {
            "alpha": "alpha namespace fact",
            "beta": "beta namespace fact",
        }
        answers: dict[str, httpx.Response] = {}

        def _call(profile: str) -> None:
            answers[profile] = _post_mcp(
                daemon,
                _rpc(1, "tools/call", {"name": "remember", "arguments": {"text": bodies[profile]}}),
                profile=profile,
            )

        threads = [threading.Thread(target=_call, args=(profile,)) for profile in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60.0)
        assert all(thread.is_alive() is False for thread in threads)

        for profile, response in answers.items():
            assert response.json()["result"]["isError"] is False, profile

        reads = {
            profile: _post_mcp(
                daemon,
                _rpc(2, "tools/call", {"name": "recall", "arguments": {"query": "namespace fact"}}),
                profile=profile,
            )
            for profile in bodies
        }

    for profile, response in reads.items():
        payload = json.loads(response.json()["result"]["content"][0]["text"])
        texts = json.dumps(payload)
        assert bodies[profile] in texts, profile
        other = next(value for key, value in bodies.items() if key != profile)
        assert other not in texts, f"{profile} saw the other namespace: {texts}"


def test_tool_call_completes_far_below_the_self_call_timeout(tmp_path: Path) -> None:
    with daemon_for_test(tmp_path) as daemon:
        started = time.monotonic()
        answered = _post_mcp(
            daemon,
            _rpc(1, "tools/call", {"name": "remember", "arguments": {"text": "off-loop probe"}}),
        )
        elapsed = time.monotonic() - started

    assert answered.json()["result"]["isError"] is False
    assert elapsed < TOOL_CALL_BOUND_S, (
        f"tools/call took {elapsed:.1f}s: the self-call is running on the event loop"
    )


def test_restart_keeps_the_stateless_surface(tmp_path: Path) -> None:
    fact = "survives a daemon restart"
    with daemon_for_test(tmp_path) as first:
        answered = _post_mcp(first, _rpc(1, "tools/call", {"name": "remember", "arguments": {"text": fact}}))
        assert answered.json()["result"]["isError"] is False

    with daemon_for_test(tmp_path) as second:
        assert second.port != first.port
        recalled = _post_mcp(second, _rpc(2, "tools/call", {"name": "recall", "arguments": {"query": fact}}))
        shake = _post_mcp(second, _rpc(3, "initialize"), profile="default")

    assert recalled.json()["result"]["isError"] is False
    assert fact in json.dumps(json.loads(recalled.json()["result"]["content"][0]["text"]))
    assert shake.json()["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_the_credential_canary_on_the_mcp_surface(tmp_path: Path) -> None:
    with daemon_for_test(tmp_path) as daemon:
        credential = read_credential_at(daemon.home)
        _post_mcp(daemon, _rpc(1, "initialize"))
        listed = _post_mcp(daemon, _rpc(2, "tools/list"))
        called = _post_mcp(
            daemon, _rpc(3, "tools/call", {"name": "remember", "arguments": {"text": "canary fact"}})
        )
        client = _daemon_client(daemon)
        audit_rows = json.dumps(client.get("/api/v1/audit")["items"], ensure_ascii=False)
        console_headers = {
            "Authorization": f"Bearer {credential}",
            "host": f"127.0.0.1:{daemon.port}",
        }
        console_document = httpx.get(f"{daemon.base_url}/", headers=console_headers, timeout=30.0)
        console_script = httpx.get(f"{daemon.base_url}/app.js", headers=console_headers, timeout=30.0)
        bodies = "".join(response.text for response in (listed, called))

    assert console_document.status_code == 200 and console_script.status_code == 200
    assert credential not in (daemon.home / "daemon.log").read_text(encoding="utf-8")
    assert credential not in audit_rows
    assert credential not in bodies
    assert credential not in console_document.text
    assert credential not in console_script.text
    assert "set-cookie" not in called.headers


def test_one_tool_call_counts_two_authenticated_mcp_requests(tmp_path: Path) -> None:
    with daemon_for_test(tmp_path) as daemon:
        _post_mcp(daemon, _rpc(1, "tools/call", {"name": "remember", "arguments": {"text": "counted pin"}}))
        activity = _daemon_client(daemon).get("/api/v1/observability")

    assert activity["authenticated_actor_counts"]["mcp"] == 2
