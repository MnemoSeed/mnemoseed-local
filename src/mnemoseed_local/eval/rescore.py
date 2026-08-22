"""Offline rescoring (B3.1 T2): re-judge a v1.1 report's recall after a ruler
revision — WITHOUT re-burning GPU.

A v1.1 report embeds each cell's full merged triple payload, so the canary
recall metrics (matched/missed facts, extra core nodes, core yield) are
recomputable from the report alone: rebuild the canary truth for the
report's seed, re-run the pure matcher over the embedded triples.

Honestly NOT recomputable offline:

- ``noise_pollution`` — the pollution judgment needs chunk-level attribution
  (noise turn -> mini-session -> chunk ids), which is rig-run state, not
  report content. The original value is CARRIED OVER verbatim.
- a reflect-seat-failed cell (collapse attempts > 0, never recovered) — its
  recall is None by signature; recomputing would revive a misleading 0.00, so
  it is carried over verbatim.
- verify/cost blocks — carrides over untouched by definition.

The rescored report writes beside the source with a ``-rescored`` suffix.
The floor sweep (B4b) re-judges the same recall surface per ``core_confidence
_floor`` value — the offline calibration table for the lite tier's merge
threshold. The raw pollution value stays rig-carried; the sweep adds
``pollution_floor``, the floor-projected projection (rig-carried polluting
node ids still present above the floor). Verify/cost stay untouched.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mnemoseed_local.eval.canary import CanaryFact, CanarySession, matches_fact
from mnemoseed_local.eval.materials import material_catalog
from mnemoseed_local.eval.metrics import CanaryMetrics
from mnemoseed_local.eval.report import (
    REPORT_SCHEMA_VERSION,
    CellReport,
    EvalReport,
    ReportedTriple,
    load_report,
    write_report,
)

_MATERIAL_INDEX_RE = re.compile(r"^canary-(?P<index>\d+)$")

#: The calibration ladder for the lite tier's core_confidence_floor decision.
FLOOR_SWEEP_DEFAULT_FLOORS: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 0.9, 0.95)


def _truth_by_name(report: EvalReport, canary_seed: int) -> dict[str, CanarySession]:
    """Rebuild the canary truth for every canary-named cell in the report."""
    indexes = sorted(
        int(m.group("index"))
        for cell in report.cells
        if (m := _MATERIAL_INDEX_RE.match(cell.material)) is not None
    )
    sessions = material_catalog(
        None, canary_seed=canary_seed, canary_count=(max(indexes) + 1 if indexes else 0)
    )
    return {m.name: m.session for m in sessions if m.session is not None}


def _rescore_canary(
    cell: CellReport, truth_by_name: dict[str, CanarySession], *, floor: float = 0.0
) -> CanaryMetrics | None:
    """Recompute the recall-shaped fields; carry pollution + everything else."""
    if cell.canary is None:
        return None
    if cell.reflect_collapse_attempts > 0 and not cell.reflect_recovered:
        # reflect-seat-failed: an offline rejudge would revive a misleading
        # 0.00 — preserve the failure signature verbatim.
        return cell.canary
    session = truth_by_name.get(cell.material)
    if session is None:
        return cell.canary  # replay material or unknown canary cell: carry
    facts = session.facts
    core = _floor_main_triples(cell, floor)

    def _hits(triple: ReportedTriple, fact: CanaryFact) -> bool:
        return matches_fact(
            {"predicate": triple.predicate, "object": triple.object, "polarity": triple.polarity},
            fact,
        )

    matched = sorted(f.fact_id for f in facts if any(_hits(t, f) for t in core))
    missed = sorted(f.fact_id for f in facts if not any(_hits(t, f) for t in core))
    extra = sorted(t.node_id for t in core if not any(_hits(t, f) for f in facts))
    total = len(facts)
    return replace(
        cell.canary,
        facts_total=total,
        facts_matched=len(matched),
        canary_recall=(len(matched) / total) if total else None,
        matched_fact_ids=tuple(matched),
        missed_fact_ids=tuple(missed),
        core_yield=len(core),
        extra_core_nodes=tuple(extra),
        # noise_pollution / polluting_nodes intentionally UNCHANGED (not
        # offline-recomputable: chunk attribution is rig-run state).
    )


def rescore_report(report_path: Path, *, canary_seed: int, out_dir: Path | None = None) -> Path:
    """Recompute the recall metrics of one v1.1 report in place (new file).
    Returns the path of the rescored report (``<stem>-rescored.json``)."""
    report = load_report(report_path)
    truth_by_name = _truth_by_name(report, canary_seed)
    rescored = EvalReport(
        eval_version=report.eval_version,
        started_at=report.started_at,
        cells=tuple(replace(cell, canary=_rescore_canary(cell, truth_by_name)) for cell in report.cells),
        skipped=report.skipped,
        seat_seed_policy=report.seat_seed_policy,
    )
    return write_report(
        rescored,
        out_dir if out_dir is not None else report_path.parent,
        matrix_slug=f"{report_path.stem}-rescored",
    )


# ---------------------------------------------------------------- floor sweep


def _floor_main_triples(cell: CellReport, floor: float) -> tuple[ReportedTriple, ...]:
    """The cell's main-graph triples whose confidence clears ``floor`` — the
    one floor filter shared by the rescore core and the sweep's pollution
    projection."""
    return tuple(t for t in cell.triples if t.graph == "main" and t.confidence >= floor)


@dataclass(frozen=True)
class FloorSweepRow:
    """One (cell x floor) sweep point: the offline-recomputable recall surface
    plus the floor-projected pollution count."""

    cell_id: str
    material: str
    floor: float
    facts_total: int
    facts_matched: int
    canary_recall: float | None
    core_yield: int
    extra_core_count: int
    pollution_floor: int  # polluting node ids surviving this floor


def _row_to_dict(row: FloorSweepRow) -> dict[str, Any]:
    return {
        "cell_id": row.cell_id,
        "material": row.material,
        "floor": row.floor,
        "facts_total": row.facts_total,
        "facts_matched": row.facts_matched,
        "canary_recall": row.canary_recall,
        "core_yield": row.core_yield,
        "extra_core_count": row.extra_core_count,
        "pollution_floor": row.pollution_floor,
    }


def _write_sweep(payload: dict[str, Any], report_path: Path, out_dir: Path | None) -> Path:
    """Never-overwrite write for the sweep payload (numeric suffix, same
    convention as write_report)."""
    target_dir = out_dir if out_dir is not None else report_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{report_path.stem}-floor-sweep.json"
    suffix = 2
    while path.exists():
        path = target_dir / f"{report_path.stem}-floor-sweep-{suffix}.json"
        suffix += 1
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def floor_sweep_report(
    report_path: Path,
    *,
    canary_seed: int,
    floors: Sequence[float] = FLOOR_SWEEP_DEFAULT_FLOORS,
    out_dir: Path | None = None,
) -> Path:
    """Write the per-floor recall table of one v1.1 report — the offline
    ``core_confidence_floor`` calibration sweep (B4b lite-tier bar work).
    Returns the sweep path (``<stem>-floor-sweep.json``)."""
    report = load_report(report_path)
    truth_by_name = _truth_by_name(report, canary_seed)
    rows = [
        FloorSweepRow(
            cell_id=cell.cell_id,
            material=cell.material,
            floor=floor,
            facts_total=metrics.facts_total,
            facts_matched=metrics.facts_matched,
            canary_recall=metrics.canary_recall,
            core_yield=metrics.core_yield,
            extra_core_count=len(metrics.extra_core_nodes),
            pollution_floor=len(
                {t.node_id for t in _floor_main_triples(cell, floor)} & set(metrics.polluting_nodes)
            ),
        )
        for cell in report.cells
        for floor in floors
        if (metrics := _rescore_canary(cell, truth_by_name, floor=floor)) is not None
    ]
    payload = {
        "eval_version": REPORT_SCHEMA_VERSION,
        "source": report_path.name,
        "canary_seed": canary_seed,
        "cells": [_row_to_dict(row) for row in rows],
    }
    return _write_sweep(payload, report_path, out_dir)
