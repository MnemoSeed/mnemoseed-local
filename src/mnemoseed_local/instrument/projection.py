"""Pure visibility projection of persisted reconciliation receipts.

The projection buckets receipts by the age of their persisted ``applied_at``
against an explicitly supplied cutoff. It is a counterfactual visibility
layer: it never decides product expiry, never reads the wall clock, and never
touches the ledger or any product store.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from mnemoseed_local.storage.drivers._time import epoch_from_iso

UNRESOLVED_DISPOSITION = "unresolved"

EXPIRED_BUCKET = "expired"
RECENT_BUCKET = "recent"
UNCLASSIFIED_BUCKET = "unclassified"

# The bound source label is a local literal: this module imports no instrument sibling.
_BOUND_CLOCK_SOURCE = "os_utc_wall"


@dataclass(frozen=True)
class ProjectionInput:
    """One persisted receipt row as the read surface reports it."""

    nomination_id: str
    disposition: str
    reason_code: str
    applied_at_raw: str


@dataclass(frozen=True)
class BucketedReceipt:
    """One receipt in a visibility bucket with the diagnostics it shows."""

    nomination_id: str
    bucket: str
    diag_codes: tuple[str, ...]
    trust_codes: tuple[str, ...]


@dataclass(frozen=True)
class ProjectionResult:
    """The bucketed projection of one receipt set."""

    expired: tuple[BucketedReceipt, ...]
    recent: tuple[BucketedReceipt, ...]
    unclassified: tuple[BucketedReceipt, ...]
    filtered_out: tuple[str, ...]
    now_raw: str | None
    now_diag_code: str | None
    cutoff_raw: str | None
    cutoff_diag_code: str | None


def project(
    receipts: Sequence[ProjectionInput],
    *,
    now_raw: str | None,
    clock_source: str,
    cutoff_seconds: object,
) -> ProjectionResult:
    """Bucket the unresolved receipts of one read surface.

    ``now_raw`` buckets receipts only when ``clock_source`` names the bound OS
    UTC wall clock; an unbound source leaves every unresolved receipt
    unclassified with a visible diagnostic.
    """
    now_epoch, now_codes = _decode_now(now_raw, clock_source)
    now_diag_code = now_codes[0] if now_codes else None
    cutoff = _validated_cutoff(cutoff_seconds)
    cutoff_diag_code = None if cutoff is not None else "invalid_cutoff"

    global_codes: tuple[str, ...] = now_codes
    if cutoff_diag_code is not None:
        global_codes += (cutoff_diag_code,)

    expired: list[BucketedReceipt] = []
    recent: list[BucketedReceipt] = []
    unclassified: list[BucketedReceipt] = []
    filtered_out: list[str] = []

    for receipt in receipts:
        if receipt.disposition != UNRESOLVED_DISPOSITION:
            filtered_out.append(receipt.nomination_id)
            continue
        if now_epoch is None or cutoff is None:
            unclassified.append(_bucketed(receipt.nomination_id, UNCLASSIFIED_BUCKET, global_codes))
            continue
        applied_epoch, applied_diag_code = _decode_applied_at(receipt.applied_at_raw)
        if applied_epoch is None:
            unclassified.append(_bucketed(receipt.nomination_id, UNCLASSIFIED_BUCKET, (applied_diag_code,)))
            continue
        if applied_epoch > now_epoch:
            recent.append(_bucketed(receipt.nomination_id, RECENT_BUCKET, ("future_timestamp",)))
            continue
        age = now_epoch - applied_epoch
        if age >= cutoff:
            expired.append(_bucketed(receipt.nomination_id, EXPIRED_BUCKET))
        else:
            recent.append(_bucketed(receipt.nomination_id, RECENT_BUCKET))

    return ProjectionResult(
        expired=tuple(expired),
        recent=tuple(recent),
        unclassified=tuple(unclassified),
        filtered_out=tuple(filtered_out),
        now_raw=now_raw,
        now_diag_code=now_diag_code,
        cutoff_raw=None if cutoff is None else str(cutoff_seconds),
        cutoff_diag_code=cutoff_diag_code,
    )


def _bucketed(nomination_id: str, bucket: str, diag_codes: Sequence[str] = ()) -> BucketedReceipt:
    """Build one bucketed receipt carrying the given diagnostic codes."""
    return BucketedReceipt(nomination_id, bucket, tuple(diag_codes), ())


def _validated_cutoff(cutoff_seconds: object) -> float | None:
    """Return the cutoff in seconds, or None when it is not a finite positive number."""
    if isinstance(cutoff_seconds, bool) or not isinstance(cutoff_seconds, int | float):
        return None
    try:
        cutoff = float(cutoff_seconds)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(cutoff) or cutoff <= 0.0:
        return None
    return cutoff


def _decode_now(now_raw: str | None, clock_source: str) -> tuple[float | None, tuple[str, ...]]:
    """Return the usable now epoch and the diagnostic codes of a defective now.

    A defective value keeps ``invalid_now`` primary; an unbound source adds
    ``unbound_now`` as the remaining code.
    """
    source_codes = ("unbound_now",) if clock_source != _BOUND_CLOCK_SOURCE else ()
    if now_raw is None:
        return None, ("unbound_now",)
    try:
        epoch = float.fromhex(now_raw)
    except (TypeError, ValueError, OverflowError):
        return None, ("invalid_now",) + source_codes
    if not math.isfinite(epoch):
        return None, ("invalid_now",) + source_codes
    if source_codes:
        return None, source_codes
    return epoch, ()


def _decode_applied_at(applied_at_raw: str) -> tuple[float | None, str]:
    """Return the decoded epoch of a persisted ISO text and its failure code."""
    try:
        epoch = epoch_from_iso(applied_at_raw)
    except (TypeError, ValueError, OverflowError):
        return None, "unparsable"
    if not math.isfinite(epoch):
        return None, "non_finite"
    return epoch, ""
