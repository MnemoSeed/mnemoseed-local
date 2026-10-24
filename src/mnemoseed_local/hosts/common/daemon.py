"""Shared daemon POST discipline for host hooks: env base URL, per-lane
timeouts, fire-and-forget failure reporting, and the dedicated recall pull."""

from __future__ import annotations

import os
import sys
from typing import Any

from httpx import Timeout

from mnemoseed_local.rest_client import DaemonClient, client_for

#: Daemon base URL a hook posts to when no env override is set.
DEFAULT_BASE_URL = "http://localhost:7788"

#: Fire-and-forget POST budget (mirrors the opencode plugin's 2s discipline).
POST_TIMEOUT_SECONDS = 2.0

#: Dedicated mid-session pull budget: a distinct constant — reusing the flat
#: 2s host budget would be too heavy for a per-turn pull.
RECALL_PULL_TIMEOUT_SECONDS = 0.3

#: Fallback item budget when the daemon omits ``budget_chars`` (older daemon,
#: or a malformed payload). The daemon is the ONLY budget authority in the
#: normal path; this mirrors the opencode plugin's ``RECALL_PULL_MAX_CHARS``.
RECALL_PULL_MAX_CHARS = 1200

#: The daemon reports its effective item budget on the wire under this key.
RECALL_BUDGET_KEY = "budget_chars"


def resolve_base_url() -> str:
    """Daemon base URL the hook posts to (env override, else the default)."""
    return (os.environ.get("MNEMOSEED_LOCAL_BASEURL") or DEFAULT_BASE_URL).rstrip("/")


def profile_id() -> str:
    """Profile binding convention: env override or the default profile."""
    return os.environ.get("MNEMOSEED_LOCAL_PROFILE_ID") or "default"


def debug_enabled() -> bool:
    return bool(os.environ.get("MNEMOSEED_LOCAL_DEBUG"))


def debug(message: str) -> None:
    """Opt-in failure lane: stderr only (never stdout), silent unless enabled."""
    if debug_enabled():
        print(f"mnemoseed-local: {message}", file=sys.stderr)


def hook_client(timeout: float | Timeout = POST_TIMEOUT_SECONDS) -> DaemonClient:
    """The hook POST client: env base URL, env profile binding, hook actor."""
    return client_for(resolve_base_url(), profile_id=profile_id(), actor="hook", timeout=timeout)


def pull_pending_recall(session_id: str, client: DaemonClient) -> dict[str, Any] | None:
    """Bounded ``POST /session/recall-pending`` pull (300ms, fail-open).

    A dedicated await with its own timeout, never the fire-and-forget
    ``post()`` budget. ``seen_chunk_ids`` is empty — hosts without a T1
    session-start replay have nothing before the T2 pull to de-duplicate
    against.
    """
    pull_client = DaemonClient(
        base_url=client.base_url,
        profile_id=client.profile_id,
        actor=client.actor,
        timeout=Timeout(RECALL_PULL_TIMEOUT_SECONDS),
        token=client.token,
    )
    try:
        return pull_client.post(
            "/session/recall-pending",
            {"profile_id": client.profile_id, "session_id": session_id, "seen_chunk_ids": []},
        )
    except Exception as exc:  # noqa: BLE001 - fail-open pull lane
        debug(f"recall-pending pull failed: {exc}")
        return None
