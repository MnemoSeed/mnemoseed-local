# PRD · 小件簇 2026-10-12（#229 / #230 / #238 / #242 / #243）

> 依据：owner 2026-10-12 "交给你处理"；各 issue 正文；#234 批 QA 的 IMP-1/IMP-3（= #242）；#239 实现席位的越界发现（= #243）。
> 理论锚：无借用——工程正确性与可观测性。不为"警告阈值"编造认知词汇。

## 范围（五件，互不相干但一个批次）

### #229 README verbs 行
README 的 verbs 行把 `recent_sessions(n_sessions?, n_per_session?)` 写成两个可选参数；MCP gateway 实际要求 `resume_query` 为必需。改文档对齐真实签名（server.py 为准）。

### #230 README sponsor URL
两个 sponsor 链接已腐烂（2026-10-09 实测：buymeacoffee 404、thanks.dev 403）；polar.sh 与 GitHub links 仍活。用有界 HEAD/GET 逐个验证；死的删除或替换；不新增任何 sponsor 主张。

### #238 run-root UNC oracle 网络往返
#235 的 UNC oracle 用 `resolve()` 证明扩展长度前缀在 live-home 比较两端被剥；未缓存 UNC 会触发 SMB 名称解析，冷启 ~2.7s、CI 双平台承重。被测属性是纯字符串变换：改为直接断言该变换（微秒级）。至多保留一个显式标记的 cold UNC 集成测试，默认 skip，须 opt-in marker。默认跑中不得有测试做网络往返。

### #242 保留端口前置/退出码 oracle（#234 QA 条件 IMP-1/IMP-3）
- IMP-1：给"会话前置条件 REPORT 占用"补单元 oracle——真实 bind 一个临时 loopback 端口，断言占用探测报 occupied、空闲端口报 free，并断言 terminal summary 反映实测状态。（QA 实测：把报告改成 no-op 全套仍绿，即该不变量后半段无自动护栏。）
- IMP-3：给退出码断言补非零退出场景——monkeypatch 让子进程非零退出，证明 teardown helper 走 register-and-raise 分支而非通过。
- tests-only；不得引入任何绕过认证或跳过隔离的开关。

### #243 MCP handshake beacon 存废（owner 决定：re-key，不弃用、不移到开放路径）
`/mcp/handshake` 关闭（任意本地进程可写的计数器）后，doctor 的"已注册但从未连接"警告（guard: `ingests > 0 and handshakes == 0`）对每个健康设置都永久为真。决定：**把"已连接"信号改键到边界之内仍可观测的东西**（例如按 profile 的首次已认证请求标记，或已认证工具调用计数），丢弃旧的无认证计数器语义；不得把 beacon 移回任何开放路径。验收：健康设置不再告警；真正从未连接的注册仍可判；两个方向各有回归测试；README/doctor 帮助与新行为一致。

## 验收判据（冻结）

1. README verbs 行与 `src/mnemoseed_local/mcp_gateway/server.py` 的真实签名逐字一致；sponsor 区仅含可解析链接。
2. UNC 属性由字符串变换断言直接证明；默认跑零网络往返；cold 集成测试存在且默认 skip。
3. #242 的两个 oracle 存在且有牙：杀掉 REPORT 报告、弱化退出码断言，各自必须 RED（在 PR 中给出变异证据）。
4. #243：doctor 在健康设置下静默；从未连接设置下告警；两个方向的测试在场；无任何开放路径上的可写计数器。
5. 门禁全绿（含 changed-comment 政策）：`pwsh -File scripts/gate.ps1` 或等价的五个检查逐字节留证。

## 纪律

- 隔离：spare port + 临时 `MNEMOSEED_LOCAL_HOME`；禁 bind 7788；禁碰安装运行时。
- TDD：每条先 RED 后 GREEN；现有测试是回归围栏。
- 风格：DRY、最小 diff、英文代码与注释；注释不带叙事/历史/人名/禁词（注释政策已两次抓到 `name/names/temporary`，写注释前先过 `scripts/check_changed_comments.py`）。
- 提交在批次分支，不推不 PR；主控跑 QA 与收口。
- Stop rules：#243 若发现"已连接"在边界内无任何可观测信号（即无法 re-key），停并上报，不得回退到开放路径。

## 收口记录（2026-10-12，commit 于批次分支 batch/small-items-20261012）

五件全部落地，门禁全绿（pytest 3314 passed / 6 skipped、ruff check、ruff format --check、mypy src 129 files、changed-comment 政策 exit 0）。

- **#229**：README verbs 行改为 `recent_sessions(resume_query, n_sessions?, n_per_session?, self_parent_id?)`，与 `mcp_gateway/server.py` 的 `inputSchema` 逐字一致；新增 doc oracle（`test_docs_honesty.py`）把 verbs 行逐个工具对回 schema（required 无 `?`、optional 带 `?`、属性序），RED→GREEN 留证。
- **#230**：逐个 sponsor URL 有界 HEAD/GET（2026-10-12 复测）：buymeacoffee 404、thanks.dev 403 删除（badge 与 "One-time tips ... or Buy Me a Coffee" 句中提法一并移除）；GitHub Sponsors 200、Polar 200（重定向至 polar.sh/checkout）保留。未新增任何 sponsor 主张。
- **238**：UNC oracle 改为直接断言字符串变换（`_strip_extended_length` / `_is_within`，微秒级）；旧的 resolve() 形式实测 2.71s（SMB 往返，与 issue 测量一致），新版 focused 选择从 12.11s 降至 1.82s；cold UNC 集成测试保留一个，默认 skip（`MNEMOSEED_UNC_COLD=1` opt-in）；变异证据：砍掉 UNC 前缀分支 → 新 oracle RED。
- **#242**：IMP-1 新增 `tests/test_session_precondition_report.py`（真实 bind 临时 loopback 端口 → occupied / 空闲端口 → free；terminal summary 经 pytest 插件钩子回显实测状态）；IMP-3 在 `tests/test_daemon_self_teardown.py` 补三个退出场景（有序拆除后非零退出、拒绝退出 register-and-raise、拆除前已非零退出），`tests/_test_daemon.py` 的 `_tear_down` 增加"已退出"记账分支（干净早退静默通过、非零早退具名失败）。变异证据：REPORT 助手 no-op / terminal 钩子 no-op / 夹具停止存储 / 退出断言弱化 / 拒绝分支不登记 / 计数前移到授权前——各自 RED。
- **#243**：beacon 退役，已连接信号改键为**门内按 actor 的已认证请求计数**（`Observability.note_authenticated_request`，由 `auth.py` 中间件仅在放行路径上写入，actor 取 `X-MnemoSeed-Actor` 自声明归因）。doctor guard = `ingests > 0 and authenticated_actor_counts.get("mcp", 0) == 0`；`/mcp/handshake` 路由、gateway beacon 线程、console 的 handshake 展示、README 关闭路径清单、design/04 与 docs/zh/README 同步更新。两个方向回归测试 + 非 gateway actor 已认证流量仍告警的反向 oracle；变异证据见 PR 正文。**未触发 stop rule**：边界内存在可区分信号（持凭据的 mcp actor 调用）。
