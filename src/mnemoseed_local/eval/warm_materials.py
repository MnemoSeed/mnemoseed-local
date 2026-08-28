"""Warm-needle materials (design/10 §5.2, Gate 2): within-session re-query shape.

The existing recall materials are TWO-session A/B (needle stored in session A,
recalled in session B). A warm-needle point is the SAME-SESSION re-query
shape: ONE needle fact measured under successive /memory/recall calls in one
session context — a first query recalls it, then a changed-wording re-query
moments later probes whether it re-surfaces.

Unlike a singleton pool (which pins every point to a vacuous rank-1), each
point seeds a competitive SAME-BAND decoy set — the same entity/domain tokens
but different asserted facts — mirroring the rescue rig's decoy discipline
(``rescue_materials.py``): rival candidates are present precisely so the
needle's top-k slot can genuinely move by score. With a full decoy set the
needle can even drop OUT of top-k entirely on a decoy-leaning re-query, which
is exactly what a future activation's decay would need to discriminate. Each
point carries one probe per measurement window:

- ``immediate`` (0s delay): a needle-aligned re-query that re-surfaces the
  needle at the top of its pool (the warm fact holds its slot, cue intact);
- ``delayed`` (a tilt the future activation's decay is judged against): a
  decoy-leaning re-query where competing decoys claim the slots and the needle
  does NOT re-surface — the decay the window exists to probe;
- ``negative_control`` (0s delay, decoy-aligned): a query where a decoy's cue
  wins and the needle is absent, proving the instrument can measure the fact
  NOT surfacing — the baseline is discriminative, not vacuous.

Deterministic under the pinned factory seed (material identity is part of any
future calibration bar). Scores discriminate lexically: the synthetic embedder
hashes tokens, so a query re-surfaces whichever stored facts share more of its
terms — a needle-aligned query favors the needle, a decoy-leaning query favors
the (numerous) same-band decoys.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Pinned factory seed (material identity is part of the measurement bar).
WARM_MATERIALS_SEED = 20260829

#: The semantic delay of the "after a delay" measurement window, in seconds.
DELAYED_WINDOW_SECONDS: float = 30.0

#: The warm-window labels the instrument measures.
WINDOW_IMMEDIATE = "immediate"
WINDOW_DELAYED = "delayed"
WINDOW_NEGATIVE_CONTROL = "negative_control"


@dataclass(frozen=True)
class WarmProbe:
    """One warm-window measurement: a first recall, then a changed-wording
    re-query ``delay_s`` seconds later, within the same session. The delayed
    window's re-query is decoy-leaning; the negative control's re-query is
    decoy-aligned by construction."""

    window: str  # "immediate" | "delayed" | "negative_control"
    delay_s: float  # seconds between the first recall and the re-query
    re_query: str  # the changed-wording query the fact is measured under


@dataclass(frozen=True)
class WarmNeedleMaterial:
    """One same-session re-query point: a stored needle fact, a competing
    same-band decoy set (same entity, different facts), plus per-window probes."""

    point_id: str
    language: str  # "en" | "zh"
    entity: str
    fact_id: str
    fact_text: str
    decoys: tuple[tuple[str, str], ...]  # (decoy_id, decoy_text) pairs
    session_id: str  # the within-session recall context measured
    first_query: str  # the needle-aligned query that recalls the needle
    probes: tuple[WarmProbe, ...]


# ------------------------------------------------------------- decoy templates
# One shared set of same-band decoy sub-topics per language, parameterized by
# the point's entity. Each decoy asserts a DIFFERENT fact than the needle under
# the same entity/domain, so the needle's rank can genuinely move by score.


def _en_decoys(entity: str) -> tuple[tuple[str, str], ...]:
    texts = (
        f"{entity} also archives the weekly digest bundle",
        f"{entity} stores its audit log on the cold storage tier",
        f"{entity} syncs the replication lag to the standby replica",
        f"{entity} schedules backup scans for the weekend window",
        f"{entity} keeps the metrics cache served from the shared pool",
        f"{entity} assigns the storage tier quota every quarter",
    )
    return tuple((f"{entity}-decoy{index}", text) for index, text in enumerate(texts, start=1))


def _zh_decoys(entity: str) -> tuple[tuple[str, str], ...]:
    texts = (
        f"{entity} 还 把 每周 摘要 包 归档",
        f"{entity} 把 审计 日志 存 在 冷 存储 层",
        f"{entity} 把 复制 延迟 同步 到 备 副本",
        f"{entity} 把 备份 扫描 安排 在 周末 窗口",
        f"{entity} 把 指标 缓存 放 在 共享 池",
        f"{entity} 每 季 度 分配 存储 层 配额",
    )
    return tuple((f"{entity}-decoy{index}", text) for index, text in enumerate(texts, start=1))


# ---------------------------------------------------------------- point plan
# One fact family per language. The first query and the immediate re-query are
# needle-aligned (they re-surface the needle top); the delayed re-query and the
# negative control are decoy-leaning/decoy-aligned (competing decoys claim the
# slots and the needle drops OUT of top-k). Every query is a distinct sentence —
# a genuine re-wording, never a repeat.

_EN_FACTS: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (
        "export-window",
        "AtlasDb",
        "AtlasDb moved its nightly export window to 02:00 UTC",
        "How did AtlasDb reschedule its nightly export window?",
        "Where does AtlasDb schedule its nightly export now?",
        "When does AtlasDb publish the weekly digest?",
        "The weekly digest bundle is archived for AtlasDb",
    ),
    (
        "review-cycle",
        "NimbusDb",
        "NimbusDb runs its quarterly review at the start of the quarter",
        "How does NimbusDb schedule its quarterly review?",
        "When does NimbusDb hold its quarterly review meeting?",
        "When does NimbusDb ship a patch release for the build?",
        "Which NimbusDb patch release ships every two weeks?",
    ),
    (
        "cluster-migrate",
        "KestrelDb",
        "KestrelDb finished migrating its staging cluster to the new hardware",
        "How did KestrelDb finish migrating its staging cluster?",
        "Which cluster did KestrelDb just migrate to the new hardware?",
        "Where does KestrelDb store the audit log on the cold tier?",
        "KestrelDb stores the audit log on the cold storage tier",
    ),
)

_ZH_FACTS: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (
        "导出时段",
        "阿特拉斯",
        "阿特拉斯 把 夜间 导出 时段 迁移 到 凌晨 两点",
        "阿特拉斯 怎么 调整 夜间 导出 窗口",
        "阿特拉斯 现在 把 夜间 导出 安排 在 什么 时候",
        "每周 摘要 包 归档 到 阿特拉斯 的 哪 里",
        "每周 摘要 包 是 阿特拉斯 归档 的",
    ),
    (
        "评审节奏",
        "尼姆巴斯",
        "尼姆巴斯 在 每 月 月初 举行 季度 评审",
        "尼姆巴斯 怎么 安排 季度 评审",
        "尼姆巴斯 的 季度 评审 在 什么 时候 开",
        "补丁 版本 每 两 周 交付 给 尼姆巴斯 用户",
        "补丁 版本 是 尼姆巴斯 每 两 周 交付 的",
    ),
    (
        "集群迁移",
        "凯斯特尔",
        "凯斯特尔 完成 了 把 预发 集群 迁移 到 新 硬件",
        "凯斯特尔 怎么 迁移 预发 集群",
        "凯斯特尔 把 哪些 集群 迁移 到 了 新 硬件",
        "凯斯特尔 把 审计 日志 存 在 哪 一 层",
        "审计 日志 存 在 冷 存储 层 是 凯斯特尔",
    ),
)


def _fact_material(
    *,
    point_id: str,
    language: str,
    entity: str,
    fact_text: str,
    first_query: str,
    immediate_re: str,
    delayed_re: str,
    control_query: str,
) -> WarmNeedleMaterial:
    """One point with the immediate, delayed, and negative-control probes. The
    delayed re-query (decoy-leaning) and the decoy-aligned control are the
    discriminating measurements; the first and immediate queries are
    needle-aligned so the warm precondition and top-slot re-surfacing hold."""
    decoys = _en_decoys(entity) if language == "en" else _zh_decoys(entity)
    return WarmNeedleMaterial(
        point_id=point_id,
        language=language,
        entity=entity,
        fact_id=f"{entity}-fact",
        fact_text=fact_text,
        decoys=decoys,
        session_id=f"sess-{entity}",
        first_query=first_query,
        probes=(
            WarmProbe(WINDOW_IMMEDIATE, 0.0, immediate_re),
            WarmProbe(WINDOW_DELAYED, DELAYED_WINDOW_SECONDS, delayed_re),
            WarmProbe(WINDOW_NEGATIVE_CONTROL, 0.0, control_query),
        ),
    )


def warm_materials(seed: int = WARM_MATERIALS_SEED) -> tuple[WarmNeedleMaterial, ...]:
    """The warm-needle material points (deterministic under ``seed``), each
    with a same-band decoy set and carrying an immediate probe, a delayed probe,
    and a decoy-aligned negative control."""
    del seed  # the plan is static today; the seed keeps the factory signature stable
    materials: list[WarmNeedleMaterial] = []
    for point_id, entity, fact, first, immediate, delayed, control in _EN_FACTS:
        materials.append(
            _fact_material(
                point_id=f"warm-en-{point_id}",
                language="en",
                entity=entity,
                fact_text=fact,
                first_query=first,
                immediate_re=immediate,
                delayed_re=delayed,
                control_query=control,
            )
        )
    for point_id, entity, fact, first, immediate, delayed, control in _ZH_FACTS:
        materials.append(
            _fact_material(
                point_id=f"warm-zh-{point_id}",
                language="zh",
                entity=entity,
                fact_text=fact,
                first_query=first,
                immediate_re=immediate,
                delayed_re=delayed,
                control_query=control,
            )
        )
    return tuple(materials)
