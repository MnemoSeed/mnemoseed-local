"""Pool-credit suppression (#394 S2): a history-import settle skips the score
pool without touching the verbatim write path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import auth_support
import pytest
from _ports import unused_loopback_port
from fastapi.testclient import TestClient

from mnemoseed_local.capture.pipeline import ScoringPipeline
from mnemoseed_local.capture.pool import ScorePool
from mnemoseed_local.capture.scorer import TurnScorer
from mnemoseed_local.schema.turn import HostId, SessionEndRequest, Turn, TurnRole, TurnStep
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder

PROFILE = "default"
SESSION = "sess-credit"
_TEXT = "我决定以后都用 pnpm 管理依赖"


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'baseurl = "http://127.0.0.1:{unused_loopback_port()}"\n'
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


def _turn(text: str, *, index: int = 0) -> Turn:
    return Turn(
        turn_index=index,
        session_id=SESSION,
        profile_id=PROFILE,
        host=HostId.GENERIC,
        started_at=0.0,
        steps=[TurnStep(role=TurnRole.USER, content=text)],
    )


def _pipeline(pool: ScorePool) -> ScoringPipeline:
    return ScoringPipeline(scorer=TurnScorer(embedder=SyntheticEmbedder()), pool=pool)


def test_credit_pool_defaults_true_and_accepts_false() -> None:
    base = SessionEndRequest(session_id=SESSION, profile_id=PROFILE)
    assert base.credit_pool is True
    explicit = SessionEndRequest(session_id=SESSION, profile_id=PROFILE, credit_pool=False)
    assert explicit.credit_pool is False


def test_drain_without_credit_skips_pool_and_records_suppressed_points() -> None:
    pool = ScorePool(clock=lambda: 1000.0)
    pipe = _pipeline(pool)
    pipe.submit_turn(_turn(_TEXT))
    pipe.drain(SESSION, credit_pool=False)
    assert pool.stats(PROFILE) is None
    assert pipe.stats.turns_in == 1
    assert pipe.stats.pool_points_suppressed > 0.0


def test_drain_default_still_credits_pool() -> None:
    pool = ScorePool(clock=lambda: 1000.0)
    pipe = _pipeline(pool)
    pipe.submit_turn(_turn(_TEXT))
    pipe.drain(SESSION)
    assert pool.stats(PROFILE) is not None
    assert pool.stats(PROFILE).turns_pooled == 1
    assert pipe.stats.pool_points_suppressed == 0.0


def _ingest(client: TestClient) -> None:
    response = client.post(
        "/ingest",
        json={
            "host": HostId.CLAUDE_CODE.value,
            "event": "user_prompt",
            "session_id": SESSION,
            "profile_id": PROFILE,
            "ts": 1.0,
            "content": {"text": _TEXT},
        },
    )
    assert response.status_code == 202, response.text


def test_settle_without_credit_leaves_pool_untouched_but_writes_the_turn(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client)
        settled = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE, "credit_pool": False},
        )
        assert settled.status_code == 200, settled.text
        assert client.app.state.score_pool.stats(PROFILE) is None
        assert client.app.state.capture.stats.turns_written == 1


def test_settle_omitting_the_field_credits_the_pool_as_before(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client)
        settled = client.post("/session/end", json={"session_id": SESSION, "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        assert client.app.state.score_pool.stats(PROFILE) is not None
        assert client.app.state.score_pool.stats(PROFILE).turns_pooled == 1


def test_settle_forwards_the_flag_to_the_drain(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        seen: list[bool] = []
        original = client.app.state.capture.drain

        def recording_drain(session_id: str, *, credit_pool: bool = True) -> Any:
            seen.append(credit_pool)
            return original(session_id, credit_pool=credit_pool)

        client.app.state.capture.drain = recording_drain
        _ingest(client)
        settled = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE, "credit_pool": False},
        )
        assert settled.status_code == 200, settled.text
        assert seen == [False]


def test_settle_response_carries_funnel_and_suppressed_points(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client)
        settled = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": PROFILE, "credit_pool": False},
        )
        assert settled.status_code == 200, settled.text
        body = settled.json()
        assert body["pool_points_suppressed"] > 0.0
        assert body["new_chunk"] + body["reinforced"] + body["needs_reconcile"] == 1


def test_settle_response_suppressed_points_are_zero_when_crediting(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client)
        settled = client.post("/session/end", json={"session_id": SESSION, "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        assert settled.json()["pool_points_suppressed"] == 0.0
