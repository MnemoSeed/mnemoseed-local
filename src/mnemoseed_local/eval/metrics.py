"""Eval metrics (B3 T3): hand-computable scores over one CellRun read-back.

Definitions are deliberately dumb and honest:

- ``canary_recall``: matched facts / total facts; ``None`` for a session with
  no facts (0/0 is unknown, never silently 1.0 or 0.0) or for a
  reflect-seat-failed run (collapse attempts > 0, never recovered — the seat
  produced no extraction, so a numeric 0.00 would mislead);
- ``noise_pollution``: core nodes citing at least one NOISE chunk. The
  evidence channel is provenance (node chunk_ids ∩ noise-attributed chunk
  ids) — never text similarity, so an "almost-noise" match is not pollution;
- ``core_yield``: raw core node count, read against the fact count;
- ``extra_core_nodes``: core nodes matching no canary fact (over-extraction
  visibility without a false-precision claim);
- verify metrics replay the audit log (``ensemble_verified`` /
  ``ensemble_verify_fallback``), filtered to THIS run's snapshot id so a rig
  reused across materials never leaks one material's judgment into another;
- cost: the run's own token delta (ledger monthly-counter diff — cross-
  material safe), the wall duration, and provider usage where the seat
  reported it (``None`` honestly when not — never invented numbers).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mnemoseed_local.eval.canary import CanarySession, matches_fact
from mnemoseed_local.eval.harness import CellRun

_VERIFIED_ACTION = "ensemble_verified"
_FALLBACK_ACTION = "ensemble_verify_fallback"


@dataclass(frozen=True)
class CanaryMetrics:
    """Canary scoring for one material under one cell."""

    facts_total: int
    facts_matched: int
    canary_recall: float | None  # None when the session carried no facts
    matched_fact_ids: tuple[str, ...]
    missed_fact_ids: tuple[str, ...]
    noise_pollution: int  # core nodes citing a noise chunk (ideal: 0)
    polluting_nodes: tuple[str, ...]  # their node ids
    core_yield: int
    extra_core_nodes: tuple[str, ...]  # core nodes matching no fact


@dataclass(frozen=True)
class VerifyMetrics:
    """Ensemble verify replay for one run (all-zero + model None when off)."""

    verifier_model: str | None
    judged: int
    accepted: int
    rejected: int
    rejected_keys: tuple[str, ...]
    fallbacks: dict[str, int]  # reason -> count


@dataclass(frozen=True)
class CostMetrics:
    """Cost surface for one run; provider fields None when unreported."""

    duration_s: float
    token_usage: int
    reflect_prompt_tokens: int | None
    reflect_completion_tokens: int | None
    verify_tokens: int | None


def _node_props(node: Any) -> dict[str, Any]:
    return {"predicate": node.predicate, "object": node.object, "polarity": node.polarity}


def score_canary(session: CanarySession, run: CellRun) -> CanaryMetrics:
    """Score one canary run against its embedded ground truth.

    Noise attribution is exact: turn index -> mini-session id (rig-recorded)
    -> chunk ids citing that session. A core node polluting on a noise chunk
    is pollution regardless of what its object text looks like.
    """
    noise_sessions = {
        run.turn_sessions[index]
        for index, turn in enumerate(session.turns)
        if turn.noise is not None and index < len(run.turn_sessions)
    }
    noise_chunk_ids = {c.chunk_id for c in run.chunks if c.session_id in noise_sessions}

    matched: list[str] = []
    missed: list[str] = []
    for fact in session.facts:
        if any(matches_fact(_node_props(node), fact) for node in run.core_nodes):
            matched.append(fact.fact_id)
        else:
            missed.append(fact.fact_id)

    polluting: list[str] = []
    extra: list[str] = []
    for node in run.core_nodes:
        if set(node.chunk_ids) & noise_chunk_ids:
            polluting.append(node.node_id)
        if not any(matches_fact(_node_props(node), fact) for fact in session.facts):
            extra.append(node.node_id)

    total = len(session.facts)
    recall: float | None = (len(matched) / total) if total else None
    if run.reflect_collapse_attempts > 0 and not run.reflect_recovered:
        # reflect-seat-failed: the seat produced no extraction, so recall is
        # unknowable (a numeric 0.00 would mislead the report).
        recall = None
    return CanaryMetrics(
        facts_total=total,
        facts_matched=len(matched),
        canary_recall=recall,
        matched_fact_ids=tuple(sorted(matched)),
        missed_fact_ids=tuple(sorted(missed)),
        noise_pollution=len(polluting),
        polluting_nodes=tuple(sorted(polluting)),
        core_yield=len(run.core_nodes),
        extra_core_nodes=tuple(sorted(extra)),
    )


def verify_metrics(run: CellRun) -> VerifyMetrics:
    """Replay this run's ensemble audit entries (filtered to the run's own
    snapshot id: a rig reused across materials never mixes judgments)."""
    snapshot_id = run.merge_summary.snapshot_id if run.merge_summary is not None else None
    judged = 0
    accepted = 0
    rejected = 0
    rejected_keys: list[str] = []
    fallbacks: dict[str, int] = {}
    model: str | None = None
    for entry in run.audit:
        if entry.action not in (_VERIFIED_ACTION, _FALLBACK_ACTION):
            continue
        if snapshot_id is not None and str(entry.detail.get("run_id", "")) != snapshot_id:
            continue
        if entry.action == _VERIFIED_ACTION:
            judged += int(entry.detail.get("judged", 0))
            accepted += int(entry.detail.get("accepted", 0))
            rejected += int(entry.detail.get("rejected", 0))
            rejected_keys.extend(str(k) for k in entry.detail.get("rejected_keys", []))
            model = str(entry.detail.get("verifier_model", "")) or model
        else:
            reason = str(entry.detail.get("reason", "unknown"))
            fallbacks[reason] = fallbacks.get(reason, 0) + 1
            model = str(entry.detail.get("verifier_model", "")) or model
    return VerifyMetrics(
        verifier_model=model,
        judged=judged,
        accepted=accepted,
        rejected=rejected,
        rejected_keys=tuple(rejected_keys),
        fallbacks=fallbacks,
    )


def cost_metrics(run: CellRun) -> CostMetrics:
    """Cost surface: duration, this run's ledger delta, provider usage when the
    seat reported any, and the verify pass's auditable token spend."""
    report = run.reflect_outcome.report if run.reflect_outcome is not None else None
    usage = report.provider_usage if report is not None else None
    verify_tokens: int | None = None
    snapshot_id = run.merge_summary.snapshot_id if run.merge_summary is not None else None
    for entry in run.audit:
        if entry.action != _VERIFIED_ACTION:
            continue
        if snapshot_id is not None and str(entry.detail.get("run_id", "")) != snapshot_id:
            continue
        tokens = entry.detail.get("tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            verify_tokens = (verify_tokens or 0) + tokens
    return CostMetrics(
        duration_s=run.duration_s,
        token_usage=run.token_usage,
        reflect_prompt_tokens=usage.prompt_tokens if usage is not None else None,
        reflect_completion_tokens=usage.completion_tokens if usage is not None else None,
        verify_tokens=verify_tokens,
    )
