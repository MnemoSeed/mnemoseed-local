"""Claude Code local-history reader: transcript shapes, turn filtering, and a
hermetic dry-run import through the CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.cli import main
from mnemoseed_local.hosts.claude_code import transcript
from mnemoseed_local.importer import runner
from mnemoseed_local.rest_client import DaemonClient

SESSION_ID = "cc-session-0001"


def _event(kind: str, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": kind,
        "uuid": f"uuid-{kind}-{len(fields)}",
        "timestamp": "2026-11-17T10:00:00.000Z",
        "sessionId": SESSION_ID,
        "cwd": "/work/mnemoseed",
    }
    record.update(fields)
    return record


def _write_transcript(path: Path, events: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects" / "-work-mnemoseed"
    events = [
        _event("user", message={"role": "user", "content": "first prompt"}),
        _event(
            "assistant",
            message={
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "text", "text": "answer line one\n"},
                    {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}},
                    {"type": "text", "text": "answer line two"},
                ],
            },
        ),
        _event("last-prompt", lastPrompt="first prompt"),
        _event(
            "user",
            message={"role": "user", "content": [{"type": "text", "text": "  spaced prompt  "}]},
        ),
        _event(
            "user",
            isMeta=True,
            message={
                "role": "user",
                "content": "<local-command-stdout>model set to opus</local-command-stdout>",
            },
        ),
        _event(
            "assistant",
            isSidechain=True,
            message={"role": "assistant", "content": "subagent answer"},
        ),
        _event("mode", mode="default"),
        _event("permission-mode", permissionMode="default"),
        _event("file-history-snapshot", snapshot={"trackedFileBackups": {}}),
        _event("attachment", attachment={"type": "document"}),
        _event("cost-state", costUSD=0.01),
        _event("assistant", message={"role": "assistant", "content": "final answer"}),
    ]
    _write_transcript(root / "session-a.jsonl", events)
    return root


def test_iter_rollouts_finds_nested_plain_jsonl_in_stable_order(projects_root: Path, tmp_path: Path) -> None:
    nested = projects_root / "nested"
    _write_transcript(nested / "session-b.jsonl", [_event("user", message={"role": "user", "content": "x"})])
    _write_transcript(nested / "session-ignored.jsonl.zst", [])
    found = transcript.iter_rollouts(projects_root.parent)
    assert found == sorted(found)
    assert [path.name for path in found] == ["session-b.jsonl", "session-a.jsonl"]
    assert transcript.iter_rollouts(tmp_path / "missing") == []


def test_read_session_meta_returns_session_id(projects_root: Path) -> None:
    rollout = projects_root / "session-a.jsonl"
    assert transcript.read_session_meta(str(rollout)) == SESSION_ID


def test_read_session_meta_returns_none_without_a_session_id(tmp_path: Path) -> None:
    rollout = _write_transcript(
        tmp_path / "no-meta.jsonl", [_event("user", message={"role": "user", "content": "x"}, sessionId=None)]
    )
    assert transcript.read_session_meta(str(rollout)) is None
    assert transcript.read_session_meta(str(tmp_path / "missing.jsonl")) is None


def test_read_turns_filters_meta_and_sidechain_and_keeps_text_verbatim(projects_root: Path) -> None:
    turns = transcript.read_turns(str(projects_root / "session-a.jsonl"))
    assert [(turn.ordinal, turn.role, turn.text) for turn in turns] == [
        (0, "user", "first prompt"),
        (1, "assistant", "answer line one\nanswer line two"),
        (3, "user", "  spaced prompt  "),
        (11, "assistant", "final answer"),
    ]
    assert [turn.ordinal for turn in turns] == sorted(turn.ordinal for turn in turns)


def test_read_turns_tolerates_missing_file_and_malformed_lines(tmp_path: Path) -> None:
    rollout = tmp_path / "mixed.jsonl"
    rollout.write_text(
        "not json\n"
        + json.dumps(_event("user", message={"role": "user", "content": "kept"}))
        + "\n"
        + json.dumps(_event("assistant", message={"role": "assistant", "content": []}))
        + "\n",
        encoding="utf-8",
    )
    turns = transcript.read_turns(str(rollout))
    assert [(turn.ordinal, turn.text) for turn in turns] == [(1, "kept")]
    assert transcript.read_turns(str(tmp_path / "missing.jsonl")) == []


def test_validate_rollout_accepts_a_readable_file(projects_root: Path, tmp_path: Path) -> None:
    transcript.validate_rollout(str(projects_root / "session-a.jsonl"))
    with pytest.raises(ValueError):
        transcript.validate_rollout(str(tmp_path / "missing.jsonl"))


def test_preflight_zstandard_only_fails_for_discovered_compressed_rollouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "find_spec", lambda name: None)
    runner.preflight_zstandard([tmp_path / "plain.jsonl"])
    with pytest.raises(runner.ImportPreflightError) as caught:
        runner.preflight_zstandard([tmp_path / "compressed.jsonl.zst"])
    assert str(caught.value) == (
        "zstandard is required to read compressed rollouts "
        "(install with: uv tool install --force 'mnemoseed-local[codex]')"
    )


def test_cli_dry_run_counts_turns_without_any_daemon_contact(
    projects_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(tmp_path / "hermetic-home"))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must not contact a daemon")

    monkeypatch.setattr(runner, "build_client", forbidden)
    monkeypatch.setattr(DaemonClient, "post", forbidden)
    code = main(
        [
            "import",
            "--host",
            "claude_code",
            "--source",
            str(projects_root.parent),
            "--profile",
            "test-profile",
            "--dry-run",
            "--json",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["sessions_scanned"] == 1
    assert report["turns_ingested"] == 4
