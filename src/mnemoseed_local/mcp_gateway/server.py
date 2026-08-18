"""Minimal MCP stdio gateway (A3 T3, design/01 §4.5).

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout — one message
per line, no Content-Length framing. Stdout carries protocol traffic only;
any diagnostics go to stderr via ``logging``. Zero new dependencies.

Protocol surface (MCP ``2024-11-05`` shape):

- ``initialize`` -> protocolVersion + capabilities + serverInfo (any client
  protocolVersion is accepted; the gateway always reports its own);
- ``notifications/initialized`` and every other ``notifications/*`` ->
  ignored, never answered;
- ``tools/list`` -> the daemon tools (recall / remember / dream_once /
  recent_sessions — the last one is the B2 time-ordered resume surface);
- ``tools/call`` -> proxied to the daemon REST (actor ``mcp``); daemon
  failures and unknown tools come back as structured ``isError`` results so
  the stdin loop never dies;
- ``ping`` -> empty result;
- unknown method with an id -> error -32601; unparseable line -> error
  -32700 when an id can be salvaged from the raw bytes, else dropped.

A request line carrying an id always gets exactly one response.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import replace
from typing import Any, TextIO

from mnemoseed_local import __version__
from mnemoseed_local.rest_client import (
    DaemonClient,
    DaemonRestError,
    DaemonUnavailableError,
    resolve_client,
)

logger = logging.getLogger(__name__)

#: MCP protocol revision this gateway speaks.
PROTOCOL_VERSION = "2024-11-05"

#: JSON-RPC error codes used on the wire.
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601

#: Audit actor the daemon attributes this surface to.
ACTOR = "mcp"

#: The tool surface: JSON Schema inputSchemas in MCP tools/list shape.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "recall",
        "description": "Recall memories from the mnemoseed-local daemon "
        "(hybrid retrieval over the configured profile).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "the recall query text"},
                "top_k": {"type": "integer", "description": "maximum number of entries to return"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remember",
        "description": "Pin a fact into mnemoseed-local memory (stored verbatim).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the fact text to remember"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dream_once",
        "description": "Launch exactly one dream consolidation cycle; returns the launched/state payload.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "recent_sessions",
        "description": "Fetch the most recent sessions' verbatim tails from mnemoseed-local — "
        "use it to re-anchor on where the previous conversation ended "
        "(time-ordered resume, newest session group first).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n_sessions": {
                    "type": "integer",
                    "description": "how many recent session groups to return (default 2, max 5)",
                },
                "n_per_session": {
                    "type": "integer",
                    "description": "verbatim tail size per session, in chunks (default 20, max 100)",
                },
            },
            "additionalProperties": False,
        },
    },
]

#: Sentinel: no request id could be salvaged from a broken line.
_MISSING: Any = object()

_ID_TOKEN_RE = re.compile(r'"id"\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+)')


def build_client(args: Any = None) -> DaemonClient:
    """The gateway's daemon client.

    Same resolution as the CLI (``--baseurl`` / config baseurl, profile from
    ``MNEMOSEED_LOCAL_PROFILE_ID`` or ``default``) but with the ``mcp`` audit
    actor (`_VALID_ACTORS` already trusts it).
    """
    return replace(resolve_client(args), actor=ACTOR)


def call_tool(client: DaemonClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one ``tools/call`` to the daemon REST.

    Every failure mode (unknown tool, unreachable daemon, non-2xx answer)
    returns a structured ``isError`` result — nothing is ever raised, so the
    stdin loop survives.
    """
    try:
        if name == "recall":
            body: dict[str, Any] = {"profile_id": client.profile_id, "query": arguments.get("query", "")}
            if arguments.get("top_k") is not None:
                body["top_k"] = arguments["top_k"]
            payload = client.post("/memory/recall", body)
        elif name == "remember":
            payload = client.post(
                "/memory/remember",
                {"profile_id": client.profile_id, "text": arguments.get("text", "")},
            )
        elif name == "dream_once":
            payload = client.post("/memory/dream_once", {"profile_id": client.profile_id})
        elif name == "recent_sessions":
            recent_body: dict[str, Any] = {"profile_id": client.profile_id}
            if arguments.get("n_sessions") is not None:
                recent_body["sessions"] = arguments["n_sessions"]
            if arguments.get("n_per_session") is not None:
                recent_body["per_session"] = arguments["n_per_session"]
            payload = client.post("/session/recent", recent_body)
        else:
            return _error_result(f"unknown tool: {name!r}")
    except (DaemonUnavailableError, DaemonRestError) as exc:
        return _error_result(f"daemon error: {exc}")
    except Exception as exc:  # never kill the loop on a tool call
        return _error_result(f"tool {name!r} failed: {exc}")
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
        "isError": False,
    }


def _error_result(reason: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": reason}], "isError": True}


def _error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _salvage_id(line: str) -> Any:
    """Best-effort ``id`` extraction from a line that failed to parse."""
    match = _ID_TOKEN_RE.search(line)
    if match is None:
        return _MISSING
    token = match.group(1)
    if token.startswith('"'):
        try:
            return json.loads(token)
        except ValueError:
            return _MISSING
    return int(token)


def handle_message(client: DaemonClient, message: dict[str, Any]) -> dict[str, Any] | None:
    """One parsed JSON-RPC message -> its response, or None when the message
    is a notification (or otherwise not answerable)."""
    method = message.get("method")
    has_id = "id" in message
    msg_id = message.get("id")

    if isinstance(method, str) and method.startswith("notifications/"):
        return None

    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mnemoseed-local", "version": __version__},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        name = params.get("name")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        result = call_tool(client, str(name) if name is not None else "", arguments)
    else:
        if not has_id:
            return None  # unknown notification-style message: dropped, never crash
        return _error_response(msg_id, METHOD_NOT_FOUND, f"method not found: {method!r}")

    if not has_id:
        return None  # known method sent as a notification: no response owed
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def handle_line(client: DaemonClient, line: str) -> dict[str, Any] | None:
    """One raw input line -> its JSON-RPC response, or None when dropped."""
    try:
        message = json.loads(line)
    except ValueError:
        salvaged = _salvage_id(line)
        if salvaged is _MISSING:
            logger.debug("dropping unparseable line with no salvageable id")
            return None
        return _error_response(salvaged, PARSE_ERROR, "parse error: line is not valid JSON")
    if not isinstance(message, dict):
        logger.debug("dropping non-object JSON-RPC message: %r", message)
        return None
    return handle_message(client, message)


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    client: DaemonClient | None = None,
) -> int:
    """Run the gateway loop until EOF (or KeyboardInterrupt); returns 0.

    A down daemon is NOT a startup error: the handshake works regardless;
    only ``tools/call`` surfaces connectivity as structured ``isError``.
    """
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    daemon = client if client is not None else build_client()
    try:
        for raw in in_stream:
            line = raw.strip()
            if not line:
                continue
            try:
                response = handle_line(daemon, line)
            except Exception:
                logger.exception("unhandled error on an input line; dropping it")
                continue
            if response is None:
                continue
            out_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            out_stream.flush()
    except KeyboardInterrupt:
        logger.info("interrupted; shutting down")
    return 0


def main() -> int:
    """Entry hook for the CLI verb (blocking stdio loop)."""
    return serve()
