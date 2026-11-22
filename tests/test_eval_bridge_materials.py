"""PRD-448 W1 bridge materials: the byte-pinned corpus, probe set and
candidate glossary seed table, plus the CueExtractor output facts the bridge
oracle derivation rests on (brief §1a / §4.1)."""

from __future__ import annotations

from mnemoseed_local.eval.bridge_materials import (
    BRIDGE_GLOSSARY_SEEDS,
    PROBE_EN_NATURAL,
    PROBE_IRRELEVANT_EN_DISEASE,
    PROBE_IRRELEVANT_EN_ROME,
    PROBE_IRRELEVANT_ZH,
    PROBE_NO_GLOSSARY_EN,
    PROBE_NO_GLOSSARY_ZH,
    PROBE_ZH_EXACT,
    PROBE_ZH_NATURAL,
    TARGET_FACT_ZH,
    bridge_corpus,
    bridge_probes,
    glossary_hits,
)
from mnemoseed_local.retrieve.cues import extract_cues


def test_corpus_shape_and_roles() -> None:
    corpus = bridge_corpus()
    roles = [chunk.role.value for chunk in corpus]
    assert roles.count("target_fact") == 1
    assert roles.count("decision_distractor") == 1
    assert roles.count("rule_distractor") == 1
    noise = [chunk for chunk in corpus if chunk.role.value.startswith("noise_")]
    assert 6 <= len(noise) <= 10
    assert {chunk.label for chunk in corpus} == {chunk.label for chunk in corpus}
    assert len({chunk.text for chunk in corpus}) == len(corpus)


def test_target_fact_is_the_brief_pinned_text() -> None:
    assert TARGET_FACT_ZH == "codex 全量 import 后台跑 15/468,账本从 161 起,340 轮 rollout settle 完成"


def test_probe_texts_are_byte_pinned() -> None:
    assert PROBE_ZH_NATURAL == "之前有没有把一部分 codex 的对话记录导入 mnemoseed 记忆里"
    assert PROBE_EN_NATURAL == "did we already import part of the codex chat history into mnemoseed?"
    assert PROBE_ZH_EXACT == "全量 import 账本 15/468 340 rollout settle"
    assert PROBE_IRRELEVANT_EN_ROME == "the history of rome"
    assert PROBE_IRRELEVANT_EN_DISEASE == "progress of the disease"


def test_probe_set_carries_the_regression_controls() -> None:
    labels = [probe.label for probe in bridge_probes()]
    for required in (
        "zh_natural",
        "en_natural",
        "zh_exact",
        "irrelevant_en_rome",
        "irrelevant_en_disease",
        "irrelevant_zh",
        "no_glossary_zh",
        "no_glossary_en",
    ):
        assert labels.count(required) == 1


def test_glossary_seed_table_shape() -> None:
    en_terms = [en for en, _zh in BRIDGE_GLOSSARY_SEEDS]
    zh_terms = [zh for _en, zh in BRIDGE_GLOSSARY_SEEDS]
    assert len(BRIDGE_GLOSSARY_SEEDS) >= 5
    assert all(term == term.lower() and term.isascii() for term in en_terms)
    assert all(term and any("\u4e00" <= ch <= "\u9fff" for ch in term) for term in zh_terms)
    assert len(set(en_terms)) == len(en_terms)
    assert len(set(zh_terms)) == len(zh_terms)


def test_no_glossary_probes_hit_nothing() -> None:
    assert glossary_hits(PROBE_NO_GLOSSARY_ZH) == ()
    assert glossary_hits(PROBE_NO_GLOSSARY_EN) == ()
    assert glossary_hits(PROBE_IRRELEVANT_ZH) == ()


def test_probe_glossary_hits_match_the_design_examples() -> None:
    assert [hit.en for hit in glossary_hits(PROBE_EN_NATURAL)] == ["import", "chat", "history"]
    assert [hit.en for hit in glossary_hits(PROBE_ZH_NATURAL)] == ["chat", "record", "import", "memory"]
    assert [hit.en for hit in glossary_hits(PROBE_ZH_EXACT)] == ["import", "ledger", "rollout", "settle"]
    assert [hit.en for hit in glossary_hits(PROBE_IRRELEVANT_EN_ROME)] == ["history"]
    assert [hit.en for hit in glossary_hits(PROBE_IRRELEVANT_EN_DISEASE)] == ["progress"]


def test_glossary_hits_carry_first_occurrence_positions() -> None:
    hits = glossary_hits(PROBE_ZH_NATURAL)
    positions = [hit.position for hit in hits]
    assert positions == sorted(positions)
    assert all(position >= 0 for position in positions)


def test_en_natural_probe_extracts_zero_entities() -> None:
    # the B-1 blocker fact (brief §1a/§2): the extractor never yields lowercase
    # ASCII words, so entity-driven mechanisms cannot reach the en probe
    assert extract_cues(PROBE_EN_NATURAL).cues.entities == []


def test_zh_natural_probe_extracts_glue_collapsed_cjk_runs() -> None:
    # brief §1a: the CJK runs collapse at glue characters and lose their heads
    entities = extract_cues(PROBE_ZH_NATURAL).cues.entities
    assert entities == ["前有没有", "话记录导入", "记忆里"]
    assert all(entity != "对话记录导入" for entity in entities)


def test_exact_probe_extracts_the_stored_cue_family() -> None:
    # brief §1a: the exact-token query shares its cue vocabulary with the
    # target chunk's stored cues (['全量', '账本'] family)
    entities = extract_cues(PROBE_ZH_EXACT).cues.entities
    assert "全量" in entities
    assert "账本" in entities
