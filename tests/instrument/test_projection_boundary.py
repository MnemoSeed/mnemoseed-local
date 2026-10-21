"""Projection boundary behaviour: bucket edges, self-filtering, and purity."""

from __future__ import annotations

import ast
import inspect
import sys
import time
from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path

import pytest

from mnemoseed_local.instrument import projection as projection_module
from mnemoseed_local.instrument.projection import (
    BucketedReceipt,
    ProjectionInput,
    ProjectionResult,
    project,
)
from mnemoseed_local.storage.drivers._time import epoch_from_iso, iso8601_utc

NOW_EPOCH = 1_700_000_000.0
NOW_RAW = float.hex(NOW_EPOCH)
UNRESOLVED = "unresolved"
THIRTY_DAYS_IN_SECONDS = 2592000.0
ALLOWED_PRODUCT_MODULES = frozenset(
    {
        "mnemoseed_local.storage.drivers",
        "mnemoseed_local.storage.drivers._time",
    }
)


def persisted(epoch: float) -> str:
    """Render the ISO text a persisted receipt row carries."""
    return iso8601_utc(epoch)


def receipt(
    nomination_id: str,
    applied_at_raw: str,
    *,
    disposition: str = UNRESOLVED,
    reason_code: str = "conflict_unresolved",
) -> ProjectionInput:
    return ProjectionInput(
        nomination_id=nomination_id,
        disposition=disposition,
        reason_code=reason_code,
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


def test_projection_public_shapes_are_frozen() -> None:
    assert [field.name for field in fields(ProjectionInput)] == [
        "nomination_id",
        "disposition",
        "reason_code",
        "applied_at_raw",
    ]
    assert [field.name for field in fields(BucketedReceipt)] == [
        "nomination_id",
        "bucket",
        "diag_codes",
        "trust_codes",
    ]
    assert [field.name for field in fields(ProjectionResult)] == [
        "expired",
        "recent",
        "unclassified",
        "filtered_out",
        "now_raw",
        "now_diag_code",
        "cutoff_raw",
        "cutoff_diag_code",
    ]


def test_projection_module_imports_only_stdlib_and_the_product_time_helper() -> None:
    tree = ast.parse(inspect.getsource(projection_module))

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import would reach an instrument sibling"
            imported.append(node.module or "")

    for name in imported:
        root = name.split(".")[0]
        assert root in sys.stdlib_module_names or name in ALLOWED_PRODUCT_MODULES, name


def test_age_equal_to_the_cutoff_is_expired() -> None:
    result = run([receipt("n-exact", persisted(NOW_EPOCH - 100.0))], cutoff_seconds=100)

    assert [entry.nomination_id for entry in result.expired] == ["n-exact"]
    assert result.recent == ()
    assert result.unclassified == ()
    assert result.expired[0].bucket == "expired"
    assert result.expired[0].diag_codes == ()
    assert result.expired[0].trust_codes == ()


def test_half_second_old_at_a_half_second_cutoff_is_expired() -> None:
    result = run([receipt("n-half", persisted(NOW_EPOCH - 0.5))], cutoff_seconds=0.5)

    assert [entry.nomination_id for entry in result.expired] == ["n-half"]
    assert result.recent == ()


def test_age_below_the_cutoff_is_recent() -> None:
    result = run([receipt("n-half", persisted(NOW_EPOCH - 0.5))], cutoff_seconds=1.0)

    assert [entry.nomination_id for entry in result.recent] == ["n-half"]
    assert result.expired == ()
    assert result.unclassified == ()


def test_age_above_the_cutoff_is_expired() -> None:
    result = run([receipt("n-old", persisted(NOW_EPOCH - 100.5))], cutoff_seconds=100)

    assert [entry.nomination_id for entry in result.expired] == ["n-old"]
    assert result.recent == ()


def test_zero_age_is_recent_and_not_flagged_as_future() -> None:
    result = run([receipt("n-now", persisted(NOW_EPOCH))], cutoff_seconds=0.5)

    assert [entry.nomination_id for entry in result.recent] == ["n-now"]
    assert "future_timestamp" not in result.recent[0].diag_codes


def test_applied_at_after_now_stays_recent_with_a_visible_diagnostic() -> None:
    result = run([receipt("n-future", persisted(NOW_EPOCH + 60.0))], cutoff_seconds=1.0)

    assert result.expired == ()
    assert [entry.nomination_id for entry in result.recent] == ["n-future"]
    assert "future_timestamp" in result.recent[0].diag_codes


def test_cutoff_is_measured_in_seconds() -> None:
    stale = [receipt("n-stale", persisted(NOW_EPOCH - 60.0))]

    by_seconds = run(stale, cutoff_seconds=30)
    by_days = run(stale, cutoff_seconds=THIRTY_DAYS_IN_SECONDS)

    assert [entry.nomination_id for entry in by_seconds.expired] == ["n-stale"]
    assert [entry.nomination_id for entry in by_days.recent] == ["n-stale"]


def test_only_unresolved_receipts_enter_the_buckets() -> None:
    receipts = [
        receipt("n-unresolved", persisted(NOW_EPOCH - 100.0)),
        receipt("n-accepted", persisted(NOW_EPOCH - 100.0), disposition="accepted"),
        receipt("n-rejected", persisted(NOW_EPOCH - 100.0), disposition="rejected"),
        receipt("n-deferred", persisted(NOW_EPOCH - 100.0), disposition="deferred"),
    ]

    result = run(receipts, cutoff_seconds=100)

    assert [entry.nomination_id for entry in result.expired] == ["n-unresolved"]
    assert result.recent == ()
    assert result.unclassified == ()
    assert all(
        any(nomination_id in entry for entry in result.filtered_out)
        for nomination_id in ("n-accepted", "n-rejected", "n-deferred")
    )
    assert not any("n-unresolved" in entry for entry in result.filtered_out)


def test_filtered_receipts_receive_neither_credit_nor_diagnostics() -> None:
    receipts = [
        receipt("n-accepted", "not-a-timestamp", disposition="accepted"),
        receipt("n-unresolved", persisted(NOW_EPOCH - 100.0)),
    ]

    result = run(receipts, cutoff_seconds=100)

    assert result.unclassified == ()
    assert [entry.nomination_id for entry in result.expired] == ["n-unresolved"]
    assert any("n-accepted" in entry for entry in result.filtered_out)


def test_protected_endpoint_receipts_still_age_into_the_buckets() -> None:
    result = run(
        [receipt("n-protected", persisted(NOW_EPOCH - 100.0), reason_code="protected_endpoint")],
        cutoff_seconds=100,
    )

    assert [entry.nomination_id for entry in result.expired] == ["n-protected"]
    assert result.unclassified == ()


def test_persisted_millisecond_text_is_decoded_verbatim() -> None:
    original = 1792472291.123
    text = persisted(original)
    decoded = epoch_from_iso(text)

    assert decoded != original

    result = run(
        [receipt("n-quantized", text)],
        now_raw=float.hex(decoded + 1000.0),
        cutoff_seconds=100.0,
    )

    assert [entry.nomination_id for entry in result.expired] == ["n-quantized"]
    assert result.unclassified == ()


def test_persisted_text_need_not_round_trip_through_the_serializer() -> None:
    text = "2023-11-14T22:13:20.9999Z"
    decoded = epoch_from_iso(text)

    assert decoded != 1700000000.9999

    result = run(
        [receipt("n-uncanonical", text)],
        now_raw=float.hex(decoded + 1000.0),
        cutoff_seconds=100.0,
    )

    assert [entry.nomination_id for entry in result.expired] == ["n-uncanonical"]
    assert result.unclassified == ()


def test_persisted_text_without_milliseconds_is_decoded() -> None:
    text = "2023-11-14T22:13:20Z"

    result = run([receipt("n-seconds", text)], now_raw=NOW_RAW, cutoff_seconds=0.5)

    assert [entry.nomination_id for entry in result.recent] == ["n-seconds"]
    assert result.unclassified == ()


def test_projection_ignores_the_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    receipts = [receipt("n-old", persisted(NOW_EPOCH - 100.0))]

    reference = run(receipts, cutoff_seconds=100)
    monkeypatch.setattr(time, "time", lambda: 4_102_444_800.0)

    assert run(receipts, cutoff_seconds=100) == reference


def test_projection_writes_nothing_to_the_isolated_home(isolated_home: Path) -> None:
    receipts = [receipt("n-old", persisted(NOW_EPOCH - 100.0))]

    run(receipts, cutoff_seconds=100)

    assert list(isolated_home.iterdir()) == []


def test_projection_does_not_mutate_its_inputs() -> None:
    receipts = [receipt("n-old", persisted(NOW_EPOCH - 100.0))]
    snapshot = list(receipts)

    run(receipts, cutoff_seconds=100)

    assert receipts == snapshot


def test_projection_is_deterministic_and_free_of_sticky_state() -> None:
    receipts = [receipt("n-old", persisted(NOW_EPOCH - 60.0))]

    narrow = run(receipts, cutoff_seconds=30.0)
    wide = run(receipts, cutoff_seconds=THIRTY_DAYS_IN_SECONDS)
    wide_again = run(receipts, cutoff_seconds=THIRTY_DAYS_IN_SECONDS)

    assert wide == wide_again
    assert [entry.nomination_id for entry in narrow.expired] == ["n-old"]
    assert [entry.nomination_id for entry in wide.recent] == ["n-old"]
