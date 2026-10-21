"""Clock capture: bracketed host readings, raw instants, and capture invariants.

An observation is bracketed by two readings of the host clock. The capture keeps
the raw hexadecimal text of both wall readings, the monotonic delta between them,
and the derived display timestamp. The invariant check classifies one capture
into its failing codes and its trust codes.

The raw text is the canonical ``float.hex()`` form. It is decoded independently
of the display timestamp, and the UTC date is derived from the raw instant at the
floored millisecond, never from a display text round trip. The collected instant
is bound to the pre instant of its own capture: a capture whose collected raw is
unusable, or whose collected instant differs from the pre instant, is refused.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from mnemoseed_local.instrument.ledger import ClockValueError
from mnemoseed_local.storage.drivers._time import iso8601_utc

BOUND_CLOCK_SOURCE = "os_utc_wall"
SEED_CLOCK_SOURCE = "seed_fixture"

TRUST_CODE_FUTURE = "clock_future_unverifiable"

_RAW_PATTERN = re.compile(r"[+-]?0x[0-9a-f]+(?:\.[0-9a-f]*)?p[+-][0-9]+")
_UTC_DATE_FORMAT = "%Y-%m-%d"


@dataclass(frozen=True)
class ClockCapture:
    """One bracketed capture: the raw wall instants, the monotonic delta, and its source."""

    capture_pre_raw: str
    capture_post_raw: str
    collected_at_raw: str
    collected_at_iso: str
    monotonic_delta_ns: int
    clock_source: str


@dataclass(frozen=True)
class CaptureCheck:
    """The classification of one capture: its primary failure, its failures, and its trust codes."""

    diag_code: str | None
    invalid_codes: tuple[str, ...]
    trust_codes: tuple[str, ...]


class CaptureSpan:
    """A capture in progress: the pre reading is taken first, the post reading last."""

    def __init__(self) -> None:
        self._pre_raw: str | None = None
        self._pre_epoch: float | None = None
        self._monotonic_pre_ns: int | None = None

    def begin(self) -> str:
        """Read the pre instant of the capture and return its raw text."""
        wall = time.time()
        monotonic = time.monotonic_ns()
        self._pre_epoch = wall
        self._pre_raw = float.hex(wall)
        self._monotonic_pre_ns = monotonic
        return self._pre_raw

    def end(self) -> ClockCapture:
        """Read the post instant and return the whole capture."""
        if self._pre_raw is None or self._pre_epoch is None or self._monotonic_pre_ns is None:
            raise ClockValueError(
                {"reason": "capture_span_not_started"},
                diag_code="binding_incomplete",
            )
        wall_post = time.time()
        monotonic_post = time.monotonic_ns()
        return ClockCapture(
            capture_pre_raw=self._pre_raw,
            capture_post_raw=float.hex(wall_post),
            collected_at_raw=self._pre_raw,
            collected_at_iso=_display_iso(self._pre_epoch),
            monotonic_delta_ns=monotonic_post - self._monotonic_pre_ns,
            clock_source=BOUND_CLOCK_SOURCE,
        )


def capture_span() -> CaptureSpan:
    """Return a capture span that reads the clock at its own begin and its own end."""
    return CaptureSpan()


def seed_capture(epoch: float) -> ClockCapture:
    """Return the capture of a synthetic fixture clock at the given epoch."""
    value = _finite_epoch(epoch)
    raw = float.hex(value)
    return ClockCapture(
        capture_pre_raw=raw,
        capture_post_raw=raw,
        collected_at_raw=raw,
        collected_at_iso=_display_iso(value),
        monotonic_delta_ns=0,
        clock_source=SEED_CLOCK_SOURCE,
    )


def parse_raw(raw: str) -> float:
    """Decode one raw instant from its hexadecimal text."""
    return _decode_raw(raw)


def utc_date_from_raw(raw: str) -> str:
    """Return the UTC calendar date of the raw instant, floored at the millisecond."""
    seconds, _ = _floor_millisecond(_decode_raw(raw))
    try:
        return time.strftime(_UTC_DATE_FORMAT, time.gmtime(seconds))
    except (OverflowError, OSError, ValueError) as exc:
        raise ClockValueError(
            {"reason": "utc_date_out_of_range"},
            diag_code="unparsable",
        ) from exc


def check_capture_invariants(
    capture: ClockCapture, *, prior_raw: str | None, cohort_created_at_raw: str
) -> CaptureCheck:
    """Classify one capture against its prior instant, its cohort, and its own readings."""
    pre = _decode_raw(capture.capture_pre_raw)
    post = _decode_raw(capture.capture_post_raw)
    cohort_created = _decode_raw(cohort_created_at_raw)
    prior = _decode_raw(prior_raw) if prior_raw is not None else None

    invalid_codes: list[str] = []
    if prior is not None and pre < prior:
        invalid_codes.append("clock_backward")
    if pre < cohort_created:
        invalid_codes.append("clock_precohort")
    if not _monotonic_consistent(capture.monotonic_delta_ns, post - pre):
        invalid_codes.append("clock_monotonic_inconsistent")
    if capture.clock_source != BOUND_CLOCK_SOURCE:
        invalid_codes.append("clock_unbound")
    _check_collected_binding(capture, pre=pre, invalid_codes=invalid_codes)
    return CaptureCheck(
        diag_code=invalid_codes[0] if invalid_codes else None,
        invalid_codes=tuple(invalid_codes),
        trust_codes=(TRUST_CODE_FUTURE,),
    )


def _decode_raw(raw: object) -> float:
    """Decode a raw instant, rejecting any text that is not a hexadecimal instant."""
    if not isinstance(raw, str):
        raise ClockValueError({"reason": "raw_not_text"}, diag_code="unparsable")
    if not _RAW_PATTERN.fullmatch(raw):
        raise ClockValueError({"reason": "raw_shape_invalid"}, diag_code="unparsable")
    try:
        value = float.fromhex(raw)
    except (OverflowError, ValueError) as exc:
        raise ClockValueError({"reason": "raw_unparsable"}, diag_code="unparsable") from exc
    if not math.isfinite(value):
        raise ClockValueError({"reason": "raw_not_finite"}, diag_code="non_finite")
    return value


def _check_collected_binding(capture: ClockCapture, *, pre: float, invalid_codes: list[str]) -> None:
    """Bind the collected instant of a capture to the pre instant of the same capture."""
    try:
        collected = _decode_raw(capture.collected_at_raw)
    except ClockValueError as exc:
        raise ClockValueError(
            exc.detail,
            diag_code=exc.diag_code,
            invalid_codes=tuple(invalid_codes),
        ) from exc
    if collected != pre:
        raise ClockValueError(
            {
                "reason": "collected_at_raw_not_capture_pre",
                "collected_at_raw": capture.collected_at_raw,
                "capture_pre_raw": capture.capture_pre_raw,
            },
            diag_code="binding_incomplete",
            invalid_codes=tuple(invalid_codes),
        )


def _finite_epoch(epoch: object) -> float:
    """Return the epoch as a finite float, rejecting any non-numeric or non-finite value."""
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        raise ClockValueError({"reason": "epoch_not_a_number"}, diag_code="unparsable")
    try:
        value = float(epoch)
    except (OverflowError, ValueError) as exc:
        raise ClockValueError({"reason": "epoch_not_finite"}, diag_code="non_finite") from exc
    if not math.isfinite(value):
        raise ClockValueError({"reason": "epoch_not_finite"}, diag_code="non_finite")
    return value


def _display_iso(epoch: float) -> str:
    """Render the display timestamp of an epoch through the product helper."""
    try:
        return iso8601_utc(epoch)
    except (OverflowError, OSError, ValueError) as exc:
        raise ClockValueError(
            {"reason": "epoch_out_of_display_range"},
            diag_code="unparsable",
        ) from exc


def _floor_millisecond(epoch: float) -> tuple[int, int]:
    """Return the second and the millisecond of an epoch, floored at the millisecond."""
    seconds = math.floor(epoch)
    millis = math.floor((epoch - seconds) * 1000.0)
    if millis >= 1000:
        seconds += 1
        millis = 0
    return seconds, millis


def _monotonic_consistent(monotonic_delta_ns: int, wall_delta: float) -> bool:
    """Return whether the two readings of one capture moved in the same direction."""
    if isinstance(monotonic_delta_ns, bool) or not isinstance(monotonic_delta_ns, int):
        return False
    return monotonic_delta_ns >= 0 and wall_delta >= 0
