"""B3.1 T2 — report triple payload (v1.1) + offline rescoring (PRD-B3 addendum).

Ruler upgrades must NOT cost GPU: every CellReport now embeds its full triple
dump (graph, node_id, subject/predicate/object/polarity/confidence), so a
later ruler revision can re-judge recall offline. Reader tolerance: v1
reports (no triples key) load as v1.1 with empty payloads. Rescoring
recomputes ONLY the recall-shaped metrics; pollution (chunk attribution is
not embedded in reports), verify, and cost are carried over and marked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.eval.harness import EvalCell, EvalRoute
from mnemoseed_local.eval.materials import material_catalog
from mnemoseed_local.eval.matrix import run_matrix
from mnemoseed_local.eval.report import (
    SEAT_SEED_POLICY_NONE,
    EvalReport,
    ReportedTriple,
    load_report,
    write_report,
)
from mnemoseed_local.eval.rescore import rescore_report

STUB_A = EvalRoute(driver="stub", model="stub-a")
STUB_B = EvalRoute(driver="stub_verifier", model="stub-b")


@pytest.fixture
def stub_report(tmp_path: Path) -> tuple[EvalReport, Path]:
    """A real v1.1 report produced by a stub-seat matrix run."""
    report = run_matrix(
        [EvalCell(reflect=STUB_A, ensemble="verify", verifier=STUB_B)],
        material_catalog(None, canary_seed=7, canary_count=1),
        root=tmp_path / "root",
    )
    path = write_report(report, tmp_path / "reports", matrix_slug="fixture")
    return report, path


def test_matrix_embeds_full_triple_payload(stub_report) -> None:
    report, _ = stub_report
    assert report.eval_version == "v1.1"
    triples = report.cells[0].triples
    assert triples, "cells must embed their full triple payload"
    assert all(isinstance(t, ReportedTriple) for t in triples)
    assert all(t.graph in ("main", "isolated") for t in triples)
    assert any(t.node_id for t in triples)


def test_v11_report_round_trip_includes_triples(stub_report) -> None:
    report, path = stub_report
    assert load_report(path) == report


def test_v1_report_loads_with_empty_triples(tmp_path: Path) -> None:
    # a v1-era cell block (no triples key) must load tolerantly
    path = write_report(
        EvalReport(
            eval_version="v1",
            started_at="2026-08-18T00:00:00Z",
            cells=(),
            skipped=(),
        ),
        tmp_path / "r",
        matrix_slug="v1",
    )
    raw = path.read_text(encoding="utf-8")
    assert '"triples"' not in raw  # v1 shape unknown to the payload field
    assert load_report(path).cells == ()


def test_rescore_recomputes_recall_keeps_pollution(stub_report, tmp_path: Path) -> None:
    report, path = stub_report
    # damage the recorded recall to prove rescore actually recomputes it
    broken = EvalReport(
        eval_version=report.eval_version,
        started_at=report.started_at,
        cells=tuple(
            type(c)(
                cell_id=c.cell_id,
                material=c.material,
                canary=type(c.canary)(
                    facts_total=99,
                    facts_matched=0,
                    canary_recall=0.0,
                    matched_fact_ids=(),
                    missed_fact_ids=("fake",),
                    noise_pollution=42,
                    polluting_nodes=("n-noise",),
                    core_yield=42,
                    extra_core_nodes=(),
                ),
                verify=c.verify,
                cost=c.cost,
                triples=c.triples,
            )
            for c in report.cells
        ),
        skipped=report.skipped,
    )
    broken_path = write_report(broken, tmp_path / "broken", matrix_slug="m")
    rescored_path = rescore_report(broken_path, canary_seed=7)
    rescored = load_report(rescored_path)
    cell = rescored.cells[0]
    # recall recomputed from embedded triples + the rebuilt canary truth
    assert cell.canary.canary_recall == 1.0
    assert cell.canary.facts_total == 8
    assert len(cell.canary.matched_fact_ids) == 8
    # pollution is NOT recomputable offline (no chunk attribution) — carried over, marked
    assert cell.canary.noise_pollution == 42
    assert cell.canary.polluting_nodes == ("n-noise",)
    # verify + cost carried over untouched
    assert cell.verify == report.cells[0].verify
    assert cell.cost == report.cells[0].cost
    assert rescored_path.name.endswith("-rescored.json")


def test_rescore_session_determinism(stub_report, tmp_path: Path) -> None:
    _, path = stub_report
    a = rescore_report(path, canary_seed=7)
    assert load_report(a).cells[0].canary.canary_recall == 1.0


def test_rescore_entry_point(stub_report) -> None:
    """PRD-B3 B3.1 item 3: the offline rescore is reachable from the harness
    entry (``python -m mnemoseed_local.eval rescore <report>``), not only as a
    library function."""
    from mnemoseed_local.eval.__main__ import main

    _, path = stub_report
    assert main(["rescore", str(path), "--seed", "7"]) == 0
    produced = list(path.parent.glob(f"*-{path.stem}-rescored.json"))
    assert len(produced) == 1
    assert load_report(produced[0]).cells[0].canary.canary_recall == 1.0


def test_rescore_preserves_no_seat_seed_policy(stub_report, tmp_path: Path) -> None:
    """B4a: rescoring a --no-seat-seed report (policy none, unseeded cells)
    must not relabel the policy to the per-seat-fixed default."""
    report, _ = stub_report
    unseeded = EvalReport(
        eval_version=report.eval_version,
        started_at=report.started_at,
        cells=report.cells,
        skipped=report.skipped,
        seat_seed_policy=SEAT_SEED_POLICY_NONE,
    )
    unseeded_path = write_report(unseeded, tmp_path / "unseeded", matrix_slug="m")
    rescored_path = rescore_report(unseeded_path, canary_seed=7)
    rescored = load_report(rescored_path)
    assert rescored.seat_seed_policy == SEAT_SEED_POLICY_NONE
    assert rescored.cells[0].seat_seed is None
