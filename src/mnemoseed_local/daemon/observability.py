"""Process-local observability counters (B2.12).

Since-boot activity signals the doctor surface reads over HTTP: capture-hook
ingest traffic vs authenticated requests per audit actor (the MCP gateway is
the only production surface that calls as the ``mcp`` actor, so its
authenticated calls since boot are the connected signal), plus first-sighting
hygiene for non-default profile_ids (a typo'd id presents as an empty
namespace). Everything here is in-memory per boot and purely observational —
no ingest/recall behavior changes.
"""

from __future__ import annotations

import logging
import threading
import time

from mnemoseed_local.rest_client import DEFAULT_PROFILE_ID

logger = logging.getLogger("mnemoseed_local.daemon.observability")


class Observability:
    """Thread-safe since-boot counters; one instance per daemon boot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._capture_ingests = 0
        self._authenticated_actors: dict[str, int] = {}
        self._seen_profiles: set[str] = set()
        # T2 auto-recall: aggregate-only since-boot counters for the mid-session
        # injection serve (count of serves + cumulative injected chars). Pure
        # observation numbers — no per-session breakdown, no ids, no text.
        self._recall_injection_count = 0
        self._recall_injection_chars_total = 0

    def note_capture_ingest(self) -> None:
        with self._lock:
            self._capture_ingests += 1

    def note_authenticated_request(self, actor: str) -> None:
        """Count one request the credential gate admitted, per audit actor.

        The counter sits behind the gate, so only a caller that presented the
        daemon credential can move it, and the attribution stays the
        self-declared header it has always been.
        """
        with self._lock:
            self._authenticated_actors[actor] = self._authenticated_actors.get(actor, 0) + 1

    def note_profile_sighting(self, profile_id: str) -> None:
        if profile_id == DEFAULT_PROFILE_ID:
            return
        with self._lock:
            first = profile_id not in self._seen_profiles
            self._seen_profiles.add(profile_id)
        if first:
            logger.info(
                "first sighting of profile_id %r in this daemon process "
                "(unknown ids present as empty namespaces until registered)",
                profile_id,
            )

    def note_recall_injection(self, char_count: int) -> None:
        """Record one T2 auto-recall injection serve (aggregate-only counter)."""
        with self._lock:
            self._recall_injection_count += 1
            self._recall_injection_chars_total += char_count

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "boot_started_at": self._started_at,
                "capture_ingest_count": self._capture_ingests,
                "authenticated_actor_counts": dict(self._authenticated_actors),
                "recall_injection_count": self._recall_injection_count,
                "recall_injection_chars_total": self._recall_injection_chars_total,
                "recall_pending_served_count": self._recall_injection_count,
            }
