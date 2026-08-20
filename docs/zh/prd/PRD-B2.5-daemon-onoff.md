# PRD · B2.5 daemon on/off 用户可控开关：记忆服务启停 + 持久禁用状态

> 依据：
> - 用户指令（2026-08-20，原文）："这个mnemoseed-local应该要有一个command能够直接关闭daemon，不让agent使用记忆功能，就是一个简单的on/off，可以直接控制是否打开服务。当然，安装后是默认打开，而用户随时可以关闭"。
> - 语义解析（architect 评审确认无误）：`off` = 关停运行中的 daemon **并持久禁用**（supervision / `up` 不得静默复活——用户不许可 agent 不得恢复记忆）；`on` = 解除禁用并启动；安装后**默认开**；两命令幂等、各完成完整状态迁移。
> - solution-architect 综合评审（2026-08-20）verdict：**SHIP-WITH-ADJUSTMENTS**（持久态机制从 config 注册键改为哨兵文件，watchdog disarm 时序钉死；其余按范围发船）。

## 理论锚：无借用

本批为工程控制面（进程生命周期启停、哨兵状态文件、优雅关停端点、诚实报错文案），**不借用任何神经科学/心理学规律**。不借清单：不开关即"遗忘"（off 不是记忆衰退语义）；不开关即"显著性"（on/off 不是注意力旋钮）。措辞纪律同 B2.3：任何"daemon 活性"表述均为工程语义，永不入认知词汇。

## 范围（批次任务）

- **T1 核心开关**：`daemon_state.py` 哨兵模块 + CLI `off`/`on` + `up` 早拒绝 + daemon `POST /daemon/shutdown`（respond-then-exit）+ `Watchdog.disarm()` + README。
- **T2 网关诚实**：`serve()` 启动读一次 disabled 态；refused-after-retry 文案在 disabled 时换成"已被用户关闭 / `mnemoseed-local on` 恢复"（纯增补，既有 pin 字节不动）。

## 设计定案（机制层；评审 issue 全并入）

### 命令面

- **平铺 `off` / `on`**（与既有平铺 `up` 对称；`config`/`dream`/`hook` 用子命令只因多子谓词，on/off 各一）。
- **`off`**：写哨兵文件（最先，写失败即 rc 1 诚实报错不再动作）→ best-effort `POST /daemon/shutdown`（`DaemonUnavailableError` 吞掉=已停）→ ≤15s 轮询等监听消失（超时如实报"may still be shutting down"）→ 报告 + rc 0。幂等：已停 + 哨兵已在 → "already off"，rc 0，哨兵重写。
- **探针与 POST 客户端时限**：存活探测用 1s 上限的探针客户端（`_probe_client`，`_OFF_PROBE_TIMEOUT_S = 1.0`）；shutdown POST 保持标准 30s 客户端。
- **意外异常宽捕获**：非 `Daemon*` 的 POST/轮询意外异常宽捕获 → stderr 指引 + **rc 1**；哨兵**保持**（状态已收敛）。
- **`on`**：删哨兵 → 若已在跑（healthz 可达）：报 "already on / already running"，**不重启**，rc 0；否则走 `cmd_up` 既有启动路径（前台阻塞）。
- **`up` 早拒绝**：`cmd_up` 最前闸见哨兵 → stderr `"error: memory service is disabled (run 'mnemoseed-local on' to re-enable)"` + **rc 1**，`run_server` 不触。**不给 `up --force`**（KISS：`on` 是唯一显式许可通道且自动启动；第二许可通道徒增面）。

### 关停端点

- `POST /daemon/shutdown`（挂在 app 路由，同 ingest/memory/configwrite 路由先例）：请求空；响应 `{"ok": true, "status": "shutting_down"}`（200）；audit `action="daemon_shutdown"`、`resolve_actor` 记档；daemon 本机 loopback（boot 绑定），不加额外面（config 路由的 `_reject_remote_writes` 是那里的纵深，此处 KISS 省）。
- **respond-then-exit**：handler 立即 200，后台 `asyncio.create_task` 在响应冲刷后调注入的 shutdown hook（仓内无此先例，新模式按最小安全形态定案）。
- **无 seam 降级**：`app.state.shutdown_hook` 缺席（TestClient boot，永不 arm watchdog）→ **503** 明文；端点可测性由此保住。
- **run_server 注入**（`runner.py`，建完 server 与 watchdog、跑 `server.run()` 前）：
```
def _intentional_shutdown() -> None:
    watchdog.disarm()          # ① 必须先于监听关闭
    server.request_shutdown()  # ② 触发优雅 teardown（保住 QA-4 drain）
app.state.shutdown_hook = _intentional_shutdown
```
- **时序钉死（F2 互咬防护）**：uvicorn 先关监听 socket 后跑 lifespan teardown（B2.3 根因定案）——armed 态下监听一关，refused 宽限（10s）开始走字；若关停的 drain+close 超 10s，watchdog 会把**有意关停**误判成 F2 而 `os._exit(1)` 断 drain。故 disarm 必须先于 request_shutdown。
- **disarm 机制**：`Watchdog.disarm()` 只置既有 `self._stop` Event（不 join，与测试清理用的 `stop()` 相区别）；探测环下一拍（1s interval）自然退，daemon 线程随进程灭。
- **如实 tradeoff**：disarm 摘了关停路上的 bounded-crash 安全网——off 路径 teardown 若永久卡，daemon 保持活尸（监听已失）、`off` 如实报"may still be shutting down"，用户可处置（任务管理器）。与 F2 之别：F2 是**未被察觉的**意外僵尸；有意 off 卡住是**可见的、用户发起的**。记边界，非静默回退。
- B2.3 全部 watchdog pin 必须续绿（never-arm、refused-grace fire、worker-hang 签名钉）。

### 持久态：哨兵文件（评审改判，config 注册键被否）

- **`CONFIG_DIR/daemon.off`**：在场 = 禁用；**缺席 = 默认开**（安装后零配置即开）。注册键方案被否三锤：(i) `reconcile_boot` 的 DB-primary 覆写——registry 键以 DB 为准，而 off/on 须在 daemon **不在**时落盘（只能离线写文件），旧 DB 行会在下次 `up` 把禁用静默复活；(ii) 语义不合——`enabled` 只在 `up` 启动读一次，不是热应用旋钮，不配 configwrite 机制；(iii) 不新增 registry 键 → `_SLOT_KEYS = sorted(REGISTRY)` 不动 → **version_id 槽位移边界（D5/NIT-3）完全不重踏**。
- **边界记档原文（入本 PRD）**：B2.5 以哨兵文件持久 on/off，刻意置于 configwrite/DB-primary 机制之外——off/on 必须在 daemon 缺席时持久，而 registry 键的 DB-primary 引导覆层会用陈旧 DB 值盖掉离线文件写入。此决策同时避免再触 version_id 槽位移：无新 registry 键，旧 version_id 解码不变。
- **共享 helper（DRY）**：新小模块 `src/mnemoseed_local/daemon_state.py`：`disabled_marker() -> Path` / `is_disabled() -> bool` / `set_disabled()` / `set_enabled()`；cli.py（up/off/on）与网关 serve() 共用；`CONFIG_DIR` 走 env 可覆写（config.py:24）。config.py / load_config / default_config_toml 零改动（默认=缺席）。

### 网关诚实（T2）

- **问题**：disabled 时 daemon 关停是构造性结论，agent 每次调用都撞 `DAEMON_DOWN_HINT` "cannot reach ... (start it with 'mnemoseed-local up')"——用户有意关闭时催 `up` 是错误指引（`up` 会拒）。
- **机制**：`serve()` 启动**读一次** `daemon_state.is_disabled()`，注入 `GatewayClient` 的 down-hint；disabled 时 refused-after-retry 文案 = `"cannot reach {base_url}: memory service is disabled by the user (run 'mnemoseed-local on' to re-enable)"`。timeout / 422 / causeless 路径不动。
- **pin 碰撞审计（全保字节）**：既有 refused 钉（`test_mcp_gateway_retry.py:92-100`）、unreachable 钉（`test_mcp_gateway.py`）、timeout/422/causeless 钉全部走"无哨兵"stub → 标准文案不变；disabled 文案只进**新测试**（monkeypatched CONFIG_DIR 置哨兵）。
- **边界**：穿越 off 仍存活的 gateway 进程持有旧 hint 到其生命周期末（启动读一次）；gateway 通常随宿主重启，如实记档。

### Supervision / README

- Task Scheduler 登录任务（RestartCount 3）在 disabled 下每次 `up` 即 rc 1 结束 → 无害短命 no-op；watcher 一行命令 15s 轮询监听 → 同样 no-op（略噪）。README 监督段加一句：关闭服务后请移除计划任务/watcher，或接受无害 exit-1；动词表加 `on`/`off` 两行。

### KISS 削减

- 无新 `status` 动词（`cmd_status` 已在 cli.py:98-115）；无 daemon 闲时自关；无 per-tool 粒杀；**hook 零改动**（火忘捕获对 daemon 缺席本就静默 no-op，即所求形态；pendingPull/tombstone 是 daemon 内存态随进程灭）；无 config 注册键；无 pidfile（B2.3 边界）；无新 MCP 工具（on/off 是 CLI 面，agent 不得自关）。

## 边界（如实）

1. off 收敛 best-effort：shutdown POST 应答 200 即 respond-then-exit，daemon 在 drain；CLI ≤15s 轮询，超时后再探一次 /healthz：存活 → 报 "daemon is still running"（复活/拒停，附手工停止指引）；不可达 → 报 "may still be shutting down"（drain 中）。数据包络 = 正常优雅关停（QA-4 drain 在位），非崩溃包络。
2. disarm 摘网 tradeoff（见上，已记档）。
3. gateway hint 陈旧窗（启动读一次）。
4. supervision 在 disabled 下产无害 rc-1 进程（略噪；文档指引移除）。
5. 哨兵文件非 config：`config get` 不可见、无版本化、无 DB 镜像——设计使然（必须在 daemon 缺席时存活）。
6. 默认开 = 哨兵缺席，装机零改动。

## 测试预言与变异体（按流，对抗式）

`test_daemon_onoff.py`（CLI）、`test_daemon_shutdown.py`（端点）、watchdog disarm（新测试或并入既有文件）、`test_gateway_disabled_hint.py`（或并入 retry 文件）：

| # | 预言 | 反变异体 |
|---|---|---|
| 1 | off 且 daemon 在跑：shutdown POST 被调 + 哨兵落盘 + rc 0 | 只写哨兵不调关停 → 红 |
| 2 | off 且 daemon 不可达：不炸、哨兵落盘、如实报、rc 0 | 不可达就抛错 → 红 |
| 3 | off 幂等：哨兵已在 → rc 0、不重复关停 | 已有哨兵报错 → 红 |
| 4 | on 且未跑：哨兵删 + `run_server` 被调 | 不删哨兵 → 红 |
| 5 | on 且已跑：哨兵删、不调 `run_server`、如实报 | 重启在跑 daemon → 红 |
| 6 | up 且哨兵在：rc 1、stderr 有 disabled + "on"、run_server 不触 | 见哨兵仍启动 → 红 |
| 7 | shutdown 端点（seam 在）：200 + 响应后 hook 触发（should_exit + disarm） | hook 不触 → 红 |
| 8 | shutdown 端点（TestClient 无 seam）：503 | 200 或 500 → 红 |
| 9 | watchdog disarm：armed 真监听上 disarm → 关监听超出 refused 宽限不 fire | disarm 不置 `_stop` → 红 |
| 10 | 网关 disabled 文案：哨兵在 → refused-after-retry 文案含 "on"/disabled 且不含 "mnemoseed-local up" | 恒用 up hint → 红 |
| 11 | B2.3 F2 签名钉续绿（worker-hang 测试） | 回归守卫 |
| 12 | B2.3 never-arm 钉续绿（TestClient 不 arm） | seam/503 路径不得 arm |

## 门禁与并行分解（2026-08-20）

- **Slice 1 核心**（`cli.py` + `daemon_state.py` 新 + `app.py` 端点 + `runner.py` 注入 + `watchdog.py` disarm + `README.md` + 新测试文件）：与 B2.4 不相交。
- **Slice 2 网关**（`mcp_gateway/server.py` + `reliable_client.py` + `test_mcp_gateway*.py`）：原与 B2.4 流 B 硬碰撞——B2.4 已合并（`ef3220a`），本批两 Slice 可全并行（文件面互不相交；Slice 2 依赖 Slice 1 的 `daemon_state.py` 契约，按本 PRD 契约实现、集成门禁兜底）。
- TDD（先红后绿）→ 对抗 QA → 全量门禁 → 单 commit 收口 + 收口记录入本 PRD。

## 批次执行记录（随批追加）

- **2026-08-20 设计评审**（solution-architect，会话 ses_fe4b30181ffeR6YkWkdEX16Jg4）：verdict SHIP-WITH-ADJUSTMENTS。调整全并入：持久态机制改判哨兵文件（注册键被否三锤，见"持久态"节）；watchdog disarm 先于 request_shutdown 的时序钉死；respond-then-exit 经后台 task + 无 seam 503 降级；网关 disabled 文案 pin 碰撞审计（全保）；`up` 早拒绝 rc 1 文案钉死；理论锚"无借用"记档。orchestration 决定：两 Slice 于 B2.4 合并后全并行（Slice 2 原硬碰撞随 B2.4 落地解除）。

#### 收口记录（2026-08-20）

**交付内容**：
- `daemon_state.py` 哨兵模块（`CONFIG_DIR/daemon.off` 在场=禁用缺席=默认开；调用时解析 CONFIG_DIR 敬测试惯例；mkdir-parents 容错）；PRD 边界记档原文生效（哨兵刻意置于 configwrite/DB-primary 之外，version_id 槽位移不重踏）。
- CLI 平铺 `off`/`on` + `up` 第一闸拒绝（rc 1 + 字节钉 stderr `"error: memory service is disabled (run 'mnemoseed-local on' to re-enable)"`）；`off` 定序 **marker-first → POST → ≤15s 轮询 → 报告**（QA IMPORTANT-2 定案，watcher/up 复活窗封死）；探针 1s 上限（`_probe_client`，QA NIT-B）；五分支存活感知报告（refused 存活/已死、轮询超时复活/drain、already-off 有/无 daemon；QA IMPORTANT-1）；意外异常宽捕获（marker 已收敛 + 指引 + rc 1；QA NIT-A）；`on` 幂等（删哨兵、已跑不重启、委托真实 cmd_up 路径）。
- daemon `POST /daemon/shutdown`（200 `{"ok":true,"status":"shutting_down"}`、respond-then-exit 经 asyncio.create_task 后台调 hook、audit、无 seam 503）；`runner.py` 模块级 `intentional_shutdown(watchdog, server)` —— **disarm() 先于 request_shutdown()**（F2 互咬防护；经 `functools.partial` 注入，QA IMPORTANT-3 顺序钉）；`Watchdog.disarm()` 只置 `_stop` 不 join。
- 网关 `GatewayClient` `down_hint` 注入通道：`serve()` 启动读一次 `is_disabled()`，disabled 时 refused-after-retry 文案 = `"cannot reach {base_url}: memory service is disabled by the user (run 'mnemoseed-local on' to re-enable)"`（字节钉）；标准 hint/timeout/422 路径字节不动；网关 hint 启动读一次的陈旧窗如实记档。
- README：动词表加 `on`/`off`；监督段加"disabled 下 `up` 即 rc 1 无害 no-op，请移除计划任务/watcher 或接受"。

**流程记录**：
- solution-architect 预评审 SHIP-WITH-ADJUSTMENTS（哨兵文件改判 + disarm 时序钉 + respond-then-exit 形态 + 网关 pin 碰撞审计全并入）。
- 双 SWE 全并行（Slice 1 核心 ∥ Slice 2 网关；B2.4 合并后碰撞解除）。
- senior QA 首轮 CLOSABLE 有条件（0 BLOCKER；IMPORTANT-1 refused 无存活感知报告、IMPORTANT-2 marker 在轮询后给 watcher 复活窗、IMPORTANT-3 disarm 顺序无钉；NIT 若干）→ 修复轮（1323）→ 复审 CLOSABLE（新 NIT-A 宽捕获指引、NIT-B 探针 30s 超时破轮询预算）→ 微修复轮（1327）→ 三审 CLOSABLE 零遗留。
- Slice 1 记档偏差（QA 验收）：`cmd_off` 吞 `DaemonRestError`（旧 daemon 对新端点回 404 曾致 marker 不落 + 后续 `up` 复活——崩溃路径劣于收敛路径），配合存活感知报告补强。
- record items（QA 三审确认随收口记档）：(1) disarm 不 join 属性无直接钉（代码审查层保证，挂死线程才可证，成本不值）；(2) "daemon stopped" 精确文案仅子串钉（五分支报告面已充分）；(3) gone 分支将 POST 超时与 refused 合并为 "daemon not running"（30s POST 超时下实际不可达）。

**测试增量与门禁**：1297 → **1327 passed / 3 skipped**（+30：onoff 家族 + shutdown 端点 + watchdog disarm + 网关 disabled hint；orchestrator 三轮独立复验一致：1316→1323→1327），ruff / ruff format / mypy 全净。

**生效前提**：`uv tool install --force .` 装机；新 `off`/`on` 立即可用（CLI 面）；`/daemon/shutdown` 端点与 watchdog disarm 随 daemon 换新生效；网关 disabled 文案随 opencode 重启（MCP 进程重建）生效。

**后续挂起**：B2.6 宿主 plugin 统一安装面（探针先行）；多 session 互认知研究专题；B2.3 挂起子项（boot 同步 dream 恢复挪出启动路径）。
