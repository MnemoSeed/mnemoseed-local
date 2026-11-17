"""History import against a real daemon: the producer is bound to the ingest
and settle routes, the store, and the resume ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import auth_support
import pytest
from fastapi.testclient import TestClient
from test_import_core import RUN_A, RUN_B, _rollout_path, _source, _write_rollout

from mnemoseed_local.daemon.observability import Observability
from mnemoseed_local.importer.ledger import ImportLedger, LedgerEntry
from mnemoseed_local.importer.runner import run_import
from mnemoseed_local.rest_client import ACTOR_HEADER, DaemonRestError
from mnemoseed_local.storage.ports import ChunkFilter, Page

pytest_plugins = ["test_credit_pool_suppression"]

PROFILE = "default"
TEXT_A = "历史导入逐字保留这段独立的用户陈述"
TEXT_B = "a distinct assistant reply about vector retention policy"
TEXT_C = "another unrelated historical prompt about cache eviction"


class _ClientAdapter:
    """run_import's HTTP seam, backed by a real daemon TestClient."""

    actor = "cli"

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {ACTOR_HEADER: self.actor}

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.get(path, params=params, headers=self._headers())
        if response.status_code >= 400:
            raise DaemonRestError(response.status_code, None)
        return response.json() if response.content else {}

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.post(path, json=body, headers=self._headers())
        if response.status_code >= 400:
            raise DaemonRestError(response.status_code, None)
        return response.json() if response.content else {}


def _chunks(client: TestClient) -> list[Any]:
    return list(
        client.app.state.stores.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(limit=1000)).items
    )


class _CrashingLedger(ImportLedger):
    """Fails the first ledger write, modelling a crash after settle 200."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail_next = True

    def record(self, entry: LedgerEntry) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("crash before ledger write")
        super().record(entry)


def test_run_import_against_a_real_daemon(
    tmp_path: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    root = tmp_path / "sessions"
    _write_rollout(
        _rollout_path(root, RUN_A),
        conversation_id="conv-shared",
        turns=((0, "user", TEXT_A), (1, "assistant", TEXT_B)),
    )
    _write_rollout(
        _rollout_path(root, RUN_B).with_name(f"rollout-b-{RUN_B}.jsonl.zst"),
        conversation_id="conv-shared",
        turns=((0, "user", TEXT_C),),
    )
    ledger = ImportLedger("codex_cli")
    ingest_notes: list[Any] = []
    original_note = Observability.note_capture_ingest

    def spy_note(self: Observability, *args: Any, **kwargs: Any) -> Any:
        ingest_notes.append((args, kwargs))
        return original_note(self, *args, **kwargs)

    monkeypatch.setattr(Observability, "note_capture_ingest", spy_note)
    with auth_support.boot(config_path.parent) as client:
        adapter = _ClientAdapter(client)
        first = run_import(source=_source(), root=root, profile_id=PROFILE, client=adapter, ledger=ledger)
        first_chunks = _chunks(client)
        assert first.sessions_imported == 2
        assert ledger.get(RUN_A).settled_through_ordinal == 1
        assert ledger.get(RUN_B).settled_through_ordinal == 0
        assert len(first_chunks) >= 2
        assert any(TEXT_A in chunk.text for chunk in first_chunks)
        assert client.get("/api/v1/observability").json()["capture_ingest_count"] == 0

        second = run_import(source=_source(), root=root, profile_id=PROFILE, client=adapter, ledger=ledger)
        assert second.skipped == {"already-imported": 2}
        assert len(_chunks(client)) == len(first_chunks)
        assert not ingest_notes


def test_rerun_after_crash_between_settle_and_ledger(
    tmp_path: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    root = tmp_path / "sessions"
    _write_rollout(
        _rollout_path(root, RUN_A),
        turns=((0, "user", TEXT_A), (1, "assistant", TEXT_B)),
    )
    crashing = _CrashingLedger("codex_cli")
    with auth_support.boot(config_path.parent) as client:
        adapter = _ClientAdapter(client)
        with pytest.raises(RuntimeError):
            run_import(source=_source(), root=root, profile_id=PROFILE, client=adapter, ledger=crashing)
        after_crash = _chunks(client)
        assert after_crash
        assert crashing.get(RUN_A) is None

    with auth_support.boot(config_path.parent) as client:
        adapter = _ClientAdapter(client)
        resumed = run_import(
            source=_source(),
            root=root,
            profile_id=PROFILE,
            client=adapter,
            ledger=ImportLedger("codex_cli"),
        )
        assert resumed.sessions_imported == 1
        assert len(_chunks(client)) == len(after_crash)
        assert sum(TEXT_A in chunk.text for chunk in _chunks(client)) == 1
        assert client.app.state.score_pool.stats(PROFILE) is None
