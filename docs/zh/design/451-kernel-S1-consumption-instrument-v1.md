# 设计 451-kernel-S1-consumption-instrument v1

> 状态：**PROPOSAL / DESIGN_PENDING（无门权）**。DESIGN ONLY；本文件不授权工程、不改产品源码/配置、不碰端口与 `~/.mnemoseed-local`。
> 关联：tracking issue #457；父工件 `design-batch-brief.md` §S1、`brief.md`（路径 A）；输入 `inv2-75-probes.md`、`competitor-memory-mechanisms.md`、`first-principles-kernel-gap.md`。
> 声明：本席 `solution-architect`（declared model `opencode/hy4-flash-free`，物理身份未自证）；access 2026-11-22。
> **本切片（含将来实现）不改变任何召回/排序/reinforce 行为；只增加观测面。**

## 0. 裁决（verdict）

**SHIP-WITH-ADJUSTMENTS** - brief §S1 的方向（additive 台账、served/consumed 分离、无声≠已用、hook 回传路径、不污染排序）成立；但原文有三处必须调整，已在本文消解：把"被动消费"降级为**可观测产物的证据分层**（宿主 hook 看不到模型内部注意），承认**现有显式反馈通道**不可另起炉灶，补上**跨 hook/daemon 关联键**这一未言明前提。

| # | 严重度 | claim（对 brief 的缺陷） | 证据 | 最小调整（本文落实位置） |
|---|---|---|---|---|
| B-1 | BLOCKER | "Passively if possible"暗示可测"模型是否真的用了注入记忆"；宿主 hook 只能看到回复文本与工具事件，渲染结果不回传，模型内部注意不可观测 | `inv2-75-probes.md:14,107`；`plugin.ts:2451,2484-2517`；`plugin.ts:3011-3014`（hook 面仅 prompt/context/compaction/tool/bus） | §4/§5 定义 `served / injected / consumed_attested` 三层**证据**，禁止写成 `used=true`；无 consumed 行 ⇒ `unknown`，永不为 "used"（§7 AC-9） |
| B-2 | BLOCKER | "Fix the fragile proxy"若在本片改判定器，就会改 `/memory/reinforce` 的触发集合 ⇒ 改排序，违反 no-ranking-change | `plugin.ts:2502`（`normalized.includes(needle)`）→ `:2514` POST → `reinforce.py:117-124` 回弹 | §6 保持判定器及其 reinforce 效果**字节不变**；只记录 `method`+`needle_len` 并给精度包络；更强判定器作为后续独立切片（OD-1） |
| I-1 | IMPORTANT | brief 只列两路信号，漏掉现有第三路显式反馈 `/memory/recall_feedback`（store-only、closed kind） | `memory.py:351-363,2844-2860,3027-3036`；`observability.py:56-59` | §5.4 复用该通道并追加结构化行，绝不新建竞争入口；保留"no ranking or retention effect" |
| I-2 | IMPORTANT | 未定义 daemon 侧 recalled 与 host 侧 injected/consumed 如何关联；两端不同进程，无共享键 | `memory.py:792`（daemon 写）vs `plugin.ts:849,1056-1060,1157-1172,1613-1621,2491`（host registry as-is 仅 needle→chunkId，无 serve_id）；`inv2:107` | §4.3 增 `serve_id`（additive 回传字段）+ **宿主按 chunk/needle 逐条携带 serve_id 的结构改动（S1-B，令主键 join 在主导 auto-inject 面可构造）** + 弱回退 join（仅非插件宿主/同 needle 多 serve 歧义）；OD-2 定夺 |
| I-3 | IMPORTANT | "evidence pointer only / no judgment"若落成布尔 `used` 列，会让 agent 自陈反馈污染 eval 并诱导后续回弹 | `storage/ports.py:564-597`（ErrorEvent 只有 evidence_ptr 无 verdict）；`observability.py:56-59`（agent 自陈 kind） | §4.2 无 verdict 列；`event_kind/method/source` 三元组；§7 排序路径零 reader（AC-8 变异体） |
| N-1 | NIT | append-only 台账在 10x-1000x 的增长与留存未定 | `_migrations.py:544-562`；`inv2:82,180`（某 boot 86 次 serve） | §9.3 profile/time 索引 + 分页读；rollup 留 OD-3 |

## 1. 核心决定（steelman）

目标：把"召回消费"从**有副作用信号、无稳定台账**变成**可测量闭环**，使 `accumulate → evaluate → optimize` 能被证伪；**不改变任何检索排序行为**（brief §S1、父 brief §路径 A）。

核心决定：在 meta store 增一张 append-only `consumption_events` 台账（migration v18，完全照 `error_events` v11 先例），记录三层证据 - daemon 的 `served`、宿主渲染的 `injected`、宿主判定/显式反馈的 `consumed_*` - 以 `serve_id` 跨进程关联；`profile_id` 恒在、`observed_at` 单调、只存 store key/哈希/长度等证据指针，**没有 verdict 列**。本片只写不读（排序路径零 reader）；S0 以只读方式 join 三层证据算 serve→consume 率与 rank 分布。语义漂移（served 驱动 reinforce）**被测量但不修复**。

## 2. 两路（实为三路）信号对账

| 路径 | 落点 | 触发时机 | 记录内容 | 对排序的作用 | 对 R23/R28 / TA-6 的忠实度 |
|---|---|---|---|---|---|
| (a) noteConsumption | `plugin.ts:2484-2517`，调用点 `:2451`（`postAssistantIngest`） | assistant 轮被 ingest（live/sweep/replay 同走） | 仅命中 chunk id；**无 ack、失败只 debugLog**（`:2488-2490,2514-2516`）；无稳定事件 | 命中即 POST `/memory/reinforce` → `reinforce.py:117-124` 回弹（改排序） | 方向对（消费才强化，`00:165`/`03:139`），但判定是字面子串、无证据留痕 |
| (b) daemon `_record_hits` | `memory.py:792`，实现 `:813-834` | **recall 装配时**（served），装配进 context 即算 hit；含 rescue pin（`reinforce.py:16-21`） | chunk/node id、rescued 标记；fire-and-forget，异常只 warning（`:835-836`） | `reinforcer.record_hits` → 回弹 + 刷新 `last_reinforced`（`reinforce.py:90-124`）- **主 reinforce 路径** | 漂移：驱动 ACT-R base-level 的 usage 主要是"被服务"而非"被消费"，排序动力学与真实消费部分解耦（`kernel-gap.md:46,72`） |
| (c) 显式反馈（brief 漏列） | `memory.py:351-363,2844-2860,3027-3036`；closed kind 在 `observability.py:56-59`（当前仅 `not_useful`） | agent/console 显式调用 | 只 append 到 audit_log detail，**不改排序/留存**（endpoint docstring 原文） | 无 | 方向诚实但稀疏、依赖显式上报；竞品同类（Mnemoverse `memory_feedback`、Cognee `improve`）均非被动（`competitor-memory-mechanisms.md:14,149,175`） |

对账结论：现有信号**都是"写排序状态"的动作，不是"留证据"的动作**。缺的是与排序解耦的 append-only 证据面；因此本片不改 (a)(b)(c) 任一行为，只在其旁挂账。语义漂移由台账暴露为可测量量（served 计数 vs consumed 计数），修复属后续 OD-2。

## 3. 理论锚（严格分离）

**借用（3 条，均为已校验规律 → 设计规则；来源与编号逐字对齐 `REFERENCES.md:49,59` 与 `00-overview-and-philosophy.md`）：**

| 锚 | 来源 / 已验证规律 | 设计规则 | 实现机制（非理论，本片产物） |
|---|---|---|---|
| R23 ACT-R base-level（`00:160`，Anderson & Schooler 1991，✅） | 可用性 = 频度×近因的幂律和 + 线索激活 + 噪声 | 排序动力学必须由**使用事件**驱动；仪表须先能区分"被服务"与"被消费"，否则闭环不可观测 | `consumption_events` 分 `served`/`consumed_*` 行；本片不改 reinforce |
| R28 提取诱发遗忘 / TA-6（`00:165` + `03:139`，Anderson, Bjork & Bjork 1994，✅） | 提取会压制竞争者并自我强化 ⇒ 注入本身不应算强化 | "注入≠强化"：served 与 consumed 必须分列；消费证据才可作为未来回弹输入 | 无 verdict 列；served 行与 consumed 行分写；回弹门控**不在本片** |
| R7 来源监控（`00:164`，Johnson et al. 1993，✅） | 来源判别天然不可靠、易混淆 | 证据只追加、只指针、不断言正确性；消费陈述带 source/method 而非"真相" | `evidence_kind/evidence_id` 指针 + `source/method`；append-only 触发器 |

**明确不借用（not borrowed）：** ①"每次载入都会强化记忆"的流行说法 - 与 R28/TA-6 直接冲突，本片以注入≠强化为红线；②间距效应冷却（`03:129-133` 明示未实施，`reinforce.py:24-28` 有意不做）- 不得借来当仪表参数；③Miller 7±2 或任何工作记忆容量 myth（`03:149` 禁止）；④"被动消费信号 = 云端遥测" - local-first，零外发；⑤竞品的 benchmark 数字（全为 CLAIMED，`competitor-memory-mechanisms.md:58,242`）- 不作任何门限出处。

## 4. 仪表形状（instrument shape）

### 4.1 表：migration v18（当前 latest=17，`_migrations.py:998-1037`）

沿用 v11 `error_events` 的全部纪律：`profile_id NOT NULL`、`observed_at` 单调、autoincrement `id` 给稳定追加序、`BEFORE UPDATE/DELETE` 触发器 abort（`:544-562`）、`(profile_id, observed_at)` 索引（`:537-542`）、born empty 不回填（`:516-518`）。

```
consumption_events(
  id INTEGER PK,
  profile_id TEXT NOT NULL,
  observed_at REAL NOT NULL,
  correlation_id TEXT NOT NULL,        -- serve_id；弱回退见 §4.3
  event_kind TEXT NOT NULL,            -- closed: served|injected|consumed_attested|consumed_explicit
  evidence_kind TEXT NOT NULL,         -- closed: chunk|node
  evidence_id TEXT NOT NULL,           -- store key（证据指针，非内容）
  source TEXT NOT NULL,                -- closed: daemon|plugin_hook|agent_tool
  method TEXT,                         -- closed: citation_substring_v1|explicit_not_useful|explicit_unknown|unspecified
  rank INTEGER,                        -- served 时的装配序；其他行为 NULL
  needle_len INTEGER,                  -- 命中针脚的内容 token 数；精度边界用
  needle_hash TEXT,                    -- 针脚 sha256；**不存注入原文/回复原文**
  session_id TEXT,
  query_hash TEXT,                     -- 查询 sha256；不存自然查询原文（隐私）
  dedup_key TEXT                       -- sha256(profile|correlation|event_kind|evidence_id|method)
)
UNIQUE(profile_id, dedup_key);
INDEX idx_consumption_profile_time(profile_id, observed_at);
INDEX idx_consumption_corr(profile_id, correlation_id);
TRIGGER trg_consumption_no_update / no_delete  -- 同 error_events 字面
```

**无 `used`/`consumed` 布尔列** - `event_kind` 是事件事实，不是裁决（I-3）。所有文本字段只存 hash/长度/key；不存回复正文、不存自然查询正文（local-first 隐私，§9.1）。

### 4.2 端口（`storage/ports.py` 照 `ErrorEvent`/`ErrorEventFilter` 形状，`:564-620`）

新增 `ConsumptionEvent`（frozen dataclass，字段同上，`id: int|None`）、`ConsumptionEventFilter`（`profile_id` 恒显式、`event_kind`、`evidence_kind`、`session_id`、`correlation_id`、`since/until`；照 `:600-620`）。写口 `append_consumption_event(event) -> bool`（重复 dedup_key 返回 False、不抛；append-only 下用 `INSERT ... ON CONFLICT DO NOTHING`，不违反触发器）；读口 profile-scoped 分页（同 ErrorEvent 读先例）。**不新增 ranking 侧端口**。

### 4.3 关联键（I-2）

**as-is 宿主结构（诚实边界，QA 复核）**：`injectedRegistry` 为 `Map<needle, Set<chunkId>>`（按 session 分组，`plugin.ts:849`）；`buildRecallInjection` 返回 `{block, registry, includedIds}` 不带 `serve_id`（`:1056-1060`）；`mergeRegistry` 按 needle 把多次 serve 的 chunkId 并进同一个 Set（`:1157-1172`），T2 poll 再并一次（`:1613-1621`）；`noteConsumption` 按 needle 查（`:2491`）。现状**无法**逐 chunk/逐 needle 携带 `serve_id`：`injected` 行带不出 `serve_id`，一 needle 多 serve 的 consumed 行也无法归到唯一 serve。原稿把主键 join 写成默认主线对此结构为假，本修订消解如下。

- 主路径（结构改动，S1-B 落地）：daemon 在 recall 装配时为本次 serve 铸造 `serve_id`（opaque，如 `sha256(profile|session|monotonic_seq)`），作为 **additive 字段**附在 `/memory/recall` 与 `/session/recall-pending` 的返回项（落点 `_memory_payload` `memory.py:838-859` 与 recall-pending item；改前 grep 全部钉字段的 test，防止 wire/doc 表被测试解析）。宿主从每个返回项读 `serve_id`，构建注入块时**按 chunk/needle 逐条携带**：registry 值从 `Set<chunkId>` 改为携带 `serve_id` 的结构（如 `Map<needle, Map<chunkId, serveId>>`），`buildRecallInjection`/`buildT2Injection` 返回按 chunk 的 `serve_id`，`mergeRegistry` 保留 per-chunk `serve_id`；`noteConsumption` 命中 needle 后按 `(chunkId, serveId)` 逐条产出 consumed 行。三行（served/injected/consumed）以该 per-chunk `serve_id` 主键 join；每个 needle 由其注入 serve 唯一确定时 ⇒ **exact 主键 join**（这是主导 auto-inject 面的常规情形）。该结构改动**纯 additive**：block 文本、needle 派生、字面子串判定器与 `/memory/reinforce` 调用集合字节不变（AC-4）；hook 行为测试须重钉并留字节恢复证据（S1-B）。
- 弱回退（读面统一标 `join=weak`，不与 exact 混算）：(a) 非插件宿主 / 宿主未回显 `serve_id`（§5.3）：`(profile_id, session_id, evidence_id, observed_at 窗口)` join；(b) **同一 needle 文本被多次 serve 注入**（即 as-is `mergeRegistry` 塌缩的情形）：字面命中无法判定是哪一次注入，按各候选 serve 逐条产出 consumed 行并标 `join=weak`。禁止把这两类冒充 exact。
- `serve_id` 是观测字段：剥离后 entries 顺序/分数与基线逐字节一致（AC-4）。

### 4.4 写入流程（三条产线，全部 fire-and-forget）

1. **served**：`memory.py:792` 旁，对 `context.entries` 每个 chunk/node 写一行（`event_kind=served, source=daemon, rank=装配序, query_hash, correlation_id=serve_id`）；与 `_record_hits` 同一 fire-and-forget 守卫（`:813-836`），台账写失败只 warning、recall 继续。
2. **injected**：宿主 `context` hook（`plugin.ts:3012` → `onSessionContext`）构建注入块处（`:1395,1519,1616-1619`）写 rendered chunk id 行（`source=plugin_hook, method=unspecified, correlation_id=该 chunk 的 serve_id`）- `served ⊇ injected` 的差集即"被预算裁掉"，是消费分析的一等事实。
3. **consumed_attested**：`noteConsumption` 命中处（`:2500-2508`）按 `(chunkId, serveId)` 逐条写命中 chunk id 行（`method=citation_substring_v1, needle_len, needle_hash, correlation_id=该 chunk 的 serve_id`）；**与现有 `/memory/reinforce` POST 并列发出、互不影响**，判定器不改。

### 4.5 读契约（S1 只写不读；S0 只读）

`ConsumptionEventFilter` 分页读 + `Observability.snapshot()` 增聚合（served/injected/consumed_* 计数、join 成功率；照 `observability.py:62-77` 的 since-boot 聚合纪律，跨 boot 禁止求和 - `inv2:82`）。状态派生规则（读面而非写面）：有 consumed 行 ⇒ 该 evidence 的 `consumed_status` 按其 method 标注；**无 consumed 行 ⇒ `unknown`，永不 "used"、也永不 "not_used"**（AC-9）。

## 5. 宿主 hook 回传路径（brief 必答项）

### 5.1 哪个 hook、什么数据
宿主侧可用面已逐字确认：`prompt`/`context`/`compaction`/`tool.execute.after`（`plugin.ts:3011-3014`）与 detached bus `event.subscribe`（`:3025-3029`）。回复文本经 bus `session.text.ended` 累积、`session.step.ended`/`session.step.failed` → `settleAssistantStep` → `postAssistantIngest` → `noteConsumption`（`:2835-2849,2776-2799,2413-2452,2484-2517`）。**这就是回传挂点**：不改 hook 注册，只在既有 `noteConsumption` 与注入块构建处各多发一笔 additive POST。

### 5.2 payload 与预算
new additive `POST /memory/consumption`（daemon router 仿 `memory_reinforce` `memory.py:3006-3017`）：
`{profile_id, host, session_id, items:[{event_kind, evidence_kind, evidence_id, method, rank, needle_len, needle_hash, observed_at, correlation_id}]}`（`correlation_id` 为该 chunk 的 `serve_id`，逐 item 携带以支撑多 serve 同 needle 的归属；单 serve 批次可在 envelope 带 `serve_id` 作默认）。

- **零模型调用**（token economy）；确定性匹配 + hash 而已。
- **不传回复正文 / 注入正文 / 自然查询正文**，只传 store key、hash、长度、计数（§9.1）。
- batch 上限复用 `REINFORCE_BATCH_SIZE=64` 先例（`plugin.ts:2510-2513`；`ReinforceRequest.max_length=64` `memory.py:341`），一 inject episode 一批。
- fire-and-forget、无 ack、失败仅 debugLog（照 `:2488-2490,2514-2516`）；**绝不 advance watermark / 绝不 scheduleRecovery**。

### 5.3 宿主无法回传时（诚实边界）
并非所有宿主都跑此插件（Claude Code / Codex 的接入形态 `[未验证]`）。宿主无 `consumption_events` 写 ⇒ 台账只有 served/injected 行 ⇒ 读面 `consumed_status=unknown`。**禁止从沉默推断 used**（`inv2:83` 引 `protocol75.md:124`"协议禁止从静默推断无丢失"；`inv2:96` 明示 served 是队列消费语义）。局限须写进 as-is 文档：本仪表测的是**宿主可观测产物**，不是模型内部使用；跨宿主被动消费遥测未提供，也不外发。

### 5.4 显式反馈路径（复用现有通道，不新建）
`/memory/recall_feedback`（`memory.py:3027-3036`）已存在且 store-only、无排序效果（`:2844-2860`），closed kind 当前仅 `not_useful`（`observability.py:56-59`）。本片：保留原 audit append 不变；**追加**一条 `consumption_events` 行（`source=agent_tool, method=explicit_not_useful|explicit_unknown`）。wire 契约为**additive 字段**：`RecallFeedbackRequest`（`memory.py:351-363`，现字段 `profile_id/kind/observed_at`）增可选 `correlation_id: str | None = None`，`/memory/recall_feedback` 请求体（`:2844-2860`、路由 `:3027-3036`）同步接纳该可选字段；`None` 为默认 ⇒ **旧调用方（不带该字段）零改动继续通过校验、audit append 字节不变、不产生带错误归属的台账行**（S1-A/S1-C 旧调用方兼容测试 + AC-11）。带值则以该值作 `correlation_id` 追加台账行；缺省则按 §4.3 弱回退并在读面标 `join=weak`。若 owner 要正反馈 kind（`explicit_used`），须走 closed-vocabulary 变更并明确标为 agent 自陈证据（非 ground truth）；本片默认只加 `explicit_unknown`，避免自陈"used"污染（OD-1 关联）。

## 6. 脆弱代理：精度边界与升级路径

现状：`normalized = text.replace(/\s+/g," ").toLowerCase()`，`normalized.includes(needle)`（`:2494-2508`），needle 是整段注入切片的规范化文本。误差两侧：

- **假阴性**：改写、摘要式使用、跨语言、gist 式吸收 - verbatim/gist 双轨（`00:159`）意味着语义使用可不留逐字痕迹；needle 越长越易全漏。
- **假阳性**：过短/泛化 needle 的巧合包含。

本片（不改判定器、不改 reinforce）：记录 `method=citation_substring_v1` + `needle_len`，把它定义为"**逐字引用证据**"而非"已使用"；冻结精度包络需离线标定：用 S0 的真实语料自然查询集 + 人工判读的小样本，报告 precision/recall 区间（机制标定值，非理论锚；标定不引入模型调用于热路径）。

**可借的更稳判定（设计、不实施）**：确定性 token-overlap v1 - 对 needle 取 distinctive content tokens，要求回复含 ≥K 个且顺序一致（K 离线标定，zero model call），作为第二个 evidence kind 与 substring 并存；它降低假阴性但不消除 gist 盲区。**拒绝**模型语义裁判：引入热路径模型调用违 token economy，且自陈判定与自证仪器风险（`inv1:272-282` 对 retention observer 的判词纪律）。判定器升级若影响 `/memory/reinforce` 触发集合，必须另开切片并带排序验收（OD-1）。

## 7. 质量反馈如何喂 S0 而不污染排序

数据流（只读）：`consumption_events` → S0 按 `correlation_id`（per-chunk `serve_id`）join served/injected/consumed_* → 产出 serve→consume 率、被消费条目的 rank 分布、按 method 分层的精度；`join=weak` 行（非插件宿主 / 同 needle 多 serve 歧义）单独分层，不与 exact 混算；与 S0 的真实语料 recall 契约并置解读（**消费成功 ≠ 召回成功**；owner 之痛的召回失败面由 S0 负责，见 `org/_state.md:7`）。

四道防污染闸：

1. **排序路径零 reader**：retriever/assembler/reinforcer 不得 import/查询本台账；AC-8 用变异体（在召回路径插入读）必须 RED。
2. **自陈只作弱证据**：`method=explicit_*` 永远带 `source=agent_tool`，不得作为 verdict；S0 不得用自陈标签当 ground truth（#206 自指仪器教训，brief S0 / `inv1:272-282`）。
3. **显式反馈零排序写入**：`decay_weight`/`last_reinforced` 不得被 feedback 端点改（变异体测试：`memory.py:2844-2860` 追加权重写 ⇒ RED）；现状已满足（`:2851` docstring）。
4. **沉默未知**：AC-9 保证无 consumed 行不会被算成 positive，避免"没报=用好"的系统性偏差。

## 8. 语义漂移的诚实 as-is 边界

本片**记录**但不修复：`_record_hits` 在装配时仍 reinforce（`memory.py:792` → `reinforce.py:117-124`），served 仍是排序主驱动；`noteConsumption` 仍按字面子串触发 reinforce。仪表把漂移变成可测量量（served 计数 vs consumed_attested 计数 + 两者的 rank 差），为后续 D5（回弹仅由消费事件触发，`kernel-gap.md:122,194`）提供证据。**不得**声称本片落实了 R28/TA-6 的完整实现 - 那是后续 owner 决策与独立切片。诚实陈述写入 as-is 边界（本文件 §8 即该陈述）。

## 9. 安全 / 失败模式 / 规模

### 9.1 隐私与 local-first
台账在 local meta store；无网络外发（无 telemetry，竞品默认遥测均被拒，`competitor-memory-mechanisms.md:198-199`）；`profile_id` 恒显式隔离（D5，`storage/ports.py:604`）；只存 store key/hash/长度，不存回复与查询原文；`query_hash`/`needle_hash` 为单向摘要。

### 9.2 失败模式
daemon 不可达 ⇒ POST 失败只 debugLog，主记忆无损（同 reinforce fire-and-forget，`plugin.ts:2514-2516`）；台账写异常 ⇒ warning、recall 继续（照 `memory.py:835-836`）；hook 缺失/宿主不回显 ⇒ unknown（§5.3）；重复送达 ⇒ dedup_key 幂等（§4.2）。**数据丢失包络**：最坏丢失"消费证据行"（可补采或永久未知），不丢记忆内容、不改排序。

### 9.3 10x-1000x 增长
行数量级 ≈ 每 turn (served + injected + consumed) ≈ `top_k` 倍数；已观测某 boot 86 次 serve（`inv2:180`）。1000x 下千万级行 ⇒ `(profile_id, observed_at)` 与 corr 索引 + 分页读支撑 v1；周期 rollup/留存策略作为 OD-3，不在本片静默引入删除（append-only 红线）。

## 10. 重大决策的 option space（含推荐）

| 决策 | 选项 A | 选项 B | 推荐 / 理由 |
|---|---|---|---|
| 消费信号来源（B-1/OD-1） | 只记 literal substring（零成本，假阴性高） | 加确定性 token-overlap v1（更低假阴性，仍需标定） | **A 先行 + B 设计为可加 kind**；模型裁判拒绝（token economy + 自证风险） |
| 关联键（I-2/OD-2） | daemon 铸 `serve_id` additive 回显 + 宿主按 chunk/needle 携带（S1-B 结构改动；精确，需改 wire 字段与 hook 测试重钉） | 内容 hash + 时间窗弱 join（零 wire 改动，精度低） | **A 为主、B 为显式降级/非插件宿主兜底**；A 的结构改动须字节保持 block/needle/判定器/reinforce（AC-4），wire 字段须过 test-pin grep；OD-2 仍待 owner |
| 语义漂移修复时点 | 本片一并门控回弹（改排序，违 no-ranking-change） | 本片只测量，D5 另切片 + owner 决策 | **B** - 与 brief "instrumentation only" 一致 |
| 显式反馈 kind | 加 `explicit_used` 正反馈 | 只加 `explicit_unknown`，保留 `not_useful` | **B** - 避免自陈"used"被误读为 ground truth |
| 增长治理（N-1/OD-3） | v1 即 rollup + 留存 | v1 仅索引+分页，rollup 后续裁决 | **B** - append-only 红线不因容量静默破例 |

## 11. 验收门（设计层；实现级见 slices 文档）

- AC-1 migration v18 建成；UPDATE/DELETE 被触发器 abort；`profile_id NOT NULL`、`observed_at` 单调、无 verdict 列。
- AC-2 三类写入齐备（served/injected/consumed_attested + 显式 append）。
- AC-3 无正文外发：payload 仅 key/hash/len/count；grep 确认无 `text`/`query` 原文写入。
- AC-4 排序不变：剥离 `serve_id` 与台账后 entries/rank/reinforce 调用序列与基线逐字节一致；宿主 registry 结构改动后注入 block 文本、needle 派生与字面子串判定器字节不变。
- AC-5 关联：exact 主键 join 成功（per-chunk `serve_id`；每个 needle 唯一确定其注入 serve 时精确）；未回显/同 needle 多 serve 歧义行读面标 `join=weak` 且不混算（变异体：折叠 per-chunk serve_id 使常规情形丢失归属 ⇒ RED；未标 `join=weak` 或把歧义行当 exact ⇒ RED）。
- AC-6 fire-and-forget：注入 daemon 故障，主 recall 成功、仅 debugLog/warning。
- AC-7 幂等：重复 POST 不产生重复行。
- AC-8 排序零 reader：召回路径插入台账读的变异体 RED。
- AC-9 无声≠已用：无 consumed 行 ⇒ `consumed_status=unknown`；推断 used 的变异体 RED。
- AC-10 feedback 零权重写：feedback 端点写 `decay_weight`/`last_reinforced` 的变异体 RED。
- AC-11 feedback 向后兼容：不带 `correlation_id` 的旧调用方请求仍 200、audit append 字节不变、不产生错误归属台账行；带值请求追加行带该 `correlation_id`（变异体：把字段设为必填而拒绝旧调用方 ⇒ RED）。

## 12. 未验证清单 / 风险与反对意见

- `[未验证]` Claude Code / Codex 宿主能否承载等价 hook（§5.3）；zcode miss 的具体检索轨迹（`kernel-gap.md:209`）；needle_len / K 标定值（§6）；竞品是否已做隐式埋点（INFERRED、未穷尽，`competitor-memory-mechanisms.md:233,242`）- **差异化叙事须补查后才可对外**。
- 反对 1："加台账=过度工程（KISS）"。回应：不新增能力，只把已有两路副作用信号留证据；W0-W4 均小面，且零模型调用。
- 反对 2："measure 但不 fix 没价值"。回应：无证据无法证伪补丁（INV-1 量纲错位教训）；先测量是 S0/S1 共同纪律，修排序须 owner 决策（OD-2）。
- 反对 3："self-reported 反馈必污染"。回应：四闸（§7）保证 feedback 只进只读台账、无排序写入、非 verdict。

## 13. 证据时效

全部产品源码/调查/竞品工件为 2026-11-22 同批一手读取（access 2026-11-22）。理论锚来源为 1991/1993/1994 经典已校验研究（`REFERENCES.md:49,59` 标 ✅；`00:160,164,165`），>90 天仍有效理由：基础认知理论非快变领域，且本仓 REFERENCES 已逐条 Crossref 核验。无快变数字被当作门限。

## 14. 置信度

- **高**：两路信号与第三路显式通道的 file:line 事实、error_events 先例、hook 挂点 - 逐字核对源码。
- **中**：hook 回传 payload 的宿主 API 稳定性（bus 事件形状在源码可见，但宿主升级漂移 `[未验证]`，`plugin.ts:3004-3009` 已 fail-open 处理注册失败）；S1-B 的 per-chunk `serve_id` registry 结构改动须重钉 hook 行为测试并留字节恢复证据（AC-4）。
- **低**：10x-1000x 增长估计与标定阈值（未做压力测试，OD-3）。

## 15. 建议（下一步，需要谁）

1. main 把本文 + slices 交独立设计 QA（跨族）取 CLOSABLE 后冻结；冻结≠工程派发。
2. owner 裁决 OD-1/OD-2/OD-3（§10），再决定是否给 S1 工程 start 授权；工程须走 issue → branch → PR，spare port + temp home（`AGENTS.md:76-79`）。
3. 本片落地后，S0 以只读 join 使用台账；D5（消费门控回弹）另案立项，不得夹带进本片。
