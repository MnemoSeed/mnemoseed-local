"""Quality contract judgement: ratified bars, proposals, and anti-gaming."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mnemoseed_local.eval.realcorpus_recall.manifest import InvalidRun
from mnemoseed_local.eval.realcorpus_recall.metrics import QuerySpec, RunMetrics, median, score_run
from mnemoseed_local.eval.realcorpus_recall.replay import ProductRecallReplay, RecallObservation

RATIFIED_IDS = frozenset({"B1", "B3"})
UNRATIFIED_IDS = frozenset({"B2", "B4", "DISP", "N", "P95"})


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Thresholds:
    b2_recall_at_8: float = 0.80
    b4_noise_max: float = 0.20
    dispersion_bound: float = 0.10
    n_runs: int = 3
    p95_ratio_max: float = 1.5
    budget_tokens: int = 800

    @property
    def ratified_ids(self) -> frozenset[str]:
        return RATIFIED_IDS

    @property
    def unratified_ids(self) -> frozenset[str]:
        return UNRATIFIED_IDS


@dataclass(frozen=True)
class BarResult:
    id: str
    ratified: bool
    verdict: Verdict
    detail: str


@dataclass(frozen=True)
class ContractResult:
    verdict: Verdict
    bars: Mapping[str, BarResult]
    diagnostics: tuple[str, ...] = ()
    b2_denominator: int = 0
    dispersion: float = 0.0
    invalid_runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "bars": {
                bar_id: {
                    "ratified": bar.ratified,
                    "verdict": bar.verdict.value,
                    "detail": bar.detail,
                }
                for bar_id, bar in self.bars.items()
            },
            "diagnostics": list(self.diagnostics),
            "b2_denominator": self.b2_denominator,
            "dispersion": self.dispersion,
            "invalid_runs": self.invalid_runs,
        }


def _observations_of(run: Any) -> Mapping[str, RecallObservation]:
    if isinstance(run, Mapping):
        observations: Mapping[str, RecallObservation] = run
        return observations
    from_run: Mapping[str, RecallObservation] = run.observations
    return from_run


def _bar(bar_id: str, verdict: Verdict, detail: str) -> BarResult:
    return BarResult(
        id=bar_id,
        ratified=bar_id in RATIFIED_IDS,
        verdict=verdict,
        detail=detail,
    )


def judge(
    runs: Sequence[Any],
    queries: Sequence[QuerySpec],
    thresholds: Thresholds,
    *,
    invalid_runs: int = 0,
) -> ContractResult:
    run_metrics = [
        score_run(
            _observations_of(run),
            queries,
            run_index=index,
            baseline_sha256=getattr(run, "baseline_sha256", ""),
        )
        for index, run in enumerate(runs)
    ]
    diagnostics = tuple(query.qid for query in queries if not query.ingestion_verified)
    holdout = [query for query in queries if query.holdout]
    b2_queries = [
        query for query in holdout if query.ingestion_verified and query.kind in ("positive", "miss_seed")
    ]
    b2_denominator = len(b2_queries)
    b2_values = [metric.recall_at_8 for metric in run_metrics if metric.recall_at_8 is not None]
    dispersion = max(b2_values) - min(b2_values) if len(b2_values) > 1 else 0.0
    frozen = invalid_runs > 0 or dispersion > thresholds.dispersion_bound

    def gate(bar_id: str, verdict: Verdict, detail: str) -> BarResult:
        return _bar(bar_id, Verdict.UNKNOWN if frozen else verdict, detail)

    b2_median = median(b2_values)
    if b2_denominator == 0:
        b2 = gate("B2", Verdict.UNKNOWN, "no holdout verified positives")
    elif b2_median is None:
        b2 = gate("B2", Verdict.UNKNOWN, "no scored runs")
    else:
        b2 = gate(
            "B2",
            Verdict.PASS if b2_median >= thresholds.b2_recall_at_8 else Verdict.FAIL,
            f"median={b2_median:.4f} threshold={thresholds.b2_recall_at_8:.4f} (unratified)",
        )

    miss_seed_queries = [query for query in holdout if query.kind == "miss_seed"]
    miss_hits = [metric.miss_seed_hit for metric in run_metrics if metric.miss_seed_hit is not None]
    if not miss_seed_queries or not miss_hits:
        b1 = gate("B1", Verdict.UNKNOWN, "no holdout miss seed")
    else:
        b1 = gate(
            "B1",
            Verdict.PASS if all(miss_hits) else Verdict.FAIL,
            f"{sum(bool(hit) for hit in miss_hits)}/{len(miss_hits)}",
        )

    honest_queries = [query for query in holdout if query.kind == "honest_empty"]
    honesty = [
        metric.honest_empty_fidelity for metric in run_metrics if metric.honest_empty_fidelity is not None
    ]
    if not honest_queries or not honesty:
        b3 = gate("B3", Verdict.UNKNOWN, "no holdout honest-empty probes")
    else:
        b3 = gate(
            "B3",
            Verdict.PASS if all(value == 1.0 for value in honesty) else Verdict.FAIL,
            f"fidelity={min(honesty):.4f}",
        )

    noise_median = median([metric.noise_admission for metric in run_metrics])
    b4 = _bar(
        "B4",
        Verdict.UNKNOWN
        if noise_median is None
        else (Verdict.PASS if noise_median <= thresholds.b4_noise_max else Verdict.FAIL),
        "diagnostic guard (unratified)",
    )
    p95 = _bar("P95", Verdict.UNKNOWN, "guard only; no baseline supplied (unratified)")
    bars = {"B1": b1, "B2": b2, "B3": b3, "B4": b4, "P95": p95}
    gates = [b1, b2, b3]
    if frozen:
        verdict = Verdict.UNKNOWN
    elif any(bar.verdict is Verdict.UNKNOWN for bar in gates):
        verdict = Verdict.UNKNOWN
    elif all(bar.verdict is Verdict.PASS for bar in gates):
        verdict = Verdict.PASS
    else:
        verdict = Verdict.FAIL
    return ContractResult(
        verdict=verdict,
        bars=bars,
        diagnostics=diagnostics,
        b2_denominator=b2_denominator,
        dispersion=dispersion,
        invalid_runs=invalid_runs,
    )


_DRIFT_FIELDS = (
    "recall_at_8",
    "miss_seed_hit",
    "honest_empty_fidelity",
    "ranks",
    "tokens_used",
    "baseline_sha256",
)


def check_no_drift(runs: Sequence[RunMetrics]) -> None:
    if not runs:
        return
    for name in _DRIFT_FIELDS:
        values = [getattr(run, name) for run in runs]
        if any(value != values[0] for value in values[1:]):
            raise InvalidRun(f"cross-run drift in {name}")


def replay_cold_starts(
    replay: ProductRecallReplay,
    queries: Sequence[QuerySpec],
    *,
    restore: Callable[[], str],
    n_runs: int,
    profile_id: str,
) -> list[RunMetrics]:
    runs: list[RunMetrics] = []
    for index in range(n_runs):
        baseline = restore()
        observations = {
            query.qid: replay.replay(query.text, qid=query.qid, profile_id=profile_id) for query in queries
        }
        runs.append(score_run(observations, queries, run_index=index, baseline_sha256=baseline))
    return runs
