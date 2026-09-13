"""Issue #193 hostile tests: vote truthfulness + report v1.2 (RED first)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mnemoseed_local.dream.reflect import ReflectedTriple, ReflectionResult, Route
from mnemoseed_local.eval.harness import EvalCell, EvalRig, EvalRoute, RigPaths
from mnemoseed_local.eval.materials import material_catalog
from mnemoseed_local.eval.matrix import run_matrix
from mnemoseed_local.schema.stamp import CognitiveTier
from mnemoseed_local.storage.ports import TurnRange

STUB_A = EvalRoute(driver="stub", model="stub-a")
STUB_B_GEN = EvalRoute(driver="stub", model="stub-b")
STUB_V = EvalRoute(driver="stub_verifier", model="stub-v")

_RANGE = TurnRange(0, 4)


def _triple(
    subject: str,
    predicate: str,
    obj: str,
    *,
    model_id: str | None,
    route: Route = Route.CORE,
    disagreement: bool = False,
    polarity: str = "positive",
) -> ReflectedTriple:
    return ReflectedTriple(
        subject=subject,
        predicate=predicate,
        object=obj,
        tiers=(CognitiveTier.TIER_1,),
        chunk_ids=("c1",),
        turn_range=_RANGE,
        confidence=0.7,
        route=route,
        polarity=polarity,
        model_id=model_id,
        vote_disagreement=disagreement,
    )


def _result(triples: tuple[ReflectedTriple, ...]) -> ReflectionResult:
    return ReflectionResult(
        snapshot_id="s",
        profile_id="canary",
        turn_range=_RANGE,
        prompt_version="v1",
        triples=triples,
    )


def test_vote_cell_has_distinct_vote_b_route() -> None:
    cell = EvalCell(reflect=STUB_A, ensemble="vote", vote_b=STUB_B_GEN)
    assert cell.vote_b is not None
    assert cell.vote_b.model == "stub-b"
    assert cell.cell_id != EvalCell(reflect=STUB_A, ensemble="vote", vote_b=STUB_V).cell_id


def test_vote_rig_runs_both_seats_and_reports() -> None:
    import tempfile as _tf
    from pathlib import Path as _P  # noqa: F401

    with _tf.TemporaryDirectory() as tmp:
        rig = EvalRig(
            RigPaths(root=Path(tmp) / "rig"),
            EvalCell(reflect=STUB_A, ensemble="vote", vote_b=STUB_B_GEN),
        )
        try:
            from mnemoseed_local.eval.canary import canary_session

            run = rig.run_canary(canary_session(71, facts=4, noise=2))
        finally:
            rig.close()
    assert run.merge_committed
    assert run.reflect_result is not None
    assert run.reflect_outcome is not None
    assert run.reflect_outcome.result is not None
    assert run.core_nodes, "vote dream wrote no core nodes"


def test_vote_never_reuses_verifier_as_b(tmp_path: Path) -> None:
    from mnemoseed_local.eval.canary import canary_session

    verifier_only = EvalCell(reflect=STUB_A, ensemble="vote", verifier=STUB_V, vote_b=None)
    with pytest.raises(Exception, match="(?i)vote_b|vote.*B|missing"):
        rig = EvalRig(RigPaths(root=tmp_path / "rig"), verifier_only)
        try:
            rig.run_canary(canary_session(72, facts=2, noise=1))
        finally:
            rig.close()


def test_missing_b_cannot_succeed(tmp_path: Path) -> None:
    cells = [EvalCell(reflect=STUB_A, ensemble="vote", vote_b=None)]
    materials = material_catalog(None, canary_seed=1, canary_count=1)
    report = run_matrix(cells, materials, root=tmp_path)
    assert report.cells == ()
    assert len(report.skipped) == 1
    assert "vote" in report.skipped[0].reason.lower() or "missing" in report.skipped[0].reason.lower()


def test_off_verify_reports_have_no_vote(tmp_path: Path) -> None:
    cells = [
        EvalCell(reflect=STUB_A, ensemble="off", verifier=STUB_V),
        EvalCell(reflect=STUB_A, ensemble="verify", verifier=STUB_V),
    ]
    materials = material_catalog(None, canary_seed=1, canary_count=1)
    report = run_matrix(cells, materials, root=tmp_path)
    assert len(report.cells) == 2
    for cell in report.cells:
        assert cell.vote is None


def test_vote_metrics_counts() -> None:
    from mnemoseed_local.eval.metrics import vote_metrics

    result = _result(
        (
            _triple("user", "prefers", "dark mode", model_id="stub-a|stub-b", route=Route.CORE),
            _triple("user", "prefers", "light mode", model_id="stub-a", disagreement=True),
            _triple("user", "prefers", "sepia mode", model_id="stub-b", disagreement=True),
            _triple("user", "decided", "ship it", model_id="stub-a"),
        )
    )
    metrics = vote_metrics(result, model_a="stub-a", model_b="stub-b")
    assert metrics.model_a == "stub-a"
    assert metrics.model_b == "stub-b"
    assert metrics.agreement_triples == 1
    assert metrics.disagreement_parties == 2
    assert metrics.disagreement_groups == 1
    assert metrics.single_side_triples == 1
    assert metrics.dropped_polarity_conflicts == 0


def test_vote_parties_vs_groups() -> None:
    from mnemoseed_local.eval.metrics import vote_metrics

    result = _result(
        (
            _triple("user", "prefers", "a1", model_id="stub-a", disagreement=True),
            _triple("user", "prefers", "a2", model_id="stub-b", disagreement=True),
            _triple("user", "decided", "b1", model_id="stub-a", disagreement=True),
        )
    )
    metrics = vote_metrics(result, model_a="stub-a", model_b="stub-b")
    assert metrics.disagreement_parties == 3
    assert metrics.disagreement_groups == 2


def test_vote_dropped_conflict_absent_graph() -> None:
    from mnemoseed_local.eval.metrics import vote_metrics

    base = _result(())
    result = type(base)(
        snapshot_id=base.snapshot_id,
        profile_id=base.profile_id,
        turn_range=base.turn_range,
        prompt_version=base.prompt_version,
        triples=(),
        conflicts=(("user", "prefers", "dark mode"),),
    )
    metrics = vote_metrics(result, model_a="stub-a", model_b="stub-b")
    assert metrics.dropped_polarity_conflicts == 1
    assert metrics.agreement_triples == 0
    assert metrics.single_side_triples == 0


def test_vote_single_side_neither() -> None:
    from mnemoseed_local.eval.metrics import vote_metrics

    result = _result((_triple("user", "prefers", "solo", model_id="stub-a"),))
    metrics = vote_metrics(result, model_a="stub-a", model_b="stub-b")
    assert metrics.single_side_triples == 1
    assert metrics.agreement_triples == 0
    assert metrics.disagreement_parties == 0


def test_vote_same_model_case() -> None:
    from mnemoseed_local.eval.metrics import vote_metrics

    result = _result(
        (
            _triple("user", "prefers", "both", model_id="stub-a|stub-a", route=Route.CORE),
            _triple("user", "prefers", "one", model_id="stub-a"),
        )
    )
    metrics = vote_metrics(result, model_a="stub-a", model_b="stub-a")
    assert metrics.agreement_triples == 1
    assert metrics.single_side_triples == 1


def test_vote_delimiter_guard() -> None:
    from mnemoseed_local.eval.metrics import vote_metrics

    result = _result((_triple("user", "prefers", "x", model_id="a|b"),))
    with pytest.raises(ValueError, match="(?i)pipe|delimiter|\\|"):
        vote_metrics(result, model_a="a|b", model_b="stub-b")


def test_report_v12_roundtrip_preserves_vote(tmp_path: Path) -> None:
    from mnemoseed_local.eval.metrics import CostMetrics, VerifyMetrics
    from mnemoseed_local.eval.report import CellReport, EvalReport, ReportedTriple, load_report, write_report

    triple = ReportedTriple(
        graph="main",
        node_id="n1",
        subject="user",
        predicate="prefers",
        object="dark mode",
        polarity="positive",
        confidence=0.8,
        model_id="stub-a|stub-b",
        vote_disagreement=False,
    )
    report = EvalReport(
        eval_version="v1.2",
        started_at="2026-08-22T00:00:00Z",
        cells=(
            CellReport(
                cell_id="c",
                material="canary-00",
                canary=None,
                verify=VerifyMetrics(
                    verifier_model=None,
                    judged=0,
                    accepted=0,
                    rejected=0,
                    rejected_keys=(),
                    fallbacks={},
                ),
                cost=CostMetrics(
                    duration_s=1.0,
                    token_usage=10,
                    reflect_prompt_tokens=None,
                    reflect_completion_tokens=None,
                    verify_tokens=None,
                ),
                triples=(triple,),
            ),
        ),
    )
    path = write_report(report, tmp_path, matrix_slug="v12")
    loaded = load_report(path)
    assert loaded == report
    assert loaded.cells[0].triples[0].model_id == "stub-a|stub-b"
    assert loaded.cells[0].triples[0].vote_disagreement is False


def test_old_schema_loads_with_defaults(tmp_path: Path) -> None:
    from mnemoseed_local.eval.report import load_report

    old = {
        "eval_version": "v1.1",
        "started_at": "2026-08-18T00:00:00Z",
        "cells": [
            {
                "cell_id": "c",
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
                    "duration_s": 1.0,
                    "token_usage": 5,
                    "reflect_prompt_tokens": None,
                    "reflect_completion_tokens": None,
                    "verify_tokens": None,
                },
                "triples": [
                    {
                        "graph": "main",
                        "node_id": "n1",
                        "subject": "user",
                        "predicate": "prefers",
                        "object": "dark mode",
                        "polarity": "positive",
                        "confidence": 0.7,
                    }
                ],
            }
        ],
        "skipped": [],
    }
    path = tmp_path / "old.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    loaded = load_report(path)
    triple = loaded.cells[0].triples[0]
    assert triple.model_id is None
    assert triple.vote_disagreement is False
    assert loaded.cells[0].vote is None


def test_vote_report_carries_combined_evidence(tmp_path: Path) -> None:
    cells = [EvalCell(reflect=STUB_A, ensemble="vote", vote_b=STUB_B_GEN)]
    materials = material_catalog(None, canary_seed=1, canary_count=1)
    report = run_matrix(cells, materials, root=tmp_path)
    assert report.eval_version == "v1.2"
    assert len(report.cells) == 1
    cell = report.cells[0]
    assert cell.vote is not None
    assert cell.vote.model_a == "stub-a"
    assert cell.vote.model_b == "stub-b"
    assert cell.vote.agreement_triples > 0
    assert cell.vote.dropped_polarity_conflicts == 0
    assert cell.triples, "vote report must embed triples"
    assert any(t.model_id == "stub-a|stub-b" for t in cell.triples)
    assert all(t.model_id is not None for t in cell.triples)


def test_vote_rescore_preserves_vote(tmp_path: Path) -> None:
    from mnemoseed_local.eval.report import load_report, write_report
    from mnemoseed_local.eval.rescore import rescore_report

    cells = [EvalCell(reflect=STUB_A, ensemble="vote", vote_b=STUB_B_GEN)]
    materials = material_catalog(None, canary_seed=7, canary_count=1)
    report = run_matrix(cells, materials, root=tmp_path / "root")
    path = write_report(report, tmp_path / "reports", matrix_slug="vote")
    rescored = load_report(rescore_report(path, canary_seed=7))
    assert rescored.cells[0].vote == report.cells[0].vote
    assert rescored.cells[0].triples == report.cells[0].triples


def test_vote_b_route_probed(tmp_path: Path) -> None:
    missing_b = EvalRoute(driver="ollama", model="absent-model:9b")
    cells = [EvalCell(reflect=STUB_A, ensemble="vote", vote_b=missing_b)]

    def fake_tags(base_url: str, timeout: float) -> tuple[str, ...]:
        return ()

    materials = material_catalog(None, canary_seed=1, canary_count=1)
    report = run_matrix(cells, materials, root=tmp_path, fetch_tags=fake_tags)
    assert report.cells == ()
    assert len(report.skipped) == 1
    assert "absent-model:9b" in report.skipped[0].reason


def test_vote_exact_seat_ids_no_substring() -> None:
    from mnemoseed_local.eval.metrics import vote_metrics

    result = _result((_triple("user", "prefers", "x", model_id="stub-a|stub-b", route=Route.CORE),))
    metrics = vote_metrics(result, model_a="stub", model_b="stub-b")
    assert metrics.agreement_triples == 0
    assert metrics.single_side_triples == 1


def test_vote_triple_overlay_uses_canonical_exact_key(tmp_path: Path) -> None:
    cells = [EvalCell(reflect=STUB_A, ensemble="vote", vote_b=STUB_B_GEN)]
    materials = material_catalog(None, canary_seed=1, canary_count=1)
    report = run_matrix(cells, materials, root=tmp_path)
    cell = report.cells[0]
    for triple in cell.triples:
        assert triple.subject and triple.predicate and triple.object and triple.polarity
        assert triple.model_id is not None
        assert triple.vote_disagreement is False


def test_vote_failure_has_no_fabricated_metrics(tmp_path: Path) -> None:
    from mnemoseed_local.llm.drivers.stub import StubLLM

    original = StubLLM.chat

    def always_fail(self, *, system: str, user: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    StubLLM.chat = always_fail  # type: ignore[method-assign]
    try:
        cells = [EvalCell(reflect=STUB_A, ensemble="vote", vote_b=STUB_B_GEN)]
        materials = material_catalog(None, canary_seed=1, canary_count=1)
        report = run_matrix(cells, materials, root=tmp_path)
    finally:
        StubLLM.chat = original  # type: ignore[method-assign]
    assert report.cells == () or all(c.vote is None for c in report.cells)
