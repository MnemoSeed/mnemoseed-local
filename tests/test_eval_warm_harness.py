"""Warm-needle measurement rig (design/10 §5.2, Gate 2): the ε=0 baseline
run path over the REAL recall daemon.

Drives /memory/recall for one warm-needle point: seed the needle fact AND its
same-band decoy in one rig, run each warm-window probe (first query recalls
the needle, then a changed-wording re-query — separated by the probe's declared
delay), and read back whether/at what score+rank the same fact re-surfaces. The
negative-control probe is decoy-aligned: a decoy's cue must win, so the needle
does NOT surface — proving the instrument's metric can move (surfaced on a
needle-aligned re-query, absent on a decoy-aligned control). Zero runtime
retrieval changes: the daemon's served responses are measured as-is, and the
report marks the run activation-off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.eval.warm_harness import (
    RigRootNotFresh,
    WarmRig,
    run_warm_point,
)
from mnemoseed_local.eval.warm_materials import (
    WINDOW_DELAYED,
    WINDOW_IMMEDIATE,
    WINDOW_NEGATIVE_CONTROL,
    warm_materials,
)


@pytest.fixture
def rig_root(tmp_path: Path) -> Path:
    return tmp_path / "rig"


def test_warm_rig_refuses_a_dirty_root(rig_root: Path) -> None:
    """Materialization is fail-loud (shared contract): prior state under the
    root is contamination evidence, never wiped."""
    rig_root.mkdir(parents=True)
    (rig_root / "config.toml").write_text("preset = 'embedded'\n", encoding="utf-8")

    with pytest.raises(RigRootNotFresh):
        WarmRig(rig_root)


def test_warm_baseline_point_metric_can_move(rig_root: Path) -> None:
    """One warm-needle point on the REAL daemon surface, with a competing
    same-band decoy seeded alongside the needle. The instrument's metric must
    genuinely move: (a) the needle re-surfaces top on the needle-aligned
    immediate re-query, and (b) it does NOT re-surface on the decoy-aligned
    negative control — proving non-surfacing is measurable, not a vacuous
    flatline rank-1. The injected fake sleep records the warm-window delays
    without letting wall-clock slow the test."""
    material = warm_materials()[0]
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    result = run_warm_point(material, root=rig_root, sleep=fake_sleep)

    # one (first query + re-query) pass per declared probe
    assert len(slept) == len(material.probes)
    assert len(result.probe_metrics) == len(material.probes)
    by_window = {metric.window: metric for metric in result.probe_metrics}

    immediate = by_window[WINDOW_IMMEDIATE]
    assert immediate.first_surfaced is True, "the engineered fact must be recalled first"
    assert immediate.re_surfaced is True, "the needle-aligned re-query must re-surface the needle"
    assert immediate.re_rank == 1, "the needle must hold its top slot ahead of the decoys"

    delayed = by_window[WINDOW_DELAYED]
    assert delayed.first_surfaced is True
    # the delayed window's decoy-leaning re-query lets the same-band decoys
    # claim the slots and the needle drops OUT of top-k — the decay the window
    # exists to probe, never a frozen identical rank-1 across both windows.
    assert delayed.re_surfaced is False, "the decoys must claim the delayed window"
    assert delayed.re_rank is None

    negative = by_window[WINDOW_NEGATIVE_CONTROL]
    assert negative.first_surfaced is True
    assert negative.re_surfaced is False, "the decoy-aligned control must NOT re-surface the needle"
    assert negative.re_rank is None

    # the metric can move: the needle surfaces on the needle-aligned re-query
    # and is absent on the decoy-aligned control, and its rank drops across the
    # warm window — the report is discriminative, never a vacuous flatline.
    assert {metric.re_surfaced for metric in result.probe_metrics if metric.first_surfaced} == {True, False}

    # the recorded delays match the declared warm window
    assert slept[0] == 0.0
    assert slept[1] > 0.0
    # the ε=0 baseline never injects a boost: the served observation is as-is
    assert result.activation_enabled is False
    assert result.activation_eps == 0.0
