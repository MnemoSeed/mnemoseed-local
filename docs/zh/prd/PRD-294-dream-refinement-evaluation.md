# 294 · 梦境引擎（AI 记忆精炼）独立 QA 复评与决策文档

- 日期：2026-11-18
- 席位：senior-qa-reviewer（独立对抗复评；与实现席位、原评估作者均不同席）
- 对应 issue：#294（Founder 2026-10-28 指令：核心基础，须谨慎评估后再投入）
- 基线：8a9a115（分支 docs/294-dream-evaluation）
- 性质：只读证据复评 + 决策建议。未改任何产品代码、配置、宿主、skill、在线数据或 PROTOCOL；所有数据库以 mode=ro 打开。本文为新增文档，不改写任何历史记录。
- 证据分级：[实测] 本轮亲自只读复测；[代码] 源码/提交可复核；[设计声明] 文档记载但本轮未找到一手测量物；[假设] 逻辑推断，未证实。

## 1. 结论（先给答案）

**建议 REPLACE（替换）：用 docs/zh/design/11-refinement-v2.md 的确定性优先设计替换 v1「本地小模型 + 无限流 + 三元组图提取」精炼路径；过渡态 NARROW：保持现网 dream.auto_trigger=false，只保留经实证在工作的捕获 + bge-m3 向量召回 + 会话注入三层，精炼代码冻结不删。**

理由压缩成一句：当前精炼层在工作负载上从未产出与成本相称的价值，且已结构性停摆；owner「完全感受不到记忆」的抱怨指向的其实是召回层，而召回层与精炼层无关。该结论与原 2026-10-28 评估一致，但本轮独立复测提供了新的证据，并对原评估中两处证据薄弱项做了降级（见第 5 节）。

owner 已于 2026-10-28 裁定「采纳 A + 重设计」（issue #294 评论），v2 设计已由 PR #298（提交 cdb9124）落盘；实现按 S1 至 S4 另开批次，尚未落地。本文给出放行的 QA 条件与否证器（第 8 节）。

## 2. 复测证据（2026-11-18，全部只读）

| 指标 | 本轮实测 | 一手来源 |
|---|---|---|
| dream 运行总数 | 30 | meta.db:dream_runs count |
| 图沉淀 | 37 节点 / 0 边 / 0 调和回执 / 0 提名 / 0 调和尝试 | graph.db:nodes/edges/reconciliation_receipts；meta.db:reconcile_nominations/reconciliation_attempts |
| 精炼 token 用量 | 约 784k（2026-08 为 757,079；2026-10 为 26,872） | meta.db:dream_token_ledger |
| 最后一次运行 | 2026-10-25 08:16:22Z 至 08:22:25Z（363.5 秒），qwen3.5:9b，26,872 token，turn 0 至 4829 | meta.db:dream_runs 最新行 |
| 积分池余额 | 0.0；filed_points_total 约 14,872；入账窗口 5 至 27，最后事件窗口 5 至 27 | meta.db:profile_score_pool 的 default 行 |
| 错误账本 | 195 条，全部 signal_type=provider_failure；按模型 top：muse 63、mimo 46、gpt-5.6-luna 22、hy4 11（top10 无 qwen3.5:9b） | meta.db:error_events group by |
| 快照目录 | ~/.mnemoseed-local/dreams/ 空（10-28 已归档至 org/evidence/dream-archive-20261028/） | 目录列举 + 归档列举 |
| 归档快照内容 | 8 个文件：3 个小运行（1 至 4 chunk）产出 2 / 6 / 8 条三元组；其余 5 个大运行（含 11,428 chunk 的 111.3 MB 文件）全部 0 条 | 逐文件解析 reflect_result.triples |
| 三元组质量抽样 | scheduler cadence / value / 60 seconds（0.9）、dream_runs 表 / 字段 / model_id,finished_at,tokens（0.95）、uvicorn traceback / outputs to / stderr（0.88），均为 mnemoseed 自身开发对话的自指琐事 | 同上（归档 64cb、c83d、a0c9） |
| 现网配置 | auto_trigger=false、reflect_batch_max_tokens=8000、floor_pool_points=1.0；dream 路由 qwen3.5:9b（ollama，num_ctx 36864，timeout 300s，think=false）；verifier gemma4:e4b；无 dream_vote 路由、无 ensemble 键（默认 off） | live config.toml:81-98,116-121（只读） |

补充实测口径说明：

- 2 / 6 / 8 条三元组是本轮逐文件解析 reflect_result.triples 得到，与原评估「8/17 三次小运行产出 2 至 8 条」一致；本轮为独立复现，非转述。
- 111.3 MB 文件（5e9c1f82，对应最后一次 run）内嵌全部 11,428 个原始 chunk 正文，产出 0 条三元组。快照无大小守卫这一结构性缺陷有了一手物证。
- dreams/ 为空是 10-28 归档动作的结果；这也意味着当前任何检查 dream 产出的运维动作都看不到历史样本，只能查归档。

## 3. 结构性代码证据（file:line）

1. 不可分批的真实材料：config.py:121 批量上限默认 8000 token，delta.py:248-268 的 plan_batches 明确规定单个超过有效上限的 chunk 仍独占一批、绝不切分文中。因此真实达 34k token 的单 chunk（原评估记录 doctor 告警 34407 大于 16384）不可能装进 8000 的批，溢出或超大停放是结构性必然，不是偶发。[代码]
2. 无路由即不运行：config.py:473 出厂 DEFAULT_LLM_ROUTES 为空字典；readiness.py:75-87 对无 dream 路由直接判 not-ready，detail 为 no dream model configured（:30）。当前 live 配置虽已绑定 qwen3.5:9b，但 auto_trigger=false 仍关掉自动触发。[代码 + 实测]
3. 双模型串行名存实亡：config.py:431-440 定义三角色；daemon/app.py:128-133 与 230-248、cli.py:1047 与 1084 均规定 dream_vote 无专用路由时回退到 dream_verifier 路由（live 为 gemma4:e4b），且 ensemble 默认 off。结论：vote 从未启用；即便启用，B 席也不是独立第二生成器，而是 verifier judging 路由的复用。原评估「实为单模型」方向正确，精确表述应为 A 恒为 qwen3.5:9b 单生成器，B 席从未真正参与。[代码 + 实测]
4. 快照内嵌正文、无大小守卫：snapshot.py:62-78 的 SnapshotChunk 携带完整 text 字符串（:73）；本轮在 snapshot.py 中未检出任何 size/MB/cap 守卫（对照 v2 设计 11-refinement-v2.md:60 新增 1MB 硬上限）。[代码]
5. 调和管线从未运行：design/12-reconciliation.md 对应的 reconcile_nominations、reconciliation_attempts、reconciliation_receipts 三表本轮实测均为 0。[实测 + 代码]
6. 理论锚本身诚实，但未被实现兑现：02-dream-engine.md:120-138 的 CLS、海马重放、重建失真、triage 锚均标注来源与状态，且 :137 明说自报 confidence 不作校准真值、自信的幻觉照样穿过，:138 明说同源系统性幻觉投票无效。这些自我披露与实测吻合：抽样三元组 conf 0.85 至 0.95 却无用户价值，正是该边界的现实化。[代码 + 设计]

## 4. 成本与收益核算

- 成本侧：约 784k token 精炼用量；单次真实规模运行 6 分钟起（最后一次 363.5 秒 / 26,872 token）；qwen3.5:9b 为 6.6 GB 模型（live 配置注释）；磁盘上留下 111 MB 单快照；管线 30 次运行跨约 3 个月且最终停摆。
- 收益侧：累计 16 条三元组（2 + 6 + 8），全部来自 1 至 4 chunk 的小运行且是自指琐事；所有真实规模运行 0 条；图 0 边、0 调和；没有任何可归因于精炼层的召回改善测量。issue #294 指令 #2 要求的 before/after 测量从未执行。
- 确实在工作的层：捕获 hook 全线活跃（hosts/claude_code/events.py:320 与 330、hosts/codex_cli/events.py:161 与 181、hosts/common/daemon.py:344 的 session-start 与中段注入及 pending recall 拉取）；bge-m3 本地嵌入模型在 live models/bge-m3。owner 能感受到的记忆来自这一层。[代码 + 实测]
- 净判断：精炼层当前的边际收益在数据上不可见（无测量）、在样本上近零（16 条琐事）、在结构上是负资产（停摆 + 111 MB 病态快照 + 无限流积压）。这不是收益小，而是证据不足加已损坏。

## 5. 对原 2026-10-28 评估的对抗性复核

站得住、且本轮复现的部分：产出历史与质量抽样、37 节点/0 边、管线停摆、111 MB 无守卫快照、vote 从未配置、捕获加召回才是价值来源，均被本轮一手证据独立确认（第 2 节）。

需要降级或修正的部分（QA 发现）：

1. 162 个 provider_failure 信号推论出 LLM 路由长期缺氧：这是[假设]，证据链不成立。本轮实测 195 条错误全部是 provider_failure，但 top 模型是 muse、mimo、gpt-5.6 等主控或 agent 宿主模型，top10 中没有 dream 路由的 qwen3.5:9b。该账本横跨整个系统，不能据此宣称 dream 路由被限流。精炼层无产出的主因是结构性（不可分批 + auto_trigger 关闭 + 无图消费端），不是 provider 配额。建议把 provider 缺氧从事实降级为未被证实的旁因。
2. 入账水位冻结在回合 4（最后事件回合 4829）：本轮读到的是 profile_score_pool 窗口 5 至 27、最后事件窗口 5 至 27，与回合 4 不一致。余额确为 0.0、且最后 dream run 覆盖 turn 0 至 4829，可支持后续 capture 分数未入池的方向；但具体死锁机制本轮未从代码重新推导，保留为原评估的待复核发现，不冒充已独立证实。
3. 9 月预注册观测 20 对投票仅 2 对被接受、席间一致率 90%：这是[设计声明]（11-refinement-v2.md:11）。本轮在仓库与 live eval 目录中未定位到可核对的一手测量物；eval 模块确有 VoteMetrics（metrics.py:79-131、report.py:176-190），但那是度量代码，不是该次观测的原始结果。引用时应标出处为设计文档，不写成实测。

## 6. 第一性原理与替代方案（理论锚纪律）

AI 精炼记忆能否既高效又高质量？可以，但只在满足四个前提时：输入有界（面对会话窗口而非无限流）、单模型少调用、覆盖由确定性逻辑承担而 LLM 只做压缩、有可证伪的质量闸门与产出验收阈值。v1 四条全违反；v2（11-refinement-v2.md:29-69）逐条对上：SessionEnd 窗口加排队上限 8（对应输入有界）、Phase 2 每会话最多 4 次单模型调用（对应单模型）、Phase 1 零成本确定性提取全量覆盖（对应确定性覆盖）、接地校验丢弃率大于 50% 判失败加至少 20 窗口人工有用率 60% 才接主图加 1MB 快照上限（对应质量闸门）。方向是对的。

| 方案 | 内容 | 代价 | 收益判断 |
|---|---|---|---|
| KEEP 保留 v1 | 重开 auto_trigger、修 score pool | 继续 6 分钟每次、无限流、低价值三元组 | 否，一手证据已否证规模产出 |
| NARROW 过渡态（已采纳） | 维持 auto_trigger=false，保留捕获加召回加注入，冻结 dream 代码 | 零运行成本，图冻结现状 | 是，owner 已经感受到的记忆不受影响 |
| REPLACE 推荐 | 按 v2 的 S1 至 S4 实现后替换 v1 精炼路径 | 中等工程量，每次少量本地 token | 是，唯一同时满足效率与质量的候选，但须过第 8 节闸门 |
| 确定性 only（v2 Phase 1 单独上线） | 只做命令、文件、决策、错误与修复对抽取，无 LLM | 近零运行成本 | 可作为 Phase 2 未过闸门时的保守降级 |
| 托管模型做精炼 | GLM 或 GPT 池做提取 | 私人对话出本机（违背 local-first，需 owner 明示）加费用 | 仅在 owner 明确放开隐私边界后讨论 |

理论锚纪律：11-refinement-v2.md:15-25 的 R1/R2/R4 沿用已登记条目，R56（von Restorff 独特性效应）新增登记且如实标为本轮未直接核验；:25 显式不借用弗洛伊德释梦、左右脑分工、学习风格、莫扎特效应等神话。本评估未新增任何理论条目，也不把 NARROW 或 REPLACE 包装成理论结论，它们是成本收益结论。

## 7. 若保留或替换，必须守住的红线

1. 有界输入：任何精炼窗口不得面对无限流；排队超过 7 天告警而非放大批量（11:33）。
2. 单模型、可整体关闭；Phase 2 失败不得静默全丢或全收（11:45-50）。
3. 快照只存引用与产出，1MB 硬上限，超限 fail-loud（11:55 与 11:60）。
4. 接地校验按 chunk 内子串、空白折叠、片段至少 16 字符；丢弃率大于 50% 判运行不合格并停用 Phase 2（11:49）。
5. 产出接主图前必须过人工有用率抽样闸门（至少 20 窗口、60% 以上）（11:62）。
6. 捕获、召回、注入三层零改动；local-first 边界不因性能诉求被绕过（11:68 与 11:74）。
7. 事实版本化走 supersede 专用路径，明确不复用从未运行的 design/12 提名管线（11:54）。

## 8. 建议、放行条件与什么证据会改变结论

决策：维持 owner 已裁的砍 v1 加重设计；本 QA 席位据此判 REPLACE，过渡态 NARROW，并给 v2 实现批次附以下放行条件：

- 先补 issue #294 指令 #2 的 before/after recall 测量（owner 实际关心的查询，精练开与关对照）。这是当前最大的 oracle 缺口；没有它，值得吗不可证伪。
- S3 实现后必须报告 Phase 2 接地校验丢弃率与人工有用率原始数据；丢弃率大于 50% 或有用率低于 60% 则不放行接主图，退回 Phase 1 only。
- 必须证明新快照不超过 1MB 且溢出与重试路径 fail-loud（可用本轮 111 MB 快照作为回归反例）。

会改变本结论的证据（否证器）：

1. 若出现受控 before/after 测量，显示精炼事实使 owner 关心的查询召回提升达预注册阈值（建议 10 个百分点以上），则从 NARROW 上调为 KEEP 加改进。
2. 若归档中出现真实规模运行产出高价值、非自指三元组的样本（本轮 8 个文件已全部核验，未见），则推翻规模产出为零。
3. 若 v2 上述闸门实测不达标且 Phase 1 only 都无法证明增量价值，则从 REPLACE 降级为 NARROW 长期化，即永久取消 LLM 精炼。
4. 若 owner 明示放开托管模型并接受隐私代价，则重开托管模型精炼选项（本轮因 local-first 红线排除）。

## 9. 范围、红线与证据索引

只读声明：数据库以 mode=ro 打开；未修改产品代码、配置、宿主、skill、PROTOCOL、在线数据；未提交任何源码；未 push。归档物仅读取。生成过程在本地工作树产生 gitignored 的 .venv（uv 建立），临时脚本在 Temp/opencode，均不入库。

provenance append-only：本文为新增文件，不改写原评估、issue 评论或任何历史记录。

证据索引：

- 一手数据：~/.mnemoseed-local/meta.db、graph.db、config.toml、models/bge-m3（只读）；org/evidence/dream-archive-20261028/（8 个快照）。
- 代码：config.py:121,431-440,473；delta.py:248-268；snapshot.py:62-78；readiness.py:75-87；daemon/app.py:128-133,230-248；cli.py:1047,1084；hosts/claude_code/events.py、hosts/codex_cli/events.py、hosts/common/daemon.py:344；eval/metrics.py:79-131。
- 设计与历史：docs/zh/design/02-dream-engine.md:120-138,205-211；11-refinement-v2.md:6-13,15-25,29-69；12-reconciliation.md；issue #294（两轮评论）；提交 cdb9124（PR #298）、b18bc78、f330f39、403ce7f，基线 8a9a115。
- 未验证项：#294 指令 #2 的 before/after 召回测量（不存在）；2/20 投票接受率一手物（未定位）；score pool 入账死锁的精确代码机制（未重推）。以上均已在上文显式标注，不作既成事实使用。
