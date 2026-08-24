"""`dream status` answers the owner's data-status question: pending pool vs
threshold, digested watermark, and dream history (committed runs + failed
extractions by class). Missing data renders honest zeros/nulls."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.storage.ports import AuditEntry, DreamRun, TurnRange

PROFILE = "default"


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


def _client(config_path: Path) -> TestClient:
    return TestClient(create_app())


def test_dream_status_reports_pool_watermark_and_history(config_path: Path) -> None:
    with _client(config_path) as client:
        stores = client.app.state.stores  # type: ignore[attr-defined]
        stores.meta.pool_credit(PROFILE, 2.05, TurnRange(start=3, end=3))
        finished = time.time() - 60.0
        stores.meta.record_dream_run(
            DreamRun(run_id="r1", started_at=finished - 30.0, finished_at=finished, tokens=100)
        )
        for cls in ("truncated_delta_deferred", "llm_unreachable", "llm_unreachable"):
            stores.meta.audit_append(
                AuditEntry(
                    actor="dream",
                    action="dream_extract_failed",
                    detail={"failure_class": cls},
                    at=time.time(),
                )
            )

        body = client.post("/memory/dream_status", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        payload = body.json()

        assert payload["pool"]["balance"] == pytest.approx(2.05)
        assert payload["pool"]["threshold"] > 0.0
        assert payload["watermark"] == {"start": 3, "end": 3}
        assert payload["history"]["committed_runs"] >= 1
        assert payload["history"]["last_commit_at"] is not None
        assert payload["history"]["extract_failures"] == {
            "truncated_delta_deferred": 1,
            "llm_unreachable": 2,
        }
        # legacy lines survive
        assert "state" in payload


def test_dream_status_empty_profile_renders_honest_nulls(config_path: Path) -> None:
    with _client(config_path) as client:
        body = client.post("/memory/dream_status", json={"profile_id": "fresh-profile"})
        assert body.status_code == 200, body.text
        payload = body.json()

        assert payload["pool"]["balance"] == 0.0
        assert payload["watermark"] is None
        assert payload["history"]["committed_runs"] == 0
        assert payload["history"]["last_commit_at"] is None
        assert payload["history"]["extract_failures"] == {}


def test_cli_dream_status_prints_the_status_block(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "state": "idle",
        "pending_queue": 0,
        "pending_manual": 0,
        "pool": {"balance": 2.05, "threshold": 1.0},
        "watermark": {"start": 3, "end": 3},
        "history": {
            "committed_runs": 2,
            "last_commit_at": "2026-08-24T07:39:04Z",
            "extract_failures": {"truncated_delta_deferred": 1},
        },
    }

    class _StubClient:
        profile_id = PROFILE

        def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
            assert path == "/memory/dream_status"
            return payload

    from mnemoseed_local import cli, rest_client

    monkeypatch.setattr(rest_client, "resolve_client", lambda args: _StubClient())
    assert cli.main(["dream", "status"]) == 0
    out = capsys.readouterr().out
    assert "pool: 2.05 / 1.0 pts" in out
    assert "digested turns: 3..3" in out
    assert "dreams committed: 2" in out
    assert "last dream: 2026-08-24T07:39:04Z" in out
    assert "extraction failures: truncated_delta_deferred=1" in out


def test_dream_status_counts_exact_beyond_single_page(config_path: Path) -> None:
    with _client(config_path) as client:
        stores = client.app.state.stores  # type: ignore[attr-defined]
        for i in range(505):
            stores.meta.audit_append(
                AuditEntry(
                    actor="dream",
                    action="dream_extract_failed",
                    detail={"failure_class": "llm_unreachable" if i % 2 else "over_budget"},
                    at=time.time(),
                )
            )
        for i in range(205):
            stores.meta.record_dream_run(
                DreamRun(run_id=f"r{i}", started_at=float(i), finished_at=float(i) + 1.0, tokens=1)
            )

        body = client.post("/memory/dream_status", json={"profile_id": PROFILE})
        payload = body.json()
        assert payload["history"]["extract_failures"] == {
            "over_budget": 253,
            "llm_unreachable": 252,
        }
        assert payload["history"]["committed_runs"] == 205
        # newest run wins (list is started_at DESC; last finish = 204+1)
        assert payload["history"]["last_commit_at"] is not None


def test_cli_prints_none_for_zero_failures(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "state": "idle",
        "pending_queue": 0,
        "pending_manual": 0,
        "pool": {"balance": 0.0, "threshold": 1.0},
        "watermark": None,
        "history": {"committed_runs": 1, "last_commit_at": "t", "extract_failures": {}},
    }

    class _StubClient:
        profile_id = PROFILE

        def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
            return payload

    from mnemoseed_local import cli, rest_client

    monkeypatch.setattr(rest_client, "resolve_client", lambda args: _StubClient())
    assert cli.main(["dream", "status"]) == 0
    out = capsys.readouterr().out
    assert "extraction failures: none" in out
    assert "digested turns: none yet" in out


def test_cli_json_mode_emits_extended_fields(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    payload = {
        "state": "idle",
        "pool": {"balance": 1.5, "threshold": 1.0},
        "watermark": {"start": 1, "end": 2},
        "history": {"committed_runs": 1, "last_commit_at": "t", "extract_failures": {}},
    }

    class _StubClient:
        profile_id = PROFILE

        def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
            return payload

    from mnemoseed_local import cli, rest_client

    monkeypatch.setattr(rest_client, "resolve_client", lambda args: _StubClient())
    assert cli.main(["dream", "status", "--json"]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["pool"]["balance"] == 1.5
    assert emitted["history"]["committed_runs"] == 1
