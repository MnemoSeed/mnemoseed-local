# PRD · #271 保留日历边界覆盖（retention calendar boundary）

> 立项：owner 2026-10-23 指令（#271），launch-plan-309 单元 `D271-BOUNDARY`（kind=design，
> unblock=`D206-CALENDAR`）。本批只交付**设计与判定文档**：不改任何代码、不启动采集、不改候选值、
> 不批准下文任何方案。#206 真实日历观察（`D206-CALENDAR`）在本文件的判定与后续单独评审完成前保持
> BLOCKED。本文所有源码锚点基于 worktree `w271` commit `78478ed`。

## 1. 目标与非目标

- 目标：诚实回答一个问题——在当前已合并的仪器 fixture 下，**有没有任何 receipt 能在真实观察窗内
  跨过候选 30 天截止边界**（recent → expired 的翻转被活数据观察到）？给出算术、给出哪些检查只剩
  supporting-evidence-only、给出修复覆盖所需的独立变更/证据计划草图。
- 非目标：不批准采集（`D206-CALENDAR` 的 unblock 仍要求 "Boundary accepted + 新 cohort 显式批准 +
  真实 30 日窗成熟"）；不改 `SEED_CLOCK_EPOCH` 或任何 fixture/CLI 面；不重开已冻结的 P2 工程结果
  （PRD-206 §15.6）；不做任何日历学分声明。

## 2. 现状机制绑定（file:line）

### 2.1 固定 2023 epoch 播种

- `src/mnemoseed_local/instrument/cli.py:112`：`SEED_CLOCK_EPOCH = 1_700_000_000.0`
  （= 2023-11-14T22:13:20Z）。`_seed_daily_home`（`cli.py:416-425`）把它同时传给 control 对与
  daily 对的 fixture builder。
- daily cohort 播种：`build_daily_cohort_fixture`（`fixtures.py:120-142`）按
  `RECIPE_KINDS`（`fixtures.py:54`，恰 2 个 recipe：`stale_revision`、`protected_endpoint`）生成
  receipt，`applied_at_epoch = epoch + 0.5 + index`（`fixtures.py:127`）→ 两条 receipt 的
  `applied_at` 恒为 `1_700_000_000.5` / `1_700_000_001.5`（2023-11-14T22:13:20.5Z / 21.5Z）。
- control 对播种：`build_repair_control_fixture`（`fixtures.py:145-176`）播 105 条 receipt，
  `applied_at_epoch = epoch + index`（`fixtures.py:161`）→ 2023-11-14T22:13:20Z 起的连续 105 秒。
- 播种 manifest 绑定该 epoch：`_seed_manifest(..., seed_clock_epoch=...)`（`fixtures.py:205-213`），
  epoch 变化 ⇒ 新 home 产生新 `seed_manifest_hash`，不触碰任何已存在 home。

### 2.2 receipt 携带的时间戳

- `ReconciliationReceipt`（`storage/ports.py:326-349`）唯一的时间字段是 `applied_at: float`
  （`ports.py:349`；application 侧同 `ports.py:322`）。receipt 不携带任何独立捕获时刻字段。
- 持久化：`_insert_receipt` 以 ISO8601 文本写 `applied_at`（`sqlite_graph.py:1058`，经
  `iso8601_utc`），读取侧 `_receipt_applied_at` 用 `epoch_from_iso` 解码（`sqlite_graph.py:180-185`）。

### 2.3 保留判定比较哪两个时钟

- 投影是纯可见性层：`age = now_epoch − applied_epoch`，`age >= cutoff` ⇒ `expired`
  （`instrument/projection.py:100-104`）。被分桶的 receipt 逐日翻桶的唯一驱动是 `now_epoch`。
- `now_epoch` 来自 `now_raw`，且**仅当** `clock_source == 'os_utc_wall'`（`projection.py:22`、
  `projection.py:142-153`）才有效；`now_raw` 在 collect 中取**真实捕获 PRE**
  （`cli.py:1165-1170`：`now_raw=capture.collected_at_raw`；`collected_at_raw := capture_pre`，
  PRD-206 §7）。summary 以最新闭合 run 的 terminal `collected_at_raw` 重投影（`cli.py:645`）。
- `cutoff` 每次显式传参、无默认回退（`cli.py:1340`/`cli.py:1351` `required=True`；
  `projection.py:123-133` 非法即全部 unclassified + `invalid_cutoff`）。
- 即：**aging 对象 = 未决（unresolved）reconciliation receipts；起始/到期可见性 =
  真实捕获 PRE（观察钟）vs receipt 的播种 `applied_at`（合成钟）**——两者在代码里已严格分离。

### 2.4 候选 30 天截止与观察窗的出处

- 候选值：issue #271 正文（"proposed 30-day retention cutoff" + 2026 观察起点）；
  launch-plan-309 `work-packages.json` 的 `D206-CALENDAR` 单元（"Actual30calendar-day isolatedcohort
  evidence"，unlock 含 "real30daywindowmatures"）；候选数值 2 592 000 s 已被测试钉为
  `THIRTY_DAYS_IN_SECONDS`（`tests/instrument/test_projection_boundary.py:27`、
  `test_projection_diagnostics.py:17`）。
- 两个"30"不是一物：`OWNER_GATE_DENOMINATOR = 30`（`instrument/ledger.py:28`，guard
  `ledger.py:520-525`）是**日历分母**（30 个真实 admitted 观测日），与 receipt 年龄截止候选值
  无推导关系，不可混用。
- launch-plan-309 `plan.zh-CN.md:130` 明确把本问题前置："#271先解决seed能否在观察期跨边界"。

### 2.5 seeded receipt 的实际 disposition（进入投影面的事实）

- fixture 传 `disposition="accepted"`（`fixtures.py:275`），但两个 recipe 是**故意语义失败**的
  （stale revision / protected endpoint），驱动 `apply_reconciliation` 的语义失败路径落库为终态
  `unresolved`（`sqlite_graph.py:886-891`）；`test_cli_daily_run.py:469-472` 断言 daily 对的 DB 行
  恰为两条 `unresolved` receipt（reason_code 即两个 recipe 名）。
- 投影读取面只取 unresolved：`WHERE disposition='unresolved'`
  （`instrument/sql_readonly.py:39-40`，`read_unresolved_receipts` `sql_readonly.py:235-251`），
  collect 据此构造 `ProjectionInput`（`cli.py:763-772`）。所以这两条 2023 receipt **恰好就是**
  真实观察里被逐日分桶的全部对象。

### 2.6 观察期内 receipt 集合冻结

- collect 对 cohort 对严格只读（PRD-206 §15.2 D1 裁定；`cli.py:750-819` 只读适配器 +
  节点完整性夹逼 `cli.py:797-803`），仪器 home 不跑 daemon/consumer，cohort 对在观察窗内**零写入**。
  ⇒ receipt 集合 = init 播种集，全程不变；且 receipt 身份不可变（同 nomination_id 同 input_hash
  直接返回既有行，`sqlite_graph.py:792-801`，无 UPDATE 路径）。

## 3. 判定（算术）

设真实观察首采 PRE 为 T0（2026 年内任意一天）。对 daily 对的两条 receipt：

- 对**每一个可能的 2026 day-0**（窗口最早端 2026-01-01T00:00Z）：`age(T0) ≥ 778.07 天
  ≈ 67.2e6 s ≥ 25.9 × 2 592 000 s`；作者日期（2026-11-05）点值 `≈ 1086.07 天 ≈ 36.2 ×`。
  全窗口内每个可能的 day-0 都远超候选截止，结论与 day-0 的具体日期无关。
- 首次采集时两者即已 `age >= cutoff` ⇒ 落 `expired`（`projection.py:101-102`）；此后每日
  `now_epoch` 单调不减而 `applied_epoch` 固定 ⇒ age 单调不减 ⇒ **桶标签永不翻转**。
- 2.6 已排除中途出现新 receipt ⇒ recent → expired 跨越在真实观察窗内的可观察次数 = **0**。
- control 对的 105 条 receipt 同为 2023 epoch，但 control 面度量的是 repair/ACK 行为，不是
  daily 投影面；结论不受其影响。

**判定：当前 fixture 下，边界跨越覆盖 = 0——没有任何 receipt 能在真实观察窗内跨过候选 30 天边界。**
按 #271 done-when 第 2 条，`D206-CALENDAR` 真实采集启动保持 **BLOCKED**，直到下述某个变更/证据计划
经**独立评审被接受**。诚实记录：本判定不是"部分缺口"，而是该候选边界的行为（翻桶时刻、
`age == cutoff` 精确帧）在活数据中完全不可见。

### 当前 fixture 下仍可作为 supporting-evidence-only 的活检查

| 检查 | 状态 | 依据 |
|---|---|---|
| 两条 receipt 每日投影恒为 `expired`（稳定性） | 活数据可查 | `projection.py:100-104` 单调性 |
| receipt 21 字段交叉核对逐日一致 | 活数据可查 | `cli.py:773-780`、PRD-206 §8 |
| 日历准入（真实采集行，30 分母） | 活数据可查，与 receipt 年龄无关 | `cli.py:1128`、`ledger.py:28` |
| 时钟链/floor/`clock_backward` 防回拨 | 活数据可查 | PRD-206 §7 |
| control repair 105/5/0、逐行 ACK 幂等 | 活数据可查 | PRD-206 §8 |
| `age == cutoff` 精确边界、`future_timestamp`、cutoff 守卫 | **仅单测**，活数据不可见 | `test_projection_boundary.py:112-153` |

## 4. 独立变更/证据计划草图（各自需单独评审；本批不批准任何一个）

### 方案 A（推荐）：capture-time-anchored synthetic ages（全新显式 home）

- 机制：`init` 播种 daily 对时，以**当时真实宿主时钟** T_seed 为锚，显式声明合成年龄偏移
  `applied_at = T_seed − k × 86400`，k ∈ {0, 5, 25, 29, 30, 35}。自 T_seed 起第 d 日该 receipt
  的 age = k + d ⇒ 翻桶发生在 d ≈ 30 − k：k=0 的 receipt 在 T_seed 后第 30 日、k=29 第 1 日、
  k=25 第 5 日、k=5 第 25 日；k=30 在 day-0 即已到期；k=35 恒 expired。「第 30 − k 日」仅当
  采集与 T_seed 同 time-of-day 时精确成立，否则随捕获时刻早晚 ±1 天移动；首采晚于播种亦同向
  前移。活证据按 §5 第 1 条的 **straddle**（前一日 `recent`、次日 `expired`）对账；
  `age == cutoff` 精确浮点帧保留给单测（§3 表）；变更批必须在 init 报告里逐字记录 T_seed
  与各 k。2023 旧 receipt 原样保留为"已过期恒定"对照行。
- 不变量符合性：applied_at 是**调用方给出的合成元数据**（PRD-206 §7 已冻结该措辞——"可 stagger、
  可回填"），本方案只是把它的取值锚到真实播种钟；`clock_source='seed_fixture'`、
  `calendar_eligible=0`、零日历学分（学分仅来自真实 `os_utc_wall` collect 行，`cli.py:1128`）；
  旧记录保留（不动任何既有 home）；零历史日学分（新 home 新 cohort 新 manifest，无回填）。
- 落地面（修正）：epoch **已经**参数化（`build_daily_cohort_fixture(seed_clock_epoch=...)`
  接受任意有限 epoch——`tests/instrument/test_store_topology.py:42` 即用 1_600_000_000.0；
  `_require_epoch` 校验已存在，`fixtures.py:122`），但 **per-receipt 年龄配方没有**：
  `build_daily_cohort_fixture` 硬编码恰 2 条 receipt、`applied_at_epoch = epoch + 0.5 + index`
  （`fixtures.py:123-131`）。因此上面声明的 6 值 k 集 + 同 home 保留 2023 对照行**不能**只靠
  "CLI 常量/传参 + init 报告字段"到达——落地必须改 fixture builder 的 per-receipt 年龄配方
  （`fixtures.py:120-142`，S3 冻结面），或退到 epoch-only 多 home 近似；后者把单一 cohort
  碎片化成多 home/多 campaign，与声明的单 cohort k 集不合，不作为本方案落地形态。
  CLI 侧改动（`cli.py:112`/`cli.py:423`/`cli.py:425`）与 init 报告字段是配套面，不是充分面。
- 需单独评审的原因：触及冻结 S3/S7 面，且与 PRD-206 §15.6"无新 CLI 参数"声明冲突，必须走独立
  设计 QA + 工程 QA 后才能用于采集。

### 方案 B：显式 `--seed-clock-epoch` init 参数

- 方案 A 的参数化形态：新 init 参数在每个调用显式提供；缺省值沿用现常量 `SEED_CLOCK_EPOCH`
  （`cli.py:112`）以维持既有可复现性，无效值 fail-closed、绝不回退到宿主时钟。
  收益是不改代码常量即可为每个新 home 选择锚点；代价是新增 CLI 面（同上冲突 §15.6）。
  与方案 A 二选一，不并行；同样受 [I-2] 修正约束——参数化 epoch 不等于参数化 per-receipt
  年龄配方，到达 6 值 k 集仍需 builder 配方变更。

### 方案 C（诚实兜底）：纯证据计划（零代码）

- 接受覆盖为 0：边界行为显式降级为"单测已钉（`test_projection_boundary.py:112-153`）、活观察仅
  提供上表 supporting-evidence-only 项"，D206 启动附本文判定作为诚实边界文档。
- 代价：活数据永远看不到 recent → expired 与 `age == cutoff` 帧；候选截止值的"真实日历尺度"
  证据继续缺失。是否足以满足 `D206-CALENDAR` unblock 的 "Boundary accepted" 由 owner 裁定。

### 方案 D（否决，不列入候选）

- 观察中期用 `seed_receipt`（`fixtures.py:230-233`）向 cohort 对注入新 receipt。拒绝依据
  （修正）：collect 读路径写操作结构上不可达（D1，PRD-206 §15.2）；seed manifest 冻结
  `seed_clock_raw`（`fixtures.py:497`）与逐条 receipt 的 `applied_at`（`fixtures.py:517-529`，
  `_seed_manifest_hash` `fixtures.py:532-533`），中途插入使活库偏离冻结的播种身份，且无任何
  授权的调解路径（诚实出路只能是放弃该 cohort，fail-closed）。如实记录：节点完整性夹逼
  （`cli.py:797-803`）覆盖 nodes/version rows，**不覆盖 receipt 行**；`_observe_cohort`
  （`cli.py:1003-1021`）不做 receipt-set-vs-manifest 复验——故夹逼不是对 receipt 注入的
  阻塞机制，D 的拒绝不依赖它。明确拒绝。

### 各方案下 supporting-evidence-only 的剩余面

- 方案 A/B 落地后：仍只剩 control repair 计数、cutoff 守卫负例、`unclassified` 诊断路径为
  supporting-evidence-only（它们不依赖 receipt 年龄）；边界翻桶本身升为**活证据**。
- 方案 C 下：上表整体维持 supporting-evidence-only，边界翻桶永远不在活证据之列。

## 5. 验收记录（后续变更批必须证明什么）

任何被批准的后续变更批，开始采集前必须逐条演示：

1. 新 home 的 day-0 投影报告里存在 `recent` 桶 receipt，且后续观测日内出现 recent → expired
   翻桶，以 **straddle** 为准：相邻两次每日采集间该 receipt 从 `recent` 变 `expired`，且记录的
   原始捕获时点满足 `age(d−1) < 2 592 000 ≤ age(d)`；straddle 观测日与声明偏移 k 及记录的
   T_seed 对账（time-of-day ±1 移动按第 4 条 §4-A 措辞吸收）。`age == cutoff` 精确浮点帧
   要求真实捕获恰落在 `T_seed + (30−k)×86400`（`capture_pre = float.hex(time.time())`、
   `iso8601_utc` 向下取整到毫秒），排程采集不可达，**保留给单测**
   （`test_projection_boundary.py:112-124`），与 §3 表一致；若后续变更批提出确定性工程机制
   （如定时到精确时点的捕获），须随该批单独设计评审，本 PRD 不预设。
2. 合成年龄与真实时钟分离可审计：逐条 receipt 可查 `clock_source='seed_fixture'`、
   `calendar_eligible=0` 与声明的 k 偏移证据；receipts 不产生任何日历学分或 admitted 日。
3. 旧记录保留：无任何 DELETE/UPDATE 路径触及 seeded receipts 与既有 ledger 行
   （append-only 触发器、不可变 receipt 身份仍绿）。
4. 零历史日学分：admitted 日期仅来自真实 collect 行（`cli.py:1128`），无回填、无 `--date` 伪造。
5. 全门禁（pytest/ruff/format/mypy）+ 具名对抗式 QA 0 BLOCKER/0 IMPORTANT，issue → branch →
   PR → merge 落地后，采集才可排期。本 PRD 自身不构成该验收。

## 6. 理论锚：不借用

- 本批是观察方法学与测量覆盖设计，**不新增借用任何神经科学/心理学理论**；候选 30 天值是工程候选
  参数，不主张任何记忆机制规律，其 ratify 属 #206 后续独立阶段。
- 显式 **not borrowed**（沿 PRD-206 §12 清单）：遗忘曲线/艾宾浩斯衰减、记忆巩固与再巩固、突触修剪、
  海马体睡眠重放、间隔重复最优间隔、认知负荷理论、米勒 7±2、任何"认知 30 天/21 天阈值"、
  多巴胺奖励预测误差、潜意识启动与"记忆宫殿"类比、左右脑分工。
- "到期 / expired" 仍是软件可见性标签（PRD-206 §12 末条），不描述任何心智过程。
