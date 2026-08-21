"""T4a — recall matrix: parameter space, 24-point median aggregation, the
coordinate-descent executor (30 parameter groups), the Pareto bars and the
empty-frontier demotion path.

The executor is a pure function over a per-point metric oracle
``metric_fn(floor, budget) -> Sequence[RecallMetrics | None]`` — the live T4b
runner feeds it rig results; the unit tests feed it synthetic hand-built
metrics, so the trajectory math is pinned without any daemon.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mnemoseed_local.eval.recall_matrix import (
    PARAM_BUDGETS,
    PARAM_FLOORS,
    START_BUDGET,
    START_FLOOR,
    TARGET_BARS,
    AggregateMetrics,
    ParamGroup,
    aggregate_metrics,
    coordinate_descent,
    meets_bars,
    param_sweep_values,
    weighted_loss,
)
from mnemoseed_local.eval.recall_metrics import RecallMetrics

# a clean aggregate: every bar satisfied, loss dominated by the overhead term
_CLEAN = AggregateMetrics(
    recall_at_k=(1.0, 1.0, 0.9, 0.9),
    precision_at_k=(1.0, 1.0, 0.9, 0.9),
    floor_fp=0.1,
    detector_fp=0.1,
    fn_rate=0.1,
    token_overhead=0.5,
    non_focal_above_floor=1.0,
    points=24,
)


# ---------------------------------------------------------------- parameter space


def test_param_sweep_values_pin_the_prd_table() -> None:
    floors, budgets = param_sweep_values()
    assert floors == (0.4, 0.45, 0.5, 0.55, 0.6)
    assert budgets == (600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400)
    assert floors == PARAM_FLOORS
    assert budgets == PARAM_BUDGETS
    assert START_FLOOR == 0.4
    assert START_BUDGET == 1200


def test_target_bars_pin_the_prd_goals() -> None:
    assert TARGET_BARS == {
        "recall@5": 0.75,
        "precision@5": 0.60,
        "floor_fp": 0.15,
        "detector_fp": 0.15,
        "fn_rate": 0.20,
        "token_overhead": 0.8,
    }


# ---------------------------------------------------------------- aggregation (24-point median)


def test_aggregate_medians_hand_computed() -> None:
    def metric(floor_fp: float | None) -> RecallMetrics:
        return RecallMetrics(
            recall_at_k=(0.5, 0.5, 0.5, 0.5),
            precision_at_k=(0.5, 0.5, 0.5, 0.5),
            floor_fp=floor_fp,
            detector_fp=0.0,
            fn_rate=0.0,
            token_overhead=0.2,
            non_focal_above_floor=2,
        )

    agg = aggregate_metrics((metric(0.1), metric(0.3), metric(0.5)))
    assert agg.floor_fp == 0.3  # odd-count median is the middle value
    assert agg.recall_at_k == (0.5, 0.5, 0.5, 0.5)
    assert agg.non_focal_above_floor == 2.0
    assert agg.points == 3

    agg4 = aggregate_metrics((metric(0.1), metric(0.3), metric(0.5), metric(0.9)))
    assert agg4.floor_fp == 0.4  # even-count median averages the two middles


def test_aggregate_skips_none_but_reports_point_count() -> None:
    def metric(floor_fp: float | None) -> RecallMetrics:
        return RecallMetrics(
            recall_at_k=(None, None, None, None),
            precision_at_k=(0.0, 0.0, 0.0, 0.0),
            floor_fp=floor_fp,
            detector_fp=None,
            fn_rate=None,
            token_overhead=None,
            non_focal_above_floor=0,
        )

    agg = aggregate_metrics((metric(0.1), metric(None)))
    assert agg.floor_fp == 0.1  # the unknown point is excluded, not a zero
    assert agg.recall_at_k == (None, None, None, None)  # all unknown
    assert agg.detector_fp is None
    assert agg.token_overhead is None
    assert agg.points == 2  # the point count stays honest


# ---------------------------------------------------------------- bars + loss


def test_meets_bars_accepts_a_clean_aggregate() -> None:
    ok, failed = meets_bars(_CLEAN)
    assert ok is True
    assert failed == ()


def test_meets_bars_reports_each_failed_bar() -> None:
    bad = replace(_CLEAN, floor_fp=0.3, fn_rate=0.5, token_overhead=0.9)
    ok, failed = meets_bars(bad)
    assert ok is False
    assert set(failed) == {"floor_fp", "fn_rate", "token_overhead"}


def test_meets_bars_fails_on_unknown_metrics() -> None:
    unknown = replace(_CLEAN, recall_at_k=(None, None, None, None))
    ok, failed = meets_bars(unknown)
    assert ok is False
    assert failed == ("recall@5",)


def test_weighted_loss_hand_computed() -> None:
    loss = weighted_loss(_CLEAN)
    # 0.4*(1-0.9) + 0.3*(1-0.9) + 0.2*0.1 + 0.1*0.5
    assert loss == pytest.approx(0.4 * 0.1 + 0.3 * 0.1 + 0.2 * 0.1 + 0.1 * 0.5)


def test_weighted_loss_missing_metric_counts_as_worst() -> None:
    unknown = replace(_CLEAN, recall_at_k=(None, None, None, None))
    assert weighted_loss(unknown) == pytest.approx(0.4 + 0.3 * 0.1 + 0.2 * 0.1 + 0.1 * 0.5)


def test_weighted_loss_none_for_infeasible_group() -> None:
    assert weighted_loss(None) is None


# ---------------------------------------------------------------- coordinate descent


def _convex_metric_fn(floor: float, budget: int) -> tuple[RecallMetrics, ...]:
    """Loss minimized uniquely at floor=0.5, budget=1400 (overhead term)."""
    overhead = 0.01 + abs(floor - 0.5) + abs(budget - 1400) / 4000
    metric = RecallMetrics(
        recall_at_k=(1.0, 1.0, 1.0, 1.0),
        precision_at_k=(1.0, 1.0, 1.0, 1.0),
        floor_fp=0.0,
        detector_fp=0.0,
        fn_rate=0.0,
        token_overhead=min(overhead, 0.8),
        non_focal_above_floor=0,
    )
    return (metric,) * 24


def test_coordinate_descent_runs_thirty_groups_in_two_rounds() -> None:
    outcome = coordinate_descent(_convex_metric_fn)
    assert len(outcome.groups) == 30
    round1 = [g for g in outcome.groups if g.sweep_round == 1]
    round2 = [g for g in outcome.groups if g.sweep_round == 2]
    assert len(round1) == 15 and len(round2) == 15
    # 5 floor sweeps then 10 budget sweeps per round (PRD: 5+10 x 2 = 30)
    assert [g.sweep_axis for g in round1] == ["floor"] * 5 + ["budget"] * 10
    assert [g.sweep_axis for g in round2] == ["floor"] * 5 + ["budget"] * 10
    # round 1 starts at the PRD start values: floor sweep at budget=1200
    assert round1[0].focal_floor == 0.4
    assert round1[0].budget_chars == START_BUDGET
    assert all(g.budget_chars == START_BUDGET for g in round1[:5])
    # round 2 re-anchors on round 1's best (floor 0.5, budget 1400)
    assert all(g.budget_chars == 1400 for g in round2[:5])
    assert all(g.focal_floor == 0.5 for g in round2[5:])


def test_coordinate_descent_finds_the_unique_optimum() -> None:
    outcome = coordinate_descent(_convex_metric_fn)
    assert outcome.recommended == (0.5, 1400)
    assert outcome.demoted is False
    assert outcome.demotion_path == ()
    assert len(outcome.results) == 30
    # the recommended point is among the evaluated groups
    assert any(g.focal_floor == 0.5 and g.budget_chars == 1400 for g in outcome.groups)


def test_coordinate_descent_groups_are_deterministic() -> None:
    first = coordinate_descent(_convex_metric_fn)
    second = coordinate_descent(_convex_metric_fn)
    assert first == second
    assert all(isinstance(g, ParamGroup) and g.index == i for i, g in enumerate(first.groups))


def test_coordinate_descent_demotes_when_no_point_meets_bars() -> None:
    """Every point violates the floor_fp bar: the frontier is empty, so the
    executor falls back to the best weighted loss and records the demotion."""

    def failing_fn(floor: float, budget: int) -> tuple[RecallMetrics, ...]:
        metric = RecallMetrics(
            recall_at_k=(1.0, 1.0, 1.0, 1.0),
            precision_at_k=(1.0, 1.0, 1.0, 1.0),
            floor_fp=0.3 + abs(floor - 0.5),  # always above the 0.15 bar
            detector_fp=0.0,
            fn_rate=0.0,
            token_overhead=0.1,
            non_focal_above_floor=0,
        )
        return (metric,) * 24

    outcome = coordinate_descent(failing_fn)
    assert outcome.demoted is True
    assert "floor_fp" in outcome.demotion_path
    # the fallback picked the min-loss point: floor 0.5, and the first
    # min-loss group in scan order (the round-1 floor sweep at budget 1200)
    assert outcome.recommended[0] == 0.5
    assert outcome.recommended[1] == START_BUDGET


def test_coordinate_descent_prefers_bar_satisfying_points() -> None:
    """When some points satisfy the bars and some do not, the recommendation
    comes from the bar-satisfying set (lower loss elsewhere is irrelevant)."""

    def mixed_fn(floor: float, budget: int) -> tuple[RecallMetrics, ...]:
        good = floor == 0.5
        metric = RecallMetrics(
            recall_at_k=(1.0, 1.0, 1.0, 1.0),
            precision_at_k=(1.0, 1.0, 1.0, 1.0),
            floor_fp=0.05 if good else 0.9,
            detector_fp=0.0,
            fn_rate=0.0,
            token_overhead=0.5 if good else 0.01,  # the bad point has the lower loss
            non_focal_above_floor=0,
        )
        return (metric,) * 24

    outcome = coordinate_descent(mixed_fn)
    assert outcome.demoted is False
    assert outcome.recommended[0] == 0.5  # the bar-satisfying floor wins
    # the first bar-satisfying group in scan order (floor sweep at budget 1200)
    assert outcome.recommended[1] == START_BUDGET
