# PRD-281：回忆时序适用性——显式到期降权（设计冻结）

日期：2026-10-31。状态：设计交付（B281-FIXTURE），待 B281-READ 工程实现与独立 QA。审查基线：`d4a16d88005c9b01a1c51c34e3847050b9a22d5a`。范围为**仅设计**：本批不写代码、不改任何行为；实现批次为 B281-READ，其写面冻结见下文专门小节。设计约束 D04（owner 已批，绑定）：首版当前性**仅解析显式到期表达**；默认注入**不给已到期约束当前指令地位**，历史读取保留；模糊时间保持 unknown；**不按年龄统删/统降**；**不把引用存在当当前有效**。

## 证据与结论边界

| 标记 | 来源与能够支持的结论 | 不能支持的结论 |
| --- | --- | --- |
| R：历史作者报告 | [#281 报告]（已装 0.0.1 运行时）：共享 ops 常用词表的回忆查询把两条 2026 年 9 月上旬 chunk（带显式过去到期时间 2026-09-08/09 的临时使用约束）顶到相似度 0.95–1.02 的前位，高于真正相关结果；一条新 pin 的 fact（sim 1.83）约 60 秒后才出现在第 1 位。期望：带显式已过期有效窗的 chunk 应被时序相关层强降权（或过滤），不能压过新鲜同题 fact | 不视为本轮直接观察；两条 chunk 的逐字文本本批不可得，其措辞是否落在冻结语法内**此处无法证实** |
| S：源码检查 | 本文件全部 file:line 绑定均来自基线 `d4a16d8` 的实际代码，本人已逐点读过 | 未运行任何回忆实验、未起 daemon、未跑测试（设计批红线） |
| U：UNKNOWN | 两条真实 chunk 文本、"similarity"数值的准确来源面、~60 s 可见性滞后的成因 | 不补写 PASS/FAIL |

"similarity 0.95–1.02 / 1.83"按报告原样引用。一个标注为**假设**的映射：`/memory/recall` 线上唯一的分值字段是 `entries[].score`（融合总分，chunk 上限约 2.8；memory.py 的 `_entry_payload`），而 store 层相似度在候选构建处被截到 [0,1]（hybrid.py:305），故报告数值与融合分读法一致——仅为假设，本批不验证。**~60 s remember→recall 可见性滞后明确不在本设计范围**。覆盖诚实声明：若 V305 实测发现两条真实 chunk 的到期措辞落在冻结语法之外，则 #281 在该点上保持 open、覆盖不成立——本 PRD 不据此追宽语法。

## 现状机制绑定（S，全部基线 d4a16d8）

**回忆排序（报告形状所在面）**：`POST /memory/recall` → `MemoryService.recall`（[memory.py:579](../../../src/mnemoseed_local/daemon/memory.py)）→ `HybridRetriever.recall`（[hybrid.py:181](../../../src/mnemoseed_local/retrieve/hybrid.py)）双轨并发 → `_merge` 排序。候选分值 = `_breakdown`（hybrid.py:461–485）：`1.0*semantic + 1.0*cue_overlap + 0.8*decay_weight + 0.5*graph_centrality + short_term_activation`；`semantic` 是 store 相似度截断值（hybrid.py:305），store 相似度 = 0.5*稠密余弦 + 0.5*稀疏余弦（[lancedb_embedded.py:353–364](../../../src/mnemoseed_local/storage/drivers/lancedb_embedded.py)，`_DENSE_FUSION_WEIGHT=0.5` 于 :68）。排序键 `_sort_key`（hybrid.py:388–392）= `(rescued, -score, kind, id)`。`Assembler.assemble`（[assemble.py:218](../../../src/mnemoseed_local/retrieve/assemble.py)）按 `effective` 序做 top_k/预算准入（`_select`，assemble.py:315–403，键于 :328–336），**渲染序**另行按原始 `candidate.score` 排（assemble.py:292–304）。既有降权先例：Freshness Guard 把生效分乘 `freshness_demotion=0.8` 后重跑选择切割（assemble.py:277、:86）。

**默认注入（T2）不走上述排序**：UserPromptSubmit ingest → `note_user_prompt`（memory.py:1338）→ `_focal_scan`（memory.py:1368–1445）：无嵌入、实体锚定元数据读，贪心准入按 `decay desc → ingested_at desc → turn_start desc`（排序于 memory.py:1447–1449）。hook 只渲条目 text 行 + pin 后缀（[t2.py:27–114](../../../src/mnemoseed_local/hosts/common/t2.py)），不渲 flags。三个消费面（MCP 网关透传 [server.py:203–207](../../../src/mnemoseed_local/mcp_gateway/server.py)、CLI `cmd_recall`（[cli.py:1095–1100](../../../src/mnemoseed_local/cli.py)）按载荷序渲染、hook）**都不重排**——daemon 出序即最终序。

**hook 点可用的 chunk 数据**：候选构建处 chunk 逐字文本与全部 stamp 已在内存（零额外 store 读）：`text`（verbatim 通道，"user:/assistant:" 拼装，[stamper.py:135–147](../../../src/mnemoseed_local/capture/stamper.py)）、`ingested_at`、`decay_weight`、`last_reinforced`、`turn_start/end`、`cues.time_bucket`、`provenance.asserted_at/source`（[stamp.py:85–122](../../../src/mnemoseed_local/schema/stamp.py)）。**chunk 无任何有效窗/到期字段**；`expires_at` 仅存在于 bearer Token 行（[ports.py:273](../../../src/mnemoseed_local/storage/ports.py)），`ChunkFilter` 无有效窗谓词（ports.py:110–139）。graph 节点另有双时相 `valid_from/valid_to`（[graph.py:106–107](../../../src/mnemoseed_local/schema/graph.py)），读侧冲突配对已把 `valid_to` 非空节点排除出"当前生效"集（assemble.py:571–573）——graph 侧既有先例，chunk 侧无对应物，此即缺口。

**今日不存在任何到期解析**：capture 侧 R1/R2/R3（stamper.py:150–176）是近重复改写冲突检测（时间**范围词** 以后/from now on 只是 R3 信号之一），不是有效窗解析；cue 抽取无日期解析（`time_bucket` 为信封透传，[cues.py](../../../src/mnemoseed_local/retrieve/cues.py)）。recall 的 `now` 仅在带 session_id 时传入（memory.py:600–602，供短期激活），且 hybrid 内该 `now` 语义即"读取时刻"。

## 冻结的语法与语义（v1，核心冻结）

**显式到期表达 v1 = 同时满足三条**：

1. **有效性标记**直接管辖同一子句内的日期 token。冻结清单——EN：`expires`、`expiry`、`expiration`、`valid until`、`valid through`、`valid before`、`good until`、`available until`、`effective until`、`until`、`before`；ZH：`有效期至`、`有效期到`、`有效期：`、`截止`、`截止至`、`截止日期`、`到期日`、`失效时间`、`过期时间`、`…前有效`、`…之前有效`。清单即全集；扩集必须改版冻结（v2），实现批不得即兴增词。EN 标记一律 casefold 后匹配（大小写不敏感，承接计划 §3.3）；ZH 标记按原样匹配。
2. **被管辖 token 只接受两种形态**：带显式时区的 ISO 8601 instant（`YYYY-MM-DDThh:mm[:ss[.f]](Z|±hh[:mm])`）；ISO 历法日 `YYYY-MM-DD`。
3. **位置洁净**：匹配不在引号 span（`"…"`、`'…'`、反引号、`「」『』`，先剥离——`_QUOTED` 先例 stamper.py:171）内，不在 URL/路径状 token（含 `://` 等）内。

**明确排除（OUT）**：相对/模糊词（下周、明天、next week、soon、recently）；月名与斜杠序日期（`Sep 8`、`09/08`）；缺年日期（`9月8日`）；**无时区 ISO instant**（→ unknown）；URL/标识符/引用内容/代码 span 内的日期；仅有起始表达（`valid from`，v1 不做"未生效"语义）；一切按年龄的推断。

**UTC 归一化**：全部解析为 UTC epoch 与读取时刻 `now` 比较。date-only 归一为当日 `23:59:59Z`，now ≥ 该界即降权——**相等即已到期**（承接计划 §3.3“等于或大于为 expired”）。如实边界：对 UTC 作者精确；对东半球（UTC+）作者偏晚 ≤14 h（欠降权，方向安全）；对西半球（UTC−）作者偏早 ≤12 h（早降权，错误方向但上限有界、损害仅为多降权一条历史 chunk）。报告案例（09-08/09，今日 10-31）距任何界均数周，不受此界影响。

**unknown 规则**：解析不出 → unknown；时区缺失/斜杠序歧义/缺年 → unknown；**同一条文本解析出两个不同 instant → 冲突 → unknown**（D04）；相同 instant 重复出现不算冲突。unknown → **不降权**（fail-open：漏降权的后果是维持现状，误降权的后果只是历史 chunk 排位后移，前者损害更小）。残余风险（如实）：同句续期/否定式（如 expires 2026-09-08, renewed through 2027-01-01；no longer/不再）在 v1 仍可能解析出首个到期并降权——损害限于把仍有效 chunk 排位后移，接受为 v1 边界；negation/renewal 剥离属 v2 改版冻结项。

**权威（谁的到期算数）**：v1 唯一权威 = chunk 自身 verbatim text。chunk 无周围有效窗元数据可与文本冲突（现状绑定见上节），provenance 字段（asserted_at/confidence/source）是捕获事实，**永不**充当有效窗；任何元数据在 v1 既不能创设也不能撤销到期。graph 节点不在 v1 范围（自有双时相链 + 既有读侧消费）。

## 语义决策表（映射 D04）

| D04 条款 | 定案 | 机制落点 |
| --- | --- | --- |
| 首版仅解析显式到期表达 | 上节语法 v1，逐词冻结 | 纯函数求值器（读侧） |
| 已到期显式窗 → 默认注入不给当前指令地位 | **结构化排名层（tier）**：带已过期显式窗的 chunk 候选排在**全部**未到期候选之后，到期内互相按既有 fused 分排序；非排除、非改分 | 排名权威点（见决策记录） |
| 历史读取保留 | tier 非排除：到期 chunk 仍在池内、仍被准入返回；池内全是到期者时照常服务；显式问过去的查询在其为唯一相关命中时照常返回 | 同上（无排除路径） |
| 模糊时间保持 unknown | unknown → 不降权，无隐式时间推断 | 求值器 unknown 规则 |
| 不按年龄统删/统降 | 触发条件唯一：显式解析出的到期 instant ≤ now（相等即已到期）；`ingested_at`/`decay_weight`/强化年龄**一律不参与**判定 | 求值器仅读 text + now |
| 不把引用存在当当前有效 | 引用（引号内容/URL/代码 span 内日期）不创设到期；chunk 未解析出自身到期 → 无降权 | 位置洁净规则 |

**机制选型（备选对照，定案 C）**：A. capture 时解析 + `ChunkStamp` 新字段——需 schema/驱动/迁移与既有 chunk 回填，两条报告 chunk 在迁移前**不被修复**，违反最小修复；弃。B. 融合分乘法降权（Freshness Guard 式）——改分点少，但非类目性（强到期候选乘弱系数仍可压过弱新鲜候选，系数需调参），且改动线上 `score` 语义；弃。C. **读侧结构化 tier（定案）**：类目性满足 D04（到期者对未到期者无当前指令优先级），候选级布字段仿 `rescued` 先例（hybrid.py:120–134、:295），text 已在候选上零额外读，不动 wire/score/捕获；采纳。D. 仅打 EntryFlag + hook 渲"已过期"标——不修排名（报告问题即排名），且 hook/T2 增渲染面；弃。E. 直接过滤——破坏历史读取保留；弃（D04）。

**tier 键冻结**：四处排序点统一由 `(rescued, -分, kind, id)` 改为 **`(expired_window, rescued, -分, kind, id)`**——`_sort_key`（hybrid.py:388）、`_select` 准入键（assemble.py:328–336）、渲染序键（assemble.py:292–304）、`_focal_scan` 排序键（memory.py:1447–1449，等价的候选元组层元素）。到期层置于 rescue 层**之前**的理据：rescued pin 是用户显式 flashbulb（推定当前有效），已到期约束不得排其前——D04 优先。这是对 design/09 §3.5"rescued 永尾随全部 normal"的有意修订（rescued 尾随**全部未到期** normal；expired-rescued 仍居末），实现批与 QA 均按此口径，不得按旧口径判违规。

### 对计划 §3.3 的修订声明（B281-READ 清单对齐用）

本 PRD 相对已批准计划 §3.3（[plan.zh-CN.md:57–66](../../../.mnemoseeding/jobs/launch-plan-309-20261029/accepted-v1/plan.zh-CN.md)）的取舍逐条声明；B281-READ 清单引用计划 §3.3 验收措辞处，以本节为准。

- **标记清单放宽 + date-only 接受**：计划的 4 个标记（`valid_until=<RFC3339>`、`expires_at=<RFC3339>`、`expires at <RFC3339>`、`有效期至：<RFC3339>`，中/英冒号均可）扩至本 PRD 冻结清单（EN 11 + ZH 11，`有效期：` 含中/英冒号两形），并新增 date-only 接受。理据：D04 授权“显式到期表达”的自然语言窄集，报告形状是自然语言约束而非键值对。**键值对形 v1 未纳入**（覆盖收窄项，如实声明），V305 若见真实使用即 v2 改版候选。
- **三态 → 二值**：计划派生状态 `active|expired|unknown`（带可观察派生状态）改为候选级二值 `expired_window`；unknown 表现为“不降权 = 原排序行为”（与计划“未知为原有排序行为”对齐），wire 零新字段。理据：D04 未要求三态服务面，排序 tier 即唯一可观察输出。
- **`temporal_mode=current|history` 不做**：wire 冻结（无新 recall 参数）；历史读取由 tier 非排除保证（历史查询在到期 chunk 为相关命中时照常返回）。理据：单路径、无查询意图分类器（KISS）；计划要求的历史可达性达成，形式不同。
- **T1/T2 中性围栏标注不做**：v1 hook 渲染面零改动，到期条目仍为普通文本行（围栏/免责行不变）；“当前约束/历史证据”分块与中性文案挂起至 v2。理据：报告问题在排名而非标注；标注要求 hook/`t2.py` 改面，超出最小修复。
- **“用户明确 pin 的结构化有效期优先”今日空真（vacuous）**：schema 与 ports 均无任何有效窗字段（[stamp.py:85–122](../../../src/mnemoseed_local/schema/stamp.py)；[ports.py:110–139](../../../src/mnemoseed_local/storage/ports.py)，`expires_at` 仅在 bearer Token 行 [ports.py:273](../../../src/mnemoseed_local/storage/ports.py)），不存在可被优先的结构化有效期，捕获文本也无从覆盖；条款在计划文本中保留，本设计无实现对象、不构成缺口。
- **保留对齐项（无修订）**：调用方注入 now（生产真实时钟、测试固定 now）、未知为原有排序行为、仍有效老决定/事实/pin 不按年龄整体降权、到期 ≠ 内容正确。

## 文件/测试面冻结（B281-READ 写面）

- **新增** `src/mnemoseed_local/retrieve/temporal.py`：纯求值器（语法 v1 + 归一化 + unknown 规则），入参 `(text, now)`，无内部时钟、无模型调用、确定性。
- **修改** `retrieve/hybrid.py`：`Candidate` 增 `expired_window: bool = False`；`_vector_track` 候选构建处置位；`_sort_key`（:388）增 tier 元素。
- **修改** `retrieve/assemble.py`：`_select` 键（:328–336）与渲染序键（:294–303）增 tier 元素。
- **修改** `daemon/memory.py`：`recall` 无条件传 `now=self._clock()`（:600–602；激活门控不动，`_merge` 既有 gate 不受影响）；`_focal_scan` 候选元组与排序（:1402–1449）增 tier 元素。
- **测试**：新增 `tests/test_retrieve_temporal.py`（语法单元 + 下表全部用例）；修改 `tests/test_retrieve_hybrid.py`、`tests/test_retrieve_assemble.py`（tier 序/修订后 rescue 纪律/age 负例）、`tests/test_recall_pending.py`（T2 tier 钉）；修改 `tests/test_retrieve_hybrid_concurrent.py`——其字节等价钉（docstring :5 “the concurrent output must be byte-identical to the sequential reference”）显式约束 `expired_window` 置位必须位于两路共享的候选构建路径（`_vector_track`）且 `now` 以相同方式送达两路（`_recall_sequential` 同步增形参），该断言即共享路径置位正确性的检查，实现批不得以双路重复求值规避。
- **不做**：capture/、schema/、storage/、hosts/（hook）、mcp_gateway/、console/、config（**无新 config 键**——读侧排名常数随代码，先例 `freshness_demotion`）；无新 wire 字段/新 EntryFlag（v1 降权仅经排序可观察）。

## 用例冻结（每行 = 输入形状 → 期望 → 夹具）

| # | 输入形状 | 期望排序/注入结果 | 夹具 |
| --- | --- | --- | --- |
| 1 | 报告形状 A：临时使用约束 chunk，显式到期 `until 2026-09-08`（ISO 日），查询共享 ops 词表，池内有新鲜同题 fact | 排全部未到期候选之后；仍可返回（非排除） | 新 `test_retrieve_temporal.py` 回归钉（合成池 + 精确 rank 断言） |
| 2 | 报告形状 B：同上，到期 `until 2026-09-09` | 同上 | 同夹具第二例 |
| 3 | 仍有效的旧决定：显式未来到期（`valid until 2027-01-01`）或无标记开放式旧 chunk | 不降权，按既有 fused 序 | 新夹具 |
| 4 | 历史查询：明确问过去，到期 chunk 为唯一相关命中 | 仍准入返回；top_k 挤出时如实计 `dropped_count` | 新夹具 |
| 5 | 时区边界：`…T23:59:00+08:00` / `…T23:59:00Z` / 无时区 instant | 前两个按归一化判定；无时区 → unknown 不降权 | 新夹具 |
| 6 | 歧义日期：`09/08`、缺年、月名 | unknown → 不降权 | 新夹具 |
| 7 | 假到期：日期在引号/反引号/URL 内（banner said "expires …"；链接路径） | 不降权 | 新夹具 |
| 8 | 冲突到期：同 chunk 两个不同 instant | unknown → 不降权 | 新夹具 |
| 9 | T2 默认注入：到期约束 chunk 与新鲜未到期候选同过 focal scan | 到期者排全部未到期之后（预算内更先落选），绝不居首 | `test_recall_pending.py` 增钉 |
| 10 | 年龄负例（D04）：老 `ingested_at` chunk 无到期标记 vs 新 chunk | 排序与今日逐字节一致（无年龄降权） | 新夹具 + hybrid 钉 |
| 11 | 精确边界：到期 instant `…T12:00:00Z`，读取时刻 now 恰等于该 instant | 计为已到期，降权触发（now ≥ 界） | 新夹具（`test_retrieve_temporal.py`） |

行 1–2 即报告形状回归夹具。**如实声明**：夹具文本按报告形状**重构**（显式 2026-09-08/09 到期标记）；真实 chunk 文本若（V305 实测）使用冻结语法外的措辞（如缺年中文日期、相对词），则报告形状在真实库上不被匹配，#281 就该点保持 open——本设计不以追宽语法换取名义覆盖。

## 理论锚

入选标准同 B2.1/B2.4：只列有实验与复现证据的规律；每条给来源、规律级表述、设计规则。机制层（正则语法、排序键、UTC 归一化）不入本节。

### TA-14 意向优先性与完成失活——已到期约束失去"待执行"地位

- 来源：Goschke & Kuhl（1993），intention superiority effect（提议登记 REFERENCES.md 新条 R57：*Representation of intentions: Persisting activation in memory*, JEP:LMC 19(5)；DOI 10.1037/0278-7393.19.5.1211；✅ Crossref 核验命中（2026-10-31，本 PRD 编写时直接核验））。
- 已验证规律：未完成（pending）意向相对已完成意向保持持久的可及性优势；意向完成后该优势回落。
- → 设计规则：带显式已过期有效窗的临时约束在"当前指令"意义上不再是 pending 意向，默认服务面不得保留其当前指令优先级（tier）；这不删条目、不禁可达——完成/过期的意向仍可被回忆，只是不再占据待办位。

### 继承 R26（McDaniel & Einstein 2000，多进程前瞻记忆框架）——焦点线索规则

- 已验证规律：前瞻提取在线索为焦点（focal，注意力正落于线索及其与意向的关联）时可靠；依赖自启动加工的提取更易失败。
- → 设计规则：有效性判定**只由显式解析的到期表达（焦点线索）触发**；模糊时间不做任何自启动推断（unknown 保持 unknown）。

### 不借用清单

- 序列位置近因优势（R24 Murdock）——不借用：自由回忆近因是**编码时邻接**的输出序规律，不是有效窗规律；借它等于给按年龄排序发执照，D04 明禁。
- ACT-R base-level 衰减（R23）——不借用：衰减已是独立机制（`decay_weight`）；到期 tier 不得从年龄/激活导出。
- 干扰论（R29 Wixted）——不借用为删除执照：干扰解释可及性下降，不给统删/统降授权。
- 流行神经：海马"时间细胞"给记忆打时间戳、固定记忆固化周期数——不借用：单模型/单研究主张，无人类长期复现。

## 验收记录

**B281-READ 必须证明**：(1) 用例表 11 行逐行有夹具测试且全绿；(2) 报告形状回归（行 1–2）在合成池上精确钉位；(3) 年龄负例（行 3/10）证明无任何按年龄的行为；(4) 修订后 rescue 纪律口径（tier 键冻结段）有显式测试钉；(5) wire 零变化（`/memory/recall` 载荷形状、flags、字段集与基线逐字节同）且全量 gates（pytest/ruff/format/mypy）绿；(6) 收口报告如实声明：真实 chunk 文本的语法覆盖未在本批验证，属 V305。**保持 open**：B281-READ 工程实现；V305 在装机运行时上的实库验证（含两条真实 chunk 是否命中冻结语法、报告数值来源面确认）；语法 v2 扩集只能以修订本冻结的方式立项，不得在实现批内即兴。
