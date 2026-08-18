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
- verify/cost blocks — carrides over untouched by definition.

The rescored report writes beside the source with a ``-rescored`` suffix.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from mnemoseed_local.eval.canary import CanaryFact, CanarySession, matches_fact
from mnemoseed_local.eval.materials import material_catalog
from mnemoseed_local.eval.metrics import CanaryMetrics
from mnemoseed_local.eval.report import CellReport, EvalReport, ReportedTriple, load_report, write_report

_MATERIAL_INDEX_RE = re.compile(r"^canary-(?P<index>\d+)$")


def _rescore_canary(cell: CellReport, truth_by_name: dict[str, CanarySession]) -> CanaryMetrics | None:
    """Recompute the recall-shaped fields; carry pollution + everything else."""
    if cell.canary is None:
        return None
    session = truth_by_name.get(cell.material)
    if session is None:
        return cell.canary  # replay material or unknown canary cell: carry
    facts = session.facts
    core = [t for t in cell.triples if t.graph == "main"]

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
    indexes = sorted(
        int(m.group("index"))
        for cell in report.cells
        if (m := _MATERIAL_INDEX_RE.match(cell.material)) is not None
    )
    sessions = material_catalog(
        None, canary_seed=canary_seed, canary_count=(max(indexes) + 1 if indexes else 0)
    )
    truth_by_name = {m.name: m.session for m in sessions if m.session is not None}
    rescored = EvalReport(
        eval_version=report.eval_version,
        started_at=report.started_at,
        cells=tuple(replace(cell, canary=_rescore_canary(cell, truth_by_name)) for cell in report.cells),
        skipped=report.skipped,
    )
    return write_report(
        rescored,
        out_dir if out_dir is not None else report_path.parent,
        matrix_slug=f"{report_path.stem}-rescored",
    )
