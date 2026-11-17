# PRD · 历史会话导入（#394）

> 依据：issue #394 + owner 2026-11-16 指令。用户在换到 Claude 前，Codex 里已有 468 个
> rollout（265 MB 压缩，2026-09-10 .. 2026-11-01，14,815 条 user/assistant 轮次），
> 而产品没有任何入口把它们放进 profile。hook 回放能跑通但太慢，本批给出可用的导入路径。
> 前置：#387 已在 `origin/main`（`afd0222`）；批分支从该点切。
> 设计评审：solution-architect 2026-11-16 **SHIP-WITH-ADJUSTMENTS**，四条调整已逐条落入本文（见 §9）。
> 理论锚：本批**不借用**任何认知科学规律，理由见 §8。

## 1. 目标（用户可感知）

一台已装 `mnemoseed-local`、Codex 里跑过几个月的机器：

1. 跑 `mnemoseed-local import --host codex_cli` → 现有 rollout 逐字进入 profile，不需要手写脚本。
2. 跑到一半被 Ctrl-C、断电或 daemon 重启 → 再跑一次从断点继续，**不产生重复 chunk**。
3. 跑到一半看得到进度：已处理几个会话、已入档几轮、产生多少新 chunk。
4. 结束后拿到一份诚实报告：扫到 / 已导入 / 跳过 / 新增 / 强化 / 待协调 / 抑制的积分，每一项都对得上账。
5. 导入**不**触发 dream 风暴：不会因为一次历史回填凭空排出上千个强制巩固事件。
6. 正在使用的 Codex 会话不会被导入碰到：拒绝并说明原因，不 settle 它。

## 2. 冻结设计

### D1 动词面

```
mnemoseed-local import --host codex_cli
                       [--source <dir>] [--profile <id>] [--limit N]
                       [--dry-run] [--json] [--yes]
```

- **顶层动词**，与 `recall` / `remember` / `forget` / `dream` 同族：都是面向 daemon 的用户数据操作。
- 不放进 `host` 组：`host` 的职责是 adapter 生命周期（写宿主配置文件、探测安装状态），导入不碰任何宿主配置；加第四个动词会改变既有测试钉住的组契约。
- 不叫 `ingest`：与 daemon 端点同名会误导，且没表达"回填"。
- `--source` 默认值由 host 模块给出（`~/.codex/sessions`），不在 importer 里写死。
- `--profile` 默认 `default`，与 hook 写入口径一致。
- `--dry-run` 只枚举并报预计规模，不发任何 POST。
- `--yes` 跳过确认；无 `--yes` 且非 TTY 时拒绝执行（长任务不能无人值守地误触发）。

### D2 会话身份与序数

**一个 rollout 文件 = 一个导入会话，`session_id` 取文件名里的 run uuid。**

理由（实测，468 个文件）：`session_meta.session_id` 是**对话** id，跨 run 不变；71 个对话 id 对应 468 个文件，其中 35 个对话跨多个文件。每个 rollout 文件**各自从 ordinal 0 编号**：

| 对话 | 文件数 | 各文件 ordinal 区间 |
| --- | --- | --- |
| `01a0f786-…` | 5 | 0..276 / 0..71 / 0..55 / 0..43 / 0..25 |

daemon 的 per-session 水位按 `(profile, session_id)` 记，跳过 `<=` 水位的序数（`daemon/ingest.py:191-203`）。若用对话 id，第二个文件从 0 开始，整段会被当成重放吞掉。

- 代价：一个被 resume 过的对话在记忆里表现为多个会话。这是**诚实的分片**，报告里按文件计。
- 同一个 bug 存在于**在线 hook 路径**，已另开 issue #395（本批不修，不在范围内）。
- 序数取 rollout 记录自带的 `ordinal`，缺失时退回行号——与 `hosts/codex_cli/transcript.py` 现有规则一致。
- `session_meta` 缺失的文件不猜身份：跳过并在报告里计为 `skipped-no-meta`。

### D3 速度：embedding 复用

`capture/scorer.py:286` 每一轮都把 recent 窗口里的每个文本**重新 embed 一次**，而这些向量在上一轮刚刚算过。窗口由 `capture/pipeline.py:317-325` 维护、上限 `recent_capacity`（daemon 用默认 16）。

- **在 `TurnScorer` 内加有界 memo**（文本 → 向量，FIFO，256 条）。原型向量已经是同样的缓存形态（`scorer.py:250-261`），这不是新机制。
- 正确性前提：embedder 是文本的纯函数。用计数 embedder 钉住（AC11），并对同一 fixture 会话断言打分结果逐字节不变。
- 量级（**估计，不是承诺**）：今天每轮最多 1（当前文本）+ 16（窗口重算）+ 1（chunk 文本，`stamper.py` 里是另一种带角色前缀的字符串，不共享缓存）≈ 18 次 embed；修后约 2 次。实测每轮 6–26 s，若 embedding 占大头，预期 3–8× 加速，全量从数十小时降到数小时。**PRD 只承诺"导入变得可行（过夜级）+ 断点续跑"，不承诺具体小时数**；批次内必须实测一个真实会话并记录数字。
- **被否决的替代**：
  - pipeline 传向量：要新增 `recent_vectors` 参数或改 `_recent` 的存储形态，动的是被测试钉住的消费者契约，收益与 memo 相同。
  - 导入跳过 scorer：F2 标注是每个 chunk 的 stamp 组成部分，跳过会让导入产生的 chunk 成为**另一种数据**。
  - 多会话并行 drain：`DrainLane` 刻意 `max_workers=1`（`app.py:627-628`），并行只会在 store 写锁和 embedder 上互相争抢。
  - 悄悄调大 `recent_capacity`：会改变 F3 新颖度语义，用性能动机改语义是禁手。

### D4 池积分抑制

每个 drain 的 turn 都会把 S 记入 profile 池（`capture/pipeline.py:333-337`），池到 50 触发 `FORCED_CONSOLIDATION`（`capture/pool.py:7`）。14,815 轮按每轮约 3 点估算会排出 **900–1,500 个强制巩固事件**：没有绑定 dream 路由时它们堆在 relay 里，一旦绑定就变成上千次 LLM 反思并永久污染 `sqlite_graph`。池的语义是"近期待处理经验带来的巩固压力"，历史回填不是这种压力。

- `SessionEndRequest` 增加 `credit_pool: bool = True`（additive，缺省逐字节不变，沿用 `transcript_ordinal` 的 additive 先例，`schema/turn.py:102-106`）。
- 由 `ScoringPipeline.drain` 透传；为 False 时跳过 `add_points`，但把**被抑制的 S 总和记入统计**，报告里如实打印。
- 结果：导入不产生任何池事件，`/session/end` 的 relay flush 成为空操作。
- 明确后果：导入不触发 dream；用户需要时自己跑 `dream --once`。
- **被否决**：靠"provider 未就绪就延迟"兜底——延迟的洪峰会在路由绑定那一刻引爆，且队列无界。

### D5 断点续跑账本

daemon 的 per-session 水位**不能**当断点：它是内存 dict（`daemon/memory.py:671`），且在 settle 时被 pop（`memory.py:2314`）。断点必须在客户端。

- 账本文件：`~/.mnemoseed-local/import-ledger-<host>.json`，原子写（`hosts/common/config.py` 的 `write_atomic` 先例）。
- 每条 rollout 记：路径、大小、mtime、`session_id`、`settled_through_ordinal`、outcome 计数、状态。
- 语义是"已 settle 到序数 X"：
  - 只有 `/session/end` 返回 200 才落账（ack = completed-applied，`daemon/ingest.py:288-298`）。
  - ingest 与 settle 之间被杀：重跑时 ingest 被水位去重，缓冲的 turn 恰好 drain 一次。
  - daemon 重启：内存缓冲与水位都没了，账本从最后 settle 的序数重放——正确，因为未 settle 的缓冲随进程消失。
- rollout 在导入后继续增长（append）：从 `settled_through_ordinal` 之后继续，天然正确。
- daemon 侧不新增任何持久化，不改 schema。

### D6 活跃会话拒绝

settle 一个正在使用的会话会关掉它当前的 turn，后续 hook ingest 会 409（`daemon/ingest.py:208-209`），等于打断用户正在跑的 Codex。

- 新增只读端点 `GET /session/active?profile_id=…` → `{"sessions": [...]}`，返回 segmenter 的缓冲会话集（`capture/pipeline.py:75` 的 `sessions()`）。
- importer 对集合内的 rollout **跳过并报告** `skipped-active`，绝不 settle。
- 这是尽力而为，且比初稿更弱：`InMemoryCapturePipeline.sessions()` 反映的是 `submit_turn`，
  只有轮次闭合才会触发；一个仍开着单个 user turn 的活跃会话**不在**集合里。尚未 ingest 过任何
  事件的活跃会话同样不在集合里。缺席不能当作"不活跃"的证据，报告措辞不得声称"保证不碰活跃会话"，
  只能表述为尽力而为检查。

### D7 进度与报告

- 运行中：每个会话一行到 stderr（`123/468 <rollout> turns=… new_chunk=… elapsed=…`）。ETA 要么不给，要么给宽区间——每轮成本随文本长度变化，给点估计是撒谎。
- 结束：stdout 一份报告（`--json` 同时给机器可读），字段全部可对账：

| 字段 | 含义 |
| --- | --- |
| `sessions_scanned` | 枚举到的 rollout 文件数 |
| `sessions_imported` | 本次真正跑完 settle 的会话数 |
| `skipped` | 按原因分：`already-imported` / `active` / `no-meta` / `zero-turn` / `corrupt` |
| `turns_ingested` | 发出的 `/ingest` 数 |
| `turns_deduped` | 被水位判为重放的数（202 且 `ack_ordinal` 命中） |
| `new_chunk` / `reinforced` / `needs_reconcile` | 漏斗 outcome 计数 |
| `pool_points_suppressed` | D4 抑制掉的 S 总和 |
| `elapsed_s` | 墙钟 |

- **口径裁定**：漏斗**不丢 turn**——逐字契约要求每个 turn 都写库（`capture/pipeline.py:396-398` 的 docstring、`WritingPipeline.drain` 的 `write_many`）。报告**不得**出现 "kept / dropped"；"28 个 chunk / 100 轮"是 **new_chunk 率**，其余是 reinforced 或 needs_reconcile。任何文档照此写。
- 禁语沿用 PRD-387 §6：不得出现「记忆已激活」「模型会主动调用」。

### D8 host seam

v1 只支持 `codex_cli`，但读取面留缝：

```
cli.py:  IMPORT_SOURCES: dict[str, str]   # host -> 模块路径（与 HOOK_HOSTS 同形）
hosts/<host>/transcript.py:  iter_rollouts(root) / read_session_meta(path) / read_turns(path)
importer/:  runner.py, ledger.py           # 不直接 import 任何 host 模块
```

`claude_code` / `cursor` / `zcode` 之后各自加一个 `transcript.py` 与一条注册项即可；importer 本身与宿主无关。v1 不做的理由：它们的落盘格式各不相同，放进本批只会把批次撑爆而不增加设计价值。

### D9 导入客户端

- 用 CLI 的 `DaemonClient`（可配超时，settle 建议 ≥ 600 s），**不用** `hook_client`（2 s 预算，每个 settle 都会超时）。
- **actor 必须是 CLI，不是 `hook`**：`observability.note_capture_ingest` 只认 `actor == "hook"`（`daemon/ingest.py:174`），回填不能被算成"在线 hook 捕获"的证据。
- profile 路由沿用现有规则（显式 `profile_id` + 绑定 agent 在 drain 时解析），importer 不新增路由规则。

## 3. 切片

| 切片 | 内容 | 依赖 |
| --- | --- | --- |
| S1 | `TurnScorer` embedding memo + 计数 embedder 钉 | — |
| S2 | `SessionEndRequest.credit_pool` 透传 + `GET /session/active` | — |
| S3 | `importer/`（runner + ledger）+ `codex_cli/transcript.py` 扩展（`iter_rollouts` / `read_session_meta`） | S1, S2 |
| S4 | `import` CLI 动词 + 报告 + 文档（README / skills 动词表） | S3 |

**同批必须更新的既有契约钉**：

1. `tests/test_scorer.py` 里 `recent_texts` 的既有断言（签名不变，行为必须逐字节一致）；
2. `SessionEndRequest` 的 wire 形状测试（缺省字段逐字节不变）；
3. CLI 动词表快照测试（新增 `import`）；
4. `skills/mnemoseed/SKILL.md` 的动词清单；
5. 冻结 HTTP 路由清单测试 `tests/test_dream_status_payload.py::test_external_surface_inventory_equals_frozen_baseline`
   （S2 新增的 `/session/active` 已在基线里；S3/S4 不新增路由，必须保持绿）；
6. `tests/test_sqlite_graph.py` / `tests/test_sqlite_meta.py` 的 autouse re-register fixture
   （`test_registry.py::_clear_registries` 清空共享驱动注册表后重新注册），属于既有护栏，必须保持绿。

## 4. 验收标准

- AC1：`import --help` 列出 `--host codex_cli`；未知 host 非零退出并打 usage。
- AC2：缺 `zstandard` 时，在**第一次 `/ingest` 之前**非零退出（假传输断言零 POST），消息带 `[codex]` 安装提示。
- AC3：预检时 daemon 不可达 → 非零退出，账本不变，零 ingest。
- AC4：同一 fixture 语料跑两次 → 第二次全部 `already-imported`；chunk 数与池状态不变。
- AC5：会话中途被杀（已 ingest、未 settle）→ 重跑每个缓冲 turn 恰好 drain 一次：无重复 chunk、无双重积分、账本一致。
- AC6：导入中途重启 daemon → 从账本最后 settle 序数重跑，最终 chunk 数与不中断单跑一致。
- AC7：池抑制——用记录型 pool sink，导入 settle **零**池事件，且被抑制的 S 总和出现在报告里。（变异：把 `credit_pool=False` 从链上任何一处拿掉都会被这个 sink 抓住。）
- AC8：报告词汇是 `new_chunk` / `reinforced` / `needs_reconcile` / `deduped`；没有 kept/dropped；每个 ingest 的 turn 都有归属。
- AC9：活跃会话——会话在 daemon 缓冲集内时，导入跳过并给出原因，其它会话继续，且**不** settle 它。
- AC10：截断/损坏的 `.jsonl.zst` → 作业继续，文件计为 `skipped-corrupt`。
- AC11：memo 化——计数 embedder 证明同一会话内窗口文本不再被重复 embed；打分结果与非 memo 版逐字节相同；只传 `recent_texts` 的既有调用方行为不变。
- AC12：wire 兼容——不带 `credit_pool` 的 `SessionEndRequest` 行为逐字节不变。
- AC13：actor 诚实——导入流量不推进 hook 捕获观测闩锁（假 observability 断言 `note_capture_ingest` 未被调用）。
- AC14：逐字契约——store 里一条 fixture chunk 的文本与原始 turn 逐字相同，且带完整 stamp。
- AC15：有效 profile 路由——绑定 agent 的导入落进绑定 profile，语义与 hook 流量一致。
- AC16：**多文件对话不丢数据**——同一 `session_meta.session_id` 下两个各自从 0 编号的 fixture rollout，两条都被导入（对应 #395 的暴露面，本批以文件身份规避）。

**变异体可能穿过的缺口，必须补钉**：

- AC-M1：`--dry-run` 真的不发 POST（假传输断言零请求），且报出的 turn 数与实际导入数一致。
- AC-M2：账本只在 200 之后写；把 settle 改成非 2xx 时账本不得前进。
- AC-M3：`skipped-active` 与 `already-imported` 不互相误报。
- AC-M4：settle 超时用的是导入客户端的预算，不是 `hook_client` 的 2 s（把超时换回 2 s 必须让测试变红）。

## 5. 不做什么

- 不做 `claude_code` / `cursor` / `zcode` 的导入：只留 seam，不实现读取器。
- 不做 ChatGPT 导出（`conversations.json`）的导入：格式、身份、时间线都不同，另开批次。
- 不做 redaction：逐字契约要求原样入库，历史文本里的密钥会原样进入 profile。PRD 与报告必须写明这一点。
- 不做 daemon 侧后台导入线程：CLI 驱动，daemon 保持请求作用域。
- 不做并行 drain、不做批量 embedding 端口改造（后者是更大的驱动层工作）。
- 不修 #395（在线 hook 的 resume 序数碰撞），只在导入侧规避。

## 6. 风险、边界与禁语

- **速度数字是估计**：PRD 不承诺小时数，只承诺"可行 + 可续跑"；批次内实测一个真实会话并记录。
- **隐私**：历史 turn 里有明文密钥（实测在第一份 rollout 里就见到一个）。导入按契约原样存储，v1 不做脱敏。报告与文档必须写"导入的是 Codex 记录的原样内容"。
- **宿主脚手架**：历史 user turn 里混着 `<environment_context>` 一类注入块，逐字契约照存。它们会抬高新颖度，不做特殊处理。
- **drain 单线程**：导入期间在线 hook 的 drain 排在后面（`app.py:641`），可能给在线会话增加数秒延迟。PRD 记录，不优化。
- **`session_meta` 覆盖**：实测 468/468 都有，但设计仍容忍缺失（跳过 + 计账）。
- **禁语**：「记忆已激活」「模型会主动调用」「导入后立刻变聪明」；报告只描述账目。

## 7. 门

`uv run pytest -q`、`ruff check`、`ruff format --check`、`mypy src` 全绿；closeout 另需独立对抗性 QA 无 BLOCKER。

## 8. 理论锚

**借用：无。**

本批是传输与机制工作：它把既有语料送进**已经锚定过的**漏斗，本身不引入任何认知科学主张。支配导入数据的锚是设计 01 里已登记的 Fuzzy-Trace（逐字/要点双轨）、STC（经池的选择性编码）、编码特异性（stamper 线索）——本设计既不援引也不修改它们。

D4 的池抑制是**机制裁定**，依据是池自身的设计语义（"近期待处理经验的巩固压力"，设计 01 §1.3），不是借来的规律。D3 的 memo 是纯工程优化，不得包装成理论。

**未借用（明确排除，照 00 §2.4 顺延）**：不引用「记忆需要重复巩固」「睡眠中重放」「左右脑分工」一类流行神经神话；本批不产生任何认知增强主张。

## 9. 评审记录

solution-architect 2026-11-16 裁定 **SHIP-WITH-ADJUSTMENTS**，四条调整与本批的落点：

| # | 调整 | 落点 |
| --- | --- | --- |
| 1 | daemon 水位不是断点机制（内存 + settle 时 pop） | §D5 客户端账本 |
| 2 | 必须显式裁定池积分（否则 ~900–1,500 个强制巩固） | §D4 `credit_pool` |
| 3 | 导入客户端不能用 `hook_client`（2 s 预算） | §D9 超时 + actor |
| 4 | 预检拒绝项（zstandard / daemon / 活跃会话）必须写死 | §D6 + AC2/AC3/AC9 |

评审同时纠正了三处事实，已并入正文：

- 「漏斗保留 28% 的 turn」是错的：逐字契约要求每个 turn 都写库，28/100 是 **new_chunk 率**，其余是 reinforced / needs_reconcile（§D7）。
- `session_id` 不在 `read_turns` 里，要从 `session_meta` 取；实测 468 个文件都有（§D2）。
- 历史语料含明文密钥与宿主脚手架，逐字入库（§6）。

批次内自发现的补充事实：`session_meta.session_id` 跨 rollout 复用、各文件序数从 0 重启，导致按对话 id 导入会丢整段——已并入 §D2 与 AC16，并另开 #395 记录在线路径的同源缺陷。

### S3/S4 收口记录（#394）

- **S3 落地**：新增 `src/mnemoseed_local/importer/{runner,ledger}.py`；`hosts/codex_cli/transcript.py`
  增加 `iter_rollouts` / `read_session_meta` / `validate_rollout`，轮次解析继续复用唯一的 `read_turns`。
  会话身份取 rollout 文件名的 run uuid，`session_meta.session_id` 仅作账本 lineage 元数据。
  账本 `~/.mnemoseed-local/import-ledger-<host>.json` 原子写，只在 `/session/end` 返回 200 后推进，
  续跑从 `settled_through_ordinal` 之后开始。客户端 `build_client` 用 `actor=cli` 与 900 s settle 预算；
  压缩帧完整性、zstandard 与 daemon 可达性都在第一个 POST 前预检。
- **S4 落地**：顶层 `import` 动词 + `IMPORT_SOURCES` 注册表（importer 不直接 import 宿主模块）；
  `--dry-run` 零 HTTP 且报告 turn 数；进度走 stderr，最终报告走 stdout / `--json`。README 与
  docs/guide 增加命令、续跑语义、逐字入库披露（明文密钥与宿主脚手架原样存储、不脱敏）以及
  "导入不触发 dream，需自行 `dream --once`"的后果。
- **修正一（HTTP surface）**：`pool_points_suppressed` 不另开端点，而是作为 additive 字段加在
  **`POST /session/end` 的响应**上，与当次 settle 的 `new_chunk` / `reinforced` / `needs_reconcile`
  增量一并返回；导入器从 settle 回复读取并计入报告。wire pin 见
  `tests/test_credit_pool_suppression.py` 的两条 settle-response 断言。
- **修正二（D6 措辞）**：活跃会话检查比原文更弱——`sessions()` 由 `submit_turn` 驱动，只覆盖已闭合
  的轮次；单个仍开着的 user turn 不会进集合。报告只能称"尽力而为检查"，缺席不等于不活跃（D6 已改）。
- **修正三（§3 pin 清单）**：补入冻结 HTTP 路由清单测试与 storage 的 autouse re-register fixture（§3 已补）。
- **变异门禁**：AC-M1 dry-run 零 POST 且 turn 数与实际导入一致；AC-M2 账本只在 200 后前进；
  AC-M3 `skipped-active` 与 `already-imported` 分账；AC-M4 settle 预算为 900 s 而非 hook 的 2 s；
  均由 `tests/test_import_core.py` / `tests/test_import_cli.py` 钉住。
