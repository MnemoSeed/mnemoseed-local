# PRD · 测试端口隔离终局（#234 / #226）

> 依据：owner 2026-10-10 决策评论（#234 帖）+ solution-architect 设计评审 CLOSABLE-WITH-CONDITIONS（全文存 #234 评论）+ owner 全盘接受 D1–D5。
> 执行前提：批分支/worktree 必须从 `origin/main (53e3cc2)` 切——当前主工作树 detached 于 `2eb5902`，guard 四件套不在其树内，S3 会空转。

## 理论锚

**无借用——工程控制面。** 本批是测试基础设施的可证成性工作，不借用任何认知/记忆理论；明确不借用清单：不为"隔离"编造注意力/工作记忆隐喻，不把"静态检查"包装成预测机制，不引用任何心理学词汇描述本批机制。

## 背景与问题

- guard 的存在理由：daemon 默认 7788 且持有真记忆；测试的数据目录隔离是**按进程**的，网络绕过它（#222 审计期真有探针连上 live daemon）。
- #226 证明进程内 patch 路线结构性失明（Windows Proactor 把真实目的端口的 connect 放在 Python socket 层之下），已四次修补，与所护套件自我缠斗。
- 关键事实（评审逐站点核验 origin/main）：**今日网络触达站点 = 0**——所有 7788/4096 出现处均为字符串断言、hermetic 假接线值、纯数据或 guard 自身断言；真 listener 已用 `bind(("127.0.0.1",0))`。本批职责是让不变量**可机械证成**，不是救火。

## 冻结不变量

1. 无测试进程 bind/connect 7788/4096；保留端口只准出现在默认配置/文档内容的字符串断言里。
2. live daemon 在 7788 上时全套必须过、不观测、不扰动。
3. 检测 = 静态检查 + 会话前置条件；不再有运行时 socket 拦截。
4. 每个测试 daemon 跑在自己的 ephemeral 端口（唯一 helper 选取），测完有界自毁并断言进程确实退出；残留进程是缺陷。
5. Bash/PowerShell/Node 子进程面由静态扫描看守（唯一现实覆盖，删除 guard 后不得丢失）。

## 规格修正（D1，冻结）

#234 原 S3"端口被占则拒绝启动"与不变量 2 及 S4 自相矛盾（阻塞式前置使 occupied 验收跑无法启动）。**冻结为 REPORT 语义**：前置条件探测占用（`bind(127.0.0.1,PORT)` 无 `SO_REUSEADDR`、立即 close），仅报告不阻塞；不区分占用者身份（无提权、无网络噪音、不向 /healthz 发请求）。#234 正文措辞随本批 PR 更正。

## 切片（TDD，按序）

- **S1 清单落表（无代码）**：本 PRD 附"网络触达数=0"逐站点表（取评审报告第(1)节纠正版；原 issue 表把 `selector_loop`/`default_probe` 误判为 real network use）；验收 = 每站点点名 API、机器可核对。
- **S2 端口 helper（依赖 S1）**：新增 `tests/_ports.py`：`unused_loopback_port()` + `listening_loopback_socket()`（context-manager 语义：listener 需保持绑定时让出 socket+port）。`test_daemon_watchdog.py` 两处 real-socket 测试改用（行为不变）。RED→GREEN 先钉 helper。
- **S3 换掉运行时 guard（依赖 S2，净删除为重点）**：
  - 删：`tests/port_guard.py`、`tests/conftest.py`（仅 guard 夹具 + sitecustomize 注入）、仅服务 guard 的子进程注入回归、fresh-interpreter `RESERVED_PORTS`/install-uninstall 钉、`test_run_isolation.py:115-120`。
  - **提拔保留（搬移非新增，D5）**：`network_primitive_violations`/`child_sources`/`POWERSHELL_NETWORK_PRIMITIVES`/`JAVASCRIPT_NETWORK_PRIMITIVES`/`FETCH_OVERRIDE` 及 `test_test_child_sources_reach_no_live_port`/`test_static_constraint_*`，入户新静态检查。
  - 增：静态检查（扫 tests 内 bind/connect/connect_ex/sendto/create_connection/open_connection/create_datagram_endpoint 指向保留字面量/常名；allow-list 仅含前置条件自身占用 bind），接成普通 pytest test。
  - 改：会话前置条件 fixture = REPORT。
  - TDD 顺序：新静态检查 + 提拔件先立（RED 于"注入一个拨保留端口的样例源被抓"）→ 再删 guard 四件套。**实数净行数，遇净增即停（stop rule）**。
- **S4 验收（依赖 S3）**：全套两跑——7788 空 / 7788 有 live listener，均过且结果一致，逐字留证于 PR。
- **S5 真 daemon 自毁测试（新能力，依赖 S2/S3）**：
  - helper：ephemeral 端口 + 子进程走产品 up 路径（`cli.py run_server`，天然含 watchdog arm + shutdown_hook），隔离 `MNEMOSEED_LOCAL_HOME`。
  - 就绪：`DaemonClient(base_url="http://127.0.0.1:<eph>")` 等 `/healthz` 200。
  - 拆除：`DaemonClient(base_url=<eph>, actor="test").post("/daemon/shutdown")`，断言 200 与 `{"ok":true,"status":"shutting_down"}`（**走 DaemonClient = D4：#239-agnostic**）。
  - 退出断言：`proc.wait(timeout≈30s)` + `assert returncode == 0`（边界对齐产品 `_OFF_POLL_TIMEOUT_S=15.0` 留余量）。
  - 拒绝退出：FAIL 该测试 + 登记 PID 给 session-sweeper（授权、记日志的会话级兜底）；**任何路径不得 inline `proc.kill()`**（D2）。
  - 测试语境 watchdog 兜底（如实形态）：产品 watchdog 在有意拆除时被 disarm（`runner.py:221`）故惰化；兜底 = fixture 外部实例化产品 `Watchdog(host,<eph>,probe=default_probe,exit=<sweeper>)` 将子进程作为被探测对象 + sweeper；文档写明"内建 watchdog 有意拆除时天然惰化"。

## 验收判据（冻结）

1. 静态检查命中注入样例；提拔的子进程扫描抓到的 offenders 集合与删除前一致。
2. 全套在 7788 空与占用两种状态下均过、结果一致（两跑留证）。
3. 无任何测试 bind/connect 7788/4096（机器可核）；`socket.socket` 无 monkeypatch。
4. 自毁测试证明进程退出（returncode 断言）；拒绝退出路径由 sweeper 登记，无 inline kill。
5. S3 为净删除（实数行数记录在 PR）。
6. 门禁全绿：`uv run pytest -q` / `ruff check` / `ruff format --check` / `mypy src`。

## 文件面与隔离纪律

- 允许面：`tests/**` + 本 PRD + #234/#226 正文更正。产品只读。
- 隔离纪律（AGENTS.md 红线）：测试/验证一律 spare port + 临时 `MNEMOSEED_LOCAL_HOME`；禁 bind 7788、禁碰安装运行时 `~/.mnemoseed-local`。
- Stop rules：净增、迁移测试改变证明面、出现"需要真 daemon 绑默认端口"的合法需求——停并上报。

## 与 #239 的次序

本批先落（tests 面轻、无外部依赖）；#239 的 S2 测试迁移（28 文件/166 处 TestClient）在本批合并后进行，避免两线同改 tests/**。D4：届时自毁 fixture 的凭据读取经 `DaemonClient` 同一接缝。
