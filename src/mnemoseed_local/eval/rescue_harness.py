"""Rescue-band calibration harness (design/09 §3.5): the MCP-recall rig.

One rig boots a REAL daemon (``daemon.app.create_app``) whose config lives
entirely under the caller-given root — including the ``[recall]`` rescue-band
thresholds under test — and drives POST /memory/recall over HTTP after seeding
the material's four actors directly through the stores (deterministic, zero
LLM). Isolation mirrors the T2 recall rig: fail-loud materialization, per-point
roots, strict serial lifecycles, daemon-log handler release on exit.
"""

from __future__ import annotations

import shutil
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.decay.reinforce import CANDIDATE_FLOOR
from mnemoseed_local.eval.recall_harness import _point_config, release_daemon_log_handler
from mnemoseed_local.eval.rescue_materials import DEAD_PIN_DECAY, RescueMaterial
from mnemoseed_local.eval.rescue_matrix import RescuePointMetrics, pin_is_eligible
from mnemoseed_local.schema.stamp import (
    EXPLICIT_PIN_SOURCE,
    ChunkStamp,
    CognitiveTier,
    Cues,
    Provenance,
    ProvenanceEvent,
)
from mnemoseed_local.storage.ports import ChunkFilter, Page, StoredProfile


class RigRootNotFresh(RuntimeError):
    """A rig root carried prior state when a point tried to materialize."""


def _config_toml(root: Path, rescue_floor: float, rescue_cue_min: float) -> str:
    stores = root / "stores"
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(stores / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(stores / "cortex.db").as_posix()}"\n'
        f"[storage.graph.instances.isolated]\n"
        f'driver = "sqlite_graph"\npath = "{(stores / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(stores / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream]\n"
        "auto_trigger = false\n"
        "floor_pool_points = 1000000\n"
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[recall]\n"
        f"rescue_floor = {rescue_floor}\n"
        f"rescue_cue_min = {rescue_cue_min}\n"
    )


class RescueRig:
    """One MCP-recall rig over a disposable daemon app, driven over HTTP."""

    def __init__(
        self,
        root: Path,
        *,
        rescue_floor: float,
        rescue_cue_min: float,
        profile_id: str = "rescue-eval",
    ) -> None:
        if root.exists() and any(root.iterdir()):
            raise RigRootNotFresh(
                f"rig root {root} is not fresh: prior state present, refusing to materialize"
            )
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.rescue_floor = rescue_floor
        self.rescue_cue_min = rescue_cue_min
        self.profile_id = profile_id
        (root / "config.toml").write_text(_config_toml(root, rescue_floor, rescue_cue_min), encoding="utf-8")
        self._stack: ExitStack | None = None
        self._client: TestClient | None = None

    @property
    def client(self) -> TestClient:
        assert self._client is not None, "RescueRig must be entered: with RescueRig(...) as rig"
        return self._client

    @property
    def _state(self) -> Any:
        """The daemon app's live state (stores/memory), typed Any — fastapi's
        TestClient stub types ``app`` as a callable."""
        return cast(Any, self.client).app.state

    def __enter__(self) -> RescueRig:
        stack = ExitStack()
        try:
            stack.enter_context(_point_config(self.root, self.root / "config.toml"))
            self._client = stack.enter_context(TestClient(create_app()))
        except BaseException:
            stack.close()
            release_daemon_log_handler(self.root)
            raise
        self._stack = stack
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        assert self._stack is not None
        self._stack.__exit__(exc_type, exc, tb)
        self._stack = None
        self._client = None
        release_daemon_log_handler(self.root)

    # ------------------------------------------------------------ pipeline

    def seed_material(self, material: RescueMaterial) -> None:
        """Write the point's four actors straight through the stores."""
        embedder = self._state.stores.embed
        vector = self._state.stores.vector
        self._state.stores.meta.upsert_profile(StoredProfile(profile_id=self.profile_id))
        actors: tuple[tuple[str, str, float, str], ...] = (
            (material.pin_id, material.pin_text, material.pin_decay, EXPLICIT_PIN_SOURCE),
            (material.decoy_id, material.decoy_text, material.decoy_decay, "capture.session"),
            (material.healthy_id, material.healthy_text, material.healthy_decay, "capture.session"),
            (material.dead_pin_id, material.dead_pin_text, DEAD_PIN_DECAY, EXPLICIT_PIN_SOURCE),
        )
        for chunk_id, text, decay, source in actors:
            embedding = embedder.embed(text)
            vector.upsert_chunk(
                ChunkStamp(
                    chunk_id=chunk_id,
                    profile_id=self.profile_id,
                    text=text,
                    cognitive_tier=CognitiveTier.TIER_1,
                    model_id="eval",
                    cues=Cues(entities=[material.entity]),
                    provenance=Provenance(
                        asserted_by="user" if source == EXPLICIT_PIN_SOURCE else "eval",
                        session_id=None,
                        source=source,
                        confidence=1.0,
                        asserted_at=1_700_000_000.0,
                        history=[ProvenanceEvent(action="created", actor="eval", at=1_700_000_000.0)],
                    ),
                    decay_weight=decay,
                    ingested_at=1_700_000_000.0,
                ),
                embedding.dense,
                embedding.sparse,
            )

    def recall(self, query: str) -> dict[str, Any]:
        response = self.client.post(
            "/memory/recall",
            json={"profile_id": self.profile_id, "query": query},
        )
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())

    def chunk_weights(self) -> dict[str, float]:
        page = self._state.stores.vector.list_chunks(
            ChunkFilter(profile_id=self.profile_id), Page(offset=0, limit=100)
        )
        return {chunk.chunk_id: chunk.decay_weight for chunk in page.items}


@dataclass(frozen=True)
class RescuePointResult:
    """One calibration point's raw evidence + derived metrics."""

    response: dict[str, Any]
    metrics: RescuePointMetrics


def score_rescue_run(
    material: RescueMaterial,
    response: dict[str, Any],
    *,
    rescue_floor: float,
    cue_min: float,
    weights_before: dict[str, float],
    weights_after: dict[str, float],
) -> RescuePointMetrics:
    """Hand-computable metrics for one run (the matrix consumes these)."""
    entries = response["memory"]["entries"]
    served_ids = [str(entry["id"]) for entry in entries]
    eligible = pin_is_eligible(
        material.pin_decay, material.cue_overlap, rescue_floor=rescue_floor, cue_min=cue_min
    )
    served = material.pin_id in served_ids
    noise_ids = {material.decoy_id, material.dead_pin_id}
    noise_admitted = sum(1 for chunk_id in served_ids if chunk_id in noise_ids)
    if not eligible and served:
        noise_admitted += 1  # an ineligible band pin crossing into the pool
    weight_before = weights_before.get(material.pin_id)
    weight_after = weights_after.get(material.pin_id)
    rebound_ok = True
    if served and weight_before is not None and weight_after is not None:
        rebound_ok = weight_after > weight_before
    residue_ids = {
        str(row.get("chunk_id")) for row in response["memory"].get("index_residue", {}).get("rows", [])
    }
    rank_after_normal: bool | None = None
    if served and material.healthy_id in served_ids:
        rank_after_normal = served_ids.index(material.pin_id) > served_ids.index(material.healthy_id)
    return RescuePointMetrics(
        eligible=eligible,
        served=served,
        in_band=rescue_floor <= material.pin_decay < CANDIDATE_FLOOR,
        noise_admitted=noise_admitted,
        rebound_ok=rebound_ok,
        dead_leaked=material.dead_pin_id in served_ids,
        dead_residue_present=material.dead_pin_id in residue_ids,
        rank_after_normal=rank_after_normal,
    )


def run_rescue_point(
    material: RescueMaterial,
    *,
    root: Path,
    rescue_floor: float,
    rescue_cue_min: float,
) -> RescuePointResult:
    """One evaluation point on its own fresh rig, strict serial lifecycle.

    Success deletes the deterministic root so later groups can reuse the name;
    a failure propagates and KEEPS the root as forensics (T4a contract)."""
    with RescueRig(root, rescue_floor=rescue_floor, rescue_cue_min=rescue_cue_min) as rig:
        rig.seed_material(material)
        weights_before = rig.chunk_weights()
        response = rig.recall(material.query)
        weights_after = rig.chunk_weights()
    metrics = score_rescue_run(
        material,
        response,
        rescue_floor=rescue_floor,
        cue_min=rescue_cue_min,
        weights_before=weights_before,
        weights_after=weights_after,
    )
    shutil.rmtree(root)
    return RescuePointResult(response=response, metrics=metrics)
