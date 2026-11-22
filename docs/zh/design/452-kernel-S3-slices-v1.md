# 452 S3 工作分片（slices） - 有界输入 + 显式失败的冷层新设计门（v1 / PROPOSAL）

> 状态：**PROPOSAL，DESIGN ONLY，无门权**。配套设计文档：`452-kernel-S3-refinement-redesign-v1.md`。
> **本 slices 文档本身不改变产品行为**；下列 U1-U6 是「设计冻结 + owner start 授权之后」才可执行的工作单元。
> 硬约束沿用 `design-batch-brief.md:7-12,84-93`：无工程派发、无产品源码/配置编辑、无 git 操作、无端口、无 `~/.mnemoseed-local` 写入；中文文档 + 英文标识符；每个行为声明绑定 file:line 或 `[未验证]`。
> 三片文件面互斥：S0 只写 450-*，S1 只写 451-*，S3 只写 452-*（`design-batch-brief.md:91-93`）。设计 QA 返回 `CLOSABLE` 后才冻结。

---

## 1. 总览与依赖图

| 单元 | 名称 | 依赖 | 产品行为变化 | 门 |
|---|---|---|---|---|
| U1 | 有界输入 packer 契约 + C2 不变量测试 | 无 | 无（纯函数/测试） | QG-1 |
| U2 | 显式 `extraction_failure` 信号 + append-only 保证 | U1（共享工作项/失败分类词汇） | 无（信号默认不写；表结构 additive） | QG-2 |
| U3 | Phase 1 确定性提取 + 接地校验 + >50% fail-loud | U1, U2 | 无（离线、默认关闭） | QG-3 接地/接地率 |
| U4 | 互补性判据可测化（五项协议） | S0 仪器；U2, U3 | 无（测量协议） | QG-3 召回贡献 |
| U5 | 诚实边界文档 + 触发默认关闭 | U4（结论输入） | 无（仅文档） | QG-4 / OD-2 |
| U6 | 可选 Phase 2 有界摘要（默认关闭） | U1-U4 + **OD-1 激活授权** | 潜在（仅激活后） | QG-5 / OD-1 |

依赖图（箭头 = 依赖）：

```
U1 ──> U2 ──> U3 ──> U4 ──> U5
 │       │       │       │
 └───────┴───────┴───────┘──> U6（需 OD-1）
S0 仪器 ─────────────────────> U4
```

> 合并顺序：U1 → U2 → U3 → U4 → U5 可依次独立合并；**U6 不得在 OD-1 授权前合并**。
> KISS 守卫：U1-U5 全部为 additive/离线/默认关闭；不触碰 capture/recall/inject 热路径，不重启 score_pool，不改 `auto_trigger`。

---

## 2. 工作单元明细

### U1 - 有界输入 packer 契约 + C2 不变量测试

- **目标**：把设计文档 §1.1 C1/C2 固化为可测的确定性 packer：per-item 工作项、硬上限 B、单项超限按语义边界拆子项并回指源 chunk；不切文中（`delta.py:208-237,255-260`）。
- **工作项**：
  1. 定义 `BoundedItem` / `BoundedPack` 数据结构（含 `source_chunk_id`、`token_estimate`、`subitem_of` 回指）。
  2. 实现 packer：渲染 token 复用 `estimate_tokens`（`02:175`）；超限走语义边界拆分；拆不动的原子项进 `parked_with_signal`（此时仅占位，U2 接信号）。
  3. 不变量测试：`consumed ∪ parked_with_signal = all_items` 且两集合不相交；测试须包含「单项 > B」「B=边界值」「空输入」三族；构造一个「旧 `pack` 裁成空 delta」的变异体必须被测试杀死（防 D3）。
- **文件 allowlist（未来实现）**：新文件 `src/mnemoseed_local/refine/bounded.py`（或既有 `dream/` 下的新模块）+ 对应 `tests/` 新测试文件。**不改** `config.py`、`daemon/app.py`、`delta.py` 既有行为；B 为模块内常量并标注 `[未验证]` 待标定。
- **依赖**：无。
- **验收门**：单测全绿 + 变异体杀死证据；不变量对三族输入成立；无产品热路径改动。
- **Owner 决策**：无（B 的最终取值可在 U4 后由 OD-3 一并确认）。
### U2 - 显式 `extraction_failure` 信号 + append-only 保证

- **目标**：把设计 §8.5 落地：新增保留枚举成员 `ErrorSignalType.EXTRACTION_FAILURE = "extraction_failure"`，0 事实 / schema 不合规 / 停车项都写一条 append-only `error_events`，**空结果永不记成功**（Graphiti 先例 `competitor:122,186`；现枚举 `ports.py:517-532`）。
- **工作项**：
  1. `storage/ports.py` 的 `ErrorSignalType` **追加**成员（不复用 `provider_failure`，不改既有值）：**仅 Python 枚举追加，不新增 DB 迁移** - `error_events.signal_type` 为 `TEXT NOT NULL`、无 DB 级 CHECK 约束（`_migrations.py:525`），追加枚举成员无需 schema 变更；BEFORE UPDATE/DELETE 触发器不动（`_migrations.py:544-562`）。**迁移版本号由 S1 集中持有在 v18**（`451-main:51`；`451-slices:28`）；S3 不触碰任何迁移号，避免与 S1 争用下一个 `_migrations.py` 版本。
  2. 定义失败分类常量（如 `empty_verdict` / `schema_noncompliance` / `oversize_parked`）与 `detector_id`、`retryable` 的取值；`evidence_ptr` 只引用源（chunk/session），不判正确性（`ports.py:550-558`）。
  3. 写入路径默认关闭（无调用方时表保持空，Born-empty 先例 `_migrations.py:516-518`）；测试覆盖：写一条、查询可查、UPDATE/DELETE 被拒（触发器）。
  4. 变异体测试：把「0 事实」写成「成功 + 0 产出」的变异体必须被杀死（防 D2）。
- **文件 allowlist（未来实现）**：`storage/ports.py`（仅追加枚举成员）、新测试文件。**不新增迁移文件、不追加任何 `_migrations.py` 版本行**（`signal_type` 无 DB CHECK，`_migrations.py:525`；迁移号由 S1 持有 v18）。**不改** `auto_trigger` / `config.py` 默认值。
- **依赖**：U1（共享 `BoundedItem` 的失败分类词汇）。
- **验收门**：枚举封闭性测试（新成员可解析、旧成员不变）；append-only 触发器测试；「0 产出却记成功」变异体被杀；无配置写入。
- **Owner 决策**：无（schema append-only 变更属 QG-2 技术门）。
- **跨片协调点（已更正）**：核查 S1（451-*）确认 - S1 **不新增任何 `ErrorSignalType` 成员**，而是在 meta store 新建独立的 `consumption_events` 表（migration v18，`451-main:25,51,56-79`），其 `event_kind` 为自有封闭集 `served|injected|consumed_attested|consumed_explicit`（`451-main:61`），**零 `ErrorSignalType` / `signal_type` 引用**（QA E4）。因此两片**不存在成员名碰撞**，无需请 main 核对碰撞；本批**仅 S3 追加** `ErrorSignalType.EXTRACTION_FAILURE = "extraction_failure"`（`ports.py:517-532`）。唯一跨片共享面是迁移版本号：S1 持 v18，S3 不新增迁移、不触碰迁移号（见工作项 1）。

### U3 - Phase 1 确定性提取 + 接地校验 + >50% fail-loud

- **目标**：实现设计 §8.3/§8.4：确定性零 LLM 提取（命令/退出码、文件路径、错误→修复对、显式决策、首次出现的工具/约束，`11:35-43`），事实带 `{session_id, chunk_ids, kind, payload}`（`11:43`）；接地校验按空白折叠 + 单 chunk 内子串匹配、片段 ≥16 字、丢弃率 >50% fail-loud（`11:49`）；key-supersede 版本链（`11:54`），不内嵌正文（`11:55`）。
- **工作项**：
  1. 从工作项结构化字段抽取事实（确定性，无模型调用）；每条带 `source_session_id` + chunk id。
  2. 接地校验器：输入为「事实的 verbatim 源片段 + 目标 chunk 文本」，按规则匹配；不匹配丢弃并调用 U2 信号写入接口。
  3. 丢弃率统计：运行级累计；>50% 触发 fail-loud（错误日志 + U2 信号），不得静默全丢或全收。
  4. key-supersede：`(subject,predicate)` 同键新证降级旧条为历史；不复用 design/12 提名管线（`11:54`）。
  5. 快照上限 1MB、只存引用（`11:55,60`）。
- **文件 allowlist（未来实现）**：新模块（如 `refine/deterministic.py` / `refine/grounding.py`）+ 新测试；如需写图，仅新增写路径，默认关闭。**不改** capture/recall/inject。
- **依赖**：U1, U2。
- **验收门**：零 LLM 调用证明（无网络/无模型客户端依赖）；接地规则单测（含跨 chunk 拼接必须失败）；>50% fail-loud 变异体测试；不内嵌正文断言；supersede 链测试。
- **Owner 决策**：无。
### U4 - 互补性判据可测化（五项协议）

- **目标**：把设计 §7.2 的「互补而非重复」五项判据变成可执行的测量协议（不激活产品行为）：接地、非重复（≥2 chunk 聚合或版本跃迁）、有用（≥20 窗口人工评审 ≥60%）、接地率（≤50%）、召回贡献（S0 冻结真实查询集上的第二轨增益，`design-batch-brief.md:28-44`）。
- **工作项**：
  1. 定义互补性评分数据结构与判定脚本（离线、对副本数据跑）。
  2. 复用 S0 的冻结真实自然查询集作为「召回贡献」仪器（**依赖 S0 冻结**；不复用 #391 synthetic rig，`08-eval-harness.md:24`；INV-1 量纲错位 `first-principles-kernel-gap.md:14`）。
  3. 明确「非重复」的可操作定义：事实须引用 ≥2 个不同 chunk id 或表达一次 supersede 跃迁；单 chunk 复述判失败。
  4. 产出一份「互补性判定报告模板」（字段 + 判定阈值 + `[未验证]` 标注位）。
- **文件 allowlist（未来实现）**：新增离线脚本/评估配置（如 `scripts/refine/complementarity.py`）与报告模板文档；**不改**产品运行时；读取真实语料副本时遵守隔离纪律（spare port + temp home，`AGENTS.md:76-79`）。
- **依赖**：S0 仪器冻结；U2, U3。
- **验收门**：五项判据各有可复现测量步骤；#391 rig 零引用声明；量纲与 S0 契约一致（recall-of-known-facts，非 recall@5/precision@5）。
- **Owner 决策**：OD-3（阈值是否采纳为冻结判据）。

### U5 - 诚实边界文档 + 触发默认关闭

- **目标**：把设计 §3/§7.3 落成文档，并确认所有门默认 OFF：不把 `auto_trigger` 设 true，不改 `config.py:991`；如实把 CLS 双存储锚点标为 as-is boundary（若 owner 选「不激活」），排除半死静默态（Seat B:111,183,212）。
- **工作项**：
  1. 在 `docs/zh/design/` 新增一篇 as-is boundary 说明（或在本设计文档追加状态段），明确「当前 shipped = verbatim-only + explicit-pin；冷层为设计提案，未激活」。
  2. 核对默认开关：`auto_trigger=false`（audit 505 / 7000005，`inv3:64-66`）保持；U1-U4 产物无自动触发接线。
  3. 给出 OD-2 的两条路径对照（复活 vs 单轨改述）的文档落点。
- **文件 allowlist（未来实现）**：仅 `docs/zh/design/` 新文档 + 本设计文档的状态更新。**不改**任何 `src/` 或配置。
- **依赖**：U4（互补性结论作为文档输入）。
- **验收门**：grep 确认无 `auto_trigger` 写入；as-is boundary 表述与 Seat B D4/b 一致；无「dual-store 已 shipped」的残留宣称。
- **Owner 决策**：OD-2（冷层命运）。

### U6 - 可选 Phase 2 有界摘要（默认关闭）

- **目标**：设计 §6「改变」项与 §9 图中 U3 之后的可选摘要层。输入 = Phase 1 事实清单 + 会话首尾摘录（非原文全文），按 B 切分；输出严格 schema，每条 fact 带 verbatim 源片段（`11:45-50`）。
- **工作项**：
  1. 摘要调用器（有界输入、调用数由工作项数决定、无「≤4」天花板）；模型走本地 dream 路由（`11:47`）。
  2. 复用 U3 接地校验与 U2 失败信号；超预算整条丢弃。
  3. **默认关闭开关**：无 OD-1 授权时该层不存在任何调用点。
- **文件 allowlist（未来实现）**：新模块（如 `refine/summary.py`）+ 新测试；新增开关默认 false。
- **依赖**：U1-U4 + **OD-1 激活授权**（硬门）。
- **验收门**：关闭状态下零 LLM 调用；开启后 token 预算有界可观测（QG-5）；失败信号路径被测试覆盖。
- **Owner 决策**：OD-1（激活问题）- **未授权前不得合并**。
---

## 3. 跨片契约与冲突检查（contract surface collisions）

| 顶面 | S3 的用法 | 潜在冲突方 | 处理 |
|---|---|---|---|
| `ErrorSignalType` 枚举（`ports.py:517-532`） | U2 仅 Python 枚举追加 `EXTRACTION_FAILURE = "extraction_failure"` | 无 - S1 不新增任何 `ErrorSignalType` 成员 | **无成员名碰撞**：S1 零 `ErrorSignalType` / `signal_type` 引用（`451-main:25,51,61`；QA E4）；本批仅 S3 追加该成员，无需 main 核对 |
| `error_events` 表（`_migrations.py:509-568`） | U2 写入失败行、Born-empty；不新增迁移（`signal_type` 无 DB CHECK，`_migrations.py:525`） | S1 新建独立 `consumption_events` 表（v18） | 两表独立、无同表冲突；迁移号由 S1 集中持有 v18，S3 不触碰迁移号 |
| 真实语料查询仪器 | U4 复用 S0 冻结集 | S0（450-*） | U4 依赖 S0 冻结；不复用 #391 synthetic rig（量纲错位，`first-principles-kernel-gap.md:14`） |
| 产品热路径（capture/recall/inject） | U1-U5 零改动 | 无 | 禁止触碰；U6 仅激活后考虑且以开关隔离 |
| `auto_trigger` / `config.py:991` | 全程不改 | #294 裁决 | audit 505 / 7000005 保持；OD-1 未决即零配置写入 |

---

## 4. 冻结门与合并顺序

1. 三片设计文档（450/451/452）全部交独立设计 QA；每片 `CLOSABLE` 后才可冻结（`design-batch-brief.md:95-97`；`org/PROTOCOL.md:176-191`）。
2. 冻结字节哈希登记后，本 slices 才进入 execution 讨论；**冻结 ≠ 工程派发**（`design-batch-brief.md:11`）。
3. 合并顺序：U1 → U2 → U3 → U4 → U5；U6 单独等待 OD-1。
4. 每个实现单元按 TDD：失败测试先行、对抗性自验、独立实现 QA；每 pushed commit 全绿（AGENTS.md:20-37）。
5. 若进入实现时范围/合同/验收门变化，暂停并回到设计修订（`org/PROTOCOL.md:188-189`）。

---

## 5. Owner 决策点汇总（本片不代答）

- **OD-1 激活问题**：U6 与「是否逆转 #294 `auto_trigger=false`（audit 505 / 7000005）」的前置。默认：**否，直到 owner 明确授权**（`design-batch-brief.md:68-70,82`）。
- **OD-2 冷层命运**：复活（新设计）vs 诚实单轨改述；禁止半死静默（Seat B:111,183,202）。落点在 U5。
- **OD-3 互补性阈值**：§7.2 的 `≥60%` / `≤50%` / 召回贡献阈值是否冻结（沿用 `11:49,62`）。落点在 U4。

---

## 6. 报告式收口（结论 - 假设 - 证据 - 时效 - 风险 - 置信度 - 建议）

- **结论**：**REDESIGN + NEW DESIGN GATE；ACTIVATION DEFERRED**。本片只交付设计门与分片，不改变产品行为，不重启 #294 旧路径。
- **假设**：`reflect_batch_max_tokens=8000` 装不下真实 chunk 的机制在 `delta.py:255-260` 与 `pipeline.py:429-436` 仍成立（逐字节读于本会话）；`#294` 的 `auto_trigger=false` 保持至今（`inv3:66,76`）。
- **证据**：全部绑定 file:line（见主设计文档 §13）；`[未验证]` 项：34k-token 现值、B 取值、8 snapshots 归档位置、未来激活收益。
- **信息时效**：INV-3 / 竞品 / Seat B / brief 均 2026-11-22（< 90 天）；11-refinement-v2 与 #294 为 2026-10-28（约 25 天）；理论经典 > 90 天但状态照抄 `REFERENCES.md`、不升级（理论锚固定）。
- **风险与反对意见**：互补性阈值未实测（中置信）；若 S0 仪器未冻结，U4 会卡住；若 owner 选不激活，U1-U4 的产出仅为「可测契约」而非 shipped 收益（这是诚实代价，非缺陷）。
- **置信度**：**高**（分片边界、依赖、文件面、门均绑定设计文档与源码）；**中**（阈值与收益）。
- **建议**：请独立设计 QA 审 450/451/452 三片；`CLOSABLE` 后冻结并交 owner 就 OD-1/2/3 裁决；在此之前无任何工程派发、无产品字节改动。