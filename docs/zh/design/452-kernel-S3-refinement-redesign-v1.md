# 452 内核 S3：冷层/gist 重新设计 - 有界输入 + 显式提取失败（v1 / PROPOSAL）

> 状态：**PROPOSAL，DESIGN ONLY，无门权**。本片属 issue #457 设计批次（`org/workspace/kernel-first-principles-20261122/design-batch-brief.md:6-12,56-93`）。
> 阶段纪律：计划/brief → 版本化设计/合同 → 工作分片 → 独立跨族设计 QA `CLOSABLE` → 冻结 → 才允许工程（`org/PROTOCOL.md:176-191`）。
> **本片不改变任何产品行为**：不写产品源码/配置、不绑端口、不碰 `~/.mnemoseed-local`、不提交。
> **是否激活冷层是后续 owner 决定**（task brief S3；`design-batch-brief.md:68-70,82`）；本设计只让该决定可被做出，不预先替 owner 做出。
> 默认姿态：**在质量门 QG-1..QG-6 全部成立之前，默认「不激活」**（理由见 §7.3）。

---

## 0. 一句话钢人化（steelman）

目标不是「把 dream 参数调大让 graph 重新长出边」，而是：**把已死的冷层（gist/graph）从一条「无界批量输入 → 静默空判定 → 78 万 token 换 0 三元组」的失败路径，重构成一条「每条/每 episode 有界输入 → 提取失败显式可数 → 冷层只存 verbatim 存不出的跨会话结构」的可测管线**；并用质量门把「是否重新开启它」保留为 owner 的独立裁决。若无法证明冷层真正互补而非重复，诚实结论是「不激活 + 把 CLS 锚点改写为 as-is boundary」。

若这段钢人化不成立（例如冷层根本无法产出 verbatim 之外的价值），第 7 节的「不激活」分支就是第一发现，而不是附注。

---

## 1. 结论（verdict）

**REDESIGN（旧 v1 提取路径）+ NEW DESIGN GATE；ACTIVATION DEFERRED。**

- 旧路径（`02-dream-engine.md:17` 的「池子积分触发 + 全量分批三元组提取 + 双席投票」）**不再作为可修复对象**：它的失效是结构性的（`inv3:46-53,70`），不是 `reflect_batch_max_tokens` 的取值问题。
- 本片交付的是**新设计门**，不是「重启 #294 已关停的 auto_trigger 路径」：所有实现切片都必须在 owner start 授权 + 设计冻结后才可执行（`design-batch-brief.md:7-12`；`org/PROTOCOL.md:176-191`）。
- 本片**不推荐立即激活**；激活问题 OD-1 交 owner（§12）。

### 1.1 有界输入契约（三行）

> **C1（上限）** 每次提取调用只消费 **一个**有界工作项（per-item / per-episode），硬上限 B token（B 为待标定常量，建议默认 ≤ 8k，`[未验证]` 需真语料标定）；由确定性 packer 产出，**永不切文中**，超限项进入「项级溢出列表」而非被截断进空 delta（对照 `src/mnemoseed_local/dream/delta.py:208-237,256-260`）。
> **C2（超限）** 单项自身 > B 时，按语义边界拆成子项（每个 ≤ B，子项携带回指源 chunk id 的 provenance）；**任何输入项都不被静默丢弃** - 不变量：`已消费项 ∪ 已停车项(带信号) = 全部输入项`。
> **C3（失败）** 有界调用返回 0 事实或 schema 不合规时，写一条 **显式 `extraction_failure` 信号**（append-only `error_events`，新增保留 `ErrorSignalType` 成员，`evidence_ptr` → 源 chunk/session，`detector_id`，`retryable`），**绝不提交「成功 + 0 三元组」**，任何有未覆盖/失败项的 run 不得把水位标记为 clean/完成。

**防积压保证**：因为每次调用只处理一个项、且每个项必须落在 C2 的不变量或 C3 的信号里，「11,413 个 chunk 被静默搁置」（`inv3:30,49,161`）在结构上不可再发生 - 任何搁置项都是**可计数、可查询、带证据指针**的失败信号。

---

## 2. 理论锚（Theory anchors - 固定，不复议；只判机制是否忠实实现）

纪律：只借用有实证基础的规律；「已验证规律 → 设计规则」与「实现机制」严格分离；显式「不借用」清单（`brief.md:22-24`；`org/PROTOCOL.md` 理论锚纪律；`AGENTS.md:66-73`）。

| 锚 | Citation | 已验证规律（理论层） | 设计规则（理论层） | 本设计里的实现机制（机制层，可改） |
|---|---|---|---|---|
| R1 CLS 双存储 ✅ | McClelland, McNaughton & O'Reilly 1995（`REFERENCES.md:17`；`00:168`） | 海马快录情景、皮层慢沉淀结构，两系统互补 | 双存储：热 verbatim 永不丢，冷 gist 沉淀**跨情节结构**；任一轨失效必须可见，不得静默吸收 | lancedb 向量池（热）+ sqlite_graph（冷）；冷层写什么见 §7.1 |
| R2 SWR 重放 ✅ | Wilson & McNaughton 1994（`REFERENCES.md:18`；`00:169`） | 睡眠期海马向皮层回放日间经验 | 巩固是**编码之后的离线、有界重复**，不在捕获热路径 | 会话结束触发（SessionEnd，未激活），一个会话一个整理窗口 |
| R12 Fuzzy-Trace verbatim/gist ✅ | Brainerd & Reyna 1990（`00:159`） | verbatim 与 gist 并行，提取 gist 不抹除原文 | verbatim 通道永不有损；gist 可蒸馏但必须可经 provenance 指回原文 | 冷层只存引用与派生事实，**绝不内嵌 chunk 正文**（`11-refinement-v2.md:55`） |
| R4 编码特异性 ✅ | Tulving & Thomson 1973（`REFERENCES.md:20`；`00:161`） | 提取成功率取决于线索与编码上下文重合；遗忘主因是线索失败 | 每条沉淀事实携带来源会话 id 与 chunk 锚点，供未来按线索检索 | `{session_id, chunk_ids, kind, payload}`（`11:43`） |
| R5 再巩固 ✅ | Nader et al. 2000（`00:171`） | 提取后进入可写窗，冲突不静默消灭 | 冲突标 `needs_reconcile`、检索成对返回 | key-supersede 版本链（§6, §8.4），不是复制 design/12 提名制 |
| R34/R35 重建性记忆/误导信息 📕 | Bartlett 1932；Loftus 2005（`00:181`） | 蒸馏/重建会扭曲，须先验证再落库 | 合并前质量门：输出经检验才写回 graph | 接地校验 + >50% 丢弃率 fail-loud（§6, §8.3） |
| R56 独特性效应 ⚠️ | von Restorff 1933；Hunt & Worthen 2006（`REFERENCES.md:125`；`11:22`） | 与背景差异显著的项记忆更牢 | 精炼优先保留差异事件（错误→修复、决策反转），平淡内容只做一句摘要 | **状态照抄 ⚠️（经典高置信但本轮未直接核验），不得升级为 ✅**；作为排序偏好而非硬门 |

**显式不借用**：
- 弗洛伊德式梦的象征解释、左右脑分工神话、学习风格匹配（已证伪）、莫扎特效应、记忆无限容量假设（沿用 `11:25`，均为无实证/已证伪）。
- **Mem0 的 ADD-only「只增不改」语义**：与 provenance 版本链/冲突调和四分支直接冲突，只能借「有界小输入」，借 ADD-only 等于自毁护城河（`competitor-memory-mechanisms.md:235`；详见 §5）。
- 任何托管/遥测、任何厂商 benchmark 数字（均 CLAIMED，`competitor:111,236`）不作为可达目标或对外宣称。
---

## 3. 与 owner 2026-10-28 #294 裁决的对账（硬性）

| 事实 | 证据 | 本设计的姿态 |
|---|---|---|
| #294 裁决：`auto_trigger=false`，8 snapshots archived，refinement stopped | `design-batch-brief.md:68`；`brief.md` 同源 | **完全承认，不翻案、不绕过** |
| 停机点：`audit_log id=505, actor=cli, key=dream.auto_trigger, value=false, version_id=7000005 @2026-10-28T01:31:11.359Z` | `inv3-graph-scorepool.md:64-66,77-79,173`（逐字） | 该配置保持至今（`inv3:66,76`）；本片不写任何配置 |
| 旧路径产出：`dreams/` 已清空，graph 37 节点/0 边/0 回执，78 万 token 换 0 三元组 | `inv3:28,37,49-50,183-185,194-196` | 视为**已归档失败事实**，不作为「待修复的活体功能」 |
| task brief 明令「NEW design gate，NOT parameter tweak，NOT silent restart」 | `design-batch-brief.md:65-82` | 以新设计门承接；激活由 OD-1 单独裁决 |

**防「后门重启」的具体保证**：
1. 本片所有切片产物默认 **OFF**：不把 `dream.auto_trigger` 设为 true，不改 `config.py:977/991` 的默认值，不复用被关停的 score_pool 自动触发（`11:32` 已判其死路）。
2. 任何未来实现切片若需要触发，只能新增一个**独立、默认关闭**的开关，并在 OD-1 未决时保持关闭；禁止改写 `audit id=505` 的版本语义或假装该裁决不存在。
3. 若最终选择「不激活」，则按 Seat B D4/b 把内核如实改述为 verbatim-only + explicit-pin 体系，CLS 锚点标为 as-is boundary，而非 shipped 冷层（`first-principles-kernel-gap.md:111,121,183,202,212`）。
---

## 4. 根因与设计目标（设计要打败的实测敌人）

**实测根因（INV-3，逐字证据）**：
- `reflect_batch_max_tokens=8000`（`config.py:254,977,991`；`daemon/app.py:917` 传为 `batch_max_tokens`）装不下真实 chunk（~34k token 为 2026-10-28 doctor 先验告警，当前不可复测 → `[未验证]`，`inv3:33,52`）。
- 分批器把整块 packing 到预算内，超出者记为 overflow（`delta.py:208-237`）；**单个 chunk 超过 packer 预算时仍在 `pack` 内被裁成空 delta / 空 packed ids**（`delta.py:255-260`）- 这就是「提取器拿到被截断 delta」的机制。
- 结果：`EMPTY extraction verdict over 15 covered chunk(s); 11413 unseen overflow chunk(s) stay pending`（`inv3:49,161`）；pipeline 把「空 verdict + 有 overflow」判为 defer/park 而非错误（`pipeline.py:357-428`），并明确 batched 情形会**提交空 verdict 并清掉 covered ids**（`pipeline.py:371-377,429-436`）。
- token 代价：30 次运行、ledger 共 783,951 token（`inv3:25,37,44,120-124,194-196`）。
- 上游/下游放大器（非本次直接机制）：provider_failure 204（9月162+11月42，`inv3:31,130-147`）、`dream_vote` 无路由 645 次（`inv3:32,164`）。

**设计目标（从第一性原理重推导，而非从竞品功能清单抄）**：
- G1 输入有界：单次提取输入上限化、项级化，消灭「无界批量 → 截断」的结构前提。
- G2 失败显式：空 verdict / schema 不合规是可数、可查、带证据指针的一等信号，不再是「成功但 0 产出」。
- G3 冷层互补：冷层只存 verbatim 存不出的跨会话结构，且有质量门证明其价值（§7）。
- G4 诚实边界：激活前不改变产品行为；不激活则如实改述边界，不留「半死静默」态（Seat B:111,183）。
---

## 5. 竞品机制借鉴（只借两条，且借法有界）

Seat A 竞品技术层（一手 README，访问 2026-11-22，逐条 VERIFIED/CLAIMED/INFERRED 分级见 `competitor-memory-mechanisms.md:103-125,181-191`）：

| 借什么 | 竞品证据 | 本设计如何借 | 明确不借 |
|---|---|---|---|
| **有界逐条/逐 episode 输入**（Mem0 single-pass、Graphiti per-episode structured output） | Mem0「每次 add 一次有界小输入提取，结构上不积累 overflow」(`competitor:107,109`)；Graphiti「逐 episode 抽取 + 结构化 JSON」(`competitor:122`) | §8 的 C1/C2：per-item 工作项 + 硬上限 B + 项级拆分，把「大 batch」从结构上取消 | 不借 Mem0 ADD-only「无 UPDATE/DELETE、只累积不覆盖」 - 它放弃覆盖式纠错，与本仓 provenance 版本链/四分支调和冲突（`competitor:235`；§6 key-supersede） |
| **显式提取失败信号**（Graphiti：schema 不合规即 extraction failure，可计数） | `competitor:122,125,186` | §8 C3 + §8.5：新增 `extraction_failure` 信号走 append-only `error_events`，空结果永不记成功 | 不把竞品召回数字当目标；不把「失败计数」做成排名奖励（防 Goodhart） |
| （参考）**crash recovery 保留已完成项** | Cognee v1.6.0 recovery（`competitor:163,187`） | 作为 C2 不变量的实现提示：已消费项幂等、崩溃后不重做已完成项 | 不引入任何云依赖 |

mempalace 的「根本没有 LLM 提取管线、原文分块 + 混合检索 + 末端有界 rerank」（`competitor:53`）是一个**更激进的替代解**：它结构上不存在空 verdict。作为 §7.3「不激活」分支的实证支持列出，但本片不把产品改成 mempalace 式架构（超出 S3 范围，且理论锚 R1/R2 要求冷层存在）。
---

## 6. 从 `11-refinement-v2.md` 重推导：保留 / 改变 / 丢弃

`11-refinement-v2.md:3` 自称「设计提案（v1 实证评估后重设计）」 - **它不是冻结设计**，所以重推导合法。下表逐项给出理由。

| 结论 | 11-refinement-v2 的原设计 | 本设计处置 | 理由（第一性） |
|---|---|---|---|
| **保留** | SessionEnd 触发、废弃池子积分（`11:29-33`） | 保留为**未来激活后**的触发候选（默认关闭） | 离线、编码之后、有界窗口忠实 R2；Stop 是热路径，不能触发（`11:31`）。**当前不接线** |
| **保留** | Phase 1 确定性零 LLM 提取（`11:35-43`） | 保留并作为**默认且唯一必开的提取层** | 零成本、全覆盖、无空 verdict 路径；直接对治 G1/G2 |
| **保留** | 接地校验：空白折叠后逐 chunk 子串匹配、片段 ≥16 字、丢弃率 >50% fail-loud（`11:49`） | 保留为 §8.3 质量门，并接 §8.5 失败信号 | 忠实 R34/R35「先验证再落库」；>50% 丢弃是可证伪的 fail-loud 阈值 |
| **保留** | key-supersede 事实版本链（以 `(subject,predicate)` 为键），明确不复用 design/12 提名制（`11:54`） | 保留 | 忠实 R5 再巩固；保住版本链护城河（§5） |
| **保留** | 快照硬上限 1MB、只存引用不存原文体（`11:55,60`）；超时/失败 fail-loud（`11:61`） | 保留并强化为 C2 不变量 | 忠实 R12；防 v1「111MB 快照」复发 |
| **改变** | 「≤8k token 默认；超限按子窗口切分多次调用，每会话调用总数 ≤4」（`11:47`） | 改为 **C1/C2 项级有界契约**：per-item 上限 B、单项超限按语义边界拆子项并回指 chunk、不变量 `已消费 ∪ 已停车(带信号) = 全部`；调用次数由工作项数而非固定 4 决定 | 固定「每会话 ≤4」在长会话上会重新造成覆盖缺失；改为「按项计数 + 全项可追责」才是防积压保证 |
| **改变** | 「不匹配即丢弃并计一次质量信号」（`11:49`，信号去向未定义） | 升级为**一等 `extraction_failure` 信号**（append-only `error_events`，`evidence_ptr`、`detector_id`、`retryable`），空结果永不提交成功（§8.5） | 忠实 G2；「计一次质量信号」若无落点就会重演「0 产出却记成功」（`pipeline.py:429-436`） |
| **改变** | Phase 2 单模型有界摘要（可选、可整体关闭，`11:45-50`） | 降为**可选且默认关闭**；只有在其产出通过 §7.2 互补性门时才可讨论开启 | §7 证明 verbatim + 确定性事实已覆盖大部分价值；摘要若与原文重复则纯烧 token |
| **丢弃** | 池子积分自动触发、score_pool 入账修复（`11:32`；`02:17`） | 丢弃 | 入账死路已在 v1 实证（`11:12`；`inv3:67`），且 owner 已关停；不修死路 |
| **丢弃** | 双席投票 / 双模型串行（`11:67`） | 丢弃 | 从未配置（645 次回退，`inv3:32,164`），2/20 接受率（`11:11`） |
| **丢弃** | 全量三元组提取（`11:66`） | 丢弃 | v1 实证无效（`inv3:46-53`） |
| **丢弃** | 「S1..S4 实现切片」的具体编号（`11:69`） | 丢弃编号，另立本批 U1..U6（见 slices doc） | 新设计门须有自己的分片与 owner 决策点 |
---

## 7. CLS 双存储：冷层存什么 verbatim 存不出什么 + 互补性质量门

### 7.1 冷层的「不可替代内容」定义

CLS 锚（R1，`00:168`）要求两轨**互补而非重复**。verbatim 轨（lancedb 向量池 + chunk）能存：原文块、时间/会话、逐 chunk 元数据。它**不能原生表示**以下三类，这正是冷层唯一合法的写入内容：

1. **跨会话的当前结构事实**：以 `(subject, predicate)` 为键、带 supersede 版本链的「当前相信什么」（例：用户偏好、项目采用的工具、错误 E 的修复 F）。单条 verbatim chunk 只是某次表述，不携带「当前版本 vs 历史版本」语义（`11:54`；R5）。
2. **显式关系边**：事实 ↔ 会话 ↔ 实体（文件/命令/工具）的图边，提供向量相似度之外的第二条遍历路径 - 向量池没有边（`11:56`；Seat B:66「图轨靠 entity 种子 + 2-hop」）。
3. **独特性排序的显著事件摘要**：错误→修复对、决策与反转、新工具首次出现（R56）；verbatim 是平坦的，无显著度分层。

### 7.2 互补性质量门（激活讨论的前提之一）

一条冷层事实只有**同时**满足以下五项，才算「互补」而非「重复」。

| QG-3 子项 | 判据 | 证据/出处 |
|---|---|---|
| 接地 | 每条事实有 `source_session_id` + chunk id 引用，**绝不内嵌正文** | `11:43,55`；R12 |
| 非重复 | 事实须聚合 ≥2 个 chunk，或表达一次版本跃迁（supersede）；仅复述单 chunk 者拒绝 | 从 §7.1 直接推导（verbatim 已存单 chunk） |
| 有用 | 人工评审 ≥20 个会话窗口，「对未来对话有用」判定 ≥ 60% | `11:62`（沿用其阈值，非新发明） |
| 接地率 | 丢弃率 ≤ 50%；>50% 判模型/提示词不合格并停 Phase 2（若启用） | `11:49`；R34/R35 |
| 召回贡献 | 在冻结的真实自然查询集上，冷层作为**第二轨**须可测地提升 recall-of-known-facts（S0 提供仪器；本片只定义冷层侧判据，不复用 #391 synthetic rig） | `design-batch-brief.md:28-44`；Seat B:14；`08-eval-harness.md:24` |

> 注：`≥60%` 与 `≤50%` 是**沿用 11-refinement-v2 的已登记阈值**，属机制层常量，可经 owner/QA 调整；它们不冒充理论锚。

### 7.3 诚实考虑「不激活」分支（必须，不得只写激活路径）

**证据支持不激活**：
- owner 已裁决关停（`inv3:64-66`；`design-batch-brief.md:68`），重启需逆裁决。
- graph 三个月 37 节点/0 边/0 回执（`inv3:28`），冷层从无可用产出的实证。
- 78 万 token 换 0 三元组（`inv3:37`），token 风险真实。
- Seat B D4 明列「诚实单轨改述」为合法选项（b），且禁止半死静默（c 不可接受，`first-principles-kernel-gap.md:111,121,202`）。
- mempalace 一手实证：无 LLM 提取管线也能工作（`competitor:53`） - 冷层不是召回质量的必要条件。

**不激活的代价**：失去 R1/R2 的双轨冗余；cue 失配时无图轨兜底（Seat B:66）；但 verbatim 轨仍在，且诚实改述后内核契约自洽。

**结论与建议**：本片**不推荐在任何质量门成立前激活**。默认路径 = 完成 U1-U4（契约、信号、互补性判据的可测化）+ U5 的诚实边界文档；把激活（OD-1）与「复活 vs 单轨改述」（OD-2）交 owner。这样无论 owner 选哪条，都不会因为本片而被动重启旧路径。
---

## 8. 有界输入契约（详细机制层，全部可改但须守住 §1.1 三行）

### 8.1 工作项与上限 B

- 工作项 = 一个 per-item / per-episode 单元（对应 verbatim chunk 或语义完整的 episode），由确定性 packer 产出（`delta.py:208-237` 的整块不切文中语义保留）。
- 单次提取调用只喂一个工作项；其渲染 token 估算复用本地确定性 `estimate_tokens`（`02:175`：CJK 1 token/字 + 其余 `ceil(chars/4)`，零网络）。**B 的具体值 `[未验证]`，须真语料标定**（建议起点 ≤ 8k，与 `config.py:991` 现值无关 - 那是旧批量上限，不是新工作项上限）。
- 每会话调用数 = 工作项数（+ 拆分产生的子项数），**不设「≤4」的人为天花板**（§6 改变项）。

### 8.2 超限处理（C2 的不变量）

- 若一个工作项渲染后 > B：按语义边界（句号/段落/chunk 内自然断裂）拆为子项，每个 ≤ B；子项 `evidence_ptr` 回指源 chunk id，保证可追责。
- **绝不切文中**：沿用 `delta.py:210,256` 的 never-split-mid-text 纪律；拆不动的原子项进入「已停车项」集合并**必须**带失败信号（C3），不许变成无信号的 empty delta（`delta.py:257-260` 的旧洞）。
- 不变量（可测）：`consumed ∪ parked_with_signal = all_items`，且两集合不相交。任何违反 = 测试红。

### 8.3 接地校验（保留 11 的确定性规则）

- 源文片段与目标文本空白折叠后，在**单个 chunk 内**做子串匹配（绝不跨 chunk 拼接）；片段 ≥16 字才参与（`11:49`）。
- 不匹配 → 丢弃该条并触发 C3 信号；丢弃率 >50% 的运行判不合格并 fail-loud（`11:49`；QG-3 接地率子项）。

### 8.4 写入与版本链

- 落图事实带 `source_session_id` + chunk 引用，不内嵌正文（`11:55`）。
- 版本化走 key-supersede：`(subject,predicate)` 同键新证 → 旧条降级历史；不同键 → 新增（`11:54`）。**明确不复用 design/12 的 nominate/adjudicate/merge 管线**（`11:54`）。
- 图边补全：事实 ↔ 会话 ↔ 实体建边（`11:56`），使 §7.1 的第 2 类内容真正可遍历。

### 8.5 显式失败信号（C3 的落地形状）

沿用 append-only `error_events` 先例（`storage/ports.py:565-579`；`_migrations.py:509-562`）：

- **新增保留成员**：`ErrorSignalType.EXTRACTION_FAILURE = "extraction_failure"`（现枚举仅 `USER_CORRECTION/EVENT_OUTCOME/COMPOSITE/PUBLISHED/PROVIDER_FAILURE`，`ports.py:517-532`）。**新增成员而非复用 `provider_failure`** - 提取失败不是 provider 错误。
- 每行字段（沿用既有 schema）：`profile_id`（必填、不猜）、`observed_at`（单调）、`evidence_ptr`（`evidence_kind=chunk|session`，`evidence_id`=源 id，只引用不判正确性，`ports.py:550-558`）、`session_id`、`turn_range`、`detector_id`（命名确定性 detector）、`retryable`（是否可重试）；append-only 由 BEFORE UPDATE/DELETE 触发器保证（`_migrations.py:544-562`）。
- **语义**：0 事实 = 失败事件，不是成功；schema 不合规 = 失败事件（Graphiti 先例，`competitor:122,186`）；停车项必须有对应信号行。
- **不污染排序**：失败信号只进账本，不改变召回排名（S1 的同纪律）；不得把失败计数做成奖励。
---

## 9. 触发与生命周期（仅设计，未接线）

- 未来激活时：SessionEnd → 一个整理窗口 → U1 确定性提取（必开）→ U3 可选摘要（默认关）→ §7.2 质量门 → 写图 + 显式失败信号。
- 排队窗口上限与陈旧告警沿用 `11:33`（≤8，>7 天告警，永不自动放大批量）。
- 当前状态：**以上全部未接线**；OD-1 未决前 `auto_trigger` 保持 false（§3）。

---

## 10. 激活质量门总表（激活「可讨论」的前提，全部成立才进入 OD-1）

| 门 | 判据 | 绑定证据 |
|---|---|---|
| QG-1 | §8 有界输入契约实现且测试证明 C2 不变量（无静默丢弃、无切文中空 delta） | `delta.py:255-260`；§1.1 |
| QG-2 | §8.5 显式失败信号可写可查；「0 产出却记成功」路径被测试杀死 | `pipeline.py:429-436`；`competitor:122,186` |
| QG-3 | §7.2 互补性五项全过（接地/非重复/有用/接地率/召回贡献） | `00:168`；`11:49,62`；S0 仪器 |
| QG-4 | verbatim 红线保持：不内嵌正文、append-only provenance、无声≠已用 | R12/R7 `00:159,164`；`11:55` |
| QG-5 | 每 run token 预算有界且可观测（有界输入使 78 万 token 空转不可复现） | `inv3:37`；`02:175,178` |
| QG-6 | owner 明确重授权（OD-1）：承认或推翻 #294 的 `auto_trigger=false`（audit 505 / 7000005） | `inv3:64-66`；`design-batch-brief.md:68-70` |

> QG-1..QG-5 是技术门，可由设计冻结后的工程+QA 证明；QG-6 是治理门，**只能由 owner 给出**，本片不代答。
---

## 11. 架构师自审（issues by severity；claim - evidence - 最小调整）

> 本片是 authoring 设计文档，以下为「若规格这样落地会出什么错」的对抗清单，developer 实现时按此消项。

### D1（BLOCKER） - 激活被预先做出
- **Claim**：任何切片若把 `auto_trigger` 设 true / 改 `config.py:991` 默认值，即后门重启 #294 已关停路径。
- **Evidence**：`inv3:64-66`（audit 505/7000005）；`design-batch-brief.md:68-70,82`。
- **最小调整**：所有门默认 OFF；OD-1 未决前零配置写入；U5 文档明确「激活属后续决定」。

### D2（BLOCKER） - 静默空 verdict 以更小规模复发
- **Claim**：有界调用返回 0 事实却提交「成功 + 0 产出」，等于重演 INV-3。
- **Evidence**：`inv3:49-50,161`；`pipeline.py:429-436`；`competitor:122,186`。
- **最小调整**：C3 强制信号；空结果不得推进水位；写入 QG-2 测试。

### D3（BLOCKER） - 有界输入仍有静默丢弃洞
- **Claim**：单项 > B 若走旧 `pack` 裁成空 delta（`delta.py:257-260`），会重现「提取器拿到被截断输入」。
- **Evidence**：`delta.py:208-237,255-260`；`config.py:977,991`。
- **最小调整**：C2 项级拆分 + `consumed ∪ parked_with_signal = all` 不变量测试；`[未验证]` 的 B 值须标定后冻结。

### D4（IMPORTANT） - 冷层互补性未定义导致「激活了重复层」
- **Claim**：若冷层只复述 verbatim，激活只是增加成本与 token 风险。
- **Evidence**：`00:168`；`first-principles-kernel-gap.md:66,93`；`inv3:28,37`。
- **最小调整**：§7.2 五项互补门 + §7.3「不激活」默认分支。

### D5（IMPORTANT） - 理论锚状态被偷升
- **Claim**：R56 若写成 ✅ 即违反理论锚纪律（本轮未直接核验）。
- **Evidence**：`REFERENCES.md:125`；`11:22`。
- **最小调整**：全文保持 ⚠️，作为排序偏好非硬门。

### D6（IMPORTANT） - ADD-only 语义被夹带
- **Claim**：借「有界小输入」时若连「只增不改」一起借，会摧毁 provenance 版本链/四分支调和。
- **Evidence**：`competitor:235,107-109`；`11:54`。
- **最小调整**：§5/§6 显式排除；key-supersede 保留。

### D7（IMPORTANT） - 失败信号 schema 处理错误
- **Claim**：复用 `provider_failure` 或改既有枚举值会污染账本语义/破坏 append-only。
- **Evidence**：`ports.py:517-532`；`_migrations.py:509-562`。
- **最小调整**：新增保留枚举成员；触发器不动；`evidence_ptr` 只引用不判定。

### D8（MEDIUM） - 跨片依赖被忽略
- **Claim**：互补性门 QG-3 的「召回贡献」依赖 S0 的真实查询仪器；若 S0 未冻结就激活，会落入 #391 量纲错位。
- **Evidence**：`design-batch-brief.md:28-44`；`first-principles-kernel-gap.md:14`；`08-eval-harness.md:24`。
- **最小调整**：slices doc 标注 U4 依赖 S0 仪器；不复用 #391 rig。

### D9（MEDIUM） - 半死静默态残留
- **Claim**：若最终不激活却仍宣称 dual-store shipped，是不诚实边界。
- **Evidence**：`first-principles-kernel-gap.md:111,183,212`。
- **最小调整**：U5 文档把 CLS 锚点标 as-is boundary；排除「维持半死」选项。
### 11.1 重大决策的选项空间（不预先拍板，供 owner 选择时可见）

1. **立即激活 vs 设计门先行、激活延后** - 推荐后者（QG-1..6 未成立，且逆 #294）。代价：冷层继续死到 owner 裁决。
2. **修旧 v1 分批参数 vs 新设计门** - 推荐新设计门（旧路径结构性失效，`inv3:46-53`；改参数不治本）。代价：工作量更大。
3. **借 ADD-only vs 只借有界输入** - 推荐只借有界输入（保版本链，`competitor:235`）。代价：不能享受 Mem0 的「无覆盖开销」。
4. **全量三元组 vs 确定性优先 + 可选摘要** - 推荐确定性优先（`inv3:46-53`；`11:35-50`）。代价：冷层产出较薄。
5. **复用 provider_failure vs 新增 extraction_failure 成员** - 推荐新增（语义正确；`ports.py:517-532`）。代价：一次 append-only schema 变更。
6. **复活冷层 vs 诚实单轨改述** - 两者都合法（Seat B D4 a/b）；**默认「不激活」直至 QG 成立**，最终由 OD-1/OD-2 决定。代价：需 owner 时间。

---

## 12. Owner 决策点（本片交出去，不代答）

- **OD-1（激活问题）**：在 QG-1..QG-5 全绿后，是否授权逆转 #294 的 `auto_trigger=false`（audit 505 / version 7000005）并进入冷层激活工程？ - **本片默认答案：否，直到 owner 明确说是。**
- **OD-2（冷层命运）**：复活精炼（新设计）vs 把内核如实改述为 verbatim-only + explicit-pin 单轨？禁止半死静默（Seat B:111,202）。
- **OD-3（互补性阈值）**：§7.2 的 `≥60%` / `≤50%` 与召回贡献阈值是否采纳为冻结判据（沿用 `11:49,62`），还是由 owner/QA 重标？
---

## 13. 证据清单（file:line 或 `[未验证]`）与信息时效

**证据（产品行为逐条绑定）**：
- INV-3 根因/日志/DB：`org/workspace/recall-quality-review-20261122/inv3-graph-scorepool.md:25-37,43-70,74-79,113-175,193-197`（逐字日志 + DB 副本，2026-11-22）。
- 旧提案：`docs/zh/design/11-refinement-v2.md:3,6-13,15-25,29-74`（2026-10-28 评估后提案）。
- 机制源码：`config.py:254,708-710,977,991`；`daemon/app.py:917`；`dream/delta.py:208-284`；`dream/pipeline.py:350-444`；`dream/verify.py:224-227,290`；`storage/ports.py:517-558,565-619`；`storage/drivers/_migrations.py:509-568`。
- 架构/理论：`docs/zh/design/00-overview-and-philosophy.md:159,161,164,168-169,171,181`；`docs/zh/design/REFERENCES.md:17,18,20,125`；`docs/zh/design/02-dream-engine.md:17,23,115,130-131,168-175,192`；`08-eval-harness.md:24`（经 brief:40 引用）。
- 竞品：`org/workspace/kernel-first-principles-20261122/competitor-memory-mechanisms.md:53,107-125,163,181-191,235`（一手 README，访问 2026-11-22；机制 VERIFIED，benchmark CLAIMED）。
- Seat B：`org/workspace/kernel-first-principles-20261122/first-principles-kernel-gap.md:14,23,34,65-66,89-94,111,121,142-143,153,183,202,212`。
- 批次纪律：`org/workspace/kernel-first-principles-20261122/design-batch-brief.md:6-12,26-93`；`brief.md:7-24`；`org/PROTOCOL.md:176-191`；`AGENTS.md:76-79,81-93`。
- `[未验证]`：34k-token 单 chunk 现值（`inv3:33,52,187`）；B 的具体取值；8 snapshots 的归档位置（`design-batch-brief.md:68` 仅述事实）；任何未来激活后的 recall 收益（未测）。

**信息时效（key-source recency）**：
- INV-3 / 竞品机制 / Seat B / brief：均 2026-11-22 一手产出，**< 90 天**，无豁免需求。
- `11-refinement-v2.md`（2026-10-28 评估后提案）与 #294 裁决（2026-10-28）：约 25 天，**< 90 天**。
- 理论文献：McClelland 1995 / Wilson & McNaughton 1994 / Tulving & Thomson 1973 / von Restorff 1933 / Hunt & Worthen 2006 均 > 90 天但为**经典实证文献**，状态照抄 `REFERENCES.md` 已登记状态（R1/R2/R4 ✅；R56 ⚠️），不重新核验、不升级 - 理论锚固定、不复议（brief:22-24）。
- 竞品厂商 README 为快照（访问 2026-11-22）；benchmark 数字标 CLAIMED，随版本变化，属快变领域，引用时「as of 2026-11-22」。

**置信度**：**高**（根因、#294 对账、有界输入/显式失败契约均逐字节绑定一手证据）；**中**（互补性阈值与激活收益尚未经真语料测量，`[未验证]` 已标注）。

**建议（下一步）**：请独立设计 QA 审本片与 450-/451- 两片；QA `CLOSABLE` 后冻结，再交 owner 就 OD-1/OD-2/OD-3 裁决。**在 owner start 授权前不得进入任何工程派发**（`design-batch-brief.md:7-12`；`org/PROTOCOL.md:186-188`）。