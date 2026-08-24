"""Rescue-band calibration (design/09 §3.5, open question 3): materials,
matrix math, and one end-to-end calibration point.

The rescue floor and cue-match minimum are NOT hand-picked: they get the same
bar-discipline calibration the T2 recall thresholds received — structured
materials on the real daemon rig (synthetic embedder, zero live models),
hand-computable per-point metrics, median aggregation, gate bars, and an
honest demotion path when no grid point passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.eval.rescue_harness import (
    RescueRig,
    RigRootNotFresh,
    run_rescue_point,
)
from mnemoseed_local.eval.rescue_materials import rescue_materials
from mnemoseed_local.eval.rescue_matrix import (
    RESCUE_PARAM_FLOORS,
    RescuePointMetrics,
    aggregate_rescue_metrics,
    meets_rescue_bars,
    pin_is_eligible,
    rescue_grid_descent,
)

# ---------------------------------------------------------------- materials


def test_rescue_materials_are_deterministic_and_structured() -> None:
    """Same seed -> byte-identical batch; every point carries its four actors
    (band pin, plain decoy, healthy baseline, dead-zone pin) on ONE topic."""
    first = rescue_materials()
    second = rescue_materials()

    assert first == second
    assert len(first) >= 8
    entities = [material.entity for material in first]
    assert len(set(entities)) == len(entities)  # unique topic per point
    for material in first:
        assert 0.05 <= material.pin_decay < 0.4  # inside or below the band
        assert material.decoy_decay < 0.4  # the decoy lives in the band too
        assert material.dead_pin_decay < min(RESCUE_PARAM_FLOORS)  # always dead


def test_pin_is_eligible_boundary_math() -> None:
    """Eligibility is exactly: pin weight at/above the rescue floor AND cue
    overlap at/above the minimum — checked at the boundaries."""
    assert pin_is_eligible(pin_decay=0.25, cue_overlap=0.30, rescue_floor=0.25, cue_min=0.30)
    assert not pin_is_eligible(pin_decay=0.2499, cue_overlap=0.30, rescue_floor=0.25, cue_min=0.30)
    assert not pin_is_eligible(pin_decay=0.25, cue_overlap=0.2999, rescue_floor=0.25, cue_min=0.30)
    # below the main pool floor the band definition ends
    assert not pin_is_eligible(pin_decay=0.40, cue_overlap=0.30, rescue_floor=0.25, cue_min=0.30)


# ---------------------------------------------------------------- matrix math


def _metrics(
    *,
    served: bool = True,
    noise: int = 0,
    rebound: bool = True,
    leak: bool = False,
    residue: bool = True,
) -> RescuePointMetrics:
    return RescuePointMetrics(
        eligible=True,
        served=served,
        in_band=True,
        noise_admitted=noise,
        rebound_ok=rebound,
        dead_leaked=leak,
        dead_residue_present=residue,
        rank_after_normal=True,
    )


def test_grid_descent_prefers_a_bar_passing_group() -> None:
    def metric_fn(floor: float, cue_min: float) -> list[RescuePointMetrics]:
        good = floor == 0.25 and cue_min == 0.30
        points = [_metrics(served=good, noise=0 if good else 1) for _ in range(4)]
        # the failing groups leak noise; their pins stay unserved and unranked
        return [
            RescuePointMetrics(
                eligible=p.eligible,
                served=p.served,
                in_band=p.in_band,
                noise_admitted=p.noise_admitted,
                rebound_ok=True,
                dead_leaked=False,
                dead_residue_present=True,
                rank_after_normal=True if good else None,
            )
            for p in points
        ]

    outcome = rescue_grid_descent(metric_fn)

    assert outcome.recommended == (0.25, 0.30)
    assert outcome.demoted is False


def test_grid_descent_demotes_honestly_when_no_group_passes() -> None:
    def metric_fn(floor: float, cue_min: float) -> list[RescuePointMetrics]:
        return [
            RescuePointMetrics(
                eligible=True,
                served=False,
                in_band=True,
                noise_admitted=1,
                rebound_ok=False,
                dead_leaked=True,
                dead_residue_present=False,
                rank_after_normal=None,
            )
        ]

    outcome = rescue_grid_descent(metric_fn)

    assert outcome.demoted is True
    assert set(outcome.demotion_path) >= {"rescue_rate", "noise_admission_rate"}


def test_rank_discipline_is_a_gate_bar_not_report_only() -> None:
    """Enforced rank discipline (design/09 §3.5): a group whose rescued
    candidates outrank normal ones fails the bars even when everything else is
    perfect — the pick can never land on a rank-violating point."""

    def metric_fn(floor: float, cue_min: float) -> list[RescuePointMetrics]:
        return [_metrics(served=True, noise=0, rebound=True, leak=False, residue=True)]

    # the shared _metrics helper reports rank_after_normal=True, so that grid
    # passes; force the violation and the same grid must fail closed
    def violating(floor: float, cue_min: float) -> list[RescuePointMetrics]:
        return [
            RescuePointMetrics(
                eligible=True,
                served=True,
                in_band=True,
                noise_admitted=0,
                rebound_ok=True,
                dead_leaked=False,
                dead_residue_present=True,
                rank_after_normal=False,
            )
        ]

    ok_violating, failed = meets_rescue_bars(aggregate_rescue_metrics(violating(0.25, 0.3)))
    assert ok_violating is False
    assert "rank_discipline" in failed

    outcome = rescue_grid_descent(violating)
    assert outcome.demoted is True


def test_bars_fail_closed_on_unknown_values() -> None:
    empty = aggregate_rescue_metrics([])  # nothing measurable

    ok, failed = meets_rescue_bars(empty)

    assert ok is False
    assert failed  # every gate bar named


# ---------------------------------------------------------------- rig e2e


@pytest.fixture
def rig_root(tmp_path: Path) -> Path:
    return tmp_path / "rig"


def test_rig_refuses_a_dirty_root(rig_root: Path) -> None:
    rig_root.mkdir(parents=True)
    (rig_root / "config.toml").write_text("preset = 'embedded'\n", encoding="utf-8")

    with pytest.raises(RigRootNotFresh):
        RescueRig(rig_root, rescue_floor=0.25, rescue_cue_min=0.30)


def test_calibration_point_end_to_end(rig_root: Path) -> None:
    """One full calibration point over the REAL daemon surface: the eligible
    band pin is served flagged rescued and rebounds; the plain decoy below the
    main floor is never admitted regardless of its perfect cue match; the
    dead-zone pin leaks nowhere but its residue line."""
    material = next(m for m in rescue_materials() if m.cue_class == "full" and m.pin_decay >= 0.3)

    result = run_rescue_point(
        material,
        root=rig_root,
        rescue_floor=0.25,
        rescue_cue_min=0.30,
    )

    metrics = result.metrics
    assert metrics.eligible is True
    assert metrics.served is True
    assert metrics.noise_admitted == 0
    assert metrics.rebound_ok is True
    assert metrics.dead_leaked is False
    assert metrics.dead_residue_present is True
    # the raw evidence backs every derived number
    served_ids = [entry["id"] for entry in result.response["memory"]["entries"]]
    assert material.pin_id in served_ids
    assert material.decoy_id not in served_ids
    residue_ids = {row["chunk_id"] for row in result.response["memory"]["index_residue"]["rows"]}
    assert material.dead_pin_id in residue_ids


def test_rank_flip_material_keeps_discipline_when_pin_outscores_healthy(rig_root: Path) -> None:
    """The rank_discipline bar must have teeth: on this point the pin's fused
    score BEATS the healthy chunk's end-to-end (asserted from the served
    scores), so 'pin ranks after healthy' can only hold through enforced
    discipline at the serving surface — never through score order."""
    material = next(m for m in rescue_materials() if m.point_id.endswith("-rank-flip"))

    result = run_rescue_point(material, root=rig_root, rescue_floor=0.25, rescue_cue_min=0.30)

    metrics = result.metrics
    assert metrics.eligible is True
    assert metrics.served is True
    entries = {entry["id"]: entry for entry in result.response["memory"]["entries"]}
    assert entries[material.pin_id]["score"] > entries[material.healthy_id]["score"]
    assert metrics.rank_after_normal is True


def test_calibration_point_keeps_partial_cue_out_at_strict_threshold(rig_root: Path) -> None:
    """The cue axis genuinely discriminates: a partial-overlap pin (β=0.3) is
    gated OUT at cue_min 0.4 and stays out without any noise admission."""
    material = next(m for m in rescue_materials() if m.cue_class == "partial")

    result = run_rescue_point(material, root=rig_root, rescue_floor=0.25, rescue_cue_min=0.40)

    assert result.metrics.eligible is False
    assert result.metrics.served is False
    assert result.metrics.noise_admitted == 0
