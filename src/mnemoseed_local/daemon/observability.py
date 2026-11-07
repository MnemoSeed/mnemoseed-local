"""Process-local observability counters (B2.12).

Since-boot activity signals the doctor surface reads over HTTP: capture-hook
ingest traffic vs authenticated requests per audit actor (the MCP gateway is
the only production surface that calls as the ``mcp`` actor, so its
authenticated calls since boot are the connected signal), plus first-sighting
hygiene for non-default profile_ids (a typo'd id presents as an empty
namespace). Everything here is in-memory per boot and purely observational —
no ingest/recall behavior changes.

The refinement status computation also lives here: one state, shared by the
status surfaces, computed on demand from explicit config values plus the dream
route's readiness probe — stateless, no polling. The capture-evidence states
for the host status surface share the same convention: pure mappings from
explicit observations onto honest states, where absence is not a failure and
an unreadable source is unknown, never a guess.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from mnemoseed_local.config import RoleLLMConfig
from mnemoseed_local.dream.readiness import DreamRouteReadiness
from mnemoseed_local.rest_client import DEFAULT_PROFILE_ID
from mnemoseed_local.util.process_memory import process_commit_mb

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
                "process_commit_mb": process_commit_mb(),
            }


#: The three computed refinement states, kept distinct on purpose: a disabled
#: posture (no dream schedule, no route probe owed) is not the same fact as an
#: enabled posture whose route currently fails, and neither is readiness.
REFINEMENT_DISABLED = "disabled"
REFINEMENT_UNAVAILABLE = "enabled_unavailable"
REFINEMENT_READY = "ready"

#: Budget of the live route probe (the readiness gate's own default).
REFINEMENT_PROBE_TIMEOUT_S: float = 1.0


@dataclass(frozen=True)
class RefinementStatus:
    """One computed refinement state, shared by the status surfaces.

    ``live_check`` marks a verdict that came from consulting the dream route's
    readiness probe; the disabled posture owes no probe at all.
    """

    state: str
    detail: str
    live_check: bool

    def line(self) -> str:
        if self.state == REFINEMENT_DISABLED:
            return f"refinement: disabled ({self.detail})"
        if self.state == REFINEMENT_UNAVAILABLE:
            return f"refinement: enabled, provider unavailable (live check: {self.detail})"
        if self.state == REFINEMENT_READY:
            return f"refinement: ready (live check: {self.detail})"
        raise ValueError(f"unknown refinement state: {self.state!r}")


def _route_probe(routes: Mapping[str, RoleLLMConfig], timeout_s: float) -> Callable[[], tuple[bool, str]]:
    """Adapt the daemon's dream-route readiness gate to a one-shot
    (ok, detail) probe — the same availability source the dream scheduler
    consumes, never a second probe implementation."""
    readiness = DreamRouteReadiness(routes, timeout_s=timeout_s)

    def _probe() -> tuple[bool, str]:
        ok = readiness.refresh()
        return ok, str(readiness.status()["detail"])

    return _probe


def refinement_status(
    *,
    auto_trigger: bool,
    auto_recall: bool,
    routes: Mapping[str, RoleLLMConfig],
    probe: Callable[[], tuple[bool, str]] | None = None,
    timeout_s: float = REFINEMENT_PROBE_TIMEOUT_S,
) -> RefinementStatus:
    """Compute the refinement state from explicit config values.

    Disabled: the dream schedule is off — the capture-recall posture when auto
    recall is on. Enabled: one bounded live probe of the configured dream route
    decides ready vs provider-unavailable; an injected ``probe`` replaces it
    for tests.
    """
    if not auto_trigger:
        detail = "capture-recall mode" if auto_recall else "auto recall off"
        return RefinementStatus(REFINEMENT_DISABLED, detail=detail, live_check=False)
    live_probe = probe if probe is not None else _route_probe(routes, timeout_s)
    ok, detail = live_probe()
    if ok:
        return RefinementStatus(REFINEMENT_READY, detail=detail, live_check=True)
    return RefinementStatus(REFINEMENT_UNAVAILABLE, detail=detail, live_check=True)


# ---------------------------------------------- capture evidence (host status)

#: The persisted-capture evidence states: absence, in-window presence and
#: out-of-window presence are three different facts, and an unreadable source
#: is none of them.
CAPTURE_EVIDENCE_NONE = "none"
CAPTURE_EVIDENCE_RECENT = "recent"
CAPTURE_EVIDENCE_STALE = "stale"
CAPTURE_EVIDENCE_UNKNOWN = "unknown"

#: The since-boot counter vocabulary shared by the ingest-acknowledgement and
#: replay observations: a counter that moved, one that has not, and one that
#: could not be read.
OBSERVATION_OBSERVED = "observed"
OBSERVATION_NOT_OBSERVED = "not_observed"
OBSERVATION_UNKNOWN = "unknown"


def _human_duration(seconds: float) -> str:
    """A coarse duration for the status lines (the JSON carries raw values)."""
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.0f}s"


@dataclass(frozen=True)
class CaptureEvidenceStatus:
    """One computed persisted-capture state for the host status surface.

    ``last_persisted_at`` is the newest host-attributed persisted chunk's
    ingest time (None for absence and for unknown); ``window_s`` is the
    freshness window the state was computed against.
    """

    state: str
    detail: str
    last_persisted_at: float | None
    window_s: float

    def line(self) -> str:
        return f"capture: {self.state} ({self.detail})"


def unreadable_capture_evidence(detail: str, *, window_s: float) -> CaptureEvidenceStatus:
    """The unknown state for evidence that could not be read — never a guess."""
    return CaptureEvidenceStatus(CAPTURE_EVIDENCE_UNKNOWN, detail, None, window_s)


def capture_evidence_status(
    *,
    last_persisted_at: float | None,
    window_s: float,
    now: float | None = None,
) -> CaptureEvidenceStatus:
    """Compute the persisted-capture state from explicit evidence values.

    Absence (no persisted capture for the host) is its own honest state — an
    idle user is not a failure. Within the window is recent; older is stale;
    a future timestamp reads as within the window (clock skew is not a
    verdict). The window is the caller's capture idle-flush convention.
    """
    if now is None:
        now = time.time()
    if last_persisted_at is None:
        return CaptureEvidenceStatus(
            CAPTURE_EVIDENCE_NONE,
            "no persisted capture observed for this host",
            None,
            window_s,
        )
    age = max(0.0, now - last_persisted_at)
    detail = f"last persisted {_human_duration(age)} ago (window {_human_duration(window_s)})"
    state = CAPTURE_EVIDENCE_RECENT if age <= window_s else CAPTURE_EVIDENCE_STALE
    return CaptureEvidenceStatus(state, detail, last_persisted_at, window_s)


@dataclass(frozen=True)
class SinceBootObservation:
    """One since-boot counter read as an honest observation.

    ``count`` is the raw counter (None when it could not be read); the state
    keeps "the counter has not moved" distinct from "the counter was
    unreadable" — neither implies the other, and neither is a failure verdict.
    """

    label: str
    state: str
    count: int | None
    detail: str

    def line(self) -> str:
        return f"{self.label}: {self.state} ({self.detail})"


def since_boot_observation(
    *,
    label: str,
    count: int | None,
    observed: str,
    not_observed: str,
    unknown: str,
) -> SinceBootObservation:
    """Map one since-boot counter onto the shared observation vocabulary."""
    if count is None:
        return SinceBootObservation(label, OBSERVATION_UNKNOWN, None, unknown)
    if count > 0:
        return SinceBootObservation(label, OBSERVATION_OBSERVED, count, observed)
    return SinceBootObservation(label, OBSERVATION_NOT_OBSERVED, count, not_observed)
