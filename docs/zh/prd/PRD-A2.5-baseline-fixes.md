# PRD · Phase A2.5 基线修正包

> 依据：`docs/zh/design/mvp-design.md` v1.3（定稿）§6 Phase A2.5。本包处置盲审 9 项 MUST 的代码侧，**先于 A3**。
> 基线：commit `a5adff9`，已复验 848 passed / 5 skipped（47.94s，2026-08-16）。
> 门禁：每任务 TDD；子代理从不提交；每任务完成后经 senior-qa-reviewer 对抗验收，FAIL 打回重做。
> 执行顺序：**批次 1 = T2 + T4（并行，区域不相交）；批次 2 = T1；批次 3 = T3**（T1/T3 都触 daemon/app.py 构造缝，串行避免冲突）。

## 任务 T1 · dream 异步化与失败退避

- 设计依据：设计稿 §4.3（失败退避）、§7.8、§7.9。
- 范围：
  1. dream 链（snapshot→reflect→merge）移出 daemon 事件循环：三个点火点（`/session/end` 的 relay flush、scheduler tick、`/memory/dream_once`）只投递 job，由 `asyncio.to_thread` 或专用 worker 线程执行；sqlite threadlocal 驱动已具备跨线程条件。
  2. 调度器失败退避：reflect 失败 / LLMUnavailable 时重置触发指纹（现指纹不变则永不重发），指数退避重发（上限 + 审计事件）；LLMUnavailable typed path 保持不变。
- AC：
  - AC1：dream 执行期间（注入延时 stub LLM）`/healthz` 与 `/ingest` 保持响应（断言响应时间阈值）。
  - AC2：reflect 失败后调度器按退避间隔重发；连续失败达上限后停止并审计；pending chunk 不丢、不重复 drain。
  - AC3：手动 dream 与调度触发并发不双跑同一快照（互斥语义不变）。
  - AC4：既有 dream/trigger 测试全绿（回归）+ 新增 AC1/AC2 覆盖。
- 代码锚点：`daemon/app.py:256-286`、`daemon/ingest.py`（relay flush）、`dream/trigger.py:439-511`、`dream/pipeline.py`、`daemon/memory.py:615-628`。

## 任务 T2 · 主检索轨 consolidated 过滤

- 设计依据：设计稿 §4.4。
- 范围：hybrid vector track 的 `ChunkFilter` 传 `consolidated=False`（`retrieve/hybrid.py:224-233`）；合并后 chunk 不进向量召回；Freshness Guard 行为保持；证据场景仅 provenance 追溯。
- AC：合并后 chunk 不出现在 recall；合并前出现（现状保持）；新增"双表示（chunk+node）不重复命中"测试；既有 retrieve 测试全绿。

## 任务 T3 · 配置面：五键注册表化 + budget 移除 + isolated 必需化 + 默认模型对齐

- 设计依据：设计稿 §4.1（budget 移除）、§4.3（pool_forced_cap）、§4.7、§4.8、§7.2。
- 范围：
  1. 五个新注册表键（校验 + 消费端热读 seam + 逐键回归测试）：
     - `dream.ensemble`（`off|verify|vote`，默认 `off`；与 `dream.hardware_tier` 联动：lite + `ensemble≠off` → 拒绝写入）；
     - `dream.core_confidence_floor`（[0,1]，默认 `0.0`；Merger 消费：core 路由但 confidence < floor → 确定性降级 isolated，**isolated 缺失时报报错不静默丢弃**）；
     - `dream.delta_budget_ceiling_tokens`（≥5000，默认 32000；DeltaPacker 消费，替代模块常量 `DELTA_BUDGET_CEILING_TOKENS`）；
     - `dream.hardware_tier`（`standard|lite|advanced`，档位锚）；
     - `dream.pool_forced_cap`（≥ floor，默认 `50.0`；ScorePool ctor 读 config）。
  2. `dream.token_budget_usd` 移除：旧配置给 deprecation 报错（LOCAL_TRACK 风格）；账本纯 token 化（移除 USD 估算与价目表）；清理 TokenLedger budget 参数与 reflect 的 within_budget 检查；受影响测试改写。
  3. isolated 实例必需化：`init` 生成的配置模板写入 `storage.graph.instances.isolated`；启动组装与 doctor 硬检查（缺失 = 明确报错文案）；断言测试"无 isolated 时分流/降级不静默丢数据"。
  4. 默认 `llm.dream` 路由对齐 `ollama/qwen3.5:9b`（params 不变；num_ctx 属 T4 seam）。
- AC：
  - 五键 `config set` 热生效且消费端活读（逐键测试：改值后无需重启即影响对应行为）+ 非法值被拒绝；
  - 移除键写入报 deprecation；账本只记 token；全仓无 budget/cap 残留逻辑；
  - 新装（init）默认配置含 isolated 实例；删除该实例后启动/doctor 明确报错；
  - 默认 config 的 `llm.dream.model == "qwen3.5:9b"`；全量回归绿。

## 任务 T4 · ollama 驱动 options seam + 输出上限 + doctor 一致性校验

- 设计依据：设计稿 §4.8（上下文窗口一致性）、§7.3。
- 范围：
  1. ollama 驱动把路由 `params` 转发进 `/api/chat` 的 `options`（`num_ctx`、`num_predict` 等）；默认行为不变（无 params 时请求体与现状一致）。
  2. 生成上限：`num_predict` 经 params 可配；openai_compatible 已有 `max_tokens` 视同满足。
  3. doctor 新增一致性校验：`cache_prefix + delta（按 dream.delta_budget_ceiling_tokens）+ 生成余量 ≤ num_ctx`，不一致时报错并给修法提示。
- AC：params 透传可测（断言请求体 options）；doctor 对不一致配置报错；默认配置下 doctor 不误报；全量回归绿。

## 完成定义（整包）

- 四任务 QA 全过；`uv run pytest -q`、`ruff check`、`ruff format --check`、`mypy src` 干净；
- 单 commit 收口（orchestrator 执行）：`phase A2.5: baseline hardening — async dream, retrieval filter, config registry, budget removal`。

## 收口记录（2026-08-16）

- 收口 commit：`16ee68b`（34 文件，+2675/-678），已推送 `MnemoSeed/mnemoseed-local`（main）。
- 全程 TDD；QA 门禁逐任务对抗验收（变异击杀 20+ 发）。最终 908 passed / 5 skipped，ruff/format/mypy 干净。
- QA 判定：T2 PASS、T4 PASS、T1a FAIL→修复后 PASS（D1 停机 future 挂起）、T1b PASS、T3a FAIL→修复后 PASS（D-T3a-1 降级混合写入原子性）、T3b PASS。

## QA 观察项存档（非阻断，供 A3/Phase B 分诊）

1. pool-fired（relay 路径）失败的梦无指纹、无快速退避，靠 hard_deadline（默认 24h）兜底——若期望全路径快速重试需补指纹登记。
2. 退避重发与新近 floor-eligible 触发可产生重叠窗口双梦（worker 串行保证安全，属浪费非损坏）。
3. `DREAM_RETRY_BASE_S` 绝对值无测试守卫（测试自引用常量）；退避 given_up 与 next_at=inf 双保险单边无守卫。
4. `DreamWorker.stop()` 后 submit 会挂起（API 级边界，daemon 停机路径不可达）；stop() 的 executor shutdown 竞态日志被吞（不影响正确性）。
5. 重载下事件循环瞬时 stall（/healthz p95≈609ms）：主体是既有 drain 成本，T1a 竞争贡献约 +35-40%；收紧方向=drain 存储写序列化/批量提交（Phase B 性能主题）。
6. `trigger.status()` 跨线程读可能撕裂快照（良性）。
7. boot recovery 链仍同步于 lifespan（真实 LLM 下启动可达分钟级）——T1b 评估为暂不修，记录。
8. `OllamaLLM` 直构默认模型已在收口统一为 qwen3.5:9b；Ledger record 签名无"拒绝 budget kwarg"级守卫（M-T3b-4 未杀中，load/configwrite 双侧已硬拦，风险低）。
9. doctor 测试的 cli/config 双 patch 已由 T3a 的 cli_home fixture 合并；`dream.ensemble`/`hardware_tier` 键尚**无运行期消费端**（vote/verify 实现属后续任务，键语义=声明性配置）。
