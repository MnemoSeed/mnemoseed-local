"""Import ledger identity and corruption visibility."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import pytest

from mnemoseed_local.importer.ledger import ImportLedger, LedgerEntry

HOST = "codex_cli"
LOGGER = "mnemoseed_local.importer.ledger"


def _ledger_path(home: Path) -> Path:
    return ImportLedger(HOST, home=home).path


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == LOGGER]


def test_missing_ledger_is_quiet_first_run(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ledger = ImportLedger(HOST, home=home)
    assert ledger.get("any") is None
    assert _warnings(caplog) == []


def test_corrupt_ledger_warns_and_starts_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _ledger_path(home).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ledger = ImportLedger(HOST, home=home)
    assert ledger.get("any") is None
    assert any("corrupt" in record.getMessage().lower() for record in _warnings(caplog))


def test_invalid_shape_ledger_warns_and_starts_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _ledger_path(home).write_text(json.dumps({"version": 1, "host": HOST, "sessions": []}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ledger = ImportLedger(HOST, home=home)
    assert ledger.get("any") is None
    assert any("invalid" in record.getMessage().lower() for record in _warnings(caplog))


def _settled(session_id: str) -> LedgerEntry:
    return LedgerEntry(
        path=f"rollout-{session_id}.jsonl",
        size=1,
        mtime=1.0,
        session_id=session_id,
        conversation_id=None,
        settled_through_ordinal=0,
        status="settled",
    )


def test_ledger_is_scoped_per_profile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    alice = ImportLedger(HOST, "alice", home=home)
    bob = ImportLedger(HOST, "bob", home=home)
    assert alice.path != bob.path
    alice.record(_settled("run-a"))
    assert alice.get("run-a") is not None
    assert bob.get("run-a") is None
    assert ImportLedger(HOST, "alice", home=home).get("run-a") is not None


def test_legacy_ledger_migrates_only_to_default_profile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / f"import-ledger-{HOST}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "host": HOST,
                "sessions": {"run-legacy": asdict(_settled("run-legacy"))},
            }
        ),
        encoding="utf-8",
    )

    default = ImportLedger(HOST, "default", home=home)
    other = ImportLedger(HOST, "bob", home=home)

    assert default.get("run-legacy") is not None
    assert other.get("run-legacy") is None
    assert default.path.exists()
