"""B2 standing-rule typed home + deterministic event-matchable advisory serve.

TDD RED phase: these tests assert the B2 brief sections 1-13 normative bars
through behavioral seams (real HTTP + real store). They must fail on the
pre-B2 tree (origin/main == be61779) for the right reasons - primarily
because RecallRule.kind lacks "standing_rule" so the /memory/remember route
rejects a standing_rule payload, and because the recall_pending event arm
never selects a typed rule. Then they go green with the B2 delta.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.stamp import EXPLICIT_PIN_SOURCE
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.ports import (
    ErrorEvent,
    ErrorEventFilter,
    ErrorSignalType,
    EvidenceKind,
    EvidencePointer,
    Page,
)
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

PROFILE = "default"
PROFILE_B = "other"

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


@pytest.fixture(autouse=True)
def _hermetic_plugin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MNEMOSEED_LOCAL_DEBUG", "MNEMOSEED_LOCAL_PROFILE_ID", "MNEMOSEED_LOCAL_BASEURL"):
        monkeypatch.delenv(var, raising=False)


def _config_toml(tmp_path: Path) -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[capture]\nauto_recall = true\n"
    )


@pytest.fixture
def b2_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


# ---------------------------------------------------------------- builders


def _match(**over: object) -> dict[str, Any]:
    base = {
        "family": "provider_error",
        "provider": "openai",
        "model": "gpt-4o",
        "status": ["quota"],
        "retryable": 0,
    }
    base.update(over)
    return base


def standing_value(*, over: dict[str, Any] | None = None) -> str:
    value: dict[str, Any] = {
        "if": "provider call fails",
        "then": "retry same provider once; on quota switch to the approved inventory model; else escalate",
        "match": _match(),
    }
    if over:
        value.update(over)
    return json.dumps(value)


def standing_rule_dict(
    *,
    value: str,
    scope: str = "profile",
    session_id: str | None = None,
    ttl_turns: int = 0,
) -> dict[str, Any]:
    return {
        "kind": "standing_rule",
        "value": value,
        "ttl_turns": ttl_turns,
        "scope": scope,
        "session_id": session_id,
    }


def remember_standing(client: TestClient, rule: dict[str, Any], profile_id: str = PROFILE) -> Any:
    return client.post(
        "/memory/remember",
        json={"profile_id": profile_id, "text": "standing directive pin", "rules": [rule]},
    )


def append_raw_event(
    client: TestClient,
    session_id: str,
    *,
    profile_id: str = PROFILE,
    status: str = "quota",
    retryable: int | None = 0,
    provider: str = "openai",
    model: str = "gpt-4o",
) -> int:
    """Write a PROVIDER_FAILURE row directly (deterministic, bypasses classifier)."""
    meta = client.app.state.stores.meta
    row = ErrorEvent(
        profile_id=profile_id,
        signal_type=ErrorSignalType.PROVIDER_FAILURE,
        observed_at=time.time(),
        evidence_ptr=EvidencePointer(kind=EvidenceKind.SESSION, id=session_id),
        session_id=session_id,
        detector_id="provider_error.v1",
        provider=provider,
        model=model,
        status=status,
        reason=f"provider_{status}_v1",
        retryable=retryable,
    )
    return meta.append_error_event(row)


def recall(client: TestClient, session_id: str, profile_id: str = PROFILE) -> dict[str, Any]:
    return client.post(
        "/session/recall-pending", json={"profile_id": profile_id, "session_id": session_id}
    ).json()


# ---------------------------------------------------------------- M-B2-5 typed read/write


def test_b2_5_import_persists_rules_json_with_provenance(b2_config: Path) -> None:
    """Importing a standing_rule stores rules_json + provenance; get_chunk reads it."""
    with TestClient(create_app()) as client:
        resp = remember_standing(client, standing_rule_dict(value=standing_value()))
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["outcome"] == "new_chunk"
        chunk = client.app.state.stores.vector.get_chunk(payload["chunk_id"])
        assert chunk is not None
        assert any(rule["kind"] == "standing_rule" for rule in chunk.rules)
        assert chunk.provenance.source == EXPLICIT_PIN_SOURCE
        assert chunk.rules[0]["value"] == standing_value()


# ---------------------------------------------------------------- M-B2-2 recent-surface exclusion


def test_b2_2_standing_rule_never_appears_in_session_recent_rules_budget(b2_config: Path) -> None:
    """A stored standing_rule never leaks onto /session/recent rules_budget."""
    with TestClient(create_app()) as client:
        resp = remember_standing(client, standing_rule_dict(value=standing_value()))
        assert resp.status_code == 200, resp.text
        data = client.post(
            "/session/recent", json={"profile_id": PROFILE, "self_session_id": "sess-x"}
        ).json()
        assert "rules_budget" not in data


def test_b2_22_t1_rules_budget_bytes_identical_with_standing_rule(b2_config: Path) -> None:
    """M-B2-22 (T1 clause, behavioral): /session/recent rules_budget bytes are
    byte-identical before vs after an in-profile standing_rule exists — the
    procedural rule never clips, caps, counts, or leaks into the T1 budget."""
    with TestClient(create_app()) as client:
        # B1 rules over a LONG exclude/boost surface so any clip/cap mutation
        # visibly truncates the serialized bytes.
        exclude_widgets = [f"secret-{i:04d}" for i in range(50)]
        resp = client.post(
            "/memory/remember",
            json={
                "profile_id": PROFILE,
                "text": "b1 rule fact",
                "rules": [
                    {
                        "kind": "exclude_entities",
                        "value": exclude_widgets,
                        "ttl_turns": 0,
                        "scope": "profile",
                        "session_id": None,
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
        assert resp.status_code == 200, resp.text

        def recent_rules_budget() -> tuple[dict, str]:
            body = client.post(
                "/session/recent", json={"profile_id": PROFILE, "self_session_id": "sess-t1"}
            ).json()
            rb = body.get("rules_budget")
            assert rb is not None, "B1 rules must produce a present rules_budget"
            return rb, json.dumps(rb, sort_keys=True, separators=(",", ":"))

        before_dict, before_bytes = recent_rules_budget()

        # author an in-profile standing_rule (must not perturb the T1 budget)
        r = remember_standing(client, standing_rule_dict(value=standing_value()))
        assert r.status_code == 200, r.text

        after_dict, after_bytes = recent_rules_budget()

        assert after_dict == before_dict, (  # no clip/cap/counter introduced
            f"rules_budget changed once a standing_rule existed: before={before_dict!r} after={after_dict!r}"
        )
        assert after_bytes == before_bytes, (
            f"serialized rules_budget not byte-identical: before={before_bytes!r} after={after_bytes!r}"
        )


# ---------------------------------------------------------------- M-B2-6/7/13 serve basics


def test_b2_6_armed_event_no_standing_rule_unresolved_no_rule(b2_config: Path) -> None:
    """No-match armed event -> unresolved=true reason=no-rule."""
    with TestClient(create_app()) as client:
        append_raw_event(client, "sess-1", status="quota", retryable=0)
        data = recall(client, "sess-1")
        assert data["detector_fired"] is True
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_7_wrong_fingerprint_no_match(b2_config: Path) -> None:
    """Provider mismatch -> no serve, unresolved."""
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-p", provider="anthropic", status="quota", retryable=0)
        data = recall(client, "sess-p")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_13_no_event_no_serve(b2_config: Path) -> None:
    """No event -> no serve, detector_fired=false (B1 M8 preserved)."""
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        data = recall(client, "sess-none")
        assert data["detector_fired"] is False
        assert data["rule_served"] is False


# ---------------------------------------------------------------- M-B2-17 flag contract


def test_b2_17_matched_serve_flag_contract(b2_config: Path) -> None:
    """A matched serve always sets rule_served=true and unresolved=false."""
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-ok", status="quota", retryable=0)
        data = recall(client, "sess-ok")
        assert data["rule_served"] is True
        assert data["unresolved"] is False
        assert data["rule_count"] == 1
        assert data["provider"] == "openai"
        assert data["rule_advisory"] == standing_value()


def test_b2_17_disabled_flag_contract(b2_config: Path) -> None:
    """auto_recall off -> enabled=false rule_served=false unresolved=false."""
    import dataclasses

    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-d", status="quota", retryable=0)
        svc = client.app.state.memory
        cfg = svc._config
        svc._config = dataclasses.replace(cfg, capture=dataclasses.replace(cfg.capture, auto_recall=False))
        data = recall(client, "sess-d")
        assert data["enabled"] is False
        assert data["detector_fired"] is False
        assert data["rule_served"] is False
        assert data["unresolved"] is False


# ---------------------------------------------------------------- M-B2-16 precedence


def test_b2_16_specific_precedence_over_wildcard(b2_config: Path) -> None:
    """Specific (provider=openai) beats broad (provider=null)."""
    with TestClient(create_app()) as client:
        broad = standing_value(
            over={
                "then": "broad advisory",
                "match": {
                    "family": "provider_error",
                    "provider": None,
                    "model": None,
                    "status": None,
                    "retryable": None,
                },
            }
        )
        specific = standing_value(over={"then": "specific advisory"})
        remember_standing(client, standing_rule_dict(value=broad, scope="profile"))
        remember_standing(client, standing_rule_dict(value=specific, scope="profile"))
        append_raw_event(client, "sess-spec", status="quota", retryable=0)
        data = recall(client, "sess-spec")
        assert data["rule_served"] is True
        assert data["rule_advisory"] == specific


def test_b2_16_tie_is_conflict_not_first_row(b2_config: Path) -> None:
    """Two truly-tied rules (same chunk, identical specificity) -> conflict."""
    with TestClient(create_app()) as client:
        v1 = standing_value(over={"then": "advisory alpha"})
        v2 = standing_value(over={"then": "advisory beta"})
        # both on the SAME chunk -> identical ingested_at + chunk_id; byte-same
        # match payload -> identical specificity => a true tuple tie.
        resp = client.post(
            "/memory/remember",
            json={
                "profile_id": PROFILE,
                "text": "tie pin",
                "rules": [
                    standing_rule_dict(value=v1, scope="session", session_id="sess-e"),
                    standing_rule_dict(value=v2, scope="session", session_id="sess-e"),
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        append_raw_event(client, "sess-e", status="quota", retryable=0)
        data = recall(client, "sess-e")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "conflict"
        assert data["conflict_reason"]


# ---------------------------------------------------------------- M-B2-8 scope


def test_b2_8_session_scope_never_crosses_session(b2_config: Path) -> None:
    with TestClient(create_app()) as client:
        remember_standing(
            client,
            standing_rule_dict(value=standing_value(), scope="session", session_id="sess-a"),
        )
        append_raw_event(client, "sess-b", status="quota", retryable=0)
        data = recall(client, "sess-b")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_8_profile_scope_never_crosses_profile(b2_config: Path) -> None:
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value(), scope="profile"), PROFILE)
        append_raw_event(client, "sess-b", profile_id=PROFILE_B, status="quota", retryable=0)
        data = recall(client, "sess-b", PROFILE_B)
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_8_global_serves_all_sessions_of_own_profile(b2_config: Path) -> None:
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value(), scope="global"))
        append_raw_event(client, "g1", status="quota", retryable=0)
        data1 = recall(client, "g1")
        assert data1["rule_served"] is True
        assert data1["unresolved"] is False
        append_raw_event(client, "g2", status="quota", retryable=0)
        data2 = recall(client, "g2")
        assert data2["rule_served"] is True
        assert data2["unresolved"] is False


def test_b2_8_global_never_serves_other_profile(b2_config: Path) -> None:
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value(), scope="global"), PROFILE)
        append_raw_event(client, "sess-x", profile_id=PROFILE_B, status="quota", retryable=0)
        data = recall(client, "sess-x", PROFILE_B)
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


# ---------------------------------------------------------------- M-B2-9 idempotent import


def test_b2_9_byte_same_reimport_single_artifact(b2_config: Path) -> None:
    with TestClient(create_app()) as client:
        r1 = remember_standing(client, standing_rule_dict(value=standing_value()))
        r2 = remember_standing(client, standing_rule_dict(value=standing_value()))
        assert r1.json()["chunk_id"] == r2.json()["chunk_id"]
        count = len(
            client.app.state.stores.vector.list_chunks(
                __import__("mnemoseed_local.storage.ports", fromlist=["ChunkFilter"]).ChunkFilter(
                    profile_id=PROFILE, rules_not_null=True
                ),
                Page(offset=0, limit=1000),
            ).items
        )
        assert count == 1


# ---------------------------------------------------------------- M-B2-10 malformed + unknown kind


def test_b2_10_malformed_standing_rule_no_match_unresolved(b2_config: Path) -> None:
    """Malformed match JSON on a standing_rule -> no-match unresolved (never crash/false-serve)."""
    with TestClient(create_app()) as client:
        malformed = standing_value(over={"match": "not-an-object"})
        remember_standing(client, standing_rule_dict(value=malformed))
        append_raw_event(client, "sess-m", status="quota", retryable=0)
        data = recall(client, "sess-m")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_10_future_kind_loud_skip_no_crash(b2_config: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A future/unknown kind is loud-skipped on the NEW daemon; no crash, no false-serve."""
    from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Provenance

    with TestClient(create_app()) as client:
        vec = client.app.state.stores.vector
        emb = client.app.state.stores.embed.embed("future rule chunk")
        stamp = ChunkStamp(
            chunk_id="future-1",
            profile_id=PROFILE,
            text="future rule chunk",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="user",
            provenance=Provenance(asserted_by="user", source="memory.remember", confidence=1.0),
            rules=[{"kind": "arbitrary_future", "value": "x", "scope": "profile", "session_id": None}],
        )
        vec.upsert_chunk(stamp, emb.dense, emb.sparse)
        with caplog.at_level("WARNING", logger="mnemoseed_local.daemon.memory"):
            data = client.post("/session/recent", json={"profile_id": PROFILE}).json()
        assert "rules_budget" not in data
        assert any("unknown" in str(r.message).lower() for r in caplog.records)


# ---------------------------------------------------------------- M-B2-15 retryable NULL


def test_b2_15_other_provider_null_retryable_fail_closed(b2_config: Path) -> None:
    """other_provider (retryable NULL) does NOT match a rule bound to retryable:0."""
    with TestClient(create_app()) as client:
        rule_value = standing_value(
            over={
                "match": {
                    "family": "provider_error",
                    "provider": None,
                    "model": None,
                    "status": ["other_provider"],
                    "retryable": 0,
                }
            }
        )
        remember_standing(client, standing_rule_dict(value=rule_value))
        append_raw_event(client, "sess-op", status="other_provider", retryable=None)
        data = recall(client, "sess-op")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_15_retryable_null_wildcard_matches_null_event(b2_config: Path) -> None:
    """Rule retryable:null wildcard matches an other_provider (retryable NULL) event."""
    with TestClient(create_app()) as client:
        remember_standing(
            client,
            standing_rule_dict(
                value=standing_value(
                    over={
                        "match": {
                            "family": "provider_error",
                            "provider": None,
                            "model": None,
                            "status": None,
                            "retryable": None,
                        }
                    }
                )
            ),
        )
        append_raw_event(client, "sess-w", status="other_provider", retryable=None)
        data = recall(client, "sess-w")
        assert data["rule_served"] is True
        assert data["unresolved"] is False


# ---------------------------------------------------------------- M-B2-18 provenance


def test_b2_18_matched_rule_source_is_provenance_of_winner(b2_config: Path) -> None:
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value(over={"then": "winner"})))
        append_raw_event(client, "sess-pv", status="quota", retryable=0)
        data = recall(client, "sess-pv")
        assert data["rule_served"] is True
        assert data["matched_rule_source"] == EXPLICIT_PIN_SOURCE


# ---------------------------------------------------------------- M-B2-19 forget (GDPR hard-delete)


def test_b2_19_forget_hard_deletes_chunk_and_stops_serve_row_remains(b2_config: Path) -> None:
    with TestClient(create_app()) as client:
        resp = remember_standing(client, standing_rule_dict(value=standing_value()))
        chunk_id = resp.json()["chunk_id"]
        append_raw_event(client, "sess-f", status="quota", retryable=0)
        assert recall(client, "sess-f")["rule_served"] is True
        # forget deletes the artifact chunk (GDPR)
        fr = client.post("/memory/forget_this", json={"profile_id": PROFILE, "chunk_id": chunk_id})
        assert fr.status_code == 200, fr.text
        assert client.app.state.stores.vector.get_chunk(chunk_id) is None
        # serve stops
        append_raw_event(client, "sess-f2", status="quota", retryable=0)
        data = recall(client, "sess-f2")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"
        # error_events evidence row is append-only and still present
        page = client.app.state.stores.meta.query_error_events(
            ErrorEventFilter(profile_id=PROFILE), Page(0, 50)
        )
        assert len(page.items) == 2


# ---------------------------------------------------------------- M-B2-11 advisory-only / M-B2-12 zero-model


def test_b2_11_no_provider_switch_side_effect(b2_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve performs no provider-switch / host-config mutation (advisory-only)."""
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-ns", status="quota", retryable=0)
        svc = client.app.state.memory
        vector = svc._stores.vector
        meta = svc._stores.meta
        calls: list[str] = []

        def _spy_upsert(*a: object, **k: object):
            calls.append("upsert_chunk")
            return None

        def _spy_delete(*a: object, **k: object):
            calls.append("delete_chunk")
            return None

        def _spy_append(*a: object, **k: object):
            calls.append("append_error_event")
            return None

        monkeypatch.setattr(vector, "upsert_chunk", _spy_upsert)
        monkeypatch.setattr(vector, "delete_chunk", _spy_delete)
        monkeypatch.setattr(meta, "append_error_event", _spy_append)
        data = recall(client, "sess-ns")
        assert data["rule_served"] is True
    assert calls == [], f"serve must not mutate host/store state: {calls}"


def test_b2_12_zero_model_calls_on_serve(b2_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No embedder/model call during recall_pending serve/flag."""
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-z", status="quota", retryable=0)
        svc = client.app.state.memory
        calls: list[str] = []

        def _spy_embed(*a: object, **k: object):
            calls.append("embed")

        monkeypatch.setattr(svc._stores.embed, "embed", _spy_embed)
        data = recall(client, "sess-z")
        assert data["rule_served"] is True
    assert calls == []


# ---------------------------------------------------------------- M-B2-20 item budget survives + consume-once


def test_b2_20_budget_chars_unchanged_and_consume_once(b2_config: Path) -> None:
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-bu", status="quota", retryable=0)
        first = recall(client, "sess-bu")
        assert first["rule_served"] is True
        assert first["budget_chars"] == 2400
        # consume-once: a re-pull of the same consumed nomination yields no second advisory
        second = recall(client, "sess-bu")
        assert second["detector_fired"] is False
        assert second["rule_served"] is False
        assert second["unresolved"] is False


# ---------------------------------------------------------------- M-B2-24 ttl==0 required


def test_b2_24_nonzero_ttl_standing_rule_fails_closed(b2_config: Path) -> None:
    """A standing_rule with nonzero ttl_turns is not served (fail-closed)."""
    with TestClient(create_app()) as client:
        resp = remember_standing(
            client,
            standing_rule_dict(value=standing_value(), ttl_turns=3),
        )
        # import succeeds (remember is permissive) but the event arm must not serve it
        assert resp.status_code == 200, resp.text
        append_raw_event(client, "sess-ttl", status="quota", retryable=0)
        data = recall(client, "sess-ttl")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


# ---------------------------------------------------------------- M-B2-7 fingerprint mismatches (per-field)


def test_b2_7_model_mismatch_no_match(b2_config: Path) -> None:
    """Model mismatch -> no serve, unresolved (fail-closed)."""
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-m", model="other-model", status="quota", retryable=0)
        data = recall(client, "sess-m")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_7_status_mismatch_no_match(b2_config: Path) -> None:
    """Status mismatch -> no serve, unresolved (fail-closed)."""
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-s", status="rate_limit", retryable=1)
        data = recall(client, "sess-s")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_7_retryable_mismatch_no_match(b2_config: Path) -> None:
    """Retryable 0-rule vs 1-event -> no serve, unresolved (fail-closed)."""
    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-r", status="rate_limit", retryable=1)
        data = recall(client, "sess-r")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_7_wildcards_match_any_event(b2_config: Path) -> None:
    """Null provider/model/status wildcards match any concrete event."""
    with TestClient(create_app()) as client:
        wildcard = standing_value(
            over={
                "match": {
                    "family": "provider_error",
                    "provider": None,
                    "model": None,
                    "status": None,
                    "retryable": None,
                }
            }
        )
        remember_standing(client, standing_rule_dict(value=wildcard))
        append_raw_event(client, "sess-w", provider="anthropic", model="m", status="timeout", retryable=1)
        data = recall(client, "sess-w")
        assert data["rule_served"] is True
        assert data["unresolved"] is False


def test_b2_15_bool_retryable_never_matches(b2_config: Path) -> None:
    """A JSON-bool retryable bound is malformed for matching: never serves."""
    with TestClient(create_app()) as client:
        bool_rule = standing_value(
            over={
                "match": {
                    "family": "provider_error",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "status": ["quota"],
                    "retryable": True,
                }
            }
        )
        remember_standing(client, standing_rule_dict(value=bool_rule))
        append_raw_event(client, "sess-b", status="quota", retryable=1)
        data = recall(client, "sess-b")
        assert data["rule_served"] is False
        assert data["unresolved"] is True
        assert data["reason"] == "no-rule"


def test_b2_16_newest_wins_on_equal_specificity(b2_config: Path) -> None:
    """Equal-specificity winners resolve deterministically to the newest artifact."""
    with TestClient(create_app()) as client:
        first = standing_value(over={"then": "first advisory"})
        second = standing_value(over={"then": "second advisory"})
        for text, value in (("standing directive pin alpha", first), ("standing directive pin beta", second)):
            resp = client.post(
                "/memory/remember",
                json={"profile_id": PROFILE, "text": text, "rules": [standing_rule_dict(value=value)]},
            )
            assert resp.status_code == 200, resp.text
        append_raw_event(client, "sess-n", status="quota", retryable=0)
        data = recall(client, "sess-n")
        assert data["rule_served"] is True
        assert data["rule_advisory"] == second


def test_b2_17_disabled_reason_code(b2_config: Path) -> None:
    """Disabled serve carries the minimal reason code (selection surface)."""
    import dataclasses

    with TestClient(create_app()) as client:
        remember_standing(client, standing_rule_dict(value=standing_value()))
        append_raw_event(client, "sess-dd", status="quota", retryable=0)
        svc = client.app.state.memory
        cfg = svc._config
        svc._config = dataclasses.replace(cfg, capture=dataclasses.replace(cfg.capture, auto_recall=False))
        data = recall(client, "sess-dd")
        assert data["enabled"] is False
        assert data["rule_served"] is False
        assert data["unresolved"] is False
        assert data["reason"] == "disabled"
