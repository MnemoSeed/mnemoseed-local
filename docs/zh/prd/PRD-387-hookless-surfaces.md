# PRD · hook-less 表面的记忆协议（#387）

> 依据：issue #387 + owner 2026-11-13 指令。Claude chat / ChatGPT web 无法装 hook 是平台限制，
> 不是我们的缺陷；但我们必须给出可被主动调用的 MCP 工具，以及一层能被注入到对话里的指令。
> 无 hook 的界面以"工具模式"为核心运作模式；有 hook 的 host 仍以 hook 为主，工具只是补充。
> 前置：#217（loopback HTTP MCP）、#272（host adapters）已在 `origin/main`；批分支从该点切。
> 设计评审：solution-architect 2026-11-13 **CLOSABLE-WITH-CONDITIONS**，条件已逐条落入本文（见 §9）。
> 理论锚：借用前瞻记忆 + 执行意图，详见 §8。

## 1. 目标（用户可感知）

一台已装 `mnemoseed-local` 的机器，新用户：

1. 跑 `mnemoseed-local host install --host claude_desktop` → 重启 Claude Desktop 后**开箱能 recall 到既有记忆**，不需要手写 JSON。
2. 在 Claude chat 问「我们上次决定的那套方案是什么」→ 模型先调 `recall` 再回答，而不是说"我没有记忆"。
3. 在 Claude chat 顺口说出一个持久决定（"我们以后都用 uv 不用 pip"）→ 记忆里留下一条 **captured** 条目，不是 pin。
4. 用户明说「记住这个」→ 留一条 **pin**，与 captured 可区分。
5. 完全无法写配置的界面（ChatGPT web）：`mnemoseed-local instructions --surface chatgpt` 直接输出可粘贴的块。
6. 有 hook 的 host（Codex / Claude Code / Cowork / opencode）行为**零变化**：注入块优先，模型不重复查。

## 2. 冻结设计

### D1 三层投递

| 层 | 机制 | 覆盖面 | 谁来写 |
| --- | --- | --- | --- |
| L1 协议层 | MCP `initialize.result.instructions` | 所有 MCP 客户端 | server 自己 |
| L2 技能层 | `skills/mnemoseed/SKILL.md` | 加载 skills 的界面（含 Claude chat） | 打包分发 |
| L3 粘贴层 | `mnemoseed-local instructions --surface …` | 任何界面 | 用户粘贴 |

L1 是主路径：MCP 规范把 `instructions` 定义为"给客户端的可选指令"（`InitializeResult` 可选字段，客户端 *MAY* 使用）。规范**没有**承诺客户端必须呈现它，因此 L1 是尽力而为的一层，L2/L3 是确定可达的一层。**任何文档、状态输出或验收都不得把 L1 生效当成保证**（见 §6）。

### D2 instructions 文本契约

- 单一常量产出于 `src/mnemoseed_local/mcp_gateway/instructions.py`；stdio 与 loopback HTTP 两条传输共用同一份字节（两者已共用 `handle_message`，由构造自动一致）。
- 上限 1800 字符（每次会话注入系统提示，必须短）。
- **工具名一律从 `TOOLS` 插值**，不在文本里写死。常量模块以模板 + 名称映射产出最终文本，因此"文本提到的工具必须真实存在"是结构性真值，而不是靠正则从自由文本里抽词。
- 必须表达四个执行意图：
  1. 会话里已有注入记忆块 → 它就是本会话的记忆通道，用它，且**不要**对同一主题重复 recall；
  2. 没有注入块 → 本客户端无捕获 hook，模型自己就是记忆通道：回答关于过往工作 / 已有决定 / 已声明偏好之前，用**用户自己的词**调 `recall`；每个主题一次足够；
  3. 没有注入块且用户陈述了持久决定、偏好或约束 → 用 `capture` 记下；**不得**记录模型自己的推测、草稿或推理；
  4. 用户明确要求"记住 / 固定" → `remember`（pin）；只有用户要求替换某条具体信念时才 `supersede`。
- 必须说明空结果的含义（没有相关记忆，直说并继续，不要猜）。
- 必须说明 pin 与 captured 的区分方式（沿用 `SKILL.md` 现有措辞，不新造名词）。
- **hook 前提必须条件化**：`initialize.instructions` 会到达每一个 MCP 客户端（含装了捕获 hook 的 host），因此规则 2/3 的前提写成「**没有注入记忆块，且该客户端无捕获 hook**」的条件式；文本本身不得无条件断言"本表面没有捕获 hook"，能力判断留给客户端自己。

### D3 hook-less 写入契约

新增 MCP 工具 `capture(text)`。**形状裁定：独立动词，不做成 `remember` 的参数**——动词本身就是指令层里的触发器（§8 借用 1），且每个工具的 description 是独立契约面，`capture` 的描述要内嵌"逐字引用用户原话、禁止记录模型推测"。

- REST：`POST /memory/remember` 增加可选 `capture: bool = false`。缺省行为逐字节不变（wire-compatible）。
- 协议面分动词，存储面共服务：`capture` 同样走 `MemoryService.remember`，只是 stamp 不同。

**stamp 逐字段裁定**（`memory.py:869-887` 现为显式 pin 的硬编码值）：

| 字段 | pin（不变） | captured | 理由 |
| --- | --- | --- | --- |
| `provenance.source` | `memory.remember` | `mcp-chat` | 沿用 `{host}-chat` 连字符约定（`capture/stamper.py` 的 `claude_code-chat`） |
| `explicit_pin` | true | false | 由 source 派生，不单独存 |
| `provenance.asserted_by` | `"user"` | `"user"` | 意图 3 本就是用户第一人称陈述；非 pin 性由 source 承载 |
| `model_id` | `"user"` | `"user"` | 同上 |
| `provenance.confidence` | `1.0` | `1.0` | 内容来源仍是用户陈述；模型转述风险由工具 description 的逐字引用要求约束 |
| `created` 事件 `actor` | `"user"` | 请求 actor（`mcp`） | 写入的执行者确实是模型经 MCP 调用 |
| `rules` | 传入值 | 传入值 | 驱动层已验证按身份 union，空 rules 不会抹掉存量 |

**必须在 `remember()` 加对称守卫**（当前守卫是不对称的）：现有 `memory.py:933` 只保证"pin 不覆盖 captured"，没有保证"captured 不覆盖 pin"。captured 命中既有 `EXPLICIT_PIN_SOURCE` 且非逐字重复时，今天会走到 supersede-in-place，用模型转述替换用户显式 pin 的 verbatim 正文。修正为：

> 只有在 **写入方与命中方都是显式 pin** 时才允许 supersede-in-place；其余近重复按分支落位：
>
> - **逐字相同**（identical wording）→ 只做强化（`decay_weight` 回弹，如 0.5 → 0.6）：不写 text，不追加 history 事件；
> - **语义冲突**（`CONFLICT`）→ 只置 `needs_reconcile` 裁决标志：pin 的 text / source / history / weight 一律不写；
> - 其余近重复 → `new_chunk`，不碰既有条目。
>
> 前两条都不是 `new_chunk`；supersede-in-place 因此仍是 pin-to-pin 独占。

该修正是本批的语义红线，必须有测试钉住两个方向；上述两个 carve-out 分支各有一条 `capture=True` 测试钉住。

**为什么不走 `/ingest`**：那需要发明 chat 的 `session_id` 与 turn 分段语义，会把节点级持久事实混进 `recent_sessions` / `session_windows` 的时间线。`HostId` 里也没有 chat 表面。

### D4 claude_desktop adapter

**路径解析（按目录存在性判定，不按文件存在性）**：

1. `MNEMOSEED_CLAUDE_DESKTOP_CONFIG` 显式覆盖（测试与 CI 用）；
2. 目录候选：
   - Windows MSIX：`%LOCALAPPDATA%\Claude-Data\<pkg>\`（`<pkg>` 目录存在即在候选内）；
   - Windows 经典：`%APPDATA%\Claude\`；macOS：`~/Library/Application Support/Claude/`；Linux：`~/.config/Claude/`；
3. **已带 `claude_desktop_config.json` 的一侧优先**：MSIX 侧存在但不带配置、经典侧带配置 → 取经典，反向同理；**两侧都带配置 → 拒绝并给人工指引**（指向 `MNEMOSEED_CLAUDE_DESKTOP_CONFIG`），不猜；
4. 两侧都不带配置 → MSIX 候选存在则取它，否则平台经典路径（创建目录）。

MSIX 目录下若存在**多个** `<pkg>` 目录：取配置文件 mtime 最新者；若其中多于一个已带 `claude_desktop_config.json`，**拒绝并给人工指引**，不猜。

只按文件存在性判定会在本机现有环境下写出一个应用永不读取的死文件（MSIX 包已装、配置文件尚未生成），install 会打印 applied、status 会报 installed，而真实表面永远不生效。

**可执行文件解析器**（三支，`args` 随支而定）：

- 冻结安装（`sys.frozen` 或 exe 形态）→ `command` = `sys.executable` 绝对路径，`args = ["mcp"]`；
- 非冻结但已安装（`uv tool install` / venv 布局）→ `command` = 解释器旁的 console script（Windows `mnemoseed-local.exe`，其它平台 `mnemoseed-local`），`args = ["mcp"]`；该脚本缺席时回退 `command = sys.executable` + `args = ["-m", "mnemoseed_local.cli", "mcp"]`。任何一支都**不得**写出 `python.exe mcp` 这种废命令；
- 开发树（dev-tree checkout）→ **拒绝 install** 并指引先用 CLI 安装形态；
- status 以 command + args **整对**与当前解析结果比较：旧的坏条目因此判 `stale`，重跑 install 即修复；开发树里没有可比的 command，非空条目如实报 `installed`（不新增第五种状态）；
- 含空格路径由 JSON 数组 args 天然承接，无需转义处理。

**合并语义**：

- 只写 `mcpServers.mnemoseed = {"command": <解析结果>, "args": <该支的网关参数>}`。`preferences` 及其它每个键保持原样。
- 已有的 `mcpServers.mnemoseed` 是用户数据：形状异常（非对象）→ **拒绝并给人工指引**，与 `mcpServers` 容器同一 `require_object` 形状、同一 `ConfigParseError` 退出路径，文件逐字节不动（status 对该形状报 `unreadable`，install 不得静默覆盖）。
- `write_atomic` 抛出任何 `OSError`/`PermissionError` → `host` 动词以 exit 1 干净失败、不打印 traceback；"先关闭 Claude Desktop"的指引仅当错误为落在配置文件路径上的 `PermissionError`（运行中的应用占用该文件）时附加，其余 OS 错误只回显自身那一行错误。
- 严格 JSON；解析失败或 `mcpServers` / `mcpServers.mnemoseed` 形状异常 → 拒绝并给人工指引，文件逐字节不动（复用 `claude_code` 的 `SettingsParseError` 模式）。
- 原子写：临时文件 + fsync + `os.replace`（复用 `hosts.common.config.write_atomic`）。
- install 输出必须提示**先关闭 Claude Desktop**：运行中的应用可能在退出时从内存快照重写配置，丢掉刚写入的条目。
- `uninstall` 只删 `mcpServers.mnemoseed`；`mcpServers` 因此变空则一并删除该键。
- `mcpServers.mnemoseed` 为 `null` 这类形状异常的条目 install 拒绝改写而 uninstall 直接删除，评审裁定该不对称可接受：条目本身不携带可保留语义，且容器层已保护其它服务器条目。

**status 四态**：`not-installed` / `installed` / `stale`（已配置但 command 与当前可执行文件不一致）/ `unreadable`（文件不可解析）。

`unreadable` 而不是 `differs`：共享 `HookStatus` 的 `differs` 已被定义为"installed but not the shipped bytes"，同一 dataclass 里一个状态名两种含义会漂移。顺带更新该 dataclass 的 docstring。

**注册**：进入 `ADAPTER_MODULES`，从而自动进入 `host install|uninstall|status`、卸载器的结构性探测与 `_hook-event` 选择表。

**已知诚实输出**：`host status` 的 JSON 会带 ingest_ack / replay / capture_evidence 观测。hook-less host 的 ingest_ack 永远为"no hook ingest since boot"，capture_evidence 永远扫不到 `cues.host == claude_desktop`。这些是**预期为 not-observed 的诚实输出，不是失败**，PRD 与测试都按此写。

**能力边界**：Claude Desktop 的 personal preferences 与 ChatGPT 的 custom instructions 都是**账号级、云端同步**，安装器写不了。install 末尾必须打印 L3 粘贴块——这是边界而非遗漏。

### D5 onboarding

`host install --host claude_desktop` 的输出就是唯一入口，必须同时给出五件事：

1. `mcp: applied`（或 up to date）
2. 先关闭、再重启 Claude Desktop
3. 可直接粘贴的指令块（L3 内容）
4. 一条可执行的验证话术：问一个只有记忆里才有答案的跨 harness 问题
5. 一句边界说明：聊天界面没有捕获 hook，记忆靠模型主动调用工具写入

## 3. 切片

| 切片 | 内容 | 依赖 |
| --- | --- | --- |
| S1 | `instructions.py`（模板 + 工具名插值）+ `initialize` 接入 | — |
| S2 | `capture` 工具 + `RememberRequest.capture` + 对称守卫 + stamp 裁定 | S1 文案引用它 |
| S3 | `claude_desktop` adapter（路径解析 / 可执行解析器 / 合并 / stale / uninstall / status） | — |
| S4 | `instructions --surface` CLI 子命令 + 安装器打印联动 + 文档 | S1, S3 |

**同批必须更新的既有契约钉**（否则 gates 会逼实现者机械改测试而不自知）：

1. `tests/test_mcp_gateway.py:110` `test_tools_list_has_exactly_the_six_tools_with_valid_schemas` → 七工具；
2. `tests/test_mcp_http_transport.py` 的六工具钉与 `TOOL_NAMES` 常量；
3. `skills/mnemoseed/SKILL.md` 的 `Verbs:` 清单加 `capture`，并理顺 `remember` 的"只在用户明确要求时调用"与 `capture` 的关系；
4. `mcp_gateway/server.py` 模块 docstring 的工具清单；
5. PRD-217 冻结条款"工具集零改动"的**有意修订**必须在 PR 正文显式声明，不得静默。

S1–S4 是一批；若 S3 在真实 MSIX 探针中发现新形态导致膨胀，S3 可独立拆为第二个 PR，S1/S2 不动。`.mcpb` 打包另起一批。

## 4. 验收标准

- AC1：`initialize` 在 stdio 与 `POST /mcp` 两条传输上返回**完全相同**的 `instructions` 字符串，长度 ≤ 1800 字符。
- AC2：instructions 的模板只引用 `TOOLS` 中存在的工具名（结构性断言，不做正则抽词）。
- AC3：`capture` 写入的条目 `explicit_pin=false`、source 为 `mcp-chat`、created 事件 actor 为 `mcp`；`remember` 写入的条目 `explicit_pin=true`、source 为 `memory.remember`、actor 为 `user`。
- AC4：`RememberRequest` 不带 `capture` 字段时，REST 行为与批前逐字节一致（回归测试）。
- AC5：`host install --host claude_desktop` 在一个含 `preferences` 的既有配置上，只新增 `mcpServers.mnemoseed`，其余键 JSON 语义相等。
- AC6：配置文件不是严格 JSON 时，install 以明确错误拒绝，文件 md5 不变。
- AC7：`host status --host claude_desktop` 在 command 指向另一个路径时报告 `stale`。
- AC8：`host uninstall --host claude_desktop` 后 `mcpServers.mnemoseed` 消失，其它 MCP 条目保留。
- AC9：`mnemoseed-local instructions --surface claude_chat|chatgpt|claude_code` 各自输出非空、互不相同的块，退出码 0。
- AC10（PR 必交证据）：把 L3 块贴进 Claude Desktop 的自定义偏好后，在 chat 里问一个只有记忆里才有答案的跨 harness 问题，回答引用了记忆内容。**输入是 L3 粘贴块，因此该验证的是 capture/recall 全链，不依赖 L1 是否被客户端呈现。**

**变异体可能穿过的缺口，必须补钉**：

- AC-M1：`tools/call name=capture` 的端到端网关分发测试（真 daemon：`capture → /memory/remember body 含 capture:true → outcome 合法`）。只把工具加进 `TOOLS` 而不接 `call_tool` 的 if 链会返回 `unknown tool`，而 AC1–AC9 全绿。
- AC-M2：capture 命中既有显式 pin 的行为判定（对称守卫，两个方向都钉）。
- AC-M3：`SKILL.md` 动词清单含 `capture`（L1/L2 一致性钉）。
- AC-M4：`stale` → 重跑 install 修复 command 的闭环。
- AC-M5：`unreadable` 与 `stale` 不互相误报。

**L1 生效性探针**（证据，不作验收门槛）：一次真机探针，记录 Claude Desktop 是否把 `initialize.instructions` 呈现给模型。遵守隔离纪律：不得占用 7788，不得触碰已安装运行时。

## 5. 不做什么

- 不做 ChatGPT web 的实时捕获：需要 Secure MCP Tunnel 与 Business/Enterprise 计划，成本与价值不匹配。
- 不做 hook-less 的自动转录：MCP 是工具调用协议，没有 turn 事件面，协议上做不到。
- 不改现有 hook 路径的任何行为。
- 不打包 Claude plugin（`.claude-plugin/plugin.json` + hooks）——下一批，因为它只在 Claude Code / Cowork 生效。

## 6. 风险、边界与禁语

- **L1 可能被客户端忽略**：规范只说客户端 *MAY* 使用 `instructions`，且行为版本相关。L3 兜底。
- **禁语清单**（文档、`host status`、README 一律不得出现）：「接入后模型会主动调用」「记忆已激活」「memory active」。status 只能报告配置结构正确。
- **capture 污染记忆**：只在无注入块时允许写入，且明令禁止记录模型自身的推测。
- **路径漂移**：`stale` 检测是本批唯一能自动发现"配置指向已删除的 venv"的机制；本机 Codex hooks 现在就指向一个开发目录，这个问题是真实的。
- **账号级偏好写不了**：Claude 的 personal preferences 与 ChatGPT 的 custom instructions 都在云端，只能粘贴。
- **重复 JSON 键**：原文件若含重复键，`json.loads` 保留末值，重写会静默合并。概率极低，文档写一句。

## 7. 门

`uv run pytest -q`、`ruff check`、`ruff format --check`、`mypy src` 全绿；closeout 另需独立对抗性 QA 无 BLOCKER。

## 8. 理论锚

**借用 1：前瞻记忆 + 执行意图**

- 来源：Gollwitzer, P. M. (1999). Implementation intentions: Strong effects of simple plans. *American Psychologist*, 54(7), 493–503；元分析 Gollwitzer & Sheeran (2006)，94 项独立研究，平均效应量 d ≈ 0.65。
- 已验证规律（对象是人）：把意图写成「如果出现情况 X，就执行动作 Y」的形式，比只表达目标或倾向显著提高实际执行率；线索越具体，效应越强。
- 设计规则：instructions 不写「你可以使用 recall 工具」，而写「当用户提到过去的决定、先前的工作或已声明的偏好时 → 先调 recall」。每条指令都是 情况→动作 的形状。
- **迁移限定**：该规律没有被验证于 LLM 指令遵循；把它迁移到模型提示是本设计的一个假设，不是已被实证的结论。设计规则本身（if-then 形状）仍然站得住，但不得援引该文献为 LLM 行为背书。

**借用 2：编码特异性（线索依赖性提取）**

- 来源：Tulving, E., & Thomson, D. M. (1973). Encoding specificity and retrieval processes in episodic memory. *Psychological Review*, 80(5), 352–373。
- 已验证规律：提取成功率取决于检索线索与编码时线索的重叠程度，而不只是目标内容本身。
- 设计规则：指令要求 recall 使用用户自己的词（专名、路径、issue 编号、决定原文），不得改写成模型的同义词。这与现有 `resume_query` 的锚标识符提取设计一致。

**未借用（明确排除）**：不引用「记忆需要重复巩固」「睡眠中重放」「左右脑分工」一类流行神经神话；不做任何未经实证的认知增强主张。

## 9. 评审记录

solution-architect 2026-11-13 裁定 **CLOSABLE-WITH-CONDITIONS**，条件与本批的落点：

| # | 条件 | 落点 |
| --- | --- | --- |
| 1 | capture 命中显式 pin 需对称守卫 | §D3 对称守卫段 + AC-M2 |
| 2 | captured 写入的 stamp 逐字段裁定 | §D3 stamp 表 |
| 3 | 路径回退按目录存在性，多 pkg 确定性规则 | §D4 路径解析 |
| 4 | 可执行解析器禁止写出 python.exe 命令 | §D4 可执行文件解析器 |
| 5 | 切片表补既有契约钉 | §3 五点 |
| 6 | AC10 改用 L3 粘贴块；L1 降为探针 | §4 AC10 + 探针段 |
| 7 | instructions 改为模板插值工具名 | §D2 + AC2 |
| 8 | `mcp-chat` 命名对齐、`unreadable` 词表、先关闭 Claude Desktop、§8 迁移限定、删 MCPB 未证引用、状态观测预期 | §D3 / §D4 / §8 / §6 |
