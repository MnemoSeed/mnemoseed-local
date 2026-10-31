# PRD-293：Cursor 宿主适配器设计（冻结）

日期：2026-10-31。状态：设计交付（C293-CURSOR-DESIGN，owner 已批 launch-plan-309 W3），待独立 QA 与主控验收；实现批次为 **C293-CURSOR-ADAPTER**（写面冻结见验收记录）。审查基线：`da6b959e3facf7a506f8ef205bcc32a3591bd22a`。范围为**仅设计**：本批不写代码、不改行为、不做适配器实现。Cursor 能力事实为 2026-10-31 本席直接抓取的官方文档（`docs.cursor.com` 308 至 `cursor.com/docs`；以页面 `*.md` 源抓取），每条引用附 URL 与访问日期；未验证处一律标 UNKNOWN，不猜测。

## 证据与结论边界

| 标记 | 来源与能够支持的结论 | 不能支持的结论 |
| --- | --- | --- |
| D：官方文档直读（2026-10-31） | [hooks](https://cursor.com/docs/hooks.md)、[mcp](https://cursor.com/docs/mcp.md)、[third-party-hooks](https://cursor.com/docs/reference/third-party-hooks.md)、[rules](https://cursor.com/docs/rules.md)、[plugins](https://cursor.com/docs/plugins.md)、[cli/overview](https://cursor.com/docs/cli/overview.md)、[cli/headless](https://cursor.com/docs/cli/headless.md)、[cli/configuration](https://cursor.com/docs/cli/reference/configuration.md)、[llms.txt 索引](https://cursor.com/docs/llms.txt)（均本轮抓取） | 文档是活页面、未锚定版本快照；不证明真实 app 实际触发 hooks、消费注入、完成 MCP `tools/call`——那些是 V305 的事 |
| S：源码检查 | 全部 file:line 绑定来自基线 `da6b959` 实际代码，本人逐点读过 | 未起 daemon、未跑测试、未装任何宿主（设计批红线） |
| U：UNKNOWN | 文档未钉死或无证据（见专节清单） | 不补写 PASS/FAIL |

无 H/C：设计批不运行 harness 或 CI。文档示例中的 `cursor_version` "1.7.2" 只是 schema 示例值，不构成能力版本钉。

## Cursor 能力事实（D 绑定，2026-10-31）

**hooks 面存在且完整**：`hooks.json` 支持项目级 `<project>/.cursor/hooks.json` 与用户级 `~/.cursor/hooks.json`（另有 Enterprise/MDM、Team 云分发、插件源；优先级 Enterprise → Team → Project → User，插件其后），形状 `{"version":1,"hooks":{"<event>":[{command, timeout, matcher, type: "command"|"prompt", loop_limit, failClosed}]}}`，配置文件被 watch、保存即重载。Agent hooks 十八个：`sessionStart`/`sessionEnd`/`preToolUse`/`postToolUse`/`postToolUseFailure`/`subagentStart`/`subagentStop`/`beforeShellExecution`/`afterShellExecution`/`beforeMCPExecution`/`afterMCPExecution`/`beforeReadFile`/`afterFileEdit`/`beforeSubmitPrompt`/`preCompact`/`stop`/`afterAgentResponse`/`afterAgentThought`；另有 Tab hooks 与 `workspaceOpen`。命令钩子经 stdio 收 JSON、回 JSON；退出码 0=成功、2=阻止（Claude 兼容）、其它=失败放行（fail-open 默认，`failClosed: true` 可翻转）；权限类钩子（`beforeShellExecution`/`beforeMCPExecution`/`beforeReadFile`/`beforeTabFileRead`/`subagentStart`/`preToolUse`）对非法 JSON 按阻止处理。官方 quickstart 钩子"读 stdin、exit 0、不打印"——**空 stdout 是文档背书的合法形状**。Windows 上桌面 app 经 PowerShell 执行命令并把 JSON 管入 stdin。

**公共输入 schema（全部 Agent hooks）**：`conversation_id`（跨 turn 稳定的会话 id）、`generation_id`、`model`/`model_id`/`model_params`、`hook_event_name`、`cursor_version`、`workspace_roots[]`（工作区根，通常一个）、`user_email`、`transcript_path`（transcripts 关闭时为 null）。**`cwd` 不是公共字段**：只在 `preToolUse`/`postToolUse`/`postToolUseFailure`/`beforeShellExecution` 的输入出现。事件特定字段：`sessionStart` 输入 `{session_id(=conversation_id), is_background_agent, composer_mode}`、输出 `{env, additional_context}`（**文档原文 fire-and-forget："the agent loop does not wait for or enforce a blocking response"**；`env` 设置后对该会话后续 hooks 可见）；`beforeSubmitPrompt` 输入 `{prompt, attachments}`、输出 `{continue, user_message}`（**无 additionalContext 通道**）；`postToolUse` 输入含 `tool_name`/`tool_input`/`tool_output`/`cwd`/`duration`、输出 `{updated_mcp_tool_output, additional_context}`；`afterAgentResponse` 输入 `{text}`；`stop` 输入 `{status: completed|aborted|error, loop_count}`、输出 `{followup_message}`（非空即被自动作为**下一条用户消息**提交）；`sessionEnd` 输入 `{session_id, reason: completed|aborted|error|window_close|user_close, duration_ms, is_background_agent, final_status, error_message}`、输出不使用。钩子环境变量：`CURSOR_PROJECT_DIR`/`CURSOR_VERSION`/`CURSOR_USER_EMAIL`/`CURSOR_TRANSCRIPT_PATH`/`CURSOR_CODE_REMOTE`/`CURSOR_PLUGIN_ROOT`/`CLAUDE_PROJECT_DIR`（别名）。

**MCP**：支持 `stdio`（`{command,args,env,envFile}`）、`SSE`、`Streamable HTTP`（`{url,headers}`）三种传输；配置在 `~/.cursor/mcp.json`（全局）与 `.cursor/mcp.json`（项目），`mcpServers` 键形；值支持 `${env:NAME}`/`${userHome}`/`${workspaceFolder}` 插值；Tools/Prompts/Resources/Roots/Elicitation/Apps 全部支持；工具默认需批准；**CLI "will automatically detect and respect your `mcp.json`"**（[cli/using](https://cursor.com/docs/cli/using.md)）。

**rules 与配置**：项目规则 `.cursor/rules/*.mdc`、AGENTS.md（根+嵌套）、User Rules 全局经 Customize（文件路径文档未钉）；CLI 配置 `~/.cursor/cli-config.json`（纯 JSON、无注释）与项目 `.cursor/cli.json`（仅 permissions），`CURSOR_CONFIG_DIR` 可覆盖。CLI 形态：`agent`（交互）、`agent -p`（headless）、`agent --resume`。

**第三方导入（关键交叉面）**：Cursor 在设置项 **Include Third-Party Plugins, Skills, and Other Configs（默认开启）** 下自动加载 Claude Code hooks（`.claude/settings.local.json` → `.claude/settings.json` → `~/.claude/settings.json`），名称映射 `UserPromptSubmit→beforeSubmitPrompt`、`SessionStart→sessionStart`、`SessionEnd→sessionEnd`、`PreToolUse→preToolUse`、`PostToolUse→postToolUse`、`Stop→stop`、`SubagentStop→subagentStop`、`PreCompact→preCompact`，且接受 Claude 嵌套 `hookSpecificOutput` 响应。**注意**：本仓 claude_code 安装器写入的 hooks 会被 Cursor 装载；文档未钉 Cursor 执行 Claude 格式钩子时的 stdin 字段方言是否翻译成 Claude 形状（U），而本仓 claude_code 归一化器要求每事件 `session_id`（Cursor 的 `beforeSubmitPrompt` 输入无此字段）——预期多数事件被丢弃（fail-open、exit 0、零 stdout），无害但属**必须实测的共存面**（V305）。

**UNKNOWN 清单（不补写）**：(1) `agent`/`agent -p` CLI 会话触发哪些 Agent hooks——hooks 页只钉 `workspaceOpen` "Runs in the Cursor desktop app and CLI"，CLI 文档页全文零提及 hooks；(2) `transcript_path` 指向文件的格式（转录差分不可设计）；(3) `sessionStart` 的 `additional_context` 在 fire-and-forget 语义下是否被可靠消费（输出字段有文档，等待语义无保证；Enterprise 侧另有 "Block until hooks are ready" 旋钮暗示首 prompt 前存在加载竞态）；(4) 会话**恢复**（resume 旧 conversation）是否触发 `sessionStart`（文档只钉 "when a new composer conversation is created"）；(5) `beforeSubmitPrompt` 空输出时的语义（本设计以显式 `{"continue": true}` 规避）；(6) Ask/Edit 等非 agent 模式是否触发 Agent hooks；(7) `hooks.json` 对 JSONC/注释的容忍度；(8) 桌面 app 受控启动的精确 argv（`cursor <path>` 未在已抓页面钉死）；(9) 钩子默认 timeout 值（"platform default" 未钉数值）。

## 共享核映射与诚实能力声明（计划 §3.2）

**直接复用（零改动）**：`hosts/common/daemon.py` 全部——`hook_client`/`resolve_base_url`/`profile_id`/`debug`/`POST_TIMEOUT_SECONDS=2.0`/`RECALL_PULL_TIMEOUT_SECONDS=0.3`（[daemon.py:27-58、180-188](../../../src/mnemoseed_local/hosts/common/daemon.py)）与绑定闸 `binding_gate`/`enforce_project_binding`（[daemon.py:103-173](../../../src/mnemoseed_local/hosts/common/daemon.py)）；`recall.py` 的 T1 读取与渲染（`pull_session_tails`/`session_start_block`/`build_t1_context`，[recall.py:154-351](../../../src/mnemoseed_local/hosts/common/recall.py)）；`fence.py` 围栏族；`config.py` 严格 JSON 手术（`load_object`/`write_atomic`，[config.py:18-63](../../../src/mnemoseed_local/hosts/common/config.py)）；`project_binding.py` 注册表。**schema 零改动**：`HostId.CURSOR = "cursor"` 已在 [turn.py:28](../../../src/mnemoseed_local/schema/turn.py) 预留，事件注释已预指 Cursor（turn.py:41-42）；`SessionEndRequest` 现成（turn.py:132-137）。**唯一共享面修改**：`ADAPTER_MODULES`（[hosts/install.py:242](../../../src/mnemoseed_local/hosts/install.py)）加一行 `"cursor"`——`HOOK_HOSTS` 与 `_hook-event --host` choices 均由它派生（[cli.py:1702-1712](../../../src/mnemoseed_local/cli.py)、:2076），**cli.py 零改动**。

**新增代码**：`hosts/cursor/events.py`（Cursor 方言归一化：snake_case 事件名、`conversation_id`/`workspace_roots` 字段提取、Cursor 平面响应构造）与 `hosts/cursor/install.py`（`hooks.json` + `mcp.json` 手术，沿 zcode 先例 [install.py:231-338](../../../src/mnemoseed_local/hosts/zcode/install.py)）。

**计划 §3.2 规则的适用（"若宿主未提供足够 cwd/MCP 生命周期信息，该宿主不得被 G2 宣称支持此绑定"）**：Cursor 在**每个** hook 的公共 schema 提供 `workspace_roots`（cwd 等价物，单根工作区即项目根），并在工具事件提供逐事件 `cwd`；MCP 生命周期信息充分（`mcp.json` 注册 + `beforeMCPExecution`/`afterMCPExecution` + CLI 同读 `mcp.json`）；会话生命周期有 `sessionStart`/`sessionEnd`（优于 ZCode 的无 SessionEnd）。**结论：Cursor 具备被 G2 宣称支持绑定的资格（eligibility），但资格不等于证据——宣称仍以 V305 实测通过为前提**（受控启动经 `project run --root <root> -- <启动 argv>` 注入 `MNEMOSEED_LOCAL_PROFILE_ID` 并由 hooks 继承；argv 精确形态与继承事实都是 V305 项）。

**共享核不适配 Cursor 之处（如实标记，不糊弄）**：

1. **T2 拉取通道无载体**：`beforeSubmitPrompt` 输出无 additionalContext，`pull_pending_recall` 与 `t2.build_t2_context` 本适配器**不接线**——四宿主中首个无中段被动注入的宿主。后果：user_prompt ingest 后 daemon 焦点槽保持 parked 直至 `sessionEnd` settle 清扫（既有 per-session 语义，量有界）；Cursor 上召回可达性 = T1 + MCP 工具显式调用。
2. **`payload.additional_context_payload` 不可复用**：它输出 Claude 嵌套 `hookSpecificOutput` 形状（[payload.py:8](../../../src/mnemoseed_local/hosts/common/payload.py)）；Cursor 原生钩子的文档形状是平面 `{"additional_context": ...}`。第三方导入页虽称接受嵌套格式，但那是 Claude 格式钩子的承诺，原生 `hooks.json` 钩子不得依赖未文档化的容忍——新建平面构造器，`payload.py` 不动。
3. **zcode 式 Stop 组合动作不迁移**：zcode/Claude 的 Stop = assistant 文本 ingest + flush（[zcode/events.py:184-198、264-268](../../../src/mnemoseed_local/hosts/zcode/events.py)）；Cursor `stop` 输入**无文本字段**（仅 `{status, loop_count}`），assistant 文本在更早的 `afterAgentResponse`。两通道拆开：`afterAgentResponse`→ingest、`stop`→仅 flush。
4. **会话身份字段不同**：`beforeSubmitPrompt` 无 `session_id`，归一化以公共 schema 的 `conversation_id` 为会话身份（`sessionStart` 文档钉 session_id == conversation_id）；现有四宿主无一读取 `conversation_id`/`workspace_roots`，方言提取是全新代码。
5. **无 prestate 侧车需求**：Cursor 无 `hooks.enabled` 总开关（ZCode 特有，[zcode/install.py:79-81](../../../src/mnemoseed_local/hosts/zcode/install.py)），卸载更简单，`status` 无 `disabled` 态。

## 冻结的设计决策

- **FD1 适配器形态：原生 hooks（用户级 `hooks.json`）+ stdio MCP 网关。** 备选否决：(a) 走第三方导入路径（零安装器，但 stdin 方言未钉、依赖默认开启的设置项、把本仓 claude_code 面与 Cursor 耦合——否）；(b) Cursor Plugins 打包（`.cursor-plugin/plugin.json` 可载 hooks，但面向 marketplace 分发与人工评审，本地单用户适配器不需要——否）；(c) MCP-only（捕获将依赖模型自觉调用工具，违背 capture-over-execute——否；MCP 只承担召回面）。
- **FD2 配置位置：仅用户级** `~/.cursor/hooks.json` + `~/.cursor/mcp.json`；**永不写项目 `.cursor/`**（项目 hooks 进版本控制，写入等于把适配器泄进用户仓库与他人 repo——捕获中立红线；绑定注册表同理，计划 §3.2"不写进用户项目"）；Enterprise/MDM/Team 分发面出范围。
- **FD3 订阅事件与映射**：`sessionStart`→T1 读（`POST /session/recent`，attempt-once 门=无状态进程本身）；`beforeSubmitPrompt`→`user_prompt` ingest；`postToolUse`→`tool_use` ingest（tool_output 截断帽沿 `MAX_TOOL_OUTPUT_CHARS`）；`afterAgentResponse`→`assistant_message` ingest（`model_id` 随公共 schema 可填）；`stop`→仅 `POST /flush`；`sessionEnd`→`POST /session/end` settle（`SessionEndRequest`，`reason` 无 wire 字段不转发，如实记录）。**不订阅**：`preToolUse`/权限族（阻止面风险、无捕获价值）、`beforeShellExecution`/`afterShellExecution`（Shell 内容已被 `postToolUse` 覆盖）、`postToolUseFailure`（`failure_type` 是 app 级 taxonomy，无 provider 命名空间→B1 provider_error 提名通道 v1 不接）、`beforeReadFile`（文件读取非会话 turn——捕获中立）、`subagentStart`/`subagentStop`（v1 最小面，归因留待需要时再议）、`preCompact`、Tab hooks、`workspaceOpen`。
- **FD4 T1/T2 走向**：T1 骑 `sessionStart` 的 `additional_context`（fire-and-forget 竞态如实入档，消费与否 V305 验证；若实测不消费，v1 **无替代通道**——诚实降级为 T1 不可用、召回仅剩 MCP）；T2 中段被动注入**不支持**（§共享核不适配 1）。否决备选：`postToolUse.additional_context` 当 T2 载体（仅在工具执行后触发、位置错——纯对话轮永不触发）；`stop.followup_message` 当注入（非空即自动提交用户消息——污染对话，捕获中立红线，绝对禁用）。`visibility` 信号只随 T1 块渲染（wire mode 权威不变）；逐 turn 捕获信号无载体，如实记录。
- **FD5 stdout 纪律（Cursor 版红线）**：stdout 仅允许两种载荷——`sessionStart` 已服务时打印平面 `{"additional_context": <T1 块>}`；`beforeSubmitPrompt` 每次打印 `{"continue": true}`（文档化显式放行，规避空输出未钉语义）；其余路径**零 stdout**（空 stdout 为 quickstart 背书合法形）。永不 exit 2、永不设 `failClosed`、畸形 stdin/未知事件/daemon 不可达一律 exit 0 落 stderr 调试道；每条目 `timeout: 10`（秒，ZCode HANDLER_TIMEOUT_MS=10000 同量级）；不设 matcher（全匹配）。
- **FD6 绑定闸**：单根工作区以 `workspace_roots[0]` 作 cwd 代理、工具事件优先真实 `cwd`；多根工作区→不喂 cwd→走闸的诚实路径（未绑定 manual / 已绑定拒绝）；profile 只经进程环境（`MNEMOSEED_LOCAL_PROFILE_ID`），**`mcp.json` env 块不得钉 profile**（计划 §3.2"网关配置不得将 profile 固定到另一个值"）。
- **FD7 安装/卸载/状态**：`hosts/cursor/install.py` 沿 zcode 先例——严格 JSON（`ConfigParseError` 拒改坏文件、原字节不动；JSONC 容忍度未知即不做容忍）、marker 契约=argv 尾精确匹配（`["_hook-event","--host","cursor"]` / 模块形，command 不比较）、原子写（temp+fsync+os.replace、UTF-8 免疫 locale）、`--dry-run`、幂等重装；install 同时 create-if-absent 写 `mcp.json` 的 `mnemoseed-local` stdio 网关条目（`mnemoseed-local mcp`，网关自取 daemon 凭据，**宿主配置不落密钥**；manual/外来条目不碰）；uninstall 恰移除 marker 匹配条目与结构匹配的 MCP 条目；status 态 = `not-installed/installed/partial/differs` + `/healthz` 探测。
- **FD8 归因**：`host="cursor"`（枚举现成）、provenance 前缀按 stamper 惯例 `cursor-chat`；无 agent 字段→`origin_agent` 恒 null（与 zcode/codex_cli 同界）。

## 理论锚

**无新增借用理论**（沿 PRD-272 §7 工程接线面先例）：本批是宿主适配工程，不引入新记忆机制或新行为理论。注入行为沿用已入册锚：B2.1 的 TA-1..6（含 TA-5 围栏净化）与 #273 的 R54/R55 可见性信号，语义不变。T2 无载体、T1 fire-and-forget 竞态都是**宿主接口事实**，不构成也不需要理论主张。**不借用清单**：不借认知负荷/注意力理论为注入预算背书（预算是 token 经济工程选择）；不借任何"记忆增强"流行神经说法为 hooks 注入宣传；不以 interface 能力差异推导任何记忆规律。

## 验收记录（C293-CURSOR-ADAPTER）

**写面冻结（提案，随批可微调但共享面必须保持）**：新增 `src/mnemoseed_local/hosts/cursor/{__init__.py,events.py,install.py}`、`tests/test_hosts_cursor.py`、`tests/fixtures/cursor_hook/{session_start,before_submit_prompt,post_tool_use,after_agent_response,stop,session_end}.json`；修改仅 `src/mnemoseed_local/hosts/install.py`（ADAPTER_MODULES 一行）；**不动**：cli.py、schema/、daemon/、mcp_gateway/、hosts/common/、payload.py、t2.py、其余四宿主。

**实现批必须证明**：(1) 六事件 golden fixture（键集白名单断言，沿 test_s0_host_contracts 先例）+ 映射表逐行测试（stop=仅 flush、sessionEnd=SessionEndRequest、`conversation_id` 作会话身份）；(2) 绑定代理测试（单根透传、多根走无 cwd 路径、绑定失配拒绝可见 stderr 且零 daemon 写）；(3) stdout 字节 oracle（`sessionStart` 仅服务时打印平面 JSON、`beforeSubmitPrompt` 恰为 `{"continue": true}`、其余零 stdout、畸形/daemon-down exit 0）；(4) 安装器手术（幂等、marker 精确、外来条目逐字节不动、`mcp.json` create-if-absent 与结构匹配移除、`ConfigParseError` 拒改、dry-run、status 四态）；(5) E2E：spare port + 隔离 `MNEMOSEED_LOCAL_HOME` + 假 `~/.cursor` 真守护进程全链（T1 读→捕获→flush→settle）；(6) 既有四宿主套件零修改全绿（共享面回归）；(7) 对抗自验（mutation：marker 移除、外来条目删除、stdout 泄漏、exit 2 泄漏各须击杀）。全 gates + 资深对抗 QA 无 BLOCKER 后方可收口。

**保持 open（V305 实宿主验证，不随本批关闭）**：桌面 app 与 CLI 的 hooks 实际触发矩阵；`sessionStart.additional_context` 是否真被消费；resume 是否触发 sessionStart；`transcript_path` 格式与为 null 的真实条件；Ask/Edit 模式触发面；**第三方导入共存**（不预设丢弃结果，逐项测量：Cursor 对 Claude 格式钩子的 stdin 方言是否翻译（UNKNOWN）决定存活面——claude_code 归一化仅在未知事件/缺 `session_id` 时丢弃（[claude_code/events.py:162-167](../../../src/mnemoseed_local/hosts/claude_code/events.py)），而 Cursor sessionStart 输入两字段皆有，方言兼容则该钩子真实开火并打印嵌套 `hookSpecificOutput` T1 回放（第三方文档称接受此形状）；四个映射事件逐一测"存活/丢弃"，并测是否出现 claude_code + cursor **双重 T1 回放块**；若出现阻止面，claude_code 侧加 `CURSOR_VERSION` 探测守卫属独立批，不得夹带）；受控启动 argv 与 profile 环境继承；`mcp.json` 热重载对本条目的生效；真实用户有用确认（V312）。

## 范围外（明确不做）

云 agent（用户级 hooks 不可用、会话边界不同、MCP 认证在 Cursor 服务器侧）；Team/Enterprise hooks 与 MCP 分发（dashboard/MDM/allowlist）；Cursor Plugins/marketplace 打包与发布；Tab hooks 与 `workspaceOpen` 自动化；远程/SSH 工作区（`CURSOR_CODE_REMOTE` 行为未验证）；rules/AGENTS.md 内容写入（任何记忆指引都不写入用户规则——捕获中立）；background agent 专属语义（`is_background_agent` 会话按普通会话捕获）；`agent -p` 流式输出对接；云同步与多端（主产品线愿景指针，本仓不动）。
