"""PRD-448 W1 bridge calibration materials: the byte-pinned bilingual corpus,
probe set and candidate glossary seed table the recall-cue-bridge design is
calibrated against (brief §4.1, §6 W1).

The corpus mirrors the diagnosis Finding-1 family: one zh/en mixed execution
state fact competes against a cue-rich DECISION distractor, a past-fact rule
distractor and bilingual noise chunks (entity-miss and entity-collision
classes). The probes pin the natural zh/en wordings, the exact-token control,
the irrelevant controls and two no-glossary-hit regression probes verified
against the candidate seed table.

``glossary_hits`` and ``probe2_variants`` mirror the W2 read-side matcher
semantics (en terms by word boundary, zh terms by substring, first-occurrence
order, cap 8) for measurement only — the product matcher lands with W2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class BridgeChunkRole(StrEnum):
    """The role of one seeded corpus chunk in the calibration fixture."""

    TARGET_FACT = "target_fact"
    DECISION_DISTRACTOR = "decision_distractor"
    RULE_DISTRACTOR = "rule_distractor"
    NOISE_ENTITY_MISS = "noise_entity_miss"
    NOISE_ENTITY_COLLISION = "noise_entity_collision"


@dataclass(frozen=True)
class BridgeChunk:
    """One seeded corpus chunk (label, role, verbatim text)."""

    label: str
    role: BridgeChunkRole
    text: str


@dataclass(frozen=True)
class BridgeProbe:
    """One fixed calibration probe (label, verbatim text)."""

    label: str
    text: str


@dataclass(frozen=True)
class GlossaryHit:
    """One seed pair matched in raw query text, with its first occurrence."""

    en: str
    zh: str
    position: int


@dataclass(frozen=True)
class Probe2Variant:
    """One probe-2 construction D2 measures (append vs replace)."""

    variant: str  # "append:all" | "replace:all" | "append:<en term>" | ...
    kind: str  # "append" | "replace"
    text: str


# ---------------------------------------------------------------- corpus

TARGET_FACT_ZH = "codex 全量 import 后台跑 15/468,账本从 161 起,340 轮 rollout settle 完成"
DECISION_DISTRACTOR = "已决定把一部分 codex 的对话记录导入 mnemoseed 记忆,等后台跑完之后再验收"
RULE_DISTRACTOR = "之前定的规则是 codex 的对话记录导入 mnemoseed 记忆之后,账本里都要记一笔进度"

_NOISE_CHUNKS: tuple[BridgeChunk, ...] = (
    BridgeChunk(
        "noise_miss_zh_backup",
        BridgeChunkRole.NOISE_ENTITY_MISS,
        "泽尼斯数据库的夜间备份与归档作业上周全部完成",
    ),
    BridgeChunk(
        "noise_miss_en_backup",
        BridgeChunkRole.NOISE_ENTITY_MISS,
        "FalconDb focuses on nightly backups and archival jobs",
    ),
    BridgeChunk(
        "noise_miss_zh_pilot",
        BridgeChunkRole.NOISE_ENTITY_MISS,
        "水星项目的试点评审安排在下周四下午进行",
    ),
    BridgeChunk(
        "noise_miss_en_pilot",
        BridgeChunkRole.NOISE_ENTITY_MISS,
        "Mercury pilot review board meets on Thursday morning",
    ),
    BridgeChunk(
        "noise_collision_zh_upgrade",
        BridgeChunkRole.NOISE_ENTITY_COLLISION,
        "codex 客户端今天升级到了新版本,几个快捷键变了",
    ),
    BridgeChunk(
        "noise_collision_en_daemon",
        BridgeChunkRole.NOISE_ENTITY_COLLISION,
        "the mnemoseed daemon log shows steady resource usage this week",
    ),
    BridgeChunk(
        "noise_collision_zh_host",
        BridgeChunkRole.NOISE_ENTITY_COLLISION,
        "mnemoseed 的会话摘要功能最近又多支持了一种 host",
    ),
)


def bridge_corpus() -> tuple[BridgeChunk, ...]:
    """The seeded corpus: target fact, two distractors, bilingual noise."""
    return (
        BridgeChunk("target_fact", BridgeChunkRole.TARGET_FACT, TARGET_FACT_ZH),
        BridgeChunk("decision_distractor", BridgeChunkRole.DECISION_DISTRACTOR, DECISION_DISTRACTOR),
        BridgeChunk("rule_distractor", BridgeChunkRole.RULE_DISTRACTOR, RULE_DISTRACTOR),
        *_NOISE_CHUNKS,
    )


# ---------------------------------------------------------------- probes

PROBE_ZH_NATURAL = "之前有没有把一部分 codex 的对话记录导入 mnemoseed 记忆里"
PROBE_EN_NATURAL = "did we already import part of the codex chat history into mnemoseed?"
PROBE_ZH_EXACT = "全量 import 账本 15/468 340 rollout settle"
PROBE_IRRELEVANT_EN_ROME = "the history of rome"
PROBE_IRRELEVANT_EN_DISEASE = "progress of the disease"
PROBE_IRRELEVANT_ZH = "帮我把会议室的投影仪预约到明天下午"
PROBE_NO_GLOSSARY_ZH = "上次说的那个周五聚餐到底定在哪家店了"
PROBE_NO_GLOSSARY_EN = "did we ever fix where the team dinner should be on friday"


def bridge_probes() -> tuple[BridgeProbe, ...]:
    """The fixed probe set (byte-pinned; see the module constants)."""
    return (
        BridgeProbe("zh_natural", PROBE_ZH_NATURAL),
        BridgeProbe("en_natural", PROBE_EN_NATURAL),
        BridgeProbe("zh_exact", PROBE_ZH_EXACT),
        BridgeProbe("irrelevant_en_rome", PROBE_IRRELEVANT_EN_ROME),
        BridgeProbe("irrelevant_en_disease", PROBE_IRRELEVANT_EN_DISEASE),
        BridgeProbe("irrelevant_zh", PROBE_IRRELEVANT_ZH),
        BridgeProbe("no_glossary_zh", PROBE_NO_GLOSSARY_ZH),
        BridgeProbe("no_glossary_en", PROBE_NO_GLOSSARY_EN),
    )


# ---------------------------------------------------------------- glossary

#: The CANDIDATE glossary seed table D2 prunes (brief §2 M5: zh<->en canonical
#: pairs; W1 measures which pairs matter for the measured gap and pins the
#: no-glossary probes against exactly this table).
BRIDGE_GLOSSARY_SEEDS: tuple[tuple[str, str], ...] = (
    ("import", "导入"),
    ("ledger", "账本"),
    ("history", "历史"),
    ("memory", "记忆"),
    ("rollout", "发布"),
    ("chat", "对话"),
    ("record", "记录"),
    ("progress", "进度"),
    ("task", "任务"),
    ("settle", "结算"),
)

#: The W2 matcher's term cap, mirrored for measurement.
GLOSSARY_TERM_CAP = 8

_EN_WORD_PATTERNS: dict[str, re.Pattern[str]] = {}


def _en_word_pattern(term: str) -> re.Pattern[str]:
    pattern = _EN_WORD_PATTERNS.get(term)
    if pattern is None:
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        _EN_WORD_PATTERNS[term] = pattern
    return pattern


def _en_word_present(text: str, term: str) -> bool:
    return _en_word_pattern(term).search(text) is not None


def glossary_hits(text: str) -> tuple[GlossaryHit, ...]:
    """Match the candidate seed table against raw query text: en terms by
    word boundary, zh terms by substring; pairs surface in first-occurrence
    order, capped at the W2 matcher's term cap."""
    found: list[GlossaryHit] = []
    for en, zh in BRIDGE_GLOSSARY_SEEDS:
        positions: list[int] = []
        en_match = _en_word_pattern(en).search(text)
        if en_match is not None:
            positions.append(en_match.start())
        zh_at = text.find(zh)
        if zh_at >= 0:
            positions.append(zh_at)
        if positions:
            found.append(GlossaryHit(en=en, zh=zh, position=min(positions)))
    found.sort(key=lambda hit: (hit.position, hit.en))
    return tuple(found[:GLOSSARY_TERM_CAP])


def _replace_first(text: str, from_term: str, to_term: str) -> str:
    if any(ch.isascii() and ch.isalnum() for ch in from_term):
        return _en_word_pattern(from_term).sub(to_term, text, count=1)
    return text.replace(from_term, to_term, 1)


def probe2_variants(text: str, hits: tuple[GlossaryHit, ...]) -> tuple[Probe2Variant, ...]:
    """The probe-2 constructions D2 measures: append vs replace, all pairs at
    once and one pair at a time. Appending adds the side of the pair the text
    does not already carry; replacing swaps the matched occurrence for it. A
    pair present on both sides needs no bridge and is skipped."""
    bridgeable: list[tuple[GlossaryHit, str, str, str]] = []
    for hit in hits:
        en_present = _en_word_present(text, hit.en)
        zh_present = hit.zh in text
        if en_present and not zh_present:
            bridgeable.append((hit, hit.zh, hit.en, hit.zh))
        elif zh_present and not en_present:
            bridgeable.append((hit, hit.en, hit.zh, hit.en))
    variants: list[Probe2Variant] = []
    if bridgeable:
        append_all = text + " " + " ".join(term for _hit, term, _f, _t in bridgeable)
        variants.append(Probe2Variant("append:all", "append", append_all))
        replaced = text
        for _hit, _term, from_term, to_term in bridgeable:
            replaced = _replace_first(replaced, from_term, to_term)
        variants.append(Probe2Variant("replace:all", "replace", replaced))
    for hit, term, from_term, to_term in bridgeable:
        variants.append(Probe2Variant(f"append:{hit.en}", "append", f"{text} {term}"))
        replaced = _replace_first(text, from_term, to_term)
        variants.append(Probe2Variant(f"replace:{hit.en}", "replace", replaced))
    return tuple(variants)
