"""B3 T3 — metrics + report: canary scoring, verify replay, cost, JSON persistence.

Metric definitions are pinned by hand-computed fixtures (never LLM judgments):

- canary_recall: matched facts / total facts (None when the session has none)
- noise_pollution: core nodes citing a noise chunk (provenance chunk_ids, not
  text similarity); the noise chunk set comes from the exact turn->session->
  chunk attribution the rig recorded
- core_yield: total core nodes (read against the fact count)
- verify metrics: audit-replayed judged/accepted/rejected + fallback reasons
- cost: duration/token counters; reflect-vs-verify token decomposition via
  DeltaReport provider usage + ensemble_verified audit tokens (None honestly
  when a side is unknowable)
"""

from __future__ import annotations

import json

import pytest

from mnemoseed_local.eval.canary import CanarySession, canary_session
from mnemoseed_local.eval.harness import (
    CellRun,
    EvalCell,
    EvalRig,
    EvalRoute,
    RecordedChunk,
    RecordedNode,
    RigPaths,
)
from mnemoseed_local.eval.metrics import (
    CanaryMetrics,
    CostMetrics,
    VerifyMetrics,
    cost_metrics,
    score_canary,
    verify_metrics,
)
from mnemoseed_local.eval.report import (
    SEAT_SEED_POLICY_FIXED,
    SEAT_SEED_POLICY_NONE,
    CellReport,
    EvalReport,
    SkippedCell,
    load_report,
    write_report,
)


def _fact_turn_index(session: CanarySession, fact_id: str) -> int:
    return next(i for i, t in enumerate(session.turns) if t.fact_id == fact_id)


STUB_A = EvalRoute(driver="stub", model="stub-a")
STUB_B = EvalRoute(driver="stub_verifier", model="stub-b")


@pytest.fixture
def canary_run(tmp_path):
    rig = EvalRig(
        RigPaths(root=tmp_path / "rig"),
        EvalCell(reflect=STUB_A, ensemble="verify", verifier=STUB_B),
    )
    try:
        session = canary_session(31, facts=8, noise=4)
        yield session, rig.run_canary(session)
    finally:
        rig.close()


def test_canary_recall_full_on_stub(canary_run) -> None:
    session, run = canary_run
    metrics = score_canary(session, run)
    assert isinstance(metrics, CanaryMetrics)
    # the stub's rule-based extraction covers every canary fact class by
    # construction (same canonical predicates) — recall must be perfect here
    assert metrics.canary_recall == 1.0
    assert list(metrics.matched_fact_ids) == sorted(f.fact_id for f in session.facts)
    assert list(metrics.missed_fact_ids) == []


def test_noise_pollution_zero_on_stub(canary_run) -> None:
    session, run = canary_run
    metrics = score_canary(session, run)
    assert metrics.noise_pollution == 0
    assert list(metrics.polluting_nodes) == []
    assert metrics.core_yield == len(run.core_nodes)


def test_recall_none_when_no_facts(tmp_path) -> None:
    rig = EvalRig(
        RigPaths(root=tmp_path / "rig"),
        EvalCell(reflect=STUB_A, ensemble="off", verifier=STUB_B),
    )
    try:
        session = canary_session(32, facts=0, noise=4)
        run = rig.run_canary(session)
    finally:
        rig.close()
    metrics = score_canary(session, run)
    assert metrics.canary_recall is None  # honest 0/0: no facts, no recall
    assert metrics.noise_pollution == 0


def test_hand_computed_synthetic_fixture() -> None:
    # fully synthetic run (no rig): 4-fact/2-noise session, 3 facts matched by
    # hand, fact[3] missed, one core node citing a noise chunk, one extra core
    # node matching no fact — every metric below is hand-computed.
    session = canary_session(33, facts=4, noise=2)
    n_turns = len(session.turns)
    turn_sessions = tuple(f"s-s{index:02d}" for index in range(n_turns))
    chunks = tuple(
        RecordedChunk(
            chunk_id=f"ck{index:02d}",
            session_id=sid,
            turn_start=0,
            turn_end=0,
            text=session.turns[index].text,
            consolidated=True,
        )
        for index, sid in enumerate(turn_sessions)
    )
    nodes: list[RecordedNode] = []
    facts = {f.fact_id: f for f in session.facts}
    # match fact turns 0..len(facts)-2 via their own phrasing; leave the last out
    for fact in session.facts[:-1]:
        nodes.append(
            RecordedNode(
                node_id=f"n-{fact.fact_id}",
                graph="main",
                subject="user",
                predicate=fact.predicate,
                object=f"we {fact.phrasings[0]} here",
                polarity=fact.polarity,
                confidence=0.7,
                chunk_ids=(f"ck{_fact_turn_index(session, fact.fact_id):02d}",),
            )
        )
    del facts
    # one polluting node citing a NOISE chunk
    noise_index = next(i for i, t in enumerate(session.turns) if t.noise is not None)
    nodes.append(
        RecordedNode(
            node_id="n-noise",
            graph="main",
            subject="session",
            predicate="discussed",
            object="deploy plan",
            polarity="positive",
            confidence=0.5,
            chunk_ids=(f"ck{noise_index:02d}",),
        )
    )
    # one over-extracted core node matching no fact
    nodes.append(
        RecordedNode(
            node_id="n-extra",
            graph="main",
            subject="user",
            predicate="prefers",
            object="unrelated hobby",
            polarity="positive",
            confidence=0.6,
            chunk_ids=(f"ck{_fact_turn_index(session, session.facts[0].fact_id):02d}",),
        )
    )
    run = CellRun(
        cell_id="synthetic",
        profile_id=session.profile_id,
        merge_committed=True,
        merge_summary=None,
        reflect_outcome=None,
        reflect_result=None,
        core_nodes=tuple(nodes),
        isolated_nodes=(),
        chunks=chunks,
        audit=(),
        token_usage=0,
        duration_s=0.0,
        turn_sessions=turn_sessions,
    )
    metrics = score_canary(session, run)
    assert metrics.facts_total == 4
    assert metrics.facts_matched == 3
    assert metrics.canary_recall == 0.75
    assert list(metrics.missed_fact_ids) == [session.facts[-1].fact_id]
    assert metrics.noise_pollution == 1
    assert list(metrics.polluting_nodes) == ["n-noise"]
    assert metrics.core_yield == len(nodes)
    assert list(metrics.extra_core_nodes) == ["n-extra", "n-noise"]


def test_verify_metrics_from_audit(canary_run) -> None:
    _, run = canary_run
    verify = verify_metrics(run)
    assert isinstance(verify, VerifyMetrics)
    verified = [a for a in run.audit if a.action == "ensemble_verified"]
    assert verified, "fixture expects a verified audit"
    assert verify.judged == verified[0].detail["judged"]
    assert verify.accepted + verify.rejected == verify.judged
    assert verify.fallbacks == {}
    assert verify.verifier_model == "stub-b"


def test_verify_metrics_fallback_reason(tmp_path) -> None:
    # an unreachable verifier route: ensemble=verify, B seat a dead ollama URL —
    # the honest-cost fallback must land A's result + a countable fallback row
    rig = EvalRig(
        RigPaths(root=tmp_path / "rig"),
        EvalCell(
            reflect=STUB_A,
            ensemble="verify",
            verifier=EvalRoute(
                driver="ollama", model="nothing:0b", params=(("base_url", "http://localhost:9"),)
            ),
        ),
    )
    try:
        run = rig.run_canary(canary_session(34, facts=4, noise=2))
    finally:
        rig.close()
    verify = verify_metrics(run)
    assert verify.judged == 0
    assert verify.fallbacks == {"llm_unavailable": 1}
    assert verify.verifier_model == "nothing:0b"


def test_verify_metrics_off_mode(tmp_path) -> None:
    rig = EvalRig(
        RigPaths(root=tmp_path / "rig"),
        EvalCell(reflect=STUB_A, ensemble="off", verifier=STUB_B),
    )
    try:
        run = rig.run_canary(canary_session(35, facts=4, noise=2))
    finally:
        rig.close()
    verify = verify_metrics(run)
    assert verify.judged == 0
    assert verify.fallbacks == {}
    assert verify.verifier_model is None


def test_cost_metrics(canary_run) -> None:
    _, run = canary_run
    cost = cost_metrics(run)
    assert isinstance(cost, CostMetrics)
    assert cost.duration_s > 0.0
    assert cost.token_usage > 0
    # stub seats report no provider usage: honest None, never invented numbers
    assert cost.reflect_prompt_tokens is None
    assert cost.reflect_completion_tokens is None
    # the verify ledger row is auditable: ensemble_verified carries its tokens
    assert cost.verify_tokens is not None and cost.verify_tokens > 0


def _report_from(session, run) -> EvalReport:
    return EvalReport(
        eval_version="v1",
        started_at="2026-08-18T00:00:00Z",
        cells=(
            CellReport(
                cell_id=run.cell_id,
                material=session.session_id,
                canary=score_canary(session, run),
                verify=verify_metrics(run),
                cost=cost_metrics(run),
            ),
        ),
        skipped=(),
    )


def test_report_round_trip(canary_run, tmp_path) -> None:
    session, run = canary_run
    report = _report_from(session, run)
    out_dir = tmp_path / "eval-reports"
    path = write_report(report, out_dir, matrix_slug="stub-matrix")
    assert path.exists()
    loaded = load_report(path)
    assert loaded == report
    assert path.name.startswith("2026-08-18T00-00-00Z-stub-matrix")
    assert path.suffix == ".json"


def test_report_filenames_never_collide(canary_run, tmp_path) -> None:
    session, run = canary_run
    report = _report_from(session, run)
    out_dir = tmp_path / "eval-reports"
    first = write_report(report, out_dir, matrix_slug="m")
    second = write_report(report, out_dir, matrix_slug="m")
    assert first != second
    assert first.exists() and second.exists()


def test_report_skipped_cells_round_trip(canary_run, tmp_path) -> None:
    session, run = canary_run
    report = EvalReport(
        eval_version="v1",
        started_at="2026-08-18T00:00:00Z",
        cells=(
            CellReport(
                cell_id=run.cell_id,
                material=session.session_id,
                canary=score_canary(session, run),
                verify=verify_metrics(run),
                cost=cost_metrics(run),
            ),
        ),
        skipped=(SkippedCell(cell_id="qwen3_5_9b+off+d32000+f0", reason="model not pulled: qwen3.5:9b"),),
    )
    path = write_report(report, tmp_path / "eval-reports", matrix_slug="m")
    assert load_report(path).skipped == report.skipped


def test_report_v11_round_trip_preserves_b4a_fields(canary_run, tmp_path) -> None:
    session, run = canary_run
    report = EvalReport(
        eval_version="v1.1",
        started_at="2026-08-20T00:00:00Z",
        cells=(
            CellReport(
                cell_id=run.cell_id,
                material=session.session_id,
                canary=score_canary(session, run),
                verify=verify_metrics(run),
                cost=cost_metrics(run),
                reflect_collapse_attempts=2,
                reflect_recovered=True,
                seat_seed=42,
            ),
        ),
        skipped=(),
        seat_seed_policy=SEAT_SEED_POLICY_FIXED,
    )
    path = write_report(report, tmp_path / "eval-reports", matrix_slug="b4a")
    loaded = load_report(path)
    assert loaded == report
    cell = loaded.cells[0]
    assert cell.reflect_collapse_attempts == 2
    assert cell.reflect_recovered is True
    assert cell.seat_seed == 42
    assert loaded.seat_seed_policy == SEAT_SEED_POLICY_FIXED


def test_pre_b4a_report_loads_with_default_fields(tmp_path) -> None:
    """A pre-B4a v1.1 report (no collapse/seed fields) loads with defaults —
    never a crash, never an invented seed. The top-level policy defaults to
    NONE: pre-B4a seats were unseeded, so a "per-seat-fixed" label would
    contradict the cells' seat_seed=None."""
    old = {
        "eval_version": "v1.1",
        "started_at": "2026-08-18T19:00:00Z",
        "cells": [
            {
                "cell_id": "qwen3_5_9b+off+d32000+f0",
                "material": "canary-00",
                "canary": None,
                "verify": {
                    "verifier_model": None,
                    "judged": 0,
                    "accepted": 0,
                    "rejected": 0,
                    "rejected_keys": [],
                    "fallbacks": {},
                },
                "cost": {
                    "duration_s": 4.3,
                    "token_usage": 522,
                    "reflect_prompt_tokens": 1370,
                    "reflect_completion_tokens": 2,
                    "verify_tokens": None,
                },
            }
        ],
        "skipped": [],
    }
    path = tmp_path / "pre-b4a.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    loaded = load_report(path)
    assert loaded.seat_seed_policy == SEAT_SEED_POLICY_NONE
    cell = loaded.cells[0]
    assert cell.reflect_collapse_attempts == 0
    assert cell.reflect_recovered is False
    assert cell.seat_seed is None
