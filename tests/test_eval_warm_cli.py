"""The warm-needle CLI baseline path (design/10 §5.2, Gate 2).

A maintainer runs ``python -m mnemoseed_local.eval warm`` to produce the ε=0
baseline. The subcommand must reach the pinned material factory, drive every
point's probes through the rig, and report the measurement honestly as
activation-off — never simulating a boost that does not exist, and never
declaring success when no measurement was produced.
"""

from __future__ import annotations

import pytest

from mnemoseed_local.eval import __main__ as eval_main
from mnemoseed_local.eval.__main__ import main
from mnemoseed_local.eval.warm_materials import WARM_MATERIALS_SEED
from mnemoseed_local.eval.warm_matrix import WARM_EPSILON_BASELINE, WarmProbeMetrics


def test_warm_cli_runs_the_baseline_over_the_material_factory(monkeypatch, capsys) -> None:
    """The ``warm`` subcommand materializes the pinned warm-needle catalog,
    runs one baseline point per material with a REAL (non-empty) per-probe
    measurement, prints each probe line, and reports the activation-off state."""
    seen = {"points": 0, "eps": None, "probe_lines": 0}

    def fake_run_point(material, *, root, sleep=0.0):
        seen["points"] += 1
        seen["eps"] = eval_main.WARM_EPSILON_BASELINE
        from mnemoseed_local.eval.warm_harness import WarmPointResult

        metrics = tuple(
            WarmProbeMetrics(
                window=probe.window,
                delay_s=probe.delay_s,
                first_surfaced=True,
                re_surfaced=probe.window != "negative_control",
                first_score=0.9,
                re_score=0.9 if probe.window != "negative_control" else None,
                re_rank=1 if probe.window != "negative_control" else None,
            )
            for probe in material.probes
        )
        seen["probe_lines"] += len(metrics)
        return WarmPointResult(
            point_id=material.point_id,
            activation_enabled=False,
            activation_eps=WARM_EPSILON_BASELINE,
            probe_metrics=metrics,
        )

    monkeypatch.setattr(eval_main, "run_warm_point", fake_run_point)
    rc = main(["warm", "--seed", str(WARM_MATERIALS_SEED)])
    assert rc == 0
    parsed = capsys.readouterr().out
    assert "activation-off" in parsed.lower()
    assert seen["points"] == len(eval_main.warm_materials())
    # the run must actually have produced per-probe measurements — never an
    # empty `run_warm_point` return masquerading as a successful baseline.
    assert seen["probe_lines"] >= 3


def test_warm_cli_rejects_a_non_default_seed(monkeypatch, capsys) -> None:
    """Material identity is part of the measurement's comparability: a
    non-default ``--seed`` fails loudly instead of running another catalog."""
    touched = False

    def fake_run_point(material, *, root, sleep=0.0):
        nonlocal touched
        touched = True

    monkeypatch.setattr(eval_main, "run_warm_point", fake_run_point)
    with pytest.raises(SystemExit) as excinfo:
        main(["warm", "--seed", str(WARM_MATERIALS_SEED + 1)])
    assert excinfo.value.code == 2
    assert "seed override not supported" in capsys.readouterr().err
    assert touched is False
