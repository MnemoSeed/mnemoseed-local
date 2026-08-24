"""B2.7 agent-side recall redesign (Scheme 2-lite + 3): the rules write path,
the ``rules_budget`` aggregation on /session/recent, and the MCP gateway rules
passthrough.

- Task A: ``/memory/remember`` carries ``rules``; all three near-duplicate
  branches (new / strong-consistent / conflict) persist rules on the chunk.
- Task B: ``/session/recent`` returns ``rules_budget`` with ABSENT semantics
  (the key is omitted when no rules apply), aggregating session/profile/global
  rules but never other-session scope.
- MCP: the remember tool schema accepts ``rules`` and the dispatch passes them
  through only when present (the no-rules body stays byte-identical).
- Task C (hook): the second ``<mnemoseed-rules-budget>`` fence is appended when
  the daemon supplies a rules_budget block.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.stamp import CognitiveTier, Cues, Provenance
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


# ---------------------------------------------------------------- Task A: remember rules


def test_remember_new_branch_stores_rules(config_path: Path) -> None:
    app = create_app()
    with TestClient(app) as client:
        rules = [
            {
                "kind": "exclude_entities",
                "value": ["secret"],
                "ttl_turns": 0,
                "scope": "profile",
                "session_id": None,
            }
        ]
        body = client.post(
            "/memory/remember", json={"profile_id": PROFILE, "text": "pin a fresh fact", "rules": rules}
        )
        assert body.status_code == 200, body.text
        assert body.json()["outcome"] == "new_chunk"
        got = app.state.stores.vector.get_chunk(body.json()["chunk_id"])
        assert got is not None
        assert got.rules == rules


def test_remember_reinforce_branch_merges_rules(config_path: Path) -> None:
    """Identical re-pin hits the strong-consistent branch; the new rules merge
    with the stored ones (union, ttl_turns takes the larger value)."""
    app = create_app()
    with TestClient(app) as client:
        r1 = {
            "kind": "exclude_entities",
            "value": ["a"],
            "ttl_turns": 1,
            "scope": "session",
            "session_id": "sx",
        }
        r2 = {
            "kind": "exclude_entities",
            "value": ["b"],
            "ttl_turns": 5,
            "scope": "profile",
            "session_id": None,
        }
        b1 = client.post(
            "/memory/remember", json={"profile_id": PROFILE, "text": "pin the same fact", "rules": [r1]}
        ).json()
        assert b1["outcome"] == "new_chunk"
        cid = b1["chunk_id"]
        b2 = client.post(
            "/memory/remember", json={"profile_id": PROFILE, "text": "pin the same fact", "rules": [r2]}
        ).json()
        assert b2["outcome"] == "reinforced"
        assert b2["chunk_id"] == cid
        got = app.state.stores.vector.get_chunk(cid)
        assert got is not None
        assert {(tuple(rule["value"]), rule["scope"]) for rule in got.rules} == {
            (("a",), "session"),
            (("b",), "profile"),
        }


def test_remember_conflict_branch_merges_rules_and_marks_reconcile(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conflict branch merges rules AND flags needs_reconcile on the same
    chunk (upsert for the rules, update_chunk_state for the flag)."""
    from mnemoseed_local.capture.stamper import ConsistencyVerdict, NearDuplicateChecker
    from mnemoseed_local.schema.stamp import ChunkStamp
    from mnemoseed_local.storage.ports import ChunkFilter, Page

    app = create_app()
    with TestClient(app) as client:
        stores = client.app.state.stores
        emb = stores.embed.embed("conflict fact text")
        stores.vector.upsert_chunk(
            ChunkStamp(
                chunk_id="conflict-1",
                profile_id=PROFILE,
                text="conflict fact text",
                cognitive_tier=CognitiveTier.TIER_1,
                model_id="user",
                cues=Cues(entities=[]),
                provenance=Provenance(asserted_by="user", source="manual"),
                rules=[
                    {
                        "kind": "exclude_entities",
                        "value": ["old"],
                        "ttl_turns": 1,
                        "scope": "profile",
                        "session_id": None,
                    }
                ],
            ),
            emb.dense,
            emb.sparse,
        )
        real_near = stores.vector.near_duplicate

        def fake_near(vec, threshold, profile_id=None):
            if threshold >= 0.9:  # reinforce threshold -> not a strong hit
                return []
            return [stores.vector.get_chunk("conflict-1")]

        monkeypatch.setattr(stores.vector, "near_duplicate", fake_near)
        monkeypatch.setattr(NearDuplicateChecker, "check", lambda self, a, b: ConsistencyVerdict.CONFLICT)
        body = client.post(
            "/memory/remember",
            json={
                "profile_id": PROFILE,
                "text": "conflict fact text",
                "rules": [
                    {
                        "kind": "exclude_entities",
                        "value": ["new"],
                        "ttl_turns": 2,
                        "scope": "profile",
                        "session_id": None,
                    }
                ],
            },
        )
        assert body.status_code == 200, body.text
        assert body.json()["outcome"] == "needs_reconcile"
        got = stores.vector.get_chunk("conflict-1")
        assert got is not None
        assert {tuple(rule["value"]) for rule in got.rules} == {("old",), ("new",)}
        flagged = stores.vector.list_chunks(
            ChunkFilter(profile_id=PROFILE, needs_reconcile=True), Page(limit=10)
        )
        assert {chunk.chunk_id for chunk in flagged.items} == {"conflict-1"}
        assert real_near is not None


# ---------------------------------------------------------------- Task B: rules_budget


def test_session_recent_rules_budget_absent_when_no_rules(config_path: Path) -> None:
    """Absent semantics: with no rules the ``rules_budget`` key is OMITTED, so
    the exact-match empty-profile pin is preserved."""
    with TestClient(create_app()) as client:
        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["sessions"] == [] and payload["self_window"] is None
        assert "rules_budget" not in payload


def test_session_recent_rules_budget_aggregates_and_scopes(config_path: Path) -> None:
    """Session/profile/global rules aggregate; another session's session-scoped
    rules never leak in. exclude_entities unions, entity_boost takes the max."""
    app = create_app()
    with TestClient(app) as client:
        client.post(
            "/memory/remember",
            json={
                "profile_id": PROFILE,
                "text": "rule fact one",
                "rules": [
                    {
                        "kind": "exclude_entities",
                        "value": ["secret1"],
                        "ttl_turns": 0,
                        "scope": "session",
                        "session_id": "sess-cur",
                    },
                    {
                        "kind": "entity_boost",
                        "value": ["boosted", "2.0"],
                        "ttl_turns": 0,
                        "scope": "profile",
                        "session_id": None,
                    },
                ],
            },
        )
        client.post(
            "/memory/remember",
            json={
                "profile_id": PROFILE,
                "text": "rule fact two",
                "rules": [
                    {
                        "kind": "exclude_entities",
                        "value": ["secret1", "secret2"],
                        "ttl_turns": 0,
                        "scope": "profile",
                        "session_id": None,
                    },
                    {
                        "kind": "entity_boost",
                        "value": ["boosted", "1.5"],
                        "ttl_turns": 0,
                        "scope": "profile",
                        "session_id": None,
                    },
                ],
            },
        )
        client.post(
            "/memory/remember",
            json={
                "profile_id": PROFILE,
                "text": "rule fact three",
                "rules": [
                    {
                        "kind": "exclude_entities",
                        "value": ["other-secret"],
                        "ttl_turns": 0,
                        "scope": "session",
                        "session_id": "sess-other",
                    }
                ],
            },
        )
        body = client.post("/session/recent", json={"profile_id": PROFILE, "self_session_id": "sess-cur"})
        assert body.status_code == 200, body.text
        payload = body.json()
        assert "rules_budget" in payload
        rb = payload["rules_budget"]
        assert set(rb["exclude_entities"]) == {"secret1", "secret2"}
        assert rb["entity_boost"] == {"boosted": 2.0}
        assert rb["auto_recall_focal_floor"] == 0.5
        assert rb["auto_recall_budget_chars"] == 2400
        assert rb["time_window_turns"] == 20


# ---------------------------------------------------------------- MCP gateway


def test_mcp_remember_passes_rules_when_present() -> None:
    from test_mcp_gateway import StubClient, _request, run_gateway

    payload = {"outcome": "stored", "chunk_id": "c-1"}
    client = StubClient(payload=payload)
    rules = [{"kind": "exclude_entities", "value": ["a"], "ttl_turns": 0, "scope": "profile"}]
    _, responses = run_gateway(
        [
            _request(
                4,
                "tools/call",
                {"name": "remember", "arguments": {"text": "x", "rules": rules}},
            )
        ],
        client,
    )
    assert responses[0]["result"]["isError"] is False
    assert client.calls == [("/memory/remember", {"profile_id": "default", "text": "x", "rules": rules})]


def test_mcp_remember_schema_has_rules_and_relaxes_additional_properties() -> None:
    from test_mcp_gateway import StubClient, _request, run_gateway

    _, responses = run_gateway([_request(2, "tools/list")], StubClient())
    tools = responses[0]["result"]["tools"]
    remember = next(tool for tool in tools if tool["name"] == "remember")
    schema = remember["inputSchema"]
    assert schema["required"] == ["text"]
    assert "rules" in schema["properties"]
    assert schema["additionalProperties"] is False
    assert "properties" in schema["properties"]["rules"]["items"]


# ---------------------------------------------------------------- Task C: hook rules fence


def test_rules_budget_fence_appended_when_daemon_supplies_block(tmp_path: Path) -> None:
    from test_hook_ts_behavior import _bundle, _run

    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "rules-budget")
    [system] = transcript["systems"]
    assert len(system) == 2, f"BASE + the rules fence expected: {system}"
    block = system[1]
    assert block.count("<mnemoseed-rules-budget>") == 1
    assert block.count("</mnemoseed-rules-budget>") == 1
    assert "daemon-supplied standing constraints" in block
    assert "secret1" in block


def test_upsert_chunk_merges_rules_keeps_larger_ttl_when_old_larger_via_remember(
    config_path: Path,
) -> None:
    """Daemon merge path: old ttl=9, new ttl=1 -> stored ttl stays 9 (max)."""
    app = create_app()
    with TestClient(app) as client:
        rule_old = {
            "kind": "exclude_entities",
            "value": ["a"],
            "ttl_turns": 9,
            "scope": "session",
            "session_id": "s_keep",
        }
        rule_new = {
            "kind": "exclude_entities",
            "value": ["a"],
            "ttl_turns": 1,
            "scope": "session",
            "session_id": "s_keep",
        }
        r1 = client.post(
            "/memory/remember", json={"profile_id": PROFILE, "text": "ttl keep old", "rules": [rule_old]}
        ).json()
        cid = r1["chunk_id"]
        r2 = client.post(
            "/memory/remember", json={"profile_id": PROFILE, "text": "ttl keep old", "rules": [rule_new]}
        ).json()
        assert r2["chunk_id"] == cid
        got = app.state.stores.vector.get_chunk(cid)
        assert got is not None
        assert got.rules == [{**rule_old, "ttl_turns": 9}]


def test_session_recent_malformed_rules_does_not_crash(config_path: Path) -> None:
    """A chunk with an invalid rule kind must not crash /session/recent."""
    app = create_app()
    with TestClient(app) as client:
        stores = client.app.state.stores
        from mnemoseed_local.schema.stamp import ChunkStamp

        emb = stores.embed.embed("malformed rules")
        stores.vector.upsert_chunk(
            ChunkStamp(
                chunk_id="mal-1",
                profile_id=PROFILE,
                text="malformed rules",
                cognitive_tier=CognitiveTier.TIER_1,
                model_id="user",
                cues=Cues(entities=[]),
                provenance=Provenance(asserted_by="user", source="manual"),
                rules=[{"kind": "invalid", "value": "x"}],
            ),
            emb.dense,
            emb.sparse,
        )
        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        data = body.json()
        # malformed rule is skipped, so no budget appears (absent semantics)
        assert "rules_budget" not in data


def test_session_recent_includes_global_scope(config_path: Path) -> None:
    app = create_app()
    with TestClient(app) as client:
        client.post(
            "/memory/remember",
            json={
                "profile_id": PROFILE,
                "text": "global rule fact",
                "rules": [
                    {
                        "kind": "exclude_entities",
                        "value": ["global-secret"],
                        "ttl_turns": 0,
                        "scope": "global",
                        "session_id": None,
                    }
                ],
            },
        )
        body = client.post("/session/recent", json={"profile_id": PROFILE, "self_session_id": "any"})
        assert body.status_code == 200, body.text
        rb = body.json().get("rules_budget")
        assert rb is not None
        assert "global-secret" in rb["exclude_entities"]


def test_mcp_remember_omits_rules_when_absent() -> None:
    from test_mcp_gateway import StubClient, _request, run_gateway

    payload = {"outcome": "stored", "chunk_id": "c-1"}
    client = StubClient(payload=payload)
    _, responses = run_gateway(
        [_request(5, "tools/call", {"name": "remember", "arguments": {"text": "x"}})],
        client,
    )
    assert responses[0]["result"]["isError"] is False
    assert client.calls == [("/memory/remember", {"profile_id": "default", "text": "x"})]


def test_session_recent_never_emits_rules_budget_null(config_path: Path) -> None:

    with TestClient(create_app()) as client:
        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        raw = body.text
        assert '"rules_budget": null' not in raw
        assert '"rules_budget":null' not in raw
        assert "rules_budget" not in body.json()
        # with a rule, the key is present and not null
        client.post(
            "/memory/remember",
            json={
                "profile_id": PROFILE,
                "text": "has rule",
                "rules": [
                    {
                        "kind": "exclude_entities",
                        "value": ["y"],
                        "ttl_turns": 0,
                        "scope": "profile",
                        "session_id": None,
                    }
                ],
            },
        )
        body2 = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body2.status_code == 200
        assert "rules_budget" in body2.json()
        assert body2.json()["rules_budget"] is not None


def test_rules_budget_fence_sanitizes_inner_fence(tmp_path: Path) -> None:
    from test_hook_ts_behavior import _bundle, _run

    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "rules-budget-sanitize")
    [system] = transcript["systems"]
    assert len(system) == 2, f"BASE + rules fence expected: {system}"
    block = system[1]
    # inner literal must be sanitized to ‹› form
    assert "‹mnemoseed-rules-budget›" in block
    assert block.count("<mnemoseed-rules-budget>") == 1
    assert block.count("</mnemoseed-rules-budget>") == 1


def test_session_recent_on_pre_b27_table_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-B2.7 DB without rules_json must still serve /session/recent (empty, no crash)."""
    from lancedb import connect

    from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore

    uri = tmp_path / "old.lance"
    db = connect(str(uri))
    probe = LanceDbEmbeddedStore(uri=tmp_path / "probe2.lance", dimensions=64)
    old_schema = probe._schema().remove(probe._schema().get_field_index("rules_json"))
    db.create_table("chunks", schema=old_schema)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "old.lance").as_posix()}"\ndimensions = 64\n'
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
    app = create_app()
    with TestClient(app) as client:
        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        assert body.json()["sessions"] == []
        # rules_not_null filter path inside _build_rules_budget must not crash
        assert "rules_budget" not in body.json()
