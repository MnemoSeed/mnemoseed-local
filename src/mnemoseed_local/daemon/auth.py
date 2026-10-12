"""The daemon's authentication surface: one default-deny ASGI middleware.

Three mechanical rules govern the protected surface:

1. **Default deny.** A request is refused unless its exact path is in the open
   set. Nothing is inferred from a prefix, a method, or a route pattern.
2. **Exact equality, no normalization.** The path from the ASGI scope is
   compared as-is: no ``rstrip``, no ``normpath``, no case folding. A crafted
   path such as ``/healthz/../api/v1/audit`` is therefore not in the open set
   and is refused, exactly like any other unknown path.
3. **An enumerated open set.** ``/healthz`` and ``/health`` (the liveness
   probes every consumer already dials) plus the console documents enumerated
   from the shipped static files at startup. A console file that is not
   enumerated answers 401: the console blanks visibly instead of quietly
   serving an unauthenticated surface.

The console authenticates with a same-origin cookie grant: a document response
whose Host is loopback and whose Origin is absent or same-origin carries the
credential in an ``HttpOnly; SameSite=Strict; Path=/`` cookie. A cross-site
fetch sends no cookie (SameSite=Strict) and cannot add an ``Authorization``
header without a preflight the daemon refuses, and a DNS-rebinding page has a
non-loopback Host so it never receives the cookie at all. The value never
enters the DOM.

The middleware is a plain ASGI wrapper (never ``BaseHTTPMiddleware``, whose
extra task-group hops would reorder the respond-then-run seam
``/daemon/shutdown`` depends on) and it wraps the request-logging middleware,
so a refused request produces no log line and nothing but method, path, and
status is ever written. It logs nothing itself; the one thing it does beyond
refusing is handing each admitted request to the observability counters under
its self-declared audit actor, which is what makes the since-boot connected
signal observable without a counter any unauthenticated caller could move.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders

from mnemoseed_local.credential import TokenVerifier, read_credential_or_none
from mnemoseed_local.daemon.actor import actor_from_header
from mnemoseed_local.rest_client import ACTOR_HEADER

#: Paths that answer without a credential: the liveness probes the watchdog,
#: the boot announce, the installer, the CLI, and the console dot all dial.
OPEN_PATHS = frozenset({"/healthz", "/health"})

#: The refusal detail. A constant: the body never echoes request material.
AUTH_DETAIL = "daemon credential required"

#: The console's same-origin grant cookie.
CREDENTIAL_COOKIE_NAME = "mnemoseed_daemon_credential"

#: Cookie attributes: HttpOnly (no DOM access), SameSite=Strict (no cross-site
#: send), Path=/ (the console's own relative fetches).
CREDENTIAL_COOKIE_ATTRIBUTES = "HttpOnly; SameSite=Strict; Path=/"

#: Host identifiers that count as loopback for the cookie grant.
LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})

#: The console's shipped static files (the mount reads the same directory).
CONSOLE_STATIC_DIR = Path(__file__).resolve().parent.parent / "console" / "static"


def console_documents() -> frozenset[str]:
    """The exact console document set, enumerated from the shipped files.

    The index is a document in its own right; every other file in the
    directory is one too. A new file that is not enumerated answers 401, so a
    console addition fails closed and visibly.
    """
    documents = {"/"}
    if CONSOLE_STATIC_DIR.is_dir():
        documents.update(f"/{entry.name}" for entry in CONSOLE_STATIC_DIR.iterdir() if entry.is_file())
    return frozenset(documents)


def auth_middleware(app: Any, verifier: TokenVerifier, get_state: Callable[[], Any]) -> Any:
    """Wrap ``app`` with the credential gate.

    ``verifier`` is the authorization seam: a test (or a future cloud issuer)
    passes its own and every client keeps the same header contract.
    ``get_state`` reads the live app state, where the enumerated console
    documents ride.
    """

    async def _entry(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        documents: frozenset[str] = getattr(get_state(), "console_documents", frozenset())
        path = scope.get("path", "")
        if path not in OPEN_PATHS and path not in documents:
            if not _authorized(scope, verifier):
                await _refuse(send)
                return
            _count_admitted_request(get_state(), scope)
        if path in documents:
            await app(scope, receive, _granting_send(send, scope))
            return
        await app(scope, receive, send)

    return _entry


def _count_admitted_request(state: Any, scope: Any) -> None:
    """Hand one admitted request to the observability counters, by its actor.

    Only the gate's admitted traffic reaches here: a refused request left at
    :func:`_refuse`, and the open paths never pass through this branch. The
    actor is the self-declared header, exactly as the routes read it.
    """
    observability = getattr(state, "observability", None)
    if observability is None:
        return
    observability.note_authenticated_request(actor_from_header(_header_value(scope, ACTOR_HEADER.lower())))


# ---------------------------------------------------------------- refusal


async def _refuse(send: Any) -> None:
    body = json.dumps({"detail": AUTH_DETAIL}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------- presented credential


def _authorized(scope: Any, verifier: TokenVerifier) -> bool:
    presented = _bearer_value(scope) or _cookie_value(scope)
    return verifier.verify(presented)


def _header_value(scope: Any, name: str) -> str | None:
    target = name.encode("ascii")
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name != target:
            continue
        value: str = raw_value.decode("latin-1")
        return value
    return None


def _bearer_value(scope: Any) -> str | None:
    header = _header_value(scope, "authorization")
    if header is None:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def _cookie_value(scope: Any) -> str | None:
    header = _header_value(scope, "cookie")
    if not header:
        return None
    for part in header.split(";"):
        name, _, value = part.partition("=")
        if name.strip() == CREDENTIAL_COOKIE_NAME:
            return value.strip()
    return None


# ---------------------------------------------------------------- console cookie grant


def _granting_send(send: Any, scope: Any) -> Any:
    cookie = _grant_cookie(scope)

    async def _send(message: Any) -> None:
        if cookie is not None and message["type"] == "http.response.start":
            MutableHeaders(scope=message).append("set-cookie", cookie)
        await send(message)

    return _send


def _grant_cookie(scope: Any) -> str | None:
    host = _header_value(scope, "host") or ""
    if not _is_loopback_host(host):
        return None
    origin = _header_value(scope, "origin")
    if origin is not None and not _is_same_origin(origin, host):
        return None
    credential = read_credential_or_none()
    if credential is None:
        return None
    return f"{CREDENTIAL_COOKIE_NAME}={credential}; {CREDENTIAL_COOKIE_ATTRIBUTES}"


def _host_name(host: str) -> str:
    value = host.strip()
    if value.startswith("["):
        return value[1:].split("]", 1)[0].lower()
    if value.count(":") == 1:
        return value.split(":", 1)[0].lower()
    return value.lower()


def _host_port(host: str) -> int | None:
    value = host.strip()
    if value.startswith("["):
        _, separator, port = value[1:].partition("]")
        if not separator or not port.startswith(":"):
            return None
        port = port[1:]
    elif value.count(":") == 1:
        port = value.split(":", 1)[1]
    else:
        return None
    return int(port) if port.isdigit() else None


def _is_loopback_host(host: str) -> bool:
    name = _host_name(host)
    return name in LOOPBACK_HOST_NAMES or name.startswith("127.")


def _is_same_origin(origin: str, host: str) -> bool:
    parsed = urlsplit(origin)
    try:
        origin_port = parsed.port
    except ValueError:
        return False
    if (parsed.hostname or "").lower() != _host_name(host):
        return False
    return origin_port == _host_port(host)
