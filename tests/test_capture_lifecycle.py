"""Senior-QA-review (2026-08-19) capture-lifecycle regression tests.

Three daemon-side findings from the adversarial review of the B2.1 baseline
fixes:

- QA-3: the segmenter's response-boundary rule was host-blind — written for
  anchor-less hosts (Cursor), it chopped EVERY multi-block assistant reply
  into orphaned assistant-only turns even when a user_prompt anchor was right
  there, degrading recall context on the verbatim lane.
- QA-4: daemon shutdown never drained the in-memory capture buffers — open
  turns (and anything closed-but-unflushed) died silently with the process.
- QA-5 (daemon half): settled sessions kept every buffered turn forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.capture import InMemoryCapturePipeline, ScoringPipeline, TurnSegmenter
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.turn import (
    HostId,
    IngestEvent,
    IngestEventType,
    MessageContent,
    ToolContent,
    TurnRole,
)

PROFILE = "default"
HOST = HostId.OPENCODE


def _message(etype: IngestEventType, session: str, ts: float, text: str) -> IngestEvent:
    return IngestEvent(
        host=HOST,
        event=etype,
        session_id=session,
        profile_id=PROFILE,
        ts=ts,
        content=MessageContent(text=text),
    )


def _tool(session: str, ts: float, name: str = "bash", output: str = "ok") -> IngestEvent:
    return IngestEvent(
        host=HOST,
        event=IngestEventType.TOOL_USE,
        session_id=session,
        profile_id=PROFILE,
        ts=ts,
        content=ToolContent(tool_name=name, input={}, output=output),
    )


# ---------------------------------------------------------------- QA-3


def test_user_anchored_turn_absorbs_a_multi_block_assistant_reply() -> None:
    """opencode's dominant mode (tool loops) emits SEVERAL assistant messages
    per user request; a user-anchored open turn must keep them together so
    the verbatim chunk recalls the reply WITH its request."""
    pipeline = InMemoryCapturePipeline()
    segmenter = TurnSegmenter(pipeline)
    session = "sess-anchored"
    segmenter.ingest(_message(IngestEventType.USER_PROMPT, session, 1.0, "第一问"))
    segmenter.ingest(_message(IngestEventType.ASSISTANT_MESSAGE, session, 2.0, "答·第一段"))
    segmenter.ingest(_tool(session, 3.0))
    segmenter.ingest(_message(IngestEventType.ASSISTANT_MESSAGE, session, 4.0, "答·第二段"))
    assert segmenter.flush(session, PROFILE) == 1
    turns = pipeline.turns(session)
    assert len(turns) == 1, "multi-block reply must NOT fragment when user-anchored"
    assert [step.role for step in turns[0].steps] == [
        TurnRole.USER,
        TurnRole.ASSISTANT,
        TurnRole.TOOL,
        TurnRole.ASSISTANT,
    ]


def test_anchorless_stream_still_segments_on_response_boundaries() -> None:
    """The response-boundary rule survives for streams without a user anchor
    (Cursor-class hosts): a second assistant message after an assistant step
    still closes the preceding turn."""
    pipeline = InMemoryCapturePipeline()
    segmenter = TurnSegmenter(pipeline)
    session = "sess-anchorless"
    segmenter.ingest(_message(IngestEventType.ASSISTANT_MESSAGE, session, 1.0, "回复一"))
    segmenter.ingest(_message(IngestEventType.ASSISTANT_MESSAGE, session, 2.0, "回复二"))
    segmenter.end_session(session, PROFILE)
    turns = pipeline.turns(session)
    assert len(turns) == 2
    assert [[step.role for step in turn.steps] for turn in turns] == [
        [TurnRole.ASSISTANT],
        [TurnRole.ASSISTANT],
    ]


# ---------------------------------------------------------------- QA-5 (daemon)


def test_settled_session_buffers_are_pruned_after_drain() -> None:
    """Every buffered layer of a settled session must be evictable: the
    funnel reads (recent-replay / scoring view) live pre-merge in the STORE,
    not in these builders' buffers, so post-drain RAM can be returned."""
    inner = ScoringPipeline()
    segmenter = TurnSegmenter(inner)
    session = "sess-prune"
    segmenter.ingest(_message(IngestEventType.USER_PROMPT, session, 1.0, "我决定天天写测试"))
    segmenter.ingest(_message(IngestEventType.ASSISTANT_MESSAGE, session, 2.0, "好主意"))
    segmenter.end_session(session, PROFILE)
    inner.drain(session)
    assert session in inner.sessions(), "precondition: buffers exist"
    inner.prune_settled(session)
    assert session not in inner.sessions()
    assert inner.turns(session) == []


def test_prune_settled_is_a_noop_for_open_sessions() -> None:
    inner = ScoringPipeline()
    segmenter = TurnSegmenter(inner)
    session = "sess-open"
    segmenter.ingest(_message(IngestEventType.USER_PROMPT, session, 1.0, "还没结算"))
    assert segmenter.flush(session, PROFILE) == 1  # submit the turn, keep the session open
    inner.drain(session)
    inner.prune_settled(session)
    assert session in inner.sessions(), "an unsettled session must keep its buffers"


# ---------------------------------------------------------------- QA-4


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Embedded single-process daemon config (same harness as test_daemon)."""
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


def test_daemon_shutdown_flushes_open_turns_and_drains(config_path: Path) -> None:
    """Dogfood reality: daemons get restarted mid-session. The lifespan
    teardown must close every in-flight turn and drain it — silently losing
    the last exchange of every restart is exactly the class of bug this
    batch exists to kill."""
    app = create_app()
    with TestClient(app) as client:
        opened = client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "user_prompt",
                "session_id": "sess-shutdown",
                "profile_id": PROFILE,
                "ts": 1.0,
                "content": {"text": "重启前最后一句"},
            },
        )
        assert opened.status_code == 202, opened.text
        drain_calls: list[str] = []
        original_drain = app.state.capture.drain

        def recording_drain(session_id: str):
            drain_calls.append(session_id)
            return original_drain(session_id)

        app.state.capture.drain = recording_drain
    # context exit == daemon shutdown: open turn closed, its buffer drained
    assert "sess-shutdown" in drain_calls


def test_segmenter_flush_all_closes_every_open_turn() -> None:
    pipeline = InMemoryCapturePipeline()
    segmenter = TurnSegmenter(pipeline)
    segmenter.ingest(_message(IngestEventType.USER_PROMPT, "s1", 1.0, "甲"))
    segmenter.ingest(_message(IngestEventType.USER_PROMPT, "s2", 1.0, "乙"))
    assert segmenter.flush_all() == 2
    assert segmenter.flush_all() == 0, "idempotent: nothing left open"
    assert len(pipeline.turns("s1")) == 1
    assert len(pipeline.turns("s2")) == 1


# ---------------------------------------------------------------- B2.2 crash replay


def _post(client, event: str, session: str, ts: float, text: str) -> None:
    response = client.post(
        "/ingest",
        json={
            "host": "opencode",
            "event": event,
            "session_id": session,
            "profile_id": PROFILE,
            "ts": ts,
            "content": {"text": text},
        },
    )
    assert response.status_code == 202, response.text


def _flush(client, session: str) -> None:
    flushed = client.post("/flush", json={"session_id": session, "profile_id": PROFILE})
    assert flushed.status_code == 200, flushed.text


def _tail_texts(client, session: str) -> list[str]:
    body = client.post("/session/recent", json={"profile_id": PROFILE, "sessions": 5, "per_session": 50})
    assert body.status_code == 200, body.text
    for group in body.json()["sessions"]:
        if group["session_id"] == session:
            return [c["text"] for c in group["chunks"]]
    return []


def test_crash_replay_of_host_history_is_absorbed_and_never_duplicated(config_path: Path) -> None:
    """PRD-B2.2 T3: the crash-resume replay re-posts tail turns the daemon may
    already hold; idempotency comes from the store's near-duplicate absorb —
    replaying the same tail twice (and across a daemon restart) must add ZERO
    chunks, while genuinely new turns still land. This is the first pin on
    the 'repeats must be absorbed' half of the 宁可重复不丢 contract."""
    session = "sess-crash"
    # ---- pre-crash: one drained turn
    with TestClient(create_app()) as client:
        _post(client, "user_prompt", session, 1.0, "崩溃前的用户消息")
        _post(client, "assistant_message", session, 2.0, "崩溃前的助手回复")
        _flush(client, session)
        assert _tail_texts(client, session) == ["user: 崩溃前的用户消息\nassistant: 崩溃前的助手回复"]
    # ---- daemon restarted over the same stores; the hook replays the tail
    with TestClient(create_app()) as client:
        _post(client, "user_prompt", session, 1.0, "崩溃前的用户消息")
        _post(client, "assistant_message", session, 2.0, "崩溃前的助手回复")
        _flush(client, session)
        assert _tail_texts(client, session) == ["user: 崩溃前的用户消息\nassistant: 崩溃前的助手回复"], (
            "replayed tail must be absorbed, not duplicated"
        )
        # live traffic continues after the reconcile — only it lands
        _post(client, "user_prompt", session, 3.0, "重启后的新消息")
        _post(client, "assistant_message", session, 4.0, "重启后的新回复")
        _flush(client, session)
        texts = _tail_texts(client, session)
        assert texts == [
            "user: 崩溃前的用户消息\nassistant: 崩溃前的助手回复",
            "user: 重启后的新消息\nassistant: 重启后的新回复",
        ], f"unexpected tail: {texts}"
