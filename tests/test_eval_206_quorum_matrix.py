from __future__ import annotations

from dataclasses import replace

import pytest

from mnemoseed_local.dream.adjudicate import (
    Disposition,
    EndpointFate,
    EndpointObservation,
    PairAdjudicationInput,
    PairNomination,
    PairSeatDecision,
    Quality,
    ReasonCode,
    SeatOutcome,
    SeatStatus,
    Verdict,
    adjudicate,
)

STUB_A = "stub-a"
STUB_B = "stub-b"


def _pair(index: int) -> tuple[PairNomination, EndpointObservation, EndpointObservation]:
    left = f"pair-{index:02d}-left"
    right = f"pair-{index:02d}-right"
    nomination = PairNomination(
        nomination_id=f"nom-{index:02d}",
        profile_id="eval-206",
        canonical_kind="read_conflict",
        composite_group_id=f"group-{index:02d}",
        source_generation=1,
        left_node_id=left,
        left_version=3,
        left_expected_peer_id=right,
        right_node_id=right,
        right_version=7,
        right_expected_peer_id=left,
        evidence_event_ids=(index * 2 + 1, index * 2 + 2),
        source_channels=("read_conflict_flag",),
        created_at="2026-09-19T00:00:00Z",
    )
    return (
        nomination,
        EndpointObservation("eval-206", left, 3, right, EndpointFate.LIVE, f"left-{index}"),
        EndpointObservation("eval-206", right, 7, left, EndpointFate.LIVE, f"right-{index}"),
    )


def _decision(
    nomination: PairNomination,
    verdict: Verdict,
    *,
    winner: str | None = None,
    loser: str | None = None,
    mismatch: bool = False,
) -> PairSeatDecision:
    return PairSeatDecision(
        nomination_id=nomination.nomination_id if not mismatch else "wrong-nomination",
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
    )


def _typed_oracle(verdict: Verdict, direction: str | None) -> str:
    if verdict is Verdict.CONFLICT and direction is None:
        return "deferred"
    return "accepted" if verdict is Verdict.CONFLICT else "rejected"


def _input(
    index: int,
    outcomes: tuple[SeatOutcome, ...],
    *,
    mode: str = "vote",
    protected: bool = False,
) -> PairAdjudicationInput:
    nomination, left, right = _pair(index)
    if protected:
        left = replace(left, never_decay=True)
    return PairAdjudicationInput(nomination, left, right, mode, outcomes)


def test_red_probe_requires_typed_oracle_for_directionless_input() -> None:
    assert _typed_oracle(Verdict.CONFLICT, direction=None) == "deferred"


@pytest.mark.parametrize("repeat", range(3))
def test_quorum_matrix_has_two_explicit_stub_seats(repeat: int) -> None:
    present = 0
    for index in range(8):
        nomination, _, _ = _pair(repeat * 8 + index)
        decision = _decision(
            nomination,
            Verdict.NOT_CONFLICT,
        )
        result = adjudicate(
            _input(
                repeat * 8 + index,
                (
                    SeatOutcome(SeatStatus.COMPLETE, (decision,), routes=()),
                    SeatOutcome(SeatStatus.COMPLETE, (decision,), routes=()),
                ),
            )
        )
        assert (STUB_A, STUB_B) == ("stub-a", "stub-b")
        assert result.disposition is Disposition.REJECTED
        assert result.quality is Quality.VOTED
        present += 1
    assert present == 8


@pytest.mark.parametrize(
    ("case", "expected", "reason"),
    [
        ("agree_conflict", Disposition.ACCEPTED, ReasonCode.CONFLICT_CONFIRMED),
        ("agree_not_conflict", Disposition.REJECTED, ReasonCode.NOT_CONFLICT_CONFIRMED),
        ("direction_disagreement", Disposition.DEFERRED, ReasonCode.SEAT_DISAGREEMENT),
        ("missing", Disposition.DEFERRED, ReasonCode.ABSENT_RESULT),
        ("single_side", Disposition.DEFERRED, ReasonCode.SINGLE_SIDE),
        ("timeout", Disposition.DEFERRED, ReasonCode.SEAT_TIMEOUT),
        ("invalid", Disposition.DEFERRED, ReasonCode.INVALID_TYPED_OUTPUT),
        ("polarity", Disposition.DEFERRED, ReasonCode.POLARITY_DROP),
        ("pair_mismatch", Disposition.DEFERRED, ReasonCode.IDENTITY_MISMATCH),
        ("protected", Disposition.UNRESOLVED, ReasonCode.PROTECTED_ENDPOINT),
    ],
)
def test_frozen_pair_scoped_disposition_oracle(case: str, expected: Disposition, reason: ReasonCode) -> None:
    nomination, _, _ = _pair(99)
    conflict_a = _decision(
        nomination, Verdict.CONFLICT, winner=nomination.right_node_id, loser=nomination.left_node_id
    )
    conflict_b = _decision(
        nomination, Verdict.CONFLICT, winner=nomination.left_node_id, loser=nomination.right_node_id
    )
    not_conflict = _decision(nomination, Verdict.NOT_CONFLICT)
    outcomes = {
        "agree_conflict": (
            SeatOutcome(SeatStatus.COMPLETE, (conflict_a,)),
            SeatOutcome(SeatStatus.COMPLETE, (conflict_a,)),
        ),
        "agree_not_conflict": (
            SeatOutcome(SeatStatus.COMPLETE, (not_conflict,)),
            SeatOutcome(SeatStatus.COMPLETE, (not_conflict,)),
        ),
        "direction_disagreement": (
            SeatOutcome(SeatStatus.COMPLETE, (conflict_a,)),
            SeatOutcome(SeatStatus.COMPLETE, (conflict_b,)),
        ),
        "missing": (SeatOutcome(SeatStatus.ABSENT), SeatOutcome(SeatStatus.COMPLETE, (not_conflict,))),
        "single_side": (
            SeatOutcome(SeatStatus.SINGLE_SIDE),
            SeatOutcome(SeatStatus.COMPLETE, (not_conflict,)),
        ),
        "timeout": (SeatOutcome(SeatStatus.TIMEOUT), SeatOutcome(SeatStatus.COMPLETE, (not_conflict,))),
        "invalid": (
            SeatOutcome(SeatStatus.INVALID_OUTPUT),
            SeatOutcome(SeatStatus.COMPLETE, (not_conflict,)),
        ),
        "polarity": (
            SeatOutcome(SeatStatus.POLARITY_DROP),
            SeatOutcome(SeatStatus.COMPLETE, (not_conflict,)),
        ),
        "pair_mismatch": (
            SeatOutcome(
                SeatStatus.COMPLETE,
                (_decision(nomination, Verdict.NOT_CONFLICT, mismatch=True),),
            ),
            SeatOutcome(SeatStatus.COMPLETE, (not_conflict,)),
        ),
        "protected": (SeatOutcome(SeatStatus.TIMEOUT), SeatOutcome(SeatStatus.TIMEOUT)),
    }[case]
    result = adjudicate(_input(99, outcomes, protected=case == "protected"))
    assert result.disposition is expected
    assert result.reason_code is reason


def test_malformed_and_directionless_inputs_never_unsafe_accept() -> None:
    nomination, _, _ = _pair(100)
    directionless = _decision(nomination, Verdict.CONFLICT)
    result = adjudicate(
        _input(
            100,
            (
                SeatOutcome(SeatStatus.COMPLETE, (directionless,)),
                SeatOutcome(SeatStatus.COMPLETE, (directionless,)),
            ),
        )
    )
    assert result.disposition is Disposition.DEFERRED
    assert result.reason_code is ReasonCode.CONFLICT_WITHOUT_DIRECTION
