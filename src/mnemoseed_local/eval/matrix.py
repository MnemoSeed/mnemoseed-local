"""Matrix runner (B3 T4): roster × ensemble with honest skip semantics.

A matrix cell is skipped (reported, never silent) when its ollama routes are
not actually pulled; a material that fails to load, or a run that blows up,
lands a typed report row too. Exit-code classification is explicit:
``missing_model:*`` reasons are skips (exit 0); every other skipped reason is
a failure (exit 1).

The runner is the ONLY place live ollama seats enter the harness: probe +
seat construction ride the same ``EvalRoute`` shape as the stub path, so the
code under measurement is identical on both sides of the fence.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

from mnemoseed_local.eval.harness import CellRun, EvalCell, EvalRig, EvalRoute, RigPaths
from mnemoseed_local.eval.materials import Material, MaterialError, fresh_replay, load_replay
from mnemoseed_local.eval.metrics import cost_metrics, score_canary, verify_metrics
from mnemoseed_local.eval.report import REPORT_SCHEMA_VERSION, CellReport, EvalReport, SkippedCell

#: The on-box model roster (roadmap B3 item 3): the 9B anchor, its small
#: siblings, and the 12B heavyweight. Availability is probed per run — an
#: absent tag marks its cells skipped, never crashes the matrix.
ROSTER_DEFAULT: tuple[str, ...] = (
    "qwen3.5:9b",
    "gemma4:e4b",
    "qwen3.5:4b",
    "qwen3:8b",
    "qwen3:4b",
    "gemma4:12b",
)

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_NUM_CTX = 16384

FetchTags = Callable[[str, float], tuple[str, ...]]


def ollama_route(
    model: str, *, base_url: str = DEFAULT_BASE_URL, num_ctx: int = DEFAULT_NUM_CTX
) -> EvalRoute:
    """An ollama seat route in the same shape as the config defaults."""
    return EvalRoute(
        driver="ollama",
        model=model,
        params=(("base_url", base_url), ("think", False), ("num_ctx", num_ctx)),
    )


def default_matrix(
    *,
    roster: Sequence[str] = ROSTER_DEFAULT,
    ensembles: Sequence[str] = ("off", "verify"),
    verifier_model: str = "gemma4:e4b",
    base_url: str = DEFAULT_BASE_URL,
    num_ctx: int = DEFAULT_NUM_CTX,
    delta_budget_tokens: int = 32000,
) -> list[EvalCell]:
    """Roster × ensembles expansion (deterministic, per-model pairing)."""
    cells: list[EvalCell] = []
    for model in roster:
        reflect = ollama_route(model, base_url=base_url, num_ctx=num_ctx)
        for ensemble in ensembles:
            verifier = (
                ollama_route(verifier_model, base_url=base_url, num_ctx=num_ctx)
                if ensemble != "off"
                else None
            )
            cells.append(
                EvalCell(
                    reflect=reflect,
                    ensemble=ensemble,
                    verifier=verifier,
                    delta_budget_tokens=delta_budget_tokens,
                )
            )
    return cells


def list_cells(cells: Sequence[EvalCell]) -> list[str]:
    """The --list surface: ids only, zero probing, zero store construction."""
    return [cell.cell_id for cell in cells]


def _fetch_ollama_tags(base_url: str, timeout: float) -> tuple[str, ...]:
    response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return tuple(str(m["name"]) for m in payload.get("models", []) if isinstance(m, dict) and m.get("name"))


def probe_ollama_models(
    models: Sequence[str],
    *,
    fetch_tags: FetchTags | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 2.0,
) -> dict[str, str | None]:
    """model -> None (pulled) | skip reason. A probe failure marks EVERY model
    honestly (one unreachable server fails the whole ollama set at once)."""
    fetch = fetch_tags or _fetch_ollama_tags
    try:
        tags = set(fetch(base_url, timeout))
    except Exception as exc:  # noqa: BLE001 - probe degradation is typed, never a crash
        return {model: f"ollama unreachable: {exc}" for model in models}
    return {model: (None if model in tags else f"model not pulled: {model}") for model in models}


def _cell_missing_reason(cell: EvalCell, probe: dict[str, str | None]) -> str | None:
    for route in (cell.reflect, cell.verifier):
        if route is None or route.driver != "ollama":
            continue
        reason = probe.get(route.model)
        if reason is not None:
            return f"missing_model: {route.model}"
    return None


def _run_material(rig: EvalRig, material: Material) -> CellRun:
    if material.kind == "canary":
        assert material.session is not None
        return rig.run_canary(material.session)
    snapshot = material.snapshot
    if snapshot is None:
        if material.path is None:  # pragma: no cover - catalog always sets one side
            raise MaterialError(f"replay material {material.name!r} carries neither snapshot nor path")
        snapshot = load_replay(material.path).snapshot
        assert snapshot is not None
    return rig.run_snapshot(fresh_replay(snapshot))


def run_matrix(
    cells: Sequence[EvalCell],
    materials: Sequence[Material],
    *,
    root: Path,
    fetch_tags: FetchTags | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> EvalReport:
    """Run every available cell over every material; collect a v1 report.

    Skip semantics: ollama routes are probed once up front; a cell with any
    missing model is a ``missing_model:`` skipped row (exit-neutral). Material
    load failures (``material_error:``) and run failures (``run_error:``) are
    failure rows. Skipped cells never build a rig.
    """
    ollama_models = sorted(
        {
            route.model
            for cell in cells
            for route in (cell.reflect, cell.verifier)
            if route is not None and route.driver == "ollama"
        }
    )
    probe = (
        probe_ollama_models(ollama_models, fetch_tags=fetch_tags, base_url=base_url) if ollama_models else {}
    )

    cell_reports: list[CellReport] = []
    skipped: list[SkippedCell] = []
    for cell in cells:
        missing = _cell_missing_reason(cell, probe)
        if missing is not None:
            skipped.append(SkippedCell(cell_id=cell.cell_id, reason=missing))
            continue
        rig = EvalRig(RigPaths(root=root / "cells" / cell.cell_id), cell)
        try:
            for material in materials:
                try:
                    run = _run_material(rig, material)
                except MaterialError as exc:
                    skipped.append(SkippedCell(cell_id=cell.cell_id, reason=f"material_error: {exc}"))
                    continue
                except Exception as exc:  # noqa: BLE001 - typed report row, never a traceback out
                    skipped.append(SkippedCell(cell_id=cell.cell_id, reason=f"run_error: {exc}"))
                    continue
                cell_reports.append(
                    CellReport(
                        cell_id=cell.cell_id,
                        material=material.name,
                        canary=score_canary(material.session, run) if material.session is not None else None,
                        verify=verify_metrics(run),
                        cost=cost_metrics(run),
                    )
                )
        finally:
            rig.close()
    return EvalReport(
        eval_version=REPORT_SCHEMA_VERSION,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        cells=tuple(cell_reports),
        skipped=tuple(skipped),
    )


def matrix_exit_code(report: EvalReport) -> int:
    """0 when the only skipped rows are missing models; 1 on any failure row."""
    return 1 if any(not s.reason.startswith("missing_model:") for s in report.skipped) else 0


def summary_lines(report: EvalReport) -> list[str]:
    """The per-cell surface + skip list, ready for PRD closeout excerpts."""
    lines: list[str] = []
    for cell in report.cells:
        canary = cell.canary
        recall = "-" if canary is None or canary.canary_recall is None else f"{canary.canary_recall:.2f}"
        pollution = "-" if canary is None else str(canary.noise_pollution)
        judged = cell.verify.judged if cell.verify.judged else "-"
        lines.append(
            f"{cell.cell_id} | {cell.material} | recall={recall} pollution={pollution} "
            f"core={canary.core_yield if canary is not None else '-'} judged={judged} "
            f"fallbacks={cell.verify.fallbacks} tokens={cell.cost.token_usage} t={cell.cost.duration_s:.1f}s"
        )
    for skip in report.skipped:
        lines.append(f"{skip.cell_id} | SKIPPED | {skip.reason}")
    return lines
