"""Admission recomputation: the derived calendar credit of one cohort.

The admission of a cohort is derived from the immutable ledger rows and is
never stored. A row earns a UTC date only when its own fields, its clock
evidence, its cohort kind, and its attempt linkage all re-derive as eligible;
the stored calendar bit is only a cross-check of that derivation, never its
source.

The recomputation is ordered: the eligible rows lose the revoked ones first,
then the lowest append order holds each UTC date, and the remaining survivors
are reported as suppressed duplicates. The clock floor of a row is the latest
collected instant of the collect rows appended before it, failed attempts and
gap rows included, so a historical row is never compared with a later global
maximum.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from mnemoseed_local.instrument.clock import (
    ClockCapture,
    check_capture_invariants,
    parse_raw,
    utc_date_from_raw,
)
from mnemoseed_local.instrument.ledger import (
    REVOCATION_REASON_CODES,
    AppendedEvent,
    ClockValueError,
    CohortRecord,
    EventRejectedError,
    LedgerEvent,
    LedgerHandle,
    LedgerMissingError,
    RunState,
    find_prior_unclosed_run_ids,
    get_cohort,
    read_events,
    read_run_state,
)

__all__ = [
    "AdmissionRow",
    "CohortAdmission",
    "REVOCATION_REASON_CODES",
    "RunState",
    "append_correction",
    "append_revocation",
    "find_prior_unclosed_run_ids",
    "read_run_state",
    "recompute_admission",
    "recompute_calendar_eligible",
]

_BOUND_CLOCK_SOURCE = "os_utc_wall"
_SEED_CLOCK_SOURCE = "seed_fixture"
_DAILY_COHORT_KIND = "daily_cohort"
_COLLECT_KIND = "collect"
_OBSERVED_OK = "observed_ok"


@dataclass(frozen=True)
class AdmissionRow:
    """One ledger row in the admission view, with the role it plays there."""

    event_id: str
    ledger_seq: int
    utc_date: str
    collected_at_raw: str
    role: str


@dataclass(frozen=True)
class CohortAdmission:
    """The derived admission of one cohort: credit, suppression, and exclusions."""

    numerator: tuple[AdmissionRow, ...]
    shadowed: tuple[AdmissionRow, ...]
    revoked: tuple[AdmissionRow, ...]
    excluded: tuple[AdmissionRow, ...]
    gap_dates: tuple[str, ...]
    revoked_no_survivor_dates: tuple[str, ...]


@dataclass(frozen=True)
class _Linkage:
    """The attempt linkage of one cohort, derived from its own rows."""

    runs: frozenset[str]
    started_attempts: frozenset[tuple[str, str]]
    gapped_attempts: frozenset[tuple[str, str]]

    def is_terminal_of_started_attempt(self, event: LedgerEvent) -> bool:
        """Return whether the row is the terminal of a started attempt of a started run."""
        attempt = (event.run_id, event.attempt_id)
        return (
            event.run_id in self.runs
            and attempt in self.started_attempts
            and attempt not in self.gapped_attempts
        )


def recompute_admission(ledger: LedgerHandle, *, cohort_id: str) -> CohortAdmission:
    """Return the derived admission of one cohort."""
    cohort = get_cohort(ledger, cohort_id=cohort_id)
    if cohort is None:
        raise LedgerMissingError({"reason": "cohort_row_missing", "cohort_id": cohort_id})
    events = read_events(ledger, cohort_id=cohort_id)
    linkage = _linkage(events)
    revoked_targets = {
        appended.event.target_event_id for appended in events if appended.event.event_type == "revocation"
    }

    eligible: list[AppendedEvent] = []
    excluded: list[AdmissionRow] = []
    prior_raw: str | None = None
    for appended in events:
        event = appended.event
        if event.event_type == "attempt_terminal":
            derived, role = _recompute_row(
                event,
                cohort,
                prior_raw=prior_raw,
                linkage_ok=linkage.is_terminal_of_started_attempt(event),
            )
            if role is not None:
                excluded.append(_admission_row(appended, role))
            elif derived == 1:
                eligible.append(appended)
        prior_raw = _advanced_floor(prior_raw, event)

    survivors = [row for row in eligible if row.event.event_id not in revoked_targets]
    withdrawn = [row for row in eligible if row.event.event_id in revoked_targets]

    holders: dict[str, AppendedEvent] = {}
    numerator: list[AdmissionRow] = []
    shadowed: list[AdmissionRow] = []
    for appended in survivors:
        date = appended.event.utc_date or ""
        if date in holders:
            shadowed.append(_admission_row(appended, "duplicate_suppressed"))
        else:
            holders[date] = appended
            numerator.append(_admission_row(appended, "numerator"))

    revoked: list[AdmissionRow] = []
    for appended in withdrawn:
        date = appended.event.utc_date or ""
        role = "revoked" if date in holders else "revoked_no_survivor"
        revoked.append(_admission_row(appended, role))

    candidate_dates = {row.event.utc_date or "" for row in eligible}
    gap_dates = sorted(candidate_dates - set(holders))
    revoked_no_survivor_dates = sorted({row.event.utc_date or "" for row in withdrawn} - set(holders))
    return CohortAdmission(
        numerator=tuple(numerator),
        shadowed=tuple(shadowed),
        revoked=tuple(revoked),
        excluded=tuple(excluded),
        gap_dates=tuple(gap_dates),
        revoked_no_survivor_dates=tuple(revoked_no_survivor_dates),
    )


def recompute_calendar_eligible(event: LedgerEvent, cohort: CohortRecord) -> tuple[int, str | None]:
    """Return the derived eligibility of one row and the diagnostic that excludes it."""
    return _recompute_row(event, cohort, prior_raw=None, linkage_ok=True)


def append_revocation(
    ledger: LedgerHandle,
    *,
    cohort_id: str,
    run_id: str,
    attempt_id: str,
    target_event_id: str,
    reason_code: str,
    payload: dict[str, Any],
) -> AppendedEvent:
    """Append one revocation that withdraws a sample row without touching that row."""
    if reason_code not in REVOCATION_REASON_CODES:
        raise EventRejectedError(
            {"stage": "state", "check": "reason_code_unlisted", "reason_code": reason_code}
        )
    return _append_targeted(
        ledger,
        event_type="revocation",
        cohort_id=cohort_id,
        run_id=run_id,
        attempt_id=attempt_id,
        target_event_id=target_event_id,
        reason_code=reason_code,
        payload=payload,
    )


def append_correction(
    ledger: LedgerHandle,
    *,
    cohort_id: str,
    run_id: str,
    attempt_id: str,
    target_event_id: str,
    reason_code: str,
    payload: dict[str, Any],
) -> AppendedEvent:
    """Append one correction that annotates a sample row without changing its credit."""
    return _append_targeted(
        ledger,
        event_type="correction",
        cohort_id=cohort_id,
        run_id=run_id,
        attempt_id=attempt_id,
        target_event_id=target_event_id,
        reason_code=reason_code,
        payload=payload,
    )


def _recompute_row(
    event: LedgerEvent,
    cohort: CohortRecord,
    *,
    prior_raw: str | None,
    linkage_ok: bool,
) -> tuple[int, str | None]:
    """Return the derived eligibility of one row and the diagnostic that excludes it."""
    if not linkage_ok:
        return (0, "binding_incomplete")
    capture_pre_raw = event.capture_pre_raw
    capture_post_raw = event.capture_post_raw
    collected_at_raw = event.collected_at_raw
    monotonic_delta_ns = event.monotonic_delta_ns
    clock_source = event.clock_source
    if (
        capture_pre_raw is None
        or capture_post_raw is None
        or collected_at_raw is None
        or monotonic_delta_ns is None
        or clock_source is None
        or event.observation_kind is None
        or event.outcome is None
        or event.utc_date is None
    ):
        return (0, "binding_incomplete")
    if clock_source == _SEED_CLOCK_SOURCE or event.observation_kind == "replay_check":
        if event.calendar_eligible == 1:
            return (0, "seed_clock_not_calendar")
        return (0, None)
    if clock_source != _BOUND_CLOCK_SOURCE:
        return (0, "clock_unbound")
    capture = ClockCapture(
        capture_pre_raw=capture_pre_raw,
        capture_post_raw=capture_post_raw,
        collected_at_raw=collected_at_raw,
        collected_at_iso=event.collected_at_iso or "",
        monotonic_delta_ns=monotonic_delta_ns,
        clock_source=clock_source,
    )
    try:
        check = check_capture_invariants(
            capture,
            prior_raw=prior_raw,
            cohort_created_at_raw=cohort.cohort_created_at_raw,
        )
        derived_date = utc_date_from_raw(collected_at_raw)
    except ClockValueError as exc:
        return (0, exc.diag_code)
    if check.invalid_codes:
        return (0, check.invalid_codes[0])
    derived = _derive_eligibility(event, cohort)
    if event.calendar_eligible != derived:
        return (0, "calendar_eligible_mismatch")
    if event.utc_date != derived_date:
        return (0, "date_derivation_mismatch")
    return (derived, None)


def _derive_eligibility(event: LedgerEvent, cohort: CohortRecord) -> int:
    """Return the derived calendar eligibility of one row from its own fields."""
    if event.observation_kind != _COLLECT_KIND:
        return 0
    if event.outcome != _OBSERVED_OK:
        return 0
    if event.clock_source != _BOUND_CLOCK_SOURCE:
        return 0
    if cohort.kind != _DAILY_COHORT_KIND:
        return 0
    return 1


def _linkage(events: list[AppendedEvent]) -> _Linkage:
    """Return the attempt linkage of the given cohort rows."""
    runs: set[str] = set()
    started: set[tuple[str, str]] = set()
    gapped: set[tuple[str, str]] = set()
    for appended in events:
        event = appended.event
        if event.event_type == "run_started":
            runs.add(event.run_id)
        elif event.event_type == "attempt_started":
            started.add((event.run_id, event.attempt_id))
        elif event.event_type == "instrument_gap":
            gapped.add((event.run_id, event.attempt_id))
    return _Linkage(
        runs=frozenset(runs),
        started_attempts=frozenset(started),
        gapped_attempts=frozenset(gapped),
    )


def _floor_candidate(event: LedgerEvent) -> str | None:
    """Return the collected instant of one row when it is real clock evidence for the floor.

    A synthetic seed instant is not a host clock reading, so it never bounds a
    later real capture.
    """
    if event.observation_kind != _COLLECT_KIND:
        return None
    if event.clock_source == _SEED_CLOCK_SOURCE:
        return None
    return event.collected_at_raw


def _later_raw(prior_raw: str | None, candidate_raw: str) -> str | None:
    """Return the later of two collected instants, keeping the prior one when unusable."""
    try:
        candidate = parse_raw(candidate_raw)
    except ClockValueError:
        return prior_raw
    if prior_raw is None:
        return candidate_raw
    try:
        prior = parse_raw(prior_raw)
    except ClockValueError:
        return candidate_raw
    return candidate_raw if candidate > prior else prior_raw


def _advanced_floor(prior_raw: str | None, event: LedgerEvent) -> str | None:
    """Return the clock floor after one row, whose own collect instant bounds the rows after it."""
    candidate = _floor_candidate(event)
    if candidate is None:
        return prior_raw
    return _later_raw(prior_raw, candidate)


def _admission_row(appended: AppendedEvent, role: str) -> AdmissionRow:
    """Return the admission view of one ledger row."""
    event = appended.event
    return AdmissionRow(
        event_id=event.event_id,
        ledger_seq=appended.ledger_seq,
        utc_date=event.utc_date or "",
        collected_at_raw=event.collected_at_raw or "",
        role=role,
    )


def _append_targeted(
    ledger: LedgerHandle,
    *,
    event_type: str,
    cohort_id: str,
    run_id: str,
    attempt_id: str,
    target_event_id: str,
    reason_code: str,
    payload: dict[str, Any],
) -> AppendedEvent:
    """Append one targeted row through the immutable ledger append path."""
    if not reason_code:
        raise EventRejectedError({"stage": "static", "field": "reason_code"})
    target = _find_event(ledger, target_event_id)
    event = LedgerEvent(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        cohort_id=cohort_id,
        run_id=run_id,
        attempt_id=attempt_id,
        recovers_attempt_id=None,
        observation_kind=None,
        outcome=None,
        diag_code=None,
        clock_source=None,
        capture_pre_raw=None,
        capture_post_raw=None,
        monotonic_delta_ns=None,
        collected_at_raw=None,
        collected_at_iso=None,
        utc_date=None,
        calendar_eligible=0,
        target_event_id=target_event_id,
        reason_code=reason_code,
        payload=_target_payload(
            event_type,
            payload,
            target_event_id=target_event_id,
            reason_code=reason_code,
            target=target,
        ),
        appended_at_raw=float.hex(time.time()),
    )
    return ledger.append(event)


def _target_payload(
    event_type: str,
    payload: dict[str, Any],
    *,
    target_event_id: str,
    reason_code: str,
    target: LedgerEvent | None,
) -> dict[str, Any]:
    """Return the payload of a targeted row, carrying the verbatim target evidence."""
    evidence = dict(payload)
    declared_target = evidence.get("target_event_id")
    if declared_target is not None and declared_target != target_event_id:
        raise EventRejectedError(
            {
                "stage": "static",
                "check": "payload_target_mismatch",
                "target_event_id": target_event_id,
            }
        )
    evidence.setdefault("event_type", event_type)
    evidence.setdefault("target_event_id", target_event_id)
    evidence.setdefault("reason_code", reason_code)
    if target is None:
        return evidence
    original = evidence.get("original_observation")
    if original is not None and not isinstance(original, dict):
        raise EventRejectedError({"stage": "static", "field": "payload"})
    copy: dict[str, Any] = dict(original) if isinstance(original, dict) else {}
    for name, value in _original_values(target).items():
        copy.setdefault(name, value)
    evidence["original_observation"] = copy
    return evidence


def _original_values(target: LedgerEvent) -> dict[str, Any]:
    """Return the verbatim observation values of a targeted row."""
    return {
        "event_id": target.event_id,
        "observation_kind": target.observation_kind,
        "outcome": target.outcome,
        "diag_code": target.diag_code,
        "clock_source": target.clock_source,
        "capture_pre_raw": target.capture_pre_raw,
        "capture_post_raw": target.capture_post_raw,
        "monotonic_delta_ns": target.monotonic_delta_ns,
        "collected_at_raw": target.collected_at_raw,
        "collected_at_iso": target.collected_at_iso,
        "utc_date": target.utc_date,
        "calendar_eligible": target.calendar_eligible,
    }


def _find_event(ledger: LedgerHandle, event_id: str) -> LedgerEvent | None:
    """Return the ledger row of one event id, or None when it is absent."""
    for appended in read_events(ledger):
        if appended.event.event_id == event_id:
            return appended.event
    return None
