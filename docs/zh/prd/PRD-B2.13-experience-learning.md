# PRD · B2.13 体验学习管线（错误事件账本 + dream 蒸馏，E0–E4）

> 依据：
> - GitHub issue #113（2026-08-25 立项）：关闭 Q3 问题（"RW outcome-feedback loop"）的更大计划——**体验学习管线**：记忆从错误中学习，使 agent 不再重犯。Owner 要求：用户反馈与事件结局同为学习信号；错误/未证实事实一旦被纠正必须被记住且不重复。处理拓扑已定案：**错误事件账本 + dream 批量蒸馏**（实时在线学习被否决：违反捕获中立 + token 经济；检索侧调制仅属投递面）。
> - 战略注记：本批翻转 gap matrix #13（竞品 Mnemoverse 经显式 `memory/feedback` API 做 RW 重排序；我方纯派生信号——对方缺全量 verbatim 捕获，结构上不可能跟进）。决策记录：marketing repo `战略讨论纪要-2026-08-25.md` §7。
> - GitHub issue #117（2026-08-25 事故）：会话开场陈旧队列状态被用于规划回答、会话中段捕获项两次漏读——该错误类是本管线 C 型检测器的种子工作例，亦是 E4 测量回路的首个测量对象。
> - 排期门：E0（本文档 + 锚登记）立即执行；E1+ 一律 gated behind #75 观察钟结果。

## 理论锚

本轮 E0 已在 `docs/zh/design/REFERENCES.md` 登记 R49–R53（新设「经验学习与强化」节）；**这些条目即本 PRD 的理论锚**。按 issue #113 指示，在 TA-10 编号冲突修复前**不新分配 TA 号**，以 R 号引用：

| 锚 | 来源 | 已验证规律 → 本管线设计规则 |
|---|---|---|
| R49 ✅ | Rescorla & Wagner (1972) | RW delta rule：预测误差驱动联结强度更新 → 错误事件按意外度晋升记忆修正（promotion thresholds）；可证伪预测：负性结果剂量 vs serve-rate 下降呈单调剂量-反应 |
| R50 📕 | Sutton & Barto (1998) | eligibility traces：结果只归因于近期参与的候选 → outcome→memory 归因的有界回看窗口（KISS 形式；缺省值 mark-as-is + T4 式遥测后校准） |
| R51 📕 | Schank (1982) / Kolodner (1993) | 案例推理：具体案例泛化为可复用教训 → lesson chunk artifact（`EVIDENCED_BY` 边回指原案例） |
| R52 ✅ | Gollwitzer (1999) | 实现意图：if-then 配对显著提升执行率 → INTENTION 节点 `{trigger_condition, action, status}`（schema 冻结于 `src/mnemoseed_local/schema/graph.py:147`） |
| R53 ✅ | Anderson (1982) | 知识编译：陈述性规则经练习自动化为程序性技能 → lesson/rule 编译为 SKILL_SEQUENCE（冻结于 `graph.py:150`） |

明确不入册（理由见 REFERENCES.md 同节未入册说明）：Fitts & Posner（描述性分期，未达可证伪门槛）、regret/counterfactual（已被 R7/R34/R35 覆盖）、Von Restorff（暂缓——竞品签名词 + 捕获中立风险，缓办备忘在册）。

## 产品北极星（owner directive 2026-08-25）

> 记忆必须在无指令文件依赖下工作——AI 自发回忆是产品承诺，AGENTS.md 这类指令文件绝不能成为回忆通道。

成熟度阶梯（#117 产品定位定案）：

- **L0** instruction-driven recall——指令文件驱动的回忆纪律（拐杖，正在拆除：#117 已移除 AGENTS.md 会话开场召回条款）
- **L1** 自动会话开场注入（T1，已发布，尚不可观测）
- **L2** 会话中段自动召回（T2，已发布，尚不可观测——观测探针已纳入 #75 观察窗）
- **L3** 系统从错误中学习该记什么、该提示什么——**即本批 #113**

「陈旧队列回答」这一错误类属于 L3（#113），不属于任何指令文件。本 PRD 是 L3 的立项文档。

## 硬约束红线（六条，自 issue #113 逐字继承）

> 1. Derived signals only; **no new agent-facing feedback API**.
> 2. **Zero model calls on the hot path** — LLM adjudication happens only in the dream pipeline.
> 3. Outcome never feeds `provenance.confidence` (mirror of the emotion red line, `stamper.py` comment).
> 4. Detectors **nominate, never adjudicate** ——话语标记 FP 面大; ledger entries are nominations, dream LLM rules truth against context.
> 5. Verbatim red line intact: lesson artifacts carry `EVIDENCED_BY` edges back to source chunks; lessons never replace originals.
> 6. Ledger pointers share fate with their evidence chunks (no dangling pointers after safe-clear).

## 信号源（全部派生，无新增 agent 面 API）

- **A 型（用户纠正标记）**：user_prompt verbatim 上的确定性双语正则——「不对」「错了」「不是这样」「应该是…才对」「重新/revert」；"no, …"、"that's wrong"、"actually…"。rules-as-data，循 `rulesets_v1.py` 先例。仅提名。
- **B 型（事件结局）**：非零退出码、`error TS*`、Traceback、test FAILED 行；edit→revert 链；red→green 序列；同命令带参数变更的重复失败。
- **复合信号**：T3 注入但未被消费 ∧ 同会话稍后出现 A 型标记 = 强归因信号。
- **已发布子集**：`needs_reconcile` 冲突事件今天就是错误信号（FR-1.8 池加成已在快车道它们）。
- **eligibility window**：有界回看，把结局归因到最近被服务过的记忆/动作（R50 eligibility traces 的 KISS 形式）。缺省值 mark-as-is 入账，后续按 live-telemetry 校准（T4 先例）。

## 范围（批次定义，E1–E4）

### E1 · 错误事件账本

Append-only 派生注释行 `{profile_id, session_id, turn_range, detector_id, eligibility_tag, evidence_ptrs}`。确定性检测器、零模型调用。持久化模式循 merge salvage queue 先例（`merge.py:313`）。wire 格式不变。账本行 profile-scoped（依赖 #109 的 profile 绑定落地；行模式保留 `profile_id` 字段 ≠ 绑定语义——E1 无论 #109 进度如何一律先保留该字段，绑定语义归 #109）。

**字段保留注记（#109）**：当前运行时只有隐式 profile、无生命周期管理面——env 变量已可产出 N 个隐式隔离命名空间，缺的是生命周期/绑定层，而非隔离本身。`profile_id` 现在（E1 落地时）就进入行模式——账本行生而 profile-scoped，#109 多 profile 运行时落地后无需任何迁移，也不另设第二套作用域机制。这与 #109 已核验的「每一层显式 `profile_id`、required and never guessed」既有不变量同构。

### E2 · dream 第二提取通道（Q3 在此落地）

Dream LLM 对提名账本行做裁决 → 经既有 verify/vote 质量门 + 预算泛化为工件：

1. **Scoped standing rule**（B2.7 RecallRule 通道，provisional ttl）——需 RecallRule kind 的 additive Literal 扩展（现冻结于 `ports.py:38`）→ 单独契约评审；需定义旧 daemon 遇未知 kind 的行为。
2. **INTENTION 节点**（if-then 守护计划，R52 锚）。
3. **Lesson chunk**，`provenance.source="dream.lesson"` 读侧派生（design/09 §3.1 先例）+ `EVIDENCED_BY` 边。
4. **负向侧**：corrected-memory downweighting，对齐主仓 FR-4.10，走版本链（绝不改写历史）。

**技能形态保留区（本设计文档内一页，不排期）**：dream 输出未来可序列化为 skill 候选链——lesson → scoped rule（认知层）→ INTENTION（联想层）→ 编译为 SKILL_SEQUENCE（自主层，R53 锚）→ USED_IN 强化 / λ-decay 兜底 → SUPERSEDES 演化。激活时的硬前置：promotion gate 实现（`PromotionStatus.QUARANTINED` 有字段无逻辑，`graph.py:53-64`）+ 来源归属对称不变量（镜像 `reflect.py:1026`：skill/lesson 节点永不得渲染为用户语句）。

**同族机制协调（#123，红线级注记）**：读路径冲突标记 → dream 侧和解（#123）与本管线同族——底层问题同为「结局改变记忆地位」：#123 的读侧可逆 flag 与本管线的 `needs_reconcile` 错误信号最终汇入同一条 dream verify/vote 裁决流。两批必须共享同一裁决与降权机制（corrected-memory downweighting 一律走版本链），**不得各建一套分叉机制**。排期上 #123 依赖 B5 vote 门；后落地一方复用先落地一方的裁决通道，不重复设计。

### E3 · 投递面

B2.7 式预算内注入块 + 围栏 + 守卫 fail-open 直通；lesson 的 recall 呈现；console 可见性（过滤项 design/07 已有）。**捆绑条件（红线级）**：单项 disable/forget 工具必须与本投递面同一批次交付——控制叙事依赖它成立。

### E4 · 测量回路

Eval harness 复发臂：构造错误材料（context C + 错误动作 A + 纠正）→ 后续会话呈现 C′≈C → 度量 P(repeat A|C′)。门槛：RR = P(on)/P(off) ≤ θ **且** false-guard rate ≤ ε（Detector-FP 校准形式，PRD-B2.1-T4）。held-out 措辞/标记变体强制（T4b 过拟合教训）。live telemetry：needs_reconcile 复发计数、纠正标记密度、guard 注入后 T3 消费命中率。

## 保留区：C 型过程遗漏检测器家族（源自 #117 architect review，一页，不排期）

- **触发条件（全部满足才提名）**：planning-surface 回答轮 ∧ 存在与回答主题重叠的会话中段捕获项 ∧ 捕获与回答之间无 queue-requery 工具调用。
- **处置**：仅提名（nomination only），dream 裁决——检测器永不裁决（红线 4）。
- **风险如实声明**：话语类 FP 面大于代码错误类；排期在 E1 之后，过同一 #75 门。
- 该家族覆盖的错误类即 #117 种子工作例（见下节）。

## 种子工作例（E4 首个测量对象，源自 #117）

2026-08-25 事故：context C =「what follow-up plans are scheduled」（规划面提问）；action A = 从会话开场的陈旧 issue 列表作答两次，而 #113/#109/#110 已在会话中段捕获（其中一项与所问问题主题直接相邻：学习改进 vs #113 体验学习管线）；owner 观察到并纠正。管线落地后，P(repeat A|C′) 即成为可度量对象——这正是 C 型家族的目标错误类，也是 L3 成熟度的第一个量化样本。

## 排期与门

- **E0 立即执行**（docs-only，无 gate 需求）——本文档 + REFERENCES R49–R53 登记即为 E0 全部交付物。
- **E1+ 一律 gated behind #75 结果**，观察数据到位前不锁定设计承诺。
- 开场即记录诚实预期：批量学习延迟 = 分钟–小时级（dream cadence）；"learned" 的定义 = 复发率统计显著下降，**永远不是零复发**。
- 叙事纪律：E4 数字落地前，对外表述限于痛点框架（见 纪要 §7.5 tiering）。

## 出界（v1 明确不做）

- 实时在线学习 / hot path 上任何模型调用（红线 2）。
- 新增 agent 面 feedback API 或任何用户显式反馈通道（红线 1；检索侧调制仅属投递面）。
- 结局信号写入 `provenance.confidence`（红线 3）或以任何形式改写历史（downweighting 只走版本链）。
- 教训工件替换原始 verbatim chunk（红线 5）或留下悬空指针（红线 6）。
- skill 形态链的实际实现（保留区一页而已，前置未备）。
- RecallRule kind 扩展随本批顺手做——单独契约评审。
- 自动刷新机制、写宿主私有配置存储、新增用户诊断配置面（#117 scope guards 同样约束本批）。

## 相关

- #109（profile 绑定——账本行 profile-scoped 的前置，见 E1 字段保留注记）
- #123（读路径冲突标记 → dream 侧和解——同族机制，裁决流必须合一，见 E2 协调注记）
- #105(originating agent——A/B 归因受益方)
- #75(观察门)

## 批次执行记录

- **E0 里程碑（2026-08-25 立项即收口，squash `2e3b63f`/PR #119 → issue #113 的 E0 部分）**：R49-R53 理论锚入册（Rescorla-Wagner 1972 提升阈值 / Sutton & Barto 1998 eligibility traces 有界归因窗 / Schank 1982+Kolodner 1993 案例推理 lesson 工件 / Gollwitzer 1999 实现意图 INTENTION 节点 / Anderson 1982 知识编译 SKILL_SEQUENCE），全部在线核验引文；明确不入册清单。同 PR 移除 AGENTS.md 手动回忆纪律。

- **E1 ledger 脚手架（2026-08-28 收口，squash PR #150 → issue #113 的 E1 部分；issue 整体保持开放至 E2+）**：架构师判定 E1 信号无关 plumbing 可提前启动（检测器选择仍待 #75 P4 误差类频次）。交付：`ErrorEvent` 行（profile_id 恒存 + 单调时间戳 + 可扩展 `ErrorSignalType` 族 + `EvidencePointer` 仅引用不判定，与 #123 `read_conflict_id` 收敛）；`MetaStore.append_error_event`/`query_error_events`；迁移 v11 独立 `error_events` 表 + append-only 触发器（audit_log 先例）。六条硬红线全守：零模型调用、仅派生信号（无反馈/纠错 API）、无检测器（`detector_id` 预留 NULL）、verbatim/capture 零接触、无 HTTP 暴露。QA CLOSABLE（0 BLOCKER）；遗留 hygiene 项入 #151（单调性强制、0.0/空白 guard 边、COMPOSITE 双源表示、红线 #6 悬空指纹耦合——皆 E2 门控）。门禁 1893 passed / 5 skipped。
