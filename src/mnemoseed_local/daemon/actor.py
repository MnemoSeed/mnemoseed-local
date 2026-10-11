"""Audit actor attribution for the A2 local daemon (no identity/accounts).

The CLI surface forwards ``X-MnemoSeed-Actor: cli``, the host-hook transformer
forwards ``hook``; anything else (console / default) answers ``console``. The
wire value never influences authorization — the credential boundary decides who
may call the daemon at all (see ``credential.py`` / ``daemon/auth.py``), and
holding the credential makes every caller equivalent, so this header stays the
self-declared attribution it has always been.
"""

from __future__ import annotations

from fastapi import Request

_VALID_ACTORS = ("cli", "console", "mcp", "hook")


def resolve_actor(request: Request) -> str:
    """The audit actor from the header, defaulting to ``console``."""
    value = request.headers.get("X-MnemoSeed-Actor")
    if value in _VALID_ACTORS:
        return value
    return "console"
