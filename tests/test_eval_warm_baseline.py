"""Warm-needle ε=0 baseline aggregation (design/10 §5.2, Gate 2).

The activation mechanism does NOT exist yet; this is the pure-measurement
baseline the future activation is judged against. Every aggregate therefore
carries the honest activation-off state (ε=0) as a first-class part of the
report — never an implied boost. The aggregation is a pure function over a
per-probe oracle, so the math is pinned without any daemon.
"""

from __future__ import annotations

from mnemoseed_local.eval.warm_materials import WINDOW_DELAYED, WINDOW_NEGATIVE_CONTROL
from mnemoseed_local.eval.warm_matrix import (
    WARM_EPSILON_BASELINE,
    WarmAggregate,
    WarmBaselineReport,
    WarmProbeMetrics,
    aggregate_warm_probes,
)


def _probe(
    *,
    surface: bool,
    score: float | None = 0.9,
    window: str = "immediate",
    first: bool = True,
) -> WarmProbeMetrics:
    return WarmProbeMetrics(
        window=window,
        delay_s=0.0 if window == "immediate" else 5.0,
        first_surfaced=first,
        re_surfaced=surface,
        first_score=1.0 if first else None,
        re_score=score if (surface and score is not None) else None,
        re_rank=1 if (surface and score is not None) else None,
    )


def test_warm_probe_metrics_carries_honest_unknowns() -> None:
    """A never-surfaced re-query is an honest None, never an invented zero —
    the instrument cannot claim a failed re-surfacing the way a quality bar."""
    probe = WarmProbeMetrics(
        window="immediate",
        delay_s=0.0,
        first_surfaced=True,
        re_surfaced=False,
        first_score=0.9,
        re_score=None,
        re_rank=None,
    )
    assert probe.re_score is None
    assert probe.re_rank is None


def test_re_surface_rate_is_measured_vs_the_warm_precondition() -> None:
    """Re-surfacing is only measurable where the first query recalled the fact
    (the warm precondition); unknown (None) when nothing warmed first."""
    agg = aggregate_warm_probes(
        [
            _probe(surface=True, first=True),
            _probe(surface=False, first=True),
            _probe(surface=True, first=False),  # never warmed first: excluded
        ]
    )
    by_window = {a.window: a for a in agg.aggregates}
    runner = by_window["immediate"]
    # two probes warmed first; one re-surfaced
    assert runner.re_surface_rate == 0.5
    assert runner.first_surface_rate == 2 / 3


def test_unmeasured_window_reports_none_honestly() -> None:
    agg = aggregate_warm_probes([])
    by_window = {a.window: a for a in agg.aggregates}
    assert by_window["immediate"].re_surface_rate is None
    assert by_window["immediate"].points == 0


def test_baseline_report_is_honestly_activation_off() -> None:
    """The ε=0 baseline must self-identify: activation is OFF and ε is the
    documented zero baseline — the report states it as a field, never assumes
    the reader knows it is only a measurement instrument."""
    report = WarmBaselineReport(
        points=1, activation_enabled=False, activation_eps=WARM_EPSILON_BASELINE, aggregates=(aggr(),)
    )
    assert report.activation_enabled is False
    assert report.activation_eps == 0.0


def aggr() -> WarmAggregate:
    return aggregate_warm_probes([_probe(surface=True)]).aggregates[0]


def test_aggregate_medians_come_from_first_surfaced_only() -> None:
    """The re-query score the baseline reports is measured where the fact
    actually re-surfaced — never fabricated from missing rows."""
    blocked = _probe(surface=False)
    surfaced = _probe(surface=True, score=0.7)
    agg = aggregate_warm_probes([blocked, surfaced])
    runner = next(a for a in agg.aggregates if a.window == "immediate")
    assert runner.median_re_score == 0.7


def test_negative_control_reports_needle_absence_honestly() -> None:
    """The decoy-aligned negative control proves discriminability: the needle
    does NOT re-surface, and that absence is a real measured value (re_surface
    rate 0.0 from the warmed precondition), never an invented zero."""
    negative = WarmProbeMetrics(
        window=WINDOW_NEGATIVE_CONTROL,
        delay_s=0.0,
        first_surfaced=True,
        re_surfaced=False,
        first_score=0.9,
        re_score=None,
        re_rank=None,
    )
    runner = next(
        a for a in aggregate_warm_probes([negative]).aggregates if a.window == WINDOW_NEGATIVE_CONTROL
    )
    assert runner.first_surface_rate == 1.0  # the needle warmed first
    assert runner.re_surface_rate == 0.0  # the decoy won; honestly measured absence
    assert runner.median_re_score is None  # nothing re-surfaced: honest None


def test_windows_report_genuinely_differing_rates() -> None:
    """Immediate vs delayed are not an identical flatline: immediate re-surfaces
    the needle, the cue-weakened delayed window (decoy wins) and the negative
    control do not — so the per-window aggregates genuinely differ."""
    probes = [
        _probe(surface=True, window="immediate"),
        _probe(surface=False, window="delayed"),
        WarmProbeMetrics(
            window=WINDOW_NEGATIVE_CONTROL,
            delay_s=0.0,
            first_surfaced=True,
            re_surfaced=False,
            first_score=0.9,
            re_score=None,
            re_rank=None,
        ),
    ]
    by_window = {a.window: a for a in aggregate_warm_probes(probes).aggregates}
    assert by_window["immediate"].re_surface_rate == 1.0
    assert by_window[WINDOW_DELAYED].re_surface_rate == 0.0
    assert by_window[WINDOW_NEGATIVE_CONTROL].re_surface_rate == 0.0
    assert by_window["immediate"].re_surface_rate != by_window[WINDOW_DELAYED].re_surface_rate
