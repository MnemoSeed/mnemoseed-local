# PRD · Phase B 路线图（总立项）

> 依据：`docs/zh/design/mvp-design.md` v1.3 §6 Phase B（子项"立项时再定"，本文件即立项）+ B1 收口实弹发现（PRD-B1 人工验证记录）+ 用户优先级拍板（2026-08-18）：**按原计划续完整个 Phase B；验证不同模型的 dream 质量；积累小模型效能数据；优化细节配置让低配电脑也有高质量 dream 与长期记忆；长期记忆靠"反复观察 + 校验"固化正确知识**。
> 基线：commit `018967a`（B1.1 含），1073 passed / 3 skipped。

## 用户命题的工程对位（先诚实标注边界）

"小模型记忆会有偏差，但经长时间重复学习和教导应能改善并固化"——本架构已有对应机制：

- **折叠强化**：同一事实多次被提取会折叠合并且 confidence 逐次 +0.05（封顶 0.95）；命中即 reinforce 对抗 decay。→ "反复学习"的量产通道已在。
- **verify 交叉校验**（B1 刚交付）：每轮 dream 由第二模型过滤噪声/幻觉。→ "教导"的第一形态已在。
- **隔离与溯源**：分歧 → isolated 永不灭档；verbatim 原文永在，provenance 可回放。

**边界（如实）**：这套"重复 + 教导"只能滤掉**抽样噪声/个体幻觉**；**同源系统性幻觉投票无效**（设计稿决策 1 原文）。小模型长期记忆质量的最终防线仍是 verbatim 通道 + provenance + isolated 结构。Phase B 的所有实测工作，本质是把这句话从"设计预期"变成"有 bar 的事实"。

## 批次序列（定序 + 理由）

### B2 · 时序接续面（UX 小刀）——第一刀
- 痛点当日实证：新 session 无法按时间接上上一 session 结尾（本次 session 开头用户需手贴引用）。
- 范围：daemon `POST /session/recent`（profile 维度、倒序、最近已关闭 session 的尾部 N 轮 verbatim 原文）+ MCP 工具 `recent_sessions(n?)`；CLI `recall` 类表面不动。
- 风险面小（只读端点 + 网关映射），不触 capture/dream 核心链。
- 依据已验构件：chunk 带 `session_id` + epoch `ingested_at`（D3 已修）；`/memory/timeline` 的 recent-first 先例。

### B3 · 评测臂（eval harness + canary bar）——一切后续的地基
- 设计依据：§6 Phase B"评测矩阵（档位 × off/verify/vote）与 bar 立项"。没有 bar,"验证模型质量"就是感觉。
- 范围：
  1. **canary 工厂**：合成 user turns 语料（EN+ZH 偏好/决定/习惯类既定事实 + 噪声轮），写入 scratch store——被测事实集是确定性的，无需人工标注。
  2. **真实材料 replay**:B1 harness 同款只读快照复放（随数据积累，材料库只增不减）。
  3. **矩阵运行器**:A 模型 × 校验层（off/verify/判定座型号）× 档位参数；本机已在位模型全上（qwen3.5:9b / gemma4:e4b / qwen3.5:4b / qwen3:8b / qwen3:4b / gemma4:12b）。
  4. **指标与 bar**:canary recall（既定事实进 core 的比率）、junk 率（噪声/机械句误进 core)、判定质量、时延、token、fallback 率；首跑后把数值 bar 钉死进 PRD。
  5. **数据积累**:JSON 报告写 `~/.mnemoseed-local/eval/`（数据目录，不入 git 防膨胀）；每次批次收口摘要入 PRD。
- 形态：`src/mnemoseed_local/eval/` 子包 + stub-LLM 单元测试（harness 自身逻辑全 TDD;live 矩阵跑的慢，不进 pytest 门禁）+ `uv run python -m mnemoseed_local.eval` 入口（不加 CLI verb，先不出产品表面）。

### B4 · lite 档定版与档位标定——吃 B3 数据
- qwen3.5:4b（官方 lite 锚点）+ gemma4:e4b 作 A 候选，lite 窗口（8k ceiling）实测 → 型号定版；
- `core_confidence_floor` 数值标定（A 自报 confidence × B 判定分歧统计）；
- `dream.capture_only` 硬模式裁定（过不了 bar 的档位只推 capture-only——不发布毒药，设计原文）。

### B5 · vote 机制 + needs_reconcile 协同
- 单快照双相位 journal + 确定性 combiner（设计决策 1 的机制改动，诚实成本版）。体量与反射核对齐，须独立 PRD 立项；排在 B3/B4 数据到手后。

### B6 · 性能：drain 写序列化 / 批量提交
- A2.5 QA 观察 5：重载下事件循环瞬时 stall(/healthz p95≈609ms,dream worker 竞争 +35-40%)。收紧 drain 存储写路径。

### 挂起项（如实记录）
- **advanced 27B 档**：本机 8GB VRAM 跑不动 27B（设计线 16-24GB)；硬件到位前挂起。
- **BYOK**：设计已定"Phase B 后"才立项。
- **ensemble 高配仲裁位**：依赖 vote + advanced 档，随 B5 再议。
- **daemon 可靠性永久修复（2026-08-19 先前排末位；同日用户改判**提前为当前批次**——B2.1 T1+T3/T2 已收口，可靠性修复先于 T4/QA-7/B4，修完重装后再回头看跨 session 感知专题）**：MCP `remember` 多次报错（daemon 忙时超时；更严重的吊死形态——python 进程存活但 7788 监听消失，connection refused，2026-08-19 实锤一次，根因未明）。立案方向：吊死根因排查（uvicorn 吊死但进程不退的常见成因清单）、daemon 自愈（健康自检 + 监听丢失自动退出交给 OS 拉起的监护形态）、MCP 网关侧重试/退避与诚实报错。KISS 边界待立项时再定。
- **跨 session 时间感知**：2026-08-19 用户立项，2026-08-20 作为 **B2.4** 收口（见批次启动记录）。
- **daemon on/off 用户可控开关**：2026-08-20 用户立项，同日作为 **B2.5** 收口（见批次启动记录）。
- **宿主 plugin 统一安装面（B2.6 候选，2026-08-20 用户立项，排在 B2.5 之后）**：每宿主定制 plugin 作 daemon 接口，MCP+hooks 打包成单 bundle、单开关整体启停（对标 Claude Code plugin 形态）。依据调研 `docs/zh/design/research-opencode-plugin-bundling.md`（2026-08-20 落盘）：opencode 无原生 bundle/无 per-plugin 开关（TUI 开关 PR #42410 未合并）；plugin `config` 钩注入 `cfg.mcp` 有真实先例但存在初始化竞态 → **前置 T0 式本地探针**；`["spec", {enabled:false}]` options 元组可作自实现整体开关；长期跟踪 agent-plugins 标准（opencode issue #39937/#40993/#41561）。
- **多 session 互认知（2026-08-20 用户立项，排在 B2.6 之后立项研究）**：多 session 互相认知到同时在跑的 sessions 在做什么/做了什么的机制设计；与 B2.4 时间窗面同族（从"事后归因"到"并行互见"）。
- **多 DB 可插拔后端（2026-08-20 用户探讨立项，暂不入排期）**：长期方向——驱动层多后端（qdrant 候选头牌、sqlite-vec 轻量选项、lance 保留），按喜好自由配置。qdrant 侧已探明：原生 sparse 倒排+HNSW、进程外故障隔离是真实收益（RAM 税 100-300MB 用户认可）；**snapshot 用自卷 MVCC（写带单调 store_version、点时读 filter `store_version<=X`）可精确复刻 lance `table.version` 语义**；Chroma 评估出局（in-proc、无版本读、更重）。最终闸门不变：下一次 watchdog fire 的堆栈取证先点名 wedge，再定是否值得迁。
- **B2.3 挂起子项（2026-08-20 收口，squash `e95921b`，PR #23 → issue #22）**：boot 同步 dream 恢复挪出启动路径（RESUME 作业变体 + scheduler 首 tick 有界 drain 闸 `RESUME_DRAIN_TIMEOUT_S=600`；QA CLOSABLE，门禁 1338 passed / 3 skipped）。watchdog `daemon.log` 同日实弹首击（refused-grace fire，F2 僵尸按设计处死退场）。**取证修正（2026-08-20 架构师日志复核）**：当日实为 **5 次** refused-grace fire（02:29/10:47/12:02/13:07/13:34），且**均无 teardown 前行**——与 P1"关停卡 join"的根因形状不符：fire 是服务中途监听直接消失，wedge 机制未确证，"Lance 写死"假设单独解释不了 refused（堵死的 loop 仍有绑定 socket，probe 只会读 stalled）。决策闸门：dump 构建上线后的下一次 fire 的全线程堆栈。

## 门禁（每包不变）

每批次独立 PRD → TDD → 对抗 QA 自验 → 全量门禁（`uv run pytest -q` / ruff / format / mypy)→ 单 commit 收口 + 收口记录。

## 批次启动记录

- **B2 时序接续面**：2026-08-18 开工并收口（commit `1edda80`，1082 passed / 3 skipped）。`POST /session/recent` + MCP `recent_sessions(n_sessions?, n_per_session?)` 落位；hook 自动注入形态存挂起（依赖宿主插件上下文注入能力验证）。
- **装机实测（同日，用户授权）**：版本线归位 `0.0.1`（`1c9fe80`）；`uv tool install --force .` 装机；daemon 换新构建重启（B1+B1.1+B2 全部在位：config 表面见 `dream_verifier` 路由、`/session/recent` 对 live 数据返回真实 session 分组尾部）；`opencode.json` 注册 MCP 网关（绝对 exe 路径，主机名含空格走数组直传）；**live smoke 抓出真缺陷并修复**：stdio 道宿主页码（cp936）下 ensure_ascii=False 帧成乱码、text-mode \n→\r\n 双坑——`7023746` 强制双道 UTF-8 + 不换行翻译（回归测试复现了 live 同指纹的解码错位 byte 0xa1@1035）；仓根新增 `AGENTS.md`（session-start 记忆纪律 + 开发门禁，`c6e9db3`）。门禁复验 1083 passed / 3 skipped。**待用户动作：重启 opencode 使 `mcp` 配置生效**（配置仅在启动时加载）。
- **B3 评测臂**：2026-08-18 开工并收口。`eval/` 子包四件齐：canary 工厂（确定性双语语料 + 纯函数匹配器）、scratch rig（1:1 生产接线 + 界外零写入守卫）、度量/报告（canary_recall / noise_pollution / verify replay / 成本面，JSON 累积入数据目录 `eval/`）、矩阵入口（`python -m mnemoseed_local.eval matrix|canary`，探活跳过/退出码语义钉死）。1133 passed / 3 skipped（+50），门禁全净。live 矩阵首跑待用户授权（收口记录见 PRD-B3）。
- **B3.1 尺修正 + 云锚**：2026-08-18 立项、2026-08-19 收口。matcher 类根集 + 词条覆盖修订、报告 v1.1 全量 triple 载荷、离线 rescore（70 cell 零偏差实测）、`--extra-route` 云锚席位（Kimi-K3 入阵）；live 重跑 14 cell × 5 材料完成并与首跑成对照表；**重大发现：单跑数值不能当 bar**（输出形状不稳、确定性零产出、超时墙三桩，见 PRD-B3 收口记录）。1181 passed / 3 skipped。B4 前置立案候选：qwen3.5:9b verify 席零产出排查。
- **B2.1 自动回忆立项 + 捕获面基线修正①-④**：2026-08-19 立项（PRD-B2.1-auto-recall，理论锚 TA-1..6 定稿）。同日 live dogfood 连抓四层捕获缺陷并全部收口：assistant 拉取 SDK 绑定形（`97493c2`+`ae18bc6`）、生命周期映射（`c56e401`）、senior-QA 对抗复审收口批（确定性重扫/锚感知分片/关停 drain/缓冲修剪/TS 语法门禁+可观测 seam，PR #2 合并）。门禁 1193 passed / 3 skipped。T0 探针观测完成（注入面定案 system.transform 追加）；T1 起始回放、T2 中段 auto-recall 待实施。
- **B2.2 崩溃耐久**：2026-08-19 立项并收口（commit `c9040ac`，PR #4 → issue #3；PRD-B2.2-crash-durability，KISS 单机制——宿主会话史重生回放：ack 水位 + per-session FIFO 链 + node 行为挂架）。QA 两轮：首轮 NOT CLOSABLE（2 BLOCKER：发送钟水位、live/replay 到达序错绑）修复并挂架钉死；复审 0 BLOCKER，2 个同族新 IMPORTANT（宕机空洞、replay 指纹时序）+ 2 NIT 随批修净。收口 1199 passed / 3 skipped，合并后 main 复验 1200 passed / 3 skipped、门禁全净；本机 tool 环境重建、daemon 换新重启（gate ok）、hook 字节级 match（收口细节见 PRD-B2.2 收口记录）。下一刀候选：B2.1 挂起的 T1 起始回放 / T2 中段 auto-recall（T0 探针已观测，注入面定案 system.transform 追加）。
- **B2.1 T1+T3 成批收口（会话起始回放注入 + 消费证据守卫）**：2026-08-19 开工并收口（squash commit `da0152d`，PR #8 → issue #7）。hook 侧 `chat.system.transform` 回放注入（attempt-once 闸门三句语义、TA-5 围栏净化、4000 字符含围栏组头预算、尾切 <200 丢弃）+ T3 消费证据守卫（needle 派生实际注入切片、归一化钉死、citedChunks 一 chunk 一 session 一次、≤64 分批、无 watermark ack）；daemon 侧 `/session/recent` 新增 `exclude_session_id`（filter-before-grouping、幸存 cap）+ 新增 `POST /memory/reinforce`（≤64、空表 422、未知 id 容忍、profile-agnostic）。solution-architect 预评审 9 issue 全并入设计；senior QA 首轮 NOT CLOSABLE（0 BLOCKER / 3 IMPORTANT / 4 NIT）→ 修复 → 复审 CLOSABLE（另 2 NIT 随批修净）。门禁 1200 → 1213 passed / 3 skipped、ruff / format / mypy 全净（收口细节见 PRD-B2.1 收口记录）。下一刀候选：T2 中段 auto-recall 管线、T4 阈值标定（吃 B3 评测臂）、QA-7 abort 形态探针。
- **B2.1 T2 中段 auto-recall 管线成批收口**：2026-08-19 开工并收口（squash commit `613659d`，PR #11 → issue #10）。daemon 新增 `POST /session/recall-pending`（focal-only embedding-free 扫描、serve=mark-seen 锁内原子、budget_chars/slot_consumed 线形、tombstone 生命周期），config 三键 `capture.auto_recall*`（默认 off），ingest 同步预取 + 未捕获 session 200 no-op settle；hook 侧 armed∧acked 门控 awaited pull（300ms fail-open）、T1/T2 独立分支、wire 预算权威、无切片下限、消费证据通道原样贯通。QA 三轮（2×NOT CLOSABLE → CLOSABLE），门禁 1213 → 1253 passed / 3 skipped 全绿。下一刀候选：T4 阈值标定、QA-7 abort 探针。
- **B2.3 daemon 可靠性永久修复**：2026-08-19 立项并收口（用户同日改判提前；plan 评审 PLAN-WITH-ADJUSTMENTS；三路并行侦察 + 根因定案——uvicorn 先关 socket 后跑 lifespan teardown × executor `shutdown(wait=True)` 永久 join 卡死 worker；双 SWE 并行实现 watchdog ∥ 网关 retry）。交付 watchdog（进程内 daemon 线程 socket 探测 + `os._exit(1)` 快退）与 `CONFIG_DIR/daemon.log` 持久日志及网关 `GatewayClient` refused-only 单次快重试。QA 首轮 CLOSABLE（2 IMPORTANT 随批修净）→ 复审 CONFIRMED-CLOSED。门禁 1253 → 1273 passed / 3 skipped 全绿。收口细节见 PRD-B2.3。squash commit `cb85991`，PR #14 → issue #13。
- **B2.4 跨 session 时间感知（会话时间窗面 + 来源归因结构）**：2026-08-20 开工并收口（squash commit `ef3220a`，PR #17 → issue #16；PRD-B2.4-time-awareness，新增理论锚 TA-7..9）。daemon `POST /session/windows` + MCP `session_windows`（精确逐 session ISO 窗 + `chunk_count`/`active`/`window_truncated = 过滤后总数超限`），recall 条目带 `session_id`/`ingested_at` ISO（graph 诚实 null/null），recent 组窗 + `self_window`/`self_session_id`（hook 单次 awaited 读取零新增调用），hook 自锚行 `<session-self/>` + 组头 `started=`（旧 daemon 逐字节回退）、共享 `escapeAttr` 防御。solution-architect SHIP-WITH-ADJUSTMENTS 全并入（含 orchestration 调整 2 条）；三路并行 SWE（A daemon ∥ B 网关 ∥ C hook，文件面互不相交）；QA 首轮 NOT CLOSABLE（3 IMPORTANT：truncated 差一 → `page.total` 语义、`?` 组计数伪造 → honest null、`capture.sessions()` 迭代竞态 → 专用 `_turns_lock`；3 NIT 随批修净）→ 修复轮 → 复审 CLOSABLE（变异体全灭，遗留 3 记录级 NIT 入 PRD）。门禁 1273 → **1297 passed / 3 skipped** 全绿。下一刀：**B2.5 daemon on/off**（设计定案已就位，见挂起项）。
- **B2.5 daemon on/off 用户可控开关（记忆服务启停 + 持久禁用状态）**：2026-08-20 用户同日指令立项并收口（squash commit `969ff90`，PR #20 → issue #19；PRD-B2.5-daemon-onoff；理论锚"无借用——工程控制面"）。CLI 平铺 `off`/`on` + `up` 第一闸 rc 1 拒绝（stderr 字节钉）；哨兵文件 `CONFIG_DIR/daemon.off` 持久禁用（刻意置于 configwrite/DB-primary 之外——version_id 槽位移不重踏、无 DB 陈旧值复活洞）；`off` 定序 **marker-first → POST → ≤15s 轮询**（watcher/up 复活窗封死）+ 五分支存活感知报告 + 1s 探针上限（`_probe_client`）；daemon `POST /daemon/shutdown` respond-then-exit、模块级 `intentional_shutdown` = `disarm()` 先于 `request_shutdown()`（F2 互咬防护、QA-4 drain 保住、顺序有钉）；网关 disabled 诚实文案（标准 hint 字节不动、disabled 文案字节钉）。solution-architect SHIP-WITH-ADJUSTMENTS 全并入；双 SWE 全并行；QA 首轮 CLOSABLE 有条件（3 IMPORTANT：refused 无存活感知、marker 后置复活窗、disarm 顺序无钉）→ 修复轮（1323）→ 复审 CLOSABLE（新 NIT-A 宽捕获指引 / NIT-B 探针 30s 超时破预算）→ 微修 → 三审 CLOSABLE 零遗留（3 记录项入 PRD）。门禁 1297 → **1327 passed / 3 skipped** 全绿。下一刀：**B2.6 宿主 plugin 统一安装面**（前置 T0 式探针）；其后**多 session 互认知**研究专题。
- **F2 根治（daemon zombie 机制级消灭，2026-08-20 用户指令立项并收口，squash `02ca93d`，PR #26 → issue #25）**：`util/DaemonExecutor`（daemon=True workers，永不入 `_threads_queues`/不被 `_python_exit` join）替换 DreamWorker/HybridRetriever 的 TPE + ingest scan 上 daemon 单例池（第二僵尸向量 anyio WorkerThread 一并消灭）；stop/close 有界（5s/2s，压在 watchdog ~11-14s 击杀线下，预算表钉死）；不堵 loop、超时**就地废弃**（journal 幂等兜底）；watchdog fire 自带全线程 `faulthandler` 取证 dump，exit 入 finally 无条件。QA 首轮 CLOSABLE → 修复轮 → 门禁 1349 passed / 3 skipped 全绿。下次 fire 将现场指出 wedge 确切栈位。
- **Wave-2 三流并行（2026-08-20 开工并收口，squash 占位 `TBD-FILL-IN-PLACEHOLDER`，PR #待定 → issue #待定；架构师计划评审 PLAN-WITH-ADJUSTMENTS 全并入）**：**W-A near-dup 预筛**：remember 探针从 O(N) 全表 Python 扫改为"profile-scoped WHERE + 有界 top-K 扫描 + 精确余弦重排"（扩窗守卫 K×4/+50、确定性 tie-break dense desc→chunk_id asc、扩窗计数近似并发热如实记）；**W-B harness 抗坍缩（B4a）**：reflect 席坍缩分类器（verbatim `[]`+completion≤2 指纹）→ 既有重试环接管（cap 3 次）、`ReflectCollapseError`、报告加性字段（collapse_attempts/recovered/seat_seed/policy）、ollama 席固定 seed 42（云席豁免）+ `--no-seat-seed` 逃生门、rescore 政策保真、legacy 报告 from_dict 默认 "none"（QA IMPORTANT-2 翻转）；**W-C drain 下 loop（B6 首件）**：`mnemoseed-drain` 单 worker DaemonExecutor lane（completed-applied ack 语义不动：先 drain 完成再 ack、prune+relay.flush 原序），teardown 在 stores.close 前有界排空（`DRAIN_STOP_TIMEOUT_S=2.0` 全队列预算、失败另计另告警、尾部放弃由 B2.2 replay 如实兜、teardown 序 loops→memory→dream→drain→stores 重排后 margin~1s 如实记）。QA 三流 CLOSABLE 有条件（7 IMPORTANT + 5 NIT）→ 三路修复轮全净（oracle 钉补：cosine-vs-L2、排序方向、扩窗双沿；静默吞异常击杀；标签诚实化；术语纠偏 "capped top-K scan"）。门禁 1349 → **1376 passed / 3 skipped** 全绿。
