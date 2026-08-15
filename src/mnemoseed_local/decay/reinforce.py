"""Reinforcement event consumer (PRD-04 FR-4.2, design/01 stage ⑤).

The counterpart of the decay sweep: where the sweep is a TREND (it only lowers
weights and uses ``last_reinforced`` as its baseline), a retrieval usage event
is an EVENT — it refreshes ``last_reinforced`` and applies a bounded rebound to
``decay_weight``, making the item decay-neutral for the next sweep interval.

Chunk hits (vector) ride the existing batch weight-update port: one
``WeightUpdate`` carries both the rebound and the refreshed baseline, and the
usage counters (``hit_count`` / ``last_hit_at``) land through the same
``update_chunk_state`` seam FR-3.7 already used. Node hits (graph) refresh the
full node through ``GraphStore.upsert_node``: the graph batch port
(``GraphWeightUpdate``) carries no ``last_reinforced`` field, and refreshing
the baseline is the only way the ordering contract holds for the graph side.

Guardrail (FR-4.3 ladder): a hit on an item whose decay_weight sits below the
candidate floor still counts the usage but never rebounds — sunk memories are
only resurrected by the explicit revival path, never by incidental recall hits.

The rebound step is ``min(1.0, decay_weight + reinforcement_bonus)`` with a
small constant bonus (see ``ReinforceConfig.bonus``). The spacing-effect
cooldown (FR-4.2: repeated recalls within a short window yield diminishing
returns) is deliberately NOT implemented here — the docs describe it but pin no
mechanism, and the capture-side event (stamper's Hebbian rebound) applies the
same flat-step semantics, so both event sides stay consistent.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from mnemoseed_local.schema.graph import GraphNode
from mnemoseed_local.schema.stamp import ChunkStamp
from mnemoseed_local.storage.factory import Stores
from mnemoseed_local.storage.ports import WeightUpdate

__all__ = ["ReinforceConfig", "Reinforcer"]

#: Design default for the FR-4.2 rebound step. design/01 §5 names the formula
#: ``min(1.0, w + reinforcement_bonus)`` but pins no value; 0.1 matches the
#: capture-side ``WriteConfig.reinforce_bonus`` so both event sides rebound
#: with the same small step.
REINFORCE_BONUS = 0.1

#: The candidate-pool decay floor (HybridConfig.min_decay / design item 2):
#: below-floor items never enter the pool, and a hit on one never rebounds.
CANDIDATE_FLOOR = 0.4


@dataclass(frozen=True)
class ReinforceConfig:
    """Tunable constants for a reinforcement event (FR-4.2).

    ``bonus`` is the rebound step toward the 1.0 ceiling; ``min_decay`` is the
    floor under which a hit counts but never rebounds (FR-4.3 ladder).
    """

    bonus: float = REINFORCE_BONUS
    min_decay: float = CANDIDATE_FLOOR


class Reinforcer:
    """Applies the FR-4.2 rebound to a batch of retrieval usage events.

    Fire-and-forget by design: unknown ids (concurrently purged) are ignored
    silently, and the caller (the daemon recall path) wraps the whole call in
    its best-effort guard so usage accounting never fails a recall.
    """

    def __init__(
        self,
        stores: Stores,
        *,
        clock: Callable[[], float] | None = None,
        config: ReinforceConfig | None = None,
    ) -> None:
        self._stores = stores
        self._clock = clock if clock is not None else time.time
        self._config = config if config is not None else ReinforceConfig()

    @property
    def config(self) -> ReinforceConfig:
        return self._config

    def record_hits(self, chunk_ids: Sequence[str], node_ids: Sequence[str]) -> None:
        """Consume one batch of retrieval usage events.

        ``chunk_ids`` are the chunk entries that made the context package
        (vector track), ``node_ids`` the graph entries (graph track). Each live
        item above the floor gets its baseline refreshed and its weight
        rebounded; below-floor items only count their usage.
        """
        now = self._clock()
        self._record_chunk_hits(chunk_ids, now)
        self._record_node_hits(node_ids, now)

    def _record_chunk_hits(self, chunk_ids: Sequence[str], now: float) -> None:
        updates: list[WeightUpdate] = []
        for chunk_id in chunk_ids:
            chunk = self._stores.vector.get_chunk(chunk_id)
            if chunk is None:
                continue
            if chunk.decay_weight >= self._config.min_decay:
                updates.append(self._chunk_update(chunk, now))
        if updates:
            self._stores.vector.update_weights(updates)
        if chunk_ids:
            # Usage accounting (FR-3.7) is independent of the rebound: every
            # hit entry counts, below-floor ones included.
            self._stores.vector.update_chunk_state(chunk_ids, hit_increment=1)

    def _chunk_update(self, chunk: ChunkStamp, now: float) -> WeightUpdate:
        rebound = min(1.0, chunk.decay_weight + self._config.bonus)
        return WeightUpdate(
            chunk_id=chunk.chunk_id,
            decay_weight=rebound,
            last_reinforced=now,
        )

    def _record_node_hits(self, node_ids: Sequence[str], now: float) -> None:
        for node_id in node_ids:
            node = self._stores.graph.get_node(node_id)
            if node is None:
                continue
            if node.decay_weight < self._config.min_decay:
                self._count_node_usage(node, now)
                continue
            self._reinforce_node(node, now)

    def _reinforce_node(self, node: GraphNode, now: float) -> None:
        """Refresh baseline + rebound + count the usage in one node write.

        ``GraphWeightUpdate`` cannot carry ``last_reinforced``, so the graph
        side of the event goes through the existing full-node write port.
        """
        rebound = min(1.0, node.decay_weight + self._config.bonus)
        reinforced = node.model_copy(
            update={
                "decay_weight": rebound,
                "last_reinforced": now,
                "hit_count": node.hit_count + 1,
                "last_hit_at": now,
            }
        )
        self._stores.graph.upsert_node(reinforced)

    def _count_node_usage(self, node: GraphNode, now: float) -> None:
        counted = node.model_copy(
            update={
                "hit_count": node.hit_count + 1,
                "last_hit_at": now,
            }
        )
        self._stores.graph.upsert_node(counted)
