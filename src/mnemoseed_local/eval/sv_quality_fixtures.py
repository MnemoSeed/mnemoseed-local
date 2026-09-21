"""Frozen synthetic conflict corpus for the single-vs-dual quality comparison.

Eight ground-truth conflict pairs plus eight matched non-conflict pairs.
Every fixture carries explicit identity, text, expected disposition, and
winner semantics so the ground truth reproduces without model judgment.
Negative shape labels are content tags only and never force procedural
deferral: every negative is an exact-identity LIVE pair whose two agreeing
NOT_CONFLICT seats terminate REJECTED.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from mnemoseed_local.dream.adjudicate import (
    EndpointFate,
    EndpointObservation,
    PairAdjudicationInput,
    PairNomination,
    PairSeatDecision,
    SeatOutcome,
    SeatStatus,
    Verdict,
)
from mnemoseed_local.eval.canary import canary_sessions
from mnemoseed_local.storage.ports import Disposition

SV_QUALITY_VERSION = "v1"
SV_QUALITY_CANARY_SEED = 2060901
SV_QUALITY_CANARY_SESSIONS = 24
SV_QUALITY_FACTS_PER_SESSION = 8
SV_QUALITY_NOISE_PER_SESSION = 6
SV_QUALITY_CANARY_TOTAL = 24
SV_QUALITY_PROMPT_VERSION = "v1"

REQUIRED_NEGATIVE_SHAPES = frozenset(
    {
        "single_side",
        "polarity",
        "stale_version",
        "no_exact_identity",
        "protected",
        "object_level",
    }
)


@dataclass(frozen=True)
class ConflictFixture:
    """One labeled pair with reproducible ground truth."""

    fixture_id: str
    is_positive: bool
    shape: str
    left_node_id: str
    right_node_id: str
    left_text: str
    right_text: str
    expected_disposition: Disposition
    expected_winner: str | None
    expected_loser: str | None


def _positive_row(
    index: int, left_id: str, right_id: str, left_text: str, right_text: str, winner: str
) -> ConflictFixture:
    loser = right_id if winner == left_id else left_id
    return ConflictFixture(
        fixture_id=f"svq-pos-{index:02d}",
        is_positive=True,
        shape="true_conflict",
        left_node_id=left_id,
        right_node_id=right_id,
        left_text=left_text,
        right_text=right_text,
        expected_disposition=Disposition.ACCEPTED,
        expected_winner=winner,
        expected_loser=loser,
    )


def positive_fixtures() -> tuple[ConflictFixture, ...]:
    """Exactly eight true-conflict pairs with explicit winners."""
    pairs = (
        ("user prefers pour-over coffee.", "User prefers dark roast drip.", "right"),
        ("User runs the full test suite before committing.", "User never runs tests.", "left"),
        ("User decided to use pnpm for dependencies.", "User decided to use npm and rejects pnpm.", "left"),
        ("User believes type safety pays for itself.", "User believes type safety is waste.", "left"),
        ("User prefers dark mode.", "User prefers light mode and rejects dark mode.", "left"),
        ("User always writes the changelog entry first.", "User never writes changelogs.", "left"),
        ("User decided trunk-based development.", "User decided long-lived feature branches.", "left"),
        ("User believes offline-first beats cloud lock-in.", "User believes cloud-only is best.", "left"),
    )
    fixtures: list[ConflictFixture] = []
    for index, (left_text, right_text, side) in enumerate(pairs):
        left_id = f"svq-pos-{index:02d}-left"
        right_id = f"svq-pos-{index:02d}-right"
        winner = left_id if side == "left" else right_id
        fixtures.append(_positive_row(index, left_id, right_id, left_text, right_text, winner))
    return tuple(fixtures)


def _negative_row(
    fixture_id: str,
    shape: str,
    left_id: str,
    right_id: str,
    left_text: str,
    right_text: str,
) -> ConflictFixture:
    return ConflictFixture(
        fixture_id=fixture_id,
        is_positive=False,
        shape=shape,
        left_node_id=left_id,
        right_node_id=right_id,
        left_text=left_text,
        right_text=right_text,
        expected_disposition=Disposition.REJECTED,
        expected_winner=None,
        expected_loser=None,
    )


def negative_fixtures() -> tuple[ConflictFixture, ...]:
    """Eight matched non-conflicts; shape labels are content tags only."""
    rows = (
        _negative_row(
            "svq-neg-00",
            "single_side",
            "svq-neg-00-left",
            "svq-neg-00-right",
            "User prefers pour-over coffee.",
            "User prefers pour-over coffee with oat milk on weekends.",
        ),
        _negative_row(
            "svq-neg-01",
            "polarity",
            "svq-neg-01-left",
            "svq-neg-01-right",
            "User prefers dark mode for night reading.",
            "User prefers dark mode for night reading with larger text.",
        ),
        _negative_row(
            "svq-neg-02",
            "stale_version",
            "svq-neg-02-left",
            "svq-neg-02-right",
            "User decided to use pnpm for dependencies.",
            "User decided to use pnpm with a frozen lockfile.",
        ),
        _negative_row(
            "svq-neg-03",
            "no_exact_identity",
            "svq-neg-03-left",
            "svq-neg-03-right",
            "User prefers pour-over coffee.",
            "User prefers pour-over coffee brewed at home.",
        ),
        _negative_row(
            "svq-neg-04",
            "protected",
            "svq-neg-04-left",
            "svq-neg-04-right",
            "User keeps a standing weekly review note.",
            "User keeps a standing weekly review note with action items.",
        ),
        _negative_row(
            "svq-neg-05",
            "object_level",
            "svq-neg-05-left",
            "svq-neg-05-right",
            "User prefers pour-over coffee.",
            "User prefers pour-over coffee on weekends only.",
        ),
        _negative_row(
            "svq-neg-06",
            "object_level",
            "svq-neg-06-left",
            "svq-neg-06-right",
            "User believes small models suffice with verification.",
            "User believes small models suffice with validation tooling.",
        ),
        _negative_row(
            "svq-neg-07",
            "single_side",
            "svq-neg-07-left",
            "svq-neg-07-right",
            "User always reviews the diff line by line.",
            "User always reviews the diff line by line before lunch.",
        ),
    )
    return rows


def all_fixtures() -> tuple[ConflictFixture, ...]:
    """Positives followed by negatives in frozen order."""
    return (*positive_fixtures(), *negative_fixtures())


def _fixture_index(fixture_id: str) -> int:
    for index, item in enumerate(all_fixtures()):
        if item.fixture_id == fixture_id:
            return index
    raise KeyError(f"unknown fixture {fixture_id!r}")


def _nomination_for(item: ConflictFixture, index: int) -> PairNomination:
    return PairNomination(
        nomination_id=f"{item.fixture_id}-nom",
        profile_id="sv-quality",
        canonical_kind="read_conflict",
        composite_group_id=f"{item.fixture_id}-group",
        source_generation=1,
        left_node_id=item.left_node_id,
        left_version=3,
        left_expected_peer_id=item.right_node_id,
        right_node_id=item.right_node_id,
        right_version=7,
        right_expected_peer_id=item.left_node_id,
        evidence_event_ids=(index * 2 + 1, index * 2 + 2),
        source_channels=("read_conflict_flag",),
        created_at="2026-09-19T00:00:00Z",
    )


def _observations_for(item: ConflictFixture) -> tuple[EndpointObservation, EndpointObservation]:
    return (
        EndpointObservation(
            profile_id="sv-quality",
            node_id=item.left_node_id,
            version=3,
            expected_peer_id=item.right_node_id,
            fate=EndpointFate.LIVE,
            content=item.left_text,
        ),
        EndpointObservation(
            profile_id="sv-quality",
            node_id=item.right_node_id,
            version=7,
            expected_peer_id=item.left_node_id,
            fate=EndpointFate.LIVE,
            content=item.right_text,
        ),
    )


def _decision_for(item: ConflictFixture, index: int, verdict: Verdict) -> PairSeatDecision:
    if verdict is Verdict.CONFLICT:
        winner = item.expected_winner or item.left_node_id
        loser = item.expected_loser or item.right_node_id
    else:
        winner = None
        loser = None
    return PairSeatDecision(
        nomination_id=f"{item.fixture_id}-nom",
        profile_id="sv-quality",
        left_node_id=item.left_node_id,
        left_version=3,
        right_node_id=item.right_node_id,
        right_version=7,
        verdict=verdict,
        winner_node_id=winner,
        loser_node_id=loser,
        target_node_ids=(item.left_node_id, item.right_node_id),
        evidence_event_ids=(index * 2 + 1, index * 2 + 2),
    )


def adjudication_input_for(fixture_id: str, verdict: str) -> PairAdjudicationInput:
    """Two agreeing stub seats over an exact-identity LIVE pair."""
    item = next(item for item in all_fixtures() if item.fixture_id == fixture_id)
    index = _fixture_index(fixture_id)
    parsed = Verdict(verdict)
    decision = _decision_for(item, index, parsed)
    left, right = _observations_for(item)
    outcome = SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,))
    return PairAdjudicationInput(
        nomination=_nomination_for(item, index),
        left=left,
        right=right,
        ensemble_mode="vote",
        seat_outcomes=(outcome, outcome),
    )


def adjudication_input_for_mixed(fixture_id: str) -> PairAdjudicationInput:
    """One CONFLICT and one NOT_CONFLICT seat sharing the same identity."""
    item = next(item for item in all_fixtures() if item.fixture_id == fixture_id)
    index = _fixture_index(fixture_id)
    left, right = _observations_for(item)
    agree = SeatOutcome(
        status=SeatStatus.COMPLETE,
        decisions=(_decision_for(item, index, Verdict.CONFLICT),),
    )
    disagree = SeatOutcome(
        status=SeatStatus.COMPLETE,
        decisions=(_decision_for(item, index, Verdict.NOT_CONFLICT),),
    )
    return PairAdjudicationInput(
        nomination=_nomination_for(item, index),
        left=left,
        right=right,
        ensemble_mode="vote",
        seat_outcomes=(agree, disagree),
    )


def adjudication_carrier_for(
    fixture_id: str,
) -> tuple[PairNomination, EndpointObservation, EndpointObservation]:
    """Expose the frozen nomination and endpoint observations without seats."""
    item = next(item for item in all_fixtures() if item.fixture_id == fixture_id)
    index = _fixture_index(fixture_id)
    left, right = _observations_for(item)
    return (_nomination_for(item, index), left, right)


def _hash_fixtures(fixtures: tuple[ConflictFixture, ...]) -> str:
    payload = json.dumps(
        [
            {
                "fixture_id": item.fixture_id,
                "is_positive": item.is_positive,
                "shape": item.shape,
                "left_node_id": item.left_node_id,
                "right_node_id": item.right_node_id,
                "left_text": item.left_text,
                "right_text": item.right_text,
                "expected_disposition": str(item.expected_disposition),
                "expected_winner": item.expected_winner,
                "expected_loser": item.expected_loser,
            }
            for item in fixtures
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def positive_corpus_hash() -> str:
    """Stable hash of the eight positive fixtures."""
    return _hash_fixtures(positive_fixtures())


def negative_corpus_hash() -> str:
    """Stable hash of the eight negative fixtures."""
    return _hash_fixtures(negative_fixtures())


def corpus_hash() -> str:
    """Stable hash of the full sixteen-fixture corpus."""
    return _hash_fixtures(all_fixtures())


def sv_canary_sessions():  # type: ignore[no-untyped-def]
    """Exactly 24 canary sessions through the existing factory API."""
    return canary_sessions(
        SV_QUALITY_CANARY_SEED,
        sessions=SV_QUALITY_CANARY_SESSIONS,
        facts_per_session=SV_QUALITY_FACTS_PER_SESSION,
        noise_per_session=SV_QUALITY_NOISE_PER_SESSION,
    )


def sv_canary_material_ids() -> tuple[str, ...]:
    """Deterministic material IDs for the 24-session corpus."""
    return tuple(session.session_id for session in sv_canary_sessions())


def sv_canary_session_hash(session_id: str) -> str:
    """Stable hash of one canary session payload."""
    session = next(item for item in sv_canary_sessions() if item.session_id == session_id)
    payload = json.dumps(
        {
            "session_id": session.session_id,
            "turns": [
                {"text": turn.text, "role": turn.role, "fact_id": turn.fact_id} for turn in session.turns
            ],
            "facts": [
                {
                    "fact_id": fact.fact_id,
                    "predicate": fact.predicate,
                    "polarity": fact.polarity,
                    "phrasings": list(fact.phrasings),
                }
                for fact in session.facts
            ],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sv_canary_corpus_hash() -> str:
    """Stable hash over all 24 session hashes in material order."""
    combined = json.dumps(
        [sv_canary_session_hash(material_id) for material_id in sv_canary_material_ids()],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()
