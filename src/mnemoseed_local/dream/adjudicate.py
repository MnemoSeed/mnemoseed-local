"""Pair-scoped typed reconciliation adjudication.

adjudicate() is pure and total over runtime seat failures. It raises
MalformedAdjudicationInputError only for structurally impossible caller
inputs; malformed or incomplete seat output returns
insufficient/degraded/deferred, never raises.

Exact means equality of nomination ID, profile ID, target node set,
node-bound versions, and evidence-event set. Endpoint ordering is
irrelevant; node/version association is not.

Seat-reported identity/version mismatch while the captured endpoints remain
live is deferred(identity_mismatch). Store-observed endpoint, version, or
expected-peer drift from the carrier is terminal
unresolved(stale_revision).

If either live endpoint has never_decay=True, protected_endpoint takes
precedence over all seat outcomes; no model invocation, mutation, or marker
clear is authorized.

S-B freezes prompt rendering, strict parsing, logical route selection, and
activation classification only. S-D alone may read configuration,
resolve/call models, schedule retries, or wire the consumer.

Legacy ReflectionResult.conflicts and surviving triples are diagnostic
inputs only. They may force deferred but can never produce accepted or
rejected without an exact typed pair-seat decision.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from mnemoseed_local.dream.reflect import ReflectionResult, Route
from mnemoseed_local.storage.ports import (
    CANONICAL_NOMINATION_KINDS,
    Disposition,
    ReasonCode,
)

PAIR_ADJUDICATION_PROMPT_VERSION: Final = "v1"

PAIR_ADJUDICATION_SYSTEM_PROMPT: Final = """\
You are the pair adjudication seat of a provenance-preserving memory engine.
Judge only the nominated pair and immutable evidence identified in the request.
Return exactly one JSON object with these exact fields: nomination_id,
profile_id, left_node_id, left_version, right_node_id, right_version, verdict,
winner_node_id, loser_node_id, target_node_ids, evidence_event_ids.
verdict is conflict, not_conflict, or insufficient. conflict requires an explicit
winner and loser from the target pair. Do not output markdown or extra fields.
"""

PAIR_SEAT_ROUTES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "verify": ("dream_verifier",),
        "vote": ("dream", "dream_vote", "dream_verifier"),
    }
)


class Verdict(StrEnum):
    CONFLICT = "conflict"
    NOT_CONFLICT = "not_conflict"
    INSUFFICIENT = "insufficient"


class Quality(StrEnum):
    VERIFIED = "verified"
    VOTED = "voted"
    DEGRADED = "degraded"


class SeatStatus(StrEnum):
    COMPLETE = "complete"
    ABSENT = "absent"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_OUTPUT = "invalid_output"
    COLLAPSE_UNRECOVERED = "collapse_unrecovered"
    SINGLE_SIDE = "single_side"
    SALVAGE = "salvage"
    POLARITY_DROP = "polarity_drop"
    IDENTITY_MISSING = "identity_missing"
    EMPTY_RESPONSE = "empty_response"
    DUPLICATE_RESPONSE = "duplicate_response"
    EXTRA_RESPONSE = "extra_response"
    BOTH_SUPPORTED = "both_supported"


class EndpointFate(StrEnum):
    LIVE = "live"
    MISSING = "missing"
    CLOSED = "closed"
    TOMBSTONED = "tombstoned"


class MalformedAdjudicationInputError(ValueError):
    """The caller supplied a structurally impossible pair carrier."""


@dataclass(frozen=True)
class PairNomination:
    nomination_id: str
    profile_id: str
    canonical_kind: str
    composite_group_id: str
    source_generation: int
    left_node_id: str
    left_version: int
    left_expected_peer_id: str
    right_node_id: str
    right_version: int
    right_expected_peer_id: str
    evidence_event_ids: tuple[int, ...]
    source_channels: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class EndpointObservation:
    profile_id: str | None
    node_id: str
    version: int | None
    expected_peer_id: str | None
    fate: EndpointFate
    content: str
    never_decay: bool = False


@dataclass(frozen=True)
class PairSeatDecision:
    nomination_id: str
    profile_id: str
    left_node_id: str
    left_version: int
    right_node_id: str
    right_version: int
    verdict: Verdict
    winner_node_id: str | None
    loser_node_id: str | None
    target_node_ids: tuple[str, str]
    evidence_event_ids: tuple[int, ...]


@dataclass(frozen=True)
class SeatOutcome:
    status: SeatStatus
    decisions: tuple[PairSeatDecision, ...] = ()
    reflection_result: ReflectionResult | None = None
    routes: tuple[Route, ...] = ()


@dataclass(frozen=True)
class PairAdjudicationInput:
    nomination: PairNomination
    left: EndpointObservation
    right: EndpointObservation
    ensemble_mode: str
    seat_outcomes: tuple[SeatOutcome, ...]


@dataclass(frozen=True)
class AdjudicationResult:
    nomination_id: str
    profile_id: str
    left_node_id: str
    left_version: int
    right_node_id: str
    right_version: int
    verdict: Verdict
    winner_node_id: str | None
    loser_node_id: str | None
    target_node_ids: tuple[str, str]
    evidence_event_ids: tuple[int, ...]
    quality: Quality
    reason_code: ReasonCode
    disposition: Disposition


@dataclass(frozen=True)
class SeatActivation:
    active: bool
    routes: tuple[str, ...]
    disposition: Disposition
    reason_code: ReasonCode


_STATUS_REASONS: Final[Mapping[SeatStatus, ReasonCode]] = MappingProxyType(
    {
        SeatStatus.ABSENT: ReasonCode.ABSENT_RESULT,
        SeatStatus.TIMEOUT: ReasonCode.SEAT_TIMEOUT,
        SeatStatus.UNAVAILABLE: ReasonCode.SEAT_UNAVAILABLE,
        SeatStatus.INVALID_OUTPUT: ReasonCode.INVALID_TYPED_OUTPUT,
        SeatStatus.COLLAPSE_UNRECOVERED: ReasonCode.COLLAPSE_UNRECOVERED,
        SeatStatus.SINGLE_SIDE: ReasonCode.SINGLE_SIDE,
        SeatStatus.SALVAGE: ReasonCode.SALVAGE,
        SeatStatus.POLARITY_DROP: ReasonCode.POLARITY_DROP,
        SeatStatus.IDENTITY_MISSING: ReasonCode.IDENTITY_MISMATCH,
        SeatStatus.EMPTY_RESPONSE: ReasonCode.EMPTY_RESPONSE,
        SeatStatus.DUPLICATE_RESPONSE: ReasonCode.DUPLICATE_RESPONSE,
        SeatStatus.EXTRA_RESPONSE: ReasonCode.EXTRA_RESPONSE,
        SeatStatus.BOTH_SUPPORTED: ReasonCode.BOTH_SUPPORTED_WITHOUT_VERDICT,
    }
)

_FATE_REASONS: Final[Mapping[EndpointFate, ReasonCode]] = MappingProxyType(
    {
        EndpointFate.MISSING: ReasonCode.MISSING_ENDPOINT,
        EndpointFate.CLOSED: ReasonCode.CLOSED_ENDPOINT,
        EndpointFate.TOMBSTONED: ReasonCode.TOMBSTONED_ENDPOINT,
    }
)

_DECISION_FIELDS: Final = frozenset(
    {
        "nomination_id",
        "profile_id",
        "left_node_id",
        "left_version",
        "right_node_id",
        "right_version",
        "verdict",
        "winner_node_id",
        "loser_node_id",
        "target_node_ids",
        "evidence_event_ids",
    }
)

_DECISION_IDENTITY_FIELDS: Final = frozenset(
    {
        "nomination_id",
        "profile_id",
        "left_node_id",
        "left_version",
        "right_node_id",
        "right_version",
        "target_node_ids",
        "evidence_event_ids",
    }
)


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise _DuplicateKeyError(key)
        parsed[key] = value
    return parsed


def activation_for(ensemble_mode: str) -> SeatActivation:
    """Classify logical pair-seat activation without reading configuration."""
    if ensemble_mode == "off":
        return SeatActivation(False, (), Disposition.DEFERRED, ReasonCode.ENSEMBLE_OFF)
    routes = PAIR_SEAT_ROUTES.get(ensemble_mode)
    if routes is None:
        return SeatActivation(False, (), Disposition.DEFERRED, ReasonCode.UNEXPECTED_ENSEMBLE_MODE)
    return SeatActivation(True, routes, Disposition.DEFERRED, ReasonCode.INSUFFICIENT)


def render_pair_adjudication_prompt(input: PairAdjudicationInput) -> str:
    """Render the immutable pair identity and evidence group deterministically."""
    _validate_input_structure(input)
    nomination = input.nomination
    if (
        input.left.fate is not EndpointFate.LIVE
        or input.right.fate is not EndpointFate.LIVE
        or not _observations_match(nomination, input.left, input.right)
    ):
        raise MalformedAdjudicationInputError("prompt endpoints must exactly match the live nomination pair")
    observed = {input.left.node_id: input.left, input.right.node_id: input.right}
    left_content = observed[nomination.left_node_id]
    right_content = observed[nomination.right_node_id]
    if not left_content.content.strip() or not right_content.content.strip():
        raise MalformedAdjudicationInputError("prompt endpoints must carry non-empty content")
    payload = {
        "canonical_kind": nomination.canonical_kind,
        "evidence_event_ids": list(nomination.evidence_event_ids),
        "left": {
            "content": left_content.content,
            "target": f"{nomination.left_node_id}@{nomination.left_version}",
        },
        "nomination_id": nomination.nomination_id,
        "profile_id": nomination.profile_id,
        "right": {
            "content": right_content.content,
            "target": f"{nomination.right_node_id}@{nomination.right_version}",
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def parse_pair_seat_output(text: str) -> SeatOutcome:
    """Strictly parse one exact typed pair decision, returning typed degradation."""
    if not isinstance(text, str):
        return SeatOutcome(status=SeatStatus.INVALID_OUTPUT)
    if not text.strip():
        return SeatOutcome(status=SeatStatus.EMPTY_RESPONSE)
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, TypeError, _DuplicateKeyError):
        return SeatOutcome(status=SeatStatus.INVALID_OUTPUT)
    if isinstance(raw, list):
        if not raw:
            return SeatOutcome(status=SeatStatus.EMPTY_RESPONSE)
        if len(raw) > 1:
            status = (
                SeatStatus.DUPLICATE_RESPONSE
                if all(item == raw[0] for item in raw)
                else SeatStatus.EXTRA_RESPONSE
            )
            return SeatOutcome(status=status)
        return SeatOutcome(status=SeatStatus.INVALID_OUTPUT)
    if not isinstance(raw, dict):
        return SeatOutcome(status=SeatStatus.INVALID_OUTPUT)
    fields = set(raw)
    if not _DECISION_IDENTITY_FIELDS <= fields:
        return SeatOutcome(status=SeatStatus.IDENTITY_MISSING)
    if fields != _DECISION_FIELDS:
        return SeatOutcome(status=SeatStatus.INVALID_OUTPUT)
    try:
        decision = _decision_from_mapping(raw)
    except (KeyError, TypeError, ValueError):
        return SeatOutcome(status=SeatStatus.INVALID_OUTPUT)
    return SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,))


def _decision_from_mapping(raw: dict[str, Any]) -> PairSeatDecision:
    string_fields = ("nomination_id", "profile_id", "left_node_id", "right_node_id")
    if any(not isinstance(raw[name], str) or not raw[name] for name in string_fields):
        raise ValueError("identity strings are required")
    for name in ("left_version", "right_version"):
        if isinstance(raw[name], bool) or not isinstance(raw[name], int):
            raise TypeError("versions must be integers")
    winner = raw["winner_node_id"]
    loser = raw["loser_node_id"]
    if winner is not None and not isinstance(winner, str):
        raise TypeError("winner must be a string or null")
    if loser is not None and not isinstance(loser, str):
        raise TypeError("loser must be a string or null")
    targets = raw["target_node_ids"]
    evidence = raw["evidence_event_ids"]
    if not isinstance(targets, list) or len(targets) != 2 or not all(isinstance(v, str) for v in targets):
        raise TypeError("target_node_ids must contain two strings")
    if not isinstance(evidence, list) or not evidence or not all(_is_int(v) for v in evidence):
        raise TypeError("evidence_event_ids must contain integers")
    return PairSeatDecision(
        nomination_id=raw["nomination_id"],
        profile_id=raw["profile_id"],
        left_node_id=raw["left_node_id"],
        left_version=raw["left_version"],
        right_node_id=raw["right_node_id"],
        right_version=raw["right_version"],
        verdict=Verdict(raw["verdict"]),
        winner_node_id=winner,
        loser_node_id=loser,
        target_node_ids=(targets[0], targets[1]),
        evidence_event_ids=tuple(evidence),
    )


def adjudicate(input: PairAdjudicationInput) -> AdjudicationResult:
    """Apply the pair identity, endpoint-fate, and logical quality gates."""
    _validate_input_structure(input)
    nomination = input.nomination

    if _has_protected_endpoint(nomination, input.left, input.right):
        return _result(nomination, Disposition.UNRESOLVED, ReasonCode.PROTECTED_ENDPOINT)
    fate_reason = _endpoint_fate_reason(input.left, input.right)
    if fate_reason is not None:
        return _result(nomination, Disposition.UNRESOLVED, fate_reason)
    if not _observations_match(nomination, input.left, input.right):
        return _result(nomination, Disposition.UNRESOLVED, ReasonCode.STALE_REVISION)

    activation = activation_for(input.ensemble_mode)
    if not activation.active:
        return _result(nomination, Disposition.DEFERRED, activation.reason_code)

    try:
        decisions, failure = _complete_decisions(input)
        if failure is not None:
            return _result(nomination, Disposition.DEFERRED, failure)
        if any(not _identity_matches(nomination, decision) for decision in decisions):
            return _result(nomination, Disposition.DEFERRED, ReasonCode.IDENTITY_MISMATCH)
        if input.ensemble_mode == "vote" and _semantic_key(decisions[0]) != _semantic_key(decisions[1]):
            return _result(nomination, Disposition.DEFERRED, ReasonCode.SEAT_DISAGREEMENT)
    except (AttributeError, TypeError, ValueError):
        return _result(nomination, Disposition.DEFERRED, ReasonCode.INVALID_TYPED_OUTPUT)

    decision = decisions[0]
    quality = Quality.VOTED if input.ensemble_mode == "vote" else Quality.VERIFIED
    if decision.verdict is Verdict.CONFLICT:
        if not _has_direction(nomination, decision):
            return _result(nomination, Disposition.DEFERRED, ReasonCode.CONFLICT_WITHOUT_DIRECTION)
        return _result(
            nomination,
            Disposition.ACCEPTED,
            ReasonCode.CONFLICT_CONFIRMED,
            verdict=Verdict.CONFLICT,
            quality=quality,
            winner=decision.winner_node_id,
            loser=decision.loser_node_id,
        )
    if decision.verdict is Verdict.NOT_CONFLICT:
        if decision.winner_node_id is not None or decision.loser_node_id is not None:
            return _result(nomination, Disposition.DEFERRED, ReasonCode.INVALID_TYPED_OUTPUT)
        return _result(
            nomination,
            Disposition.REJECTED,
            ReasonCode.NOT_CONFLICT_CONFIRMED,
            verdict=Verdict.NOT_CONFLICT,
            quality=quality,
        )
    return _result(nomination, Disposition.DEFERRED, ReasonCode.INSUFFICIENT)


def _validate_structure(nomination: PairNomination) -> None:
    required = (
        ("nomination_id", nomination.nomination_id),
        ("profile_id", nomination.profile_id),
        ("canonical_kind", nomination.canonical_kind),
        ("composite_group_id", nomination.composite_group_id),
        ("left_node_id", nomination.left_node_id),
        ("left_expected_peer_id", nomination.left_expected_peer_id),
        ("right_node_id", nomination.right_node_id),
        ("right_expected_peer_id", nomination.right_expected_peer_id),
        ("created_at", nomination.created_at),
    )
    for label, value in required:
        if not isinstance(value, str) or not value.strip():
            raise MalformedAdjudicationInputError(f"nomination {label} must be a non-empty string")
    if nomination.canonical_kind not in CANONICAL_NOMINATION_KINDS:
        raise MalformedAdjudicationInputError("canonical kind is outside the closed vocabulary")
    if nomination.left_node_id == nomination.right_node_id:
        raise MalformedAdjudicationInputError("pair endpoints must be distinct")
    if (
        nomination.left_expected_peer_id != nomination.right_node_id
        or nomination.right_expected_peer_id != nomination.left_node_id
    ):
        raise MalformedAdjudicationInputError("pair expected-peer bindings must be reciprocal")
    numbers = (nomination.left_version, nomination.right_version, nomination.source_generation)
    if any(not _is_int(value) or value < 1 for value in numbers):
        raise MalformedAdjudicationInputError("versions and source generation must be positive")
    evidence = nomination.evidence_event_ids
    if (
        not isinstance(evidence, tuple)
        or not evidence
        or any(not _is_int(value) or value < 1 for value in evidence)
        or len(set(evidence)) != len(evidence)
    ):
        raise MalformedAdjudicationInputError("evidence event identity must be non-empty and unique")
    channels = nomination.source_channels
    if (
        not isinstance(channels, tuple)
        or not channels
        or any(not isinstance(value, str) or not value.strip() for value in channels)
    ):
        raise MalformedAdjudicationInputError("source channels must contain non-empty strings")


def _validate_input_structure(input: PairAdjudicationInput) -> None:
    if not isinstance(input, PairAdjudicationInput):
        raise MalformedAdjudicationInputError("adjudication input must be PairAdjudicationInput")
    if not isinstance(input.nomination, PairNomination):
        raise MalformedAdjudicationInputError("nomination must be PairNomination")
    if not isinstance(input.left, EndpointObservation) or not isinstance(input.right, EndpointObservation):
        raise MalformedAdjudicationInputError("endpoint observations must be typed")
    if not isinstance(input.ensemble_mode, str):
        raise MalformedAdjudicationInputError("ensemble mode must be a string")
    if not isinstance(input.seat_outcomes, tuple):
        raise MalformedAdjudicationInputError("seat outcomes must be a tuple")
    for outcome in input.seat_outcomes:
        if not isinstance(outcome, SeatOutcome):
            raise MalformedAdjudicationInputError("seat outcomes must be typed")
        if not isinstance(outcome.status, SeatStatus):
            raise MalformedAdjudicationInputError("seat status must be typed")
        if not isinstance(outcome.decisions, tuple) or not all(
            isinstance(decision, PairSeatDecision) for decision in outcome.decisions
        ):
            raise MalformedAdjudicationInputError("seat decisions must be a typed tuple")
        if outcome.reflection_result is not None and not isinstance(
            outcome.reflection_result, ReflectionResult
        ):
            raise MalformedAdjudicationInputError("reflection diagnostic must be typed")
        if not isinstance(outcome.routes, tuple) or not all(
            isinstance(route, Route) for route in outcome.routes
        ):
            raise MalformedAdjudicationInputError("seat routes must be a typed tuple")
    _validate_structure(input.nomination)
    _validate_observation(input.left)
    _validate_observation(input.right)
    if input.left.node_id == input.right.node_id:
        raise MalformedAdjudicationInputError("endpoint observations must name distinct nodes")


def _has_protected_endpoint(
    nomination: PairNomination,
    left: EndpointObservation,
    right: EndpointObservation,
) -> bool:
    targets = {nomination.left_node_id, nomination.right_node_id}
    return any(
        endpoint.fate is EndpointFate.LIVE
        and endpoint.never_decay
        and endpoint.profile_id == nomination.profile_id
        and endpoint.node_id in targets
        for endpoint in (left, right)
    )


def _validate_observation(endpoint: EndpointObservation) -> None:
    if not isinstance(endpoint.fate, EndpointFate):
        raise MalformedAdjudicationInputError("endpoint fate must be typed")
    if not isinstance(endpoint.node_id, str) or not endpoint.node_id.strip():
        raise MalformedAdjudicationInputError("endpoint node_id must be a non-empty string")
    if not isinstance(endpoint.content, str) or not isinstance(endpoint.never_decay, bool):
        raise MalformedAdjudicationInputError("endpoint content and protection flag must be typed")
    if endpoint.fate is EndpointFate.LIVE:
        if not isinstance(endpoint.profile_id, str) or not endpoint.profile_id.strip():
            raise MalformedAdjudicationInputError("live endpoint profile_id must be a non-empty string")
        if (
            not isinstance(endpoint.version, int)
            or isinstance(endpoint.version, bool)
            or endpoint.version < 1
        ):
            raise MalformedAdjudicationInputError("live endpoint version must be positive")
        if endpoint.expected_peer_id is not None and (
            not isinstance(endpoint.expected_peer_id, str) or not endpoint.expected_peer_id.strip()
        ):
            raise MalformedAdjudicationInputError(
                "live endpoint expected peer must be a non-empty string or None"
            )


def _endpoint_fate_reason(left: EndpointObservation, right: EndpointObservation) -> ReasonCode | None:
    for endpoint in (left, right):
        if endpoint.fate is not EndpointFate.LIVE:
            return _FATE_REASONS[endpoint.fate]
    return None


def _observations_match(
    nomination: PairNomination,
    left: EndpointObservation,
    right: EndpointObservation,
) -> bool:
    expected = {
        nomination.left_node_id: (
            nomination.profile_id,
            nomination.left_version,
            nomination.left_expected_peer_id,
        ),
        nomination.right_node_id: (
            nomination.profile_id,
            nomination.right_version,
            nomination.right_expected_peer_id,
        ),
    }
    observed = {
        left.node_id: (left.profile_id, left.version, left.expected_peer_id),
        right.node_id: (right.profile_id, right.version, right.expected_peer_id),
    }
    return observed == expected


def _complete_decisions(
    input: PairAdjudicationInput,
) -> tuple[tuple[PairSeatDecision, ...], ReasonCode | None]:
    expected_seats = 1 if input.ensemble_mode == "verify" else 2
    if len(input.seat_outcomes) < expected_seats:
        return (), ReasonCode.ABSENT_RESULT
    if len(input.seat_outcomes) > expected_seats:
        return (), ReasonCode.EXTRA_RESPONSE
    decisions: list[PairSeatDecision] = []
    for outcome in input.seat_outcomes:
        reason = _STATUS_REASONS.get(outcome.status)
        if reason is not None:
            return (), reason
        if outcome.status is not SeatStatus.COMPLETE:
            return (), ReasonCode.INVALID_TYPED_OUTPUT
        if len(outcome.decisions) > 1:
            reason = (
                ReasonCode.DUPLICATE_RESPONSE
                if len(set(outcome.decisions)) == 1
                else ReasonCode.EXTRA_RESPONSE
            )
            return (), reason
        if not outcome.decisions:
            if outcome.reflection_result is not None or outcome.routes:
                return (), ReasonCode.LEGACY_DIAGNOSTIC_ONLY
            return (), ReasonCode.EMPTY_RESPONSE
        if Route.SALVAGE in outcome.routes or (
            outcome.reflection_result is not None
            and any(triple.route is Route.SALVAGE for triple in outcome.reflection_result.triples)
        ):
            return (), ReasonCode.SALVAGE
        if outcome.reflection_result is not None and outcome.reflection_result.conflicts:
            return (), ReasonCode.POLARITY_DROP
        decisions.append(outcome.decisions[0])
    return tuple(decisions), None


def _identity_matches(nomination: PairNomination, decision: PairSeatDecision) -> bool:
    expected_versions = {
        nomination.left_node_id: nomination.left_version,
        nomination.right_node_id: nomination.right_version,
    }
    reported_versions = {
        decision.left_node_id: decision.left_version,
        decision.right_node_id: decision.right_version,
    }
    return (
        decision.nomination_id == nomination.nomination_id
        and decision.profile_id == nomination.profile_id
        and set(decision.target_node_ids) == set(expected_versions)
        and len(set(decision.target_node_ids)) == 2
        and reported_versions == expected_versions
        and set(decision.evidence_event_ids) == set(nomination.evidence_event_ids)
        and len(decision.evidence_event_ids) == len(nomination.evidence_event_ids)
    )


def _semantic_key(decision: PairSeatDecision) -> tuple[Any, ...]:
    versions = tuple(
        sorted(
            ((decision.left_node_id, decision.left_version), (decision.right_node_id, decision.right_version))
        )
    )
    return (
        decision.nomination_id,
        decision.profile_id,
        versions,
        decision.verdict,
        decision.winner_node_id,
        decision.loser_node_id,
        frozenset(decision.target_node_ids),
        frozenset(decision.evidence_event_ids),
    )


def _has_direction(nomination: PairNomination, decision: PairSeatDecision) -> bool:
    return (
        isinstance(decision.winner_node_id, str)
        and isinstance(decision.loser_node_id, str)
        and decision.winner_node_id != decision.loser_node_id
        and {decision.winner_node_id, decision.loser_node_id}
        == {nomination.left_node_id, nomination.right_node_id}
    )


def _result(
    nomination: PairNomination,
    disposition: Disposition,
    reason: ReasonCode,
    *,
    verdict: Verdict = Verdict.INSUFFICIENT,
    quality: Quality = Quality.DEGRADED,
    winner: str | None = None,
    loser: str | None = None,
) -> AdjudicationResult:
    return AdjudicationResult(
        nomination_id=nomination.nomination_id,
        profile_id=nomination.profile_id,
        left_node_id=nomination.left_node_id,
        left_version=nomination.left_version,
        right_node_id=nomination.right_node_id,
        right_version=nomination.right_version,
        verdict=verdict,
        winner_node_id=winner,
        loser_node_id=loser,
        target_node_ids=(nomination.left_node_id, nomination.right_node_id),
        evidence_event_ids=nomination.evidence_event_ids,
        quality=quality,
        reason_code=reason,
        disposition=disposition,
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = [
    "PAIR_ADJUDICATION_PROMPT_VERSION",
    "PAIR_ADJUDICATION_SYSTEM_PROMPT",
    "PAIR_SEAT_ROUTES",
    "AdjudicationResult",
    "Disposition",
    "EndpointFate",
    "EndpointObservation",
    "MalformedAdjudicationInputError",
    "PairAdjudicationInput",
    "PairNomination",
    "PairSeatDecision",
    "Quality",
    "ReasonCode",
    "SeatActivation",
    "SeatOutcome",
    "SeatStatus",
    "Verdict",
    "activation_for",
    "adjudicate",
    "parse_pair_seat_output",
    "render_pair_adjudication_prompt",
]
