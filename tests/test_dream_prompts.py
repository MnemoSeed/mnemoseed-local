"""Dream reflection de-biasing prompt template (PRD-02 T3; FR-2.2, design/02 §5).

The prompt template is deterministic text (same snapshot -> same prompt),
parameterized by snapshot content only, and carries the de-biasing contract:
triple extraction, stripping of emotional/tone/persona artifacts, never-store
speaking-style, per-triple tier provenance, the Tier-3 anti-backflow rule, and
the FR-2.12 preference/evidence boundary (preference-type extraction only from
user-originated chunks).
"""

from __future__ import annotations

import json

from mnemoseed_local.dream import StubReflectLLM, build_reflect_prompt
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import TurnRange

_RANGE = TurnRange(0, 4)


def _stamp(
    chunk_id: str,
    text: str,
    *,
    tier: CognitiveTier = CognitiveTier.TIER_1,
    origin: str = "user",
    persona_id: str | None = None,
    turn_start: int | None = 0,
    turn_end: int | None = 1,
) -> ChunkStamp:
    asserted_by = "user" if origin == "user" else "anima-model"
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id="alice",
        text=text,
        cognitive_tier=tier,
        model_id="anima-model" if origin == "agent" else "test-model",
        persona_id=persona_id,
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by=asserted_by, session_id="s1", source="manual"),
        turn_start=turn_start,
        turn_end=turn_end,
    )


def _snap(*chunks: ChunkStamp) -> Snapshot:
    return Snapshot(
        snapshot_id="snap-p1",
        profile_id="alice",
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in chunks),
        created_at=1000.0,
        phases=frozenset({"snapshot_done"}),
    )


# ---------------------------------------------------------------- determinism


def test_prompt_is_deterministic_same_snapshot_same_text() -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    assert build_reflect_prompt(snap) == build_reflect_prompt(snap)


def test_prompt_order_is_chunk_deterministic_regardless_of_input_order() -> None:
    a = _stamp("c1", "I prefer dark mode", turn_start=2, turn_end=3)
    b = _stamp("c2", "I like coffee", turn_start=0, turn_end=1)
    forward = _snap(a, b)
    reversed_snap = _snap(b, a)  # same content, different tuple order
    assert build_reflect_prompt(forward).user == build_reflect_prompt(reversed_snap).user


# ---------------------------------------------------------------- de-biasing contract


def test_prompt_contains_debiasing_contract_rules() -> None:
    prompt = build_reflect_prompt(_snap(_stamp("c1", "hi")))
    system = prompt.system.casefold()
    assert "subject | predicate | object" in system
    assert "speak-style is never a fact" in system
    assert "never" in system and "main graph" in system
    assert "isolated" in system and "salvage" in system and "core" in system
    # anti-backflow AND the FR-2.12 preference-boundary are explicit instructions
    assert "origin=user" in system
    assert "preference" in system


def test_prompt_is_versioned() -> None:
    from mnemoseed_local.dream import PROMPT_VERSION

    assert PROMPT_VERSION == "v1"
    assert build_reflect_prompt(_snap(_stamp("c1", "x"))).version == PROMPT_VERSION


def test_prompt_is_frozen_value_and_renders_metadata() -> None:
    snap = _snap(
        _stamp("c1", "I prefer dark mode and vim", tier=CognitiveTier.TIER_3, turn_start=2, turn_end=5)
    )
    prompt = build_reflect_prompt(snap)
    assert prompt.user.index("chunk_id: c1") < prompt.user.index("I prefer dark mode")
    assert "tier: 3" in prompt.user
    assert "origin: user" in prompt.user
    assert "turn: 2-5" in prompt.user
    # parameterized by snapshot content only: the text itself is present verbatim
    assert "I prefer dark mode and vim" in prompt.user


def test_prompt_marks_agent_rendered_origin() -> None:
    snap = _snap(_stamp("c1", "I love coffee", origin="agent", persona_id="anima-1"))
    assert "origin: agent" in build_reflect_prompt(snap).user


# ---------------------------------------------------------------- prompt feeds the stub


def test_stub_chat_output_is_parseable_json_under_the_prompt_contract() -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    prompt = build_reflect_prompt(snap)
    text = StubReflectLLM().chat(system=prompt.system, user=prompt.user)
    payload = json.loads(text)
    assert isinstance(payload, list)
    assert payload  # the preference yields one mention
    assert payload[0]["predicate"] == "prefers"
    assert payload[0]["route"] == "core"


def test_reflect_prompt_round_trips_through_stub() -> None:
    """The deterministic block grammar the stub parses is exactly what the
    prompt renders: a grammar drift would break this round trip."""
    snap = _snap(
        _stamp("u1", "I prefer warm light", origin="user"),
        _stamp("a1", "I love warm light", origin="agent", persona_id="anima-1"),
        _stamp(
            "t3", "the answer is definitely B", tier=CognitiveTier.TIER_3, origin="agent", persona_id="meh"
        ),
    )
    prompt = build_reflect_prompt(snap)
    text = StubReflectLLM().chat(system=prompt.system, user=prompt.user)
    mentions = json.loads(text)
    # user-origin only for the preference; the agent preference is dropped,
    # and the tier-3 confident claim is an isolated assertion
    assert [m["subject"] for m in mentions].count("user") == 1
    assert any(m["predicate"] == "asserts" and m["route"] == "isolated" for m in mentions)
