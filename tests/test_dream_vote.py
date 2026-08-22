"""B5 vote mechanism — journal phases, resume boundary, pure combiner.

The vote ensemble (mvp-design decision 1) is the honest-cost dual-reflect
layer: A and B each fully generate over the same delta, the journal carries
both per-phase results (REFLECT_A_DONE / REFLECT_B_DONE), a deterministic
combiner folds them into ONE result (COMBINE_DONE), and a single merge commits
it. This module pins the journal/recovery shape and the pure-combiner contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.dream import (
    ReflectedTriple,
    ReflectionResult,
    Route,
    SnapshotPhase,
    load_snapshot_file,
    resume_boundary,
    write_snapshot_file,
)
from mnemoseed_local.dream.combine import (
    COMBINE_PROMPT_VERSION,
    combine_results,
)
from mnemoseed_local.dream.snapshot import Snapshot
from mnemoseed_local.schema.stamp import CognitiveTier
from mnemoseed_local.storage.ports import TurnRange

_RANGE = TurnRange(0, 4)
_PROFILE = "alice"
_SNAP_ID = "snap-vote"


# ---------------------------------------------------------------- journal phases


def test_vote_phases_are_declared() -> None:
    assert SnapshotPhase.REFLECT_A_DONE.value == "reflect_a_done"
    assert SnapshotPhase.REFLECT_B_DONE.value == "reflect_b_done"
    assert SnapshotPhase.COMBINE_DONE.value == "combine_done"


def test_resume_boundary_vote_progression(tmp_path: Path) -> None:
    """A vote dream resumes at the exact phase: reflect_a -> reflect_b ->
    combine -> merge, mirroring the single-model progression (reflect -> merge)."""
    snap = _snap()
    assert resume_boundary(snap) == "reflect"  # fresh: A phase first

    a = snap.with_phase(SnapshotPhase.REFLECT_A_DONE.value)
    assert resume_boundary(a) == "reflect_b"  # A done: run B

    b = a.with_phase(SnapshotPhase.REFLECT_B_DONE.value)
    assert resume_boundary(b) == "combine"  # A+B done: run combiner

    combined = b.with_phase(SnapshotPhase.COMBINE_DONE.value)
    assert resume_boundary(combined) == "merge"  # combined: single merge

    merged = combined.with_phase(SnapshotPhase.MERGE_DONE.value)
    assert resume_boundary(merged) is None  # terminal


def test_resume_boundary_single_model_unchanged(tmp_path: Path) -> None:
    """The single-model path keeps its legacy boundary: fresh -> reflect,
    reflect done -> merge. Adding vote markers must not shift it."""
    snap = _snap()
    assert resume_boundary(snap) == "reflect"
    reflected = snap.with_phase(SnapshotPhase.REFLECT_DONE.value)
    assert resume_boundary(reflected) == "merge"


def test_vote_carrier_round_trips_through_journal(tmp_path: Path) -> None:
    """The per-phase A/B results ride on an opaque vote carrier that survives a
    journal round-trip, and merge only ever reads the single reflect_result."""
    snap = _snap().with_vote_seat("a", _payload("a-model")).with_vote_seat("b", _payload("b-model"))
    write_snapshot_file(tmp_path, snap)

    on_disk = load_snapshot_file(tmp_path / f"{_SNAP_ID}.json")
    assert on_disk is not None
    assert on_disk.vote_results == {
        "a": _payload("a-model"),
        "b": _payload("b-model"),
    }
    assert on_disk.reflect_result is None  # combined not yet written


def test_vote_carrier_absent_for_single_model(tmp_path: Path) -> None:
    snap = _snap()
    write_snapshot_file(tmp_path, snap)
    on_disk = load_snapshot_file(tmp_path / f"{_SNAP_ID}.json")
    assert on_disk is not None
    assert on_disk.vote_results is None


# ---------------------------------------------------------------- pure combiner


def _triple(
    obj: str,
    *,
    confidence: float,
    route: Route = Route.CORE,
    predicate: str = "prefers",
    model_id: str | None = None,
    polarity: str = "positive",
) -> ReflectedTriple:
    return ReflectedTriple(
        subject="user",
        predicate=predicate,
        object=obj,
        tiers=(CognitiveTier.TIER_1,),
        chunk_ids=("c1",),
        turn_range=_RANGE,
        confidence=confidence,
        route=route,
        model_id=model_id,
        polarity=polarity,
    )


def _result(triples: tuple[ReflectedTriple, ...], *, model_id: str) -> ReflectionResult:
    return ReflectionResult(
        snapshot_id=_SNAP_ID,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=triples,
    )


def test_combiner_agree_folds_confidence() -> None:
    """A and B agree on the same (subject, predicate, object): the combined
    result carries ONE triple with the reinforced confidence (the existing
    fold formula: max + 0.05 per extra mention, capped 0.95) and both models
    attributed."""
    a = _result((_triple("dark mode", confidence=0.7, model_id="a-model"),), model_id="a-model")
    b = _result((_triple("dark mode", confidence=0.6, model_id="b-model"),), model_id="b-model")
    out = combine_results(a, b)
    assert len(out.triples) == 1
    t = out.triples[0]
    assert t.route is Route.CORE
    assert t.confidence == pytest.approx(0.75)  # max(0.7) + 0.05
    assert t.model_id == "a-model|b-model"


def test_combiner_cross_seat_polarity_guard_on_agreement() -> None:
    """g2 cross-seat negation guard: A and B agree on the SAME key but with
    contradictory polarity — never folded into one reinforced triple. Both
    parties drop and the key lands on conflicts (the vote-side mirror of the
    single-seat negation guard)."""
    a = _result(
        (_triple("dark mode", confidence=0.7, model_id="a-model", polarity="positive"),),
        model_id="a-model",
    )
    b = _result(
        (_triple("dark mode", confidence=0.8, model_id="b-model", polarity="negative"),),
        model_id="b-model",
    )
    out = combine_results(a, b)
    assert out.triples == ()
    assert ("user", "prefers", "dark mode") in out.conflicts


def test_combiner_disagreement_goes_isolated() -> None:
    """A and B disagree on the same key (different objects): the divergence is
    preserved as ISOLATED, never voted away — the vote-side mirror of the
    verify-side 'reject -> isolated' philosophy."""
    a = _result((_triple("dark mode", confidence=0.7, model_id="a-model"),), model_id="a-model")
    b = _result((_triple("light mode", confidence=0.8, model_id="b-model"),), model_id="b-model")
    out = combine_results(a, b)
    # both divergent parties survive in the isolated track
    assert {t.object for t in out.triples} == {"dark mode", "light mode"}
    assert all(t.route is Route.ISOLATED for t in out.triples)


def test_combiner_single_side_only_survives() -> None:
    """A triple only one seat produced survives; a CORE single-side triple keeps
    its route, a low-confidence one is not silently invented elsewhere."""
    a = _result((_triple("dark mode", confidence=0.7, model_id="a-model"),), model_id="a-model")
    b = _result((), model_id="b-model")
    out = combine_results(a, b)
    assert len(out.triples) == 1
    assert out.triples[0].object == "dark mode"
    assert out.triples[0].route is Route.CORE


def test_combiner_folds_agreed_keys_before_isolating_divergent() -> None:
    """QA (over-isolation): a predicate where the two seats agree on ONE object
    but diverge on a sibling must fold the AGREED key (both seats on the same
    (s, p, o)) into one reinforced triple and isolate ONLY the divergent
    single-side parties — never demote the agreed key to ISOLATED."""
    a = _result(
        (
            _triple("dark mode", confidence=0.7, model_id="a-model"),
            _triple("light mode", confidence=0.6, model_id="a-model"),
        ),
        model_id="a-model",
    )
    b = _result(
        (
            _triple("dark mode", confidence=0.8, model_id="b-model"),
            _triple("sepia mode", confidence=0.9, model_id="b-model"),
        ),
        model_id="b-model",
    )
    out = combine_results(a, b)
    objects = {t.object: t for t in out.triples}
    # the agreed "dark mode" key folds into one reinforced CORE triple
    agreed = objects["dark mode"]
    assert agreed.route is Route.CORE
    assert agreed.confidence == pytest.approx(0.85)  # max(0.8) + 0.05
    assert agreed.model_id == "a-model|b-model"
    assert not agreed.vote_disagreement
    # only the divergent single-side parties are isolated
    assert objects["light mode"].route is Route.ISOLATED
    assert objects["light mode"].vote_disagreement is True
    assert objects["sepia mode"].route is Route.ISOLATED
    assert objects["sepia mode"].vote_disagreement is True
    assert len(out.triples) == 3


def test_combiner_keeps_both_overflow_and_conflicts() -> None:
    """The combined result preserves the overflow (safe-clear allow-list) and
    negation-conflict accounting from both seats."""
    a = ReflectionResult(
        snapshot_id=_SNAP_ID,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(_triple("dark mode", confidence=0.7, model_id="a-model"),),
        overflow_chunk_ids=("c1",),
        consumed_chunk_ids=("c1",),
    )
    b = _result((), model_id="b-model")
    out = combine_results(a, b)
    assert out.overflow_chunk_ids == ("c1",)
    assert out.consumed_chunk_ids == ("c1",)


def test_combine_prompt_version_constant() -> None:
    assert COMBINE_PROMPT_VERSION


# ---------------------------------------------------------------- helpers


def _payload(model_id: str) -> dict[str, object]:
    return {
        "snapshot_id": _SNAP_ID,
        "profile_id": _PROFILE,
        "turn_range": {"start": 0, "end": 4},
        "prompt_version": "v1",
        "model_id": model_id,
        "triples": [],
        "conflicts": [],
        "delta_overflow": [],
        "consumed_chunk_ids": [],
    }


def _snap() -> Snapshot:
    return Snapshot(
        snapshot_id=_SNAP_ID,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        chunks=(),
        created_at=1000.0,
        phases=frozenset({SnapshotPhase.SNAPSHOT_DONE.value}),
    )
