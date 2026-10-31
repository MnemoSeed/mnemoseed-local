# PRD-305：真实宿主验证（V305-ONE，Windows 11 第一波）

日期：2026-10-31。状态：V305-ONE 执行完毕，待独立 QA 与主控验收。基线：`da6b959e3facf7a506f8ef205bcc32a3591bd22a`（分支 `docs/305-real-host-verification`）。本轮在真实 Windows 11 主机上以「真实宿主 CLI + 合成守护进程 + 隔离宿主目录」的方式执行捕获→回忆注入→MCP 工具调用全链路验证，只新增本文档，不改变产品代码、配置或默认值。

## 证据与结论边界

检查点分层：**安装**（adapter 写入隔离宿主目录）→ **配置/握手**（真实宿主 app 加载配置并触发 hook）→ **真实捕获**（守护进程收到会话 ingest）→ **回忆注入**（T1 会话启动回放 / T2 会话中召回真正到达宿主上下文）→ **MCP tools/call**（宿主配置注册的 stdio 网关完成 initialize + tools/list + remember + recall）→ **卸载/清理**。前一项通过从不代表后一项；每格独立给出 PASS / BLOCKED / FAIL 与精确证据。

真实模型消费是单独一格语义：隔离宿主目录不含提供方登录态，codex 与 claude 的模型回合分别以 401 / "Not logged in" 结束。因此本轮的注入证据停留在 **hook 输出面（宿主真正接收到的 additionalContext 字节）与守护进程服务计数**，不声称"模型已消费注入内容"。

所有测试内容均为合成（AnchorLight / tidepool-scheduler / harbor-lake 为虚构工程名），不含任何私人身份、密钥或真实记忆。真实已装运行时（`~/.mnemoseed-local`，7788 端口常驻守护进程）除一次已披露的只读 `GET /healthz` 存活探测（见边界事件 3）外，本轮对其零写入、零数据读取、未停启；`~/.mnemoseed-local` 与全部真实宿主配置的**零写入**结论不受该探测影响。真实运行时仅作为环境背景记录。

## 机器与环境清单

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows 11 Pro 25H2，build 10.0.26200.8655 |
| codex-cli | 0.164.0（npm 安装） |
| Claude Code | 2.1.310（`claude --version`） |
| OpenCode | v2.0.32（npm `@opencode/cli`） |
| ZCode CLI 运行时 | 桌面应用内置 `zcode.cjs`；系统 Node v20.12.2（无 `node:sqlite`，见 ZCode 格） |
| mnemoseed-local | 0.2.0（worktree 源码 `da6b959`，经 `uv run` 执行） |
| Python | venv 3.12.13（uv 管理）；系统 Python 3.10.7（仅用于探针脚本） |
| uv | 0.12.5 |
| 合成守护进程 | `mnemoseed-local up --host 127.0.0.1 --port 27788`（备用端口，7788/4096 未用） |
| 隔离目录 | `%LOCALAPPDATA%\Temp\w305-v305\`：`mnemoseed-home`（首轮，保留）、`mnemoseed-home2`（主轮）、`codex-home`、`claude-home-root`、`opencode-home`、`zcode-root`、`cwd-*`（各宿主会话工作目录）、`sessions\`（日志与探针脚本）；空目录（创建后未使用）：`claude-home\`、`gateway\`、`zcode-base\` |
| 守护进程 PID | 12116（home 首次启动，启动失败退出）；4216（home，正常退出）；33696（home2，本轮全程，正常退出） |

隔离机制（全部为环境变量指向 + 合成其余一切）：守护进程与 CLI 用 `MNEMOSEED_LOCAL_HOME` 指向临时家；hook 用 `MNEMOSEED_LOCAL_BASEURL=http://127.0.0.1:27788` 指向合成守护进程；宿主目录分别用 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`（adapter 安装步以 `USERPROFILE` 重定向使 `Path.home()` 落入临时树）、`OPENCODE_CONFIG_DIR`、`ZCODE_DATA_BASE_DIR`（ZCode 桌面 bundle 实测解析 `ZCODE_DATA_BASE_DIR || HOME`，adapter 安装步同样以 `USERPROFILE` 重定向）。`PATH` 前置 worktree `.venv/Scripts`，使 `mnemoseed-local` 解析为 `da6b959` 的 worktree 代码而非已安装副本。

合成守护进程 `config.toml`（home2）：`preset = "embedded"`、`baseurl = "http://127.0.0.1:27788"`、`[storage.embed] driver = "synthetic"`、`[storage.vector] dimensions = 64`、`[storage.graph.instances.isolated]`（显式临时路径，避免 init 模板 `~/.mnemoseed-local/isolated.db` 的 `~` 展开触及真实家）、`[logging] requests = true`（每请求一行证据进 `daemon.log`）。运行中经产品自身热应用通道执行 `config set capture.idle_flush_sec 20`（版本 3000002），使空闲 flush 在本轮内可观察。

## 每宿主每检查点证据矩阵

| 宿主 | 安装 | 配置/握手 | 真实捕获 | 回忆注入 | MCP tools/call | 卸载/清理 |
| --- | --- | --- | --- | --- | --- | --- |
| Windows 11 / Codex CLI | PASS | PASS + BLOCKED（信任子格，见注 1） | PASS | PASS（守护进程面；模型消费 BLOCKED） | PASS（注 5） | PASS |
| Windows 11 / Claude Code | PASS | PASS | PASS | PASS（hook 输出面 + 守护进程面；模型消费 BLOCKED） | PASS（手动注册，注 5） | PASS |
| Windows 11 / OpenCode | PASS | FAIL（事件名漂移，#337，根因未完全判定） | FAIL | FAIL | PASS（手动注册，注 5） | PASS |
| Windows 11 / ZCode | PASS | BLOCKED | BLOCKED | BLOCKED | PASS（仅网关面，注 5） | PASS |

注 5（MCP 通路诚实边界，四宿主一致）：没有任何宿主 app 在真实会话内执行过 tools/call；四格 PASS 均为以该宿主配置注册的原样命令直接驱动 stdio 网关（JSON-RPC over stdin/stdout）所得。

### Codex CLI（`CODEX_HOME` 隔离）

- **安装 PASS**：`mnemoseed-local host install --host codex_cli` 在 `CODEX_HOME=%TEMP%\w305-v305\codex-home` 写入 `hooks.json`（SessionStart / UserPromptSubmit / Stop / SessionEnd 四事件，各一个 canonical handler，命令为 worktree venv shim 绝对路径 + `_hook-event --host codex_cli` argv 尾）与 `config.toml`（`[mcp_servers.mnemoseed]`，`command = <worktree shim 绝对路径>`，`args = ["mcp"]`）。
- **配置/握手 PASS + 信任子格 BLOCKED（注 1）**：`host status` = `installed, trust unverified`、`daemon: reachable (http://127.0.0.1:27788)`。真实 `codex exec --skip-git-repo-check -s read-only` 在临时目录启动真实会话（log 首部记录 session id 与 model 行），宿主已加载安装的配置。**信任子格 BLOCKED**：hooks 仅在逐次 `--dangerously-bypass-hook-trust`（codex 官方非交互旗标，仅当次生效，不改任何持久状态）下触发；未带该旗标时不触发。冻结验收要求信任前置未满足即记 BLOCKED——Codex `/hooks` 用户信任是真实 app 中的手动 owner 步骤，本轮从未自动化，也从未声称完成，故该子格如实记 BLOCKED。
- **真实捕获 PASS**：`codex-exec-3.log`（会话 01a1919d-c152…）记录 `hook: SessionStart` / `hook: UserPromptSubmit` 及各自 Completed；`daemon.log` 对应 `POST /session/recent -> 200`、`POST /ingest -> 202`；守护进程计数 `capture_ingest_count` 增至 4；`POST /session/recent` 读回该会话尾：`"user: Note for later: the AnchorLight nightly batch runs use the tidepool-scheduler v2 config."`，`source: "codex_cli-chat"`、`host: "codex_cli"`（逐字保留）。空闲 flush 后会话尾可见（`idle_flush_sec=20` 热应用后约 24 秒）。
- **回忆注入 PASS（守护进程面）**：第二条真实 codex 会话（`codex-exec-6.log`，提示词含 `tidepool-scheduler` 实体锚）`UserPromptSubmit` hook 触发后，守护进程计数 `recall_pending_served_count: 0 -> 1`、`recall_injection_count: 1`、`recall_injection_chars_total: 95`——T2 焦点扫描停靠并被 hook 真实拉走。模型消费格受阻：隔离 `CODEX_HOME` 无提供方登录态，`codex exec` 以 `401 Unauthorized`（api.openai.com）结束，助手回合从未完成；该子格记 BLOCKED（owner 登录步骤）。
- **MCP tools/call PASS（注 5：网关直驱，非宿主会话内调用）**：以 codex `config.toml` 注册的原样命令驱动 stdio 网关（JSON-RPC over stdin/stdout）：`initialize` 返回 `serverInfo mnemoseed-local 0.2.0`、`protocolVersion 2024-11-05`；`tools/list` 返回 `recall / remember / supersede / dream_once / recent_sessions / session_windows`；`tools/call remember` 返回 `{"outcome":"new_chunk","chunk_id":"4ca2c9a9…"}`；`tools/call recall` 原样返回该 pin（`explicit_pin: true`、`provenance_source: "memory.remember"`）。
- **卸载 PASS**：`host uninstall --host codex_cli` 后 `host status` = `not-installed`；`config.toml` 中 mnemoseed 表移除（文件清空）、`hooks.json` 中 `mnemoseed` 引用计数 0。真实宿主自身运行态（`sessions/`、各 sqlite）留在隔离目录内，未触碰真实 `~/.codex`。

### Claude Code（`CLAUDE_CONFIG_DIR` 隔离 + adapter 安装步 `USERPROFILE` 重定向）

- **安装 PASS**：`host install --host claude_code` 写入 `<隔离根>\.claude\settings.json`：7 个 hook 事件（SessionStart / UserPromptSubmit / Stop / PostToolUse / PreCompact / SessionEnd / SubagentStop），命令 `mnemoseed-local _hook-event --host claude_code`，`timeout: 10`；onboarding 输出如实声明 MCP 未由本 installer 注册。安装步以 `USERPROFILE=%TEMP%\w305-v305\claude-home-root` 运行使 `Path.home()` 落入临时树（实测打印确认），随后会话以 `CLAUDE_CONFIG_DIR=<同一 .claude>` 使真实 Claude Code 读取同一文件。
- **配置/握手 PASS**：真实 `claude -p "…" --output-format stream-json --verbose --include-hook-events --max-turns 1`（临时目录、`--max-turns 1` 限一回合）在流中输出 `hook_started` / `hook_response`（`SessionStart:startup`、`UserPromptSubmit`），hook stderr 显示 `binding: profile default unbound (legacy default entry; no project binding)`。
- **真实捕获 PASS**：会话 1 后守护进程计数 `capture_ingest_count: 4 -> 5`（claude 的 UserPromptSubmit ingest 到达）；后续会话继续递增（最终 6）。
- **回忆注入 PASS（hook 输出面 + 守护进程面）**：
  - T1：会话 1 的 `SessionStart` hook_response 携带 `hookSpecificOutput.additionalContext` = `<mnemoseed-memory-recall>` 块，逐字回放前一轮 **codex** 会话尾（`How is the tidepool-scheduler configured for AnchorLight nightly batches?` 等）——跨宿主 T1 回放真实发生。
  - T2：会话 2（提示词含实体锚）的 `UserPromptSubmit` hook_response 携带 `[mnemoseed] captured this session` + `<mnemoseed-memory-recall>` 召回块（`[mnemoseed] recalled related memories` + 相关会话尾）——该 stdout 即 Claude Code 注入模型上下文的字节。
  - 守护进程面：`recall_pending_served_count: 1 -> 2`、`recall_injection_count: 2`、`recall_injection_chars_total: 270`。计数语义：这两格只对 T2 拉取服务计数，T1 会话启动回放（SessionStart）不计入——因此 Claude 格呈现 T1+T2 两种注入而计数仅 1→2。
  - 模型消费格受阻：隔离 `CLAUDE_CONFIG_DIR` 无登录态，模型回合以 `Not logged in · Please run /login` 结束（`is_error: true`）；记 BLOCKED（owner 登录步骤）。
- **MCP tools/call PASS（手动注册；注 5：网关直驱，非宿主会话内调用）**：installer 不注册 MCP（hooks-only，PRD-292 静态结论在实机上复现）。以 `claude mcp add --scope user mnemoseed -- mnemoseed-local mcp` 写入隔离家（落点 `…\claude-home-root\.claude\.claude.json`，`File modified` 行为证），随后以注册的原样命令驱动网关：initialize / tools-list / remember（`new_chunk b7f8105a…`）/ recall（返回含 codex 捕获 chunk 的条目，`provenance_source: "codex_cli-chat"`——跨宿主召回）。
- **卸载 PASS**：`claude mcp remove --scope user mnemoseed` 移除手动注册；`host uninstall` 后 `host status` = `not-installed`；`settings.json` 恢复为 `{"hooks": {}}`（仅移除 marker 条目，容器保留——卸载器设计如此）。真实 `~/.claude` 未触碰。

### OpenCode（`OPENCODE_CONFIG_DIR` 隔离）

- **安装 PASS**：`host install --host opencode` 写入 `<隔离根>\plugin\mnemoseed-local.ts`；真实 `opencode plugin list` 同时发现全局（OPENCODE_CONFIG_DIR 下）与项目级（诊断用复制件）两份，`SOURCE` 列为绝对路径——宿主对 `OPENCODE_CONFIG_DIR` 的支持与插件发现机制均属实。
- **配置/握手 FAIL（#337）**：真实 `opencode run`（四个会话，其一 `--format json` 输出完整 step/text 事件，exit 0，模型 `build · inclusionai/ling-3.1-flash-1019` 正常回答）中插件**零触发**、零调试输出。已判定的部分：shipped `plugin.ts` 订阅 `chat.message`、`chat.system.transform`、`tool.execute.after`，而 opencode 2.0.32 二进制内这三个事件名出现次数为 **0**（`message.updated`、`message.part.updated`、`session.idle` 存在）。未判定的部分：插件还注册了通用总线 handler `event`（plugin.ts:2649，内部分派 `message.updated` / `session.idle`，:2514/:2523）与 `config` hook（:2635）——这些名字在 2.0.32 二进制中**存在**，真实会话中助手消息与配置加载必然发生，但该通路同样零触发、零输出（含 `MNEMOSEED_LOCAL_DEBUG=1` 的升级报错通道）。因此"事件名漂移"只解释三条命名 hook 的缺失，**不解释总线通路的沉默**：插件模块是否真的被执行、总线事件对象形状是否变化，均未判定。#337 的修复范围必须覆盖事件/对象形状与模块装载路径（先判定插件模块在 2.0.32 中是否执行），仅重命名三个 hook 名不足以关闭。
- **真实捕获 FAIL**：四个真实会话后 `capture_ingest_count` 保持不变，`daemon.log` 无任何来自插件的请求。
- **回忆注入 FAIL**：同一根因（`chat.system.transform` 不存在于 2.0.32），T1/T2 通路均不可能触发。
- **MCP tools/call PASS（手动注册；注 5：网关直驱，非宿主会话内调用）**：installer 提示 MCP 为手动步；在隔离家写入 `opencode.json`（`mcp.mnemoseed = {type: local, command: ["mnemoseed-local","mcp"], enabled: true}`）后以注册命令驱动网关：initialize / tools-list / remember（`new_chunk 05ae96ab…`）/ recall 通过。
- **卸载 PASS**：`host uninstall` 后 `host status` = `not-installed`，插件文件移除。

### ZCode（隔离 `USERPROFILE` 重定向；`ZCODE_DATA_BASE_DIR` 实测存在）

- **安装 PASS**：`host install --host zcode` 写入 `<隔离根>\.zcode\cli\config.json`：`hooks.enabled: true` + 四事件（SessionStart / UserPromptSubmit / PostToolUse / Stop）`type: process` handler（worktree shim + `_hook-event --host zcode` 保留 argv，`timeoutMs: 10000`）、`mcp.servers["mnemoseed-local"]`（stdio，`args: ["mcp"]`）、`mnemoseed-local.json` prestate sidecar。环境调查结论（读桌面 bundle，只读）：ZCode 数据根解析为 `ZCODE_DATA_BASE_DIR || HOME || homedir()` 下的 `.zcode`——env 覆盖**存在**，故本宿主未按"无覆盖"整行 BLOCKED，而是逐格如实记录。
- **配置/握手 BLOCKED**：ZCode CLI（`resources/glm/zcode.cjs`）以系统 Node v20.12.2 启动即 `ERR_UNKNOWN_BUILTIN_MODULE: node:sqlite`（需 Node ≥22.5）；机器无其他 Node/bun 运行时；唯一可运行 ZCode 的载体是桌面 GUI app——即本 ZCode 会话自身的宿主，依边界绝不触碰。初次调查（`zcode.cjs --help`）已在 `ZCODE_DATA_BASE_DIR` 指向临时目录的前提下进行，`~/.zcode` 全程零触碰。
- **真实捕获 BLOCKED / 回忆注入 BLOCKED**：同上（无可运行的宿主运行时）。
- **MCP tools/call PASS（仅网关面，注 5）**：以隔离 `config.json` 注册的原样命令驱动网关：initialize / tools-list / remember（`new_chunk 66b1c232…`）/ recall 通过。诚实边界：ZCode app 自身从未加载该注册，本格只证明注册内容的网关可用性。
- **卸载 PASS**：`host uninstall` 后 `config.json` 中 `hooks` 与 `mcp` 块整体移除、prestate sidecar 删除、`host status` = `not-installed`。

## 覆盖陈述（对 PRD-292 十二格矩阵）

PRD-292 记录真实宿主完整流程为 **0/12**（Windows 11、Ubuntu、macOS × 四宿主）。本轮推进其中 Windows 11 一行的四格：

- **0/12 → 3/12 具备真实宿主链路证据**：Codex CLI 与 Claude Code 两格取得"安装→握手→捕获→注入→MCP→卸载"全链路实测证据（受限子格如实标注：Codex 格带信任 BLOCKED 与模型消费 BLOCKED 两个子格，Claude 格带模型消费 BLOCKED 一个子格）；OpenCode 格以 **FAIL 证据**推进（安装/卸载/网关真实可用，#337 事件名漂移导致捕获/注入不可用——失败同样是真实宿主证据）。
- **1/12 仅安装/卸载面**：ZCode（会话面 BLOCKED，原因如上）。
- **8/12 维持 0**：Ubuntu、macOS 全部八格（3 + 1 + 8 = 12）。模型消费等受限子格是 Windows 已计数格内部的如实标注，不作为独立残留格重复计数。不补写 PASS。

## 边界事件与偏差（全部如实记录）

1. **首次启动失败**：home 首次 `up` 因缺 `[storage.graph.instances.isolated]` 在 lifespan 处 `RuntimeError` 退出（PID 12116）；修正配置后重启成功。init 模板的 `~` 展开会触及真实家——本轮改用显式临时绝对路径，未采用 init 模板。
2. **维度不匹配**：lancedb 默认 `dimensions = 1024` 与 `synthetic` embedder 的 64 不一致，`/memory/remember` 返回 500（`query dim(64) doesn't match … dim(1024)`）。以**全新 home2**（`[storage.vector] dimensions = 64`）重启解决；home1 数据**未删除、原地保留**（避免仓库外删除操作），两个家均在清单中登记。
3. **一次只读探测偏差**：opencode 卸载后的 `host status` 命令遗漏 `MNEMOSEED_LOCAL_BASEURL`，共享状态探测对真实守护进程 `http://localhost:7788` 发出一次 `GET /healthz`（无凭据、只读存活探测、无状态改变）。其余全部命令均在环境变量校验后执行。
4. **codex 出网 401**：`codex exec` 以无凭据的隔离家尝试连接 api.openai.com，全部 401——无任何真实模型调用完成，无计费可能；未创建任何密钥。
5. **未使用 `uninstall --purge`**：该动词在 Windows 上首先操作真实登录任务（`MnemoSeedLocalDaemon`），超出本轮边界，刻意避开；临时目录内未执行任何删除，全部清理经由 adapter 卸载与 `/daemon/shutdown` 完成。
6. **合成 embedder 偏差**：`[storage.embed] driver = "synthetic"`（仓库注册的确定性驱动，prd-08 D7）替代 bge-m3，避免 500MB 模型下载与网络依赖；焦点扫描为 embedding-free 实体锚通路、MCP 召回经混合检索在本轮全程可用。语义边界：64 维哈希向量上的 MCP recall PASS 只证明**通路可用性**，不代表真实 embedder 下的召回语义质量。若主控要求存储默认面完全对齐，V305-TWO 可以 bge-m3 复跑。
7. **`--dangerously-bypass-hook-trust`**：codex 官方逐次旗标，仅用于让 hook 在无信任记录的非交互会话中触发；持久信任状态零改变，`/hooks` 手动信任步骤如实记为未完成。
8. **ZCode 运行时调查**：仅以只读方式 grep 桌面 bundle 与 `resources/` 目录；`zcode.cjs` 唯一一次执行在 `ZCODE_DATA_BASE_DIR` 已指向临时目录后发生。

## 理论锚

本批为验证与文档批次，不引入新特性、新记忆机制，无新增借用理论；不以上一轮未验证的神经科学类比支持任何安装或接线结论。注入机制的理论锚（T1 会话启动回放 / T2 焦点扫描、budget 语义）已在 design/01 §4.6 与 PRD-B2.1 注册，本轮仅实测其宿主端到端行为。

## V305-TWO 仍欠

1. **模型消费格**：为 Codex CLI / Claude Code 提供沙箱化提供方登录态（owner 步骤或 owner 授权的测试凭据），验证注入内容真实进入模型回答——当前两宿主的该子格为 BLOCKED。
2. **OpenCode 事件名修复（#337）**：工程批次须先判定插件模块在 2.0.32 中是否真实执行（`event` 总线 handler 与 `config` hook 名两侧均存在却同样沉默），再将 `plugin.ts` 迁移到实测存在的事件/对象形状（`message.part.updated` / `session.idle` 等）；修复范围必须覆盖事件与对象形状路径，仅重命名三个 hook 名不足以关闭，随后在本轮同法复测捕获与注入。
3. **ZCode 实机会话**：Node ≥22.5 运行时或桌面 app 内的手动会话（owner 步骤）下的 hooks 触发、T1/T2 注入与信任流。
4. **Codex `/hooks` 手动信任**：owner 在真实 app 中审阅并信任 hooks 后，复测无 bypass 旗标的 hook 触发。
5. **bge-m3 对齐轮**（可选）：以存储默认 embedder 复跑注入链路。
6. **其余平台**：Ubuntu、macOS 的八格维持 0，待后续批次。
7. **清理**：`%LOCALAPPDATA%\Temp\w305-v305\` 临时树（含两个守护进程家、四个隔离宿主家、会话日志与探针脚本）由 owner 按需删除；本轮按边界未执行任何仓库外删除。

## 验收记录

文档编写已核对上述命令输出与守护进程日志；临时树 `sessions\` 实际留存的原文为：`daemon-up2.log`（home2 启动与运行日志）、`codex-exec-*.log`、`claude-*.log/.err`（stream-json 含 hook 事件）、`opencode-*.log`、`mcp_probe.py`（探针脚本）。**未**以文件形式留存（仅存在于本轮会话记录）：MCP 探针的 JSON-RPC 输出、各 `host status` 输出、守护进程计数快照、boot 1（PID 12116）崩溃回溯与 boot 2 启动日志（`daemon-up.log` 被第二次启动重定向覆盖）。独立实现 QA、完整本地 gates、本 PR CI、merge 与最终验收均待主控记录，当前不声明 #305 完成。
