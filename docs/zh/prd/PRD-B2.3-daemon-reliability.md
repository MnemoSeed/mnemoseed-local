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

### 挂起子项收口：boot 同步 dream 恢复挪出启动路径（2026-08-20 开工并收口）

- **设计（solution-architect 波次计划评审并入的 2 条 IMPORTANT 调整）**：`_build_capture` 保持 `recover()/adopt()/resume_boundary()` 分类与 `trigger.resume()/resume_merge()` **O(1) 同步**（推迟它们会让 scheduler 首 tick 在 `dream_in_flight=False` 下并发发射新 snapshot）；只将 `pipeline.run(snapshot)` 延迟为 dream worker 的 **RESUME 作业变体**（`_DreamJob` 独立字段，不混入 event/manual union，FIFO 单 worker 保序，enqueue 在 `worker.start()` 之后、lifespan `yield` 之前，端口零 LLM 调用即绑定）；**scheduler 首 tick 等待全部 deferred resume 排空**（drain 计数 try/finally 递减、异常/取消亦释放、零 resume 预置）——防"恢复窗口内对刚合并范围重复做梦"；QA IMPORTANT-1 修复：drain 等待有界 `RESUME_DRAIN_TIMEOUT_S=600` + 超时 WARNING 后照常 tick（防 resume 卡死导致调度静默停摆——本项目第一类缺陷形态）。
- **QA**：首轮 **CLOSABLE**（0 BLOCKER / IMPORTANT-1 drain 无超时随批修净；NIT docstring pairs 随批修净；NIT-1 pin-3 的 `completed_later==1` 依赖 1s 墙钟窗、NIT-3 job 互斥不变量未强约束、NIT-4 enqueue_resume 无 pre-start 守卫——三条记录为本节如实边界）；崩溃窗口未加宽（journal 仍是唯一事实源，双恢复幂等）。
- **门禁**：1327 → **1338 passed / 3 skipped**（本批 boot-recovery 5 钉 + 同波 B2.6 探针 6 钉）；ruff/format/mypy 全净。
- **生效前提**：`uv tool install --force` + daemon 重启。

### 批次执行：F2 根治（2026-08-20 用户指令立项并收口——Zombie 不可 killable，必须 impossible）

- **动议**：B2.3 watchdog 把 F2 僵尸转为干净崩溃，但僵尸本体不该出现；上线首日 watchdog 实弹 **5 击**（daemon.log 02:29 / 10:47 / 12:02 / 13:07 / 13:34 CRITICAL，均无 dump——早于本 build），证实 zombie 仍在日常生成；且 5 次 fire **均无 teardown 前行**，与 P1 根因形状不符（服务中途监听消失），wedge 机制未确证，本批根治的是 join 类通道、取证待 dump 时代。本批目标：机制级根治。
- **根因补充定案（solution-architect 复核，含 3.12.13 stdlib 源码+运行时实证）**：ThreadPoolExecutor worker 非 daemon 且被 `_threads_queues` 注册，`concurrent.futures._python_exit` / `threading._shutdown` 在解释器退出时 join **全部** executor 线程——teardown 的 `shutdown(wait=True)` 并不是唯一的绞索；**第二僵尸向量（架构师起获）**：anyio 4.14.2 `WorkerThread` 同样非 daemon，`ingest.py:60` 的 focal scan 走它，一样被 join。
- **设计（架构师 SHIP-WITH-ADJUSTMENTS，2 BLOCKER 并入：anyio 向量必须修、停止预算不得竞速 watchdog 击杀线）**：
  - **D1 共享模块 `util/daemon_executor.py`**：plain `threading.Thread(daemon=True)` workers on `queue.Queue`；TPE 兼容 `submit()`→Future / `close(timeout)`（sentinel-per-worker + 全局 deadline 等 running+已排队 future、未决者废弃不迟跑——QA NIT-2 定案）；RuntimeError after close；submit 与 close 竞态 lock-ordered；**永不注册 `_threads_queues`，卡死 worker 随进程亡**。
  - **D2 DreamWorker**：`DaemonExecutor(1, mnemoseed-dream)`；`stop_timeout=DREAM_STOP_TIMEOUT_S=5.0`（注入式）；`stop()` 有界等待不堵 loop（`asyncio.wait_for(wrap_future(cf))`），超时**就地废弃**（journal 双恢复幂等兜底）；`close(timeout=0)`；`_inflight_launched()` 以 `cf.done()` 先验（原 `cf.result()` 无界调用在废弃路径上必冻——架构师 IMPORTANT 修入）。
  - **D3 HybridRetriever**：`DaemonExecutor(2, mnemoseed-track)`；`close(timeout=close_timeout)`，`RETRIEVER_CLOSE_TIMEOUT_S=2.0` 注入式。
  - **D4 ingest scan**：模块级 `scan_executor` 单例（2 workers，**刻意从不关闭**——线程随进程亡，watchdog/announcer 先例），`anyio.to_thread` 全删；原 fire-and-forget except 封套不动。
  - **D5 watchdog 法医 dump**：fire 序列 = 末语 critical → flush 链 → dump 头（时间戳+reason+watchdog 线程名）→ `faulthandler.dump_traceback(all_threads=True)` 追加 `CONFIG_DIR/daemon.log`（**call-time 解析**，QA I-1 纪律）→ `os._exit(1)`；QA 修复轮再硬化：整个序列入 try、**`_exit(1)` 入 finally 无条件达成**（debug 日志自身抛错也不再跳过 exit）。
- **teardown 预算表（QA 与架构师共同钉死）**：retriever close 2s + dream stop 5s + drain/stores ~2s ≈ **9s** < watchdog refused-grace 击杀线 ~11-14s——健康关停永不被 watchdog 误杀。
- **判决否决记录**：TPE 子类 daemon 覆盖（注册表非 daemon 旗决定 join，实证无用）、submit-then-detach（WeakKeyDictionary 时序 hack）、multiprocessing（Windows spawn+store IPC 成本）、SIGBREAK 手动 dump（headless 无 console + fd 常驻）——全部记为 rejected 设计。
- **QA**：首轮 **CLOSABLE**（0 BLOCKER / 1 IMPORTANT：fire 路径 dump 的 `open` 在停摆网络盘可**挂而不抛**，try/except 挡不住——in-band 部分以 finally 无条件 exit 修净，残余为一类边界）；修复轮（finally-exit + close 排干已排队项、debug-log 自抛吞没）→ **门禁 1338 → 1346 → 1349 passed / 3 skipped** 全绿，ruff/format/mypy 净。
- **测试增量**：**13 钉**：翻转 pin（join-hang 文档 → bounded-abandon 实证）、daemon/unregistered 注册表 pin、subprocess 红绿对（DaemonExecutor wedged 子进程 rc=0 ∥ TPE wedged 子进程必挂——机制级直接证据）、retriever close 有界、recall-after-close RuntimeError、ingest scan 上 daemon 池且无 AnyIO worker、fire dump 全线程帧、dump 失败/ debug 失败皆不挡 exit、close 弃队不迟跑、提交竞态 lock-ordered。
- **如实边界**：(i) dump 目标盘的 BLOCKING open（停摆网络 share）可推迟 exit——band 外边界；(ii) 卡死的调用本体保持卡死（abandoned 线程+被占的 `_write_lock` 至重启；D4-变体 = 有界 `_write_lock.acquire(timeout=...)` 挂起，等 D5 堆栈证据命名 wedge 层）；(iii) daemon.log 每次 fire 追加全线程 dump（百行级，fire 稀少可承受）；(iv) anyio scan 池永不关闭是刻意单例；(v) dump 判帧用函数名（Windows faulthandler 不打印线程名）——实现偏离如实记。
- **生效前提**：`uv tool install --force` + daemon 重启；之后每次 watchdog fire 自带全线程堆栈取证。

## 附录 Rev 3 · watchdog 诊断批次（solution-architect + senior QA 终审 CLOSABLE）

> 范围冻结：仅 `daemon/watchdog.py`、`daemon/runner.py`、`tests/test_daemon_watchdog.py`；
> D1-D8 一字不改。WATCHDOG 常量、wall-clock 宽限判定、`boot-grace`/`refused-grace`
> 字符串、fire exit code 1、`create_app`/TestClient 永不武装、`up` 不 spawn 子进程
> 均不变；不新增 scheduler/guard/supervisor/restart/retry/self-heal。
> 本批是纯诊断取证批次——**9/3 listener 丢失根因仍未知**，外部自动重启已移除，
> 本批未部署前不改变任何现网行为。

- **R1 探针结果类型**：`ProbeKind = Literal[success, refused, timeout, other_oserror]`，
  frozen `ProbeResult(alive, kind, latency_ms)`；`probe` 接受
  `Callable[[], bool | ProbeResult]`，`_run` 每轮恰调用一次（bool False→REFUSED、
  True→SUCCESS 归一化，fire 只看 `alive`）；生产 `default_probe_result` 单
  `create_connection`（捕获顺序 ConnectionRefusedError → timeout/TimeoutError →
  其他 OSError，含 WinError 64 → alive/other_oserror），`default_probe` 保留 bool
  wrapper 与原 B6 日志行；latency 由 `_run` 单次调用前后 monotonic 计量并覆盖，
  `default_probe_result` 不二次计时。
- **R2 有界统计**：Watchdog 内固定标量 `probe_total`、四 kind counts、
  `last/max_latency_ms`、`refused_window_start`、`snapshot_errors`、
  `instrumentation_errors`；禁 list/deque/p50/全量 latency；仅 refused 窗口
  start/end transition 与 fire 记日志（B6 行格式不变）。
  > **日志边界澄清（QA 收口）**：这里"edge-only"特指本次 Rev 3 **新增**的
  > transition 行（refused 窗口 start/end）与 fire summary 行——它们只在
  > 状态边沿触发。**预先存在的 per-probe B6 行（timeout 与 other_oserror
  > 的"stalled; treating as alive (B6 domain)"）并非 edge-only**：它们本就
  > 在每次 stall 探针时照常逐行出现，格式与频率保持 Rev 3 之前一致，未改动。
- **R3 双 stop 检查**：`_run` 顺序 单 probe → stats → 第一 `_stop.is_set()` →
  alive/refused bookkeeping/transition → 达宽限后 snapshot+summary（失败只计数/debug）
  → 第二 `_stop.is_set()` 紧贴 `_fire` → fire；`fire: Callable[[str], NoReturn]`
  签名不变，summary 暂存实例，旧 reason 单参注入测试零改。
- **R4 纯读快照**：`runner._snapshot_server(server) -> tuple[dict, int]`，
  读 should_exit/started 与至多 2 servers × 4 sockets，逐项封套；仅输出
  bool/int fd/TCP host+port/error type+errno（单字段 ≤200B、总值 ≤2KB，超限降级并
  计错；非 TCP 地址不记 path）；禁 `import json` 与 socket/server repr；
  helper 不共享 Watchdog 可变 counter，`run_server` 以 optional
  `server_snapshot` callable 接入。
- **R5 故障计数纪律**：新观测代码禁裸 except/pass；外层先增 `snapshot_errors` 或
  `instrumentation_errors` 再调永不抛 `_safe_debug`；仅 `_safe_debug` 最内层允许
  bare pass 且不再触 stats/time/IO/log（防递归）。
- **R6 fire 顺序**：critical 末语+summary → logger 链 flush → with open
  daemon.log append 写 header+summary → `dump.flush()` → faulthandler all_threads
  → with close → finally exit(1)；禁 fsync；直接 `_default_fire` 旧测试无 summary
  时走最小安全 summary。
- **R7 日志白名单**：transition/fire schema 仅 host/port、reason、monotonic
  elapsed、kind counts、last/max latency、armed、snapshot 安全字段、error
  counters；新日志禁 text/content/chunk/header/ssl/path/payload/profile/session/turn。
- **门禁与证据**：12 个 Rev3 新钉（RED 先行，10 红 2 回归 pin）→ 全绿；
  双 stop-check mutant 各自杀死对应测试；`uv run pytest -q` 全量通过；
  ruff check / format / mypy 全净。