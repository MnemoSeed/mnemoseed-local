# PRD-320：原始记忆召回全链路追踪（合成会话 × 隔离宿主，诊断分类学）

日期：2026-11-07。状态：执行完毕；首轮独立 QA 判定 CLOSABLE（2 IMPORTANT 引用完整性修复 + 5 NIT），修复轮已逐项落实（I-1 四处引用改锚 / I-2 boot2 窗口逐字摘录补存 + 台账归属更正 / NIT 1–5），待主控复核。基线：`dcac6b0`（分支 `feat/320-raw-recall-trace`，worktree `.mnemoseeding/worktrees/w320`，= 当前 main）。本批为 #320 的验证/调查单元：在隔离宿主目录 + 合成守护进程 + 备用端口上，把一条**唯一合成会话**沿「捕获 → 持久化 → 受控重启 → 索引覆盖 → 冻结公开召回/会话恢复 → 注入送达」全链路逐格取证，并为六类故障构造**相互可区分**的判别证据。除本文档外零仓库写入、零产品代码改动；全部探针证据保存在新建证据树 `%LOCALAPPDATA%\Temp\w320-trace\`。

## 证据与结论边界

全链路检查点分层：**(a) ingest 承认 → (b) 持久化（非仅缓冲）→ (c) 受控重启存活 → (d) 向量索引覆盖 → (e) 冻结公开召回 + 会话恢复读回 → (f) 注入送达代理面**。前一项通过从不代表后一项；每格独立给出 PASS/FAIL 与在盘证据。送达格的三层证据（注入块 → 模型输入 → 模型答案引用）与 PRD-305 第三轮的证据形态一致。

「持久化」的判定语义：chunk 已落存储（向量行 + 溯源身份），且**重启后仍可读回**——只被 /ingest 202 承认而未 drain 的数据不满足本格（该态由诊断分类学第 2 类单列）。索引覆盖的判定语义：needle 是一个**可查询的 embedding**（稠密向量检索可命中、原始行 `vector_dense` 非空），不是"仅一行文本"。

「送达代理」的判定语义：注入字节进入宿主会话上下文（插件 debug 面的注入行）**且**模型推理文本自述看到回放、答案忠实引用被捕获事实（hosted 模型，cost 0）。模型答案引用 ≠ 语义召回质量声明：本轮 embedder 为 synthetic（64 维哈希向量），召回 PASS 只证明通路可用性与 verbatim 通道完好，不代表真实 embedder（bge-m3）下的检索语义质量。

所有测试内容均为合成（verdant-obelisk / quartz-vault / QZX-4417 / crimson-lantern-5523 / slate-hollow / azure-marrow-2210 / pale-forge / obsidian-spire-9931 为虚构锚名），不含任何私人身份、密钥或真实记忆。真实已装运行时（`~/.mnemoseed-local`，7788 常驻守护进程）除边界事件 1 的失控重启（详见该节）与一次披露的只读 `GET /healthz` 存活复核外，本轮零写入、零数据读取、未停启。

## 机器与环境清单

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows 11 Pro 25H2，build 10.0.26200（与 PRD-305 三轮同一主机） |
| OpenCode | v2.0.32（npm `@opencode/cli`；**serve 面**，PRD-345 修正方法论的规定面） |
| mnemoseed-local | 0.3.0（worktree `w320` @ `dcac6b0`，`.venv` 经 `uv sync --frozen` 建立，Python 3.12.13，uv 0.12.5） |
| 合成守护进程 | `mnemoseed-local up --host 127.0.0.1 --port 27791`（主链；备用端口）；neg4 漂移构造用 27792；7788/4096 全程未用作本轮端口 |
| opencode serve | `opencode serve --hostname 127.0.0.1 --port 4591 --print-logs --log-level debug`（boot2，cwd=隔离目录；4096 未使用） |
| 托管模型回合 | `opencode/big-pickle`（agent `build`），`cost: 0`，隔离数据目录无 auth.json，无凭据创建 |
| 隔离目录 | `%LOCALAPPDATA%\Temp\w320-trace\`：`mnemoseed-home\`（守护进程家）、`opencode-home\`（OPENCODE_CONFIG_DIR）、`opencode-root\`（HOME/USERPROFILE 重定向根）、`opencode-data\`+`opencode-cache\`+`opencode-state\`（XDG 三件套）、`cwd\`（serve 工作目录）、`cwd-neg\`（预留未用）、`neg4-home\`（索引漂移构造的弃置家）、`sessions\`（日志/探针/进程台账/真实家快照） |
| 进程台账 | `sessions\daemon-pid.txt`：守护进程 6 次 boot（boot1 36580、boot2 **边界事件** 31680、boot3 13380、boot4 36776、boot5 29300、boot6 收尾）+ neg4 家 3 次独立 boot（27792）+ opencode serve boot1 32880（killed）/boot2 36060（killed）；终态仅余 owner 的 7788 守护进程（PID 22260），全部探针端口确认释放 |

主链守护进程 `config.toml`（mnemoseed-home，启动前写定）：`preset = "embedded"`、`baseurl = "http://127.0.0.1:27791"`、`[storage.embed] driver = "synthetic"`、`[storage.vector] dimensions = 64`、`[storage.graph.instances.isolated]`（显式临时绝对路径）、`[logging] requests = true`、`[capture] idle_flush_sec = 20.0`；N2 期间经产品热应用通道改为 `capture.idle_flush_sec = 3600`（configwrite 版本 3000002，诊断分类学第 2 类的构造前提，已记录）。

隔离机制（PRD-305 三轮 + PRD-345 配方的合并）：守护进程与 CLI 用 `MNEMOSEED_LOCAL_HOME` 指向临时家；插件用 `MNEMOSEED_LOCAL_BASEURL=http://127.0.0.1:27791` + `MNEMOSEED_LOCAL_DATA_DIR`（= mnemoseed-home，读凭据 + 写 hook-debug.jsonl）+ `MNEMOSEED_LOCAL_DEBUG=1`；opencode serve 用 PRD-345 配方 A+（`HOME`+`USERPROFILE` 重定向 → `opencode-root\`，`OPENCODE_CONFIG_DIR`/`XDG_DATA_HOME`/`XDG_STATE_HOME`/`XDG_CACHE_HOME`/`TMP`/`TEMP` 全部指向证据树），**PATH 白名单**（System32/Windows/PowerShell/nodejs/mingw64/usr/bin——黑名单剥离在预检中两次漏放 `~/.local/bin` 后改为白名单，预检 `command -v mnemoseed-local` 必须失败为前置门，本轮记录 SCRUBBED-OK 于 `sessions\precheck-debug-paths.txt`）。启动前 `opencode debug paths` 断言 home/data/cache/config/state/tmp/log/db 十行全部落在证据树内。

## 全链路检查点矩阵（唯一合成会话：verdant-obelisk 链）

合成会话内容（会话 1，`ses_e4be4aae3ffeWv1bymcyHyiEqW`，经 serve API 创建 + prompt 驱动）：`user: Note for later: the verdant-obelisk build pipeline uses the quartz-vault cache index QZX-4417. …`；模型（big-pickle）回合中还经注入的 remote MCP entry 在会话内真实调用 `remember` 落了一个 pin（`649e7836…`，见 (f) 披露）。

| 检查点 | 判定 | 精确证据（均在证据树） |
| --- | --- | --- |
| (a) ingest 承认 | PASS | `mnemoseed-home\daemon.log` 11:44:54–11:45:01 共 4 行 `POST /ingest -> 202`（1 user + 3 assistant/工具，hook actor）；`sessions\obs-after-s1.json`：`capture_ingest_count: 0 -> 4`、actor `hook: 7`；hook-debug.jsonl 对应 reconcile/session-start/assistant step 行 |
| (b) 持久化（非仅缓冲） | PASS | PR #315 空闲清扫器行 11:45:27 `idle flush: drained 1 session(s): ses_e4be4aae…`（ingest 后约 26 s，`idle_flush_sec=20` + 30 s tick 的设计窗口内）；`sessions\session-recent-s1.json`：chunk `52989cbc71934cf0…`，`source: "opencode-chat"`、`host: "opencode"`、`session_id` 归组、turn 0..0，**逐字**保留 user 行——verbatim 通道完好 |
| (c) 受控重启存活 | PASS | `POST /daemon/shutdown -> 200`（11:48:14 teardown complete，端口释放）→ 同家重启（boot3；healthz 复核 `embed: synthetic` 后才继续）→ `sessions\session-recent-after-restart.json`：同一 chunk `52989cbc…` 仍可读回（判别提取 `sessions\restart-survival-check.txt`：该已保存 payload 的只读解析，needle chunk 在场 = True）。首次重启尝试即边界事件 1（env 丢失落真实家），已按纪律杀除并以正确 env 复跑；本格证据取自验证过 home 的 boot3 |
| (d) 向量索引覆盖 | PASS | `sessions\probe-index-out.txt`（`sessions\probe_index.py`，经 factory 构建与守护进程同构的存储栈）：needle 查询向量检索命中 `52989cbc…`（similarity 0.2763，稠密检索命中的行必然携带可查询 embedding）；原始 lance 行 `vector_dense` **NON-NULL len=64**、`vector_sparse` **NON-NULL**——embedding 实体在盘 |
| (e) 冻结公开召回 + 会话恢复 | PASS | CLI `mnemoseed-local recall "verdant-obelisk quartz-vault cache index"`（`sessions\recall-cli.txt/.json`）：2 条目均带来源引用（`provenance_source: "opencode-chat"` / `"memory.remember"`），`coverage: vector_hits=2 profile_chunks=2`；`/session/recent` 读回见 (b)/(c) 证据文件；终态完整清单 `sessions\session-recent-final-default.json`（4 会话组 + pin 组，见下） |
| (f) 注入送达代理面 | PASS | 会话 2（`ses_e4bda055affe…`，prompt 含实体锚）：hook-debug `2026-11-07T03:56:02.488Z`（= 本地 11:56:02.488）`session-start injection {"groups":1,"injectedChars":597}` + 守护进程面同刻 `POST /session/recent -> 200`（11:56:02.486，与 hook-debug 行相差 2 ms，属两侧时钟渲染路径之差）；`sessions\session2-messages.json` 模型 reasoning 逐字「**The memory replay shows QZX-4417 quartz-vault cache index.**」、答案正文忠实引用被捕获事实——注入块 → 模型输入 → 答案引用三面闭合。同会话 T2 poll（`candidatesAboveFloor:2, injectedChars:0, armCleared:false`）为 **D7 seen-registry 去重**（T1 已投递同一内容，T2 正确不复投），非投递失败（见诊断分类学尾注） |

**链路结论：无 BREAK**——(a)–(f) 全部 PASS，无新增缺陷 issue。（b)/(c) 同时证明 PR #315 的持久化语义按设计工作：flush 过的数据在重启后存活，未 flush 的数据死亡（第 2 类的对照面）。

### 终态会话清单（boot6，`sessions=5` 读回，判别底账）

- `default`：`ses_e4bd043a…`（neg6 山峰问句，**已持久化**——持久化载体是 boot5 收尾 shutdown 的 teardown flush_all drain，**非** idle flush：此时 `capture.idle_flush_sec` 已热应用为 3600，清扫器不可能触达；daemon.log 12:11:36.884 teardown complete）、`ses_w320neg2`（crimson，清扫器 drain 12:02:12.855）、`ses_e4bda055…`（会话 2）、`ses_e4be4aae…`（会话 1，needle 原文行 + pin `649e7836…`）。
- `w320-b`：`ses_w320neg3`（azure-marrow）。
- **缺席**（均为正确缺席）：`ses_w320neg2b`（缓冲-only 死亡，第 2 类）、`ses_e4bd505c…`（neg1，从未 ingest，第 1 类）。

## 诊断分类学（六类故障 × 相互可区分的判别证据）

构造原则：**负向模拟按其真实发生机制构造，不伪造状态**。每类给出：构造方式、判别性可观测量（与相邻类的区分点）、证据文件。未诚实可构造的类如实记 untested，绝不捏造。

| # | 故障类 | 构造 | 判别性可观测量（区分点） | 证据 |
| --- | --- | --- | --- | --- |
| 1 | 缺失捕获（no ingest） | CONSTRUCTED：经 serve API 创建宿主会话 `ses_e4bd505c…` 后**从不 prompt** | 宿主侧会话对象存在，守护进程侧**全零**：daemon.log 中该 session id 出现 0 次、无 202 行、无任何计数增量、所有读表面缺席。**区分点**：连 202 都没有（第 2 类有 202） | `neg1-session-create.json` + 对在盘 `mnemoseed-home\daemon.log` 复跑 `grep -c ses_e4bd505c…` = 0 + 终态清单缺席 |
| 2 | 缓冲-only（已承认未落盘即死亡） | CONSTRUCTED ×2：(i) 首次尝试：ingest 202 于 12:01:41.9（daemon.log），缺席检查 12:01:58/59（两读面均缺席），清扫器于 ingest 后 **+30.9 s**（12:02:12.9 `idle flush: drained 1 session(s): ses_w320neg2`，单样本）完成 drain——**先于 taskkill（~12:02:18）**，数据存活（构造失败本身即 PR #315 网络的实测窗口证据：观测样本 ≈31 s）；(ii) 热应用 `idle_flush_sec=3600`（v3000002）中和清扫器后重做：ingest 202（12:03:56）→ 1 s 内 taskkill → 重启 | **有** 202 行 + hook 计数 +1（`neg2-obs-before-kill.json`：3），但 kill 前 `/session/recent` 与 `/memory/recall` 均已缺席（write-behind 实证），**重启后仍缺席**；同一 payload 形状的对照对：清扫器 drain 过的 `ses_w320neg2` 存活、kill 前未 drain 的 `ses_w320neg2b` 消失。**区分点**：有 202 与计数（第 1 类没有）、读表面全缺席（第 3 类在正确 profile 下在场）、重启后消失（第 3/5 类重启后仍在）。该态的唯一持久痕迹是 daemon.log 的 202 请求行 | `neg2b-ingest-202.json`、`neg2b-recent-before-kill.json`（neg2b 缺席/neg2 在场）、`neg2b-recent-after-kill.json`（同判）、`neg2-config-set.txt`、`daemon-pid.txt` |
| 3 | 错误 profile（B 捕获，A 查询） | CONSTRUCTED：真实 profile `w320-b`（`POST /api/v1/profiles` 创建）下 ingest+flush azure-marrow 会话，双 profile 对查 | 数据**已持久化且已索引**但只在其捕获 profile 可见：`recall(profile=default)` 0 azure 命中（`coverage.profile_chunks=4` 全是 default 数据）、`recall(profile=w320-b)` 1 azure 命中（`profile_chunks=1`）；`/session/recent` 同构缺席/在场。**区分点**：行级数据在正确 profile 的读表面**在场**（第 1/2 类全缺席）；coverage 的 `profile_chunks` 按档案分离是诊断入口。相邻 ingest 边界变体：会话已被 profile 绑定后从另一 profile 再 ingest → 409 `ProfileMismatchError`（产品既有语义，本轮未另行构造） | `neg3-profile-create.json`、`neg3-ingest-202.json`、`neg3-flush-200.json`、`neg3-recall-default.json`、`neg3-recall-w320b.json`、`neg3-recent-{default,w320b}.json` |
| 4 | 索引覆盖缺失（行在、embedding 缺） | **AS-SPECIFIED UNTESTED**（产品面不可诚实构造：`upsert_chunk` 契约要求 dense，写路径上 embed 失败会使 drain 失败、数据停留在缓冲——落成第 2 类形态而非"无 embedding 的行"；不存在静默半行态）+ **相邻构造 CONSTRUCTED**（neg4 弃置家）：embedder 维度漂移（synthetic `dimension=128`）对 64 维既有 lance 表 | 相邻构造的判别观测量：`/memory/recall` → **HTTP 500**（维度不匹配，检索面整体不可用），而 `/session/recent`（行读）**仍返回该行**——「行侧在场 vs 索引侧失能」的分裂可观测量，诊断轴与规格所写同类（索引侧 vs 行侧），但形态是"检索报错"而非"静默漏检"。**区分点**：第 5 类的检索正常返回（截断由 payload 内 dropped_count 可见），本类检索直接 500 | `neg4-home\config.toml`（两态）；HTTP 500 的 ASGI traceback（lance `RuntimeError: query dim(128) doesn't match the column vector_dense vector dim(64)`）在 `sessions\neg4-daemon-up3.log`；漂移配置下的行读为存储级只读直读（守护进程已停，不经 HTTP）：`sessions\neg4-rowread-out.txt`（obsidian 行在场 = True，含 chunk_id 与 provenance） |
| 5 | 查询/排序 miss（已持久化已索引，但被 top-k 截断） | CONSTRUCTED：default 档案下两条不同合成会话（verdant + crimson）均在盘，`recall(query=crimson…, top_k=1)` vs `top_k=10` | `top_k=1`：entries=1、**`dropped_count=3`**、verdant 家族整链低于截断线；`top_k=10`：entries=4、`dropped_count=0`、两家族齐返；`/session/recent`（无排序；默认窗口 2 个会话组）不受 top-k 影响，返回的组内 chunk 完整。**区分点**：截断写在**同一个 recall payload 里**（`dropped_count>0` 且 `entries < coverage.profile_chunks`），行表面完整、抬高 top_k 即恢复——纯排序/预算问题，非数据缺陷。即 done-when 的「不承诺每条语义查询都把每条记忆送进 top 结果」的实测形态 | `neg5-recall-top1.json`、`neg5-recall-top10.json` |
| 6 | 从未送达（recall 已服务，注入通道未携带） | CONSTRUCTED（经真实宿主面）：neg6 会话 prompt 与任何已捕获内容**无实体锚交集**（山峰问句） | 直连 `recall` **满载**（4 条目、dropped 0、vector_hits=4——召回表面工作），但注入通道对会话空载：T1 跳过（`session-start injection skipped (selection=unresolved)`——#187 scoped-resume 契约的 fail-closed 面）+ T2 pull `items=[], non_focal_above_floor=4, slot_consumed=false`（focal 扫描无 cue 实体 → 零准入；`non_focal_above_floor` 是衰减健康总体的报告数，**不是**待投递数）；模型答案零记忆引用（自述「check mnemoseed memory? Probably unnecessary」）。**区分点**：第 5 类的截断在 recall payload 内可见，本类 recall payload 完整而**注入侧 payload**（session/recent 读 + recall-pending）为空——两具不同的"尸体"，诊断面不同 | `neg6-session-create.json`、`neg6-prompt.json`、hook-debug（neg6 四行）、`neg6-recall-pending-raw.json`、`neg6-direct-recall.json`、`neg6-messages.json` |

**尾注（T2 去重形态，不是故障类）**：会话 2 的 T2 poll（`candidatesAboveFloor:2, injectedChars:0, armCleared:false`）出现在 T1 注入**之后**——D7 seen-registry：T1 已投递的内容 T2 不复投。判别式：有前置的 T1 注入行（hook-debug 时间序）即去重，无任何注入行且 items 空即第 6 类。两态的 hook-debug 观测量几乎同形，**必须**结合 T1 行判读——这是本分类学的关键易混点。

## 复用而非重实现（PR #315）

本轮验证的持久化行为**就是**已合并的 PR #315 机制本体，零重实现、零产品代码改动：write-behind `/ingest` 202 + 显式 `/flush`（N3/N4/neg 路径使用）+ 空闲清扫器（主链 (b) 格的 flush 载体，`idle flush: drained 1 session(s)` 日志行即 `daemon/idle_flush.py` 的产品输出）+ `capture.idle_flush_sec` 经 configwrite 热应用（N2 的构造前提，版本 3000002）+ teardown flush_all（boot6 收尾 shutdown）。重启存活 (c) 与缓冲-only (第 2 类) 是同一机制的正/反两面。source merge 未被当作部署：本轮跑的是 worktree `dcac6b0` 代码（`uv sync --frozen`），非任何已安装副本。

## 覆盖边界（本批证明什么、不证明什么）

- **证明**：单台 Windows 11 主机、单宿主面（opencode **serve**）、synthetic embedder（64 维哈希）、hosted 模型（big-pickle，cost 0）下，捕获→持久化→重启→索引→冻结公开召回/会话恢复→注入送达的全链路可用，verbatim 通道与溯源身份（source/host/session/chunk_id/audit）完好；六类故障有相互可区分的判别证据（第 4 类按规格 untested、相邻构造补轴）。
- **不证明**：(1) bge-m3 真 embedder 下的检索语义质量与排序结构（synthetic 哈希向量上的 similarity 数值无语义含义）；(2) `top_k`/focal floor/budget 的默认值在真实负载下的召回率——第 5 类恰恰演示了 top-k 截断是**设计内**行为；(3) Ubuntu/macOS 及其他宿主（codex/claude/zcode/cursor）——本批未触及，PRD-292 矩阵格不变；(4) 模型答案引用为单样本（一次会话），非稳定性声明；(5) 静默半行态（行在、embedding 空 null）在产品面不可构造，本批只能给出漂移形态的相邻证据。
- **冻结契约**：`/memory/recall`、`/session/recent`、CLI recall 全程只读使用；#187 的 scoped-resume T1 契约按原样触发（neg6 的 unresolved 跳过即其 fail-closed 面，非缺陷）。跨项目 general recall 契约零改动。

## 边界事件与偏差（全部如实记录）

1. **boot2 失控落真实家（本轮最重边界事件，本批唯一真实面写触）**：第一次受控重启的启动子壳丢失 `MNEMOSEED_LOCAL_HOME`（bash 工具调用间 env 不延续），守护进程解析到**真实家** `~/.mnemoseed-local` 并以 CLI `--port 27791` 绑定，运行约 33 s。健康检查的 `embed: bge_m3_onnx`（真实家 preset 形态）暴露异常后立即 `taskkill /F`。取证（`~/.mnemoseed-local/daemon.log` 该窗口 + 真实家快照）：boot 行/healthz 行写入真实 daemon.log；真实存储栈被**打开**（唯一文件触痕 `meta.db-wal` 头部，11:48:32.720，sqlite 连接）；`storage stack ready` 行证实 boot 时迁移检查为 no-op（migrations 已 16）；**未收到任何 ingest、未 drain、无 /flush、无 dream run**（dream trigger 因 ollama 不可用而 paused，只读探测）。当时真实家 daemon.log 中的 `POST /ingest -> 202`（11:49:00.740）经 hook-debug.jsonl 时间序核对**不属于 boot2**（插件该时刻零活动），归属真实守护进程自身流量。该窗口的逐字行摘录已**只读保全**为 `sessions\real-daemon-log-boot2-window.txt`（QA 修复轮补存：头部标注来源路径、抽取方法、窗口与归属注记，原文行未改动；boot2 的最后可观测日志行为 11:48:37.493，taskkill 时刻无日志行——11:49:00.740 ingest 的归属依据是 hook-debug 排除法，**不是** kill 时界）。进程台账（`sessions\daemon-pid.txt`）已同步修订：初版台账曾把该 ingest 误记为 boot2 收到，已按本节归属更正，并把全部时间戳改为日志溯源（taskkill 类无日志时刻如实标 [session record] 近似）。修复：一个披露的只读 `GET /healthz` on 7788 复核真实守护进程存活（uptime ≈34.9 h，未受扰，PRD-305 轮一边界事件 3 同型先例）+ 真实家快照 diff（下条）+ 以「env 与启动同命令、healthz 复核 embed=synthetic 后才继续」重跑重启格。方法论教训已并入本 PRD 清单。
2. **真实家前后快照 diff（同范围同格式，name+size+links 判据，PRD-345 配方）**：28 行 diff，逐项归属——(a) 本批可归因：boot2 的 `meta.db-wal` 连接头（事件 1）与 boot2 写入真实 daemon.log 的少量行；(b) 真实系统自身活动（非本批）：`chunks.lance` 3 个新文件（txn/manifest/data）、daemon.log 与 meta.db-wal 的其余增长、`~/.local/share/opencode/log/opencode.log` +23 KB 与 `shell/*.out` 新文件（owner 常驻 opencode 2.2.5 服务自己的会话活动）——7788 守护进程全程在捕捉 owner 实时会话（含本批会话文本中出现的合成锚词，那是生产系统的正常捕获行为，本批从未连接 7788 读取或使用任何真实记忆）；(c) `~/.config/opencode`、`~/.opencode`、`~/.local/state/opencode`、`~/.cache/opencode`：**零 diff**。快照前后同范围同格式，无中途缩圈。
3. **serve boot1 的 cwd 失误（已纠正）**：首次 serve 从仓库树内目录启动，config 发现沿 cwd 链读到了 owner 的项目级 `G:\Development\MnemoSeed\opencode.json`（只读）且项目 `.opencode/plugins/` 在扫描链上；即杀（taskkill，台账在案）并从隔离 `cwd\` 重启（boot2）。boot2 的 config 目录清单实测仅 `opencode-home`（隔离）+ `C:\Users\Little Star\.opencode`（PRD-345 EXP-C4 已裁定不可移的只读残留，本轮快照证实零写入）。
4. **PATH 黑名单剥离两次未遂（预检拦截，未遂）**：黑名单法因 Git Bash PATH 混合分隔符（`tr ':'` 拆坏 `C:\…` 项）两次漏放 `~/.local/bin/mnemoseed-local`——均被「`command -v mnemoseed-local` 必须失败」预检在**任何宿主子进程启动前**拦截，改 PATH 白名单后 SCRUBBED-OK。无任何子进程在未剥离状态下运行。
5. **N2 首次构造被清扫器抢先（如实记为构造失败）**：ingest 12:01:41.9 后，清扫器于 **+30.9 s**（12:02:12.9，单样本）drain，先于 taskkill（~12:02:18）——人工时序败给 20 s idle + 30 s tick，数据存活。该失败本身成为 PR #315 网络窗口的实测证据（观测样本 ≈31 s，单样本非上界证明）；随后以 configwrite 热应用 `idle_flush_sec=3600` 中和后重做成功。批内主链 config 的最终态（3600）与启动态（20）不一致已在此披露；两态下的会话恢复/召回行为均实测无差。
6. **`/session/recent` 的 `sessions` 参数上限**：请求 `sessions=10` 触发 422（schema `le=5`）；终态清单以合法 `sessions=5` 重取。此为请求校验面，非缺陷，不立 issue。
7. **进程台账**：全部探针进程 kill/正常退出并逐条记录（`sessions\daemon-pid.txt`）；终态 netstat：27791/27792/4591 全释放，仅余 owner 7788（PID 22260）与 owner 自己的 28894 服务；4096 全程零接触。
8. **托管模型回合披露**：三个 opencode 会话回合走自带 hosted provider（`opencode/big-pickle`，agent build），`cost: 0`、隔离数据目录无 auth.json、无凭据创建、无计费面；提示词均为合成内容。

## 理论锚

本批为验证与文档批次，不引入新特性、新记忆机制，无新增借用理论；不以上一轮未验证的神经科学类比支持任何链路或分类学结论。注入机制的理论锚（T1 会话启动回放 / T2 焦点扫描、budget 语义）已在 design/01 §4.6 与 PRD-B2.1 注册，本轮实测的是同一锚在 `dcac6b0` 上的端到端行为与六类故障的判别面。

## 残留未知

1. **bge-m3 对齐轮**：真 embedder（默认 preset）下的索引覆盖、召回排序与 focal 预算行为未测（500 MB 模型下载出本轮预算）；第 5 类的排序形态在真 embedder 下可能不同。
2. **第 4 类的静默半行态**：产品面不可构造（upsert 契约要求 dense）；若未来存储层出现"行在 embedding 缺"的路径（如稀疏驱动缺 sparse 能力的部分覆盖态），须先立工程批次再补该类证据。
3. **送达示例为单样本**：会话 2 的三面闭合是一次真实回合；引用稳定性、多锚并发、预算边界待后续轮扩样。
4. **`non_focal_above_floor` 的诊断语义**：该字段是衰减健康总体报告而非待投递数——插件 debug 面的 `candidatesAboveFloor` 名称易与 items 混读（本轮第 6 类与 T2 去重尾注的判读依赖此区分）；是否改名/拆字段属产品决定，移交主控分诊（观察，不立 issue）。
5. **宿主矩阵**：本批仅 opencode serve 一宿主一面；codex/claude/zcode/cursor 与 `opencode run --standalone` 面的等价链路未在本批重复（PRD-305 三轮证据维持）。
6. **清理**：`%LOCALAPPDATA%\Temp\w320-trace\`（守护进程家 ×2、opencode 隔离家全套、会话日志、探针脚本、进程台账、真实家快照与 diff）由 owner 按需删除；本轮按边界未执行任何仓库外删除。

## 验收记录

文档编写核对的证据**在盘**（`%LOCALAPPDATA%\Temp\w320-trace\`，逐件可开）：`mnemoseed-home\daemon.log`（逐请求行：4×202 ingest、清扫器 drain 行（11:45:27 / 11:56:42 / 12:02:12）、shutdown/teardown 五次、MCP 握手、422 校验行）、`hook-debug.jsonl`（会话 1/2/neg6 的注入与 poll 行含 `selection=%s` 字面占位符——PRD-305 V305-FOUR 第 10 条已记录的插件 debug 格式化缺陷复现，本批不修）、`config.toml`（终态含热应用 3600）、`sessions\` 下 **69 件**（不含 `tmp\` 目录），其中含 10 份 session-recent payload（s1 / after-restart / final-default / final-w320b / neg2 前后两份 / neg2b 前后两份 / neg3 双 profile 两份）、observability 四态快照、recall-cli 两份、probe_index.py + probe-index-out.txt、audit-needle-chunk.json、neg1–neg6 全套、daemon-up*.log 六份、**daemon-pid.txt 台账（QA 修复轮修订版：全部时间戳日志溯源，taskkill 类无日志时刻标 [session record]，boot2 条目按归属证据更正）**、real-daemon-log-boot2-window.txt（QA 修复轮补存的只读逐字摘录）、restart-survival-check.txt 与 neg4-rowread-out.txt（QA 修复轮补存的派生只读判别件，分别锚定 (c) 格与第 4 类行读证据）、precheck-debug-paths.txt、install-opencode.log、home-snapshot.sh + real-home-before/after/diff、openapi.json、serve 双 boot 日志、neg4 家三份 up 日志（up3 含 500 的 ASGI traceback）。**未以文件留存件**（仅存在于本轮会话记录，正文引用处已逐一改锚到在盘对应物或如实降级）：serve API 逐次 curl 响应头（各请求的 payload/结果均有对应在盘文件）、boot2 现场终止时的瞬时 netstat（台账已改为标注来源的时间戳，不再回填）、neg4 家 500 响应的完整 HTTP body（在盘对应物为 up3 日志的 ASGI traceback）、会话 1 模型回合的完整转写（未保存——(f) 格证据取自会话 2；会话 1 的 answer 行已在 (b) 格 chunk 逐字内）、运行时终端回显中曾被引用的两处判别标记（`NEEDLE-SURVIVES-RESTART` 与行读 grep 计数）——二者的在盘替代即上述两个 QA 修复轮补存件。独立 QA（首轮 CLOSABLE + 2 IMPORTANT 修复轮）、完整本地 gates、本 PR CI、merge 与最终验收均待主控记录；本批不声明 #320 完成。
