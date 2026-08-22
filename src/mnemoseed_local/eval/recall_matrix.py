"""T4a recall matrix (PRD-B2.1-T4): parameter space, 24-point aggregation,
the coordinate-descent executor and the Pareto bars.

The executor is a pure function over a per-point metric oracle
``metric_fn(floor, budget) -> Sequence[RecallMetrics | None]``: the live T4b
runner feeds it rig results, the unit tests feed it synthetic metrics, so
the trajectory math is pinned without any daemon.

Coordinate descent (PRD execution design): each round sweeps one axis with
the other fixed — 5 floor values at the fixed budget, then 10 budget values
at the best floor — 15 groups per round, 2 rounds, 30 groups total. The
frontier scalar is the 24-point MEDIAN of each metric (the material points
carry the variance; a fixed material seed spans the whole sweep). When no
point satisfies every bar, the executor demotes to the best weighted loss
and records the path honestly.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from mnemoseed_local.eval.recall_metrics import RECALL_KS, RecallMetrics

#: The injected parameter space (PRD table; needle_* is deferred to B2.7+).
PARAM_FLOORS: tuple[float, ...] = (0.4, 0.45, 0.5, 0.55, 0.6)
PARAM_BUDGETS: tuple[int, ...] = tuple(range(600, 2401, 200))

#: The coordinate-descent start values: the PRD 起步值 anchor (the descent
#: sweeps the full grid each round, so this only fixes round-1 anchoring).
START_FLOOR: float = 0.4
START_BUDGET: int = 1200

#: The calibration goals (PRD 标定目标), recalibrated to the T4b package
#: envelope as a two-tier structure:
#: - Gate tier (a recommended point must satisfy every entry): precision@5's
#:   ceiling is 2/3 (two referenced fact turns + one serveable interference
#:   chunk per point), so 0.60 is now reachable; detector_fp/fn_rate keep
#:   their PRD values (five citation variants dilute the shared-head needle's
#:   structural false-reinforcement rate under the bar).
#: - Report-only tier: floor_fp carries NO bar — with the aged interference
#:   decayed below the whole floor sweep, its serveable pool is empty exactly
#:   where the gate works (an unknown would fail honestly but vacuously), so
#:   gating it would reward materials that keep noise serveable. It is still
#:   computed, aggregated and printed on every frontier line.
TARGET_BARS: dict[str, float] = {
    "recall@5": 0.75,
    "precision@5": 0.60,
    "detector_fp": 0.15,
    "fn_rate": 0.20,
    "token_overhead": 0.8,
}

#: The demotion ordering (PRD 空前沿回退规则): weighted loss over the gate
#: scalars, renormalized after floor_fp left the set; a missing metric counts
#: as the worst (full weight).
LOSS_WEIGHTS: dict[str, float] = {
    "recall@5": 0.5,
    "precision@5": 0.3,
    "token_overhead": 0.2,
}

#: How each frontier scalar reads off an aggregate — bars and loss share it.
_AGG_SCALARS: dict[str, Callable[[AggregateMetrics], float | None]] = {
    "recall@5": lambda agg: agg.recall_at_5,
    "precision@5": lambda agg: agg.precision_at_5,
    "floor_fp": lambda agg: agg.floor_fp,
    "detector_fp": lambda agg: agg.detector_fp,
    "fn_rate": lambda agg: agg.fn_rate,
    "token_overhead": lambda agg: agg.token_overhead,
}

_HIGHER_IS_BETTER: frozenset[str] = frozenset({"recall@5", "precision@5"})

_AT_5 = 2  # index of the k=5 slot in the @k tuples


@dataclass(frozen=True)
class AggregateMetrics:
    """One parameter group's frontier scalars: the 24-point medians."""

    recall_at_k: tuple[float | None, ...]
    precision_at_k: tuple[float | None, ...]
    floor_fp: float | None
    detector_fp: float | None
    fn_rate: float | None
    token_overhead: float | None
    non_focal_above_floor: float | None
    points: int  # how many material points contributed (honest denominator)

    @property
    def recall_at_5(self) -> float | None:
        return self.recall_at_k[_AT_5]

    @property
    def precision_at_5(self) -> float | None:
        return self.precision_at_k[_AT_5]


@dataclass(frozen=True)
class ParamGroup:
    """One evaluated parameter combination on the descent trajectory."""

    index: int
    sweep_round: int  # 1 | 2
    sweep_axis: str  # "floor" | "budget"
    focal_floor: float
    budget_chars: int


@dataclass(frozen=True)
class ParamGroupResult:
    """A group's aggregate + its demotion loss (None when nothing was runnable)."""

    group: ParamGroup
    aggregate: AggregateMetrics | None
    loss: float | None


@dataclass(frozen=True)
class CoordinateDescentOutcome:
    """The full 30-group trajectory plus the frontier recommendation."""

    groups: tuple[ParamGroup, ...]
    results: tuple[ParamGroupResult, ...]
    recommended: tuple[float, int] | None  # (focal_floor, budget_chars)
    demoted: bool  # True when the empty-frontier fallback path was used
    demotion_path: tuple[str, ...]  # the bars the recommended point misses


MetricFn = Callable[[float, int], Sequence[RecallMetrics | None]]


def param_sweep_values() -> tuple[tuple[float, ...], tuple[int, ...]]:
    """The PRD parameter space: (floors, budgets)."""
    return PARAM_FLOORS, PARAM_BUDGETS


def _median(values: Sequence[float | None]) -> float | None:
    """Median over the present values; unknowns are excluded (a 0/0 point is
    not a zero). All-unknown -> None."""
    present = sorted(value for value in values if value is not None)
    if not present:
        return None
    middle = len(present) // 2
    if len(present) % 2:
        return present[middle]
    return (present[middle - 1] + present[middle]) / 2


def aggregate_metrics(metrics: Sequence[RecallMetrics | None]) -> AggregateMetrics:
    """The 24-point frontier scalars: per-metric medians over the points."""
    measured = [m for m in metrics if m is not None]
    return AggregateMetrics(
        recall_at_k=tuple(_median([m.recall_at_k[i] for m in measured]) for i in range(len(RECALL_KS))),
        precision_at_k=tuple(_median([m.precision_at_k[i] for m in measured]) for i in range(len(RECALL_KS))),
        floor_fp=_median([m.floor_fp for m in measured]),
        detector_fp=_median([m.detector_fp for m in measured]),
        fn_rate=_median([m.fn_rate for m in measured]),
        token_overhead=_median([m.token_overhead for m in measured]),
        non_focal_above_floor=_median([m.non_focal_above_floor for m in measured]),
        points=len(metrics),
    )


def meets_bars(agg: AggregateMetrics | None) -> tuple[bool, tuple[str, ...]]:
    """Every acceptance bar satisfied? Returns (ok, failed-bar names). An
    unknown (None) metric fails its bar — honest, never a silent pass."""
    if agg is None:
        return False, tuple(TARGET_BARS)
    failed = [
        name
        for name in TARGET_BARS
        if _fails_bar(_AGG_SCALARS[name](agg), TARGET_BARS[name], name in _HIGHER_IS_BETTER)
    ]
    return (not failed), tuple(failed)


def _fails_bar(value: float | None, bar: float, higher_is_better: bool) -> bool:
    if value is None:
        return True
    ok = value >= bar if higher_is_better else value <= bar
    return not ok


def weighted_loss(agg: AggregateMetrics | None) -> float | None:
    """The demotion loss (PRD fallback ordering): weighted sum over the gate
    scalars; a missing metric contributes its full weight (worst)."""
    if agg is None:
        return None
    total = 0.0
    for name, weight in LOSS_WEIGHTS.items():
        value = _AGG_SCALARS[name](agg)
        if value is None:
            total += weight
        elif name in _HIGHER_IS_BETTER:
            total += weight * (1.0 - value)
        else:
            total += weight * value
    return total


def coordinate_descent(
    metric_fn: MetricFn,
    *,
    floors: Sequence[float] = PARAM_FLOORS,
    budgets: Sequence[int] = PARAM_BUDGETS,
    start_floor: float = START_FLOOR,
    start_budget: int = START_BUDGET,
) -> CoordinateDescentOutcome:
    """The 30-group coordinate descent over (focal_floor, budget_chars).

    Round 1 sweeps the floor at the start budget, then the budget at the best
    floor; round 2 re-anchors on round 1's bests (5 + 10 per round). The
    frontier recommendation prefers bar-satisfying points; when none exists
    the executor demotes to the best weighted loss and records the path.
    """
    groups: list[ParamGroup] = []
    results: list[ParamGroupResult] = []

    def run_group(sweep_round: int, sweep_axis: str, floor: float, budget: int) -> None:
        group = ParamGroup(len(groups), sweep_round, sweep_axis, floor, budget)
        groups.append(group)
        aggregate = aggregate_metrics(tuple(metric_fn(floor, budget)))
        results.append(ParamGroupResult(group, aggregate, weighted_loss(aggregate)))

    def best(sweep_round: int, sweep_axis: str) -> ParamGroupResult | None:
        candidates = [
            r for r in results if r.group.sweep_round == sweep_round and r.group.sweep_axis == sweep_axis
        ]
        with_loss = [r for r in candidates if r.loss is not None]
        if not with_loss:
            return None
        return min(with_loss, key=lambda r: (r.loss, r.group.index))

    # round 1: floor sweep at the start budget, then budget sweep at the best floor
    budget = start_budget
    for floor in floors:
        run_group(1, "floor", floor, budget)
    best_floor = best(1, "floor")
    assert best_floor is not None, "coordinate descent: no floor group scored"
    floor = best_floor.group.focal_floor
    for value in budgets:
        run_group(1, "budget", floor, value)
    best_budget = best(1, "budget")
    assert best_budget is not None, "coordinate descent: no budget group scored"
    budget = best_budget.group.budget_chars

    # round 2: re-anchor on round 1's bests
    for floor in floors:
        run_group(2, "floor", floor, budget)
    best_floor = best(2, "floor")
    assert best_floor is not None, "coordinate descent: round-2 floor sweep empty"
    floor = best_floor.group.focal_floor
    for value in budgets:
        run_group(2, "budget", floor, value)

    passing = [r for r in results if r.aggregate is not None and meets_bars(r.aggregate)[0]]
    demoted = not passing
    if passing:
        chosen = min(
            passing,
            key=lambda r: (r.loss if r.loss is not None else float("inf"), r.group.index),
        )
    else:
        chosen = min(
            results,
            key=lambda r: (r.loss if r.loss is not None else float("inf"), r.group.index),
        )
    recommended: tuple[float, int] | None
    demotion_path: tuple[str, ...]
    if chosen.aggregate is None:
        recommended = None
        demotion_path = ()
    else:
        recommended = (chosen.group.focal_floor, chosen.group.budget_chars)
        demotion_path = meets_bars(chosen.aggregate)[1] if demoted else ()
    return CoordinateDescentOutcome(
        groups=tuple(groups),
        results=tuple(results),
        recommended=recommended,
        demoted=demoted,
        demotion_path=demotion_path,
    )
