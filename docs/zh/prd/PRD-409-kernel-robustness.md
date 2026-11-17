# PRD · 内核健壮性（#409）

> 依据：issue #409 + owner 2026-11-17 "不设 supervisor，靠 durable 状态 + 清晰降级" 决定。
> 真实事故：daemon 被硬杀（无 shutdown 日志），未 drain 的 turn 丢失，客户端只能报连不上，
> 残留 5 个 stale MCP 子进程。设计评审 solution-architect 2026-11-17
> **SHIP-WITH-ADJUSTMENTS**，B1–B5 / M1–M5 已逐条落入本文（§9）。
> 前置：`main@435d715`（已升 Python 3.14）。理论锚：无新增，沿用设计 14 §8。

## 1. 目标（用户可感知）

1. daemon 在任何时刻被杀 → 重启后所有已 ACK 的 turn 恰好入库一次，不重复 chunk、不双计 pool。
2. daemon 不在时，CLI / MCP / hook 都给明确错误；hook 的 turn 不静默丢，落盘并在下次恢复。
3. 客户端不会把 bearer token 发给一个端口复用进程；陈旧端点给明确错误。
4. 新装用户无 Ollama 也能 capture / recall；dream 是 opt-in，配置不会因升级写出坏值导致 boot 失败。
5. 不引入 supervisor、Docker、多租户；wire 既有键语义零改动。

## 2. 冻结设计

### D1 队列落点：原始 IngestEvent 边界（B1）

真正的丢失点是 `capture/segment.py` 里未闭合的 in-flight turn（`_sessions` / `open_turn`），
不是 `capture/pipeline.py::_turns`。所以持久化发生在 **`/ingest` 入口、`segmenter.ingest()` 之前**：
先写 durable 表再推进水位再回 202。恢复时把未 applied 的原始事件**按序重放给现有 segmenter**，
状态机仍是事件流的纯函数，不复制 segmenter 逻辑。`/session/end` 与 `/flush` 也作为标记落表，
用于恢复 settled / rollout 状态。

### D2 双水位：seen / applied（B2）

- `seen_ordinal`：维持现有 202 前推进的语义，供客户端发送下限与 `deduped` / `ack_ordinal` 回包。
- `applied_ordinal`：仅在 chunk 写盘 + pool 计分成功后推进；崩溃重放门用它。
- 内存去重仍用 seen（wire 行为不变）；durable 重放只看 applied。
- 两者键同为 `(profile_id, session_id, rollout_id)`（`rollout_id` 可为 None，沿用 #395 语义）。

### D3 确定性 event_id 与 applied marker（B3）

`capture/stamper.py:409` 的 `chunk_id` 是 `uuid4()`，store 没有幂等键——重放会生成新 chunk、
把新写变成 REINFORCED、并重复加分。因此：

- 每条 ingest 事件带确定性 `event_id = hash(profile, session, rollout, ordinal, content)`。
- meta.db 新增 applied 表；drain 成功后在**同一 meta.db 事务**里写 applied marker 与 pool credit。
- 重放只处理未 applied 的事件；已 applied 的直接跳过，不重新 embedding（must-NOT）。
- **原子性边界**：chunk 写（LanceDB）与 pool credit（meta.db）跨存储，无法单事务。可观测残差是
  "chunk 已写但 marker 未写 → 整条重放 → near-duplicate 命中 REINFORCED"，本批如实接受，
  在 closeout 记录为 as-is boundary。

### D4 endpoint 验活：PID + instance_id 握手（B4）

Windows PID 可复用，单靠 `kill(pid,0)` 挡不住把 bearer 发给端口复用进程。

- daemon 写 `CONFIG_DIR/endpoint.json`：`pid` + 每启动 UUID `instance_id` + `base_url` + `started_at`；
  clean shutdown 删除。
- `/healthz` 响应 additive 增加 `instance_id`（该路径免鉴权）。
- 客户端先用 `/healthz` 比对 `instance_id`，一致才发 bearer；PID 只作快速预筛。
- 新增 `endpoint.resolve()` 统一四套 base URL 来源（`rest_client.py`、`hosts/common/daemon.py`、
  `cli.py` importer、`hosts/opencode/plugin.ts`）。显式 override（`--baseurl` / `MNEMOSEED_LOCAL_BASEURL`）
  跳过验活但打警告。路径必须基于 call-time `CONFIG_DIR` 以满足测试隔离。

### D5 hook 降级：有界 POST + JSONL spool（B5）

四个宿主的 hook 现在全部 `except Exception → debug()`，daemon 不在时 turn 永久丢失。

- `hosts/common/daemon.py` 增加共享 `post_capture()`：单次有界 POST（保持 ≤2s 预算，**不加 MCP 式重试**）。
- 失败即追加 `CONFIG_DIR/hook-outbox.jsonl` 一行，并 **always-on stderr 一行告警**（不依赖 debug 开关）。
- 下一次成功 POST 前按序 flush spool；daemon 的 ordinal 去重吸收重复。
- 保留期建议 24h / 每宿主 10MB，超限丢弃并永远保留 stderr 告警（可逆默认值）。
- TS 插件复用 `plugin.ts:513` 现有 debug sink 做落盘。
- 重试矩阵：MCP 1 次（已有 `reliable_client.py`）、CLI 0、hook 0+spool、importer 靠 client ledger resume。

### D6 渐进 onboarding 与能力报告（M2 / N1）

- load 期已拒 driver-without-model（`config.py:767-771`），默认模板已注释 dream 路由——**不要重做**。
- 真缺口在写路径：`ConfigWriteService.set` 单字段写可造出 `model=""`，重启 boot 失败。
  加跨字段不变量：写 `driver` / `model` 时校验**结果对**均非空，复用 load 的同一句 actionable 消息。
- 能力报告用 additive `/healthz.capabilities`（复用现有 `dream_route` + storage gate），
  status / doctor 只加显示行；不新建端点。核心 capture/recall 不依赖 Ollama 在代码中已成立。

### D7 importer 双 ledger 非冲突规则（M3）

持久化 seen 水位后，崩溃重发会命中 `deduped:true` 而不再分段。client ledger 仍是 resume floor，
daemon applied 水位驱动重放；两者键同为 `(profile, session, rollout, ordinal)`。写进契约并加测试。

### D8 boot 恢复钩子（M1）

架构文档提到的 `recover_running` **在代码中不存在**。在 `app.py` lifespan（idle sweeper 启动前）
引入显式 `IngestQueue.recover_pending()`，按 D1/D2/D3 重放。命名与行为写进测试。

## 3. 切片（按依赖顺序）

| 切片 | 内容 | 依赖 |
| --- | --- | --- |
| S0 | D6：configwrite 跨字段校验 + `/healthz.capabilities` + status/doctor 显示 | — |
| S1 | D4：endpoint.json + instance_id 握手 + `endpoint.resolve()` + 四路径接入 | — |
| S2 | D5：hook spool + always-on 告警 + 重试矩阵 | S1 |
| S3 | D1–D3 数据层：migration、event_id、双水位、applied marker、pool credit 同事务 | — |
| S4 | D8 + D7：202 前落 queue、drain 后 marker、boot replay、清理、importer 对齐 | S3 |
| S5 | SIGKILL 集成 + 对抗 QA（spare port / isolated home） | S0–S4 |

S0/S1 与 S3 文件面基本不重叠，可并行；S2 与 S1 同改 `hosts/common/daemon.py`，必须串行。

## 4. 验收标准

- AC1：kill -9 在 `/ingest` 202 之后、`/session/end` 之前 → 重启后该 turn 恰好一次（chunk 数 +1、pool `points_added` 增量恰好一次）。
- AC2：kill -9 在 drain 中途 → 无重复 chunk、无重复 REINFORCED、pool 无双计；applied 行恰好一条。
- AC3：已 settle 的 importer rollout 重跑 → 全部 `deduped:true`，ledger 与 daemon 水位不冲突，无新 chunk。
- AC4：endpoint PID 为死值 / 伪造 PID 复用但 instance_id 不同 → CLI/MCP/hook/importer 均报明确 `StaleEndpointError`，**抓包确认未发送 Authorization 头**。
- AC5：daemon 停止期间发 20 个 hook turn → spool 有 20 行、stderr 有告警；重启后首 POST flush，store 中 20 turn 各一次。
- AC6：全新配置 + Ollama 关闭 → 正常 boot，capture/recall 可用，`/healthz.capabilities.dream.ready=false` 带原因，status 显示清楚；dream verb 给明确错误而非 boot crash。
- AC7：`config set dream.llm.dream.driver ollama`（不设 model）→ 写时即拒，config.toml 不变，重启 boot 成功。
- AC8：全程无 supervisor / 无 Docker / 无多租户；wire 202/200 body 键集合不删。
- AC9：删除"重放跳过 applied 行" → 双计分测试红；seen/applied 合并 → crash-before-drain 测试红；endpoint 只比 PID → 伪造 instance_id 测试红；spool 改 debug-only → down 时落盘断言红；configwrite 跨字段校验移除 → 写单字段 driver 测试红；boot replay 移除 → SIGKILL 后丢 turn 断言红。

## 5. 不做什么

- 不设 supervisor、不做进程内重生 / 自动拉起 daemon。
- 不做 Docker、不做多租户 / 组织层。
- 不改 `/ingest` 202 / `/session/end` 200 及 `credit_pool` / `deduped` / `ack_ordinal` 语义与键名。
- 不重新 embedding（applied marker 保证不重处理）。
- 不让 hook 直写 daemon SQLite（所有权 / 锁模型破坏）。
- 不把"单写者"写进不变量（设计 14 已明确否决）。
- 不扩大 CI 超时（#406 单独跟踪）；SIGKILL 集成测试放 spare port + temp home，不进默认 10 分钟 job。

## 6. 风险、边界与禁语

- **性能**：每 turn 一次 SQLite INSERT（WAL + busy_timeout，复用 `sqlite_meta` 的线程连接）；PRD 要求 before-ack 持久化，批次内做隔离基准，记录单次写入延迟。
- **原子性边界**：chunk 写与 pool credit 跨存储，残差如 D3 所述，closeout 必须如实写。
- **平台细节**：Windows PID 创建时间校验可用性未验证；instance_id 握手是平台无关兜底。
- **plugin.ts sink 完整落盘格式**未验证，S2 前先探测。
- **禁语**：「记忆已激活」「模型会主动调用」；报告只描述账目与恢复状态。

## 7. 门

`uv run pytest -q`、`ruff check`、`ruff format --check`、`mypy src` 全绿；closeout 另需独立对抗 QA 无 BLOCKER。

## 8. 理论锚

沿用设计 14 §8：无新增借用。durable 队列 ≠ 睡眠重放；单写者 ≠ 记忆巩固；能力报告 ≠ 记忆增强。

## 9. 评审记录

solution-architect 2026-11-17 SHIP-WITH-ADJUSTMENTS，逐条落点：

| # | 调整 | 落点 |
| --- | --- | --- |
| B1 | 队列落在原始 IngestEvent 边界，不是 _turns | §D1 |
| B2 | seen / applied 双水位 | §D2 |
| B3 | 确定性 event_id + applied marker + pool credit 同事务 | §D3 |
| B4 | PID + instance_id + 免鉴权 /healthz 握手 | §D4 |
| B5 | hook spool + always-on 告警 | §D5 |
| M1 | recover_running 不存在，显式实现 recover_pending | §D8 |
| M2 | #390 load 已修，缺口在 configwrite | §D6 |
| M3 | importer 双 ledger 非冲突规则 | §D7 |
| M4 | 四套 base URL 收敛 endpoint.resolve | §D4 |
| M5 | SIGKILL 测试不进默认 10 分钟 CI job | §5 |

owner 授权的可逆默认值：hook spool 24h / 10MB，超限丢弃并保留 stderr 告警（§D5）。
