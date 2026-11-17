# PRD · 召回排序修复（#391）

> 依据：issue #391 + owner 2026-11-17 指令。hook-less 表面（Claude Desktop）真的调了
> `recall`（daemon 日志 `POST /memory/recall -> 200`），但拿回 20 条无关的编排脚手架，
> 于是诚实回答"没有相关记忆"。传输与指令层已经证明可用，本批只修检索排序。
> 前置：`main@afd0222`。**不重新 embed 现有 11,855 个 chunk**（实测 340 轮/42 分钟的
> 成本，全量重算是小时级）。设计评审：solution-architect 2026-11-17
> **SHIP-WITH-ADJUSTMENTS**，四条调整已逐条落入本文（见 §9）。

## 1. 目标（用户可感知）

1. `Claude Desktop memory protocol` → 当时那条完整设计消息仍在前 3（不回归）。
2. `PRD-387 hookless surfaces` → 不再出现 `You are a subagent spawned by another session`。
3. `我们上周关于 hook-less 表面最后定下来的方案是什么？` → T12 survival / O(n²) / 提交过程语句不再出现在返回里；要么命中三层投递的决定，要么**明确返回空**。
4. 返回空时，模型按 PRD-387 已冻结的指令直说"没有相关记忆"——这条指令今天是一句死话，因为召回**结构上不可能为空**；本批让它成真，指令文本一个字不改。
5. 融合分数重新有区分度（服务集内 max−min > 0.3），而不是全挤在 1.04~1.21。

## 2. 冻结设计

### D1 根因裁定

| # | 根因 | 状态 | 证据 |
| --- | --- | --- | --- |
| RC-1 | 融合总分被 decay 项支配 | **已证** | `retrieve/hybrid.py:495-501` 的 `total = 1.0*semantic + 1.0*cue + 0.8*decay + 0.5*centrality`；每个候选 `decay ≥ 0.4`（`hybrid.py:99,284`），新鲜 chunk `decay≈1.0` 自带 +0.8。新鲜噪声 semantic 0.35 → 1.15；较旧正确答案 semantic 0.55 / decay 0.5 → 0.95，噪声赢。与实测 1.04~1.21 无区分带完全吻合。 |
| RC-2 | 服务路径上没有任何相关性下限 | **已证** | 向量轨接受所有 store hit（`hybrid.py:298-339` 只过滤 decay / pin 类 / 实体不匹配，不看 `hit.similarity`）；store 也不设阈（`lancedb_embedded.py:337-364`）；装配器按名次和预算准入（`assemble.py:316-406`）。所以 `vector_hits=20` 恒定，召回不可能返回空。 |
| RC-3 | cue 抽取器丢掉 ID 形状的词 | **已证** | `_CAP_TOKEN`（`retrieve/cues.py:259`）要求 token 内部有小写字；`PRD-387` / `B1` / `L2` 全大写加数字，一个都不是实体。于是 `graph_hits=0`（`hybrid.py:351-353` 在 entities 为空时直接返回），`cue_overlap≈0`，非语义权重全压在 RC-1 的 decay 上。评审纠正了本文初稿：`PRD-387 hookless surfaces` 对这个抽取器是**实体为空**，不是实体密集。 |
| RC-4 | 编排脚手架是高质量、高 decay 的噪声团 | **已证** | 脚手架是真实 turn 逐字入库（`capture/stamper.py:135-147`）；F1 stripper 只处理机械形状（`capture/rulesets_v1.py`），对话脚手架刻意不动（`stripper.py:10-15`）。稠密/稀疏 50/50 融合（`lancedb_embedded.py:68,357-361`）让复述查询词的样板文本拿到词汇增益——"只重复查询词的 chunk 压过 pin"正是这个。 |
| RC-5 | 中文问句与陈述句的 embed 不对称 | **推断，未测** | BGE-M3 ONNX 是多语种强模型；`Claude Desktop memory protocol` 能命中说明跨短语检索部分可用。问句不对称估计损失 0.1~0.2 cosine，是次要因素。**冻结阈值前必须先量一次**。 |
| RC-6 | `vector_top_k=20` 截断 | **不是主因** | store 先扫 `candidate_k = max(4*top_k, top_k+50) = 80` 个稠密候选再留 20（`lancedb_embedded.py:345-352`）。失败样例是排序失败，不是准入失败。 |

### D2 F1 — 重新平衡融合权重

- `retrieve/hybrid.py:97` `weight_decay: 0.8 → 0.2`。标定范围 **[0.1, 0.3]**；若评测显示召回把陈旧结论排在新鲜结论之前，退到 0.3。
- decay 的准入职责不变：`min_decay` 前过滤（`hybrid.py:284`）与救援带（`rescue_min_decay`）都在别处执行，总分里的 decay 是**重复计入**。
- daemon 构造 `HybridConfig` 时只传救援参数（`daemon/memory.py:611-616`），所以这是单点默认值改动。本批**不**把全部权重开到 `RecallConfig` 配置面。

### D3 F2 — 语义相关性下限

- `HybridConfig` 新增 `min_similarity`，初值 **0.45**，批次内用 `eval/rescue_harness.py` 在真 embedder 上标定后写入 closeout 记录（先例：`config.py:144-150` 的救援带常量）。
- 施加位置：`_vector_track` 对 `hit.similarity` 的后过滤（`hybrid.py:298-339`）。普通候选低于下限即丢弃并计数；**被救援的 pin 不受下限约束**（它们走 decay 驱动的另一套准入，`hybrid.py:300-314`）。
- **不放在融合总分上**：那个分数被 RC-1 的加性项污染，永远不是纯语义。
- **不改 store 驱动**：距离阈值无法在 `.where` 里表达，改 port 无收益。
- 下限作用于 store 的稠密+稀疏融合相似度，词汇污染（RC-4）仍在；但 F1 已修好排序，下限只需砍掉最底层的噪声，本批不重构 `SearchHit`。

### D4 F3 — 诚实的空结果契约

- `HybridRecall` 新增 `floor_dropped: int`（清过 decay / pin 准入但没过 D3 下限的数量）；`assemble.py:164-181` 的 `CoverageReport` 同名字段；`memory.py:776-795` 透传。
- 没有任何候选过线时：`entries=()`、`dropped_count=0`、`tokens_used=0`、`budget_tokens` 保持配置值 800（它是预算不是用量，`assemble.py:83`）、`coverage.vector_hits=0`、`coverage.floor_dropped=N`。
- **`dropped_count` 语义不变**：它只表示预算闸门的拒绝（`assemble.py:329`），不混装下限拒绝。
- `coverage.vector_hits` 的新语义是**下限过滤后的计数**，必须在文档与 closeout 记录里声明。
- MCP 网关对 `entries=[]` 必须正常序列化（`mcp_gateway/server.py:236-240`）。
- PRD-387 的指令文本**不改一个字**；本批让它的前提第一次成真。

### D5 F4 — 修 ID 形状词的抽取

- `retrieve/cues.py:259`：新增兄弟模式，产出带数字或连字符的全大写 token（`PRD-387`、`B1`、`L2`），长度 ≥ 2，且不做纯数字垃圾。
- 不能松到把 "JSON" / "API" 之类散文词也当实体：模式必须锚定在数字/连字符或长度约束上，否则会挤占 16 个实体的上限（`cues.py:52`）。
- **诚实边界**：对**查询**立即生效（图种子 + cue 重叠）；对**已入库 chunk** 只对未来新 chunk 生效——存量 chunk 的 cue 是旧抽取器盖的章。这是补充修复，不是主修复。

### D6 图轨与脚手架的处置

- `graph_hits=0` 是两个原因叠加：没有种子实体（RC-3，本批修）＋ 图节点只由 dream 巩固写入（`dream/merge.py:239-241`，capture 不 upsert 节点）。**图回填不在本批范围**。
- 在 capture 时剥离对话脚手架会破坏逐字契约（PRD-394 §D7/§6 明确要求原样存储），**不做**。诚实替代就是 D2+D3：脚手架稠密语义低，过不了下限就不被服务。
- 按来源标记做查询时降权是二阶启发式，**本批刻意不做**——先看 D2+D3 之后还剩多少残留再说。

### D7 与 #281 的关系

issue #281「过期约束 chunk 主导相似度排序」的机制**已经实现并合并**（PR #332：`retrieve/temporal.py`、`hybrid.py:404-417` 的 tier key、`assemble.py:330-339`），issue 只是忘了关。本批与之组合：过期 chunk 仍在 tier 里排在所有正常候选之后，D3 还可能额外砍掉其中低相似度的那些。**不在本批内吸收 #281**；本批落地后单独验证并关单。

## 3. 切片

| 切片 | 内容 | 依赖 |
| --- | --- | --- |
| S1 | F1 权重默认值 + F3 空结果契约（`hybrid.py` / `assemble.py` / `memory.py`） | — |
| S2 | F2 相关性下限 + 标定记录（`hybrid.py` / config 常量 / harness） | S1 |
| S3 | F4 cue 抽取器（ID token 模式）+ 图种子测试 | — |
| S4 | 契约钉更新（`tests/test_retrieve_hybrid.py` 默认值 / MCP 空结果 / 文档） | S1–S3 |

**必须更新的既有契约钉**：

1. `tests/test_retrieve_hybrid.py:777-786` `test_default_config_documented_defaults` 钉了 `weight_decay=0.8` → 改成新值，并同时钉住 `min_similarity`（否则把新默认值改回去仍全绿）；
2. `tests/test_retrieve_hybrid_concurrent.py` 的并发/顺序逐字节一致性（下限放在共享 track 内，应保持）；
3. 救援带测试 `test_rescue_band_admits_pin_with_strong_cue_below_main_floor` 在下限之下仍要通过；
4. MCP `recall` 空结果序列化测试。

## 4. 验收标准

- AC1：`Claude Desktop memory protocol` → 当时那条完整设计消息在返回前 3（回归钉）。
- AC2：`PRD-387 hookless surfaces` → 服务条目里没有 `You are a subagent spawned by another session`；诊断 pin 出现（名次 ≤ 3）或返回空。
- AC3：`我们上周关于 hook-less 表面最后定下来的方案是什么？` → T12 survival / O(n²) / 提交过程语句不在服务条目里；三层投递的决定命中或返回空。
- AC4：`initialize instructions capture verb` → 编排器自己的粘贴指令 chunk 不在服务条目里，或返回空。
- AC5：任意查询的服务集内，融合分数 max−min > 0.3；或返回空。
- AC6：每条返回的 `coverage` 如实报告 `vector_hits ≤ 20`；`floor_dropped > 0` 时差值对得上。
- AC7：`extract_cues("PRD-387 hookless surfaces").cues.entities` 含 `PRD-387`；`"L1 L2 L3"` 产出 `L1, L2, L3`。
- AC8：图种子——未加反引号的查询里出现实体 `PRD-387` 时，图上该节点可达（杀掉 RC-3 变异）。
- AC9：全量套件保持绿——救援带、PRD-281 tier key、并发逐字节一致。
- AC10：空结果端到端——haystack 只有低于下限的 chunk 时，daemon 返回 `entries=[]`、`vector_hits=0`、`floor_dropped=N`、`dropped_count=0`，MCP 网关不崩。

**变异体可能穿过的缺口，必须补钉**：

- AC-M1：下限测试必须断言**空结果本身**，只断言重排的话，"总是返回 top-5" 的变异会全绿；daemon 边界也要镜像一条。
- AC-M2：needle-vs-boilerplate 必须断言**名次**，只断言"在里面"的话，降低 gamma 但噪声仍交错的变异会全绿。
- AC-M3：救援绕过下限——低于下限但有强 cue 的 pin 仍准入。
- AC-M4：`floor_dropped` 计数精确；并发与顺序输出逐字节一致。

## 5. 不做什么

- 不换 embedder 模型（RC-5 只是推断，而且换模型要重算全部向量）。
- 不重算 / 不重建 11,855 个 chunk 的索引。
- 不盲目调大 `vector_top_k`（候选池已经是 80→20，调大只会服务更多噪声）。
- 不删除或改写历史 chunk（来源不可变与逐字红线）。
- 不在 capture 时剥离脚手架（PRD-394 的契约）。
- 不硬编码查询串、噪声 chunk id 或中英文开关（过拟合六个探针）。
- 不在召回路径加 LLM rerank（token 经济不变量：基础设施管线不调模型）。
- 不在融合总分上加下限（被加性项污染，语义上不可解释）。
- 不回填图 / 不做 dream 巩固。
- 不在本批内关 #281。

## 6. 风险、边界与禁语

- **下限过高 → 诚实但无用的空结果**：0.45 必须在真 embedder 上标定后才冻结；救援带保证 pin 在阈值下仍可达。
- **gamma=0.2 可能过度削弱新近性**：「上周决定什么」可能翻出陈旧结论；预约定标范围 [0.1, 0.3]，0.3 是回退值。
- **`vector_hits` 语义变化**：`eval/harness.py`、CLI recall trace、`cli.py:1163` 都读这个字段；新增 `floor_dropped` 是 additive，但语义必须在文档写清。
- **F4 正则回归**：过松的 ALL-CAPS 模式会把散文词当实体；模式必须锚数字/连字符或长度。
- **T2 自动召回面未检查**：focal floor / pending-recall 是另一条服务路径（`memory.py` B2.1 机制），本批的默认值改动对它的交互**未验证**，closeout 必须如实写"未检查"，不得默认没事。
- **禁语**：「记忆已激活」「模型会主动调用」；报告只描述检索账目。

## 7. 门

`uv run pytest -q`、`ruff check`、`ruff format --check`、`mypy src` 全绿；closeout 另需独立对抗性 QA 无 BLOCKER。

## 8. 理论锚

**本批不新增借用。** 沿用 PRD-387 §8 已登记的**编码特异性**（Tulving & Thomson, 1973：提取线索与编码线索的重叠决定提取成功率）：D5 修 ID 形状词的抽取，正是让查询端产生的线索与入库时盖的章对得上；设计规则保持不变（用用户自己的词，不改写成同义词）。本批没有把任何新规律搬进系统。

D1–D4 是机制裁定（加性分数污染、相关性下限、空结果契约），不是认知科学主张。

**未借用（明确排除，照 00 §2.4 顺延）**：不引用「记忆需要重复巩固」「睡眠中重放」「左右脑分工」一类流行神经神话；「LLM 会像人一样被问句措辞影响检索」只是待验证的推断（RC-5），不是已证规律，不得据此写作设计依据。

## 9. 评审记录

solution-architect 2026-11-17 裁定 **SHIP-WITH-ADJUSTMENTS**，四条调整与本批落点：

| # | 调整 | 落点 |
| --- | --- | --- |
| 1 | 初稿漏了已证根因 RC-3（cue 抽取丢 ID token） | §D1 RC-3 + §D5 + AC7/AC8 |
| 2 | 下限位置必须钉死在向量轨相似度上，不能放融合总分 | §D3 |
| 3 | gamma 不能写死单值，给标定范围与回退值 | §D2 [0.1,0.3]，回退 0.3 |
| 4 | 空结果契约的字段语义必须逐项写清 | §D4 |

评审同时纠正三处事实，已并入正文：

- `PRD-387 hookless surfaces` 不是"实体密集"查询，对该抽取器是实体为空（§D1 RC-3）。
- `vector_top_k=20` 不是主因：store 先扫 80 个候选（§D1 RC-6）。
- #281 的机制已合并、issue 只是忘关；本批不吸收、落地后单独关单（§D7）。

owner 2026-11-17 追加指令：**模型池加入 Hy4 flash，所有 agents 优先使用 Hy4 Flash**；该偏好已捕获进记忆，与本批实现无耦合。
