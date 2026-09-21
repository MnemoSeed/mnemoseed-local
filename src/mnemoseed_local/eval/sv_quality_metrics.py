"""Pure oracle metrics for the single-vs-dual quality comparison.

Extraction micro F1 aggregates existing CanaryMetrics fields only; vote
counts stay diagnostic and never enter the verdict. Adjudication FPR counts
terminal negative results; degraded rows are excluded from denominators.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from mnemoseed_local.eval.metrics import CanaryMetrics
from mnemoseed_local.storage.ports import Disposition


class AdjudicationRowLike(Protocol):
    """Structural row for FPR scoring without importing the runner."""

    is_positive: bool
    disposition: Disposition


ORACLE_VERSION = "v1"
F1_DELTA_PP = 0.05
FPR_RELATIVE_DROP = 0.20
CANARY_INFORMATIVE_BAR = 18
CANARY_TOTAL = 24
PAIR_INFORMATIVE_BAR = 6
PAIR_TOTAL = 8
DENOMINATOR_POLICY = "terminal-only-v1"


class Verdict(StrEnum):
    """Experiment verdict, kept separate from the posture recommendation."""

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class Recommendation(StrEnum):
    """Posture recommendation accompanying the verdict."""

    RETAIN_DUAL = "retain_dual"
    RECOMMEND_SINGLE = "recommend_single"
    SINGLE_OFF = "single_off"


@dataclass(frozen=True)
class ExtractionScore:
    """Micro-averaged extraction score with explicit undefined handling."""

    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    f1: float | None


def extraction_micro_f1(metrics: Sequence[CanaryMetrics]) -> ExtractionScore:
    """Micro F1 over facts matched, missed facts, and extra core nodes."""
    true_positives = sum(item.facts_matched for item in metrics)
    false_negatives = sum(len(item.missed_fact_ids) for item in metrics)
    false_positives = sum(len(item.extra_core_nodes) for item in metrics)
    precision: float | None = None
    recall: float | None = None
    if true_positives + false_positives > 0:
        precision = true_positives / (true_positives + false_positives)
    if true_positives + false_negatives > 0:
        recall = true_positives / (true_positives + false_negatives)
    f1: float | None = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = 2 * precision * recall / (precision + recall)
    return ExtractionScore(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def canary_informative_count(metrics: Sequence[CanaryMetrics]) -> int:
    """Sessions carrying a defined recall judgment (facts never counted)."""
    return sum(1 for item in metrics if item.canary_recall is not None)


@dataclass(frozen=True)
class AdjudicationScore:
    """FPR over terminal negatives plus informative-N accounting."""

    false_positives: int
    true_negatives: int
    fpr: float | None
    informative_positives: int
    informative_negatives: int
    safety_failed: bool


def adjudication_score(rows: Sequence[AdjudicationRowLike]) -> AdjudicationScore:
    """Score rows carrying fixture identity and terminal disposition."""
    false_positives = 0
    true_negatives = 0
    informative_positives = 0
    informative_negatives = 0
    safety_failed = False
    for row in rows:
        is_positive = bool(row.is_positive)
        disposition = row.disposition
        terminal = disposition in (Disposition.ACCEPTED, Disposition.REJECTED)
        if is_positive:
            if terminal:
                informative_positives += 1
        else:
            if disposition is Disposition.ACCEPTED:
                false_positives += 1
                informative_negatives += 1
                safety_failed = True
            elif disposition is Disposition.REJECTED:
                true_negatives += 1
                informative_negatives += 1
    denominator = false_positives + true_negatives
    fpr: float | None = (false_positives / denominator) if denominator else None
    return AdjudicationScore(
        false_positives=false_positives,
        true_negatives=true_negatives,
        fpr=fpr,
        informative_positives=informative_positives,
        informative_negatives=informative_negatives,
        safety_failed=safety_failed,
    )


@dataclass(frozen=True)
class QualityDecision:
    """Separated verdict and posture recommendation."""

    verdict: Verdict
    recommendation: Recommendation


def decide(
    *,
    single_f1: float | None,
    dual_f1: float | None,
    single_fpr: float | None,
    dual_fpr: float | None,
    canary_informative: int,
    canary_total: int,
    positive_informative: int,
    positive_total: int,
    negative_informative: int,
    negative_total: int,
    safety_failed: bool,
) -> QualityDecision:
    """Apply safety, informativeness gates, then the improvement threshold."""
    if safety_failed:
        return QualityDecision(Verdict.FAIL, Recommendation.SINGLE_OFF)
    if (
        canary_total < CANARY_TOTAL
        or canary_informative < CANARY_INFORMATIVE_BAR
        or positive_total < PAIR_TOTAL
        or positive_informative < PAIR_INFORMATIVE_BAR
        or negative_total < PAIR_TOTAL
        or negative_informative < PAIR_INFORMATIVE_BAR
    ):
        return QualityDecision(Verdict.INDETERMINATE, Recommendation.SINGLE_OFF)
    f1_gain = dual_f1 is not None and single_f1 is not None and (dual_f1 - single_f1) >= F1_DELTA_PP - 1e-9
    fpr_gain = (
        single_fpr is not None
        and dual_fpr is not None
        and single_fpr > 0
        and ((single_fpr - dual_fpr) / single_fpr) >= FPR_RELATIVE_DROP - 1e-9
    )
    if f1_gain or fpr_gain:
        return QualityDecision(Verdict.PASS, Recommendation.RETAIN_DUAL)
    return QualityDecision(Verdict.FAIL, Recommendation.RECOMMEND_SINGLE)
