"""Eval report persistence (B3 T3): one JSON file per matrix run, append-only.

Reports accumulate under ``CONFIG_DIR / "eval"`` by default (the data dir —
never the repo, they would bloat git); ``--out`` overrides. The schema is
``eval_version = "v1"``: fields are stable, serialization is key-sorted and
UTF-8 so reports diff cleanly across runs, and ``load_report(write_report(x))``
is an exact round-trip.

Filenames carry the compact UTC start timestamp + matrix slug and NEVER
overwrite: a same-second rerun gets a numeric suffix.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mnemoseed_local.config import CONFIG_DIR
from mnemoseed_local.eval.metrics import CanaryMetrics, CostMetrics, VerifyMetrics

REPORT_SCHEMA_VERSION = "v1.1"


@dataclass(frozen=True)
class SkippedCell:
    """A matrix cell that did not run, with the honest reason why."""

    cell_id: str
    reason: str


@dataclass(frozen=True)
class ReportedTriple:
    """One merged node's eval surface (v1.1 payload — full triple dump so a
    later RULER revision can re-judge recall offline, no GPU rerun)."""

    graph: str  # "main" | "isolated"
    node_id: str
    subject: str
    predicate: str
    object: str
    polarity: str
    confidence: float


@dataclass(frozen=True)
class CellReport:
    """One (cell × material) scored block. ``canary`` is None for replay
    materials (no embedded ground truth — recall/pollution are canary-only).
    ``triples`` is the full merged graph payload (v1.1; empty for replay runs
    that produced nothing, unknown on v1 reports)."""

    cell_id: str
    material: str
    canary: CanaryMetrics | None
    verify: VerifyMetrics
    cost: CostMetrics
    triples: tuple[ReportedTriple, ...] = ()


@dataclass(frozen=True)
class EvalReport:
    """The top-level v1 report: matrix metadata + per-cell blocks + skips."""

    eval_version: str
    started_at: str  # ISO-8601 UTC ("...Z")
    cells: tuple[CellReport, ...]
    skipped: tuple[SkippedCell, ...] = ()


def default_out_dir() -> Path:
    """The accumulation dir: ``<CONFIG_DIR>/eval`` (data dir, never the repo)."""
    return CONFIG_DIR / "eval"


# ---------------------------------------------------------------- serialization


def _canary_to_dict(m: CanaryMetrics) -> dict[str, Any]:
    return {
        "facts_total": m.facts_total,
        "facts_matched": m.facts_matched,
        "canary_recall": m.canary_recall,
        "matched_fact_ids": list(m.matched_fact_ids),
        "missed_fact_ids": list(m.missed_fact_ids),
        "noise_pollution": m.noise_pollution,
        "polluting_nodes": list(m.polluting_nodes),
        "core_yield": m.core_yield,
        "extra_core_nodes": list(m.extra_core_nodes),
    }


def _canary_from_dict(data: dict[str, Any]) -> CanaryMetrics:
    return CanaryMetrics(
        facts_total=int(data["facts_total"]),
        facts_matched=int(data["facts_matched"]),
        canary_recall=None if data["canary_recall"] is None else float(data["canary_recall"]),
        matched_fact_ids=tuple(data["matched_fact_ids"]),
        missed_fact_ids=tuple(data["missed_fact_ids"]),
        noise_pollution=int(data["noise_pollution"]),
        polluting_nodes=tuple(data["polluting_nodes"]),
        core_yield=int(data["core_yield"]),
        extra_core_nodes=tuple(data["extra_core_nodes"]),
    )


def _verify_to_dict(m: VerifyMetrics) -> dict[str, Any]:
    return {
        "verifier_model": m.verifier_model,
        "judged": m.judged,
        "accepted": m.accepted,
        "rejected": m.rejected,
        "rejected_keys": list(m.rejected_keys),
        "fallbacks": dict(m.fallbacks),
    }


def _verify_from_dict(data: dict[str, Any]) -> VerifyMetrics:
    return VerifyMetrics(
        verifier_model=None if data["verifier_model"] is None else str(data["verifier_model"]),
        judged=int(data["judged"]),
        accepted=int(data["accepted"]),
        rejected=int(data["rejected"]),
        rejected_keys=tuple(data["rejected_keys"]),
        fallbacks={str(k): int(v) for k, v in dict(data["fallbacks"]).items()},
    )


def _cost_to_dict(m: CostMetrics) -> dict[str, Any]:
    return {
        "duration_s": m.duration_s,
        "token_usage": m.token_usage,
        "reflect_prompt_tokens": m.reflect_prompt_tokens,
        "reflect_completion_tokens": m.reflect_completion_tokens,
        "verify_tokens": m.verify_tokens,
    }


def _cost_from_dict(data: dict[str, Any]) -> CostMetrics:
    def _opt(value: Any) -> int | None:
        return None if value is None else int(value)

    return CostMetrics(
        duration_s=float(data["duration_s"]),
        token_usage=int(data["token_usage"]),
        reflect_prompt_tokens=_opt(data["reflect_prompt_tokens"]),
        reflect_completion_tokens=_opt(data["reflect_completion_tokens"]),
        verify_tokens=_opt(data["verify_tokens"]),
    )


def report_to_dict(report: EvalReport) -> dict[str, Any]:
    return {
        "eval_version": report.eval_version,
        "started_at": report.started_at,
        "cells": [
            {
                "cell_id": cell.cell_id,
                "material": cell.material,
                "canary": _canary_to_dict(cell.canary) if cell.canary is not None else None,
                "verify": _verify_to_dict(cell.verify),
                "cost": _cost_to_dict(cell.cost),
                "triples": [
                    {
                        "graph": t.graph,
                        "node_id": t.node_id,
                        "subject": t.subject,
                        "predicate": t.predicate,
                        "object": t.object,
                        "polarity": t.polarity,
                        "confidence": t.confidence,
                    }
                    for t in cell.triples
                ],
            }
            for cell in report.cells
        ],
        "skipped": [{"cell_id": s.cell_id, "reason": s.reason} for s in report.skipped],
    }


def report_from_dict(data: dict[str, Any]) -> EvalReport:
    return EvalReport(
        eval_version=str(data["eval_version"]),
        started_at=str(data["started_at"]),
        cells=tuple(
            CellReport(
                cell_id=str(cell["cell_id"]),
                material=str(cell["material"]),
                canary=None if cell["canary"] is None else _canary_from_dict(cell["canary"]),
                verify=_verify_from_dict(cell["verify"]),
                cost=_cost_from_dict(cell["cost"]),
                # v1 reports carry no payload field: tolerate (empty triples).
                triples=tuple(
                    ReportedTriple(
                        graph=str(t["graph"]),
                        node_id=str(t["node_id"]),
                        subject=str(t["subject"]),
                        predicate=str(t["predicate"]),
                        object=str(t["object"]),
                        polarity=str(t["polarity"]),
                        confidence=float(t["confidence"]),
                    )
                    for t in cell.get("triples", [])
                ),
            )
            for cell in data["cells"]
        ),
        skipped=tuple(
            SkippedCell(cell_id=str(item["cell_id"]), reason=str(item["reason"]))
            for item in data.get("skipped", [])
        ),
    )


# ---------------------------------------------------------------- io


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _filename_stem(report: EvalReport, matrix_slug: str) -> str:
    utc_compact = _SLUG_RE.sub("", report.started_at.replace(":", "-"))
    slug = _SLUG_RE.sub("_", matrix_slug).strip("_") or "matrix"
    return f"{utc_compact}-{slug}"


def write_report(report: EvalReport, out_dir: Path, matrix_slug: str) -> Path:
    """Write one report; never overwrites (numeric suffix on collision)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _filename_stem(report, matrix_slug)
    path = out_dir / f"{stem}.json"
    suffix = 2
    while path.exists():
        path = out_dir / f"{stem}-{suffix}.json"
        suffix += 1
    path.write_text(
        json.dumps(report_to_dict(report), indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_report(path: Path) -> EvalReport:
    """Load one report back; the write/load pair is an exact round-trip."""
    return report_from_dict(json.loads(path.read_text(encoding="utf-8")))
