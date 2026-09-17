from __future__ import annotations

import json
from dataclasses import replace

import pytest

from mnemoseed_local.config import (
    DEFAULT_DREAM_ENSEMBLE,
    DEFAULT_EXPERIENCE_CHANNEL_ENABLED,
)
from mnemoseed_local.dream.adjudicate import (
    PAIR_ADJUDICATION_PROMPT_VERSION,
    PAIR_SEAT_ROUTES,
    AdjudicationResult,
    Disposition,
    EndpointFate,
    EndpointObservation,
    MalformedAdjudicationInputError,
    PairAdjudicationInput,
    PairNomination,
    PairSeatDecision,
    Quality,
    ReasonCode,
    SeatOutcome,
    SeatStatus,
    Verdict,
    activation_for,
    adjudicate,
    parse_pair_seat_output,
    render_pair_adjudication_prompt,
)
from mnemoseed_local.dream.reflect import ReflectedTriple, ReflectionResult, Route
from mnemoseed_local.schema.stamp import CognitiveTier
from mnemoseed_local.storage.ports import TurnRange


def _nomination() -> PairNomination:
    return PairNomination(
        nomination_id="nom-1",
        profile_id="profile-1",
        canonical_kind="read_conflict",
        composite_group_id="group-1",
        source_generation=1,
        left_node_id="left",
        left_version=3,
        left_expected_peer_id="right",
        right_node_id="right",
        right_version=7,
        right_expected_peer_id="left",
        evidence_event_ids=(11, 12),
        source_channels=("read_conflict_flag",),
        created_at="2026-09-17T00:00:00Z",
    )


def _observations() -> tuple[EndpointObservation, EndpointObservation]:
    return (
        EndpointObservation(
            profile_id="profile-1",
            node_id="left",
            version=3,
            expected_peer_id="right",
            fate=EndpointFate.LIVE,
            content="User prefers light mode.",
        ),
        EndpointObservation(
            profile_id="profile-1",
            node_id="right",
            version=7,
            expected_peer_id="left",
            fate=EndpointFate.LIVE,
            content="User prefers dark mode.",
        ),
    )


def _decision(
    verdict: Verdict,
    *,
    winner: str | None = None,
    loser: str | None = None,
) -> PairSeatDecision:
    return PairSeatDecision(
        nomination_id="nom-1",
        profile_id="profile-1",
        left_node_id="left",
        left_version=3,
        right_node_id="right",
        right_version=7,
        verdict=verdict,
        winner_node_id=winner,
        loser_node_id=loser,
        target_node_ids=("left", "right"),
        evidence_event_ids=(11, 12),
    )


def _input(*outcomes: SeatOutcome, mode: str = "verify") -> PairAdjudicationInput:
    left, right = _observations()
    return PairAdjudicationInput(
        nomination=_nomination(),
        left=left,
        right=right,
        ensemble_mode=mode,
        seat_outcomes=outcomes,
    )


def _assert_deferred(result: AdjudicationResult, reason: ReasonCode) -> None:
    assert result.verdict is Verdict.INSUFFICIENT
    assert result.quality is Quality.DEGRADED
    assert result.disposition is Disposition.DEFERRED
    assert result.reason_code is reason
    assert result.winner_node_id is None
    assert result.loser_node_id is None


def _reflection(
    *, conflicts: tuple[tuple[str, str, str], ...] = (), triples: bool = False
) -> ReflectionResult:
    reflected = ()
    if triples:
        reflected = (
            ReflectedTriple(
                subject="user",
                predicate="prefers",
                object="dark mode",
                tiers=(CognitiveTier.TIER_1,),
                chunk_ids=("chunk-1",),
                turn_range=TurnRange(1, 1),
                confidence=0.8,
                route=Route.CORE,
            ),
        )
    return ReflectionResult(
        snapshot_id="snapshot-1",
        profile_id="profile-1",
        turn_range=TurnRange(1, 1),
        prompt_version="v1",
        triples=reflected,
        conflicts=conflicts,
    )


def test_exact_conflict_with_direction_is_accepted() -> None:
    decision = replace(
        _decision(Verdict.CONFLICT, winner="right", loser="left"),
        left_node_id="right",
        left_version=7,
        right_node_id="left",
        right_version=3,
        target_node_ids=("right", "left"),
        evidence_event_ids=(12, 11),
    )
    result = adjudicate(
        _input(
            SeatOutcome(
                status=SeatStatus.COMPLETE,
                decisions=(decision,),
            )
        )
    )
    assert result.disposition is Disposition.ACCEPTED
    assert result.verdict is Verdict.CONFLICT
    assert result.quality is Quality.VERIFIED
    assert result.reason_code is ReasonCode.CONFLICT_CONFIRMED
    assert (result.winner_node_id, result.loser_node_id) == ("right", "left")
    assert result.target_node_ids == ("left", "right")
    assert result.evidence_event_ids == (11, 12)


def test_exact_not_conflict_is_rejected() -> None:
    payload = {
        "nomination_id": "nom-1",
        "profile_id": "profile-1",
        "left_node_id": "left",
        "left_version": 3,
        "right_node_id": "right",
        "right_version": 7,
        "verdict": "not_conflict",
        "winner_node_id": None,
        "loser_node_id": None,
        "target_node_ids": ["left", "right"],
        "evidence_event_ids": [11, 12],
    }
    outcome = parse_pair_seat_output(json.dumps(payload))
    assert outcome.status is SeatStatus.COMPLETE
    result = adjudicate(_input(outcome))
    assert result.disposition is Disposition.REJECTED
    assert result.verdict is Verdict.NOT_CONFLICT
    assert result.quality is Quality.VERIFIED
    assert result.reason_code is ReasonCode.NOT_CONFLICT_CONFIRMED


def test_conflict_without_direction_is_deferred() -> None:
    result = adjudicate(
        _input(SeatOutcome(status=SeatStatus.COMPLETE, decisions=(_decision(Verdict.CONFLICT),)))
    )
    _assert_deferred(result, ReasonCode.CONFLICT_WITHOUT_DIRECTION)


def test_pair_mismatch_is_deferred() -> None:
    mismatches = (
        replace(_decision(Verdict.NOT_CONFLICT), target_node_ids=("left", "other")),
        replace(_decision(Verdict.NOT_CONFLICT), nomination_id="other"),
        replace(_decision(Verdict.NOT_CONFLICT), profile_id="other"),
        replace(_decision(Verdict.NOT_CONFLICT), evidence_event_ids=(11, 13)),
    )
    for decision in mismatches:
        result = adjudicate(_input(SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,))))
        _assert_deferred(result, ReasonCode.IDENTITY_MISMATCH)


def test_seat_version_mismatch_is_deferred() -> None:
    decision = replace(_decision(Verdict.NOT_CONFLICT), right_version=8)
    result = adjudicate(_input(SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,))))
    _assert_deferred(result, ReasonCode.IDENTITY_MISMATCH)


def test_current_version_mismatch_is_unresolved_stale_revision() -> None:
    left, right = _observations()
    result = adjudicate(
        PairAdjudicationInput(
            nomination=_nomination(),
            left=left,
            right=replace(right, version=8),
            ensemble_mode="verify",
            seat_outcomes=(
                SeatOutcome(status=SeatStatus.COMPLETE, decisions=(_decision(Verdict.NOT_CONFLICT),)),
            ),
        )
    )
    assert result.disposition is Disposition.UNRESOLVED
    assert result.reason_code is ReasonCode.STALE_REVISION


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (SeatOutcome(status=SeatStatus.ABSENT), ReasonCode.ABSENT_RESULT),
        (SeatOutcome(status=SeatStatus.TIMEOUT), ReasonCode.SEAT_TIMEOUT),
        (SeatOutcome(status=SeatStatus.UNAVAILABLE), ReasonCode.SEAT_UNAVAILABLE),
        (SeatOutcome(status=SeatStatus.INVALID_OUTPUT), ReasonCode.INVALID_TYPED_OUTPUT),
        (SeatOutcome(status=SeatStatus.COLLAPSE_UNRECOVERED), ReasonCode.COLLAPSE_UNRECOVERED),
        (SeatOutcome(status=SeatStatus.SINGLE_SIDE), ReasonCode.SINGLE_SIDE),
        (SeatOutcome(status=SeatStatus.SALVAGE), ReasonCode.SALVAGE),
        (SeatOutcome(status=SeatStatus.POLARITY_DROP), ReasonCode.POLARITY_DROP),
        (SeatOutcome(status=SeatStatus.IDENTITY_MISSING), ReasonCode.IDENTITY_MISMATCH),
        (SeatOutcome(status=SeatStatus.EMPTY_RESPONSE), ReasonCode.EMPTY_RESPONSE),
        (SeatOutcome(status=SeatStatus.DUPLICATE_RESPONSE), ReasonCode.DUPLICATE_RESPONSE),
        (SeatOutcome(status=SeatStatus.EXTRA_RESPONSE), ReasonCode.EXTRA_RESPONSE),
        (SeatOutcome(status=SeatStatus.BOTH_SUPPORTED), ReasonCode.BOTH_SUPPORTED_WITHOUT_VERDICT),
    ],
)
def test_forced_deferred_seat_shapes(outcome: SeatOutcome, reason: ReasonCode) -> None:
    _assert_deferred(adjudicate(_input(outcome)), reason)
    _assert_deferred(adjudicate(_input(outcome, mode="surprise")), ReasonCode.UNEXPECTED_ENSEMBLE_MODE)

    parsed = parse_pair_seat_output("not json")
    assert parsed.status is SeatStatus.INVALID_OUTPUT
    assert parse_pair_seat_output("").status is SeatStatus.EMPTY_RESPONSE
    assert parse_pair_seat_output("[]").status is SeatStatus.EMPTY_RESPONSE
    assert parse_pair_seat_output('[{}, {"extra": true}]').status is SeatStatus.EXTRA_RESPONSE
    assert parse_pair_seat_output("{}").status is SeatStatus.IDENTITY_MISSING
    assert parse_pair_seat_output("[{}]").status is SeatStatus.INVALID_OUTPUT
    duplicate_key = '{"verdict":"conflict","verdict":"not_conflict"}'
    assert parse_pair_seat_output(duplicate_key).status is SeatStatus.INVALID_OUTPUT
    if reason is ReasonCode.SALVAGE:
        routed = SeatOutcome(
            status=SeatStatus.COMPLETE,
            decisions=(_decision(Verdict.NOT_CONFLICT),),
            routes=(Route.SALVAGE,),
        )
        _assert_deferred(adjudicate(_input(routed)), ReasonCode.SALVAGE)


def test_bare_conflict_tuple_without_identity_is_deferred() -> None:
    outcome = SeatOutcome(
        status=SeatStatus.COMPLETE,
        reflection_result=_reflection(conflicts=(("user", "prefers", "dark mode"),)),
    )
    _assert_deferred(adjudicate(_input(outcome)), ReasonCode.LEGACY_DIAGNOSTIC_ONLY)


def test_surviving_triples_alone_cannot_dispose() -> None:
    outcome = SeatOutcome(status=SeatStatus.COMPLETE, reflection_result=_reflection(triples=True))
    _assert_deferred(adjudicate(_input(outcome)), ReasonCode.LEGACY_DIAGNOSTIC_ONLY)


def test_both_supported_without_explicit_not_conflict_is_deferred() -> None:
    outcome = SeatOutcome(status=SeatStatus.BOTH_SUPPORTED)
    _assert_deferred(adjudicate(_input(outcome)), ReasonCode.BOTH_SUPPORTED_WITHOUT_VERDICT)


def test_protected_endpoint_is_terminal_unresolved_without_store_touch() -> None:
    class BombStore:
        calls = 0

        def observe(self) -> None:
            self.calls += 1
            raise AssertionError("store must not be touched")

    bomb = BombStore()
    left, right = _observations()
    result = adjudicate(
        PairAdjudicationInput(
            nomination=_nomination(),
            left=replace(left, never_decay=True),
            right=right,
            ensemble_mode="verify",
            seat_outcomes=(SeatOutcome(status=SeatStatus.TIMEOUT),),
        )
    )
    assert result.disposition is Disposition.UNRESOLVED
    assert result.verdict is Verdict.INSUFFICIENT
    assert result.quality is Quality.DEGRADED
    assert result.reason_code is ReasonCode.PROTECTED_ENDPOINT
    assert result.winner_node_id is None and result.loser_node_id is None
    assert bomb.calls == 0
    for changed_right in (
        replace(right, version=8),
        replace(right, expected_peer_id="new-peer"),
        replace(right, fate=EndpointFate.MISSING, version=None, expected_peer_id=None),
    ):
        protected = adjudicate(
            PairAdjudicationInput(
                nomination=_nomination(),
                left=replace(left, never_decay=True),
                right=changed_right,
                ensemble_mode="verify",
                seat_outcomes=(),
            )
        )
        assert protected.reason_code is ReasonCode.PROTECTED_ENDPOINT


def test_missing_closed_and_tombstoned_endpoints_are_unresolved() -> None:
    left, right = _observations()
    expected = {
        EndpointFate.MISSING: ReasonCode.MISSING_ENDPOINT,
        EndpointFate.CLOSED: ReasonCode.CLOSED_ENDPOINT,
        EndpointFate.TOMBSTONED: ReasonCode.TOMBSTONED_ENDPOINT,
    }
    for fate, reason in expected.items():
        result = adjudicate(
            PairAdjudicationInput(
                nomination=_nomination(),
                left=replace(left, fate=fate),
                right=right,
                ensemble_mode="verify",
                seat_outcomes=(),
            )
        )
        assert result.disposition is Disposition.UNRESOLVED
        assert result.reason_code is reason


def test_malformed_structural_input_raises_typed_error() -> None:
    left, right = _observations()
    with pytest.raises(MalformedAdjudicationInputError):
        adjudicate(
            PairAdjudicationInput(
                nomination=replace(_nomination(), right_node_id="left"),
                left=left,
                right=right,
                ensemble_mode="verify",
                seat_outcomes=(),
            )
        )
    impossible_inputs = (
        PairAdjudicationInput(
            nomination=_nomination(),
            left=left,
            right=right,
            ensemble_mode=[],  # type: ignore[arg-type]
            seat_outcomes=(),
        ),
        PairAdjudicationInput(
            nomination=_nomination(),
            left=left,
            right=right,
            ensemble_mode="verify",
            seat_outcomes=[],  # type: ignore[arg-type]
        ),
        PairAdjudicationInput(
            nomination=_nomination(),
            left=left,
            right=right,
            ensemble_mode="verify",
            seat_outcomes=(SeatOutcome(status="complete"),),  # type: ignore[arg-type]
        ),
    )
    for impossible in impossible_inputs:
        with pytest.raises(MalformedAdjudicationInputError):
            adjudicate(impossible)
    malformed = (
        replace(_nomination(), profile_id=1),  # type: ignore[arg-type]
        replace(_nomination(), left_version=True),
        replace(_nomination(), evidence_event_ids=(0,)),
        replace(_nomination(), source_channels=("",)),
    )
    for nomination in malformed:
        with pytest.raises(MalformedAdjudicationInputError):
            adjudicate(
                PairAdjudicationInput(
                    nomination=nomination,
                    left=left,
                    right=right,
                    ensemble_mode="verify",
                    seat_outcomes=(),
                )
            )
    with pytest.raises(MalformedAdjudicationInputError):
        adjudicate(
            PairAdjudicationInput(
                nomination=_nomination(),
                left=replace(left, fate="live"),  # type: ignore[arg-type]
                right=right,
                ensemble_mode="verify",
                seat_outcomes=(),
            )
        )


def test_vote_requires_two_matching_pair_decisions() -> None:
    decision = _decision(Verdict.CONFLICT, winner="right", loser="left")
    one = adjudicate(_input(SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,)), mode="vote"))
    _assert_deferred(one, ReasonCode.ABSENT_RESULT)

    disagree = adjudicate(
        _input(
            SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,)),
            SeatOutcome(
                status=SeatStatus.COMPLETE,
                decisions=(_decision(Verdict.NOT_CONFLICT),),
            ),
            mode="vote",
        )
    )
    _assert_deferred(disagree, ReasonCode.SEAT_DISAGREEMENT)

    matching = adjudicate(
        _input(
            SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,)),
            SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,)),
            mode="vote",
        )
    )
    assert matching.disposition is Disposition.ACCEPTED
    assert matching.quality is Quality.VOTED


def test_ensemble_off_has_no_active_pair_seat() -> None:
    activation = activation_for("off")
    assert activation.active is False
    assert activation.routes == ()
    assert activation.reason_code is ReasonCode.ENSEMBLE_OFF
    _assert_deferred(adjudicate(_input(mode="off")), ReasonCode.ENSEMBLE_OFF)
    assert PAIR_SEAT_ROUTES["verify"] == ("dream_verifier",)
    assert PAIR_SEAT_ROUTES["vote"] == ("dream", "dream_vote", "dream_verifier")
    assert PAIR_ADJUDICATION_PROMPT_VERSION
    rendered = render_pair_adjudication_prompt(_input())
    assert rendered == render_pair_adjudication_prompt(_input())
    assert "nom-1" in rendered and "left@3" in rendered and "right@7" in rendered
    assert "User prefers light mode." in rendered and "User prefers dark mode." in rendered
    assert '"evidence_event_ids":[11,12]' in rendered
    with pytest.raises(MalformedAdjudicationInputError):
        render_pair_adjudication_prompt(_nomination())  # type: ignore[arg-type]
    left, right = _observations()
    for invalid_left in (replace(left, content=""), replace(left, version=4)):
        with pytest.raises(MalformedAdjudicationInputError):
            render_pair_adjudication_prompt(
                PairAdjudicationInput(
                    nomination=_nomination(),
                    left=invalid_left,
                    right=right,
                    ensemble_mode="verify",
                    seat_outcomes=(),
                )
            )
    assert DEFAULT_DREAM_ENSEMBLE == "off"
    assert DEFAULT_EXPERIENCE_CHANNEL_ENABLED is False
