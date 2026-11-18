"""zcode SQLite backfill reader (design 14 §2): per-session rollout handles,
native ``sequence`` ordinals, and text-only turns that leave reasoning and tool
parts out of the conversational channel."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local import cli
from mnemoseed_local.hosts.zcode import transcript
from mnemoseed_local.importer.ledger import ImportLedger
from mnemoseed_local.importer.runner import RolloutSource, load_source, run_import
from mnemoseed_local.util.rollout import rollout_run_id

SESSION_A = "sess_a1111111-1111-4111-8111-111111111111"
SESSION_SUB = "sess_subagent_agent_dispatch42"
SESSION_B = "sess_b2222222-2222-4222-8222-222222222222"


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params))
        return {"sessions": []}

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", path, body))
        if path == "/session/end":
            return {
                "status": "settled",
                "new_chunk": 0,
                "reinforced": 0,
                "needs_reconcile": 0,
                "pool_points_suppressed": 0.0,
            }
        return {"status": "accepted"}


def _msg(
    session: str,
    sequence: int,
    role: str,
    *,
    mid: str | None = None,
    created: int = 1000,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str, int, int, str]:
    data: dict[str, Any] = {"role": role, "time": {"created": created}, "agent": "zcode-agent"}
    if extra:
        data.update(extra)
    return (mid or f"msg-{session}-{sequence}", session, sequence, created, json.dumps(data))


def _part(
    mid: str,
    session: str,
    sequence: int,
    ptype: str,
    text: str | None = None,
) -> tuple[str, str, str, int, str]:
    data: dict[str, Any] = {"type": ptype}
    if text is not None:
        data["text"] = text
    return (f"part-{mid}-{sequence}", mid, session, sequence, json.dumps(data))


def _write_db(
    root: Path,
    sessions: list[tuple[str, int]],
    messages: list[tuple[str, str, int, int, str]],
    parts: list[tuple[str, str, str, int, str]],
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "db.sqlite"
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT, time_created INTEGER)"
        )
        con.execute(
            "CREATE TABLE message "
            "(id TEXT PRIMARY KEY, session_id TEXT, sequence INTEGER, time_created INTEGER, data TEXT)"
        )
        con.execute(
            "CREATE TABLE part "
            "(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, sequence INTEGER, data TEXT)"
        )
        for sid, created in sessions:
            con.execute(
                "INSERT INTO session (id, title, directory, time_created) VALUES (?, ?, ?, ?)",
                (sid, f"title-{sid}", "G:/work/proj", created),
            )
        for mid, sid, sequence, created, data in messages:
            con.execute(
                "INSERT INTO message (id, session_id, sequence, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (mid, sid, sequence, created, data),
            )
        for pid, mid, sid, sequence, data in parts:
            con.execute(
                "INSERT INTO part (id, message_id, session_id, sequence, data) VALUES (?, ?, ?, ?, ?)",
                (pid, mid, sid, sequence, data),
            )
        con.commit()
    finally:
        con.close()
    return db


def _source() -> RolloutSource:
    return RolloutSource(
        host="zcode",
        default_source=str(transcript.DEFAULT_SOURCE),
        iter_rollouts=transcript.iter_rollouts,
        read_session_meta=transcript.read_session_meta,
        read_turns=transcript.read_turns,
        validate_rollout=transcript.validate_rollout,
    )


def _quiet(_: str) -> None:
    return None


def test_default_source(tmp_path: Path) -> None:
    assert transcript.DEFAULT_SOURCE == "~/.zcode/cli/db"


def test_import_help_lists_all_backfill_hosts(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["import", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for host in ("codex_cli", "claude_code", "opencode", "zcode"):
        assert host in out


def test_iter_rollouts_stable_real_handles(tmp_path: Path) -> None:
    root = tmp_path / "zcode-db"
    _write_db(
        root,
        [(SESSION_B, 2000), (SESSION_SUB, 1500), (SESSION_A, 1000)],
        [],
        [],
    )
    handles = transcript.iter_rollouts(root)
    assert [h.name for h in handles] == [
        f"{SESSION_A}.zcode",
        f"{SESSION_SUB}.zcode",
        f"{SESSION_B}.zcode",
    ]
    assert all(isinstance(h, Path) and h.is_file() for h in handles)
    assert all(h.parent.name == ".mnemoseed-rollouts" for h in handles)
    assert rollout_run_id(handles[0]) in SESSION_A


def test_read_session_meta_returns_session_id(tmp_path: Path) -> None:
    root = tmp_path / "zcode-db"
    _write_db(root, [(SESSION_SUB, 1000)], [], [])
    handle = transcript.iter_rollouts(root)[0]
    assert transcript.read_session_meta(str(handle)) == SESSION_SUB


def test_read_turns_uses_native_sequence_and_excludes_reasoning_and_tool(tmp_path: Path) -> None:
    root = tmp_path / "zcode-db"
    m0 = _msg(SESSION_A, 0, "user", extra={"text": "DECOY-NOT-USED"})
    m1 = _msg(SESSION_A, 1, "assistant")
    m4 = _msg(SESSION_A, 4, "user")
    m5 = _msg(SESSION_A, 5, "assistant")
    _write_db(
        root,
        [(SESSION_A, 1000)],
        [m0, m1, m4, m5],
        [
            _part(m0[0], SESSION_A, 0, "text", "u0"),
            _part(m1[0], SESSION_A, 0, "reasoning", "SECRET-CHAIN-OF-THOUGHT"),
            _part(m1[0], SESSION_A, 1, "tool", "TOOL-PAYLOAD"),
            _part(m1[0], SESSION_A, 2, "text", "PUBLIC-"),
            _part(m1[0], SESSION_A, 3, "text", "ANSWER"),
            _part(m4[0], SESSION_A, 0, "text", "u4"),
            _part(m5[0], SESSION_A, 0, "tool", "ONLY-TOOL-NO-TURN"),
        ],
    )
    handle = transcript.iter_rollouts(root)[0]
    turns = transcript.read_turns(str(handle))
    assert [(t.ordinal, t.role, t.text) for t in turns] == [
        (0, "user", "u0"),
        (1, "assistant", "PUBLIC-ANSWER"),
        (4, "user", "u4"),
    ]
    assert not any("SECRET" in t.text or "TOOL" in t.text or "DECOY" in t.text for t in turns)


def test_read_turns_isolates_sessions(tmp_path: Path) -> None:
    root = tmp_path / "zcode-db"
    ma = _msg(SESSION_A, 0, "user")
    mb = _msg(SESSION_B, 0, "user")
    _write_db(
        root,
        [(SESSION_A, 1000), (SESSION_B, 1000)],
        [ma, mb],
        [_part(ma[0], SESSION_A, 0, "text", "only-a"), _part(mb[0], SESSION_B, 0, "text", "only-b")],
    )
    handles = {transcript.read_session_meta(str(h)): h for h in transcript.iter_rollouts(root)}
    assert [t.text for t in transcript.read_turns(str(handles[SESSION_A]))] == ["only-a"]
    assert [t.text for t in transcript.read_turns(str(handles[SESSION_B]))] == ["only-b"]


def test_read_session_meta_rejects_non_rollout_path(tmp_path: Path) -> None:
    bad = tmp_path / "elsewhere" / f"{SESSION_A}.zcode"
    assert transcript.read_session_meta(str(bad)) is None
    assert transcript.read_session_meta(None) is None


def test_validate_rollout_refuses_missing_db(tmp_path: Path) -> None:
    handle = tmp_path / ".mnemoseed-rollouts" / f"{SESSION_A}.zcode"
    with pytest.raises(ValueError, match="db.sqlite"):
        transcript.validate_rollout(str(handle))


def test_validate_rollout_refuses_unreadable_db(tmp_path: Path) -> None:
    root = tmp_path / "zcode-db"
    (root / "db.sqlite").mkdir(parents=True)
    handle = root / ".mnemoseed-rollouts" / f"{SESSION_A}.zcode"
    with pytest.raises(ValueError):
        transcript.validate_rollout(str(handle))


def test_iter_rollouts_skips_session_ids_that_escape_the_stub_dir(tmp_path: Path) -> None:
    root = tmp_path / "zcode-db"
    _write_db(
        root,
        [
            (SESSION_A, 1000),
            ("../escaped", 1000),
            ("..\\windows-escaped", 1000),
            (str(root / "abs-escaped"), 1000),
            ("sess/with-slash", 1000),
        ],
        [],
        [],
    )
    handles = transcript.iter_rollouts(root)
    assert [handle.name for handle in handles] == [f"{SESSION_A}.zcode"]
    stub_dir = root / ".mnemoseed-rollouts"
    assert all(handle.parent == stub_dir for handle in handles)
    assert list(root.rglob("*escaped*")) == []
    assert not (root / "with-slash.zcode").exists()


def test_iter_rollouts_raises_actionable_error_when_stub_dir_is_a_file(tmp_path: Path) -> None:
    root = tmp_path / "zcode-db"
    _write_db(root, [(SESSION_A, 1000)], [], [])
    (root / ".mnemoseed-rollouts").write_text("not a directory", encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        transcript.iter_rollouts(root)
    assert type(excinfo.value).__name__ == "ZcodeTranscriptError"
    assert "stub" in str(excinfo.value).lower()


def test_runner_dry_run_no_posts_and_text_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "mnemo-home"))
    root = tmp_path / "zcode-db"
    m0 = _msg(SESSION_A, 0, "user")
    m1 = _msg(SESSION_A, 1, "assistant")
    _write_db(
        root,
        [(SESSION_A, 1000)],
        [m0, m1],
        [
            _part(m0[0], SESSION_A, 0, "text", "u0"),
            _part(m1[0], SESSION_A, 0, "reasoning", "SECRET"),
            _part(m1[0], SESSION_A, 1, "text", "PUBLIC"),
        ],
    )
    client = _FakeClient()
    report = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=client,
        ledger=ImportLedger("zcode", home=tmp_path / "home"),
        dry_run=True,
        progress=_quiet,
    )
    assert report.sessions_scanned == 1
    assert report.turns_ingested == 2
    assert report.sessions_imported == 0
    assert client.calls == []


def test_runner_dry_run_creates_no_stub_files(tmp_path: Path) -> None:
    root = tmp_path / "zcode-db"
    m0 = _msg(SESSION_A, 0, "user")
    _write_db(root, [(SESSION_A, 1000)], [m0], [_part(m0[0], SESSION_A, 0, "text", "u0")])
    source = load_source("zcode", "mnemoseed_local.hosts.zcode.transcript")
    report = run_import(
        source=source,
        root=root,
        profile_id="default",
        client=_FakeClient(),
        ledger=ImportLedger("zcode", home=tmp_path / "home"),
        dry_run=True,
        progress=_quiet,
    )
    assert report.turns_ingested == 1
    stub_dir = root / ".mnemoseed-rollouts"
    assert not stub_dir.exists() or not any(stub_dir.iterdir())


def test_runner_labels_locked_database_distinct_from_corrupt(tmp_path: Path) -> None:
    def locked_turns(_path: str) -> Any:
        raise sqlite3.OperationalError("database is locked")

    def broken_turns(_path: str) -> Any:
        raise ValueError("bad row")

    source = RolloutSource(
        host="zcode",
        default_source="",
        iter_rollouts=lambda _root: [Path("locked.zcode"), Path("broken.zcode")],
        read_session_meta=lambda _path: "conv",
        read_turns=lambda path: (
            locked_turns(path) if str(path).endswith("locked.zcode") else broken_turns(path)
        ),
        validate_rollout=lambda _path: None,
    )
    emitted: list[str] = []
    report = run_import(
        source=source,
        root=tmp_path,
        profile_id="default",
        client=None,
        ledger=ImportLedger("zcode", home=tmp_path / "home"),
        dry_run=True,
        progress=emitted.append,
    )
    assert report.skipped.get("locked") == 1
    assert report.skipped.get("corrupt") == 1
    assert any("warning" in line.lower() and "lock" in line.lower() for line in emitted)
