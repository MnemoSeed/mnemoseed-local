"""F2 durability annotator + F3 importance scorer (FR-1.3 / FR-1.4 / FR-1.9).

``TurnScorer.score_turn`` runs the capture funnel's classification and scoring
inside one deterministic call:

- F2 - durability ANNOTATION: labels phatic/vented/time-scoped turns
  DISPOSABLE and preferences, decisions, personal rules and stances DURABLE,
  plus strong-markerless anchors via an embedding fallback. A near-verbatim
  session repeat is always labeled disposable. The verdict is metadata only
  (carried on the ScoredTurn + stats telemetry) — it NEVER gates persistence:
  the verbatim contract (design/01 §4) stores every conversational turn.
- F3 - importance: S = w1*arousal_saturated + w2*novelty + w3*causal_chain on a
  0..10 scale (so the pool thresholds read as points). Arousal saturates at a
  cap; valence lives only in the ``cues.emotion`` field and never reaches S or
  provenance confidence. An explicit importance_hint max-merges into S. Every
  turn's S credits the score pool.

Causal chains count distinct connectives plus decision / habit-rule markers,
capped at a constant per turn.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from mnemoseed_local.capture.lexicon_v1 import Lexicon
from mnemoseed_local.schema.stamp import EmotionCue
from mnemoseed_local.schema.turn import Turn, TurnRole
from mnemoseed_local.storage.ports import Embedder


class Durability(StrEnum):
    """F2 annotation for one scored turn (metadata only — never a gate)."""

    DURABLE = "durable"  # annotated as holding long-term-memory value
    DISPOSABLE = "disposable"  # annotated as mechanical / phatic / vented


@dataclass(frozen=True)
class ScoreComponents:
    """The three F3 terms, each on the 0..10 scale."""

    arousal: float  # lexicon arousal, saturation-capped
    novelty: float  # 1 - max cosine vs recent session text
    causal_chain: float  # distinct causal / decision / habit markers


@dataclass(frozen=True)
class DurabilityResult:
    """F2 verdict plus its evidence."""

    durability: Durability
    confidence: float
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScoredTurn:
    """One turn through F2/F3: verdict, S, and the evidence pieces."""

    turn: Turn
    importance: float  # 0..10
    components: ScoreComponents
    durability: DurabilityResult
    emotion: EmotionCue | None  # valence lives here only (the red line)
    causal_reasons: list[str]
    features: dict[str, float]


@dataclass(frozen=True)
class ScoringConfig:
    """Tunable scorer parameters. Weights default to (0.3, 0.4, 0.3)."""

    weights: tuple[float, float, float] = (0.3, 0.4, 0.3)
    arousal_cap: float = 0.75  # Yerkes-Dodson saturation cap
    venting_arousal: float = 0.65
    gaps_arousal: float = 0.8  # attentional-narrowing flag threshold
    neutral_arousal: float = 0.05
    novelty_top: int = 8
    repeat_cosine: float = 0.95  # near-verbatim repeat cutoff
    prototype_margin: float = 0.6
    score_max: float = 10.0


# ------------------------------------------------------------ marker patterns
#
# Durability markers are ordered: a strong-marker turn is durable even when it
# also carries noise; venting/interjection/time-scoped markers reject otherwise
# markerless turns; the default is the conservative reject.

_PREF_ZH = r"我[^。！？\n]{0,12}?(?:喜欢|爱|偏爱|欣赏|倾向于|偏好|钟意|认可|爱用|推崇)"
_PREF_EN = r"\b(?:i|we|i'?d)\b[^.!?\n]{0,20}?\b(?:like|love|prefer|enjoy|value|favor|favour)\b"
_DECISION = (
    r"以后|决定|打算|改为|改成|换(?:用|成|掉)|弃用|一律用|统一(?:用|采用)|"
    r"从今往后|之后都|下次开始|从此|坚持会|一定会用|此后都|改用|"
    r"那就选|就选|就这么(?:定|办)|就这样(?:定|办)|先暂定|"
    # milestone/decision go-ahead only: requires an explicit 就/可以 modal.
    # "接下来要开始..." / bare "接下去开始..." read as one-off operational
    # imperatives (disposable), not milestone decisions.
    r"(?:接下去|接下来)[^。！？\n]{0,8}?(?:就|可以)开始"
)
_RULE = (
    r"每次.{0,18}?(?:都要|都必须|都得|必须|一定要|不要|别|一律)|"
    r"(?:都必须|都要|一定得|一定要|绝不能|再也不会|从不|一律)"
)
_STANCE = r"我(?:认为|觉得|相信|坚持|反对|支持|建议|推荐|希望|看重|在意)"
# Mediated stance: 我 separated from the stance verb by up to 12 non-sentence-final
# chars (e.g. "我刚才说的希望..."), still an explicit first-person preference.
_STANCE_MEDIATED = r"我[^。！？\n]{0,12}?希望"
# Task nouns that mark an immediate implementation/troubleshooting object. Both
# the 确保/保证 and the 探讨 alternatives of _OPEN_CONCERN_ZH gate on them: a
# "怎么确保/探讨" whose object is one of these is an in-the-moment task, not an
# open design/product concern. Shared so the two gates can never drift apart.
_OPEN_TASK_NOUNS = r"(?:函数|代码|编码|模块|接口|组件|脚本|报错|重构|算法|写法|测试|部署|bug)"

_OPEN_CONCERN_ZH = (
    r"疑虑|顾虑|疑问|"
    # design/product-concern phrasing only: 怎么/如何 + 确保/保证 question the
    # behavior of the thing being designed. 怎么解决/如何处理 and a concrete task
    # object ("怎么确保这个测试通过") read as immediate asks and stay disposable.
    r"(?:怎么|如何)(?:确保|保证)(?!\s*[^。！？\n]{0,8}?" + _OPEN_TASK_NOUNS + r")|"
    r"是否能够|"
    # open exploration is durable only when the object is not a code-task noun;
    # "探讨一下这个函数怎么写" is an in-the-moment implementation question.
    r"探讨(?:一下|过)?(?!\s*[^。！？\n]{0,8}?" + _OPEN_TASK_NOUNS + r")|"
    r"有什么技术壁垒"
)
_ABSTRACTION = (
    r"原则|方法论|习惯|规则|流程|方案|标准|规范|偏好|模板|套路|机制|"
    r"文化|风格|准则|纪律|定式"
)

_VENTING_ZH = (
    r"烦死|气死|累死|怨死|受不了|真是受够|要疯了|疯了吧|崩溃|无语|服了|吐血|"
    r"救命|太难了|太累了|真的要命|不想干了|好烦|很烦|真烦|心烦|糟心|闹心|"
    r"头疼|头大|焦头烂额|麻了|绷不住|破防|心态崩|emo了|哭死|难受死|烦透|"
    r"恶心人|烦人|见鬼"
)
_VENTING_EN = (
    r"\b(?:ugh|so (?:tired|done|annoyed|frustrated)|can'?t stand|cannot stand|"
    r"this is (?:so )?(?:annoying|awful|terrible|horrible)|i (?:hate|despise|loathe)|"
    r"worst (?:day|week)|so (?:stressed|frustrating)|screw this|just why)\b"
)
_INTERJECTION = (
    r"(?i)^\s*(?:好的|好|嗯|嗯嗯|哦|噢|奥|好吧|行|行吧|收到|知道了|明白|对|是|"
    r"没错|可以|哈哈哈|哈哈|ok|okay|fine|great|thanks|thank you|got it|sure|"
    r"yep|yeah|yes|no|cool|noted|understood|了解|没问题|同意|支持|确认|保重|"
    r"再见|拜拜)\s*[。！？!?.,…]*\s*$"
)
_TIME_SCOPED = (
    r"今天|明天|昨天|后天|上周|下周|这个月|那个月|下个月|等下|待会|现在|马上|"
    r"下午|上午|晚上|\d+[:：]\d+|\d+\s*[点:]\s*\d*|\d{1,2}\s*(?:号|日|点钟)"
)

_CAUSAL_TERMS: tuple[str, ...] = (
    # connectives
    "因为",
    "所以",
    "导致",
    "造成",
    "使得",
    "于是",
    "因此",
    "从而",
    "既然",
    "since",
    "because",
    "therefore",
    "caused",
    "led to",
    "resulting",
    # decisions
    "决定",
    "打算",
    "以后",
    "下次",
    "从此",
    "改为",
    "改成",
    "换",
    "弃用",
    "坚持",
    "decided",
    "decided to",
    "from now on",
    # habit / rule
    "每次",
    "一律",
    "只要",
    "每当",
    "一旦",
)

_DURABLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pref-marker", re.compile(_PREF_ZH + "|" + _PREF_EN, re.IGNORECASE)),
    ("decision-marker", re.compile(_DECISION)),
    ("rule-marker", re.compile(_RULE)),
    ("stance-marker", re.compile(_STANCE + "|" + _STANCE_MEDIATED)),
    ("open-concern-marker", re.compile(_OPEN_CONCERN_ZH)),
    ("abstraction-noun", re.compile(_ABSTRACTION)),
)

_VENTING_PATTERN = re.compile(_VENTING_ZH + "|" + _VENTING_EN, re.IGNORECASE)

_DISPOSABLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("venting-marker", _VENTING_PATTERN),
    ("interjection", re.compile(_INTERJECTION)),
    ("time-scoped", re.compile(_TIME_SCOPED)),
)

_DISPOSABLE_CONFIDENCE: dict[str, float] = {
    "session-repetition": 0.9,
    "venting-marker": 0.85,
    "embedding-disposable": 0.8,
    "interjection": 0.8,
    "time-scoped": 0.7,
    "default-deferral": 0.6,
}


def _user_text(turn: Turn) -> str:
    """Forward, joined content of the USER steps (assistant/tool are ignored)."""
    parts = [step.content for step in turn.steps if step.role is TurnRole.USER]
    return " ".join(parts)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine between two dense vectors (both unit-normalized by embedders)."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    return max(-1.0, min(1.0, dot))


class TurnScorer:
    """F2/F3 in one deterministic call. Fits the capture funnel's drain side."""

    def __init__(
        self,
        embedder: Embedder,
        config: ScoringConfig | None = None,
        lexicon: Lexicon | None = None,
    ) -> None:
        self._embedder = embedder
        self._config = config if config is not None else ScoringConfig()
        self._lexicon = lexicon if lexicon is not None else Lexicon()
        self._prototype_cache: tuple[Sequence[float], Sequence[float]] | None = None

    def _prototypes(self) -> tuple[Sequence[float], Sequence[float]]:
        """Lazy F3 prototype vectors: the first score_turn call embeds them.
        Construction stays embedding-free so the daemon can assemble the funnel
        before the (possibly not-yet-downloaded) model is needed."""
        if self._prototype_cache is None:
            self._prototype_cache = (
                self._embedder.embed("我用模板管理复用代码").dense,
                self._embedder.embed("老是无缘无故卡住").dense,
            )
        return self._prototype_cache

    @property
    def config(self) -> ScoringConfig:
        return self._config

    def score_turn(
        self,
        turn: Turn,
        *,
        recent_texts: Sequence[str] = (),
        importance_hint: float | None = None,
    ) -> ScoredTurn:
        """Classify durability, score S, and attach the emotion cue."""
        config = self._config
        score_max = config.score_max
        text = _user_text(turn)
        lower = text.casefold()
        current = self._embedder.embed(text).dense

        # ---- novelty + near-verbatim repeat from the injected recent window
        max_sim = 0.0
        novelty = score_max
        is_repeat = False
        if recent_texts:
            sims = [_cosine(current, self._embedder.embed(other).dense) for other in recent_texts]
            max_sim = max(sims)
            is_repeat = max_sim >= config.repeat_cosine
            novelty = max(0.0, min(score_max, (1.0 - max_sim) * score_max))

        # ---- emotion cue and saturated arousal (valence stays in the cue)
        matched = self._lexicon.scan(text)
        if matched:
            peak = max(matched, key=lambda entry: entry.arousal)
            peak_arousal = peak.arousal
            emotion = EmotionCue(
                valence=peak.valence,
                arousal=peak.arousal,
                peripheral_gaps=peak.arousal >= config.gaps_arousal,
            )
        else:
            peak_arousal = config.neutral_arousal
            if _VENTING_PATTERN.search(text) is not None:
                emotion = EmotionCue(
                    valence=-1.0,
                    arousal=config.venting_arousal,
                    peripheral_gaps=False,
                )
            else:
                emotion = None
        arousal = min(peak_arousal, config.arousal_cap) / config.arousal_cap * score_max

        # ---- causal chain (distinct markers, capped)
        causal_matched = [term for term in _CAUSAL_TERMS if term in lower]
        causal_count = min(len(causal_matched), 5)
        causal_chain = float(causal_count) * 2.0

        # ---- embedding evidence (used by markers and the fallback alike)
        durable_prototype, disposable_prototype = self._prototypes()
        durable_sim = _cosine(current, durable_prototype)
        disposable_sim = _cosine(current, disposable_prototype)
        margin = config.prototype_margin

        # ---- durability verdict
        if is_repeat:
            durability = Durability.DISPOSABLE
            reasons = ["session-repetition"]
        else:
            reasons = [marker_id for marker_id, pattern in _DURABLE_PATTERNS if pattern.search(text)]
            if reasons:
                durability = Durability.DURABLE
                if durable_sim >= margin and durable_sim > disposable_sim:
                    reasons.append("embedding-durable")
            else:
                reasons = [marker_id for marker_id, pattern in _DISPOSABLE_PATTERNS if pattern.search(text)]
                if reasons:
                    durability = Durability.DISPOSABLE
                elif durable_sim >= margin and durable_sim > disposable_sim:
                    durability = Durability.DURABLE
                    reasons = ["embedding-durable"]
                elif disposable_sim >= margin and disposable_sim > durable_sim:
                    durability = Durability.DISPOSABLE
                    reasons = ["embedding-disposable"]
                else:
                    durability = Durability.DISPOSABLE
                    reasons = ["default-deferral"]

        if durability is Durability.DURABLE:
            confidence = min(0.7 + 0.05 * min(len(reasons), 4), 0.95)
        else:
            confidence = _DISPOSABLE_CONFIDENCE.get(reasons[0] if reasons else "", 0.7)

        # ---- F3 S (0..10), hint max-merges
        w_arousal, w_novelty, w_causal = config.weights
        auto_s = w_arousal * arousal + w_novelty * novelty + w_causal * causal_chain
        if importance_hint is not None:
            hint_s = max(0.0, min(1.0, importance_hint)) * score_max
            importance = max(auto_s, hint_s)
        else:
            importance = auto_s
        importance = max(0.0, min(score_max, importance))

        result = ScoredTurn(
            turn=turn,
            importance=importance,
            components=ScoreComponents(
                arousal=arousal,
                novelty=novelty,
                causal_chain=causal_chain,
            ),
            durability=DurabilityResult(
                durability=durability,
                confidence=confidence,
                reasons=reasons,
            ),
            emotion=emotion,
            causal_reasons=causal_matched,
            features={
                "durable_similarity": durable_sim,
                "disposable_similarity": disposable_sim,
                "causal_terms": float(causal_count),
            },
        )
        return result
