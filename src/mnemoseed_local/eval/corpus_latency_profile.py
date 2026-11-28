"""Stage-level latency profile of the recall route over a copied corpus home.

The baseline answers how often a probe serves its source; this profile answers
where the wire latency goes. Per probe it times cue extraction, embedding, the
fused vector search, a dense-only search (the sparse-leg plus fusion cost) and
the full ``POST /memory/recall`` round trip directly on the live daemon state,
then decomposes the wire total into the measured stages and the residual that
carries assembly, budget and transport.

The probe population is the baseline's: the same seeded plan (seed 20261127,
stratified sampling) trimmed to two probes per family. Boot artifacts of an
earlier run are removed from the copy before the manifest check, the model
cache is staged read-only from the installed runtime, and the daemon boots on a
spare loopback port (never 7788, never the installed runtime).

Command line: ``python -m mnemoseed_local.eval.corpus_latency_profile --home
eval/corpus-baseline-home --out eval``.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
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
    _stage_models,
)
from mnemoseed_local.eval.recall_harness import _LOOPBACK_HOST, _spare_loopback_port
from mnemoseed_local.retrieve.cues import extract_cues
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.ports import ChunkFilter

# ---------------------------------------------------------------- constants

#: The sampling seed the baseline plan is built with.
_SEED = 20261127

#: Probe count: two per family, in the baseline's interleaved order.
_PROBES = 6

#: Measured repeats per probe and stage (the warmup pass is excluded).
_REPEATS = 5

#: Warmup passes per probe before the measured repeats.
_WARMUP_PASSES = 1

#: The vector search window both search stages ask for.
_VECTOR_TOP_K = 20

#: The timed stages, in route order (``wire`` is the full route itself).
_STAGES: tuple[str, ...] = ("cue_extract", "embed", "search_full", "search_dense", "wire")

#: The derived stages: the sparse-leg cost and the route residual.
_DERIVED: tuple[str, ...] = ("ann_fused_minus_dense", "route_overhead")

#: The additive stages of the decomposition (their means sum to the wire mean).
_ADDITIVE_STAGES: tuple[str, ...] = ("cue_extract", "embed", "search_full")

#: The boot artifacts an earlier daemon run leaves in the copy.
_BOOT_ARTIFACTS: tuple[str, ...] = (
    "secrets",
    "daemon.log",
    "daemon.err.log",
    "daemon.out.log",
    "eval",
    "daemon.coordination.lock",
)

#: The execution provider the embed driver hands to its InferenceSession.
_DRIVER_PROVIDERS: tuple[str, ...] = ("CPUExecutionProvider",)

_DISCLOSURES: tuple[str, ...] = (
    "/memory/recall reinforces the chunks it serves on the copy, so decay weights and "
    "last_reinforced stamps drift while the profile runs.",
    "stage samples are taken on the live in-process state; only the wire sample carries the "
    "HTTP round trip and its response parse.",
    "route_overhead is a residual (wire minus embed, fused search and cue extraction): it "
    "carries assembly, budget and transport, and turns negative when the route runs cheaper "
    "than the sum of its separately timed legs.",
    "search_dense and ann_fused_minus_dense are diagnostics: the route issues one fused "
    "search, so the dense-only call is an extra ANN pass over the same candidate window.",
    "the six-probe trim reuses the baseline's seeded source selection, but the per-probe "
    "query windows are drawn from a shorter random stream than the ninety-probe baseline, "
    "so an exact-family span can differ from the baseline run's span.",
    "the model cache is staged read-only from the installed runtime, and neither port 7788 "
    "nor the installed runtime is ever touched by the run.",
    "cold_start holds the first call of each stage in the run (the first probe's warmup "
    "pass) and is excluded from every stage statistic.",
)

# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class CueStage:
    """The cue extractor the profile times, plus the surface it came from."""

    surface: str
    extract: Callable[[str], Any]


@dataclass(frozen=True)
class ProbeProfile:
    """One probe's per-stage and derived latency samples."""

    probe_id: str
    family: str
    query: str
    stage_samples_ms: dict[str, tuple[float, ...]]
    derived_samples_ms: dict[str, tuple[float, ...]]


# ---------------------------------------------------------------- home


def _clean_boot_artifacts(home: Path) -> list[str]:
    """Remove an earlier run's boot artifacts from the copy so the manifest
    check sees the clean copy set. Only the named entries are touched, each
    one a direct child of ``home`` (printed before it is removed)."""
    if not home.is_dir():
        raise RuntimeError(f"corpus home {home} is not a directory")
    root = home.resolve()
    removed: list[str] = []
    for name in _BOOT_ARTIFACTS:
        target = home / name
        if not target.exists():
            continue
        resolved = target.resolve()
        if resolved.parent != root:
            raise RuntimeError(f"refusing to remove {resolved}: not a direct child of {root}")
        print(f"[clean] removing boot artifact {resolved}")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(name)
    return removed


# ---------------------------------------------------------------- cue surface


def _live_cue_extractor(state: Any) -> Callable[[str], Any] | None:
    """The state's own cue extractor (the recall route's ``extract``) when the
    daemon exposes one, else None for the module-level default."""
    for owner in (state, getattr(state, "memory", None)):
        if owner is None:
            continue
        for attribute in ("cues", "_cues", "cue_extractor"):
            extract = getattr(getattr(owner, attribute, None), "extract", None)
            if callable(extract):
                return cast(Callable[[str], Any], extract)
    return None


def _cue_stage(state: Any) -> CueStage:
    """The cue extraction the route performs, resolved from the live state
    first and from the shipped default extractor otherwise."""
    extractor = _live_cue_extractor(state)
    if extractor is not None:
        return CueStage(surface="daemon state cue extractor (.extract)", extract=extractor)
    return CueStage(surface="mnemoseed_local.retrieve.cues.extract_cues", extract=extract_cues)


# ---------------------------------------------------------------- instrumentation


def _timed(call: Callable[[], Any]) -> tuple[float, Any]:
    """Wall time of one call in milliseconds, plus its return value."""
    start = time.perf_counter()
    value = call()
    return (time.perf_counter() - start) * 1000.0, value


def _stage_timings(rig: SweepRig, query: str, cue: Callable[[str], Any]) -> dict[str, float]:
    """One pass over the recall stages, each timed in isolation on the live
    state (the searches consume the timed embed's own embedding)."""
    state = rig.state
    store_filter = ChunkFilter(profile_id=_PROFILE_ID)
    search = state.stores.vector.search
    timings: dict[str, float] = {}
    timings["cue_extract"], _ = _timed(lambda: cue(query))
    timings["embed"], embedding = _timed(lambda: state.stores.embed.embed(query))
    timings["search_full"], _ = _timed(
        lambda: search(embedding.dense, embedding.sparse, store_filter, _VECTOR_TOP_K)
    )
    timings["search_dense"], _ = _timed(lambda: search(embedding.dense, None, store_filter, _VECTOR_TOP_K))
    timings["wire"], _ = _timed(lambda: _recall(rig, query))
    return timings


def _derived_row(timings: Mapping[str, float]) -> dict[str, float]:
    """The derived costs of one pass: the sparse-leg plus fusion cost and the
    residual the measured stages leave of the wire total."""
    measured = timings["embed"] + timings["search_full"] + timings["cue_extract"]
    return {
        "ann_fused_minus_dense": timings["search_full"] - timings["search_dense"],
        "route_overhead": timings["wire"] - measured,
    }


def _profile_probe(
    rig: SweepRig,
    probe: Probe,
    cue: Callable[[str], Any],
    *,
    repeats: int,
    cold: dict[str, float],
) -> ProbeProfile:
    """One probe: the warmup passes over every stage, then the measured
    repeats (the run's first warmup pass fills ``cold``)."""
    stage_samples: dict[str, list[float]] = {stage: [] for stage in _STAGES}
    derived_samples: dict[str, list[float]] = {name: [] for name in _DERIVED}
    for index in range(_WARMUP_PASSES + repeats):
        timings = _stage_timings(rig, probe.query, cue)
        if index < _WARMUP_PASSES:
            for stage, value in timings.items():
                cold.setdefault(stage, value)
            continue
        for stage, value in timings.items():
            stage_samples[stage].append(value)
        for name, value in _derived_row(timings).items():
            derived_samples[name].append(value)
    return ProbeProfile(
        probe_id=probe.probe_id,
        family=probe.family,
        query=probe.query,
        stage_samples_ms={stage: tuple(values) for stage, values in stage_samples.items()},
        derived_samples_ms={name: tuple(values) for name, values in derived_samples.items()},
    )


# ---------------------------------------------------------------- runtime profile


def _runtime_profile(state: Any) -> dict[str, Any]:
    """The embedder's ONNX Runtime configuration, as the driver configures it
    and as the live session reports it."""
    from mnemoseed_local.storage.drivers import bge_m3_onnx as driver_module

    embed = getattr(state.stores, "embed", None)
    session = getattr(embed, "_session", None)
    get_providers = getattr(session, "get_providers", None)
    live_providers = list(get_providers()) if callable(get_providers) else None

    intra_op: int | None = None
    arena: bool | None = None
    options_factory = getattr(driver_module, "_session_options", None)
    if callable(options_factory):
        session_options = options_factory()
        intra_op = getattr(session_options, "intra_op_num_threads", None)
        arena = getattr(session_options, "enable_cpu_mem_arena", None)
    default_dimension = getattr(driver_module, "_DEFAULT_DIMENSION", None)
    default_max_length = getattr(driver_module, "_DEFAULT_MAX_LENGTH", None)

    return {
        "driver": getattr(getattr(embed, "info", None), "name", None),
        "session_loaded": session is not None,
        "live_session_providers": live_providers,
        "configured_providers": list(_DRIVER_PROVIDERS),
        "intra_op_num_threads": intra_op,
        "enable_cpu_mem_arena": arena,
        "dimension": getattr(embed, "dimension", default_dimension),
        "max_length": getattr(embed, "max_length", default_max_length),
    }


# ---------------------------------------------------------------- aggregation


def _stage_samples(profiles: Sequence[ProbeProfile], stage: str) -> list[float]:
    """Every measured sample of one stage, across probes."""
    return [sample for profile in profiles for sample in profile.stage_samples_ms[stage]]


def _derived_samples(profiles: Sequence[ProbeProfile], name: str) -> list[float]:
    """Every measured sample of one derived stage, across probes."""
    return [sample for profile in profiles for sample in profile.derived_samples_ms[name]]


def _decomposition(
    stages: Mapping[str, Mapping[str, Any]],
    derived: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The decomposition rows: the additive route stages first, then the
    diagnostic ones (a row's share is its mean over the wire mean)."""
    wire_mean = float(stages["wire"]["mean_ms"])

    def row(name: str, stats: Mapping[str, Any], kind: str) -> dict[str, Any]:
        share = stats["mean_ms"] / wire_mean if wire_mean else None
        return {
            "stage": name,
            "kind": kind,
            "mean_ms": stats["mean_ms"],
            "p50_ms": stats["p50_ms"],
            "p95_ms": stats["p95_ms"],
            "share_of_wire": share,
        }

    rows = [row(name, stages[name], "additive") for name in _ADDITIVE_STAGES]
    rows.append(row("route_overhead", derived["route_overhead"], "additive"))
    rows.append(row("search_dense", stages["search_dense"], "diagnostic"))
    rows.append(row("ann_fused_minus_dense", derived["ann_fused_minus_dense"], "diagnostic"))
    return rows


def _verdict(
    decomposition: Sequence[Mapping[str, Any]],
    stages: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The dominant stage of the wire total and the additive-sum check."""
    additive = [entry for entry in decomposition if entry["kind"] == "additive"]
    dominant = max(additive, key=lambda entry: float(entry["mean_ms"]))
    wire_mean = float(stages["wire"]["mean_ms"])
    additive_sum = sum(float(entry["mean_ms"]) for entry in additive)
    share = float(dominant["share_of_wire"] or 0.0)
    return {
        "dominant_stage": dominant["stage"],
        "dominant_mean_ms": dominant["mean_ms"],
        "dominant_share_of_wire": dominant["share_of_wire"],
        "wire_mean_ms": wire_mean,
        "wire_p50_ms": stages["wire"]["p50_ms"],
        "wire_p95_ms": stages["wire"]["p95_ms"],
        "additive_sum_ms": additive_sum,
        "additive_residual_ms": wire_mean - additive_sum,
        "statement": (
            f"{dominant['stage']} dominates the recall wire time: "
            f"{float(dominant['mean_ms']):.1f} ms of {wire_mean:.1f} ms ({share:.0%})"
        ),
    }


# ---------------------------------------------------------------- run


def run_profile(home: Path, *, seed: int = _SEED) -> dict[str, Any]:
    """One profile pass over a corpus-copy home: the seeded baseline plan, the
    per-stage instrumentation on the live daemon state and the decomposition."""
    _require_copy_home(home)
    cleaned = _clean_boot_artifacts(home)
    _require_manifest_root(home)
    staging = _stage_models(home)
    (home / "config.toml").write_text(
        _config_toml(home, f"http://{_LOOPBACK_HOST}:{_spare_loopback_port()}"),
        encoding="utf-8",
    )
    rng = random.Random(seed)
    cold: dict[str, float] = {}
    with _home_scope(home), SweepRig(home) as rig:
        state = rig.state
        store = cast(LanceDbEmbeddedStore, state.stores.vector)
        chunks = _page_all(store)
        epochs = [_chunk_epoch(chunk) for chunk in chunks]
        cuts = _recency_cuts(epochs)
        plan = _plan_probes(chunks, rng, cuts, probes=_PROBES)
        cue = _cue_stage(state)
        profiles = [
            _profile_probe(rig, probe, cue.extract, repeats=_REPEATS, cold=cold) for probe in plan.probes
        ]
        runtime = _runtime_profile(state)
    stages = {stage: _latency_stats(_stage_samples(profiles, stage)) for stage in _STAGES}
    derived = {name: _latency_stats(_derived_samples(profiles, name)) for name in _DERIVED}
    decomposition = _decomposition(stages, derived)
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ"),
        "home": str(home),
        "profile_id": _PROFILE_ID,
        "seed": seed,
        "probes_requested": _PROBES,
        "probes_run": len(profiles),
        "family_selected": plan.selected,
        "total_chunks": len(chunks),
        "repeats": _REPEATS,
        "warmup_passes": _WARMUP_PASSES,
        "vector_top_k": _VECTOR_TOP_K,
        "model_staging": staging,
        "boot_artifacts_removed": cleaned,
        "onnxruntime": runtime,
        "cue_surface": cue.surface,
        "cold_start": dict(cold),
        "stages": stages,
        "derived": derived,
        "decomposition": decomposition,
        "verdict": _verdict(decomposition, stages),
        "probes": [asdict(profile) for profile in profiles],
        "disclosures": list(_DISCLOSURES),
    }


# ---------------------------------------------------------------- report


def _stats_row(name: str, stats: Mapping[str, Any]) -> str:
    """One Markdown row of a stage statistics table."""
    return (
        f"| {name} | {stats['samples']} | {float(stats['mean_ms']):.1f}"
        f" | {float(stats['p50_ms']):.1f} | {float(stats['p95_ms']):.1f}"
        f" | {float(stats['max_ms']):.1f} |"
    )


def _render_markdown(record: Mapping[str, Any]) -> str:
    """The human-readable profile summary (JSON carries the full detail)."""
    lines: list[str] = []
    append = lines.append
    staging = record["model_staging"]
    runtime = record["onnxruntime"]
    verdict = record["verdict"]
    selected = record["family_selected"]
    removed = record["boot_artifacts_removed"]
    append("# Corpus recall latency profile record")
    append("")
    append(f"- generated: {record['generated_at']}")
    append(f"- home: {record['home']}")
    append(f"- profile: {record['profile_id']} (seed {record['seed']})")
    append(f"- probes: {record['probes_run']} of {record['probes_requested']} ({selected})")
    append(f"- corpus chunks: {record['total_chunks']}")
    append(f"- model staging: mode={staging['mode']} source={staging.get('source')}")
    append(f"- repeats per stage: {record['repeats']}")
    append(f"- warmup passes per probe: {record['warmup_passes']}")
    append(f"- vector top-k per search: {record['vector_top_k']}")
    append(f"- boot artifacts removed: {removed or 'none'}")
    append("")
    append("## ONNX Runtime")
    append("")
    append(f"- driver: {runtime['driver']}")
    append(f"- session loaded: {runtime['session_loaded']}")
    append(f"- live session providers: {runtime['live_session_providers']}")
    append(f"- configured providers: {runtime['configured_providers']}")
    append(f"- intra_op_num_threads: {runtime['intra_op_num_threads']}")
    append(f"- enable_cpu_mem_arena: {runtime['enable_cpu_mem_arena']}")
    append(f"- dimension: {runtime['dimension']}")
    append(f"- max_length: {runtime['max_length']}")
    append(f"- cue surface: {record['cue_surface']}")
    append("")
    append("## Cold start (first call per stage, excluded from the statistics)")
    append("")
    append("| stage | cold ms |")
    append("| --- | --- |")
    for stage, value in record["cold_start"].items():
        append(f"| {stage} | {float(value):.1f} |")
    append("")
    append("## Per-stage latency (warmup discarded)")
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
    append("| stage | kind | mean ms | share of wire | p50 ms | p95 ms |")
    append("| --- | --- | --- | --- | --- | --- |")
    for entry in record["decomposition"]:
        share_value = entry["share_of_wire"]
        share = "n/a" if share_value is None else f"{float(share_value) * 100:.1f}%"
        append(
            f"| {entry['stage']} | {entry['kind']} | {float(entry['mean_ms']):.1f}"
            f" | {share} | {float(entry['p50_ms']):.1f} | {float(entry['p95_ms']):.1f} |"
        )
    append("")
    append(f"- additive sum: {float(verdict['additive_sum_ms']):.1f} ms")
    append(f"- wire mean: {float(verdict['wire_mean_ms']):.1f} ms")
    append(f"- residual: {float(verdict['additive_residual_ms']):.1f} ms")
    append("")
    append("## Verdict")
    append("")
    append(f"- DOMINANT stage: **{verdict['dominant_stage']}**")
    append(f"- {verdict['statement']}")
    append(f"- wire mean: {float(verdict['wire_mean_ms']):.1f} ms")
    append(f"- wire p50: {float(verdict['wire_p50_ms']):.1f} ms")
    append(f"- wire p95: {float(verdict['wire_p95_ms']):.1f} ms")
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
    eval artifact convention: <UTC-timestamp>-corpus-latency-profile.{json,md})."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{record['generated_at']}-corpus-latency-profile"
    json_path = out_dir / f"{base}.json"
    md_path = out_dir / f"{base}.md"
    json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(record), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corpus recall latency profile (eval only)")
    parser.add_argument(
        "--home",
        type=Path,
        required=True,
        help="corpus-copy home (never the installed runtime)",
    )
    parser.add_argument("--out", type=Path, default=Path("eval"), help="report output dir")
    parser.add_argument("--seed", type=int, default=_SEED, help="sampling seed (recorded)")
    args = parser.parse_args(argv)
    record = run_profile(args.home, seed=args.seed)
    json_path, md_path = write_report(record, args.out)
    print(f"latency profile record: {json_path}")
    print(f"summary: {md_path}")
    print(f"chunks: {record['total_chunks']}")
    print(f"probes: {record['probes_run']} (seed {record['seed']})")
    for stage in _STAGES:
        stats = record["stages"][stage]
        mean = float(stats["mean_ms"])
        p50 = float(stats["p50_ms"])
        p95 = float(stats["p95_ms"])
        print(f"{stage}: mean={mean:.1f}ms p50={p50:.1f}ms p95={p95:.1f}ms")
    overhead = float(record["derived"]["route_overhead"]["mean_ms"])
    print(f"route overhead: mean={overhead:.1f}ms")
    print(f"verdict: {record['verdict']['statement']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
