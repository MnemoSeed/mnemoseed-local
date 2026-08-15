"""F2 persistence classifier + F3 importance scorer (FR-1.3 / FR-1.4 / FR-1.9).

All behaviour goes through TurnScorer.score_turn: durability label + confidence
for F2, then saturated arousal / novelty / causal-chain and the combined S for
F3, with valence restricted to the cues field and the importance_hint max-merge.
"""

from __future__ import annotations

import pytest

from mnemoseed_local.capture.scorer import (
    Durability,
    ScoredTurn,
    ScoringConfig,
    TurnScorer,
)
from mnemoseed_local.schema.turn import HostId, Turn, TurnRole, TurnStep
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder


def _turn(text: str, *, index: int = 0, profile: str = "prof-main") -> Turn:
    return Turn(
        turn_index=index,
        session_id="sess-score-1",
        profile_id=profile,
        host=HostId.GENERIC,
        started_at=0.0,
        steps=[TurnStep(role=TurnRole.USER, content=text)],
    )


def _scorer(config: ScoringConfig | None = None) -> TurnScorer:
    return TurnScorer(embedder=SyntheticEmbedder(), config=config)


# ---------------------------------------------------------------- F2 label


def test_ac2_bug_venting_class_rejected() -> None:
    result = _scorer().score_turn(_turn("这 bug 烦死了"))
    assert result.durability.durability is Durability.DISPOSABLE
    assert 0.0 <= result.durability.confidence <= 1.0
    assert result.durability.reasons


def test_ac2_review_preference_accepted() -> None:
    result = _scorer().score_turn(_turn("我 review 喜欢简洁"))
    assert result.durability.durability is Durability.DURABLE
    assert result.durability.confidence >= 0.7
    assert "pref-marker" in result.durability.reasons


def test_decision_sentence_accepted() -> None:
    result = _scorer().score_turn(_turn("以后都用 pnpm 管理依赖"))
    assert result.durability.durability is Durability.DURABLE


def test_pure_venting_rejected() -> None:
    result = _scorer().score_turn(_turn("今天累死了 真是受不了"))
    assert result.durability.durability is Durability.DISPOSABLE


def test_cjk_en_mixed_preference_accepted() -> None:
    result = _scorer().score_turn(_turn("每次 code review 我都要简洁 别寒暄"))
    assert result.durability.durability is Durability.DURABLE


def test_phatic_interjection_rejected_as_conservative_default() -> None:
    result = _scorer().score_turn(_turn("好的"))
    assert result.durability.durability is Durability.DISPOSABLE


def test_markerless_neutral_text_defaults_to_reject() -> None:
    result = _scorer().score_turn(_turn("下午三点开会"))
    assert result.durability.durability is Durability.DISPOSABLE


def test_verbatim_session_repeat_is_rejected_as_repetition() -> None:
    text = "我 review 喜欢简洁"
    scorer = _scorer()
    first = scorer.score_turn(_turn(text))
    assert first.durability.durability is Durability.DURABLE
    repeat = scorer.score_turn(_turn(text), recent_texts=[text])
    assert repeat.durability.durability is Durability.DISPOSABLE
    assert "session-repetition" in repeat.durability.reasons


def test_embedding_fallback_accepts_markerless_durable_anchor() -> None:
    result = _scorer().score_turn(_turn("我用模板管理复用代码"))
    assert result.durability.durability is Durability.DURABLE
    assert "embedding-durable" in result.durability.reasons


def test_embedding_fallback_rejects_markerless_disposable_anchor() -> None:
    result = _scorer().score_turn(_turn("老是无缘无故卡住"))
    assert result.durability.durability is Durability.DISPOSABLE
    assert "embedding-disposable" in result.durability.reasons


# ---------------------------------------------------------------- F2 calibration (NFR-1.3)
# Patterns from the human-labeled durability set that the v1 lexicon rejected; each
# is a general Chinese durable-intent expression (no project-specific tokens).

CALIBRATED_FN_TEXTS = [
    # decision go-ahead ("接下去可以开始")
    "官方云端还需要多一层，就是系统管理员，属于我管理整个系统运行，和查看所有数据，也可以查看销售增长等等，服务是否上线等等的管理。接下去可以开始M0",
    # open design concern (疑虑 / 怎么确保)
    (
        "设计没问题，但我还有一个疑虑，就是用户使用cursor之类的工具时，"
        "你说没有startsession hook的功能，那样要怎么确保AI会自行运用记忆服务呢？"
        "在整个session的对话过程中，又怎么确保AI能有效写入和读取记忆呢？"
    ),
    # open exploration question (探讨 / 是否能够 / 技术壁垒)
    "claude desktop/codex desktop这类的桌面型应用，也可以探讨一下是否能够接入MnemoSeed，有什么技术壁垒",
    # preference statement with a mediated stance verb ("我刚才说的希望...")
    (
        "AI Mode里的反馈并不是绝对的，你可以自行斟酌是否要加入设计。"
        "还有目前我用着mempalace，但是有时候可能触发记忆存储的间隔太远，"
        "一旦中间关闭session重开就失忆，你有办法解决吗？"
        "顺带一提，我刚才说的希望语气拟人一点，沟通更多以说明情况并给出选择和背后原因，"
        "而不是用很多缩写代号等等最后变成无字天书。但我同时要你尽量精简内容，节省token消耗"
    ),
    # conditional decision (那就选)
    "1)LanceDB\n2)我需要支持多语言，特别是中文和英文，bge-m3合适吗？如果使用gemma，有什么限制？对于未来产品推出有什么影响？两个模型之间的差别？如果效能结果都相似，甚至更好，并且没有资安风险，那就选bge-m3\n3）行",
]


@pytest.mark.parametrize("text", CALIBRATED_FN_TEXTS)
def test_calibrated_chinese_durable_markers_classify_durable(text: str) -> None:
    result = _scorer().score_turn(_turn(text))
    assert result.durability.durability is Durability.DURABLE, text


def test_mediated_preference_stance_classifies_durable() -> None:
    result = _scorer().score_turn(_turn("我刚才说的希望语气拟人一点，尽量精简内容"))
    assert result.durability.durability is Durability.DURABLE
    assert "stance-marker" in result.durability.reasons


def test_imperative_requests_do_not_flip_to_durable() -> None:
    # Regression fence against overfitting: bare "可以开始" / bare "确保" / "我需要你"
    # are one-off operational instructions, not durable signals.
    disposable_kept = [
        # "可以开始" appears but without the 接下去/接下来 decision prefix
        "我想要你先补上缺失的references，然后可以开始讨论M0，不过M0是什么？",
        # bare 确保 without 怎么/如何; "我需要你" is a request, not a requirement marker
        "改，但同时我需要你仔细查看现在各个最新工具各自的能力，并针对性优化这些记忆使用模式，确保整个记忆使用体验是丝滑流畅无障碍的",
    ]
    for text in disposable_kept:
        result = _scorer().score_turn(_turn(text))
        assert result.durability.durability is Durability.DISPOSABLE, text


def test_open_concern_immediate_tasks_do_not_flip_to_durable() -> None:
    # Fence against open-concern-marker overreach: "怎么解决/如何处理/探讨..." on
    # an immediate code/file task is a trouble-shooting ask, not an open design or
    # product question. Only the latter class is durable (human labeling standard).
    disposable_stays = [
        "这个 bug 怎么解决",  # debugging ask, not a product concern
        "文件权限问题如何处理",  # operational fix ask
        "我们来探讨一下这个函数怎么写",  # in-the-moment code task
        # 怎么/如何 + 确保/保证 on a concrete task object: still an immediate task
        "怎么确保这个测试通过",  # task object: 测试
        "怎么保证接口不超时",  # task object: 接口 (same noun the 探讨 gate already blocks)
        "怎么确保部署成功",  # task object: 部署
    ]
    # mirror direction: the two human-durable open-concern rows must stay durable
    durable_stays = [
        # :59 design concern about product behavior
        (
            "设计没问题，但我还有一个疑虑，就是用户使用cursor之类的工具时，"
            "你说没有startsession hook的功能，那样要怎么确保AI会自行运用记忆服务呢？"
            "在整个session的对话过程中，又怎么确保AI能有效写入和读取记忆呢？"
        ),
        # :64 open product/platform exploration
        "claude desktop/codex desktop这类的桌面型应用，也可以探讨一下是否能够接入MnemoSeed，有什么技术壁垒",
    ]
    for text in disposable_stays:
        result = _scorer().score_turn(_turn(text))
        assert result.durability.durability is Durability.DISPOSABLE, text
    for text in durable_stays:
        result = _scorer().score_turn(_turn(text))
        assert result.durability.durability is Durability.DURABLE, text


def test_go_ahead_decision_prefix_requires_modal() -> None:
    # Fence against decision-prefix overreach: "接下来要开始..." / "接下去开始..." are
    # one-off operational imperatives (disposable); only an explicit modal go-ahead
    # ("接下去可以开始") is a milestone decision (decision-marker).
    disposable_stays = [
        "接下来要开始部署了",
        "接下去开始跑测试",
    ]
    # mirror direction: the human-durable milestone go-ahead must stay durable
    durable_stays = [
        (
            "官方云端还需要多一层，就是系统管理员，属于我管理整个系统运行，和查看所有数据，"
            "也可以查看销售增长等等，服务是否上线等等的管理。接下去可以开始M0"
        ),
    ]
    for text in disposable_stays:
        result = _scorer().score_turn(_turn(text))
        assert result.durability.durability is Durability.DISPOSABLE, text
    for text in durable_stays:
        result = _scorer().score_turn(_turn(text))
        assert result.durability.durability is Durability.DURABLE, text


# ---------------------------------------------------------------- F3 arousal


def test_arousal_saturates_at_cap() -> None:
    scorer = _scorer()
    extreme = scorer.score_turn(_turn("崩溃极了"))
    mild = scorer.score_turn(_turn("有点烦"))
    assert extreme.components.arousal == pytest.approx(10.0)
    assert mild.components.arousal == pytest.approx(0.6 / 0.75 * 10.0)
    assert extreme.components.arousal > mild.components.arousal


def test_peripheral_gaps_flag_on_extreme_arousal() -> None:
    scorer = _scorer()
    assert scorer.score_turn(_turn("崩溃极了")).emotion is not None
    assert scorer.score_turn(_turn("崩溃极了")).emotion.peripheral_gaps is True
    assert scorer.score_turn(_turn("有点烦")).emotion.peripheral_gaps is False


# ---------------------------------------------------------------- F3 valence red line


def test_valence_never_enters_s_score_or_confidence() -> None:
    # two turns with equal arousal, equal novelty (self-reference), opposite valence
    scorer = _scorer()
    negative = scorer.score_turn(_turn("真气啊 我气坏了"), recent_texts=["真气啊 我气坏了"])
    positive = scorer.score_turn(_turn("太爽了 真爽"), recent_texts=["太爽了 真爽"])
    assert negative.emotion is not None and negative.emotion.valence is not None
    assert positive.emotion is not None and positive.emotion.valence is not None
    assert negative.emotion.valence < 0.0 < positive.emotion.valence
    assert negative.emotion.arousal == pytest.approx(positive.emotion.arousal)
    assert negative.importance == pytest.approx(positive.importance)
    assert negative.importance > 0.0  # negative valence did not reduce S


# ---------------------------------------------------------------- F3 novelty


def test_novelty_higher_for_distant_topics() -> None:
    scorer = _scorer()
    text = "我 review 喜欢简洁"
    distant = scorer.score_turn(_turn(text), recent_texts=["部署 K8s 集群时的网络配置"])
    repeated = scorer.score_turn(_turn(text), recent_texts=[text])
    assert distant.components.novelty > repeated.components.novelty + 5.0
    assert repeated.components.novelty == pytest.approx(0.0)
    assert 0.0 <= distant.components.novelty <= 10.0


def test_novelty_ten_when_nothing_recent() -> None:
    result = _scorer().score_turn(_turn("我 review 喜欢简洁"))
    assert result.components.novelty == pytest.approx(10.0)


# ---------------------------------------------------------------- F3 causal chain


def test_causal_chain_counts_connectives_and_decisions() -> None:
    scorer = _scorer()
    text = "因为接口变了 导致全部报错 我决定以后都用这个库"
    result = scorer.score_turn(_turn(text))
    # connectives: 因为, 导致; decisions: 决定, 以后
    assert result.components.causal_chain == pytest.approx(min(4, 5) * 2.0)
    assert result.causal_reasons
    assert _scorer().score_turn(_turn("好的")).components.causal_chain == pytest.approx(0.0)


# ---------------------------------------------------------------- S combo / config


def test_weights_are_configurable() -> None:
    text = "因为接口变了 导致全部报错 我决定以后都用这个库"
    arousal_only = _scorer(ScoringConfig(weights=(1.0, 0.0, 0.0))).score_turn(_turn(text))
    causal_only = _scorer(ScoringConfig(weights=(0.0, 0.0, 1.0))).score_turn(_turn(text))
    assert arousal_only.importance == pytest.approx(arousal_only.components.arousal)
    assert causal_only.importance == pytest.approx(causal_only.components.causal_chain)


def test_importance_hint_max_merges() -> None:
    scorer = _scorer()
    plain = scorer.score_turn(_turn("好的"))
    hinted_low = scorer.score_turn(_turn("好的"), importance_hint=0.1)
    hinted_full = scorer.score_turn(_turn("好的"), importance_hint=1.0)
    assert hinted_low.importance == pytest.approx(plain.importance)
    assert hinted_full.importance == pytest.approx(10.0)


def test_importance_hint_never_reduces_score() -> None:
    scorer = _scorer()
    text = "我 review 喜欢简洁"
    plain = scorer.score_turn(_turn(text))
    # auto-S here is already above 0.5; the hint must not pull it down
    hinted = scorer.score_turn(_turn(text), importance_hint=0.4)
    assert hinted.importance >= plain.importance


def test_score_bounds_and_result_shape() -> None:
    result: ScoredTurn = _scorer().score_turn(_turn("我 review 喜欢简洁"))
    assert 0.0 <= result.importance <= 10.0
    assert result.turn.turn_index == 0
    assert result.durability.confidence <= 1.0
