"""The ``capture`` verb: the hook-less write arm of the memory protocol.

A chat surface has no capture hook, so the assistant itself must be able to write
a durable decision the user stated. ``capture`` shares the write service with
``remember`` and differs only in its stamp: a capture (source ``mcp-chat``, no pin
marker, created by the calling actor) instead of the user's own explicit pin.
The REST surface keeps its pre-existing default: a request without the optional
``capture`` field is the pin write it always was.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from _test_daemon import TEST_ACTOR, daemon_for_test

from mnemoseed_local.credential import read_credential_at
from mnemoseed_local.daemon.memory import CAPTURED_CHAT_SOURCE, RememberRequest
from mnemoseed_local.mcp_gateway.http_transport import MCP_PATH, PROFILE_HEADER
from mnemoseed_local.mcp_gateway.server import TOOLS, call_tool
from mnemoseed_local.rest_client import DaemonClient
from mnemoseed_local.schema.stamp import EXPLICIT_PIN_SOURCE

CAPTURE_TOOL = next(tool for tool in TOOLS if tool["name"] == "capture")


class StubClient:
    """DaemonClient double: records posts and returns a canned payload."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.profile_id = "default"
        self.payload = payload if payload is not None else {"ok": True}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, body))
        return self.payload


def _rpc(msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _post_mcp(daemon: Any, payload: dict[str, Any]) -> Any:
    """One MCP request onto a real daemon's HTTP surface."""
    import httpx

    return httpx.post(
        f"{daemon.base_url}{MCP_PATH}",
        json=payload,
        headers={
            "Authorization": f"Bearer {read_credential_at(daemon.home)}",
            PROFILE_HEADER: "default",
        },
        timeout=60.0,
    )


def _exported_chunks(daemon: Any) -> dict[str, dict[str, Any]]:
    """The profile's stored chunks keyed by their verbatim text."""
    client = DaemonClient(base_url=daemon.base_url, actor=TEST_ACTOR, token=read_credential_at(daemon.home))
    payload = client.post("/memory/export", {"profile_id": "default", "limit": 200})
    return {str(chunk["text"]): chunk for chunk in payload["chunks"]}


# ---------------------------------------------------------------- the tool surface


def test_capture_is_a_separate_verb_taking_one_required_text() -> None:
    """The verb itself is the trigger the instruction layer names, and its own
    description carries the no-speculation rule."""
    schema = CAPTURE_TOOL["inputSchema"]
    assert schema["required"] == ["text"]
    assert schema["properties"]["text"]["type"] == "string"
    assert schema["additionalProperties"] is False
    description = CAPTURE_TOOL["description"]
    assert "verbatim" in description
    assert "never" in description.lower()


def test_capture_dispatches_to_the_remember_endpoint_with_the_capture_flag() -> None:
    """Missing this dispatch leg is the failure the end-to-end pin exists for:
    the tool would be listed and answer ``unknown tool`` forever."""
    payload = {"outcome": "new_chunk", "chunk_id": "c-9"}
    client = StubClient(payload=payload)
    result = call_tool(client, "capture", {"text": "we use uv, not pip"})
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == payload
    assert client.calls == [
        (
            "/memory/remember",
            {"profile_id": "default", "text": "we use uv, not pip", "capture": True},
        )
    ]


@pytest.mark.parametrize("text", [None, "", "   ", 17])
def test_capture_refuses_a_text_it_cannot_quote(text: Any) -> None:
    client = StubClient()
    result = call_tool(client, "capture", {"text": text})
    assert result["isError"] is True
    assert client.calls == []


# ---------------------------------------------------------------- the write contract


def test_capture_writes_a_captured_entry_end_to_end(tmp_path: Any) -> None:
    """The whole chain: MCP tool -> /memory/remember with capture -> a stored
    entry whose stamp says capture, not pin."""
    fact = "the deployment script moves to uv on this machine"
    with daemon_for_test(tmp_path) as daemon:
        answered = _post_mcp(daemon, _rpc(1, "tools/call", {"name": "capture", "arguments": {"text": fact}}))
        assert answered.status_code == 200
        result = answered.json()["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["outcome"] in {
            "new_chunk",
            "reinforced",
            "needs_reconcile",
        }
        stored = _exported_chunks(daemon)[fact]

    provenance = stored["provenance"]
    assert provenance["source"] == CAPTURED_CHAT_SOURCE
    assert provenance["asserted_by"] == "user"
    assert provenance["confidence"] == 1.0
    assert stored["model_id"] == "user"
    assert [event["action"] for event in provenance["history"]] == ["created"]
    assert provenance["history"][0]["actor"] == "mcp"


def test_remember_writes_an_explicit_pin_end_to_end(tmp_path: Any) -> None:
    """The pin arm is unchanged on the same surface: the user's own verbatim
    entry, attributed to the user, marked as a pin by its source."""
    fact = "the audit bundle ships every Friday"
    with daemon_for_test(tmp_path) as daemon:
        answered = _post_mcp(daemon, _rpc(1, "tools/call", {"name": "remember", "arguments": {"text": fact}}))
        assert answered.json()["result"]["isError"] is False
        stored = _exported_chunks(daemon)[fact]

    provenance = stored["provenance"]
    assert provenance["source"] == EXPLICIT_PIN_SOURCE
    assert provenance["history"][0]["actor"] == "user"


def test_the_captured_and_pinned_entries_stay_distinguishable(tmp_path: Any) -> None:
    """The two arms are told apart exactly by the source label the skill
    documents: one comparison, no second vocabulary."""
    pinned_fact = "we always run the gate script before pushing"
    captured_fact = "we prefer short release branches"
    with daemon_for_test(tmp_path) as daemon:
        _post_mcp(daemon, _rpc(1, "tools/call", {"name": "remember", "arguments": {"text": pinned_fact}}))
        _post_mcp(daemon, _rpc(2, "tools/call", {"name": "capture", "arguments": {"text": captured_fact}}))
        stored = _exported_chunks(daemon)

    assert stored[pinned_fact]["provenance"]["source"] == EXPLICIT_PIN_SOURCE
    assert stored[captured_fact]["provenance"]["source"] == CAPTURED_CHAT_SOURCE
    assert stored[captured_fact]["provenance"]["source"] != EXPLICIT_PIN_SOURCE


# ---------------------------------------------------------------- the pre-existing wire default


def test_the_remember_request_defaults_to_the_pin_write() -> None:
    """The optional field is additive on the wire: an absent ``capture`` is the
    pin write the endpoint has always performed."""
    request = RememberRequest.model_validate({"profile_id": "default", "text": "a fact"})
    assert request.capture is False
    assert request.model_dump() == {
        "profile_id": "default",
        "text": "a fact",
        "rules": None,
        "capture": False,
    }


def test_a_remember_request_without_capture_still_writes_a_user_pin(tmp_path: Any) -> None:
    """Regression: the REST caller that knows nothing about capture keeps its
    exact stamp and its exact response shape."""
    fact = "the gate script is scripts/gate.ps1"
    with daemon_for_test(tmp_path) as daemon:
        client = DaemonClient(
            base_url=daemon.base_url, actor=TEST_ACTOR, token=read_credential_at(daemon.home)
        )
        body = client.post("/memory/remember", {"profile_id": "default", "text": fact})
        assert set(body) == {"outcome", "chunk_id"}
        assert body["outcome"] == "new_chunk"
        stored = _exported_chunks(daemon)[fact]

    provenance = stored["provenance"]
    assert provenance["source"] == EXPLICIT_PIN_SOURCE
    assert provenance["history"][0]["actor"] == "user"
    assert stored["decay_weight"] == 1.0
