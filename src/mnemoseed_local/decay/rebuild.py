"""One-time pin-weight rebuild (design/09 §4.1, retention redesign).

Chunks written through the explicit-pin path spent their early life decaying at
the ordinary verbatim-chunk rate; the flashbulb tier is much slower. This
migration recomputes every live pin's effective weight from its
``last_reinforced`` baseline (``ingested_at`` fallback, the same rule the sweep
uses) under the pin λ and writes it back through the existing batch weight port
— no new storage surface, no schema change (the class derives from
provenance.source at read time).

Determinism and idempotency: the recomputation is a pure function of stored
state, so a crash before the completion marker reruns harmlessly; after the
marker lands in the meta store (same convention as the sweep cursor — outside
the configwrite registry) the pass never runs again. The summary append is
guarded by a prior-row check, so even a crash between the audit write and the
marker keeps the accounting at exactly ONE summary entry.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from mnemoseed_local.config import Config
from mnemoseed_local.decay.model import SECONDS_PER_DAY, decay_weight, lambda_for
from mnemoseed_local.schema.stamp import EXPLICIT_PIN_SOURCE, ChunkStamp
from mnemoseed_local.storage.factory import Stores
from mnemoseed_local.storage.ports import AuditEntry, AuditFilter, ChunkFilter, Page, WeightUpdate

#: Private meta-store marker: once present, the migration already ran. The
#: ``__``-prefixed name stays outside the configwrite registry (sweep-cursor
#: convention), so the versioned settings history never lists it.
PIN_REBUILD_MARKER_KEY = "__retention__pin_weight_rebuild_v1"

_REBUILD_AUDIT_ACTION = "pin_weight_rebuild"
_REBUILD_ACTOR = "daemon"

#: Bounded read page for the per-profile scans (sweeper convention).
_PAGE_LIMIT = 2000


@dataclass(frozen=True)
class PinRebuildStats:
    """Migration accounting (the payload of one audit entry)."""

    profiles_scanned: int
    pins_scanned: int
    pins_updated: int


def rebuild_pin_weights(
    stores: Stores,
    config: Config,
    *,
    clock: Callable[[], float] | None = None,
) -> PinRebuildStats | None:
    """Recompute every explicit-pin chunk's weight under the pin λ, once.

    Returns None when the migration already completed; otherwise the stats of
    this pass. Sub-min_apply_delta differences skip the write so a rerun in the
    crash window is a no-op on already-correct rows.
    """
    if stores.meta.get_config(PIN_REBUILD_MARKER_KEY) is not None:
        return None
    now = clock() if clock is not None else time.time()
    lam_map = config.decay.lambda_per_type
    min_delta = config.decay.min_apply_delta

    profiles = {profile.profile_id for profile in stores.meta.list_profiles()}
    profiles.update(stores.meta.pool_states())
    pins_scanned = 0
    updates: list[WeightUpdate] = []
    for profile_id in sorted(profiles):
        offset = 0
        while True:
            page = stores.vector.list_chunks(
                ChunkFilter(profile_id=profile_id), Page(offset=offset, limit=_PAGE_LIMIT)
            )
            if not page.items:
                break
            for chunk in page.items:
                if chunk.provenance.source != EXPLICIT_PIN_SOURCE:
                    continue
                pins_scanned += 1
                target = _rebuild_target(chunk.provenance.confidence, chunk, now, lam_map)
                if abs(chunk.decay_weight - target) < min_delta:
                    continue
                updates.append(WeightUpdate(chunk_id=chunk.chunk_id, decay_weight=target))
            if len(page.items) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT
    if updates:
        stores.vector.update_weights(updates)

    stats = PinRebuildStats(
        profiles_scanned=len(profiles),
        pins_scanned=pins_scanned,
        pins_updated=len(updates),
    )
    if not _summary_already_audited(stores):
        stores.meta.audit_append(
            AuditEntry(
                actor=_REBUILD_ACTOR,
                action=_REBUILD_AUDIT_ACTION,
                detail={
                    "profiles_scanned": stats.profiles_scanned,
                    "pins_scanned": stats.pins_scanned,
                    "pins_updated": stats.pins_updated,
                },
                at=now,
            )
        )
    stores.meta.set_config(PIN_REBUILD_MARKER_KEY, {"completed_at": now})
    return stats


def _summary_already_audited(stores: Stores) -> bool:
    """A crashed predecessor (audit row landed, marker did not) must not
    duplicate the summary on the rerun — the design's exactly-one accounting."""
    page = stores.meta.audit_query(AuditFilter(action=_REBUILD_AUDIT_ACTION), Page(limit=1))
    return bool(page.items)


def _rebuild_target(confidence: float, chunk: ChunkStamp, now: float, lam_map: dict[str, float]) -> float:
    """The flashbulb-curve value from the sweep's baseline rule."""
    baseline = chunk.last_reinforced if chunk.last_reinforced is not None else chunk.ingested_at
    lam = lambda_for("pin", lam_map)
    days = max(0.0, (now - baseline) / SECONDS_PER_DAY)
    return decay_weight(confidence, lam, days)
