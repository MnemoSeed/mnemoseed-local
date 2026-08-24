"""``uv run python -m mnemoseed_local.eval`` — the eval harness entry (B3 T4).

NOT a product surface: no ``mnemoseed`` CLI verb, no daemon endpoint. Three
subcommands:

- ``matrix``: roster x ensemble over the material catalog; reports accumulate
  as JSON under ``<CONFIG_DIR>/eval`` (or ``--out``). Use ``--list`` for a
  side-effect-free preview of the expanded cells. The verifier seat is
  uniform per run (``--verifier``); run twice with swapped A/B anchors for
  the bidirectional pair column (B1 live-record shape).
- ``canary``: seconds-fast stub-seat self-check (recall must be 1.0, noise
  pollution 0) — the pre-live gate proving the harness itself is sound.
- ``rescore``: offline re-judge of a v1.1 report's recall after a ruler
  revision — the embedded triple payload + rebuilt canary truth, no GPU.

Exit codes are ``matrix.matrix_exit_code`` semantics: missing models are
skips (0), material/run failures are failures (1).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from mnemoseed_local.eval.canary import canary_session
from mnemoseed_local.eval.harness import EvalCell, EvalRoute
from mnemoseed_local.eval.materials import DEFAULT_CANARY_COUNT, DEFAULT_CANARY_SEED, material_catalog
from mnemoseed_local.eval.matrix import (
    DEFAULT_BASE_URL,
    DEFAULT_NUM_CTX,
    ROSTER_DEFAULT,
    SEAT_SEED_DEFAULT,
    default_matrix,
    list_cells,
    matrix_exit_code,
    parse_extra_route,
    run_matrix,
    summary_lines,
)
from mnemoseed_local.eval.recall_harness import RecallRig
from mnemoseed_local.eval.recall_materials import (
    RECALL_MATERIALS_SEED,
    RecallMaterial,
    recall_materials,
)
from mnemoseed_local.eval.recall_matrix import (
    PARAM_BUDGETS,
    PARAM_FLOORS,
    START_BUDGET,
    START_FLOOR,
    CoordinateDescentOutcome,
    coordinate_descent,
)
from mnemoseed_local.eval.recall_metrics import RecallMetrics, score_recall
from mnemoseed_local.eval.report import default_out_dir, write_report
from mnemoseed_local.eval.rescue_harness import run_rescue_point
from mnemoseed_local.eval.rescue_materials import rescue_materials
from mnemoseed_local.eval.rescue_matrix import (
    RescuePointMetrics,
    rescue_grid_descent,
)


def _matrix_command(args: argparse.Namespace) -> int:
    models = [m.strip() for m in args.models.split(",") if m.strip()] or list(ROSTER_DEFAULT)
    ensembles = [e.strip() for e in args.ensemble.split(",") if e.strip()]
    seat_seed = None if args.no_seat_seed else SEAT_SEED_DEFAULT
    extra_routes = [parse_extra_route(spec, seat_seed=seat_seed) for spec in args.extra_route]
    cells = default_matrix(
        roster=models,
        ensembles=ensembles,
        verifier_model=args.verifier,
        base_url=args.base_url,
        num_ctx=args.num_ctx,
        delta_budget_tokens=args.delta_budget,
        extra_routes=extra_routes,
        seat_seed=seat_seed,
    )
    if args.list:
        for cell_id in list_cells(cells):
            print(cell_id)
        return 0
    materials = material_catalog(
        Path(args.materials_dir) if args.materials_dir else None,
        canary_seed=args.seed,
        canary_count=args.canary_count,
    )
    report = run_matrix(cells, materials, root=Path(args.workdir), base_url=args.base_url)
    out_dir = Path(args.out) if args.out else default_out_dir()
    path = write_report(report, out_dir, matrix_slug=f"{len(cells)}cells-{len(materials)}materials")
    for line in summary_lines(report):
        print(line)
    print(f"report: {path}")
    return matrix_exit_code(report)


def _canary_command(args: argparse.Namespace) -> int:
    """Stub-seat self-check: the harness proves itself before any live model."""
    from mnemoseed_local.eval.harness import EvalRig, RigPaths
    from mnemoseed_local.eval.metrics import score_canary

    session = canary_session(args.seed)
    failures = 0
    for ensemble in ("off", "verify"):
        rig = EvalRig(
            RigPaths(root=Path(args.workdir) / f"selfcheck-{ensemble}"),
            EvalCell(
                reflect=EvalRoute(driver="stub", model="stub-a"),
                ensemble=ensemble,
                verifier=EvalRoute(driver="stub_verifier", model="stub-b"),
            ),
        )
        try:
            metrics = score_canary(session, rig.run_canary(session))
        finally:
            rig.close()
        ok = metrics.canary_recall == 1.0 and metrics.noise_pollution == 0
        failures += 0 if ok else 1
        status = "PASS" if ok else "FAIL"
        print(
            f"[{ensemble}] {status} recall={metrics.canary_recall} "
            f"pollution={metrics.noise_pollution} core={metrics.core_yield}"
        )
    return 0 if failures == 0 else 1


def _rescore_command(args: argparse.Namespace) -> int:
    """Offline re-judge of a v1.1 report's recall surface (B3.1 T2) — the
    embedded triple payload + rebuilt canary truth, zero GPU."""
    from mnemoseed_local.eval.rescore import rescore_report

    out = rescore_report(
        Path(args.report),
        canary_seed=args.seed,
        out_dir=Path(args.out) if args.out else None,
    )
    print(f"rescored: {out}")
    return 0


def write_calibration_defaults(config_path: Path, focal_floor: float, budget_chars: int) -> bool:
    """Land a calibration outcome into config.py by hand-editing rules.

    The effective defaults are the UPPERCASE module constants; the lowercase
    ``auto_recall_*`` assignments exist only inside the generated-toml
    template comments and are kept in sync. Returns False (writing nothing)
    unless every constant line matched — a partial match would silently
    drift documentation from behavior.
    """
    content = config_path.read_text(encoding="utf-8")
    replacements = (
        (r"(DEFAULT_AUTO_RECALL_FOCAL_FLOOR:\s*float\s*=\s*)[\d.]+", repr(focal_floor)),
        (r"(DEFAULT_AUTO_RECALL_BUDGET_CHARS:\s*int\s*=\s*)\d+", str(budget_chars)),
        (r"(#\s*auto_recall_focal_floor\s*=\s*)[\d.]+", repr(focal_floor)),
        (r"(#\s*auto_recall_budget_chars\s*=\s*)\d+", str(budget_chars)),
    )
    updated = content
    for pattern, value in replacements:
        updated, count = re.subn(pattern, rf"\g<1>{value}", updated)
        if count == 0:
            return False
    config_path.write_text(updated, encoding="utf-8")
    return True


def run_calibration_point(
    material: RecallMaterial,
    *,
    runs_root: Path,
    focal_floor: float,
    budget_chars: int,
) -> RecallMetrics:
    """One evaluation point on its own rig, per the harness contract.

    The rig root is the deterministic ``<runs_root>/<point_id>``, lifecycle is
    strict serial context-managed, and there are no retries: any failure
    propagates and KEEPS the root as forensics, while success deletes it so
    deterministic names stay re-claimable by a later group.
    """
    root = runs_root / material.point_id
    with RecallRig(root, focal_floor=focal_floor, budget_chars=budget_chars) as rig:
        result = rig.run_material(material)
    metrics = score_recall(result)
    shutil.rmtree(root)
    return metrics


def _recall_command(args: argparse.Namespace) -> int:
    """T4b live calibration: run coordinate descent over the T2 pipeline rig
    and emit recommended (focal_floor, budget_chars). Optionally writes to
    config.py default values."""

    # Build a metric_fn that gives every material point its own freshly
    # materialized rig (per-point isolation) and returns the 24 RecallMetrics
    # for the coordinate descent.
    materials = recall_materials()

    def metric_fn(floor: float, budget: int) -> list[RecallMetrics]:
        runs_root = Path(args.workdir) / "runs" / f"f{floor}-b{budget}"
        return [
            run_calibration_point(mat, runs_root=runs_root, focal_floor=floor, budget_chars=budget)
            for mat in materials
        ]

    outcome: CoordinateDescentOutcome = coordinate_descent(
        metric_fn,
        floors=PARAM_FLOORS,
        budgets=PARAM_BUDGETS,
        start_floor=START_FLOOR,
        start_budget=START_BUDGET,
    )

    print(f"Coordinate descent completed: {len(outcome.groups)} groups")
    if outcome.recommended is None:
        print("No runnable groups — demotion path empty")
        return 1

    floor, budget = outcome.recommended
    status = "DEMOTED" if outcome.demoted else "ACCEPTED"
    print(f"Recommended: focal_floor={floor}, budget_chars={budget} ({status})")
    if outcome.demoted:
        print(f"Missed bars: {', '.join(outcome.demotion_path)}")

    # Print the final frontier summary
    print("\nFrontier summary (median over 24 points):")

    def fmt(value: float | None) -> str:
        # a vacuous scalar (empty pool, e.g. floor_fp with no serveable noise)
        # is an honest None, never a formatted zero
        return "None" if value is None else f"{value:.3f}"

    for res in outcome.results:
        if res.aggregate is None:
            print(f"  floor={res.group.focal_floor:.2f} budget={res.group.budget_chars}: NO DATA")
            continue
        agg = res.aggregate
        print(
            f"  floor={res.group.focal_floor:.2f} budget={res.group.budget_chars}: "
            f"R@5={fmt(agg.recall_at_5)} P@5={fmt(agg.precision_at_5)} "
            f"floor_fp={fmt(agg.floor_fp)} detector_fp={fmt(agg.detector_fp)} "
            f"fn_rate={fmt(agg.fn_rate)} overhead={fmt(agg.token_overhead)} "
            f"points={agg.points}"
        )

    # Write to config.py if requested
    if args.write_config:
        config_path = Path("src/mnemoseed_local/config.py")
        if write_calibration_defaults(config_path, floor, budget):
            print(f"Updated {config_path} with focal_floor={floor}, budget_chars={budget}")
        else:
            print(f"FAILED to update {config_path}: calibration constants not found")
            return 1

    return 0


def write_rescue_defaults(config_path: Path, rescue_floor: float, rescue_cue_min: float) -> bool:
    """Land an ACCEPTED rescue-band outcome into config.py (T4b rules)."""
    content = config_path.read_text(encoding="utf-8")
    replacements = (
        (r"(DEFAULT_RECALL_RESCUE_FLOOR:\s*float\s*=\s*)[\d.]+", repr(rescue_floor)),
        (r"(DEFAULT_RECALL_RESCUE_CUE_MIN:\s*float\s*=\s*)[\d.]+", repr(rescue_cue_min)),
    )
    updated = content
    for pattern, value in replacements:
        updated, count = re.subn(pattern, rf"\g<1>{value}", updated)
        if count == 0:
            return False
    config_path.write_text(updated, encoding="utf-8")
    return True


def _rescue_command(args: argparse.Namespace) -> int:
    """Rescue-band calibration (design/09 §3.5): exhaust the small
    (rescue_floor × cue_min) grid over the MCP-recall rig and emit the
    recommended pair under the gate bars. Synthetic embedder — no live models."""

    materials = rescue_materials()

    def metric_fn(floor: float, cue_min: float) -> list[RescuePointMetrics]:
        runs_root = Path(args.workdir) / "runs" / f"f{floor}-c{cue_min}"
        points = (
            run_rescue_point(mat, root=runs_root / mat.point_id, rescue_floor=floor, rescue_cue_min=cue_min)
            for mat in materials
        )
        return [point.metrics for point in points]

    outcome = rescue_grid_descent(metric_fn)

    def fmt(value: float | None) -> str:
        return "None" if value is None else f"{value:.3f}"

    for res in outcome.results:
        if res.aggregate is None:
            print(f"  floor={res.group.rescue_floor:.2f} cue_min={res.group.cue_min:.2f}: NO DATA")
            continue
        agg = res.aggregate
        print(
            f"  floor={res.group.rescue_floor:.2f} cue_min={res.group.cue_min:.2f}: "
            f"recovery={fmt(agg.band_recovery_rate)} rescue={fmt(agg.rescue_rate)} "
            f"noise={fmt(agg.noise_admission_rate)} rebound={fmt(agg.rebound_rate)} "
            f"leak={fmt(agg.dead_leak_rate)} residue={fmt(agg.residue_coverage)} "
            f"rank={fmt(agg.rank_discipline)} points={agg.points}"
        )

    if outcome.recommended is None:
        print("No runnable groups — demotion path empty")
        return 1

    floor, cue_min = outcome.recommended
    status = "DEMOTED" if outcome.demoted else "ACCEPTED"
    print(f"Recommended: rescue_floor={floor}, rescue_cue_min={cue_min} ({status})")
    if outcome.demoted:
        print(f"Missed bars: {', '.join(outcome.demotion_path)}")

    if args.write_config:
        if outcome.demoted:
            print("DEMOTED outcomes are never written to product config")
            return 1
        config_path = Path("src/mnemoseed_local/config.py")
        if write_rescue_defaults(config_path, floor, cue_min):
            print(f"Updated {config_path} with rescue_floor={floor}, rescue_cue_min={cue_min}")
        else:
            print(f"FAILED to update {config_path}: rescue constants not found")
            return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mnemoseed_local.eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    matrix = sub.add_parser("matrix", help="run the eval matrix over the material catalog")
    matrix.add_argument("--models", default=",".join(ROSTER_DEFAULT), help="comma-separated ollama tags")
    matrix.add_argument("--ensemble", default="off,verify", help="comma-separated ensemble modes")
    matrix.add_argument("--verifier", default="gemma4:e4b", help="uniform verifier seat model")
    matrix.add_argument(
        "--extra-route",
        action="append",
        default=[],
        metavar="driver|model|base_url[|key_env[|timeout[|max_tokens]]]",
        help="cloud anchor seat (repeatable); key_env is an ENV-VAR NAME, never a key",
    )
    matrix.add_argument("--base-url", default=DEFAULT_BASE_URL, help="ollama server base url")
    matrix.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX, help="ollama num_ctx per seat")
    matrix.add_argument("--delta-budget", type=int, default=32000, help="explicit delta budget per cell")
    matrix.add_argument(
        "--no-seat-seed",
        action="store_true",
        help="do not pin the fixed sampling seed on ollama seats (report marks seat_seed_policy none)",
    )
    matrix.add_argument("--materials-dir", default=None, help="directory of replay snapshot journals")
    matrix.add_argument("--seed", type=int, default=DEFAULT_CANARY_SEED, help="canary factory seed")
    matrix.add_argument(
        "--canary-count",
        type=int,
        default=DEFAULT_CANARY_COUNT,
        help="number of canary sessions in the material catalog",
    )
    matrix.add_argument("--out", default=None, help="report dir (default: <CONFIG_DIR>/eval)")
    matrix.add_argument("--workdir", default=".eval-rigs", help="scratch root for rig stores")
    matrix.add_argument("--list", action="store_true", help="list expanded cell ids and exit")

    canary = sub.add_parser("canary", help="stub-seat self-check of the harness itself")
    canary.add_argument("--seed", type=int, default=DEFAULT_CANARY_SEED, help="canary factory seed")
    canary.add_argument("--workdir", default=".eval-rigs", help="scratch root for rig stores")

    rescore = sub.add_parser("rescore", help="re-judge a v1.1 report's recall offline (no GPU)")
    rescore.add_argument("report", help="path to the v1.1 report JSON")
    rescore.add_argument("--seed", type=int, default=DEFAULT_CANARY_SEED, help="canary factory seed")
    rescore.add_argument("--out", default=None, help="output dir (default: beside the source report)")

    recall = sub.add_parser("recall", help="T4b live calibration: coordinate descent over T2 pipeline rig")
    recall.add_argument("--workdir", default=".eval-rigs", help="scratch root for rig stores")
    recall.add_argument("--write-config", action="store_true", help="write recommended values to config.py")
    recall.add_argument(
        "--seed",
        type=int,
        default=RECALL_MATERIALS_SEED,
        help="material catalog seed (pinned; overrides are rejected)",
    )

    rescue = sub.add_parser(
        "rescue",
        help="rescue-band calibration (design/09): (rescue_floor, cue_min) grid on the MCP-recall rig",
    )
    rescue.add_argument("--workdir", default=".eval-rigs", help="scratch root for rig stores")
    rescue.add_argument(
        "--write-config",
        action="store_true",
        help="write ACCEPTED values to config.py (never a DEMOTED outcome)",
    )

    args = parser.parse_args(argv)
    if args.command == "recall" and args.seed != RECALL_MATERIALS_SEED:
        # Material identity is part of the calibration bar: the descent's bars
        # hold only for the pinned catalog seed, so another catalog would yield
        # outcomes incomparable with every calibrated constant — a seed override
        # is rejected instead of silently running the default catalog.
        parser.error(
            f"--seed {args.seed}: seed override not supported for calibration "
            f"comparability (bars assume the pinned catalog seed {RECALL_MATERIALS_SEED})"
        )
    if args.command == "matrix":
        return _matrix_command(args)
    if args.command == "rescore":
        return _rescore_command(args)
    if args.command == "recall":
        return _recall_command(args)
    if args.command == "rescue":
        return _rescue_command(args)
    return _canary_command(args)


if __name__ == "__main__":
    sys.exit(main())
