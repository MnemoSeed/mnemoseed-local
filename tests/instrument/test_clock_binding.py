"""Clock binding: real clock readings, seed clocks, and capture invariants.

Every observation is bound to the readings of the operating system clock. These
cases exercise the binding itself, the separation of the synthetic seed clock
from the real one, and the invariant check that classifies a capture.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from mnemoseed_local.instrument.clock import (
    CaptureCheck,
    CaptureSpan,
    ClockCapture,
    capture_span,
    check_capture_invariants,
    parse_raw,
    seed_capture,
)
from mnemoseed_local.instrument.ledger import CLOCK_SOURCES, ClockValueError
from mnemoseed_local.storage.drivers._time import iso8601_utc

WALL_PRE = 1_700_000_000.0
WALL_POST = 1_700_000_000.5
MONO_PRE = 900_000_000_000
MONO_POST = 900_000_000_250
COHORT_CREATED = 1_600_000_000.0
SEED_EPOCH = 1_700_006_400.5


class _ClockProbe:
    """A deterministic stand-in for the operating system clock readings."""

    def __init__(self) -> None:
        self.wall = WALL_PRE
        self.monotonic_ns = MONO_PRE

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> int:
        return self.monotonic_ns


def make_capture(
    *,
    pre: float,
    post: float,
    monotonic_delta_ns: int,
    clock_source: str = "os_utc_wall",
) -> ClockCapture:
    """Build one capture from explicit readings."""
    return ClockCapture(
        capture_pre_raw=float.hex(pre),
        capture_post_raw=float.hex(post),
        collected_at_raw=float.hex(pre),
        collected_at_iso=iso8601_utc(pre),
        monotonic_delta_ns=monotonic_delta_ns,
        clock_source=clock_source,
    )


def test_capture_span_binds_both_ends_to_the_clock_readings(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _ClockProbe()
    monkeypatch.setattr(time, "time", probe.time)
    monkeypatch.setattr(time, "monotonic_ns", probe.monotonic)

    span = capture_span()
    pre_raw = span.begin()
    probe.wall = WALL_POST
    probe.monotonic_ns = MONO_POST
    capture = span.end()

    assert isinstance(span, CaptureSpan)
    assert pre_raw == float.hex(WALL_PRE)
    assert capture.capture_pre_raw == float.hex(WALL_PRE)
    assert capture.capture_post_raw == float.hex(WALL_POST)
    assert capture.collected_at_raw == capture.capture_pre_raw
    assert capture.collected_at_raw == float.hex(WALL_PRE)
    assert capture.monotonic_delta_ns == MONO_POST - MONO_PRE
    assert capture.clock_source == "os_utc_wall"
    assert capture.collected_at_iso == iso8601_utc(WALL_PRE)


def test_every_span_reads_the_clock_again(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _ClockProbe()
    monkeypatch.setattr(time, "time", probe.time)
    monkeypatch.setattr(time, "monotonic_ns", probe.monotonic)

    first = capture_span()
    first_pre = first.begin()
    probe.wall = WALL_POST
    second = capture_span()
    second_pre = second.begin()

    assert first_pre == float.hex(WALL_PRE)
    assert second_pre == float.hex(WALL_POST)


def test_seed_capture_binds_the_explicit_epoch_and_the_seed_clock_source() -> None:
    capture = seed_capture(SEED_EPOCH)

    assert capture.capture_pre_raw == float.hex(SEED_EPOCH)
    assert capture.collected_at_raw == float.hex(SEED_EPOCH)
    assert capture.collected_at_iso == iso8601_utc(SEED_EPOCH)
    assert capture.clock_source == "seed_fixture"
    assert capture.clock_source in CLOCK_SOURCES
    assert capture.clock_source != "os_utc_wall"


def test_seed_capture_accepts_an_integer_epoch() -> None:
    capture = seed_capture(1_700_000_000)

    assert capture.collected_at_raw == float.hex(1_700_000_000.0)


@pytest.mark.parametrize(
    "epoch",
    [float("nan"), float("inf"), float("-inf"), True, False, "1700000000.0", None, [1700000000.0]],
    ids=["nan", "inf", "-inf", "true", "false", "text", "none", "list"],
)
def test_seed_capture_rejects_an_invalid_epoch(epoch: object) -> None:
    with pytest.raises(ClockValueError):
        seed_capture(epoch)


@pytest.mark.parametrize(
    "value",
    [0.0, 1.0, -1.5, 1_700_000_000.0, 1_700_006_400.5, 1_700_006_399.9995],
    ids=["zero", "one", "negative", "second-boundary", "half-second", "half-millisecond"],
)
def test_parse_raw_decodes_the_hex_raw_exactly(value: float) -> None:
    parsed = parse_raw(float.hex(value))

    assert parsed == value
    assert isinstance(parsed, float)


@pytest.mark.parametrize(
    "raw",
    ["", "not-a-raw", "1.0", "inf", "-inf", "nan", "0x1.8p+1z"],
    ids=["empty", "text", "decimal", "inf", "-inf", "nan", "bad-digits"],
)
def test_parse_raw_rejects_an_invalid_raw_shape(raw: str) -> None:
    with pytest.raises(ClockValueError):
        parse_raw(raw)


@pytest.mark.parametrize("raw", [1.5, None, b"0x1p+0"], ids=["float", "none", "bytes"])
def test_parse_raw_rejects_a_non_text_raw(raw: object) -> None:
    with pytest.raises(ClockValueError):
        parse_raw(raw)


def test_a_clock_capture_is_immutable() -> None:
    capture = make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000)

    with pytest.raises(AttributeError):
        capture.capture_pre_raw = float.hex(0.0)


def test_a_capture_check_is_immutable() -> None:
    check = check_capture_invariants(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000),
        prior_raw=None,
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert isinstance(check, CaptureCheck)
    with pytest.raises(AttributeError):
        check.diag_code = "clock_backward"


def test_a_bound_capture_reports_no_invalid_code() -> None:
    check = check_capture_invariants(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000),
        prior_raw=None,
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert check.diag_code is None
    assert check.invalid_codes == ()


def test_the_future_annotation_is_reported_as_a_trust_code() -> None:
    check = check_capture_invariants(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000),
        prior_raw=None,
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert "clock_future_unverifiable" in check.trust_codes
    assert check.diag_code is None
    assert check.invalid_codes == ()


def test_a_capture_before_the_prior_instant_is_backward() -> None:
    # the prior floor also carries the raw instant left by an earlier failed attempt
    check = check_capture_invariants(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000),
        prior_raw=float.hex(WALL_PRE + 10.0),
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert check.diag_code == "clock_backward"
    assert set(check.invalid_codes) == {"clock_backward"}


def test_a_capture_equal_to_the_prior_instant_is_not_backward() -> None:
    check = check_capture_invariants(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000),
        prior_raw=float.hex(WALL_PRE),
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert check.diag_code is None
    assert check.invalid_codes == ()


def test_a_capture_before_the_cohort_creation_is_precohort() -> None:
    check = check_capture_invariants(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000),
        prior_raw=None,
        cohort_created_at_raw=float.hex(WALL_PRE + 10.0),
    )

    assert check.diag_code == "clock_precohort"
    assert set(check.invalid_codes) == {"clock_precohort"}


def test_a_capture_equal_to_the_cohort_creation_is_not_precohort() -> None:
    check = check_capture_invariants(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000),
        prior_raw=None,
        cohort_created_at_raw=float.hex(WALL_PRE),
    )

    assert check.diag_code is None
    assert check.invalid_codes == ()


@pytest.mark.parametrize(
    ("post", "monotonic_delta_ns"),
    [
        (WALL_POST, -1),
        (WALL_PRE - 1e-6, 1_000),
    ],
    ids=["negative-monotonic", "backward-wall"],
)
def test_a_capture_with_inconsistent_readings_is_monotonic_inconsistent(
    post: float, monotonic_delta_ns: int
) -> None:
    check = check_capture_invariants(
        make_capture(pre=WALL_PRE, post=post, monotonic_delta_ns=monotonic_delta_ns),
        prior_raw=None,
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert check.diag_code == "clock_monotonic_inconsistent"
    assert set(check.invalid_codes) == {"clock_monotonic_inconsistent"}


def test_a_capture_without_a_bound_clock_source_is_unbound() -> None:
    check = check_capture_invariants(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000, clock_source="unbound"),
        prior_raw=None,
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert check.diag_code == "clock_unbound"
    assert set(check.invalid_codes) == {"clock_unbound"}


def test_the_first_failing_code_is_reported_and_every_failure_is_kept() -> None:
    check = check_capture_invariants(
        make_capture(
            pre=WALL_PRE,
            post=WALL_PRE - 1.0,
            monotonic_delta_ns=250,
            clock_source="unbound",
        ),
        prior_raw=float.hex(WALL_PRE + 10.0),
        cohort_created_at_raw=float.hex(WALL_PRE + 10.0),
    )

    assert check.diag_code == "clock_backward"
    assert set(check.invalid_codes) == {
        "clock_backward",
        "clock_precohort",
        "clock_monotonic_inconsistent",
        "clock_unbound",
    }
    assert check.invalid_codes[0] == "clock_backward"
    assert "clock_backward" in check.invalid_codes


def test_the_precohort_code_precedes_the_monotonic_and_unbound_codes() -> None:
    check = check_capture_invariants(
        make_capture(
            pre=WALL_PRE,
            post=WALL_PRE - 1.0,
            monotonic_delta_ns=250,
            clock_source="unbound",
        ),
        prior_raw=float.hex(WALL_PRE - 10.0),
        cohort_created_at_raw=float.hex(WALL_PRE + 10.0),
    )

    assert check.diag_code == "clock_precohort"
    assert set(check.invalid_codes) == {
        "clock_precohort",
        "clock_monotonic_inconsistent",
        "clock_unbound",
    }


def test_the_monotonic_code_precedes_the_unbound_code() -> None:
    check = check_capture_invariants(
        make_capture(
            pre=WALL_PRE,
            post=WALL_PRE - 1.0,
            monotonic_delta_ns=250,
            clock_source="unbound",
        ),
        prior_raw=None,
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert check.diag_code == "clock_monotonic_inconsistent"
    assert set(check.invalid_codes) == {"clock_monotonic_inconsistent", "clock_unbound"}


def test_the_future_annotation_never_invalidates_a_capture() -> None:
    check = check_capture_invariants(
        make_capture(
            pre=WALL_PRE,
            post=WALL_PRE - 1.0,
            monotonic_delta_ns=250,
            clock_source="unbound",
        ),
        prior_raw=float.hex(WALL_PRE + 10.0),
        cohort_created_at_raw=float.hex(WALL_PRE + 10.0),
    )

    assert "clock_future_unverifiable" not in check.invalid_codes
    assert check.diag_code != "clock_future_unverifiable"


def test_a_coherent_capture_keeps_its_collected_binding_and_its_empty_verdict() -> None:
    capture = make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000)

    assert capture.collected_at_raw == capture.capture_pre_raw

    check = check_capture_invariants(
        capture,
        prior_raw=None,
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert check.diag_code is None
    assert check.invalid_codes == ()
    assert "clock_future_unverifiable" in check.trust_codes


def test_a_seed_capture_keeps_its_collected_binding_and_stays_unbound() -> None:
    capture = seed_capture(SEED_EPOCH)

    assert capture.collected_at_raw == capture.capture_pre_raw

    check = check_capture_invariants(
        capture,
        prior_raw=None,
        cohort_created_at_raw=float.hex(COHORT_CREATED),
    )

    assert check.diag_code == "clock_unbound"
    assert set(check.invalid_codes) == {"clock_unbound"}


@pytest.mark.parametrize(
    "collected_raw",
    [
        "garbage",
        "",
        "nan",
        "inf",
        "1.0",
        "0x1.8p+1z",
        "0x1p+1024",
        "-0x1p+1024",
        True,
        False,
        None,
        1.5,
        b"0x1p+0",
    ],
    ids=[
        "text",
        "empty",
        "nan-text",
        "inf-text",
        "decimal",
        "bad-digits",
        "overflow",
        "negative-overflow",
        "true",
        "false",
        "none",
        "float",
        "bytes",
    ],
)
def test_a_capture_with_an_unusable_collected_raw_is_rejected(collected_raw: object) -> None:
    capture = replace(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000),
        collected_at_raw=collected_raw,
    )

    with pytest.raises(ClockValueError) as decode_error:
        parse_raw(collected_raw)
    with pytest.raises(ClockValueError) as check_error:
        check_capture_invariants(
            capture,
            prior_raw=None,
            cohort_created_at_raw=float.hex(COHORT_CREATED),
        )

    assert check_error.value.diag_code == decode_error.value.diag_code
    assert check_error.value.diag_code in {"unparsable", "non_finite"}


@pytest.mark.parametrize(
    "collected_raw",
    [float.hex(WALL_PRE + 1.0), float.hex(WALL_PRE + 86_400.0), float.hex(WALL_PRE - 1.0)],
    ids=["one-second-later", "one-day-later", "one-second-earlier"],
)
def test_a_capture_whose_collected_raw_differs_from_the_capture_pre_is_binding_incomplete(
    collected_raw: str,
) -> None:
    capture = replace(
        make_capture(pre=WALL_PRE, post=WALL_POST, monotonic_delta_ns=1_000),
        collected_at_raw=collected_raw,
    )

    with pytest.raises(ClockValueError) as error:
        check_capture_invariants(
            capture,
            prior_raw=None,
            cohort_created_at_raw=float.hex(COHORT_CREATED),
        )

    assert error.value.diag_code == "binding_incomplete"


def test_the_four_clock_codes_are_kept_on_a_capture_whose_collected_raw_is_incoherent() -> None:
    capture = replace(
        make_capture(
            pre=WALL_PRE,
            post=WALL_PRE - 1.0,
            monotonic_delta_ns=250,
            clock_source="unbound",
        ),
        collected_at_raw=float.hex(WALL_PRE + 10.0),
    )

    with pytest.raises(ClockValueError) as error:
        check_capture_invariants(
            capture,
            prior_raw=float.hex(WALL_PRE + 10.0),
            cohort_created_at_raw=float.hex(WALL_PRE + 10.0),
        )

    assert error.value.diag_code == "binding_incomplete"
    assert error.value.invalid_codes == (
        "clock_backward",
        "clock_precohort",
        "clock_monotonic_inconsistent",
        "clock_unbound",
    )
