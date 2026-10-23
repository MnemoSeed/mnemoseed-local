# PRD-206 P2 隔离测量仪器（内部工具）

> 状态：独立设计 QA 已 CLOSABLE（0B/0I）并冻结（工程合同 composite hash
> `0bbee6fc6866804ad78e09c268478c75bc9ba2574f25eec0e23dd5870b87eb0e`），契约冻结与 S1 启动均已完成，
> workflow 为 EXECUTING gen5。S1 实现代码与测试已在本工作树完成，完整门禁与独立实现 QA 均绿，
> 但**尚未提交、尚未开 PR、尚未合并**；S2–S7 未启动。
> 本 PRD 只约束 P2 的**隔离内部仪器**，不新增产品面。理论锚：不借用（见 §12）。
> 本轮更新**只改本 PRD 文档**：未新增任何 P2 源码、测试文件、CLI 参数、manifest 键或 schema 变更。

## 1. 目标

操作者在隔离 home 下使用 store-level 仪器 CLI：初始化显式合成的 cohort，采集一次真实时钟观测，
在独立 control store 上运行受控修复与只读证据检查，并读取确定性汇总。P2 **实现并测试**该工具，
只跑一次性临时 home；不启动任何真实日历 cohort。

## 2. 非目标（P2 明确不做）

- 不启动真实 30 日历日采集；不产生任何有效观测日计数或日历学分声明。
- 不接线产品：不改 schema/config/route/默认 retention/daemon/MCP/HTTP/端口，不新增读机制参数面。
- 不做 retention 数值 ratify、不做 activation、不做产品到期判定；「过期」仅为可见性标签。
- 不做 provider 质量实验；不调用任何 provider；不读写的安装 runtime（`~/.mnemoseed-local`）。
- 不做跨库/跨文件原子性承诺；不补记历史、不自动重置、不自动初始化。
- 不重开 P1 语义（五值、门序、诊断词汇、报告量）。

## 3. 权威输入与哈希绑定

| 项 | 绑定值 | 说明 |
|---|---|---|
| 基线 commit | `16006b897a66b39f80748be85b48d46b312fce23` | 所有源码锚点以此为准 |
| P1 契约 | `instrument-contract.md`（freeze-candidate 正文 + 独立验收记录） | 语义权威，本 PRD 不改其任何一条 |
| 逐文件 sha256 | P1 `p1-source-bindings.md` 逐字清单与 `p1-source-supplement.md` | 本 PRD **不复制、不重算、不发明**哈希数值 |
| `source_commit` / `source_hash` | 上述 commit + **声明的**基线清单规范化 JSON 的 sha256 | 每次 run 记录，只读；`source_hash` 是**声明摘要**，仪器运行时不读取、不重算产品文件，**不证明磁盘文件与基线一致** |
| `schema_hash` | ledger DDL 文本 sha256（`schema_version=1`，DDL 逐字冻结） | 与实际 DDL 的 schema 哈希不符即 fail-closed；**无自动迁移、无自动重置** |
| `instrument_hash` | 仪器源码文件清单（path+sha256）规范化 JSON 的 sha256 | 每次 run 记录；与 `source_hash`/`schema_hash`/`config_hash` 是**四个互不相同的定义** |
| `config_hash` / `config_json` | 冻结配置证据文本（canonical JSON：`home`/`denominator=30`/`kind`/`cohort_id`/`control_id`/`repair_limit`）及其 sha256 | `config_json` 是 `cohorts` 新增的**唯一**不可变列；`config_hash = sha256(config_json)`；`repair_limit ∈ {None,100,99}` 是**声明值**，不重新推导；观测期内不变 |

- `source_hash` 的边界：清单路径**相对 `src/mnemoseed_local`**，必须落在冻结 7 路径集内
  （`storage/drivers/sqlite_graph.py`、`storage/drivers/sqlite_meta.py`、`storage/drivers/_migrations.py`、
  `storage/drivers/_time.py`、`storage/ports.py`、`dream/repair.py`、`schema/graph.py`），每值须为
  64 位小写十六进制**形状**检查；路径集或形状不符即 `cohort_identity_mismatch`。本 PRD 不给出任何哈希数值。
- `config_json` 的键集恰为上述六个键（无多无缺），须与解析内容的 canonical 重序列化**逐字节相同**；
  `home` 必须是真实解析路径；整数域（`denominator`、`repair_limit`）**拒绝 `bool`**。

## 4. 隔离拓扑与所有权

- 三个**物理独立**逻辑存储，各有显式绝对路径：`instrument/ledger.db`（append-only 事件）、
  `cohort/graph.db` + `cohort/meta.db`（日常 cohort）、`control/graph.db` + `control/meta.db`（控制 fixture）。
- cohort 与 control **不共享文件、不共库、不互为重建面**；control 的 repair/replay 永不触碰 cohort 对，
  cohort 的 pending/receipt 计数不因 control repair 改变。
- 隔离 home 分两个：**home A** = daily cohort 对 + 105 control 对（**同一 ledger、两条 `cohorts` 行**）；
  **home B** = 全新 105 control 对（**独立 home、独立 `control_id`、独立 DB 对**，cap=99）。每 home 一个 ledger，
  home B 永不重建、改名或重测 home A 的 105 对与 daily 对。
- **仪器自有 ledger DDL 的变更边界**（产品 schema 不变，见 §2）：`cohorts` 表在 P1 §4 之上**只新增一列**
  `config_json TEXT NOT NULL`（canonical 证据文本，创建即不可变）；**P1 §4 `events` 表的 21 个应用侧列 +
  `ledger_seq` 逐字不变**，配置证据不得渗入事件行。
- 全部路径位于显式临时 `MNEMOSEED_LOCAL_HOME` 之下；不使用 7788、不启动 daemon/consumer、零 provider。
- 无跨库事务：产品写由各自 API 提交，ledger 追加是其后独立事务；冻结 manifest、`cohorts` 行与 ledger
  是三次独立写入，部分初始化即 fail-closed，禁止自动补写。

## 5. 账本与单写者锁

- **全程单写者 OS 文件锁**：写者对整个「产品操作 + ledger 追加」序列持有 `instrument/writer.lock`
  的精确路径 OS 锁，覆盖序列全程而非单次追加。锁不可得、所有者未知或语义不明 → 该次运行 fail-closed：
  **不向 ledger 追加任何行**，改在 ledger 之外以独占创建写一次性调用回执
  `instrument/invocations/<run_id>.<attempt_id>.receipt.json`（不覆盖、零学分）；
  回执亦不可写 → 显式外部失败、trace 不可用。不接管、不按 PID 判陈旧、不删除锁文件。
- 单事件 `BEGIN IMMEDIATE` 只串行化一次追加，**不**承担单写者语义；这不是可降级的替代。
- **append-only**：`events`/`cohorts` 无 UPDATE/DELETE 路径，`BEFORE UPDATE`/`BEFORE DELETE`
  触发器 `RAISE(ABORT)`（负例期望 `IntegrityError`）；无 `status`、无 `admitted` 可变列。
- **幂等按完整应用侧不可变内容判定**，不看 `payload_json` 单列：逐字比较 `event_id`、`event_type`、
  `cohort_id`、`run_id`、`attempt_id`、`recovers_attempt_id`、`observation_kind`、`outcome`、
  `diag_code`、`clock_source`、`capture_pre_raw`、`capture_post_raw`、`monotonic_delta_ns`、
  `collected_at_raw`、`collected_at_iso`、`utc_date`、`calendar_eligible`、`target_event_id`、
  `reason_code`、`payload_json`、`appended_at_raw`；唯一排除 DB 生成列 `ledger_seq`。
  全部逐字相同 → 返回既有 `event_id`/`ledger_seq`，不产生第二行；任一项不同 → 拒绝追加并记
  `ledger_idempotence_conflict`（并列出差异列名），原行逐字不变。同键重试**必须提交与原事件逐字相同的 21 列
  应用侧内容**（含原 `event_id` 与原 `appended_at_raw`）：调用方不重新打戳、编号或序列化，ledger 也不重分配任何一列。
- 每行只写一个主失效 `diag_code`；其余失效码进 `payload_json.invalid_codes` 并全部参与准入；
  信息码进 `payload_json.trust_codes`，不使行失效。
- 事件类型、sentinel `attempt_id=''`（仅 `run_started`/`run_closed`）、`run_closed` 的「全部 attempt
  已 terminal 之后、每 run 至多一行」、`instrument_gap` 必填项、`payload_json.event_type` 逐字一致，
  均按 P1 §4 强制；无孤儿事件（`FOREIGN KEY` + `PRAGMA foreign_keys=ON`）。

## 6. 连续性 manifest（外置、fail-closed）

- `instrument/expected_cohort.json` 位于 ledger **之外**，是连续性的外置权威，键集**恰为十个批准键**：
  `manifest_version, cohort_id, kind, seed_manifest_hash, schema_hash, instrument_hash, config_hash,
  control_id, cohort_created_at_raw, frozen_at_iso`；`manifest_version=1`，未知键或版本不符即不符；
  随归属冻结、观测期内不变。
- **主行携带与 manifest 同键相同的业务 `control_id`**（home A 的 daily 主行带其链接 control 的业务 id；
  home B 的 `repair_control` 主行带自己的 id）；歧义由 `kind` 消解，**不由 NULL 消解**。manifest 的
  `control_id` 键等于该行 `control_id` 列，故 manifest 与 `config_hash` 同时绑定该链接；
  `find_cohort_by_control_id` 只选 `kind='repair_control' AND control_id=?`：0 行 → `None`，
  ≥2 行 → `cohort_identity_mismatch`，永不返回 `daily_cohort` 行。
- 每次 daily run 先读 manifest 再与 ledger `cohorts` 行比对，顺序固定，任何偏离 → `ledger_missing`
  fail-closed 中止、零学分：
  1. manifest 可解析、`manifest_version==1`、键集恰为上述十键；
  2. `cohorts[manifest.cohort_id]` 行存在，且其 `cohort_id` 与 `cohort_created_at_raw` 与 manifest 逐字相同；
  3. 行在 `{kind, seed_manifest_hash, schema_hash, instrument_hash, config_hash, control_id}` 上与 manifest 相等；
  4. **cohort_id 重导出**：由**行内持久化的 raw 创建值**（`cohort_created_at_raw`，唯一权威；不接受 CLI 传入
     ISO，不接受再次提供的创建时刻）重算 `cohort_id`，须同时等于行 `cohort_id` 与 manifest `cohort_id`；
  5. `verify_config_evidence(row)` 通过；
  6. 若 `row.control_id` 非空：linked 行存在、`kind='repair_control'`、`control_id` 三方相等、
     `schema_hash`/`instrument_hash`/`source_commit`/`source_hash` 与主行相同，且 linked 行自身
     `verify_config_evidence` 通过（其 `repair_limit ∈ {100,99}`）。
  缺失或错配的 linked 行即连续性失败：**不静默换链、不自动重链、不重播种**。
- **不初始化、不重播种、不因日运行发明新 cohort、不改 manifest**。新建 cohort 是独立的显式未来操作
  （新 manifest + 新 id），不是任何恢复默认。

## 7. 时钟与时间语义

- 采集区间 `[capture_pre, capture_post]`；`collected_at_raw := capture_pre`（`float.hex()`，唯一权威时点），
  `collected_at_iso` 仅展示/复核；`utc_date` 只在追加时派生一次、存储后不可变，跨 UTC 午夜只记起始日。
- **存储日期不可重算亦不可改写**：准入在**每次汇总**都必须由权威 `collected_at_raw` 独立重算 UTC 日期，
  与存储值不符即 `date_derivation_mismatch` 并排除该行；原行、原日期、原 payload 一律不回写。这是 P1 §4
  的既有语义（`utc_date` 等于由 `collected_at_raw` 重算的真实 UTC 日期），不是本 PRD 的新增要求。
- **原始真实时钟与合成 receipt 时间严格分离**：fixture 的 `application.applied_at` 是调用方给出的合成元数据
  （可 stagger、可回填），单独记录 `fixture_seed_clock` 与 `clock_source='seed_fixture'`、`calendar_eligible=0`；
  它既不是真实历史 apply 日，也不产生日历学分，也不使一次真实每日采集失效。
- 只有 `clock_source='os_utc_wall'` 的真实采集进入日历候选；失效码 = `clock_backward`、
  `clock_monotonic_inconsistent`、`clock_unbound`、`clock_precohort`；`clock_future_unverifiable` 仅信息性。
- 下界取自同 cohort 全部更早 collect 行的**原始时点**，含仅失败尝试留下的 raw 时点；首个成功样本同样受限。
- **持久化时间戳量化限制**：读取侧只读原始文本并独立 `epoch_from_iso` 解码，**不重新序列化、不回写、
  不重建文本**，也不以「解码后重编码等于原文」为不变量；oracle 只用精确可表示的 `.000`/`.500` 边界，
  任意毫秒值仅标注量化限制。本批**不修改产品序列化或毫秒量化行为**。
- `cutoff` 一律**显式传参**，无任何默认回退；字面谓词 = `isinstance(v,(int,float)) and not isinstance(v,bool)
  and math.isfinite(v) and v>0`（`bool` 永不当作整数），不满足 → 全部 unclassified + `invalid_cutoff`；无 date 参数、
  无 override、无回填；同一守卫（类型 + 非 `bool` + 有限）适用于每个调用方给定的数值时钟输入（→ `invalid_now`/`unbound_now`）。

## 8. 准入重算与控制面证据

- 排序权威是 `ledger_seq`（追加序），不是 `collected_at`：同日多有效样本时**最早 `ledger_seq` 持有**，
  其余为 `duplicate_suppressed`；乱序完成不能抢分。
- **撤销先于去重**：候选集先在 revocation 目标集上剔除，`shadowed` 只在幸存者之间计算；
  被撤销行单列 `revoked`（无幸存者标 `revoked_no_survivor`），不递补升格，不删除行、不改原时刻。
- `calendar_eligible` 是派生列的存储副本：由行内字段逐项重算，不符即 `calendar_eligible_mismatch` 并排除；
  绝不盲信已持久化的 `1`。分母恒为 `OWNER_GATE_DENOMINATOR=30`，owner 日历门保持显式、不机械 ratify。
- 控制面 ACK：control store 播 105 pending，`limit=100` 依次得 **100 / 5 / 0**（累计 105）；
  `limit=99` 必须在**home B 的另一对全新 control fixture**（独立 home、独立 id、独立 DB 对）上独立断言得 **99**，
  绝不在已排空的同一 store 补测；两 profile 的一次全局 repair 证明修复无 profile 谓词。
- 逐行 ACK 只能由真实 API 证明：对已投递行再调 `mark_reconciliation_audit_delivered` 期望 `False`
  且 `delivered_at` 逐字不变；不得以第二次全局 repair 的聚合 0 冒充逐行幂等。
- 只读 SQL 面仅指向隔离 `control/graph.db`（outbox）与隔离 `cohort/graph.db`（receipt 枚举、节点面），
  SQL 文本 sha256 进 provenance；**无直接 SQL 写、不新增 getter、不读 live 路径**。
- 保护端点证据须**同时**覆盖：`node_versions` 零新增行 + `(node_id, version, changed_at, payload)`
  逐行哈希与 schema 哈希不变，与 `get_node` 返回的**完整**当前节点内容哈希不变（不得只哈希
  `nodes.payload` 或只看 version 链；第二只读出口 `versions(node_id)` 仅作交叉核对）。
- 不可采不得伪装成空投影：receipt/时间戳/证据 id/store 绑定/schema/hash 任一缺失或无效 →
  `binding_incomplete` 的不可采行，绝不返回全空「成功」投影。
- **receipt 比对面（精确 21 字段）**：public 21 字段即 `ReconciliationReceipt` 字段集（`ports.py:325-349`），
  按逐列点名查询读取。可空字段 `winner_node_id`/`loser_node_id`（`str`）、`loser_prior_version`/
  `loser_new_version`（`int`）、`downweight_factor`（`float`）**先保 `None` 精确**：两侧皆 `None` ⇒ 相等；
  一侧 `None` ⇒ 不等；否则按各自类型比较（`str` 逐字、`int` 相等、`float` 精确相等）。**不得 `str(None)`，
  不得把 `None` 折成 `''` 或 0**。`applied_at` 以同一持久化文本两侧 `epoch_from_iso` 后精确浮点相等比较；
  `evidence_event_ids` 以 `list(receipt.evidence_event_ids) == json.loads(row_text)` 比较；任一不符 ⇒
  交叉核对失败并按 `binding_incomplete` 阻塞，绝不静默跳过。
- **schema 前置检查（集合/类型式，与物理列序无关）**：必需列集 = 上述 21 个 public 字段名 **加内部 `input_hash`
  列，共 22 个列名**，顺序无关；public 21 字段的比较顺序由 SELECT 列表给出；每列 `PRAGMA table_info` 类型串须等于
  §3 绑定表在 S1 记录的 source-bound 类型映射（源自迁移 DDL / `_RECEIPT_COLUMNS` 的源码检视）；缺列、多列、类型不符 ⇒
  `binding_incomplete`（detail 列出相关列名）。`input_hash` 只作**存在性验证**，**不进入 public 21 字段比较类型**；
  receipt 查询抛出的任何 `sqlite3.Error`（如 `no such column`）必须转为 `binding_incomplete`，**不得逸出为未捕获异常**，也不得产出 `observed_ok` 终态或成功交叉核对。

## 9. 切片、DAG 与文件所有权（S1–S7）

- 源码面（仓库相对）：`src/mnemoseed_local/instrument/{__init__,ledger,clock,fixtures,repair_oracle,
  sql_readonly,admission,projection,cli}.py`；测试面 `tests/instrument/` 下的固定文件集（见下表）。
- **S1 唯一**创建 `src/mnemoseed_local/instrument/__init__.py` 与 `tests/instrument/conftest.py`；
  **S1 之后任何片不得编辑 `__init__.py`**（不新增 re-export），跨片引用一律直接模块导入
  （如 `from mnemoseed_local.instrument.clock import ...`）；共享测试夹具只在 S1 的 conftest 提供，
  后续片只可通过自己片内的 helper 扩展，禁止复制粘贴同一夹具。违反即停止条件。
- 本 PRD 的**初始正文由 S1 拥有**，在 S2/S3 dispatch 前生效；**S7 只在其它片所有者全部 idle 后**
  把最终 closeout 段落序列化进本文件，**任何片都不写 `docs/zh/prd/PRD-B2-roadmap.md`**。

| 片 | 源码面（`src/mnemoseed_local/instrument/`） | 测试面（`tests/instrument/`） | PRD 面 | 依赖 |
|---|---|---|---|---|
| S1 ledger | `__init__.py`、`ledger.py` | `conftest.py`、`test_ledger_schema.py`、`test_ledger_append_only.py`、`test_ledger_idempotence.py`、`test_cohort_continuity.py`、`test_invocation_receipt.py` | 本 PRD 初始正文 | 无 |
| S2 clock | `clock.py` | `test_clock_binding.py`、`test_utc_date_boundary.py` | 无 | S1 |
| S3 fixtures | `fixtures.py` | `test_store_topology.py`、`test_control_fixture_105.py`、`test_control_fixture_99.py` | 无 | S1 |
| S4 repair/ACK | `repair_oracle.py`、`sql_readonly.py` | `test_repair_ack_105.py`、`test_delivered_read_surface.py` | 无 | S1、S3 |
| S5 admission | `admission.py` | `test_admission_order.py`、`test_revocation.py` | 无 | S1、S2 |
| S6 projection | `projection.py` | `test_projection_boundary.py`、`test_projection_diagnostics.py` | 无 | S2 |
| S7 daily CLI | `cli.py` | `test_cli_daily_run.py`、`test_cli_restart_and_gap.py`、`test_cli_control_repair.py` | 仅序列化最终 closeout | S1–S6 |

- DAG：`S1 → {S2, S3}`；`S2 → {S5, S6}`；`S3 → S4`；`{S2, S3, S4, S5, S6} → S7`。真序依赖在同一任务内串行。
- 实工作树约定：`.mnemoseeding/worktrees/retention-p2-s1`（仓库绑定值），并行片各用独立 worktree 与分支。
- S7 仅编排：不含新判定逻辑；`ledger_missing` / `writer_lock_held` / `invocation_receipt_unavailable`
  原样 fail-closed 退出，无自动初始化、重建或重置路径。
- **运行态归 S1**（全部在 `instrument/ledger.py`，S5 导入，不重复实现谓词）：`RunState` / `read_run_state` /
  `find_prior_unclosed_run_ids` / `read_run_events` / `evaluate_run_closability` 与**唯一可闭性谓词**都在 S1；
  可闭性按固定原因序取首个：`no_attempt` → `open_attempt` → `gap_present` → `failed_outcome`；`run_closed`
  追加时 S1 自行执行该评估，故 S1 先落地即可闭合 run。correction/revocation 行**不是**观测尝试（不建 attempt、
  不需 `attempt_started` 链接、既不开启也不阻塞闭合）；只有 `attempt_started` / `attempt_terminal` /
  `instrument_gap` 参与；`run_closed` 还要求该 `run_id` 已有 `run_started` 行，否则拒绝追加。
- **初始化上下文归 S1**：`FreshInit` 在 `instrument/ledger.py`（不引入 S3，播种可调用对象为注入参数），
  顺序固定为 **取锁 → 锁内查新鲜 → 播种 → 建 ledger → 注册 cohort → 最后写 manifest**；
  `__enter__` 取 `instrument/writer.lock`（永不 unlink、不按 PID 接管）后在锁内复查新鲜度，失败即释放锁并
  抛 `cohort_identity_mismatch`（`reason='partial_initialization'`）；新鲜度检查每上下文**恰好一次**，
  未通过则 `seed()` 抛 `freshness_not_verified`。**不自动补写、不重播种、不清理任何产物（含锁文件）**，
  操作者改用全新 home。
- **诚实可观测边界**：锁文件**不是**新鲜度信号（它跨运行合法存在、永不删除），因此**锁文件单独不能证明
  曾发生过 crash**。只有持久产物构成拒绝依据（字面谓词：`exists_any_db_or_manifest or
  (invocations_dir.exists() and any(invocations_dir.iterdir()))`）：`instrument/ledger.db`、
  `instrument/expected_cohort.json`、`instrument/invocations/`（存在且非空）、任一产品 DB
  （`cohort/graph.db`、`cohort/meta.db`、`control/graph.db`、`control/meta.db`）或已注册的 `cohorts` 行；
  在任何持久写之前 crash **不留可观测痕迹**，故该 home 后续 `init` 不会被拒；`instrument/invocations/` **不存在或为空即新鲜**，无需预先创建该目录。不承诺不可检测的历史，不发明 PID/时间戳/陈旧启发式，也不新增持久初始化标记文件。

## 10. 验收行为（P2 可执行断言）

1. ledger 追加后 UPDATE/DELETE 抛 `IntegrityError`；重放同内容返回既有 `event_id`/`ledger_seq`。
2. 同键改 `collected_at_raw`/`utc_date` 的重试被拒并记 `ledger_idempotence_conflict`，原行逐字不变。
3. manifest 期望 cohort 而 ledger 缺失/身份不符 → `ledger_missing`，无新 cohort、无补写。
4. 第二写者持锁 → `writer_lock_held`，ledger 零新增行，ledger 外 sidecar 回执独占创建且不覆盖。
5. 合成 `applied_at` 的 fixture 记录 `calendar_eligible=0`，且不改变真实采集的候选资格。
6. 同日乱序两次有效 → 早 `ledger_seq` 持有；撤销唯一持有者 → 该日空缺 + `revoked_no_survivor`。
7. 105 pending 得 100/5/0；独立 99 对得 99；cohort 与 control 计数互不影响。
8. 投影 `cutoff` 缺失 → 全部 unclassified + `invalid_cutoff`，无 30d 回退；`age==cutoff` 判 expired。
9. 节点面：版本链零新增行且当前完整节点哈希不变；仅改 `nodes.payload` 被捕获为阻塞。
10. 隔离：全程 temp `MNEMOSEED_LOCAL_HOME`、无 7788、零 provider、不读安装 runtime。
11. 初始化：锁内新鲜度检查先于播种、manifest 最后写；存在残留持久产物 → `cohort_identity_mismatch`，
    无自动补写/重播种/删产物；仅锁文件存在**不**构成拒绝依据。
12. receipt 比对：可空字段 `None` 语义精确（不折 `''`/0）；缺 `input_hash` 或任一 public 列 →
    `binding_incomplete`；receipt 查询异常不逸出为未捕获异常。
13. `source_hash` 只来自**声明的**清单；清单路径不在冻结 7 路径集内或值形状不符 →
    `cohort_identity_mismatch`；仪器不声称已核验磁盘产品文件。

## 11. 执行与落地纪律

- **测试先行**：每片先写失败测试，main 在冻结 argv 与一次性 home 上记录**实际红相位证据**（失败原因须是期望行为
  尚不存在，而非语法/导入噪音）；红相位证据只本地留存，**绝不推送红提交**。
- **冻结 argv**：每片为 `uv run pytest <该片测试文件（按 §9 表序）> -q`，均带 `PYTHONUTF8=1`、临时
  `MNEMOSEED_LOCAL_HOME`、备用端口；文件清单即 §9 表，**不使用 `test_sN` 之类占位名**。冻结命令与门由 main 统一执行并记录。
- 绿相位门（每 worktree 各自跑）：`pwsh -File scripts/gate.ps1`（变更注释检查 + `pytest` + `ruff check` +
  `ruff format --check` + `mypy src`）；**独立 `senior-qa-reviewer` 对抗式复核在提交/合并之前**
  （不得 merge-then-QA），要求 0 BLOCKER 且 0 IMPORTANT 方可放行；CI 在 PR 上兜底。
- **每个已推送提交必须绿**：`scripts/check_commit_range.py` 对区间内每个提交读取廉价门（变更注释、
  `ruff check`、`ruff format --check`），类型检查与全量测试读 tip。发现某提交门红 → 修正该提交本身
  （amend）或补一个说明修复内容的后续提交，绝不折叠进后续提交或 merge。
- 落地路径：issue → branch → PR → merge；PR 正文 `Closes #<child>`；单提交 slice closeout（显式路径 staging）。
  基础队列 issue #255；**不得因仪器基础设施关闭父 issue #206**。
- 合并后清理：`git worktree list` 移除过期任务树，`git branch --merged main` 删除已合并分支。
- 并行片须先经 `solution-architect` 检查文件面重叠、测试 oracle 冲突与顺序，再执行。

## 12. 理论锚：不借用

- 本 PRD 是测量溯源与可见性分派的工程契约，**不借用任何神经科学/心理学理论**；不存在需要实证来源的
  规律、阈值或行为学假设。实现机制（OS 锁、append-only 表、哈希绑定、派生重算）与理论层严格分离，
  不得反向包装为心理学主张。
- 显式 **not borrowed**：遗忘曲线 / 艾宾浩斯衰减、记忆巩固与再巩固、突触修剪、海马体睡眠重放与
  「睡眠巩固记忆」、间隔重复最优间隔、认知负荷理论、米勒 7±2 与工作记忆容量、任何「认知 30 天/
  21 天阈值」、多巴胺奖励预测误差、潜意识启动与「记忆宫殿」类比、左右脑分工。
- 这里的「到期 / 过期」（expiry）只是软件可见性标签，不主张任何心智过程或生物机制。

## 13. 未定与待闭项

- 内部工程符号签名、异常类型、规范化序列化细节（schema JSON 与 `float.hex()` 规则、禁用不稳定
  `model_dump_json` 字节）、只读 SQL 的 query hash、跨平台 OS 锁原语与释放语义、事件版本化：
  **已通过独立设计 QA 并冻结**（工程合同 SHA256 `0bbee6fc6866804ad78e09c268478c75bc9ba2574f25eec0e23dd5870b87eb0e`）；
  实现必须遵循冻结签名。
  （manifest 的键集与 `manifest_version=1` 已在上文冻结，不再是待定项。）
- 独立设计 QA 为 CLOSABLE（0B/0I），契约冻结与 START 已完成，workflow 为 EXECUTING gen5。
- S1 收尾记录（2026-10-21）：
  - 真实初始 RED = 缺少 ledger API（失败原因是期望行为尚不存在，而非语法/导入噪音）；其后由 main
    独立复现 target sentinel 绕过 RED（同一 `run_id` 出现两条 `run_started`），最小修复后转为 GREEN，
    再由受信探针复验。
  - 专项测试 132 passed（早前一轮 124 passed 的记录以本轮为准）；完整门禁 3570 passed / 8 skipped /
    1 既有 warning，Ruff clean、475 formatted、mypy 132 源文件 clean、变更注释门 clean。
  - 独立具名实现 QA 结论 0B/0I（BLOCKER 与 IMPORTANT 皆为 0）。
  - 落地**尚未发生**：无 commit、无 PR、无 merge；分支 `codex/retention-instrument-s1`，issue #255 待以
    `Closes #255` 关闭，父 issue #206 保持 open。S2–S7 未启动。
  - 保留的失败/UNKNOWN 事实：初版 writer 与 QA 判定为 UNKNOWN、失败 QA、基于已撤回规则得出的伪缺陷结论、
    首次完整门禁的 Windows UTF-8 输出解码失败（9 项；PowerShell 7 + UTF-8 下该组 40 passed）与注释门失败，
    均按原样保留，不作成功重分类。
- 真实日历采集、证据独立复核、30d pre-registration ratify、产品集成一致性、activation 均为后续独立阶段，
  P2 完成只表示仪器行为、门与落地被验证。

## 14. S3/S7 边界注记（依已接受契约与架构裁定）

1. **载荷证据边界**：通用「每事件携带该类型完整证据」是 **S7（写者）义务**；S1 只强制冻结的静态列与
   载荷形状（含 `instrument_gap` 的 `product_effect_may_exist`、失败阶段与错误摘要等必填项）。
   证据缺失时 S7 写 `outcome='failed_read'` + `binding_incomplete` 的不可采行，缺失证据**不产生日历学分**。
   因此 ledger **不是**完整证据校验器，冻结契约下也不存在覆盖通用载荷完整性的机械校验器。
2. **S3 返回注册前值，S7 最终绑定**：S3 的 `FixtureHandle.cohort` 身份完整（`cohort_id`、`kind`、
   `cohort_created_at_raw`、`seed_manifest_hash`、`schema_hash`、`instrument_hash`），但
   `source_commit`/`source_hash`/`config_json`/`config_hash` 与链接 `control_id` **未绑定**，且以显式的
   unbound 表示（不得以空串静默注册成永久不可修复的行）。S7 在任何注册之前只最终确定
   `source_commit`、`source_hash`、`config_json`、`config_hash`、`control_id`，取值来自 CLI 供给值与
   既有 S1 的 build/verify 辅助函数；**绝不**改动 cohort 身份输入与已持久化的行。首次 ledger 写入之前
   必须校验绑定完整、且 primary 与 control 的 `schema_hash`/`instrument_hash`/`source_commit`/`source_hash`
   相等；遗漏绑定即 fail-closed。无新参数、无隐藏 env 传输、无运行时文件核验、无 schema 变更。
3. **播种与注册的分工**：S3 只播种真实隔离产品库（daily 对与 control 对），不写 ledger、不写 manifest、
   不做 CLI init，也不断言 S4 的 repair 结果。S7 先最终确定 primary 与 linked-control 两条记录，
   再由 S1 注册两行并**最后**写 manifest，全程在 `FreshInit` 的单写者锁与新鲜度检查之下。S3 测试只断言
   fixture 事实：拓扑、105 pending、零学分身份事实（`clock_source='seed_fixture'`、`calendar_eligible=0`）、
   seed manifest hash 与身份派生关系，不断言 ledger 行、manifest 或 CLI init。当前 `source_hash` 仍是
   **声明摘要**（不证明磁盘文件与基线一致）。真实 cohort 的 `cohort_created_at` 取宿主时钟，
   fixture 时钟不得回填日历证据。

- **列归属修正**：`fixture_seed_clock_raw` 按 P1 §4 保留给 control 行；daily 行保持 `None`，
  seed 时间戳可留在既有 fixture seed 证据中，不借用该列。
- **落地措辞澄清**：架构裁定中出现的「landed S1」是源码检视措辞，不表示 S1 已落地；
  S1 尚未提交、未开 PR、未合并。

## 15. S7 终段机制与合并状态记录（append-only，时点快照）

> 本节只做 S7 终段的机制与范围记录，不改变 §1–§14 的任何一条。理论锚仍是 §12 的
> 「不借用」：不新增任何神经科学/心理学借用，无新参数、新默认值、新诊断码、新门、新 CLI 面。
> 本节为文档快照；**S7 自身的最终完整门禁、独立实现 QA 与合并均仍为 PENDING**，
> 完成真相以 #264 落地 PR 与最终归档记录为准，本节不声明 S7 自身的 PR 号、commit、
> merge 或门禁结果，也不引用任何尚未发生的自指哈希。

### 15.1 实际 S1–S6 合并引用（来自提供的实际证据）

上文头部状态行与 §13 中「S1 尚未落地」等叙述是各自时点的快照，按 append 纪律原样保留；
其后 S1–S6 已按 issue → branch → PR → merge 逐片落地（证据 `s7-pre-doc-actual-evidence.json`：
六片 `MERGED`、各自五项 CI 全绿）：

| 片 | issue | PR | merge commit |
|---|---|---|---|
| S1 | #255 | #258 | `52a1fc1fdaab8d16c0b91423a28c837d7ef7fd6f` |
| S2 | #256 | #262 | `e77bb86c97738f2caa873544906d4fa33fac7b40` |
| S3 | #257 | #263 | `20e734bcb0dca22bcbae4b21191b8fa83e9103cc` |
| S4 | #259 | #269 | `65d872ee7f8c5dbbb47bdaef6d4ce5420c09b347` |
| S5 | #260 | #266 | `604be63942bfcd41559de47d1d2a65111e888c17` |
| S6 | #261 | #265 | `406aac4fc0aec57eea6e4c1f2e72439185d774f1` |

S7 的实现面（`cli.py` 与 `tests/instrument/test_cli_daily_run.py`、
`test_cli_restart_and_gap.py`、`test_cli_control_repair.py`）在隔离当前代码与冻结测试上
记录了六项实际预文档检查（comments/focused/ruff/format/mypy/probe，exitCode 全 0）；
该预文档阶段**不是**最终门禁、不是独立 QA、不是提交/合并验收。S7 终段的全量门禁
（注释门 + pytest + ruff check + ruff format --check + mypy src）、具名独立对抗式 QA
（0 BLOCKER 且 0 IMPORTANT）、`Closes` 式落地 PR、合并后 worktree/分支清理，均为
待办的后续动作。

### 15.2 已接受的机制裁定：仅写方 DELETE 规范化（D2）与严格只读适配器（D1）

- **裁定集**：`s7-revised-ro-binding.md`（D1：collect/summary 的严格只读适配器可行，
  首裁中的「逻辑 no-op bootstrap」回退**已撤回**；collect 与 summary 对产品存储均为
  物理严格只读）、`s7-final-wal-ruling.md`（D2 作为规范机制偏差**显式记录**而非隐式默许）、
  `s7-final-summary-ruling.md`（裁定与既有条款**兼容**：ledger 权威 summary 可采纳，
  竞争被消除而非豁免）。
- **D1（§13 机制偏差，已记录）**：`cli.py` 内私有适配器把产品驱动绑定到显式绝对路径
  与 S4 冻结的 `readonly_connection`（`mode=ro` URI + `PRAGMA query_only=ON`），客户端
  逐连接补 `row_factory=sqlite3.Row` 与 `busy_timeout`；不调用父类构造器、不迁移、
  不 `mkdir`、不建文件，产品写路径结构上不可达（继承写路径与迁移、写版 journal pragma
  均不可执行）。读取一律经**继承的真实产品方法**；写企图返 `sqlite3.OperationalError`。
  这是「导入不改产品」的偏离点（绑定不经构造器），**按记录呈现、不静默重释**；
  该适配器为单线程语义（CLI 本身单线程）。
- **D2（§11/§13 规范机制偏差，已记录；字节级而非值级）**：在**已获写授权**的
  `init`/`control-repair` 路径内执行一次直接写 pragma `PRAGMA journal_mode=DELETE`。
  所写的是**文件头 journal-mode 字节**，可能把既有 WAL checkpoint 进主文件字节；
  但**任何行的值、schema 对象、`sqlite_master`/`table_info` 哈希、
  `schema_version`/`seed_manifest_hash` 等逻辑证据全保留不变**。只读 SQL 证据面
  （S4 `sql_readonly.py`）保持零写；记录路径（P1 §5「记录永不触发产品改写」）不在
  pragma 之内。
- **规范化次序与失败语义**：`_normalize_to_rollback_journal` 先做一次状态探测 open，
  再用转换 open 执行 pragma，验证返回 mode 恰为 `delete`、随后校验头 18/19 字节
  恰 `\x01\x01` 且连接关闭后无 `-wal`/`-shm` 兄弟文件；任一步失败即类型化
  （`store_not_normalized` / `store_normalization_unverified` / `binding_incomplete`），
  存储（含 WAL 存储）**保持原样、不删除任何产物、不回滚已提交的效果**、绝不静默空读。
  `init` 内规范化先于任何 ledger 写：顺序为 取锁 → 锁内新鲜检查 → 真实播种 →
  **显式关闭全部 seeded 驱动**（显式关闭是"database is locked"的硬前提）→ 规范化
  （daily home 4 库；meta.db 虽不被 collect 读取，亦为 home 一致而同样规范化）→
  下面 15.4 的五字段最终化 → `create_ledger` → register → **manifest 最后写**；
  规范化失败 ⇒ 无 ledger 行、无 manifest、零删除，后续 `init` 由既有新鲜拒绝拒不补写，
  `collect` 如实 `ledger_missing` fail-closed。`control-repair` 内：真实 repair 通过与
  逐行 ACK 证据之后**关闭 control 驱动**，仅规范化 control 对；失败即 exit 1，
  已提交的 repair 效果永不回滚、永不重放，存储保持 WAL 且下一次只读打开如实失败。
- **精确 pre-open 拒绝（先于任何 SQLite open）**：RO 池每次连接前对存储做纯文件字节
  检查 `store_file_missing`（缺文件即类型失败，**绝不**触发父类构造器的静默建空库）、
  `store_not_sqlite`（魔法串）、`store_journal_not_rollback`（头 18/19 必须恰 `\x01\x01`
  — 回退 journal，非 1/1 即 `binding_incomplete` 且 header_pair 以 hex 显示）、
  `store_sidecar_present`（存在任何 `-wal`/`-shm` 兄弟即拒绝）、头不可读/截短亦类型化。
  从不 `immutable=1`、从不在 open 之后回退改判、collect/summary **永不**规范化、
  **永不**打开产品驱动、**永不重进 WAL**。
- **as-is 边界（诚实声明）**：`summary` 按冻结契约不取 WriterLock，故并发
  `control-repair` 理论上可在 summary 运行期间对 control 对重进 WAL——按原样记录，
  daily 对从不被 repair 触碰；不新增补救参数。

### 15.3 已接受的写方重试裁定（control-repair）

已接受裁定（id `retention-p2-s7-control-retry-plan-20261023`，`ALLOW EXPLICIT
WRITER RETRY`）：**单独再次请求**的 `control-repair` 使用真实可写驱动，即使先前一次
规范化失败遗留 WAL，也允许重新规范化；该重试必须观测 **0 pending、零 receipt 效应
重放、`delivered_at` 戳逐字稳定**；RO 适配器开侧的严格 pre-open guard **保持不变**；
不引入自动补完、不新增任何命令或参数。

### 15.4 当前 CLI 实态：init / collect / summary 机制

- **init（§14.2/§14.3 的实现了结）**：`FreshInit` 单写者 OS 锁全程，锁内新鲜检查
  每 context 恰一次；来源清单为调用方声明文件（字典形字符串→字符串），`source_hash`
  仍只来自声明清单（冻结 7 路径集 + 64 位小写十六进制形状检查），仪器不声称已核验
  磁盘产品文件。真实播种后**关闭 seeded 驱动 → DELETE 规范化 → 恰五个预注册字段
  最终化**（`control_id`、`source_commit`、`source_hash`、`config_json`、`config_hash`；
  `cohort` 身份输入与已持久化值不动；home B 仅 control 记录）→ `create_ledger` →
  register（primary 与链接 control 两行）→ **manifest 最后写** → 输出冻结报告字段。
- **collect（一次真实观测 run）**：WriterLock 全程；锁不可得 ⇒ ledger 零新增行 +
  ledger 外**独占创建**一次性调用回执（不覆盖、零学分）+ exit 1。run 顺序固定：
  `run_started`（携带 schema/instrument/source 绑定与重启链接 `restart_of_run_id`/
  `prior_run_unclosed`）→ 对每个**未恢复的中断 attempt** **恰一次** recovery attempt
  （`observation_kind='recovery'`、`recovers_attempt_id`、`calendar_eligible=0`、
  真实时钟区间的受限再读；**无任何 repair 效果重放、无日历晋升**）→ 一次主 collect
  attempt。观测是真实时钟括区间（**PRE `capture_pre` 即 `collected_at_raw` 唯一权威
  时点**）；失效则 `failed_read`/`clock_invalid`（主诊断 = 检查首码），失败终端行如实
  携带原始 raw 时点；`utc_date` 在追加时一次性派生存储，之后不可变；
  `calendar_eligible=1` 仅当 `observed_ok` 且属 `daily_cohort`。历史 floor 复用 S5
  `_advanced_floor`：仅 `observation_kind='collect'` 且非 seed 时钟的行（含失败终端行
  与 collect 种 `instrument_gap` 行的原始时点）推进 floor；`attempt_started` 不参与、
  recovery/replay_check 行**不推进**；S7 追加行写入前无任何隐藏时钟读与默认回退。
  `run_closed` 仅当 S1 唯一可闭性谓词通过，否则 `binding_incomplete`（`run_not_closable`）
  原样退出——存在 gap 的 run **不闭合、不产出 run_closed、不补记、不重放**。
  collect 报告（stdout 冻结 canonical JSON）含 `run_id`/`attempt_id`/`event_id`/
  `ledger_seq`/`outcome`/`utc_date`/完整分桶投影文档/`diag_codes`/`trust_codes`。
- **collect 读取面（严格 RO）**：四空库两对逐个 pre-open guard 后，经 S4
  `readonly_connection` 打开 cohort 对并配 RO 适配器：未决 receipts 逐行成
  `ProjectionInput`（`nomination_id`/`disposition`/`reason_code`/`applied_at_raw`
  **持久化原文逐字**，不重序列化）→ S4 逐字段 receipt 交叉核对（任一不符 ⇒
  `receipt_crosscheck_mismatch` 阻塞，绝不静默跳过）→ profiles/nodes/version 链经
  继承真实方法分页读取；`node_versions` 零新增 + 逐行哈希 + `sqlite_master`/
  `table_info`/完整当前节点内容哈希前后夹逼（`assert_node_integrity` 阻塞任何漂移；
  第二只读出口 `versions(node_id)` 交叉核对）；读取到的快照同样进入节点完整性括区。
  SQL 文本 sha256 进 provenance（query hash 表 + 每模板按占位符数渲染后哈希）。
- **summary（ledger 权威、as-of 最新完整观测）**：**只开 ledger**（ro URI +
  `query_only=ON`；存在性 ⇒ 否则 `ledger_missing`；`user_version==SCHEMA_VERSION`、
  `integrity_check=='ok'`、`GUARD_TRIGGERS` 触发器齐全，否则 `ledger_corrupt`）与
  manifest 连续性（`check_cohort_continuity`，缺/错配 fail-closed）；请求
  `cohort_id` 与行身份不符即拒。**最新 `run_closed` 的 run 即最新完整观测**（S1
  唯一可闭性谓词 ⇒ 其全部 attempt 均 `observed_ok`、无 gap；闭 run 无终端行、非
  `observed_ok`、缺 `clock_source` 均为 `binding_incomplete` 显式失败——失败/gapped
  run 永不具投影权威，其证据经 `instrument_gap` 行与既有数量如实可见）。对该观测
  terminal 载荷中**逐字记录**的原始 `ProjectionInput` 列表 + 该观测捕捉的 PRE（作为
  `now_raw`，不重读 OS 时钟、不读产品库、无隐藏时钟）重算 S6 `project`；
  `--cutoff-seconds` 每次显式（A6.2 字面谓词，非粘滞，可对同一冻结输入重复重投影）。
  **null 与零严格分离**：从未有完整观测 ⇒ `projection` 组为 **null**、观测日期
  为 null（**不得**以全零冒充「空投影成功」），numerator/shadowed/revoked/gap 数量
  照报并显式标注 as-of；有观测但该观测无幸存 admitted 行 ⇒ 各组计 0——两者可分辨。
  terminal 载荷缺 `projection_inputs` 或其条目形状不符（键集不恰为四键、含非字符串值）
  ⇒ `projection_inputs_missing` / `projection_inputs_malformed` 显式失败 exit 1，
  **错误负载绝不渲染为成功汇总**。summary 还报告首个/末个 admitted UTC 日期、
  `date_difference_days`、`elapsed_seconds`（首末真实捕捉差，描述性）、
  `calendar_gap_days`（admitted 跨度内无 admitted 持有者的 UTC 日期数，描述性——
  **不是**任何再推导阈值或门，keep 显示位）。

### 15.5 receipt 只读证据面：精确 22 列名 schema/类型映射（源绑定）

需求列集为源绑定 22 个列名：public 21 字段（`ReconciliationReceipt`
dataclass 21 字段）**加**内部 `input_hash`（**仅存在性验证，永不进入 public 比较**）。
列声明类型逐列绑定于迁移 DDL / `_RECEIPT_COLUMNS` 源检视：

| 列名 | SQLite 声明类型 | public Python 类型 |
|---|---|---|
| `nomination_id` | TEXT | str |
| `profile_id` | TEXT | str |
| `disposition` | TEXT | str（枚举级 str） |
| `reason_code` | TEXT | str（枚举级 str） |
| `canonical_kind` | TEXT | str |
| `composite_group_id` | TEXT | str |
| `evidence_event_ids` | TEXT | tuple[int, ...] |
| `verdict` | TEXT | str |
| `quality` | TEXT | str |
| `left_node_id` | TEXT | str |
| `left_version` | INTEGER | int |
| `left_expected_peer_id` | TEXT | str |
| `right_node_id` | TEXT | str |
| `right_version` | INTEGER | int |
| `right_expected_peer_id` | TEXT | str |
| `winner_node_id` | TEXT | str \| None |
| `loser_node_id` | TEXT | str \| None |
| `loser_prior_version` | INTEGER | int \| None |
| `loser_new_version` | INTEGER | int \| None |
| `downweight_factor` | REAL | float \| None |
| `applied_at` | TEXT | float（由同一持久化文本解码比较） |
| `input_hash`（内部） | TEXT | —（仅存在性） |

- `evidence_event_ids` 的中立 JSON 词表在 SQLite 渲染为 **TEXT**（`PRAGMA table_info`
  实现观察的类型串，而非迁移字面 `JSON`）；比较以 `json.loads(row_text) ==
  list(receipt.evidence_event_ids)` 完成。
- **集合/类型式（与物理列序无关）**：DDL 实际列序（含 `input_hash` 中位插入）与
  public 比较序不同；`verify_receipt_schema` 以 `PRAGMA table_info` 的列名集合与每列
  类型串对照上述映射——缺列 / 多列 / 类型不符 ⇒ `binding_incomplete`（detail 列出
  `missing_columns`/`unexpected_columns`/`mistyped_columns`）。public 21 字段的比较
  顺序由冻结 SELECT 列表给出。
- **类型化 SQLite receipt-read 边界**：两侧均来自**同一持久化行**（SQL 文本读出的
  原始值 vs 同行产品 API 解码），可空字段 **None 语义精确**：两侧皆 `None` ⇒ 相等，
  一侧 `None` ⇒ 不等，否则各自类型逐字/精确比较（不做 `str(None)`、不折 `''` 或 0；
  数值比较拒绝 `bool`）；`applied_at` 以同一持久化文本两侧 `epoch_from_iso` 后精确
  浮点相等。产品读取路径的任何 `sqlite3.Error` 与不可解码 receipt 内容
  （`ValueError`/`TypeError`/`OverflowError`）转 `binding_incomplete`
  （`product_receipt_query_error` / `product_receipt_decode_error`）；行缺失 /
  receipt 缺失进 `mismatch_fields`（`row_missing` / `receipt_missing`）。错误既不
  逸出为未捕获异常，也绝不产出 `observed_ok` 终态或成功交叉核对。

### 15.6 范围与状态声明（不作扩圈）

- **P2 全程保持隔离合成工程**：无真实采集、无 30 真实日历日、无有效观测日计数或
  日历学分声明、无 provider 质量实验、无 owner 侧 ratify、无 activation、不触产品
  面与安装 runtime；不启动 7788、不启动 daemon/consumer。
- 本节未新增源码、测试、CLI 参数、manifest 键或 schema 变更；§10 的 13 条验收断言、
  §9 的切片表与测试文件清单、§12 的 not-borrowed 清单按 P1 冻结语义不变。
  S7 后续状态变化只经 #264 落地 PR 与最终归档记录，不在本文档回填。

### 15.7 只读连接关闭的补充验证

只读池在配置连接前把句柄登记至继承的 `_open` 集合，使 `close_all()` 确定关闭实际连接；新增关闭后 SQL 操作被拒绝的回归用例。此项修复不改变采集、日历计数、理论锚或任何参数。本轮门禁、独立复审及落地状态以当前 PR 与归档为准，既有失败与否决记录保留。
