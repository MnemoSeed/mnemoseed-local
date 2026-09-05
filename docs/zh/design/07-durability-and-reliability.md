# 07 · 耐久与可靠性（Durability & Reliability）

> 元信息：本仓库 daemon 的记忆服务在崩溃、进程吊死、用户启停三种失效面下的工程可靠性层设计——崩溃由宿主会话史重生回放兜底，进程死亡由进程内 watchdog 自检测 + 快退，启停由用户可控 on/off 开关。全部为纯工程可靠性，不借用任何记忆/认知理论。
> 状态基线：commit `e95921b`（B2.3 挂起子项）与 `02ca93d`（F2 根治）为本文锚定实现；当时门禁 1349 passed / 3 skipped，ruff / format / mypy 全净。行号引用一律钉在基线 commit（`02ca93d` 代码内容）之上；B6/B4a 等在途批次合入 src 后，行号引注须随基线推进重钉。
> 主要依据 PRD：`PRD-B2.2-crash-durability.md`、`PRD-B2.3-daemon-reliability.md`、`PRD-B2.5-daemon-onoff.md`，以及 `PRD-B2-roadmap.md` 中 F2 根治批次记录（squash `02ca93d`，PR #26 → issue #25）。

## 0. 功能定位与边界

本设计文档覆盖 daemon 记忆服务在三种失效形态下的可靠性与可控性，三块机制各自独立、互为兜底：

| 失效面 | 机制 | 依据批次 |
|---|---|---|
| 崩溃（断电 / 系统 crash / 杀进程） | 宿主会话史重生回放（单机制，KISS 减法定案） | B2.2 |
| 进程吊死 / 监听消失 | 进程内 watchdog 自检测 + 快退 + 法医 dump | B2.3 + F2 根治 |
| 用户可控启停（on/off） | 哨兵文件持久禁用 + 优雅关停端点 | B2.5 |

三块共享的顶层语义：**"检测 + 快退 + 可复原"，不是"自愈"**。shipped 代码只负责把失效形态转化为干净的退出（exit code 1 + 末语日志 + 法医 dump），退出之后的拉起归用户侧（手点 `up`，或 README 文档化的登录计划任务 / watcher 一行命令）。数据丢失包络 = 普通崩溃同款，由 B2.2 的 ack 水位 + 会话级重生回放兜底；源头（宿主会话史）持久，视图即可重建。

边界（诚实）：

- shipped **不新增 supervisor、不 spawn 子进程**（`test_cli.py:582-601` 的"`up` 绝不 spawn 子进程"钉不被触碰）、不落 pidfile、不做端口预检。
- 事件循环饥饿（bound-but-stalled，监听在但请求不答）属 B6 域，本批不治——watchdog 对 connect 成功一律视为存活，只记日志行不退出。
- 防火墙静默丢弃 loopback SYN（无 RST）的主机上，死监听与 stall 不可区分，watchdog 惰性（不治，记档）。
- Windows 无 SIGTERM 语义：`os._exit` / 任务管理器结束任务都是硬杀，不触发 lifespan teardown，capture drain 不跑——这正是 B2.2 兜底的存在理由。
- opencode 对 MCP 网关进程的 respawn 生命周期在仓库边界之外。

## 1. 流程（mermaid + 走查）

### 1.1 B2.2 崩溃耐久：宿主会话史重生回放（单机制）

```
flowchart TD
    A[daemon 2xx 回执] -->|ack 钟推进| W[(hook-watermarks*.json 文件族<br/>legacy 只读 + per-process 独占分片<br/>每次 max-merge 后只写自己分片)]
    B[host 持久化完整会话史<br/>client.session.messages] --> C{每个 session 首见事件}
    C -->|有水位| D[拉宿主消息史]
    C -->|无水位| S[跳过回放<br/>debug lane 记录]
    D --> E[只重放 ts > 水位 - 30s 尾部]
    E --> F[per-session FIFO 链<br/>replay 先于 live]
    F --> G[daemon ingest<br/>近重复吸收 replay]
    G -->|2xx| A
    G -->|nack / 被拒| U[解除 reconciled<br/>下事件按最后 ack 水位重放]
```

走查：

1. **ack 水位（ack 钟，非发送钟）**：`plugin.ts` 的 `noteWatermark` 只在 POST 收到 daemon **2xx** 回执时推进，推钟路径全部经 `post()` 的 2xx 分支。发出≠到达——fire-and-forget 语义下若按发送钟推水位，宕机会静默吞掉未达内容（B2.2 BLOCKER-1 已修）。水位文件族 `hook-watermarks*.json` 仍算**单一可删除机制**：legacy `hook-watermarks.json` 对新代码只读（永不覆写，纳入每次合并）；新代码只写自己的独占分片 `hook-watermarks.<pid>.<uuid>.json`（进程自有 tmp 名含 pid+uuid+单调 counter，原子 write+rename，Windows 瞬态 `EPERM/EACCES/EBUSY` 有界重试 N≤5、总时限有界，失败只记 debug；进程内 persist 串行化，调用永不 reject）。初次加载与每次 persist 都先枚举 legacy + 全部分片完整快照、只接受有限非负数值、逐 key 取 max 与内存合并，再落盘自己分片（原子替换下读者只见旧/新完整快照之一，源分片保留至安全 GC，故并发不丢 key）。刷写点 = 各 cadence 点（idle/error/deleted/compacting）；读写故障一律吞没降级，绝不侵入主链路。腐坏冻结：非法文件/key 跳过、原文件保留、仅记 debug 元数据。保守 GC：分片年龄 >24h 且 PID 经 `ESRCH` 确认已死才合规（存活/`EPERM`/未知/复用歧义跳过），先把全局最大值落盘成功后再删最多 20 个；永不删 legacy、腐坏文件、存活/不确定分片或他进程 tmp。混跑（旧+新进程共存）降级至全宿主重启才收敛，记为残留。
2. **会话级重生回放**：`reconcileSession` 在 **每个 session 首次见到其事件时**（惰性，不扫全宿主）执行一次对账（`plugin.ts:927`）：`listSessionMessages` 拉该 session 宿主侧消息史 → 映射为既有 ingest 载荷（user/assistant/tool 三分流，ts 取 `info.time.created` / `completed`）→ **只重放 `ts > 水位 - 30s` 的尾部**（`REPLAY_OVERLAP_MS = 30000`）→ 按时间序逐条 POST。**到达序保证 = 每 session 一条 FIFO promise 链**（`enqueueForSession`，`plugin.ts:909`）：replay 段插队在该 session 一切 live 内容之前（分片器按到达序切 turn，乱序即错绑——B2.2 BLOCKER-2 已修），replay 成功才标 `reconciled`（`plugin.ts:989`），失败留待下次事件重试。**无水位 session 跳过回放**（`plugin.ts:936`，特性前历史不可重放，跳过决策进 debug lane 非静默）。
3. **nack un-reconcile（宕机空洞）**：`scheduleRecovery`（`plugin.ts:738`）在 ingest 被拒 / 失败时 `reconciledSessions.delete(sessionID)`，下一事件自动按最后 ack 水位重放——**宕机空洞不能被后续 ack 跳跃覆盖**（B2.2 复审 NEW-1 已修）。replay 的 assistant 指纹若在 POST 发出时置位、TOP 前置 unpark，单发失败即真丢失——指纹置位保留（抑在飞重复），但 nack 回滚指纹并重挂 pending（复审 NEW-2 已修）。
4. **指纹回滚 + 近重复吸收**：replay assistant 与 live 通道共用指纹守卫，成功即解 pending；daemon 侧近重复检测把切分一致的字节级重放吸收为**零新 chunk**（`reconciledSessions` + daemon 幂等）。

### 1.2 B2.3 daemon 可靠性：watchdog 状态机

```
stateDiagram-v2
    [*] --> PRE_BIND
    PRE_BIND --> ARMED: 首次 connect 成功
    PRE_BIND --> FIRE: 连续 refused ≥ WATCHDOG_BOOT_GRACE_S (300s)<br/>reason=boot-grace
    ARMED --> ARMED: connect 成功 (reset 计数器)
    ARMED --> FIRE: 连续 refused ≥ WATCHDOG_REFUSED_GRACE_S (10s)<br/>reason=refused-grace
    FIRE --> [*]: 末语日志 + 全线程 faulthandler dump<br/>+ os._exit(1)
```

走查：

- **探测语义只管 refused**：`default_probe`（`watchdog.py:49`）用裸 TCP connect 探测 `127.0.0.1:7788`，`ConnectionRefusedError` = 死信号；connect 成功或任何其它错误（超时/网络错）= 存活（bound-but-stalled 属 B6，只记日志不退出）。探针预算 `_PROBE_TIMEOUT_S = 3.0`：本机类过滤 Windows 主机 loopback refused 延迟 ~2s 才送达，1s 预算会把死监听错判为 stalled=alive（实测钉死，S1 三修随批）。
- **两态机**：`PRE_BIND`（首连前，boot 宽限 `WATCHDOG_BOOT_GRACE_S = 300` 连续 refused → fire，覆盖 boot 同步 dream 恢复吊死变体）→ `ARMED`（首连后，`WATCHDOG_REFUSED_GRACE_S = 10` 连续 refused → fire；正常 teardown 远快于 10s，吊死关停被宽限兜住后处死）。任何 connect 成功重置 refused 累计器。
- **fire = 末语 → 法医 dump → exit**（`watchdog.py:190`）：logger critical 末语行 → `_flush_logger_chain` 沿链 flush 到 daemon.log 的 FileHandler → 追加 dump 头（时间戳 + reason + watchdog 线程名）→ `faulthandler.dump_traceback(all_threads=True)` 全线程堆栈 → `os._exit(1)`（跳过卡死的 join 与 atexit）。**exit 入 finally 无条件达成**（debug 日志自身抛错也不跳过 exit）；CONFIG_DIR 于调用时解析（`daemon_state`/`daemon.log` call-time 纪律，QA I-1 修复定案形态）。
- **持久日志**：lifespan startup 挂 FileHandler → `CONFIG_DIR/daemon.log`（`config.py:24` 根，与 hook-watermarks 同根），utf-8、double-attach guard、call-time 解析 CONFIG_DIR 让测试 fixture 可重定向；捕获 boot 行（pid/version/preset/port）、teardown 各阶段 ENTER 行、watchdog 末语同链落盘。uvicorn 自身日志仍走 stderr（如实）。
- **ARM 纪律**：只经 `run_server` 路径武装（`runner.py:108` 在 `server.run()` 前起线程）；`create_app()` / 模块 import / TestClient 永不武装（thread-enumerate pin 钉死）。
- **网关 refused-only 单次快重试**（`reliable_client.py`）：`GatewayClient` 薄包装，`serve()` 单点 wrap；规则 = 至多一次快速重试、仅当首败为 `DaemonUnavailableError.__cause__` 是 `httpx.ConnectError`（refused = daemon 重启窗）；重试腿预算 `RETRY_TIMEOUT_SECONDS = 1.5`（首腿保持 30s）；`DaemonRestError` / `TimeoutException` / 无 cause 一律不重试。诚实报错保 pin：refused 双败 → "cannot reach ... (start it with 'mnemoseed-local up')"、timeout → "daemon timed out after 30s (busy or hung; try again shortly)"、4xx/5xx 原样透传、无 cause 原样透传。

### 1.3 F2 根治：DaemonExecutor（机制级消灭僵尸）

```
flowchart LR
    subgraph 旧[ThreadPoolExecutor]
        T1[worker 非 daemon] --> R[注册 _threads_queues]
        T2[teardown shutdown wait=True] --> J[永久 join 卡死]
        T3[_python_exit atexit 再 join] --> J
    end
    subgraph 新[DaemonExecutor]
        D1[daemon=True worker on queue.Queue] --> D2[永不注册 _threads_queues]
        D2 --> D3[卡死 worker 随进程亡]
        D4[close timeout 有界] --> D5[未决者就地废弃<br/>journal 幂等兜底]
    end
```

走查：

- **F2 根因（P1 定案，置信 High）**：uvicorn 关停 = 先关监听 socket 再跑 lifespan teardown（0.52.3 `Server.shutdown()` 源码实锤）× teardown 链 `MemoryService.close()` / `DreamWorker.stop()` 的 `executor.shutdown(wait=True)` **永久 join** 在飞的 dream/recall worker——worker 可卡在 LanceDB 无界写腿（`lancedb_embedded.py` 无超时；ollama LLM 腿有 60s timeout）；Python 3.12 ThreadPoolExecutor worker 非 daemon，`_python_exit` atexit 还会再 join 一遍 → 端口先死、进程卡在 join、永不退出。
- **第二僵尸向量（架构师起获）**：anyio 4.14.2 `WorkerThread` 同样非 daemon，`ingest.py:60` 的 focal scan 走它，一样被 join。
- **`util/DaemonExecutor`**（`daemon_executor.py`）：plain `threading.Thread(daemon=True)` workers on `queue.Queue`；TPE 兼容 `submit()` → Future / `close(timeout)`（sentinel-per-worker + 全局 deadline 等 running + 已排队 future、未决者废弃不迟跑）；RuntimeError after close；submit 与 close 竞态 lock-ordered；**永不注册 `_threads_queues`，卡死 worker 随进程亡**。
- **替换面**：DreamWorker（`DaemonExecutor(1)`，`stop_timeout = DREAM_STOP_TIMEOUT_S = 5.0`，超时**就地废弃**，journal 双恢复幂等兜底）；HybridRetriever（`DaemonExecutor(2)`，`close_timeout = RETRIEVER_CLOSE_TIMEOUT_S = 2.0`）；ingest scan（模块级 `scan_executor` 单例 2 workers，**刻意从不关闭**，线程随进程亡，anyio `to_thread` 全删）。
- **teardown 预算表**：retriever close 2s + dream stop 5s + drain/stores ~2s ≈ **9s** < watchdog refused-grace 击杀线 ~11–14s——健康关停永不被 watchdog 误杀。
- **法医 dump**：fire 序列自带全线程 `faulthandler` 取证 dump 追加 daemon.log，下次 fire 将现场指出 wedge 确切栈位（2026-08-20 取证修正的决策闸门）。
- **判决否决记录**：TPE 子类 daemon 覆盖（注册表非 daemon 旗决定 join，实证无用）、submit-then-detach（WeakKeyDictionary 时序 hack）、multiprocessing（Windows spawn + store IPC 成本）、SIGBREAK 手动 dump（headless 无 console + fd 常驻）——全部记为 rejected 设计。

### 1.4 B2.5 on/off：用户可控启停

```
flowchart TD
    OFF[mnemoseed-local off] --> M1[marker-first 写 daemon.off]
    M1 --> P[POST /daemon/shutdown]
    P -->|DaemonUnavailable| G1[gone 分支]
    P -->|DaemonRestError| R[refused 分支]
    P -->|2xx| W[≤15s 轮询等监听消失]
    W -->|消失| S1[daemon stopped]
    W -->|仍存活| S2[daemon still running]
    W -->|不可达| S3[may still be shutting down]
    ON[mnemoseed-local on] --> M2[删哨兵]
    M2 -->|healthz 可达| A[already on 不重启]
    M2 -->|不可达| U[委托 cmd_up 启动]
    UP[mnemoseed-local up] -->|哨兵在| RC[rc 1 + stderr 拒绝]
```

走查：

- **哨兵文件 `CONFIG_DIR/daemon.off`**（`daemon_state.py`）：在场 = 禁用；缺席 = 默认开（安装后零配置）。注册键方案被否三锤：(i) `reconcile_boot` 的 DB-primary 覆写——registry 键以 DB 为准，而 off/on 须在 daemon **不在**时落盘，旧 DB 行会在下次 `up` 把禁用静默复活；(ii) 语义不合——`enabled` 只在 `up` 启动读一次，不是热应用旋钮，不配 configwrite 机制；(iii) 不新增 registry 键 → `_SLOT_KEYS = sorted(REGISTRY)` 不动 → **version_id 槽位移边界完全不重踏**。
- **`off` 定序 marker-first → POST → ≤15s 轮询 → 报告**（`cli.py:145`）：写哨兵最先（写失败即 rc 1 诚实报错不再动作），watchatcher / `up` 复活窗封死；best-effort `POST /daemon/shutdown`（`DaemonUnavailableError` 吞掉 = 已停）；≤15s 轮询等监听消失，超时如实报 "may still be shutting down"；五分支存活感知报告（refused 存活 / 已死、轮询超时复活 / drain、already-off 有 / 无 daemon）。探针用 1s 上限客户端（`_probe_client`，`_OFF_PROBE_TIMEOUT_S = 1.0`），shutdown POST 保持标准 30s。意外异常宽捕获 → stderr 指引 + rc 1，哨兵保持（状态已收敛）。
- **`on`**：删哨兵 → 若已在跑（healthz 可达）：报 "already on / already running"，不重启，rc 0；否则走 `cmd_up` 既有启动路径（前台阻塞）。
- **`up` 早拒绝**：`cmd_up` 最前闸见哨兵 → stderr `"error: memory service is disabled (run 'mnemoseed-local on' to re-enable)"` + rc 1，`run_server` 不触。**不给 `up --force`**（KISS：`on` 是唯一显式许可通道）。
- **关停端点 `POST /daemon/shutdown`**：respond-then-exit——handler 立即 200 `{"ok":true,"status":"shutting_down"}`，后台 `asyncio.create_task` 在响应冲刷后调注入的 shutdown hook；无 seam 降级 503（TestClient boot 永不 arm watchdog，可测性由此保住）。
- **run_server 注入 `intentional_shutdown`**（`runner.py:71`）：`watchdog.disarm()` **先于** `server.request_shutdown()`（F2 互咬防护：uvicorn 先关监听 socket 后跑 teardown，armed 态下监听一关 refused 宽限走字，若 drain 超 10s watchdog 会把**有意关停**误判成 F2 而 `os._exit(1)` 断 drain；故 disarm 必须先行）。`Watchdog.disarm()` 只置 `_stop` Event 不 join，探测环下一拍自然退。如实 tradeoff：disarm 摘了关停路上的 bounded-crash 安全网——off 路径 teardown 若永久卡，daemon 保持活尸，`off` 如实报 "may still be shutting down"，用户可处置。
- **网关 disabled 诚实文案**：`serve()` 启动读一次 `is_disabled()` 注入 down-hint；disabled 时 refused-after-retry 文案 = `"cannot reach {base_url}: memory service is disabled by the user (run 'mnemoseed-local on' to re-enable)"`。标准 hint / timeout / 422 / causeless 路径字节不动（pin 碰撞审计全保）；disabled 文案只进新测试。边界：穿越 off 仍存活的 gateway 进程持有旧 hint 到其生命周期末（启动读一次）。

## 2. 理论锚

本节照抄三段 PRD 的"无借用"声明（VERBATIM，引用块 + PRD 出处；禁止转述）。

### 2.1 B2.2（出处：`PRD-B2.2-crash-durability.md` §理论锚）

> **本功能不借用任何心理学/神经科学理论**——它是确保核心功能正常运作的纯工程可靠性层（崩溃恢复），不属于记忆功能设计，理论锚纪律的"不借清单"照常适用：不得把任何耐久性工程手段包装成记忆理论动机。唯一援引的是一条工程性事实（非理论）：**OpenCode 宿主自身持久化完整会话史**（`client.session.messages` 可读回），daemon 的捕获是宿主会话存储的派生视图——源头不丢，视图即可重建。

### 2.2 B2.3（出处：`PRD-B2.3-daemon-reliability.md` §理论锚）

> 本批是 daemon 进程可靠性工程（吊死检测/退出/网关重试），**不借用任何神经科学/心理学规律**——自觉排除清单：Heap/self-heal 不是记忆理论，不给运行时长机制穿认知词汇（B2.2 同款纪律）。任何"daemon 活性"措辞仅为工程语义。

### 2.3 B2.5（出处：`PRD-B2.5-daemon-onoff.md` §理论锚）

> 本批为工程控制面（进程生命周期启停、哨兵状态文件、优雅关停端点、诚实报错文案），**不借用任何神经科学/心理学规律**。不借清单：不开关即"遗忘"（off 不是记忆衰退语义）；不开关即"显著性"（on/off 不是注意力旋钮）。措辞纪律同 B2.3：任何"daemon 活性"表述均为工程语义，永不入认知词汇。

## 3. 实施方式（code-level）

本文实现全部为新增面 + 既有机制替换，生产核心链（capture / dream / retrieve）本身不被侵入（`up` 路径绝不 spawn 子进程 pin 保住）。

| 模块 | 文件 | 职责 |
|---|---|---|
| watchdog 状态机 | `daemon/watchdog.py` | daemon 线程裸 TCP 探测；PRE_BIND / ARMED 两态；fire = 末语 + 法医 dump + `os._exit(1)`；`disarm()` 只置 `_stop` |
| run_server 装配 | `daemon/runner.py` | 起 watchdog 线程 + announcer；`intentional_shutdown`（disarm 先于 request_shutdown）经 `functools.partial` 注入 `app.state.shutdown_hook` |
| DaemonExecutor | `util/daemon_executor.py` | daemon 线程池替换 TPE / anyio；有界 close + 就地废弃 |
| 网关重试 + 诚实报错 | `mcp_gateway/reliable_client.py` | `GatewayClient` refused-only 单次快重试；disabled hint 注入 |
| 哨兵状态 | `daemon_state.py` | `CONFIG_DIR/daemon.off` 在场=禁用缺席=开；call-time 解析 CONFIG_DIR |
| CLI on/off/up | `cli.py` | `cmd_off` / `cmd_on` / `cmd_up` 早拒绝 |
| hook 重生回放 | `hosts/opencode/plugin.ts` | ack 水位、per-session FIFO 链、reconcileSession 回放、nack un-reconcile |

关键代码取证：

- ack 钟水位：`plugin.ts:616` `noteWatermark`（仅在 post 2xx 分支被调）；`plugin.ts:625` `persistWatermarks`（tmp+rename 原子写）。
- FIFO 链：`plugin.ts:909` `enqueueForSession`（replay 段先于 live 入链）。
- 回放：`plugin.ts:927` `reconcileSession`（`ts > 水位 - 30s` 尾部、无水位跳过、成功才标 reconciled）。
- nack：`plugin.ts:738` `scheduleRecovery`（un-reconcile 防宕机空洞被跳跃覆盖）。
- watchdog 两态 + fire：`watchdog.py:174` `_run`、`watchdog.py:190` `_default_fire`。
- DaemonExecutor：`daemon_executor.py:33` `__init__`（daemon=True）、`daemon_executor.py:87` `close(timeout)`（有界 + 废弃）。
- on/off：`cli.py:145` `cmd_off`、`cli.py:216` `cmd_on`、`cli.py:59` `cmd_up`（`is_disabled()` 早拒绝）。
- 端点注入：`runner.py:71` `intentional_shutdown`（disarm 先于 request_shutdown）。

## 4. 红线与诚实边界

- **"检测 + 快退 + 可复原"，不是"自愈"**：shipped 代码只把失效转化为干净退出，拉起归用户侧；监督语义绝不升级为 shipped supervisor。
- **Ack 钟纪律**：水位 = 最后一次 daemon 2xx 确认的 ts（含 cadence 未落盘的最近 ack——崩溃前最后一个 cadence 内 acked 轮次会被多重放，由近重复吸收，代价良性）；到达序保证是**派发序**（loopback 亚毫秒级乱序实践不可达）。
- **残余缺口（放弃 WAL 的代价）如实声明**：daemon 接受了 POST 但崩溃在 drain 之前、且宿主侧尚未走到下一轮 idle——最多丢当前一轮；daemon 持续 202 但永不 drain（flush 链路坏死）时 acked 轮次不会被重放——属另一类故障，不归本批。
- **吸收界限**：近重复吸收对"切分一致的字节级重放"是确定性的；切分不同的重放可能落一条内容重复的 chunk——容忍（噪声非丢失）。
- **无水位 session 降级**：跳过 = 该类 session 在"特性启用后、首次成功持久水位前"的 crash 尾巴不可重放，损失以最后一次成功持久为界。
- **watchdog 惰性边界**：静默丢 SYN 主机上死监听与 stall 不可区分；事件循环饥饿归 B6（只记日志不退出）；fire 在停摆网络盘上的 BLOCKING open 可推迟 exit（band 外边界，try/except 挡不住，in-band 部分以 finally 无条件 exit 修净）。
- **5 次 refused-grace fire 取证修正（2026-08-20 如实记录）**：上线首日 watchdog 实弹 5 击（02:29 / 10:47 / 12:02 / 13:07 / 13:34），均无 teardown 前行，与 P1"关停卡 join"的根因形状不符——fire 是服务中途监听直接消失，wedge 机制未确证；决策闸门 = dump 构建上线后的下一次 fire 的全线程堆栈。本批根治的是 join 类通道，取证待 dump 时代。
- **B2.3 挂起子项如实**：boot 同步 dream 恢复挪出启动路径（RESUME 作业变体 + scheduler 首 tick 有界 drain 闸 `RESUME_DRAIN_TIMEOUT_S = 600`）——推迟的是 `pipeline.run(snapshot)` 本身（端口零 LLM 调用即绑定），`recover()/adopt()/resume_boundary()` 分类与 `trigger.resume()/resume_merge()` 保持 O(1) 同步（防 scheduler 首 tick 在 `dream_in_flight=False` 下并发发射新 snapshot）。
- **disarm 摘网 tradeoff**：有意关停若永久卡，daemon 保持活尸（可见、用户发起的），与 F2（未被察觉的意外僵尸）有别，记边界非静默回退。
- **哨兵文件非 config**：`config get` 不可见、无版本化、无 DB 镜像——设计使然（必须在 daemon 缺席时存活）。
- **不开关即"遗忘"/"显著性"的禁令**：off/on 是工程控制面，绝不入记忆衰退 / 注意力旋钮语义（见 2.3 理论锚）。

## 5. 本篇引用

本文锚定仓库实现与 PRD，不引入外部学术文献；唯一援引的"工程性事实"（OpenCode 宿主自身持久化完整会话史，`client.session.messages` 可读回）是本文所述的宿主行为，非文献条目，注明于下。

- `docs/zh/prd/PRD-B2.2-crash-durability.md`（仓库 PRD，B2.2 崩溃耐久；同主仓 Rxx 状态：本仓库自己）
- `docs/zh/prd/PRD-B2.3-daemon-reliability.md`（仓库 PRD，B2.3 daemon 可靠性 + F2 根治；同主仓 Rxx 状态：本仓库自己）
- `docs/zh/prd/PRD-B2.5-daemon-onoff.md`（仓库 PRD，B2.5 on/off；同主仓 Rxx 状态：本仓库自己）
- `docs/zh/prd/PRD-B2-roadmap.md`（仓库 PRD，Phase B 总路线图与批次记录，含 F2 根治批次；同主仓 Rxx 状态：本仓库自己）
- `README.md` §Daemon supervision（仓库文档，Task Scheduler AtLogOn + RestartCount、ExecutionTimeLimit=0、watchdog exit code 1 语义、watcher 一行命令；工程形态一段，可引用为运维形态）
- 工程性事实（非文献条目，注明）：OpenCode 宿主自身持久化完整会话史（`client.session.messages` 可读回），daemon 捕获为派生视图——源头不丢，视图即可重建。

实现代码取证源（均在仓库 `src/mnemoseed_local/` 与 `hosts/opencode/` 下）：`daemon/watchdog.py`、`daemon/runner.py`、`util/daemon_executor.py`、`mcp_gateway/reliable_client.py`、`daemon_state.py`、`cli.py`、`hosts/opencode/plugin.ts`。
