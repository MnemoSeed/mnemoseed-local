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
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mnemoseed_local.dream.delta import DeltaReport
from mnemoseed_local.dream.merge import Merger
from mnemoseed_local.dream.reflect import (
    ReflectionResult,
    ReflectOrchestrator,
    ReflectOutcome,
    result_from_payload,
)
from mnemoseed_local.dream.snapshot import (
    Snapshot,
    SnapshotPhase,
    resume_boundary,
)
from mnemoseed_local.dream.trigger import DreamTrigger
from mnemoseed_local.storage.ports import TurnRange

logger = logging.getLogger("mnemoseed_local.dream.pipeline")

#: Identical no-progress deferrals tolerated before a window parks. Parking
#: stops the LLM cost of hopeless retries; the journal keeps the data and a
#: daemon restart grants fresh attempts.
_PARK_AFTER_IDENTICAL_DEFERRALS = 3


def _window_key(snapshot: Snapshot) -> tuple[str, int, int]:
    """Stable identity for one pending dream window.

    Every retry recaptures a brand-new snapshot id (``FileSnapshotter`` mints
    a fresh uuid per capture), so snapshot ids can never key a strike counter
    in production. The (profile, turn-range) window is what survives
    recapture: identical re-emissions accumulate strikes; genuine growth is a
    different window with fresh attempts.
    """
    return (snapshot.profile_id, snapshot.turn_range.start, snapshot.turn_range.end)


@dataclass(frozen=True)
class RunCompletion:
    """One committed dream's completion record for the dream log surface.

    ``run_id`` mirrors the snapshot id (the run row's registered id). ``tokens``
    is delta estimate + provider-reported completion — the same total the
    monthly ledger meters (0 when the usage side is unknowable, e.g. a
    merge-boundary journal recovery).
    """

    run_id: str
    profile_id: str
    started_at: float
    finished_at: float
    tokens: int


@dataclass(frozen=True)
class ExtractFailure:
    """One failed dream attempt, classified for the observation log.

    ``failure_class`` is coarse and honest: llm_unreachable / persist_failed /
    over_budget / truncated_delta_deferred / oversized_parked / merge_degraded /
    journal_unrecoverable / reflect_error. ``tokens`` is the wasted attempt's
    known cost (0 when unknowable).
    """

    profile_id: str
    turn_range: TurnRange
    stage: str
    failure_class: str
    detail: str | None
    tokens: int


def _classify_failure(outcome: ReflectOutcome) -> str:
    if outcome.llm_unavailable:
        return "llm_unreachable"
    error = outcome.error or ""
    if error.startswith("persist failed"):
        return "persist_failed"
    if "exceed the delta budget" in error:
        return "over_budget"
    return "reflect_error"


def _completion_tokens(report: DeltaReport | None) -> int:
    if report is None:
        return 0
    completion = 0
    if report.provider_usage is not None and report.provider_usage.completion_tokens is not None:
        completion = report.provider_usage.completion_tokens
    return report.delta_tokens + completion


class _Snapshotter(Protocol):
    """The minimal snapshotter surface the pipeline needs: read-active."""

    def active(self, profile_id: str) -> Snapshot | None: ...


class DreamPipeline:
    """Drive one profile's dream across the reflect and merge boundaries.

    ``reflector`` and ``merger`` are the seams to the T3/T4 engines (injected
    here on the drain path, not assembled inside hot-path code). ``trigger``
    advances the state machine at each completion seam and owns the committer
    that fires the safe-clear purger after a committed merge. ``on_outcome`` is
    the A2.5 backoff seam: it is invoked on the dream worker thread with the
    final (ok/error) of every attempt so the scheduler can re-fire failed
    windows on its exponential backoff.
    """

    def __init__(
        self,
        *,
        trigger: DreamTrigger,
        snapshotter: _Snapshotter,
        reflector: ReflectOrchestrator,
        merger: Merger,
        on_outcome: Callable[[str, TurnRange, bool, str | None], None] | None = None,
        on_run_committed: Callable[[RunCompletion], None] | None = None,
        on_extract_failed: Callable[[ExtractFailure], None] | None = None,
        mode: Callable[[], str] | None = None,
    ) -> None:
        self._trigger = trigger
        self._snapshotter = snapshotter
        self._reflector = reflector
        self._merger = merger
        self._on_run_committed = on_run_committed
        self.on_extract_failed = on_extract_failed
        # B5 vote: the live ensemble mode ("off" | "verify" | "vote"). When
        # wired, the pipeline dispatches the fresh-snapshot boundary to the
        # vote dual-seat chain instead of the single-model reflect.
        self._mode = mode if mode is not None else (lambda: "off")
        # Wired after construction: the scheduler is built after the pipeline in
        # the daemon lifespan, exactly like ``FileSnapshotter.on_ready``.
        self.on_outcome = on_outcome
        # Oversized-delta hot-loop guard: consecutive identical deferrals per
        # pending window. A window that keeps deferring with no progress parks
        # after the limit and stops consuming LLM calls until a daemon restart
        # grants fresh attempts.
        self._deferred_attempts: dict[tuple[str, int, int], int] = {}
        self._parked_windows: set[tuple[str, int, int]] = set()

    def _is_vote(self) -> bool:
        """Whether the live ensemble mode is the vote dual-seat path."""
        return self._mode() == "vote"

    def on_snapshot_ready(self, profile_id: str) -> None:
        """Trigger seam: a fresh snapshot completed capture, the state machine
        advances (DREAMING), and the reflect boundary runs immediately."""
        self._trigger.on_snapshot_ready(profile_id)
        snapshot = self._snapshotter.active(profile_id)
        if snapshot is not None:
            self.run(snapshot)

    def run(self, snapshot: Snapshot, *, counts_toward_parking: bool = True) -> None:
        """Run whichever boundary the snapshot resumes at. Never raises; the
        journal is the source of truth and every failure stays tracked there.

        ``counts_toward_parking``: boot-recovery replay of a journaled
        merge-boundary snapshot re-defers with NO new LLM evidence — replaying
        an old verdict must not arm the oversized-delta parking guard, or every
        boot would re-park the window before any batched reflect could run.
        Only fresh attempts (launch seam / eval harness) count.
        """
        window = _window_key(snapshot)
        if window in self._parked_windows:
            logger.warning(
                "dream window %s (%s..%s) is parked after repeated oversized-delta "
                "deferrals; skipping without any LLM call. A daemon restart grants "
                "fresh attempts.",
                snapshot.profile_id,
                snapshot.turn_range.start,
                snapshot.turn_range.end,
            )
            # Release the trigger's in-flight bookkeeping (the state machine
            # already advanced to DREAMING on the launch path) and report the
            # failed outcome so scheduler retries count toward give-up instead
            # of wedging the profile forever.
            self._trigger.on_dream_failed(snapshot.profile_id)
            if self.on_outcome is not None:
                self.on_outcome(
                    snapshot.profile_id, snapshot.turn_range, False, "oversized-delta window parked"
                )
            return
        boundary = resume_boundary(snapshot)
        if boundary is None:
            return  # merge already committed; the journal terminated this dream
        if boundary == "merge":
            self._run_merge_boundary(snapshot, counts_toward_parking=counts_toward_parking)
        elif boundary == "reflect":
            if self._is_vote():
                self._run_vote_a_boundary(snapshot, counts_toward_parking=counts_toward_parking)
            else:
                self._run_reflect_boundary(snapshot, counts_toward_parking=counts_toward_parking)
        elif boundary == "reflect_b":
            self._run_vote_b_boundary(snapshot, counts_toward_parking=counts_toward_parking)
        elif boundary == "combine":
            self._run_combine_boundary(snapshot, counts_toward_parking=counts_toward_parking)

    # ------------------------------------------------------------ boundaries

    def _run_reflect_boundary(self, snapshot: Snapshot, *, counts_toward_parking: bool = True) -> None:
        """Fresh/interrupted-at-reflect snapshot: reflect, then merge on success."""
        outcome = self._reflector.reflect(snapshot)
        if not outcome.ok or outcome.result is None:
            logger.warning(
                "reflect degraded for %s (ok=%s); snapshot stays journaled",
                snapshot.profile_id,
                outcome.ok,
            )
            self._fail(snapshot, outcome.error, stage="reflect", outcome=outcome)
            return
        self._merge(
            snapshot,
            outcome.result,
            outcome.report,
            counts_toward_parking=counts_toward_parking,
            empty_verdict_commitable=outcome.batched,
        )

    def _run_vote_a_boundary(self, snapshot: Snapshot, *, counts_toward_parking: bool = True) -> None:
        """B5 vote: run seat A, then chain B -> combine -> merge on success."""
        outcome = self._reflector.reflect_vote_a(snapshot)
        if not outcome.ok or outcome.result is None:
            logger.warning(
                "vote seat A degraded for %s (ok=%s); snapshot stays journaled",
                snapshot.profile_id,
                outcome.ok,
            )
            self._fail(snapshot, outcome.error, stage="vote_seat_a", outcome=outcome)
            return
        self._run_vote_b_boundary(snapshot, counts_toward_parking=counts_toward_parking)

    def _run_vote_b_boundary(self, snapshot: Snapshot, *, counts_toward_parking: bool = True) -> None:
        """B5 vote: run seat B, then chain combine -> merge on success."""
        outcome = self._reflector.reflect_vote_b(snapshot)
        if not outcome.ok or outcome.result is None:
            logger.warning(
                "vote seat B degraded for %s (ok=%s); snapshot stays journaled",
                snapshot.profile_id,
                outcome.ok,
            )
            self._fail(snapshot, outcome.error, stage="vote_seat_b", outcome=outcome)
            return
        self._run_combine_boundary(snapshot)

    def _run_combine_boundary(self, snapshot: Snapshot, *, counts_toward_parking: bool = True) -> None:
        """B5 vote: fold the two seat results, then merge the combined result."""
        outcome = self._reflector.combine(snapshot)
        if not outcome.ok or outcome.result is None:
            logger.warning(
                "vote combine degraded for %s (ok=%s); snapshot stays journaled",
                snapshot.profile_id,
                outcome.ok,
            )
            self._fail(snapshot, outcome.error, stage="vote_combine", outcome=outcome)
            return
        self._merge(snapshot, outcome.result, None)

    def _run_merge_boundary(self, snapshot: Snapshot, *, counts_toward_parking: bool = True) -> None:
        """Recovered at the merge boundary: merge ONLY, re-loading the persisted
        result from the journal — reflect must never re-run."""
        if (
            SnapshotPhase.REFLECT_DONE.value not in snapshot.phases
            and SnapshotPhase.COMBINE_DONE.value not in snapshot.phases
        ):
            logger.warning(
                "merge-boundary snapshot %s lacks a reflect/combine marker; staying journaled",
                snapshot.snapshot_id,
            )
            self._fail(
                snapshot,
                "merge-boundary snapshot lacks REFLECT_DONE/COMBINE_DONE",
                stage="merge_boundary",
                failure_class="journal_unrecoverable",
            )
            return
        result = result_from_payload(snapshot.reflect_result)
        if result is None:
            logger.warning(
                "merge-boundary snapshot %s has no recoverable result; staying journaled",
                snapshot.snapshot_id,
            )
            self._fail(
                snapshot,
                "merge-boundary snapshot has no recoverable result",
                stage="merge_boundary",
                failure_class="journal_unrecoverable",
            )
            return
        self._merge(
            snapshot,
            result,
            None,
            counts_toward_parking=counts_toward_parking,
            empty_verdict_commitable=result.batched,
        )

    def _merge(
        self,
        snapshot: Snapshot,
        result: ReflectionResult,
        report: DeltaReport | None = None,
        *,
        counts_toward_parking: bool = True,
        empty_verdict_commitable: bool = False,
    ) -> None:
        batched_commitable = empty_verdict_commitable or result.batched
        has_batched_key = "batched" in (snapshot.reflect_result or {})
        # Legacy stale-journal migration applies ONLY at the merge boundary: the
        # snapshot actually carries a persisted reflect_result from a prior boot
        # whose payload predates the ``batched`` key. A fresh reflect-boundary
        # snapshot keeps reflect_result None in-memory (reflect persists to disk
        # only), so keying off that dict would read a bare pre-persist object and
        # wrongly treat a fresh non-batched truncated-empty as stale. Gating on
        # the persisted/merge boundary keeps migration scoped to the legacy file
        # while every fresh/reflect rebroadcast defers (D1, FR-2.5 never-drop).
        stale_recovery_commitable = (
            not counts_toward_parking
            and bool(result.consumed_chunk_ids)
            and resume_boundary(snapshot) == "merge"
            and not has_batched_key
        )
        if (
            not result.triples
            and result.overflow_chunk_ids
            and not batched_commitable
            and not stale_recovery_commitable
        ):
            # Engine-side insurance (D1, FR-2.5 never-drop invariant): the
            # result is empty BECAUSE the delta was truncated, so committing it
            # would fire the safe-clear and purge source chunks the model never
            # saw. Defer instead: the snapshot stays journaled and a later dream
            # (larger budget / manual run) can pick the overflow up. A genuinely
            # empty result with NO overflow (all-noise session) still merges and
            # purges normally.
            #
            # Batched exception (#99): when the batched seat ran, every COVERED
            # chunk was fully handed to the model under budget — an empty
            # verdict there is a genuine "nothing durable in this range", so
            # merge commits it and the allow-list safe-clear clears exactly the
            # covered ids while the unseen overflow tail stays journaled. This
            # is what lets an unextractable head-of-queue advance instead of
            # wedging the whole backlog forever.
            if counts_toward_parking:
                attempts = self._deferred_attempts.get(_window_key(snapshot), 0) + 1
                self._deferred_attempts[_window_key(snapshot)] = attempts
                if attempts >= _PARK_AFTER_IDENTICAL_DEFERRALS:
                    self._parked_windows.add(_window_key(snapshot))
                    logger.error(
                        "dream window %s (%s..%s) parked after %d identical oversized-delta "
                        "deferrals (%d overflow chunks, no progress); stopping "
                        "automatic retries to stop the token burn",
                        snapshot.profile_id,
                        snapshot.turn_range.start,
                        snapshot.turn_range.end,
                        attempts,
                        len(result.overflow_chunk_ids),
                    )
                    self._fail(
                        snapshot,
                        f"oversized delta parked after {attempts} identical deferrals",
                        stage="merge",
                        failure_class="oversized_parked",
                        report=report,
                    )
                    return
                logger.warning(
                    "merge deferred for %s (attempt %d/%d): reflect covered a truncated "
                    "delta with %d overflow chunks and produced no triples; snapshot "
                    "stays journaled so a later dream can pick the overflow up",
                    snapshot.profile_id,
                    attempts,
                    _PARK_AFTER_IDENTICAL_DEFERRALS,
                    len(result.overflow_chunk_ids),
                )
            else:
                # Boot-recovery replay of a journaled merge-boundary verdict:
                # no new LLM evidence, so no strike — otherwise every boot
                # would re-park the window before any fresh batched reflect
                # could ever run.
                logger.warning(
                    "merge deferred for %s (boot replay, not counted): journaled "
                    "truncated delta with %d overflow chunks stays journaled",
                    snapshot.profile_id,
                    len(result.overflow_chunk_ids),
                )
            self._fail(
                snapshot,
                "reflect covered a truncated delta with overflow; merge deferred",
                stage="merge",
                failure_class="truncated_delta_deferred",
                report=report,
            )
            return
        if not result.triples and batched_commitable:
            logger.warning(
                "batched dream for %s commits an EMPTY extraction verdict over %d covered "
                "chunk(s); %d unseen overflow chunk(s) stay pending for a later dream",
                snapshot.profile_id,
                len(result.consumed_chunk_ids),
                len(result.overflow_chunk_ids),
            )
        elif not result.triples and stale_recovery_commitable:
            logger.warning(
                "stale journal recovery for %s commits an EMPTY extraction verdict over %d covered "
                "chunk(s); %d unseen overflow chunk(s) stay pending (migration)",
                snapshot.profile_id,
                len(result.consumed_chunk_ids),
                len(result.overflow_chunk_ids),
            )
        outcome = self._merger.merge(snapshot, result)
        if outcome.ok:
            self._deferred_attempts.pop(_window_key(snapshot), None)
        if not outcome.ok:
            logger.warning(
                "merge degraded for %s: %s; snapshot stays journaled for resume_merge",
                snapshot.profile_id,
                outcome.error,
            )
            self._fail(snapshot, outcome.error, stage="merge", failure_class="merge_degraded", report=report)
            return
        if outcome.committed and self._on_run_committed is not None:
            now = time.time()
            completion = RunCompletion(
                run_id=snapshot.snapshot_id,
                profile_id=snapshot.profile_id,
                started_at=snapshot.created_at,
                finished_at=now,
                tokens=_completion_tokens(report),
            )
            logger.info(
                "dream committed for %s (run %s): %.1fs, tokens=%d",
                completion.profile_id,
                completion.run_id,
                completion.finished_at - completion.started_at,
                completion.tokens,
            )
            self._on_run_committed(completion)
        if self.on_outcome is not None:
            self.on_outcome(snapshot.profile_id, snapshot.turn_range, True, None)

    def _fail(
        self,
        snapshot: Snapshot,
        error: str | None,
        *,
        stage: str = "reflect",
        outcome: ReflectOutcome | None = None,
        failure_class: str | None = None,
        report: DeltaReport | None = None,
    ) -> None:
        """One dream attempt ended without committing: the trigger drops its
        in-flight bookkeeping (so a retried event can launch again) and the
        outcome seam reports the failure to the scheduler's retry backoff.
        Never a raise; the journal stays the source of truth. The observation
        seam receives one classified record; a faulty sink is isolated."""
        self._trigger.on_dream_failed(snapshot.profile_id)
        if self.on_extract_failed is not None:
            classified = failure_class or (
                _classify_failure(outcome) if outcome is not None else "reflect_error"
            )
            try:
                self.on_extract_failed(
                    ExtractFailure(
                        profile_id=snapshot.profile_id,
                        turn_range=snapshot.turn_range,
                        stage=stage,
                        failure_class=classified,
                        detail=error,
                        tokens=_completion_tokens(
                            report if report is not None else (outcome.report if outcome else None)
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - observation must never break the dream
                logger.warning("extract-failure observer raised for %s: %s", snapshot.profile_id, exc)
        if self.on_outcome is not None:
            self.on_outcome(snapshot.profile_id, snapshot.turn_range, False, error)
