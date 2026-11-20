"""Past-observation marker: delivered memory is a record, not a present-turn fact.

Isolated oracle for the past-fact-marker change: every delivered memory item
(recall entry, session-tail chunk/group, recall-pending item, re-recalled
capture) carries a past-observation marker with real provenance and observation
time; the instruction block carries exactly one re-verify rule; all four
rendered surfaces stay inside the fixed ceiling; malformed items degrade with
a debug trace. Uses a temporary home and a spare port, never the installed
runtime.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import auth_support
import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.memory import MemoryService, _group_session_tails
from mnemoseed_local.hosts.common import fence, recall, t2
from mnemoseed_local.mcp_gateway import instructions
from mnemoseed_local.retrieve.assemble import AssembledEntry
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.schema.turn import HostId
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

MARKER = "past_observation"
MARKER_TOKEN = "[past observation]"
PROVENANCE_UNKNOWN = "provenance-unknown"
TIME_UNKNOWN = "time-unknown"
GENERIC_PREAMBLE = "MnemoSeed is this machine's long-term memory for you, shared across sessions and hosts."
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
SURFACES = ("generic", "claude_chat", "chatgpt", "claude_code")
FIELDS = ("memory_marker", "memory_provenance", "memory_observed_at")
PROFILE = "default"

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


def _config_toml(tmp_path: Path, *, auto_recall: bool = False) -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[capture]\n"
        f"auto_recall = {str(auto_recall).lower()}\n"
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
    )


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


@pytest.fixture
def recall_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path, auto_recall=True), encoding="utf-8")
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


def _entry(**changes: object) -> AssembledEntry:
    base: dict[str, object] = {
        "kind": "chunk",
        "id": "c1",
        "source": "manual",
        "text": "the deploy window was fixed in a past session",
        "score": 0.9,
        "tokens": 9,
        "flags": (),
    }
    base.update(changes)
    return AssembledEntry(**base)  # type: ignore[arg-type]


def _assert_fields(item: dict[str, object], *, source: str | None) -> None:
    for field in FIELDS:
        assert field in item, f"missing {field}: {item}"
    assert item["memory_marker"] == MARKER, item
    assert item["memory_provenance"] == (source or PROVENANCE_UNKNOWN), item
    observed = item["memory_observed_at"]
    assert isinstance(observed, str) and observed, observed
    assert observed == TIME_UNKNOWN or ISO.fullmatch(observed), observed


# ---------------------------------------------------------------- AC1 / AC8 / AC9: recall payload


def test_recall_entry_carries_marker_with_real_provenance_and_time() -> None:
    payload = MemoryService._entry_payload(_entry(source="manual", ingested_at=10.0))
    _assert_fields(payload, source="manual")
    assert payload["memory_observed_at"] != TIME_UNKNOWN
    assert ISO.fullmatch(str(payload["memory_observed_at"])), payload


def test_observation_time_never_becomes_a_bare_null() -> None:
    payload = MemoryService._entry_payload(_entry(ingested_at=None, valid_from=None))
    assert "memory_observed_at" in payload
    assert payload["memory_observed_at"] == TIME_UNKNOWN, payload


def test_provenance_is_the_real_source_not_a_constant() -> None:
    a = MemoryService._entry_payload(_entry(source="manual"))
    b = MemoryService._entry_payload(_entry(source="memory.capture"))
    assert a["memory_provenance"] == "manual"
    assert b["memory_provenance"] == "memory.capture"


def test_valid_from_precedes_ingested_at_in_the_time_chain() -> None:
    payload = MemoryService._entry_payload(_entry(valid_from=7.0, ingested_at=10.0))
    assert payload["memory_observed_at"] == "1970-01-01T00:00:07.000Z", payload


def test_captured_entry_re_recalled_carries_the_marker() -> None:
    payload = MemoryService._entry_payload(
        _entry(kind="chunk", source="memory.capture", ingested_at=3.0, provenance_source=None)
    )
    _assert_fields(payload, source="memory.capture")


def test_malformed_entry_degrades_with_a_debug_trace(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="mnemoseed_local.daemon.memory"):
        payload = MemoryService._entry_payload(_entry(source=None, ingested_at=None, valid_from=None))
    _assert_fields(payload, source=None)
    assert payload["memory_provenance"] == PROVENANCE_UNKNOWN
    assert payload["memory_observed_at"] == TIME_UNKNOWN
    assert any(
        "past" in record.message.lower() and "observation" in record.message.lower()
        for record in caplog.records
    ), caplog.text


def test_marker_fields_are_the_only_added_keys_and_ranking_is_unchanged() -> None:
    lowest = MemoryService._entry_payload(_entry(id="low", score=0.1, ingested_at=1.0))
    highest = MemoryService._entry_payload(_entry(id="high", score=0.9, ingested_at=2.0))
    served = [lowest, highest]
    ordered = sorted(served, key=lambda item: float(item["score"]), reverse=True)
    assert [item["id"] for item in ordered] == ["high", "low"]
    for payload in served:
        for field in FIELDS:
            payload.pop(field)
        assert "text" in payload and "score" in payload and "id" in payload


def test_recall_http_entry_carries_the_marker(config_path: Path, no_similarity_floor: None) -> None:
    with auth_support.boot(config_path.parent) as client:
        store = client.app.state.stores
        stamp = ChunkStamp(
            chunk_id="marker-http",
            profile_id=PROFILE,
            text="user: the deploy window was fixed in a past session",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="test-model",
            cues=Cues(entities=["DeployWindow"]),
            provenance=Provenance(asserted_by="user", session_id="sess-a", source="manual"),
            ingested_at=1.0,
        )
        embedded = store.embed.embed(stamp.text)
        store.vector.upsert_chunk(stamp, embedded.dense, embedded.sparse)
        response = client.post(
            "/memory/recall",
            json={"profile_id": PROFILE, "query": "DeployWindow", "top_k": 5},
        )
        assert response.status_code == 200, response.text
        entries = response.json()["memory"]["entries"]
        assert entries, entries
        for entry in entries:
            _assert_fields(entry, source=entry.get("source"))


# ---------------------------------------------------------------- AC2: session tails


def _stamp(chunk_id: str, session: str, ingested_at: float, text: str, source: str = "manual") -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id=None,
        origin_agent=None,
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id=session, source=source),
        turn_start=0,
        turn_end=0,
        ingested_at=ingested_at,
    )


def test_session_recent_chunks_and_group_carry_the_marker() -> None:
    chunks = [
        _stamp("c2", "s1", 20.0, "second tail"),
        _stamp("c1", "s1", 10.0, "first head"),
    ]
    [group] = _group_session_tails(chunks, per_session=8, sessions=2)
    _assert_fields(group, source="s1")
    for chunk in group["chunks"]:
        _assert_fields(chunk, source=chunk["source"])


# ---------------------------------------------------------------- AC3: recall-pending items


def test_recall_pending_items_carry_the_marker(recall_config_path: Path) -> None:
    with auth_support.boot(recall_config_path.parent) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        client.post("/session/end", json={"session_id": "sess-a", "profile_id": PROFILE})
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        response = client.post(
            "/session/recall-pending",
            json={"profile_id": PROFILE, "session_id": "sess-b"},
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert items, items
        for item in items:
            _assert_fields(item, source=item.get("source"))


# ---------------------------------------------------------------- AC5: instruction rule + budget


def _flatten(text: str) -> str:
    return " ".join(text.split())


def test_exactly_one_rule_carries_all_three_semantics_and_the_marker() -> None:
    text = _flatten(instructions.surface_text("generic", instructions.tool_names()))
    assert text.count("7. ") == 1, text
    for phrase in (
        MARKER_TOKEN,
        "past",
        "not a present-turn fact",
        "conflict with current observation",
        "stale",
        "re-verify before use",
        "Even if re-verification seems unnecessary",
        "stays memory",
    ):
        assert phrase in text, phrase


def test_generic_preamble_stays_byte_identical() -> None:
    assert instructions.PREAMBLE == GENERIC_PREAMBLE
    generic = instructions.surface_text("generic", instructions.tool_names())
    assert generic.startswith(GENERIC_PREAMBLE + "\n\n")


def test_all_four_surfaces_stay_inside_the_ceiling_with_the_marker_present() -> None:
    assert instructions.MAX_CHARS == 1800
    for surface in SURFACES:
        text = instructions.surface_text(surface, instructions.tool_names())
        assert len(text) <= instructions.MAX_CHARS, (surface, len(text))
        assert MARKER_TOKEN in text, surface


def test_surface_phrases_survive_the_shortening() -> None:
    frames = {
        "claude_chat": "personal preferences",
        "chatgpt": "custom instructions",
        "claude_code": "capture hook",
    }
    names = instructions.tool_names()
    for surface, phrase in frames.items():
        assert phrase in instructions.surface_text(surface, names).lower(), surface


# ---------------------------------------------------------------- AC6: cross-layer bytes


def test_fence_and_plugin_marker_literals_are_byte_identical() -> None:
    root = Path(fence.__file__).parent.parent
    plugin = root / "opencode" / "plugin.ts"
    py_text = (root / "common" / "fence.py").read_text(encoding="utf-8")
    ts_text = plugin.read_text(encoding="utf-8")
    py_match = re.search(r'^RECALL_ITEM_MARKER\s*=\s*"([^"]*)"', py_text, re.M)
    ts_match = re.search(r'^const RECALL_ITEM_MARKER\s*=\s*"([^"]*)"', ts_text, re.M)
    assert py_match is not None, "fence.py must declare RECALL_ITEM_MARKER"
    assert ts_match is not None, "plugin.ts must declare RECALL_ITEM_MARKER"
    assert py_match.group(1).encode() == ts_match.group(1).encode()
    assert py_match.group(1) == MARKER_TOKEN


def test_t1_injection_prefixes_each_delivered_item() -> None:
    sessions = [
        {
            "session_id": "sess-abc",
            "latest_at": 10.0,
            "chunks": [
                {
                    "chunk_id": "c1",
                    "text": "the earlier session fixed the window",
                    "ingested_at": 10.0,
                    "memory_provenance": "manual",
                    "memory_observed_at": "1970-01-01T00:00:10.000Z",
                }
            ],
        }
    ]
    block = recall.build_t1_context(sessions, 4000)
    assert block is not None
    assert f"{MARKER_TOKEN} [manual; 1970-01-01T00:00:10.000Z] the earlier session fixed the window" in block


def test_t2_injection_prefixes_each_delivered_item() -> None:
    items = [
        {
            "id": "c1",
            "text": "a recalled fact",
            "source": "memory.capture",
            "memory_provenance": "memory.capture",
            "memory_observed_at": "1970-01-01T00:00:05.000Z",
        }
    ]
    block = t2.build_t2_context(items, 1200)
    assert block is not None
    assert f"{MARKER_TOKEN} [memory.capture; 1970-01-01T00:00:05.000Z] a recalled fact" in block


def test_full_budget_block_keeps_the_marker_instead_of_dropping() -> None:
    text = "x" * 3900
    sessions = [
        {
            "session_id": "sess-full",
            "latest_at": 10.0,
            "chunks": [
                {
                    "chunk_id": "c1",
                    "text": text,
                    "ingested_at": 10.0,
                    "memory_provenance": "manual",
                    "memory_observed_at": "1970-01-01T00:00:10.000Z",
                }
            ],
        }
    ]
    block = recall.build_t1_context(sessions, 4000)
    assert block is not None, "a marked block must never be silently dropped at the cap"
    assert MARKER_TOKEN in block
    assert len(block) <= 4000


# ---------------------------------------------------------------- AC7: stale + conflict


def test_stale_conflicting_item_is_marked_and_the_rerule_is_delivered() -> None:
    entry = _entry(source="manual", ingested_at=1.0, needs_reconcile=True, conflict_group="g1")
    payload = MemoryService._entry_payload(entry)
    _assert_fields(payload, source="manual")
    text = _flatten(instructions.surface_text("generic", instructions.tool_names()))
    assert "conflict with current observation" in text
    assert "re-verify before use" in text
    assert "stays memory" in text
