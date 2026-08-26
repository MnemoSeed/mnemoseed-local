# 10 · 短期激活（Short-Term Activation：最近被召回的记忆保持易再浮现）

> 一句话定位：给「刚被召回过的记忆」一个分钟尺度的临时可及性加成，让它在同一会话内更容易再次浮现——排序-only、会话内易失、默认关闭。这是已注册理论锚 R23（ACT-R base-level learning + activation）的快半边（分钟尺度）；慢半边（天尺度的 decay/reinforce/sweep）已在库。
> 状态基线：设计文档批次（docs-only）。行号引注钉在基线 commit `0ba0167`（2026-08-26 工作树）；实现批开工时须按当时基线重钉。
> 主要依据：GitHub issue #122（立项与范围拍板）、issue #75（dogfood 遥测前置闸，观察窗口运行中）、issue #80（EvalRig/RecallRig 物化语义统一前置闸）、design/03 §2.2 / §3.4 / §3.6（TA-1 同构、融合排序、衰减引擎）、design/06 §2（TA-1 全文）。
> **实施状态：本篇是设计先行稿。两个前置闸门当前均未满足（见 §5）——闸门通过前，任何人不得据本篇开工写码。**

## 0. 功能定位与边界

**本篇讲什么**：召回事件之后的短期激活——一个易失的、按 `(profile_id, session_id)` 隔离的内存态加成，带分钟尺度衰减，只参与融合排序、永不参与候选过滤；以及它必须等待的两个未满足的实施闸门。

**本篇明确出界的事**（承 issue #122 拍板，逐条如实记录）：

- 读时冲突检测：不做；
- 从检索路径自动作废/改写记录：绝不——provenance 只追加红线原样不动；
- 复用 reinforce / consumption-evidence 通道：不复用——FR-4.2 回弹（`decay/reinforce.py`）写的是**持久的** `decay_weight` 与 `last_reinforced`，本机制是**易失的**激活信号，两种信号两条通道，混用会把分钟尺度语义泄漏进持久保留动力学；
- 跨进程持久化：不做。daemon 重启即失温，属诚实的 as-is 边界——缺状态时行为精确退化为今天的检索（fail-open，无静默错误路径）。

## 1. 问题陈述

现状只有一条长程权重通路：`decay_weight = confidence × exp(−λ × days_since_last_reinforced)`（`decay/model.py:69-78`），λ 分层为天—月尺度（fact ≈69 天半衰期、episode ≈23 天，`decay/model.py:5-9`）；召回命中的强化事件也落在同一慢通路上——`min(1.0, w + 0.1)` 回弹并刷新基线（`decay/reinforce.py:126-132`）。也就是说，一条记忆被召回之后，它在存储里的可及性变化是以**天**为单位刻度的。

会话内的实际体验却是分钟刻度的：第 3 轮刚被召回过的记忆，在第 8 轮换个措辞再问时，它与「几十天没被碰过的任意候选」站在完全相同的起跑线上——能否再次浮现完全取决于第二次查询的语义相似度与线索重叠度。查询措辞一变、实体线索一弱，刚用过的记忆就可能输掉 top-k 名额（`AssembleConfig.top_k = 5`，`retrieve/assemble.py:80`），而人类记忆在这个场景下表现出相反的规律：刚提取过的内容在随后几分钟内显著更容易再次提取。

痛点频率目前**未被测量**——这正是 §5.1 的 Gate 1。本篇先把设计与理论立此存照，不为未证实的频率提前建机制。

## 2. 理论锚

入选标准同全系列：只收有实验与长期复现证据验证的规律；每条给来源 / 已验证规律 / →设计规则。理论回答「为什么这样设计」；一切数值（boost 上限、快衰减常数、TTL、ε 权重）仍是机制层标定项，由 §5 的评测闸门校准，不因理论获得神圣性。

### 2.1 ACT-R 双成分记忆（base-level learning + activation）——引用既有注册锚 R23，不重复注册

- 来源：Anderson, J. R., & Schooler, L. J. (1991). Reflections of the environment in memory. *Psychological Science*, 2(6), 396–408. **已核验 · 本仓 `docs/zh/design/REFERENCES.md` R23 ✅**（注册表原文用途：「ACT-R base-level 排序动力学（频率/近因/间隔环境规律）」，对应本篇已列 00、01、03、05、06）。本仓 TA-1 全文登记于 PRD-B2.1 与 design/06 §2 TA-1，design/03 §2.2 为一句话同构陈述。**本篇只引用该条目，不新注册重复锚。**
- 已验证规律（R23 承载的规律中与本篇相关的切面）：记忆的可用性 = 基础激活 + 当前线索的扩散激活 + 噪声；其中基础激活按幂律对历次使用求和（β = ln Σ t_j⁻ᵈ），频度与时近各自独立地提升提取概率。对本篇要害的一条推论：**一次提取行为本身会在提取后的短时间内显著抬高该记忆的再提取概率，该抬升随时间快速消退，最终回落到由长程频度决定的水平**——即「热手效应」是双成分结构里的快成分，不是错觉。
- → 设计规则：
  1. 检索强度是双成分的：慢成分（频度 × 长时近，天—月尺度）+ 快成分（最近提取，分钟尺度）。慢成分本仓已落地为 TA-1 同构物——sweep 按时近单调衰减 + 命中回弹刷新基线（design/03 §1.3）；**快成分缺失**，即本篇要补的对象。
  2. 快成分只能由**真实召回事件**产生：被装配进上下文包才算提取；写入、注入未消费不算（与 TA-6「注入 ≠ 强化」的边界精神一致，见 §3.2 开放问题 A）。
  3. 快成分只影响排序（activation 进融合分），永不充当过滤条件——ACT-R 里 activation 差异表现为提取概率的连续变化，不是有无门槛。
  4. 幂律求和意味着紧邻的重复提取边际收益递减——工程化表述：**重复命中刷新而非叠加**（时间戳覆盖，不累加 bump）。

**严格分离声明**：以上为理论层。快衰减常数、boost 幅度、TTL 等数值没有理论出处，全部属 §3 的机制层标定项。

### 2.2 不借清单（pop-neuro 挡板）

- **Miller 7±2 不作为任何容量/条数常量的出处**（全系列既定规则；如需提工作记忆容量，只可引 Cowan 2001 的 ~4，REFERENCES R13 ✅）。本机制的每会话条目上限是工程护栏值，与容量锚无关。
- **艾宾浩斯曲线不外推到分钟级快窗**：R8 📕 只锚无复习条件下小时—天以上的遗忘形态。快成分的衰减常数**没有曲线形状锚**，是纯标定值；文档与注释里不得出现「Ebbinghaus 短窗版」之类的表述。
- **「闪回记忆感觉确定 = 更可信」不收**：Neisser & Harsch（1992，REFERENCES R16 ✅）证伪了 flashbulb 记忆的准确性。短期激活同样永不触碰 `provenance.confidence`——升温的记忆只是更容易被看到，不是更可信。
- **Von Restorff 隔离效应不入册也不借用**（REFERENCES.md 未入册说明已明示暂缓：营销概念污染 + 触碰捕获中立红线的风险）。
- **「记忆像肌肉，越练越永久」的训练话术不收**：提取练习效应（R46 ✅）已被 design/09 §2.2 引用并落地为持久回弹通路；本篇不再叠加任何「练得越多永久权重越高」的叙事，快成分与持久回弹严格分账。
- **学习风格匹配论、左脑/右脑记忆、莫扎特效应**：无可复现实证，一律不收。

## 3. 实现机制草案（与理论层严格分离；闸门未过前仅为纸面设计）

### 3.1 状态载体：易失的每会话激活表

建议新模块 `src/mnemoseed_local/retrieve/activation.py` 或 `daemon/session_activation.py`（归 06 会话续传面更贴切——生命周期绑会话；实现批定夺）：

- 数据形态：`dict[(profile_id, session_id), dict[target_id, (last_hit_ts_monotonic, bump)]]`，`target_id` 为 chunk_id 或 node_id。
- 库内先例：`MemoryService` 已持有一组同键形的易失会话态（`_pending_slots` / `_seen_chunk_ids` / `_session_epoch`，`daemon/memory.py:406-428`），以 `_pending_lock`（`daemon/memory.py:425`）串行化并发访问，并在 `/session/end` 生命周期统一清理——激活表照抄同一模式。
- 时钟用 `time.monotonic()`（进程内相对时间即可，状态本来就不跨进程）。

### 3.2 写入点：召回命中即升温（fire-and-forget）

- 主面挂在 `MemoryService.recall`（`daemon/memory.py:442-485`）已有的命中消费处：`_record_hits(context)`（`daemon/memory.py:487-510`）已经以 fire-and-forget 纪律消费命中清单（chunk_ids / node_ids / rescued），激活 bump 在同一处追加、同一个 try/except 吞异常纪律之下——**计温失败绝不 fail recall**。
- 冲突组联动（issue #122 拍板）：bump 一条图节点时同时 bump 其 `conflict_group` 全组，保证冲突两侧同温。组员判据与 `Assembler._group_of`（`retrieve/assemble.py:467-474`）共用：`conflict_flag ∧ conflict_group is not None`（字段定义在 `schema/graph.py` 的 GraphNode）。chunk 无冲突组字段，不适用联动。
- 开放问题 A：T2 中段自动回忆面（`note_user_prompt` → `recall_pending` serve，`daemon/memory.py:1007` 起）的 served 条目是否也计升温？倾向计入（被装配到模型面前就是提取事实），但实现批须把「激活通道 ≠ FR-4.2 reinforce 通道」的边界在测试里钉死——TA-6 约束的是强化语义，不约束可及性排序信号。

### 3.3 衰减：读时惰性计算，无后台循环

有效加成 = `bump × exp(−λ_fast × seconds_since_last_hit)`，λ_fast 为分钟尺度标定项。与 `decay/sweeper.py` 的常驻趋势循环不同，这里**不需要后台 sweeper**：状态是进程内存态、只在打分时读，惰性求值即可；过期条目在读路径顺手丢弃，无崩溃恢复游标问题（重启即清零，天然幂等）。「重复命中刷新而非叠加」（§2.1 规则 4）实现为时间戳覆盖。

### 3.4 与现有排序的整合：第五个融合分量（方案甲，推荐）

- **关键约束（来自现有契约）**：`HybridRetriever` 的模块级确定性契约是「no clocks, no randomness, no network; ties break by (kind, id)」（`retrieve/hybrid.py:30-31`）。因此激活值**不能**在 retriever 内部取时钟现算——正确接缝是：调用方（`MemoryService`，持有锁与时钟）在调 `recall()` 前算好一份只读映射 `{id: activation ∈ [0,1]}` 作为参数传入；retriever 只是把它当作又一个确定性输入分量使用。确定性契约原样保住。
- 融合公式扩为五项：`score += ε · activation`。落点：`HybridConfig` 新增 `weight_activation` 字段（默认 0.0 = 关，`retrieve/hybrid.py:79-99`）；`_breakdown` 加分量并让 `ScoreBreakdown` 自报该字段（`hybrid.py:105-115` / `hybrid.py:394-415`，透明度纪律与 ε·共现项「缺位自报」先例一致）。
- 备选方案乙（已论证放弃）：仿 Freshness Guard 的乘子降权先例（×0.8 后重跑选择循环，`assemble.py:216-246`）在 Assembler 层做乘子。放弃理由：候选池在 retriever 阶段已被截断（vector_top_k/graph_top_k=20，且存储级过滤先行），Assembler 乘子救不了「根本没进池的热记忆」——机制位置错位。
- 测试面提示：`ScoreBreakdown` 形状被既有测试 pin 定，新增字段须同步更新 pins；双轨合并字节等价测试不受影响（激活作为输入参数进入两侧同一公式）。

### 3.5 红线：只排序、永不过滤

- 存储级预过滤保持原样：向量轨 `ChunkFilter.min_decay`（`hybrid.py:249-265`）与图轨 decay 地板（`hybrid.py:332-334`）不因激活改变——激活不抬地板、不降地板、不进任何 filter 参数。后果如实记录：从未入池的记忆无法靠「别人热了」入池，激活只能重排已在池内的成员。若 Gate 1 遥测显示主瓶颈恰是池边界截断（热的候选没进 top-20），那是另一个问题，另立 issue，不在本机制夹带。
- rescued 位强制排序不变（`_sort_key`，`hybrid.py:374-378`）：被救候选恒排正常候选之后，激活不改这条显式纪律。
- 激活永不触碰 `provenance.confidence`、永不写回存储（§0 出界项）。

### 3.6 配置开关：默认关闭

- `[recall]` 配置节先例：`RecallConfig`（`config.py:262` 起，rescue_floor/rescue_cue_min 解析于 `config.py:736-742`）。新增键建议 `recall.shortterm_activation.enabled`（bool，**默认 false**）+ 标定键（λ_fast、bump、TTL）。enabled=false 时整条路径零开销短路（不建表、不查表、ε=0）。
- configwrite 注册先例：`capture.auto_recall_focal_floor` 的 ConfigKey 注册（`configwrite/service.py:625-631`）；新键照抄该模式，doctor/config 面自动可见、可热改。
## 4. 失败模式与规模（简）

- **表增长**：每会话条目设工程上限（量级数百，标定项），超限按最冷逐出；会话结束随生命周期清理（§3.1 先例）。10x–1000x 规模下这是 O(活跃会话 × 上限)，与 daemon 常驻态同阶。
- **并发**：锁纪律镜像 `_pending_lock`——读路径只做字典查找，临界区极短。
- **崩溃/重启**：状态丢失 = 冷启动，行为退化为今天，无数据损坏面（状态从不落盘）。
- **时钟回拨**：monotonic 时钟免疫。

## 5. 实施闸门（当前均未满足——本篇不是 ready-to-build）

### 5.1 Gate 1 · dogfood 遥测证明痛点频率（issue #75，观察窗口运行中）

issue #75 的 P0–P2 一周 dogfood 观察仍在运行。**范围缺口如实标注**：#75 已承诺的 P0–P2 并不产出「会话内再相关性丢失」频率这一指标——Gate 1 要求先以 issue 评论清单项的形式把该指标**追加进正在运行的观察**（不改 #75 既有范围），由补入的指标产出结论；**#75 在未补入该指标的情况下关闭，不构成 Gate 1 通过**。通过条件：补入的指标显示丢失发生得足够频繁、足以被感知，结论记录进 #75 并被实现批的 PRD 引用。若数据显示罕见 → 关闭 #122 为 won't-do-with-numbers，本篇归档为设计存照，不建机制。

### 5.2 Gate 2 · eval rig 缺 warm-needle 材料类（前置依赖 issue #80）

两套评测装置的生命周期契约曾分裂：`EvalRig`（`eval/harness.py`）matrix 命令原为 wipes-by-construction，而 `RecallRig` / `RescueRig` 已是 fail-loud 的 `RigRootNotFresh` 物化语义——这正是 issue #80 要求统一的分裂点。#80 已落地统一：三个评测 rig（`EvalRig` / `RecallRig` / `RescueRig`）现共享同一 fail-loud 物化契约（`eval/rig_freshness.py` 的 `require_fresh_root` / `RigRootNotFresh`：fresh = 不存在或空目录，既有痕迹绝不抹除；隔离改由调用方按 per-run 目录划分）。warm-needle 材料类（「先召回一次、随后换措辞在会话内追问同一事实」的多轮回放材料，现有 `eval/materials.py` / `eval/recall_materials.py` / `eval/rescue_materials.py` 均无此形态）必须搭建在**统一后**的物化语义之上（前置已满足）：在分裂语义上建测量，等于把污染风险浇进校准 ε/λ_fast 所依赖的仪器本身。剩余顺序：warm-needle 材料类 + 基线测量 → 实现批 → 评测矩阵标定 → 才谈开启默认值。

### 5.3 闸门顺序总结

#75 结论（go/no-go 决策点）→ 若 go：#80 统一物化语义 → warm-needle 材料类 + ε=0 基线 → 按 §3 实现于默认关闭旗标之后 → eval matrix 标定 ε / λ_fast / bump / TTL → 数据支持时才讨论默认开启。

## 6. 红线与诚实边界

1. **排序-only**：激活只进融合分，永不进任何过滤参数（§3.5）；rescued 强制序不变。
2. **confidence 不触碰**：升温 ≠ 更可信；捕获中立红线原样（评分不读偏好/anima，激活信号同样不读内容语义）。
3. **verbatim 通道零接触**：本机制不读写任何 `text` 字段。
4. **通道分账**：不复用 FR-4.2 reinforce / consumption-evidence 通道；两种信号、两条通路、两套测试边界。
5. **易失诚实标注**：重启失温是特性不是缺陷，文档与遥测不得冒充持久能力。
6. **默认关闭 + 可见性**：关闭时零开销短路；开启时激活分量经 `ScoreBreakdown` 自报，行为差异永远可审计。
7. **未实施诚实标注**：本篇全部为设计，截至基线 commit 无一行对应代码；两个实施闸门未过。

## 7. 开放问题

- A：T2 serve 是否计升温事件（§3.2）——倾向计入，实现批以测试钉死通道边界。
- B：ε 与 λ_fast 的缺省标定值——等 Gate 2 的 warm-needle 基线，本文故意不给数。
- C：模块归属 `retrieve/activation.py` vs `daemon/session_activation.py`——倾向后者（生命周期绑会话），实现批定夺。

## 8. 本篇引用

- R23 — Anderson, J. R., & Schooler, L. J. (1991). Reflections of the environment in memory. *Psychological Science*, 2(6), 396–408.（已核验 · REFERENCES R23 ✅；TA-1 主锚。**仅引用，未新注册**）
- R13 — Miller (1956) + Cowan (2001) 组合条目（REFERENCES R13 ✅；仅作 7±2 禁用规则的出处提及）。
- R16 — Neisser & Harsch (1992) Phantom flashbulbs（REFERENCES R16 ✅；反证锚：「感觉确定 ≠ 准确」）。
- R46 — Roediger & Karpicke (2006)（REFERENCES R46 ✅；边界锚：持久提取练习效应已归 design/09，本篇不再叠加）。
- 工单：#122（立项）、#75（Gate 1 dogfood 遥测）、#80（Gate 2 前置：EvalRig/RecallRig 物化语义统一）。