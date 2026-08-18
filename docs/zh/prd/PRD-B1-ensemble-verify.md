# PRD · Phase B1 ensemble verify 运行期消费端

> 依据：`docs/zh/design/mvp-design.md` v1.3 决策 1（ensemble 校验层诚实成本版：**verify = 模型 B 逐条验证模型 A 的产出；B 失败/格式崩坏的回退 = 采用 A 原样 + 审计记录**）、决策 7（`dream.ensemble` 为注册表热键，本包补上它的运行期消费端）；A2.5 QA 观察项 9（`dream.ensemble`/`hardware_tier` 键尚无运行期消费端）。Phase 2 启动指令：先落地 verify 运行期消费端，再用持续累积的真实 sessions 跑双模型串行双向配对实测。
> 基线：commit `851b04a`，复验 1028 passed / 3 skipped（73.30s，2026-08-18）。
> 门禁：每任务 TDD；子代理从不提交；每任务完成后经对抗性 QA 验收，FAIL 打回重做。
> 执行顺序：**T1 → T2 → T3 串行**（T1 钉角色/配置管线，T2 引擎落 reflect 构造缝，T3 daemon + doctor 集成缝）。
> vote 模式**不在本包范围**（journal 双相位 + combiner 的机制改动另行立项，见设计稿决策 1）。

## 语义定版（本包拍板，写死进测试）

1. **送裁范围**：仅 `route == core` 的 folded triple 送 B 裁。isolated/salvage 已在主图之外——无可保护对象；B 永不升级任何路由、永不删除任何产出。
2. **否决定去**：reject → 确定性改道 `Route.ISOLATED`（绝不投票消灭，与决策 1 vote 侧"分歧 → isolated"同哲学）。accept → 原样。
3. **回退全集（采用 A 原样，整轮粒度）**：B LLMUnavailable / JSON 解析失败 / 覆盖不齐（缺号、多号、重号、垃圾 verdict 值、非数组顶层）。单次调用、**B 不重试**——校验层不是关键路径，回退代价低，与决策 1"B 失败回退 A 原样"对齐。
4. **零受审短路**：无 core triple → 不调用 B、不审计。
5. **三重审计**（actor 均按既有 dream 路径惯例）：
   - `llm_role_configured`（role=`dream_verifier`，RoleRouter 物化时既有面自动落地）；
   - `ensemble_verified`（actor `dream`）：{run_id, verifier_model, verify_prompt_version, judged, accepted, rejected, rejected_keys（subject|predicate|object 痕迹）, tokens}；
   - `ensemble_verify_fallback`（actor `dream`）：{run_id, verifier_model, reason ∈ {llm_unavailable, malformed_output, coverage_mismatch}, detail 单句}。
6. **journal/resume 零改动**：验证发生在 reflect retry 循环成功组装后、`_finalize` 前；落盘载荷 = 验证后结果，`REFLECT_DONE` 单标记语义不变，merge/safe-clear 不感知。
7. **账本**：verifier prompt（`estimate_tokens` 估算）+ completion token 附加记账（与 reflect 同一月度计数器，append-only 遥测语义不变）。
8. **档位门控不变**：lite + ensemble≠off 拒绝写入的既有校验面不动；verify 默认关闭（`off`）。

## 任务 T1 · `dream_verifier` 角色管线

- 设计依据：决策 1（校验位的唯一配置增量 = 第二个 LLM 路由）；决策 6（api_key_env 全展示面 redact 惯例对新角色一体适用）。
- 范围：
  1. `LLM_ROLES` 增 `"dream_verifier"`；`DEFAULT_LLM_ROUTES` 增默认路由：`ollama / gemma4:e4b`，params `{base_url, think=False, num_ctx=16384}`——判定任务比生成简单（决策 1），校验位默认小档模型；型号在本包收口后的 live 双向配对实测复核。
  2. 派生面零代码：loader 角色循环（config.py:515）、configwrite `_role_key_specs` 循环（service.py:547）、secret-ref 校验（`secrets:mnemoseed/dream/dream_verifier`，config.py:352）全部自动——本任务补钉死测试。
  3. init 模板注释段补 `[dream.llm.dream_verifier]` 示例（与 dream 段同款注释形态）。
  4. 更新既有钉死测试：`test_llm_routing.py`（LLM_ROLES 断言、`DEFAULT_LLM_ROUTES` 断言）、`test_configwrite_service.py`（角色键注册面）等因角色集扩张而失效的断言。
- AC：
  - `config set dream.llm.dream_verifier.model ...` 热生效（generation 递增，RoleRouter 下次 resolve 拿新实例，与 dream 角色同 seam）；
  - 非法 driver / 字面量 api key / 非法 base_url 写入被拒（复用既有校验面，逐型反例）；
  - 全新 init 生成的 config 含 `dream_verifier` 默认路由；全量回归绿。

## 任务 T2 · 验证相位引擎（`dream/verify.py`）+ reflect 集成

- 设计依据：决策 1（verify 语义）、决策 7（热读 seam，镜像 `Merger._confidence_floor` / `DeltaPacker._ceiling` 活读惯例）；红线（溯源不可变：reject 只改道不删除）。
- 范围：
  1. 新模块 `src/mnemoseed_local/dream/verify.py`：
     - `VERIFY_PROMPT_VERSION = "v1"`；
     - 系统模板：judge 指令（只评判"证据是否支撑该 triple"；拿不准即 accept；输出严格 JSON verdict 数组，无其他文本）；
     - 用户面文法：`<candidate>` 块（index / subject / predicate / object / route / confidence + 该 triple 证据 chunk 块，复用 `render_chunk_block`），候选序 = folded triples 序（确定性）；
     - `TripleVerifier`：注入 `llm`（boot 兜底）+ `resolve_llm`（每轮钉定，镜像 reflect 的 F2 seam）+ `config`（活引用，`ensemble` 热读）+ `ledger` + `meta`（审计 sink）+ `sleep`；`verify(snapshot, result) -> ReflectionResult`，off 时原样直返且 B 零调用；
     - verdict 解析：顶层数组 `[{"index", "verdict"}]`；`index` 走有界数字 coercion（D4 同哲学）；`verdict` 有界词表 {accept, accepted, reject, rejected}（casefold）；其他一律 malformed；
     - 应用：`dataclasses.replace` 逐枚改道 + 重建 result。
  2. `ReflectOrchestrator` 增可选 `verifier` 参数（结构 Protocol 定义在 reflect.py，避免 import 环）；集成点 = retry 循环成功后、ledger/`_finalize` 前。`llm`/`resolve_llm` 缺失时 verifier 不动作（off 语义）。
- AC：
  - 四主用例：全 accept 原样；单 reject 改道 isolated（其余字段不变）；非 core 不送裁（B 收到候选数 = core 数）；零 core 零调用零审计；
  - 回退路径逐型：LLMUnavailable / 垃圾 JSON / 缺号 / 多号 / 重号 / 垃圾 verdict 值——均返回 A 原样 + `ensemble_verify_fallback` 正确 reason；reflect 边界照常 `REFLECT_DONE` finalize；
  - `ensemble_verified` 审计 detail 字段钉死；verifier token 附加记账断言；
  - off→verify 热切换：下一轮 dream 生效，off 期 B 零调用；
  - 全量回归绿 + ruff/format/mypy 干净。

## 任务 T3 · daemon 接线 + `stub_verifier` 驱动 + doctor 校验位检查

- 范围：
  1. `llm/drivers/stub_verifier.py`：`StubVerifyLLM`（解析 `<candidate>` 文法：证据 chunk 块 ≥1 → accept，否则 reject；lazy import 防环，镜像 stub.py 先例）注册驱动 `stub_verifier`（测试用件，永不作生产默认）。
  2. `daemon/app.py`：`_VERIFIER_ROLE = "dream_verifier"`；`_build_verifier_llm(router)`（typed degrade 镜像 `_build_dream_llm`，坏路由不炸 boot）；`TripleVerifier(...)` 构造（meta=config 同源、共享 ledger）；注入 reflector。
  3. doctor 新增 "ensemble verifier" 检查：仅 `dream.ensemble == verify` 且校验位路由为 ollama 时生效（off / 非 ollama 显式 skip）；复用 T5 的模型存在性判定函数（名称规格化同源）；缺失 → FAIL 附 `ollama pull <model>`；服务器不可达 → FAIL 附启动提示。**`up` 预检不阻断**——校验位缺失按设计回退为未验证 dream，不是不可运行状态。
- AC：
  - daemon 级集成：ensemble=verify + `stub`/`stub_verifier` 双路由全链路 dream 跑通；`/api/v1/audit` 可见 `ensemble_verified`；reject（证据空）用例节点落 isolated；
  - doctor 三态 + 两 skip 用例；
  - 全量回归绿 + ruff/format/mypy 干净。

## 完成定义（整包）

- 三任务 QA 全过；`uv run pytest -q`、`ruff check`、`ruff format --check`、`mypy src` 干净；
- 单 commit 收口（orchestrator 执行）：`phase B1: ensemble verify runtime — verifier role, verify phase with fallback, triple audit`。
- 收口后人工验证项：双模型串行双向配对实测（qwen3.5:9b ↔ gemma 互换 A/B）跑真实 sessions，复核校验位默认型号；真实 OpenCode 会话端联调记录。

## 收口记录（2026-08-18）

- 收口 commit：`10d82da`（15 文件，+1400/-32）。最终 1064 passed / 3 skipped（70.29s），ruff/format/mypy 干净；基线 1028 → 增量 +36。
- 批次执行：T1（5 红 → 全绿，含 2 枚钉死断言随角色集扩张更新：LLM_ROLES 元组、legacy 容忍面、router 序）→ T2（21 红 → 全绿；1 枚测试预期自纠：非数组顶层 verdict 被最宽括号兜底修复为 [] 后按 coverage_mismatch 归类——与 reflect 输出巷同源，语义诚实）→ T3（8 红 → 全绿；1 枚测试基建自纠：dream_once 语义 = 消费池事件，测试须把 floor 降到单次触发线，与既有 daemon 测试同形）。
- 对抗自验增量（收口前变异击杀向）：string-digit index 正向 coercion、bool index、负 index 三枚补钉；三重审计第一面（`llm_role_configured` 携 `role == "dream_verifier"`）在 daemon 集成层钉死。
- 待办人工验证项：双模型串行双向配对实测（qwen3.5:9b ↔ gemma 互换 A/B）跑真实 sessions 并复核校验位默认型号 `gemma4:e4b`（设计稿 §8 原标 gemma 4 12b 为估算值，live D4 矩阵已实测 e4b）；真实 OpenCode 会话端 verify 联调留证。

## 人工验证记录（2026-08-18，双模型串行双向配对实测）

形态：harness（temp 脚本，未入仓）直组**生产链类**——live 仓 4 枚 pending chunks（1707 tok 真实 sessions 材料，turns 0-1）经真 vector 驱动 `snapshot_read` 只读取出，同一冻结快照跑两向：`DeltaPacker→ReflectOrchestrator(真 ollama)→journal→TripleVerifier(真 ollama)→audit`；merge 不跑、store 零写入、live daemon 全程在线未动；双座均 `think=False + num_ctx=36864`（1:1 镜像 live dream 窗口）。证据 JSON 转录于 temp 工作目录。

**机制实弹（引擎侧全过）**：
1. verify 相位实裁：B=qwen3.5:9b 裁 A=gemma4:e4b 的 25 枚 core 候选 → 18 accept / **7 reject→isolated**，`ensemble_verified` 审计全字段落盘（judged/accepted/rejected/rejected_keys/tokens）；reject 判定谱人工抽查合理（类别化摘要、session-meta、噪声）。
2. 零 core 短路实弹：A=qwen3.5:9b 空提取（`[]`，completion=2）→ B 零调用零审计（设计语义）。
3. 两模型 verdict JSON 均严格合规（18k prompt 下 343 / 355 completion token），判定词表/覆盖检查实模型一次通过；journal 载荷即验证后结果（rejudge 靠 d2 journal 原样还原候选集，交叉自证）。

**模型行为发现**：
1. **A 侧召回不对称（重大）**：qwen3.5:9b 对本材料**两次稳定空提取**（同 prompt=1875，completion=2，~21s）；gemma4:e4b 同材料富提取 25 枚（54.6s / 2938 completion）。live graph.db 已存 16 nodes（qwen 历史提取正常工作）→ 属**材料相关欠抽取，非全局失效**。单一模型欠抽取时校验层无米下锅（design 预期内：verify 滤噪声不补召回）。
2. B 斜视判定一致率 **80%**（20/25）：共同拒绝 3（phase-meta / gemma-observes / goal-junk），qwen 独严 4（类别化摘要），gemma 独严 1；分歧全部 reject→isolated 保档，永不灭档。
3. 判定座速度：B=gemma4:e4b **32.9s** vs B=qwen3.5:9b **86.4s**（同一 18k 判定 prompt）——决策 1"判定任务比生成简单、小档校验位够用"实测成立；gemma4:e4b 校验位默认型号**复核通过**（2.6× 快 + 合理拒绝谱 + 有独立主见）。
4. A 侧成本反转：A=gemma4:e4b 54.6s 富提取；A=qwen3.5:9b 20.8s 空提取。

## QA 观察项存档（非阻断，供后续分诊）

1. verify 单次调用不重试：瞬时 ollama 抖动会损失该轮的交叉验证（回退兜底兜底正确性，不兜底验证信号）。若实测回退率高，立项加一次 in-process 重试。
2. verifier 覆盖不齐 = 整轮回退（all-or-nothing），不做逐条降级：部分缺失时放弃全部 B 信号，属保守浪费而非损坏。
3. `ensemble=vote` 仍无运行期消费端（配置层接受写入，运行时按 off 处理）——键语义继续为声明性配置，立项机制改动时再补消费端。
4. `dream_runs.model` 只钉 A 模型；校验位型号在 `ensemble_verified`/`ensemble_verify_fallback` audit detail 的 `verifier_model` 里溯源（本条为记录——若要表级归因，随 vote 的 journal 扩展一起做）。
5. daemon boot 无条件物化校验位路由（ensemble=off 也建实例）：构造无网络 I/O、零运行成本；坏路由的 warning 在 off 态也会出现一行——如实报。
6. init 模板注释段的"every durable capture turn"旧措辞已顺手对齐 v1.4（capture 语义无变化，仅文案）；README/README zh 的"状态"小节仍停在 A3 前夜口径（装脚本/MCP 网关写作"land in Phase A3"），待一次文档 pass 一并清算。
7. **校验位窗口缺口（人工验证发现，行动项候选）**：25 候选实裁 prompt=18287 tok，**超出工厂默认 verifier num_ctx=16384**——大提取量下判定会被 ollama 静默截断；doctor 只查 dream 侧 ctx（`cache_prefix + delta + 生成余量 ≤ num_ctx`），无校验位 ctx 检查。后续任务：verifier 默认窗口随 `delta_budget_ceiling_tokens` 对齐 / doctor 增校验位 ctx 一致性检查。
8. qwen3.5:9b 材料相关欠抽取需后续真实批次 watch（本次 1707-tok session-meta 类材料两次稳定空提取；历史 16 nodes 证明其正常工作）：若复发率高，单独立项（prompt 或 A 侧选型），不在本包。
