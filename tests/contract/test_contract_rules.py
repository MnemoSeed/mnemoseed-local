"""B2.7 rules column contract: driver-level rules merge + rules_not_null filter.

The rules column is the Scheme 2-lite write path: rules ride the chunk
metadata (``rules_json``) and are merged on a re-upsert of the same chunk_id
(union by rule identity, ``ttl_turns`` takes the larger value). The merge is a
driver responsibility so the daemon's three remember branches share one
mechanism; usage counters survive a matched upsert (a re-remember must never
clobber them).
"""

from __future__ import annotations

from _support import PROFILE, make_stamp, raw_chunk

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
