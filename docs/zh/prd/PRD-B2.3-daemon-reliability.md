# PRD-B2.3 · daemon 可靠性永久修复

## 理论锚（照抄纪律：本批为纯工程，不借理论）

本批是 daemon 进程可靠性工程（吊死检测/退出/网关重试），**不借用任何神经科学/心理学规律**——自觉排除清单：Heap/self-heal 不是记忆理论，不给运行时长机制穿认知词汇（B2.2 同款纪律）。任何"daemon 活性"措辞仅为工程语义。

## 背景与症状（如实）

- F2（最重，2026-08-19 实锤）：python 进程存活但 :7788 监听消失，connection refused，事件循环吊死但进程不退，根因当时未明。
- F1：MCP `remember` 多次超时/报错（daemon 忙时）；2026-08-19 重装后 killing gateway 进程导致本会话 mnemoseed_* 工具失联一轮。
- roadmap 立案方向（docs/zh/prd/PRD-B2-roadmap.md 挂起项）：吊死根因 → 自检 + 监听丢失自动退出 → 网关重试/诚实报错；用户 2026-08-19 改判：本批提前为当前批次（B2.1 T1+T3/T2 已收口，可靠性先于 T4/QA-7/B4）。

## 根因结论（三路侦察 P1/P2/P3 综合，solution-architect 计划评审 PLAN-WITH-ADJUSTMENTS 3 BLOCKER 已全部并入）

**F2 根因（P1 定案，置信 High）**：uvicorn 关停顺序 = 先关监听 socket 再跑 lifespan shutdown（uvicorn 0.52.3 `Server.shutdown()` 源码实锤）；daemon teardown 链 `app.py:629 MemoryService.close()`（→ `hybrid.py:155 executor.shutdown(wait=True)`）与 `app.py:632 DreamWorker.stop()`（`app.py:339 executor.shutdown(wait=True)`）**永久 join** 在飞的 dream/recall worker——worker 可卡在 LanceDB 无界写（`lancedb_embedded.py:118-159` 无超时；ollama LLM 腿有 60s timeout `ollama.py:80-90` 但 store I/O 腿无界）；Python 3.12 的 ThreadPoolExecutor worker 是非 daemon 线程，`_python_exit` atexit 还会再 join 一遍。合起来 = 端口先死、进程卡在 join、永不退出。

**确定性 boot 变体（P1 发现）**：`_build_capture` 的 journaled snapshot 恢复循环（`app.py:501-508`）在 **bind 前同步跑完整 dream 链**（`pipeline.py:123-134` → `reflect.py:550 llm.chat`），带 60s timeout + 重试——启动可卡数分钟、端口永不绑定。

**被否决的预置机制**：`cmd_up` 预检 double-open（`cli.py:86-91`）不成立——`LanceDbEmbeddedStore.close()` 是 no-op（`lancedb_embedded.py:291-292`），每 boot 构造全新驱动实例，无句柄复用风险。

**P2**：网关是串行 stdio 循环（`server.py:278-290`），单次 `tools/call` 冻结全网关最长 30s（`rest_client.py:29` 单次无重试）；错误经 `DaemonUnavailableError`（`raise ... from exc`，可用 `__cause__` 分类 refused/timeout，零改 rest_client 的缝）与 `DaemonRestError`。

**P3**：监护减法调研（选项排位表见下）。

## 设计决定（decided design questions，全部钉死）

- **D1 形态**：只 ship **进程内自检测 + 快退** + 持久日志。不 ship supervisor（KISS 减法，表见下）；拉起归用户侧（README 文档化 Task Scheduler + watcher 一行命令，P3 交付物 B 原文）。
- **D2 watchdog（唯一新增进程内机制）**：daemon 线程（非 asyncio——循环死时 asyncio 任务同死，侦察定案）裸 TCP connect 探测 `127.0.0.1:7788`，间隔 1s；两态机：**PRE_BIND**（首连成功前；超过 `WATCHDOG_BOOT_GRACE_S = 300` 仍 refused → 退出——覆盖 boot dream 恢复吊死变体）→ **ARMED**（首连成功后；连续 refused ≥ `WATCHDOG_REFUSED_GRACE_S = 10` → 退出——正常关停 teardown 远快于 10s，吊死关停被宽限兜住后处死）。**退出 = 写末语日志行后 `os._exit(1)`**（跳过卡死的 join 与 atexit；数据丢失包络 = 普通崩溃同款，B2.2 水印 + 会话级回放兜底，ack 钟语义不变）。**ARM 纪律**：只经 `run_server` 路径武装（`runner.py` 在 `server.run()` 前起线程）；`create_app()`/模块 import/TestClient 永不武装（`app.py:714` 模块级 boot 纪律）；正常关停时线程随解释器退出（daemon thread 绝不阻塞退出），teardown 期间保持 ARMED 生效（`memory.close`/`dream_worker.stop` 正是要守的悬挂点）。
- **D3 no-response 边界**：探测语义只管 **refused（监听消失）**与 **boot 超宽限**；connect 成功即视为存活（bound-but-stalled 的事件循环饥饿属 B6 域，本批不治——stalled 只记日志行不退出，诚实边界）。
- **D4 持久日志**：lifespan startup 时给 `mnemoseed_local` logger 挂 `FileHandler` → `CONFIG_DIR/daemon.log`（`config.py:24` 根，与 hook-watermarks 同根）；捕获 boot 行（pid/version/port）、teardown 行（各阶段进入）、watchdog 末语（fire 原因+时间戳，fire 后立即 flush）。uvicorn 自身日志仍走 stderr（如实）。
- **D5 网关 retry（gateway-local，零改 rest_client.py）**：新模块 `mcp_gateway/reliable_client.py` 的 `GatewayClient` 薄包装；`serve()` 单点 wrap（不动 `build_client()`——`:333-336` actor pin）；规则：**至多一次快速重试、仅当首败为 `DaemonUnavailableError.__cause__` 是 `httpx.ConnectError`（refused=daemon 重启窗）**；重试腿预算 `RETRY_TIMEOUT_SECONDS = 1.5`（首腿保持 30s 不变）；`DaemonRestError`/`TimeoutException`/无 cause 一律不重试。语义安全已逐工具核验（recall/recent 读；remember 近重复吸收；dream_once 重叠守卫）。
- **D6 诚实报错（pin 兼容）**：(a) refused → `cannot reach {base_url}: daemon is not running (start it with 'mnemoseed-local up')`（保 `:246` "cannot reach" pin）；(b) timeout → `cannot reach {base_url}: daemon timed out after 30s (busy or hung; try again shortly)`；(c) 4xx/5xx 原样透传（保 `:258` "422" pin）；无 cause 原样透传。`daemon error: ` 前缀留在 server.py 不动。
- **D7 只读面（本批钉死）**：`rest_client.py`、`plugin.ts`、`cli.py` 的 `up` verb（no-subprocess pin `test_cli.py:582-601` 不触）、config registry 零新键（全常量，precedent `runner.py:22-24`）。
- **D8 出范围（诚实边界）**：事件循环饥饿（`trigger.py:528-535,712-735`、`sweeper.py:109-133`、`ingest.py:41,94-96` 同步 on-loop 点）= B6 域；boot 同步 dream 恢复挪出启动路径 = **后续批次挂起项**（本批由 D2 的 PRE_BIND 宽限快退把僵尸转成崩溃+日志，不再扩展范围）；opencode 对 gateway 进程的 respawn 行为在仓外。

## KISS 减法表

| 候选机制 | 判定 | 理由 |
|---|---|---|
| **(a) 无监护（本批基线，排第 1）** | **做（shipped 形态）** | 本批 shipped = 进程内自检测 + 快退：daemon 线程 socket 探活，宽限期后 `os._exit(1)` + 末语日志。零新增进程树、零 pidfile/锁、零双 daemon 碰撞面，test_cli.py 的"`up` 绝不 spawn 子进程"钉（`tests/test_cli.py:582-601`）不被触碰。自诊断成立的前提是**末语落盘到持久文件**（现状 daemon 日志只走 stderr，无 FileHandler——见文末事实 5，本批需补 `CONFIG_DIR/daemon.log` 式落点）。退出码 1 + 末语 = 交给用户或下方 (b) 一行命令接手，语义清晰、可复原 |
| **(b) 用户侧 Task Scheduler / watcher 一行命令（排第 2）** | **文档（README/PRD 数行，不 shipped）** | 零代码成本、环境可复原（删任务即回原状）；与 (a) 是**互补而非替代**——调度器只认"进程退出且非零码"，而"进程存活但 7788 消失"（2026-08-19 实锤形态）在调度器眼里是 Running、永不重启；(a) 的 exit-fast 恰好把吊死转化为非零退出，才让 (b) 的 RestartCount 变得有效。仓库已有同族先例：`install.ps1` 给 ollama serve 注册的登录任务（`install.ps1:281-285`，RestartCount 3 / 1min）。具体定义见文末"交付物 B" |
| **(c) shipped 独立 supervisor verb（`up --supervised` / 兄弟 wrapper）** | **不做（减法 OUT）** | 完整代价：新的进程树管理（谁等谁、孤儿收养）、pidfile + 锁文件（Windows 上需 job object 才可靠回收子进程——`CreateJobObject` 是新增的 ctypes/win32 依赖面）、**双 daemon 绑定碰撞**（supervisor 与自愈后重拉起的实例抢 7788）、测试面（新增一个常驻进程生命周期测试族）。收益只是"免手点一次 `up`"，与 (b) 的文档化一行命令等价——成本收益不成比例。且 pin 死：`up` 路径绝不 spawn 子进程（`tests/test_cli.py:586-596`），shipped supervisor 只能走旁路 verb，等于再造半个"服务管理"产品 |
| 进程内 **asyncio-task 看门狗** | **不做（已否决）** | 自身与业务跑同一事件循环：循环卡死（uvicorn 吊死的主因场景）时看门狗任务也永远不被调度——**测不到自己要守的死**。这正是 shipped 形态选 daemon **线程** + 阻塞 socket 探测的原因：OS 线程独立于事件循环，循环死了它照常探测、照常退出 |
| **OS 服务管理器（`sc.exe` / NSSM）** | **不做** | `sc.exe` 建服务需管理员提权，NSSM 是第三方二进制、仓库不打包；二者都绑定"注册到系统"的安装形态，与 CLI-first + `uv tool install`（`README.md:30-31`）的用户可移植哲学相悖。安装器已克制到"只 hint、不 relocate"（`install.ps1:186-187` 同型原则），服务注册是同一类越界。真需要常驻监督时，(b) 的登录任务已是当前用户零提权替代 |

**排位逻辑**：(a) 先立 shipped 最小面 → (b) 文档补足"死了有人拉" → (c) 被 (b) 覆盖且引入全部新风险面 → 两个已否决项是形态原因（asyncio 测不到自身循环 / 系统服务不用户可移植）。

## 交付物 B —— 实际可粘的文档化监督定义（本批仅落文档，供 README 引用）

**登录计划任务（推荐；对齐 `install.ps1` 的 ollama 先例）**

```powershell
$shim = Join-Path $env:USERPROFILE ".local\bin\mnemoseed-local.exe"
$action   = New-ScheduledTaskAction -Execute $shim -Argument "up"
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "MnemoSeedLocalDaemon" `
  -Action $action -Trigger $trigger -Settings $settings -Force
```

诚实标注的三个坑：(i) **必须用 AtLogOn + RestartCount，禁用周期触发器**——周期"每 X 分钟跑一次"会在健康 daemon 占端口时再起一个 `up`（重复运行）；restart-on-failure 只在动作未运行且非零退出时触发。(ii) **`-ExecutionTimeLimit` 必须置 0（不限）**——Task Scheduler 默认 3 天上限会把健康长跑 daemon 硬杀。(iii) `mnemoseed-local` 是 uv tool shim，`-Execute` 用全路径；RestartCount 命中"非零退出"的失败——正对 watchdog 的 `os._exit(1)`。

**watcher 一行命令（不愿建任务时）**

```powershell
while ($true) {
  if (-not (Get-NetTCPConnection -LocalPort 7788 -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -WindowStyle Hidden mnemoseed-local -ArgumentList 'up'
  }
  Start-Sleep -Seconds 15
}
```

防重复运行守卫 = **端口监听检查**（不是 healthz 探测：吊死但监听在时 healthz 会超时、反而触发误起；watchdog 会在宽限内释放端口，下一轮循环自然拉起）；若与释放竞态，新 `up` 绑定失败会未捕获抛错并立即以非零退出码退场（uvicorn EADDRINUSE 直传，非自定义码），硬底兜住。

## 边界（如实）

> **边界（如实）**：本批 shipped 的监督语义是"**自检测 + 快退**"，不是"自愈"——daemon 只负责把'吊死（进程存活、7788 监听消失，2026-08-19 实锤形态）'和'监听丢失'检测出来，宽限期后带末语日志 `os._exit(1)` 干净退场；**退出之后的拉起归用户侧**：手点 `mnemoseed-local up`，或按 README 文档化的登录计划任务 / watcher 一行命令重启（RestartCount 只认非零退出的失败——exit-fast 正是让调度器监督变得有效的另一半）。shipped 代码**不新增 supervisor、不 spawn 子进程**（test_cli.py 钉死）、不落 pidfile、不做端口预检（双 daemon 由端口绑定自排除）。Windows 无 SIGTERM 语义：`os._exit` / 任务管理器结束任务都是硬杀，**不触发 lifespan teardown**——capture drain（`app.py:610-633`）不跑，内存状态机直接全灭；这正是 B2.2 的 ack 水位 + 会话级重生回放兜底的存在理由（源头宿主会话史持久，视图可重建），硬杀缺口以'最后一次成功持久为界'如实接受。opencode MCP 网关是宿主的 stdio 子进程，其 respawn 生命周期归 opencode 自己（README:53-62 只承诺握手在 daemon 下线时照常、工具调用报 isError），**在本仓库边界之外**，不归本批。

## 门禁

同 B2.2 PRD：TDD 先红后绿 → 对抗 QA（无 BLOCKER 方可收口）→ 全量门禁（`uv run pytest -q` / ruff / format / mypy）→ 单 commit 收口。

## 批次执行记录（2026-08-19 开工 → 2026-08-20 收口）

**流程**：solution-architect 计划评审 **PLAN-WITH-ADJUSTMENTS**（3 BLOCKER 全部并入：up 路径禁子进程 pin → 本批只交付进程内 exit-fast + 持久日志、retry 只落 mcp_gateway/ 且 rest_client.py 只读、watchdog 绑 lifespan 且只经 run_server 武装）→ 三路并行只读侦察（P1 吊死 RCA / P2 网关面 / P3 监护减法，互不冲突、当日完成）→ PRD 落档定案 D1-D8 → **双 SWE 并行实现**（S1 daemon 侧 watchdog+log ∥ S2 网关侧 retry，文件面零相交）→ 门禁全绿 → senior QA 首轮 **CLOSABLE**（0 BLOCKER / 2 IMPORTANT / 9 NIT）→ 修复轮（I-1 测试写真 home 污染、I-2 stalled→alive 红线索钉）+ 残余扫净（4 小 suite 一并补 fixture）→ QA 复审 **CONFIRMED-CLOSED**（0 pushback，3 个轻 NIT 记边界）。

**根因定案（P1）**：uvicorn 关停 = 先关监听 socket 再跑 lifespan teardown（0.52.3 源码实锤）× teardown 链 `executor.shutdown(wait=True)` 永久 join 卡死 dream/recall worker（LanceDB 无界写腿，ollama 仅 LLM 腿有 60s timeout）；Python 3.12 executor worker 非 daemon 线程 `_python_exit` 再 join 一遍 → 端口先死、进程卡 join 永不退。boot 变体：`_build_capture` 在 bind 前同步跑完整 dream 恢复链。预置机制 3（cmd_up 预检 double-open）被侦察否决（lancedb close no-op、每 boot 新驱动实例）。

**交付**：
- **watchdog**（`daemon/watchdog.py`，新增 172 行模块）：daemon 线程裸 TCP 探测，PRE_BIND（首连前，boot 宽限 `WATCHDOG_BOOT_GRACE_S=300` 连续 refused → fire）/ARMED（首连后，`WATCHDOG_REFUSED_GRACE_S=10` 连续 refused → fire）两态；refused=死信号，connect 成功/超时=活（stalled 归 B6，只记日志不退出）；fire = 末语行（logger `mnemoseed_local.daemon`，沿链 flush 到 daemon.log 的 FileHandler）后 `os._exit(1)`（绕过卡死 join 与 atexit，数据丢失包络 = 普通崩溃同款，B2.2 水印+回放兜底）；只经 `runner.py:99-100` 的 `run_server()` 武装，`create_app()`/import/TestClient 永不武装（thread-enumerate pin 钉死）。
- **探针超时实战修正**：`_PROBE_TIMEOUT_S = 3.0`（开发中发现：本机类过滤 Windows 主机 loopback refused 延迟 ~2s 才送达，1s 预算会把死监听错判为 stalled=alive——实测钉死，S1 三修随批），docstring 记证。
- **持久日志**：lifespan startup 挂 FileHandler（`CONFIG_DIR/daemon.log`，utf-8、double-attach guard、call-time 解析 CONFIG_DIR 让测试 fixture 可重定向——QA I-1 修复的定案形态）；boot 行（pid/version/preset/port）+ teardown 各阶段 ENTER 行 + watchdog 末语同链落盘。
- **网关 retry**（`mcp_gateway/reliable_client.py`）：`GatewayClient` 薄包装、`serve()` 单点注入；至多一次快重试且仅当首败为 `DaemonUnavailableError.__cause__` 是 `httpx.ConnectError`（refused=重启窗，证 pre-request 不会双重作用）；重试腿 `RETRY_TIMEOUT_SECONDS=1.5`（真 DaemonClient 走 `dataclasses.replace(timeout=...)`，stub 回退同 client 并如实记注）；RestError/Timeout/无 cause 零重试；诚实报错保 pin：refused 双败 → "daemon is not running (start it with 'mnemoseed-local up')"、timeout → "daemon timed out after 30s (busy or hung; try again shortly)"、"cannot reach"/"422" 既有 pin 不动。
- **README**：`Daemon supervision (optional)` 一节（Task Scheduler 登录任务块 + watcher 一行命令 + 4 条诚实坑），bind 失败如实记为"未捕获 EADDRINUSE → 非零退出"（曾误写"码 3"，QA N-1 修正）。AGENTS.md 增补并行化执行条款（2026-08-19 用户指令，随批入仓）。

**QA 修复轮（IMPORTANT 随批修净）**：I-1 测试套件向真 `~/.mnemoseed-local/daemon.log` 灌垃圾 → call-time 解析 + 8 个 booting suite 全部补 `config.CONFIG_DIR` fixture 补丁 + LastWriteTime 不变实证（9 个 booting 文件全枚举，stragglers=0）；I-2 `default_probe` 任意 OSError→死 的变异体原可过绿 → B6 双钉（probe 级 monkeypatch TimeoutError→True+B6 日志、状态机级 always-alive 无 fire）。NIT：N-1 文档事实修正、N-2 SYNs 静默丢弃主机的惰性边界、N-3 PRE_BIND 时钟起点、N-5 stop-then-start 文档、N-7 boot 行针加严；N-4（双武装生产不可达）/N-6（时序面）/N-8（日志级）如实记为边界。

**TDD 偏离两则（如实）**：test 7 停表改独立线程实测（`executor.shutdown(wait=True)` 会堵死事件循环，`asyncio.wait_for` 的 timeout 永不交付）；test 3 探针曾误用"本机静默丢 SYN"假设，实测证伪后回滚为生产 default_probe（refused 恒 ~2s 到达）。

**测试增量与门禁**：1253 → **1273 passed / 3 skipped**（+20：watchdog 8 + 修复轮 3、gateway retry 9）；ruff / format / mypy 全净。QA 独立复验全量门禁三次一致。

**如实边界**：(i) 防火墙静默丢弃 loopback SYN（无 RST）的主机上死监听与 stalled 不可区分，watchdog 惰性——不治，记档；(ii) 事件循环饥饿归 B6（stall 只记日志不退出）；(iii) boot 同步 dream 恢复挪出启动路径 = 后续挂起（本批 PRE_BIND 宽限把僵尸转为崩溃+日志）；(iv) 数据丢失包络 = 普通崩溃同款，B2.2 兜底；(v) opencode 对 gateway 进程的 respawn 归仓外；(vi) FileHandler 双 boot 异 tmp 目录时 first-wins（name-only guard，测试态残留 fd 与串台日志指向，QA 记 NIT，baseFilename-aware 加固留后续）。

**生效前提**：`uv tool install --force .` + daemon 重启得 watchdog 与 daemon.log；README 监督段即读即用；预污染的旧 daemon.log 收口时一次性清理。

## 测试要求（钉给实现的）

- **repro 钉（P1 §5 原文改写）**：`DreamWorker.stop()` 对"卡死 in-flight job"在有限时长内不返回（阻塞 snapshotter double + wait_for TimeoutError）——必须先红在现有代码上（现有 wait=True join 必卡）、watchdog 上线后行为不变但进程语义改变，钉住的是机制本身；teardown 期间 refused+宽限 → fire 决策的单测（watchdog 线程逻辑用假 socket 探针注入）；boot PRE_BIND 超宽限 fire 决策单测；**`create_app()`/TestClient 永不武装的 pin**；FileHandler 落 `CONFIG_DIR/daemon.log` 的行内容 pin（boot/teardown/watchdog 末语三类）。
- **`tests/test_mcp_gateway_retry.py`（P2 §4 的 8 例原文）**：refused→success 恰好 2 次调用、refused 双败报 down hint、timeout/RestError/无 cause 零重试、成功路径恰好 1 次、重试两侧请求体逐项等值、ping/tools/list 不经 client。
- **既有 pin 全保**：test_mcp_gateway.py `:148-258`、`test_cli.py:582-601`、`test_hosts_opencode.py:32-41`、`test_hosts_install.py:186`。