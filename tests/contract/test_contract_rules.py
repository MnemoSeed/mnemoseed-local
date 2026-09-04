"""B2.7 rules column contract: driver-level rules merge + rules_not_null filter.

The rules column is the Scheme 2-lite write path: rules ride the chunk
metadata (``rules_json``) and are merged on a re-upsert of the same chunk_id
(union by rule identity, ``ttl_turns`` takes the larger value). The merge is a
driver responsibility so the daemon's three remember branches share one
mechanism; usage counters survive a matched upsert (a re-remember must never
clobber them).
"""

from __future__ import annotations

import json
from pathlib import Path

from _support import DIMENSION, PROFILE, make_stamp, raw_chunk
from lancedb import connect

from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.ports import ChunkFilter, Page

_RULE_A = {"kind": "exclude_entities", "value": ["a"], "ttl_turns": 1, "scope": "session", "session_id": "s1"}
_RULE_A9 = {
    "kind": "exclude_entities",
    "value": ["a"],
    "ttl_turns": 9,
    "scope": "session",
    "session_id": "s1",
}
_RULE_B = {"kind": "exclude_entities", "value": ["b"], "ttl_turns": 2, "scope": "profile", "session_id": None}
_RULE_PROF = {
    "kind": "exclude_entities",
    "value": ["z"],
    "ttl_turns": 0,
    "scope": "profile",
    "session_id": None,
}


def _standing_value_obj() -> dict:
    return {
        "if": "provider call fails",
        "then": "retry same provider once; on quota escalate to the approved inventory model",
        "match": {
            "family": "provider_error",
            "provider": "openai",
            "model": "gpt-4o",
            "status": ["quota"],
            "retryable": 0,
        },
    }


_STANDING_VALUE_A = json.dumps(_standing_value_obj(), sort_keys=True)
_STANDING_VALUE_B = json.dumps({"then": "b", "match": _standing_value_obj()["match"]}, sort_keys=True)

_RULE_STANDING = {
    "kind": "standing_rule",
    "value": _STANDING_VALUE_A,
    "ttl_turns": 0,
    "scope": "profile",
    "session_id": None,
}


def _with_rules(stack, chunk_id: str, text: str, rules: list[dict]) -> object:
    stamp = make_stamp(chunk_id, text)
    stamp.rules = rules
    return stamp


def test_upsert_chunk_persists_rules(stack) -> None:
    emb = stack.embed.embed("alpha rules")
    stack.vector.upsert_chunk(_with_rules(stack, "x1", "alpha rules", [_RULE_A]), emb.dense, emb.sparse)
    got = stack.vector.get_chunk("x1")
    assert got is not None
    assert got.rules == [_RULE_A]


def test_upsert_chunk_merges_rules_union_and_max_ttl(stack) -> None:
    """Re-upserting the same chunk_id merges rules: same-identity rules keep the
    larger ttl_turns; new identities append (union)."""
    emb = stack.embed.embed("alpha rules")
    stack.vector.upsert_chunk(_with_rules(stack, "x1", "alpha rules", [_RULE_A]), emb.dense, emb.sparse)
    stack.vector.upsert_chunk(
        _with_rules(stack, "x1", "alpha rules", [_RULE_A9, _RULE_B]), emb.dense, emb.sparse
    )
    got = stack.vector.get_chunk("x1")
    assert got is not None
    assert got.rules == [{**_RULE_A, "ttl_turns": 9}, _RULE_B]


def test_upsert_chunk_preserves_usage_counters_on_match(stack) -> None:
    """A matched upsert (rules merge) must never clobber the usage counters or
    the reconcile flag — the remember merge path shares the row."""
    emb = stack.embed.embed("counter rules")
    stack.vector.upsert_chunk(_with_rules(stack, "c1", "counter rules", []), emb.dense, emb.sparse)
    stack.vector.update_chunk_state(["c1"], hit_increment=3, needs_reconcile=True)
    stack.vector.upsert_chunk(_with_rules(stack, "c1", "counter rules", [_RULE_PROF]), emb.dense, emb.sparse)
    row = raw_chunk(stack, "c1")
    assert int(row["hit_count"]) == 3
    assert row["needs_reconcile"] is True
    got = stack.vector.get_chunk("c1")
    assert got is not None and got.rules == [_RULE_PROF]


def test_list_chunks_rules_not_null_filter(stack) -> None:
    emb = stack.embed.embed("rules filter")
    stack.vector.upsert_chunk(_with_rules(stack, "wf", "rules filter", [_RULE_PROF]), emb.dense, emb.sparse)
    stack.vector.upsert_chunk(_with_rules(stack, "wo", "rules filter", []), emb.dense, emb.sparse)
    result = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE, rules_not_null=True), Page(limit=10))
    assert {chunk.chunk_id for chunk in result.items} == {"wf"}
    all_rows = stack.vector.list_chunks(ChunkFilter(profile_id=PROFILE), Page(limit=10))
    assert {chunk.chunk_id for chunk in all_rows.items} == {"wf", "wo"}


def test_upsert_chunk_merges_rules_keeps_larger_ttl_when_old_larger(stack) -> None:
    """Old ttl larger than new ttl must survive (max semantics, not new-wins)."""
    emb = stack.embed.embed("alpha rules")
    stack.vector.upsert_chunk(_with_rules(stack, "x_ttl", "alpha rules", [_RULE_A9]), emb.dense, emb.sparse)
    stack.vector.upsert_chunk(_with_rules(stack, "x_ttl", "alpha rules", [_RULE_A]), emb.dense, emb.sparse)
    got = stack.vector.get_chunk("x_ttl")
    assert got is not None
    assert got.rules == [{**_RULE_A, "ttl_turns": 9}]


def test_upsert_chunks_bulk_preserves_counters_and_merges_rules(stack) -> None:
    """Bulk upsert must reuse the per-row merge logic: counters survive and rules merge."""
    emb = stack.embed.embed("bulk rules")
    # seed one row with counters and a rule
    stack.vector.upsert_chunk(_with_rules(stack, "bulk-1", "bulk rules", [_RULE_A]), emb.dense, emb.sparse)
    stack.vector.update_chunk_state(["bulk-1"], hit_increment=2, needs_reconcile=True)
    # also set reinforce_count / last_hit_at via direct table update
    from mnemoseed_local.storage.drivers.lancedb_embedded import _escape as _esc

    # bump reinforce_count manually via raw update to simulate prior reinforce
    stack.vector._table.update(where=f"chunk_id = {_esc('bulk-1')}", values={"reinforce_count": 5})
    import time as _time

    before = _time.time()
    stack.vector._table.update(where=f"chunk_id = {_esc('bulk-1')}", values_sql={"last_hit_at": repr(before)})
    # bulk upsert with colliding id carries a different rule and should merge
    stamp_new = make_stamp("bulk-1", "bulk rules")
    stamp_new.rules = [_RULE_B]
    stamp_other = make_stamp("bulk-2", "bulk other")
    stamp_other.rules = [_RULE_PROF]
    emb2 = stack.embed.embed("bulk other")
    stack.vector.upsert_chunks(
        [
            (stamp_new, emb.dense, emb.sparse),
            (stamp_other, emb2.dense, emb2.sparse),
        ]
    )
    row = raw_chunk(stack, "bulk-1")
    assert int(row["hit_count"]) == 2
    assert int(row["reinforce_count"]) == 5
    assert row["needs_reconcile"] is True
    assert float(row["last_hit_at"]) >= before
    got = stack.vector.get_chunk("bulk-1")
    assert got is not None
    # union of old and new rules, max ttl for overlapping identity (none overlap here)
    import json as _json

    got_set = {_json.dumps(r, sort_keys=True) for r in got.rules}
    expected = {_json.dumps(_RULE_A, sort_keys=True), _json.dumps(_RULE_B, sort_keys=True)}
    assert got_set == expected
    # the other chunk was inserted
    assert stack.vector.get_chunk("bulk-2") is not None


def test_list_chunks_rules_not_null_on_pre_b27_table(tmp_path: Path) -> None:
    """Pre-B2.7 table without rules_json must degrade gracefully (empty, not crash)."""
    uri = tmp_path / "old.lance"
    db = connect(str(uri))
    probe = LanceDbEmbeddedStore(uri=tmp_path / "probe.lance", dimensions=DIMENSION)
    old_schema = probe._schema().remove(probe._schema().get_field_index("rules_json"))
    db.create_table("chunks", schema=old_schema)
    # reopen via the driver -> should migrate
    store = LanceDbEmbeddedStore(uri=uri, dimensions=DIMENSION)
    assert "rules_json" in store._table.schema.names
    # no rows, filter must not crash and return empty
    result = store.list_chunks(ChunkFilter(profile_id=PROFILE, rules_not_null=True), Page(limit=10))
    assert result.items == []
    assert result.total == 0


def test_origin_agent_column_migrates_and_legacy_rows_stay_null(tmp_path: Path) -> None:
    """Pre-attribution table gains origin_agent through the driver's add-columns
    migration; legacy rows keep NULL (no backfill — provenance is immutable),
    and only writes after the migration carry a label."""
    from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder

    uri = tmp_path / "old.lance"
    db = connect(str(uri))
    probe = LanceDbEmbeddedStore(uri=tmp_path / "probe.lance", dimensions=DIMENSION)
    embedder = SyntheticEmbedder(dimension=DIMENSION)
    old_schema = probe._schema().remove(probe._schema().get_field_index("origin_agent"))
    db.create_table("chunks", schema=old_schema)
    legacy = make_stamp("legacy-1", "pre-attribution turn")
    emb = embedder.embed(legacy.text)
    row = {
        key: value
        for key, value in probe._to_row(legacy, list(emb.dense), None).items()
        if key != "origin_agent"
    }
    db.open_table("chunks").add([row])

    store = LanceDbEmbeddedStore(uri=uri, dimensions=DIMENSION)
    assert "origin_agent" in store._table.schema.names

    got = store.get_chunk("legacy-1")
    assert got is not None
    assert got.origin_agent is None, "legacy rows stay NULL: no backfill"

    labeled = make_stamp("labeled-1", "attributed turn")
    labeled.origin_agent = "build"
    result = embedder.embed(labeled.text)
    store.upsert_chunk(labeled, result.dense, result.sparse)
    stored = store.get_chunk("labeled-1")
    assert stored is not None
    assert stored.origin_agent == "build"


# ---------------------------------------------------------------- B2 standing_rule contract


def test_upsert_chunk_persists_standing_rule(stack) -> None:
    """A standing_rule (kind + JSON-object value string) reads back after write."""
    emb = stack.embed.embed("standing directive pin")
    stack.vector.upsert_chunk(
        _with_rules(stack, "sr1", "standing directive pin", [_RULE_STANDING]), emb.dense, emb.sparse
    )
    got = stack.vector.get_chunk("sr1")
    assert got is not None
    assert got.rules == [_RULE_STANDING]
    assert got.rules[0]["value"] == _STANDING_VALUE_A


def test_upsert_chunk_merges_standing_rule_identical_identity_single(stack) -> None:
    """Byte-same standing_rule re-upsert on the same chunk -> one artifact (union-dedup)."""
    emb = stack.embed.embed("standing directive pin")
    stack.vector.upsert_chunk(
        _with_rules(stack, "sr2", "standing directive pin", [_RULE_STANDING]), emb.dense, emb.sparse
    )
    stack.vector.upsert_chunk(
        _with_rules(stack, "sr2", "standing directive pin", [_RULE_STANDING]), emb.dense, emb.sparse
    )
    got = stack.vector.get_chunk("sr2")
    assert got is not None
    assert len(got.rules) == 1
    assert got.rules[0]["value"] == _STANDING_VALUE_A


def test_upsert_chunk_merges_standing_rule_edited_value_new_identity(stack) -> None:
    """An edited standing_rule value yields a distinct identity (appended, not merged)."""
    edited = {**_RULE_STANDING, "value": _STANDING_VALUE_B}
    emb = stack.embed.embed("standing directive pin")
    stack.vector.upsert_chunk(
        _with_rules(stack, "sr3", "standing directive pin", [_RULE_STANDING]), emb.dense, emb.sparse
    )
    stack.vector.upsert_chunk(
        _with_rules(stack, "sr3", "standing directive pin", [edited]), emb.dense, emb.sparse
    )
    got = stack.vector.get_chunk("sr3")
    assert got is not None
    values = {r["value"] for r in got.rules if r["kind"] == "standing_rule"}
    assert values == {_STANDING_VALUE_A, _STANDING_VALUE_B}
