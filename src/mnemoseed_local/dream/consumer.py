"""Dormant reconciliation consumer (S-D).

Synchronous coordinator for one profile's nominations. Runs on the dream
worker thread only, after a successful non-skipped merge, and ONLY when a
future ratified wiring binds it through DreamPipeline.on_graph_committed:
ZERO production call sites enable it in this slice.

Ordering (design/12 section 7.2, frozen): regular merge, bounded consumer,
bounded audit repair, trigger completion and safe-clear, then the existing
run record and nomination scan. The consumer never gates completion.

Disabled posture: any unset bound (None) means NO reconciliation work at
all — process() returns 0 before any store read, and repair() returns 0.
Unset never means unlimited. Failure handling: seat and application
infrastructure faults record deferred and the dream still completes;
malformed carrier construction is loud (a bug, never a seat verdict);
infrastructure failures roll back their own transaction and propagate
only as far as the pipeline seam, which always completes the dream.

Cursor discipline: every examined carrier consumes scan budget. Fully
handled pages advance the per-profile opaque cursor even after skips;
an interrupted page retains its incoming cursor. EOF resets the cursor
and ends the pass without wrapping. Skipped carriers return next sweep.
Reservation replay never grants execution; deferred audits dedup per dream.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from mnemoseed_local.dream.adjudicate import (
    AdjudicationResult,
    Disposition,
    EndpointFate,
    EndpointObservation,
    MalformedAdjudicationInputError,
    PairAdjudicationInput,
    PairNomination,
    ReasonCode,
    SeatOutcome,
    activation_for,
    adjudicate,
)
from mnemoseed_local.dream.pipeline import CommittedMerge
from mnemoseed_local.dream.repair import repair_reconciliation_audit
from mnemoseed_local.storage.ports import (
    AttemptReservationOutcome,
    AuditEntry,
    GraphStore,
    MetaStore,
    ReconciliationApplication,
    ReconciliationNominationCarrier,
    reconciliation_deferred_dedup_key,
)

logger = logging.getLogger(__name__)

_DEFERRED_ACTION = "reconcile_deferred"
_TERMINAL_ACTIONS = {
    Disposition.ACCEPTED: "reconcile_accepted",
    Disposition.REJECTED: "reconcile_rejected",
    Disposition.UNRESOLVED: "reconcile_unresolved",
}

_VERIFY_MODE = "verify"
_VOTE_MODE = "vote"


def consumer_enabled(
    *,
    ratified: bool,
    configured: bool,
    ensemble_active: bool,
    policy_ok: bool,
) -> bool:
    """Pure activation gate: all four conditions AND together.

    Flags never ratification: a config flag or ensemble mode alone can
    never enable the consumer; ratification is a separate, explicit input.
    No production wiring supplies ratified=True in this slice.
    """
    return bool(ratified and configured and ensemble_active and policy_ok)


@dataclass(frozen=True)
class ConsumerPolicy:
    """Bounded execution policy (S-D). Unset (None) bounds disable all work.

    downweight_factor is a pass-through for accepted applications only;
    None means no accepted application can be applied (the applier rejects
    it loudly). TEST_ONLY_* values in tests have no product meaning.
    """

    nomination_limit: int | None
    scan_limit: int | None
    audit_repair_limit: int | None
    attempt_limit: int | None
    retry_backoff_seconds: float | None
    seat_mode: str
    downweight_factor: float | None
    clock: Callable[[], float]
    attempt_timeout_seconds: float | None = None

    def ready(self) -> bool:
        return all(
            value is not None
            for value in (
                self.nomination_limit,
                self.scan_limit,
                self.audit_repair_limit,
                self.attempt_limit,
                self.retry_backoff_seconds,
                self.attempt_timeout_seconds,
                self.downweight_factor,
            )
        )

    def __post_init__(self) -> None:
        if self.seat_mode not in (_VERIFY_MODE, _VOTE_MODE):
            raise ValueError("seat_mode must be 'verify' or 'vote'")
        for name in (
            "nomination_limit",
            "scan_limit",
            "audit_repair_limit",
            "attempt_limit",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        for name in ("retry_backoff_seconds", "attempt_timeout_seconds"):
            duration = getattr(self, name)
            if duration is not None and (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise ValueError(f"{name} must be a positive finite duration")
        factor = self.downweight_factor
        if factor is not None and (
            isinstance(factor, bool)
            or not isinstance(factor, (int, float))
            or not math.isfinite(factor)
            or not 0.0 < factor < 1.0
        ):
            raise ValueError("downweight_factor must be strictly between zero and one")


class ReconciliationConsumer:
    """Same-profile serialized, bounded reconciliation coordinator.

    One instance per profile at the (future) call sites. Every entry
    serializes on the instance lock so a merge and the consumer for the
    same profile can never interleave. Cross-profile retry state is
    independent (attempts are keyed by profile and nomination).
    """

    def __init__(
        self,
        *,
        graph: GraphStore,
        meta: MetaStore,
        policy: ConsumerPolicy,
        seats: Callable[[PairNomination], tuple[SeatOutcome, ...]] | None = None,
    ) -> None:
        self._graph = graph
        self._meta = meta
        self._policy = policy
        self._seats = seats or (lambda nomination: ())
        self._lock = threading.Lock()
        self._cursors: dict[str, str | None] = {}

    def coordinate(
        self,
        event: CommittedMerge,
        *,
        ratified: bool = False,
        configured: bool = False,
        ensemble_active: bool = False,
    ) -> None:
        if not consumer_enabled(
            ratified=ratified,
            configured=configured,
            ensemble_active=ensemble_active,
            policy_ok=self._policy.ready(),
        ):
            return
        try:
            self.process(event.profile_id, dream_run_id=event.dream_run_id)
        finally:
            try:
                self.repair(event.profile_id)
            except Exception:
                logger.exception("reconciliation repair failed for %s", event.profile_id)

    def process(self, profile_id: str, *, dream_run_id: str) -> int:
        """One bounded pass over the profile's nominations.

        Returns the number of nominations that reached a recorded outcome
        this pass (deferred audit, terminal receipt, or a stored receipt
        re-confirmation). Backoff-skipped nominations consume scan budget
        only and are rediscovered on the next sweep.
        Unset bounds mean no work: returns 0 before any store read.
        """
        policy = self._policy
        nom_limit = policy.nomination_limit
        scan_limit = policy.scan_limit
        if not policy.ready():
            return 0
        assert nom_limit is not None and scan_limit is not None
        execution_owner = json.dumps([1, dream_run_id, str(uuid.uuid4())], separators=(",", ":"))
        with self._lock:
            processed = 0
            scanned = 0
            cursor = self._cursors.get(profile_id)
            while processed < nom_limit and scanned < scan_limit:
                page = self._meta.query_reconciliation_nominations(
                    profile_id=profile_id,
                    limit=min(scan_limit - scanned, nom_limit - processed),
                    cursor=cursor,
                )
                for carrier in page.items:
                    scanned += 1
                    if self._process_one(
                        profile_id, carrier, dream_run_id=dream_run_id, execution_owner=execution_owner
                    ):
                        processed += 1
                cursor = page.next_cursor
                self._cursors[profile_id] = cursor
                if cursor is None:
                    break
            return processed

    def repair(self, profile_id: str) -> int:
        """One bounded outbox-to-audit repair pass. Unset limit: no work."""
        limit = self._policy.audit_repair_limit
        if not self._policy.ready():
            return 0
        assert limit is not None
        with self._lock:
            return repair_reconciliation_audit(self._graph, self._meta, limit=limit)

    # ------------------------------------------------------------ one nomination

    def _process_one(
        self,
        profile_id: str,
        carrier: ReconciliationNominationCarrier,
        *,
        dream_run_id: str,
        execution_owner: str,
    ) -> bool:
        policy = self._policy
        now = policy.clock()
        assert policy.attempt_limit is not None
        assert policy.retry_backoff_seconds is not None
        receipt = self._graph.get_reconciliation_receipt(
            profile_id=profile_id, nomination_id=carrier.nomination_id
        )
        if receipt is not None:
            return True
        attempts = self._meta.list_reconciliation_attempts(
            profile_id=profile_id, nomination_id=carrier.nomination_id
        )
        if any(attempt.next_eligible_at > now for attempt in attempts):
            return False
        reservation = self._meta.reserve_attempt(
            profile_id=profile_id,
            nomination_id=carrier.nomination_id,
            dream_run_id=execution_owner,
            attempt_limit=policy.attempt_limit,
            reserved_at=now,
            next_eligible_at=now + policy.retry_backoff_seconds,
        )
        if reservation.outcome is not AttemptReservationOutcome.RESERVED:
            return self._apply_exhausted(profile_id, carrier, dream_run_id)
        if not reservation.newly_reserved:
            return False
        evidence = self._meta.read_reconciliation_evidence(
            profile_id=profile_id, nomination_id=carrier.nomination_id
        )
        if evidence is None:
            return False
        try:
            input = PairAdjudicationInput(
                nomination=_pair_nomination(carrier),
                left=_observation(self._graph, carrier.lo_node_id),
                right=_observation(self._graph, carrier.hi_node_id),
                ensemble_mode=policy.seat_mode,
                seat_outcomes=(),
            )
            result = adjudicate(input)
            if result.disposition is Disposition.DEFERRED and activation_for(policy.seat_mode).active:
                result = adjudicate(replace(input, seat_outcomes=self._seats(input.nomination)))
        except MalformedAdjudicationInputError:
            raise
        except Exception as exc:
            logger.warning("adjudication failed for %s: %s", carrier.nomination_id, exc)
            return self._record_deferred(profile_id, carrier, ReasonCode.SEAT_UNAVAILABLE, dream_run_id)
        if result.disposition is Disposition.DEFERRED:
            return self._record_deferred(profile_id, carrier, result.reason_code, dream_run_id)
        return self._apply_terminal(profile_id, carrier, result, dream_run_id)

    def _apply_exhausted(
        self, profile_id: str, carrier: ReconciliationNominationCarrier, dream_run_id: str
    ) -> bool:
        """Budget exhausted: terminal unresolved without any model call."""
        application = ReconciliationApplication(
            nomination_id=carrier.nomination_id,
            profile_id=profile_id,
            disposition="unresolved",
            reason_code="retry_exhausted",
            canonical_kind=carrier.canonical_kind,
            composite_group_id=carrier.composite_group_id,
            evidence_event_ids=tuple(carrier.evidence_event_ids),
            verdict="insufficient",
            quality="degraded",
            left_node_id=carrier.lo_node_id,
            left_version=carrier.lo_version,
            left_expected_peer_id=carrier.lo_expected_peer,
            right_node_id=carrier.hi_node_id,
            right_version=carrier.hi_version,
            right_expected_peer_id=carrier.hi_expected_peer,
            winner_node_id=None,
            loser_node_id=None,
            loser_prior_version=None,
            loser_new_version=None,
            applied_at=self._policy.clock(),
        )
        return self._apply(application, carrier, dream_run_id, None)

    def _apply_terminal(
        self,
        profile_id: str,
        carrier: ReconciliationNominationCarrier,
        result: AdjudicationResult,
        dream_run_id: str,
    ) -> bool:
        now = self._policy.clock()
        winner = result.winner_node_id if result.disposition is Disposition.ACCEPTED else None
        loser = result.loser_node_id if result.disposition is Disposition.ACCEPTED else None
        factor = self._policy.downweight_factor if result.disposition is Disposition.ACCEPTED else None
        if result.disposition is Disposition.ACCEPTED and factor is None:
            raise ValueError("accepted application requires a downweight_factor; none configured")
        application = ReconciliationApplication(
            nomination_id=carrier.nomination_id,
            profile_id=profile_id,
            disposition=str(result.disposition),
            reason_code=str(result.reason_code),
            canonical_kind=carrier.canonical_kind,
            composite_group_id=carrier.composite_group_id,
            evidence_event_ids=tuple(carrier.evidence_event_ids),
            verdict=str(result.verdict),
            quality=str(result.quality),
            left_node_id=carrier.lo_node_id,
            left_version=carrier.lo_version,
            left_expected_peer_id=carrier.lo_expected_peer,
            right_node_id=carrier.hi_node_id,
            right_version=carrier.hi_version,
            right_expected_peer_id=carrier.hi_expected_peer,
            winner_node_id=winner,
            loser_node_id=loser,
            loser_prior_version=None,
            loser_new_version=None,
            applied_at=now,
        )
        return self._apply(application, carrier, dream_run_id, factor)

    def _apply(
        self,
        application: ReconciliationApplication,
        carrier: ReconciliationNominationCarrier,
        dream_run_id: str,
        factor: float | None,
    ) -> bool:
        try:
            self._graph.apply_reconciliation(application, downweight_factor=factor)
        except Exception as exc:
            logger.warning("terminal apply failed for %s: %s", carrier.nomination_id, exc)
            receipt = self._graph.get_reconciliation_receipt(
                profile_id=application.profile_id,
                nomination_id=carrier.nomination_id,
            )
            if receipt is None:
                return self._record_deferred(
                    application.profile_id,
                    carrier,
                    ReasonCode.SEAT_UNAVAILABLE,
                    dream_run_id,
                )
        return True

    def _record_deferred(
        self,
        profile_id: str,
        carrier: ReconciliationNominationCarrier,
        reason_code: ReasonCode,
        dream_run_id: str,
    ) -> bool:
        """Record the deferred disposition. The durable attempt reservation
        was taken BEFORE this call and survives any failure here: the
        budget is never refunded."""
        try:
            self._meta.audit_append(
                AuditEntry(
                    actor="dream-engine",
                    action="reconcile_deferred",
                    detail={
                        "nomination_id": carrier.nomination_id,
                        "profile_id": profile_id,
                        "reason_code": str(reason_code),
                        "disposition": str(Disposition.DEFERRED),
                    },
                    at=self._policy.clock(),
                    dedup_key=reconciliation_deferred_dedup_key(
                        carrier.nomination_id, dream_run_id, "reconcile_deferred"
                    ),
                )
            )
        except Exception as exc:
            logger.warning("deferred audit failed for %s: %s", carrier.nomination_id, exc)
            return False
        return True


def _pair_nomination(carrier: ReconciliationNominationCarrier) -> PairNomination:
    return PairNomination(
        nomination_id=carrier.nomination_id,
        profile_id=carrier.profile_id,
        canonical_kind=carrier.canonical_kind,
        composite_group_id=carrier.composite_group_id,
        source_generation=carrier.source_generation,
        left_node_id=carrier.lo_node_id,
        left_version=carrier.lo_version,
        left_expected_peer_id=carrier.lo_expected_peer,
        right_node_id=carrier.hi_node_id,
        right_version=carrier.hi_version,
        right_expected_peer_id=carrier.hi_expected_peer,
        evidence_event_ids=tuple(carrier.evidence_event_ids),
        source_channels=tuple(carrier.source_channels),
        created_at=carrier.created_at,
    )


def _observation(graph: GraphStore, node_id: str) -> EndpointObservation:
    node = graph.get_node(node_id)
    if node is None or not node.is_current:
        return EndpointObservation(
            profile_id=None,
            node_id=node_id,
            version=None,
            expected_peer_id=None,
            fate=EndpointFate.MISSING,
            content="",
        )
    return EndpointObservation(
        profile_id=node.profile_id,
        node_id=node.node_id,
        version=node.version,
        expected_peer_id=node.read_conflict_id,
        fate=EndpointFate.LIVE,
        content=str(node.props.get("statement", "")),
        never_decay=node.never_decay,
    )


__all__ = ["ConsumerPolicy", "ReconciliationConsumer", "consumer_enabled"]
