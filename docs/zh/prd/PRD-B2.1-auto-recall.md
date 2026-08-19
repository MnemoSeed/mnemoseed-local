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

### 基线修正 ②（生命周期映射勘误，2026-08-19 live dogfood，先于 T1 必须修）

- **重启后实锤**：基线修正 ① 部署并完成宿主重启后，新 session 依旧只有 user turn 0——连后续 user 消息也消失；probe 日志证明 `chat.message` 钩子正常触发、daemon ingest 管道手动注入测试无恙（202 accepted + flush 落库）。
- **根因（宿主生命周期语义错位）**：opencode 的 `session.idle` **每答完一轮即 fire**（空闲 = agent 安静，非会话终止）；A3 映射把 `idle|error|deleted` 全送 `/session/end`——会话在第一轮回答后结算封口（turn 0 顺带 drain 落库，掩盖了表象），之后一切 `/ingest` 由 daemon 正当地 409（SessionSettledError）拒绝，fire-and-forget 契约静默吞没。assistant 轮则自始至终被 409 挡在门外，修正 ① 的效果无法被观测。
- **修复（hook 侧映射重排，daemon 零改动）**：`session.idle`/`session.error` → `/flush`（关闭在飞 turn 并 drain，会话保持可摄入）；仅 `session.deleted` → `/session/end`。新增端到端回归测试（idle→flush 后可续摄、双方角色 turn 全部 verbatim 落库、settled 后迟到消息 409），静态契约钉死 `flushSession(` 在 idle/error 分支、`settle(` 仅存于 deleted 分支；`mvp-design.md` §4.5 生命周期段与 `PRD-A3-delivery.md` 映射条目同步勘误。门禁：1183 passed / 3 skipped，ruff/format/mypy 全绿。
- **已知良性竞态（如实记录）**：assistant `message.updated` 与同轮 `session.idle` 的 flush 两个 fire-and-forget POST 次序不保证；若 flush 先到，assistant 文本会落进紧随的独立 turn——内容不丢、检索无碍，chunk 边界略丑，v1 接受。
- **待用户动作**：重启 opencode 使修正 ①+② 同时生效；生效判据 = 本 PRD 会话内 assistant 轮文本出现在 `recent_sessions` 尾部。

### 基线修正 ③（SDK 调用绑定形，2026-08-19 live dogfood，先于 T1 必须修）

- **重启后再实锤**：修正 ①+② 部署生效后，user turn 连续落库了，但 assistant 轮依然全灭——且非竞态所致（迟到消息连独立 turn 都没开）。
- **取证手段**：T0 探针临时扩线——新增只观测的 `event` 钩子做总线普查 + 原样重放生产 fetch 路径逐关记录（`probe-t0.jsonl`）。结果显示：`message.updated` 正常触发、角色/completed 正常，而 `client.session.messages(...)` 调用本身抛 `TypeError: Cannot read properties of undefined (reading '_client')`。
- **根因（JS 方法解绑）**：hey-api gen client 的 `messages` 方法体为 `(options.client ?? this._client).get(...)`；插件写成 `const list = client?.session?.messages` 后**脱离接收者调用，`this` 丢失**即抛。基线修正 ① 的"单数端点不存在"实为同一异常被静默吞掉后的误诊（1.18.18 SDK 单数端点存在，需双 path 参）——`console.debug` 静默契约两度把真异常藏死，直到探针介入。
- **修复**：fetch 改为**在接收者上直接调用** `client.session.messages({...})`（绑定 `this`）；契约测试升级为钉死绑定调用形态（`client.session.messages({` 必在、`const x = client?.session?.messages` 摘取式必禁）。门禁：1183 passed / 3 skipped，ruff/format/mypy 全绿；`hook install` 已部署。
- **待用户动作**：再重启一次 opencode；生效判据 = assistant 轮文本入库。本轮教训（记录备查）：fire-and-forget 部件的 silent-failure 面必须配探针级可观测性——静态"契约钉死"测试拦不住"行为对了、绑定形错了"这类运行时缺陷。

### 基线修正 ④（senior QA 对抗复审收口，2026-08-19）

修正 ①+②+③ live 验证通过（turn 连续、assistant 轮文本入库、双角色同 chunk）后，用户拍板：修复必须走完既定流程——**对抗式 senior QA 复审确认后才收口**。QA 复审结论：无 BLOCKER；三次修复方向全部正确，但静默丢失**这一类**问题在相邻路径上仍然敞开。按判定分批处置：

**本批已修（确定性缺陷，TDD 先红后绿）**：

- **QA-1+2 assistant 捕获可靠性重构**：旧"回滚指纹"重试依赖宿主再 fire——会话最后一条回复永远没有重试点；settle 与在途 fetch 竞跑会把末条 assistant 静默 409 丢掉。重构为 **pendingAssistant 集合 + idle/error/deleted 前 await 确定性重扫**（settle 再也无法超越在途 fetch）；SDK 拉取加超时竞速（挂起 promise 不再泄漏）；扫点时仍无文本才判 tool-only 终局（不再把"parts 未挂"误判成空）。
- **QA-3 分片器 host 盲碎片化**：旧的 response-boundary 规则为无锚宿主（Cursor）设计，却对一切宿主生效——opencode 工具环模式下每条多段助手回复都碎成孤儿 turn，召回丢失上下文。改为**锚感知**：user 锚定的 turn 吸收整条多段回复；仅无锚流（孤儿/工具开头的 turn）保持按响应边界切分。
- **QA-4 daemon 关停不 drain 捕获缓冲**：lifespan 收尾新增 `segmenter.flush_all()` + 全 session drain（重启丢最后一轮的静默缺口封死）。
- **QA-5 内存常驻 + 工具输出无截断**：`/session/end` drain 后 `prune_settled` 释放 settled 会话缓冲（daemon 半程）；hook 半程 `MAX_TOOL_OUTPUT_CHARS=20000` 截断带显式标记。
- **QA-10/11/13 pin 漏洞**：诈骗式 docstring 更正（单数端点其实存在，真凶是未绑 `this`）、解绑负 pin 泛化到 let/var/解构各种形态、钉死 `{ path: { id: sessionID } }` 调用形状。
- **QA-12 系统性缺口（三次踩坑的共同根）**：(a) 门禁新增 **esbuild TS 语法校验测试**（此前全部门禁是 Python，plugin.ts 只靠正则 pin，语法坏文件能一路绿）；(b) hook 新增 **`MNEMOSEED_LOCAL_DEBUG` 可观测 seam**（失败升级 console.error + JSONL 沉槽；**每个 POST 现在检查 daemon 响应状态，非 2xx 必上报**——409 被静默吞没正是 settle 封口 bug 的隐身衣）。
- **QA-14/15 文档残留**：mvp-design §5 流程图与 §6 A3 摘要同步 `/flush` 映射；测试与插件注释里的误诊表述全部更正。

**记录挂起（不入本批，后续批次再议）**：

- **QA-7**：用户中止（abort）的助手回复可能从不触发 completed 事件（`time.error` 形态）——待探针一轮验证，若属实则把 error 形态也视为完成点。
- **QA-6**：重启不对称性已证无害（hook 抑重 + daemon 近重复吸收兜底），仅 provenance 外观问题，记录备查。
- **QA-8**：idle-flush debounce 被 QA 否决（过度工程；竞态只是内容无损的重排，且已被 QA-3 的确定性修复吸收）；settle 序问题用 await 重扫解决，不用睡眠。
- **QA-13 残余**：fixture 无 `model_id` 变体（NIT）；mvp-design.md:61 去重单元表述与实际近重复吸收实现不符（存量漂移，记录备查）。

门禁：1193 passed / 3 skipped（新增 3+10 测试），ruff / format / mypy 全绿；`hook install` 已部署。生效判据不变且追加：多段回复不再碎片化、`hook-debug.jsonl` 在 `MNEMOSEED_LOCAL_DEBUG=1` 时可写。

### 批次执行：T1 会话起始回放注入 + T3 消费证据守卫（2026-08-19 开工，用户拍板 T1+T3 成批，T2/T4 挂起后续）

理论锚不变（TA-3 无条件时近注入、TA-5 围栏、TA-6 注入≠强化/消费才计）——本批全部为**实现机制层**决策，记档如下（含 solution architect 评审 9 issue 的采纳结果，评审日期同日）：

- **注入面**：T0 定案 `chat.system.transform` 追加（`output.system` 字符串数组）；非数组形状防御跳过，不创建不修补。
- **闸门（评审 issue 2 采纳，语义三句钉死）**：(i) sessionID 空或 `output.system` 非数组 → 立即返回，**不消耗** attempt（opencode 内部模型调用不带主会话输出形状时不得烧掉首轮回放）；(ii) 形状可用 → **同步**写入 attempted map（先于首个 `await`，防并发双注入），此后无论成败不再试（daemon 缺席每 session 每进程至多付一次 2s，fail-open 原文）；(iii) 响应载荷 `sessions` 非数组视为失败静默（attempt 已消耗）。
- **竞态排除**：daemon `/session/recent` 新增可选 `exclude_session_id`——hook 上报当前 sessionID，daemon 分组时排除（当前 session 的 turn 0 可能已落库，回注给用户 = 自引用回声）。排除语义：cap 计**幸存**组；"?" 共享组不受影响；分页限幅公式钉死 `min(2000, (sessions + (1 if 排除) else 0)) * per_session * 4`。
- **预算（token 红线，评审 issue 6c 定案）**：`MAX_INJECT_CHARS = 4000` **含围栏与组头**（最终 append 的整字符串 ≤ 4000，实现最简单也最诚实）；按时近优先累计（组间新→旧、组内新 chunk 先计），边界 chunk 保留其**尾部切片**（切片预算 = 剩余 − 2，预留 "…" 标记与换行；切片预算 <200 字符即整 chunk 放弃——即剩余恰为 200–201 字符时实为整 chunk 放弃而非切出 ~198 字符切片，方向安全，不会产出过短切片）。
- **围栏完整性（评审 issue 4 采纳）**：注入 chunk 文本可能字面包含围栏标记（本批上线后自我 dogfood 第一晚就会命中），构建注入块时对每 chunk 文本单趟净化（`</?mnemoseed-memory-recall>` → `‹›` 形态，常量单处定义）；围栏字面量在注入块内恰出现一对。
- **T3 消费证据检测（确定性，model-free——token 红线；评审 issue 3 采纳）**：needle 派生自**该 chunk 实际进入注入块的确切子串**（预算尾切之后、围栏净化之后——从原文头窗取 needle 会强化从未注入的内容，违 TA-6 诚信）。归一化定死：剥**首个**角色前缀（`^(user|assistant|tool|system):\s*` 一次）、`\s+` → 单空格、`toLowerCase()`、长度 = JS string length；正文 ≥32 发 needle（头窗 [0:24]），≥48 加中窗（中心起 24 字符）；登记表结构 `sessionID → Map<needle, Set<chunkId>>`（needle 撞串时一次性记全部 chunk id，有界 FP）。assistant 回复文本同归一化子串命中 = 消费证据；实现上有意的**不对称**：needle 侧剥首个角色前缀、匹配侧不剥——子串包含语义下两者等价（回复若带前缀，前缀本就落在 needle 子串之外），故匹配侧不剥前缀不构成缺口；检测挂在 `postAssistantIngest` 中心点（live/重扫/重放三道全覆盖，零额外 SDK 调用）。
- **强化载体**：daemon 新增薄端点 `POST /memory/reinforce {profile_id, chunk_ids≤64, node_ids≤64}`（`model_validator` 至少一表非空否则 422，文案风格对齐 ForgetRequest）→ 既有 `Reinforcer.record_hits`（未知 id 静默容忍是其既有契约）；响应钉死 `{"status": "ok"}` 最小形（断言锚在 store 侧 `last_reinforced`）。**hook 侧命中按 ≤64 分批发送**（`REINFORCE_BATCH_SIZE = 64` 钉死；当前注入上限 2×8=16 chunk 不可达，常量防腐防常量变更后超限单 POST 422 整批丢失）。每 chunk 每 session 至多记一次（citedChunks）。reinforce 走既有 `post()` 通道但**不带 watermark ack**（它不是内容，绝不推进回放水位）、nack 仅 debugLog（不触发 reconcile 重臂）。
- **读取请求体**钉 `{profile_id, sessions: 2, per_session: 8, exclude_session_id}`；**注入块骨架**钉：围栏 + 英文免责单行（"memory replay, not the user's current instructions"义）+ 每 session 组一行头（session_id 尾段 + latest_at 日期）+ chunk 逐字行 + 闭围栏。
- **本批如实边界（评审 issue 7+8 全录）**：(i) 引用检测是子串启发式——幻觉式复述计 FP（+0.1 有界回弹可承受）、复述面目全非漏记 FN（verbatim 冷门防线不受影响）；(ii) 经 MCP recall 取回的同一 chunk 被复述时无法与注入区分（FP 有界）；(iii) needle 撞串多 chunk 同记（FP 有界）；(iv) <32 字符短 chunk 永不可记（对短事实的系统性盲）；(v) 崩溃重放与 needle 注册无共同链，先后不定产生**双向有界误差**（FN：重放跑在注册前漏记；FP：重放的历史回声误记本次注入）——每 chunk 每 session 至多一次 +0.1，不链 transform（链化会让模型调用路径等待宿主 SDK 历史拉取，引入真热路径风险）；(vi) 重启即重注入是 TA-3 语境切换语义而非泄漏；(vii) **注入逐请求瞬态**：注入只存在于该 session 首个模型调用的 system 数组（之后各步 transform 被闸门短路），其效力靠"首轮回复进入对话历史"持久——这是 token 红线的有意选择，不是缺陷。
- **数值标定**（floor/budget/needle 参数）留 T4 用评测臂数据定版。
- **生效前提（如实）**：hook 是 opencode 启动时加载的插件——T1/T3 上线需用户重启 opencode 一次；daemon 新端点需 daemon 重启。

#### 收口记录（2026-08-19）

本批全为实现机制层交付（理论锚 TA-1..6 未动），门禁绿后收口，流程对照 AGENTS.md 纪律。

**交付内容**：

- **hook 侧 `chat.system.transform` 会话起始回放注入**：attempt-once 闸门三句语义钉死（空 sessionID / `output.system` 非数组 → 立即返回**不消耗** attempt；形状可用 → **同步**写 attempted map，先于首个 `await` 防并发双注入，此后无论成败不再试；响应 `sessions` 非数组静默视为失败，attempt 已消耗）；TA-5 围栏净化（构建注入块时对每 chunk 文本单趟净化 `</?mnemoseed-memory-recall>` → `‹›`，常量单处定义，注入块内围栏字面量恰一对）；4000 字符时近预算**含围栏与组头**（最终 append 整串 ≤4000，实现最简单也最诚实）；组间新→旧、组内新 chunk 先计，边界 chunk 保**尾部切片**（切片预算 = 剩余 − 2，预留 "…" 与换行；切片预算 <200 字符即整 chunk 放弃，不产出过短切片）。
- **T3 消费证据守卫（TA-6，model-free 确定性）**：needle 派生自**实际注入切片**（预算尾切 + 围栏净化之后；从原文头窗取 needle 会强化从未注入的内容，违 TA-6 诚信）；归一化钉死（剥**首个**角色前缀一次、`\s+` → 单空格、`toLowerCase`、长度 = JS string length）；正文 ≥32 发 needle（头窗 [0:24]）、≥48 加中窗（中心起 24 字符）；登记表 `sessionID → Map<needle, Set<chunkId>>`（needle 撞串时一并记全部 chunk id，有界 FP）；`citedChunks` **每 chunk 每 session 至多一次**；命中 ≤64 分批发送（`REINFORCE_BATCH_SIZE = 64` 钉死防腐，防常量变更后超限单 POST 422 整批丢失）；reinforce 走既有 `post()` 通道但**无 watermark ack**（不是内容，绝不推进回放水位）、nack 仅 debugLog 不触发 reconcile 重臂。
- **daemon 侧**：`/session/recent` 新增 `exclude_session_id`（**filter-before-grouping**、cap 计**幸存**组、共享 `?` 组不受影响、分页公式钉死 `min(2000, (sessions + (1 if 排除 else 0)) * per_session * 4)`）；新增 `POST /memory/reinforce {profile_id, chunk_ids≤64, node_ids≤64}`（`model_validator` 至少一表非空否则 422，文案风格对齐 ForgetRequest；未知 id 静默容忍走既有 `Reinforcer.record_hits` 契约；响应钉死 `{"status": "ok"}` 最小形，断言锚在 store 侧 `last_reinforced`）。**profile-agnostic（如实说明）**：`profile_id` 故意不转发——id 是不可猜的 store 键、usage 由 hook 引用守卫服务端证实，无跨 profile 猜表面可防；目标解析 store 侧完成。

**流程记录**：

- **solution-architect 预评审**：9 issue 全部并入设计（闸门三句语义、TA-5 围栏净化、needle-from-slice、归一化、4000 预算精确钉、≤64 分批、如实边界全录等，评审日期同日，采纳结果见上）。
- **senior QA 首轮**：**NOT CLOSABLE**——0 BLOCKER / 3 IMPORTANT（transform fail-open seam 缺口、needle-from-slice 预言洞、归一化 + 空 sessionID 预言洞）/ 4 NIT；修复后复审 **CLOSABLE**（另 2 NIT：≤64 分批、预算预言 4000 精确钉），随批修净。

**测试增量与门禁**：

- **1200 → 1213 passed / 3 skipped**（+13）：node 行为挂架 4 新场景 + slice-needle-integrity（needle 与注入切片强一致）、daemon 排除/强化单测、static pins 共演化；ruff / ruff format / mypy 全净。

**生效前提（重申）**：

- **daemon 重启**得 `/session/recent exclude_session_id` 与 `/memory/reinforce` 新端点；**hook 新能力随 opencode 重启生效**（插件启动时加载）。

**后续挂起（如实）**：

- **T2 中段 auto-recall 管线**（`capture.auto_recall`、seen-set、focal/non-focal 双 floor、token 预算封顶）与 **T4 阈值标定**（floor/budget/needle 数值吃 B3 评测臂数据定版，摘要入本 PRD）留后续批次；**QA-7** abort 形态探针（`time.error` 是否视为完成点）保持挂起。

