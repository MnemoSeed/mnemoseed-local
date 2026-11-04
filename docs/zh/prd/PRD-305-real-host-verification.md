# PRD-305：真实宿主验证（V305-ONE，Windows 11 第一波）

日期：2026-10-31。状态：V305-ONE 执行完毕，待独立 QA 与主控验收；V305-TWO（2026-11-03，第二轮）已追加执行完毕，记录见文末「第二轮（V305-TWO，2026-11-03）」一节。基线：`da6b959e3facf7a506f8ef205bcc32a3591bd22a`（分支 `docs/305-real-host-verification`）。本轮在真实 Windows 11 主机上以「真实宿主 CLI + 合成守护进程 + 隔离宿主目录」的方式执行捕获→回忆注入→MCP 工具调用全链路验证，只新增本文档，不改变产品代码、配置或默认值。

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

---

# 第二轮（V305-TWO，2026-11-03）

日期：2026-11-03。状态：V305-TWO 执行完毕，待独立 QA 与主控验收。基线：`74f82f1`（分支 `docs/305-verification-round-two`，worktree `.mnemoseeding/worktrees/w305b`，含 #337 修复 PR #341 的 V2 opencode 插件与 #286 修复 PR #342）。本轮沿用第一轮的协议、隔离策略与诚实边界规则，在同一台真实 Windows 11 主机上复测：OpenCode（#337 关闭证据）、模型消费格（本地 ollama 路线）、Cursor（新宿主调查）、ZCode（node 版本复测）、Codex `/hooks` 信任（隔离家复测）。除本文档外零仓库写入；第一轮临时树 `%LOCALAPPDATA%\Temp\w305-v305\` 原样保留未触碰。

## 证据与结论边界（第二轮增量）

检查点分层与第一轮一致：**安装 → 配置/握手 → 真实捕获 → 回忆注入 → MCP tools/call → 卸载/清理**；前一项通过从不代表后一项。本轮在注入格上新增一个更深的证据面：**模型输入面**——以 codex 官方 `RUST_LOG=trace` 捕获发往模型的真实请求载荷，直接判定注入字节是否进入模型输入（第一轮只能止步于 hook 输出面/守护进程面）。注入进模型输入 ≠ 模型答案引用了注入内容：后者是模型消费格的判定标准，本轮如实分开。

模型消费格的沙箱路线：不创建凭据、不花 owner 费用——本机已装 ollama 0.34.4 与多个本地模型（qwen3 0.6b/4b/8b、qwen3.5 4b/9b、gemma4 e4b/12b，模型 blob 已在盘，零下载），以 ollama 本地端点（11434）作为 codex 的模型提供方完成真实模型回合。OpenCode 侧的模型回合解析到其自带 `build` 托管提供方（与第一轮相同的宿主自然默认行为），未创建任何凭据。

## 机器与环境清单（第二轮，增量）

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows 11 Pro 25H2，build 10.0.26200（与第一轮同一主机） |
| codex-cli | 0.164.0（未变） |
| Claude Code | 2.1.310（未变） |
| OpenCode | v2.0.32（未变；本轮聚焦 V2 插件执行裁定） |
| 系统 Node | v20.12.2（复测仍 < 22.5，`node:sqlite` 仍 `ERR_UNKNOWN_BUILTIN_MODULE`） |
| ollama | 0.34.4（`%LOCALAPPDATA%\Programs\Ollama`，服务初始未运行；`OLLAMA_MODELS=E:\Ollama\models`，已装模型 55G blob，零下载） |
| Cursor | 3.24.18（`%LOCALAPPDATA%\Programs\cursor`；产品侧无 Cursor adapter，见 Cursor 格） |
| mnemoseed-local | 0.2.0（worktree `w305b` @ `74f82f1`，经 `uv run` 执行；`.venv` Python 3.12.13，uv 0.12.5） |
| 合成守护进程 | `mnemoseed-local up --host 127.0.0.1 --port 27789`（备用端口；7788/4096/27788 均未用作本轮端口） |
| 隔离目录 | `%LOCALAPPDATA%\Temp\w305-v305-r2\`：`mnemoseed-home`（守护进程家）、`codex-home`、`opencode-home`（OPENCODE_CONFIG_DIR 隔离家）、`opencode-data-home`（XDG_DATA_HOME 尝试隔离，见边界事件 2）、`cwd-opencode`、`cwd-codex`（各宿主会话工作目录）、`sessions\`（日志与探针脚本）、`ollama\`（空，serve 日志进 sessions） |
| 进程记录 | 本轮守护进程共 **4 次 boot**：boot1 PID 41696（27789，07:04:00–07:40:16，`/daemon/shutdown` 正常退出，uvicorn 行见 `sessions\daemon-up.log`）；**agent 自启 boot2 PID 37736**（7788，07:06:16 健康行，agent 前台 `up` 尝试，uvicorn 行留存于 `sessions\opencode-r2-a.err`，随其工具调用超时终止）；**agent 自启 boot3 PID 24224**（7788，07:07:38 健康行，agent 后台重试；PID 来自现场 netstat/Get-CimInstance，属现场观测）；boot4 PID 4932（27789，07:41:29 起，opencode MCP 探针轮，`/daemon/shutdown` 正常退出）。7788 在场窗口为 37736+24224 的合计跨度（约 07:06:16–07:10±，≈4 分钟，见边界事件 1）。ollama serve：`daemon-pid.txt` 中 `OLLAMA_PID=7552` 为 shell 包装进程号，实际服务进程为 PID 35680（`taskkill /F` 终止，端口 11434 随即释放） |

合成守护进程 `config.toml`（mnemoseed-home）与第一轮同构：`preset = "embedded"`、`baseurl = "http://127.0.0.1:27789"`、`[storage.embed] driver = "synthetic"`、`[storage.vector] dimensions = 64`、`[storage.graph.instances.isolated]`（显式临时绝对路径）、`[logging] requests = true`；运行中经产品热应用通道 `config set capture.idle_flush_sec 20`（版本 3000002）。

## 每宿主每检查点证据矩阵（第二轮）

| 宿主 | 安装 | 配置/握手 | 真实捕获 | 回忆注入 | MCP tools/call | 卸载/清理 |
| --- | --- | --- | --- | --- | --- | --- |
| Windows 11 / Codex CLI | PASS | PASS + 信任子格 BLOCKED（复测确认） | PASS（bypass 旗标下复现，注 1） | **PASS（模型输入面，本轮新证据）**；模型答案引用子格 BLOCKED | PASS（网关面，注 5；会话内工具调用未达成） | PASS |
| Windows 11 / OpenCode | PASS | **装载 PASS / 执行 FAIL**（模块加载但 setup 从未被调用，#337 未关闭） | FAIL | FAIL | PASS（手动注册，网关面，注 5） | PASS |
| Windows 11 / ZCode | 轮一 PASS 证据维持（未重装） | BLOCKED（node v20.12.2 复测，未变） | BLOCKED | BLOCKED | 轮一网关面 PASS 证据维持 | 轮一 PASS 证据维持 |
| Windows 11 / Cursor（PRD-292 十二格之外的新宿主行） | BLOCKED（产品无 adapter） | BLOCKED | BLOCKED | BLOCKED | BLOCKED | — |

注 1（bypass 旗标，与第一轮同义）：`--dangerously-bypass-hook-trust` 为 codex 官方逐次旗标，仅用于在无信任记录的隔离家中复现捕获链（本轮为模型消费格服务）；持久信任状态零改变，`/hooks` 手动信任步骤如实记为未完成（见 Codex 格）。注 5（MCP 通路诚实边界）与第一轮一致：两格 PASS 均为以该宿主注册的原样命令直接驱动 stdio 网关（JSON-RPC over stdin/stdout）所得，没有任何宿主 app 在真实会话内执行过 tools/call。

### Codex CLI（`CODEX_HOME` 隔离，0.164.0）

- **安装 PASS**：`host install --host codex_cli` 在隔离 `CODEX_HOME` 写入 `hooks.json`（SessionStart / UserPromptSubmit / Stop / SessionEnd 四事件，worktree venv shim 绝对路径）与 `config.toml`（`[mcp_servers.mnemoseed]` stdio 网关注册）。
- **配置/握手 PASS + 信任子格 BLOCKED（复测确认）**：`host status` = `installed, trust unverified`、`daemon: reachable (http://127.0.0.1:27789)`。真实 `codex exec --skip-git-repo-check -s read-only`（**不带** bypass 旗标，本地 ollama 模型正常完成回合，exit 0）两次：hook 零触发、守护进程计数零变化（`capture_ingest_count` 保持 0）——信任门槛行为与第一轮判定一致；owner 手动信任步骤仍未完成，子格如实记 BLOCKED。
- **真实捕获 PASS（bypass 旗标下复现，注 1）**：带旗标会话（`Note for later: the AnchorLight nightly batch runs use the tidepool-scheduler v2 config.`）日志记录 `hook: SessionStart` / `hook: UserPromptSubmit` / `hook: Stop` 及各自 Completed；`daemon.log` 对应 `POST /ingest -> 202`（boot1 全轮累计 16 次 ingest，hook actor 调用 32 次）；空闲 flush 后 `/session/recent` 读回该会话尾逐字：`user: Note for later: the AnchorLight nightly batch runs use the tidepool-scheduler v2 config.`，`source: "codex_cli-chat"`、`host: "codex_cli"`——verbatim 通道完好。
- **回忆注入 PASS（模型输入面，本轮新证据）**：`RUST_LOG=trace` 的真实会话中，发往模型的请求载荷（trace stderr 原文，`sessions\codex-r2-trace.err`）含 **4 条 developer 角色消息**，其中同时出现 **T1 会话启动回放块**（`[mnemoseed] replayed context from a previous session` + `<session-tail>`）**与 T2 会话中召回块**（`[mnemoseed] recalled related memories`）；T1 块内逐字包含先前会话尾的捕获行（`user: Note for later: the AnchorLight nightly batch runs use the tidepool-scheduler v2 config.`）——注入字节真实进入模型输入。同会话 `POST /session/recent -> 200`、`POST /session/recall-pending -> 200`（T2 焦点拉取被 hook 拉走并与 T1 块一并出现在载荷中）。
- **模型答案引用子格 BLOCKED（不捏造）**：四个已装本地模型在 codex 代理回路内均完成真实回合但无一忠实引用注入块——`qwen3:0.6b` 跑题长讷（sub-agent 幻想）、`qwen3:4b` 回复 `No function call needed`（推理文本显示其只复述 codex 工具 schema 上下文）、`qwen3.5:9b` 反问澄清问题、`gemma4:12b` 复述 codex 系统提示并在随后一次 `--json` 会话中触发 ollama 流断开（`stream disconnected before completion`）。注入进模型输入（host 面 PASS）与模型答案引用（消费格）是两个判定面，后者本轮 BLOCKED，原因精确记录为：沙箱内无凭据、无计费路线下，已装本地模型在 codex 代理回路内的指令遵循不足以产出可采信的引用；**从未声称 answer-use PASS**。
- **MCP tools/call PASS（网关面，注 5）**：以 codex `config.toml` 注册的原样命令驱动 stdio 网关：`initialize` 返回 `serverInfo mnemoseed-local 0.2.0`、`protocolVersion 2024-11-05`；`tools/list` 返回 6 工具（`dream_once / recall / recent_sessions / remember / session_windows / supersede`）；`tools/call remember` 返回 `{"outcome":"new_chunk","chunk_id":"50ae2096…"}`；`tools/call recall`（查询 `AnchorLight nightly batch`）命中含 `AnchorLight` 的条目且出处为 `codex_cli-chat`——跨通道召回真实发生。会话内 tools/call：本地模型从未发起对 mnemoseed 工具的真实调用（qwen3:4b 曾幻觉调用不存在的 `create_goal`，与 mnemoseed 无关，如实记录），故会话内格不声称。
- **卸载 PASS**：`host uninstall` 后 `host status` = `not-installed`（两次环境变量校验下的 status 探测均指向 27789 合成守护进程，无真实守护进程探测）。
- 本轮新增的 codex 0.164 配置事实（全部在隔离家内实测）：`wire_api = "chat"` 已被移除（报错指向 `wire_api = "responses"`）；`ollama` 是内建保留 provider id（不允许 `[model_providers.ollama]` 覆盖，但允许 `model_provider = "ollama"` 直接选用内建）；ollama 0.34.4 原生支持 `/v1/responses`；TOML 顶层键置于 `[mcp_servers.*]` 表头之后会被并入该表（本轮以文件顶部书写修正）。

### OpenCode（V2 插件 @ 74f82f1，2.0.32）—— #337 裁定

- **安装 PASS**：`host install --host opencode` 写入隔离 `OPENCODE_CONFIG_DIR` 下 `plugin\mnemoseed-local.ts`；**与 worktree `src/mnemoseed_local/hosts/opencode/plugin.ts` 逐字节相同**（`cmp` 实测）。V2 形态确认：`export default { id: "mnemoseed-local", setup(ctx) }`、`session.hook("prompt")` / `session.hook("context")` / `ctx.mcp.transform`；第一轮的三个 V1 事件名（`chat.message`、`chat.system.transform`、`tool.execute.after` 的 V1 用法）在文件中出现次数为 0。
- **配置/握手：装载 PASS / 执行 FAIL（#337 未关闭，裁定升级）**：
  - **装载 PASS（宿主服务日志摘录已保全）**：宿主服务日志（真实家 `~/.local/share/opencode/log/opencode.log`，env 缺口所在，见边界事件 2）已按本轮会话窗口**只读摘录**保全于证据树：`sessions\opencode-service-log-excerpts.log`（52 行；含来源路径、抽取方法与窗口标注，原文行未改动）。摘录显示五个真实 `opencode run` 会话（a/b/d/e/f，隔离 CLI 日志 `opencode-data-home\opencode\log\opencode.log` 的 5 条 `cli starting args=["run",…]` 行为界）中，加载器对目标文件逐一产生 `msg="loading plugin"` 且 entrypoint 解析正确——通道绑定：**shipped V2 插件经项目 `.opencode/plugin/` 目录扫描**（会话 b/d/e/f 窗口的 loading 行）；**B2.6 探针经项目 `.opencode/plugins/` 目录扫描**（会话 d 窗口，23:17:51.764Z）**与 `opencode.json` `plugin` 数组的包目录形态**（会话 f 窗口，23:19:37.397Z，entrypoint 解析到 `b26-pkg/index.ts`）。V2 目标文件在其被加载的各窗口均无 `failed to load plugin` 行。对照组：真实家中的 V1 插件副本在每个会话窗口稳定失败——`failed to load plugin ... PluginModule.LoadError: Plugin must export a default definition with an id and an effect or setup function. (cause: SchemaError(Expected object at ["default"]))`（摘录含 6 条该 WARN，err id 逐会话不同）——证明 2.0.32 加载器即 V2 形态校验器，且 V2 文件通过了该校验。
  - **执行 FAIL（B2.6 探针实测）**：仓库自带观察探针 `mnemoseed_b26_probe.ts`（`setup` 首行即写 JSONL 观测行，sink 经 `MNEMOSEED_LOCAL_DATA_DIR` 指向隔离守护进程家）经上述**两条通道**（`plugins/` 目录扫描、数组包目录）注册后运行真实会话（本地模型，exit 0），`probe-b26.jsonl` **不存在**——`setup(ctx)` 在 2.0.32 上从未被调用。随附佐证：shipped 插件五会话零效果——hook 零触发、`hook-debug.jsonl` **不存在**（`MNEMOSEED_LOCAL_DEBUG=1` 已武装）、守护进程零插件请求（opencode 会话窗口内 `capture_ingest_count` 为 0；boot1 最终值 16 全部来自 codex 会话）。会话 A 转写中模型自述「No mempalace MCP tools are in the catalog, so I'll pin the fact through the available mnemoseed-local memory…」——该行同时显示代理将 mnemoseed 工具命名空间视为可见（与真实家插件副本被加载的 env 缺口后果一致），故**目录观测不构成零效果的判据**；零效果裁定只立足于两个 sink 的不存在与守护进程面沉默。
  - **数组通道的文件路径形态被拒**：`opencode.json` `"plugin": ["./.opencode/plugins/mnemoseed_b26_probe.ts"]` 触发宿主警告 `configured plugin path must be a directory`（摘录在案，23:18:59.045Z）——数组条目要求包目录而非文件路径；该形态下无任何插件执行。
- **真实捕获 FAIL / 回忆注入 FAIL**：同根因（setup 未执行 → hook 未注册），全部会话 `capture_ingest_count` 无插件贡献。
- **MCP tools/call PASS（手动注册，网关面，注 5）**：在隔离家写入手动 `opencode.json` mcp 注册（与第一轮同法）后以注册命令驱动网关：`initialize` OK、`tools/list` 6 工具、`tools/call remember` 对同一文本返回 `{"outcome":"reinforced","chunk_id":"50ae2096…"}`（dedupe 强化路径为产品真实行为）、`tools/call recall` 执行但对该查询无命中（codex 注册命令的同网关探针在数分钟前已证明 recall 命中与跨通道出处，网关功能面无疑）。
- **卸载 PASS**：`host uninstall` 后 `host status` = `not-installed`。
- **第一轮结论修正（绑定保全工件）**：第一轮记「`opencode plugin list` 同时发现全局（OPENCODE_CONFIG_DIR 下）与项目级两份——宿主对 OPENCODE_CONFIG_DIR 的支持属实」。本轮复测已保全于 `sessions\opencode-pluginlist-rerun.txt`（QA 后补跑，两次）：第一次在 `OPENCODE_CONFIG_DIR` 指向隔离家时列出 5 条——真实家全局 2 条（`C:\Users\Little Star\.config\opencode\plugin[s]`）+ 项目级 3 条（cwd 的 `.opencode/` 下），**无一条来自隔离家**；第二次在把 V2 插件文件重新放回 `OPENCODE_CONFIG_DIR\plugin\` 后列出**同样的 5 条**——隔离家的插件文件在盘而列表仍不含它。即：2.0.32 在 Windows 上不遵守 `OPENCODE_CONFIG_DIR`（全局插件扫描固定解析 `~/.config/opencode`），项目级（cwd）发现可用；第一轮的两份条目更可能是「真实家全局 + 项目级复制件」的误归属。同时 `XDG_DATA_HOME` 仅对 CLI 日志生效，会话 db 与 auth 仍落真实家（见边界事件 2）。该修正不改变第一轮的捕获/注入 FAIL 结论，但把 #337 的修复定位从「事件名漂移」上移一层：**模块装载成功而 setup 从未被调用**，仅迁移事件名（PR #341）不可能关闭 #337。

### ZCode（复测）

- Node 复测：系统 Node 仍为 v20.12.2；`node -e "require('node:sqlite')"` 复现 `ERR_UNKNOWN_BUILTIN_MODULE`——`node:sqlite`（需 Node ≥ 22.5）仍不可用。本轮未执行 `zcode.cjs`、未触碰 `~/.zcode` 与桌面 app（本轮会话的宿主），第一轮的 bundle 调查结论（`ZCODE_DATA_BASE_DIR` 覆盖存在等）无变化。整行维持 BLOCKED。

### Cursor（PRD-292 十二格之外的新宿主行；本轮新调查）

- **宿主存在**：Cursor 编辑器 3.24.18（`%LOCALAPPDATA%\Programs\cursor`），CLI 入口 `resources\app\bin\cursor`。
- **产品侧 BLOCKED（精确原因）**：`74f82f1` 的产品**没有 Cursor adapter**——共享注册表 `ADAPTER_MODULES` 仅含 `claude_code / codex_cli / opencode / zcode`，`src/mnemoseed_local/hosts/cursor/` 不存在（任务简报所引 `src/mnemoseed_local/hosts/cursor/install.py` 在该基线不存在——简报假设与本基线代码现实不符，按边界如实记录而非即兴造 adapter）。宿主侧亦无可驱动面：CLI bundle 仅含打开器/隧道包装（`cursor`、`code-tunnel.exe`、`cursor-tunnel.exe`），无 headless 代理会话入口。故安装→握手→捕获全链 BLOCKED，属**产品缺口**而非验证缺口，移交主控分诊。

## 边界事件与偏差（第二轮，全部如实记录）

1. **agent 自启守护进程短暂占用 7788（本轮最重边界事件）**：第一个真实 `opencode run` 会话（会话 A）中，宿主代理（`build` 托管模型）自主决定"修复记忆守护进程"，经继承的 PATH 找到 worktree venv 的 `mnemoseed-local.exe` 并执行 `mnemoseed-local up`（默认端口 7788；`MNEMOSEED_LOCAL_HOME` 继承自本轮隔离环境 → 指向**临时家**，非真实家）。agent 的 `up` 共两次：前台尝试（boot2，PID 37736，`daemon.log` 07:06:16 健康行；uvicorn 启动行留存于 `sessions\opencode-r2-a.err`）随其工具调用超时终止；后台重试（boot3，PID 24224，`daemon.log` 07:07:38 健康行；PID 来自现场 netstat/Get-CimInstance）在 `timeout 180` 终止 opencode 进程树时随之**自终止**（本人 `taskkill` 到达时进程已不存在，7788 随即释放）。7788 在场窗口为两次 boot 的合计跨度（07:06:16–07:10±，≈4 分钟；两次 boot 共写同一 `daemon.log`，请求行不含端口，只能以健康行 + 现场观测界定）。期间代理经 REST 直接 `POST /memory/remember -> 200`、`POST /memory/recall -> 200`（数据写入**临时家**存储，`source: "memory.remember"` 条目在 `/session/recent` 读回中可见）——**归因如实记录**：共享 `daemon.log` 无法判读这两次请求打到哪个 boot；按会话 env（代理 shell 继承 `MNEMOSEED_LOCAL_BASEURL=http://127.0.0.1:27789`）推断更可能由 boot1（27789 合成守护进程）应答，而非 agent 自己拉起的 7788 boot；该归因为推断，非日志直证。真实 `~/.mnemoseed-local` 全程零触碰（`daemon.log` mtime/size 前后一致，启动于本轮之前的进程仅为 owner 环境）。修复措施：后续 opencode 会话一律剥离子进程 PATH 中的 worktree venv（`mnemoseed-local` CLI 对代理不可见），未再复发。「never port 7788」的字面边界被本人动作链违反约 4 分钟，如实记录。
2. **opencode 真实面触碰（env 隔离不完整）**：2.0.32 在 Windows 上不完全遵守 `OPENCODE_CONFIG_DIR` / `XDG_CONFIG_HOME` / `XDG_DATA_HOME`：插件发现、会话 db 与 auth 解析仍落真实家（`~/.config/opencode`、`~/.local/share/opencode`），仅 CLI 日志遵守 `XDG_DATA_HOME`。后果：(a) 本轮 opencode 会话在真实 opencode 数据目录留下会话/日志写 touch（与第一轮同类，本轮补记）；(b) 每个会话都加载真实家插件副本（V1 副本每次加载失败，无执行副作用；`plugins\` 副本无副作用观测）；(c) 模型回合解析到 opencode 自带 `build` 托管提供方——环境凭据状态未检视（`auth.json` 属秘密文件，仅确立存在性），会话 A 代理在 180 秒超时前的 token 消耗无上界，其余四个会话（b/d/e/f）为单句提示的最小消耗。与第一轮同为宿主自然默认行为，本轮如实补记并收紧。
3. **`--dangerously-bypass-hook-trust`**：仅用于 codex 捕获链复现（模型消费格依赖），与第一轮同义同记；无旗标会话的零 hook 触发为本轮信任子格 BLOCKED 的实测证据。
4. **本地模型使用**：ollama serve 由本轮启动/停止（PID 35680，`taskkill /F`），零模型下载（全部已装），推理全程本地；opencode 会话走其自带 `build` 提供方（同第一轮）。
5. **MCP 探针脚本缺陷（本人工具，非产品）**：首轮探针对 `notifications/initialized` 通知读响应导致自阻塞；修正（通知不读响应）后重跑成功。无产品影响；缺陷版本脚本按原样留存于 `sessions\mcp_probe_r2.py`，修正式以 heredoc 运行未落盘（见验收记录未留存清单）。
6. **守护进程二次启动**：为补齐 opencode 注册命令的网关探针（与第一轮四宿主对齐），boot4（PID 4932，第二轮第四次 boot）仅服务该探针（`mcp` actor 2 次调用）后经 `/daemon/shutdown` 正常退出。
7. **隔离家内的 codex 配置试错**：`wire_api = "chat"` 被拒、保留 id 覆盖被拒、TOML 键位错误——三次失败均在隔离 `CODEX_HOME` 内、真实家零触碰，最终以文件顶部 `model`/`model_provider = "ollama"` 内建 provider 形态定型。

## 覆盖陈述（对 PRD-292 十二格矩阵，第二轮更新）

计数结构与第一轮一致，格内证据质量升级：

- **3/12 具备真实宿主链路证据**：Codex CLI 格本轮在 bypass 旗标下复现捕获链，并把注入证据从「守护进程面/hook 输出面」升级为「**模型输入面**」（trace 载荷原文）；模型答案引用子格仍 BLOCKED（原因从「无提供方登录态」变更为「沙箱内无可靠本地模型可在 codex 代理回路内产出引用」，同样如实不捏造）。Claude Code 格本轮未复测，第一轮全链证据维持。OpenCode 格本轮以**更深的 FAIL 证据**推进：装载成功而 setup 从未被调用（#337 的失败层级重新定位），安装/卸载/网关面 PASS 维持。
- **1/12 仅安装/卸载面**：ZCode 维持（node v20.12.2 复测未变，BLOCKED 原因不变）。
- **8/12 维持 0**：Ubuntu、macOS 全部八格（3 + 1 + 8 = 12）。Cursor 为十二格矩阵之外的**新增宿主行**，本轮整行 BLOCKED（产品无 adapter），不计入十二格算术。不补写 PASS。

## #337 关闭状态（对第一轮 done-when 的逐项裁定）

1. 「先判定插件模块在 2.0.32 中是否真实执行」——**本轮已用真实宿主证据判定**：模块被加载器发现、导入、通过 V2 默认导出 schema 校验（V1 文件对照失败），但 `setup(ctx)` 从未被调用（探针首行日志零输出：探针的两条注册通道 × 五个真实会话 a/b/d/e/f；服务日志摘录与两个 sink 的不存在均在证据树）。第一轮遗留的「模块执行未判定」问题关闭，裁定为「装载 PASS / 执行 FAIL」。
2. 「迁移 plugin.ts 到实测存在的事件/对象形状」——PR #341 已迁移且文件确为本轮基线内容，但**不足以关闭**：失败在加载器调用层，不在事件名层。
3. 「随后同法复测捕获与注入」——已按第一轮同法复测：FAIL（零捕获、零注入）。**#337 维持 OPEN**；修复须上移到「2.0.32 加载器为何不调用本地插件的 setup」层（含 `plugin` 数组包目录通道），修复后以本轮同款探针法复测。

## V305-THREE 仍欠

1. **#337 工程批次**：以「setup 从未被调用」为新起点定位 2.0.32 加载器行为（本轮已覆盖的注册面：shipped 插件的 `plugin/` 目录扫描、探针的 `plugins/` 目录扫描与 `opencode.json` 数组包目录；数组文件路径形态被宿主拒绝——均为待修复面），修复后用 B2.6 探针法（本轮同款）复测捕获/注入。
2. **模型答案引用格**：在 codex 代理回路内取得一个可采信的本地模型引用（更大参数量本地模型、或 owner 授权的沙箱化提供方登录态）；或以 Claude Code 格复刻「模型输入面 + 答案引用」双面证据。
3. **Codex `/hooks` 手动信任**：owner 在真实 app 中审阅信任后复测无旗标触发（本轮已实测无信任即零触发）。
4. **ZCode 实机会话**：Node ≥ 22.5 运行时或桌面 app 内 owner 手动会话。
5. **Cursor adapter**：产品缺口（`ADAPTER_MODULES` 无此宿主、无 `hosts/cursor/` 模块），需先立工程批次再谈验证；宿主 CLI 无 headless 会话入口，验证方法本身需设计。
6. **opencode env 隔离缺口**：`OPENCODE_CONFIG_DIR`/`XDG_CONFIG_HOME`/`XDG_DATA_HOME` 在 Windows 2.0.32 上被部分忽略——上游/宿主行为问题，影响后续所有 opencode 实机验证方法（第三轮需项目级隔离或接受并记录真实面触碰）。
7. **其余平台**：Ubuntu、macOS 八格维持 0。
8. **清理**：`%LOCALAPPDATA%\Temp\w305-v305-r2\`（守护进程家、隔离宿主家、会话日志、探针脚本、`daemon-pid.txt` 进程台账）与第一轮临时树一并由 owner 按需删除；本轮按边界未执行任何仓库外删除。

## 验收记录（第二轮，QA 修复轮后修订）

文档编写核对的证据分两类，如实分账：**(a) 证据树内保全件**（临时树 `sessions\` 与各隔离家）：`daemon-up.log`（本人两次 boot 的 uvicorn 行：41696、4932）、`daemon-pid.txt`（进程台账，含 OLLAMA_PID=7552 shell 包装号与 OLLAMA_KILL=35680 实际服务号）、`opencode-service-log-excerpts.log`（**宿主服务日志摘录**：来源为真实家 `~/.local/share/opencode/log/opencode.log`，按本轮五个会话窗口以 grep 只读抽取 52 行，含 loading plugin / failed to load plugin(PluginModule.LoadError SchemaError) / must-be-a-directory 原文行与来源、方法标注；真实家日志本体未改动、全量未保全）、`opencode-pluginlist-rerun.txt`（QA 后补跑的两次 `opencode plugin list` 输出，XDG_DATA_HOME 指向临时树故该补跑自身日志落证据树、真实家零写入）、`opencode-r2-{a,b,d,e,f}.{log,err}`、`codex-r2-nobypass.log`、`codex-r2-capture.log`、`codex-r2-answer{,2,3,4}.log`、`codex-r2-json{,2}.{log,err}`、`codex-r2-trace.{log,err}`（模型请求载荷原文所在，含 4 条 developer 消息）、`mcp-probe-r2-output.txt`（codex 注册命令网关探针）、`mcp-probe-r2-opencode.txt`（opencode 注册命令网关探针）、`r2-env.sh`（隔离 env 清单）、`mnemoseed-home\config.toml`、`codex-home\{hooks.json,config.toml}`、`opencode-data-home\opencode\log\opencode.log`（隔离 CLI 日志，5 条 run 调用为会话计数之界）。**关键否定性证据亦在盘**：`mnemoseed-home/probe-b26.jsonl` 与 `mnemoseed-home/hook-debug.jsonl` 均**不存在**（探针与 shipped 插件零执行的直接证据；opencode 会话窗口内 `capture_ingest_count` 为 0，boot1 最终值 16 全部来自 codex 会话）。**(b) 未以文件留存件**（仅存在于本轮会话记录或为真实家在场的本体）：宿主服务日志全量（真实家本体，本轮仅摘录保全）、QA 前的 `opencode plugin list` 输出（已被保全的补跑工件取代）、各 `host status` 输出、`/session/recent` 读回原文、`healthz` 快照、trace 的完整载荷转储（载荷原文的关键行在 `codex-r2-trace.err` 在盘）、**修正式 MCP 探针脚本**（以 heredoc 运行未落盘；在盘的 `mcp_probe_r2.py` 为含通知读响应缺陷的首版，如实留存）、agent 后台 boot3 的 uvicorn 启动行（PID 24224 来自现场 netstat/Get-CimInstance 观测）。独立实现 QA（已出 NOT CLOSABLE 判定与本修复轮）、完整本地 gates、本 PR CI、merge 与最终验收均待主控记录；#337 维持 OPEN，当前不声明 #305 完成。
