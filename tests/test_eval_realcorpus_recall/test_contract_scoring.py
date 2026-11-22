"""Contract scoring, anti-gaming rules, and the measurement-neutrality killer."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.eval.realcorpus_recall.contract import (
    Thresholds,
    Verdict,
    check_no_drift,
    judge,
    replay_cold_starts,
)
from mnemoseed_local.eval.realcorpus_recall.manifest import InvalidRun
from mnemoseed_local.eval.realcorpus_recall.metrics import QuerySpec
from mnemoseed_local.eval.realcorpus_recall.replay import ProductRecallReplay, RecallObservation


def _query(
    qid: str,
    kind: str,
    *,
    holdout: bool = True,
    seen: bool = False,
    ingestion_verified: bool = True,
    targets: tuple[str, ...] = ("chunk-a",),
) -> QuerySpec:
    return QuerySpec(
        qid=qid,
        text=f"query {qid}",
        lang="zh",
        kind=kind,
        target_chunk_ids=list(targets),
        target_content_sha256="abc",
        ingestion_verified=ingestion_verified,
        rationale="probe",
        author="owner",
        date="2026-11-22",
        holdout=holdout,
        seen=seen,
        frozen_sha256="",
    )


def _observation(
    qid: str,
    served: tuple[tuple[str, float, int], ...],
    *,
    tokens_used: int | None = None,
) -> RecallObservation:
    from mnemoseed_local.eval.realcorpus_recall.replay import EntryObservation

    entries = tuple(
        EntryObservation(id=chunk_id, kind="chunk", rank=rank, score=score, tokens=tokens, text="")
        for rank, (chunk_id, score, tokens) in enumerate(served, start=1)
    )
    return RecallObservation(
        qid=qid,
        query="q",
        profile_id="p",
        session_id=None,
        entries=entries,
        dropped_count=0,
        tokens_used=sum(e.tokens for e in entries) if tokens_used is None else tokens_used,
        budget_tokens=800,
        coverage={},
    )


def test_ratified_and_proposal_defaults_are_distinguished() -> None:
    thresholds = Thresholds()
    assert thresholds.ratified_ids == {"B1", "B3"}
    assert thresholds.unratified_ids == {"B2", "B4", "DISP", "N", "P95"}
    assert thresholds.b2_recall_at_8 == 0.80
    assert thresholds.b4_noise_max == 0.20
    assert thresholds.dispersion_bound == 0.10
    assert thresholds.n_runs == 3
    assert thresholds.budget_tokens == 800


def test_ingestion_unverified_excluded_from_b2_denominator() -> None:
    queries = [
        _query("Q-001", "positive", targets=("chunk-a",)),
        _query("Q-002", "positive", ingestion_verified=False, targets=("chunk-b",)),
    ]
    runs = [
        {
            "Q-001": _observation("Q-001", (("chunk-a", 0.9, 10),)),
            "Q-002": _observation("Q-002", ()),
        }
    ]
    result = judge(runs, queries, Thresholds())  # type: ignore[arg-type]
    assert result.b2_denominator == 1
    assert list(result.diagnostics) == ["Q-002"]


def test_invalid_run_is_unknown_not_pass() -> None:
    queries = [
        _query("Q-001", "miss_seed"),
        _query("Q-002", "honest_empty", targets=()),
    ]
    runs = [
        {
            "Q-001": _observation("Q-001", (("chunk-a", 0.9, 10),)),
            "Q-002": _observation("Q-002", ()),
        }
    ]
    result = judge(runs, queries, Thresholds(), invalid_runs=1)  # type: ignore[arg-type]
    assert result.verdict is Verdict.UNKNOWN


def test_dispersion_overflow_is_unknown() -> None:
    queries = [_query("Q-001", "positive")]
    runs = [
        {"Q-001": _observation("Q-001", (("chunk-a", 0.9, 10),))},
        {"Q-001": _observation("Q-001", (("other", 0.9, 10),))},
    ]
    result = judge(runs, queries, Thresholds(dispersion_bound=0.10))  # type: ignore[arg-type]
    assert result.verdict is Verdict.UNKNOWN
    assert result.dispersion > 0.10


def test_seen_only_subset_cannot_pass() -> None:
    queries = [
        _query("Q-001", "miss_seed", holdout=False),
        _query("Q-002", "honest_empty", targets=(), holdout=False),
    ]
    runs = [
        {
            "Q-001": _observation("Q-001", (("chunk-a", 0.9, 10),)),
            "Q-002": _observation("Q-002", ()),
        }
    ]
    result = judge(runs, queries, Thresholds())  # type: ignore[arg-type]
    assert result.b2_denominator == 0
    assert result.verdict is not Verdict.PASS


def test_holdout_only_bars_ignore_seen_hits() -> None:
    queries = [
        _query("Q-001", "miss_seed", targets=("chunk-a",)),
        _query("Q-002", "honest_empty", targets=()),
        _query("Q-003", "positive", targets=("chunk-target",)),
        _query("Q-004", "positive", holdout=False, seen=True, targets=("chunk-seen",)),
    ]
    run = {
        "Q-001": _observation("Q-001", (("chunk-a", 0.9, 10),)),
        "Q-002": _observation("Q-002", ()),
        "Q-003": _observation("Q-003", (("other", 0.9, 10),)),
        "Q-004": _observation("Q-004", (("chunk-seen", 0.9, 10),)),
    }
    result = judge([run], queries, Thresholds(b2_recall_at_8=0.6))  # type: ignore[arg-type]
    assert result.b2_denominator == 2
    assert result.bars["B1"].verdict is Verdict.PASS
    assert result.bars["B3"].verdict is Verdict.PASS
    assert result.bars["B2"].verdict is Verdict.FAIL
    assert result.verdict is Verdict.FAIL


def test_ratified_miss_seed_and_honest_empty_are_enforced() -> None:
    queries = [
        _query("Q-001", "miss_seed"),
        _query("Q-002", "honest_empty", targets=()),
    ]
    failing = [
        {
            "Q-001": _observation("Q-001", (("other", 0.9, 10),)),
            "Q-002": _observation("Q-002", (("noise", 0.8, 10),)),
        }
    ]
    result = judge(failing, queries, Thresholds(b2_recall_at_8=0.0))  # type: ignore[arg-type]
    assert result.verdict is Verdict.FAIL
    assert result.bars["B1"].verdict is Verdict.FAIL
    assert result.bars["B3"].verdict is Verdict.FAIL


def test_pass_requires_all_bars_with_configurable_thresholds() -> None:
    queries = [
        _query("Q-001", "miss_seed"),
        _query("Q-002", "positive"),
        _query("Q-003", "honest_empty", targets=()),
    ]
    passing = [
        {
            "Q-001": _observation("Q-001", (("chunk-a", 0.9, 10),)),
            "Q-002": _observation("Q-002", (("chunk-a", 0.9, 10),)),
            "Q-003": _observation("Q-003", ()),
        }
    ]
    result = judge(passing, queries, Thresholds(b2_recall_at_8=0.5))  # type: ignore[arg-type]
    assert result.verdict is Verdict.PASS
    assert result.bars["B2"].ratified is False


class _ReboundingService:
    def __init__(self) -> None:
        self.baseline = {"Q-001": 0.30, "Q-002": 0.30}
        self.state = dict(self.baseline)

    def restore(self) -> str:
        self.state = dict(self.baseline)
        return hashlib.sha256(repr(sorted(self.state.items())).encode()).hexdigest()

    def recall(self, **kwargs: Any) -> dict[str, Any]:
        query = kwargs["query"]
        self.state[query] = min(1.0, self.state.get(query, 0.30) + 0.25)
        score = self.state[query]
        served = [{"id": "chunk-a", "kind": "chunk", "text": "a", "score": score, "tokens": 10}]
        if score > 0.8:
            served.insert(0, {"id": "distractor", "kind": "chunk", "text": "d", "score": 0.9, "tokens": 10})
        return {
            "memory": {
                "entries": served,
                "dropped_count": 0,
                "tokens_used": len(served) * 10,
                "coverage": {},
            }
        }


def test_measurement_neutrality_pristine_reset_has_zero_drift() -> None:
    service = _ReboundingService()
    runs = replay_cold_starts(
        ProductRecallReplay(service),
        [_query("Q-001", "miss_seed"), _query("Q-002", "honest_empty", targets=())],
        restore=service.restore,
        n_runs=3,
        profile_id="p",
    )
    check_no_drift(runs)  # type: ignore[arg-type]
    assert len({run.tokens_used for run in runs}) == 1
    assert len({run.baseline_sha256 for run in runs}) == 1


def test_measurement_neutrality_killer_reused_copy_drifts() -> None:
    service = _ReboundingService()
    runs: list[Any] = []
    for index in range(3):
        observations = {
            query.qid: ProductRecallReplay(service).replay(query.text, qid=query.qid, profile_id="p")
            for query in _queries()
        }
        from mnemoseed_local.eval.realcorpus_recall.metrics import score_run

        runs.append(score_run(observations, _queries(), run_index=index, baseline_sha256="stale"))
    with pytest.raises(InvalidRun):
        check_no_drift(runs)  # type: ignore[arg-type]


def _queries() -> list[QuerySpec]:
    return [_query("Q-001", "miss_seed"), _query("Q-002", "honest_empty", targets=())]


def test_report_append_is_byte_stable_except_timestamp_and_run_id(tmp_path: Path) -> None:
    from mnemoseed_local.eval.realcorpus_recall.report import (
        append_report,
        deterministic_payload,
        read_report,
    )

    record = {
        "utc": "2026-11-22T00:00:00Z",
        "run_id": "one",
        "verdict": "PASS",
        "pins": {"code_revision": "abc"},
    }
    path = tmp_path / "report.jsonl"
    append_report(path, record)
    append_report(path, {**record, "utc": "2026-11-22T01:00:00Z", "run_id": "two"})
    assert deterministic_payload(read_report(path)[0]) == deterministic_payload(read_report(path)[1])
    assert len(read_report(path)) == 2
