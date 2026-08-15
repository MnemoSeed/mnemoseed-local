"""Timestamp boundary helpers for SQLite drivers.

All storage temporal columns are ISO8601 UTC text; the model layer exchanges
epoch floats. The two convert at the driver's read/write boundary.
"""

from __future__ import annotations

import calendar
import time

_FORMAT = "%Y-%m-%dT%H:%M:%S"
_ZSUFFIX = "Z"


def iso8601_utc(epoch: float) -> str:
    """Render an epoch float as an ISO8601 UTC timestamp, e.g. 2026-08-08T01:02:03.456Z."""
    secs = int(epoch)
    millis = int(round((epoch - secs) * 1000.0))
    if millis >= 1000:
        secs += 1
        millis = 0
    if millis < 0:
        secs -= 1
        millis = 999
    stamp = time.strftime(_FORMAT, time.gmtime(secs))
    return f"{stamp}.{millis:03d}{_ZSUFFIX}"


def epoch_from_iso(value: str) -> float:
    """Parse an ISO8601 UTC timestamp back to an epoch float."""
    body = value[:-1] if value.endswith(_ZSUFFIX) else value
    base, _, frac = body.partition(".")
    parts = time.strptime(base, _FORMAT)
    millis = int(frac or "0")
    return calendar.timegm(parts) + millis / 1000.0
