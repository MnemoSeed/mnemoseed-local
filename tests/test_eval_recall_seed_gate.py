"""The recall calibration CLI's seed gate.

Material identity is part of the calibration bar: the descent's demotion bars
hold only for the pinned catalog seed, so a non-default ``--seed`` must fail
loudly instead of silently running the default catalog.
"""

from __future__ import annotations

import pytest

from mnemoseed_local.eval import __main__ as eval_main
from mnemoseed_local.eval.__main__ import main
from mnemoseed_local.eval.recall_materials import RECALL_MATERIALS_SEED
from mnemoseed_local.eval.recall_matrix import CoordinateDescentOutcome


def test_recall_cli_rejects_a_non_default_seed(monkeypatch, capsys) -> None:
    """A non-default --seed is a usage error (exit 2), raised before any rig
    work — never a silent run of the default catalog."""
    touched = False

    def fake_descent(metric_fn, **kwargs):
        nonlocal touched
        touched = True
        return CoordinateDescentOutcome(
            groups=(), results=(), recommended=None, demoted=True, demotion_path=()
        )

    monkeypatch.setattr(eval_main, "coordinate_descent", fake_descent)
    monkeypatch.setattr(eval_main, "run_calibration_point", lambda mat, **kwargs: None)
    with pytest.raises(SystemExit) as excinfo:
        main(["recall", "--seed", str(RECALL_MATERIALS_SEED + 1)])
    assert excinfo.value.code == 2
    assert "seed override not supported" in capsys.readouterr().err
    assert touched is False, "the descent ran on the default catalog despite the rejected seed"


def test_recall_cli_default_seed_reaches_the_catalog(monkeypatch, capsys) -> None:
    """The default seed passes validation and drives the pinned 24-point
    catalog through the coordinate descent."""
    seen: dict[str, int] = {}

    def fake_descent(metric_fn, **kwargs):
        seen["points"] = len(metric_fn(0.4, 1200))
        return CoordinateDescentOutcome(
            groups=(), results=(), recommended=None, demoted=True, demotion_path=()
        )

    monkeypatch.setattr(eval_main, "coordinate_descent", fake_descent)
    monkeypatch.setattr(eval_main, "run_calibration_point", lambda mat, **kwargs: None)
    rc = main(["recall"])
    assert seen["points"] == 24
    assert rc == 1  # the empty fake frontier demotes: nonzero without a recommendation
    assert "seed" not in capsys.readouterr().err.lower()
