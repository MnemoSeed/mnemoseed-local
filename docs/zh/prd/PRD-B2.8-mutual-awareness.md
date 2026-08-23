# PRD · B2.8 多 session 互认知：peer 目录动词 + ambient 存在线

> 依据：
> - 立项：`PRD-B2-roadmap.md:53`（2026-08-20 用户立项，多 session 互认知专题）；调研依据全文 `docs/zh/design/research-multi-session-awareness.md`（2026-08-23 落盘，逐论断附 file:line 证据）。本文把调研 §6.1 范围切刀与 §6.2 验收方向升格为**绑定需求**，附录开放问题 1–8 逐条定案或挂起（见"开放问题处置"表）。
> - orchestration 定案（2026-08-23）：MVP 恰为三面——daemon `POST /session/peers` + 第 6 个 MCP 动词 `peer_sessions` + `/session/recent` 加性 peers 数组；护栏五条、wire 冻结四处、v1 削减清单、跨 profile 永久禁区，以下全部写为固定决策，不再重开。
> - 前置依赖：P0/P1/P2 探针（issue #75）须在**实现批启动前**收满出报告；本文起草与探针并行，不受其阻塞。挂起型开放问题的定夺截止线 = 实现批启动前。
> - 本文由 solution-architect 依 orchestration 决策起草（2026-08-23，docs-only 批次）；后续调整记入"批次执行记录"，不回写正文定案。

## 成功判据（先行；可观测结果，拒绝"能看到彼此"式空话）

| # | 判据 | 观察点 |
|---|---|---|
| SC-1 | 并行批次的冲突检查请求里出现对 peer 材料的引用（ambient 存在线的 id/时间，或 `peer_sessions` 返回），替代每次人工全量侦察 | 冲突检查会话记录（dogfood） |
| SC-2 | 2026-08-19 型误判场景重放（工作树现陌生产物）：第一轮回复内完成正确归因——环境化存在线或**单次** `peer_sessions` 调用即消解，零人工提示 | 重放会话记录（dogfood） |
| SC-3 | P2 字节预算验证：有并行者会话的 peer 线实际字节分布被记录并对照 ~120–200 字符估计带；空 peers 会话的注入块与今日**逐字节一致**（零增量） | debug lane（`MNEMOSEED_LOCAL_DEBUG`）日志统计 ≥1 周（issue #75 P2 报告） |

SC-1/SC-2 = 调研 §1.4 两判据原样升格；SC-3 = P2 实测。若实测否定估计带，修正属 P2 报告的决策项、实现批启动前定夺，不预建补救机制。

## 理论锚（本专题新增 TA-10..TA-12；TA-1..9 继承不变）

入选标准同 B2.1/B2.4：只列有实验与长期复现证据的规律；每条给来源、规律原文级表述、及它推导出的设计规则。理论回答"为什么这样设计"；字符帽/行数上限/渲染序属实现机制层，不入本节。

### TA-10 交互记忆系统（transactive memory）—— 目录是索引，不是副本

- 来源：Wegner（1987）"Transactive memory: A contemporary analysis of the group mind"（*Theories of Group Behavior* 章节，pp. 185–208；已登记 R38）；团队层元分析 DeChurch & Mesmer-Magnus（2010, *Journal of Applied Psychology*；R39）——transactive memory system 结构质量与团队绩效正相关。
- 已验证规律：高效群体的关键不是成员互相复制全部内容，而是持有"谁知道什么/谁在做什么"的**元记忆索引**；索引可用性与协调绩效相关。
- → 设计规则：供给 **peer 目录（存在 + 身份 + 近况指针）**；内容本体永远按需拉取——ambient 行只渲属性，正文头部只在显式动词返回。daemon 不做任何转述（verbatim 通道红线）。

### TA-11 共同基础与 grounding 成本 —— 存在线是最小 grounding 信号

- 来源：Clark & Marshall（1981）相互知识与指称理解（*Elements of Discourse Understanding* 章节；R40）；Clark & Brennan（1991）"Grounding in communication"（*Perspectives on Socially Shared Cognition* 章节；R41）。
- 已验证规律：协作方需要最低限度的相互知识（对方存在、身份、状态）才能协调；建立共同基础本身有通信成本，成本结构由介质决定。
- → 设计规则：存在线恰三属性（id 尾段 / started / last）+ 进程内 active 旗，多一分都是未邀请的通信税；完整 grounding（读对方内容）永远走显式动作。继承 TA-8 纪律：属性行是**事实行**（"此 session 始于 X、最近活跃于 Y"），绝不做解释或断言（"它正在改你的文件"这类话永不出现在注入里）。

### TA-12 任务切换与中断恢复成本 —— pull 优于 push

- 来源：Monsell（2003, *Trends in Cognitive Sciences*）task switching 综述（R42）；Altmann & Trafton（2002, *Cognitive Science*）目标激活模型——挂起任务受干扰源竞争与时间积累双重惩罚（R43）。
- 已验证规律：任务中断产生可测量的恢复代价，且随挂起时长增长。
- → 设计规则：互见默认 **pull / 搭车**（ambient 搭既有注入、深查走按需动词）；daemon 发起的推送被否决——该裁决与宿主原语缺失相互独立地成立（见削减表）。

### 类比迁移声明（诚实规则，调研 §4 原样继承）

三条锚均为人类群体/认知心理学规律，映射到 LLM 会话是**类比迁移**——锚治理供给的*形式*（目录而非复制 / 最小 grounding / pull 而非 push），不承诺产出效果；效果归 SC-1..3 的 dogfood 验收取舍。若评审不接受这步迁移，本节可整体降级为"无借用——工程面"而不损机制设计：三锚约束的全是呈现层选择，不是正确性依据。

### 继承（不变）

TA-7（时间窗是一等对比结构）、TA-8（缺标记者显式呈现，绝不猜归属）、TA-9（结构给人、判断归人）继续治理 peer 归因面：peer 时间字段沿用 ISO-8601 UTC 格式化权威与"daemon 供结构、消费侧判断"分工（格式权威见 B2.4 机制层事实与 M3 格式纪律，`PRD-B2.4-time-awareness.md:32/:56`）。

### 机制层事实（非理论锚，如实记录）

- 模型不知道"现在几点"，除非被告知 → 相对新鲜度可比的前提是响应带 `server_now`（ISO）；否则 `window.latest` 只是死数据。仅显式动词面携带（边界 3）。
- session 尾段 id 不可读 → 沿用 B2.4 规则：短 id 展示 + `window.first == started=` 匹配识别自我。
- chunk 文本自带 `"user: "` 角色前缀（`capture/stamper.py:124-141`），newest-first 页内前缀匹配即可定位最新 user 行；无前缀情形如实按原文处理，绝不猜角色。

### 不借清单（本专题新增；B2.1/B2.4 既有条目继承）

- **跨脑同步/hyperscanning "team flow"**——小样本神经主张被流行文化放大，无稳定复现到工程可映射的规律。
- **集体意识/群体心智涌现**——无经验内容，纯隐喻。
- **镜像神经元使"理解他人动作意图"自动发生**——过度外推的经典案例；人类文献内部也远未支持"自动互知"。
- **Dunbar 数字硬套 session 并发上限**——人类社交群规模规律，与会话并发数无映射关系（本文 ≤3/≤10 是 token 经济数字，不引 Dunbar）。
- **心智理论（ToM）的神经基础当实现承诺**——ToM 在这里最多是消费侧模型的类比；写成系统机制等于伪造能力。
- **Google 效应/数字失忆直接外推为多 agent 协调规律**——Sparrow et al.（2011）说的是个体对外部存储的信任性卸载，不是群体互知。

## 语义定版（本包拍板，写死进测试）

1. **记忆层不是协调层**：peer 面只供事实目录（谁存在 / 何时起 / 最近何时动 / 多大），永不编排、永不传话、永不建议行动；注入里只有事实行。
2. **目录不是内容**（TA-10）：ambient 面（recent 的 peers 数组 + hook 存在线）纯属性、零内容字段；全系统唯一内容载体 = `peer_sessions` 显式返回的 `last_user_head`。
3. **pull 永不 push**（TA-12）：无推送/广播/常驻连接/webhook；一切互见要么搭既有读取，要么按需拉取。
4. **零新状态 / 零新 config 键 / hook 零新增 awaited fetch**：recency 与目录全部派生自存量 store 扫描 + 进程内 buffer 注册表；hook 的 peers 消费搭既有 T1 单次读取。
5. **verbatim 红线**：daemon 管道内零转述、零摘要、零模型调用；`last_user_head` 是存量文本的机械截取（角色前缀剥离 + ≤160 字符截断），不是生成。
6. **新鲜度 = 事实对**：只给 `window.latest`（ISO）+ `server_now`（ISO），不做 live/dead/recent 分类；陈旧判断归消费侧（TA-7/TA-9 既定分工）。
7. **空态零输出**：peers 为空/缺席时 ambient 面零字节、动词面返回空数组；绝不渲占位符。
8. **跨 profile 是永久禁区**（D5 隔离；开放问题 7 定案记档）：profile_id 显式于每个请求，peer 信息只在同一 profile 信任域内流转；任何"跨 profile 桥"提案须先出示新实证，否则自动驳回。

## 范围（批次任务）

- **T1 daemon peer 目录端点**：`POST /session/peers`（复用共享 helper 第 4 处消费；零新状态）。
- **T2 MCP 第 6 动词**：`peer_sessions`（payload 透传；网关工具精确集钉 5→6 双文件同批升）。
- **T3 ambient 加性面**：`/session/recent` 响应加性顶层 `peers`（≤3 条纯属性）+ hook 于既有 T1 读取消费并渲 ≤3 行 `<peer-session/>`（4000 含包裹预算计账；旧 daemon 逐字节回退）。
- **前置探针依赖（实现批闸门）**：P0（并行形态普查：ambient ≤3 够不够、last 粒度够不够）、P1（子代理是否以独立 session_id 进同一 profile 且 recent 可见；hook 载荷有无父子关联）、P2（peer 线字节实测）——issue #75，三项全部零代码观察级。**三项收满出报告之前，实现批不得启动**；本文起草与探针并行、不受阻。

## 设计定案（机制层）

### 表面形状规格（wire 定案）

1. **`POST /session/peers`**（新端点，风格对齐 `SessionWindowsRequest` 先例 `memory.py:180`）：
   - 请求 `{profile_id: str (required), peers?: int = 3 (ge=1, le=10), exclude_session_id?: str | None = None}`（exclude 镜像 `/session/recent` 请求模型先例 `memory.py:170-177`；兑现调研 §6.2 "self 排除"预言）。
   - 响应 `{profile_id, server_now: ISO-8601 UTC, peers: [{session_id, window: {first, latest}|null, chunk_count: int, window_truncated: bool, active: bool, last_user_head: str|null}]}`（端点面 `peers` 恒在场、空档为 `[]`——新端点自有形状；与 recent 面的空态缺席语义之别见 wire 审计）。
   - newest-first（discovery 首见序即 recency 序）；共享 `?` pin 组**排除**（`?` 不是 session；`_discover_session_ids` 对它的注释语义 `memory.py:226-230`，peer 目录须显式滤除）；`exclude_session_id` 在场时同序排除。
   - `server_now` 经既有 `iso8601_utc` 格式化；同响应内绝不混 epoch（B2.4 NIT-1 纪律）。
   - `last_user_head`：同一次扫描页框内 newest-first 找首个 `"user: "` 前缀 chunk，剥前缀取 ≤160 字符 verbatim（零额外 store 读）；页内无 user 前缀行 → `null`（honest null——绝不拿 assistant 行充数）。截断不加省略号，字段名即"头部"语义。
   - `chunk_count` 为扫描上限内精确 int（≤2000，`SESSION_WINDOW_SCAN_LIMIT`，扫描实现 `memory.py:278-285`；`?` 组已排除、discovery 蕴含 ≥1 chunk，B2.4 伪 0 教训在此不适用），超限以 `window_truncated: true` 如实标记（沿用 `_scan_session_window` 现成语义，零新机制）；`window` 沿用 `_window_iso` 语义（空窗/非正 epoch → null）；`active` 经守卫 seam 取进程内 buffer 注册表（镜像 ingest 路由先例）。
   - **零新状态**：无注册表、无锁、无心跳——recency 全部派生自存量扫描。
2. **MCP 第 6 动词 `peer_sessions`**：schema `{n_peers?: integer (default 3, max 10)}`；`call_tool` 映射 `n_peers→peers`，body 仅 `{profile_id[, peers]}`（网关不知 sessionID、不传 exclude——调用者自身会出现在结果里，凭 `<session-self/>` 锚自行辨认，边界 5）。daemon 不可达照 isError 先例。
3. **`/session/recent` 加性**：响应顶层 += `peers?: [{session_id, window:{first,latest}|null, chunk_count, window_truncated, active}]`（≤3 条、**纯属性：无 `server_now`、无 `last_user_head`**）；**先滤后计帽**：先剔除 `?` 组与请求既有 `exclude_session_id`，再按 discovery 序取 ≤3——`_discover_session_ids` 把 `?` 计入配额（`memory.py:236-241`），而 remember 直建 stamp 的 pin（`session_id=None`→`?`）常占发现页首，字面复用＋后滤会系统性饿死 peer 名额；这正是 B2.1「filter-before-grouping、幸存 cap」先例（`PRD-B2-roadmap.md:69`）的镜像应用。**空态语义定版：无合格 peer 时 `peers` 键整体缺席**（非空数组），`tests/test_session_recent.py:235` 整包相等钉因此零改动；hook 对缺席/空组/旧 daemon 三态共用同一零输出分支。其余既有字段字节不动。
4. **hook 渲染**：消费同一 T1 响应的 `peers` 字段（零新增 awaited 调用，`plugin.ts:26-29` 全 hook 唯一会话级 awaited 读不变量保持）；围栏内渲 ≤3 行 `<peer-session id="<尾段>" started="<ISO>" last="<ISO>" active="true|false"/>`，位置 = 免责行与 `<session-self/>` 之后、首个组头之前（存在信息是"读其他一切的地基"）；peer 行字节计入 up-front 包裹预算扣除（`plugin.ts:352-363` 计账形状扩展）；全部插值经既有 `escapeAttr` 单点（`plugin.ts:296-302`）；peers 空/字段缺席 → 零输出、逐字节回退今日渲染。
5. **DRY 强制**：peer 发现与窗扫描必须复用 `_discover_session_ids` / `_scan_session_window` / `_window_iso`（`memory.py:220/261/288`，B2.4 三处消费先例的第 4 处消费），不得出现第二份实现；newest-first 排序直接继承 discovery 序。**签名扩展豁免**：为兑现先滤后计帽，允许给共享 helper 增带默认值的可选参数（如「`?` 不计入帽」开关）或在调用前预滤页框序列——二者皆为共享实现的合法延伸，不构成第二实现；复制实现本体仍然禁止。

### 常量与 config

- `PEERS_DEFAULT = 3`、`PEERS_CAP = 10`（请求界 ge=1 le=10）；`AMBIENT_PEER_LINES = 3`（recent 数组与 hook 渲染共用帽）；`LAST_USER_HEAD_CHARS = 160`。
- **config 键：无**（镜像 B2.4 理据 `PRD-B2.4-time-awareness.md:81`：只读确定性 model-free 结构 + agent 自发付费 + 空态零行为变化；auto_recall 默认 off 的教训不适用——本面没有默认 token 流出）。

### 削减（v1 明令不做，理由记档）

| 削减项 | 理由 |
|---|---|
| 推送/广播（SSE/websocket/webhook） | TA-12 中断代价 + opencode 无"推进运行中上下文"入站原语（推送到达仍要等下一次 transform，实时性是假的）+ daemon→host 方向反转违 KISS 与 loopback-simple 启动纪律；未来宿主提供合适原语再议 |
| sidecar presence 文件 | 第二状态面（聚合职责本属 daemon）+ 多 hook 进程并发写（Windows 文件锁坑区）+ 违 B2.5 off 语义精神（daemon 关掉后感知通道悄悄复活半套服务） |
| ambient 内容头 | 违 TA-10 目录/内容分离 + 未邀请付 token；内容只在显式动词返回 |
| 冲突检测 / 文件锁管理 / 编辑碰撞预警 | 超出记忆系统职责（记忆层不是协调层）；未来独立专题 |
| CLI verb | 消费方是 agent（MCP）；CLI recall 面不动先例保持（B2.4 同款裁决 `PRD-B2.4-time-awareness.md:66`） |
| 新 config 键 | 见"常量与 config" |
| 跨 profile peers | 永久禁区（D5），语义定版 8 记档防无据翻案 |
| dream 衍生 topic 摘要 | graph 整合节点为 peer 提供 topic 级摘要是远期方向（调研附录 6）；v1 不碰、不承诺 |
| 父子 session 区分 | 待 P1 探针证据；v1 一律平铺，如实记档（开放问题 1） |

### 开放问题处置（调研附录 1–8 逐条定案）

| # | 问题 | 处置 |
|---|---|---|
| 1 | 父子 session 关联 | **挂起 P1**：hook 载荷若有可用 parent 链接且 dogfood 显示区分有价值，实现批前定标注方案；若无链接则 v1 平铺 + 边界记档。截止：实现批启动前 |
| 2 | 新鲜度阈值（要不要 `recent: bool`） | **定案：不做**。只给事实对（latest + server_now），分类判断归消费侧（TA-7/TA-9 分工；daemon 不做模糊分类） |
| 3 | 噪声门（活动窗口重叠日才渲？） | **定案：无条件渲染 + 空 peers 零输出**。独占日的检查成本 ≈ 0 字节（空态），无需重叠判定逻辑（KISS） |
| 4 | T1 冻结的实际伤害 | **挂起 P0/P2**：误导频率由 dogfood 数据回答；A2（T2 搭车刷新）升级为正式面的条件 = P0/P2 报告显示高频误导，届时另立批次，v1 不预建 |
| 5 | `last_user_head` 隐私粒度（要不要"仅长度"保守档） | **定案：v1 单档**（≤160 verbatim）。同 profile 信任域内流转 + 显式付费查询（非注入）；保守档不做，dogfood 出现真实需求再议 |
| 6 | dream topic 摘要协同 | **定案：远期记档，v1 不碰**（削减表同款） |
| 7 | 跨 profile：永久禁区 vs 可配置桥 | **定案：永久禁区**（D5；语义定版 8 记档防无据翻案） |
| 8 | windows 扩展 vs 新端点 | **定案：新端点 + 新动词**。`test_session_windows.py:250` 整包相等钉不动、既有 wire 表全冻结；动词数成本 < 搅动冻结面的成本 |

### wire 兼容审计（合同面碰撞全列）

- **六工具升钉**：`tests/test_mcp_gateway.py:110` 五工具精确集 + retry 孪生 `tests/test_mcp_gateway_retry.py:205-211` 同批 5→6（例行合法增补，B2.4 四→五先例；两处都改，漏一处即红）。
- **windows 字节冻结**：`tests/test_session_windows.py:250` 整包相等钉**不动**；本批对 `/session/windows` 及其测试文件零改动。
- **EXPECTED_MAPPING 无新行**：peers 搭既有 T1 单次 awaited 读（fetch body `plugin.ts:468-479` 不变），hook 保持唯一 awaited 会话级读不变量；`tests/test_hosts_opencode.py:32,264` wire 表零改动，"无第三个 awaited fetch"行为场景钉续写。
- recent 加性审计（IMPORTANT-1 修正）：`tests/test_session_recent.py:235` 是**整包相等钉**（空 profile 断言无多余键），并非"只断言在场字段"。处置 = **空态键缺席（ABSENT-on-empty）**：无合格 peer 时 `peers` 键不出现在响应体，:235 与全部既有 recent 钉零改动（该文件仅此一处整包钉，其余皆字段访问式断言——非空响应加键不触碰它们）；B2.4 `self_window` 先例走的是恒在场＋同批升钉路线，此处反选缺席路线，理由 = 冻结面最小化，且 hook 本就必须处理字段缺席（旧 daemon 回退路径现成）。端点面 `/session/peers` 不受此限（新面自有形状，`peers` 恒在场）。
- 4000 含包裹预算不变量保持：peer 行计入 up-front wrapper 扣除；病理近满预算下最多挤占 ~350 字符回放地盘（有界，边界 9）。
- 旧 daemon + 新 hook：`peers` 字段缺席 → 逐字节回退今日渲染（B2.4 M4b/M5-lite 回退先例）；混合版本为不受支持边界。
- **归位注记（NIT-1）**：`last_user_head` 位于 daemon `/session/peers` 响应（而非仅 MCP 工具层）系调研文档选项 B 的原始 wire 形状（research-multi-session-awareness.md:91）——属归位非漂移；网关是纯代理、无 store，内容头只能源于 daemon，ambient 面永不携带内容字段的纪律不变。

## 边界（如实）

1. **T1 冻结**：ambient peer 画面冻结于该 session 首轮注入；长 session 内必然陈旧且无陈旧告警。不解之处靠按需动词补；A2 刷新升级条件挂 P0/P2 数据（开放问题 4）。
2. **active ≠ 最近活跃**：active 是进程内 buffer 注册表语义（daemon 重启清空直到各活 session 下次 ingest，B2.4 边界继承）；近况看 `last` 时间戳，不看 active。
3. **ambient 面无 server_now**：存在线的新鲜度只能近似判断（模型自携时间感）；精确比较必须走 `peer_sessions`（server_now 在场）。设计使然（TA-11 最小 grounding + 冻结面最小化），非遗漏。
4. **`last_user_head` 是头部不是全文**：≤160 字符硬截断、无省略号标记，截断与完结从载荷不可分辨（字段名即语义）；只取最新 user 前缀行，assistant-only 页 → null。
5. **verb 结果含调用者自身**：网关 session-blind（B2.4 先例），不传 exclude；自我 session 凭 `<session-self/>` 锚（`started=` 匹配）自行辨认——结构给人、判断归人，不做魔法剔除。
6. **时钟域单一**：server_now 与 window 同出 daemon 钟（单机 loopback，不存在跨机 peer）；亚分钟级对比不可靠（B2.4 边界继承）。
7. **peer 目录是摄入视图**：火忘延迟、30s 重放重叠、捕获滞后、宕机空洞照常适用于 window/chunk_count；"最近活跃"的下界是最近一次成功 ingest，不是用户最后一次键入。
8. **旧 daemon 回退**：新 hook + 旧 daemon = 今日渲染逐字节不变；失去 peer 能力但不损坏。
9. **预算挤压有界**：peer 行计入包裹后，病理近满情形最多挤占 ~350 字符回放内容（ISO 各 24 字符、单行 ≈110 字符、×3 行）；剩余不足 MIN_SLICE_CHARS 时整块 null（今日行为不变）。
10. **明令不给（v1 全集）**：推送/广播、sidecar、ambient 内容头、冲突检测/文件锁、CLI verb、config 键、跨 profile（永久禁区）、dream topic 摘要、父子区分（待 P1）。此清单 = 调研 §6.1 削减刀原样升格；任何一项回流须走新 PRD 并出示新证据。

## 测试预言与变异体（按流，对抗式）

新 `test_session_peers.py`；扩 `test_session_recent.py` / `test_mcp_gateway*.py` / `test_hosts_opencode.py` / `test_hook_ts_behavior.py`：

| # | 流 | 预言 | 反变异体 |
|---|---|---|---|
| 1 | A | peers newest-first 且 `?` 组不在场 | discovery 后未滤 `?` → 红 |
| 2 | A | `server_now` 匹配 ISO-8601 UTC 正则；同响应零 epoch | epoch 直塞 → 红 |
| 3 | A | `exclude_session_id` 在场 → 该 session 不在 peers | 排除被无视 → 红 |
| 4 | A | `peers` 越界（0 / 11）→ 422 | 校验缺席 → 红 |
| 5 | A | 空 profile → `{profile_id, server_now, peers: []}` | 伪造条目 → 红 |
| 6 | A | `last_user_head` = 最新 user 前缀行剥前缀 ≤160 字符，断言为存储文本子串（verbatim 钉） | assistant 行充数 / 改写 / 加省略号 → 红 |
| 7 | A | 页内无 user 行 → `last_user_head: null` | 拿 assistant 行或 pin 文本冒充 → 红 |
| 8 | A | recent 非空态顶层 `peers` ≤3 条且键集精确 = {session_id, window, chunk_count, window_truncated, active}（无内容键） | 内容头/now 混入 ambient → 红 |
| 9 | A | ambient peers 天然 self-free（hook 式 exclude 请求下） | 自身混入 → 红 |
| 10 | A | `chunk_count` 扫描上限内精确、超限组 `window_truncated=true`（超限夹具杀"谎报精确"）；`active` 真值经守卫 seam（未 settle 缓冲夹具杀"恒 false"） | 伪 0 / 截断瞒报 / 恒 false → 红 |
| 11 | B | 六工具精确集（主钉 + retry 孪生双文件同绿） | 只升一处 / 漏升 → 红 |
| 12 | B | `n_peers→peers` 映射 + payload 透传（StubClient.calls 记录模式）；无参 → body 仅 profile_id | 键名错 / 默认值外泄 → 红 |
| 13 | B | daemon 不可达 → 结构化 isError（先例续绿） | 裸异常穿透 → 红 |
| 14 | C | peers 消费自同一 T1 响应；全 hook 仍恰一个会话级 awaited fetch（行为场景钉 + 不变量注释续写） | 新增 awaited → 红 |
| 15 | C | ≤3 行 `<peer-session/>`，位置在免责行与自锚行之后、首个组头之前 | 位置错 / 超 3 行 → 红 |
| 16 | C | peers 空/缺席 → 零输出；旧 daemon 夹具下渲染块与今日基线**逐字节相等** | 占位符 / 半渲染 / 崩溃 → 红 |
| 17 | C | peer 行计入 4000 包裹预算（贴边夹具断言最终块 ≤4000） | 计账遗漏溢出 → 红 |
| 18 | C | 全部属性插值经 escapeAttr（恶意 id/ISO 夹具） | 裸插值 → 红 |
| 19 | C | EXPECTED_MAPPING 断言零改动通过（回归守卫） | wire 表漂移 → 红 |
| 20 | — | `test_session_windows.py` 全套续绿（整包钉 :250 不动） | 对冻结面的任何搅动 → 红 |
| 21 | A | **空态键缺席**：空 profile 与独居 profile 的 recent 响应体整包等于今日基线（:235 续绿即证）；非空且有合格 peer 时 `peers` 键在场 | 空态仍带 `peers` 键（哪怕空数组）/ 占位键 → 红 |
| 22 | A | **先滤后计帽**：页首 `?` pin 夹具（remember 式 stamp）＋页内 ≥n 个有标 session ⇒ peers 仍足额 n 条 newest-first 且无 `?` 条目 | 后滤计帽（名额被 `?` 吞掉）→ 红 |

## 门禁与并行分解

- **流 A daemon**（`daemon/memory.py`：peers 端点 + recent 加性；新 `test_session_peers.py` + 扩 `test_session_recent.py`）。
- **流 B MCP 网关**（`mcp_gateway/server.py` + `test_mcp_gateway.py` + `test_mcp_gateway_retry.py` 双钉升级）。与 A 全并行（按本 PRD wire 契约写钉，集成门禁兜底）。
- **流 C hook 切片**（`hosts/opencode/plugin.ts` + `test_hosts_opencode.py` + `test_hook_ts_behavior.py`）。三流文件面互不相交。
- DRY 审查项（QA 必查）：`memory.py` 不得出现第二份发现/扫描/ISO 化实现；helper 消费点 3 → 4 处。
- TDD（先红后绿）→ 对抗 QA（senior-qa-reviewer，无 BLOCKER 方可收口）→ 全量门禁（`pwsh -File scripts/gate.ps1`）→ 单 commit 收口 + 收口记录入本文；落地走 issue → branch → PR → merge。
- **实现批启动闸门**：issue #75（P0/P1/P2）报告收满 + 开放问题 1/4 挂起项定夺完毕，二者齐备才开工；探针期本文可先行修订（修订记入批次执行记录）。
- **收口同步义务**：`docs/zh/design/06-session-continuity.md` §2 逐字镜像理论锚文本（现载 TA-1..9），收口时必须同步增补 TA-10..12；漏同步 = 收口不完整（QA 复审必查项）。

## 批次执行记录（随批追加）

- **2026-08-23 本文起草**（solution-architect，docs-only 批次）：依 orchestration 定案 + 调研文档成文；理论锚 TA-10/11/12 同步登记 `docs/zh/design/REFERENCES.md`（R38–R43；核验状态如实标注：✅×3 直接命中、📕×2 经典章节、⚠️×1 待抽查）。实现批记录待 P0/P1/P2 报告后追加。
- **2026-08-23 起草评审并入**（senior-qa-reviewer，verdict CLOSABLE：2 IMPORTANT + 5 NIT 全并入）：IMPORTANT-1 recent 空态改判 **ABSENT-on-empty**（:235 整包钉零改动，审计条款重写并点名钉位）；IMPORTANT-2 peer 发现改判**先滤后计帽**（roadmap:69 先例）＋ DRY 签名扩展豁免＋oracle #22；NIT-1 `last_user_head` 归位注记入 wire 审计（选项 B 原始形状，属归位非漂移）；NIT-2 peers 形状增 `window_truncated`（诚实规则，chunk_count 改"上限内精确"）；NIT-3 预算挤压 ~200→~350 字符；NIT-4 B2.4 引用改 :32/:56；NIT-5 design/06 §2 TA 镜像同步义务入门禁。
