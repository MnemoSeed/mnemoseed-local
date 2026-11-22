"""Contract metrics computed only from product recall observations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from mnemoseed_local.eval.realcorpus_recall.replay import RecallObservation


@dataclass(frozen=True)
class QuerySpec:
    qid: str
    text: str
    lang: str
    kind: str
    target_chunk_ids: list[str]
    target_content_sha256: str
    ingestion_verified: bool
    rationale: str
    author: str
    date: str
    holdout: bool
    seen: bool
    frozen_sha256: str


@dataclass(frozen=True)
class QueryMetrics:
    qid: str
    kind: str
    holdout: bool
    seen: bool
    ingestion_verified: bool
    hit: bool
    rank: int | None
    honest_empty_ok: bool
    noise_count: int
    served_count: int
    tokens_used: int
    dropped_count: int


@dataclass(frozen=True)
class RunMetrics:
    run_index: int
    baseline_sha256: str
    per_query: tuple[QueryMetrics, ...]
    observations: Mapping[str, RecallObservation] = field(default_factory=dict)
    recall_at_8: float | None = None
    miss_seed_hit: bool | None = None
    honest_empty_fidelity: float | None = None
    noise_admission: float = 0.0
    ranks: tuple[int | None, ...] = ()
    tokens_used: tuple[int, ...] = ()


def score_query(
    observation: RecallObservation,
    query: QuerySpec,
    all_target_ids: frozenset[str],
) -> QueryMetrics:
    served = observation.served_ids
    targets = frozenset(query.target_chunk_ids)
    hit = bool(targets & set(served))
    rank = next(
        (index for index, chunk_id in enumerate(served, start=1) if chunk_id in targets),
        None,
    )
    return QueryMetrics(
        qid=query.qid,
        kind=query.kind,
        holdout=query.holdout,
        seen=query.seen,
        ingestion_verified=query.ingestion_verified,
        hit=hit,
        rank=rank,
        honest_empty_ok=(not served) if query.kind == "honest_empty" else False,
        noise_count=sum(chunk_id not in all_target_ids for chunk_id in served),
        served_count=len(served),
        tokens_used=observation.tokens_used,
        dropped_count=observation.dropped_count,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def score_run(
    observations: Mapping[str, RecallObservation],
    queries: Sequence[QuerySpec],
    *,
    run_index: int,
    baseline_sha256: str,
) -> RunMetrics:
    all_target_ids = frozenset(target for query in queries for target in query.target_chunk_ids)
    per_query = tuple(score_query(observations[q.qid], q, all_target_ids) for q in queries)
    positive = [
        metric
        for metric in per_query
        if metric.kind in ("positive", "miss_seed") and metric.ingestion_verified
    ]
    miss_seeds = [metric for metric in per_query if metric.kind == "miss_seed"]
    honest_empty = [metric for metric in per_query if metric.kind == "honest_empty"]
    served_total = sum(metric.served_count for metric in per_query)
    noise_total = sum(metric.noise_count for metric in per_query)
    return RunMetrics(
        run_index=run_index,
        baseline_sha256=baseline_sha256,
        per_query=per_query,
        observations=dict(observations),
        recall_at_8=_ratio(sum(metric.hit for metric in positive), len(positive)),
        miss_seed_hit=all(metric.hit for metric in miss_seeds) if miss_seeds else None,
        honest_empty_fidelity=_ratio(
            sum(metric.honest_empty_ok for metric in honest_empty), len(honest_empty)
        ),
        noise_admission=noise_total / served_total if served_total else 0.0,
        ranks=tuple(metric.rank for metric in per_query),
        tokens_used=tuple(metric.tokens_used for metric in per_query),
    )


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
