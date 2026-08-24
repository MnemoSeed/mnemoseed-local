"""Rescue-band matrix (design/09 §3.5, open question 3): parameter grid,
per-point metrics, aggregation, gate bars, and the frontier pick.

The executor is a pure function over a per-point metric oracle — the live
runner feeds it rig results, the unit tests feed it synthetic metrics, so the
selection math is pinned without any daemon. Bars follow the T4b discipline:
a gate tier every recommended point must satisfy, report-only scalars printed
on every line, and an honest demotion path when no grid point passes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mnemoseed_local.decay.reinforce import CANDIDATE_FLOOR

#: The swept rescue-band space: the floor's lower edge and the cue minimum.
RESCUE_PARAM_FLOORS: tuple[float, ...] = (0.15, 0.20, 0.25, 0.30, 0.35)
RESCUE_PARAM_CUE_MINS: tuple[float, ...] = (0.20, 0.30, 0.40, 0.50)

#: Gate bars (design/09 §3.5 acceptance): rescued pins must actually arrive,
#: and the band must stay tight — no plain chunk below the main floor, no
#: rebound failure on a served pin, nothing leaking out of the dead zone, and
#: rank discipline enforced (a rescued candidate never outranks a normal one).
TARGET_BARS: dict[str, float] = {
    "rescue_rate": 0.75,
    "noise_admission_rate": 0.0,
    "rebound_rate": 1.0,
    "dead_leak_rate": 0.0,
    "rank_discipline": 1.0,
}

#: Demotion ordering when no group passes / frontier pick among passers
#: (weighted loss over the gate tier plus the recovery scalar): the bars cap
#: the risk side, ``band_recovery_rate`` pushes the pick toward the deepest
#: band that still satisfies every bar (T4b budget-headroom precedent).
LOSS_WEIGHTS: dict[str, float] = {
    "band_recovery_rate": 0.4,
    "rescue_rate": 0.3,
    "rebound_rate": 0.2,
    "noise_admission_rate": 0.1,
}

_HIGHER_IS_BETTER: frozenset[str] = frozenset(
    {"rescue_rate", "rebound_rate", "band_recovery_rate", "rank_discipline"}
)

_AGG_SCALARS: dict[str, Callable[[RescueAggregate], float | None]] = {
    "rescue_rate": lambda agg: agg.rescue_rate,
    "noise_admission_rate": lambda agg: agg.noise_admission_rate,
    "rebound_rate": lambda agg: agg.rebound_rate,
    "dead_leak_rate": lambda agg: agg.dead_leak_rate,
    "band_recovery_rate": lambda agg: agg.band_recovery_rate,
    "rank_discipline": lambda agg: agg.rank_discipline,
}


@dataclass(frozen=True)
class RescuePointMetrics:
    """One material point's hand-computable evidence under one parameter set.

    ``in_band`` marks a pin the band COULD reach under this group (weight
    between the floor and the main floor) regardless of its cue verdict, so
    ``band_recovery_rate`` can discriminate floors and cue minimums that
    otherwise tie on the gate bars.
    """

    eligible: bool  # the band pin passes this group's thresholds
    served: bool  # observed among the response entries
    in_band: bool  # the pin sits inside this group's band at all
    noise_admitted: int  # sub-floor items served that must never be there
    rebound_ok: bool  # a served pin's weight increased (vacuous when unserved)
    dead_leaked: bool  # a dead-zone pin appeared as a served entry
    dead_residue_present: bool  # the dead-zone pin rendered its residue line
    rank_after_normal: bool | None  # served pin ranked behind the healthy chunk


@dataclass(frozen=True)
class RescueAggregate:
    """One parameter group's frontier scalars (rates over the points)."""

    rescue_rate: float | None
    noise_admission_rate: float | None
    rebound_rate: float | None
    dead_leak_rate: float | None
    band_recovery_rate: float | None  # served share of ALL in-band pins
    residue_coverage: float | None  # report-only
    rank_discipline: float | None  # gate bar: rescued never outrank normal
    points: int


@dataclass(frozen=True)
class RescueGroup:
    """One evaluated parameter combination."""

    index: int
    rescue_floor: float
    cue_min: float


@dataclass(frozen=True)
class RescueGroupResult:
    """A group's aggregate + its demotion loss (None when nothing was measurable)."""

    group: RescueGroup
    aggregate: RescueAggregate | None
    loss: float | None


@dataclass(frozen=True)
class RescueDescentOutcome:
    """The full grid trajectory plus the frontier recommendation."""

    groups: tuple[RescueGroup, ...]
    results: tuple[RescueGroupResult, ...]
    recommended: tuple[float, float] | None  # (rescue_floor, cue_min)
    demoted: bool  # True when the empty-frontier fallback path was used
    demotion_path: tuple[str, ...]  # the bars the recommended point misses


MetricFn = Callable[[float, float], list[RescuePointMetrics]]


def pin_is_eligible(pin_decay: float, cue_overlap: float, *, rescue_floor: float, cue_min: float) -> bool:
    """Band membership: at/above the rescue floor, strictly below the main
    candidate floor, with a cue overlap at/above the minimum."""
    return rescue_floor <= pin_decay < CANDIDATE_FLOOR and cue_overlap >= cue_min


def aggregate_rescue_metrics(metrics: list[RescuePointMetrics]) -> RescueAggregate:
    """Rates over the point batch; unknowns stay honest (never invented zeros)."""
    points = len(metrics)
    eligible = [m for m in metrics if m.eligible]
    served_eligible = [m for m in eligible if m.served]
    served_any = [m for m in metrics if m.served]
    in_band = [m for m in metrics if m.in_band]
    recovered = [m for m in in_band if m.served]
    ranks = [m.rank_after_normal for m in metrics if m.rank_after_normal is not None]

    def _ratio(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    return RescueAggregate(
        rescue_rate=_ratio(len(served_eligible), len(eligible)),
        noise_admission_rate=(sum(m.noise_admitted for m in metrics) / points if points else None),
        rebound_rate=_ratio(sum(1 for m in served_any if m.rebound_ok), len(served_any)),
        dead_leak_rate=(sum(1 for m in metrics if m.dead_leaked) / points if points else None),
        band_recovery_rate=_ratio(len(recovered), len(in_band)),
        residue_coverage=_ratio(sum(1 for m in metrics if m.dead_residue_present), points),
        rank_discipline=_ratio(sum(1 for r in ranks if r), len(ranks)),
        points=points,
    )


def meets_rescue_bars(agg: RescueAggregate | None) -> tuple[bool, tuple[str, ...]]:
    """Every gate bar satisfied? An unknown metric fails its bar honestly."""
    if agg is None:
        return False, tuple(TARGET_BARS)
    failed = [
        name
        for name, bar in TARGET_BARS.items()
        if _fails_bar(_AGG_SCALARS[name](agg), bar, name in _HIGHER_IS_BETTER)
    ]
    return (not failed), tuple(failed)


def _fails_bar(value: float | None, bar: float, higher_is_better: bool) -> bool:
    if value is None:
        return True
    ok = value >= bar if higher_is_better else value <= bar
    return not ok


def weighted_loss(agg: RescueAggregate | None) -> float | None:
    """Weighted loss over the gate scalars; a missing metric costs full weight."""
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


def rescue_grid_descent(metric_fn: MetricFn) -> RescueDescentOutcome:
    """Full grid over (rescue_floor × cue_min): small enough to exhaust, so no
    coordinate-descent anchoring heuristics are needed."""
    groups: list[RescueGroup] = []
    results: list[RescueGroupResult] = []
    index = 0
    for floor in RESCUE_PARAM_FLOORS:
        for cue_min in RESCUE_PARAM_CUE_MINS:
            group = RescueGroup(index=index, rescue_floor=floor, cue_min=cue_min)
            groups.append(group)
            aggregate = aggregate_rescue_metrics(metric_fn(floor, cue_min))
            results.append(RescueGroupResult(group, aggregate, weighted_loss(aggregate)))
            index += 1

    passing = [r for r in results if r.aggregate is not None and meets_rescue_bars(r.aggregate)[0]]
    demoted = not passing
    chosen = min(
        passing or results,
        key=lambda r: (r.loss if r.loss is not None else float("inf"), r.group.index),
    )
    recommended: tuple[float, float] | None
    demotion_path: tuple[str, ...]
    if chosen.aggregate is None:
        recommended = None
        demotion_path = ()
    else:
        recommended = (chosen.group.rescue_floor, chosen.group.cue_min)
        demotion_path = meets_rescue_bars(chosen.aggregate)[1] if demoted else ()
    return RescueDescentOutcome(
        groups=tuple(groups),
        results=tuple(results),
        recommended=recommended,
        demoted=demoted,
        demotion_path=demotion_path,
    )
