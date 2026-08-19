# PRD · B2.1 自动回忆（auto-recall）：会话起始回放 + 中段线索触发

> 依据：
> - B2 收口的挂起项："hook 自动注入形态存挂起（依赖宿主插件上下文注入能力验证）"；
> - B2 立项痛点（当日实证）：新 session 无法按时间接上上一 session 结尾（需手动贴引用）；
> - B3.1 收口发现再确认：模型自决 recall 在原理上不可靠（unknown unknowns——模型不知道自己不知道什么）；
> - 用户拍板（2026-08-19）：回忆架构必须以**已验证理论**为基石，理论与实现机制严格分层；并升级为长期纪律——**mnemoseed 全系（本仓与主仓）所有功能理论必须附完整设计文档，是系统核心设计的基石**。

## 理论锚（核心设计基石；任何改动须先改本节再改代码）

入选标准：只列**有实验与长期复现证据验证的规律**；每条给出来源、规律原文级表述、以及它在本系统推导出的**设计规则**。理论回答"为什么这样设计"；延迟/缓存/预取/预算属实现机制层，不入本节。

### TA-1 ACT-R 陈述性记忆激活方程 —— 全局排序动力学

- 来源：Anderson & Schooler（1991）对人类记忆统计结构的实证（"回忆概率 ≈ 环境中需要的概率"）；ACT-R base-level learning 方程（β = ln Σ t_j^-d，历史使用的频度与时近按幂律求和），数十年跨任务复验。
- 已验证规律：记忆的可用性 = 基础激活（使用频度 × 时近）+ 当前线索的扩散激活 + 噪声。
- → 设计规则：**回忆排序 = 基础激活 + 线索激活**。本系统既有 decay/reinforce 是该方程的同构物（自洽，非巧合）；排序公式的任何变更必须先改本规则再改代码。

### TA-2 编码特异性 / cue-dependent forgetting（Tulving）—— 线索工程

- 来源：Tulving & Thomson（1973）encoding specificity principle；"available but not accessible" 是遗忘的主形态。
- 已验证规律：遗忘的主因是**线索失败**而不是存储失败；提取成功率取决于线索与编码时上下文的重合度。
- → 设计规则：**回忆的本职是线索工程**——当前轮**原文直接作线索**（不做模型重写式 query）；保留编码时元数据（时间/项目/实体）作为线索面；线索分两级：**实体精确命中 = focal 线索，纯语义相似 = non-focal 线索**，两类分设相关度 floor（阈值经 T4 标定）。

### TA-3 近因优势与无线索态默认（serial-position recency / TCM）—— 会话起始

- 来源：Murdock（1962）系列位置曲线中最稳定的效应；Howard & Kahana（2002）时序语境模型——提取由当前语境状态驱动，语境切换后唯一稳定的线索维度是时间近因。
- 已验证规律：**无外部线索时，近因主导回忆**。
- → 设计规则：**新 session 首轮无条件注入时近回放**（`recent_sessions` 尾部），**不做"是否与旧对话相关"的触发侧预审**——触发侧预审是拿非 LLM 部件做 NLP 判断（错位）；相关性判断由消费侧完成（模型读入后忽略的成本≈零）；注入必须带围栏（TA-5）。

### TA-4 前瞻记忆多加工框架 —— 自动 vs 自决的分界线

- 来源：McDaniel & Einstein multiprocess framework（2000 起，含元分析支持）：意图的提取依赖**事件线索的自发提取（spontaneous retrieval）**；事件线索显著优于时间线索；focal 线索触发即自发提取，non-focal 线索需要自主监控（strategic monitoring）——后者贵且易漏。
- 已验证规律：计划/决定/意图类内容的可靠提取依靠自发提取通道；**自主监控是昂贵易漏的回退形态**。
- → 设计规则：**对话进程的自动回忆 = 自发提取通道，必须常开**；计划/决定/意图类事实在实体级 focal 命中时**自动浮现**；non-focal 弱关联**不自动注入**，留给模型显式查询（MCP 工具）。**"模型自决回忆"被本框架证伪**（等效于让模型全程自主监控），作为默认形态明确否决，仅保留为深挖通道。

### TA-5 来源监控错误 —— 注入必须带围栏

- 来源：Johnson 等 source-monitoring framework（1993）。
- 已验证规律：人对"内容来自记忆还是来自当前输入"的判别**本质上不可靠**，来源混淆是常态而非例外。
- → 设计规则：**一切注入上下文必须带明确围栏（"此为记忆回放，非用户当下指令"）**；由注入模板基础设施级内建，禁止裸注入。

### TA-6 提取诱发遗忘家族 —— 注入 ≠ 强化

- 来源：Anderson, Bjork & Bjork（1994）retrieval-induced forgetting：提取 X 会压制其竞争者；长期提取优势会重塑可访问性格局。
- 已验证规律：频繁被提取的记忆会压制相邻记忆（受欢迎度自我强化）。
- → 设计规则：**auto-recall 的"被注入"不计 reinforce**；仅当有证据表明注入内容被消费使用（assistant 轮文本实际引用注入内容）才计。防马太效应埋葬冷门正确事实；冷门正确事实的最终防线仍是 verbatim 通道 + isolated 结构（设计稿既有红线）。

### 不借清单（防伪理论混入，与上同重）

- "7±2 工作记忆容量"：Miller（1956）已被后续研究修正（chunk 依赖的 ~4，Cowan 2001）——不得作为任何数字常量的出处；
- 左右脑神话、学习风格论等神经营销话术：无有效证据；
- 任何未经复现的单次实验结论：借用前须有 replicated evidence；本仓评测臂可以产数据，但数据不得反向包装成"理论"。

## 范围（批次任务）

- **T0 宿主能力探针**（B2 挂起项收口）：临时探针插件（**只观测、零注入**），确认 `chat.system.transform` / `chat.messages.transform` / `chat.message` parts 可变性的真相，观测日志转录入本 PRD 收口记录。首选面被证伪时按备选次序降级：→ `chat.message` 追加 part → `tui.prompt.append` → 保守回退（维持 MCP 现状，如实记录）。探针需宿主重启才会加载，须用户配合一次重启观察。
- **T1 会话起始回放注入**（TA-3/TA-5）：新 session 首轮注入 recent 尾部 + 围栏标记；hook 契约保持 fail-open（2s 超时、daemon 缺席静默跳过、每 session 至多一次注入）。
- **T2 中段自动回忆管线**（TA-1/TA-2/TA-4）：daemon 侧新增 `capture.auto_recall`（config 热键，默认 off 起步）——turn 捕获后即跑 recall-in-place：seen-set 会话态（同一 chunk/node 每 session 至多注入一次）+ focal/non-focal 双 floor + token 预算封顶；**判定全在 daemon，hook 只做注入**（可灰度、可回滚）。空注入零 token、延迟离线（捕获后预取）为实现机制，同批落。
- **T3 消费证据守卫**（TA-6）：注入计入 seen-set；reinforce 仅在 assistant 轮文本实际引用注入内容时记；检测规则与单测同批落。
- **T4 评测标定**：floor/focality 阈值进 B3 评测臂拿数据后定版（吃现有矩阵基建），报告入数据目录 `eval/`，摘要入本 PRD 收口记录。

## 边界（如实）

- 探针可能证伪首选注入面——届时按 T0 的降级次序执行并如实记录，不硬上。
- 理论锚管设计动机，不管性能承诺；性能数字属实现与评测（T4 前一切 floor 数字只是起步值）。
- 本批次不改 capture/dream 核心链；hook 既有火忘契约（fire-and-forget、失败吞没）不变，注入面新增的是 fail-open 的读取路径。
- `capture.auto_recall` 默认 off：先给观望者与安全验证留门，按 T4 数据再议默认翻转。

## 门禁（不变）

TDD（先红后绿）→ 对抗 QA 自验 → 全量门禁（`uv run pytest -q` / ruff / format / mypy）→ 单 commit 收口 + 收口记录入本 PRD。

## 批次执行记录（随批追加）

### T0 探针观测结果（2026-08-19，临时插件 `mnemoseed-probe.ts`，只观测零注入）

三候选注入面全部真实触发，形状实测（日志 `~/.mnemoseed-local/probe-t0.jsonl`）：

- **`chat.system.transform` —— 首选面成立**：`input = { sessionID, model }`，`output.system` 为**字符串数组**（含系统提示词）。注入 = 追加一个字符串元素，最干净、与消息流无侵扰。
- `chat.messages.transform` —— 备选：`output.messages` 为 `{info, parts}` 消息列表，可插合成消息但需构造合法 info，侵扰面更大。
- `chat.message` —— 备选：`output = { message, parts }`（用户消息），可追加 text part，但只作用于当轮用户消息。

注入策略按此定案：回放/回忆注入走 system.transform 追加；每 session/每轮闸控（session 首轮判定、seen-set）在 hook 侧以 sessionID 为键执行。

### 基线修正（捕获面红线，先于 T1 必须修）

- **Dogfood 实锤**：新 session 实测"刚刚做了什么"——记忆只有 user 轮，assistant 轮全灭，回忆无从衔接。
- **根因（SDK 契约层）**：hook 调用了 `client.session.message`（单数）——@opencode-ai/sdk gen 客户端**只暴露列表端点** `session.messages({path:{id}}) -> [{ info, parts }]`；`typeof query !== "function"` 使每条 assistant 轮静默短路进 console.debug（宁吞不炸的 hook 契约放大了缺口）。
- **修复**：`fetchAssistantText` 改走列表端点 + `info.id` 查目标消息 + 未冲刷时 ok:false 回滚指纹稍后重试（宁可重复不丢）；新增契约钉死测试（session.messages 复数调用 / 单数名缺席 / info.id 查找三点）；`hook install opencode` 已把修复部署到全局插件目录，**待宿主重启后 live 验证 assistant 轮入库**。

