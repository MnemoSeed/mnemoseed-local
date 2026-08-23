"""T4b caller contract — the calibration loop's per-point rig isolation.

The harness contract scopes each evaluation point under
``workdir / runs / <run-id> / <point_id>``; the caller (the ``recall``
command's metric oracle) must honor it: one fresh, serially context-managed
rig per material, deterministic root names, no retries. Artifact hygiene:
a point's rig root is deleted on success and kept on failure — a rerun over
kept forensics then fails loudly instead of wiping evidence.

Synthetic mini-materials only: the seeded 24-point catalog stays
calibration-comparable and is never load-bearing for this oracle.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_eval_recall_harness import _mini_material

from mnemoseed_local.eval.__main__ import run_calibration_point
from mnemoseed_local.eval.recall_harness import RecallRig, RigRootNotFresh
from mnemoseed_local.eval.recall_metrics import RecallMetrics

_RUN_ID = "f0.4-b1200"


def _runs_root(tmp_path: Path) -> Path:
    return tmp_path / "workdir" / "runs" / _RUN_ID


def test_calibration_point_is_per_point_isolated_and_hygienic(tmp_path: Path) -> None:
    """Two synthetic points sharing a miss text run through the caller's
    per-point shape: hand-computable isolated metrics, and no rig-root
    accumulation after success (deterministic names stay re-claimable)."""
    shared_miss = "FalconDb focuses on nightly backups and archival"
    materials = [
        _mini_material("syn-a", "AtlasDb", miss_text=shared_miss),
        _mini_material("syn-b", "NimbusDb", miss_text=shared_miss),
    ]
    runs_root = _runs_root(tmp_path)
    for material in materials:
        metrics = run_calibration_point(material, runs_root=runs_root, focal_floor=0.4, budget_chars=1200)
        _assert_isolated_short_point(metrics)
    # success deleted every point root: no accumulation, names re-claimable
    assert list(runs_root.iterdir()) == []
    rerun = run_calibration_point(materials[0], runs_root=runs_root, focal_floor=0.4, budget_chars=1200)
    _assert_isolated_short_point(rerun)


def test_calibration_point_keeps_root_on_failure(tmp_path: Path) -> None:
    """A non-fresh root (kept forensics from a failed point) fails loudly and
    the evidence survives untouched — never silently wiped, never retried."""
    material = _mini_material("syn-a", "AtlasDb")
    stale = _runs_root(tmp_path) / material.point_id
    stale.mkdir(parents=True)
    forensic = stale / "forensic.txt"
    forensic.write_text("keep me", encoding="utf-8")
    with pytest.raises(RigRootNotFresh, match="not fresh"):
        run_calibration_point(material, runs_root=_runs_root(tmp_path), focal_floor=0.4, budget_chars=1200)
    assert forensic.read_text(encoding="utf-8") == "keep me"


def test_calibration_point_midrun_failure_keeps_root_and_leaves_it_deletable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure AFTER materialization keeps the root as forensics — and the
    retained evidence stays deletable: the rig exit released its daemon.log
    pin, so hygiene cleanup can remove the root without fighting the logger."""
    material = _mini_material("syn-a", "AtlasDb")

    def _boom(self: RecallRig, _material: object) -> object:
        raise RuntimeError("mid-run boom")

    monkeypatch.setattr(RecallRig, "run_material", _boom)
    kept = _runs_root(tmp_path) / material.point_id
    with pytest.raises(RuntimeError, match="mid-run boom"):
        run_calibration_point(material, runs_root=_runs_root(tmp_path), focal_floor=0.4, budget_chars=1200)
    assert kept.exists()
    assert (kept / "config.toml").exists()  # the materialized world survives
    shutil.rmtree(kept)
    assert not kept.exists()


def _assert_isolated_short_point(metrics: RecallMetrics) -> None:
    """The isolated short-point picture: exactly the fact + support served,
    both genuinely referenced, no serveable noise, no detector error, and a
    zero weak-association probe (no foreign transcript chunks visible)."""
    assert metrics.recall_at_k == (0.5, 1.0, 1.0, 1.0)
    assert metrics.precision_at_k == (1.0, 1.0, 1.0, 1.0)
    assert metrics.floor_fp is None  # honest unknown: the noise pool is empty
    assert metrics.detector_fp == 0.0
    assert metrics.fn_rate == 0.0
    assert metrics.non_focal_above_floor == 0
