# 设计 · mnemoseed 四层架构与交付形态

> 依据：owner 2026-11-17 指令——区分核心 daemon / MCP / harness adapter / 管理界面四层；
> 当前内核以**单 agent profile**为目标，未来云端在 Profile 之上增加"组织/个人"一层，
> 内核需可整体移植，不提前引入多租户维度。
> 管理界面以**现有本地 HTML console** 为基础演进，数据面走 daemon REST。
> ChatGPT web 导入 v1 不做（逐条浏览器抓取不稳定且慢；要求用户导出是劝退要素），
> 未来管理界面成熟后再做 export → upload → 处理/同步/去重。
> daemon 生命周期：**不设 supervisor**，以 durable 状态 + 清晰降级换取健壮性。
> 语言：保持 Python，目标版本 3.14（理由见 §7）。理论锚：本文件为工程架构，无新增借用（§8）。

## 1. 四层所有权

| 层 | 拥有 | 不得拥有 | 现有模块 |
|---|---|---|---|
| L1 核心 daemon | 记忆服务（capture → dream → retrieve → decay）、stores、REST、配置写、凭据与鉴权、watchdog、生命周期 | host 事件映射、宿主配置改写、MCP 帧、UI 渲染 | `daemon/*`、`capture/*`、`dream/*`、`retrieve/*`、`decay/*`、`storage/*`、`schema/*`、`config.py`、`configwrite/*`、`secrets/*`、`credential.py`、`rest_client.py` |
| L2 MCP | JSON-RPC 2.0 帧、工具 schema、`isError`、actor/profile 头、无状态分发 | 记忆逻辑、宿主生命周期、UI 数据整形 | `mcp_gateway/{server,reliable_client,http_transport,instructions}.py`；HTTP `/mcp` handler 由 daemon 挂载 |
| L3 harness adapter | 宿主 hook/transcript → `IngestEvent`、ordinal、注入信封、宿主配置 install/status、host-agnostic 回填引擎 | 打分、记忆决策、直接 store 访问、UI | `hosts/common/*`、`hosts/<name>/{events,install}.py`、`hosts/opencode/plugin.ts`、`hosts/codex_cli/transcript.py`、`importer/{runner,ledger}.py` |
| L4 管理界面 | 渲染、交互、同源 REST 调用 | 业务规则、直接 store 访问、绕过 adapter 改宿主配置 | `console/static/{index.html,app.js,style.css}`，由 daemon 挂载并 cookie 鉴权 |

`cli.py` **不是第五层**，而是跨 L1/L3/L4 的**本地控制壳**：daemon 生命周期动词走 L1，adapter 调度走 L3，数据动词作为 REST 客户端，UI 启动指针走 L4。UI 不重复实现 CLI 解析；需要触发长任务时由 daemon 暴露 REST 任务端点。

## 2. adapter 契约

| 能力 | 最小函数集 |
|---|---|
| live hook | `<host>/events.py::transform_event(raw: str) -> int` + `HOOK_HOSTS` 注册；opencode 为 TS plugin |
| history backfill | `<host>/transcript.py` 导出 `DEFAULT_SOURCE` / `iter_rollouts(root)` / `read_session_meta(path)` / `read_turns(path)` / `validate_rollout(path)`，加一条 `IMPORT_SOURCES`；`importer/runner.py::load_source` 动态解析 |
| install/status | `<host>/install.py` 的 `install()` / `uninstall()` / `status(path=None, base_url=None)`，注册进 `ADAPTER_MODULES` |

当前覆盖：

| host | live | backfill | install/status |
|---|---|---|---|
| codex_cli | ✅ | ✅ | ✅ |
| claude_code | ✅ | ✅ | ✅ |
| claude_desktop | ❌（按设计仅 MCP） | ❌ | ✅ |
| opencode | ✅（TS） | ✅（SQLite） | ✅ |
| cursor | ✅ | ❌ | ✅ |
| zcode | ✅ | ❌ | ✅ |
| t3code | ❌（新 harness，仓库无此名） | ❌ | ❌ |

回填扩展的最小单位 = 一个 `transcript.py` + 一行 `IMPORT_SOURCES` + 测试，**不改 daemon、不加新路由**。
本机已确认 claude_code 的 transcript 是 `~/.claude/projects/**/*.jsonl`（行事件 JSON，无原生 ordinal，需合成）；opencode 的正文在 `~/.local/share/opencode/opencode.db`（SQLite，`mode=ro` 读 WAL，只用 `seq` 与 `text` 部件）。cursor / zcode 的本机聊天正文未找到，先探测再估工。

## 3. 内核边界：单 profile 内核可移植

- 当前只支持单 agent profile。`profile_id` 是内核的一等维度，但**多租户不是本阶段能力**。
- 未来云端在 Profile 之上加"组织/个人"一层，内核从本地 runtime 整体抬升；因此内核不得把"单用户/单机器"写进不变量。
- `host` / `session` 是一等归属维度，写进 stamp 与报告；不给它们加租户语义。
- **不设单写者锁**：MemPalace 的 per-palace 锁是嵌入式 HNSW 的 workaround，不是产品语义。当前 `DrainLane max_workers=1` 是嵌入式 store 的**实现约束**（物理写串行），逻辑 session 并行不受影响；cloud 化换事务型 store 即可。这条必须写进契约，防止被误读为"产品单写者"。

## 4. daemon 生命周期

**决定：不设 supervisor。** 依据：EverOS / MemPalace / mem0 三家本地优先记忆方案都无常驻 supervisor；OpenClaw / Hermes / DSH 属于"必须时刻在线接消息"的服务，形态不同。

为达成"没有 supervisor 也健壮"，需补齐三件事：

1. **PID 陈旧检测**：`endpoint.json` 带 PID；客户端连前验活，避免把 bearer token 发给端口复用进程。
2. **durable ingest 队列**：`/ingest` 的 turn 落 SQLite（不再只留内存），daemon 重启后 `recover_running` 重放——这是进程被杀时丢数据的根因。
3. **明确降级**：CLI/MCP 在 daemon 不可达时给清楚错误；hook 有界重试；不静默 202。

Windows 登录任务只修正确性（从配置读 base URL，不硬编码 7788），**不做进程内重生**。是否注册为系统服务（带可验证 boot）等管理界面落地后作为用户可选开关。

## 5. 管理界面边界

**数据面 = 现有 daemon loopback REST。** `console/static/app.js` 已同源 fetch，文档由 daemon 挂载、cookie 鉴权；`X-MnemoSeed-Actor` 区分 `cli|console|mcp`。

- 不把 MCP 当 UI 后端：工具 schema 面向稀疏 LLM 调用，actor 固定 `mcp`，无任务/进度/上传面。
- 不新开端口：保持单端口环回信任边界，避免重复凭据、CORS / DNS-rebinding 面。
- 演进顺序：先加导入任务/进度端点，未来再加 ChatGPT export 上传（有界 body、类型校验、verbatim 存储声明）。UI 必须由 daemon 提供，不能用 `file://`（会丢 cookie 鉴权）。

## 6. 一次性安装（setup）

目标：一个新用户一条命令把 runtime、daemon、MCP、adapter 全部装好并验证。Docker **不做**——mem0 用容器是因为要跑 Postgres + Neo4j 两个外部服务；我们是嵌入式 SQLite + Lance，单目录可复制可备份，容器只增重量。

`mnemoseed-local setup` 幂等流程：

1. **只读探测**：OS、已装 harness（`~/.codex`、`~/.claude`、`~/.config/opencode`、Claude Desktop 配置）、已有 home/profile、daemon 状态、缺的 extra（如 codex 的 zstandard）、端口占用。**绝不重建已有 home**。
2. **计划确认**：列出要装的组件和要接的 host，一次确认。
3. **逐步执行**：每步调用现有动词；重跑时已完成步骤跳过。
4. **实际验证**：`host status` 每个 host + `/healthz` + 对每个 MCP 做 `initialize`/`tools/list` 握手，**断言工具确实在**。"包装好 ≠ MCP 连上"。
5. **报告就绪**：每 host 状态、必须用户手做的两步（Claude Desktop 偏好粘贴、Codex `/hooks` 批准）、下一步命令。

设计原则（借鉴各家）：
- 先探测后写；不覆盖已有配置（MemPalace / OpenClaw）。
- 步骤与提示词从 CLI 动态取（`mnemoseed-local instructions ...`），不硬编码进 skill（MemPalace）。
- 渐进能力：核心 capture/recall 不依赖 Ollama，dream 为 opt-in；`/health` 与 `status` 报能力，缺能力给清楚错误而非启动失败（EverOS）。
- `skills/mnemoseed/SKILL.md` 可承载引导协议，但 `Verbs:` 清单钉的是 MCP 工具，不得改成 CLI 动词。

## 7. 语言与运行时

**保持 Python。** 六家的实际选择：EverOS / MemPalace / mem0 / Hermes 的记忆内核都是 Python；OpenClaw / DSH 是 Node/TS（harness 形态，不是记忆内核）；Rust 只出现在热路径（向量引擎本身、MemPalace 的 `mempalace-native`）。我们 172 源文件 / 4.9 万行 / 5000+ 测试的 Python 内核，整体重写 Rust 无产品收益；Node.js 也不会更 compact（runtime + node_modules）。"compact" 靠打包解决（`uv tool install` 隔离环境，必要时 PyInstaller/Nuitka 单文件），不靠换语言。

**版本：3.12 → 3.14。**

- 当前：Python 3.12.13。`requires-python = ">=3.12"`，ruff `py312`，mypy `3.12`，CI 矩阵三处 `3.12`。
- CPython 官方状态（2026-11-17 查）：3.12 已进 **security-only**（EOL 2028-10），3.13 同；**3.14 / 3.15 为 bugfix 全支持**，EOL 分别 2030-10 / 2031-10；3.10 已 EOL（2026-10-01）。
- 3.15 暂不可升：本机原生依赖 wheel 只到 cp314——onnxruntime 1.32.0、zstandard 0.25.0、tokenizers 0.23.3 均无 cp315；numpy 2.5.5 有 cp315。等这些上游补齐再评估 3.15。
- CVE 视角：3.12 仍在安全补丁期内，不是紧急事故；真正的暴露面是 64 个依赖包（lancedb 固定 0.36.0、onnxruntime、numpy、pydantic-core 等）。因此升级批次同时引入依赖审计与锁文件刷新机制（CI 定期跑，而非只在 release 时）。
- 升级步骤：`requires-python` / ruff / mypy / CI 矩阵统一 3.14，`uv lock` 刷新，四门全绿后发版。不保留 3.12 源码回退窗口——ruff `py314` 会生成 PEP 758 `except A, B:` 语法，3.12 无法解析；回退只能在已发布制品层面做。

## 8. 理论锚

**无新增借用。** 本文件是分层、契约、打包与运行时工程决策，不引入认知科学规律；不改变 `docs/zh/design/REFERENCES.md` 已登记的锚。明确不借用："单写者 ≠ 记忆巩固"、"durable 队列 ≠ 睡眠重放"、"管理界面 ≠ 记忆增强"。

## 9. 执行顺序

1. 现有缺陷：#395（resume 吞轮次）、#392（GBK 控制台崩溃）、#393（codex_cli 安装不检查 zstandard）。
2. 内核健壮性：PID 陈旧检测 + durable ingest 队列 + 渐进 onboarding（#310 / #390）+ Python 3.14 升级与依赖审计。
3. 本地回填 reader：claude_code → opencode → cursor / zcode（各自先探测格式）。
4. 管理界面：现有 console + daemon REST 导入任务/进度端点。
5. 未来：组织/个人层抬升；系统服务注册作为可选开关；ChatGPT export 上传。
6. 新 harness t3code：单列规划，不与 zcode 混。

每一步独立可发；不得因后续步骤而暂停前面的发货。
