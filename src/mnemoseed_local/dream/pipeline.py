"""Dream pipeline orchestration (PRD-02 T4 wiring; FR-2.3 / NFR-2.3).

The DreamPipeline drives the reflect -> merge -> commit chain strictly OFF the
/ingest hot path: it is invoked only from the trigger's snapshot-ready seam and
from the daemon's boot-recovery loop (both run on the session-end drain path,
exactly like the existing drain precedent). A snapshot at the reflect boundary
runs reflect then merge; a recovered snapshot at the merge boundary runs merge
ONLY — the persisted ReflectionResult payload is re-loaded from the journal and
reflect is never re-run (NFR-2.3 idempotent recovery).

Degradation is typed, never raised: a reflect failure, or a merge-boundary
snapshot whose journal payload is unrecoverable, leaves the snapshot journaled
and never hands anything to the merger, so a later boot re-runs the boundary
instead of double-writing.
"""

from __future__ import annotations

import logging
from typing import Protocol

from mnemoseed_local.dream.merge import Merger
from mnemoseed_local.dream.reflect import ReflectionResult, ReflectOrchestrator, result_from_payload
from mnemoseed_local.dream.snapshot import (
    Snapshot,
    SnapshotPhase,
    resume_boundary,
)
from mnemoseed_local.dream.trigger import DreamTrigger

logger = logging.getLogger("mnemoseed_local.dream.pipeline")


class _Snapshotter(Protocol):
    """The minimal snapshotter surface the pipeline needs: read-active."""

    def active(self, profile_id: str) -> Snapshot | None: ...


class DreamPipeline:
    """Drive one profile's dream across the reflect and merge boundaries.

    ``reflector`` and ``merger`` are the seams to the T3/T4 engines (injected
    here on the drain path, not assembled inside hot-path code). ``trigger``
    advances the state machine at each completion seam and owns the committer
    that fires the safe-clear purger after a committed merge.
    """

    def __init__(
        self,
        *,
        trigger: DreamTrigger,
        snapshotter: _Snapshotter,
        reflector: ReflectOrchestrator,
        merger: Merger,
    ) -> None:
        self._trigger = trigger
        self._snapshotter = snapshotter
        self._reflector = reflector
        self._merger = merger

    def on_snapshot_ready(self, profile_id: str) -> None:
        """Trigger seam: a fresh snapshot completed capture, the state machine
        advances (DREAMING), and the reflect boundary runs immediately."""
        self._trigger.on_snapshot_ready(profile_id)
        snapshot = self._snapshotter.active(profile_id)
        if snapshot is not None:
            self.run(snapshot)

    def run(self, snapshot: Snapshot) -> None:
        """Run whichever boundary the snapshot resumes at. Never raises; the
        journal is the source of truth and every failure stays tracked there."""
        boundary = resume_boundary(snapshot)
        if boundary is None:
            return  # merge already committed; the journal terminated this dream
        if boundary == "merge":
            self._run_merge_boundary(snapshot)
        else:
            self._run_reflect_boundary(snapshot)

    # ------------------------------------------------------------ boundaries

    def _run_reflect_boundary(self, snapshot: Snapshot) -> None:
        """Fresh/interrupted-at-reflect snapshot: reflect, then merge on success."""
        outcome = self._reflector.reflect(snapshot)
        if not outcome.ok or outcome.result is None:
            logger.warning(
                "reflect degraded for %s (ok=%s); snapshot stays journaled",
                snapshot.profile_id,
                outcome.ok,
            )
            return
        self._merge(snapshot, outcome.result)

    def _run_merge_boundary(self, snapshot: Snapshot) -> None:
        """Recovered at the merge boundary: merge ONLY, re-loading the persisted
        result from the journal — reflect must never re-run."""
        if SnapshotPhase.REFLECT_DONE.value not in snapshot.phases:
            logger.warning(
                "merge-boundary snapshot %s lacks REFLECT_DONE; staying journaled",
                snapshot.snapshot_id,
            )
            return
        result = result_from_payload(snapshot.reflect_result)
        if result is None:
            logger.warning(
                "merge-boundary snapshot %s has no recoverable result; staying journaled",
                snapshot.snapshot_id,
            )
            return
        self._merge(snapshot, result)

    def _merge(self, snapshot: Snapshot, result: ReflectionResult) -> None:
        if not result.triples and result.overflow_chunk_ids:
            # Engine-side insurance (D1, FR-2.5 never-drop invariant): the
            # result is empty BECAUSE the delta was truncated, so committing it
            # would fire the safe-clear and purge source chunks the model never
            # saw. Defer instead: the snapshot stays journaled and a later dream
            # (larger budget / manual run) can pick the overflow up. A genuinely
            # empty result with NO overflow (all-noise session) still merges and
            # purges normally.
            logger.warning(
                "merge deferred for %s: reflect covered a truncated delta with %d "
                "overflow chunks and produced no triples; snapshot stays journaled "
                "so a later dream can pick the overflow up",
                snapshot.profile_id,
                len(result.overflow_chunk_ids),
            )
            return
        outcome = self._merger.merge(snapshot, result)
        if not outcome.ok:
            logger.warning(
                "merge degraded for %s: %s; snapshot stays journaled for resume_merge",
                snapshot.profile_id,
                outcome.error,
            )
