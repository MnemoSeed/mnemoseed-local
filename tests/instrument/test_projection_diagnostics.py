"""Projection diagnostics: cutoff and clock validation, and the visible diagnostic codes."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from mnemoseed_local.instrument.projection import ProjectionInput, ProjectionResult, project
from mnemoseed_local.storage.drivers._time import iso8601_utc

NOW_EPOCH = 1_700_000_000.0
NOW_RAW = float.hex(NOW_EPOCH)
OLD_ISO = iso8601_utc(NOW_EPOCH - 100.0)
HUNDRED_DAYS_ISO = iso8601_utc(NOW_EPOCH - 100 * 86400.0)
THIRTY_DAYS_IN_SECONDS = 2592000.0


def receipt(
    nomination_id: str,
    applied_at_raw: str,
    *,
    disposition: str = "unresolved",
) -> ProjectionInput:
    return ProjectionInput(
        nomination_id=nomination_id,
        disposition=disposition,
        reason_code="conflict_unresolved",
        applied_at_raw=applied_at_raw,
    )


def run(
    receipts: Sequence[ProjectionInput],
    *,
    now_raw: str | None = NOW_RAW,
    clock_source: str = "os_utc_wall",
    cutoff_seconds: object = 100.0,
) -> ProjectionResult:
    return project(
        receipts,
        now_raw=now_raw,
        clock_source=clock_source,
        cutoff_seconds=cutoff_seconds,
    )


@pytest.mark.parametrize(
    "cutoff",
    [
        None,
        "100",
        0,
        -0.0,
        -1,
        -0.5,
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        10**400,
        Decimal("30"),
        complex(1, 0),
    ],
    ids=[
        "none",
        "string",
        "zero",
        "negative_zero",
        "negative_int",
        "negative_fraction",
        "true",
        "false",
        "nan",
        "inf",
        "neg_inf",
        "huge_int",
        "decimal",
        "complex",
    ],
)
def test_invalid_cutoff_is_reported_and_grants_no_credit(cutoff: object) -> None:
    result = run([receipt("n-old", OLD_ISO)], cutoff_seconds=cutoff)

    assert result.cutoff_diag_code == "invalid_cutoff"
    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-old"]


@pytest.mark.parametrize(
    "cutoff",
    [1, 100, 0.5, 5e-324, 1e300],
    ids=["int", "int_seconds", "half_second", "smallest_subnormal", "large_float"],
)
def test_finite_positive_cutoff_is_accepted(cutoff: float) -> None:
    result = run([receipt("n-old", OLD_ISO)], cutoff_seconds=cutoff)

    assert result.cutoff_diag_code is None
    assert result.unclassified == ()
    assert len(result.expired) + len(result.recent) == 1


def test_missing_cutoff_has_no_default_fallback() -> None:
    without_cutoff = run([receipt("n-ancient", HUNDRED_DAYS_ISO)], cutoff_seconds=None)
    with_explicit_cutoff = run(
        [receipt("n-ancient", HUNDRED_DAYS_ISO)],
        cutoff_seconds=THIRTY_DAYS_IN_SECONDS,
    )

    assert without_cutoff.cutoff_diag_code == "invalid_cutoff"
    assert without_cutoff.expired == ()
    assert [entry.nomination_id for entry in without_cutoff.unclassified] == ["n-ancient"]
    assert [entry.nomination_id for entry in with_explicit_cutoff.expired] == ["n-ancient"]


def test_unbound_now_is_reported_as_unbound() -> None:
    result = run([receipt("n-old", OLD_ISO)], now_raw=None)

    assert result.now_diag_code == "unbound_now"
    assert result.cutoff_diag_code is None
    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-old"]


@pytest.mark.parametrize("now_raw", ["inf", "nan"], ids=["infinite", "not_a_number"])
def test_non_finite_now_is_reported_as_invalid(now_raw: str) -> None:
    result = run([receipt("n-old", OLD_ISO)], now_raw=now_raw)

    assert result.now_diag_code == "invalid_now"
    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-old"]


def test_unparsable_now_is_reported_as_invalid() -> None:
    result = run([receipt("n-old", OLD_ISO)], now_raw="not-a-clock-value")

    assert result.now_diag_code == "invalid_now"
    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-old"]


def test_healthy_projection_carries_no_diagnostic_codes() -> None:
    result = run([receipt("n-old", OLD_ISO)], cutoff_seconds=100)

    assert result.now_diag_code is None
    assert result.cutoff_diag_code is None
    assert result.unclassified == ()


def test_applied_at_must_be_persisted_iso_text_and_not_float_hex() -> None:
    result = run([receipt("n-hex", NOW_RAW)], cutoff_seconds=100)

    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-hex"]
    assert "unparsable" in result.unclassified[0].diag_codes


@pytest.mark.parametrize(
    "applied_at_raw",
    ["not-a-timestamp", "2023-13-45T99:99:99.000Z", ""],
    ids=["garbage", "malformed_iso", "empty"],
)
def test_unparsable_applied_at_is_unclassified_with_a_visible_code(applied_at_raw: str) -> None:
    result = run([receipt("n-bad", applied_at_raw)], cutoff_seconds=100)

    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-bad"]
    assert "unparsable" in result.unclassified[0].diag_codes


def test_out_of_range_applied_at_is_unclassified_without_crashing() -> None:
    applied_at_raw = "2023-11-14T22:13:20." + "9" * 400 + "Z"

    result = run([receipt("n-overflow", applied_at_raw)], cutoff_seconds=100)

    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-overflow"]
    assert result.unclassified[0].diag_codes
    assert set(result.unclassified[0].diag_codes) <= {"unparsable", "non_finite"}


def test_filtered_receipts_never_produce_diagnostics() -> None:
    result = run(
        [receipt("n-accepted", "not-a-timestamp", disposition="accepted")],
        cutoff_seconds=100,
    )

    assert result.unclassified == ()
    assert result.expired == ()
    assert result.recent == ()
    assert "n-accepted" in result.filtered_out


RECENT_ISO = iso8601_utc(NOW_EPOCH - 50.0)
FUTURE_ISO = iso8601_utc(NOW_EPOCH + 50.0)


@pytest.mark.parametrize(
    "clock_source",
    ["seed_fixture", "unbound", "unknown-source", ""],
    ids=["seed_fixture", "unbound", "unknown_source", "empty_source"],
)
def test_a_finite_now_from_an_unbound_source_is_reported_as_unbound(clock_source: str) -> None:
    result = run(
        [receipt("n-old", OLD_ISO), receipt("n-accepted", OLD_ISO, disposition="accepted")],
        clock_source=clock_source,
        cutoff_seconds=100,
    )

    assert result.now_diag_code == "unbound_now"
    assert result.cutoff_diag_code is None
    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-old"]
    assert result.unclassified[0].diag_codes == ("unbound_now",)
    assert result.unclassified[0].trust_codes == ()
    assert result.filtered_out == ("n-accepted",)


def test_an_unbound_source_leaves_every_unresolved_receipt_unclassified() -> None:
    receipts = [
        receipt("n-old", OLD_ISO),
        receipt("n-recent", RECENT_ISO),
        receipt("n-future", FUTURE_ISO),
        receipt("n-accepted", OLD_ISO, disposition="accepted"),
    ]

    result = run(receipts, clock_source="seed_fixture", cutoff_seconds=100)

    assert result.now_diag_code == "unbound_now"
    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-old", "n-recent", "n-future"]
    assert all("unbound_now" in entry.diag_codes for entry in result.unclassified)
    assert result.filtered_out == ("n-accepted",)


def test_the_bound_os_source_with_the_same_finite_now_still_buckets_receipts() -> None:
    receipts = [receipt("n-old", OLD_ISO), receipt("n-recent", RECENT_ISO)]

    result = run(receipts, clock_source="os_utc_wall", cutoff_seconds=100)

    assert result.now_diag_code is None
    assert result.cutoff_diag_code is None
    assert [entry.nomination_id for entry in result.expired] == ["n-old"]
    assert [entry.nomination_id for entry in result.recent] == ["n-recent"]
    assert result.unclassified == ()
    assert result.expired[0].diag_codes == ()
    assert result.recent[0].diag_codes == ()


@pytest.mark.parametrize(
    "now_raw",
    ["not-a-clock-value", "inf", "nan"],
    ids=["unparsable", "infinite", "not_a_number"],
)
def test_a_defective_now_keeps_the_value_code_primary_under_an_unbound_source(now_raw: str) -> None:
    result = run(
        [receipt("n-old", OLD_ISO)],
        now_raw=now_raw,
        clock_source="seed_fixture",
        cutoff_seconds=100,
    )

    assert result.now_diag_code == "invalid_now"
    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-old"]
    assert result.unclassified[0].diag_codes == ("invalid_now", "unbound_now")
    assert result.unclassified[0].trust_codes == ()


def test_an_absent_now_stays_unbound_under_an_unbound_source() -> None:
    result = run(
        [receipt("n-old", OLD_ISO)],
        now_raw=None,
        clock_source="seed_fixture",
        cutoff_seconds=100,
    )

    assert result.now_diag_code == "unbound_now"
    assert result.expired == ()
    assert result.recent == ()
    assert [entry.nomination_id for entry in result.unclassified] == ["n-old"]
    assert set(result.unclassified[0].diag_codes) == {"unbound_now"}
