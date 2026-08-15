"""Audit actor attribution for the A2 local daemon (no identity/accounts).

The CLI surface forwards ``X-MnemoSeed-Actor: cli``; anything else (console /
default) answers ``console``. The wire value never influences authorization —
the daemon is localhost-only by default and there are no tokens.
"""

from __future__ import annotations

from fastapi import Request

_VALID_ACTORS = ("cli", "console", "mcp")


def resolve_actor(request: Request) -> str:
    """The audit actor from the header, defaulting to ``console``."""
    value = request.headers.get("X-MnemoSeed-Actor")
    if value in _VALID_ACTORS:
        return value
    return "console"
