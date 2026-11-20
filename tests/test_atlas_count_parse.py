"""Behavioral oracle for the Atlas DOM count reader and its assertions."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "atlas_verify.py"


def load_harness():
    spec = importlib.util.spec_from_file_location("atlas_count_harness", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePage:
    """Scripted page: wait always settles, evaluate replays texts then holds the last."""

    def __init__(self, *texts: str) -> None:
        self.texts = texts
        self.index = 0
        self.evaluate_calls = 0
        self.wait_calls = 0

    def wait_for_function(self, script: str, *, timeout: int | None = None) -> None:
        self.wait_calls += 1

    def evaluate(self, script: str) -> str:
        self.evaluate_calls += 1
        if self.index < len(self.texts):
            value = self.texts[self.index]
            self.index += 1
            return value
        return self.texts[-1] if self.texts else ""


TRIPLE_EMPTY = "0 items · Rescue 0 · Fading 0"
TRIPLE_FILLED = "10 items · Rescue 0 · Fading 0"
EM_DASH = "\u2014"


def test_parse_counts_reads_all_three_tokens_as_integers() -> None:
    verify = load_harness()
    assert verify.parse_counts(TRIPLE_EMPTY) == [0, 0, 0]
    assert verify.parse_counts(TRIPLE_FILLED) == [10, 0, 0]


def test_parse_counts_returns_none_for_no_digit_text() -> None:
    verify = load_harness()
    for text in (EM_DASH, "…", "", "Loading your memories...", "\ufffd", "no counts here"):
        assert verify.parse_counts(text) is None


def test_ten_items_is_not_mistaken_for_zero_items() -> None:
    verify = load_harness()
    assert "0 items" in TRIPLE_FILLED
    assert verify.parse_counts(TRIPLE_FILLED) != [0, 0, 0]
    assert verify.parse_counts(TRIPLE_FILLED) == [10, 0, 0]


def test_read_counts_returns_empty_triple_from_settled_dom(monkeypatch: Any) -> None:
    verify = load_harness()
    monkeypatch.setattr(verify, "COUNT_RETRY_SECONDS", 0.01)
    page = FakePage(TRIPLE_EMPTY)
    assert verify.read_counts(page, timeout_ms=1000) == [0, 0, 0]
    assert page.evaluate_calls == 1


def test_read_counts_returns_filled_triple_from_settled_dom(monkeypatch: Any) -> None:
    verify = load_harness()
    monkeypatch.setattr(verify, "COUNT_RETRY_SECONDS", 0.01)
    page = FakePage(TRIPLE_FILLED)
    assert verify.read_counts(page, timeout_ms=1000) == [10, 0, 0]


def test_read_counts_retries_a_transient_em_dash_until_digits_arrive(monkeypatch: Any) -> None:
    verify = load_harness()
    monkeypatch.setattr(verify, "COUNT_RETRY_SECONDS", 0.01)
    page = FakePage(EM_DASH, EM_DASH, TRIPLE_FILLED)
    assert verify.read_counts(page, timeout_ms=2000) == [10, 0, 0]
    assert page.evaluate_calls == 3


def test_read_counts_exhausts_persistent_no_digit_to_none(monkeypatch: Any) -> None:
    verify = load_harness()
    monkeypatch.setattr(verify, "COUNT_RETRY_SECONDS", 0.01)
    page = FakePage(EM_DASH)
    assert verify.read_counts(page, timeout_ms=80) is None
    assert page.evaluate_calls >= 2


def test_empty_assertion_accepts_only_the_zero_triple() -> None:
    verify = load_harness()
    verify.require_empty_counts(TRIPLE_EMPTY, [0, 0, 0])
    for parsed in ([10, 0, 0], None, [0, 0], [0, 0, 0, 0]):
        with pytest.raises(AssertionError) as caught:
            verify.require_empty_counts(EM_DASH, parsed)
        message = str(caught.value)
        assert "raw=" in message and "parsed=" in message
        assert repr(parsed) in message


def test_persistent_no_digit_reaches_a_diagnostic_assertion(monkeypatch: Any) -> None:
    verify = load_harness()
    monkeypatch.setattr(verify, "COUNT_RETRY_SECONDS", 0.01)
    raw = EM_DASH
    page = FakePage(raw)
    parsed = verify.read_counts(page, timeout_ms=80)
    assert parsed is None
    with pytest.raises(AssertionError) as caught:
        verify.require_empty_counts(raw, parsed)
    message = str(caught.value)
    assert repr(raw) in message
    assert "parsed=None" in message


def test_filled_assertion_accepts_positive_and_rejects_zero_or_none() -> None:
    verify = load_harness()
    verify.require_filled_counts(TRIPLE_FILLED, [10, 0, 0])
    for parsed in ([0, 0, 0], [0, 1, 1], None):
        with pytest.raises(AssertionError) as caught:
            verify.require_filled_counts(TRIPLE_FILLED, parsed)
        message = str(caught.value)
        assert "raw=" in message and "parsed=" in message
        assert repr(parsed) in message
