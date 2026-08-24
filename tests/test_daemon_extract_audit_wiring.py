"""The daemon wiring of the extract-failure observation seam: classified rows
land in the audit surface, store faults are swallowed, successes write none."""

from __future__ import annotations

import time
from typing import Any

from mnemoseed_local.daemon.app import _audit_extract_failure
from mnemoseed_local.dream.pipeline import ExtractFailure
from mnemoseed_local.storage.ports import TurnRange


class _FakeMeta:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: list[Any] = []
        self._fail = fail

    def audit_append(self, entry: Any) -> None:
        if self._fail:
            raise RuntimeError("audit store down")
        self.rows.append(entry)


def _failure() -> ExtractFailure:
    return ExtractFailure(
        profile_id="default",
        turn_range=TurnRange(start=0, end=1),
        stage="reflect",
        failure_class="llm_unreachable",
        detail="connection reset",
        tokens=42,
    )


def test_extract_failure_lands_one_classified_audit_row() -> None:
    meta = _FakeMeta()
    _audit_extract_failure(meta, _failure())
    assert len(meta.rows) == 1
    row = meta.rows[0]
    assert row.actor == "dream"
    assert row.action == "dream_extract_failed"
    assert row.detail["failure_class"] == "llm_unreachable"
    assert row.detail["tokens"] == 42
    assert isinstance(row.at, float)
    assert row.at <= time.time() + 1


def test_audit_store_fault_never_raises() -> None:
    meta = _FakeMeta(fail=True)
    _audit_extract_failure(meta, _failure())
    assert meta.rows == []
