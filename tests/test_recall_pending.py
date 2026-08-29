"""B2.1 T2: the mid-session auto-recall pipeline (design/01 §4.6, PRD-B2.1).

Daemon ``POST /session/recall-pending`` serves the focal scan produced by the
most recent user prompt of a session:

- a user_prompt ingest runs the embedding-free focal scan (entity-anchored,
  decay floor) and parks the budgeted candidates as the session's pending
  slot — the 202 ack implies the slot is ready (ack-implies-ready);
- the pull merges the hook's seen ids, serves the slot ONCE (mark-seen) and
  reports the T4 calibration count ``non_focal_above_floor``;
- current-session chunks and daemon-seen ids are never served;
- assistant_message ingests never scan; /session/end drops the slot;
- the whole pipeline is gated on ``capture.auto_recall`` (on by default;
  off-state scenarios set the key explicitly) and
  hot-applies through the configwrite surface.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.schema.turn import HostId
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.ports import ChunkFilter, Page, WeightUpdate
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

PROFILE = "default"

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


def _config_toml(tmp_path: Path, capture: str = "") -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n' + capture
    )


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with capture.auto_recall explicitly off (off-state scenarios)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path, "[capture]\nauto_recall = false\n"), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


@pytest.fixture
def recall_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path, "[capture]\nauto_recall = true\n"), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


def _ingest(client: TestClient, session_id: str, ts: float, text: str, event: str = "user_prompt") -> None:
    response = client.post(
        "/ingest",
        json={
            "host": HostId.CLAUDE_CODE.value,
            "event": event,
            "session_id": session_id,
            "profile_id": PROFILE,
            "ts": ts,
            "content": {"text": text},
        },
    )
    assert response.status_code == 202, response.text


def _settle(client: TestClient, session_id: str) -> None:
    response = client.post("/session/end", json={"session_id": session_id, "profile_id": PROFILE})
    assert response.status_code == 200, response.text


def _pull(client: TestClient, session_id: str, seen: list[str] | None = None) -> dict:
    body = client.post(
        "/session/recall-pending",
        json={
            "profile_id": PROFILE,
            "session_id": session_id,
            **({"seen_chunk_ids": seen} if seen is not None else {}),
        },
    )
    assert body.status_code == 200, body.text
    return body.json()


def _store_chunks(client: TestClient) -> dict[str, str]:
    """Map stored chunk text -> chunk id (newest first order)."""
    page = client.app.state.stores.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(0, 50))
    return {chunk.text: chunk.chunk_id for chunk in page.items}


# ---------------------------------------------------------------- gating (D5/D8)


def test_recall_pending_gated_off_answers_disabled_and_consumes_nothing(config_path: Path) -> None:
    """capture.auto_recall = false: the pull answers enabled:false with
    an empty selection, never scans, and never consumes anything."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        assert _pull(client, "sess-b") == {
            "enabled": False,
            "items": [],
            "non_focal_above_floor": 0,
            "budget_chars": 2400,
            "slot_consumed": False,
        }


def test_recall_pending_serves_focal_entities_once_and_marks_seen(recall_config_path: Path) -> None:
    """D3/D6: the scan serves the entity-anchored candidate exactly once —
    a second pull finds the slot consumed (serve = mark-seen): the consumed
    tombstone answers slot_consumed:true with an empty selection so the hook
    clears its arm (QA BLOCKER-2)."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        payload = _pull(client, "sess-b")
        assert payload["enabled"] is True
        assert payload["non_focal_above_floor"] == 0
        assert payload["budget_chars"] == 2400
        assert payload["slot_consumed"] is True
        items = payload["items"]
        assert len(items) == 1, items
        assert items[0]["kind"] == "chunk"
        assert items[0]["text"] == "user: 上一轮我们把 LanceDb 定为向量存储"
        assert items[0]["id"] == _store_chunks(client)["user: 上一轮我们把 LanceDb 定为向量存储"]
        second = _pull(client, "sess-b")
        assert second["items"] == []
        assert second["slot_consumed"] is True, "the consumed tombstone survives the serve"


def test_recall_pending_items_carry_pin_source_for_injection(recall_config_path: Path) -> None:
    """R2 provenance-trust injection wire: each served pending item carries its
    pin/source signal so the T2 injection host can mark pins (source == memory.remember)."""
    with TestClient(create_app()) as client:
        store = client.app.state.stores
        stamp = ChunkStamp(
            chunk_id="pin-src",
            profile_id=PROFILE,
            text="user: 记得用 LanceDb 做向量存储",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="test-model",
            cues=Cues(entities=["LanceDb"]),
            provenance=Provenance(asserted_by="user", session_id="sess-a", source="memory.remember"),
            ingested_at=1.0,
        )
        embedded = store.embed.embed(stamp.text)
        store.vector.upsert_chunk(stamp, embedded.dense, embedded.sparse)
        client.post("/session/end", json={"session_id": "sess-a", "profile_id": PROFILE})
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        items = _pull(client, "sess-b")["items"]
        assert len(items) == 1, items
        assert items[0]["source"] == "memory.remember"
        assert items[0]["kind"] == "chunk"


def test_recall_pending_consumed_tombstone_survives_until_session_end(recall_config_path: Path) -> None:
    """QA BLOCKER-2: the serve leaves a CONSUMED TOMBSTONE distinct from the
    slot — a retry pull after a lost response answers {enabled:true, items:[],
    slot_consumed:true} so the hook clears its arm (the retry loop must not
    pull into the void forever); /session/end drops the tombstone with the
    lifecycle state (a later pull answers slot_consumed:false and materializes
    nothing per NIT-6); a fresh scan afterwards parks a NEW slot that serves
    normally — the tombstone never leaks across the epoch."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        first = _pull(client, "sess-b")
        assert first["enabled"] is True
        assert first["slot_consumed"] is True
        assert len(first["items"]) == 1
        # the retry after a serve: the slot is gone, the tombstone answers
        retry = _pull(client, "sess-b")
        assert retry == {
            "enabled": True,
            "items": [],
            "non_focal_above_floor": 0,
            "budget_chars": 2400,
            "slot_consumed": True,
        }
        # the settle is terminal for the tombstone too
        _settle(client, "sess-b")
        after = _pull(client, "sess-b")
        assert after["enabled"] is True
        assert after["items"] == []
        assert after["slot_consumed"] is False
        memory = client.app.state.memory
        assert (PROFILE, "sess-b") not in memory._seen_chunk_ids
        assert (PROFILE, "sess-b") not in memory._pending_slots
        assert (PROFILE, "sess-b") not in memory._pending_consumed
        # a fresh scan after the settle parks a new slot that serves normally
        memory.note_user_prompt(PROFILE, "sess-b", "LanceDb 的进展")
        served = _pull(client, "sess-b")
        assert served["enabled"] is True
        assert served["slot_consumed"] is True
        assert len(served["items"]) == 1
        assert "上一轮" in served["items"][0]["text"]


def test_recall_pending_merges_hook_seen_ids_before_selecting(recall_config_path: Path) -> None:
    """D2: the hook's T1-seen chunk ids are merged into the selection — a
    chunk the caller already saw is filtered out; the slot survives an empty
    serve so a fresh pull can still consume it."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        chunk_id = _store_chunks(client)["user: 上一轮我们把 LanceDb 定为向量存储"]
        assert _pull(client, "sess-b", seen=[chunk_id])["items"] == []
        assert _pull(client, "sess-b")["items"] != [], "the slot must survive an empty serve"


def test_recall_pending_excludes_the_requesting_session(recall_config_path: Path) -> None:
    """The scan never serves the requesting session's own chunks — the older
    session's LanceDb decision comes back, the in-flight session's turn 0
    does not."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "我们继续做 LanceDb 的接入")
        client.post("/flush", json={"session_id": "sess-b", "profile_id": PROFILE})
        _ingest(client, "sess-b", 3.0, "LanceDb 卡在哪一步")
        items = _pull(client, "sess-b")["items"]
        assert len(items) == 1, items
        assert "上一轮" in items[0]["text"]
        assert "继续做" not in items[0]["text"], "the requesting session's own chunk must be excluded"


def test_recall_pending_assistant_message_does_not_scan(recall_config_path: Path) -> None:
    """D1: only user_prompt ingests run the focal scan — an assistant reply
    leaves the slot empty."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "LanceDb 相关的说明", event="assistant_message")
        payload = _pull(client, "sess-b")
        assert payload["enabled"] is True
        assert payload["items"] == []


# ---------------------------------------------------------------- non-focal probe (TA-2/T4)


def test_recall_pending_reports_non_focal_above_floor(recall_config_path: Path) -> None:
    """D3/T4: decay-healthy chunks the focal scan did NOT select (no entity
    overlap) ride along as the non_focal_above_floor calibration count."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "LanceDb 作为向量存储")
        _ingest(client, "sess-a", 1.5, "please keep the design simple and honest")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "LanceDb 相关")
        payload = _pull(client, "sess-b")
        assert payload["enabled"] is True
        assert payload["non_focal_above_floor"] == 1
        assert len(payload["items"]) == 1
        assert "LanceDb 作为向量存储" in payload["items"][0]["text"]


def test_recall_pending_below_focal_floor_is_excluded(recall_config_path: Path) -> None:
    """The focal scan honors the decay floor: a decayed-away chunk is neither
    selected nor counted."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "LanceDb 相关内容")
        _settle(client, "sess-a")
        chunk_id = _store_chunks(client)["user: LanceDb 相关内容"]
        client.app.state.stores.vector.update_weights([WeightUpdate(chunk_id, decay_weight=0.3)])
        _ingest(client, "sess-b", 2.0, "LanceDb 相关")
        payload = _pull(client, "sess-b")
        assert payload["enabled"] is True
        assert payload["items"] == []
        assert payload["non_focal_above_floor"] == 0


# ---------------------------------------------------------------- budget (D4, T1 semantics)


def _budget_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: int) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        _config_toml(tmp_path, f"[capture]\nauto_recall = true\nauto_recall_budget_chars = {budget}\n"),
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


def test_recall_pending_same_stamp_tie_breaks_by_turn_start(recall_config_path: Path) -> None:
    """D4 tie-break three-key pin: two chunks with the SAME decay_weight and
    the SAME ingested_at (a B6 batch drain stamps one clock tick) are selected
    by turn_start desc — the tie no longer depends on timestamp uniqueness."""
    with TestClient(create_app()) as client:
        stores = client.app.state.stores
        for turn_start, chunk_id, text in [
            (1, "tie-newer", "LanceDb 平局的新轮"),
            (0, "tie-older", "LanceDb 平局的旧轮"),
        ]:
            stamp = ChunkStamp(
                chunk_id=chunk_id,
                profile_id=PROFILE,
                text=text,
                cognitive_tier=CognitiveTier.TIER_1,
                model_id="test-model",
                cues=Cues(entities=["LanceDb"]),
                provenance=Provenance(asserted_by="user", session_id="sess-a", source="manual"),
                decay_weight=0.9,
                ingested_at=1000.0,
                turn_start=turn_start,
                turn_end=turn_start,
            )
            embedded = stores.embed.embed(text)
            stores.vector.upsert_chunk(stamp, embedded.dense, embedded.sparse)
        _ingest(client, "sess-b", 2.0, "LanceDb 相关")
        items = _pull(client, "sess-b")["items"]
        assert [item["id"] for item in items] == ["tie-newer", "tie-older"], items


def test_recall_pending_budget_admits_greedy_decay_and_drops_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: selection is greedy decay_weight desc (tie newest-first) under the
    char budget — the strongest candidate lands, everything past the budget
    is dropped wholesale (T1 slice semantics: a slice below 200 chars is
    dropped entirely)."""
    _budget_config(tmp_path, monkeypatch, budget=300)
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "LanceDb " + "甲" * 200)
        _ingest(client, "sess-a", 2.0, "LanceDb " + "乙" * 200)
        _ingest(client, "sess-a", 3.0, "LanceDb " + "丙" * 150)
        _settle(client, "sess-a")
        by_text = _store_chunks(client)
        # decay ordering: newest (丙) and middle (乙) at 0.9, oldest (甲) at 0.5
        client.app.state.stores.vector.update_weights(
            [
                WeightUpdate(by_text["user: LanceDb " + "丙" * 150], decay_weight=0.9),
                WeightUpdate(by_text["user: LanceDb " + "乙" * 200], decay_weight=0.9),
                WeightUpdate(by_text["user: LanceDb " + "甲" * 200], decay_weight=0.5),
            ]
        )
        _ingest(client, "sess-b", 4.0, "LanceDb 相关")
        items = _pull(client, "sess-b")["items"]
        assert [item["id"] for item in items] == [by_text["user: LanceDb " + "丙" * 150]], items
        assert sum(len(item["text"]) + 1 for item in items) <= 300
        assert _pull(client, "sess-b")["items"] == [], "the served slot is consumed"


def test_recall_pending_budget_tail_slices_the_boundary_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4/T1: the boundary item keeps only its tail slice — marked with … and
    sized at remaining-budget minus the 2-char marker/newline — and the total
    never exceeds the budget."""
    _budget_config(tmp_path, monkeypatch, budget=560)
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "LanceDb " + "甲" * 300)
        _ingest(client, "sess-a", 2.0, "LanceDb " + "乙" * 300)
        _settle(client, "sess-a")
        by_text = _store_chunks(client)
        newest = by_text["user: LanceDb " + "乙" * 300]
        boundary = by_text["user: LanceDb " + "甲" * 300]
        _ingest(client, "sess-b", 3.0, "LanceDb 相关")
        items = _pull(client, "sess-b")["items"]
        assert [item["id"] for item in items] == [newest, boundary], items
        full_cost = len("user: LanceDb " + "乙" * 300) + 1
        remaining = 560 - full_cost
        slice_len = remaining - 2  # the … marker + the newline
        assert slice_len >= 200, "the slice must clear the 200-char drop floor"
        assert items[1]["text"] == "…" + ("user: LanceDb " + "甲" * 300)[-slice_len:]
        assert sum(len(item["text"]) + 1 for item in items) <= 560


# ---------------------------------------------------------------- lifecycle + configwrite


def test_recall_pending_session_end_drops_the_pending_slot(recall_config_path: Path) -> None:
    """/session/end settles the session and drops its pending slot — a pull
    after the settle finds nothing to serve."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        assert _pull(client, "sess-b")["items"] != []
        client.post("/session/end", json={"session_id": "sess-b", "profile_id": PROFILE})
        payload = _pull(client, "sess-b")
        assert payload["enabled"] is True
        assert payload["items"] == []


def test_recall_pending_hot_applies_via_configwrite(config_path: Path) -> None:
    """D5: capture.auto_recall hot-applies through the configwrite surface —
    the daemon's pull answers enabled:false until the key flips true."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        assert _pull(client, "sess-b")["enabled"] is False
        response = client.post("/api/v1/config/set", json={"key_path": "capture.auto_recall", "value": True})
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        _ingest(client, "sess-b", 3.0, "LanceDb 的进度")
        payload = _pull(client, "sess-b")
        assert payload["enabled"] is True
        assert len(payload["items"]) == 1
        assert "上一轮" in payload["items"][0]["text"]


def test_recall_pending_rejects_bad_bodies(config_path: Path) -> None:
    """Wire validation mirrors the sibling surfaces: a blank profile or a
    seen list over the 16-id cap is a 422."""
    with TestClient(create_app()) as client:
        response = client.post("/session/recall-pending", json={"profile_id": "", "session_id": "sess-b"})
        assert response.status_code == 422
        response = client.post(
            "/session/recall-pending",
            json={
                "profile_id": PROFILE,
                "session_id": "sess-b",
                "seen_chunk_ids": [str(i) for i in range(17)],
            },
        )
        assert response.status_code == 422


# ---------------------------------------------------------------- QA round-2 assertions


def test_recall_pending_admits_decay_exactly_at_the_focal_floor(recall_config_path: Path) -> None:
    """The focal floor is a >= gate: decay_weight == 0.5 (the default floor)
    IS admitted — the 0.3-excluded pin above pins the other side."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "LanceDb 相关内容")
        _settle(client, "sess-a")
        chunk_id = _store_chunks(client)["user: LanceDb 相关内容"]
        client.app.state.stores.vector.update_weights([WeightUpdate(chunk_id, decay_weight=0.5)])
        _ingest(client, "sess-b", 2.0, "LanceDb 相关")
        items = _pull(client, "sess-b")["items"]
        assert len(items) == 1, items
        assert items[0]["id"] == chunk_id


def test_recall_pending_config_off_pull_consumes_nothing_and_marks_nothing(config_path: Path) -> None:
    """NIT-4: the serve+mark is gated on `enabled` — a config-off pull returns
    enabled:false / empty WITHOUT consuming the parked slot or marking its ids
    seen; flipping the config back on serves the parked selection."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        # flip on, park a slot via a fresh prompt, flip off
        assert (
            client.post("/api/v1/config/set", json={"key_path": "capture.auto_recall", "value": True}).json()[
                "ok"
            ]
            is True
        )
        _ingest(client, "sess-b", 3.0, "LanceDb 的进度")
        assert (
            client.post(
                "/api/v1/config/set", json={"key_path": "capture.auto_recall", "value": False}
            ).json()["ok"]
            is True
        )
        payload = _pull(client, "sess-b")
        assert payload["enabled"] is False
        assert payload["items"] == []
        assert payload["slot_consumed"] is False
        # the slot survived and nothing was marked: flip back on -> serves
        assert (
            client.post("/api/v1/config/set", json={"key_path": "capture.auto_recall", "value": True}).json()[
                "ok"
            ]
            is True
        )
        payload = _pull(client, "sess-b")
        assert payload["enabled"] is True
        assert len(payload["items"]) == 1
        assert "上一轮" in payload["items"][0]["text"]


def test_recall_pending_unknown_session_materializes_no_seen_set(recall_config_path: Path) -> None:
    """NIT-6: a pull for a session with no slot must not materialize an empty
    seen-set, a tombstone, or any other lifecycle state (setdefault leak —
    white-box on the service attribute)."""
    with TestClient(create_app()) as client:
        payload = _pull(client, "never-seen")
        assert payload["items"] == []
        memory = client.app.state.memory
        assert (PROFILE, "never-seen") not in memory._seen_chunk_ids
        assert (PROFILE, "never-seen") not in memory._pending_slots
        assert (PROFILE, "never-seen") not in memory._pending_consumed


def test_recall_pending_stale_scan_cannot_overwrite_a_newer_slot(recall_config_path: Path) -> None:
    """NIT-5a: two in-flight scans for one session — the LAST to START wins;
    a scan that captured its sequence before a newer scan wrote must NOT
    overwrite the newer selection."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        service = client.app.state.memory
        entered = threading.Event()
        release = threading.Event()
        real_scan = service._focal_scan

        def slow_scan(profile_id: str, session_id: str, text: str, seen: set[str]):
            entered.set()
            assert release.wait(timeout=5)
            return ([{"kind": "chunk", "id": "stale-id", "text": "stale selection"}], 0)

        service._focal_scan = slow_scan

        def scan_a() -> None:
            service.note_user_prompt(PROFILE, "sess-b", "LanceDb 是什么")

        thread = threading.Thread(target=scan_a)
        thread.start()
        assert entered.wait(timeout=5), "scan A must reach its store read"
        service._focal_scan = real_scan
        service.note_user_prompt(PROFILE, "sess-b", "LanceDb 状态")  # scan B: newer, writes first
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), "scan A must finish"
        items = _pull(client, "sess-b")["items"]
        assert len(items) == 1, items
        assert items[0]["id"] != "stale-id", "the stale scan must not overwrite the newer slot"
        assert "stale selection" not in items[0]["text"]


def test_recall_pending_scan_before_end_session_cannot_repark(recall_config_path: Path) -> None:
    """NIT-5b: a scan started BEFORE /session/end settles the session must not
    re-park a slot afterwards (the settle is terminal — no pull will ever
    come)."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        service = client.app.state.memory
        entered = threading.Event()
        release = threading.Event()
        real_scan = service._focal_scan

        def slow_scan(profile_id: str, session_id: str, text: str, seen: set[str]):
            entered.set()
            assert release.wait(timeout=5)
            return ([{"kind": "chunk", "id": "stale-id", "text": "stale selection"}], 0)

        service._focal_scan = slow_scan

        def scan_a() -> None:
            service.note_user_prompt(PROFILE, "sess-b", "LanceDb 是什么")

        thread = threading.Thread(target=scan_a)
        thread.start()
        assert entered.wait(timeout=5), "scan A must reach its store read"
        _settle(client, "sess-b")  # end_session runs while the scan is in flight
        service._focal_scan = real_scan
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), "scan A must finish"
        memory = client.app.state.memory
        assert memory._pending_slots.get((PROFILE, "sess-b")) is None, "no re-park after settle"
        assert _pull(client, "sess-b")["items"] == []


def test_recall_pending_concurrent_pulls_serve_each_item_exactly_once(
    recall_config_path: Path,
) -> None:
    """Lock-removal mutant: two concurrent pulls of one slot must partition it
    exactly — the union of served ids has N distinct items and no id appears
    twice across the responses (serve = mark-seen is atomic under the lock)."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "LanceDb " + "甲" * 200)
        _ingest(client, "sess-a", 2.0, "LanceDb " + "乙" * 200)
        _settle(client, "sess-a")
        _ingest(client, "sess-b", 3.0, "LanceDb 相关")
        service = client.app.state.memory
        key = (PROFILE, "sess-b")
        with service._pending_lock:
            n = len(service._pending_slots[key])
        assert n >= 2, "the parked slot must carry two candidates"
        barrier = threading.Barrier(2)
        served: list[list[str]] = []

        def pull_thread() -> None:
            barrier.wait()
            payload = service.recall_pending(PROFILE, "sess-b", [])
            served.append([item["id"] for item in payload["items"]])

        threads = [threading.Thread(target=pull_thread) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        all_ids = [cid for batch in served for cid in batch]
        assert len(all_ids) == n, f"exactly N ids served across the pulls: {all_ids}"
        assert len(set(all_ids)) == n, f"no id may be served twice: {all_ids}"


def test_recall_pending_chunk_precedes_node_on_identical_decay_and_stamp(
    recall_config_path: Path,
) -> None:
    """Admission order at an identical (decay, stamp) tie is page-order-stable:
    the vector-page chunk precedes the graph-page node (the candidate list
    appends chunks first, and Python's sort is stable)."""
    with TestClient(create_app()) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        _settle(client, "sess-a")
        chunk_id = _store_chunks(client)["user: 上一轮我们把 LanceDb 定为向量存储"]
        chunk = client.app.state.stores.vector.get_chunk(chunk_id)
        assert chunk is not None
        node = GraphNode(
            profile_id=PROFILE,
            node_type=NodeType.DECISION,
            entities=["LanceDb"],
            props={"statement": "user: 上一轮我们确认 LanceDb 的定位"},
            decay_weight=chunk.decay_weight,
            updated_at=chunk.ingested_at,
            provenance=Provenance(asserted_by="dream-engine", source="consolidation"),
        )
        client.app.state.stores.graph.upsert_node(node)
        _ingest(client, "sess-b", 2.0, "LanceDb 相关")
        items = _pull(client, "sess-b")["items"]
        assert [item["kind"] for item in items] == ["chunk", "node"], items
        assert items[0]["id"] == chunk_id
        assert items[1]["text"] == "user: 上一轮我们确认 LanceDb 的定位"
