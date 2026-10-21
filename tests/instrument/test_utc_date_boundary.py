"""UTC date derivation at day boundaries and exact millisecond fixtures.

The date of an observation is derived from the raw epoch of the capture, never
from a stored display text. These cases pin the floor at the millisecond, the
day boundary, and the independence from the local timezone.
"""

from __future__ import annotations

import datetime
import re
import time

import pytest

from mnemoseed_local.instrument.clock import ClockCapture, utc_date_from_raw
from mnemoseed_local.instrument.ledger import ClockValueError
from mnemoseed_local.storage.drivers._time import epoch_from_iso, iso8601_utc

UTC_MIDNIGHT = 1_700_006_400.0
PREVIOUS_DAY = "2023-11-14"
CURRENT_DAY = "2023-11-15"
EPOCH_DAY = "1970-01-01"


def utc_date_oracle(epoch: float) -> str:
    """Return the UTC calendar date of an epoch, computed independently."""
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC).strftime("%Y-%m-%d")


def test_the_midnight_constant_is_the_utc_day_boundary() -> None:
    boundary = datetime.datetime(2023, 11, 15, tzinfo=datetime.UTC)

    assert boundary.timestamp() == UTC_MIDNIGHT


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [
        (UTC_MIDNIGHT, CURRENT_DAY),
        (UTC_MIDNIGHT - 0.0001, PREVIOUS_DAY),
        (UTC_MIDNIGHT - 0.0005, PREVIOUS_DAY),
        (UTC_MIDNIGHT + 0.0001, CURRENT_DAY),
        (UTC_MIDNIGHT - 0.5, PREVIOUS_DAY),
        (UTC_MIDNIGHT + 0.5, CURRENT_DAY),
        (UTC_MIDNIGHT - 1.0, PREVIOUS_DAY),
        (0.0, EPOCH_DAY),
    ],
    ids=[
        "midnight",
        "tenth-ms-before",
        "half-ms-before",
        "tenth-ms-after",
        "half-second-before",
        "half-second-after",
        "one-second-before",
        "epoch",
    ],
)
def test_the_date_follows_the_floored_millisecond_of_the_raw_epoch(epoch: float, expected: str) -> None:
    assert utc_date_from_raw(float.hex(epoch)) == expected


@pytest.mark.parametrize(
    "epoch",
    [0.0, 1_700_000_000.0, UTC_MIDNIGHT - 0.0001, UTC_MIDNIGHT, UTC_MIDNIGHT + 0.5],
    ids=["epoch", "second-boundary", "before-midnight", "midnight", "after-midnight"],
)
def test_the_date_matches_an_independent_utc_calendar(epoch: float) -> None:
    assert utc_date_from_raw(float.hex(epoch)) == utc_date_oracle(epoch)


def test_the_date_is_never_read_from_the_local_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    def _reject(*args: object, **kwargs: object) -> None:
        raise AssertionError("the UTC date must not be derived from the local timezone")

    monkeypatch.setattr(time, "localtime", _reject)

    assert utc_date_from_raw(float.hex(UTC_MIDNIGHT)) == CURRENT_DAY
    assert utc_date_from_raw(float.hex(UTC_MIDNIGHT - 0.0001)) == PREVIOUS_DAY


def test_the_date_has_the_ledger_date_shape() -> None:
    derived = utc_date_from_raw(float.hex(UTC_MIDNIGHT))

    assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", derived)


@pytest.mark.parametrize(
    ("epoch", "stamp"),
    [
        (UTC_MIDNIGHT, "2023-11-15T00:00:00.000Z"),
        (UTC_MIDNIGHT + 0.5, "2023-11-15T00:00:00.500Z"),
    ],
    ids=["whole-millisecond", "half-millisecond"],
)
def test_an_exact_millisecond_fixture_keeps_its_display_timestamp(epoch: float, stamp: str) -> None:
    assert iso8601_utc(epoch) == stamp
    assert epoch_from_iso(stamp) == epoch
    assert utc_date_from_raw(float.hex(epoch)) == stamp[:10]


def test_a_capture_across_utc_midnight_records_only_its_starting_day() -> None:
    capture = ClockCapture(
        capture_pre_raw=float.hex(UTC_MIDNIGHT - 0.0001),
        capture_post_raw=float.hex(UTC_MIDNIGHT + 1.0),
        collected_at_raw=float.hex(UTC_MIDNIGHT - 0.0001),
        collected_at_iso=iso8601_utc(UTC_MIDNIGHT - 0.0001),
        monotonic_delta_ns=1_000_000,
        clock_source="os_utc_wall",
    )

    assert utc_date_from_raw(capture.collected_at_raw) == PREVIOUS_DAY


@pytest.mark.parametrize(
    "raw",
    ["", "not-a-raw", "1.0", "inf", "nan"],
    ids=["empty", "text", "decimal", "inf", "nan"],
)
def test_the_date_rejects_an_unparsable_raw(raw: str) -> None:
    with pytest.raises(ClockValueError):
        utc_date_from_raw(raw)
