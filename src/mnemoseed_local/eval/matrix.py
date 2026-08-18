"""Matrix runner (B3 T4 / B3.1 T3): roster × ensemble with honest skip semantics.

A matrix cell is skipped (reported, never silent) when its ollama routes are
not actually pulled; a material that fails to load, or a run that blows up,
lands a typed report row too. Exit-code classification is explicit:
``missing_model:*`` reasons are skips (exit 0); every other skipped reason is
a failure (exit 1).

B3.1: cloud anchor seats (any OpenAI-compatible provider — Modal-hosted
Kimi-K3, DeepSeek, ...) join the roster as extra routes: same cell expansion,
same rig, an honest driver-check probe where a failed anchor is a loud
``route_unavailable:`` failure (exit 1). API keys travel by ENV-VAR NAME only
(``api_key_env``), resolved from the process environment at probe/run time —
never materialized into cells, reports, or logs.

The runner is the ONLY place live seats enter the harness: probe + seat
construction ride the same ``EvalRoute`` shape as the stub path, so the code
under measurement is identical on both sides of the fence.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

from mnemoseed_local.eval.harness import CellRun, EvalCell, EvalRig, EvalRoute, RigPaths
from mnemoseed_local.eval.materials import Material, MaterialError, fresh_replay, load_replay
from mnemoseed_local.eval.metrics import cost_metrics, score_canary, verify_metrics
from mnemoseed_local.eval.report import (
    REPORT_SCHEMA_VERSION,
    CellReport,
    EvalReport,
    ReportedTriple,
    SkippedCell,
)

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

#: Drivers that never need an availability probe (deterministic offline seats).
NON_PROBED_DRIVERS: frozenset[str] = frozenset({"stub", "stub_verifier"})

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
    extra_routes: Sequence[EvalRoute] = (),
) -> list[EvalCell]:
    """Roster × ensembles expansion (deterministic, per-model pairing).
    ``extra_routes`` (cloud anchors) join the same off/verify pairing."""
    cells: list[EvalCell] = []
    seats = [ollama_route(model, base_url=base_url, num_ctx=num_ctx) for model in roster]
    seats.extend(extra_routes)
    for reflect in seats:
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


#: Pipe-separated extra-route spec (B3.1): driver|model|base_url[|key_env[|timeout_s[|max_tokens]]]
_EXTRA_ROUTE_MIN_PARTS = 3
_EXTRA_ROUTE_MAX_PARTS = 6


def parse_extra_route(spec: str) -> EvalRoute:
    """Parse one ``--extra-route`` spec into an EvalRoute.

    Shape: ``driver|model|base_url[|key_env[|timeout_s[|max_tokens]]]`` (pipe
    separator — URLs carry colons). key_env is an ENV-VAR NAME, never a key.
    Defaults: no key env, 60s timeout, 8192 max tokens (cloud anchors need the
    headroom — B1 saw ~3k-token reflect completions)."""
    parts = [p.strip() for p in spec.split("|")]
    if not _EXTRA_ROUTE_MIN_PARTS <= len(parts) <= _EXTRA_ROUTE_MAX_PARTS or any(not p for p in parts[:3]):
        raise ValueError(
            f"bad --extra-route spec {spec!r}: expected "
            "driver|model|base_url[|key_env[|timeout[|max_tokens]]]"
        )
    driver, model, base_url = parts[0], parts[1], parts[2]
    params: list[tuple[str, object]] = [("base_url", base_url)]
    if driver == "ollama":
        params.append(("think", False))
        params.append(("num_ctx", DEFAULT_NUM_CTX))
    if len(parts) > 3 and parts[3]:
        params.append(("api_key_env", parts[3]))
    params.append(("timeout", float(parts[4]) if len(parts) > 4 else 60.0))
    if driver != "ollama":
        # the cloud driver reads max_tokens directly; ollama's generation cap
        # is num_predict — local roster runs leave it unset, so an extra ollama
        # route only gets a cap when the spec names one.
        params.append(("max_tokens", int(parts[5]) if len(parts) > 5 else 8192))
    elif len(parts) > 5:
        params.append(("num_predict", int(parts[5])))
    return EvalRoute(driver=driver, model=model, params=tuple(params))


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
        if route is None:
            continue
        reason = probe.get(route.model)
        if reason is not None:
            if route.driver == "ollama" and "unreachable" not in (reason or ""):
                return f"missing_model: {route.model}"
            return f"route_unavailable: {route.model} ({reason})"
    return None


RouteChecker = Callable[[EvalRoute, str | None], str | None]


def _default_route_checker(route: EvalRoute, api_key: str | None) -> str | None:
    """Live availability probe for a non-ollama route: build the driver and
    run its typed health check. Returns None when healthy, the failure reason
    otherwise (typed like the ollama probe, never a raise)."""
    from mnemoseed_local.llm import LLM_DRIVERS  # local import: registered drivers

    params = {k: v for k, v in dict(route.params).items() if k != "api_key_env"}
    params["model"] = route.model
    params["api_key"] = api_key or ""
    from mnemoseed_local.llm.types import LLMError

    try:
        driver = LLM_DRIVERS.build(route.driver, params)
    except LLMError as exc:
        return str(exc)
    try:
        report = driver.check()
    except Exception as exc:  # noqa: BLE001 - a broken check() degrades like a downed route
        return f"check crashed: {exc}"
    if report.ok:
        return None
    return str(report.detail.get("error") or report.detail)


def probe_routes(
    routes: Sequence[EvalRoute],
    *,
    fetch_tags: FetchTags | None = None,
    checker: RouteChecker | None = None,
    env: Callable[[str], str | None] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 2.0,
) -> dict[str, str | None]:
    """model -> None (usable) | skip reason, over BOTH ollama (tags probe) and
    other-driver (driver-check probe) routes. Key-material access: the
    ``api_key_env`` NAME in the route params is resolved through ``env``;
    a missing variable is itself the loud reason."""
    env = env if env is not None else os.environ.get
    checker = checker if checker is not None else _default_route_checker
    result: dict[str, str | None] = {}
    ollama_models = sorted({r.model for r in routes if r.driver == "ollama"})
    if ollama_models:
        result.update(
            probe_ollama_models(ollama_models, fetch_tags=fetch_tags, base_url=base_url, timeout=timeout)
        )
    seen: set[str] = set()
    for route in routes:
        if route.driver == "ollama" or route.model in seen:
            continue
        if route.driver in NON_PROBED_DRIVERS:
            # deterministic offline seats: always available, never probed
            continue
        seen.add(route.model)
        key_env = dict(route.params).get("api_key_env")
        api_key = env(str(key_env)) if key_env else None
        if key_env and not api_key:
            result[route.model] = f"api_key_env {key_env} is not set in the environment"
        else:
            result[route.model] = checker(route, api_key)
    return result


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
    route_checker: RouteChecker | None = None,
    env: Callable[[str], str | None] | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> EvalReport:
    """Run every available cell over every material; collect a v1.1 report.

    Skip semantics: every seat route (both roles) is probed once up front; a
    cell with any unusable route is a skipped row — ``missing_model:`` for an
    absent ollama tag (exit-neutral), ``route_unavailable:`` for a dead
    server/cloud anchor or an unset key env (a loud failure row). Material
    load failures (``material_error:``) and run failures (``run_error:``) are
    failure rows. Skipped cells never build a rig.
    """
    unique_routes = {
        (route.driver, route.model): route
        for cell in cells
        for route in (cell.reflect, cell.verifier)
        if route is not None
    }
    probe = (
        probe_routes(
            tuple(unique_routes.values()),
            fetch_tags=fetch_tags,
            checker=route_checker,
            env=env,
            base_url=base_url,
        )
        if unique_routes
        else {}
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
                        # v1.1: full merged payload for GPU-free rescoring later
                        triples=tuple(
                            ReportedTriple(
                                graph=node.graph,
                                node_id=node.node_id,
                                subject=node.subject,
                                predicate=node.predicate,
                                object=node.object,
                                polarity=node.polarity,
                                confidence=node.confidence,
                            )
                            for node in (*run.core_nodes, *run.isolated_nodes)
                        ),
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
