# PRD · Phase A3 交付包

> 依据：`docs/zh/design/mvp-design.md` v1.3（定稿）§6 Phase A3 + §4.5（宿主 hook）、§4.8/决策 8（荐档与模型 UX）、§2（核心 UX）。
> 基线：commit `27622aa`，复验 908 passed / 5 skipped（63.19s，2026-08-17）。
> 门禁：每任务 TDD；子代理从不提交；每任务完成后经对抗性 QA 验收，FAIL 打回重做。
> 执行顺序：**批次 1 = T1 + T5（并行，区域不相交：仓根脚本 vs cli.py）；批次 2 = T2；批次 3 = T3**（T2/T3 都向 cli.py 注册新 verb——build_parser/main 调度缝相邻，串行避免冲突）；**批次 4 = T4**（orchestrator 直接执行：ci.yml 触发器对齐 + 引用 T1 脚本的 dry-run smoke job）。收口整包单 commit（orchestrator）。

## 任务 T1 · 零依赖安装编排脚本

- 设计依据：§2 UX 第 1 条、§6 Phase A3。
- 范围：
  1. 仓根新增 `install.ps1`（Windows 入口，`irm ... | iex`）与 `install.sh`（Linux/macOS 入口，`curl | sh`）。两步入口均收敛同一编排序：
     a. 探测/安装 ollama（Windows：winget `Ollama.Ollama`，无 winget 则给出手动下载指引并退出码非零；Linux/macOS：官方 `curl -fsSL https://ollama.com/install.sh | sh`）；
     b. 探测/安装 uv（官方 `irm astral.sh/uv/install.ps1 | iex` / `curl -LsSf astral.sh/uv/install.sh | sh`；装完对当前进程补 PATH）；
     c. `uv tool install mnemoseed-local`（已装则 `uv tool upgrade`）；
     d. `mnemoseed-local init`（已初始化则跳过）；
     e. `mnemoseed-local doctor`（内含 T5 的硬件荐档行），报告荐档与模型名；
     f. **交互确认后** `ollama pull <model>`（荐档对应默认模型：standard→`qwen3.5:9b`、lite→`qwen3.5:4b`、advanced→官方 27B 标）；`-Yes`/`--yes` 跳确认；**绝不静默拉取**；
     g. 末次 doctor 复验 + 引导语（`mnemoseed-local up`，hook 安装提示）。
  2. `--dry-run` 模式：只打印将执行的步骤与探测结果，不产生任何副作用（脚本在 CI 可验证的唯一形态）。
  3. 幂等：每一步"已就绪则跳过"；任一外部命令失败给出单句原因 + 退出码非零；仅用系统自带命令（pwsh 5.1+ / POSIX sh + curl/wget），不引入新依赖。
  4. README 增补一行安装小节（英文，含两枚一行命令）。
- AC：
  - 两脚本 `--dry-run` 在干净机器与已就绪机器上输出正确的步骤计划（跳过语义正确）；
  - 语法自验：`pwsh -NoProfile` 解析通过、`sh -n` 通过（CI smoke 引用，见 T4）；
  - 交互确认是默认路径（无 `-Yes` 时拉模型前必问）；全量回归绿。

## 任务 T2 · OpenCode 宿主 hook 适配（首发）

- 设计依据：§4.5（摄取主通道 ①；会话生命周期必须映射；去重单元）、§6 Phase A3。
- 范围：
  1. 新子包 `src/mnemoseed_local/hosts/`：
     - `hosts/opencode/plugin.ts`（随包分发的 OpenCode 插件源码，wheel package data）；
     - `hosts/install.py`（安装/卸载/状态逻辑：解析 opencode 全局配置根（`OPENCODE_CONFIG_DIR` > XDG_CONFIG_HOME > `~/.config/opencode`，Windows 同路径约定），把插件写入 `~/.config/opencode/plugin/mnemoseed-local.ts`，幂等覆盖；uninstall 删除；status 报告 installed/not-installed + daemon 可达性）。
  2. 插件事件映射（fire-and-forget，全部错误吞掉仅 console.debug，绝不阻塞宿主会话）：
     - `chat.message`（用户消息钩子）→ `IngestEvent(user_prompt)`；
     - `message.updated` 且 role=assistant 且 `time.completed` 已置（经 client 拉 parts 文本）→ `assistant_message`（client 端按 (sessionID, messageID) LRU 抑重）；
     - `tool.execute.after` → `tool_use`（tool_name / input=args / output=result 文本化）；
     - `session.idle` / `session.error` / `session.deleted` → POST `/session/end`（**会话生命周期映射必需，缺它永不 drain**）；
     - `experimental.session.compacting` → POST `/flush`（pre-compact 救援；宿主事件缺席时安全跳过）；
     - 底座：`IngestEvent.host = "opencode"`；baseurl = env `MNEMOSEED_LOCAL_BASEURL` 或 `http://localhost:7788`；profile = env `MNEMOSEED_LOCAL_PROFILE_ID` 或 `default`；请求超时 ≤2s。
  3. CLI 新 verb：`mnemoseed-local hook install|uninstall|status`（本地操作，不走 daemon REST 写路径）。
  4. 跨通道去重兜底按设计拍板：hook 端尽力抑重（同事件不双发），跨通道 turn_range 不一致时**宁可重复摄入由 daemon 近重复检测吸收，也不丢**——本包不新增 daemon 侧逻辑。
- AC：
  - 插件线束 golden 测试：插件预期发出的各事件 JSON fixture 全部通过 `IngestEvent`/`SessionEndRequest`/`FlushRequest` pydantic 校验（线形钉死，防 TS 侧漂移）；
  - 安装/卸载/状态逻辑测试（临时 HOME 下幂等覆盖、status 三态）；
  - 插件源码静态契约测试（必须注册的事件键清单与端点映射钉死）；
  - 全量回归绿 + ruff/format/mypy 干净；真实 OpenCode 联调为人工验证项，收口记录留证。

## 任务 T3 · MCP 网关骨架

- 设计依据：§4.5（摄取通道 ③，备胎第二位；骨架表面 = stdio + recall/remember/dream_once）、§6 Phase A3。
- 范围：
  1. 新子包 `src/mnemoseed_local/mcp_gateway/`：手写最小 MCP stdio 服务器（JSON-RPC 2.0 行分隔，**零新依赖**，不引 mcp SDK）。方法面：`initialize`（protocolVersion + serverInfo + tools capability）、`notifications/initialized`（无响应）、`tools/list`、`tools/call`、`ping`、`notifications/cancelled`（忽略）、未知方法返回 `-32601`。
  2. 工具集（全部经 `DaemonClient` 走 daemon REST，actor = `mcp`——`_VALID_ACTORS` 已含）：
     - `recall(query, top_k?)` → POST `/memory/recall`；
     - `remember(text)` → POST `/memory/remember`；
     - `dream_once()` → POST `/memory/dream_once`；
     - 工具失败以协议错误/isError 结构化返回，绝不整死 stdin 循环；daemon 不可达返回明确 isError。
  3. CLI 新 verb：`mnemoseed-local mcp`（前台 stdio 循环；daemon 不可达时仍正常握手、仅在 tools/call 时报错）。
  4. 文档：PRD 内 + README 片段给 OpenCode `mcp` 配置样例（`{"type":"local","command":["mnemoseed-local","mcp"]}`）。
- AC：
  - 握手序列测试（initialize → initialized → tools/list 三工具齐备、inputSchema 合法）；
  - 三工具各一正一反用例（stub DaemonClient 注入：正例透传参数与返回、反例 daemon 不可达 → isError）；
  - 畸形行/未知方法/半截消息不崩循环；
  - 全量回归绿 + ruff/format/mypy 干净。

## 任务 T4 · .github 工作流对齐

- 设计依据：§6 Phase A3、§7.4（现状 = 主仓 v0.1.1 形态）。
- 范围：
  1. `ci.yml`：push/PR 触发分支改为 `[main]`（删除 development）；
  2. `ci.yml` 新增 `install-script-smoke` job（matrix：ubuntu-latest 跑 `sh -n install.sh && ./install.sh --dry-run`；windows-latest 跑 `install.ps1 -DryRun`）——消费 T1 产物，故排批次 4；
  3. `release.yml`：已满足 tag → PyPI trusted publishing，保持不动（PRD 仅核验记录）。
- AC：工作流 YAML 语法有效；触发器 diff 仅含 development 移除；smoke job 引用的脚本路径与 T1 交付物一致。

## 任务 T5 · 模型缺失 UX + doctor 硬件荐档

- 设计依据：§6 Phase A3（"模型缺失 UX：init/doctor 引导 + up 启动检查、缺失时报错附 `ollama pull` 提示，**绝不静默拉取**（复用 bge-m3 懒加载先例）"）、§4.8/决策 8（三档与荐档）。
- 范围：
  1. doctor 新增 **"dream model"** 检查：ollama 路由时比较配置 `model` 与 `GET /api/tags` 结果（名称规格化：允许 `name` 或 `name:latest` 等价），缺失 → FAIL 且 detail 附 `ollama pull <model>`；服务器不可达 → FAIL 附启动提示；非 ollama 路由跳过（与 ctx-window 检查同一先例）。
  2. doctor 新增 **"hardware tier"** 信息化检查（恒 ok=true）：探测总 RAM（Windows ctypes GlobalMemoryStatusEx / Linux /proc/meminfo / macOS sysctl，零新依赖）与 NVIDIA VRAM（`nvidia-smi` 存在时；缺席视为 0），输出 `recommended tier: X（VRAM y GB / RAM z GB）(current: dream.hardware_tier)`；推荐规则：VRAM ≥ 22GB → advanced；VRAM ≥ 7GB 或 RAM ≥ 30GB → standard；否则 lite。不一致仅提示，不 FAIL。
  3. `up` 启动检查：dream 路由为 ollama 时，run_server 前做模型存在性预检（复用 doctor 同一判定函数）；服务器不可达或模型缺失 → stderr 单句报错（附 `ollama pull <model>` 或启动 ollama 提示）退出码 1；**绝不静默拉取**；非 ollama 路由跳过。
  4. `init` 引导文案：写配置后追加三行 next-steps（doctor / `ollama pull qwen3.5:9b` / `up`）。
- AC：
  - doctor 三态用例（模型在位 ok / 缺失 FAIL 附 pull 提示 / 服务器不可达 FAIL 附启动提示；非 ollama 路由显式 skip）；名称规格化等价用例；
  - hardware tier 检查恒通过且报告推荐档与当前档（探测函数可注入 stub 覆盖 RAM/VRAM 三档分支）；
  - `up` 预检两反例（缺模型 / 服务器不可达）均为退出码 1 + stderr 提示，且断言全路径无对 `ollama pull` 的子进程调用；通过例放行；
  - init 输出含引导语；全量回归绿。

## 完成定义（整包）

- 五任务 QA 全过；`uv run pytest -q`、`ruff check`、`ruff format --check`、`mypy src` 干净；
- 脚本 `--dry-run` 人工走查留证；OpenCode 插件真实宿主联调为人工验证项（收口记录）；
- 单 commit 收口（orchestrator 执行）：`phase A3: install scripts, opencode hook adapter, MCP gateway skeleton, CI alignment, model-missing UX`。
