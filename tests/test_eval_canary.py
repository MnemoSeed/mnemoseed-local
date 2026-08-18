"""B3 T1 — canary factory: deterministic synthetic eval corpus (PRD-B3).

The canary session is the labeled-harness-free ground truth: the factory
generates established user-source facts (preference / decision / habit /
stance, EN+ZH) interleaved with four noise classes, and every fact carries
the deterministic signature a correct dream should land in the core graph.
Same seed => byte-identical corpus; the matcher is a pure function.
"""

from __future__ import annotations

import json
import re

import pytest

from mnemoseed_local.eval.canary import (
    CanaryTurn,
    NoiseKind,
    canary_session,
    canary_sessions,
    matches_fact,
)


def _turn_chunk(turn: CanaryTurn, chunk_id: str):
    """Minimal SnapshotChunk-shaped carrier for one canary turn's text."""
    from mnemoseed_local.dream.snapshot import SnapshotChunk
    from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Provenance

    stamp = ChunkStamp(
        chunk_id=chunk_id,
        profile_id="canary",
        text=f"{turn.role}: {turn.text}",
        cognitive_tier=CognitiveTier.TIER_2,
        model_id="canary-factory",
        provenance=Provenance(asserted_by=turn.role, session_id="canary-t1", source="canary"),
        turn_start=0,
        turn_end=0,
        ingested_at=0.0,
    )
    return SnapshotChunk.from_stamp(stamp)


_CJK_RE = re.compile(r"[一-鿿]")


def test_same_seed_byte_identical() -> None:
    a = canary_session(7)
    b = canary_session(7)
    assert a == b
    assert a.turns == b.turns
    assert a.facts == b.facts
    assert [t.text for t in a.turns] == [t.text for t in b.turns]


def test_different_seed_differs() -> None:
    a = canary_session(7)
    b = canary_session(8)
    assert [t.text for t in a.turns] != [t.text for t in b.turns]


def test_predicate_and_kind_coverage() -> None:
    session = canary_session(11, facts=8, noise=8)
    predicates = {f.predicate for f in session.facts}
    assert {"prefers", "has_habit", "decided", "believes"} <= predicates
    noise_kinds = {t.noise for t in session.turns if t.noise is not None}
    assert set(NoiseKind) <= noise_kinds


def test_bilingual_fact_coverage() -> None:
    session = canary_session(11, facts=8, noise=0)
    fact_turns = [t for t in session.turns if t.fact_id is not None]
    assert any(_CJK_RE.search(t.text) for t in fact_turns), "no ZH fact turn"
    assert any(not _CJK_RE.search(t.text) for t in fact_turns), "no EN fact turn"


def test_noise_roles() -> None:
    session = canary_session(11, facts=0, noise=8)
    for turn in session.turns:
        assert turn.noise is not None
        if turn.noise is NoiseKind.ASSERTION:
            assert turn.role == "assistant"
        else:
            assert turn.role == "user"


def test_fact_turn_self_consistent_with_phrasings() -> None:
    session = canary_session(11, facts=8, noise=0)
    facts_by_id = {f.fact_id: f for f in session.facts}
    for turn in session.turns:
        if turn.fact_id is None:
            continue
        fact = facts_by_id[turn.fact_id]
        folded = turn.text.casefold()
        assert any(p.casefold() in folded for p in fact.phrasings), (
            f"fact turn {turn.text!r} contains none of {fact.phrasings!r}"
        )


@pytest.mark.parametrize("seed", [11, 20260818])
def test_fact_turns_stub_extractable(seed: int) -> None:
    """Anti-drift guard: every canary fact turn must be extractable by the
    deterministic StubReflectLLM (the stub seat is the harness's own floor —
    a template even the stub can't parse silently deflates every recall
    number the matrix reports). Parametrized over a dev seed AND the pinned
    default-seed corpus the bar is measured against."""
    from mnemoseed_local.dream.prompts import render_chunk_block
    from mnemoseed_local.dream.reflect import StubReflectLLM
    from mnemoseed_local.eval.materials import DEFAULT_CANARY_SEED

    del DEFAULT_CANARY_SEED  # seed params carry both values; import documents the pin
    session = canary_session(seed, facts=8, noise=0)
    facts_by_id = {f.fact_id: f for f in session.facts}
    stub = StubReflectLLM()
    for index, turn in enumerate(session.turns):
        chunk = _turn_chunk(turn, f"ck{index:02d}")
        payload = json.loads(stub.chat(system="", user=render_chunk_block(chunk)))
        assert payload, f"stub extracted nothing from fact turn {turn.text!r}"
        fact = facts_by_id[turn.fact_id]  # type: ignore[index]
        assert any(
            matches_fact(
                {
                    "predicate": item["predicate"],
                    "object": item["object"],
                    "polarity": item.get("polarity", "positive"),
                },
                fact,
            )
            for item in payload
        ), f"stub extraction of {turn.text!r} missed fact {fact.fact_id}"


def test_fact_and_noise_markers_mutually_exclusive() -> None:
    session = canary_session(11, facts=8, noise=8)
    for turn in session.turns:
        assert not (turn.fact_id is not None and turn.noise is not None)
        assert turn.text.strip()


def test_fact_ids_unique_and_turns_marked() -> None:
    session = canary_session(11, facts=8, noise=8)
    fact_ids = [t.fact_id for t in session.turns if t.fact_id is not None]
    assert len(fact_ids) == len(set(fact_ids))
    assert len(fact_ids) == 8
    assert sum(1 for t in session.turns if t.noise is not None) == 8
    assert len(session.turns) == 16


def _props(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "subject": "user",
        "predicate": "prefers",
        "object": "pour-over coffee",
        "polarity": "positive",
    }
    base.update(overrides)
    return base


def test_matches_fact_positive() -> None:
    session = canary_session(11, facts=8, noise=0)
    fact = next(f for f in session.facts if f.phrasings)
    props = _props(predicate=fact.predicate, object=f"x {fact.phrasings[0]} y", polarity=fact.polarity)
    assert matches_fact(props, fact) is True


def test_matches_fact_casefold_hit() -> None:
    session = canary_session(11, facts=8, noise=0)
    fact = session.facts[0]
    props = _props(predicate=fact.predicate, object=fact.phrasings[0].upper(), polarity=fact.polarity)
    assert matches_fact(props, fact) is True


def test_matches_fact_negative_paths() -> None:
    session = canary_session(11, facts=8, noise=0)
    fact = session.facts[0]
    phrase = fact.phrasings[0]
    bad_predicate = _props(predicate="definitely-not", object=phrase, polarity=fact.polarity)
    assert matches_fact(bad_predicate, fact) is False
    flipped = "negative" if fact.polarity == "positive" else "positive"
    bad_polarity = _props(predicate=fact.predicate, object=phrase, polarity=flipped)
    assert matches_fact(bad_polarity, fact) is False
    no_phrase = _props(predicate=fact.predicate, object="unrelated thing", polarity=fact.polarity)
    assert matches_fact(no_phrase, fact) is False


def test_matches_fact_class_roots() -> None:
    session = canary_session(11, facts=8, noise=0)
    pref = next(f for f in session.facts if f.predicate == "prefers")
    # creative-but-honest renderings land in the prefers class (B3.1 roots)
    for rendering in ("prefer", "likes", "loves", "偏爱", "enjoys"):
        props = _props(predicate=rendering, object=pref.phrasings[0], polarity=pref.polarity)
        assert matches_fact(props, pref) is True, rendering
    # the canonical class word itself still matches
    props = _props(predicate="prefers", object=pref.phrasings[0], polarity=pref.polarity)
    assert matches_fact(props, pref) is True


def test_canary_sessions_plural_deterministic() -> None:
    batch_a = canary_sessions(3, sessions=2, facts_per_session=4, noise_per_session=3)
    batch_b = canary_sessions(3, sessions=2, facts_per_session=4, noise_per_session=3)
    assert batch_a == batch_b
    assert len(batch_a) == 2
    assert batch_a[0] != batch_a[1]
    assert all(len(s.turns) == 7 for s in batch_a)


@pytest.mark.parametrize("seed", range(2, 6))
def test_parametrized_seeds_stable(seed: int) -> None:
    assert canary_session(seed) == canary_session(seed)
