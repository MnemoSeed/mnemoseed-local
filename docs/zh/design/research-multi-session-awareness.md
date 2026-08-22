# 调研文档 · 多 session 互认知机制 —— 从"事后归因"到"并行互见"的选项空间

> 性质：立项前研究落盘（2026-08-23），非 PRD、不产生代码承诺；作为**多 session 互认知专题**（`docs/zh/prd/PRD-B2-roadmap.md:53`，2026-08-20 用户立项，排在 B2.6 之后）的依据资料。
> 调研范围：本仓库既有基座（daemon 读面 / MCP 网关 / opencode hook / capture 并发保护）之上的并行互见机制设计空间——每条论断附 file:line 证据；凡需实证才能定论处标 **needs-probe** 并给出探针协议。
> 同族定位：B2.4 跨 session 时间感知交付了"事后归因"（会话时间窗 + 来源归因结构，TA-7..9）；本专题回答"并行互见"——同时活着的 sessions 如何互相认知对方在做什么/做了什么。
> 纪律声明：理论锚节按项目纪律只收经验验证规律并附明确不借清单；实现机制严格分离。本文不触碰 `REFERENCES.md`（理论锚注册表），锚行随 PRD 立项时再登记。

---

## 摘要：逐问结论表

| # | 问题 | 结论 |
|---|------|------|
| 1 | "并行互见"今天到底缺什么 | 三件事：G1 一跳可读的"谁在跑 + 在干什么"目录（现在只有几百字符的逐字尾部 + 不可读的尾段 id）；G2 新鲜度语义残缺（`active` 是进程内注册表语义≠最近活跃，且消费侧无 daemon 时钟参照）；G3 中段冻结（T1 每 session 只注入一次，并行画面必然陈旧且无陈旧告警） |
| 2 | 主力形态 | **MCP 主动拉取新动词 `peer_sessions`（pay-per-query）为骨干 + T1 既有单次读取搭车渲 ≤3 行纯属性存在线（~120-200 字符、空态零输出）为环境化保险**——复刻 B2.4"M2 主通道 + M5-lite 最小保险"的成功形状 |
| 3 | 推送/广播形态 | **今日不可行也不该做**：opencode 插件面没有"推进运行中上下文"的原语；中断恢复有实证代价（TA-12）；KISS 反对 |
| 4 | 宿主侧共享信号文件（绕过 daemon 的 sidecar） | **否决**：第二状态面 + 多 hook 进程写者竞争（Windows 文件锁痛点）+ 违背 B2.5 off 语义精神 |
| 5 | ambient 块要不要带对方内容头部 | **不带**：ambient 只渲属性（id 尾段/started/last），内容头部只在显式查询返回——理论锚（TA-10 目录≠复制）与 token 经济同向 |
| 6 | 理论锚 | **部分借用**（TA-10..12：交互记忆系统 / 共同基础 grounding / 任务切换成本），TA-7..9 继续治理归因面；人类群体规律→LLM 会话是类比迁移，如实标注并给出降级路径 |
| 7 | 是否需要前置探针 | 需要，但全部是零代码观察级（P0-P2），无 T0 式宿主行为风险 |

---

## 1. 问题定义与用户价值

### 1.1 命题

让并行运行的 sessions 互相认知到对方在做什么/做了什么。

### 1.2 宿主里的真实场景（全部在本项目历史中实发生过）

1. **并行子代理冲突检查**（AGENTS.md 委派执行纪律的直接后果）：主 session 派多个 senior-software-engineer 并行跑不相交文件面时，冲突检查目前靠人工向 solution-architect 发起全量侦察（2026-08-22 实例：一次真实 conflict-check 靠人工核对文件清单完成）。若各 session 可见彼此的存在与最近动作指针，第一手材料自动在场。
2. **陌生产物误判**（B2.4 立项原案，`PRD-B2.4-time-awareness.md:4`）：主 session 发现并行研究 session 落盘的未跟踪文档，不识、误判旧遗留。B2.4 给了事后归因结构（时间窗比对）；若当时有"另一个 session 正在跑、始于 X、最近活跃于 Y"的存在信息，误判在第一眼就不会发生——这正是从"事后归因"到"并行互见"的同族延伸。
3. **双实例并行开发**：用户开两个 opencode 实例（主线开发 + 旁路调研），同机同 profile。任一方动文件前，理想状态是一跳可知"另一边刚动过什么"。daemon 是天然聚合点：每个 opencode 实例的 hook 进程独立，但都指向同一 BASE_URL（`hosts/opencode/plugin.ts:50`），daemon 因此看见全部 session。

### 1.3 与 B2.4 的边界（不是重复立项）

B2.4 已覆盖互见的**静态骨架**：T1 注入块本来就携带其他 session 的逐字尾部（读的就是含他组的 `/session/recent`，`plugin.ts:467-498`），组头带 `started=`（M5-lite，`plugin.ts:304-308`）。B2.4 还明确削减过完整公告板段 M5（理由：尾部已在场，重复付 token 只为窗界，`PRD-B2.4-time-awareness.md:62`）。本专题的真实增量只有三件：

- **G1 目录缺失**："谁在跑、在干什么"需要读完几百字符逐字文本才能回答，而 session 尾段 id 不可读（`sessionTailId` 只取尾段，`plugin.ts:283-289`）；
- **G2 新鲜度语义残缺**：`active` = "自 daemon 启动以来摄入过且未 settle+prune"（注册表进出路径 `capture/pipeline.py:61-63,75-77,79-91`；边界记档 `PRD-B2.4-time-awareness.md:95`），不是"最近活跃"；且响应里没有 daemon 当前时间，消费侧无法把 `window.latest` 换算成"多久没动静"；
- **G3 中段冻结**：T1 每 session 只注入一次（`injectedSessions` 同步门，`plugin.ts:552-556`），长 session 里并行画面必然陈旧，模型对陈旧无告警。

G3 有一个既有的部分解：T2 中段 pull 每 ACKED 用户轮都会发生（armed∧acked 门，`plugin.ts:577-583`）——但它默认关（`capture.auto_recall` 默认 off），不能作为唯一通道。

### 1.4 用户价值判据（验收方向的种子）

- 冲突检查请求里出现对 peer 存在线/查询结果的引用，而非每次人工全量侦察；
- 2026-08-19 型误判场景重放时，环境化存在线或**单次** `peer_sessions` 调用即可消解归因错误。

---

## 2. 现有基座盘点（file:line 证据）

| 能力 | 实现 | 证据 |
|---|---|---|
| daemon `POST /session/windows` | 逐 session 精确扫描窗 + `chunk_count`/`active`/`window_truncated` | 服务层 `daemon/memory.py:837-862`；路由 `memory.py:1275-1283`；扫描上限 `SESSION_WINDOW_SCAN_LIMIT=2000` `memory.py:98`；`_scan_session_window` `memory.py:261-285`；ISO 化 `_window_iso` `memory.py:288-297` |
| daemon `POST /session/recent` | newest-first 逐字尾部分组 + `exclude_session_id`/`self_session_id`/`self_window` | 请求模型 `memory.py:170-177`；服务层 `memory.py:706-763`；分组 `_group_session_tails` `memory.py:300-348`；路由 `memory.py:1256-1272` |
| MCP 工具面 | 五工具精确集：recall / remember / dream_once / recent_sessions / session_windows | `mcp_gateway/server.py:57-141`（recent :106-124、windows :125-140）；分发 `server.py:159-203`；五工具钉 `tests/test_mcp_gateway.py:110`（retry 孪生钉见 roadmap:72 收口记录） |
| `_turns_lock` 并发保护 | 缓冲注册表快照与异步 ingest 的迭代竞态防护 | `capture/pipeline.py:59`（锁）、`:61-63`（submit_turn 注册）、`:68-70`（turns 快照）、`:75-77`（sessions 快照）、`:79-91`（prune_settled）；QA 起因 `PRD-B2.4-time-awareness.md:132` |
| hook 单次 awaited 注入 | T1 会话起始回放是整个 hook 唯一的会话级 awaited 读（不变量注释即合同） | 不变量声明 `plugin.ts:26-29`；fetch body 带 exclude+self `plugin.ts:474-480`；T1 同步门 `plugin.ts:552-556` |
| T2 bounded pull | armed∧acked 门控的 300ms fail-open 拉取（默认 off） | 常量 `plugin.ts:100`；pull 实现 `plugin.ts:500-533`；门 `plugin.ts:577-583` |
| 自锚行 / 组头 / 转义 | `<session-self/>`、`started=` 属性、共享 `escapeAttr` | `plugin.ts:296-302`（escapeAttr）、`:304-308`（groupStarted）、`:310-315`（sessionSelfLine）、包裹计账 `plugin.ts:352-363` |
| chunk 文本自带角色标签 | verbatim 文本按 `"user: …"/"assistant: …"` 行组装 | `capture/stamper.py:124-141`（`_assemble_text`）；hook 侧角色前缀剥离先例 `plugin.ts:230-238` |
| flush/settle 生命周期 | idle→flush 保可摄入；deleted 才 settle+prune+摘除 hook 态 | `plugin.ts:759-777`（flush/settle 语义与实现）、`:1156-1168`（deleted 清理）；daemon 侧 settle→drain→prune `daemon/ingest.py:84-138` |
| 显式 pin 无 session 归属 | remember 直建 stamp，`provenance.session_id=None` → 共享 `?` 组 | `memory.py:532-544`；`?` 组发现处理 `memory.py:236-243` |
| wire 合同钉 | 网关工具精确集断言；hook wire 表由测试解析；空载荷整包相等钉 | `tests/test_mcp_gateway.py:110`；`tests/test_hosts_opencode.py:32,264`（EXPECTED_MAPPING）；`tests/test_session_windows.py:250`（**exact 整包相等钉**） |
| B2.6 bundle 开关 | options 元组整体短路已探针确认 | `plugin.ts:36-43`（探针结论注释）、`:1063-1072`（短路实现）、`:1229-1241`（config hook 注入 mcp） |

关键语义细节（设计输入）：

- **"对方最近一条用户指令头部"可零 schema 改动提取**：chunk 逐字文本自带角色标签（`stamper.py:129-141`），newest-first 页内前缀匹配即可定位 user 头部；但显式 pin 文本无标签（`memory.py:525-544`）——提取必须容错无前缀情形，如实按"首行原文"对待。
- **freshness 数据已在手**：windows 与 recent 组都带精确 `window.latest`（`memory.py:278-285`），缺的只是可比的"现在"（→ `server_now` 机制事实，§4）。
- **`?` 组不是 session**：peer 目录必须排除它（镜像 `_discover_session_ids` 对它的特殊处理，`memory.py:236-243`）。
- **active ≠ 最近活跃**：一个几小时没说话但未删除的 session 在 daemon 未重启期间恒 `active:true`。peer 面若只透传 active 会制造"正在活动"错觉——必须同时暴露 last-activity 时间戳。

---

## 3. 机制选项空间

### 选项 A —— 被动注入式存在窗（ambient presence lines）

- 形态：`/session/recent` 响应加性增 `peers` 数组（≤3 条、纯属性字段：session_id、window.first/latest 或 last ISO、active、chunk_count 可选）；hook 在既有 T1 单次 awaited 读里消费该字段，围栏内渲 ≤3 行 `<peer-session id="<尾段>" started="<ISO>" last="<ISO>" active="…"/>`，计入 4000 含包裹预算（镜像 selfLine 计账先例 `plugin.ts:352-363`）。零新增 awaited 网络调用。
- 变体 A2：把 peer 新鲜度搭在 T2 pull 响应上（armed∧acked 时顺带渲染）——免费通道，但 auto_recall 默认 off → 覆盖受限，只能作补充。
- **收益**：消除"须想到去查"的元认知依赖——B2.4 前提质询已实证这是失败案的真正瓶颈（`PRD-B2.4-time-awareness.md:121`）；空 peers（独自工作常态）零输出零 token。
- **成本**：每个新 session 固定 ~120-200 字符（仅当真有并行者）；4000 预算内与回放内容争地盘（渲染优先序 PRD 定夺：建议 peer 线排在自锚行之后、逐字组之前——存在信息是"读其他一切的地基"）。
- **风险**：T1 一次性语义决定画面冻结（G3 只解一半）；陈旧信息若不携带 last 时间戳会误导（红线 §5.5）；旧 daemon 字段缺席需逐字节回退（B2.4 先例成立，成本低）。

### 选项 B —— 主动拉取 MCP 动词（on-demand peer probe）

- 形态：第六个 MCP 动词 `peer_sessions`（背后新 daemon 端点 `POST /session/peers`）：请求 `{profile_id, peers?: int}`，响应 `{profile_id, server_now, peers: [{session_id, window:{first,latest}, chunk_count, active, last_user_head?}]}`，newest-first，排除 `?` 组；`last_user_head` = 该 session 最新 user 角色行的 ≤N 字符 verbatim 头部（存量文本直读，零转述）。
- **收益**：token pay-per-query（agent 自发调查先例 = B2.4 M2 主通道，`PRD-B2.4-time-awareness.md:53`）；天然承载较重载荷（内容头部）；确定性 model-free；`server_now` 一并解决 G2 的新鲜度比较。
- **成本**：依赖模型选择调用——元认知瓶颈正是 B2.4 实证过的失败点（`PRD-B2.4-time-awareness.md:121`），所以单独不足以闭环（需 A-lite 补位）；第六动词触发网关精确集钉升六（四→五先例合法增补，roadmap:72；`test_mcp_gateway.py:110` 与 retry 孪生同批升）。
- **风险**：低。唯一合同面注意点：**不要**把 peer 载荷塞进 `session_windows` 的加性字段——`test_session_windows.py:250` 是整包相等钉，任何加性键必破它（合法升钉但纯属额外搅动）；新动词让既有 wire 表全部冻结。

### 选项 C —— 事件推送/广播（SSE/webhook 形态）

- 形态：daemon 暴露 SSE/WebSocket 或向宿主 POST webhook，宿主插件监听并把"有新并行 session 出现"通知进运行中会话。
- **否决理由（三条独立）**：
  1. **宿主无原语**：opencode 插件面只有 `chat.system.transform`（模型调用时才 fire）与总线事件（出站），没有"向运行中上下文推进一行字"的入站通道——推送到达后仍要等下一次 transform 才能呈现，实时性是假的（插件能力全景见姊妹篇 `research-opencode-plugin-bundling.md` §1-§5）；
  2. **中断代价**：任务切换/中断恢复有可测量的实证代价（TA-12），为"知道旁边有人"打断当前任务是负价值交换；
  3. **架构反向**：daemon→host 方向反转现有单向数据流，新增常驻连接与安全面，违 KISS 与 loopback-simple 启动纪律（`daemon/app.py:840-845`）。
- 保留观察位：未来 opencode 若提供合适的入站 hook 原语再议（届时才值得跑 needs-probe）。

### 选项 D —— 宿主侧共享信号文件（sidecar presence file）

- 形态：各 hook 进程往 `CONFIG_DIR` 下心跳文件追加行，peers 读之，绕过 daemon。
- **否决理由**：
  1. **第二状态面**：聚合职责本属 daemon（单一真相点），sidecar 制造双写路径与崩溃一致性负担；
  2. **多写者竞争**：多个 opencode 实例 = 多个 hook 进程并发写同一文件；Windows 文件锁正是本项目已趟过的坑区邻域（stdio cp936/换行双坑先例，roadmap:64）；无锁方案则丢行、加锁方案则把锁语义引进 hook 热路径；
  3. **违背 off 语义精神**：B2.5 的用户契约是"off = 记忆服务停"，一个 daemon 关掉后仍在工作的感知通道会悄悄复活半套服务（哨兵语义 `PRD-B2-roadmap.md:73`）。

### 对比表

| 维度 | A ambient 存在线 | A2 T2 搭车 | B MCP 动词 | C 推送 | D sidecar |
|---|---|---|---|---|---|
| 解决 G1 目录 | 部分（无内容头部） | 部分 | **完全**（含头部） | 部分 | 部分 |
| 解决 G2 新鲜度 | 部分（冻结于 T1） | 部分 | **完全**（server_now） | 完全 | 部分 |
| 解决 G3 中段 | 否 | 仅 auto_recall on 时 | 是（按需即最新） | 是 | 部分 |
| ambient token 成本 | ~120-200 B/session 起 | 零附加 | 零（pay-per-query） | 负（中断成本） | 零 |
| 元认知依赖 | **无** | 无 | 有（B2.4 实证瓶颈） | 无 | 无 |
| 新增状态/连接 | 无 | 无 | 无 | 常驻连接+方向反转 | 第二状态面+多写者 |
| 合同面碰撞 | recent 钉升补 | recall-pending 钉 | 六工具升钉（例行） | 大 | 大 |
| KISS | 高 | 高 | 高 | 低 | 低 |

### 推荐

**B 为骨干 + A-lite 为环境化保险**（复刻 B2.4 已验证的形状分工：M2 主通道 + M5-lite 最小保险）：

- 分工原则由理论锚双向锁定：ambient 线是最小 grounding 信号（TA-11：存在/身份/近况三属性足矣）；目录指向内容、绝不复制内容（TA-10）。内容头部属于显式查询——那里 agent 自己为 token 付费。
- v1 发船面：daemon `POST /session/peers` + MCP 第六动词 `peer_sessions` + `/session/recent` 响应加性 `peers`（≤3 条纯属性）+ hook 渲染（无新增 awaited 调用、EXPECTED_MAPPING 无新行）。
- DRY 强制：peer 发现与窗口扫描必须复用 `_discover_session_ids`/`_scan_session_window`/`_window_iso` 共享 helper（`memory.py:220-297`，B2.4 三处消费先例），不得出现第二份实现。

---

## 4. 理论锚（按纪律）

**判定：部分借用（TA-10..TA-12 新增候选）。** 三条均为人类群体/认知心理学中经验验证过的规律，各自干净地映射到一个形式决策（目录而非复制 / 最小 grounding 线 / pull 而非 push）。诚实标注：人类群体→LLM 会话是**类比迁移**——锚治理的是供给的*形式*，不承诺产出效果（效果归 dogfood 验收取舍）。若评审不接受这步迁移，本节可整体降级为"无借用——工程面"而不损机制设计：三条锚约束的全是呈现层选择，不是正确性依据。

### TA-10 交互记忆系统（transactive memory）—— 目录是索引，不是副本

- 来源：Wegner（1987）"Transactive memory: A contemporary analysis of the group mind"（Theories of Group Behavior 章节）；Wegner, Giuliano & Hertel（1985）亲密关系交互记忆实证；团队层元分析 DeChurch & Mesmer-Magnus（2010, Journal of Applied Psychology）——transactive memory system 结构质量与团队绩效正相关。
- 已验证规律：高效群体的关键不是成员互相复制全部内容，而是持有"谁知道什么/谁在做什么"的**元记忆索引**；索引可用性与协调绩效相关。
- → 设计规则：供给 **peer 目录（存在 + 身份 + 近况指针）**；内容本体永远按需拉取——ambient 行只渲属性，正文头部只在显式动词返回。daemon 不做任何转述（verbatim 通道红线）。

### TA-11 共同基础与 grounding 成本 —— 存在线是最小 grounding 信号

- 来源：Clark & Marshall（1981）相互知识与指称理解（Elements of Discourse Understanding 章节）；Clark & Brennan（1991）"Grounding in communication"（Perspectives on Socially Shared Cognition 章节）。
- 已验证规律：协作方需要最低限度的相互知识（对方存在、身份、状态）才能协调；建立共同基础本身有通信成本，成本结构由介质决定。
- → 设计规则：存在线恰三属性（id 尾段 / started / last），多一分都是未邀请的通信税；完整 grounding（读对方内容）永远走显式动作。继承 TA-8 纪律：属性行是**事实行**（"此 session 始于 X、最近活跃于 Y"），绝不做解释或断言（"它正在改你的文件"这类话永不出现在注入里）。

### TA-12 任务切换与中断恢复成本 —— pull 优于 push

- 来源：Monsell（2003, Trends in Cognitive Sciences）task switching 综述；Altmann & Trafton（2002, Cognitive Science）目标激活模型——挂起任务受干扰源竞争与时间积累双重惩罚。
- 已验证规律：任务中断产生可测量的恢复代价，且随挂起时长增长。
- → 设计规则：互见默认 **pull/搭车**（ambient 搭既有注入、深查走按需动词）；daemon 发起的推送中断被否决（选项 C 的理论根据，与宿主原语缺失相互独立地成立）。

### 继承（不变）

TA-7（时间窗是一等对比结构）、TA-8（来源监控：缺标记者显式呈现，绝不猜归属）、TA-9（双加工再认：结构给人、判断归人）继续治理 peer 归因面——peer 时间字段沿用 ISO-8601 UTC 格式化权威与"daemon 供结构、消费侧判断"分工（`PRD-B2.4-time-awareness.md:12-28`）。

### 机制层事实（非理论锚，如实记录）

- 模型不知道"现在几点"，除非被告知 → 相对新鲜度可比的前提是响应带 `server_now`（ISO）或等价物；否则 `window.latest` 只是死数据。
- session 尾段 id 不可读 → 沿用 B2.4 规则：短 id 展示 + `window.first==started=` 匹配识别自我（`PRD-B2.4-time-awareness.md:57,98`）。
- 显式 pin 文本无角色前缀（`memory.py:525-544`）→ 头部提取必须容错，无前缀按原文首行对待，绝不猜角色。

### 不借清单（本专题新增；B2.1/B2.4 既有条目继承）

- **跨脑同步/hyperscanning "team flow"**——小样本神经主张被流行文化放大，无稳定复现到工程可映射的规律。
- **集体意识/群体心智涌现**——无经验内容，纯隐喻。
- **镜像神经元使"理解他人动作意图"自动发生**——过度外推的经典案例；即便人类文献内部也远未支持"自动互知"。
- **Dunbar 数字硬套 session 并发上限**——人类社交群规模规律，与会话并发数无映射关系。
- **心智理论（ToM）的神经基础当实现承诺**——ToM 在这里最多是消费侧模型的类比；把它写成系统机制等于伪造能力。
- **Google 效应/数字失忆直接外推为多 agent 协调规律**——Sparrow et al.（2011）说的是个体对外部存储的信任性卸载，不是群体互知；硬套即神话。

---

## 5. 风险与红线

1. **并发写**：peer 发现 v1 是纯读（复用既有扫描 helper 即无新锁）；若未来加 daemon 侧心跳注册表，必须镜像 `_pending_lock`（`memory.py:389`）/ `_turns_lock`（`pipeline.py:59`）先例全路径加锁，audit 追加保持 best-effort（`app.py:1063-1067` 先例）。v1 明令不加新状态——recency 从存量 store 派生，零注册表。
2. **token 经济（无模型调用 in plumbing）**：peer 内容一律存量 verbatim 文本，daemon 零转述零摘要零模型调用（verbatim 通道红线）；ambient 上限固定小预算且空态零输出；深查 pay-per-query；头部字符帽在 PRD 定数（建议 ≤160 chars/条）。
3. **隐私 / local-first**：全程 loopback 单机（启动纪律 `app.py:840-845`）；peer 信息在同一 profile 信任域内流转，无新暴露面；**跨 profile/跨主机 peer 明令禁止**（D5 隔离，profile_id 显式于每个请求，`RecallRequest` 先例 `memory.py:110-118`）。
4. **fail-open**：hook 侧 peer 渲染包进既有 try/catch debug-only 外壳（`plugin.ts:626-629`）；旧 daemon 字段缺席逐字节回退今日渲染（B2.4 M4b/M5-lite 回退先例，`PRD-B2.4-time-awareness.md:76`）；daemon down → 无 peer 线，会话照常。
5. **陈旧误导**：存在线必须携带 last 时间戳；注入里永不出现"正在进行 X"式断言（只渲事实行，TA-11）；T1 冻结语义如实记档为已知边界，不解之处靠按需动词补。
6. **合同面碰撞**：第六动词 → 网关精确集钉同批升六（`test_mcp_gateway.py:110` + retry 孪生）；EXPECTED_MAPPING 无新行纪律保持——peers 必须搭既有 awaited 读，不得出现第三个 awaited fetch（B2.4 测试预言先例，`PRD-B2.4-time-awareness.md:115`）；`/session/windows` 不做加性扩展（整包相等钉 `test_session_windows.py:250`）；4000 含包裹预算不变量保持——peer 线计入 wrapper 计账（`plugin.ts:352-363` 形状）。
7. **注入卫生**：v1 纯属性方案天然规避文本注入；若未来 ambient 携带任何文本，必须走 `sanitizeRecallText`/`escapeAttr` 既有单点（`plugin.ts:257-268,296-302`），禁止旁路插值。

---

## 6. 立项建议

### 6.1 范围切刀（v1 建议）

**发船**：
- daemon `POST /session/peers`：`{profile_id, peers?: int=3 (ge=1, le=10)}` → `{profile_id, server_now, peers:[{session_id, window:{first,latest}, chunk_count, active}]}`；newest-first；排除 `?` 组；recency 全部派生自存量扫描 + buffer 注册表，**零新状态**。
- MCP 第六动词 `peer_sessions`：`{n_peers?}` payload 透传；响应含可选 `last_user_head`（≤N chars verbatim，仅此动词携带——ambient 面永不携带内容字段）。
- `/session/recent` 响应加性增顶层 `peers`（≤3 条、纯属性、无内容字段）；hook 于既有 T1 单次读取消费，围栏内、免责行与自锚行之后、组头之前渲 ≤3 行 `<peer-session id=".." started=".." last=".." active=".."/>`；无 peers 零输出。
- 共享 helper 复用强制（DRY，`memory.py:220-297`）。

**削减（理由记档）**：
- ambient 正文头部（对方内容进系统提示）→ 违 TA-10 目录/内容分离 + 未邀请付 token；
- 推送/广播（选项 C）→ TA-12 + 宿主无原语；
- sidecar（选项 D）→ 第二状态面 + 多写者 + off 语义冲突；
- 冲突检测器（文件锁管理、编辑碰撞预警）→ 超出记忆系统职责，未来独立专题；
- CLI verb → 消费方是 agent（MCP），CLI recall 面不动先例保持（B2.4 同款裁决，`PRD-B2.4-time-awareness.md:66`）。
- config 键 → v1 无（镜像 B2.4 理据 `PRD-B2.4-time-awareness.md:81`：只读确定性 model-free 结构 + agent 自发付费 + 空态零行为变化；auto_recall 默认 off 的教训不适用——本面没有默认 token 流出）。

### 6.2 验收方向

- 确定性预言（对抗式）：self 排除（exclude 语义镜像）；`?` 组不在 peers；honest null（未知即 null，绝不伪 0）；`server_now` ISO 正则钉；同结构内不混 epoch/ISO（B2.4 NIT-1 先例）；六工具升钉 + windows 整包钉不动；EXPECTED_MAPPING 无新行 + "无第三 awaited fetch"行为场景钉；4000 包裹计账含 peer 线；escapeAttr 全插值点覆盖；旧 daemon 缺字段逐字节回退场景。
- Dogfood 验收：§1.4 两判据——冲突检查引用 peer 材料；2026-08-19 型误判场景一跳消解。

### 6.3 前置探针（全部零代码观察级，无 T0 式宿主行为风险）

- **P0 并行形态普查**：dogfood 一周统计并发 session 数分布、flush 后尾部新鲜度体感——决定 ambient ≤3 够不够、last 粒度是否够用。
- **P1 子代理会话形态**：确认并行 SWE 子代理以独立 session_id 进入同一 profile 且 recent 面可见（当前环境记忆强烈提示成立，仍需正式记录）；顺带探 opencode hook 载荷是否携带父子 session 关联——若无，v1 不区分顶层/子代理 session，如实记档。
- **P2 token 开销实测**：经 debug lane（`MNEMOSEED_LOCAL_DEBUG`，`plugin.ts:103-113`）记录 peer 线实际字节一周，验证 ~120-200 字符估计。

---

## 附：开放问题（如实）

1. **父子 session 关联**：opencode hook 载荷里是否有可用的 parent 链接？若无，peer 目录里顶层与子代理 session 无法区分——这对冲突检查价值损伤多大？（P1 回答）
2. **新鲜度阈值**：v1 只给事实（latest + server_now）不给"live/dead"分类，消费侧自己比。是否需要一个 daemon 判好的 `recent: bool`？（倾向不需要：判归属归消费侧是 TA-7/TA-9 既定分工；PRD 定夺）
3. **噪声门**：是否仅在"当日确有 ≥2 个 session 活动窗口重叠"时才渲 ambient 线？无条件渲染更简单，独占日也只付几十字节的检查成本。（倾向无条件 + 空 peers 零输出已足够 KISS）
4. **T1 冻结的实际伤害**：长 session 内 peer 画面陈旧的误导频率需要 dogfood 数据；若高频，A2（T2 搭车）升级为正式面的条件是什么？
5. **`last_user_head` 的隐私粒度**：他人指令头部进入查询响应（非注入），是否有场景需要更保守的"仅长度不预览"档？
6. **dream 整合面的远期协同**：整合后的 graph 节点能否在不违 token 经济的前提下，为 peer 提供 topic 级摘要（dream 本就是管道外模型调用）？远期方向，v1 不碰。
7. **跨 profile**：永久禁区（D5）还是未来可配置的显式桥？（倾向永久禁区；记录在案防无据翻案。）
8. **`session_windows` 扩展 vs 新端点的最终取舍**：本研究推荐新端点/新动词以冻结既有 wire 表（整包相等钉证据 `test_session_windows.py:250`）；若 PRD 阶段认定动词数成本更高，反转前需先列清钉位代价清单。
