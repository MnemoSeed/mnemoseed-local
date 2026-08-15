"""v1 seed affective lexicon: EN + ZH arousal/valence entries (FR-1.4).

The emotion axes follow the NRC VAD shape (valence x arousal, Russell's
circumplex), but v1 ships a hand-curated seed rather than a downloaded
resource: NRC VAD itself is form-gated and cannot be auto-fetched, so this
module bundles a compact, high-confidence seed covering everyday emotion words
plus developer-domain terms. It is explicitly v1 DATA with a loading seam
(``Lexicon``) so a calibrated resource can drop in later; the FR-1.4 benchmark
will measure whether the seed is sufficient.

``Lexicon`` normalizes an arbitrary entry collection into a lookup plus a
longest-match scanner (CJK strings carry no word boundaries, so ASCII terms
match on word boundaries and CJK terms on longest substring), and validates
ranges so a replacement resource cannot silently poison the scorer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass


def _is_ascii(term: str) -> bool:
    return all(ord(ch) < 128 for ch in term)


def _ascii_spans(text: str, term: str) -> list[tuple[int, int]]:
    # ASCII word boundaries only: Python's `\b` treats CJK ideographs as word
    # letters, which would hide an English term glued to CJK ("这个bug"). The
    # ASCII-alnum lookarounds keep the standalone-word semantics while letting a
    # term match directly against CJK on either side.
    pattern = re.compile(rf"(?i)(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])")
    return [(m.start(), m.end()) for m in pattern.finditer(text)]


def _cjk_spans(text: str, term: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        found = text.find(term, start)
        if found < 0:
            return spans
        spans.append((found, found + len(term)))
        start = found + len(term)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


@dataclass(frozen=True)
class AffectiveEntry:
    """One lexicon word with NRC-style arousal (0..1) and valence (-1..1)."""

    term: str
    arousal: float
    valence: float


# ---------------------------------------------------------------- data

EN_LEXICON_V1: tuple[AffectiveEntry, ...] = (
    # ---- positive, high arousal ----
    AffectiveEntry("amazing", 0.9, 0.9),
    AffectiveEntry("awesome", 0.85, 0.85),
    AffectiveEntry("fantastic", 0.9, 0.85),
    AffectiveEntry("incredible", 0.9, 0.85),
    AffectiveEntry("brilliant", 0.85, 0.85),
    AffectiveEntry("wonderful", 0.9, 0.9),
    AffectiveEntry("excellent", 0.8, 0.85),
    AffectiveEntry("thrilling", 0.9, 0.8),
    AffectiveEntry("exciting", 0.85, 0.7),
    AffectiveEntry("excited", 0.9, 0.75),
    AffectiveEntry("love", 0.9, 0.9),
    AffectiveEntry("adore", 0.9, 0.85),
    AffectiveEntry("joy", 0.8, 0.85),
    AffectiveEntry("joyful", 0.65, 0.8),
    AffectiveEntry("happy", 0.75, 0.85),
    AffectiveEntry("glad", 0.6, 0.75),
    AffectiveEntry("delighted", 0.75, 0.85),
    AffectiveEntry("thrilled", 0.85, 0.85),
    AffectiveEntry("euphoric", 0.9, 0.85),
    AffectiveEntry("ecstatic", 0.95, 0.9),
    AffectiveEntry("proud", 0.6, 0.75),
    AffectiveEntry("celebrate", 0.7, 0.75),
    AffectiveEntry("celebration", 0.7, 0.75),
    AffectiveEntry("win", 0.65, 0.6),
    AffectiveEntry("winner", 0.65, 0.7),
    AffectiveEntry("victory", 0.8, 0.75),
    AffectiveEntry("success", 0.6, 0.7),
    AffectiveEntry("successful", 0.6, 0.7),
    AffectiveEntry("triumph", 0.8, 0.75),
    AffectiveEntry("breakthrough", 0.8, 0.75),
    AffectiveEntry("solved", 0.6, 0.6),
    AffectiveEntry("solution", 0.5, 0.55),
    AffectiveEntry("enjoy", 0.55, 0.7),
    AffectiveEntry("fascinating", 0.7, 0.7),
    AffectiveEntry("cheerful", 0.7, 0.8),
    AffectiveEntry("optimistic", 0.7, 0.75),
    AffectiveEntry("hopeful", 0.6, 0.65),
    AffectiveEntry("grateful", 0.6, 0.7),
    AffectiveEntry("thankful", 0.55, 0.65),
    AffectiveEntry("fun", 0.65, 0.7),
    AffectiveEntry("hilarious", 0.8, 0.75),
    AffectiveEntry("outstanding", 0.85, 0.85),
    AffectiveEntry("remarkable", 0.75, 0.8),
    AffectiveEntry("impressive", 0.7, 0.75),
    AffectiveEntry("achievement", 0.65, 0.7),
    AffectiveEntry("milestone", 0.7, 0.7),
    AffectiveEntry("relief", 0.5, 0.6),
    AffectiveEntry("merry", 0.7, 0.8),
    AffectiveEntry("lovely", 0.7, 0.85),
    AffectiveEntry("adorable", 0.85, 0.85),
    AffectiveEntry("bliss", 0.8, 0.9),
    AffectiveEntry("splendid", 0.8, 0.85),
    AffectiveEntry("superb", 0.85, 0.85),
    AffectiveEntry("elegant", 0.6, 0.7),
    AffectiveEntry("polished", 0.6, 0.7),
    AffectiveEntry("streamlined", 0.5, 0.6),
    AffectiveEntry("intuitive", 0.5, 0.6),
    AffectiveEntry("neat", 0.5, 0.6),
    AffectiveEntry("tidy", 0.35, 0.55),
    AffectiveEntry("organized", 0.4, 0.5),
    AffectiveEntry("robust", 0.4, 0.5),
    AffectiveEntry("solid", 0.4, 0.5),
    AffectiveEntry("consistent", 0.4, 0.5),
    AffectiveEntry("dependable", 0.45, 0.55),
    # ---- positive, low arousal ----
    AffectiveEntry("calm", 0.2, 0.55),
    AffectiveEntry("peaceful", 0.25, 0.65),
    AffectiveEntry("relaxed", 0.25, 0.6),
    AffectiveEntry("relaxing", 0.3, 0.65),
    AffectiveEntry("serene", 0.3, 0.65),
    AffectiveEntry("content", 0.3, 0.55),
    AffectiveEntry("contented", 0.3, 0.6),
    AffectiveEntry("satisfied", 0.35, 0.6),
    AffectiveEntry("pleasant", 0.35, 0.6),
    AffectiveEntry("gentle", 0.3, 0.55),
    AffectiveEntry("cozy", 0.4, 0.7),
    AffectiveEntry("comfortable", 0.3, 0.6),
    AffectiveEntry("fine", 0.3, 0.45),
    AffectiveEntry("okay", 0.3, 0.4),
    AffectiveEntry("stable", 0.3, 0.5),
    # ---- negative, high arousal ----
    AffectiveEntry("awful", 0.8, -0.8),
    AffectiveEntry("terrible", 0.8, -0.8),
    AffectiveEntry("horrible", 0.85, -0.85),
    AffectiveEntry("horrendous", 0.9, -0.85),
    AffectiveEntry("horrific", 0.9, -0.85),
    AffectiveEntry("disgusting", 0.8, -0.85),
    AffectiveEntry("nauseating", 0.75, -0.8),
    AffectiveEntry("hate", 0.85, -0.85),
    AffectiveEntry("despise", 0.8, -0.8),
    AffectiveEntry("loathe", 0.85, -0.85),
    AffectiveEntry("furious", 0.95, -0.9),
    AffectiveEntry("enraged", 0.95, -0.9),
    AffectiveEntry("angry", 0.9, -0.8),
    AffectiveEntry("anger", 0.85, -0.8),
    AffectiveEntry("infuriated", 0.9, -0.85),
    AffectiveEntry("mad", 0.8, -0.7),
    AffectiveEntry("rage", 0.9, -0.85),
    AffectiveEntry("panic", 0.95, -0.85),
    AffectiveEntry("panicked", 0.9, -0.8),
    AffectiveEntry("terrified", 0.95, -0.9),
    AffectiveEntry("afraid", 0.8, -0.7),
    AffectiveEntry("fear", 0.8, -0.7),
    AffectiveEntry("scared", 0.85, -0.75),
    AffectiveEntry("frightened", 0.8, -0.7),
    AffectiveEntry("horrified", 0.9, -0.85),
    AffectiveEntry("anxious", 0.75, -0.6),
    AffectiveEntry("anxiety", 0.75, -0.6),
    AffectiveEntry("nervous", 0.7, -0.5),
    AffectiveEntry("stressed", 0.7, -0.55),
    AffectiveEntry("stress", 0.7, -0.55),
    AffectiveEntry("overwhelmed", 0.75, -0.6),
    AffectiveEntry("frustrated", 0.7, -0.6),
    AffectiveEntry("frustration", 0.7, -0.6),
    AffectiveEntry("frustrating", 0.7, -0.6),
    AffectiveEntry("annoying", 0.6, -0.5),
    AffectiveEntry("annoyed", 0.6, -0.5),
    AffectiveEntry("irritating", 0.6, -0.5),
    AffectiveEntry("irritated", 0.6, -0.5),
    AffectiveEntry("infuriating", 0.85, -0.8),
    AffectiveEntry("disappointed", 0.6, -0.6),
    AffectiveEntry("disappointing", 0.6, -0.6),
    AffectiveEntry("upset", 0.7, -0.6),
    AffectiveEntry("distressed", 0.75, -0.65),
    AffectiveEntry("devastating", 0.9, -0.85),
    AffectiveEntry("devastated", 0.85, -0.8),
    AffectiveEntry("desperate", 0.9, -0.85),
    AffectiveEntry("hopeless", 0.7, -0.75),
    AffectiveEntry("depressed", 0.8, -0.8),
    AffectiveEntry("depression", 0.8, -0.8),
    AffectiveEntry("miserable", 0.8, -0.8),
    AffectiveEntry("suffering", 0.7, -0.7),
    AffectiveEntry("pain", 0.7, -0.7),
    AffectiveEntry("painful", 0.7, -0.7),
    AffectiveEntry("agonizing", 0.85, -0.8),
    AffectiveEntry("crisis", 0.85, -0.8),
    AffectiveEntry("emergency", 0.8, -0.7),
    AffectiveEntry("danger", 0.85, -0.75),
    AffectiveEntry("dangerous", 0.8, -0.7),
    AffectiveEntry("threat", 0.8, -0.7),
    AffectiveEntry("disaster", 0.9, -0.8),
    AffectiveEntry("catastrophic", 0.9, -0.85),
    AffectiveEntry("tragic", 0.85, -0.8),
    AffectiveEntry("grief", 0.8, -0.8),
    AffectiveEntry("mourn", 0.8, -0.8),
    AffectiveEntry("death", 0.75, -0.8),
    AffectiveEntry("dying", 0.8, -0.8),
    AffectiveEntry("crash", 0.85, -0.8),
    AffectiveEntry("crashed", 0.85, -0.8),
    AffectiveEntry("broken", 0.7, -0.7),
    AffectiveEntry("fail", 0.7, -0.7),
    AffectiveEntry("failed", 0.7, -0.7),
    AffectiveEntry("failure", 0.7, -0.7),
    AffectiveEntry("error", 0.6, -0.55),
    AffectiveEntry("bug", 0.5, -0.5),
    AffectiveEntry("sucks", 0.7, -0.65),
    AffectiveEntry("ugh", 0.5, -0.5),
    AffectiveEntry("unbearable", 0.8, -0.75),
    AffectiveEntry("intolerable", 0.8, -0.7),
    AffectiveEntry("excruciating", 0.9, -0.85),
    AffectiveEntry("torment", 0.8, -0.8),
    AffectiveEntry("nightmare", 0.85, -0.8),
    AffectiveEntry("hellish", 0.85, -0.85),
    AffectiveEntry("chaos", 0.8, -0.7),
    AffectiveEntry("mayhem", 0.85, -0.75),
    AffectiveEntry("turmoil", 0.8, -0.7),
    AffectiveEntry("anguished", 0.85, -0.8),
    AffectiveEntry("sorrow", 0.7, -0.7),
    AffectiveEntry("grumpy", 0.6, -0.5),
    AffectiveEntry("cranky", 0.6, -0.45),
    AffectiveEntry("whiny", 0.5, -0.5),
    AffectiveEntry("complaining", 0.55, -0.5),
    AffectiveEntry("gripe", 0.55, -0.5),
    AffectiveEntry("sigh", 0.3, -0.3),
    AffectiveEntry("cried", 0.75, -0.7),
    AffectiveEntry("screaming", 0.85, -0.7),
    AffectiveEntry("violent", 0.9, -0.85),
    AffectiveEntry("nasty", 0.7, -0.7),
    AffectiveEntry("cruel", 0.8, -0.8),
    AffectiveEntry("unfair", 0.7, -0.65),
    AffectiveEntry("rejected", 0.7, -0.7),
    AffectiveEntry("lonely", 0.6, -0.6),
    AffectiveEntry("isolated", 0.55, -0.55),
    AffectiveEntry("abandoned", 0.75, -0.75),
    AffectiveEntry("helpless", 0.75, -0.75),
    AffectiveEntry("trapped", 0.8, -0.75),
    # ---- negative, low arousal ----
    AffectiveEntry("boring", 0.3, -0.45),
    AffectiveEntry("bored", 0.3, -0.4),
    AffectiveEntry("tired", 0.35, -0.4),
    AffectiveEntry("sleepy", 0.3, -0.35),
    AffectiveEntry("exhausted", 0.55, -0.55),
    AffectiveEntry("weary", 0.35, -0.45),
    AffectiveEntry("dull", 0.3, -0.45),
    AffectiveEntry("tedious", 0.3, -0.5),
    AffectiveEntry("mundane", 0.3, -0.4),
    AffectiveEntry("monotonous", 0.3, -0.45),
    AffectiveEntry("meh", 0.25, -0.35),
    AffectiveEntry("sluggish", 0.3, -0.4),
    AffectiveEntry("weak", 0.3, -0.45),
    AffectiveEntry("numb", 0.3, -0.45),
    AffectiveEntry("gloomy", 0.35, -0.5),
    AffectiveEntry("dreary", 0.3, -0.5),
    AffectiveEntry("bleak", 0.4, -0.55),
    AffectiveEntry("draining", 0.45, -0.5),
    AffectiveEntry("hassle", 0.5, -0.5),
    AffectiveEntry("burden", 0.45, -0.5),
    # ---- developer domain ----
    AffectiveEntry("debug", 0.5, -0.3),
    AffectiveEntry("debugging", 0.5, -0.3),
    AffectiveEntry("compile", 0.4, -0.25),
    AffectiveEntry("building", 0.4, -0.3),
    AffectiveEntry("deploy", 0.5, 0.0),
    AffectiveEntry("release", 0.5, 0.55),
    AffectiveEntry("shipped", 0.6, 0.7),
    AffectiveEntry("shipping", 0.5, 0.6),
    AffectiveEntry("launch", 0.7, 0.7),
    AffectiveEntry("launching", 0.65, 0.6),
    AffectiveEntry("production", 0.5, -0.3),
    AffectiveEntry("outage", 0.8, -0.7),
    AffectiveEntry("deadline", 0.6, -0.5),
    AffectiveEntry("crunch", 0.6, -0.45),
    AffectiveEntry("refactor", 0.45, 0.45),
    AffectiveEntry("refactoring", 0.45, 0.45),
    AffectiveEntry("review", 0.4, 0.45),
    AffectiveEntry("reviews", 0.4, 0.45),
    AffectiveEntry("merge", 0.4, -0.2),
    AffectiveEntry("merged", 0.45, 0.3),
    AffectiveEntry("conflict", 0.6, -0.5),
    AffectiveEntry("segfault", 0.8, -0.7),
    AffectiveEntry("deadlock", 0.8, -0.7),
    AffectiveEntry("hung", 0.6, -0.5),
    AffectiveEntry("stuck", 0.55, -0.45),
    AffectiveEntry("blocked", 0.55, -0.5),
    AffectiveEntry("flaky", 0.6, -0.4),
    AffectiveEntry("flake", 0.6, -0.4),
    AffectiveEntry("regression", 0.6, -0.5),
    AffectiveEntry("repro", 0.6, -0.3),
    AffectiveEntry("reproducible", 0.5, -0.3),
    AffectiveEntry("stackoverflow", 0.65, -0.5),
    AffectiveEntry("stacktrace", 0.55, -0.45),
    AffectiveEntry("traceback", 0.55, -0.45),
    AffectiveEntry("unhandled", 0.65, -0.55),
    AffectiveEntry("timeout", 0.6, -0.5),
    AffectiveEntry("throttled", 0.55, -0.4),
    AffectiveEntry("latency", 0.5, -0.45),
    AffectiveEntry("bottleneck", 0.55, -0.5),
    AffectiveEntry("overload", 0.65, -0.55),
    AffectiveEntry("unstable", 0.55, -0.5),
    AffectiveEntry("corrupt", 0.7, -0.65),
    AffectiveEntry("corrupted", 0.7, -0.65),
    AffectiveEntry("migration", 0.4, -0.25),
    AffectiveEntry("rollback", 0.5, -0.35),
    AffectiveEntry("exception", 0.6, -0.5),
    AffectiveEntry("warning", 0.45, -0.4),
    AffectiveEntry("weird", 0.5, -0.4),
    AffectiveEntry("strange", 0.45, -0.35),
    AffectiveEntry("glitch", 0.6, -0.45),
    AffectiveEntry("oops", 0.5, -0.4),
    AffectiveEntry("doh", 0.5, -0.35),
)

ZH_LEXICON_V1: tuple[AffectiveEntry, ...] = (
    # ---- positive, high arousal ----
    AffectiveEntry("开心", 0.8, 0.8),
    AffectiveEntry("高兴", 0.75, 0.8),
    AffectiveEntry("快乐", 0.8, 0.85),
    AffectiveEntry("愉快", 0.7, 0.8),
    AffectiveEntry("幸福", 0.8, 0.9),
    AffectiveEntry("欢喜", 0.75, 0.8),
    AffectiveEntry("爽", 0.8, 0.75),
    AffectiveEntry("兴奋", 0.9, 0.75),
    AffectiveEntry("激动", 0.85, 0.7),
    AffectiveEntry("喜欢", 0.7, 0.75),
    AffectiveEntry("爱", 0.9, 0.9),
    AffectiveEntry("爱了", 0.8, 0.8),
    AffectiveEntry("欣赏", 0.55, 0.65),
    AffectiveEntry("佩服", 0.6, 0.7),
    AffectiveEntry("崇拜", 0.7, 0.75),
    AffectiveEntry("甜蜜", 0.55, 0.6),
    AffectiveEntry("暖心", 0.5, 0.65),
    AffectiveEntry("感动", 0.6, 0.65),
    AffectiveEntry("治愈", 0.6, 0.7),
    AffectiveEntry("温柔", 0.4, 0.6),
    AffectiveEntry("可靠", 0.4, 0.6),
    AffectiveEntry("骄傲", 0.55, 0.7),
    AffectiveEntry("自豪", 0.6, 0.7),
    AffectiveEntry("成就感", 0.6, 0.7),
    AffectiveEntry("顺利", 0.5, 0.6),
    AffectiveEntry("成功", 0.65, 0.7),
    AffectiveEntry("搞定", 0.6, 0.65),
    AffectiveEntry("完成", 0.45, 0.55),
    AffectiveEntry("通过", 0.55, 0.6),
    AffectiveEntry("通关", 0.7, 0.7),
    AffectiveEntry("胜利", 0.75, 0.7),
    AffectiveEntry("突破", 0.7, 0.7),
    AffectiveEntry("惊喜", 0.75, 0.7),
    AffectiveEntry("喜悦", 0.75, 0.8),
    AffectiveEntry("幸运", 0.7, 0.7),
    AffectiveEntry("满意", 0.5, 0.7),
    AffectiveEntry("满足", 0.45, 0.65),
    AffectiveEntry("期待", 0.6, 0.6),
    AffectiveEntry("盼望", 0.55, 0.55),
    AffectiveEntry("棒", 0.7, 0.7),
    AffectiveEntry("好棒", 0.85, 0.85),
    AffectiveEntry("太棒", 0.9, 0.85),
    AffectiveEntry("完美", 0.6, 0.75),
    AffectiveEntry("顶级", 0.6, 0.6),
    AffectiveEntry("厉害", 0.6, 0.65),
    AffectiveEntry("牛", 0.55, 0.6),
    AffectiveEntry("强", 0.5, 0.55),
    AffectiveEntry("庆祝", 0.7, 0.7),
    AffectiveEntry("温暖", 0.5, 0.65),
    AffectiveEntry("亲切", 0.45, 0.6),
    AffectiveEntry("友好", 0.5, 0.65),
    AffectiveEntry("爽快", 0.75, 0.75),
    AffectiveEntry("痛快", 0.7, 0.7),
    AffectiveEntry("畅快", 0.75, 0.75),
    AffectiveEntry("过瘾", 0.7, 0.7),
    AffectiveEntry("尽兴", 0.7, 0.7),
    AffectiveEntry("妙", 0.65, 0.7),
    AffectiveEntry("绝了", 0.8, 0.8),
    AffectiveEntry("哇", 0.6, 0.6),
    AffectiveEntry("耶", 0.7, 0.75),
    AffectiveEntry("哈哈", 0.6, 0.6),
    # ---- positive, low arousal ----
    AffectiveEntry("舒服", 0.4, 0.7),
    AffectiveEntry("舒适", 0.4, 0.7),
    AffectiveEntry("安心", 0.35, 0.55),
    AffectiveEntry("放心", 0.4, 0.6),
    # ---- negative, high arousal ----
    AffectiveEntry("烦", 0.6, -0.55),
    AffectiveEntry("烦躁", 0.7, -0.6),
    AffectiveEntry("烦死", 0.8, -0.7),
    AffectiveEntry("烦死了", 0.85, -0.75),
    AffectiveEntry("讨厌", 0.75, -0.7),
    AffectiveEntry("恶心", 0.7, -0.7),
    AffectiveEntry("嫌弃", 0.6, -0.6),
    AffectiveEntry("厌恶", 0.7, -0.7),
    AffectiveEntry("憎恨", 0.85, -0.85),
    AffectiveEntry("恨", 0.8, -0.8),
    AffectiveEntry("气", 0.8, -0.75),
    AffectiveEntry("生气", 0.85, -0.8),
    AffectiveEntry("愤怒", 0.9, -0.85),
    AffectiveEntry("怒", 0.9, -0.85),
    AffectiveEntry("火大", 0.8, -0.7),
    AffectiveEntry("气死", 0.9, -0.85),
    AffectiveEntry("憋屈", 0.65, -0.6),
    AffectiveEntry("委屈", 0.7, -0.65),
    AffectiveEntry("伤心", 0.8, -0.8),
    AffectiveEntry("难过", 0.75, -0.7),
    AffectiveEntry("难受", 0.7, -0.65),
    AffectiveEntry("痛", 0.75, -0.7),
    AffectiveEntry("痛苦", 0.85, -0.8),
    AffectiveEntry("崩溃", 0.9, -0.85),
    AffectiveEntry("崩塌", 0.85, -0.8),
    AffectiveEntry("爆炸", 0.8, -0.7),
    AffectiveEntry("炸了", 0.85, -0.75),
    AffectiveEntry("悲剧", 0.7, -0.75),
    AffectiveEntry("惨", 0.75, -0.75),
    AffectiveEntry("惨痛", 0.8, -0.8),
    AffectiveEntry("可怜", 0.6, -0.65),
    AffectiveEntry("焦虑", 0.8, -0.65),
    AffectiveEntry("紧张", 0.75, -0.55),
    AffectiveEntry("压力", 0.75, -0.55),
    AffectiveEntry("压抑", 0.7, -0.6),
    AffectiveEntry("恐慌", 0.9, -0.8),
    AffectiveEntry("害怕", 0.85, -0.8),
    AffectiveEntry("恐惧", 0.9, -0.85),
    AffectiveEntry("吓人", 0.8, -0.75),
    AffectiveEntry("吓死", 0.9, -0.85),
    AffectiveEntry("慌张", 0.8, -0.6),
    AffectiveEntry("着急", 0.8, -0.6),
    AffectiveEntry("担忧", 0.6, -0.55),
    AffectiveEntry("担心", 0.6, -0.55),
    AffectiveEntry("不安", 0.6, -0.5),
    AffectiveEntry("心虚", 0.6, -0.5),
    AffectiveEntry("郁闷", 0.55, -0.5),
    AffectiveEntry("沮丧", 0.7, -0.65),
    AffectiveEntry("失落", 0.6, -0.6),
    AffectiveEntry("失望", 0.7, -0.7),
    AffectiveEntry("绝望", 0.9, -0.9),
    AffectiveEntry("无助", 0.7, -0.7),
    AffectiveEntry("无奈", 0.55, -0.5),
    AffectiveEntry("迷茫", 0.6, -0.55),
    AffectiveEntry("麻木", 0.35, -0.4),
    AffectiveEntry("折磨", 0.7, -0.7),
    AffectiveEntry("煎熬", 0.75, -0.75),
    AffectiveEntry("倒霉", 0.65, -0.6),
    AffectiveEntry("糟糕", 0.7, -0.7),
    AffectiveEntry("麻烦", 0.6, -0.55),
    AffectiveEntry("棘手", 0.7, -0.6),
    AffectiveEntry("窝火", 0.7, -0.65),
    AffectiveEntry("恼火", 0.8, -0.7),
    AffectiveEntry("气愤", 0.85, -0.8),
    AffectiveEntry("顶不住", 0.75, -0.7),
    AffectiveEntry("撑不住", 0.7, -0.7),
    AffectiveEntry("想死", 0.95, -0.9),
    AffectiveEntry("恨死", 0.9, -0.85),
    AffectiveEntry("心疼", 0.65, -0.6),
    AffectiveEntry("心碎", 0.85, -0.85),
    AffectiveEntry("阴霾", 0.5, -0.55),
    AffectiveEntry("低沉", 0.55, -0.55),
    # ---- negative, low arousal ----
    AffectiveEntry("呆滞", 0.3, -0.4),
    AffectiveEntry("无聊", 0.4, -0.45),
    AffectiveEntry("乏味", 0.4, -0.45),
    AffectiveEntry("没劲", 0.45, -0.45),
    AffectiveEntry("平淡", 0.3, -0.35),
    AffectiveEntry("单调", 0.35, -0.4),
    AffectiveEntry("空虚", 0.45, -0.5),
    AffectiveEntry("累", 0.4, -0.45),
    AffectiveEntry("累死", 0.75, -0.6),
    AffectiveEntry("疲惫", 0.5, -0.55),
    AffectiveEntry("累了", 0.45, -0.5),
    AffectiveEntry("辛苦", 0.55, -0.6),
    # ---- developer domain ----
    AffectiveEntry("报错", 0.65, -0.55),
    AffectiveEntry("错误", 0.6, -0.5),
    AffectiveEntry("挂了", 0.75, -0.65),
    AffectiveEntry("崩了", 0.8, -0.7),
    AffectiveEntry("死机", 0.75, -0.65),
    AffectiveEntry("卡死", 0.75, -0.6),
    AffectiveEntry("卡顿", 0.55, -0.45),
    AffectiveEntry("卡", 0.5, -0.4),
    AffectiveEntry("慢", 0.4, -0.4),
    AffectiveEntry("变慢", 0.45, -0.45),
    AffectiveEntry("超时", 0.6, -0.5),
    AffectiveEntry("失败", 0.6, -0.55),
    AffectiveEntry("没通过", 0.55, -0.5),
    AffectiveEntry("编译", 0.4, -0.25),
    AffectiveEntry("构建", 0.4, -0.25),
    AffectiveEntry("部署", 0.5, -0.3),
    AffectiveEntry("上线", 0.6, 0.6),
    AffectiveEntry("发布", 0.55, 0.6),
    AffectiveEntry("回滚", 0.5, -0.35),
    AffectiveEntry("迁移", 0.4, -0.25),
    AffectiveEntry("重构", 0.45, 0.45),
    AffectiveEntry("合并", 0.4, -0.2),
    AffectiveEntry("依赖", 0.4, -0.2),
    AffectiveEntry("冲突", 0.6, -0.5),
    AffectiveEntry("加班", 0.6, -0.5),
    AffectiveEntry("通宵", 0.7, -0.6),
    AffectiveEntry("熬夜", 0.7, -0.6),
    AffectiveEntry("紧急", 0.7, -0.5),
    AffectiveEntry("修好", 0.5, 0.55),
    AffectiveEntry("修复", 0.5, 0.5),
    AffectiveEntry("跑通", 0.6, 0.65),
    AffectiveEntry("测试", 0.3, 0.3),
    AffectiveEntry("坑", 0.6, -0.5),
    AffectiveEntry("坑爹", 0.7, -0.6),
    AffectiveEntry("踩坑", 0.65, -0.55),
    AffectiveEntry("背锅", 0.6, -0.55),
    AffectiveEntry("甩锅", 0.55, -0.5),
    AffectiveEntry("翻车", 0.7, -0.65),
    AffectiveEntry("事故", 0.75, -0.7),
    AffectiveEntry("故障", 0.7, -0.65),
    AffectiveEntry("宕机", 0.8, -0.7),
    AffectiveEntry("恢复", 0.45, 0.5),
    AffectiveEntry("排查", 0.5, -0.3),
    AffectiveEntry("定位", 0.4, -0.25),
    AffectiveEntry("复现", 0.55, -0.3),
    AffectiveEntry("根因", 0.45, -0.3),
    AffectiveEntry("补丁", 0.4, 0.3),
    AffectiveEntry("交接", 0.4, -0.25),
    AffectiveEntry("催", 0.55, -0.45),
    AffectiveEntry("被催", 0.6, -0.5),
    AffectiveEntry("回归", 0.55, -0.4),
    AffectiveEntry("遗漏", 0.5, -0.45),
    AffectiveEntry("积压", 0.5, -0.45),
    AffectiveEntry("延迟", 0.5, -0.4),
    AffectiveEntry("性能", 0.4, 0.3),
    AffectiveEntry("优化", 0.5, 0.55),
)

LEXICON_V1: tuple[AffectiveEntry, ...] = EN_LEXICON_V1 + ZH_LEXICON_V1


# ---------------------------------------------------------------- loading seam


class Lexicon:
    """Normalized, validated lookup plus longest-match scanner.

    The constructor is the loading seam: swap in a calibrated resource by
    passing a different sequence of entries. Terms are folded case-insensitively
    for lookup; invalid entries (empty term, out-of-range axes) are rejected so
    a replacement resource cannot silently corrupt scoring.
    """

    def __init__(self, entries: Sequence[AffectiveEntry] = LEXICON_V1) -> None:
        self._by_term: dict[str, AffectiveEntry] = {}
        for entry in entries:
            self._validate(entry)
            folded = entry.term.casefold()
            if folded in self._by_term:
                raise ValueError(f"duplicate lexicon term {entry.term!r}")
            self._by_term[folded] = entry
        self._scan_order = sorted(
            self._by_term.values(),
            key=lambda e: (len(e.term), e.term),
            reverse=True,
        )

    @property
    def size(self) -> int:
        return len(self._by_term)

    def lookup(self, term: str) -> AffectiveEntry | None:
        return self._by_term.get(term.casefold())

    def scan(self, text: str) -> list[AffectiveEntry]:
        """Distinct entries present in ``text``, longest term first per span."""
        matched: list[AffectiveEntry] = []
        used: list[tuple[int, int]] = []
        for entry in self._scan_order:
            term = entry.term
            spans = _ascii_spans(text, term) if _is_ascii(term) else _cjk_spans(text, term)
            kept = [span for span in spans if not any(_overlaps(span, other) for other in used)]
            if kept:
                used.extend(kept)
                matched.append(entry)
        return matched

    @staticmethod
    def _validate(entry: AffectiveEntry) -> None:
        if not entry.term:
            raise ValueError("lexicon term must be non-empty")
        if not 0.0 <= entry.arousal <= 1.0:
            raise ValueError(f"arousal out of range for {entry.term!r}: {entry.arousal}")
        if not -1.0 <= entry.valence <= 1.0:
            raise ValueError(f"valence out of range for {entry.term!r}: {entry.valence}")
