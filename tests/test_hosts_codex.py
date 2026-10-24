"""#272 S2b: Codex CLI host hook adapter (PRD-272 §3.3/§5, §9.3/§9.5).

Golden fixtures under ``tests/fixtures/codex_hook/`` pin the official stdin
contract the ``_hook-event`` transformer consumes; the rollout transcript at
``transcript_path`` is the facts source and the hook is only the clock.
Transformer tests pin: codex_cli capture attribution on /ingest, the T1
session-start replay lane, the T2 mid-session recall lane, the transcript
differential (per-turn ordinals on the wire, the first ack as the intra-
process send floor), and the zero-stdout red line on every other path. The
watermark three-state tests boot the REAL daemon and assert the ingest-
boundary oracle — segmenter acceptances and settled turn counts, never HTTP
counts (§9.3: same-state replay still travels, the daemon skips it). Lifecycle
tests pin the marked-entry surgery on the sandbox ``CODEX_HOME`` (Claude-Code-
compatible ``hooks.json`` command entries, the ``config.toml`` marker block
for the stdio MCP gateway) and the three honest trust states from
``[hooks.state]``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import auth_support
import pytest
from fastapi.testclient import TestClient
from test_stamper import _Clock, _FakeVectorStore

from mnemoseed_local.capture import TurnSegmenter
from mnemoseed_local.capture.scorer import Durability, DurabilityResult, ScoreComponents, ScoredTurn
from mnemoseed_local.capture.stamper import StampWriter, WriteContext, WriteOutcomeKind
from mnemoseed_local.cli import main
from mnemoseed_local.hosts.codex_cli import events, transcript
from mnemoseed_local.hosts.codex_cli import install as cx_install
from mnemoseed_local.rest_client import DaemonClient, DaemonRestError, DaemonUnavailableError
from mnemoseed_local.schema.turn import (
    HostId,
    IngestEvent,
    IngestEventType,
    MessageContent,
    Turn,
    TurnRole,
    TurnStep,
)
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex_hook"

ENV_VARS = ("MNEMOSEED_LOCAL_BASEURL", "MNEMOSEED_LOCAL_PROFILE_ID", "MNEMOSEED_LOCAL_DEBUG")

SESSION = "cx-wm-01"
ACTOR_HEADER = "X-MnemoSeed-Actor"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def cx_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic Codex config root via the sandbox CODEX_HOME override."""
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def captured_posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Record DaemonClient.post calls instead of hitting the network."""
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    return calls


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _run(monkeypatch: pytest.MonkeyPatch, payload: object) -> int:
    _feed_stdin(monkeypatch, payload)
    return main(["_hook-event", "--host", "codex_cli"])


# ---------------------------------------------------------------- transcript fixtures


def _transcript_line(ordinal: int, role: str, text: str, kind: str = "response_item") -> str:
    return json.dumps(
        {
            "ordinal": ordinal,
            "timestamp": "2026-10-24T10:00:00.000Z",
            "type": kind,
            "payload": {
                "type": "message",
                "role": role,
                "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
            },
        },
        ensure_ascii=False,
    )


def _write_transcript(path: Path, turns: list[tuple[int, str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_transcript_line(*t) for t in turns) + "\n", encoding="utf-8")
    return path


def _payload_with(name: str, tmp_path: Path, turns: list[tuple[int, str, str]], **overrides: object) -> dict:
    payload = dict(_fixture(name))
    payload["transcript_path"] = str(_write_transcript(tmp_path / "rollout.jsonl", turns))
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- daemon-under-test seam


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


@pytest.fixture
def daemon(config_path: Path) -> TestClient:
    with auth_support.boot(config_path.parent) as client:
        yield client


def _bridge(daemon: TestClient):
    """The hook's DaemonClient.post body, forwarded into the booted app."""

    def post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        response = daemon.post(path, json=body, headers={ACTOR_HEADER: self.actor})
        if response.status_code >= 400:
            raise DaemonRestError(response.status_code, None)
        return response.json() if response.content else {}

    return post


@pytest.fixture
def hook_posts(daemon: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict | None, dict]]:
    """Every hook POST: (path, body, reply) through the real daemon."""
    posts: list[tuple[str, dict | None, dict]] = []
    base = _bridge(daemon)

    def recording(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        reply = base(self, path, body)
        posts.append((path, body, reply))
        return reply

    monkeypatch.setattr(DaemonClient, "post", recording)
    return posts


@pytest.fixture
def accepted(daemon: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[IngestEvent]:
    """The events that reached the segmenter — the ingest-boundary oracle."""
    seen: list[IngestEvent] = []
    real = TurnSegmenter.ingest

    def spy(self: TurnSegmenter, event: IngestEvent) -> None:
        seen.append(event)
        real(self, event)

    monkeypatch.setattr(TurnSegmenter, "ingest", spy)
    return seen


def _crash_after(
    daemon: TestClient, monkeypatch: pytest.MonkeyPatch, limit: int
) -> list[tuple[str, dict | None]]:
    """A hook process that dies after ``limit`` successful POSTs (the mid-batch
    crash behind §9.3's replay story); returns the POSTs it managed to send."""
    base = _bridge(daemon)
    sent: list[tuple[str, dict | None]] = []
    state = {"n": 0}

    def crashing(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        if state["n"] >= limit:
            raise DaemonUnavailableError("hook process died mid-batch")
        state["n"] += 1
        sent.append((path, body))
        return base(self, path, body)

    monkeypatch.setattr(DaemonClient, "post", crashing)
    return sent


# ---------------------------------------------------------------- pull payload builders


def _pull_payload(**overrides: object) -> dict:
    payload = {
        "enabled": True,
        "items": [{"kind": "chunk", "id": "c1", "text": "中段关键线索"}],
        "non_focal_above_floor": 0,
        "budget_chars": events.RECALL_PULL_MAX_CHARS,
        "slot_consumed": False,
    }
    payload.update(overrides)
    return payload


def _capture_pull(monkeypatch: pytest.MonkeyPatch, payload: dict) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        if path == "/session/recall-pending":
            return payload
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    return calls


def _recent_payload(**overrides: object) -> dict:
    payload = {
        "profile_id": "default",
        "sessions": [
            {
                "session_id": "cx-earlier-session-77",
                "latest_at": 1761000000.0,
                "window": {
                    "first": "2026-10-20T10:00:00+00:00",
                    "latest": "2026-10-20T11:30:00+00:00",
                },
                "window_truncated": False,
                "active": False,
                "seconds_since_last_activity": 120.0,
                "chunks": [
                    {"chunk_id": "c1", "text": "第一条线索"},
                    {"chunk_id": "c2", "text": "第二条线索"},
                ],
            }
        ],
        "self_window": None,
        "guidance": "",
        "selection": "unscoped",
        "visibility": {"mode": "compact"},
    }
    payload.update(overrides)
    return payload


def _capture_recent(monkeypatch: pytest.MonkeyPatch, payload: dict) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        if path == "/session/recent":
            return payload
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    return calls


# ---------------------------------------------------------------- golden fixtures -> wire (§5.1)


def test_user_prompt_fixture_ingests_with_codex_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    turns = [(0, "user", "按模块梳理召回链路的测试盲区"), (1, "assistant", "答")]
    assert _run(monkeypatch, _payload_with("user_prompt", tmp_path, turns)) == 0
    ingests = [body for path, body in captured_posts if path == "/ingest"]
    assert len(ingests) == 1
    event = IngestEvent.model_validate(ingests[0])
    assert event.host is HostId.CODEX_CLI
    assert event.event is IngestEventType.USER_PROMPT
    assert event.session_id == "cx-sess-01"
    assert isinstance(event.content, MessageContent)
    assert event.content.text == "按模块梳理召回链路的测试盲区"
    assert event.transcript_ordinal == 0
    assert ingests[0]["raw"]["turn_id"] == "turn-0001"


def test_user_prompt_anchor_uses_the_newest_matching_user_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    turns = [(0, "user", "第一问"), (1, "assistant", "答一"), (5, "user", "第二问")]
    payload = _payload_with("user_prompt", tmp_path, turns, prompt="第一问")
    assert _run(monkeypatch, payload) == 0
    assert captured_posts[0][1]["transcript_ordinal"] == 0


def test_unanchorable_prompt_ingests_without_an_ordinal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    payload = _payload_with("user_prompt", tmp_path, [(0, "user", "另一句话")], prompt="完全不匹配的提问")
    assert _run(monkeypatch, payload) == 0
    assert len(captured_posts) == 2, "the ingest still fires, then the T2 pull"
    assert captured_posts[0][1]["transcript_ordinal"] is None


def test_stop_fixture_posts_the_transcript_differential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    turns = [(0, "user", "问题"), (1, "assistant", "答复一"), (2, "assistant", "答复二")]
    assert _run(monkeypatch, _payload_with("stop", tmp_path, turns)) == 0
    ingests = [body for path, body in captured_posts if path == "/ingest"]
    assert [(body["transcript_ordinal"], body["event"]) for body in ingests] == [
        (1, "assistant_message"),
        (2, "assistant_message"),
    ]
    assert all(body["content"]["text"] in ("答复一", "答复二") for body in ingests)
    assert all(body["content"]["model_id"] == "gpt-5-codex" for body in ingests)


def test_differential_ignores_non_conversation_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": "x"}}),
        _transcript_line(1, "developer", "instructions not captured"),
        _transcript_line(2, "user", "问题"),
        json.dumps({"ordinal": 3, "type": "event_msg", "payload": {"kind": "token_count"}}),
        json.dumps(
            {
                "ordinal": 4,
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]},
            }
        ),
        _transcript_line(5, "assistant", "答复"),
        json.dumps(
            {
                "ordinal": 6,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": " "}],
                },
            }
        ),
        "not json at all",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = dict(_fixture("stop"))
    payload["transcript_path"] = str(path)
    assert _run(monkeypatch, payload) == 0
    ingests = [body for path_, body in captured_posts if path_ == "/ingest"]
    assert [(body["transcript_ordinal"], body["event"]) for body in ingests] == [(5, "assistant_message")]


def test_session_end_fixture_settles_after_the_differential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    turns = [(0, "user", "问题"), (1, "assistant", "收尾答复")]
    assert _run(monkeypatch, _payload_with("session_end", tmp_path, turns)) == 0
    assert [path for path, _ in captured_posts] == ["/ingest", "/session/end"]
    settle = captured_posts[1][1]
    assert set(settle) == {"session_id", "profile_id", "ts"}
    assert settle["session_id"] == "cx-sess-01"
    assert settle["profile_id"] == "default"


def test_session_start_fixture_maps_to_the_t1_read_lane(
    monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    assert _run(monkeypatch, _fixture("session_start")) == 0
    assert [path for path, _ in captured_posts] == ["/session/recent"]


def test_stamper_derives_codex_cli_chat_source_prefix() -> None:
    turn = Turn(
        turn_index=0,
        session_id="cx-sess-01",
        profile_id="prof-main",
        host=HostId.CODEX_CLI,
        model_id="test-model",
        started_at=0.0,
        steps=[TurnStep(role=TurnRole.USER, content="hello")],
    )
    scored = ScoredTurn(
        turn=turn,
        importance=5.0,
        components=ScoreComponents(arousal=5.0, novelty=5.0, causal_chain=2.0),
        durability=DurabilityResult(durability=Durability.DURABLE, confidence=0.85, reasons=["pref-marker"]),
        emotion=None,
        causal_reasons=[],
        features={},
    )
    store = _FakeVectorStore()
    outcome = StampWriter(store=store, embedder=SyntheticEmbedder(), clock=_Clock()).write(
        scored, WriteContext(profile_id="prof-main")
    )
    assert outcome.kind is WriteOutcomeKind.NEW_CHUNK
    assert store.chunks[outcome.chunk_id].provenance.source == "codex_cli-chat"


# ---------------------------------------------------------------- T1 session-start replay (§5.3)


def test_session_start_pulls_recent_once_with_the_legacy_identity_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_recent(monkeypatch, _recent_payload())
    assert _run(monkeypatch, _fixture("session_start")) == 0
    assert [path for path, _ in calls] == ["/session/recent"]
    body = calls[0][1]
    assert set(body) == {"profile_id", "sessions", "per_session", "exclude_session_id"}
    assert body["sessions"] == 2
    assert body["per_session"] == 8
    assert body["exclude_session_id"] == "cx-sess-01"


def test_session_start_injects_the_fenced_replay_under_compact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_recent(monkeypatch, _recent_payload())
    assert _run(monkeypatch, _fixture("session_start")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    block = out["hookSpecificOutput"]["additionalContext"]
    assert block.startswith(events.RECALL_FENCE_OPEN)
    assert block.endswith(events.RECALL_FENCE_CLOSE)
    assert block.count("<mnemoseed-memory-recall>") == 1
    assert block.split("\n")[1] == events.T1_SIGNAL
    assert '<session-tail id="77" ' in block
    assert len(block) <= 4000


def test_session_start_without_served_content_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_recent(monkeypatch, _recent_payload(sessions=[]))
    assert _run(monkeypatch, _fixture("session_start")) == 0
    assert capsys.readouterr().out == ""


def test_session_start_off_or_absent_visibility_drops_the_signal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for visibility in ({"mode": "off"}, None):
        payload: dict = _recent_payload()
        if visibility is None:
            payload.pop("visibility", None)
        else:
            payload["visibility"] = visibility
        _capture_recent(monkeypatch, payload)
        assert _run(monkeypatch, _fixture("session_start")) == 0
        out = json.loads(capsys.readouterr().out)
        assert "[mnemoseed]" not in out["hookSpecificOutput"]["additionalContext"], visibility


def test_session_start_pull_failure_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonRestError(503, "daemon down")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    assert _run(monkeypatch, _fixture("session_start")) == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- T2 mid-session recall (§5.3)


def test_user_prompt_injects_t2_after_the_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    turns = [(0, "user", "按模块梳理召回链路的测试盲区"), (1, "assistant", "答")]
    calls = _capture_pull(monkeypatch, _pull_payload(visibility={"mode": "compact", "capture": False}))
    assert _run(monkeypatch, _payload_with("user_prompt", tmp_path, turns)) == 0
    assert [path for path, _ in calls] == ["/ingest", "/session/recall-pending"]
    pull_body = calls[1][1]
    assert pull_body == {"profile_id": "default", "session_id": "cx-sess-01", "seen_chunk_ids": []}
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    block = out["hookSpecificOutput"]["additionalContext"]
    assert "中段关键线索" in block
    assert events.T2_SIGNAL in block


def test_user_prompt_off_or_absent_stays_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for visibility in ({"mode": "off", "capture": True}, None):
        payload: dict = _pull_payload(enabled=False, items=[])
        if visibility is not None:
            payload["visibility"] = visibility
        _capture_pull(monkeypatch, payload)
        turns = [(0, "user", "按模块梳理召回链路的测试盲区")]
        assert _run(monkeypatch, _payload_with("user_prompt", tmp_path, turns)) == 0
        assert capsys.readouterr().out == "", visibility


def test_ingest_rejection_never_pulls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        calls.append((path, body or {}))
        if path == "/ingest":
            raise DaemonRestError(409, "already settled")
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    turns = [(0, "user", "按模块梳理召回链路的测试盲区")]
    assert _run(monkeypatch, _payload_with("user_prompt", tmp_path, turns)) == 0
    assert [path for path, _ in calls] == ["/ingest"]
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- ack-refined differential (hook side)


def test_differential_uses_the_first_ack_as_the_intra_process_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    """After the first 202, the ack_ordinal starts the send floor: batch turns
    at or below it are already held by the daemon and are not re-sent."""
    turns = [(0, "user", "问"), (1, "assistant", "a1"), (2, "assistant", "a2"), (3, "assistant", "a3")]

    def fake_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        captured_posts.append((path, body or {}))
        if path == "/ingest" and len([1 for p, _ in captured_posts if p == "/ingest"]) == 1:
            return {"ack_ordinal": 2}
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    assert _run(monkeypatch, _payload_with("stop", tmp_path, turns)) == 0
    ordinals = [body["transcript_ordinal"] for path, body in captured_posts if path == "/ingest"]
    assert ordinals == [1, 3], "ordinal 2 rides the ack floor and stays unsent"


# ---------------------------------------------------------------- watermark three states (§5.2, real daemon)


def test_watermark_repeated_firing_stores_zero_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_posts: list[tuple[str, dict | None, dict]],
    accepted: list[IngestEvent],
    daemon: TestClient,
) -> None:
    turns = [(0, "user", "第一问"), (1, "assistant", "第一答")]
    prompt = _payload_with("user_prompt", tmp_path, turns, session_id=SESSION, prompt="第一问")
    stop = _payload_with("stop", tmp_path, turns, session_id=SESSION)
    assert _run(monkeypatch, prompt) == 0
    assert _run(monkeypatch, stop) == 0
    assert _run(monkeypatch, stop) == 0, "the same transcript state fires again"
    ingests = [body for path, body, _ in hook_posts if path == "/ingest"]
    assert len(ingests) == 3, "the stateless hook replays over HTTP (the documented lower bound)"
    assert [event.transcript_ordinal for event in accepted] == [0, 1], "the daemon stored each turn once"
    deduped_reply = hook_posts[-1][2]
    assert deduped_reply["ack_ordinal"] == 1, "the 202 echoes the stored high watermark"
    settled = daemon.post("/session/end", json={"session_id": SESSION, "profile_id": "default"})
    assert settled.status_code == 200
    assert settled.json()["turns"] == 1


def test_watermark_append_stores_exactly_the_increment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_posts: list[tuple[str, dict | None, dict]],
    accepted: list[IngestEvent],
    daemon: TestClient,
) -> None:
    v1 = [(0, "user", "第一问"), (1, "assistant", "第一答")]
    first = _payload_with("user_prompt", tmp_path, v1, session_id=SESSION, prompt="第一问")
    assert _run(monkeypatch, first) == 0
    assert _run(monkeypatch, _payload_with("stop", tmp_path, v1, session_id=SESSION)) == 0
    v2 = v1 + [(2, "user", "第二问"), (3, "assistant", "第二答")]
    second = _payload_with("user_prompt", tmp_path, v2, session_id=SESSION, prompt="第二问")
    assert _run(monkeypatch, second) == 0
    assert _run(monkeypatch, _payload_with("stop", tmp_path, v2, session_id=SESSION)) == 0
    assert [event.transcript_ordinal for event in accepted] == [0, 1, 2, 3], "exactly the appended turns"
    assert [event.event for event in accepted] == [
        IngestEventType.USER_PROMPT,
        IngestEventType.ASSISTANT_MESSAGE,
        IngestEventType.USER_PROMPT,
        IngestEventType.ASSISTANT_MESSAGE,
    ]
    settled = daemon.post("/session/end", json={"session_id": SESSION, "profile_id": "default"})
    assert settled.json()["turns"] == 2


def test_watermark_loss_replays_without_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_posts: list[tuple[str, dict | None, dict]],
    accepted: list[IngestEvent],
    daemon: TestClient,
) -> None:
    turns = [(0, "user", "问"), (1, "assistant", "a1"), (2, "assistant", "a2"), (3, "assistant", "a3")]
    opening = _payload_with("user_prompt", tmp_path, turns, session_id=SESSION, prompt="问")
    assert _run(monkeypatch, opening) == 0
    crashed = _crash_after(daemon, monkeypatch, limit=2)
    assert _run(monkeypatch, _payload_with("stop", tmp_path, turns, session_id=SESSION)) == 0
    crashed_ordinals = [body["transcript_ordinal"] for _, body in crashed if body]
    assert crashed_ordinals == [1, 2], "the crash cut the batch short"
    assert [event.transcript_ordinal for event in accepted] == [0, 1, 2]

    base = _bridge(daemon)
    recovery: list[tuple[str, dict | None, dict]] = []

    def recording(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        reply = base(self, path, body)
        recovery.append((path, body, reply))
        return reply

    monkeypatch.setattr(DaemonClient, "post", recording)
    assert _run(monkeypatch, _payload_with("stop", tmp_path, turns, session_id=SESSION)) == 0
    ordinals = [body["transcript_ordinal"] for path, body, _ in recovery if path == "/ingest" and body]
    assert ordinals == [1, 3], "the ack floor (2) skips ordinal 2 without an HTTP send"
    stored = [event.transcript_ordinal for event in accepted]
    assert stored == [0, 1, 2, 3], "replay filled the gap, no duplicates"
    settled = daemon.post("/session/end", json={"session_id": SESSION, "profile_id": "default"})
    assert settled.json()["turns"] == 1


def test_legacy_202_body_stays_byte_identical(daemon: TestClient) -> None:
    """Hosts that never send the ordinal keep the exact pre-change 202 body;
    an ordinal-carrying event gains only the additive ack_ordinal key."""
    legacy = {
        "host": "claude_code",
        "event": "user_prompt",
        "session_id": "cc-legacy-01",
        "profile_id": "default",
        "ts": 0.0,
        "content": {"text": "hello"},
    }
    response = daemon.post("/ingest", json=legacy)
    assert response.status_code == 202
    assert set(response.json()) == {"status", "session_id", "profile_id", "event"}

    stamped = dict(legacy, session_id="cc-stamped-01", transcript_ordinal=7)
    response = daemon.post("/ingest", json=stamped)
    assert response.status_code == 202
    assert response.json()["ack_ordinal"] == 7

    negative = dict(legacy, session_id="cc-negative-01", transcript_ordinal=-1)
    assert daemon.post("/ingest", json=negative).status_code == 422


# ---------------------------------------------------------------- stdin defense (§5.4)


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[1, 2]",
        '{"hook_event_name": "Interrupt", "session_id": "cx-1"}',
        '{"model": "gpt-5"}',
    ],
)
def test_malformed_unknown_or_identityless_stdin_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[tuple[str, dict]],
    capsys: pytest.CaptureFixture[str],
    raw: str,
) -> None:
    assert _run(monkeypatch, raw) == 0
    assert capsys.readouterr().out == ""
    assert captured_posts == []


def test_blank_prompt_is_dropped_silently(
    monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    payload = dict(_fixture("user_prompt"), prompt="   ")
    assert _run(monkeypatch, payload) == 0
    assert captured_posts == []


def test_all_lanes_fail_open_when_the_daemon_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_post(self: DaemonClient, path: str, body: dict | None = None) -> dict:
        raise DaemonUnavailableError("connection refused")

    monkeypatch.setattr(DaemonClient, "post", dead_post)
    for name in ("session_start", "user_prompt", "stop", "session_end"):
        turns = [(0, "user", "问"), (1, "assistant", "答")]
        assert _run(monkeypatch, _payload_with(name, tmp_path, turns)) == 0
        assert capsys.readouterr().out == "", name


# ---------------------------------------------------------------- transcript reader + lazy zstandard (§5.7)


def test_read_turns_filters_to_conversation_messages(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    lines = [
        json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": "x"}}),
        _transcript_line(1, "developer", "kept out"),
        _transcript_line(2, "user", "问题"),
        json.dumps({"ordinal": 3, "type": "event_msg", "payload": {}}),
        _transcript_line(4, "assistant", "答复"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    turns = transcript.read_turns(str(path))
    assert [(t.ordinal, t.role, t.text) for t in turns] == [(2, "user", "问题"), (4, "assistant", "答复")]


def test_read_turns_falls_back_to_the_line_index_ordinal(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    lines = [
        json.dumps({"type": "session_meta", "payload": {}}),
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "问"}],
                },
            },
            ensure_ascii=False,
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    turns = transcript.read_turns(str(path))
    assert [(t.ordinal, t.role) for t in turns] == [(1, "user")]


def test_read_turns_degrades_missing_and_garbage_files(tmp_path: Path) -> None:
    assert transcript.read_turns(None) == []
    assert transcript.read_turns("") == []
    assert transcript.read_turns(str(tmp_path / "absent.jsonl")) == []
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_bytes(b"\xff\xfe not utf-8 at all")
    assert transcript.read_turns(str(garbage)) == []


def test_zst_transcript_roundtrips_real_zstandard(tmp_path: Path) -> None:
    zstandard = pytest.importorskip("zstandard")
    lines = "\n".join(
        [
            _transcript_line(0, "user", "问"),
            _transcript_line(1, "assistant", "答"),
        ]
    ).encode("utf-8")
    path = tmp_path / "rollout.jsonl.zst"
    path.write_bytes(zstandard.ZstdCompressor().compress(lines))
    turns = transcript.read_turns(str(path))
    assert [(t.ordinal, t.role, t.text) for t in turns] == [
        (0, "user", "问"),
        (1, "assistant", "答"),
    ]


def test_zst_transcript_reads_through_the_lazy_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    raw = "\n".join(
        [
            _transcript_line(0, "user", "问"),
            _transcript_line(1, "assistant", "答"),
        ]
    ).encode("utf-8")
    module = ModuleType("zstandard")

    class _Reader:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def read(self) -> bytes:
            return raw

        def __enter__(self) -> _Reader:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    class _Decompressor:
        def stream_reader(self, stream: Any) -> _Reader:
            return _Reader(stream)

    module.ZstdDecompressor = _Decompressor
    monkeypatch.setitem(sys.modules, "zstandard", module)
    path = tmp_path / "rollout.jsonl.zst"
    path.write_bytes(b"compressed bytes go here")
    payload = dict(_fixture("stop"))
    payload["transcript_path"] = str(path)
    assert _run(monkeypatch, payload) == 0
    ordinals = [body["transcript_ordinal"] for path_, body in captured_posts if path_ == "/ingest"]
    assert ordinals == [1]


def test_missing_zstandard_answers_with_the_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "zstandard", None)
    path = tmp_path / "rollout.jsonl.zst"
    path.write_bytes(b"compressed")
    payload = dict(_fixture("stop"))
    payload["transcript_path"] = str(path)
    assert _run(monkeypatch, payload) == 0
    captured = capsys.readouterr()
    assert captured.out == "", "stdout stays empty even on a setup fault"
    assert "zstandard" in captured.err
    assert "[codex]" in captured.err


def test_missing_zstandard_still_settles_session_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_posts: list[tuple[str, dict]]
) -> None:
    monkeypatch.setitem(sys.modules, "zstandard", None)
    path = tmp_path / "rollout.jsonl.zst"
    path.write_bytes(b"compressed")
    payload = dict(_fixture("session_end"))
    payload["transcript_path"] = str(path)
    assert _run(monkeypatch, payload) == 0
    assert [path_ for path_, _ in captured_posts] == ["/session/end"], "settle survives the setup fault"


# ---------------------------------------------------------------- installer lifecycle (§5.5)


FOREIGN_HOOKS = {
    "Stop": [{"hooks": [{"type": "command", "command": "my-own-hook --flag"}]}],
}

FOREIGN_TOML = (
    "# user tuning\n"
    'model = "gpt-5-codex"\n'
    "\n"
    "[mcp_servers.node_repl]\n"
    'command = "npx"\n'
    'args = ["-y", "some/server"]\n'
    "\n"
    '[projects."G:/dev/demo"]\n'
    'trust_level = "trusted"\n'
)


@pytest.fixture
def stable_command(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    exe = str(Path(__file__).resolve())
    command = f"{exe} {' '.join(cx_install.TRANSFORM_ARGS)}"
    monkeypatch.setattr(cx_install, "transform_executable", lambda: exe)
    monkeypatch.setattr(cx_install, "transform_command", lambda: command)
    return exe, command


def test_install_writes_the_canonical_hooks_and_mcp_block(
    cx_home: Path, stable_command: tuple[str, str]
) -> None:
    hooks_file, changed = cx_install.install()
    assert changed is True
    assert hooks_file == cx_home / "hooks.json"
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert set(hooks) == set(cx_install.HOOK_EVENTS)
    exe, command = stable_command
    for event in cx_install.HOOK_EVENTS:
        groups = hooks[event]
        assert len(groups) == 1, event
        handler = groups[0]["hooks"][0]
        assert handler == {"type": "command", "command": command}
    config = (cx_home / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.mnemoseed]" in config
    entry = tomllib.loads(config)["mcp_servers"]["mnemoseed"]
    assert entry == {"command": exe, "args": ["mcp"]}


def test_install_is_idempotent_byte_identical(cx_home: Path, stable_command: tuple[str, str]) -> None:
    cx_install.install()
    hooks_first = (cx_home / "hooks.json").read_bytes()
    config_first = (cx_home / "config.toml").read_bytes()
    _, changed = cx_install.install()
    assert changed is False
    assert (cx_home / "hooks.json").read_bytes() == hooks_first
    assert (cx_home / "config.toml").read_bytes() == config_first


def test_install_never_mutates_foreign_entries(cx_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_file = cx_home / "hooks.json"
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(FOREIGN_HOOKS, indent=2) + "\n", encoding="utf-8")
    config = cx_home / "config.toml"
    config.write_text(FOREIGN_TOML, encoding="utf-8")
    cx_install.install()
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert hooks["Stop"][0] == FOREIGN_HOOKS["Stop"][0]
    assert len(hooks["Stop"]) == 2, "our group joins the foreign one"
    config_text = config.read_text(encoding="utf-8")
    assert config_text.startswith(FOREIGN_TOML), "foreign toml bytes stay byte-identical ahead of our block"


def test_uninstall_removes_exactly_our_entries(cx_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_file = cx_home / "hooks.json"
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(FOREIGN_HOOKS, indent=2) + "\n", encoding="utf-8")
    config = cx_home / "config.toml"
    config.write_text(FOREIGN_TOML, encoding="utf-8")
    cx_install.install()
    _, existed = cx_install.uninstall()
    assert existed is True
    assert json.loads(hooks_file.read_text(encoding="utf-8")) == FOREIGN_HOOKS
    assert config.read_text(encoding="utf-8") == FOREIGN_TOML, "the restored bytes equal the original"
    assert "mnemoseed" not in config.read_text(encoding="utf-8")


def test_uninstall_without_install_reports_absent(cx_home: Path) -> None:
    _, existed = cx_install.uninstall()
    assert existed is False


def test_dry_run_previews_without_writing(cx_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_file = cx_home / "hooks.json"
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(FOREIGN_HOOKS, indent=2) + "\n", encoding="utf-8")
    config = cx_home / "config.toml"
    config.write_text(FOREIGN_TOML, encoding="utf-8")
    _, would_change = cx_install.install(dry_run=True)
    assert would_change is True
    assert hooks_file.read_text(encoding="utf-8") == json.dumps(FOREIGN_HOOKS, indent=2) + "\n"
    assert config.read_text(encoding="utf-8") == FOREIGN_TOML
    _, changed = cx_install.install()
    assert changed is True


def test_dry_run_uninstall_previews_without_writing(cx_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_file = cx_home / "hooks.json"
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    seeded = json.dumps(FOREIGN_HOOKS, indent=2) + "\n"
    hooks_file.write_text(seeded, encoding="utf-8")
    config = cx_home / "config.toml"
    config.write_text(FOREIGN_TOML, encoding="utf-8")
    cx_install.install()
    _, would_change = cx_install.uninstall(dry_run=True)
    assert would_change is True
    installed_hooks = hooks_file.read_text(encoding="utf-8")
    installed_config = config.read_text(encoding="utf-8")
    assert "mnemoseed" in installed_hooks and "[mcp_servers.mnemoseed]" in installed_config
    _, existed = cx_install.uninstall()
    assert existed is True
    assert json.loads(hooks_file.read_text(encoding="utf-8")) == FOREIGN_HOOKS
    assert config.read_text(encoding="utf-8") == FOREIGN_TOML


def test_malformed_hooks_json_refuses_instead_of_corrupting(
    cx_home: Path, stable_command: tuple[str, str]
) -> None:
    hooks_file = cx_home / "hooks.json"
    cases = [
        '{\n  // user annotation\n  "Stop": []\n}\n',
        "{broken",
        json.dumps({"Stop": "garbage"}),
    ]
    for original in cases:
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        hooks_file.write_text(original, encoding="utf-8")
        with pytest.raises(cx_install.ConfigParseError):
            cx_install.install()
        assert hooks_file.read_text(encoding="utf-8") == original


def test_malformed_config_toml_refuses_instead_of_corrupting(
    cx_home: Path, stable_command: tuple[str, str]
) -> None:
    config = cx_home / "config.toml"
    cases = ["[broken\n", "model = \n", 'model = "gpt-5"\n[unclosed\n']
    for original in cases:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(original, encoding="utf-8")
        with pytest.raises(cx_install.ConfigParseError):
            cx_install.install()
        assert config.read_text(encoding="utf-8") == original


def test_foreign_keys_under_our_mcp_table_refuse(cx_home: Path, stable_command: tuple[str, str]) -> None:
    config = cx_home / "config.toml"
    original = (
        FOREIGN_TOML + '\n[mcp_servers.mnemoseed]\ncommand = "C:/tools/other.exe"\nenv_vars = { A = "1" }\n'
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(original, encoding="utf-8")
    with pytest.raises(cx_install.ConfigParseError):
        cx_install.install()
    assert config.read_text(encoding="utf-8") == original


def _seed_trusted_hash(cx_home: Path, digest: str) -> None:
    config = cx_home / "config.toml"
    with config.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[hooks.state."cx:hooks.json:stop:0:0"]\ntrusted_hash = "{digest}"\n')


def test_status_reports_the_three_trust_states(
    cx_home: Path, stable_command: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    assert cx_install.status().state == "not-installed"
    cx_install.install()
    assert cx_install.status().state == cx_install.TRUST_PENDING
    digest = hashlib.sha256((cx_home / "hooks.json").read_bytes()).hexdigest()
    _seed_trusted_hash(cx_home, digest)
    assert cx_install.status().state == "installed"
    hooks = json.loads((cx_home / "hooks.json").read_text(encoding="utf-8"))
    hooks["Stop"].append({"hooks": [{"type": "command", "command": "extra --flag"}]})
    (cx_home / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
    assert cx_install.status().state == cx_install.TRUST_STALE


def test_status_reports_the_config_path_and_daemon_probe(
    cx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_probe(base_url: str, timeout: float = 2.0) -> bool:
        seen["base_url"] = base_url
        return True

    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", fake_probe)
    info = cx_install.status()
    assert info.daemon_reachable is True
    assert info.path == cx_home / "hooks.json"
    assert info.root == cx_home
    assert seen["base_url"] == "http://localhost:7788"


def test_transform_command_resolves_an_absolute_tool_reference() -> None:
    command = cx_install.transform_command()
    assert command.endswith(" " + " ".join(cx_install.TRANSFORM_ARGS))
    head = command.split(" ", 1)[0]
    assert Path(head.strip('"')).is_absolute()


def test_foreign_module_form_mentioning_the_verb_is_never_ours() -> None:
    tail = " ".join(cx_install.TRANSFORM_ARGS)
    legit_module = f"{sys.executable} -m mnemoseed_local.cli {tail}"
    foreign_module = f"{sys.executable} -m evil_pkg {tail}"
    shim_form = f"{sys.executable} {tail}"
    assert cx_install.is_ours({"type": "command", "command": legit_module}) is True
    assert cx_install.is_ours({"type": "command", "command": shim_form}) is True
    assert cx_install.is_ours({"type": "command", "command": foreign_module}) is False
    assert cx_install.is_ours({"type": "command", "command": "my-own-hook --flag"}) is False
    assert cx_install.is_ours({"type": "process", "command": shim_form}) is False


def test_foreign_entry_mentioning_the_marker_is_kept_with_a_warning(
    cx_home: Path, stable_command: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    hooks_file = cx_home / "hooks.json"
    foreign = {"Stop": [{"hooks": [{"type": "command", "command": "mnemoseed-local-helper.exe watch"}]}]}
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(foreign, indent=2) + "\n", encoding="utf-8")
    _, changed = cx_install.install()
    assert changed is True
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert len(hooks["Stop"]) == 2
    _, existed = cx_install.uninstall()
    assert existed is True
    assert json.loads(hooks_file.read_text(encoding="utf-8")) == foreign
    assert "mnemoseed-local-helper" in capsys.readouterr().err


def test_utf8_foreign_content_survives_the_roundtrip(cx_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_file = cx_home / "hooks.json"
    foreign = {"Stop": [{"hooks": [{"type": "command", "command": "我的钩子 --标记"}]}]}
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(foreign, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config = cx_home / "config.toml"
    seeded = '[mcp_servers."本地服务器"]\ncommand = "C:/工具/服务器.exe"\n\n' + FOREIGN_TOML
    config.write_text(seeded, encoding="utf-8")
    cx_install.install()
    cx_install.uninstall()
    assert json.loads(hooks_file.read_text(encoding="utf-8")) == foreign
    assert config.read_text(encoding="utf-8") == seeded


def test_installer_bytes_survive_a_hostile_locale(tmp_path: Path) -> None:
    home = tmp_path / "cx"
    config = home / "config.toml"
    original = '# 用户注释\nmodel = "gpt-5-codex"\n\n[mcp_servers."本地网关"]\ncommand = "C:/工具/网关.exe"\n'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(original, encoding="utf-8")
    script = "from mnemoseed_local.hosts.codex_cli import install as cx\ncx.install()\ncx.uninstall()\n"
    env = dict(os.environ, CODEX_HOME=str(home), PYTHONIOENCODING="gbk")
    for var in ENV_VARS:
        env.pop(var, None)
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=180, check=False
    )
    assert result.returncode == 0, result.stderr
    assert config.read_text(encoding="utf-8") == original


def test_host_cli_lifecycle_wiring(
    cx_home: Path,
    stable_command: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    assert main(["host", "install", "--host", "codex_cli"]) == 0
    assert "applied" in capsys.readouterr().out
    assert (cx_home / "hooks.json").is_file()

    assert main(["host", "status", "--host", "codex_cli"]) == 0
    out = capsys.readouterr().out
    assert "not yet trusted" in out

    assert main(["host", "uninstall", "--host", "codex_cli"]) == 0
    assert "applied" in capsys.readouterr().out
