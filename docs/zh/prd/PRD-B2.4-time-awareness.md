# PRD · B2.4 跨 session 时间感知：会话时间窗面 + 来源归因结构

> 依据：
> - 用户立项（2026-08-19，PRD-B2-roadmap 挂起项原案）：主 session 开发 B2.1-T2 期间，用户在**并行的新 session** 讨论落盘了 `docs/zh/design/research-agent-memory-benchmarks.md`（未跟踪文件）；主 session 发现时不识、误判为旧 session 遗留。期望能力：agent 遇到陌生产物时能**自然回忆到它发生在并行新 session**——判别依据是时间对比（产物 mtime vs 本会话窗口/各 session 时间窗），recall/recent 结果需带上可对比的时间窗。和 T1 回放注入、T2 中段 auto-recall 的线索工程同族。
> - 解锁条件已满足：daemon 可靠性永久修复 + 重装（B2.3，PR #14/#15，2026-08-19）。
> - solution-architect 综合评审（2026-08-20）verdict：**SHIP-WITH-ADJUSTMENTS**（IMP-1..5 + NIT-1 全部并入本文定案；另有两条 orchestration 调整记于"批次执行记录"）。

## 理论锚（本专题新增 TA-7..TA-9；TA-1..6 继承不变）

入选标准同 B2.1：只列有实验与长期复现证据的规律；每条给出来源、规律原文级表述、及它推导出的设计规则。理论回答"为什么这样设计"；格式化/扫描上限/缓存属实现机制层，不入本节。

### TA-7 时序语境作为残余线索 —— 时间窗是读面一等结构

- 来源：Howard & Kahana（2002）temporal context model——语境切换后，时间近因与项目间的时序接续是**残余语境**的主要携带者；TCM 被多任务复验（连续记忆、自由回忆输出序），与 TA-3 同源不同义。
- 已验证规律：编码时间接近的项目互为线索；语境切换后"什么与什么同时发生"只能靠时间结构恢复。
- → 设计规则：**会话时间窗（first/latest）是全部读取面的一等可对比结构**（recall 条目、recent 组、windows 工具一致携带）；**daemon 只供给结构、从不判归属**——mtime 落在哪个窗内的判断归消费侧模型完成。

### TA-8 来源监控 · 归因支架（TA-5 的姊妹锚）

- 来源：Johnson 等 source-monitoring framework（1993）——与 TA-5 同框架；内容"来自哪次经历"的判定天然易错，主动归因（含"我是否写过这个文件"）同样易错。
- 已验证规律：来源判别在无外显线索时系统性偏斜（把陌生内容归给熟悉来源）。
- → 设计规则：**读面必须携带逐项可比的来源线索**（session_id 原样；无标记者显式以共享 `?` 组/null 呈现），**绝不把缺省猜成旧会话**；hook 注入的自锚行是事实行（"你的会话始于 X"），不是解释。

### TA-9 双加工再认 —— 实败是回忆失败，不是熟悉度失败

- 来源：Yonelinas（2002）dual-process recognition——熟悉度（familiarity）与回忆（recollection）是两条独立通道；熟悉度提供"见过感"，回忆恢复语境。
- 已验证规律：当熟悉度低且回忆失败时，项目被判为陌生/错源（本案：文件"不识"且被误判旧遗留——典型的回忆失败形态）。
- → 设计规则：**系统支持回忆：暴露会话窗让模型做时间基的语境恢复；不制造熟悉感**（不加"时间相似度"检索项、不改排序）。结构给人，判断归人。

### 机制层事实（非理论锚，如实记录）

- 模型对裸 epoch 浮点的直觉比较/算术不可靠；ISO-8601/相对时间串可靠得多。→ 机制规则：daemon 统一格式化新时间字段为 ISO-8601 UTC（`storage/drivers/_time.py` 既有 `iso8601_utc`）。
- 模型不知道自己当前会话的身份与起点，除非被告知。→ 机制规则：hook 在 T1 围栏内注入一行自锚（会话尾段 + 起点 ISO）。

### 不借清单（本专题新增；B2.1 既有条目继承）

- "扩散激活自动处理时间"——否：ACT-R base-level learning（TA-1）只把时近折成一个幂律项，不产生任何会话边界语义；会话窗是独立结构，不可由激活导出。
- "时间细胞/海马重放可以解释来源归因"——否：单研究神经主张，无复现。
- "熟悉度判断可靠用于来源归因"——否：与来源监控文献相反，且本案实败恰是熟悉度通道未提供信息。

## 范围（批次任务）

- **T1 会话窗口端点**：daemon `POST /session/windows` + MCP 工具 `session_windows`。
- **T2 读面时间结构补齐**：recall 条目带 `session_id`/`ingested_at`；`recent_sessions` 组带 `window`/`window_truncated`、顶层 `self_window`，请求增 `self_session_id`。
- **T3 hook 自锚与组头窗口**：T1 注入块内 `<session-self/>` 自锚行 + 组头 `started=` 属性。
- **T4（挂起，本批不依赖）**：宿主 MCP 环境注入探针（M4a）——探 opencode 是否给 MCP 进程注入 per-session 环境变量；预测结论是无（MCP 进程跨 session 常驻），仅当未来需要 session 感知 MCP 工具时再跑。

## 设计定案（机制层；评审 issue 全并入）

### 面上清单

**发船**：
- **M2 会话窗面**（本案核心闭环）：新端点 + 新工具。agent stat 到产物 mtime → `session_windows(5)` → 找到包含 mtime 的窗。确定性、model-free、agent 自发调查（TA-4 合规）、不用时零 token。窗值由**逐 session 精确扫描**得出（`list_chunks(ChunkFilter(profile_id, session_id), Page(0, LIMIT))`），非 recent 的分页可见片段。
- **M1 recall 条目来源**（立项原案明令"recall/recent 结果需带可对比时间窗"）：`AssembledEntry` 增默认字段 `session_id`/`ingested_at`，`_entry()` 从候选项直接读（recall 热路径零额外 store 读），载荷渲染 `session_id`（原样|null）+ `ingested_at`（ISO|null）；graph 条目诚实 null/null（整合节点无单一会话，`updated_at` 是合并写时间，借它会制造来源混淆，违 TA-8）。
- **M2b recent 组窗**（增补字段）：组 `window:{first,latest}` + `window_truncated`；**窗值同样走逐 session 精确扫描**（orchestration 调整，见执行记录），`window_truncated` 精确语义 = 该 session 行数超扫描上限。既有 epoch `latest_at` 与 chunk `ingested_at` 字节不动。
- **M3 格式纪律**：daemon 是唯一格式化权威；全部**新**时间字段 = ISO-8601 UTC；同一比较结构内绝不混 epoch 与 ISO。hook 不重格式化（既有 `isoEnded` 仅作旧 daemon 回退）。
- **M4b hook 自锚**：`SessionRecentRequest` 增可选 `self_session_id`；响应顶层 `self_window:{session_id,window,chunk_count,active}|null`。hook 的 T1 读取（已带 `exclude_session_id`）**同一次 awaited 调用**加带 `self_session_id`，零新增 awaited 网络调用。围栏内渲一行 `<session-self id="<尾段>" started="<ISO>"/>`；self_window 为 null 则省行。自我识别规则 = `session_windows` 结果里 `window.first` 与自锚 `started=` 相符者即本会话。
- **M5-lite 组头窗口**：组头增 `started="<ISO>"`，仅当 `window` 在场且未 truncated 时渲；否则**省属性**（KISS，不渲占位符）。~35 字符/组，环境化并行 session 感知的最小保险。
- **M6 不变**：MCP 显式调查是主通道（TA-4）；capture 链零改动。

**削减（理由记档）**：
- 完整 M5（并行 session 公告板段）——冗：T1 块已注入他 session 逐字尾部，重复付 token 只为窗界；M5-lite 以 ~70 字符拿同样边际价值。
- M4a（MCP 环境注入探针作为依赖）——降为可选挂起：MCP 进程跨 session 常驻，启动时 env 里的 sessionID 必然陈旧，永不能作自通道。
- M4c（daemon 猜"最新组=调用者"）——否决：hook 读已排除自身、MCP 调用者不是 session、`?` 共享组破坏该前提。
- 时间相似度检索项 / 熟悉度制造——否决：违 TA-9 与 model-free 读面不变量；daemon 不做模糊判断。
- session_windows 的 CLI verb——否决：消费方是 agent（MCP）；CLI recall 面不动先例保持。

### 表面形状规格（wire 定案）

1. **`POST /session/windows`**（新，风格对齐 `SessionRecentRequest`）：
   - 请求 `{profile_id, sessions?: int = 3 (ge=1, le=10)}`。
   - 响应 `{profile_id, sessions: [{session_id, window:{first,latest}(ISO), chunk_count, active, window_truncated}]}`，newest-first（同既有首见序）；`?` 组包含在内、`active: false`；`active` = 在 `WritingPipeline.sessions()` 进程内缓冲注册表中（路由经守卫 seam 取用，镜像 ingest 路由先例）。
2. **`/session/recent` 增补**：请求 `+= self_session_id?: str | None = None`；组 `+= window:{first,latest}(ISO), window_truncated: bool`；顶层 `+= self_window: {session_id, window, chunk_count, active} | null`（self_session_id 非空且有 chunk 时精确扫描得出，否则 null）。其余字段字节不动。
3. **`/memory/recall` 条目增补**：`entry += session_id: str|null（chunk 原样）、ingested_at: ISO|null（仅 chunk）`。`AssembledEntry` 两处默认 None 字段；唯一构造点 `_entry()` 填充。
4. **MCP**：`TOOLS` 增 `session_windows`（`{n_sessions?: integer (default 3, max 10)}`）；`call_tool` 映 `n_sessions→sessions`，body 仅 `{profile_id[, sessions]}`（网关不知 sessionID——v1 该工具不接 self）。
5. **hook**：T1 fetch body `+= self_session_id: sessionID`；`buildRecallInjection` 渲自锚行（`<session-self .../>`，免责行之后）与组头 `started=`；包裹计入最终块串，4000 含围栏组头预算不变量不变（最终 append 整串 ≤4000）；旧 daemon（字段缺席）回退今日渲染。无新增 awaited 调用、`post()` arity 不动、`EXPECTED_MAPPING` 无新行。

### 常量与 config

- `SESSION_WINDOW_SCAN_LIMIT = 2000`（镜像既有分页上限量级）；超限时 `window_truncated: true`。
- **config 键：无**。理据：T2 default-off 先例管的是"主动注入且有 token 代价的面"；本批全部是 (i) 只读确定性 model-free 结构，(ii) agent 自发（token 由调用选择支付），(iii) 已在 4000 预算内的 ~70 字符渲染。加门 = 零行为变化的过度工程。如实记边界：T1 块增自锚行 + 组头属性。

### wire 兼容审计（评审 IMP-3 钉死）

- `tests/test_mcp_gateway.py:113` 四工具精确集断言**必破** → 同批更到五（合法增补钉）。
- recent/recall hook 侧既有钉全部只断言既有字段值，不动字段缺席 → 增补安全；T3 needle 派生自 chunk text，窗/自锚是头行永不进 chunk 文本 → 零接触。
- CLI recall 渲染 `entry.get(...)` 宽容 → 安全。esbuild TS 语法门禁须续绿。

## 边界（如实）

1. 窗是**chunk 摄入窗**，非 session 真值（火忘延迟、30s 重放重叠、hook 捕获滞后、daemon 宕机空洞）；亚分钟级 mtime 对比不可靠，模型应以 ±分钟对待。
2. `window_truncated` 真值仅在 session 行数超 `SESSION_WINDOW_SCAN_LIMIT`（精确语义，非分页截断）。
3. 任何窗外产物（非捕获工具所建、本特性前遗留、他机）→ 诚实空结果"无可归因"，绝不猜。
4. 时钟域：mtime=文件系统钟、`ingested_at`=宿主钟（同机今日）；远端产物=偏斜，不处理。
5. `active` 进程内局部——daemon 重启清空缓冲注册表，直到各活 session 下次 ingest；静默未言的并行 session 在重启瞬间报 `active:false`。
6. graph 条目永 `session_id:null`、`ingested_at:null`——整合节点无单一会话，不造假。
7. 共享 `?` 组 = 无标 pin 聚集，非 session，`active:false`。
8. session-id 形态：MCP 面全量 id，T1 注入头只显尾段；跨面匹配用全量 id 或 `first==started=` 规则。
9. 旧 daemon + 新 hook（字段缺席）：hook 退化为今日渲染；混合版本为不受支持边界。
10. 明令不给：daemon 侧产物→session 映射、产物扫描、模糊时间分类、跨 profile/跨主机的窗、中段重注入（自锚随首轮回复入史持久，T1 瞬态语义不变）。

## 门禁与并行分解（2026-08-20 并行化纪律）

- **流 A daemon 读面**（`memory.py`+`assemble.py`；T1/T2 共居 `memory.py` 不得再拆）：新测试 `test_session_windows.py`、扩 `test_session_recent.py`/`test_retrieve_assemble.py`/`test_daemon.py`。
- **流 B MCP 网关**（`server.py`+`test_mcp_gateway.py`，含四→五工具钉更新）。与 A 全并行。
- **流 C hook 切片**（`plugin.ts`+`test_hosts_opencode.py`+`test_hook_ts_behavior.py`）。三面文件集互不相交；C 按本 PRD 契约写钉，以集成门禁兜底。
- DRY 要求：newest-first 分 session 步与逐 session 精确窗扫描在 `memory.py` 提取共享 helper，三处消费（windows 端点 / recent 组窗 / self_window），不得复制三份。
- 跨流规则：全量门禁若见流外文件红，作为 cross-stream 如实报告，不动手修。

### 测试预言与变异体（按流，对抗式）

- **windows**：精确 first（长旧 session + 短新 session 夹具杀"分页可见代替精确"变异）、ISO 正则全匹配钉（杀 epoch 混入）、active 真值（未 settle 缓冲 session 夹具杀"恒 false"）、truncated 真值（超限夹具）。
- **recent/recall 增补**：truncated 仅超限组为真（杀"恒 false"）；self_window 只含窗字段绝无 chunk 文本（杀夹带）；graph 条目 null/null（杀"updated_at 充 ingested_at"）；ISO 正则钉。
- **网关**：五工具集、`n_sessions→sessions` 映射、payload 透传（StubClient.calls 记录模式）。
- **hook**：fetch body 带 self_session_id；自锚行在围栏内且 self_window 在场时恰一次；`started=` 仅 window 在且未 truncated 时；无第三个 awaited fetch（不变量注释钉 + 行为场景）；旧 daemon 缺字段不炸（回退场景）。

## 批次执行记录（随批追加）

- **2026-08-20 设计评审**（solution-architect，会话 ses_fe4be1619ffeU4eSKORvsqCkee）：verdict SHIP-WITH-ADJUSTMENTS；IMP-1（AssembledEntry 默认字段、热路径零额外读）、IMP-2（组窗误导风险 → truncated 旗 + 精确扫描归属独立端点）、IMP-3（四工具钉碰撞）、IMP-4（自锚走 hook 既有 T1 读取，MCP env 探针降级为挂起）、IMP-5（active 经守卫 seam + 进程内边界记档）、NIT-1（同结构内不混 epoch/ISO）全部并入定案。
- **orchestration 调整两条**：(i) recent 组窗不采用"分页可见 + 截断旗"方案，统一走逐 session 精确扫描（与 /session/windows、self_window 共享 helper，truncated 语义精确化为超限；满足 IMP-2 意图且消歧歧义）；(ii) 组头 truncated 时省 `started=` 属性（不渲占位符）。
- **前提质询（记档）**：意愿 vs 结构——实证表明失败 agent 已调查（"提交前先查来源"），瓶颈是归因步缺可对比结构、继而来源监控式脑补"旧遗留"；故最小闭环 = 结构供给（TA-8/TA-9），M5-lite 仅为消除"须想到去查"这一元认知依赖的环境化保险（~70 字符），完整公告板段（M5）削减。

#### 收口记录（2026-08-20）

本批全为实现机制层交付（理论锚 TA-7..9 未动），门禁绿后收口，流程对照 AGENTS.md 纪律（3 平行 SWE 流 + 修复轮）。

**交付内容**：

- **daemon 读面**：新增 `POST /session/windows`（逐 session 精确扫描窗，`SESSION_WINDOW_SCAN_LIMIT = 2000`；`window_truncated` 精确语义 = 过滤后 `page.total > LIMIT`）；`SessionRecentRequest` 增可选 `self_session_id`；recent 组增精确 `window` + `window_truncated`、顶层增 `self_window`（精确扫描得出，窗字段绝无 chunk 文本，缺席/未知 → null）；recall 条目增 `session_id`（chunk 原样|null）+ `ingested_at`（ISO|null），graph 条目诚实 null/null；共享 DRY helper（`_discover_session_ids` / `_scan_session_window` / `_window_iso`）三处消费（windows / recent 组窗 / self_window）；`_window_iso` 非正 epoch → null（防 1970 陷阱）。
- **MCP 网关**：新工具 `session_windows`（`n_sessions` 默认 3 上限 10 → daemon `sessions`，payload 透传）；两处四工具精确集钉升五（`test_mcp_gateway.py` 与 initially-missed `test_mcp_gateway_retry.py`）。
- **hook 切片**：T1 单次 awaited POST 加带 `self_session_id`（零新增 awaited 调用）；围栏内自锚行 `<session-self id="<尾段>" started="<ISO>"/>`（self_window 在场时恰一行）；组头 `started=` 仅 window 在场且未 truncated 时渲、否则省属性；旧 daemon 字段缺席逐字节回退今日渲染；共享 `escapeAttr` 单一定义覆盖全部属性插值点（防御纵深，needle/文本通道零接触）；自锚+组头包裹计入 4000 预算。
- **修复轮（QA 首轮 → 双 SWE）**：truncated 差一修正（→ 过滤后 total 语义）；`?` 组 `chunk_count` 伪 0 → honest null（注：discovery 蕴含 ≥1 chunk，真 session 计数恒精确）；`capture.sessions()` 与异步 ingest 的迭代竞态 → pipeline 专用 `_turns_lock` 全路径加锁（无重入/死锁）；README 工具表补齐五个。

**流程记录**：

- solution-architect 预评审 **SHIP-WITH-ADJUSTMENTS**（IMP-1..5 + NIT-1 全部并入定案；orchestration 调整 2 条——recent 组窗统一逐 session 精确扫描、组头 truncated 时省 `started=`——已记上文批次执行记录）。
- 三路并行 SWE（文件面互不相交 A daemon / B 网关 / C hook）。
- senior QA 首轮 **NOT CLOSABLE**（0 BLOCKER；IMPORTANT-1 truncated 差一、IMPORTANT-2 `?` 组计数伪造、IMPORTANT-3 `capture.sessions()` 迭代竞态；NIT-4 属性未转义、NIT-5 1970 陷阱、NIT-6 README）→ 双 SWE 修复轮 → 复审 **CLOSABLE**（变异体全灭；page.total 偏差正确记档）。
- 遗留 NIT（记录级）：NIT-A 锁钉为结构钉非真并发压测（实现正确、确定性优先）；NIT-B 字面 `"?"` 命名 session 与共享 `?` 组不可分辨（既有设计限制，现实无触发）；NIT-C `submit_turn` 在事件循环持锁 O(1) 可忽略。
- 已知如实边界：本 PRD §边界 10 条继续有效 + 本轮修订边界——`?` 组 `chunk_count: null`（诚实未知，与 `window:null` 同款）；truncated = 过滤后总数超限（非 `len(items)`）；字面 `"?"` 冲突（NIT-B）。

**测试增量与门禁**：

- **1273 → 1297 passed / 3 skipped**（+24：新 windows 套件 + recent/self_window 钉 + assemble/recall 钉 + 网关五工具与映射钉 + hook 自锚/组头/回退/转义钉 + 锁钉 + 边界钉）；ruff / ruff format / mypy 全净（orchestrator 独立复验 1297）。

**生效前提（如实）**：

- `uv tool install --force .` + daemon 换新重启得 `POST /session/windows` 与读面增补；MCP 面随网关自然生效；hook 新渲染（自锚行 + `started=`）随 `hook install opencode` + **opencode 重启**生效（插件启动时加载）。

**后续挂起（如实）**：

- **B2.5** daemon on/off（设计定案已就位，本批后发）；**B2.6** 宿主 plugin 统一安装面（调研已落盘 `docs/zh/design/research-opencode-plugin-bundling.md`，前置 T0 式探针）；**多 session 互认知专题**（队列末位立项候选）；B2.3 挂起子项（boot 同步 dream 恢复挪出启动路径）照旧。

**PRD 定案修订（随收口并入）**：

- §边界第 2 条措辞精确化为"truncated = 过滤后 page.total 超限"；§边界第 7 条追加"`?` 组 chunk_count 为 null（诚实未知）"；§表面形状规格 recall 条目补 `_window_iso` 非正 → null；hook 规格补"属性插值统一经 escapeAttr"。
