"""History import core (#394 S3): transcript readers, run-uuid identity, the
hook-shaped ingest envelope, settle-gated ledger, and the exact report ledger."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import zstandard

from mnemoseed_local.hosts.codex_cli import transcript
from mnemoseed_local.hosts.common.daemon import POST_TIMEOUT_SECONDS
from mnemoseed_local.importer import runner
from mnemoseed_local.importer.ledger import ImportLedger, LedgerEntry
from mnemoseed_local.importer.runner import (
    RolloutSource,
    build_client,
    run_import,
)
from mnemoseed_local.rest_client import DaemonRestError

RUN_A = "c0de5e55-0000-4000-8000-000000000001"
RUN_B = "c0de5e55-0000-4000-8000-000000000002"
CONV = "conv-shared"
REPORT_FIELDS = {
    "sessions_scanned",
    "sessions_imported",
    "skipped",
    "turns_ingested",
    "turns_deduped",
    "new_chunk",
    "reinforced",
    "needs_reconcile",
    "pool_points_suppressed",
    "elapsed_s",
}


def _write_rollout(
    path: Path,
    *,
    conversation_id: str | None = CONV,
    turns: tuple[tuple[int, str, str], ...] = (),
    corrupt: bool = False,
) -> Path:
    lines: list[str] = []
    if conversation_id is not None:
        lines.append(
            json.dumps(
                {
                    "ordinal": 0,
                    "type": "session_meta",
                    "payload": {"type": "session_meta", "id": "meta", "session_id": conversation_id},
                }
            )
        )
    for ordinal, role, text in turns:
        lines.append(
            json.dumps(
                {
                    "ordinal": ordinal,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
        )
    data = ("\n".join(lines) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".zst":
        payload = zstandard.ZstdCompressor().compress(data)
        path.write_bytes(payload[: len(payload) // 2] if corrupt else payload)
    else:
        path.write_bytes(data[: len(data) // 2] if corrupt else data)
    return path


def _rollout_path(root: Path, run: str, *, suffix: str = ".jsonl.zst") -> Path:
    return root / "2026" / "09" / "10" / f"rollout-2026-09-10t10-00-00-000000-{run}{suffix}"


def _source() -> RolloutSource:
    return RolloutSource(
        host="codex_cli",
        default_source="~/.codex/sessions",
        iter_rollouts=transcript.iter_rollouts,
        read_session_meta=transcript.read_session_meta,
        read_turns=transcript.read_turns,
        validate_rollout=transcript.validate_rollout,
    )


class _FakeClient:
    """Records every verb/path/body and returns configurable replies."""

    def __init__(
        self,
        *,
        active: tuple[str, ...] = (),
        settle_status: int = 200,
        settle_reply: dict[str, Any] | None = None,
        ingest_ack: int | None = None,
        ingest_reply: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.active = active
        self.settle_status = settle_status
        self.settle_reply = settle_reply
        self.ingest_ack = ingest_ack
        self.ingest_reply = ingest_reply

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params))
        if path == "/session/active":
            return {"sessions": list(self.active)}
        return {}

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", path, body))
        if path == "/session/end":
            if self.settle_status >= 400:
                raise DaemonRestError(self.settle_status, "settle refused")
            return self.settle_reply or {
                "status": "settled",
                "new_chunk": 2,
                "reinforced": 0,
                "needs_reconcile": 0,
                "pool_points_suppressed": 1.5,
            }
        if self.ingest_reply is not None:
            return self.ingest_reply
        reply: dict[str, Any] = {"status": "accepted"}
        if self.ingest_ack is not None:
            reply["ack_ordinal"] = self.ingest_ack
        return reply


def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    return home


def _quiet(_: str) -> None:
    return None


# ---------------------------------------------------------------- transcript seam


def test_iter_rollouts_walks_date_tree_in_sorted_order(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    later = _write_rollout(root / "2026" / "10" / "24" / f"rollout-later-{RUN_B}.jsonl.zst")
    earlier = _write_rollout(_rollout_path(root, RUN_A))
    assert transcript.iter_rollouts(root) == [earlier, later]


def test_read_session_meta_returns_conversation_id_only(tmp_path: Path) -> None:
    path = _write_rollout(_rollout_path(tmp_path, RUN_A), conversation_id="conv-7")
    assert transcript.read_session_meta(path) == "conv-7"
    assert transcript.read_session_meta(_rollout_path(tmp_path, RUN_B, suffix=".jsonl")) is None


# ---------------------------------------------------------------- envelope identity


def test_ingest_envelope_matches_hook_shape_with_file_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(
        _rollout_path(root, RUN_A),
        turns=((0, "user", "u0"), (1, "assistant", "a0")),
    )
    client = _FakeClient()
    report = run_import(
        source=_source(),
        root=root,
        profile_id="prof",
        client=client,
        ledger=ImportLedger("codex_cli"),
        progress=_quiet,
    )
    ingests = [body for verb, path, body in client.calls if verb == "POST" and path == "/ingest"]
    assert len(ingests) == 2
    assert ingests[0]["host"] == "codex_cli"
    assert ingests[0]["event"] == "user_prompt"
    assert ingests[1]["event"] == "assistant_message"
    assert ingests[0]["session_id"] == ingests[1]["session_id"] == RUN_A
    assert ingests[0]["profile_id"] == ingests[1]["profile_id"] == "prof"
    assert [item["transcript_ordinal"] for item in ingests] == [0, 1]
    assert ingests[0]["content"] == {"text": "u0", "model_id": None}
    settle = [body for verb, path, body in client.calls if verb == "POST" and path == "/session/end"]
    assert settle[0]["credit_pool"] is False
    assert report.sessions_imported == 1


def test_conversation_id_is_ledger_lineage_not_session_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(_rollout_path(root, RUN_A), turns=((0, "user", "u0"),))
    _write_rollout(
        _rollout_path(root, RUN_B).with_name(f"rollout-b-{RUN_B}.jsonl.zst"),
        turns=((0, "user", "u1"),),
    )
    ledger = ImportLedger("codex_cli")
    report = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=_FakeClient(),
        ledger=ledger,
        progress=_quiet,
    )
    assert report.sessions_imported == 2
    assert ledger.get(RUN_A) is not None and ledger.get(RUN_B) is not None
    assert ledger.get(RUN_A).conversation_id == ledger.get(RUN_B).conversation_id == CONV


# ---------------------------------------------------------------- client discipline


def test_build_client_uses_cli_actor_and_long_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_client_for(
        base_url: str,
        *,
        profile_id: str,
        actor: str,
        timeout: float,
        token: str | None = None,
    ) -> object:
        seen.update(base_url=base_url, profile_id=profile_id, actor=actor, timeout=timeout, token=token)
        return object()

    monkeypatch.setattr(runner, "client_for", fake_client_for)
    build_client("http://127.0.0.1:9911", "prof")
    assert seen["actor"] == "cli"
    assert seen["profile_id"] == "prof"
    assert seen["timeout"] >= 600.0
    assert seen["timeout"] != POST_TIMEOUT_SECONDS


def _capture_settle_timeout(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_client_for(
        base_url: str,
        *,
        profile_id: str,
        actor: str,
        timeout: float,
        token: str | None = None,
    ) -> object:
        seen.update(timeout=timeout)
        return object()

    monkeypatch.setattr(runner, "client_for", fake_client_for)
    return seen


def test_build_client_settle_timeout_defaults_to_3600(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNEMOSEED_IMPORT_SETTLE_TIMEOUT_SECONDS", raising=False)
    seen = _capture_settle_timeout(monkeypatch)
    build_client("http://127.0.0.1:9911", "prof")
    assert seen["timeout"] == 3600.0


def test_build_client_settle_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOSEED_IMPORT_SETTLE_TIMEOUT_SECONDS", "7200")
    seen = _capture_settle_timeout(monkeypatch)
    build_client("http://127.0.0.1:9911", "prof")
    assert seen["timeout"] == 7200.0


# ---------------------------------------------------------------- skip reasons


def test_active_and_already_imported_are_distinct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(_rollout_path(root, RUN_A), conversation_id="conv-a", turns=((0, "user", "u0"),))
    _write_rollout(
        _rollout_path(root, RUN_B).with_name(f"rollout-b-{RUN_B}.jsonl.zst"),
        conversation_id="conv-b",
        turns=((0, "user", "u1"),),
    )
    ledger = ImportLedger("codex_cli")
    ledger.record(
        LedgerEntry(
            path=str(_rollout_path(root, RUN_B)),
            size=1,
            mtime=1.0,
            session_id=RUN_B,
            conversation_id="conv-b",
            settled_through_ordinal=0,
            status="settled",
            outcomes={},
        )
    )
    client = _FakeClient(active=("conv-a",))
    report = run_import(
        source=_source(), root=root, profile_id="default", client=client, ledger=ledger, progress=_quiet
    )
    assert report.skipped == {"active": 1, "already-imported": 1}
    assert not [call for call in client.calls if call[0] == "POST"]


def test_missing_meta_zero_turn_and_corrupt_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(_rollout_path(root, RUN_A), conversation_id=None, turns=((0, "user", "u0"),))
    _write_rollout(
        _rollout_path(root, RUN_B).with_name(f"rollout-b-{RUN_B}.jsonl.zst"),
        turns=(),
    )
    corrupt_run = "c0de5e55-0000-4000-8000-000000000003"
    _write_rollout(
        _rollout_path(root, corrupt_run).with_name(f"rollout-c-{corrupt_run}.jsonl.zst"), corrupt=True
    )
    good_run = "c0de5e55-0000-4000-8000-000000000004"
    _write_rollout(
        _rollout_path(root, good_run).with_name(f"rollout-d-{good_run}.jsonl.zst"),
        turns=((0, "user", "ok"),),
    )
    report = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=_FakeClient(),
        ledger=ImportLedger("codex_cli"),
        progress=_quiet,
    )
    assert report.sessions_scanned == 4
    assert report.sessions_imported == 1
    assert report.skipped == {"no-meta": 1, "zero-turn": 1, "corrupt": 1}


# ---------------------------------------------------------------- ledger gate & resume


def test_ledger_advances_only_after_settle_200(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(_rollout_path(root, RUN_A), turns=((0, "user", "u0"),))
    ledger = ImportLedger("codex_cli")
    with pytest.raises(DaemonRestError):
        run_import(
            source=_source(),
            root=root,
            profile_id="default",
            client=_FakeClient(settle_status=500),
            ledger=ledger,
            progress=_quiet,
        )
    assert ledger.get(RUN_A) is None


def test_resume_ingests_only_turns_after_settled_ordinal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(
        _rollout_path(root, RUN_A), turns=((0, "user", "u0"), (1, "assistant", "a0"), (2, "user", "u1"))
    )
    ledger = ImportLedger("codex_cli")
    ledger.record(
        LedgerEntry(
            path=str(_rollout_path(root, RUN_A)),
            size=1,
            mtime=1.0,
            session_id=RUN_A,
            conversation_id=CONV,
            settled_through_ordinal=0,
            status="settled",
            outcomes={},
        )
    )
    client = _FakeClient()
    report = run_import(
        source=_source(), root=root, profile_id="default", client=client, ledger=ledger, progress=_quiet
    )
    ingests = [body for verb, path, body in client.calls if verb == "POST" and path == "/ingest"]
    assert [item["transcript_ordinal"] for item in ingests] == [1, 2]
    assert report.turns_ingested == 2
    assert ledger.get(RUN_A).settled_through_ordinal == 2


def test_accounts_for_deduped_turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(_rollout_path(root, RUN_A), turns=((0, "user", "u0"),))
    client = _FakeClient(ingest_reply={"status": "accepted", "deduped": True, "ack_ordinal": 7})
    report = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=client,
        ledger=ImportLedger("codex_cli"),
        progress=_quiet,
    )
    assert report.turns_ingested == 1
    assert report.turns_deduped == 1


def test_dedup_at_equal_watermark_is_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(_rollout_path(root, RUN_A), turns=((0, "user", "u0"), (1, "assistant", "a0")))
    client = _FakeClient(ingest_reply={"status": "accepted", "deduped": True, "ack_ordinal": 1})
    report = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=client,
        ledger=ImportLedger("codex_cli"),
        progress=_quiet,
    )
    assert report.turns_ingested == 2
    assert report.turns_deduped == 2


# ---------------------------------------------------------------- report contract


def test_report_vocabulary_is_exact_and_turns_are_accounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(_rollout_path(root, RUN_A), turns=((0, "user", "u0"), (1, "assistant", "a0")))
    report = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=_FakeClient(),
        ledger=ImportLedger("codex_cli"),
        progress=_quiet,
    )
    payload = asdict(report)
    assert set(payload) == REPORT_FIELDS
    assert "kept" not in json.dumps(payload).lower()
    assert "dropped" not in json.dumps(payload).lower()
    assert report.turns_ingested == report.new_chunk + report.reinforced + report.needs_reconcile
    assert report.pool_points_suppressed == 1.5
    assert report.elapsed_s >= 0.0


def test_dry_run_counts_turns_without_any_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(_rollout_path(root, RUN_A), turns=((0, "user", "u0"), (1, "assistant", "a0")))
    client = _FakeClient()
    report = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=None,
        ledger=ImportLedger("codex_cli"),
        dry_run=True,
        progress=_quiet,
    )
    assert report.turns_ingested == 2
    assert report.sessions_imported == 0
    assert not client.calls


def test_dry_run_turn_count_matches_a_real_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(
        _rollout_path(root, RUN_A),
        turns=((0, "user", "u0"), (1, "assistant", "a0"), (2, "user", "u1")),
    )
    dry = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=None,
        ledger=ImportLedger("codex_cli"),
        dry_run=True,
        progress=_quiet,
    )
    real = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=_FakeClient(),
        ledger=ImportLedger("codex_cli"),
        progress=_quiet,
    )
    assert dry.turns_ingested == real.turns_ingested == 3


def test_second_run_is_entirely_already_imported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = tmp_path / "sessions"
    _write_rollout(_rollout_path(root, RUN_A), turns=((0, "user", "u0"), (1, "assistant", "a0")))
    ledger = ImportLedger("codex_cli")
    first = run_import(
        source=_source(),
        root=root,
        profile_id="default",
        client=_FakeClient(),
        ledger=ledger,
        progress=_quiet,
    )
    client = _FakeClient()
    second = run_import(
        source=_source(), root=root, profile_id="default", client=client, ledger=ledger, progress=_quiet
    )
    assert first.sessions_imported == 1
    assert second.sessions_imported == 0
    assert second.skipped == {"already-imported": 1}
    assert not [call for call in client.calls if call[0] == "POST"]
