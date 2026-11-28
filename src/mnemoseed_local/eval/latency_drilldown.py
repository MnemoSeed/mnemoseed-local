"""Stage drilldown of the residual inside ``MemoryService.recall``.

The stage profile times the recall wire against cue extraction, embedding and
the fused vector search; what those legs leave over is produced by the route's
own remaining stages. This drilldown times those stages directly on the live
daemon service over a copied corpus home: the wire round trip, the hybrid
retriever call, the assembler pass, the reinforcement write, the served-ledger
append and the residue scan — each in isolation, one round at a time, so the
residual decomposes into named stages instead of an unexplained remainder.

Measurement mutates the medium it measures: the retriever and assembler legs run
against the copy's real stores, and the reinforcement and served-ledger legs
write to them exactly as the route does. The copy is disposable; neither port
7788 nor the installed runtime is ever touched.

The probe population is the stage profile's: the same seeded stratified plan
(seed 20261127), trimmed to four probes. Boot artifacts of an earlier run are
removed from the copy before the manifest check, the model cache is staged
read-only from the installed runtime, and the daemon boots on a spare loopback
port.

Command line: ``python -m mnemoseed_local.eval.latency_drilldown --home
eval/corpus-baseline-home --out eval``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mnemoseed_local.eval.bridge_sweep import SweepRig, _config_toml, _latency_stats
from mnemoseed_local.eval.corpus_baseline import (
    _PROFILE_ID,
    Probe,
    _chunk_epoch,
    _home_scope,
    _page_all,
    _plan_probes,
    _recall,
    _recency_cuts,
    _require_copy_home,
    _require_manifest_root,
)
from mnemoseed_local.eval.corpus_latency_profile import (
    _clean_boot_artifacts,
    _stats_row,
    _timed,
)
from mnemoseed_local.eval.recall_harness import _LOOPBACK_HOST, _spare_loopback_port
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.ports import ConsumptionEvidenceKind

# ---------------------------------------------------------------- constants

#: The sampling seed the baseline plan is built with.
_SEED = 20261127

#: Probe count: the seeded plan trimmed to four probes.
_PROBES = 4

#: Measured rounds per probe (the warmup round is excluded).
_REPEATS = 3

#: Warmup rounds per probe before the measured rounds.
_WARMUP_PASSES = 1

#: The staged route calls, in route order (``wire`` is the full route itself).
_MEASURED_STAGES: tuple[str, ...] = (
    "retriever_only",
    "assemble_only",
    "record_hits_only",
    "record_served_only",
    "index_residue_only",
)

#: Every timed stage, the wire total first.
_STAGES: tuple[str, ...] = ("wire", *_MEASURED_STAGES)

#: The derived stages: the subtraction the measured stages leave of the wire.
_DERIVED: tuple[str, ...] = ("residual",)

#: The installed runtime's model cache, staged read-only into the copy.
_INSTALLED_MODEL_DIR = Path(".mnemoseed-local") / "models" / "bge-m3"

_DISCLOSURES: tuple[str, ...] = (
    "/memory/recall and the staged replica both reinforce what they serve and append served "
    "rows on the copy, so decay weights, last_reinforced stamps and the consumption ledger "
    "drift while the drilldown runs.",
    "the retriever and assembler legs run on the copy's real stores through the service's own "
    "collaborators; only the wire sample carries the HTTP round trip and the response parse.",
    "residual is a subtraction (wire minus the five staged calls): it carries the payload "
    "build, the serve-id mint, transport and request parsing, and turns negative when the "
    "staged calls run dearer than the route that contains them.",
    "wire is sampled first in every round, so a round's staged calls never perturb that "
    "round's wire sample; the reinforcement they apply reaches the next round's route.",
    "retriever_only carries the cue extraction its argument expression performs, because the "
    "route extracts cues inside the same stage boundary it hands to the retriever.",
    "the four-probe trim reuses the baseline's seeded source selection, but the per-probe "
    "query windows are drawn from a shorter random stream than the ninety-probe baseline, so "
    "a probe's query can differ from the baseline run's query for the same source.",
    "the model cache is staged read-only from the installed runtime, and neither port 7788 "
    "nor the installed runtime is ever touched by the run.",
    "cold_start holds the first round of each stage in the run (the first probe's warmup "
    "round) and is excluded from every stage statistic.",
)

# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class RecallStages:
    """The ``MemoryService`` collaborators one recall round drives.

    The eval code reaches the service's private seam by name; resolving the
    collaborators once keeps the measurement free of attribute lookups.
    """

    cues: Any
    retriever: Any
    assembler: Any
    record_hits: Any
    record_served: Any
    index_residue: Any
    next_serve_id: Any
    recall_view: Any
    stores: Any
    clock: Any


@dataclass(frozen=True)
class RoundSample:
    """One measured round: the per-stage samples plus the sizes they walked."""

    stage_ms: dict[str, float]
    derived_ms: dict[str, float]
    counts: dict[str, Any]


@dataclass(frozen=True)
class ProbeDrill:
    """One probe's per-stage and derived latency samples, over its rounds."""

    probe_id: str
    family: str
    query: str
    stage_samples_ms: dict[str, tuple[float, ...]]
    derived_samples_ms: dict[str, tuple[float, ...]]
    counts: tuple[dict[str, Any], ...]


# ---------------------------------------------------------------- service seam


def _model_source() -> Path | None:
    """The installed runtime's model cache when it exists, else None so the
    copy's own cache is used."""
    source = Path.home() / _INSTALLED_MODEL_DIR
    return source if source.is_dir() else None


def _recall_stages(service: Any) -> RecallStages:
    """Resolve the recall seam the drilldown times from the live service."""
    return RecallStages(
        cues=service._cues,
        retriever=service._retriever,
        assembler=service._assembler,
        record_hits=service._record_hits,
        record_served=service._record_served,
        index_residue=service._index_residue,
        next_serve_id=service._next_serve_id,
        recall_view=service._recall_view,
        stores=service._stores,
        clock=service._clock,
    )


# ---------------------------------------------------------------- route stages


def _retrieve(stages: RecallStages, query: str, profile_id: str) -> Any:
    """One retrieval stage exactly as the route issues it with no session:
    cue extraction, then the fused hybrid search over the recall view."""
    return stages.retriever.recall(
        query,
        stages.cues.extract(query),
        profile_id=profile_id,
        vector_store=stages.recall_view,
        graph_store=stages.stores.graph,
        embedder=stages.stores.embed,
        activation_snapshot=None,
        session_id=None,
        now=stages.clock(),
    )


def _assemble(stages: RecallStages, recall_result: Any, profile_id: str) -> Any:
    """One assembler pass over the retriever stage's own result."""
    return stages.assembler.assemble(
        recall_result,
        profile_id=profile_id,
        meta_store=stages.stores.meta,
        vector_store=stages.stores.vector,
        graph_store=stages.stores.graph,
    )


def _query_hash(query: str) -> str:
    """The served row's query digest, as the route computes it."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _evidence_kind(entry_kind: str) -> ConsumptionEvidenceKind:
    """The served row's evidence kind for one assembled entry kind."""
    if entry_kind == "chunk":
        return ConsumptionEvidenceKind.CHUNK
    return ConsumptionEvidenceKind.NODE


def _served_pairs(context: Any) -> list[tuple[ConsumptionEvidenceKind, str]]:
    """The serve-ledger rows for one assembled context, built as the route
    builds them."""
    pairs: list[tuple[ConsumptionEvidenceKind, str]] = []
    for entry in context.entries:
        pairs.append((_evidence_kind(entry.kind), entry.id))
    return pairs


def _record_served(
    stages: RecallStages,
    profile_id: str,
    serve_id: str,
    pairs: Sequence[tuple[ConsumptionEvidenceKind, str]],
    query: str,
) -> None:
    """One served-ledger append pass, with the route's own row shape."""
    stages.record_served(
        profile_id=profile_id,
        session_id=None,
        serve_id=serve_id,
        pairs=pairs,
        query_hash=_query_hash(query),
    )


def _round_counts(
    context: Any,
    pairs: Sequence[tuple[ConsumptionEvidenceKind, str]],
    residue: Any,
) -> dict[str, Any]:
    """The sizes each stage walked, recorded beside its sample."""
    entries = list(getattr(context, "entries", ()))
    rows = residue.get("rows") if isinstance(residue, Mapping) else None
    truncated = bool(residue.get("window_truncated")) if isinstance(residue, Mapping) else False
    return {
        "assembled_entries": len(entries),
        "chunk_entries": sum(1 for entry in entries if getattr(entry, "kind", None) == "chunk"),
        "node_entries": sum(1 for entry in entries if getattr(entry, "kind", None) == "graph"),
        "served_pairs": len(pairs),
        "residue_rows": len(rows) if isinstance(rows, list) else 0,
        "residue_window_truncated": truncated,
    }


def _measure_round(
    rig: SweepRig,
    stages: RecallStages,
    query: str,
    *,
    profile_id: str,
) -> RoundSample:
    """One round over the route: the wire total first, then each remaining
    stage timed in isolation (the retriever result and the assembled context
    are reused, never re-derived)."""
    stage_ms: dict[str, float] = {}
    elapsed, _ = _timed(lambda: _recall(rig, query))
    stage_ms["wire"] = elapsed
    elapsed, recall_result = _timed(lambda: _retrieve(stages, query, profile_id))
    stage_ms["retriever_only"] = elapsed
    elapsed, context = _timed(lambda: _assemble(stages, recall_result, profile_id))
    stage_ms["assemble_only"] = elapsed
    elapsed, _ = _timed(lambda: stages.record_hits(context))
    stage_ms["record_hits_only"] = elapsed
    pairs = _served_pairs(context)
    serve_id = stages.next_serve_id(profile_id, None)
    elapsed, _ = _timed(lambda: _record_served(stages, profile_id, serve_id, pairs, query))
    stage_ms["record_served_only"] = elapsed
    elapsed, residue = _timed(lambda: stages.index_residue(profile_id))
    stage_ms["index_residue_only"] = elapsed
    measured = sum(stage_ms[stage] for stage in _MEASURED_STAGES)
    return RoundSample(
        stage_ms=stage_ms,
        derived_ms={"residual": stage_ms["wire"] - measured},
        counts=_round_counts(context, pairs, residue),
    )


# ---------------------------------------------------------------- per-probe drill


def _drill_probe(
    rig: SweepRig,
    stages: RecallStages,
    probe: Probe,
    *,
    repeats: int,
    cold: dict[str, float],
) -> ProbeDrill:
    """One probe: the warmup round (which fills ``cold`` on the run's first
    probe), then the measured rounds."""
    stage_samples: dict[str, list[float]] = {stage: [] for stage in _STAGES}
    derived_samples: dict[str, list[float]] = {name: [] for name in _DERIVED}
    counts: list[dict[str, Any]] = []
    for index in range(_WARMUP_PASSES + repeats):
        sample = _measure_round(rig, stages, probe.query, profile_id=_PROFILE_ID)
        if index < _WARMUP_PASSES:
            for stage, value in sample.stage_ms.items():
                cold.setdefault(stage, value)
            for name, value in sample.derived_ms.items():
                cold.setdefault(name, value)
            continue
        for stage, value in sample.stage_ms.items():
            stage_samples[stage].append(value)
        for name, value in sample.derived_ms.items():
            derived_samples[name].append(value)
        counts.append(sample.counts)
    return ProbeDrill(
        probe_id=probe.probe_id,
        family=probe.family,
        query=probe.query,
        stage_samples_ms={stage: tuple(values) for stage, values in stage_samples.items()},
        derived_samples_ms={name: tuple(values) for name, values in derived_samples.items()},
        counts=tuple(counts),
    )


# ---------------------------------------------------------------- aggregation


def _stage_samples(drills: Sequence[ProbeDrill], stage: str) -> list[float]:
    """Every measured sample of one stage, across probes."""
    return [sample for drill in drills for sample in drill.stage_samples_ms[stage]]


def _derived_samples(drills: Sequence[ProbeDrill], name: str) -> list[float]:
    """Every measured sample of one derived stage, across probes."""
    return [sample for drill in drills for sample in drill.derived_samples_ms[name]]


def _decomposition(
    stages: Mapping[str, Mapping[str, Any]],
    derived: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The decomposition rows: the five staged calls, then the residual they
    leave (a row's share is its mean over the wire mean)."""
    wire_mean = float(stages["wire"]["mean_ms"])

    def row(name: str, stats: Mapping[str, Any]) -> dict[str, Any]:
        share = stats["mean_ms"] / wire_mean if wire_mean else None
        return {
            "stage": name,
            "mean_ms": stats["mean_ms"],
            "p50_ms": stats["p50_ms"],
            "p95_ms": stats["p95_ms"],
            "share_of_wire": share,
        }

    rows = [row(name, stages[name]) for name in _MEASURED_STAGES]
    rows.append(row(_DERIVED[0], derived[_DERIVED[0]]))
    return rows


def _verdict(
    decomposition: Sequence[Mapping[str, Any]],
    stages: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The dominant stage of the wire total, plus the sum check that the staged
    calls and the residual account for the wire mean."""
    dominant = max(decomposition, key=lambda entry: float(entry["mean_ms"]))
    wire_mean = float(stages["wire"]["mean_ms"])
    staged_sum = 0.0
    residual_mean = 0.0
    for entry in decomposition:
        value = float(entry["mean_ms"])
        if entry["stage"] in _MEASURED_STAGES:
            staged_sum += value
        else:
            residual_mean = value
    share = float(dominant["share_of_wire"] or 0.0)
    return {
        "dominant_stage": dominant["stage"],
        "dominant_mean_ms": dominant["mean_ms"],
        "dominant_share_of_wire": dominant["share_of_wire"],
        "wire_mean_ms": wire_mean,
        "wire_p50_ms": stages["wire"]["p50_ms"],
        "wire_p95_ms": stages["wire"]["p95_ms"],
        "staged_sum_ms": staged_sum,
        "residual_mean_ms": residual_mean,
        "accounted_ms": staged_sum + residual_mean,
        "statement": (
            f"{dominant['stage']} dominates the recall wire time: "
            f"{float(dominant['mean_ms']):.1f} ms of {wire_mean:.1f} ms ({share:.0%})"
        ),
    }


def _count_values(drills: Sequence[ProbeDrill], key: str) -> list[float]:
    """Every recorded value of one count, across the measured rounds."""
    return [float(count[key]) for drill in drills for count in drill.counts if key in count]


def _mean_count(drills: Sequence[ProbeDrill], key: str) -> float:
    """The mean of one recorded count across every measured round."""
    values = _count_values(drills, key)
    return statistics.mean(values) if values else 0.0


def _any_count(drills: Sequence[ProbeDrill], key: str) -> bool:
    """Whether any measured round reports the named flag."""
    return any(bool(count.get(key)) for drill in drills for count in drill.counts)


def _stage_notes(drills: Sequence[ProbeDrill]) -> dict[str, str]:
    """What each stage actually did per call, as its own samples' evidence."""
    entries = _mean_count(drills, "assembled_entries")
    chunks = _mean_count(drills, "chunk_entries")
    nodes = _mean_count(drills, "node_entries")
    pairs = _mean_count(drills, "served_pairs")
    rows = _mean_count(drills, "residue_rows")
    truncated = _any_count(drills, "residue_window_truncated")
    return {
        "wire": "the full POST /memory/recall round trip, sampled first in each round",
        "retriever_only": (
            "cue extraction plus the fused hybrid retrieval on the live service; "
            "session_id=None, so no activation snapshot and no activation refresh"
        ),
        "assemble_only": (
            f"Assembler.assemble over the retriever stage's own result: "
            f"{entries:.1f} assembled entries per call"
        ),
        "record_hits_only": (
            f"Reinforcer.record_hits: one chunk read per served chunk id plus one weight "
            f"batch; {chunks:.1f} chunk and {nodes:.1f} node ids per call"
        ),
        "record_served_only": (f"one served-ledger append per assembled item: {pairs:.1f} rows per call"),
        "index_residue_only": (
            f"the bounded newest-first residue scan: {rows:.1f} rows per call, window_truncated={truncated}"
        ),
        "residual": (
            "wire minus the five staged calls: payload build, serve-id mint, transport and request parsing"
        ),
    }


# ---------------------------------------------------------------- run


def run_drilldown(home: Path, *, seed: int = _SEED) -> dict[str, Any]:
    """One drilldown pass over a corpus-copy home: the seeded probe plan, the
    staged calls timed per round on the live service, and the decomposition of
    the wire total."""
    _require_copy_home(home)
    cleaned = _clean_boot_artifacts(home)
    _require_manifest_root(home)
    model_source = _model_source()
    (home / "config.toml").write_text(
        _config_toml(home, f"http://{_LOOPBACK_HOST}:{_spare_loopback_port()}"),
        encoding="utf-8",
    )
    rng = random.Random(seed)
    cold: dict[str, float] = {}
    with _home_scope(home), SweepRig(home, model_source=model_source) as rig:
        state = rig.state
        stages = _recall_stages(state.memory)
        store = cast(LanceDbEmbeddedStore, state.stores.vector)
        chunks = _page_all(store)
        epochs = [_chunk_epoch(chunk) for chunk in chunks]
        cuts = _recency_cuts(epochs)
        plan = _plan_probes(chunks, rng, cuts, probes=_PROBES)
        drills: list[ProbeDrill] = []
        for probe in plan.probes:
            drills.append(_drill_probe(rig, stages, probe, repeats=_REPEATS, cold=cold))
    stage_stats = {stage: _latency_stats(_stage_samples(drills, stage)) for stage in _STAGES}
    derived = {name: _latency_stats(_derived_samples(drills, name)) for name in _DERIVED}
    decomposition = _decomposition(stage_stats, derived)
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ"),
        "home": str(home),
        "profile_id": _PROFILE_ID,
        "seed": seed,
        "probes_requested": _PROBES,
        "probes_run": len(drills),
        "family_selected": plan.selected,
        "total_chunks": len(chunks),
        "repeats": _REPEATS,
        "warmup_passes": _WARMUP_PASSES,
        "model_source": str(model_source) if model_source is not None else None,
        "boot_artifacts_removed": cleaned,
        "cold_start": dict(cold),
        "stages": stage_stats,
        "derived": derived,
        "decomposition": decomposition,
        "stage_notes": _stage_notes(drills),
        "verdict": _verdict(decomposition, stage_stats),
        "probes": [asdict(drill) for drill in drills],
        "disclosures": list(_DISCLOSURES),
    }


# ---------------------------------------------------------------- report


def _render_markdown(record: Mapping[str, Any]) -> str:
    """The human-readable drilldown summary (JSON carries the full detail)."""
    lines: list[str] = []
    append = lines.append
    verdict = record["verdict"]
    removed = record["boot_artifacts_removed"]
    append("# Recall residual drilldown record")
    append("")
    append(f"- generated: {record['generated_at']}")
    append(f"- home: {record['home']}")
    append(f"- profile: {record['profile_id']} (seed {record['seed']})")
    probes = f"{record['probes_run']} of {record['probes_requested']}"
    append(f"- probes: {probes} ({record['family_selected']})")
    append(f"- corpus chunks: {record['total_chunks']}")
    append(f"- model cache source: {record['model_source'] or 'copy only'}")
    append(f"- measured rounds per probe: {record['repeats']}")
    append(f"- warmup rounds per probe: {record['warmup_passes']}")
    append(f"- boot artifacts removed: {removed or 'none'}")
    append("")
    append("## Cold start (first round per stage, excluded from the statistics)")
    append("")
    append("| stage | cold ms |")
    append("| --- | --- |")
    for stage, value in record["cold_start"].items():
        append(f"| {stage} | {float(value):.1f} |")
    append("")
    append("## Per-stage latency (warmup round discarded)")
    append("")
    append("| stage | samples | mean ms | p50 ms | p95 ms | max ms |")
    append("| --- | --- | --- | --- | --- | --- |")
    for stage in _STAGES:
        append(_stats_row(stage, record["stages"][stage]))
    append("")
    append("### Derived")
    append("")
    append("| derived stage | samples | mean ms | p50 ms | p95 ms | max ms |")
    append("| --- | --- | --- | --- | --- | --- |")
    for name in _DERIVED:
        append(_stats_row(name, record["derived"][name]))
    append("")
    append("## Decomposition of the wire recall")
    append("")
    append("| stage | mean ms | share of wire | p50 ms | p95 ms |")
    append("| --- | --- | --- | --- | --- |")
    for entry in record["decomposition"]:
        share_value = entry["share_of_wire"]
        share = "n/a" if share_value is None else f"{float(share_value) * 100:.1f}%"
        mean_ms = float(entry["mean_ms"])
        p50_ms = float(entry["p50_ms"])
        p95_ms = float(entry["p95_ms"])
        append(f"| {entry['stage']} | {mean_ms:.1f} | {share} | {p50_ms:.1f} | {p95_ms:.1f} |")
    append("")
    append("## Verdict")
    append("")
    append(f"- DOMINANT stage: **{verdict['dominant_stage']}**")
    append(f"- {verdict['statement']}")
    append(f"- wire mean: {float(verdict['wire_mean_ms']):.1f} ms")
    append(f"- wire p50: {float(verdict['wire_p50_ms']):.1f} ms")
    append(f"- wire p95: {float(verdict['wire_p95_ms']):.1f} ms")
    append(f"- staged calls: {float(verdict['staged_sum_ms']):.1f} ms")
    append(f"- residual: {float(verdict['residual_mean_ms']):.1f} ms")
    append("")
    append("## Stage notes")
    append("")
    for stage, note in record["stage_notes"].items():
        append(f"- **{stage}**: {note}")
    append("")
    append("## Per-probe stage means (ms)")
    append("")
    columns = (*_STAGES, *_DERIVED)
    append("| probe | family | " + " | ".join(columns) + " |")
    append("| --- | --- | " + " | ".join("---" for _ in columns) + " |")
    for probe in record["probes"]:
        cells: list[str] = []
        for name in columns:
            samples = probe["stage_samples_ms"].get(name)
            if not samples:
                samples = probe["derived_samples_ms"].get(name)
            cells.append(f"{statistics.mean(samples):.1f}" if samples else "n/a")
        append(f"| {probe['probe_id']} | {probe['family']} | " + " | ".join(cells) + " |")
    append("")
    append("## Disclosures")
    append("")
    for disclosure in record["disclosures"]:
        append(f"- {disclosure}")
    append("")
    return "\n".join(lines)


def write_report(record: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """Write the JSON record and the Markdown summary (timestamped names, the
    eval artifact convention: <UTC-timestamp>-latency-drilldown.{json,md})."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{record['generated_at']}-latency-drilldown"
    json_path = out_dir / f"{base}.json"
    md_path = out_dir / f"{base}.md"
    json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(record), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recall residual drilldown (eval only)")
    parser.add_argument(
        "--home",
        type=Path,
        required=True,
        help="corpus-copy home (never the installed runtime)",
    )
    parser.add_argument("--out", type=Path, default=Path("eval"), help="report output dir")
    parser.add_argument("--seed", type=int, default=_SEED, help="sampling seed (recorded)")
    args = parser.parse_args(argv)
    record = run_drilldown(args.home, seed=args.seed)
    json_path, md_path = write_report(record, args.out)
    print(f"drilldown record: {json_path}")
    print(f"summary: {md_path}")
    print(f"chunks: {record['total_chunks']}")
    print(f"probes: {record['probes_run']} (seed {record['seed']})")
    for stage in _STAGES:
        stats = record["stages"][stage]
        mean_ms = float(stats["mean_ms"])
        p50_ms = float(stats["p50_ms"])
        p95_ms = float(stats["p95_ms"])
        print(f"{stage}: mean={mean_ms:.1f}ms p50={p50_ms:.1f}ms p95={p95_ms:.1f}ms")
    residual = float(record["derived"][_DERIVED[0]]["mean_ms"])
    print(f"residual: mean={residual:.1f}ms")
    print(f"verdict: {record['verdict']['statement']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
