"""PRD-448 W1 bridge calibration harness: the real-daemon rig that measures
the recall surface the cue-bridge design targets (brief §4.1, §6 W1).

The rig boots the REAL daemon (``daemon.app.create_app``) in a fail-loud
fresh root (``rig_freshness``), a temp ``MNEMOSEED_LOCAL_HOME`` and a spare
loopback port, seeds the byte-pinned corpus through the real ingest funnel
(``POST /ingest`` -> ``POST /session/end``), and per fixed probe measures:

- per-candidate dense / sparse / fused similarity over the whole pool (the
  store fuses ``0.5*dense + 0.5*sparse`` on the dense ANN candidate window,
  so a ``sparse=None`` search returns the dense-only scores for the SAME
  candidates and ``sparse = 2*fused - dense`` recovers the sparse component
  exactly);
- the store-similarity floor decision per candidate (the D1 inputs);
- the wire recall response — served top-k with scores, budget metrics and
  coverage — the RED baseline on current main;
- the gated-max branch per candidate (zero-sparse / dense / fused);
- recall-route latency (warmup + timed repeats, p95).

Model-cache contract for the real embedded preset: the rig stages the BGE-M3
cache into its OWN model dir — copied from the installed runtime (read-only:
files are copied, never moved or modified) when cached there, otherwise left
for the driver to download into the rig root.

Command line: ``python -m mnemoseed_local.eval.bridge_harness --root <fresh
dir> [--out eval] [--model-source <dir>] [--latency-repeats N]``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from mnemoseed_local.eval.bridge_materials import bridge_corpus, bridge_probes
from mnemoseed_local.eval.recall_harness import (
    _LOOPBACK_HOST,
    _point_config,
    _restore_home_env,
    _spare_loopback_port,
    release_daemon_log_handler,
    rig_client,
)
from mnemoseed_local.eval.rig_freshness import require_fresh_root
from mnemoseed_local.retrieve.cues import extract_cues
from mnemoseed_local.retrieve.hybrid import HybridConfig
from mnemoseed_local.schema.stamp import ChunkStamp
from mnemoseed_local.schema.turn import HostId
from mnemoseed_local.storage.ports import ChunkFilter, Page

#: Per-probe measurement search width: the whole pool must fit inside the
#: driver's dense-ANN candidate window (max(top_k*4, top_k+50)).
_MEASUREMENT_TOP_K = 100

#: The installed runtime's model cache (READ-ONLY source for staging).
INSTALLED_MODEL_DIR = Path.home() / ".mnemoseed-local" / "models" / "bge-m3"

_MODEL_ARTIFACTS: tuple[str, ...] = ("model_quantized.onnx", "tokenizer.json", "sparse_linear.pt")

_BRIEF_FREEZE = "1aaf929aa7a2650d349224ad778590aa18d89ddba5eac79d94f436ffffd41e1d"


# ---------------------------------------------------------------- staging


@dataclass(frozen=True)
class StagedFile:
    """One staged model artifact with its size and hash."""

    name: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ModelStaging:
    """Which staging path the rig took for the embedder's model cache."""

    mode: str  # "copied" | "download" | "not-needed"
    source: str | None
    files: tuple[StagedFile, ...]


def stage_model_cache(target: Path, *, source: Path | None = None) -> ModelStaging:
    """Stage the BGE-M3 cache into ``target``: copy every artifact (plus its
    ``.complete`` sidecar) from ``source`` when the source carries a complete
    cache, otherwise leave the dir empty so the driver downloads into the rig
    root on first embed. The source is only ever read."""
    target.mkdir(parents=True, exist_ok=True)
    if source is not None and all((source / name).is_file() for name in _MODEL_ARTIFACTS):
        staged: list[StagedFile] = []
        for name in _MODEL_ARTIFACTS:
            artifact = source / name
            shutil.copyfile(artifact, target / name)
            sidecar = artifact.with_name(name + ".complete")
            if sidecar.is_file():
                shutil.copyfile(sidecar, target / (name + ".complete"))
            data = (target / name).read_bytes()
            staged.append(StagedFile(name=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
        return ModelStaging(mode="copied", source=str(source), files=tuple(staged))
    return ModelStaging(mode="download", source=None, files=())


# ---------------------------------------------------------------- cues


def cue_entities(text: str) -> tuple[str, ...]:
    """The cue entities the recall path derives for a text (the daemon's
    default extractor)."""
    return tuple(extract_cues(text).cues.entities)


# ---------------------------------------------------------------- rig


def _latency_stats(latencies_ms: list[float]) -> dict[str, Any]:
    ordered = sorted(latencies_ms)
    if not ordered:
        return {"samples": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return ordered[index]

    return {
        "samples": len(ordered),
        "mean_ms": sum(ordered) / len(ordered),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": ordered[-1],
    }


def _config_toml(
    root: Path,
    base_url: str,
    *,
    vector_dimensions: int,
    embed_driver: str,
    embed_dimension: int,
    model_dir: Path,
) -> str:
    stores = root / "stores"
    if embed_driver == "synthetic":
        embed_section = f'[storage.embed]\ndriver = "{embed_driver}"\ndimension = {embed_dimension}\n'
    else:
        embed_section = f'[storage.embed]\ndriver = "{embed_driver}"\nmodel_dir = "{model_dir.as_posix()}"\n'
    vector_uri = (stores / "chunks.lance").as_posix()
    return (
        'preset = "embedded"\n'
        f'baseurl = "{base_url}"\n'
        f'[storage.vector]\nuri = "{vector_uri}"\ndimensions = {vector_dimensions}\n'
        f'[storage.graph]\npath = "{(stores / "cortex.db").as_posix()}"\n'
        "[storage.graph.instances.isolated]\n"
        'driver = "sqlite_graph"\n'
        f'path = "{(stores / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(stores / "meta.db").as_posix()}"\n'
        f"{embed_section}"
        "[dream]\n"
        "auto_trigger = false\n"
        "floor_pool_points = 1000000\n"
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
    )


@dataclass(frozen=True)
class CandidateMeasurement:
    """One pool chunk's similarity decomposition for one probe.

    Under the gated-max fusion the sparse component is score-recoverable only
    on the branches where the returned score is the plain fusion: an
    exactly-zero sparse leg (``zero_sparse``, score = 0.5*dense) and a sparse
    leg above the dense one (``fused``, score = 0.5*dense + 0.5*sparse). On
    the ``dense`` branch (0 < sparse <= dense) the gate returned the dense
    score itself and the sparse value is not recoverable from scores — it is
    recorded as ``None``.
    """

    chunk_id: str
    fused_rank: int
    dense_similarity: float
    fused_similarity: float
    sparse_similarity: float | None
    gate_branch: str
    above_floor: bool


@dataclass(frozen=True)
class ProbeMeasurement:
    """All pool candidates for one probe, in fused-similarity order."""

    candidates: tuple[CandidateMeasurement, ...]


class BridgeRig:
    """One bridge-calibration rig over a disposable real daemon app.

    Materialization is fail-loud (fresh root only); the daemon world — config,
    stores, model cache, journal, daemon.log — lives entirely under the rig
    root with a spare loopback port identity and a temp MNEMOSEED_LOCAL_HOME
    for the boot window.
    """

    def __init__(
        self,
        root: Path,
        *,
        embed_driver: str = "bge_m3_onnx",
        embed_dimension: int = 1024,
        model_source: Path | None = None,
        profile_id: str = "bridge-eval",
    ) -> None:
        require_fresh_root(root)
        self.root = root
        self.embed_driver = embed_driver
        self.embed_dimension = embed_dimension
        self.model_source = model_source
        self.profile_id = profile_id
        self.base_url = f"http://{_LOOPBACK_HOST}:{_spare_loopback_port()}"
        self.model_staging: ModelStaging | None = None
        (root / "config.toml").write_text(
            _config_toml(
                root,
                self.base_url,
                vector_dimensions=embed_dimension,
                embed_driver=embed_driver,
                embed_dimension=embed_dimension,
                model_dir=root / "models" / "bge-m3",
            ),
            encoding="utf-8",
        )
        self._stack: ExitStack | None = None
        self._client: TestClient | None = None

    @property
    def client(self) -> TestClient:
        assert self._client is not None, "BridgeRig must be entered: with BridgeRig(...) as rig"
        return self._client

    @property
    def _state(self) -> Any:
        """The daemon app's live state (stores/memory/config), typed Any —
        fastapi's TestClient stub types ``app`` as a callable."""
        return cast(Any, self.client).app.state

    def __enter__(self) -> BridgeRig:
        stack = ExitStack()
        try:
            previous_home = os.environ.get("MNEMOSEED_LOCAL_HOME")
            os.environ["MNEMOSEED_LOCAL_HOME"] = str(self.root)
            stack.callback(_restore_home_env, previous_home)
            stack.enter_context(_point_config(self.root, self.root / "config.toml"))
            if self.embed_driver != "synthetic":
                self.model_staging = stage_model_cache(
                    self.root / "models" / "bge-m3", source=self._resolved_model_source()
                )
            self._client = stack.enter_context(rig_client())
        except BaseException:
            stack.close()
            self._release_daemon_log_handler()
            raise
        self._stack = stack
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        assert self._stack is not None
        self._stack.__exit__(exc_type, exc, tb)
        self._stack = None
        self._client = None
        self._release_daemon_log_handler()

    def _release_daemon_log_handler(self) -> None:
        release_daemon_log_handler(self.root)

    def _resolved_model_source(self) -> Path | None:
        if self.model_source is not None:
            return self.model_source
        return INSTALLED_MODEL_DIR if INSTALLED_MODEL_DIR.is_dir() else None

    # ------------------------------------------------------------ funnel

    def seed_corpus(self) -> dict[str, str]:
        """Seed the byte-pinned corpus through the real ingest funnel (one
        session per chunk, settled) and resolve label -> stored chunk id."""
        ts = 1_700_000_000.0
        for index, chunk in enumerate(bridge_corpus()):
            session_id = f"bridge-a{index:02d}"
            self._ingest(session_id, chunk.text, ts + index)
            self._settle(session_id)
        by_session = {chunk.provenance.session_id: chunk.chunk_id for chunk in self.stored_chunks()}
        resolved: dict[str, str] = {}
        for index, chunk in enumerate(bridge_corpus()):
            chunk_id = by_session.get(f"bridge-a{index:02d}")
            if chunk_id is None:
                raise KeyError(f"corpus chunk {chunk.label} resolved to no stored chunk")
            resolved[chunk.label] = chunk_id
        return resolved

    def stored_chunks(self) -> list[ChunkStamp]:
        page = self._state.stores.vector.list_chunks(
            ChunkFilter(profile_id=self.profile_id), Page(offset=0, limit=100)
        )
        return list(page.items)

    def recall(self, query: str) -> dict[str, Any]:
        response = self.client.post(
            "/memory/recall",
            json={"profile_id": self.profile_id, "query": query},
        )
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())

    def measure_probe(self, text: str, *, top_k: int = _MEASUREMENT_TOP_K) -> ProbeMeasurement:
        """Per-candidate similarity decomposition over the whole pool, with
        the exact store filter the vector track passes (the pool sits far
        below the dense-ANN candidate window, so nothing is prefiltered)."""
        config = HybridConfig()
        embedding = self._state.stores.embed.embed(text)
        store_filter = ChunkFilter(
            profile_id=self.profile_id,
            min_decay=config.min_decay,
            pin_min_decay=config.rescue_min_decay,
            consolidated=False,
        )
        fused_hits = self._state.stores.vector.search(embedding.dense, embedding.sparse, store_filter, top_k)
        dense_hits = self._state.stores.vector.search(embedding.dense, None, store_filter, top_k)
        dense_by_id = {hit.chunk.chunk_id: hit.similarity for hit in dense_hits}

        def decompose(gated: float, dense: float) -> tuple[float | None, str]:
            if gated < dense:
                return 0.0, "zero_sparse"
            if gated == dense:
                return None, "dense"
            return 2.0 * gated - dense, "fused"

        candidates: list[CandidateMeasurement] = []
        for index, hit in enumerate(fused_hits):
            dense = dense_by_id[hit.chunk.chunk_id]
            sparse, branch = decompose(hit.similarity, dense)
            candidates.append(
                CandidateMeasurement(
                    chunk_id=hit.chunk.chunk_id,
                    fused_rank=index + 1,
                    dense_similarity=dense,
                    fused_similarity=hit.similarity,
                    sparse_similarity=sparse,
                    gate_branch=branch,
                    above_floor=hit.similarity >= config.min_similarity,
                )
            )
        return ProbeMeasurement(candidates=tuple(candidates))

    # ------------------------------------------------------------ internals

    def _ingest(self, session_id: str, text: str, ts: float) -> None:
        response = self.client.post(
            "/ingest",
            json={
                "host": HostId.GENERIC.value,
                "event": "user_prompt",
                "session_id": session_id,
                "profile_id": self.profile_id,
                "ts": ts,
                "content": {"text": text},
            },
        )
        assert response.status_code == 202, response.text

    def _settle(self, session_id: str) -> None:
        response = self.client.post(
            "/session/end", json={"session_id": session_id, "profile_id": self.profile_id}
        )
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------- record


@dataclass(frozen=True)
class StoredCueAssertion:
    """The §4.1 oracle derivation for one seeded chunk."""

    label: str
    chunk_id: str
    stored_entities: tuple[str, ...]
    expected_entities: tuple[str, ...]
    ok: bool


@dataclass(frozen=True)
class ServedEntry:
    """One served wire entry (the RED baseline ranking)."""

    rank: int
    kind: str
    id: str
    label: str
    score: float
    text: str


@dataclass(frozen=True)
class CandidateRecord:
    """One pool candidate's measured similarities for one probe."""

    label: str
    role: str
    chunk_id: str
    fused_rank: int
    dense_similarity: float
    fused_similarity: float
    sparse_similarity: float | None
    gate_branch: str
    above_floor: bool


@dataclass(frozen=True)
class ProbeRecord:
    """One probe's full measurement (baseline + similarity decomposition +
    budget metrics + latency)."""

    label: str
    text: str
    cue_entities: tuple[str, ...]
    candidates: tuple[CandidateRecord, ...]
    served: tuple[ServedEntry, ...]
    served_ids: tuple[str, ...]
    target_served: bool
    target_served_rank: int | None
    target_fused: float | None
    target_dense: float | None
    target_sparse: float | None
    target_above_floor: bool
    target_floor_rank: int | None
    dropped_count: int
    budget_tokens: int
    tokens_used: int
    coverage: dict[str, Any]
    latency: dict[str, Any]
    latency_samples_ms: tuple[float, ...]


@dataclass(frozen=True)
class CalibrationRecord:
    """The bridge calibration record (JSON + markdown artifact)."""

    generated_at: str
    brief_freeze: str
    embed_driver: str
    model_staging: dict[str, Any]
    stored_cue_assertions: tuple[StoredCueAssertion, ...]
    stored_cue_assertions_ok: bool
    probes: tuple[ProbeRecord, ...]
    aggregate_latency: dict[str, Any]


# ---------------------------------------------------------------- calibration


def _floor_rank(candidates: dict[str, CandidateMeasurement], target_id: str, floor: float) -> int | None:
    """The target's rank among ABOVE-FLOOR candidates under the full-order key
    (fused desc, chunk_id asc)."""
    passing = sorted(
        (c for c in candidates.values() if c.fused_similarity >= floor),
        key=lambda c: (-c.fused_similarity, c.chunk_id),
    )
    for index, candidate in enumerate(passing, start=1):
        if candidate.chunk_id == target_id:
            return index
    return None


def _stored_cue_assertions(rig: BridgeRig, label_map: dict[str, str]) -> tuple[StoredCueAssertion, ...]:
    stored = {chunk.chunk_id: chunk for chunk in rig.stored_chunks()}
    assertions: list[StoredCueAssertion] = []
    for chunk in bridge_corpus():
        stamped = stored[label_map[chunk.label]]
        expected = cue_entities(chunk.text)
        entities = tuple(stamped.cues.entities)
        assertions.append(
            StoredCueAssertion(
                label=chunk.label,
                chunk_id=stamped.chunk_id,
                stored_entities=entities,
                expected_entities=expected,
                ok=list(entities) == list(expected),
            )
        )
    return tuple(assertions)


def _measure_probe(
    rig: BridgeRig,
    probe_label: str,
    probe_text: str,
    label_map: dict[str, str],
    role_by_label: dict[str, str],
    config: HybridConfig,
    latency_repeats: int,
) -> ProbeRecord:
    target_id = label_map["target_fact"]
    label_by_id = {chunk_id: label for label, chunk_id in label_map.items()}
    measurement = rig.measure_probe(probe_text)
    by_id = {candidate.chunk_id: candidate for candidate in measurement.candidates}
    payload = rig.recall(probe_text)
    latencies: list[float] = []
    for _ in range(latency_repeats):
        start = time.perf_counter()
        rig.recall(probe_text)
        latencies.append((time.perf_counter() - start) * 1000.0)
    memory = payload["memory"]
    served_ids = tuple(str(entry["id"]) for entry in memory["entries"] if entry["kind"] == "chunk")
    served = tuple(
        ServedEntry(
            rank=index + 1,
            kind=str(entry["kind"]),
            id=str(entry["id"]),
            label=label_by_id.get(str(entry["id"]), "?"),
            score=float(entry["score"]),
            text=str(entry.get("text", "")),
        )
        for index, entry in enumerate(memory["entries"])
        if entry["kind"] == "chunk"
    )
    target = by_id.get(target_id)
    return ProbeRecord(
        label=probe_label,
        text=probe_text,
        cue_entities=cue_entities(probe_text),
        candidates=tuple(
            CandidateRecord(
                label=label_by_id.get(candidate.chunk_id, "?"),
                role=role_by_label.get(label_by_id.get(candidate.chunk_id, ""), "?"),
                chunk_id=candidate.chunk_id,
                fused_rank=candidate.fused_rank,
                dense_similarity=candidate.dense_similarity,
                fused_similarity=candidate.fused_similarity,
                sparse_similarity=candidate.sparse_similarity,
                gate_branch=candidate.gate_branch,
                above_floor=candidate.above_floor,
            )
            for candidate in measurement.candidates
        ),
        served=served,
        served_ids=served_ids,
        target_served=target_id in served_ids,
        target_served_rank=served_ids.index(target_id) + 1 if target_id in served_ids else None,
        target_fused=target.fused_similarity if target else None,
        target_dense=target.dense_similarity if target else None,
        target_sparse=target.sparse_similarity if target else None,
        target_above_floor=bool(target.above_floor) if target else False,
        target_floor_rank=_floor_rank(by_id, target_id, config.min_similarity),
        dropped_count=int(memory["dropped_count"]),
        budget_tokens=int(memory["budget_tokens"]),
        tokens_used=int(memory["tokens_used"]),
        coverage=dict(memory["coverage"]),
        latency=_latency_stats(latencies),
        latency_samples_ms=tuple(latencies),
    )


def run_calibration(
    root: Path,
    *,
    model_source: Path | None = None,
    latency_repeats: int = 12,
    embed_driver: str = "bge_m3_onnx",
    embed_dimension: int = 1024,
) -> CalibrationRecord:
    """The full calibration on one fresh rig (the root is deleted on success
    and kept as forensics on failure)."""
    rig = BridgeRig(
        root,
        embed_driver=embed_driver,
        embed_dimension=embed_dimension,
        model_source=model_source,
    )
    with rig:
        staging = rig.model_staging or ModelStaging(mode="not-needed", source=None, files=())
        label_map = rig.seed_corpus()
        role_by_label = {chunk.label: chunk.role.value for chunk in bridge_corpus()}
        assertions = _stored_cue_assertions(rig, label_map)
        pool = len(rig.stored_chunks())
        candidate_window = max(_MEASUREMENT_TOP_K * 4, _MEASUREMENT_TOP_K + 50)
        if pool > candidate_window:
            raise RuntimeError(
                f"corpus pool {pool} exceeds the measurement candidate window {candidate_window}"
            )
        config = HybridConfig()
        probes: list[ProbeRecord] = []
        for probe in bridge_probes():
            probes.append(
                _measure_probe(
                    rig, probe.label, probe.text, label_map, role_by_label, config, latency_repeats
                )
            )
    shutil.rmtree(root)
    return CalibrationRecord(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ"),
        brief_freeze=_BRIEF_FREEZE,
        embed_driver=embed_driver,
        model_staging=asdict(staging),
        stored_cue_assertions=assertions,
        stored_cue_assertions_ok=all(assertion.ok for assertion in assertions),
        probes=tuple(probes),
        aggregate_latency=_latency_stats(
            [sample for record in probes for sample in record.latency_samples_ms]
        ),
    )


# ---------------------------------------------------------------- v3 expectations

#: The §8.3 disclosure table (gated-max + depth 8 vs main): the per-probe
#: served counts and target ranks the W4 rerun must reproduce (drift fails
#: loud).
_V3_EXPECTED_SERVED_COUNTS: dict[str, int] = {
    "zh_natural": 7,
    "en_natural": 6,
    "zh_exact": 2,
    "irrelevant_en_rome": 0,
    "irrelevant_en_disease": 0,
    "irrelevant_zh": 4,
    "no_glossary_zh": 2,
    "no_glossary_en": 1,
}
_V3_EXPECTED_TARGET_RANKS: dict[str, int] = {"zh_natural": 6, "en_natural": 6, "zh_exact": 1}
_V3_TARGET_ABSENT_LABELS: tuple[str, ...] = (
    "irrelevant_en_rome",
    "irrelevant_en_disease",
    "irrelevant_zh",
    "no_glossary_zh",
    "no_glossary_en",
)


def check_v3_expectations(record: CalibrationRecord) -> dict[str, Any]:
    """Pin the §8.3 table against a rerun record: per-probe served counts,
    target ranks (zh/en natural at rank 6 within depth 8, exact-token target
    top-1) and target absence on the no-hit/irrelevant probes."""
    by_label = {probe.label: probe for probe in record.probes}
    drift: list[str] = []
    for label, expected in _V3_EXPECTED_SERVED_COUNTS.items():
        actual = len(by_label[label].served_ids)
        if actual != expected:
            drift.append(f"{label}: served {actual} != expected {expected}")
    for label, expected in _V3_EXPECTED_TARGET_RANKS.items():
        actual_rank = by_label[label].target_served_rank
        if actual_rank != expected:
            drift.append(f"{label}: target rank {actual_rank} != expected {expected}")
    for label in _V3_TARGET_ABSENT_LABELS:
        if by_label[label].target_served:
            drift.append(f"{label}: target served but must be absent")
    return {"ok": not drift, "drift": drift}


def write_record(record: CalibrationRecord, out_dir: Path) -> tuple[Path, Path]:
    """Write the JSON record and the human-readable summary (eval/ artifact
    convention: <UTC-timestamp>-bridge-calibration.{json,md})."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{record.generated_at}-bridge-calibration"
    json_path = out_dir / f"{base}.json"
    md_path = out_dir / f"{base}.md"
    json_path.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_summary(record), encoding="utf-8")
    return json_path, md_path


def _render_summary(record: CalibrationRecord) -> str:
    lines: list[str] = []
    append = lines.append
    append("# PRD-448 W1 bridge calibration record")
    append("")
    append(f"- generated: {record.generated_at}")
    append(f"- brief freeze: {record.brief_freeze[:8]}…")
    append(f"- embed driver: {record.embed_driver}")
    staging = record.model_staging
    append(f"- model staging: mode={staging['mode']} source={staging.get('source')}")
    for file in staging.get("files", []):
        append(f"  - {file['name']}: {file['bytes']} bytes sha256={file['sha256'][:16]}…")
    append("")
    append("## Stored cue stamping assertions (§4.1 oracle derivation)")
    append("")
    append(f"- all ok: {record.stored_cue_assertions_ok}")
    for assertion in record.stored_cue_assertions:
        mark = "OK" if assertion.ok else "FAIL"
        append(
            f"- [{mark}] {assertion.label}: stored={list(assertion.stored_entities)}"
            f" expected={list(assertion.expected_entities)}"
        )
    append("")
    append("## RED baseline + per-probe measurements (current main)")
    append("")
    for probe in record.probes:
        append(f"### {probe.label} — target served={probe.target_served} (rank={probe.target_served_rank})")
        append(f"- probe text: {probe.text}")
        append(f"- cue entities: {list(probe.cue_entities)}")
        append(
            f"- target similarities: dense={probe.target_dense} fused={probe.target_fused}"
            f" sparse={probe.target_sparse} above_floor={probe.target_above_floor}"
            f" floor_rank={probe.target_floor_rank}"
        )
        append(
            f"- budget: dropped_count={probe.dropped_count} tokens_used={probe.tokens_used}"
            f" budget_tokens={probe.budget_tokens}"
        )
        append(f"- coverage: {probe.coverage}")
        append(f"- latency ms: {probe.latency}")
        append("- served top-k:")
        for entry in probe.served:
            append(
                f"  {entry.rank}. [{entry.label}] {entry.id} score={entry.score:.4f} text={entry.text[:60]!r}"
            )
        append("- per-candidate similarities (gated-max branches):")
        for candidate in probe.candidates:
            sparse = "n/a" if candidate.sparse_similarity is None else f"{candidate.sparse_similarity:.4f}"
            append(
                f"  [{candidate.label}/{candidate.role}] rank={candidate.fused_rank}"
                f" dense={candidate.dense_similarity:.4f} gated={candidate.fused_similarity:.4f}"
                f" sparse={sparse} branch={candidate.gate_branch} above_floor={candidate.above_floor}"
            )
        append("")
    append("## Recall-route latency (aggregate over the probe set)")
    append("")
    append(f"- {record.aggregate_latency}")
    append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PRD-448 bridge calibration (real embedded preset)",
    )
    parser.add_argument(
        "--root", type=Path, required=True, help="fresh rig root (fail-loud; deleted on success)"
    )
    parser.add_argument("--out", type=Path, default=Path("eval"), help="record output dir (default: eval/)")
    parser.add_argument(
        "--model-source", type=Path, default=None, help="installed model cache dir to copy from"
    )
    parser.add_argument("--latency-repeats", type=int, default=12)
    parser.add_argument(
        "--expect-v3",
        action="store_true",
        help="pin the served-count disclosure table (fail loud on drift)",
    )
    args = parser.parse_args(argv)
    record = run_calibration(
        args.root,
        model_source=args.model_source,
        latency_repeats=args.latency_repeats,
    )
    json_path, md_path = write_record(record, args.out)
    print(f"calibration record: {json_path}")
    print(f"summary: {md_path}")
    print(f"stored-cue assertions ok: {record.stored_cue_assertions_ok}")
    print(f"aggregate latency: {record.aggregate_latency}")
    if args.expect_v3:
        check = check_v3_expectations(record)
        print(f"v3 disclosure-table check: ok={check['ok']}")
        for line in check["drift"]:
            print(f"DRIFT {line}")
        if not check["ok"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
