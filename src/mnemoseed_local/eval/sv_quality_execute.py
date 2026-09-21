"""Luna-backed single-vs-dual executor (eval-only, injected seams only).

No real provider call happens at import or in tests: every seat is built
through the injected factory and every matrix pass through the injected
runner. The module adds no public CLI, router, or README surface.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from mnemoseed_local.dream.adjudicate import (
    PAIR_ADJUDICATION_PROMPT_VERSION,
    PAIR_ADJUDICATION_SYSTEM_PROMPT,
    EndpointObservation,
    PairAdjudicationInput,
    PairNomination,
    SeatOutcome,
    adjudicate,
    parse_pair_seat_output,
    render_pair_adjudication_prompt,
)
from mnemoseed_local.eval.harness import EvalCell, EvalRoute
from mnemoseed_local.eval.materials import Material
from mnemoseed_local.eval.report import EvalReport
from mnemoseed_local.eval.sv_quality_fixtures import (
    SV_QUALITY_CANARY_SEED,
    SV_QUALITY_PROMPT_VERSION,
    ConflictFixture,
    adjudication_carrier_for,
    all_fixtures,
    negative_corpus_hash,
    positive_corpus_hash,
    sv_canary_corpus_hash,
    sv_canary_material_ids,
    sv_canary_sessions,
)
from mnemoseed_local.eval.sv_quality_manifest import (
    MANIFEST_VERSION,
    preregistration_hash,
    write_manifest,
)
from mnemoseed_local.eval.sv_quality_metrics import (
    DENOMINATOR_POLICY,
    ORACLE_VERSION,
    adjudication_score,
    decide,
    extraction_micro_f1,
)
from mnemoseed_local.llm.types import LLMUnavailable
from mnemoseed_local.storage.ports import Disposition

LIVE_PORT = 7788
DEFAULT_PORT = 17891
SMOKE_MAX_TOKENS = 16
ORG_EVIDENCE_MIRROR = "../org/evidence/2026-09-21-sv-single-vs-dual/"

_QUOTA_ERRORS = (LLMUnavailable, httpx.HTTPError, TimeoutError, ConnectionError, OSError)


class QuotaStop(RuntimeError):
    """Quota/unavailable rejection: stop the day, no retry or fallback."""


@dataclass(frozen=True)
class LunaSeat:
    """One Luna cloud seat; secrets travel by ENV NAME only, never values."""

    model: str
    base_url: str
    api_key_env: str
    timeout: float = 60.0
    max_tokens: int = 8192


@dataclass(frozen=True)
class ExecutorConfig:
    """Sealed executor configuration validated before any seam contact."""

    run_root: Path
    canonical_run_root: str
    repository_sha: str
    port: int
    reflect_a: LunaSeat
    reflect_b: LunaSeat
    vote_b: LunaSeat
    verifier: LunaSeat
    allow_fallback: bool = False


@dataclass(frozen=True)
class AdjudicationRow:
    """Terminal outcome for one fixture under one arm."""

    fixture_id: str
    is_positive: bool
    disposition: Disposition


def _reject_muse(value: str) -> None:
    lowered = value.casefold()
    if "muse" in lowered or "claude" in lowered:
        raise ValueError(f"execution model {value!r} must never enter eval output")


def _check_luna_seat(seat: LunaSeat) -> None:
    if not seat.model.strip() or not seat.base_url.strip() or not seat.api_key_env.strip():
        raise ValueError("Luna seat requires model, base_url, and api_key_env")
    if not seat.api_key_env.replace("_", "").isalnum() or not seat.api_key_env[0].isalpha():
        raise ValueError("api_key_env must be an ENV NAME, never a secret value")
    if len(seat.api_key_env) > 64 or " " in seat.api_key_env or ":" in seat.api_key_env:
        raise ValueError("api_key_env must be an ENV NAME, never a secret value")
    _reject_muse(seat.model)
    if seat.timeout <= 0 or seat.max_tokens <= 0:
        raise ValueError("Luna seat timeout and max_tokens must be positive")


def _canonical_run_root(run_root: Path) -> str:
    text = str(run_root).replace("\\", "/")
    if ".." in Path(text).parts or ".." in text.casefold().split("/"):
        raise ValueError("run root traversal is rejected")
    resolved = Path(os.path.abspath(os.path.expanduser(str(run_root))))
    try:
        canonical = str(resolved.resolve())
    except OSError as exc:
        raise ValueError(f"run root cannot be canonicalized: {exc}") from exc
    live_home = os.environ.get("MNEMOSEED_HOME", "")
    if live_home:
        live_resolved = str(Path(os.path.abspath(os.path.expanduser(live_home))).resolve())
        if canonical == live_resolved or canonical.startswith(live_resolved.rstrip("/\\") + os.sep):
            raise ValueError("run root must stay isolated from the live home")
    if ".mnemoseed-local" in canonical.replace("\\", "/").casefold():
        raise ValueError("configured run root must stay isolated from the installed runtime")
    return canonical


def build_executor_config(
    *,
    run_root: Path,
    repository_sha: str,
    port: int = DEFAULT_PORT,
    reflect_a: LunaSeat,
    reflect_b: LunaSeat,
    vote_b: LunaSeat,
    verifier: LunaSeat,
    allow_fallback: bool = False,
) -> ExecutorConfig:
    """Validate Luna-only routes, distinct vote-B, and isolation guards."""
    if allow_fallback:
        raise ValueError("fallback mixing is rejected for the quality comparison")
    if port == LIVE_PORT:
        raise ValueError("live port 7788 is never used for eval runs")
    if not repository_sha.strip():
        raise ValueError("repository SHA is required")
    for seat in (reflect_a, reflect_b, vote_b, verifier):
        _check_luna_seat(seat)
    routes = [(seat.model, seat.base_url) for seat in (reflect_a, reflect_b, verifier)]
    if (vote_b.model, vote_b.base_url) in routes:
        raise ValueError("vote-B must differ from reflect and verifier routes")
    if vote_b.model in (reflect_a.model, reflect_b.model, verifier.model):
        raise ValueError("vote-B must differ from reflect and verifier routes")
    canonical = _canonical_run_root(run_root)
    return ExecutorConfig(
        run_root=run_root,
        canonical_run_root=canonical,
        repository_sha=repository_sha,
        port=port,
        reflect_a=reflect_a,
        reflect_b=reflect_b,
        vote_b=vote_b,
        verifier=verifier,
        allow_fallback=allow_fallback,
    )


def luna_route(seat: LunaSeat) -> EvalRoute:
    """Cloud route without a seed; key travels by ENV NAME only."""
    _check_luna_seat(seat)
    return EvalRoute(
        driver="openai_compatible",
        model=seat.model,
        params=(
            ("base_url", seat.base_url),
            ("api_key_env", seat.api_key_env),
            ("timeout", float(seat.timeout)),
            ("max_tokens", int(seat.max_tokens)),
        ),
    )


def build_cells(config: ExecutorConfig) -> tuple[EvalCell, EvalCell]:
    """Cell A off then cell B vote, both Luna-only with distinct vote-B."""
    route_a = luna_route(config.reflect_a)
    route_b = luna_route(config.reflect_b)
    vote = luna_route(config.vote_b)
    verifier = luna_route(config.verifier)
    for route in (route_a, route_b, vote, verifier):
        assert route.driver == "openai_compatible"
        assert "seed" not in dict(route.params)
    cell_a = EvalCell(reflect=route_a, ensemble="off")
    cell_b = EvalCell(reflect=route_b, ensemble="vote", verifier=verifier, vote_b=vote)
    return (cell_a, cell_b)


def build_materials() -> tuple[Material, ...]:
    """Exactly the 24 fixed canary sessions in deterministic order."""
    sessions = sv_canary_sessions()
    assert len(sessions) == 24
    assert [item.session_id for item in sessions] == list(sv_canary_material_ids())
    return tuple(Material(kind="canary", name=item.session_id, session=item) for item in sessions)


def prereg_for(
    *,
    prompt_version: str,
    canary_seed: int,
    reflect_a: tuple[str, str],
    reflect_b: tuple[str, str],
    vote_b: tuple[str, str],
    verifier: tuple[str, str],
    material_ids: tuple[str, ...] | list[str],
    oracle_version: str,
    denominator: str,
) -> str:
    """Pre-registration hash over all four seats and frozen settings."""
    return preregistration_hash(
        prompt_version=prompt_version,
        canary_seed=canary_seed,
        routes=[
            f"{reflect_a[0]}|{reflect_a[1]}",
            f"{reflect_b[0]}|{reflect_b[1]}",
            f"{vote_b[0]}|{vote_b[1]}",
            f"{verifier[0]}|{verifier[1]}",
        ],
        material_ids=tuple(material_ids),
        oracle_version=oracle_version,
        denominator=denominator,
    )


def group_paired_canary(
    report: EvalReport,
    cell_a_id: str,
    cell_b_id: str,
    material_ids: Sequence[str],
) -> tuple[list[Any], list[Any]]:
    """Group real CellReports by arm cell ID and session, strict A-then-B."""
    if report.skipped:
        raise ValueError(f"matrix skipped {len(report.skipped)} cells; refusing paired scoring")
    if len(material_ids) != 24:
        raise ValueError("paired scoring requires exactly the 24 frozen materials")
    by_arm: dict[str, dict[str, Any]] = {cell_a_id: {}, cell_b_id: {}}
    order: list[str] = []
    for cell in report.cells:
        if cell.cell_id not in by_arm:
            raise ValueError(f"unexpected cell {cell.cell_id!r} outside the A/B pair")
        if cell.material in by_arm[cell.cell_id]:
            raise ValueError(f"duplicate cell {cell.cell_id!r} material {cell.material!r}")
        by_arm[cell.cell_id][cell.material] = cell.canary
        order.append(cell.cell_id)
    expected_order = [cell_a_id] * 24 + [cell_b_id] * 24
    if order != expected_order:
        raise ValueError("cells must arrive strict A-then-B in material order")
    metrics_a: list[Any] = []
    metrics_b: list[Any] = []
    for name in material_ids:
        metric_a = by_arm[cell_a_id].get(name)
        metric_b = by_arm[cell_b_id].get(name)
        if metric_a is None or metric_b is None:
            raise ValueError(f"missing paired material {name!r}")
        metrics_a.append(metric_a)
        metrics_b.append(metric_b)
    return (metrics_a, metrics_b)


def carrier_decisions(
    nomination: PairNomination,
    left: EndpointObservation,
    right: EndpointObservation,
    verdict: str,
    item: ConflictFixture,
    index: int,
) -> PairAdjudicationInput:
    """Two agreeing seats over an existing carrier for oracle tests."""
    from mnemoseed_local.dream.adjudicate import PairSeatDecision, SeatOutcome, SeatStatus, Verdict

    parsed = Verdict(verdict)
    fixture = next(entry for entry in all_fixtures() if entry.fixture_id == item.fixture_id)
    if parsed is Verdict.CONFLICT:
        winner = fixture.expected_winner or fixture.left_node_id
        loser = fixture.expected_loser or fixture.right_node_id
    else:
        winner = None
        loser = None
    decision = PairSeatDecision(
        nomination_id=nomination.nomination_id,
        profile_id=nomination.profile_id,
        left_node_id=nomination.left_node_id,
        left_version=nomination.left_version,
        right_node_id=nomination.right_node_id,
        right_version=nomination.right_version,
        verdict=parsed,
        winner_node_id=winner,
        loser_node_id=loser,
        target_node_ids=(nomination.left_node_id, nomination.right_node_id),
        evidence_event_ids=nomination.evidence_event_ids,
    )
    outcome = SeatOutcome(status=SeatStatus.COMPLETE, decisions=(decision,))
    return PairAdjudicationInput(
        nomination=nomination,
        left=left,
        right=right,
        ensemble_mode="vote",
        seat_outcomes=(outcome, outcome),
    )


def carrier_decisions_mixed(
    nomination: PairNomination,
    left: EndpointObservation,
    right: EndpointObservation,
    item: ConflictFixture,
    index: int,
) -> PairAdjudicationInput:
    """One CONFLICT and one NOT_CONFLICT seat sharing one carrier."""
    agree = carrier_decisions(nomination, left, right, "conflict", item, index)
    disagree = carrier_decisions(nomination, left, right, "not_conflict", item, index)
    return PairAdjudicationInput(
        nomination=nomination,
        left=left,
        right=right,
        ensemble_mode="vote",
        seat_outcomes=(agree.seat_outcomes[0], disagree.seat_outcomes[0]),
    )


_SECRET_PATTERNS = ("bearer", "basic", "api-key", "apikey", "api_key")


def _scan_for_secrets(payload: Mapping[str, Any] | str, secrets: Sequence[str]) -> None:
    if isinstance(payload, str):
        text = payload
    else:
        redacted = {key: value for key, value in payload.items() if key != "api_key_env"}
        text = json.dumps(redacted, ensure_ascii=False)
    lowered = text.casefold()
    for pattern in _SECRET_PATTERNS:
        if pattern in lowered:
            raise ValueError("raw payload must never carry bearer/basic/api-key material")
    for secret in secrets:
        if secret and secret in text:
            raise ValueError("raw payload must never carry the resolved secret")
    url_match = re.search(r"://[^/\s]*:[^/\s]*@", text)
    if url_match:
        raise ValueError("raw payload must never carry URL-embedded credentials")


def write_raw_pair(
    run_root: Path,
    fixture_id: str,
    seat_index: int,
    payload: dict[str, Any],
    api_key_value: str = "",
    secrets: Sequence[str] = (),
) -> Path:
    """Atomically persist one per-pair/per-seat raw JSON after redaction."""
    values = [api_key_value, *secrets] if api_key_value else list(secrets)
    _scan_for_secrets(payload, values)
    directory = run_root / "raw"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{fixture_id}-seat{seat_index}.json"
    if target.exists():
        raise RuntimeError(f"refusing to overwrite raw artifact {target}")
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    descriptor, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".raw-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        Path(tmp_name).replace(target)
    finally:
        leftover = Path(tmp_name)
        if leftover.exists():
            leftover.unlink()
    return target


SeatFactory = Callable[..., Any]
MatrixRunner = Callable[..., Any]


def _resolve_secrets(config: ExecutorConfig, env: Callable[[str], str | None]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for label, seat in (
        ("reflect_a", config.reflect_a),
        ("reflect_b", config.reflect_b),
        ("vote_b", config.vote_b),
        ("verifier", config.verifier),
    ):
        value = env(seat.api_key_env) or ""
        resolved[label] = value
    return resolved


def _smoke_once(factory: SeatFactory, config: ExecutorConfig) -> None:
    client = factory(config.reflect_a, max_tokens=SMOKE_MAX_TOKENS, role="smoke")
    result = client.chat(system="pong", user="pong")
    _ = result


def adjudicate_arm(
    role: str,
    seat_texts: Sequence[tuple[str, str]],
    nomination: PairNomination,
    left: EndpointObservation,
    right: EndpointObservation,
) -> AdjudicationRow:
    """Parse seat texts strictly and adjudicate one arm without shortcuts."""

    if role == "verify" and len(seat_texts) != 1:
        return AdjudicationRow("unknown", True, Disposition.DEFERRED)
    if role == "vote" and len(seat_texts) != 2:
        return AdjudicationRow("unknown", True, Disposition.DEFERRED)
    outcomes: list[SeatOutcome] = [parse_pair_seat_output(text) for text, _ in seat_texts]
    mode = "verify" if role == "verify" else "vote"
    result = adjudicate(
        PairAdjudicationInput(
            nomination=nomination, left=left, right=right, ensemble_mode=mode, seat_outcomes=tuple(outcomes)
        )
    )
    return AdjudicationRow("unknown", True, result.disposition)


def _paired_canary_metrics(metrics_a: Sequence[Any], metrics_b: Sequence[Any]) -> tuple[list[Any], list[Any]]:
    paired_a: list[Any] = []
    paired_b: list[Any] = []
    for item_a, item_b in zip(metrics_a, metrics_b, strict=True):
        if item_a.canary_recall is not None and item_b.canary_recall is not None:
            paired_a.append(item_a)
            paired_b.append(item_b)
    return (paired_a, paired_b)


def sum_arm_cost(cells: Sequence[Any]) -> tuple[int, float, int]:
    """Aggregate tokens, durations, and collapse retries over one arm's cells."""
    tokens = sum(int(cell.cost.token_usage) for cell in cells)
    duration = sum(float(cell.cost.duration_s) for cell in cells)
    retries = sum(int(cell.reflect_collapse_attempts) for cell in cells)
    return (tokens, duration, retries)


def run_executor(
    config: ExecutorConfig,
    *,
    seat_factory: SeatFactory,
    matrix_runner: MatrixRunner,
    env: Callable[[str], str | None] | None = None,
    quota_on_smoke: bool = False,
) -> dict[str, Any]:
    """Drive smoke, matrix A-then-B, live pairs, and the final manifest."""
    manifest_path = config.run_root / "manifest.json"
    if manifest_path.exists():
        raise RuntimeError("partial runs are never resumed; use a new run ID")
    config.run_root.mkdir(parents=True, exist_ok=True)
    if any(config.run_root.iterdir()):
        raise RuntimeError("run root must be fresh; refusing to reuse prior state")
    resolve = env if env is not None else os.environ.get
    secrets_map = _resolve_secrets(config, resolve)
    secret_values = [value for value in secrets_map.values() if value]
    try:
        _smoke_once(seat_factory, config)
    except (QuotaStop, *_QUOTA_ERRORS) as exc:
        _scan_for_secrets(str(exc), secret_values)
        raise QuotaStop(str(exc)) from exc
    if quota_on_smoke:
        raise QuotaStop("quota rejected on smoke")
    cells = list(build_cells(config))
    assert [cell.ensemble for cell in cells] == ["off", "vote"]
    materials = list(build_materials())
    try:
        report = matrix_runner(cells, materials, root=config.run_root)
    except (QuotaStop, *_QUOTA_ERRORS) as exc:
        _scan_for_secrets(str(exc), secret_values)
        raise QuotaStop(str(exc)) from exc
    if isinstance(report, dict):
        raise ValueError("matrix must return a real EvalReport, dict snapshots are test-only")
    metrics_a, metrics_b = group_paired_canary(
        report, cells[0].cell_id, cells[1].cell_id, [item.name for item in materials]
    )
    skipped = tuple(getattr(report, "skipped", ()))
    if skipped:
        raise ValueError("matrix skips force INDETERMINATE; refusing paired scoring")
    rows_a: list[Any] = []
    rows_b: list[Any] = []
    raw_paths: list[str] = []
    raw_hashes: list[str] = []
    verify_client = seat_factory(config.verifier, role="verify")
    vote_client_a = seat_factory(config.reflect_b, role="vote_a")
    vote_client_b = seat_factory(config.vote_b, role="vote_b")
    for item in all_fixtures():
        nomination, left, right = adjudication_carrier_for(item.fixture_id)
        try:
            user = render_pair_adjudication_prompt(_prompt_only_input(nomination, left, right))
        except (TypeError, ValueError, AssertionError):
            raise
        try:
            text_a = _seat_text(verify_client, user)
            outcome_a = parse_pair_seat_output(text_a)
            decided_a = adjudicate(
                PairAdjudicationInput(
                    nomination=nomination,
                    left=left,
                    right=right,
                    ensemble_mode="verify",
                    seat_outcomes=(outcome_a,),
                )
            )
        except (QuotaStop, *_QUOTA_ERRORS) as exc:
            _scan_for_secrets(str(exc), secret_values)
            raise QuotaStop(str(exc)) from exc
        rows_a.append(AdjudicationRow(item.fixture_id, item.is_positive, decided_a.disposition))
        try:
            text_b1 = _seat_text(vote_client_a, user)
            text_b2 = _seat_text(vote_client_b, user)
            outcome_b1 = parse_pair_seat_output(text_b1)
            outcome_b2 = parse_pair_seat_output(text_b2)
            decided_b = adjudicate(
                PairAdjudicationInput(
                    nomination=nomination,
                    left=left,
                    right=right,
                    ensemble_mode="vote",
                    seat_outcomes=(outcome_b1, outcome_b2),
                )
            )
        except (QuotaStop, *_QUOTA_ERRORS) as exc:
            _scan_for_secrets(str(exc), secret_values)
            raise QuotaStop(str(exc)) from exc
        rows_b.append(AdjudicationRow(item.fixture_id, item.is_positive, decided_b.disposition))
        for seat_index, (text, client_label, outcome) in enumerate(
            ((text_a, "verify", outcome_a), (text_b1, "vote_a", outcome_b1), (text_b2, "vote_b", outcome_b2))
        ):
            seat = {"verify": config.verifier, "vote_a": config.reflect_b, "vote_b": config.vote_b}[
                client_label
            ]
            payload = {
                "driver": "openai_compatible",
                "model": seat.model,
                "base_url": seat.base_url,
                "api_key_env": seat.api_key_env,
                "system_prompt": PAIR_ADJUDICATION_SYSTEM_PROMPT,
                "system_version": PAIR_ADJUDICATION_PROMPT_VERSION,
                "prompt_hash": hashlib.sha256(user.encode()).hexdigest(),
                "response_text": text,
                "response_hash": hashlib.sha256(text.encode()).hexdigest(),
                "seat_status": str(outcome.status.value),
                "prompt_version": PAIR_ADJUDICATION_PROMPT_VERSION,
            }
            raw_path = write_raw_pair(
                config.run_root, item.fixture_id, seat_index, payload, secrets=secret_values
            )
            raw_paths.append(str(raw_path))
            raw_hashes.append(hashlib.sha256(raw_path.read_bytes()).hexdigest())
    paired_a, paired_b = _paired_canary_metrics(metrics_a, metrics_b)
    report_cells_a = [cell for cell in report.cells if cell.cell_id == cells[0].cell_id]
    report_cells_b = [cell for cell in report.cells if cell.cell_id == cells[1].cell_id]
    tokens_a, duration_a, retries_a = sum_arm_cost(report_cells_a)
    tokens_b, duration_b, retries_b = sum_arm_cost(report_cells_b)
    score_a = extraction_micro_f1(paired_a)
    score_b = extraction_micro_f1(paired_b)
    score_rows_a = adjudication_score(rows_a)
    score_rows_b = adjudication_score(rows_b)
    safety_failed = bool(score_rows_a.safety_failed or score_rows_b.safety_failed)
    material_ids = [item.name for item in materials]
    decision = decide(
        single_f1=score_a.f1,
        dual_f1=score_b.f1,
        single_fpr=score_rows_a.fpr,
        dual_fpr=score_rows_b.fpr,
        canary_informative=len(paired_a),
        canary_total=len(material_ids),
        positive_informative=score_rows_b.informative_positives,
        positive_total=8,
        negative_informative=score_rows_b.informative_negatives,
        negative_total=8,
        safety_failed=safety_failed,
    )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "run_id": uuid.uuid4().hex[:8],
        "repository_sha": config.repository_sha,
        "hardware": platform.machine(),
        "provider": "luna",
        "cell_a": {"cell_id": cells[0].cell_id, "model": config.reflect_a.model},
        "cell_b": {
            "cell_id": cells[1].cell_id,
            "model": config.reflect_b.model,
            "vote_b_model": config.vote_b.model,
        },
        "vote_b_distinct": (config.vote_b.model, config.vote_b.base_url)
        not in [
            (config.reflect_a.model, config.reflect_a.base_url),
            (config.reflect_b.model, config.reflect_b.base_url),
            (config.verifier.model, config.verifier.base_url),
        ],
        "prompt_version": SV_QUALITY_PROMPT_VERSION,
        "canary_seed": SV_QUALITY_CANARY_SEED,
        "material_ids": material_ids,
        "canary_hash": sv_canary_corpus_hash(),
        "positive_hash": positive_corpus_hash(),
        "negative_hash": negative_corpus_hash(),
        "preregistration_hash": prereg_for(
            prompt_version=SV_QUALITY_PROMPT_VERSION,
            canary_seed=SV_QUALITY_CANARY_SEED,
            reflect_a=(config.reflect_a.model, config.reflect_a.base_url),
            reflect_b=(config.reflect_b.model, config.reflect_b.base_url),
            vote_b=(config.vote_b.model, config.vote_b.base_url),
            verifier=(config.verifier.model, config.verifier.base_url),
            material_ids=tuple(material_ids),
            oracle_version=ORACLE_VERSION,
            denominator=DENOMINATOR_POLICY,
        ),
        "oracle_version": ORACLE_VERSION,
        "denominator": DENOMINATOR_POLICY,
        "tokens": {"single": tokens_a, "dual": tokens_b},
        "durations": {"single": duration_a, "dual": duration_b},
        "failures": [str(item) for item in skipped],
        "degraded": {
            "single": len(metrics_a) - len(paired_a),
            "dual": len(metrics_b) - len(paired_b),
        },
        "retries": {"single": retries_a, "dual": retries_b},
        "raw_paths": raw_paths,
        "raw_hashes": raw_hashes,
        "verdict": decision.verdict.value,
        "recommendation": decision.recommendation.value,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dry_run": False,
    }
    _scan_for_secrets(manifest, secret_values)
    path = write_manifest(config.run_root, manifest)
    print(f"intended org evidence mirror (not landed): {ORG_EVIDENCE_MIRROR}")
    return {"dry_run": False, "manifest": str(path), "attempts": {"single": 1, "dual": 1}}


def _prompt_only_input(
    nomination: PairNomination, left: EndpointObservation, right: EndpointObservation
) -> PairAdjudicationInput:

    return PairAdjudicationInput(
        nomination=nomination, left=left, right=right, ensemble_mode="verify", seat_outcomes=()
    )


def _seat_text(client: Any, user: str) -> str:
    result = client.chat(system=PAIR_ADJUDICATION_SYSTEM_PROMPT, user=user)
    if isinstance(result, str):
        return result
    text = getattr(result, "text", "")
    return str(text)


def main(argv: list[str] | None = None) -> int:
    """Refuse without an explicit private execution config; never a no-op success."""
    parser = argparse.ArgumentParser(prog="python -m mnemoseed_local.eval.sv_quality_execute")
    parser.add_argument("--run-root", default=None)
    parser.add_argument("--repository-sha", default=None)
    parser.add_argument("--confirm-luna-burn", action="store_true")
    args = parser.parse_args(argv or [])
    if not args.confirm_luna_burn:
        print("refusing: --confirm-luna-burn is required")
        return 2
    print("refusing: CLI is not a public execution surface; supply a private config to the API")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
