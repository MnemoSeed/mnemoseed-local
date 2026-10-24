# PRD · #272 ZCode 与 Codex 宿主捕获适配器（T1 实施批）

> 立项：owner 2026-10-24 批准（"立，而且不只 ZCode，Codex 一样需要"）。**同日范围扩展
> （owner 指令）：claude_code 适配器不只是对标基准——本身也要体检（正常运作？全面？）
> 然后补全优化**；§3.5 为体检记录，S1 吸收补全项。前置：T0 探针已入库
> （`docs/zh/design/research-zcode-codex-capture-probe.md`，PR #276）——两宿主全部 FEASIBLE。
> 本文件是 T1 实施合同；solution-architect 2026-10-24 评审 **SHIP-WITH-ADJUSTMENTS（6 条
> 已全部折入本文）**，本文即冻结合同。
> 目标全景：owner 的日常开发流（ZCode + Codex）获得与 opencode/claude_code 同等的
> 捕获 + T1 会话起始回放 + T2 中段被动回想 + 可见性信号（#273 已落地的 compact 信号）。

## 1. 目标（用户可见行为）

- ZCode / Codex 会话的 user/assistant/tool turn 被自动捕获入 daemon，带宿主归因：
  `origin.host` = **`zcode`（新增枚举）** / **`codex_cli`（复用既有枚举，`turn.py:24-32`
  已存在）**；`provenance_source` 随 stamper 机制自动为 `zcode-chat` / `codex_cli-chat`。
- 两宿主会话起始（SessionStart）注入 T1 回放（围栏 + compact 信号行，预算内）；用户
  prompt 后按 auto_recall 拉取注入 T2（additionalContext 形态）。
- 宿主安装面一等：`mnemoseed-local host install / host uninstall / host status --host
  zcode|codex`（**新 `host` 子命令组，与既有 daemon/数据目录级 `install`/`uninstall`
  动词命名隔离**，cli.py:1018/1520 冲突消解）；doctor 对"已装未生效"诚实可见（Codex
  hooks 信任门未过、hook 文件更新后的 stale `trusted_hash`、ZCode hooks 全局开关状态）。
- off 语义与 #273 一致：`[visibility] mode = off` 或 wire 字段缺席 ⇒ 注入字节与既有
  claude_code off 形态同构（零信号）。
- **claude_code 补全**（范围扩展）：适配器获得 T1 会话起始回放（SessionStart 注入——
  当前官方文档已支持 additionalContext，B2.10 时代的"v1 边界"可关闭）与子代理事件
  （SubagentStart/Stop）摄取评估；四宿主（opencode/claude_code/zcode/codex_cli）达到
  等体验。
- **本机部署升级**（收尾步骤，owner 面向）：安装 runtime 从 v0.0.1 升级到本批产物
  （`uv tool install --force .` 路线，#179 列车前）；刷新已漂移的 opencode 插件文件；
  安装 claude_code 适配器（owner 确认后）。

## 2. 非目标（v1 明确不做）

- **不发布 marketplace**（ZCode 插件市场 / Codex 公共插件目录都拒带 hooks 的插件）——
  分发面留后续；本批只做本地安装器。
- **不做常驻 file-watch 守护**：Codex 用 hooks 作时钟、transcript 作事实源（§3.3），
  独立 watcher 等有证据（hooks 漏捕）再立项。
- **不验证 Codex Desktop app-server 模式**（探针 needs-probe #5）——v1 目标 CLI 形态；
  Desktop 行为如实记为未验证。
- **不合成 ZCode SessionEnd**：session 关闭走惰性边界（§6.5），不发明伪事件。
- **不改 opencode / claude_code 的既有可观测行为**：共享核抽取（DRY）必须以 claude_code
  **既有**测试零修改全绿为硬约束（字节 oracle 保护）；claude_code 的**新增**能力（T1、
  子代理摄取）以新增测试钉死、不改既有断言；opencode 源码面零触碰（部署面刷新除外）。
  `hosts/install.py` 是 opencode 专属安装器，S3 **沿其模式而非从它抽模块**。
- 不做 origin_agent 归因（两宿主 payload 无 agent 字段，`origin_agent` 保持 null——
  schema 本就可空；claude_code 的 agent 字段摄取维持现状）。

## 3. 两宿主形态定案（吃探针结论 + 架构师核正）

### 3.1 共用底座（DRY 抽取）

- 复用隐藏 verb：`mnemoseed-local _hook-event --host zcode|codex_cli`（扩 cli.py 既有
  `_hook-event` 分派，现 `choices=("claude_code",)` at cli.py:1560-1561）——hook 进程是
  无状态 stdin 转换器。
- 从 `hosts/claude_code/events.py` 抽取**共享事件核**（ENDPOINTS→normalize→ingest→ack
  后拉取→注入构造骨架，events.py:64 起）到 `hosts/common/`（命名 S1 定）：claude_code
  重接共享核，**既有 claude_code 测试零修改全绿**是抽取的硬约束（字节 oracle 保护）。
- #273 信号行构造复用：新宿主注入走同一词表/预算 shed 语义（wire `visibility` 字段
  缺席 ⇒ off）。
- **S1 同时落 `host` 子命令组分派骨架**（cli.py 共享面一次落定），S3a/S3b 只填各
  install 模块、不再碰 cli.py——解并行冲突。

### 3.2 ZCode（hooks 型，对标 claude_code）

- **订阅事件**（ZCode 七事件之四）：`SessionStart`（T1 注入）、`UserPromptSubmit`
  （user turn 捕获 + T2 拉取注入）、`PostToolUse`（tool 捕获）、`Stop`（assistant 尾部
  捕获）。
- **注入通道**：hook stdout JSON `additionalContext`（严格 schema——只发文档化键，
  未知键整包失效的教训写进测试）。T2 在 UserPromptSubmit 属实证语义；**T1 在
  SessionStart 是否支持 additionalContext 未实证（探针 Q2 只证实 UserPromptSubmit 语义）
  ——S0 显式核实项**，降级预案预写：T1 改由**首个 UserPromptSubmit 捎带**（拉
  `/session/recent` 一次并注入，attempt-once 门控）或 AGENTS.md 静态指令行，S0 定案。
- **payload 契约假设**：模板变量 `${CLAUDE_SESSION_ID}` 暗示 ZCode hooks 走 Claude-Code
  兼容 JSON 方言——S0 以 zcode-guide 文档钉死字段清单；假设不成立则 S0 产出真实方言
  fixture 再实现。
- **安装面**：`~/.zcode/cli/config.json` 顶层 `hooks`（默认禁用，安装器**显式翻转
  `hooks.enabled: true` 并记录 pre-state**——卸载时仅在 pre-state 为 false 时回翻）+
  `mcp.servers` 写入 `{"type":"http","url":"http://localhost:7788/mcp"}`（canonical、
  绝对路径、schema 严格）。marker 精确匹配 + 原子重写 + UTF-8 免疫 locale（claude_code
  安装器先例）。
- **捕获通道**：hooks 直供（事件载荷），不依赖 db.sqlite（WAL 轮询留待有漏捕证据）。

### 3.3 Codex（hooks 时钟 + transcript 事实源，宿主名 codex_cli）

- **订阅事件**：`SessionStart`（T1）、`UserPromptSubmit`（user turn + T2）、`Stop`
  （assistant 增量捕获）、`SessionEnd`（终态增量 + `POST /session/end` settle）。
- **捕获机制（核心设计）**：hook stdin 自带 `transcript_path` 直指
  `sessions/**/rollout-*.jsonl.zst`——**hooks 作时钟、transcript 作事实源、水位差分作
  载荷**：每次 firing 解压当前 transcript，按行 `ordinal` 与 per-session 水位差分，仅
  POST 增量 user/assistant turn（`response_item.payload.type == "message"`）。退化兼容
  未压缩 `.jsonl`。**水位存储归 daemon 侧**（hook 无状态）：`/ingest` ack 捎带 per-
  session ordinal 水位（S0 已定案见 §9.3；对齐 B2.2 ack 水位先例）。同状态重放 ⇒
  daemon 侧零重复入库（幂等语义定在摄取边界，见 §9.3；HTTP 重放仍发生，属无状态
  hook 下界）；宿主崩溃 ⇒ 下次 firing 从 ack 水位补齐（天然 replay）。
- **注入通道**：`UserPromptSubmit` 走 JSON `hookSpecificOutput.additionalContext`
  （~2500 token 上限，预算 shed 按 #273 语义先信号后内容）；`SessionStart` 的 T1 形态
  （developer-context stdout vs JSON 面）S0 核实。
- **安装面**：`~/.codex/hooks.json`（用户级）+ `config.toml` 增
  `[mcp_servers.mnemoseed]`（stdio 网关：`command` 绝对路径）；**信任门如实入档**：
  hooks 装好后须用户在 Codex 内 `/hooks` 审阅信任一次（内容 hash 键控）——安装器输出
  引导文案，`host status`/doctor 检测 `[hooks.state] trusted_hash` 缺失（已装未信任）与
  **hook 文件更新后的 stale hash**（已信任但内容已变）两种形态并如实区分。
- **zstandard 依赖**：**optional extras `[codex]`**（`mnemoseed-local[codex]`）+ lazy
  import——base 安装不为 Codex 付费；extras 缺失时诚实报错（指明安装命令），不裸栈。

### 3.4 MCP 注册（两宿主共同）

daemon 已有 stdio 网关与 HTTP `/mcp`（#249）；本批只做安装器写入，不改网关。ZCode 用
http 形态、Codex 用 stdio 形态（各按探针判定的最稳路径）。

### 3.5 claude_code 适配器体检记录（2026-10-24 本机只读实况 + 官方文档核对）

**运作实况（只读取证）**：

| 项 | 实况 | 判定 |
|---|---|---|
| `~/.claude/settings.json` hooks | **零条目**（顶层仅 autoUpdatesChannel/enabledPlugins/env/extraKnownMarketplaces/theme） | **适配器未安装**——"未运作"是部署态，非代码缺陷（仓内测试链全绿） |
| daemon.log（近 500 行） | claude_code 零提及；末条 2026-10-16 17:39 | 无 claude_code 捕获流 |
| 安装 runtime | `mnemoseed-local` **v0.0.1**（uv tool） | 落后 main 约两个月（缺 B2.3 后全部修复/特性：watchdog、时间感知、保留重设计、provenance、可见性…） |
| opencode 插件 | 已装 `~/.config/opencode/plugin/mnemoseed-local.ts`（103,388 B / `a2c441ec…`）vs 仓内 112,745 B / `3d334053…` | **漂移约 9.4KB**——缺后续批次（R2 针标/B2.7 规则预算/#273 可见性，按批次时序） |
| 数据面 | meta.db/graph.db/isolated.db + chunks.lance 均有内容 | 捕获在积累；最近一周日志安静（不据此断言停摆，lance 计数留升级后 doctor 核） |

**全面性差距矩阵（vs 官方 hooks 文档 2026-10-24 + opencode 对标）**：

| 能力 | opencode | claude_code 现状 | 补全裁定 |
|---|---|---|---|
| T1 会话起始回放 | ✓ | **缺**（B2.10 定为 v1 边界——当时 SessionStart 无注入） | **本批关闭**：SessionStart 现支持 additionalContext（matcher `startup/resume/clear/compact/fork`），共享核 T1 机器直接复用；attempt-once 门控 + 预算 + 可见性信号同语义 |
| T2 中段回想 | ✓ | ✓ | 不动 |
| 规则预算注入（B2.7） | ✓（骑 T1 读取） | **缺**（无 T1 故无载体） | 随 T1 补齐（同一注入载荷） |
| 子代理摄取 | ✓（session_task_link 族系） | **缺**（events.py:17-18 显式不入 SubagentStart/Stop） | **S0 书面裁定，仅当零 daemon schema/语义新增**（纯复用共享核 normalize；parent 锚定与 provenance 形态写明；stdin 自带 agent_id——采纳时须明示与新宿主 origin_agent=null 非目标的边界）才随批；否则挂起记录 |
| T3 消费证据/针标/崩溃回放/归因 | ✓ | ✓（B2.2/R2/B2.9 已落） | 不动（随 runtime 升级生效） |
| 可见性信号（#273） | ✓ | 代码已合、**装机未及** | 随部署升级生效 |
| 事件面覆盖 | — | 注册 5 事件（UserPromptSubmit/Stop/PostToolUse/PreCompact/SessionEnd）；官方现已扩张（Setup/PostCompact/Subagent*/FileChanged 等） | 本批仅评估子代理两项；其余新事件**不追**（无捕获价值主张，防范围蔓延） |

**优化结论**：claude_code 的"未运作"根因是**部署停滞**（未安装 + runtime v0.0.1 + 插件漂移），
代码面真实差距是 T1（含规则预算载体）与子代理摄取两项——前者本批关闭，后者 S0 评估。
部署升级（runtime + 插件刷新 + claude_code 安装）列为 S4 收尾的 owner 面向步骤。

## 4. 切片与文件面（S1 后两席并行，worktree 隔离）

| 切片 | 内容 | 主面 | 依赖 |
|---|---|---|---|
| S0 | 契约钉死：zcode-guide/codex 文档逐字段 fixture（`tests/fixtures/zcode_hook/`、`tests/fixtures/codex_hook/`）+ `zcode` 枚举 additive + 三项定案（ZCode SessionStart 注入能力及 T1 降级选择、ZCode payload 方言、Codex 水位 wire 形状） | fixtures、`schema/turn.py`、定案记录入本文 §9 | — |
| S1a | 共享核抽取（claude_code 重接，既有测试零修改全绿——纯重构）+ `_hook-event` 多宿主分派 + `host` 子命令组骨架 + `zstandard` extras 骨架 | `hosts/common/`、`hosts/claude_code/`、`cli.py`、`pyproject.toml` | S0 |
| S1b | **claude_code 补全**：T1 via SessionStart + 规则预算载体 + 子代理摄取[按 S0 裁定] | `hosts/claude_code/` + 新增测试 | S1a |
| S2a | ZCode 适配器（normalize→ingest→T1/T2→可见性） | `hosts/zcode/` + `tests/test_hosts_zcode.py` | S1a |
| S2b | Codex 适配器（transcript 差分读、ack 水位幂等、settle、lazy zstandard） | `hosts/codex/` + `tests/test_hosts_codex.py` | S1a |
| S3a/S3b | 安装器（marker 原子写、`--dry-run`、pre-state 回滚、卸载恰移除、doctor 诚实面） | `hosts/<h>/install.py`（**不碰 cli.py**，沿 `hosts/install.py` 模式） | S2x |
| S4 | E2E 收口：spare port + 隔离 HOME 真 daemon 全链 + README/config 诚实化（README 改动压到最小，排在 trust-comment 分支之后或 rebase）+ **本机部署升级（runtime→本批产物、opencode 插件刷新至逐字节等于仓内、claude_code 适配器安装；升级后 doctor 复核捕获流）** | E2E 测试 + 文档 + owner 面向部署步骤 | S3 |

S2a/S2b、S3a/S3b 文件面互不相交（cli.py 已由 S1 一次落定），两工程席两 worktree 并行。

## 5. 验收断言（可执行）

1. **捕获归因**：每订阅事件经 golden fixture 归一化后 POST /ingest，`origin.host` =
   `zcode`/`codex_cli`，provenance 前缀随 stamper 机制；`zcode` 枚举新增纯 additive
   （无 schema 迁移；wire 侧 host 为自由 str，枚举约束在 capture 侧——验收不主张 wire
   层校验）。
2. **Codex 水位幂等**（语义按 §9.3 摄取边界读法）：同一 transcript 状态重复 firing ⇒
   daemon 侧零重复入库（HTTP 重放仍发生，属无状态 hook 下界）；追加增量 ⇒ 恰增量入库；
   模拟中途崩溃（水位丢失）下次 firing 从 ack 水位补齐不重复。测试 oracle 断言 daemon
   侧 turn 计数。
3. **T1/T2 注入**：T1 按 S0 定案通道产出（围栏 + compact 信号行、预算内）；acked user
   ingest 后 UserPromptSubmit 产 T2；`visibility` 缺席/off ⇒ 零注入零额外 stdout——与
   claude_code off 形态字节同构。
4. **stdin 防御**：畸形/未知 payload ⇒ 诚实 stderr + 空 stdout + fail-open（不阻断宿主、
   不抛栈）；`_hook-event --host <未知>` ⇒ 明确报错非静默。
5. **安装器**：marker 精确、原子写、幂等重装；卸载恰移除己方条目（他人的 hooks/mcp
   条目逐字节不动）+ **`hooks.enabled` 按记录的 pre-state 回滚**；ZCode 翻转显式 +
   `--dry-run` 预览；Codex 未信任/stale-hash 两态在 `host status`/doctor 可见区分；
   UTF-8/locale 免疫（B2.10 双坑回归复刻）。
6. **共享核零回归**：claude_code 既有测试**零修改**全绿；opencode 面零触碰。
7. **extras 诚实**：base 环境缺 zstandard 时 Codex 捕获路径给出明确指引报错（含安装
   命令），不裸 ImportError 栈。
8. **claude_code 补全**：T1 在 SessionStart 产出（attempt-once、围栏 + compact 信号行、
   预算内、含规则预算载体）；既有 claude_code 断言零修改全绿；子代理摄取按 S0 裁定
   （做 ⇒ 有归因/parent 锚定测试；不做 ⇒ §9 记录理由）。
9. **部署升级验证**（S4 owner 面向验收，非 CI 断言；PR 附 doctor 复核清单）：**升级顺序钉死
   = 先 runtime（含 daemon stop/up 重启）→ 后 opencode 插件刷新 → 再 claude_code 适配器
   安装**（旧 daemon v0.0.1 可能缺 `/session/recall-pending`——先升 daemon 使窗口内行为
   单调向好）；升级后 `~/.config/opencode/plugin/mnemoseed-local.ts` 与仓内 `plugin.ts`
   **逐字节相等**；`~/.claude/settings.json` 出现 marker 精确的 mnemoseed hooks 条目；
   `mnemoseed-local doctor` 无"已注册未连接"类告警；捕获流经 doctor/observability 复核。
10. **E2E**：隔离 `MNEMOSEED_LOCAL_HOME` + spare port + 沙箱宿主 HOME（fake `~/.zcode`、
    temp `CODEX_HOME`）下全链绿；门禁全绿（pytest/ruff/format/mypy + commit-range）。

## 6. 风险与未决（S0 必须消解或降级）

1. **ZCode SessionStart 注入能力未实证**（架构师核正）——S0 显式核实；降级预案已预写
   （§3.2：首 prompt 捎带 / AGENTS.md 静态行）。
2. **ZCode payload 方言假设**（CC 兼容）——S0 文档钉死；不足则降级为一次性 workspace 级
   临时 hook 实捕（不碰用户级 `~/.zcode`）。
3. **Codex 沙箱实捕**：`CODEX_HOME` 指向 temp + `codex exec`；若被 auth 挡，降级
   fixture-from-docs + owner 辅助确认。
4. **ZCode 严格 schema additionalContext**：未知键整包失效——注入只用文档化键，golden
   字节测试钉死。
5. **ZCode 惰性会话边界**（无 SessionEnd）：daemon 侧 session 不显式关闭——如实记录；
   若 E2E 显现副作用（slots/tombstone 语义），如实上报再议。
6. **同日多 PR 串行**：后开分支须 rebase 最新 main 再合（#277 教训，已入 org 状态）；
   README 面排 trust-comment 之后。

## 7. 理论锚

**无新增借用——工程接线面**（沿 B2.5 先例）：本批是宿主适配工程，不引入新的记忆行为
理论。T1/T2 注入行为沿用 B2.1 已入册锚（TA-1..6，含 TA-5 围栏净化）；可见性信号沿用
R54/R55（#273 批已入册）。不入册清单：无（无候选）。

## 8. 依赖与落地

- 依赖：#273（已落地，wire `visibility`）；探针 PR #276（已入库）。无其他上游。
  （架构师提的 187 顺序约束经核实为陈旧分支误判：#187 已于 2026-09-13 经 PR #188
  squash `a92cebf` 落地 main、远端分支已删，不构成本批约束。）
- 新运行依赖：`zstandard`（optional extras `[codex]`，lazy import）。
- 落地：S0+S1a、S1b、S2a+S3a、S2b+S3b、S4 各一 PR（`Part of #272`），全部合入后收口 PR
  `Closes #272`；本 PRD 随首个 PR 入库，收口记录回填 §9。**#179（发版/装机 pin）保持独立
  立项跟进，不折入本批**——本地升级走 `uv tool install --force .`，不依赖发版。

## 9. S0 定案记录（append-only）

### 9.1 ZCode T1 注入通道：SessionStart + `additionalContext`（2026-10-24，docs-only）

- **定案**：T1 走 `SessionStart` hook stdout JSON 的 `additionalContext` 注入；§3.2 预写
  降级梯（首 UserPromptSubmit 捎带 / AGENTS.md 静态行）未触发。
- **证据**：官方指南 `zcode-guide/0.3.0/skills/diagnosing-hooks/SKILL.md` §2 "Hook
  output"："additionalContext is injected into the conversation" 是通用 JSON 输出能力
  陈述，其后仅枚举两个事件特例（`PreToolUse` → permission decision、`Stop` →
  continuation），`SessionStart` 不在例外之列；同节钉死 `SessionStart` matcher 语义
  （`startup|resume|clear|compact`，大小写敏感正则、省略即全匹配）——四值为 claude_code
  SessionStart `source` 枚举（startup/resume/clear/compact/fork）的子集，佐证方言同源。
  T2 的 UserPromptSubmit 通道（探针 Q2 已实证语义）不变。
- **边界（如实记录）**：指南只钉"能力级"注入语义与严格 schema（未知键整包校验失败、
  事件名必须正确——同文档 pitfall 8），未给出输出 JSON 的键位布局；本批按 claude_code
  同款 `hookSpecificOutput.hookEventName/additionalContext` 布局实现（与 9.2 的 stdin
  方言同一假设），S2a 一次实捕同时钉死两形。若实捕证伪 ⇒ 启用 §3.2 降级梯
  （首 UserPromptSubmit 捎带，attempt-once 门控）。

### 9.2 ZCode stdin payload 方言：docs-only 降级，CC 兼容方言为工作契约（2026-10-24）

- **定案**：zcode-guide 0.3.0 全部六个 skill 对 `hook_event_name`/`stdin` 零提及
  （插件树全量 grep 无匹配；文档只覆盖注册形状、matcher 语义、模板变量、输出语义）——
  官方文档不足以钉死 stdin 字段清单，按指令降级 docs-only，本轮不实捕（禁启宿主进程）；
  §3.2 预注册的 Claude-Code 兼容 JSON 方言假设为工作契约。
- **落地面**：`tests/fixtures/zcode_hook/` 四订阅事件 golden stdin JSON
  （session_start / user_prompt / tool_use / stop）按方言假设落盘；
  `tests/test_s0_host_contracts.py` 形状断言钉契约（单行 JSON、`hook_event_name` ∈
  文档化七事件名之四、`session_id` 非空、键集 == 方言允许清单、逐字段类型）。
- **缺口移交 S2a**：一次 workspace 级临时 hook 实捕（§6.2 形态，不碰用户级 `~/.zcode`）
  核对方言；证伪则改 fixture + 断言再实现。已钉死的文档事实（事件名、matcher 值、输出
  严格 schema、模板变量）不依赖该核对，S2a/S3a 可直接消费。

### 9.3 Codex 水位 wire 形状：请求 `transcript_ordinal` + 202 回传 `ack_ordinal`（2026-10-24）

- **定案**（对齐 B2.2 ack 水位先例，可行性已对代码核证）：
  - **请求字段**：`IngestEvent` 增 additive 可选 `transcript_ordinal: int | None = None`
    ——该事件所归一化的 transcript 行 `ordinal`（rollout 文件行号，≥0）。Codex turn
    事件必带，其余宿主/事件缺席。旧 daemon（pydantic extra-ignore）静默忽略、双向
    wire 兼容（additive 请求字段先例：`IngestEvent.agent`/`session_parent_id`，
    `schema/turn.py:98,101`）。
  - **daemon 存储**：内存 per-(effective profile, session_id) 高水位整数——memory 服务
    per-session 映射族（同 pending slots / capture latch 先例，key 归属 effective
    profile 与 `note_capture` 一致，`daemon/memory.py:1724`）。仅捕获进度标记，
    scoring/ranking/provenance 永不读取（inert 纪律同 origin_agent）。更新规则：请求
    携带该字段且事件被 segmenter 接受 ⇒ 高水位 = max(存量, transcript_ordinal)。
  - **响应字段**：/ingest 202 体增 additive `ack_ordinal: int`——当且仅当请求携带
    `transcript_ordinal` 时在位（值 = 本请求处理后的存量高水位）；缺席时既有宿主的
    202 逐字节不变（additive 响应先例：recall_pending `visibility`，
    `daemon/memory.py:1934-1935`；202 体现状 `daemon/ingest.py:224-229`；hook 侧经
    `DaemonClient.post` 返回体可达，`rest_client.py:96`——claude_code 路径今日丢弃该体
    `cli.py:1387`，Codex 路径将读取）。
  - **幂等语义（钉死）**：定在**摄取边界**——daemon 对 `transcript_ordinal` ≤ 存量
    高水位的事件跳过入段（零重复摄取）。hook 无状态且无读通道，无法在发 POST 前知晓
    daemon 水位，故同状态重放的 HTTP 请求仍会发生（每 firing ≥1 次是无状态前提下的
    下界）；验收 §5.2 "零重复 POST" 按摄取边界语义执行，测试 oracle 断言 daemon 侧
    turn 计数而非 HTTP 计数。hook 侧差分 = **局部下界 + ack 补齐**：Stop firing 只
    POST 末条 user message ordinal 之后的 message turn（transcript append-only，可
    本地推导）；同进程内以 202 `ack_ordinal` 为差分起点补发 (ack_ordinal, N] 内未发
    增量——宿主/hook 中途崩溃后，下次任一 firing 经其首个 POST 的 ack 天然 replay。
  - **清理**：/session/end 随 session 状态清扫一并丢弃高水位（exact key + session 级
    sweep，沿 `daemon/memory.py:1938-1999` 模式）；settled 会话再捕获被
    SessionSettledError 拒绝，无复活路径。daemon 重启丢失该水位——同其余 in-memory
    session 态的既有设计边界，如实记录。
  - **实现归属**（orchestrator 2026-10-24 订正）：wire 字段 + 202 回传 + daemon 存取/
    清理整体归 S2b（schema/daemon 非纯重构面，不入 S1a）；S1a 维持纯重构 + 骨架。

### 9.4 claude_code 子代理摄取裁定：随批（零 daemon schema/语义新增成立）（2026-10-24）

- **裁定**：SubagentStop 摄取随批；SubagentStart 不摄取。
- **零 schema 新增（核实）**：归因与 lineage 字段全部先在——`IngestEvent.agent`
  （`schema/turn.py:98`）、`Turn.origin_agent`（:186）、`ChunkStamp.origin_agent`
  （`schema/stamp.py:97`）；normalizer 侧 `_agent_of` 已解析 `agent_type`/`agent_id`
  （`hosts/claude_code/events.py:225-229`），归因链路 segment → write context → stamp
  现成（`capture/segment.py:76` → `daemon/app.py:332` → `capture/stamper.py:423`）。
  无新枚举、无迁移、无新端点。
- **SubagentStop → 既有 `_stop` 通道**：assistant_message ingest（payload 文本 +
  transcript 尾兜底取文本/model_id，`events.py:247-259` 原样复用）+ `_agent_of` 填
  `agent` ⇒ `origin_agent`；`provenance.source` 仍 `claude_code-chat`
  （`stamper.py:403`），归因仅为 origin_agent 标签，B2.9 inert 纪律（scoring/ranking
  永不读取）原样适用。
- **SubagentStart 不摄取的理由**：裸"启动"信号无 turn 内容，映射须新增
  IngestEventType（schema 新增，违反裁定准绳）；子代理调用已被 PostToolUse-on-Task
  以 tool_use 捕获（tool_input 载子代理 prompt），零信息损失。
- **parent 锚定（payload 形状推理，如实标注）**：CC 子代理事件 stdin 与 Stop 同形
  （session_id/transcript_path/cwd/hook_event_name + agent 字段），无独立子会话 id
  字段——session_id 即父会话，子代理 turn 摄入父会话；`session_parent_id` 保持 None
  属 correct-by-construction（该会话即 root，`schema/turn.py:99-101` 语义），非猜测。
  对照：opencode 的 parentID 来自其宿主自有字段（`hosts/opencode/plugin.ts:557`），
  CC payload 无对应字段可填。S1b 以 fixture 形状测试钉死；若未来 CC payload 出现独立
  子会话 id，形状测试先行失败。
- **边界**：新宿主（zcode/codex_cli）payload 无 agent 字段 ⇒ `origin_agent` 恒 null
  （§2 非目标原样）；子代理事件无 user_prompt ⇒ 无 focal scan、无 T2——既有行为，
  零语义新增。

### 9.5 Codex stdin 契约订正：幽灵字段剔除，按官方文档重钉（2026-10-24，QA 实抓纠正）

- **订正**：S0 首版 codex fixture 的 `prompt_id`/`scratchpad_dir`/`effort` 为**幽灵字段**
  （官方文档全站零记录）；`model` 被误限为 SessionStart 专属；`permission_mode` 被误入
  SessionEnd。来源如实记录：orchestrator 在派工简报中把 Claude Code hooks 文档字段
  误供为 Codex 官方事实（简报标注"orchestrator 2026-10-24 已核"），S0 未独立抓取即
  采信；adversarial QA 实抓官方文档判 NOT CLOSABLE，S0 复抓（curl
  developers.openai.com/codex/hooks，2026-10-24）1:1 证实 QA 清单。教训入档：外部
  契约字段一律以本席独立抓取为准，转述"已核"不作为采信依据。
- **官方清单**（developers.openai.com/codex/hooks，2026-10-24 抓取；与
  learn.chatgpt.com/docs/hooks 同页）：
  - **公共 stdin 字段（每命令钩子）**：`session_id`（string；文档明示子代理钩子用父
    会话 id）、`transcript_path`（string|null）、`cwd`（string）、`hook_event_name`
    （string）、`model`（string，Codex 扩展，当前模型 slug）。
  - **permission_mode**（default/acceptEdits/plan/dontAsk/bypassPermissions）：九事件
    在列（SessionStart/PreToolUse/PermissionRequest/PostToolUse/UserPromptSubmit/
    SubagentStart/SubagentStop/Stop/Interrupt）——**SessionEnd 不在列**。
  - **事件特定字段**：SessionStart + `source`（startup/resume/clear/compact/fork）；
    UserPromptSubmit + `turn_id`/`prompt`；Stop + `turn_id`/`stop_hook_active`/
    `last_assistant_message`（string|null）；SessionEnd + `reason`（现仅 "other"）。
    `turn_id` 为 turn 级事件的 Codex 扩展；matcher 语义：SessionStart 匹配 source，
    SessionEnd 匹配 reason，UserPromptSubmit/Stop 暂不使用 matcher。
  - 官方同页确认输出面：SessionStart/UserPromptSubmit 的 JSON 输出即
    `hookSpecificOutput.hookEventName/additionalContext`（§9.1 的输出布局假设在
    Codex 侧获官方证实；ZCode 侧仍待 S2a 实捕）。
- **重钉**：`tests/fixtures/codex_hook/` 四 fixture 与 `tests/test_s0_host_contracts.py`
  允许清单按上述清单重钉（键集 == 允许清单的白名单式断言，多余键即红）；幽灵字段
  零残留。§9.3 水位 wire 形状不涉及 stdin 字段，无需订正。

### 9.6 S2a 定案：实捕降级（无头 CLI 缺位）、T1 通道形状、Stop 形状（2026-10-24）

- **实捕结果：降级 docs-only（第二次）**。探针过程：`zcode` 不在 PATH（Git Bash/where 均无）；本机
  ZCode 仅以 Electron 桌面应用形态存在（`%LOCALAPPDATA%/Programs/ZCode`，`app.asar.unpacked` 内无
  CLI 入口二进制）；npm 全局 bin 仅有 codex/opencode shim。无头/`-p` 模式无载体，触发一次会话
  只能启动用户桌面应用并落用户级 `~/.zcode`（合同红线）⇒ 按简报预置门立即降级。**§9.2 的
  CC 兼容方言假设保持工作契约**，`tests/fixtures/zcode_hook/` 四 fixture 零改动（无实捕差异可
  钉）；§9.1 的 `hookSpecificOutput.{hookEventName, additionalContext}` 输出布局假设同样保持
  （SessionStart T1 注入通道未实证这一点如实保持，S4/E2E 或 owner 辅助可再核）。
- **T1 通道形状定案（docs-only 定案，S2a 已实现）**：SessionStart firing ⇒ 无状态 hook 进程内
  **恰一次** `POST /session/recent`（attempt-once 的门 = 进程本身，跨 firing 不共享状态）；请求体
  = `{profile_id, sessions: 2, per_session: 8, exclude_session_id}`，**不带 `self_session_id` 与
  `resume_query`**——会话起始无 prompt 可作 scoped-resume 锚，且 daemon 对"双身份字段 + 无
  resume_query"按 automatic-path fail-closed 处理（`daemon/memory.py` session_recent 守卫），
  legacy unscoped 重放才是无状态 SessionStart hook 的诚实形状。注入块沿 opencode
  `buildRecallInjection` 形状（围栏 + disclaimer + `<session-tail id/ended/started>` 组 + 边界块
  尾切片），预算 4000 chars，信号行按 wire `visibility` mode（off/缺席 ⇒ 无信号行）。
- **T1/T2 状态边界（如实记录）**：T2 pull 的 `seen_chunk_ids` 恒为空——SessionStart 进程退出后
  T1 已注入的 chunk id 无处存活，focal 命中可能重放一条 T1 已服务过的 chunk。无状态 hook 的
  设计下界，与 §3.3 对 Codex 的判断一致；若 E2E 显现重复注入副作用，再议 per-session 侧车。
- **Stop 形状定案**：`last_assistant_message`（方言假设字段）为主文本；`transcript_path` 尾部按
  Claude-Code 转录 JSONL 方言防御性解析（取 model_id 与文本兜底）——zcode rollout 文件
  （`model-io-session_*.jsonl`）内部形状未经实捕核实，解析失败降级 `(None, "")`，事件不丢弃。
  `stop_hook_active` 不改变捕获行为（续答轮的 assistant 文本仍属会话内容）。
- **B2.7 规则预算载体未随 zcode T1**：§3.2/§5.3 对 zcode T1 的冻结验收仅钉"围栏 + compact 信号
  行、预算内"；opencode 载体与 claude_code S1b 补全项不自动扩展到 zcode（记为收口前 owner 可选
  项，非本批验收缺口）。
- **安装器机制定案**：`hooks.enabled` pre-state 记录在配置同目录 sidecar
  `mnemoseed-local.json`（`{"hooks_enabled": true|false|null}`，null=键缺席）——不向宿主 config
  写外来键；卸载仅按记录回滚（记录缺失 = 诚实未知，开关不动）。marker 契约 = `type: "process"`
  且 `args` 精确等于保留 argv（`_hook-event --host zcode` 直连形，或 `-m mnemoseed_local.cli …`
  模块形）——command 本身随环境（PATH 上的工具 shim 优先，否则运行期解释器绝对路径），故以
  argv 而非路径为保留签名。`--dry-run` 为模块级能力（`install(dry_run=True)`/`uninstall(
  dry_run=True)`）；`host` 子命令组分派约定零参调用，CLI 旗标需改 cli.py（S1 面已冻结），如需
  CLI 露出留收口批裁定。
- **暂存债务（QA 对抗审入册）**：claude_code 侧与 common 新核存在同逻辑副本，收口前归位
  `hosts/common/`——`claude_code/events.py:194-227,252-262,300-370` ↔ `common/recall.py:98-147,
  323-371`（T1/T2 读取-渲染 lane）；`claude_code/install.py:105-121` ↔ `common/config.py:35-66`
  （strict-JSON 配置手术）。归位切片 = S1b（claude_code 重接 common lane，字节 oracle 护航）；
  归位前两侧零漂移由测试钉死。

### 9.7 S2b 定案：Codex 适配器实现形状（2026-10-24）

- **包面订正**：适配器落在 `hosts/codex_cli/`（`events.py` / `transcript.py` /
  `install.py`），以 cli.py S1a 冻结的 `HOOK_HOSTS` 分派串
  （`mnemoseed_local.hosts.codex_cli.*`）为准——§4 表内 "`hosts/codex/`" 按切片简称
  读，cli.py 零触碰。测试 `tests/test_hosts_codex.py`（50 例）。
- **差分读形状**：transcript 事实源（`.jsonl.zst` 经 `common/zstd` lazy 解压、
  `.jsonl` 直读兜底；行 `ordinal` 取记录自带值，缺席时回退物理行号）。Stop/
  SessionEnd 批 = 末条 user message ordinal 之后的 message turn——append-only 转录
  下即纯 assistant 批（user turn 只由其 UserPromptSubmit firing 携带）；首 POST 的
  202 `ack_ordinal` 即本进程后续发送下界（≤ack 跳发，中途崩溃后下次 firing 经局部
  下界重推导 + ack 跳发天然 replay）。UserPromptSubmit 锚 = 转录中与 prompt 精确
  （strip 相等）匹配的最新 user turn；无匹配 ⇒ 事件不带 ordinal（仍摄取、不参与水
  位——诚实方向，水位偏低只多做去重、永不丢数据）。stdin 的 `last_assistant_message`
  不作兜底文本：转录即事实源，无 ordinal 的旁路文本是后续差分无法去重的静默双捕。
- **zstd 缺席**：诚实报错（无条件 stderr 一行，含 `[codex]` 安装指引），差分降级为
  空批；SessionEnd 的 settle 仍发。stdout 纪律与 claude_code/zcode 同构。
- **daemon 侧（§9.3 wire 落地）**：`IngestEvent.transcript_ordinal: int | None =
  Field(default=None, ge=0)`（additive，ge=0 钉住行号语义）；memory 服务 per-
  (effective profile, session) 高水位（in-memory、max 更新、复用 `_capture_lock`）；
  dedup 门在 provider_error 分派之后、segmenter 之前——≤ 水位的事件跳过入段、跳过
  capture latch 与 T2 scan，202 带 `ack_ordinal`（既有宿主 202 逐字节不变，测试断言
  键集）；水位只在 segmenter 接受后推进；`/session/end` 按 exact key + session 级
  sweep 清理，settled 会话的重放走 SessionSettledError 409（无复活路径）；
  provider_error 提名永不参与水位（该 lane 无 ordinal，其 202 保持原形）。
- **安装器**：`hooks.json` = Claude-Code 兼容顶层事件映射（探针实证插件形，四事件
  单 `type: command` handler）；marker = argv 尾契约（shim 形
  `_hook-event --host codex_cli` / 完整模块形 `-m mnemoseed_local.cli …`；命令含
  `-m` 但非完整模块尾 ⇒ 非 ours）。`config.toml` marker-block 文本手术：严格
  tomllib 拒改坏文件 → 精确 canonical 块替换/移除 → 重写后再解析验证 → 原子写
  （`common/config.write_text_atomic` 新增，`write_atomic` 收敛为同核——common 最小
  小补）；`[mcp_servers.mnemoseed]` 下携带本工具不管理的键 ⇒ 拒改并指引手工修复；
  `command` 一律 which().resolve() 绝对路径。
- **信任三态**：`[hooks.state.*].trusted_hash` 值集 vs sha256(hooks.json 字节)；
  无记录 ⇒ `installed, not yet trusted`；有记录无匹配 ⇒ `stale trust (hooks
  changed; re-trust via /hooks)`；匹配 ⇒ `installed`。边界如实记录：Codex 自身的
  hash 算法与 per-hook key 命名未获证据钉死（探针只录得键形状，值脱敏），本侧比较
  为保守近似——失配一律建议重信任（安全方向）；真实信任后若仍报 stale 属保守误报，
  只影响 status 文案，不阻塞捕获。
- **dispatch 测试重指**：`test_host_dispatch.py` 的"未实现宿主"覆盖改用 ghost
  binding（codex_cli 落地后 HOOK_HOSTS 内已无未实现宿主可指）。

### 9.8 S2b 对抗审记档（2026-10-24，QA 终判 CLOSABLE）

- **ack 低报方向的双防线**：`test_watermark_loss_replays_without_duplicates` 对 ack 谎报
  不敏感（恢复 firing 首 POST 走 dedup 门读真实水位，不经被谎报的 note 路径）；该方向由
  `test_legacy_202_body_stays_byte_identical` 的 ack==7 精确断言补位。两个 ack 源
  （note 推进回传 / 门读取）各有精确 oracle，防线整体成立。
- **zstd 真实解压路径入测**：zstandard 进 dev 依赖组（0.25.0），新增
  `test_zst_transcript_roundtrips_real_zstandard`（importorskip 护栏）补齐 QA IMPORTANT-1
  的"生产主捕获形态零实测"缺口；`[codex]` extras 语义不变。
- **NIT 记档（不随批修）**：① `install.py` `_trusted_hashes` 对 `tomllib.TOMLDecodeError`
  （`ConfigParseError` 非 ValueError 子类）未捕——实测死路径（前序短路）；② TOML span
  替换在"漂移重装"时可吞我们表头后的用户注释（首装与正常路径实证不动）；③ hooks/config
  双文件写非事务（先验证后写、hooks 先 config 后，半态窗极窄）；④ 混合 ordinal/缺失行
  回退物理行号在真实 rollout（ordinal==行号）自洽，人为背离时 floor 永久跳过（防御路径）。
