"""B3.1 T1 — matcher semantic revision (PRD-B3 addendum, first-run autopsy).

The v1 ruler (exact canonical predicate + full-phrase substring) judged
REAL GOOD extractions wrong: live models render predicates creatively
("plans to use", "runs before committing", "偏爱", "think") and rewrite
objects ("trunk-based development" -> "main branch development",
"i-always-run..." -> "full test suite"). The revision stays a deterministic
pure function with CURATED answer keys — synonym renderings the first live
run proved honest are enumerated in the corpus pools, and the matching logic
itself is auditable set arithmetic:

- predicate: class root-set hit (creative but honest renderings land in
  their fact class; verified against first-run autopsy triples)
- object: token-subset coverage (casefold, single/plural trim, stopword
  dropped) OR zh substring
- polarity: unchanged (exact)

Autopsy fixtures below are the REAL first-run outputs (rig stores,
2026-08-18T16-37Z matrix), one row per (predicate, object) -> expected
fact class or None for noise. These rows are the ruler's live bar.
"""

from __future__ import annotations

import pytest

from mnemoseed_local.eval.canary import canary_session, matches_fact

session = canary_session(20260818)


def _fact_for(predicate_class: str, en_item: str):
    """The factory's fact signature for a specific corpus item (phrasings ride
    the item pools directly, so the autopsy rows never depend on a seed's
    item draw)."""
    from mnemoseed_local.eval.canary import _FACT_ITEMS, CanaryFact

    item = next(i for i in _FACT_ITEMS[predicate_class] if i[0] == en_item)
    return CanaryFact("fx", predicate_class, "positive", (item[0], item[1], *item[2]))


def _match(predicate: str, obj: str, predicate_class: str, en_item: str) -> bool:
    return matches_fact(
        {"predicate": predicate, "object": obj, "polarity": "positive"},
        _fact_for(predicate_class, en_item),
    )


# ------------------------------------------------------------ autopsy positives (must now MATCH)
#
# (predicate, object) -> fact class, from the live first run's core tables
# (cells: qwen3_5_9b off, gemma4_e4b off/verify, qwen3_5_4b verify, qwen3_8b verify)

_AUTOPSY_POSITIVES: tuple[tuple[str, str, str, str], ...] = (
    ("plans to use", "main branch for development", "decided", "trunk-based development"),
    ("uses", "time-series database for logs", "decided", "a time-series database for logs"),
    ("switch", "time-series database", "decided", "a time-series database for logs"),
    ("switch_to", "time-series database for logs", "decided", "a time-series database for logs"),
    ("develop", "main trunk", "decided", "trunk-based development"),
    ("plan", "main branch development only", "decided", "trunk-based development"),
    ("plan", "develop on main branch", "decided", "trunk-based development"),
    ("打算", "主干开发", "decided", "trunk-based development"),
    ("prefers", "mechanical keyboard", "prefers", "mechanical keyboards"),
    ("prefer", "mechanical keyboards", "prefers", "mechanical keyboards"),
    ("prefer", "static type", "prefers", "static types in python"),
    ("prefers", "static types in Python", "prefers", "static types in python"),
    ("偏爱", "机械键盘", "prefers", "mechanical keyboards"),
    ("偏爱", "Python 里的静态类型", "prefers", "static types in python"),
    ("prefer", "Python 里的静态类型", "prefers", "static types in python"),
    ("runs before committing", "full test suite", "has_habit", "run the full test suite before committing"),
    ("run", "full test suite", "has_habit", "run the full test suite before committing"),
    ("writes first", "changelog entry", "has_habit", "write the changelog entry first"),
    ("write", "changelog entry", "has_habit", "write the changelog entry first"),
    (
        "execute",
        "full test suite before committing",
        "has_habit",
        "run the full test suite before committing",
    ),
    ("execute", "write changelog entry first", "has_habit", "write the changelog entry first"),
    (
        "believes",
        "small model with verification sufficient",
        "believes",
        "small models are enough with a verify pass",
    ),
    (
        "believe",
        "small model combination with verification",
        "believes",
        "small models are enough with a verify pass",
    ),
    (
        "believe",
        "small models with validation suffice",
        "believes",
        "small models are enough with a verify pass",
    ),
    (
        "believe",
        "small model with validation is sufficient",
        "believes",
        "small models are enough with a verify pass",
    ),
    ("think", "type safety pays for itself", "believes", "type safety pays for itself"),
    ("think", "type safety pays", "believes", "type safety pays for itself"),
)


@pytest.mark.parametrize(
    ("predicate", "obj", "predicate_class", "en_item"),
    _AUTOPSY_POSITIVES,
    ids=[f"{c}:{p}:{o}" for c, p, o, _ in _AUTOPSY_POSITIVES],
)
def test_autopsy_positives_match(predicate: str, obj: str, predicate_class: str, en_item: str) -> None:
    assert _match(predicate, obj, predicate_class, en_item) is True


# ------------------------------------------------------------ autopsy negatives (noise must NOT match)
#
# Real noise triples extracted by gemma4:e4b off on canary material:
# META syncs, mechanical filler, pleasantries, and the assistant assertion.

_AUTOPSY_NEGATIVES: tuple[tuple[str, str], ...] = (
    ("synchronize", "memory-daemon status"),
    ("synchronize_status", "memory-daemon"),
    ("synchronize_status", "retrieval pipeline"),
    ("同步", "memory-daemon的状态"),
    ("同步", "检索管线 status"),
    ("move on", "Sounds good"),
    ("先", "这样"),
    ("表达", "辛苦"),
    ("acknowledge", "work effort"),
    ("decide", "proceed"),
    ("decide", "stop current action"),
    ("has", "millions of active users"),
    ("配合", "校验"),
)


def test_autopsy_negatives_match_nothing() -> None:
    for predicate, obj in _AUTOPSY_NEGATIVES:
        for fact in session.facts:
            assert (
                matches_fact({"predicate": predicate, "object": obj, "polarity": "positive"}, fact) is False
            ), f"noise triple ({predicate!r}, {obj!r}) matched fact {fact.fact_id}"


# ------------------------------------------------------------ unit semantics


def test_singular_plural_object_trim() -> None:
    assert _match("prefers", "mechanical keyboards", "prefers", "mechanical keyboards") is True
    assert _match("prefers", "mechanical keyboard", "prefers", "mechanical keyboards") is True


def test_stopword_tail_tolerated() -> None:
    # the stub's "for good" tail on the decided class must not break coverage
    session33 = canary_session(33, facts=8, noise=0)
    fact = next(f for f in session33.facts if f.predicate == "decided")
    assert matches_fact(
        {"predicate": "decided", "object": f"{fact.phrasings[0]} for good", "polarity": "positive"},
        fact,
    )


def test_zh_object_substring() -> None:
    fact = _fact_for("decided", "pnpm for dependency management")
    assert (
        matches_fact(
            {"predicate": "决定", "object": f"用{fact.phrasings[1]}", "polarity": "positive"},
            fact,
        )
        is True
    )


def test_wrong_class_still_rejects() -> None:
    # same object, wrong predicate class -> no match (the class line holds)
    assert _match("eat", "pour-over coffee", "believes", "type safety pays for itself") is False


def test_polarity_still_exact() -> None:
    fact = _fact_for("prefers", "mechanical keyboards")
    assert (
        matches_fact(
            {
                "predicate": "prefers",
                "object": fact.phrasings[0],
                "polarity": "negative",
            },
            fact,
        )
        is False
    )


def test_unknown_predicate_rejects() -> None:
    assert _match("mangled-output-xyz", "mechanical keyboards", "prefers", "mechanical keyboards") is False
