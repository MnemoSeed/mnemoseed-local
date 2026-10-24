# 调研文档 · ZCode 与 Codex 宿主捕获/注入扩展面 —— issue #272 前置可行性探针（T0）

> 性质：本机只读取证 + 官方文档网络调研（2026-10-24），untracked 草稿，不产生代码承诺；作为 issue #272（宿主适配器扩展）的前置可行性依据。
> 探针边界：**全程只读**——未启动 ZCode / Codex 任何宿主进程；未触碰 7788 端口与 `~/.mnemoseed-local` 安装 runtime；未修改本仓库已跟踪文件。`~/.codex/auth.json` 及一切凭据文件**未读取**；引用 config 时只引键名与结构，敏感值一律 `<REDACTED>`。
> 证据优先级：本机文件（路径+行为）＞宿主内置官方指南插件（zcode-guide，随 ZCode 分发的权威文档）＞官方站点文档。凡未实证处标 **needs-probe**。
> 对标先例：opencode（宿主持久化完整会话史 ⇒ 捕获可为派生视图，file-watch/tail 亦算可行捕获通道），见 `research-opencode-plugin-bundling.md`。

---

## 摘要：逐问结论表

| # | 宿主 | 问题 | VERDICT |
|---|------|------|---------|
| Q1 | ZCode | 会话事件钩子 | **FEASIBLE-hooks** —— 恰好 7 事件（`SessionStart`/`UserPromptSubmit`/`PreToolUse`/`PermissionRequest`/`PostToolUse`/`PostToolUseFailure`/`Stop`）；**无 `SessionEnd`**；config-file hooks 与 plugin hooks 双通道 |
| Q2 | ZCode | 上下文注入 | **FEASIBLE-additionalContext** —— hook stdout JSON 的 `additionalContext` 注入对话（claude_code 同款语义）；无 system.transform，T1 走 `AGENTS.md` 指令文件 |
| Q3 | ZCode | MCP 注册 | **FEASIBLE-mcp** —— `~/.zcode/cli/config.json` → `mcp.servers`（stdio + http/sse 双形态，会话启动自动连接）；插件 manifest `mcpServers` 字段原生捆绑 |
| Q4 | ZCode | 会话持久化 | **FEASIBLE-file-watch** —— `cli/db/db.sqlite` 关系型全史（`message` 按 sequence、`input_history` 用户输入）+ `cli/rollout/model-io-*.jsonl`（注意 **tail 截断**）+ `cli/agents/` 子代理产物 |
| Q5 | Codex | 钩子/notify | **FEASIBLE-hooks（+notify 有限）** —— hooks 事件全集比 ZCode 更全（含 `SessionEnd`/`SubagentStart`/`Interrupt`/`Pre`/`PostCompact`）；`notify` 仅 `agent-turn-complete` 且 Desktop app-server 模式忽略 |
| Q6 | Codex | 会话持久化 | **FEASIBLE-file-watch** —— `sessions/YYYY/MM/DD/rollout-*.jsonl.zst` 全事件流；`response_item payload.type=message` 含完整 user/assistant turn（实测） |
| Q7 | Codex | MCP 注册 | **FEASIBLE-mcp** —— `config.toml` `[mcp_servers.<name>]`（本机 stdio 实证 `command`/`args`/`env`/`startup_timeout_sec`）；当前版本亦支持 `url`（streamable HTTP） |

**两宿主均判定为可行适配目标**：ZCode 走 **hooks 型**适配器（对标 claude_code 形态，且可声明式 bundle）；Codex 走 **hooks + file-watch 双通道**型适配器。

---

## 1. ZCode（`C:\Users\Little Star\.zcode\`）

本机布局：`cli/`（config.json、plugins/、db/、rollout/、agents/、exec/、log/）、`v2/`（桌面端状态：setting.json、tasks-index.sqlite 等）、`plugin-workspace/`、`workspace/`。
权威本地文档：**zcode-guide 官方插件**（`C:\Users\Little Star\.zcode\cli\plugins\cache\zcode-plugins-official\zcode-guide\0.3.0\skills\`，含 `diagnosing-hooks` / `diagnosing-mcp` / `zcode-configuration-guide` 等 SKILL.md）。公开文档：https://zcode.z.ai/en/docs/hooks 、 https://zcode.z.ai/en/docs/plugin 。

### Q1 会话事件钩子 —— FEASIBLE-hooks

- **七事件硬清单**（`diagnosing-hooks/SKILL.md` L26）："Event names (exactly seven): `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `Stop`"。明确**不支持** `Notification`、`SubagentStop`、`PreCompact`（无 `SessionEnd`）。
- **两个注册通道**（`diagnosing-hooks/SKILL.md` §1）：
  1. **config-file hooks**：`~/.zcode/cli/config.json`（或工作区 `<repo>/.zcode/config.json` / `zcode.json`）顶层 `hooks` 键，形状 `{ enabled?, timeoutMs?, maxOutputBytes?, events: { <Event>: [{ matcher?, hooks: [...] }] } }`；**默认禁用，须 `hooks.enabled: true`**。
  2. **plugin hooks**：插件 `hooks/hooks.json` 或 manifest `hooks` 字段（`zcode-configuration-guide/SKILL.md` L33、L78）；任一插件贡献 hook 时 runner 自动启用。
- **hook 形态**：`type: "command"`（shell 串，`timeout` 秒）或 `type: "process"`（`command`+`args[]` 无 shell 向量，最可移植，`timeoutMs` 毫秒）；matcher 为大小写敏感正则（对 tool 名 / prompt 文本 / SessionStart 的 `startup|resume|clear|compact`）。
- **模板变量**：`${CLAUDE_SESSION_ID}`、`${ZCODE_PROJECT_DIR}`、`${CLAUDE_PROJECT_DIR}`；插件 hook 另有 `${CLAUDE_PLUGIN_ROOT}`/`${ZCODE_PLUGIN_ROOT}`（同值注入环境变量）。
- **本机先例**：已装 28 个插件 manifest（survey 全量）字段词表为 `name/version/description/author/license/skills/agents/commands/mcpServers/userConfig/ui/description_i18n` —— **无一使用 `hooks` 字段**。能力有文档、本机无消费者（我们是首个第三方先例的风险项，非阻塞）。
- 网络佐证：https://zcode.z.ai/en/docs/hooks （hook 面向"plugin developers…extra context, or policy enforcement"）。

### Q2 上下文注入 —— FEASIBLE-additionalContext

- **T2 注入**：hook stdout 按 JSON 解析，**`additionalContext` 注入对话**（`diagnosing-hooks/SKILL.md` L36："additionalContext is injected into the conversation"）；schema 严格（未知键即校验失败）；也可走 exit code（0 过 / 2 阻断）。与 claude_code 适配器的 additionalContext 通道**同语义**。
- **T1 注入**：无 system prompt transform 等价物；等价通道是 **AGENTS.md 指令文件**（user `~/.zcode/AGENTS.md` 先注入、workspace `<repo>/AGENTS.md` 后注入，`zcode-configuration-guide/SKILL.md` L39-47）——静态规则面，不适合动态 recall，动态注入走 hook。
- **记忆插件先例**（网络）：Hindsight 即用 `UserPromptSubmit` 注入 recall、`Stop` 存储当轮 —— https://hindsight.vectorize.io/guides/2026/07/17/guide-zcode-memory-with-hindsight 。与 mnemoseed-local 的捕获/注入模型逐点对应。
- 注入量约束：未见文档化的 additionalContext 大小上限（Codex 有 ~2500 token 上限，ZCode 未标）；needs-probe 实测。

### Q3 MCP 注册 —— FEASIBLE-mcp

- **用户/工作区配置**（`diagnosing-mcp/SKILL.md` §1-2）：`~/.zcode/cli/config.json` → `mcp.servers`；`<repo>/.zcode/config.json` → `mcp.servers`；fallback `~/.agents/mcp.json` / `<repo>/.agents/mcp.json`（顶层 `mcpServers` 键，仅同 scope 无 `.zcode` MCP 时读取）。
- **形态**：`stdio`（`command` 字符串 + `args[]` + `cwd`/`env`）与 `http`/`sse`（`url` + `headers`）双支持；`type` 省略时可由 `command`/`url` 推断；**schema 严格，未知键整条丢弃**；默认超时 30000ms；**所有 scope 会话启动即自动连接**（免手动信任）。
- **插件捆绑**：manifest `mcpServers` 字段原生支持（stdio 实例：android-emulator `{command,args,cwd,env}` 含 `${ZCODE_PLUGIN_DATA}`/`${user_config.*}` 模板；http 实例：image-search `{"type":"http","url":"${ZCODE_BASE_URL}/...","auth":{...},"timeoutMs":90000}`）；插件服务器命名空间化为 `plugin:<plugin>:<server>`。模板变量**仅插件面**展开，config-file 面不展开（用绝对路径）。
- **daemon 接入判定**：`{"type":"http","url":"http://localhost:7788/mcp"}` 写入 `mcp.servers` 属官方 canonical 形态（`diagnosing-mcp` §2 示例即 http+url+headers）——**强推断可行**；仅"localhost 无 auth 是否被桌面端管理面改写"需轻探针。stdio 网关（`mnemoseed-local mcp`）亦可作 fallback。

### Q4 会话持久化 —— FEASIBLE-file-watch（注意通道分层）

三层存储，能力不同：

1. **`cli/db/db.sqlite`（全史，捕获主通道）**：表 `session`(id/project_id/title/directory/…)、`message`(id, session_id, data, **sequence**)、`session_entry`、`session_input`(kind/delivery/payload/status…)、`input_history`(session_id, **text**, kind, attachments)、`turn_usage`(turn_id/trace_id/user_message_id/时间戳/计数)、`session_task_link`(父子会话=子代理树)、`todo`/`permission`/`tool_usage`/`model_usage`/`workflow_*`。**user/assistant turn 完整、按序、关系型**——捕获可作派生视图（SQLite WAL 只读轮询 `max(sequence)` 增量拉取；WAL 单写多读并发安全，轮询频率与只读连接细节 needs-probe）。
2. **`cli/rollout/model-io-session_<id>.jsonl`（每次模型调用一行）**：`{startedAt, completedAt, durationMs, requestId, attempt, model{modelId,providerId}, querySource, sessionId, turnId, traceId, type:"model_io", request{body{system,tools,max_tokens,…}, messages, messageOffset, messageCount, messagesKind}, response{text, toolCalls, reasoningText, usage, finishReason}}`。**实测截断**：样本 line0 `messageOffset=58, messageCount=122` 而落盘 messages 仅 64 条（`messagesKind:"tail"`，另有 `"delta"`）——**不可单独作全史**，只作增量提示/模型侧证据。
3. **`cli/agents/session_<id>/agent_<id>/{metadata.json, output.txt, task.output}`**：子代理会话产物（output.txt 即子代理转写）。

结论：**宿主持久化完整会话史 ⇒ 捕获可为派生视图**（满足 opencode 先例原则）；首选 db.sqlite 轮询，rollout JSONL 作低延迟增量信号。

---

## 2. Codex（`C:\Users\Little Star\.codex\`）

本机布局：`config.toml`（键结构取证如下，值脱敏）、`sessions/`+`archived_sessions/`+`session_index.jsonl`、`plugins/`（marketplace 缓存与 staging）、`app-server-daemon/`（桌面常驻）、`history.jsonl`、`version.json`（updater 知悉最新 0.160.0）。**auth.json 未读**。

### Q5 钩子/notify —— FEASIBLE-hooks（notify 有限）

**config.toml 键结构**（本机，全部键名、值脱敏）：顶层 `model` / `model_reasoning_effort` / `service_tier` / `notify`；`[marketplaces.*]`（source_type/source/last_updated/last_revision）；`[plugins."<name>@<marketplace>"] enabled`；`[mcp_servers.node_repl]` + `[mcp_servers.node_repl.env]`；`[projects.'<path>'] trust_level`；`[desktop]`（UI 偏好）；`[features] memories`；`[memories] generate_memories/use_memories`；**`[hooks.state."<plugin>:hooks/hooks.json:session_start:0:0"] trusted_hash`**；`[tui]` 等。

- **notify**：`notify = ["<REDACTED>", …]`（TOML 数组，本机 config.toml:18）。官方语义：外部程序在 Codex 发出事件时被调用，stdin 收 JSON；**当前仅 `agent-turn-complete` 一个事件**；**Desktop app-server 模式忽略 notify**（issue https://github.com/openai/codex/issues/13019 ）。⇒ 可作 turn 边界信号，不可作完整捕获/注入面。
- **hooks（主通道）**，官方 https://learn.chatgpt.com/docs/hooks ：
  - **位置**：`~/.codex/hooks.json`、`~/.codex/config.toml` 内联 `[hooks]`、`<repo>/.codex/hooks.json`（项目层须信任）、插件 `hooks/hooks.json` 或 `.codex-plugin/plugin.json` 的 `hooks` 字段、企业 managed（requirements.toml）。多层合并。
  - **事件全集**（比 ZCode 丰富）：turn 级 `PreToolUse`/`PermissionRequest`/`PostToolUse`/`PreCompact`/`PostCompact`/`UserPromptSubmit`/`SubagentStop`/`Stop`；生命周期 `SessionStart`/`SessionEnd`/`SubagentStart`；`Interrupt`。
  - **stdin JSON**：`session_id`、**`transcript_path`（直指 rollout 文件）**、`cwd`、`hook_event_name`、`model`、事件特定 `tool_name`/`tool_input`/`prompt`/`turn_id`。
  - **输出语义**：stdout 纯文本对 SessionStart/SubagentStart/UserPromptSubmit 变为 developer context；JSON 字段 `hookSpecificOutput.additionalContext`（约 2500 token 上限 `additionalContextLimit`）、`permissionDecision`（allow/deny/改写 `updatedInput`）；exit 2 阻断。
  - **信任门**：非 managed hooks须经 `/hooks` 审阅信任，**按内容 hash 键控**——与本机 `[hooks.state] trusted_hash` 完全互证（该 entry 即 aws-startup-advisor 插件 `session_start` hook 的信任记录，证明 hooks 系统在本机真实运行过）。
  - **开关**：`[features] hooks = false` 可禁（`codex_hooks` 为弃用别名）。
- **本机插件实证**：`plugins/.marketplace-plugin-source-staging/.../aws-startup-advisor/.codex-plugin/plugin.json` 含 `"hooks": "./com.anthropic.claude-code/hooks/hooks.json"` 与 `"mcpServers": "./.mcp.json"`；该 hooks.json 注册 `SessionStart`/`PostToolUse`/`Stop`/`SessionEnd` 四事件命令钩子（Claude Code 兼容格式，模板变量 `${CLAUDE_PLUGIN_ROOT}`）。⇒ **Codex 原生消费 Claude 式插件 bundle（hooks + .mcp.json + skills）**。

### Q6 会话持久化 —— FEASIBLE-file-watch

- **位置与索引**：`sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl.zst`；归档移入 `archived_sessions/`（扁平）；`session_index.jsonl` 每行 `{id, thread_name, updated_at}`。
- **事件形状**（样本实测，23 行）：每行 `{ordinal, timestamp, type, payload}`；type 直方图 `session_meta×1 / response_item×11 / event_msg×9 / turn_context×1 / world_state×1`。
  - `session_meta.payload`：`base_instructions, cli_version, context_window, cwd, history_mode, id, model_provider, originator, session_id, source, thread_source, timestamp`
  - `response_item.payload`：`{type: message|reasoning|function_call|function_call_output|tool_search_call|tool_search_output, role?, content?, id…}`；**message 的 role 实测含 `developer/user/user/assistant/assistant/user`，content 为 `[{type, text}]`** —— **user/assistant turn 完整可派生捕获**。
  - `turn_context.payload`：`model, cwd, turn_id, effort, sandbox_policy, workspace_roots, …`
  - `event_msg.payload`：`task_started / item_completed / token_count / turn_aborted` 等。
- **压缩细节**：`.zst`（zstd）。增量 tail 协议（帧追加 vs 重写）未探明——**needs-probe**；退化方案（mtime 轮询 + 整文件解压重读）成本可接受，且 hooks 的 `transcript_path` 可精准指向当前文件。

### Q7 MCP 注册 —— FEASIBLE-mcp

- **本机实证（stdio）**：`[mcp_servers.node_repl]` 含 `command` / `args` / `env_vars` / `startup_timeout_sec` + 子表 `[mcp_servers.node_repl.env]`（环境变量注入）。daemon 的 stdio 网关（`mnemoseed-local mcp`）与此形态**直接适配**。
- **HTTP（streamable）**：当前版本支持 `[mcp_servers.<name>] url = "…"`（早期 experimental，issue https://github.com/openai/codex/issues/3696 已落地；Desktop 显示层有 bug https://github.com/openai/codex/issues/15609 ——显示为 STDIO 但功能在）。daemon `/mcp` 以 `url = "http://localhost:7788/mcp"` 接入 = 可行（Desktop 显示瑕疵需注意）；**首选 stdio**。
- **插件面**：`.mcp.json`（Claude 式）随插件 bundle 分发（本机 aws-startup-advisor 实证）。

---

## 3. 适配器形态建议

| 宿主 | 形态 | 依据 |
|------|------|------|
| **ZCode** | **hooks 型**（对标 claude_code 适配器，而非 opencode 的 plugin-API 型）+ 插件 bundle | `.zcode-plugin/plugin.json` 一个 manifest 声明 `hooks` + `mcpServers`（+`skills`）——**原生声明式 bundle，整包安装/启停**（优于 opencode 需 config-hook 自注入的方案）。捕获：`UserPromptSubmit`/`PostToolUse`/`Stop` hooks POST daemon；注入：`UserPromptSubmit` hook 输出 `additionalContext`；兜底：db.sqlite 轮询派生视图。会话边界以 `Stop` + sequence 差分近似（无 `SessionEnd`）。 |
| **Codex** | **hooks + file-watch 双通道型** | hooks 事件最全（含 `SessionEnd`），stdin 自带 `session_id`+`transcript_path`——既是捕获信号又是注入点（`hookSpecificOutput.additionalContext`）；rollout `.jsonl.zst` file-watch 作兜底/回填/离线重放；MCP 走 `config.toml` `[mcp_servers.mnemoseed]`（stdio 网关首选）。分发可打包为 Codex 插件（`.codex-plugin/plugin.json` + hooks + `.mcp.json`），但**公共插件目录不收带 hooks 的插件**（community 论坛），本地/marketplace 分发无碍。 |

两宿主均满足"一个 bundle = MCP + hooks"（opencode 调研报告 §4 的缺口在这两宿主反而是原生能力）。

---

## 4. 风险与未决项（needs-probe 清单，供 T1）

1. **ZCode 无 `SessionEnd`**：会话结束捕获用 `Stop` 近似 + db.sqlite `session` 表状态差分；边界语义需实测。
2. **ZCode model-io tail 截断**（实测 offset=58/count=122 仅存 64 条）：全史必须走 db.sqlite；WAL 只读连接的并发安全与轮询开销需 T1 实测。
3. **ZCode hooks 本机零先例**：28 个已装 manifest 无一用 `hooks` 字段；我们可能首个踩坑（schema 严格、输出校验失败即弃）。
4. **ZCode config-file MCP 不展开模板变量** + schema 严格：安装器须写绝对路径、canonical 字段。
5. **Codex Desktop app-server 模式下 hooks 行为未验证**（本机即 Desktop 常驻 `app-server-daemon/`；官方文档只提云端受限与 notify 被忽略 #13019）——T1 必测。
6. **Codex hooks 信任门**：`/hooks` 交互信任按 hash 键控，变更需重信任；安装流程须引导用户过一次信任。
7. **Codex rollout .zst 增量读协议未知**：帧追加 or 整体重写；退化方案（轮询+解压重读）成本可接受。
8. **Codex mcp url 形态**在 Desktop 有显示 bug（#15609）；stdio 稳。
9. **注入量上限**：ZCode additionalContext 无文档化上限（Codex ~2500 token）；T1 实测。
10. 版本快照：ZCode 指南插件 0.3.0（app 版本未探测——未启动宿主）；Codex updater 知悉 0.160.0；官方文档检索日期 2026-10-24，后续版本可能漂移。

---

## 5. 探针边界声明（复述）

只读取证；未启动任何宿主；未触碰 7788 端口与 `~/.mnemoseed-local`；未读取 `~/.codex/auth.json` 及任何凭据；config 只引键名与结构、值 `<REDACTED>`；样本会话内容仅取字段形状与最小引句；本文件为 untracked 草稿，不 git add。

**主要来源**：本机 `~/.zcode`（zcode-guide SKILL.md × 3、28 manifest survey、db.sqlite 表结构只读、rollout 样本）；本机 `~/.codex`（config.toml 键结构、hooks.state、staging 插件 bundle、rollout 样本解压）；https://zcode.z.ai/en/docs/hooks 、 https://zcode.z.ai/en/docs/plugin 、 https://github.com/zai-org/zcode-plugins/blob/main/docs/PLUGIN_DEVELOPMENT.md 、 https://learn.chatgpt.com/docs/hooks 、 https://learn.chatgpt.com/docs/config-file/config-advanced 、 https://github.com/openai/codex/issues/13019 、 https://github.com/openai/codex/issues/3696 、 https://github.com/openai/codex/issues/15609 、 https://hindsight.vectorize.io/guides/2026/07/17/guide-zcode-memory-with-hindsight 。
