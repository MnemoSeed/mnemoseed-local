"""Canary factory (B3 T1): deterministic synthetic eval corpus with ground truth.

A canary session is a labeled set of conversation turns whose correctness is
known BY CONSTRUCTION, so dream quality is measurable without manual labeling:

- fact turns are user-source established facts of four predicate classes
  (prefers / has_habit / decided / believes), templated in EN and ZH over
  fixed bilingual item pools;
- noise turns cover the four junk classes the B1 live record saw models
  over-extract: session META, MECHANICAL filler, PLEASANTRY, and assistant
  ASSERTION (confident-but-unverifiable tier-3-style claims — the only noise
  class spoken by the assistant);
- each fact carries its matching signature (predicate + alt-predicates +
  phrasing alternatives + polarity) so ``matches_fact`` is a pure function,
  never an LLM judgment.

Determinism is the contract: same seed -> the same session, byte-identical
(turn order included). A different seed produces a materially different
corpus. Language and class coverage are guaranteed by construction (deck
cycling), never left to luck.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

CANARY_VERSION = "v1"


class NoiseKind(StrEnum):
    """Junk classes a dream must never route into the core graph (B1 live list)."""

    META = "meta"  # session bookkeeping ("today we'll go over the deploy plan")
    MECHANICAL = "mechanical"  # filler acknowledgments ("ok, let's go with that")
    PLEASANTRY = "pleasantry"  # thanks / tone-only turns
    ASSERTION = "assertion"  # assistant confident-unverifiable claims (tier-3 shape)


@dataclass(frozen=True)
class CanaryFact:
    """One ground-truth fact plus the signature a correct extraction must show."""

    fact_id: str
    predicate: str  # fact class: prefers | has_habit | decided | believes
    polarity: str  # "positive" | "negative"
    phrasings: tuple[str, ...]  # object match = any alternative hit (see matches_fact)


@dataclass(frozen=True)
class CanaryTurn:
    """One corpus turn. Exactly one of ``fact_id`` / ``noise`` may be set."""

    text: str
    role: str  # "user" | "assistant" (maps to the ingest event type)
    fact_id: str | None = None
    noise: NoiseKind | None = None


@dataclass(frozen=True)
class CanarySession:
    """A labeled session: turns in conversation order + the fact ground truth."""

    session_id: str
    profile_id: str
    turns: tuple[CanaryTurn, ...]
    facts: tuple[CanaryFact, ...]

    @property
    def fact_turns(self) -> tuple[CanaryTurn, ...]:
        return tuple(t for t in self.turns if t.fact_id is not None)

    @property
    def noise_turns(self) -> tuple[CanaryTurn, ...]:
        return tuple(t for t in self.turns if t.noise is not None)


# ---------------------------------------------------------------- matcher
#
# B3.1 revision (first-run autopsy, 2026-08-18): live models render predicates
# creatively ("plans to use", "runs before committing", "偏爱", "think") and
# rewrite objects ("trunk-based development" -> "main branch"). The v1 ruler
# (exact predicate + full-phrase substring) killed honest extractions. The
# revision is STILL a deterministic pure function with curated answer keys:
#
# - predicate: class root-set membership (creative-but-honest renderings land
#   in their fact class);
# - object: EN token-subset coverage (casefold, bounded singular trim, stopwords
#   dropped) OR zh substring (whitespace-free) — synonyms live only in the
#   corpus pools as enumerated phrasing alternatives, never in the matcher;
# - polarity: exact, unchanged.

#: Fact-class predicate roots: canonical words + creative renderings that live
#: models actually produced in the first matrix run (PRD-B3 B3.1 table).
PREDICATE_CLASS_ROOTS: dict[str, frozenset[str]] = {
    "prefers": frozenset(
        {
            "prefers",
            "prefer",
            "preferred",
            "likes",
            "like",
            "loves",
            "love",
            "enjoys",
            "enjoy",
            "偏爱",
            "喜欢",
            "喜爱",
        }
    ),
    "has_habit": frozenset(
        {
            "has_habit",
            "habit",
            "runs",
            "run",
            "writes",
            "write",
            "reviews",
            "review",
            "rebases",
            "rebase",
            "executes",
            "execute",
            "always",
            "usually",
            "runs before committing",
            "writes first",
            "每次",
            "总是",
            "通常",
        }
    ),
    "decided": frozenset(
        {
            "decided",
            "decide",
            "decides",
            "plan",
            "plans",
            "plans to use",
            "uses",
            "use",
            "switch",
            "switches",
            "switch_to",
            "switched_to",
            "commits",
            "commit",
            "committed_to",
            "develop",
            "develops",
            "决定",
            "打算",
            "用",
            "切换",
        }
    ),
    "believes": frozenset(
        {
            "believes",
            "believe",
            "think",
            "thinks",
            "thought",
            "supports",
            "support",
            "认为",
            "相信",
            "坚持",
            "坚信",
        }
    ),
}

#: EN object-matching stopwords dropped from both sides before subset coverage.
_OBJECT_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "the", "for", "to", "of", "in", "on", "with", "from", "and", "by"}
)

_CJK_RE = re.compile(r"[一-鿿]")


def _class_of(predicate: str) -> str | None:
    """Normalize a rendered predicate into its fact class (None when garbage)."""
    normalized = predicate.strip().casefold()
    for fact_class, roots in PREDICATE_CLASS_ROOTS.items():
        if normalized in roots:
            return fact_class
    return None


def _trim_token(token: str) -> str:
    """Bounded singular trim: drop one trailing 's' (guarded: length > 3 and
    no 'ss' tail, so 'class'/'pass'/'stress' stay whole)."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _object_tokens(text: str) -> frozenset[str]:
    tokens = frozenset(re.split(r"[^0-9a-z]+", text.casefold()))
    return frozenset(_trim_token(t) for t in tokens if t and t not in _OBJECT_STOPWORDS)


def _phrase_hit(phrase: str, obj: str) -> bool:
    """One phrasing alternative vs one node object: zh substring on whitespace-
    free text; otherwise EN token-subset coverage (every phrase token present
    in the object's trimmed token set)."""
    if _CJK_RE.search(phrase):
        compact_phrase = re.sub(r"\s+", "", phrase.casefold())
        compact_obj = re.sub(r"\s+", "", obj.casefold())
        return compact_phrase in compact_obj
    phrase_tokens = _object_tokens(phrase)
    if not phrase_tokens:
        return False
    return phrase_tokens <= _object_tokens(obj)


def matches_fact(props: Mapping[str, Any], fact: CanaryFact) -> bool:
    """Pure-function match: does one core node's prop triple evidence ``fact``?

    Match = the node's predicate lands in the fact's CLASS ROOT SET (exact
    casefold membership, never fuzzy) AND polarity equal AND any phrasing
    alternative hits (zh substring or EN token-subset coverage).
    """
    if _class_of(str(props.get("predicate", ""))) != fact.predicate:
        return False
    polarity = str(props.get("polarity", "positive")).strip().casefold()
    if polarity != fact.polarity.casefold():
        return False
    obj = str(props.get("object", ""))
    return any(_phrase_hit(phrase, obj) for phrase in fact.phrasings)


# ---------------------------------------------------------------- corpus pools
#
# Each fact class carries bilingual ITEM triples ((en object, zh object,
# curated extra accepted renderings)); the first two strings are guaranteed
# embedded in the generated turn text (self-consistency invariant), the extras
# are KNOWN-HONEST rewrites observed in live matrix runs — the curated answer
# keys (PRD-B3 B3.1). Adding a rendering requires a live observation + a PRD
# line; the matcher itself never gets fuzzier.

_PREF_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pour-over coffee", "手冲咖啡", ()),
    ("mechanical keyboards", "机械键盘", ()),
    ("static types in python", "Python 里的静态类型", ("static type", "static types")),
    ("dark mode", "深色模式", ()),
)
_HABIT_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("run the full test suite before committing", "提交前跑完整测试套件", ("full test suite",)),
    ("write the changelog entry first", "先写变更日志再动手", ("changelog entry",)),
    ("review the diff line by line", "逐行审查 diff", ("diff review", "review the diff")),
    ("rebase before opening the PR", "开 PR 前 rebase", ()),
)
_DECIDE_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pnpm for dependency management", "pnpm 管理依赖", ("pnpm",)),
    ("trunk-based development", "主干开发", ("main branch", "main trunk", "trunk")),
    ("a time-series database for logs", "时序数据库来存日志", ("time-series database", "时序数据库")),
    ("uv for python packaging", "uv 管理 Python 包", ()),
)
_BELIEVE_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "small models are enough with a verify pass",
        "小模型配合校验就够用",
        (
            "small model with verification sufficient",
            "small model combination with verification",
            "small models with validation suffice",
            "small models are enough with verification",
            "small model with validation is sufficient",
        ),
    ),
    ("type safety pays for itself", "类型安全值回成本", ("type safety pays", "type safety")),
    ("offline-first beats cloud lock-in", "离线优先胜过云锁定", ()),
    ("verbatim memory is the last line of defense", "原文记忆是最后的防线", ()),
)

#: (en template, zh template) pairs per predicate class; ``{x}`` is the item.
_TEMPLATES: dict[str, tuple[tuple[str, str], ...]] = {
    "prefers": (
        ("I really love {x}.", "我真的很喜欢{x}。"),
        ("I prefer {x} over all the alternatives.", "我偏爱{x}。"),
    ),
    "has_habit": (
        ("I always {x}.", "我每次都{x}。"),
        ("I usually {x}.", "我通常会{x}。"),
    ),
    "decided": (
        ("I've decided to use {x} from now on.", "我决定以后都用{x}。"),
        ("I've switched to {x} for good.", "我打算以后就{x}。"),
    ),
    "believes": (
        ("I believe {x}.", "我认为{x}。"),
        ("I firmly think {x}.", "我相信{x}。"),
    ),
}

_FACT_ITEMS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "prefers": _PREF_ITEMS,
    "has_habit": _HABIT_ITEMS,
    "decided": _DECIDE_ITEMS,
    "believes": _BELIEVE_ITEMS,
}

_FACT_CLASSES: tuple[str, ...] = ("prefers", "has_habit", "decided", "believes")

_PROJECTS: tuple[str, ...] = ("memory-daemon", "eval-harness", "dream 引擎", "检索管线")
_COMPANIES: tuple[str, ...] = ("Globex", "Initech", "Hooli", "Umbrella Corp", "蓝山科技", "北辰半导体")

_META_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("Let's use this session to go over the {p} plan.", "今天这个 session 主要对一下{p}的进度。"),
    ("Quick sync on {p} status before we start.", "开始前先同步一下{p}的状态。"),
)
_MECHANICAL_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("OK, let's go with that for now.", "好的，那就先这样。"),
    ("Sounds good, moving on.", "行，继续。"),
)
_PLEASANTRY_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("Thanks, that's really helpful!", "谢谢，帮大忙了！"),
    ("Nice, appreciate it.", "太好了，辛苦！"),
)
_ASSERTION_TEMPLATES: tuple[tuple[str, str], ...] = (
    (
        "As far as I know, {c} definitely has millions of active users.",
        "{c}绝对是行业领头羊，用户量肯定过亿。",
    ),
    ("Everyone knows {c} certainly dominates its market.", "众所周知，{c}肯定垄断了它的市场。"),
)

_NOISE_CLASSES: tuple[NoiseKind, ...] = (
    NoiseKind.META,
    NoiseKind.MECHANICAL,
    NoiseKind.PLEASANTRY,
    NoiseKind.ASSERTION,
)


def _balanced_languages(count: int, rng: random.Random) -> list[int]:
    """A language deck (0=EN, 1=ZH) with both languages present for count>=2,
    shuffled by the session rng — coverage by construction, never by luck."""
    deck = [i % 2 for i in range(count)]
    rng.shuffle(deck)
    return deck


def _build_fact_turns(
    count: int,
    session_id: str,
    rng: random.Random,
) -> tuple[list[CanaryTurn], list[CanaryFact]]:
    turns: list[CanaryTurn] = []
    facts: list[CanaryFact] = []
    languages = _balanced_languages(count, rng)
    for index in range(count):
        # Class cycling guarantees predicate coverage for count >= 4.
        predicate = _FACT_CLASSES[index % len(_FACT_CLASSES)]
        item = rng.choice(_FACT_ITEMS[predicate])
        template = rng.choice(_TEMPLATES[predicate])
        lang = languages[index]
        text = template[lang].format(x=item[lang])
        fact_id = f"{session_id}-F{index:02d}"
        turns.append(CanaryTurn(text=text, role="user", fact_id=fact_id))
        facts.append(
            CanaryFact(
                fact_id=fact_id,
                predicate=predicate,
                polarity="positive",
                # the item's (en, zh) strings + its curated rewrite answer keys
                phrasings=(item[0], item[1], *item[2]),
            )
        )
    return turns, facts


def _build_noise_turns(count: int, rng: random.Random) -> list[CanaryTurn]:
    turns: list[CanaryTurn] = []
    languages = _balanced_languages(count, rng)
    for index in range(count):
        kind = _NOISE_CLASSES[index % len(_NOISE_CLASSES)]
        lang = languages[index]
        if kind is NoiseKind.META:
            text = rng.choice(_META_TEMPLATES)[lang].format(p=rng.choice(_PROJECTS))
        elif kind is NoiseKind.MECHANICAL:
            text = rng.choice(_MECHANICAL_TEMPLATES)[lang]
        elif kind is NoiseKind.PLEASANTRY:
            text = rng.choice(_PLEASANTRY_TEMPLATES)[lang]
        else:
            text = rng.choice(_ASSERTION_TEMPLATES)[lang].format(c=rng.choice(_COMPANIES))
        # Assertions are assistant-spoken (tier-3 shape); every other noise
        # class is user chatter the dream must not crystallize.
        role = "assistant" if kind is NoiseKind.ASSERTION else "user"
        turns.append(CanaryTurn(text=text, role=role, noise=kind))
    return turns


def canary_session(
    seed: int,
    *,
    facts: int = 8,
    noise: int = 6,
    session_id: str = "canary",
    profile_id: str = "canary",
) -> CanarySession:
    """Generate one deterministic labeled session from ``seed``.

    Class coverage is guaranteed for facts >= 4 and noise >= 4 by deck
    cycling; language coverage is guaranteed for counts >= 2. Fact and noise
    turns are interleaved with the session rng, so the same seed reproduces
    the corpus byte-identically and a different seed materially differs.
    """
    rng = random.Random(seed)
    fact_turns, fact_truth = _build_fact_turns(facts, session_id, rng)
    noise_turns = _build_noise_turns(noise, rng)
    deck = [*fact_turns, *noise_turns]
    rng.shuffle(deck)
    return CanarySession(
        session_id=session_id,
        profile_id=profile_id,
        turns=tuple(deck),
        facts=tuple(fact_truth),
    )


def canary_sessions(
    seed: int,
    *,
    sessions: int = 1,
    facts_per_session: int = 8,
    noise_per_session: int = 6,
    profile_id: str = "canary",
) -> tuple[CanarySession, ...]:
    """A deterministic batch of canary sessions (distinct sub-seeds and ids)."""
    return tuple(
        canary_session(
            seed * 1000 + index,
            facts=facts_per_session,
            noise=noise_per_session,
            session_id=f"canary-{index:02d}",
            profile_id=profile_id,
        )
        for index in range(sessions)
    )
