"""Audit actor attribution for the A2 local daemon (no identity/accounts).

The CLI surface forwards ``X-MnemoSeed-Actor: cli``, the host-hook transformer
forwards ``hook``; anything else (console / default) answers ``console``. The
wire value never influences authorization — the credential boundary decides who
may call the daemon at all (see ``credential.py`` / ``daemon/auth.py``), and
holding the credential makes every caller equivalent, so this header stays the
self-declared attribution it has always been.

:func:`actor_from_header` is the one reading of the header: the FastAPI routes
read it through a request, and the credential gate reads the same header off the
raw ASGI scope, so both spellings of the parse stay identical.
"""

from __future__ import annotations

from fastapi import Request

from mnemoseed_local.rest_client import ACTOR_HEADER

_VALID_ACTORS = ("cli", "console", "mcp", "hook")

#: The actor recorded for a caller that declares none the daemon knows.
CONSOLE_ACTOR = "console"


def actor_from_header(value: str | None) -> str:
    """The audit actor a header value names; anything else is the console."""
    return value if value in _VALID_ACTORS else CONSOLE_ACTOR


def resolve_actor(request: Request) -> str:
    """The audit actor from the header, defaulting to ``console``."""
    return actor_from_header(request.headers.get(ACTOR_HEADER))
