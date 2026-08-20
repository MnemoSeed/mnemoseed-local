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
import sys
from pathlib import Path

from mnemoseed_local.eval.canary import canary_session
from mnemoseed_local.eval.harness import EvalCell, EvalRoute
from mnemoseed_local.eval.materials import DEFAULT_CANARY_SEED, material_catalog
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
from mnemoseed_local.eval.report import default_out_dir, write_report


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

    args = parser.parse_args(argv)
    if args.command == "matrix":
        return _matrix_command(args)
    if args.command == "rescore":
        return _rescore_command(args)
    return _canary_command(args)


if __name__ == "__main__":
    sys.exit(main())
