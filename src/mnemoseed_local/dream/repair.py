"""Bounded reconciliation audit repair."""

from __future__ import annotations

import time

from mnemoseed_local.storage.ports import (
    AuditEntry,
    GraphStore,
    MetaStore,
    reconciliation_audit_dedup_key,
)


def repair_reconciliation_audit(graph: GraphStore, meta: MetaStore, *, limit: int) -> int:
    """This function performs one bounded repair pass only. It owns
    neither scheduling nor threading. S-D decides when it runs, the
    supplied limit, retry policy, and ordering relative to dreams.
    """
    repaired = 0
    for pending in graph.pending_reconciliation_audits(limit):
        meta.audit_append(
            AuditEntry(
                actor="dream-engine",
                action=pending.action,
                detail=pending.detail,
                at=pending.created_at,
                dedup_key=reconciliation_audit_dedup_key(pending.nomination_id, pending.action),
            )
        )
        if graph.mark_reconciliation_audit_delivered(pending.outbox_id, time.time()):
            repaired += 1
    return repaired


__all__ = ["repair_reconciliation_audit"]
