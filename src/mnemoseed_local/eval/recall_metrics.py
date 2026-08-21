"""T4a recall metrics (PRD-B2.1-T4): 7 hand-computable scores over one
material point's T2-pipeline run.

Definitions are deliberately dumb and honest (hand-computable, never LLM
judgments):

- **Recall@k** = min(|served|, k) / candidate_pool — the focal cue hit rate:
  how much of the candidate pool the serve admitted, capped at k
  (None when the pool is empty);
- **Precision@k** = |served ∩ referenced| / |served| — of the injected
  chunks, the fraction the assistant genuinely referenced (None when nothing
  was served);
- **Floor-FP** = served_noise / noise_pool — the served-noise ratio, the
  primary floor-too-low signal (None when the noise pool is empty);
- **Detector-FP** = Σ|reinforced − referenced| / Σ|reinforced| over the reply
  observations — of the detector's reinforcements, the fraction that were not
  genuinely referenced (the needle-collision / hallucination path, TA-6);
- **FN rate** = Σ|referenced − reinforced| / Σ|referenced| over the reply
  observations — references the needle matcher missed (consumption misses);
- **Token overhead** = injected_chars / budget_chars — budget utilization;
- **Non-focal above floor** = the TA-4 probe count, reported verbatim.

Per-observation FN/Detector denominators keep every template's contribution
visible: a paraphrase that references the fact without firing any needle is
counted as a miss even when another template cited the same chunk.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The k values the recall/precision families report at (PRD T4a).
RECALL_KS: tuple[int, ...] = (1, 3, 5, 10)


@dataclass(frozen=True)
class ReplyObservation:
    """One simulated assistant reply turn's detector behavior."""

    template_name: str
    reinforced: tuple[str, ...]  # chunk ids the needle matcher reinforced
    referenced: tuple[str, ...]  # chunk ids the material declares genuinely cited


@dataclass(frozen=True)
class RecallRunResult:
    """One material point's full T2-pipeline evidence, read back from the rig."""

    point_id: str
    served: tuple[str, ...]  # the served (injected) chunk ids, serve order
    candidate_pool: int  # focal candidates the cue scan could have served
    noise_pool: int  # noise chunks that pass the focal filter (Floor-FP denominator)
    served_noise: int  # served chunks that are noise (Floor-FP numerator)
    observations: tuple[ReplyObservation, ...]  # one per simulated reply turn
    injected_chars: int  # the served selection's char cost (len+1 per item)
    budget_chars: int  # the daemon's effective item budget
    non_focal_above_floor: int  # TA-4 probe: decay-healthy non-focal count

    @property
    def reinforced(self) -> tuple[str, ...]:
        """First-seen union of the per-reply reinforcements (per-chunk-per-
        session dedupe, mirroring the hook's citedChunks)."""
        seen: set[str] = set()
        out: list[str] = []
        for observation in self.observations:
            for chunk_id in observation.reinforced:
                if chunk_id not in seen:
                    seen.add(chunk_id)
                    out.append(chunk_id)
        return tuple(out)

    @property
    def true_referenced(self) -> tuple[str, ...]:
        """First-seen union of the declared genuine references."""
        seen: set[str] = set()
        out: list[str] = []
        for observation in self.observations:
            for chunk_id in observation.referenced:
                if chunk_id not in seen:
                    seen.add(chunk_id)
                    out.append(chunk_id)
        return tuple(out)


@dataclass(frozen=True)
class RecallMetrics:
    """The 7 calibration metrics for one material point under one parameter set."""

    recall_at_k: tuple[float | None, ...]  # Recall@1,3,5,10
    precision_at_k: tuple[float | None, ...]  # Precision@1,3,5,10
    floor_fp: float | None
    detector_fp: float | None
    fn_rate: float | None
    token_overhead: float | None
    non_focal_above_floor: int


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    """Honest 0/0: unknown, never an invented 0.0 or 1.0."""
    return (numerator / denominator) if denominator else None


def score_recall(run: RecallRunResult) -> RecallMetrics:
    """Score one material point against its served/reinforced/referenced truth."""
    served = set(run.served)
    referenced = set(run.true_referenced)
    recall = tuple(
        (min(len(run.served), k) / run.candidate_pool) if run.candidate_pool else None for k in RECALL_KS
    )
    precision = tuple((len(served & referenced) / len(run.served)) if run.served else None for _ in RECALL_KS)
    false_reinforcements = sum(
        len(set(observation.reinforced) - set(observation.referenced)) for observation in run.observations
    )
    reinforcements = sum(len(set(observation.reinforced)) for observation in run.observations)
    missed_references = sum(
        len(set(observation.referenced) - set(observation.reinforced)) for observation in run.observations
    )
    references = sum(len(set(observation.referenced)) for observation in run.observations)
    return RecallMetrics(
        recall_at_k=recall,
        precision_at_k=precision,
        floor_fp=_safe_ratio(run.served_noise, run.noise_pool),
        detector_fp=_safe_ratio(false_reinforcements, reinforcements),
        fn_rate=_safe_ratio(missed_references, references),
        token_overhead=(run.injected_chars / run.budget_chars) if run.budget_chars else None,
        non_focal_above_floor=run.non_focal_above_floor,
    )
