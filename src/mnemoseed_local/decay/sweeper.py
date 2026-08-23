"""Decay sweep loop (PRD-04 FR-4.1 / FR-4.4, design/01 stage ⑤).

The daemon-owned periodic task walks every profile's unreinforced graph nodes
and verbatim chunks, computes the FR-4.1 curve from each item's
``last_reinforced`` baseline, and writes batch weight updates through the
existing storage ports (``VectorStore.update_weights`` /
``GraphStore.batch_update_weights``) — no new port method. Per-profile resume
cursors persist in the meta config store, so a crash between the weight writes
and the cursor update re-sweeps idempotently on the next boot (deterministic
recomputation plus the min_apply_delta floor make the re-pass a write no-op).

Guardrail ordering (design/01 §5): reinforcement is an EVENT — it raises the
weight and refreshes ``last_reinforced``; the sweep is a TREND — it only ever
lowers weights, uses ``last_reinforced`` as its baseline, and never raises
(the monotonic min() with the recomputed target). ``never_decay`` pins are
excluded by an explicit filter; tombstoned/deleted revisions are excluded by
construction because the sweep only reads current revisions. The FR-4.1
base_confidence factor is a ceiling the trend re-asserts once an item has been
unreinforced for a full sweep interval.

Observability: exactly ONE ``decay_sweep`` audit entry per profile pass
(actor=daemon) carrying the per-sweep stats (scanned / updated / max-drop) —
never per-node audit noise.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from mnemoseed_local.config import Config
from mnemoseed_local.decay.model import (
    SECONDS_PER_DAY,
    chunk_lambda_type,
    decay_weight,
    lambda_for,
)
from mnemoseed_local.schema.graph import GraphNode
from mnemoseed_local.schema.stamp import ChunkStamp
from mnemoseed_local.storage.factory import Stores
from mnemoseed_local.storage.ports import (
    AuditEntry,
    ChunkFilter,
    GraphWeightUpdate,
    NodeFilter,
    Page,
    WeightUpdate,
)

logger = logging.getLogger("mnemoseed_local.decay.sweeper")

#: Private meta-store key holding the per-profile resume cursor. The
#: ``__``-prefixed name is deliberately outside the configwrite registry, so the
#: append-only versioned settings history never lists it (same convention as the
#: configwrite file-state key).
_SWEEP_CURSOR_KEY = "__decay__cursor"

_SWEEP_AUDIT_ACTION = "decay_sweep"
_SWEEP_ACTOR = "daemon"

#: Bounded read page for the per-store scans.
_PAGE_LIMIT = 2000


@dataclass(frozen=True)
class SweepStats:
    """Per-profile sweep accounting (the payload of one decay_sweep audit)."""

    profile_id: str
    chunks_scanned: int
    chunks_updated: int
    nodes_scanned: int
    nodes_updated: int
    max_drop: float


class DecaySweeper:
    """Runs the decay sweep over every due profile (daemon-owned, async loop).

    The sweeper holds a LIVE reference to the Config: λ, the sweep interval and
    the enabled flag are re-read at sweep time, so a configwrite change hot-
    applies to the next pass without a restart.
    """

    def __init__(
        self,
        stores: Stores,
        config: Config,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._stores = stores
        self._config = config
        self._clock = clock if clock is not None else time.time
        self._last_stats: list[SweepStats] = []

    @property
    def enabled(self) -> bool:
        return self._config.decay.enabled

    @property
    def interval(self) -> float:
        return self._config.decay.sweep_interval_s

    def last_stats(self) -> list[SweepStats]:
        """The most recent pass's per-profile stats (console observability)."""
        return list(self._last_stats)

    # ------------------------------------------------------------ one pass

    def run_once(self) -> list[SweepStats]:
        """One full sweep pass over every profile whose cursor is due.

        A profile is due when its last-swept timestamp is older than one sweep
        interval (or never swept). The cursor is updated only after each
        profile's pass completes, so a crash mid-pass re-runs exactly the
        unfinished profiles on the next boot — deterministically idempotent.
        """
        if not self._config.decay.enabled:
            return []
        now = self._clock()
        interval = self._config.decay.sweep_interval_s
        cursor = self._read_cursor()
        stats: list[SweepStats] = []
        for profile_id in sorted(self._profiles()):
            last = cursor.get(profile_id)
            if last is not None and now - last < interval:
                continue
            stats.append(self._sweep_profile(profile_id, now, interval))
            cursor[profile_id] = now
            # Persist after EACH profile: a crash mid-pass re-runs exactly the
            # unfinished profiles on the next boot (no re-scan of done ones).
            self._stores.meta.set_config(_SWEEP_CURSOR_KEY, {"profiles": dict(cursor)})
        self._last_stats = stats
        return stats

    async def run_forever(self) -> None:
        """The daemon-owned periodic loop.

        Ticks once immediately (crash-safe catch-up after downtime), then
        sleeps one interval. The interval and the enabled flag are re-read each
        iteration, so configwrite changes hot-apply to the next tick.
        """
        while True:
            try:
                if self._config.decay.enabled:
                    self.run_once()
            except Exception:
                logger.exception("decay sweep failed; the next tick retries")
            await asyncio.sleep(self._config.decay.sweep_interval_s)

    # ------------------------------------------------------------ one profile

    def _sweep_profile(self, profile_id: str, now: float, interval: float) -> SweepStats:
        """Sweep one profile's graph nodes and chunks; one audit entry per pass.

        The scan is read-page based so the sweep stays within the existing port
        surface (no new batch-read method): current-revision nodes only
        (tombstoned/deleted excluded by construction), live chunks only.
        """
        cut = now - interval
        lam_map = self._config.decay.lambda_per_type
        min_delta = self._config.decay.min_apply_delta
        nodes_scanned = nodes_updated = 0
        chunks_scanned = chunks_updated = 0
        max_drop = 0.0

        node_updates: list[GraphWeightUpdate] = []
        offset = 0
        while True:
            page = self._stores.graph.list_nodes(
                NodeFilter(profile_id=profile_id), Page(offset=offset, limit=_PAGE_LIMIT)
            )
            if not page.items:
                break
            for node in page.items:
                nodes_scanned += 1
                target = self._node_target(node, now, cut, lam_map)
                if target is None:
                    continue
                drop = node.decay_weight - target
                if drop >= min_delta:
                    node_updates.append(GraphWeightUpdate(node_id=node.node_id, decay_weight=target))
                    max_drop = max(max_drop, drop)
            if len(page.items) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT
        nodes_updated = len(node_updates)
        if node_updates:
            self._stores.graph.batch_update_weights(node_updates)

        chunk_updates: list[WeightUpdate] = []
        offset = 0
        while True:
            chunk_page = self._stores.vector.list_chunks(
                ChunkFilter(profile_id=profile_id), Page(offset=offset, limit=_PAGE_LIMIT)
            )
            if not chunk_page.items:
                break
            for chunk in chunk_page.items:
                chunks_scanned += 1
                target = self._chunk_target(chunk, now, cut, lam_map)
                if target is None:
                    continue
                drop = chunk.decay_weight - target
                if drop >= min_delta:
                    chunk_updates.append(WeightUpdate(chunk_id=chunk.chunk_id, decay_weight=target))
                    max_drop = max(max_drop, drop)
            if len(chunk_page.items) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT
        chunks_updated = len(chunk_updates)
        if chunk_updates:
            self._stores.vector.update_weights(chunk_updates)

        self._stores.meta.audit_append(
            AuditEntry(
                actor=_SWEEP_ACTOR,
                action=_SWEEP_AUDIT_ACTION,
                detail={
                    "profile_id": profile_id,
                    "nodes_scanned": nodes_scanned,
                    "nodes_updated": nodes_updated,
                    "chunks_scanned": chunks_scanned,
                    "chunks_updated": chunks_updated,
                    "max_drop": max_drop,
                },
                at=now,
            )
        )
        return SweepStats(
            profile_id=profile_id,
            chunks_scanned=chunks_scanned,
            chunks_updated=chunks_updated,
            nodes_scanned=nodes_scanned,
            nodes_updated=nodes_updated,
            max_drop=max_drop,
        )

    def _node_target(
        self,
        node: GraphNode,
        now: float,
        cut: float,
        lam_map: dict[str, float],
    ) -> float | None:
        """The recomputed weight for one node, or None when it must be skipped.

        Excluded: ``never_decay`` pins (FR-4.4) and items reinforced within the
        current sweep interval (the event's value stands). The sweep is
        monotonic: the candidate only binds when it is below the current weight.
        """
        if node.never_decay:
            return None
        if node.last_reinforced >= cut:
            return None
        lam = lambda_for(node.node_type.value, lam_map)
        days = max(0.0, (now - node.last_reinforced) / SECONDS_PER_DAY)
        candidate = decay_weight(node.confidence, lam, days)
        return min(node.decay_weight, candidate)

    def _chunk_target(
        self,
        chunk: ChunkStamp,
        now: float,
        cut: float,
        lam_map: dict[str, float],
    ) -> float | None:
        """The recomputed weight for one chunk (verbatim channel).

        The baseline is ``last_reinforced`` when the store carries one (the
        event's timestamp wins over the original ingestion time); freshly
        captured shards fall back to ``ingested_at``. The λ tier resolves from
        the provenance source (design/09 §3.1): an explicit-pin chunk fades at
        the flashbulb rate, everything else at the chunk rate. A consolidated
        chunk (post-dream merge marker, design/03 §4) resolves its λ at 3× the
        resolved rate — the evidence scene fades once the gist is in the graph.
        """
        baseline = chunk.last_reinforced if chunk.last_reinforced is not None else chunk.ingested_at
        if baseline >= cut:
            return None
        lam = lambda_for(chunk_lambda_type(chunk.provenance.source), lam_map, consolidated=chunk.consolidated)
        days = max(0.0, (now - baseline) / SECONDS_PER_DAY)
        candidate = decay_weight(chunk.provenance.confidence, lam, days)
        return min(chunk.decay_weight, candidate)

    # ------------------------------------------------------------ plumbing

    def _profiles(self) -> set[str]:
        """Every profile with a row or a score-pool ledger (D5 isolation)."""
        known = {profile.profile_id for profile in self._stores.meta.list_profiles()}
        known.update(self._stores.meta.pool_states())
        return known

    def _read_cursor(self) -> dict[str, float]:
        entry = self._stores.meta.get_config(_SWEEP_CURSOR_KEY)
        if entry is None:
            return {}
        profiles = entry.value.get("profiles")
        if not isinstance(profiles, dict):
            return {}
        return {
            str(profile_id): float(last)
            for profile_id, last in profiles.items()
            if isinstance(last, (int, float))
        }
