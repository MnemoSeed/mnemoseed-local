"""S4 repair evidence: the bounded control repair, the receipt replay, and the
integrity seam of the read-only surface.

One pass is one call of the real global product repair over the pending audit
outbox of an isolated control pair. The repair carries no profile predicate: a
single pass delivers the pending audits of every profile of that store.

The integrity helpers live in the read-only surface; they are re-exported here
as the same objects, so both module paths name one implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, cast

from mnemoseed_local.dream.repair import repair_reconciliation_audit
from mnemoseed_local.instrument.ledger import BindingIncompleteError
from mnemoseed_local.instrument.sql_readonly import (
    NodeIntegritySnapshot,
    _read_product_receipt,
    assert_node_integrity,
    capture_node_integrity,
)
from mnemoseed_local.storage.ports import ReconciliationApplication, ReconciliationReceipt

if TYPE_CHECKING:
    from mnemoseed_local.instrument.fixtures import FixtureHandle
    from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver


@dataclass(frozen=True)
class RepairPassResult:
    """The outcome of one bounded repair pass over the control backlog."""

    repaired: int
    remaining_pending: int
    control_id: str
    pass_index: int


def run_control_repair(control: FixtureHandle, *, limit: int, pass_index: int) -> RepairPassResult:
    """Run one bounded global repair pass over the pending audits of a control pair."""
    resolved_limit = _require_limit(limit)
    resolved_index = _require_pass_index(pass_index)
    control_id = control.control_id
    if control_id is None:
        raise BindingIncompleteError({"field": "control_id"})
    # The product repair takes the storage ports; the fixture holds concrete drivers.
    repaired = repair_reconciliation_audit(
        cast(Any, control.graph),
        cast(Any, control.meta),
        limit=resolved_limit,
    )
    remaining = _pending_audit_count(control, floor=resolved_limit)
    return RepairPassResult(
        repaired=repaired,
        remaining_pending=remaining,
        control_id=control_id,
        pass_index=resolved_index,
    )


def assert_receipt_replay(
    graph: SqliteGraphDriver,
    application: ReconciliationApplication,
    downweight_factor: float | None,
) -> ReconciliationReceipt:
    """Replay one terminal application and compare it with the persisted receipt.

    The persisted receipt is read before the replay, so an application the
    store never recorded fails typed instead of being applied for the first
    time. Both compared receipts are decoded from the same persisted row, so
    the comparison is exact; a difference is a binding failure, never a silent
    success.
    """
    _require_persisted_receipt(graph, application)
    replayed = graph.apply_reconciliation(application, downweight_factor=downweight_factor)
    persisted = _require_persisted_receipt(graph, application)
    mismatches = _receipt_field_mismatches(replayed, persisted)
    if mismatches:
        mismatch_detail: dict[str, Any] = {
            "reason": "receipt_replay_mismatch",
            "nomination_id": application.nomination_id,
            "mismatch_fields": mismatches,
        }
        raise BindingIncompleteError(mismatch_detail)
    return replayed


def _require_persisted_receipt(
    graph: SqliteGraphDriver,
    application: ReconciliationApplication,
) -> ReconciliationReceipt:
    """The persisted receipt of one application, or a typed failure when absent."""
    receipt = _read_product_receipt(
        graph, profile_id=application.profile_id, nomination_id=application.nomination_id
    )
    if receipt is None:
        missing_detail: dict[str, Any] = {
            "reason": "receipt_missing",
            "nomination_id": application.nomination_id,
        }
        raise BindingIncompleteError(missing_detail)
    return receipt


def _pending_audit_count(control: FixtureHandle, *, floor: int) -> int:
    """The pending audit rows that survived a pass.

    The read bound is the number of persisted receipts of the control profiles:
    every receipt owns exactly one outbox row, so that count never truncates
    the pending scan.
    """
    bound = max(floor, _receipt_total(control))
    return len(control.graph.pending_reconciliation_audits(bound))


def _receipt_total(control: FixtureHandle) -> int:
    """The number of persisted receipts of the control profiles."""
    total = 0
    for profile_id in control.profile_ids:
        total += sum(control.graph.count_reconciliation_receipts(profile_id=profile_id).values())
    return total


def _receipt_field_mismatches(
    replayed: ReconciliationReceipt,
    persisted: ReconciliationReceipt,
) -> list[str]:
    """The receipt fields whose replayed and persisted values differ."""
    return [
        item.name
        for item in fields(replayed)
        if getattr(replayed, item.name) != getattr(persisted, item.name)
    ]


def _require_limit(value: object) -> int:
    """The cap of one repair pass: a non-negative integer, never a boolean."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BindingIncompleteError({"field": "limit"})
    return value


def _require_pass_index(value: object) -> int:
    """The ordinal of one repair pass: a positive integer, never a boolean."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BindingIncompleteError({"field": "pass_index"})
    return value


__all__ = [
    "NodeIntegritySnapshot",
    "RepairPassResult",
    "assert_node_integrity",
    "assert_receipt_replay",
    "capture_node_integrity",
    "run_control_repair",
]
