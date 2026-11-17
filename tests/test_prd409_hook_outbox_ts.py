"""D5 TS hook outbox: the opencode plugin spools failed POSTs and both awaited
recall pulls into the reused debug sink, with an always-on warning and ordered
flush before the next live POST."""

from __future__ import annotations

from pathlib import Path

import test_hook_ts_behavior as ts


def test_ts_spools_failed_ingest_with_visible_warning(tmp_path: Path) -> None:
    bundle = ts._bundle(tmp_path)
    transcript = ts._run(bundle, "hook-outbox-spool", debug=None)
    assert transcript["outboxCount"] >= 1, "a failed ingest must land in the outbox"
    assert transcript["text"] == "down turn"
    assert transcript["warned"] is True, "the warning must be visible with DEBUG off"


def test_ts_flushes_spooled_turn_before_the_live_one(tmp_path: Path) -> None:
    bundle = ts._bundle(tmp_path)
    transcript = ts._run(bundle, "hook-outbox-flush", debug=None)
    assert transcript["order"] == ["first down turn", "live turn"], transcript
    assert transcript["remaining"] == 0, "a fully flushed spool is emptied"


def test_ts_spools_failed_t1_recall_pull(tmp_path: Path) -> None:
    bundle = ts._bundle(tmp_path)
    transcript = ts._run(bundle, "hook-outbox-pull-recent", debug=None)
    assert transcript["count"] >= 1, "a failed /session/recent must spool"
    assert transcript["warned"] is True


def test_ts_spools_failed_t2_recall_pull(tmp_path: Path) -> None:
    bundle = ts._bundle(tmp_path)
    transcript = ts._run(bundle, "hook-outbox-pull-pending", debug=None)
    assert transcript["count"] >= 1, "a failed /session/recall-pending must spool"
    assert transcript["warned"] is True
