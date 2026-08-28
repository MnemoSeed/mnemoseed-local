"""Warm-needle ε=0 baseline aggregation (design/10 §5.2, Gate 2).

The activation mechanism does NOT exist yet; this is the pure-measurement
baseline the future activation is judged against. Every aggregate therefore
carries the honest activation-off state (ε=0) as a first-class part of the
report — never an implied boost. The aggregation is a pure function over a
per-probe oracle, so the math is pinned without any daemon.
"""

from __future__ import annotations

from dataclasses import dataclass

from mnemoseed_local.eval.warm_materials import (
    WINDOW_DELAYED,
    WINDOW_IMMEDIATE,
    WINDOW_NEGATIVE_CONTROL,
)

#: The ε value of the baseline run: the activation weight is zero because the
#: mechanism does not exist yet — this instrument records current behavior
#: against which a future activation is later measured.
WARM_EPSILON_BASELINE: float = 0.0

#: The windows the instrument measures (one aggregate per window, including the
#: decoy-aligned negative control that proves non-surfacing is measurable).
WARM_WINDOWS: tuple[str, ...] = (WINDOW_IMMEDIATE, WINDOW_DELAYED, WINDOW_NEGATIVE_CONTROL)


@dataclass(frozen=True)
class WarmProbeMetrics:
    """One measured warm-window probe of one material point.

    ``first_surfaced`` is the warm precondition (the first query recalled the
    fact); ``re_surfaced`` ``re_rank`` ``re_score`` observe whether/at what
    rank+score the changed-wording re-query re-surfaced the SAME fact. An
    unknown is an honest None, never an invented zero.
    """

    window: str
    delay_s: float
    first_surfaced: bool
    re_surfaced: bool
    first_score: float | None  # the first query's fused score for the fact
    re_score: float | None  # the re-query's fused score where it re-surfaced
    re_rank: int | None  # 1-based rank in the re-query's entries, else None


@dataclass(frozen=True)
class WarmAggregate:
    """One warm window's scalar rates over the batch of probes."""

    window: str
    first_surface_rate: float | None  # share of probes whose first query recalled the fact
    re_surface_rate: float | None  # share of warmed probes that re-surfaced (None when none warmed)
    median_re_score: float | None  # median re-query score where the fact re-surfaced
    points: int


@dataclass(frozen=True)
class WarmBaselineReport:
    """The ε=0 baseline: the honest activation-off state plus per-window rates."""

    points: int
    activation_enabled: bool  # always False at baseline
    activation_eps: float  # always WARM_EPSILON_BASELINE (0.0)
    aggregates: tuple[WarmAggregate, ...]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def aggregate_warm_probes(probes: list[WarmProbeMetrics]) -> WarmBaselineReport:
    """Rates per warm window over the batch; unknowns stay honest (never
    invented zeros). Re-surfacing is only measurable where the first query
    recalled the fact (the warm precondition)."""
    by_window: dict[str, list[WarmProbeMetrics]] = {window: [] for window in WARM_WINDOWS}
    for probe in probes:
        by_window.setdefault(probe.window, []).append(probe)

    aggregates: list[WarmAggregate] = []
    for window in WARM_WINDOWS:
        group = by_window[window]
        points = len(group)
        first_surfaced = [p for p in group if p.first_surfaced]
        re_surfaced = [p for p in first_surfaced if p.re_surfaced]
        first_surface_rate = len(first_surfaced) / points if points else None
        re_surface_rate = len(re_surfaced) / len(first_surfaced) if first_surfaced else None
        median_score = _median([p.re_score for p in re_surfaced if p.re_score is not None])
        aggregates.append(
            WarmAggregate(
                window=window,
                first_surface_rate=first_surface_rate,
                re_surface_rate=re_surface_rate,
                median_re_score=median_score,
                points=points,
            )
        )
    return WarmBaselineReport(
        points=len(probes),
        activation_enabled=False,
        activation_eps=WARM_EPSILON_BASELINE,
        aggregates=tuple(aggregates),
    )
