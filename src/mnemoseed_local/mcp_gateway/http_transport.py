"""The loopback HTTP MCP transport: one exact path on the daemon app.

The daemon already speaks MCP over stdio (``mcp_gateway/server.py``); this
module adds the same JSON-RPC 2.0 surface over HTTP so a client connects to a
URL instead of spawning a gateway process. The envelope calls the unchanged
``handle_message`` / ``call_tool`` / ``TOOLS``, so the tool set, the
``isError`` shapes and the audit actor stay byte-identical to the stdio lane.

Contract:

- one exact path ``/mcp``; ``POST`` carries one JSON-RPC object, every other
  method is refused 405, and a trailing slash is not this route (the console
  mount answers it);
- per-request identity: ``Authorization: Bearer`` is the existing credential
  gate (default deny, unchanged), ``X-MnemoSeed-Profile`` names the request's
  namespace and is refused as a constant 400 when missing or blank, and the
  self-declared actor header is never trusted for the daemon self-call, which
  is pinned to the gateway's own actor;
- stateless: no session id and no server-side session table, so a restart
  loses nothing a client has to re-establish;
- every request builds its own daemon client, so concurrent clients with
  different profiles stay isolated;
- ``tools/call`` runs the loopback self-call off the event loop
  (``anyio.to_thread.run_sync`` under a per-app limiter): an inline dispatch
  would block the loop the self-call needs and deadlock.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio
from fastapi import APIRouter, Request, status
from starlette.responses import JSONResponse, Response

from mnemoseed_local.credential import read_credential_or_none
from mnemoseed_local.mcp_gateway.reliable_client import GatewayClient
from mnemoseed_local.mcp_gateway.server import ACTOR, handle_message
from mnemoseed_local.rest_client import DaemonClient

logger = logging.getLogger(__name__)

#: The one path this transport answers; registered before the console mount.
MCP_PATH = "/mcp"

#: The per-request namespace header, interpreted only on this path.
PROFILE_HEADER = "X-MnemoSeed-Profile"

#: The method whose answer needs the daemon loop (the self-call leg).
TOOL_CALL_METHOD = "tools/call"

#: Constant refusal details: a response body never echoes request material.
MISSING_PROFILE_DETAIL = "X-MnemoSeed-Profile header required"
MALFORMED_MESSAGE_DETAIL = "request body must be one JSON-RPC object"
METHOD_NOT_ALLOWED_DETAIL = "POST only: the HTTP MCP surface speaks JSON-RPC over POST"

#: Concurrent tool calls one app admits before a request queues. Anyio's
#: default thread limiter is 40, so four keeps the self-call lane bounded
#: without starving it.
MAX_CONCURRENT_TOOL_CALLS = 4

#: The per-app state key the tool-call limiter rides under.
_LIMITER_STATE_KEY = "mcp_call_limiter"

router = APIRouter()


@router.api_route(MCP_PATH, methods=["POST"])
async def post_mcp(request: Request) -> Response:
    """One JSON-RPC message in, its answer out (202 for a notification)."""
    profile_id = _requested_profile(request)
    if profile_id is None:
        return _refusal(MISSING_PROFILE_DETAIL)
    message = _jsonrpc_message(await request.body())
    if message is None:
        return _refusal(MALFORMED_MESSAGE_DETAIL)
    client = _gateway_client(request.app, profile_id)
    result = await _answered(client, message, request.app)
    if result is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return JSONResponse(content=result)


@router.api_route(MCP_PATH, methods=["GET", "DELETE", "PUT", "PATCH", "OPTIONS"])
async def refuse_non_post() -> Response:
    return _refusal(
        METHOD_NOT_ALLOWED_DETAIL,
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        headers={"Allow": "POST"},
    )


def _requested_profile(request: Request) -> str | None:
    """The request's namespace, or None when the header is missing or blank."""
    value = request.headers.get(PROFILE_HEADER)
    if value is None:
        return None
    return value.strip() or None


def _jsonrpc_message(body: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(body)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


async def _answered(client: GatewayClient, message: dict[str, Any], app: Any) -> dict[str, Any] | None:
    """The message's answer, with the daemon-touching methods off the loop.

    Only ``tools/call`` reaches the daemon, so only it pays the thread hop;
    the limiter is created once per app (the event loop is single-threaded,
    so the check-and-set cannot interleave).
    """
    if message.get("method") != TOOL_CALL_METHOD:
        return handle_message(client, message)
    limiter = getattr(app.state, _LIMITER_STATE_KEY, None)
    if limiter is None:
        limiter = anyio.CapacityLimiter(MAX_CONCURRENT_TOOL_CALLS)
        setattr(app.state, _LIMITER_STATE_KEY, limiter)
    async with limiter:
        return await anyio.to_thread.run_sync(handle_message, client, message)


def _gateway_client(app: Any, profile_id: str) -> GatewayClient:
    """One daemon client per request: the request's namespace, the pinned
    gateway actor, and the credential this config home issued."""
    return GatewayClient.wrap(
        DaemonClient(
            base_url=app.state.config.baseurl,
            profile_id=profile_id,
            actor=ACTOR,
            token=read_credential_or_none(),
        )
    )


def _refusal(
    detail: str,
    *,
    status_code: int = status.HTTP_400_BAD_REQUEST,
    headers: dict[str, str] | None = None,
) -> Response:
    return JSONResponse(content={"detail": detail}, status_code=status_code, headers=headers)
