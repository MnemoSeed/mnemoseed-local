"""B2: the time-ordered session-resume surface (design: continue a new session
where the last one ended, by TIME not by semantic query).

Daemon ``POST /session/recent`` returns the most recent sessions' chunk tails
verbatim: newest session group first, chunks inside each group ascending
(reading order). The endpoint never guesses which session is "closed" — the
caller sees at most ``sessions`` groups and recognizes its own current one as
the still-growing newest group (preferred over an arbitrary cut).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.daemon.memory import _group_session_tails
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.schema.turn import HostId
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.drivers._time import iso8601_utc
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

PROFILE = "default"

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")

# test_registry.py clears the driver registries wholesale; any daemon-booting
# module ordered after it must defensively re-register (test_preset_embedded
# precedent).
_DRIVERS = (
    (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
    (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
    (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
    (EMBED_DRIVERS, bge_m3_onnx.BgeM3OnnxEmbedder),
    (EMBED_DRIVERS, synthetic_embedder.SyntheticEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_registered():
    for registry, cls in _DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


# ---------------------------------------------------------------- grouping (pure)


def _stamp(chunk_id: str, session: str, ingested_at: float, text: str, turn: int = 0) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id=None,
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id=session, source="manual"),
        turn_start=turn,
        turn_end=turn,
        ingested_at=ingested_at,
    )


def test_group_session_tails_orders_groups_recent_first_and_chunks_ascending() -> None:
    """The store feeds ingested_at-desc rows: newest session group first, and
    each group's tail in ascending (reading) order."""
    chunks = [
        _stamp("c4", "s2", 40.0, "second session tail"),
        _stamp("c3", "s2", 30.0, "second session head"),
        _stamp("c2", "s1", 20.0, "first session tail"),
        _stamp("c1", "s1", 10.0, "first session head"),
    ]
    groups = _group_session_tails(chunks, per_session=20, sessions=2)
    assert [g["session_id"] for g in groups] == ["s2", "s1"]
    assert [c["text"] for c in groups[0]["chunks"]] == ["second session head", "second session tail"]
    assert [c["text"] for c in groups[1]["chunks"]] == ["first session head", "first session tail"]
    assert groups[0]["latest_at"] == 40.0


def test_group_session_tails_caps_the_tail_not_the_head() -> None:
    chunks = [
        _stamp("c3", "s1", 30.0, "newest"),
        _stamp("c2", "s1", 20.0, "middle"),
        _stamp("c1", "s1", 10.0, "oldest"),
    ]
    groups = _group_session_tails(chunks, per_session=2, sessions=5)
    assert [c["text"] for c in groups[0]["chunks"]] == ["middle", "newest"]


def test_group_session_tails_respects_the_session_cap_and_empty_input() -> None:
    chunks = [
        _stamp("c3", "s3", 30.0, "x"),
        _stamp("c2", "s2", 20.0, "x"),
        _stamp("c1", "s1", 10.0, "x"),
    ]
    groups = _group_session_tails(chunks, per_session=5, sessions=2)
    assert [g["session_id"] for g in groups] == ["s3", "s2"]
    assert _group_session_tails([], per_session=5, sessions=2) == []


def test_group_session_tails_excludes_exactly_the_named_session() -> None:
    """B2.1 T1 (exclusion is a filter before grouping): the caller's own
    session must never be echoed back to it, and the shared '?' group (a chunk
    without any session label) is never excluded by an exact-match exclusion."""
    chunks = [
        _stamp("c3", "sess-cur", 30.0, "current session turn 0"),
        _stamp("c2", "sess-old", 20.0, "older session"),
        _stamp("c1", None, 10.0, "manual pin"),
    ]
    groups = _group_session_tails(chunks, per_session=20, sessions=5, exclude_session_id="sess-cur")
    assert [g["session_id"] for g in groups] == ["sess-old", "?"]
    assert [c["text"] for c in groups[0]["chunks"]] == ["older session"]
    assert [c["text"] for c in groups[1]["chunks"]] == ["manual pin"]


def test_group_session_tails_cap_counts_survivor_groups_after_exclusion() -> None:
    """The session cap counts SURVIVOR groups: the excluded session is gone
    before grouping, so the remaining newest sessions fill the slots."""
    chunks = [
        _stamp("c3", "sess-cur", 30.0, "current"),
        _stamp("c2", "sess-old2", 20.0, "older two"),
        _stamp("c1", "sess-old1", 10.0, "older one"),
    ]
    groups = _group_session_tails(chunks, per_session=5, sessions=2, exclude_session_id="sess-cur")
    assert [g["session_id"] for g in groups] == ["sess-old2", "sess-old1"]


# ---------------------------------------------------------------- daemon integration


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


def _ingest(client: TestClient, session_id: str, ts: float, text: str) -> None:
    response = client.post(
        "/ingest",
        json={
            "host": HostId.CLAUDE_CODE.value,
            "event": "user_prompt",
            "session_id": session_id,
            "profile_id": PROFILE,
            "ts": ts,
            "content": {"text": text},
        },
    )
    assert response.status_code == 202, response.text


def test_session_recent_returns_both_session_tails_verbatim_in_order(config_path: Path) -> None:
    """The new-session seam: two drained sessions come back newest-group-first,
    chunks ascending, text verbatim — exactly what an agent needs to re-anchor
    on the previous conversation's tail."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-old", 1.0, "上次我们敲定了 verify 回退语义")
        _ingest(client, "sess-old", 2.0, "还给 gemma4:e4b 定了校验位")
        client.post("/session/end", json={"session_id": "sess-old", "profile_id": PROFILE})
        _ingest(client, "sess-new", 3.0, "现在开始做窗口守卫")
        client.post("/session/end", json={"session_id": "sess-new", "profile_id": PROFILE})

        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["profile_id"] == PROFILE
        sessions = payload["sessions"]
        assert [s["session_id"] for s in sessions] == ["sess-new", "sess-old"]
        # the verbatim channel stores turns with their role label — the prefix
        # is exactly what an agent re-anchoring on a conversation tail wants
        assert [c["text"] for c in sessions[0]["chunks"]] == ["user: 现在开始做窗口守卫"]
        assert [c["text"] for c in sessions[1]["chunks"]] == [
            "user: 上次我们敲定了 verify 回退语义",
            "user: 还给 gemma4:e4b 定了校验位",
        ]
        for group in sessions:
            assert group["latest_at"] > 0
            for chunk in group["chunks"]:
                assert chunk["chunk_id"]
                assert chunk["ingested_at"] > 0


def test_session_recent_honors_the_caps(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        for i in range(3):
            _ingest(client, f"s{i}", float(i + 1), f"turn {i}")
            client.post("/session/end", json={"session_id": f"s{i}", "profile_id": PROFILE})
        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "sessions": 1, "per_session": 5},
        )
        assert body.status_code == 200, body.text
        sessions = body.json()["sessions"]
        assert [s["session_id"] for s in sessions] == ["s2"]


def test_session_recent_empty_profile_returns_no_groups(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["sessions"] == []
        assert payload["self_window"] is None
        assert "do not adopt" in payload["guidance"].lower(), "guidance ships even when empty"


def test_session_recent_rejects_out_of_range_caps(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        body = client.post("/session/recent", json={"profile_id": PROFILE, "per_session": 0})
        assert body.status_code == 422
        body = client.post("/session/recent", json={"profile_id": PROFILE, "sessions": 99})
        assert body.status_code == 422


def test_session_recent_excludes_the_named_session(config_path: Path) -> None:
    """B2.1 T1 integration: the injection read asks the daemon to exclude its
    own session (a race can land the current session's turn 0 before the
    transform fires) — only the older session's tail comes back. An unknown
    exclusion id leaves the order and shape untouched."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-old", 1.0, "老session内容")
        client.post("/session/end", json={"session_id": "sess-old", "profile_id": PROFILE})
        _ingest(client, "sess-new", 2.0, "新session内容")
        client.post("/session/end", json={"session_id": "sess-new", "profile_id": PROFILE})

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "exclude_session_id": "sess-new"},
        )
        assert body.status_code == 200, body.text
        sessions = body.json()["sessions"]
        assert [s["session_id"] for s in sessions] == ["sess-old"]
        assert [c["text"] for c in sessions[0]["chunks"]] == ["user: 老session内容"]

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "exclude_session_id": "no-such-session"},
        )
        assert body.status_code == 200, body.text
        sessions = body.json()["sessions"]
        assert [s["session_id"] for s in sessions] == ["sess-new", "sess-old"]


# ---------------------------------------------------------------- B2 windows / self_window


def _write_chunk(
    client: TestClient,
    chunk_id: str,
    session_id: str | None,
    ingested_at: float,
    text: str,
) -> None:
    stores = client.app.state.stores
    stamp = ChunkStamp(
        chunk_id=chunk_id,
        profile_id=PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id=session_id, source="manual"),
        ingested_at=ingested_at,
    )
    result = stores.embed.embed(text)
    stores.vector.upsert_chunk(stamp, result.dense, result.sparse)


def test_session_recent_group_windows_are_exact_not_page_visible(config_path: Path) -> None:
    """A long old session's true first chunk sits beyond the recent discovery
    page; the group window must come from an exact per-session scan, never a
    page-visible approximation."""
    with TestClient(create_app()) as client:
        _write_chunk(client, "new-1", "s-new", 1000.0, "newest turn")
        _write_chunk(client, "new-2", "s-new", 1001.0, "newer turn")
        for i in range(1, 251):
            _write_chunk(client, f"old-{i:03d}", "s-old", float(i), f"old turn {i}")

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "sessions": 2, "per_session": 20},
        )
        assert body.status_code == 200, body.text
        groups = body.json()["sessions"]
        assert [g["session_id"] for g in groups] == ["s-new", "s-old"]
        assert groups[0]["window"] == {"first": iso8601_utc(1000.0), "latest": iso8601_utc(1001.0)}
        # the page-visible first for s-old would be turn 93; the exact scan sees turn 1
        assert groups[1]["window"] == {"first": iso8601_utc(1.0), "latest": iso8601_utc(250.0)}
        assert groups[1]["window_truncated"] is False
        for group in groups:
            assert _ISO.fullmatch(group["window"]["first"])
            assert _ISO.fullmatch(group["window"]["latest"])


def test_session_recent_self_window_exact_window_only_keys(config_path: Path) -> None:
    """The top-level self_window carries the exact window and the active flag —
    and nothing else: no chunk text may leak into the window surface."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-self", 1.0, "self session turn")
        flushed = client.post("/flush", json={"session_id": "sess-self", "profile_id": PROFILE})
        assert flushed.status_code == 200, flushed.text

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "self_session_id": "sess-self"},
        )
        assert body.status_code == 200, body.text
        self_window = body.json()["self_window"]
        assert set(self_window) == {"session_id", "window", "chunk_count", "active"}
        assert self_window["session_id"] == "sess-self"
        assert _ISO.fullmatch(self_window["window"]["first"])
        assert _ISO.fullmatch(self_window["window"]["latest"])
        assert self_window["chunk_count"] >= 1
        assert self_window["active"] is True  # flushed, not settled: still buffered


def test_session_recent_self_window_null_when_absent_or_unknown(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        _ingest(client, "sess-x", 1.0, "some turn")
        client.post("/session/end", json={"session_id": "sess-x", "profile_id": PROFILE})

        absent = client.post("/session/recent", json={"profile_id": PROFILE})
        assert absent.status_code == 200, absent.text
        assert absent.json()["self_window"] is None

        unknown = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "self_session_id": "no-such-session"},
        )
        assert unknown.status_code == 200, unknown.text
        assert unknown.json()["self_window"] is None


def test_session_recent_window_truncated_only_for_exceeding_group(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mnemoseed_local.daemon.memory.SESSION_WINDOW_SCAN_LIMIT", 2)
    with TestClient(create_app()) as client:
        for i in range(1, 4):
            _write_chunk(client, f"big-{i}", "s-big", float(i), f"big turn {i}")
        _write_chunk(client, "small-1", "s-small", 4.0, "small turn")

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "sessions": 2, "per_session": 5},
        )
        assert body.status_code == 200, body.text
        groups = {g["session_id"]: g for g in body.json()["sessions"]}
        assert groups["s-big"]["window_truncated"] is True
        assert groups["s-big"]["window"] == {"first": iso8601_utc(2.0), "latest": iso8601_utc(3.0)}
        assert groups["s-small"]["window_truncated"] is False
        assert groups["s-small"]["window"] == {"first": iso8601_utc(4.0), "latest": iso8601_utc(4.0)}


def test_session_recent_question_group_window_is_null(config_path: Path) -> None:
    """The shared '?' group has no session identity to scan; its window is the
    honest null, never a guessed session window."""
    with TestClient(create_app()) as client:
        _write_chunk(client, "c1", "s-a", 2.0, "labeled turn")
        _write_chunk(client, "c2", None, 1.0, "manual pin")

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "sessions": 5, "per_session": 5},
        )
        assert body.status_code == 200, body.text
        groups = {g["session_id"]: g for g in body.json()["sessions"]}
        assert "?" in groups
        assert groups["?"]["window"] is None
        assert groups["?"]["window_truncated"] is False


def test_session_recent_flags_active_sessions_and_embeds_anti_adoption_guidance(config_path: Path) -> None:
    """Cross-session contamination guard: a session still capturing is ANOTHER
    conversation in progress. The payload must flag liveness per group (and
    expose staleness in seconds) plus embed explicit do-not-adopt guidance, so
    a resuming agent never mistakes another session's open work for its own
    todo list."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-old", 1.0, "早已结束的旧会话")
        client.post("/session/end", json={"session_id": "sess-old", "profile_id": PROFILE})
        _ingest(client, "sess-live", 2.0, "另一条线还在进行中")
        client.post("/session/end", json={"session_id": "sess-live", "profile_id": PROFILE})
        # liveness comes from the live-capture registry, not the store: point
        # it at sess-live directly to simulate that session capturing again
        client.app.state.capture.sessions = lambda: ("sess-live",)

        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        payload = body.json()
        by_id = {s["session_id"]: s for s in payload["sessions"]}
        assert by_id["sess-live"]["active"] is True, "a still-capturing session must be flagged active"
        assert by_id["sess-old"]["active"] is False
        stale = by_id["sess-old"]["seconds_since_last_activity"]
        assert isinstance(stale, (int, float)) and stale > 0
        guidance = payload["guidance"]
        assert "in progress" in guidance
        assert "do not adopt" in guidance.lower()
