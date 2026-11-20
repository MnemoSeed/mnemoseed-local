# PRD-309 · 公开 Alpha 执行计划（证据刷新版）

> 版本：`2026-11-19-309-alpha-refresh`。状态：**PLANNING REFRESH / NOT AN ENGINEERING AUTHORIZATION**。
> 基线：当前 `main` = `8ad2d6ace54c1fca467796cc6ddfcbf596318168`（2026-11-19 查询）。
> 席位：`product-manager`。本文件只做计划对账、门槛与决策点定义；不授权工程、发布、部署、对外联系或改动已安装运行时。
> 证据纪律：所有判断链接到下方证据；无证据标 `[未验证]`。本轮未跑产品测试，门槛数值为**待冻结的提案目标**，不是已测 SLA。

## English executive summary

Current `main` ships more than the 2026-10-30 plan assumed: the no-Ollama capture-recall setup
(#310, CLOSED) and expired-constraint recall down-ranking (#281, CLOSED) are in, and four history
readers (codex_cli, claude_code, opencode, zcode) are registered in code with PRs #416/#417/#418/#420 merged
on 2026-11-18. It also ships a new architecture source of truth (design 14). It does **not** ship a
usable public-alpha install path: PyPI returns 404, the package version was re-frozen to 0.1.0 after a
0.3.0 freeze, the console exposes raw config keys rather than the documented provider catalog (#381 OPEN),
`recent_sessions` still needs caller-known anchor syntax (#411 OPEN), and config reload-invalid writes are
not rejected (#385 OPEN). Recommendation: keep G1 (companion private trial, source-frozen artifact) as the
only alpha exit criterion; make G2 (self-service) contingent on four explicit owner decisions and gates
below. Eight customer-facing claims are marked SHIPPED / PARTIAL / UNVERIFIED / CONTRADICTED.

---

## 0. PM 结论（先读）

1. **Alpha 的出口不是"功能齐全"，而是 G1 的一段真实闭环**：真实 host 捕获 → 持久化 → 受控重启 → 新会话恢复 → MCP 实际调用与代理使用 → 人工确认有用。原计划 §3 的硬门保持不变，不能用文档、ACK、握手或模型复述替代。
2. **当前 main 的能力面比原计划假设更靠前**：#281 与 #310 已 CLOSED，四个历史 reader 已合并且已在代码中注册。原 r3 的"36 open 快照"已过时（2026-11-19 执行时 `gh issue list` 实测为 23 open）。这些变化缩短了部分工程面，但**没有**产生任何真实 host 有用性证据。
3. **自助 Alpha（G2）仍被四个客户可见缺口卡住**：PyPI 未发布（#179 OPEN，404 实测）、provider catalog 未实现（#381 OPEN）、`recent_sessions` 锚点语法（#411 OPEN）、配置重载无效写入未拦截（#385 OPEN）。加上跨 host 项目绑定 #311 OPEN，G2 不具备"无人值守可自助"的前提。
4. **设计 14 是工程架构事实，不是产品承诺**：其 §6 的 `mnemoseed-local setup` 一条命令流程在 CLI 中**不存在**（当前动词为 `init --mode capture-recall` + `doctor` + installers）；其 §2 的 host 矩阵把 zcode 回填标为 ❌，与今日已合并的 zcode reader 冲突。两者都需在 G0 文档门中处理。
5. **不要重问 D01–D06**：原 owner 已授权方向；本文只新增需要今日裁决的 D-A..D-F（§5）。

## 1. 证据基线与时效

| 来源 | 类型 | 发布/观察日期 | 访问日期 | 用途 |
|---|---|---|---|---|
| `docs/zh/design/14-product-architecture.md`（base `8a9a115`） | 设计文档 | 2026-11-17 owner 指令 | 2026-11-18 | 四层架构、host 矩阵、安装目标、非目标 |
| `README.md`、`docs/guide.md`（base `8a9a115`） | 客户文档 | 随 main | 2026-11-18 | 客户可见承诺、诚实边界 |
| `src/mnemoseed_local/cli.py:1832-1836` | 源码 | 随 main | 2026-11-18 | `IMPORT_SOURCES` 实际注册 4 个 reader |
| `src/mnemoseed_local/console/static/app.js:313-321,394-400` | 源码 | 随 main | 2026-11-18 | console 只暴露原始 config key，无 catalog/test-connection/search |
| `pyproject.toml:7,11,60,66`；`install.sh:77`；`install.ps1:107` | 源码 | 随 main | 2026-11-18 | 版本 0.1.0、`requires-python >=3.14`、installer pin 0.1.0 |
| `git log/show`：#370=`889e430`（冻结 0.3.0）；`1aa5774`（2026-11-10 重新冻结 0.1.0）；`68b2dcf`（py314） | git 历史 | 2026-11-07 / 11-10 | 2026-11-18 | 发布身份漂移 |
| GitHub issues/PRs #281/#310/#381/#385/#411/#416/#417/#418/#419/#420/#179/#179-引用#370 | 线上状态 | 见 §2 | 2026-11-18 | 关闭/合并状态 |
| `https://pypi.org/pypi/mnemoseed-local/json` | 在线查询 | — | 2026-11-18 | HTTP 404：包未发布 |
| `org/plans/mnemoseed-local-alpha-309-current.json` | 规划指针 | as_of 2026-10-30T05:09:12Z | 2026-11-18 | 原计划入口与 acceptance 哈希 |
| `…/resume-20261030/workspace/outputs/next-execution-plan.zh-CN.md`（r3） | 原计划 | 2026-10-30 | 2026-11-18 | G1/G2 配方、证据 schema、写面顺序 |
| `…/retrieval-correction-20261030/zcode-resume-with-retrieval.zh-CN.md` | 原计划增补 | 2026-10-30 | 2026-11-18 | 原始记忆链诊断与恢复成本义务 |

原规划验收哈希（用于核对入口，不在本轮重判）：r3 `eab15ed160d75a17be3f55c371216a71999e0368e7ac75498e177ae7df3543a5`；retrieval-v1 `f2835b038c40d5f3428919771e05aa767a08d74f13f757f048b9adb36e4395c8`。这些仍指向**规划接受**，不是工程/发布授权。

## 2. 当前 main 实际交付 vs 客户可见承诺（对账）

| 能力 | 代码/线上证据（2026-11-18） | 文档/客户承诺 | 裁定 |
|---|---|---|---|
| 本地优先、无账户、无遥测、loopback 凭据 | README/guide；design 14 §1/§4/§5 | "local memory for your coding agents"；无加密-at-rest 已如实披露 | **SHIPPED**（诚实边界到位） |
| 四层架构、单 profile 内核可移植、无 supervisor | design 14 §1/§3/§4 | 工程架构说明，非客户承诺 | **SHIPPED（工程事实）** |
| 无 Ollama 的 capture-recall 安装模式 | #310 CLOSED `2026-11-18T05:49:45Z`；PR #317/#318；`init --mode capture-recall` 在 CLI | README/guide 安装段、dream 默认关闭 | **SHIPPED（代码层）**；真实 host 成功仍属 G1，**[未验证]** |
| 过期时间约束降权 | #281 CLOSED `2026-11-18T05:49:41Z`；PR #332 "down-rank expired explicit-validity chunks" | guide "retention with cue-based rescue"；无 Recall@k 数字承诺 | **SHIPPED（代码层）**；"recall 更好"的量化主张 **[未验证]**，见 §6 M2 |
| 历史会话导入 | `cli.py:1832-1836` 注册 codex_cli/claude_code/opencode/zcode；PR #416/#417/#418/#420 MERGED（11-18），#419 CLOSED | README/guide 只写 `import --host codex_cli`；design 14 §2 仍标 zcode 回填 ❌ | **PARTIAL**：代码 4/4，文档 1/4，设计矩阵过期。三个新 reader 的真实数据正确性 **[未验证]** |
| 一条命令安装 / 包索引 | PyPI 404（今日实测）；#179 OPEN；#370 冻结 0.3.0 后 `1aa5774` 重新冻结 0.1.0；installers pin 0.1.0 | README/guide 已诚实写"source checkout only，index installers unavailable" | **CONTRADICTED**："one-command install" 不存在。文档已诚实，但客户目标未达成；G2 BLOCKER |
| console provider catalog（preset / test connection / searchable list） | console `app.js` 只有 `dream.llm.<role>.{driver,model,base_url,provider,...}` 原始 key；#381 OPEN | README/guide："bind a model through the console provider catalog — OpenAI-compatible preset (…), custom endpoint, Ollama" | **CONTRADICTED / PARTIAL**：catalog 能力未实现，措辞超出证据。G2 决策点 D-B |
| `recent_sessions` 易恢复 | main `daemon/memory.py:1370-1409`（commit `21e2a9e`）已有 bound-root 恢复；#411 OPEN（剩余：无绑定时给 actionable `unresolved` + 输入字段文档；PR #414 错链，不据此关闭） | guide 列出 `self_parent_id` / `caller_cwd` 参数 | **PARTIAL**：自助恢复语义未闭合。G2 决策点 D-C |
| host status 真实性 / 配置安全 | #408 OPEN（codex_cli 声称 trust unverified 但有 511 ingest）；#385 OPEN（reload-invalid 角色写入未拒绝） | guide："A config entry or successful MCP handshake alone does not prove capture" | **PARTIAL**：文档边界诚实，产品行为仍有缺口；G2 收口项 |
| 跨 host 同一项目绑定 | #311 OPEN | 计划 G2 单元 B311-BINDING/HOSTS | **NOT SHIPPED**；G2 BLOCKER |
| Python 3.14 | `requires-python >=3.14`、ruff/mypy `py314`、commit `68b2dcf` | design 14 §7 | **SHIPPED（工程事实）** |

> 在途项真实状态（2026-11-19 执行时 `gh issue list` / `gh pr view` 实测）：#385 OPEN，其修复 PR #432 OPEN / MERGEABLE（独立 QA 判词 CLOSABLE，合并待执行）；#440、#390、#441 均为 queue / OPEN，未关闭。本批 W1-D（#441 + G0 文档对齐）承接 design 14 §2/§6 的 G0-2 项。

## 3. 与原 #309 计划的差异（reconciliation）

| 原计划假设（r3 / retrieval-v1，2026-10-30） | 今日事实 | 对计划的影响 |
|---|---|---|
| 61 单元 / 36 open 快照 | 23 open（2026-11-19 执行时实测）；#281、#310 已 CLOSED | 从 G2 依赖中移除 B281/B310 的"关闭"义务，但**保留**真实 host 证据义务（G1 必须重新证明"当前事实"与"无 Ollama 模式"） |
| 停点 B301 现有候选待 QA、A292 文档收口 | 今日 issue 列表不含 B301/A292 的未完成标记；本轮不重判其终态 | 不宣称它们已完成；按原 admission 规则逐片核验，不因本计划刷新释放旧 unknown |
| G1 手工固定源码、不依赖 PyPI/v2/第二 host | 仍成立，且更紧迫：PyPI 404、版本回退 0.1.0 | G1 路线**不变**；R310 的 index 路线仍 `BLOCKED_OPERATOR` |
| R310 冻结版本（原计划"不猜版本号"） | 曾冻结 0.3.0，后被 owner 重新冻结 0.1.0；PyPI 仍 404 | 版本身份以 main 当前 0.1.0 为准；不得 retag 0.3.0，发布必须重新生成 manifest |
| design 14 尚不存在（原计划引用 design 10–13） | design 14 于 2026-11-17 落盘 | 架构源切到 design 14；其 §2 host 矩阵和 §6 setup 目标需在 G0 修订/注释 |
| 历史导入 reader 不在单元清单 | 4 个 reader 已合并 | 新增"reader 文档与证据"切片（G0），不新增工程实现切片 |
| #411 修复曾被误认为 PR #414 | issue 仍 OPEN，comment 澄清范围 | 保持 OPEN，仅按 comment 的两项剩余范围收口 |

## 4. 具体就绪门槛（Readiness gates）

门槛按顺序推进；任一硬门 FAIL/UNKNOWN 则该阶段不通过，**不得**用 help、握手、ACK 或模型复述补 PASS。

### G0 — 证据与文档完整性门（PM 主责，进入邀请前完成）

| 项 | 退出判据 | 证据 | Owner |
|---|---|---|---|
| G0-1 | README/guide 的 import 段列出 4 个 host 的真实命令与 `--dry-run` 用法；三个新 reader 各附至少一个真实或合成 fixture 的导入报告 | 文档 diff + fixture 报告 | PM + docs engineer |
| G0-2 | design 14 §2 host 矩阵把 zcode 回填更新为 ✅（或注明"reader 已合并、真实数据待证"）；§6 明确 `setup` 是设计目标、当前动词为 `init --mode capture-recall`（本批 W1-D 已承接：design 14 §2 zcode 行与 §6 setup 注记已更新） | 设计文档 diff | PM + solution-architect |
| G0-3 | 本文落主干，且 org `_state.md` 当前指针指向本文（不是旧 v1/不存在的 accepted-v1） | 合并 PR + `_state.md` | PM |
| G0-4 | §7 的 8 条客户承诺逐条裁定：CONTRADICTED 项要么改代码、要么改文档措辞，不允许保持"文档暗示已交付" | 对账表更新 | PM + owner（D-A/D-B） |

> **G0-4 后续对账（W1-D 登记）**：README.md:47-53 仍宣称"bind a model through the console provider catalog"，而本文 §2 catalog 行（第 59 行）把同一能力标为 CONTRADICTED / PARTIAL（#381 OPEN）。S5/S6（W1-B，#381）排在本批之后，故本批不改 README 的该声明；**待 W1-B/S6 合并后，对账 README.md:47-53 与本文 §2 catalog 行并关闭 G0-4**，不得静默遗留。

### G1 — 陪同私测（沿用原计划 §3，不降门）

- **入口**：固定完整源码 40-hex SHA、OS/真实 host 版本、checkout 外安装目录、隔离 `MNEMOSEED_LOCAL_HOME` 与 host 配置 root、显式 profile、spare port（不得 7788/4096）、owned PID 树。Windows + Claude Code 首选，Codex CLI 替代。
- **退出判据（逐项留证，见原计划 §3 的 `g1-evidence/` schema）**：
  1. 三项当前事实 + 一项真实未完成任务被恢复，来源引用正确且符合仓库当前状态；
  2. 真实 host 捕获 → 持久化可见 → 隔离 daemon 受控重启 → 新 host 会话经真实 T1/`recent_sessions` 恢复 → 实际 MCP `tools/call` → 代理用事实完成下一步；
  3. 零跨 profile 泄漏；无答案问题不编造；到期约束作为人工当前性诊断披露，不冒充 #281 自动 PASS；
  4. 至少 1 次人工独立确认"有帮助"，第二次使用未发生记 `UNKNOWN`；
  5. `stages.json` 中无 FAIL/UNKNOWN 被自动补成 PASS。
- **可测目标（提案，待工程 freeze 确认）**：见 §6 M1/M2/M3/M6。
- **容量**：3–5 位自愿开发者；联系/发布需另行授权（Tier 3）。

### G2 — 自助 Alpha（通知，不在本轮启动）

硬依赖（全部满足才具备"自助"前提）：

1. **R0 安装身份**：#179 有 owner 裁决 —— 要么发布 0.1.0 到新索引并在发布 commit 重新生成 manifest，要么明确保持 source-only 并把"一条命令"措辞从所有客户文档移除。index 路线未授权即 `BLOCKED_OPERATOR`，禁止静默 source fallback。
2. **#381**：provider catalog 能力交付 **或** README/guide 改为描述真实 raw config key（D-B）。
3. **#411**：无绑定时 `recent_sessions` 返回 actionable `unresolved` + 输入字段契约落文档（D-C）。
4. **#385 / #408**：reload-invalid 写入被拒；host status 不再把"配置了但无真实证据"说成 verified。
5. **#311**：跨 host 同项目绑定交付并完成真实双 host 证明。
6. **V305-TWO / V312-G2**：同一交付制品在两个真实 host 上完成双向接续、项目隔离、expiry 负例、卸载保留数据；G2 verifier 的 RED→GREEN、全门、独立 QA、PR/CI/merge/main 接受齐备。
7. **卸载**：默认 keep-data，purge 显式确认，partial 列出残余与下一步尾步骤，不把尾步骤未执行说成完成。

## 5. Owner 决策点（需 JH 裁决，均可 3 分钟答复）

| ID | 问题 | 选项 | PM 建议 | 影响 |
|---|---|---|---|---|
| D-A | 公开 Alpha 的安装路线 | (1) 发布 0.1.0 到 PyPI；(2) 保持 source-only 并改文档措辞 | **(2)**，index 保持 `BLOCKED_OPERATOR`；不 retag 0.3.0 | Tier 3（对外发布）；决定 G2 是否存在 |
| D-B | "provider catalog" 承诺 | (1) Alpha 前实现 #381；(2) 立即改文档为 raw config key，#381 排 Alpha 后 | **(2)**，消除 CONTRADICTED 项 | Tier 2；决定 README/guide 是否需改 |
| D-C | `recent_sessions` 自助恢复 | (1) Alpha 前做绑定 UX + 锚点语法隐藏；(2) doc-only 契约 + actionable `unresolved`，复用现有 `profile/project` CLI | **(2)** | Tier 2；决定 #411 是否 G2 阻断 |
| D-D | 私测邀请与对外口径 | (1) G1 通过后联系 3–5 人；(2) 等待 G2 | **(1) 但仅限私测**，公告/发布/outreach 另需 Tier 3 授权 | Tier 3（对外承诺） |
| D-E | 是否把 reader 批次纳入 G1 证据切片 | (1) 纳入（用隔离 home + 合成内容，尤其 zcode 是创始人记忆痛点案例）；(2) 不纳入 | **(1)**，但不作为 G1 硬门 | Tier 1；影响 G1 证据丰富度 |
| D-F | t3code / 其它 harness 是否进 Alpha | (1) 进；(2) 不进 | **(2)**（design 14 §9.6 单列规划） | Tier 2；护 scope |

## 6. 可测结果（Measurable outcomes）

数值均为**提案目标**，需在对应片区的 freeze 中钉死测量方法；基线 `[未验证]` 表示当前无测量，不表示通过。

| ID | 指标 | 基线 | 目标 | 证据来源 | 门槛 |
|---|---|---|---|---|---|
| M1 | Capture success rate（真实 host，≥2 host × ≥20 turn） | `[未验证]` | ≥ 0.95 | `g1-evidence/stages.json` + host-capture | G1 |
| M2 | Recall@5，10k chunk needle-in-haystack | `[未验证]` | ≥ 0.7 | eval harness `recall` / matrix；#281 过期降权回归测试绿 | G1 |
| M3 | 召回预算/延迟：focal floor 0.5、budget 2400 字符（guide 既有锁定值）；p95 延迟目标 | 锁定值已有；p95 `[未验证]` | p95 ≤ 1.5 s（待 freeze） | eval + `observability` | G1 |
| M4 | Fresh-install 成功率 | 0（PyPI 404） | (1) 索引路线可用则 3 OS 冒烟全绿；或 (2) source 路线 3 OS 全绿且文档不再承诺 one-command | installer 冒烟 + 文档 | G2 |
| M5 | 人工有用确认 | 0 | G1 每格 ≥1；G2 ≥60% 的 ≥20 会话（与 D294 研究 bar 对齐，非本计划自创） | `human-label.json` | G1/G2 |
| M6 | daemon 常驻内存 / CPU | `[未验证]`（H219 待测） | 目标在 H219 冻结时定；无实测不宣称低资源 | H219 分阶段 RSS | G1（记录） |
| M7 | 零跨 profile 泄漏 / 零编造答案 / 零 ACK 冒充持久化 | 未测 | 0 / 0 / 0 | `g1-evidence` 硬门 | G1 |
| M8 | day-7 留存 | `[未验证]`，首次邀请后才可观测 | 仅观测，不作为 Alpha 出口硬门 | 后续独立观察 | 非出口 |

> 注：M2/M3 的数字是产品测量目标，不等于"竞品优越"或"记忆已被证明有效"；promotion 只能说实际通过的 host/route/能力。

## 7. 客户可见声明的证据状态（明确标记）

| # | 声明（来自 README/guide/design 14） | 状态 | 证据 / 缺口 | 处置 |
|---|---|---|---|---|
| C1 | "一条命令安装 / 包索引可用" | **CONTRADICTED** | PyPI 404；#179 OPEN；版本回退 0.1.0 | D-A；文档已诚实，G2 前不得改成承诺 |
| C2 | "console provider catalog（preset / test connection / searchable list）" | **CONTRADICTED** | console 只有 raw config key；#381 OPEN | D-B：改措辞或实现 |
| C3 | "可从 4 个 host 导入历史会话" | **PARTIAL** | 代码 4/4，文档 1/4；真实数据正确性 `[未验证]` | G0-1 文档；D-E 证据切片 |
| C4 | "recall 真正帮上忙 / 恢复未完成任务" | **UNVERIFIED** | G1 未跑；#411 自助恢复缺口 | G1 出口硬门 |
| C5 | "`recent_sessions` 易恢复" | **PARTIAL** | bound-root 恢复已合；#411 OPEN（unresolved 语义 + 字段契约） | D-C |
| C6 | "host status 真实反映捕获/信任" | **PARTIAL** | #408 / #385 OPEN | G2-4 |
| C7 | "版本 0.3.0 已冻结/可发布" | **CONTRADICTED** | `1aa5774` 重新冻结 0.1.0；未发布 | 以 main 0.1.0 为准，不 retag |
| C8 | "Python 3.14 支持" | **SHIPPED** | `requires-python >=3.14`、`py314`、`68b2dcf` | 无需处置 |

## 8. 非目标（本阶段明确不做）

云同步、多用户、加密-at-rest/E2EE/TEE、supervisor 或崩溃自愈、agent 框架、语义搜索替代、知识图谱即真理、dream v2 作为交付承诺、t3code 纳入 Alpha、无授权的付费/部署/对外发布。理论锚继承 r3 §7（R4 编码特异性、R54/R55 信任校准）；本计划不引入新脑机制，也不把工程约束包装成神经科学结论。

## 9. 收口要求

- 本文经独立 QA/主控接受后按 `issue → branch → PR → merge` 落主干；PR 正文 `Closes #309`；提交 trailer 用 `Seat: product-manager`。
- org `_state.md` 追加当前指针（本文路径 + base + 接受哈希），`#309` 由落地 PR 关闭。
- 本轮仅文档：未改产品代码/配置/已安装运行时/设计字节；未跑产品测试；未联系外部人员；未发布/未 push。
