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
tests pin the marked-entry surgery on the sandbox ``CODEX_HOME`` (the
documented ``hooks.json`` root ``hooks`` container: event -> matcher groups ->
command handlers; the ``config.toml`` marker block for the stdio MCP gateway),
the legacy flat-shape migration, and the trust-unverified status of a complete
install.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
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


IS_WINDOWS = os.name == "nt"

FOREIGN_HANDLER = {"type": "command", "command": "my-own-hook --flag"}


def _handler_for(command: str) -> dict[str, str]:
    return {"type": "command", "command": command}


def _group(handlers: list[dict[str, str]], matcher: str | None = None) -> dict[str, object]:
    group: dict[str, object] = {"hooks": handlers}
    if matcher is not None:
        group["matcher"] = matcher
    return group


_FOREIGN_STOP = _group([dict(FOREIGN_HANDLER)], matcher="Bash")
_FOREIGN_STOP["notes"] = {"origin": "user"}
_FOREIGN_PROMPT = _group([{"type": "command", "command": "other-helper.exe lint", "timeout": 30}])
FOREIGN_HOOKS = {"hooks": {"Stop": [_FOREIGN_STOP], "UserPromptSubmit": [_FOREIGN_PROMPT]}}

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

#: Our gateway table as a previous install left it (same keys, stale command).
STALE_MCP_BLOCK = '[mcp_servers.mnemoseed]\ncommand = "C:/tools/stale.exe"\nargs = ["mcp"]\n'

_LEGACY_EXE = str(Path(__file__).resolve())

#: The command shapes a flat (pre-container) install left behind: the shim and
#: the ``-m`` module fallback, both with a quoted executable head.
LEGACY_OWN_SHIM = f'"{_LEGACY_EXE}" {" ".join(cx_install.TRANSFORM_ARGS)}'
LEGACY_OWN_MODULE = f'"{sys.executable}" -m mnemoseed_local.cli {" ".join(cx_install.TRANSFORM_ARGS)}'


@pytest.fixture
def stable_command(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    exe = str(Path(__file__).resolve())
    command = f"{exe} {' '.join(cx_install.TRANSFORM_ARGS)}"
    monkeypatch.setattr(cx_install, "transform_executable", lambda: exe)
    monkeypatch.setattr(cx_install, "transform_command", lambda: command)
    return exe, command


def _nested(hooks: dict[str, Any]) -> dict[str, Any]:
    container = hooks.get("hooks")
    return container if isinstance(container, dict) else {}


def _own_commands(hooks: dict[str, Any]) -> list[str]:
    """Every marker-matched command in the nested container and in the legacy
    flat root shape."""
    containers = [_nested(hooks), {key: value for key, value in hooks.items() if key != "hooks"}]
    found: list[str] = []
    for container in containers:
        for event in cx_install.HOOK_EVENTS:
            groups = container.get(event)
            if not isinstance(groups, list):
                continue
            for group in groups:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                for handler in handlers if isinstance(handlers, list) else []:
                    if cx_install.is_ours(handler):
                        found.append(str(handler["command"]))
    return found


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _seed_hooks(cx_home: Path, document: object) -> Path:
    hooks_file = cx_home / "hooks.json"
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return hooks_file


def test_install_writes_the_nested_hooks_container_and_mcp_block(
    cx_home: Path, stable_command: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    hooks_file, changed = cx_install.install()
    assert changed is True
    assert hooks_file == cx_home / "hooks.json"
    assert "/hooks" in capsys.readouterr().out, "install points at the one-time trust review"
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert set(hooks) == {"hooks"}
    container = _nested(hooks)
    assert set(container) == set(cx_install.HOOK_EVENTS)
    exe, command = stable_command
    for event in cx_install.HOOK_EVENTS:
        groups = container[event]
        assert len(groups) == 1, event
        assert groups[0]["hooks"] == [_handler_for(command)]
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


def test_install_preserves_foreign_nested_groups_and_metadata(
    cx_home: Path, stable_command: tuple[str, str]
) -> None:
    hooks_file = _seed_hooks(cx_home, FOREIGN_HOOKS)
    config = cx_home / "config.toml"
    config.write_text(FOREIGN_TOML, encoding="utf-8")
    cx_install.install()
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    stop_groups = _nested(hooks)["Stop"]
    assert FOREIGN_HOOKS["hooks"]["Stop"][0] in stop_groups, "the matcher, metadata and handler survive"
    assert len(stop_groups) == 2, "our group joins the foreign one"
    prompt_groups = _nested(hooks)["UserPromptSubmit"]
    assert prompt_groups[0] == FOREIGN_HOOKS["hooks"]["UserPromptSubmit"][0]
    assert len(prompt_groups) == 2
    assert FOREIGN_HOOKS["hooks"]["Stop"][0]["matcher"] == "Bash"
    assert FOREIGN_HOOKS["hooks"]["Stop"][0]["notes"] == {"origin": "user"}
    assert len(_own_commands(hooks)) == len(cx_install.HOOK_EVENTS)
    config_text = config.read_text(encoding="utf-8")
    assert config_text.startswith(FOREIGN_TOML), "foreign toml bytes stay byte-identical ahead of our block"


def test_uninstall_removes_exactly_our_entries(cx_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_file = _seed_hooks(cx_home, FOREIGN_HOOKS)
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


def test_nested_install_then_uninstall_leaves_no_own_entries(
    cx_home: Path, stable_command: tuple[str, str]
) -> None:
    cx_install.install()
    _, existed = cx_install.uninstall()
    assert existed is True
    hooks_file = cx_home / "hooks.json"
    hooks = json.loads(hooks_file.read_text(encoding="utf-8")) if hooks_file.is_file() else {}
    assert _own_commands(hooks) == []
    config = cx_home / "config.toml"
    assert "[mcp_servers.mnemoseed]" not in (config.read_text(encoding="utf-8") if config.is_file() else "")


def test_legacy_flat_owned_entries_migrate_into_the_nested_container(
    cx_home: Path, stable_command: tuple[str, str]
) -> None:
    ours_shim = _handler_for(LEGACY_OWN_SHIM)
    ours_module = _handler_for(LEGACY_OWN_MODULE)
    legacy = {
        "Stop": [_group([ours_shim, dict(FOREIGN_HANDLER)], matcher="Bash")],
        "UserPromptSubmit": [_group([ours_module])],
        "SessionStart": [_group([dict(FOREIGN_HANDLER)])],
    }
    hooks_file = _seed_hooks(cx_home, legacy)
    _, changed = cx_install.install()
    assert changed is True
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    container = _nested(hooks)
    assert set(container) == set(cx_install.HOOK_EVENTS)
    _, command = stable_command
    assert _own_commands(hooks) == [command] * len(cx_install.HOOK_EVENTS)
    expected_stop = [_group([dict(FOREIGN_HANDLER)], matcher="Bash")]
    expected_start = [_group([dict(FOREIGN_HANDLER)])]
    assert hooks["Stop"] == expected_stop, "the foreign flat value and its group metadata stay put"
    assert "UserPromptSubmit" not in hooks, "a flat event holding only our handler is gone"
    assert hooks["SessionStart"] == expected_start, "foreign flat entries are never migrated"
    migrated = hooks_file.read_bytes()
    _, changed = cx_install.install()
    assert changed is False
    assert hooks_file.read_bytes() == migrated

    _, existed = cx_install.uninstall()
    assert existed is True
    remaining = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert _own_commands(remaining) == []
    assert remaining["Stop"] == expected_stop
    assert remaining["SessionStart"] == expected_start


def test_install_replaces_a_stale_own_command_and_keeps_its_siblings(
    cx_home: Path, stable_command: tuple[str, str]
) -> None:
    stale = "C:/old/venv/Scripts/mnemoseed-local.exe " + " ".join(cx_install.TRANSFORM_ARGS)
    stale_group = _group([_handler_for(stale), dict(FOREIGN_HANDLER)], matcher="Bash")
    hooks_file = _seed_hooks(cx_home, {"hooks": {"Stop": [stale_group]}})
    _, changed = cx_install.install()
    assert changed is True
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    groups = _nested(hooks)["Stop"]
    assert groups[0] == _group([dict(FOREIGN_HANDLER)], matcher="Bash")
    _, command = stable_command
    ours = [group for group in groups if any(cx_install.is_ours(h) for h in group.get("hooks", []))]
    assert len(ours) == 1, "one canonical handler total"
    assert ours[0]["hooks"] == [_handler_for(command)]
    assert stale not in hooks_file.read_text(encoding="utf-8")


def test_install_relocates_a_filtered_own_group_and_keeps_its_metadata(
    cx_home: Path, stable_command: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our handler only counts as canonical in an unfiltered group: a matcher
    that can never fire moves the handler out, while the group's own metadata
    survives in the emptied original."""
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    _, command = stable_command
    filtered = _group([_handler_for(command)], matcher="never-match-this-session")
    filtered["notes"] = {"origin": "user"}
    hooks_file = _seed_hooks(cx_home, {"hooks": {"Stop": [filtered]}})
    assert cx_install.status().state == "partial"

    _, changed = cx_install.install()
    assert changed is True
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    stop_groups = _nested(hooks)["Stop"]
    emptied = {"hooks": [], "matcher": "never-match-this-session", "notes": {"origin": "user"}}
    assert emptied in stop_groups, "the emptied group keeps its matcher and metadata"
    occupied = [group for group in stop_groups if group.get("hooks")]
    assert occupied == [_group([_handler_for(command)])]
    assert _own_commands(hooks) == [command] * len(cx_install.HOOK_EVENTS)

    installed = hooks_file.read_bytes()
    _, changed = cx_install.install()
    assert changed is False
    assert hooks_file.read_bytes() == installed


def test_uninstall_strips_owned_entries_from_both_shapes(
    cx_home: Path, stable_command: tuple[str, str]
) -> None:
    ours_shim = _handler_for(LEGACY_OWN_SHIM)
    ours_module = _handler_for(LEGACY_OWN_MODULE)
    seeded = {
        "hooks": {"Stop": [_group([ours_shim, dict(FOREIGN_HANDLER)], matcher="Bash")]},
        "Stop": [_group([ours_module])],
        "UserPromptSubmit": [_group([ours_module])],
    }
    hooks_file = _seed_hooks(cx_home, seeded)
    _, existed = cx_install.uninstall()
    assert existed is True
    remaining = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert remaining == {"hooks": {"Stop": [_group([dict(FOREIGN_HANDLER)], matcher="Bash")]}}


def test_dry_run_previews_without_writing(cx_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_file = _seed_hooks(cx_home, FOREIGN_HOOKS)
    seeded = hooks_file.read_text(encoding="utf-8")
    config = cx_home / "config.toml"
    config.write_text(FOREIGN_TOML, encoding="utf-8")
    _, would_change = cx_install.install(dry_run=True)
    assert would_change is True
    assert hooks_file.read_text(encoding="utf-8") == seeded
    assert config.read_text(encoding="utf-8") == FOREIGN_TOML
    _, changed = cx_install.install()
    assert changed is True


def test_dry_run_uninstall_previews_without_writing(cx_home: Path, stable_command: tuple[str, str]) -> None:
    hooks_file = _seed_hooks(cx_home, FOREIGN_HOOKS)
    config = cx_home / "config.toml"
    config.write_text(FOREIGN_TOML, encoding="utf-8")
    exe, command = stable_command
    cx_install.install()
    container = _nested(json.loads(hooks_file.read_text(encoding="utf-8")))
    assert set(container) == set(cx_install.HOOK_EVENTS)
    for event in cx_install.HOOK_EVENTS:
        groups = container[event]
        owned = [group for group in groups if any(cx_install.is_ours(h) for h in group.get("hooks", []))]
        assert owned == [_group([_handler_for(command)])], event
    entry = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["mnemoseed"]
    assert entry == {"command": exe, "args": ["mcp"]}

    hooks_before, config_before = _digest(hooks_file), _digest(config)
    _, would_change = cx_install.uninstall(dry_run=True)
    assert would_change is True
    assert _digest(hooks_file) == hooks_before, "the dry-run preview wrote no hooks bytes"
    assert _digest(config) == config_before, "the dry-run preview wrote no config bytes"

    _, existed = cx_install.uninstall()
    assert existed is True
    assert json.loads(hooks_file.read_text(encoding="utf-8")) == FOREIGN_HOOKS
    assert config.read_text(encoding="utf-8") == FOREIGN_TOML


def test_malformed_hooks_json_refuses_instead_of_corrupting(
    cx_home: Path, stable_command: tuple[str, str]
) -> None:
    hooks_file = cx_home / "hooks.json"
    cases = [
        '{\n  // user annotation\n  "hooks": {"Stop": []}\n}\n',
        "{broken",
        "[]",
    ]
    for original in cases:
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        hooks_file.write_text(original, encoding="utf-8")
        with pytest.raises(cx_install.ConfigParseError):
            cx_install.install()
        assert hooks_file.read_text(encoding="utf-8") == original
        assert not (cx_home / "config.toml").exists(), "no config write lands either"


@pytest.mark.parametrize(
    "document",
    [
        {"hooks": []},
        {"hooks": "garbage"},
        {"hooks": {"Stop": "garbage"}},
        {"hooks": {"Stop": ["garbage"]}},
        {"hooks": {"Stop": [{"hooks": "garbage"}]}},
        {"Stop": {"hooks": []}},
        {"Stop": ["garbage"]},
        {"Stop": "garbage"},
    ],
)
def test_malformed_hooks_structure_refuses_before_any_write(
    cx_home: Path, stable_command: tuple[str, str], document: object
) -> None:
    hooks_file = _seed_hooks(cx_home, document)
    config = cx_home / "config.toml"
    config.write_text(FOREIGN_TOML + STALE_MCP_BLOCK, encoding="utf-8")
    hooks_before, config_before = _digest(hooks_file), _digest(config)
    with pytest.raises(cx_install.ConfigParseError):
        cx_install.install()
    with pytest.raises(cx_install.ConfigParseError):
        cx_install.uninstall()
    assert _digest(hooks_file) == hooks_before, document
    assert _digest(config) == config_before, document


@pytest.mark.parametrize(
    "document",
    [
        {"hooks": None},
        {"hooks": {"Stop": [{"hooks": None}]}},
    ],
)
def test_explicit_null_hook_containers_refuse_before_any_write(
    cx_home: Path,
    stable_command: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    document: object,
) -> None:
    """An explicit null is a malformed container, not an absent one."""
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    hooks_file = _seed_hooks(cx_home, document)
    config = cx_home / "config.toml"
    config.write_text(FOREIGN_TOML + STALE_MCP_BLOCK, encoding="utf-8")
    hooks_before, config_before = _digest(hooks_file), _digest(config)
    with pytest.raises(cx_install.ConfigParseError):
        cx_install.install()
    with pytest.raises(cx_install.ConfigParseError):
        cx_install.uninstall()
    assert _digest(hooks_file) == hooks_before, document
    assert _digest(config) == config_before, document
    assert cx_install.status().state == "malformed", document


@pytest.mark.parametrize("original", ["[broken\n", "model = \n", 'model = "gpt-5"\n[unclosed\n'])
def test_malformed_config_toml_refuses_before_any_write(
    cx_home: Path, stable_command: tuple[str, str], original: str
) -> None:
    config = cx_home / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(original, encoding="utf-8")
    hooks_file = _seed_hooks(cx_home, FOREIGN_HOOKS)
    hooks_before, config_before = _digest(hooks_file), _digest(config)
    with pytest.raises(cx_install.ConfigParseError):
        cx_install.install()
    with pytest.raises(cx_install.ConfigParseError):
        cx_install.uninstall()
    assert _digest(hooks_file) == hooks_before, "the hooks write never lands"
    assert _digest(config) == config_before
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


def test_status_reports_structural_states(
    cx_home: Path, stable_command: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    assert cx_install.status().state == "not-installed"

    hooks_file = _seed_hooks(cx_home, {"Stop": [_group([_handler_for(LEGACY_OWN_SHIM)])]})
    assert cx_install.status().state in {"not-installed", "partial"}, "a flat root shape is never canonical"

    hooks_file = _seed_hooks(cx_home, {"hooks": {"Stop": [_group([_handler_for(LEGACY_OWN_SHIM)])]}})
    assert cx_install.status().state == "partial"

    malformed = (
        "{broken",
        "[]",
        json.dumps({"hooks": []}),
        json.dumps({"hooks": {"Stop": "garbage"}}),
    )
    for document in malformed:
        hooks_file.write_text(document, encoding="utf-8")
        assert cx_install.status().state == "malformed", document


def test_status_treats_a_filtered_own_group_as_incomplete(
    cx_home: Path, stable_command: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our handler under a matcher that can never fire is not a complete
    install, and a reinstall relocates it into an unfiltered group."""
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    _, command = stable_command
    cx_install.install()
    hooks_file = cx_home / "hooks.json"
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    _nested(hooks)["Stop"][0]["matcher"] = "never-match-this-session"
    hooks_file.write_text(json.dumps(hooks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert cx_install.status().state == "partial"

    _, changed = cx_install.install()
    assert changed is True
    repaired = json.loads(hooks_file.read_text(encoding="utf-8"))
    moved = [group for group in _nested(repaired)["Stop"] if group.get("hooks")]
    assert moved == [_group([_handler_for(command)])]
    assert "unverified" in cx_install.status().state.lower()


def test_status_reports_a_complete_install_as_trust_unverified(
    cx_home: Path,
    stable_command: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", lambda *a, **k: False)
    cx_install.install()
    config = cx_home / "config.toml"
    assert "[hooks.state" not in config.read_text(encoding="utf-8"), "install never writes a trust record"
    info = cx_install.status()
    assert "unverified" in info.state.lower()
    assert info.state not in {"installed", "not-installed", "partial", "malformed"}

    digest = hashlib.sha256((cx_home / "hooks.json").read_bytes()).hexdigest()
    _seed_trusted_hash(cx_home, digest)
    assert "unverified" in cx_install.status().state.lower(), "a whole-file hash is not a trust signal"

    assert main(["host", "status", "--host", "codex_cli"]) == 0
    out = capsys.readouterr().out
    assert "unverified" in out
    assert "/hooks" in out


def test_status_reports_the_config_path_and_daemon_probe(
    cx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_probe(base_url: str, timeout: float = 2.0) -> bool:
        seen["base_url"] = base_url
        return True

    monkeypatch.setattr("mnemoseed_local.hosts.install.daemon_reachable", fake_probe)
    info = cx_install.status()
    assert info.state == "not-installed", "structure and reachability are reported apart"
    assert info.daemon_reachable is True
    assert info.path == cx_home / "hooks.json"
    assert info.root == cx_home
    assert seen["base_url"] == "http://localhost:7788"


WINDOWS_HOSTILE_PATHS = [
    r"C:\Program Files\MnemoSeed\mnemoseed-local.exe",
    r"C:\Users\O'Brien\mnemoseed-local.exe",
    r"C:\Users\$env:PATH\mnemoseed-local.exe",
    r"C:\Users\back`tick\mnemoseed-local.exe",
    r"C:\Users\Little Star\O'Brien $x `y\mnemoseed-local.exe",
]

POSIX_HOSTILE_PATHS = [
    "/opt/mnemo seed/mnemoseed-local",
    "/opt/O'Brien/mnemoseed-local",
    "/opt/$HOME/`tick`/mnemoseed-local",
    "/opt/Mnemo Seed/O'Brien $X `y`/mnemoseed-local",
]


def _tail(module_form: bool) -> tuple[str, ...]:
    base = ("-m", "mnemoseed_local.cli") if module_form else ()
    return (*base, *cx_install.TRANSFORM_ARGS)


def _powershell_literal(text: str) -> tuple[str, str]:
    """The value of a leading single-quoted PowerShell literal plus the rest."""
    assert text.startswith("'"), text
    value: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == "'":
            if text[index + 1 : index + 2] == "'":
                value.append("'")
                index += 2
                continue
            return "".join(value), text[index + 1 :]
        value.append(char)
        index += 1
    raise AssertionError(f"unterminated PowerShell literal: {text}")


@pytest.mark.parametrize("module_form", [False, True])
@pytest.mark.parametrize("exe", WINDOWS_HOSTILE_PATHS)
def test_windows_commands_call_a_single_quoted_literal(exe: str, module_form: bool) -> None:
    tail = _tail(module_form)
    command = cx_install.render_command(exe, tail, windows=True)
    assert command.startswith("& "), command
    literal, rest = _powershell_literal(command[2:])
    assert literal == exe, "the path is a literal, never an executable expression"
    assert rest == " " + " ".join(tail)
    assert cx_install.is_ours({"type": "command", "command": command}) is True


@pytest.mark.parametrize("module_form", [False, True])
@pytest.mark.parametrize("exe", POSIX_HOSTILE_PATHS)
def test_posix_commands_keep_the_path_as_one_shell_word(exe: str, module_form: bool) -> None:
    tail = _tail(module_form)
    command = cx_install.render_command(exe, tail, windows=False)
    assert shlex.split(command) == [exe, *tail]
    assert not command.startswith("&")
    assert cx_install.is_ours({"type": "command", "command": command}) is True


@pytest.mark.parametrize("shim_present", [True, False])
def test_transform_command_renders_the_platform_quote(
    monkeypatch: pytest.MonkeyPatch, shim_present: bool
) -> None:
    windows_exe = r"C:\Program Files\MnemoSeed\mnemoseed-local.exe"
    posix_exe = "/opt/mnemo seed/mnemoseed-local"
    exe = windows_exe if IS_WINDOWS else posix_exe
    found = "mnemoseed-local" if shim_present else None
    monkeypatch.setattr(cx_install, "transform_executable", lambda: exe)
    monkeypatch.setattr(cx_install.shutil, "which", lambda name: found)
    expected = cx_install.render_command(exe, _tail(not shim_present), windows=IS_WINDOWS)
    assert cx_install.transform_command() == expected
    assert cx_install.is_ours({"type": "command", "command": expected}) is True


def test_foreign_module_form_mentioning_the_verb_is_never_ours() -> None:
    legit_module = cx_install.render_command(sys.executable, _tail(True), windows=IS_WINDOWS)
    foreign_module = cx_install.render_command(
        sys.executable, ("-m", "evil_pkg", *cx_install.TRANSFORM_ARGS), windows=IS_WINDOWS
    )
    shim_form = cx_install.render_command(sys.executable, _tail(False), windows=IS_WINDOWS)
    assert cx_install.is_ours({"type": "command", "command": legit_module}) is True
    assert cx_install.is_ours({"type": "command", "command": shim_form}) is True
    assert cx_install.is_ours({"type": "command", "command": foreign_module}) is False
    assert cx_install.is_ours({"type": "command", "command": "my-own-hook --flag"}) is False
    assert cx_install.is_ours({"type": "process", "command": shim_form}) is False


def test_foreign_entry_mentioning_the_marker_is_kept_with_a_warning(
    cx_home: Path, stable_command: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    foreign = {"hooks": {"Stop": [_group([_handler_for("mnemoseed-local-helper.exe watch")])]}}
    hooks_file = _seed_hooks(cx_home, foreign)
    _, changed = cx_install.install()
    assert changed is True
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert len(_nested(hooks)["Stop"]) == 2
    _, existed = cx_install.uninstall()
    assert existed is True
    assert json.loads(hooks_file.read_text(encoding="utf-8")) == foreign
    assert "mnemoseed-local-helper" in capsys.readouterr().err


def test_utf8_foreign_content_survives_the_roundtrip(cx_home: Path, stable_command: tuple[str, str]) -> None:
    foreign = {"hooks": {"Stop": [_group([_handler_for("我的钩子 --标记")], matcher="Bash")]}}
    hooks_file = _seed_hooks(cx_home, foreign)
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
    hooks = json.loads((cx_home / "hooks.json").read_text(encoding="utf-8"))
    assert set(hooks) == {"hooks"}

    assert main(["host", "status", "--host", "codex_cli"]) == 0
    out = capsys.readouterr().out
    assert "unverified" in out
    assert "/hooks" in out

    assert main(["host", "uninstall", "--host", "codex_cli"]) == 0
    assert "applied" in capsys.readouterr().out
