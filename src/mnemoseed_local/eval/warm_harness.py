"""Warm-needle measurement rig (design/10 §5.2, Gate 2): the ε=0 baseline
run path over the REAL recall daemon.

One rig boots a REAL daemon (``daemon.app.create_app``) whose config lives
entirely under the caller-given root, seeds one warm-needle point's needle fact
AND its same-band decoy, and drives each warm-window probe via POST
/memory/recall: a first query recalls the needle, then (after the probe's
declared delay) a changed-wording re-query is measured for whether/at what
rank+score the SAME needle fact re-surfaces. The decoy-aligned negative control
proves the instrument can also measure the needle NOT surfacing. Zero runtime
retrieval changes: the daemon serves as-is and the observation is measured, and
the result is labelled activation-off (ε=0) — the mechanism it measures does not
exist yet. Isolation mirrors the T2 recall / rescue rigs: fail-loud
materialization, per-point roots, strict serial lifecycles, daemon-log handler
release.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.eval.recall_harness import _point_config, release_daemon_log_handler

# re-export: the shared freshness contract keeps its historical import path
from mnemoseed_local.eval.rig_freshness import RigRootNotFresh as RigRootNotFresh
from mnemoseed_local.eval.rig_freshness import require_fresh_root
from mnemoseed_local.eval.warm_materials import WarmNeedleMaterial
from mnemoseed_local.eval.warm_matrix import (
    WARM_EPSILON_BASELINE,
    WarmProbeMetrics,
)
from mnemoseed_local.schema.stamp import (
    ChunkStamp,
    CognitiveTier,
    Cues,
    Provenance,
    ProvenanceEvent,
)
from mnemoseed_local.storage.ports import StoredProfile


def _config_toml(root: Path) -> str:
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
    )


class WarmRig:
    """One /memory/recall rig over a disposable daemon app, driven over HTTP."""

    def __init__(self, root: Path, *, profile_id: str = "warm-eval") -> None:
        require_fresh_root(root)
        self.root = root
        self.profile_id = profile_id
        (root / "config.toml").write_text(_config_toml(root), encoding="utf-8")
        self._stack: ExitStack | None = None
        self._client: TestClient | None = None

    @property
    def client(self) -> TestClient:
        assert self._client is not None, "WarmRig must be entered: with WarmRig(...) as rig"
        return self._client

    @property
    def _state(self) -> Any:
        """The daemon app's live state (stores/memory), typed Any — fastapi's
        TestClient stub types ``app`` as a callable."""
        return cast(Any, self.client).app.state

    def __enter__(self) -> WarmRig:
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

    def seed_material(self, material: WarmNeedleMaterial) -> None:
        """Write the point's needle fact AND its whole same-band decoy set
        straight through the stores — rival candidates per point so the needle's
        top-k slot can genuinely move by score (even drop out of top-k)."""
        embedder = self._state.stores.embed
        vector = self._state.stores.vector
        self._state.stores.meta.upsert_profile(StoredProfile(profile_id=self.profile_id))
        actors: tuple[tuple[str, str], ...] = (
            (material.fact_id, material.fact_text),
            *material.decoys,
        )
        for chunk_id, text in actors:
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
                        asserted_by="eval",
                        session_id=material.session_id,
                        source="capture.session",
                        confidence=1.0,
                        asserted_at=1_700_000_000.0,
                        history=[ProvenanceEvent(action="created", actor="eval", at=1_700_000_000.0)],
                    ),
                    decay_weight=1.0,
                    ingested_at=1_700_000_000.0,
                ),
                embedding.dense,
                embedding.sparse,
            )

    def recall(self, query: str) -> dict[str, Any]:
        """POST /memory/recall for the rig's query.

        Design/10 §3.1 keys the future activation state by
        ``(profile_id, session_id)``, but this endpoint currently carries no
        ``session_id`` field. The instrument keeps each point's session identity
        explicit at the material level (``WarmNeedleMaterial.session_id``); when
        activation is implemented, /memory/recall will need a session_id seam so
        the per-session activation map can be keyed — out of scope for this
        baseline (zero runtime retrieval changes)."""
        response = self.client.post(
            "/memory/recall",
            json={"profile_id": self.profile_id, "query": query},
        )
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())


@dataclass(frozen=True)
class WarmPointResult:
    """One warm-needle point's measured probes plus the honest activation state."""

    point_id: str
    activation_enabled: bool  # False: the activation mechanism is not implemented
    activation_eps: float  # WARM_EPSILON_BASELINE: the recorded ε=0 baseline
    probe_metrics: tuple[WarmProbeMetrics, ...]


def _served_entries(response: dict[str, Any]) -> list[dict[str, Any]]:
    return list(response["memory"]["entries"])


def _entry(entries: list[dict[str, Any]], fact_id: str) -> dict[str, Any] | None:
    return next((entry for entry in entries if str(entry["id"]) == fact_id), None)


def _rank(entries: list[dict[str, Any]], fact_id: str) -> int | None:
    for index, entry in enumerate(entries):
        if str(entry["id"]) == fact_id:
            return index + 1
    return None


def run_warm_point(
    material: WarmNeedleMaterial,
    *,
    root: Path,
    sleep: Callable[[float], None] = time.sleep,
) -> WarmPointResult:
    """One warm-needle point on its own fresh rig, strict serial lifecycle.

    Drives every probe (immediate, delayed, and the decoy-aligned negative
    control) uniformly: a first recall of the needle, then the probe's re-query
    after its declared delay. Success deletes the deterministic root so later
    groups can reuse the name; a failure propagates and KEEPS the root as
    forensics (T4a contract)."""
    with WarmRig(root) as rig:
        rig.seed_material(material)
        metrics: list[WarmProbeMetrics] = []
        for probe in material.probes:
            first = rig.recall(material.first_query)
            first_entries = _served_entries(first)
            first_entry = _entry(first_entries, material.fact_id)
            first_score = float(first_entry["score"]) if first_entry is not None else None
            # the warm window: the re-query lands `delay_s` after the first recall
            sleep(probe.delay_s)
            re = rig.recall(probe.re_query)
            re_entries = _served_entries(re)
            re_entry = _entry(re_entries, material.fact_id)
            metrics.append(
                WarmProbeMetrics(
                    window=probe.window,
                    delay_s=probe.delay_s,
                    first_surfaced=first_entry is not None,
                    re_surfaced=re_entry is not None,
                    first_score=first_score,
                    re_score=float(re_entry["score"]) if re_entry is not None else None,
                    re_rank=_rank(re_entries, material.fact_id),
                )
            )
    result = WarmPointResult(
        point_id=material.point_id,
        activation_enabled=False,
        activation_eps=WARM_EPSILON_BASELINE,
        probe_metrics=tuple(metrics),
    )
    shutil.rmtree(root)
    return result
