"""Damaged stored reconciliation receipts must fail visibly during replay.

A receipt row can be damaged in storage (bad timestamp, malformed evidence
JSON, off-vocabulary enum, or a fractional REAL in a version column, since
SQLite INTEGER affinity keeps non-integral REALs unconverted). Decoding must
raise a typed failure naming the column and never silently coerce a value
(versions keep the failed ``int()`` conversion types: OverflowError for
non-finite floats, ValueError otherwise); a failed replay must leave every
stored row byte-identical.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.ports import ReconciliationApplication


def _application(nomination_id: str = "nom-1") -> ReconciliationApplication:
    return ReconciliationApplication(
        nomination_id=nomination_id,
        profile_id="p1",
        disposition="rejected",
        reason_code="conflict_confirmed",
        canonical_kind="read_conflict",
        composite_group_id=f"group-{nomination_id}",
        evidence_event_ids=(7, 9),
        verdict="conflict",
        quality="verified",
        left_node_id="a",
        left_version=1,
        left_expected_peer_id="b",
        right_node_id="b",
        right_version=1,
        right_expected_peer_id="a",
        winner_node_id=None,
        loser_node_id=None,
        loser_prior_version=None,
        loser_new_version=None,
        applied_at=100.0,
    )


def _seeded_graph(tmp_path: Path) -> SqliteGraphDriver:
    graph = SqliteGraphDriver(tmp_path / "graph.db")
    graph.apply_reconciliation(_application(), downweight_factor=None)
    return graph


def _corrupt(db_path: Path, column: str, value: Any) -> None:
    """Write one damaged column directly, bypassing the append-only trigger.

    The trigger is dropped on the isolated copy only; it simulates damage
    that a lossy writer or external tool could leave behind.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("DROP TRIGGER IF EXISTS trg_reconciliation_receipts_no_update")
        conn.execute(
            f"UPDATE reconciliation_receipts SET {column} = ? WHERE nomination_id = 'nom-1'",
            (value,),
        )
    finally:
        conn.close()


def _snapshot(db_path: Path) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """Every user-table row of the isolated database, row-order fixed."""
    conn = sqlite3.connect(db_path)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            name: tuple(conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()) for name in names
        }
    finally:
        conn.close()


def _receipt_row(db_path: Path) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM reconciliation_receipts").fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


DAMAGED_COLUMNS: list[tuple[str, Any, type[BaseException], str]] = [
    ("applied_at", "not-a-timestamp", ValueError, "applied_at"),
    ("applied_at", "2026-02-30T00:00:00Z", ValueError, "applied_at"),
    ("disposition", "shredded", ValueError, "disposition"),
    ("reason_code", "vibes", ValueError, "reason_code"),
    ("evidence_event_ids", "not-json", ValueError, "evidence_event_ids"),
    ("evidence_event_ids", "7", TypeError, "evidence_event_ids"),
    ("evidence_event_ids", "[7.5]", ValueError, "evidence_event_ids"),
    ("evidence_event_ids", '["7"]', ValueError, "evidence_event_ids"),
    ("left_version", 1.5, ValueError, "left_version"),
    ("right_version", 2.5, ValueError, "right_version"),
    ("loser_prior_version", 1.5, ValueError, "loser_prior_version"),
    ("loser_new_version", 3.5, ValueError, "loser_new_version"),
    ("left_version", float("inf"), OverflowError, "left_version"),
    ("left_version", "not-an-int", ValueError, "left_version"),
]


@pytest.mark.parametrize(("column", "damaged_value", "error_type", "fragment"), DAMAGED_COLUMNS)
def test_replay_of_damaged_receipt_fails_and_preserves_rows(
    tmp_path: Path, column: str, damaged_value: Any, error_type: type[BaseException], fragment: str
) -> None:
    graph = _seeded_graph(tmp_path)
    db_path = tmp_path / "graph.db"
    _corrupt(db_path, column, damaged_value)
    before = _snapshot(db_path)

    with pytest.raises(error_type, match=fragment):
        graph.apply_reconciliation(_application(), downweight_factor=None)

    assert _snapshot(db_path) == before


@pytest.mark.parametrize(
    ("column", "damaged_value"),
    [("left_version", 1.5), ("right_version", 2.5), ("loser_new_version", 3.5)],
)
def test_fractional_version_is_rejected_without_rounding(
    tmp_path: Path, column: str, damaged_value: float
) -> None:
    graph = _seeded_graph(tmp_path)
    db_path = tmp_path / "graph.db"
    _corrupt(db_path, column, damaged_value)

    with pytest.raises(ValueError, match=column):
        graph.apply_reconciliation(_application(), downweight_factor=None)

    stored = _receipt_row(db_path)
    assert stored[column] == damaged_value
    assert isinstance(stored[column], float)


def test_read_path_reports_damaged_receipt(tmp_path: Path) -> None:
    graph = _seeded_graph(tmp_path)
    db_path = tmp_path / "graph.db"
    _corrupt(db_path, "applied_at", "yesterday-ish")

    with pytest.raises(ValueError, match="applied_at"):
        graph.get_reconciliation_receipt(profile_id="p1", nomination_id="nom-1")


def test_intact_receipt_replay_stays_idempotent(tmp_path: Path) -> None:
    graph = _seeded_graph(tmp_path)
    db_path = tmp_path / "graph.db"
    before = _snapshot(db_path)

    replayed = graph.apply_reconciliation(_application(), downweight_factor=None)
    read_back = graph.get_reconciliation_receipt(profile_id="p1", nomination_id="nom-1")

    assert read_back == replayed
    assert read_back is not None
    assert read_back.disposition == "rejected"
    assert read_back.evidence_event_ids == (7, 9)
    assert _snapshot(db_path) == before
