"""T4a — recall metrics math: 7 metrics over a synthetic RecallRunResult,
hand-computed fixtures (never LLM judgments).

Definitions (pinned here, documented in recall_metrics.py):

- Recall@k   = min(|served|, k) / candidate_pool        (None when pool = 0)
- Precision@k = |served ∩ referenced| / |served|        (None when served = 0)
- Floor-FP   = served_noise / noise_pool                (None when pool = 0)
- Detector-FP = Σ|reinf − ref| / Σ|reinf| over observations (None when 0/0)
- FN rate    = Σ|ref − reinf| / Σ|ref| over observations (None when 0/0)
- Overhead   = injected_chars / budget_chars            (None when budget = 0)
- Non-focal  = non_focal_above_floor, reported verbatim
"""

from __future__ import annotations

from dataclasses import replace

from mnemoseed_local.eval.recall_metrics import (
    RECALL_KS,
    RecallMetrics,
    RecallRunResult,
    ReplyObservation,
    score_recall,
)


def _base_run(**overrides) -> RecallRunResult:
    run = RecallRunResult(
        point_id="p0",
        served=("fact", "collision", "needle"),  # serve order
        candidate_pool=3,
        noise_pool=2,
        served_noise=2,
        observations=(
            ReplyObservation(template_name="cite", reinforced=("fact",), referenced=("fact",)),
            ReplyObservation(template_name="stray", reinforced=("fact", "collision"), referenced=("fact",)),
            ReplyObservation(template_name="no_cite", reinforced=(), referenced=()),
            ReplyObservation(template_name="paraphrase", reinforced=(), referenced=("fact",)),
        ),
        injected_chars=900,
        budget_chars=1200,
        non_focal_above_floor=1,
    )
    return replace(run, **overrides)


def test_hand_computed_fixture_all_seven_metrics() -> None:
    metrics = score_recall(_base_run())
    assert isinstance(metrics, RecallMetrics)
    assert RECALL_KS == (1, 3, 5, 10)
    # recall: served 3 of a 3-candidate pool, capped at k
    assert metrics.recall_at_k == (1 / 3, 1.0, 1.0, 1.0)
    # precision: only "fact" of the 3 served is genuinely referenced
    assert metrics.precision_at_k == (1 / 3, 1 / 3, 1 / 3, 1 / 3)
    # floor-fp: both entity-carrying noise chunks were served
    assert metrics.floor_fp == 1.0
    # detector-fp: of the 3 reinforcements, the stray's collision is false
    assert metrics.detector_fp == 1 / 3
    # fn rate: of the 3 genuine references, the paraphrase produced no needle
    assert metrics.fn_rate == 1 / 3
    # overhead: 900 injected chars against a 1200 cap
    assert metrics.token_overhead == 0.75
    assert metrics.non_focal_above_floor == 1


def test_empty_pool_recall_is_none() -> None:
    metrics = score_recall(_base_run(candidate_pool=0))
    assert metrics.recall_at_k == (None, None, None, None)


def test_no_served_precision_is_none() -> None:
    metrics = score_recall(_base_run(served=()))
    assert metrics.precision_at_k == (None, None, None, None)


def test_empty_noise_pool_floor_fp_is_none() -> None:
    metrics = score_recall(_base_run(noise_pool=0, served_noise=0))
    assert metrics.floor_fp is None


def test_no_reinforcements_detector_fp_is_none() -> None:
    run = _base_run(observations=(ReplyObservation(template_name="no_cite", reinforced=(), referenced=()),))
    metrics = score_recall(run)
    assert metrics.detector_fp is None
    assert metrics.fn_rate is None  # nothing referenced either


def test_no_budget_overhead_is_none() -> None:
    metrics = score_recall(_base_run(budget_chars=0))
    assert metrics.token_overhead is None


def test_perfect_detector_metrics() -> None:
    """A run where every reinforcement is genuine and every reference is
    caught: both integrity metrics are exactly 0."""
    run = _base_run(
        observations=(
            ReplyObservation(template_name="cite", reinforced=("fact",), referenced=("fact",)),
            ReplyObservation(template_name="no_cite", reinforced=(), referenced=()),
        )
    )
    metrics = score_recall(run)
    assert metrics.detector_fp == 0.0
    assert metrics.fn_rate == 0.0
    assert metrics.precision_at_k == (1 / 3, 1 / 3, 1 / 3, 1 / 3)


def test_reinforced_chunk_never_referenced_detector_fp_full() -> None:
    """A hallucinated citation: the detector reinforced a chunk no reply
    references — detector-fp = 1.0, fn = 0."""
    run = _base_run(
        observations=(
            ReplyObservation(template_name="hallucination", reinforced=("collision",), referenced=()),
        )
    )
    metrics = score_recall(run)
    assert metrics.detector_fp == 1.0
    assert metrics.fn_rate is None


def test_cited_but_undetected_fn_full() -> None:
    """A reference the needle matcher missed entirely: fn = 1.0, no
    reinforcements at all."""
    run = _base_run(
        observations=(ReplyObservation(template_name="paraphrase", reinforced=(), referenced=("fact",)),)
    )
    metrics = score_recall(run)
    assert metrics.fn_rate == 1.0
    assert metrics.detector_fp is None


def test_recall_at_k_capped_by_served() -> None:
    """A single served chunk against a 3-candidate pool: recall@1 = 1/3,
    and every larger k stays 1/3 (only 1 of 3 candidates was served)."""
    run = _base_run(
        served=("fact",),
        served_noise=0,
        observations=(ReplyObservation(template_name="cite", reinforced=("fact",), referenced=("fact",)),),
    )
    metrics = score_recall(run)
    assert metrics.recall_at_k == (1 / 3, 1 / 3, 1 / 3, 1 / 3)
    assert metrics.precision_at_k == (1.0, 1.0, 1.0, 1.0)
