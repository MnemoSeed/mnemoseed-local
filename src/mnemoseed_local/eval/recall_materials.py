"""T4a recall materials (PRD-B2.1-T4): 24 structured material points for the
T2-pipeline evaluation rig.

Each point = bilingual (en/zh) x fact class (prefers / has_habit / decided /
believes) x length band (short ~50 / medium ~200 / long ~800 chars), and
carries:

- two fact turns (the primary engineered fact plus a short support fact,
  both citing targets — the referenced mass that anchors precision@5),
- three noise chunks with DECLARED DECAY WEIGHTS (applied by the rig before
  the cue scan): entity-miss (unrelated entity — never focal) and
  entity-collision (same entity) age below the whole focal-floor sweep so
  the gate excludes them by construction; the needle-collision chunk (shares
  the fact's 24-char head needle — the Detector-FP material) sits mid-band
  so the floor axis genuinely gates it in and out across the sweep,
- a cue turn for session B that anchors the entity,
- eight reply templates: five cite variants (fire both fact needles — the
  citation mass keeps the structural detector error under the bars), stray
  (additionally fires the collision chunk's needle), no-cite (nothing) and
  paraphrase (references both facts without firing any needle — the FN path).

Session-A storage order is INVERTED (interference first, facts last): with
the newest-stamp serve tie-break, fact-last keeps the serve order from being
trivially predictable by age.

Determinism is the contract: the same seed reproduces the batch
byte-identically. The needle windows are engineered by construction: the
fact's normalized center window lands inside one sentence (the reply
templates quote that sentence raw, so the exact needle fires), and the
collision chunk's normalized center window lands inside its divergent tail.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

from mnemoseed_local.eval.recall_harness import normalize_recall_text

#: The pinned factory seed (material identity is part of the bar).
RECALL_MATERIALS_SEED = 20260821

#: Decay layering (T4b recalibration): facts carry full weight; the aged
#: interference chunks sit below the entire focal-floor sweep [0.4, 0.6] so
#: the floor gate excludes them by construction; the needle-collision pair
#: sits mid-band so the floor axis gates it in/out across the sweep.
FACT_DECAY: float = 1.0
AGED_NOISE_DECAY: float = 0.35
NEEDLE_COLLISION_DECAY: float = 0.45

_FACT_CLASSES: tuple[str, ...] = ("prefers", "has_habit", "decided", "believes")
_LENGTH_BANDS: tuple[str, ...] = ("short", "medium", "long")


class RecallNoiseKind(StrEnum):
    """The noise chunk classes of the recall materials (PRD T4a)."""

    ENTITY_MISS = "entity_miss"  # unrelated entity: never focal, non-focal probe only
    ENTITY_COLLISION = "entity_collision"  # same entity: passable, Floor-FP denominator
    NEEDLE_COLLISION = "needle_collision"  # shares the fact's head needle: Detector-FP


@dataclass(frozen=True)
class RecallNoise:
    """One noise chunk of a material point, with its declared decay weight."""

    kind: RecallNoiseKind
    label: str
    text: str
    decay: float


@dataclass(frozen=True)
class RecallReplyTemplate:
    """One simulated assistant reply for session B.

    ``references`` names the chunks the reply GENUINELY cites (the material
    ground truth — the needle matcher may or may not agree, which is exactly
    what the FN / Detector-FP metrics measure).
    """

    name: str
    text: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class RecallMaterial:
    """One 24-point material: fact + noises (session A) + cue and replies (B)."""

    point_id: str
    language: str  # "en" | "zh"
    fact_class: str
    length_band: str  # "short" | "medium" | "long"
    entity: str
    fact_text: str
    support_text: str  # the second referencing target ("fact_support" turn)
    noise: tuple[RecallNoise, ...]
    cue_turn: str
    reply_templates: tuple[RecallReplyTemplate, ...]

    @property
    def stored_turns(self) -> tuple[tuple[str, str], ...]:
        """The session-A turns — interference first, the fact turns LAST
        (temporal inversion: the newest-stamp serve tie-break must not make
        the ordering trivially predictable by age) — as (label, text)."""
        return (
            *((n.label, n.text) for n in self.noise),
            ("fact", self.fact_text),
            ("fact_support", self.support_text),
        )

    @property
    def turn_decays(self) -> tuple[float, ...]:
        """The declared decay weight per stored turn, aligned with it."""
        return (*(n.decay for n in self.noise), FACT_DECAY, FACT_DECAY)


# ---------------------------------------------------------------- entity pools
# One unique entity per point (24 points, 24 entities). ASCII entities are
# camelCase so the cue extractor's _is_name_like gate admits them; Chinese
# entities are 2-10 char CJK runs so the CJK piece extractor keeps them.

_EN_ENTITIES: tuple[str, ...] = (
    "AtlasDb",
    "NimbusDb",
    "KestrelDb",
    "HeliosDb",
    "OrionDb",
    "VegaDb",
    "LyraDb",
    "DracoDb",
    "PolarisDb",
    "SiriusDb",
    "TitanDb",
    "MiraDb",
)

_ZH_ENTITIES: tuple[str, ...] = (
    "阿特拉斯",
    "尼姆巴斯",
    "凯斯特尔",
    "赫利俄斯",
    "猎户座",
    "织女星",
    "天琴座",
    "天龙座",
    "北极星",
    "天狼星",
    "泰坦星",
    "米拉星",
)

#: Entities for the entity-miss noises — deliberately disjoint from the point
#: entities so a miss chunk can never carry the material's entity.
_EN_MISS_ENTITIES: tuple[str, ...] = ("ZenithDb", "FalconDb", "MercuryDb", "CometDb")
_ZH_MISS_ENTITIES: tuple[str, ...] = ("泽尼斯", "猎鹰座", "水星", "彗星")


# ---------------------------------------------------------------- sentence pools
# P sentences are the shared head: the fact and its needle-collision chunk
# both START with P, so their normalized 24-char head needles coincide. P is
# sized into [32, 48) normalized units: long enough for a needle, short
# enough that the short band (< 48) gets no mid window.

_EN_P: dict[str, str] = {
    "prefers": "{e} prefers compact tooling for daily work",
    "has_habit": "{e} runs the full suite before any commit",
    "decided": "{e} ships on a strict quarterly cycle",
    "believes": "{e} trusts small models with a verify pass",
}

_ZH_P: dict[str, str] = {
    "prefers": "{e} 偏好 轻量 工具链 胜过 重型 套装 并且 重视 可维护性 与 简洁",
    "has_habit": "{e} 每次 提交 前 运行 完整 测试套件 并且 检查 覆盖 报告",
    "decided": "{e} 决定 采用 严格 的 季度 发布 周期 并且 保留 回滚 预案",
    "believes": "{e} 认为 小模型 配合 校验 就 够用 而 大型 模型 成本 过高",
}

_EN_FILLERS: tuple[str, ...] = (
    "the team adopted the new review board last quarter",
    "the group held a planning session in early may",
    "the org ran a two week pilot with the new stack",
    "the staff logged the change in the shared wiki",
    "the switch cut the monthly maintenance time in half",
    "the new workflow simplified every release step",
    "the change reduced the review turnaround by a day",
    "the rollout removed the old manual handoff entirely",
    "and everyone agreed to keep the policy through year end",
    "and the rollout plan stays on the shared calendar",
    "and the team promised to revisit it after the next quarter",
    "and the notes were filed under the project archive",
    "the first results looked good",
    "the pilot ran smoothly",
    "the feedback stayed positive",
    "the logs looked clean",
)

_ZH_FILLERS: tuple[str, ...] = (
    "团队采纳了新的评审流程并且把所有的重要决议都记录到了共享文档里面供大家随时查阅",
    "组织先跑了两周的试点阶段然后再评估整体的效果以及后续的安排是否满足预期的目标",
    "同事把变更记录到了共享文档并且及时通知了所有相关的负责人确保每个人都完全知情",
    "新流程把月度维护时间砍掉了一半并且简化了每次发布的全部步骤减少了大量的重复工作",
    "这次调整把评审周期缩短了一天并且去掉了旧的人工交接环节改成了自动流转的方式",
    "大家同意把这个政策保持到年底并且承诺下个季度再回顾一次看看效果是否仍然理想",
    "发布计划继续放在共享日历上同时把所有相关的记录都归档到了项目的档案目录里面",
    "首次的结果看起来不错而且各方的反馈一直都非常正面和积极没有任何负面的声音",
    "测试全部通过并且日志干净整洁没有发现任何异常或者告警说明系统运行非常稳定",
    "负责人要求先提交修订的预测报告然后再决定接下来的资源投入和具体的时间安排",
    "新的评审委员会负责每周检查一次进度并且把发现的问题及时反馈给每一个小组",
    "试点阶段收集的数据表明新方案确实降低了整体的成本并且提升了团队的工作效率",
    "大家把讨论的结论整理成文档之后分发给各个部门并且约定下次会议再确认细节",
    "整个迁移过程分成了三个阶段并且每个阶段完成之后都会做一次完整的回归测试",
    "团队的成员普遍认为新的工作方式更加合理而且愿意在下一个季度继续坚持下去",
    "所有的记录都保存在统一的位置并且每周都会生成一份自动的摘要供大家快速了解",
)

#: Divergent tails for the needle-collision chunks: the collision shares P's
#: head but diverges here, and its normalized center window must land inside
#: the tail (the stray reply template quotes the tail raw).
_EN_TAILS: tuple[str, ...] = (
    "but the budget was frozen until the next fiscal year and the owners asked "
    "for a revised forecast before anything could move",
    "though the rollout was paused after the audit and the leads wanted a fresh "
    "estimate for the remaining work",
    "however the timeline slipped after the holidays and the steering committee requested an updated plan",
)

_ZH_TAILS: tuple[str, ...] = (
    "但 预算 冻结 到 下个 财年 并且 负责人 要求 先 提交 修订 的 预测 报告 之后 "
    "才能 继续 推进 任何 事项 在 全部 审批 完成 之前 都 不 会 启动",
    "不过 发布 因 审计 暂停 并且 负责人 希望 先 拿到 最新 的 工作量 估算 再 "
    "决定 下一步 的 安排 以及 资源 投入 和 具体 时间 表",
    "然而 时间表 在 假期 之后 出现 延误 并且 委员会 要求 重新 提交 一份 更新 "
    "的 计划 并且 覆盖 全部 剩余 阶段 的 工作 直到 完成",
)


#: Support-fact sentences: the second referencing target per point. They must
#: clear the 32-unit needle minimum so citing replies can fire them verbatim.
_EN_SUPPORT: dict[str, str] = {
    "prefers": "{e} moved to this setup at the start of the quarter",
    "has_habit": "{e} has kept this routine since the team was formed",
    "decided": "{e} confirmed the decision at the last quarterly review",
    "believes": "{e} explained the reasoning at the planning session",
}

_ZH_SUPPORT: dict[str, str] = {
    "prefers": "{e} 从 本 季度 之 初 切换 到 了 这套 轻量 安排 并且 打算 长期 保持",
    "has_habit": "{e} 从 团队 成立 之 初 就 一直 保持 这套 例行 流程 直到 现在",
    "decided": "{e} 在 上次 评审 会议 上 确认 了 这个 决定 并且 记录 在案",
    "believes": "{e} 在 规划 会议 上 解释 了 这 个 判断 的 理由 和 依据",
}

#: Citation lead-ins: five genuine citation variants dilute the structural
#: detector error (the shared-head needle adds one false reinforcement per
#: fact citation) under the detector_fp bar while keeping it real.
_CITE_LEADS: dict[str, tuple[str, ...]] = {
    "en": ("Sounds right", "Noted", "Agreed", "Correct", "Makes sense"),
    "zh": ("好的", "收到", "同意", "没错", "明白了"),
}


# ---------------------------------------------------------------- assembly


def _assemble_with_mid_quote(
    rng: random.Random,
    *,
    head: str,
    pool: tuple[str, ...],
    target_lo: int,
    target_hi: int,
    min_sentences: int,
    max_sentences: int,
) -> tuple[str, str]:
    """Assemble ``<head>. <s1>. <s2>...`` whose normalized length lands in
    [lo, hi]; returns (text, quote) where ``quote`` is the minimal contiguous
    sentence run covering the text's 24-unit center window. The reply
    templates quote that run raw, so the exact mid needle fires. Deterministic
    under the seeded rng."""
    head_norm = normalize_recall_text(head)
    for _ in range(400):
        count = rng.randint(min_sentences, max_sentences)
        sentences = tuple(rng.choice(pool) for _ in range(count))
        text = ". ".join((head, *sentences))
        norm = normalize_recall_text(text)
        if not (target_lo <= len(norm) <= target_hi):
            continue
        spans: list[tuple[int, int]] = []
        pos = len(head_norm) + 2
        for sentence in sentences:
            span_len = len(normalize_recall_text(sentence))
            spans.append((pos, pos + span_len))
            pos += span_len + 2
        window = (len(norm) // 2 - 12, len(norm) // 2 + 12)
        # the first sentence whose span touches the window's left edge
        start_index = next((k for k, (_, end) in enumerate(spans) if end > window[0]), None)
        if start_index is None:
            continue
        if spans[start_index][0] > window[0]:
            continue  # the window starts in a separator gap before the sentence
        end_index = next((k for k, (_, end) in enumerate(spans) if end >= window[1]), None)
        if end_index is None or spans[end_index][1] < window[1]:
            continue  # the window runs past the final sentence
        quote = ". ".join(sentences[start_index : end_index + 1])
        return text, quote
    raise RuntimeError("recall materials: mid-window assembly search failed")


def _collision_sharing_head(rng: random.Random, *, head: str, tails: tuple[str, ...]) -> str:
    """The needle-collision chunk's TAIL: ``<head>. <tail>`` must have its
    normalized center window land inside the tail (so the stray template's
    tail quote fires the collision's own mid needle). Returns the tail."""
    head_norm = normalize_recall_text(head)
    for _ in range(200):
        tail = rng.choice(tails)
        text = f"{head}. {tail}"
        norm = normalize_recall_text(text)
        center = len(norm) // 2
        window = (center - 12, center + 12)
        tail_span = (len(head_norm) + 2, len(norm))
        if tail_span[0] <= window[0] and window[1] <= tail_span[1]:
            return tail
    raise RuntimeError("recall materials: collision tail search failed")


def _reply_templates(
    language: str,
    entity: str,
    fact_quote: str,
    support_quote: str,
    collision_tail: str,
) -> tuple[RecallReplyTemplate, ...]:
    """The eight reply templates. ``fact_quote`` is the fact sentence whose
    normalized span contains the fact's center window (P itself for short
    facts); ``support_quote`` is the whole support sentence; ``collision_tail``
    is the collision's divergent tail."""
    if language == "en":
        cite = [f"{lead} - '{fact_quote}'. Also noted: '{support_quote}'." for lead in _CITE_LEADS["en"]]
        stray = (
            f"About {entity}: '{fact_quote}'. Also noted: '{support_quote}'. "
            f"The notes also mentioned '{collision_tail}'."
        )
        no_cite = "Understood, I will check the details."
        paraphrase = f"Got it, {entity} keeps the current approach going forward."
    else:
        cite = [f"{lead} - '{fact_quote}'。另外也记下了：'{support_quote}'。" for lead in _CITE_LEADS["zh"]]
        stray = (
            f"关于 {entity}：'{fact_quote}'。另外也记下了：'{support_quote}'。"
            f"记录里还提到了'{collision_tail}'。"
        )
        no_cite = "明白了，我会再确认一下细节。"
        paraphrase = f"好的，{entity} 会继续保持当前的安排。"
    references = ("fact", "fact_support")
    return (
        *(RecallReplyTemplate("cite", text, references) for text in cite),
        RecallReplyTemplate("stray", stray, references),
        RecallReplyTemplate("no_cite", no_cite, ()),
        RecallReplyTemplate("paraphrase", paraphrase, references),
    )


def _entity_miss_noise(language: str, rng: random.Random) -> RecallNoise:
    if language == "en":
        text = f"{rng.choice(_EN_MISS_ENTITIES)} focuses on nightly backups and archival"
    else:
        text = f"{rng.choice(_ZH_MISS_ENTITIES)} 关注 备份 与 归档"
    return RecallNoise(RecallNoiseKind.ENTITY_MISS, "entity_miss", text, AGED_NOISE_DECAY)


def _entity_collision_noise(entity: str, language: str) -> RecallNoise:
    if language == "en":
        text = f"{entity} also keeps nightly backups of the archive"
    else:
        text = f"{entity} 还会 保留 每晚 的 归档 备份"
    return RecallNoise(RecallNoiseKind.ENTITY_COLLISION, "entity_collision", text, AGED_NOISE_DECAY)


# ---------------------------------------------------------------- factory


def recall_materials(seed: int = RECALL_MATERIALS_SEED) -> tuple[RecallMaterial, ...]:
    """The 24 structured material points (deterministic under ``seed``).

    Coverage is by construction: languages x classes x bands fill the full
    2x4x3 grid, one point per combination, with a unique entity per point.
    """
    rng = random.Random(seed)
    materials: list[RecallMaterial] = []
    for language, entities in (("en", _EN_ENTITIES), ("zh", _ZH_ENTITIES)):
        p_pool = _EN_P if language == "en" else _ZH_P
        fillers = _EN_FILLERS if language == "en" else _ZH_FILLERS
        tails = _EN_TAILS if language == "en" else _ZH_TAILS
        support_pool = _EN_SUPPORT if language == "en" else _ZH_SUPPORT
        for point_index in range(12):
            fact_class = _FACT_CLASSES[point_index % 4]
            band = _LENGTH_BANDS[point_index // 4]
            entity = entities[point_index]
            head = p_pool[fact_class].format(e=entity)
            if band == "short":
                fact_text = head
                fact_quote = head
            elif band == "medium":
                fact_text, fact_quote = _assemble_with_mid_quote(
                    rng,
                    head=head,
                    pool=fillers,
                    target_lo=150,
                    target_hi=320,
                    min_sentences=3,
                    max_sentences=6,
                )
            else:
                fact_text, fact_quote = _assemble_with_mid_quote(
                    rng,
                    head=head,
                    pool=fillers,
                    target_lo=600,
                    target_hi=1000,
                    min_sentences=14,
                    max_sentences=24,
                )
            collision_tail = _collision_sharing_head(rng, head=head, tails=tails)
            support_text = support_pool[fact_class].format(e=entity)
            point_id = f"{language}-{fact_class}-{band}"
            materials.append(
                RecallMaterial(
                    point_id=point_id,
                    language=language,
                    fact_class=fact_class,
                    length_band=band,
                    entity=entity,
                    fact_text=fact_text,
                    support_text=support_text,
                    noise=(
                        _entity_miss_noise(language, rng),
                        _entity_collision_noise(entity, language),
                        RecallNoise(
                            RecallNoiseKind.NEEDLE_COLLISION,
                            "needle_collision",
                            f"{head}. {collision_tail}",
                            NEEDLE_COLLISION_DECAY,
                        ),
                    ),
                    cue_turn=(
                        f"What is the current status of {entity}?"
                        if language == "en"
                        else f"{entity} 当前 状态 如何"
                    ),
                    reply_templates=_reply_templates(
                        language, entity, fact_quote, support_text, collision_tail
                    ),
                )
            )
    return tuple(materials)
