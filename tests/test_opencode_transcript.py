"""opencode SQLite backfill reader (design 14 §2): session-synthesised rollout
handles, native ``seq`` ordinals, and text-only turn extraction that leaves
reasoning/tool parts out of the conversational channel."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.hosts.opencode import transcript
from mnemoseed_local.importer.ledger import ImportLedger
from mnemoseed_local.importer.runner import RolloutSource, load_source, run_import
from mnemoseed_local.util.rollout import rollout_run_id

SESSION_A = "ses_a1111111111WgK5b3MDl0WTIU"
SESSION_B = "ses_b2222222222WgK5b3MDl0WTIU"


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


def _user(
    text: str, *, seq: int, created: int = 1000, session: str = SESSION_A
) -> tuple[str, str, int, int, str]:
    return ("user", session, seq, created, json.dumps({"time": {"created": created}, "text": text}))


def _assistant(
    parts: list[dict[str, str]], *, seq: int, created: int = 1000, session: str = SESSION_A
) -> tuple[str, str, int, int, str]:
    data = {
        "time": {"created": created},
        "agent": "build",
        "model": {"id": "test-model"},
        "content": parts,
    }
    return ("assistant", session, seq, created, json.dumps(data))


def _write_db(root: Path, messages: list[tuple[str, str, int, int, str]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "opencode.db"
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT, directory TEXT, time_created INTEGER)"
        )
        con.execute(
            "CREATE TABLE session_message "
            "(id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, "
            "time_created INTEGER, data TEXT)"
        )
        session_ids: dict[str, int] = {}
        for row in messages:
            session_ids.setdefault(row[1], row[3])
        for sid, created in session_ids.items():
            con.execute(
                "INSERT INTO session_v2 (id, title, directory, time_created) VALUES (?, ?, ?, ?)",
                (sid, f"title-{sid}", "G:/work/proj", created),
            )
        for index, (msg_type, sid, seq, created, data) in enumerate(messages):
            con.execute(
                "INSERT INTO session_message "
                "(id, session_id, type, seq, time_created, data) VALUES (?, ?, ?, ?, ?, ?)",
                (f"msg-{index}", sid, msg_type, seq, created, data),
            )
        con.commit()
    finally:
        con.close()
    return db


def _source() -> RolloutSource:
    return RolloutSource(
        host="opencode",
        default_source=str(transcript.DEFAULT_SOURCE),
        iter_rollouts=transcript.iter_rollouts,
        read_session_meta=transcript.read_session_meta,
        read_turns=transcript.read_turns,
        validate_rollout=transcript.validate_rollout,
    )


def _quiet(_: str) -> None:
    return None


def test_default_source(tmp_path: Path) -> None:
    assert transcript.DEFAULT_SOURCE == "~/.local/share/opencode"


def test_iter_rollouts_stable_real_handles(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    _write_db(
        root,
        [
            ("user", SESSION_B, 0, 2000, json.dumps({"time": {"created": 2000}, "text": "later"})),
            ("user", SESSION_A, 0, 1000, json.dumps({"time": {"created": 1000}, "text": "earlier"})),
        ],
    )
    handles = transcript.iter_rollouts(root)
    assert [h.name for h in handles] == [f"{SESSION_A}.opencode", f"{SESSION_B}.opencode"]
    assert all(isinstance(h, Path) for h in handles)
    assert all(h.is_file() for h in handles)
    assert all(rollout_run_id(h) == h.stem for h in handles)


def test_read_session_meta_returns_session_id(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    _write_db(root, [("user", SESSION_A, 0, 1000, json.dumps({"time": {"created": 1000}, "text": "hi"}))])
    handle = transcript.iter_rollouts(root)[0]
    assert transcript.read_session_meta(str(handle)) == SESSION_A


def test_read_turns_uses_native_seq_and_excludes_reasoning(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    _write_db(
        root,
        [
            _user("u0", seq=0),
            _assistant(
                [
                    {"type": "reasoning", "text": "SECRET-CHAIN-OF-THOUGHT"},
                    {"type": "tool", "text": "TOOL-PAYLOAD"},
                    {"type": "text", "text": "PUBLIC-ANSWER"},
                ],
                seq=1,
            ),
            _user("u2", seq=4),
        ],
    )
    handle = transcript.iter_rollouts(root)[0]
    turns = transcript.read_turns(str(handle))
    assert [(t.ordinal, t.role, t.text) for t in turns] == [
        (0, "user", "u0"),
        (1, "assistant", "PUBLIC-ANSWER"),
        (4, "user", "u2"),
    ]
    assert not any("SECRET" in t.text or "TOOL" in t.text for t in turns)


def test_validate_rollout_refuses_missing_db(tmp_path: Path) -> None:
    handle = tmp_path / "opencode" / ".mnemoseed-rollouts" / f"{SESSION_A}.opencode"
    with pytest.raises(ValueError, match="opencode.db"):
        transcript.validate_rollout(str(handle))


def test_validate_rollout_refuses_unreadable_db(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    (root / "opencode.db").mkdir(parents=True)
    handle = root / ".mnemoseed-rollouts" / f"{SESSION_A}.opencode"
    with pytest.raises(ValueError):
        transcript.validate_rollout(str(handle))


def test_runner_dry_run_no_posts_and_text_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "mnemo-home"))
    root = tmp_path / "opencode"
    _write_db(
        root,
        [
            _user("u0", seq=0),
            _assistant(
                [
                    {"type": "reasoning", "text": "SECRET"},
                    {"type": "text", "text": "PUBLIC"},
                ],
                seq=1,
            ),
        ],
    )
    client = _FakeClient()
    report = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=client,
        ledger=ImportLedger("opencode"),
        dry_run=True,
        progress=_quiet,
    )
    assert report.sessions_scanned == 1
    assert report.turns_ingested == 2
    assert report.sessions_imported == 0
    assert client.calls == []


def test_runner_real_import_ingests_with_statable_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "mnemo-home"))
    root = tmp_path / "opencode"
    _write_db(root, [_user("u0", seq=0), _user("u1", seq=1)])
    client = _FakeClient()
    report = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=client,
        ledger=ImportLedger("opencode"),
        progress=_quiet,
    )
    ingests = [body for verb, _path, body in client.calls if verb == "POST" and _path == "/ingest"]
    assert len(ingests) == 2
    assert all(body["session_id"] == SESSION_A for body in ingests)
    assert report.sessions_imported == 1


def _open_wal_db(root: Path) -> tuple[Path, sqlite3.Connection]:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "opencode.db"
    writer = sqlite3.connect(db)
    try:
        mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode == "wal"
        writer.execute(
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT, directory TEXT, time_created INTEGER)"
        )
        writer.execute(
            "CREATE TABLE session_message "
            "(id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, "
            "time_created INTEGER, data TEXT)"
        )
        writer.execute(
            "INSERT INTO session_v2 (id, title, directory, time_created) VALUES (?, ?, ?, ?)",
            (SESSION_A, "title", "G:/work/proj", 1000),
        )
        writer.execute(
            "INSERT INTO session_message "
            "(id, session_id, type, seq, time_created, data) VALUES (?, ?, ?, ?, ?, ?)",
            ("m0", SESSION_A, "user", 0, 1000, json.dumps({"text": "baseline"})),
        )
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        writer.execute(
            "INSERT INTO session_message "
            "(id, session_id, type, seq, time_created, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "m1",
                SESSION_A,
                "assistant",
                1,
                1001,
                json.dumps({"content": [{"type": "text", "text": "wal-only"}]}),
            ),
        )
        writer.commit()
    except BaseException:
        writer.close()
        raise
    return db, writer


def test_read_turns_sees_wal_only_turns_through_readonly_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "opencode"
    _db, writer = _open_wal_db(root)
    stub = root / ".mnemoseed-rollouts" / f"{SESSION_A}.opencode"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.touch()
    uris: list[str] = []
    real_connect = transcript.sqlite3.connect

    def record_connect(database: Any, **kwargs: Any) -> sqlite3.Connection:
        uris.append(str(database))
        return real_connect(database, **kwargs)

    monkeypatch.setattr(transcript.sqlite3, "connect", record_connect)
    try:
        turns = transcript.read_turns(str(stub))
    finally:
        writer.close()
    assert [turn.text for turn in turns] == ["baseline", "wal-only"]
    assert len(uris) == 1
    assert "mode=ro" in uris[0]
    assert "immutable" not in uris[0]
    assert "nolock" not in uris[0]


def test_iter_rollouts_skips_session_ids_that_escape_the_stub_dir(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    _write_db(
        root,
        [
            ("user", SESSION_A, 0, 1000, json.dumps({"text": "ok"})),
            ("user", "../escaped", 0, 1000, json.dumps({"text": "bad"})),
            ("user", "..\\windows-escaped", 0, 1000, json.dumps({"text": "bad"})),
            ("user", str(root / "abs-escaped"), 0, 1000, json.dumps({"text": "bad"})),
        ],
    )
    handles = transcript.iter_rollouts(root)
    assert [handle.name for handle in handles] == [f"{SESSION_A}.opencode"]
    stub_dir = root / ".mnemoseed-rollouts"
    assert all(handle.parent == stub_dir for handle in handles)
    assert not (root / "escaped.opencode").exists()
    assert not (root / "windows-escaped.opencode").exists()
    assert not (root / "abs-escaped.opencode").exists()
    assert list(root.rglob("*escaped*")) == []


def test_iter_rollouts_raises_actionable_error_when_stub_dir_is_a_file(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    _write_db(root, [("user", SESSION_A, 0, 1000, json.dumps({"text": "ok"}))])
    (root / ".mnemoseed-rollouts").write_text("not a directory", encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        transcript.iter_rollouts(root)
    assert type(excinfo.value).__name__ == "OpencodeTranscriptError"
    assert "stub" in str(excinfo.value).lower()


def test_runner_dry_run_creates_no_stub_files(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    _write_db(root, [_user("u0", seq=0), _user("u1", seq=1)])
    source = load_source("opencode", "mnemoseed_local.hosts.opencode.transcript")
    report = run_import(
        source=source,
        root=root,
        profile_id="default",
        client=_FakeClient(),
        ledger=ImportLedger("opencode", home=tmp_path / "home"),
        dry_run=True,
        progress=_quiet,
    )
    assert report.turns_ingested == 2
    stub_dir = root / ".mnemoseed-rollouts"
    assert not stub_dir.exists() or not any(stub_dir.iterdir())


def test_runner_labels_locked_database_distinct_from_corrupt(tmp_path: Path) -> None:
    def locked_turns(_path: str) -> Any:
        raise sqlite3.OperationalError("database is locked")

    def broken_turns(_path: str) -> Any:
        raise ValueError("bad row")

    source = RolloutSource(
        host="opencode",
        default_source="",
        iter_rollouts=lambda _root: [Path("locked.opencode"), Path("broken.opencode")],
        read_session_meta=lambda _path: "conv",
        read_turns=lambda path: (
            locked_turns(path) if str(path).endswith("locked.opencode") else broken_turns(path)
        ),
        validate_rollout=lambda _path: None,
    )
    emitted: list[str] = []
    report = run_import(
        source=source,
        root=tmp_path,
        profile_id="default",
        client=None,
        ledger=ImportLedger("opencode", home=tmp_path / "home"),
        dry_run=True,
        progress=emitted.append,
    )
    assert report.skipped.get("locked") == 1
    assert report.skipped.get("corrupt") == 1
    assert any("warning" in line.lower() and "lock" in line.lower() for line in emitted)
