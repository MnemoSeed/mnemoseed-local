# PRD · #273 会话内动作可见性（conversation visibility）

> 立项：owner 2026-10-24 指令。痛感：捕获、T1 会话起始回放注入、T2 中段被动回想全部静默发生，
> owner 在对话里无法分辨 mnemoseed 何时记了什么、何时注入了什么。方向讨论定案：给这些动作一个
> **会话内可见的一行信号**，配灵活开关。**owner 拍板：默认 `compact`**（issue #273 评论
> 2026-10-24）。
> 设计评审：solution-architect 2026-10-24 裁定 **SHIP-WITH-ADJUSTMENTS（7 条已全部折入本文）**，
> 本文即冻结合同。

## 1. 目标（用户可见行为）

mnemoseed 每次动作，在对话流里出现一条**惰性**（不指挥模型）的一行信号：

| 事件 | compact（默认） | verbose | off |
|---|---|---|---|
| T1 会话起始回放注入（opencode） | 注入时一行 | compact + 记忆条数/字符数 | 零信号 |
| T2 中段被动回想注入（两宿主） | 注入时一行 | compact + 条数/字符数 | 零信号 |
| 捕获（capture ingest） | **每 session 首次捕获**一行（此后静默） | 每次 flush 一行含计数 | 零信号 |
| 主动 recall（MCP 工具调用） | 不处理——工具调用本身在对话中已可见 | 同左 | 同左 |

信号文案固定词表（英文、中性、无祈使），形如
`[mnemoseed] captured this session` / `[mnemoseed] replayed context from a previous session` /
`[mnemoseed] recalled related memories`；具体字节由测试钉死；verbose 计数格式亦由测试钉死。

## 2. 非目标

- 不改变模型行为：信号行**绝不含指令/祈使内容**（"use this"/"you should" 等一律禁止），固定词表。
- 不改捕获、排名、保留、注入选择逻辑本身——只加可见性。
- 不加 console 页面、不加 CLI verb；信号不持久化（debug lane 既有 JSONL 照旧可记）。
- ZCode / Codex 宿主渲染不在本批（#272 适配器落地后随适配器带出）。
- compact 不做逐 turn 捕获报数（噪音）。

## 3. 机制（基于 2026-10-24 代码面侦察 + 架构师核正）

- **配置**：新增 `[visibility] mode = "off" | "compact" | "verbose"`（默认 `compact`；
  枚举校验沿 `DREAM_ENSEMBLE_MODES` 先例 `config.py:149`）。configwrite 注册表加热生效
  （先例：`capture.auto_recall` 热生效测试 `test_recall_pending.py:456`），下一事件即生效、免重启。
  **S1 必改 `default_config_toml()` 模板面（`config.py:922`），模板字节若被测试钉住则同步更新。**
- **配置如何到达 hook**：沿既有 wire 字段模式（`enabled`/`budget_chars` 先例）——daemon 在
  `/session/recent` 与 `/session/recall-pending` 响应里加 `visibility` 字段；hook **不读
  config.toml、不调 config 端点**。**字段缺席 ⇒ 按 off 处理**（旧 daemon × 新 hook 字节级不变；
  新 daemon × 旧 hook 自然忽略——双向兼容）。
- **opencode 渲染**（`hosts/opencode/plugin.ts`，常驻进程）：T1/T2 信号行随既有
  `hookOutput.system.push(...)` 通道（`plugin.ts:1016/:1073`）；首次捕获用**独立的
  `firstCaptureSeen` 闩锁**（不复用 `reconciledSessions`——后者语义是"已 reconcile"，
  有三处入队触发点，复用会把两种语义耦死），在下一个 transform 时机渲染。
- **claude_code 渲染**（`hosts/claude_code/events.py`，无状态进程）：T2 信号行进
  `additionalContext`（`events.py:430-437`）；捕获信号由 daemon 侧 per-session 首次捕获闩锁
  记账、经下一次 UserPromptSubmit 响应捎带。**须重构 served-pull 早退（`events.py:425-430`）：
  `enabled=false` 且仅有捕获信号时也要发 stdout 载荷**——这正是 stdout 红线修订的实质。红线
  修订为：stdout 仅允许 (a) T2 additionalContext（既有），(b) `mode != off` 时的信号载荷；
  测试须单列"仅捕获信号、无 recall 项"用例。
- **预算与 needle 隔离**：信号行计入 T1 4000 字符包装成本与 T2 `budget_chars`（affix 挤压沿
  pin-suffix 先例 `events.py:334-348`：预算紧张先 shed 信号行、再 shed 既有内容，绝不超顶）；
  **信号文本不得进入 needle/消费证据集合**（needle oracle 扩展钉死）。
- **宿主用户可见性核验**（切片内必做）：逐宿主核验所选通道在宿主 UI 里 owner 看得见。若某宿主
  确认不渲染，如实记录并把该宿主信号降级为"transcript 可见"，不发明新通道。
- **README/config 注释诚实化**：最小改动、放在收尾（与在途 trust-comment 分支可能碰 README
  协调；冲突面留给落地时解决）。

## 4. 切片（单工程席顺序执行，分支 batch/273-visibility，独立 worktree）

| 切片 | 内容 | 主面 |
|---|---|---|
| S1 | `[visibility]` 配置 + 模板 + 热生效 + wire 字段 + daemon 侧 per-session 首捕闩锁 | `config.py`、`configwrite/`、`daemon/memory.py`、`daemon/ingest.py` + 各自测试 |
| S2 | claude_code：信号渲染 + 早退重构 + stdout 红线修订 + off 字节回归 + "仅捕获信号"用例 | `hosts/claude_code/`、`cli.py`、`test_hosts_claude_code.py` |
| S3 | opencode：T1/T2/首捕信号（独立闩锁）+ 预算/needle 隔离 + off 字节回归 | `hosts/opencode/plugin.ts`、`test_hook_ts_behavior.py`、`test_hosts_opencode.py` |
| S4 | verbose 增量（格式钉死）+ 词表/惰性 regex oracle + E2E golden 更新 + README 最小诚实化 | 全面上收口测试 |

默认 `compact` 意味着**默认注入字节有变化**：既有 golden 字节测试（advisory fence E2E 等）换新
golden；旧 golden 保留为 **off 模式回归 oracle**（off 必须逐字节还原今日行为）。

## 5. 验收断言（可执行）

1. `visibility.mode` 三值枚举，非法值启动报错；默认 `compact`；configwrite 写入后下一事件
   生效（免重启），测试沿 hot-apply 先例；**configwrite rollback 改回 mode 后下一事件同样生效**。
2. **off = 字节级 no-op**：off 模式下全部既有注入/输出与改动前逐字节一致；**既有测试的断言零
   修改**，允许（且仅允许）统一的 off 配置 fixture 覆盖来引导既有 golden 跑在 off 下。
3. compact：T1 注入恰一行信号（opencode）；T2 注入恰一行（两宿主）；session 首次捕获恰一行、
   每 session 至多一次；全部在既有预算内（不超 T1 4000 / T2 budget_chars）。
4. needle 集合与信号文本零交集（needle oracle 扩展）；信号词表固定且无祈使内容
   （regex oracle：禁 must/should/use/please 等）。
5. wire 字段双向兼容：**daemon 侧与 hook 侧各自**的"字段缺席 ⇒ off"行为都有独立单测；
   字段存在被旧 hook 忽略。
6. verbose：compact 之外追加的计数行格式由测试逐字节钉死。
7. Windows 编码纪律：全链路 UTF-8、无 locale 依赖（B2.10 双坑不复发）。
8. 门禁全绿：`uv run pytest -q` / `ruff check` / `ruff format --check` / `mypy src`。

## 6. 理论锚

- **R-a（拟入册，须 Crossref 核验后入册 REFERENCES.md）**：Lee & See 2004, *Trust in Automation:
  Designing for Appropriate Reliance*, Human Factors 46(1)——已验证规律：人对自动化的信任校准
  依赖于系统行为的可观察性；不可观察的自动化导致 misuse/disuse（配合 Parasuraman & Riley 1997,
  *Humans and Automation: Use, Misuse, Disuse, Abuse*, Human Factors 39(2)）。设计规则：自动
  记忆动作在发生时以紧凑、真实（绝不伪造）的信号外显，支持适度依赖。
- 设计判断（非借用）：默认 compact、off 可退、verbose 只加计数——锚只支撑"外显"方向，不支撑
  任何具体信号频率主张。
- **不入册清单**：多巴胺/奖励回路类比、游戏化反馈心理、"透明必然建立信任"口号式主张、进度条
  心理效应——全部无本设计所需的实证支撑。

## 7. 风险与边界

- **观察者效应**：信号行模型可见（虽惰性），会轻微改变对话形态——如实记入 #75 观察窗备忘。
- opencode 用户可见性未确证（system transform 是否渲染给用户）——切片内核验，查实则降级并
  如实记录，不阻塞其余宿主。
- claude_code stdout 红线是 B2.10 收口的对抗 QA 成果，本次修订必须窄化到 `mode != off` 且由
  QA 对抗复审。
- **顺序裁定（2026-10-24 冲突排查定案）**：#187 session-resume 已于 2026-09-13 经 PR #188
  （squash `a92cebf`）落地 main；本地 `fix/187-scoped-session-resume` 为陈旧残留分支，其与
  本批的表面重叠是假象，不构成顺序约束。batch/206-realmem-measure、feat/214-atlas-proof 与
  本批零交集可并行；feat/trust-comment-verify-20260923 可能碰 README——本批 README 改动
  已压到最小并放收尾。

## 8. 依赖与落地

- 无上游依赖；#272（ZCode/Codex 适配器）落地后把渲染通道模式复用过去。
- 落地 PR `Closes #273`；本 PRD 随批入库；收口记录回填本文末尾。

## 9. 收口记录（2026-10-24）

- **落地**：分支 `batch/273-visibility`（基线 main `baa42c4`，独立 worktree），S1–S4 单席 TDD 完成；
  对抗 QA 终判 **CLOSABLE**（无 BLOCKER），两项收口前置条件均已处置——IMPORTANT-1（needle
  隔离行为 oracle 空转）已修复并以现实变异体验证非空转；IMPORTANT-2（宿主可见性核验记录）
  即下条。三项机制裁量（verbose 计数在 recall-pending 消费渲染 / "每 block 恰一行 + 首捕独立" /
  opencode 闩锁取最近观测 wire 模式）经 QA 逐项裁定为忠实实现。3 个 NIT（verbose 预置闩锁、
  注释 issue 引用、计数行措辞）随批修净。
- **宿主可见性核验（诚实记录）**：两宿主均复用既有已验证注入通道（opencode `system.transform`
  push / claude_code `UserPromptSubmit` additionalContext），本批开发环境无法启动真实宿主 GUI
  ——按 §3 降级记录为"通道同既有注入，transcript/system-prompt 可见"；owner 本机 dogfood 的
  live UI 渲染确认待做，结果回填此处。
- **理论锚入册**：R54（Lee & See 2004，DOI 10.1518/hfes.46.1.50.30392）、R55（Parasuraman &
  Riley 1997，DOI 10.1518/001872097778543886）均 Crossref 核验命中，入册 `design/REFERENCES.md`。
- **门禁**：`uv run pytest -q` 3937 passed / 6 skipped / 9 failed——9 例全部为
  `tests/test_windows_autostart.py` 预存环境失败（干净基线 `baa42c4` 双重复现：工程席 stash 复跑
  + 协调者主克隆复跑，本批零触碰该文件族；CI 为该族权威门）；`ruff check` / `ruff format --check`
  / `mypy src` 全绿。测试收集 3900 → 3951（+51）。
- **遗留**：预存 autostart 失败归 #252 族 triage；live UI 可见性确认待 dogfood；观察者效应备忘
  已列入 #75 观察窗。
