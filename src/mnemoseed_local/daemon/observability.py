"""Process-local observability counters (B2.12).

Since-boot activity signals the doctor surface reads over HTTP: capture-hook
ingest traffic vs MCP-gateway handshakes (a registered-but-never-connected MCP
server is invisible otherwise), plus first-sighting hygiene for non-default
profile_ids (a typo'd id presents as an empty namespace). Everything here is
in-memory per boot and purely observational — no ingest/recall behavior
changes.
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
        self._mcp_handshake_count = 0
        self._last_mcp_handshake_at: float | None = None
        self._seen_profiles: set[str] = set()

    def note_capture_ingest(self) -> None:
        with self._lock:
            self._capture_ingests += 1

    def note_mcp_handshake(self) -> None:
        with self._lock:
            self._mcp_handshake_count += 1
            total = self._mcp_handshake_count
            self._last_mcp_handshake_at = time.time()
        logger.info("MCP gateway handshake recorded (%d since boot)", total)

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

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "boot_started_at": self._started_at,
                "capture_ingest_count": self._capture_ingests,
                "mcp_handshake_count": self._mcp_handshake_count,
                "last_mcp_handshake_at": self._last_mcp_handshake_at,
            }
