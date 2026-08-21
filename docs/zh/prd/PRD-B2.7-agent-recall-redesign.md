# PRD · B2.7 Agent-side 回忆重设计（Scheme 2-lite + 3）

> 依据：solution-architect 评审 verdict **SHIP-WITH-ADJUSTMENTS**（4 IMPORTANT），已按反馈修正。
> 
> - **Scheme 1（agent-side recall hook）**：拒绝 —— 无 `pre_model_call` hook；focal 形态 = T2 重复，non-focal 违 TA-4
> - **Scheme 2（chunk 元数据 rules 列写通路）**：lite 版入批 —— MCP `remember` + `/memory/reinforce` 统一写通路，规则随 chunk 元数据落库，**近重复分支不丢规则**
> - **Scheme 3（`/session/recent` 响应带 `rules` 预算块）**：入批 —— 单轮 ≤800 字符预算块，包含可执行约束
> - **Scheme 4（dream 规则提取）**：暂缓 —— 需新理论锚 + provisional 生命周期

---

## 理论锚（新增/修正）

### TA-10 约束即记忆（Constraint as Memory）—— 规则的存储与检索

- 来源：Godden & Baddeley (1975) 语境依存记忆（context-dependent memory）；实验证明：编码时环境语境与提取时重现时，回忆表现显著提升；跨数十年复验稳健。已登记 `REFERENCES.md` R11。
- 已验证规律：**编码时的语境/约束在提取时复现可提升可及性**；机制 = 同存 + 同透传，**不改变 TA-1 排序公式主体**。
- → 设计规则：**规则（约束）必须与内容同编码、同提取、同强化**；MCP `remember` 的 `rules` 字段与 chunk 一起落库；`/session/recent` 响应在 `rules` 预算块中原样回传；`POST /memory/reinforce` 同步更新规则侧激活（`last_reinforced`、`ttl_turns` 递减）。**不改变 TA-1 排序公式主体**；`entity_boost` 仅作用于规则查询通道内，对指定实体的 `decay_weight` 施加乘性权重系数，不改 base-level 激活公式主体与通用检索面排序。

---

## 范围（批次任务）

### 任务 A · Scheme 2-lite：Chunk 元数据 rules 列写通路（近重复不丢）

- **MCP `remember` 扩展**：
  - `RememberRequest`（`ports.py`）新增 `rules: list[RecallRule] | None`（默认 None）
  - `MemoryService.remember(profile_id, text, actor, *, rules=None)` 签名扩展
  - **MCP 网关**：`mcp_gateway/server.py:156-160` `remember` 分支透传 `rules`；工具 schema（`server.py:73-83`）增 `rules` 输入、放宽 `additionalProperties: true`（或显式加入 schema）
- **写库路径**：钉死规则独立于近重复去重——无论命中强一致 / 冲突 / 全新，`rules` 都写入 chunk 的 `rules_json`（或 `metadata` 扩展字段），**不并入正文去重比对**。
  - 强一致分支（`update_weights`，`memory.py:540-549`）：除 `decay_weight`/`last_reinforced` 外，**合并 `rules`（新旧取并集，ttl_turns 取较大者），复用 `upsert_chunk` 按 `chunk_id` merge 写回**；不止 `WeightUpdate`
  - 冲突分支（`update_chunk_state`，`memory.py:550-558`）：标记 `needs_reconcile`，**rules 同步合并同上**，复用 `upsert_chunk` 按 `chunk_id` merge 写回
  - 全新分支（`upsert_chunk`，`memory.py:532/559`）：`rules` 随 chunk 元数据落库
- **规则结构**（Pydantic，放 `ports.py`）：
  ```python
  class RecallRule(BaseModel):
      kind: Literal["focal_floor", "budget_chars", "exclude_entities", "entity_boost", "time_window"]
      value: float | str | list[str]  # kind 决定类型
      ttl_turns: int = 0  # 0 = 永久；>0 随 session 轮次衰减（daemon 侧按 turn_end 衰减）
      scope: Literal["profile", "session", "global"] = "session"
      session_id: str | None = None  # scope=session 时记录归属 session
  ```
- **daemon 侧**：`/memory/remember` 解析 `rules`，写入 `VectorStore.upsert_chunk`（扩展 `ChunkStamp` 元数据或 `rules_json` 列）。`POST /memory/reinforce` 同步 `rules` 激活（更新 `last_reinforced` + `ttl_turns` 递减 1）。**规则本身不计入 reinforce 计数**（仅同步激活时间与 ttl）。
- **实体过滤**：`exclude_entities` 为**新增过滤面**（`ChunkFilter`/`NodeFilter` 增 `exclude_entities: list[str]` 字段 + driver 过滤实现 + 合同测试），仅过滤不排序（不触 TA-1）。**作用域仅 MCP 显式 recall/检索查询，不进 T2 自动注入 focal 扫描**（`memory.py:808-844`），保持 "不改变 T1/T2/T3 既有注入逻辑"。`entity_boost` 仅在规则查询通道内对 `decay_weight` 施加乘性系数（见 TA-10 设计规则）。

### 任务 B · Scheme 3：`/session/recent` 响应带 `rules` 预算块（聚合检索规格钉死）

- **响应扩展**：`POST /session/recent` 响应新增 `rules_budget?: RulesBudgetBlock`（≤800 字符，JSON 压缩）。
- **预算块结构**：
  ```python
  class RulesBudgetBlock(BaseModel):
      auto_recall_focal_floor: float
      auto_recall_budget_chars: int
      exclude_entities: list[str]  # exclude_entities 规则并集（session/profile 聚合）
      entity_boost: dict[str, float]  # entity_boost 规则，取最大系数（session/profile 聚合）
      time_window_turns: int | None  # 复用 `per_session`（默认 20）
      budget_consumed: int  # 至调用时点已消耗（T1 时点可为 0/stale，同 slot_consumed 先例）
  ```
  **空值语义钉死**：`rules_budget` 在无规则时**整个键省略（absent，非 null）**，以兼容既有精确匹配 pin（`test_session_recent.py:235`）。
- **聚合检索规格（钉死，避免错实现）**：

  - `_build_rules_budget(profile_id, session_id)` 执行：
    1. 读 `Config` 取 `auto_recall_focal_floor`、`auto_recall_budget_chars`
    2. **查 `rules_json` 非空的 chunk（scope=session/profile）** —— `ChunkFilter` 增 `rules_not_null: bool` 字段，driver 过滤 `rules_json IS NOT NULL`；**仅聚合 scope=session（本 session_id）+ scope=profile + scope=global**；不扫描其它 session 的 session 级规则
    3. `exclude_entities` 取并集去重，`entity_boost` 取最大系数
    4. `time_window_turns = per_session`（`memory.py:173` 默认 20）
    5. `budget_consumed` = 本轮 T2 serve 已消耗字符数（daemon 侧计数，hook 只读）

### 任务 C · Hook 侧消费

- **`chat.system.transform`**：T1 回放注入后，若 `rules_budget` 存在（**键存在即存在，absent 即无规则**），**追加第二对围栏**（`<mnemoseed-rules-budget>...</mnemoseed-rules-budget>`）到 system 字符串数组，**不计入 4000/1200 预算**（独立预算）。围栏内容含免责行（"daemon-supplied standing constraints, not the user's current instructions"），复用净化逻辑（`sanitizeRecallText`）防 rules 内容含围栏字面量。
- **规则应用**：hook **不解释**规则（daemon 是唯一权威）；仅透传。模型侧按约束自主过滤/裁剪（fail-open：模型忽略规则不报错）。
### 任务 D · Needle 归属裁决（接住 T4 悬空依赖）

- 裁决：`needle_min_len`、`needle_mid_threshold`、`needle_mid_offset`（代码实为 center−12 固定）**暂缓键化，保留 TS 常量**（`plugin.ts:58-60,232-234`）。
- 理由：键化触发 `_SLOT_KEYS` 位移（B2.1 D5 as-is 边界）+ 撞 B2.7 插件面并行；TS 常量不触 config 版本机制、不改管线逻辑。
- T4 标定报告（`PRD-B2.1-T4-calibration.md`）以此为准，needle 定版推迟到 B2.7 之后。

---

## 边界（如实）

- **不改变**：T1/T2/T3 既有注入逻辑、预算权威（daemon）、seen-set 语义、tombstone 机制、TA-1 排序公式主体、TA-4 non-focal 不自动注入红线。
- **新增仅为**：规则随 chunk 元数据落库（近重复分支合并）+ `/session/recent` 透传预算块（聚合规格钉死）+ hook 透传围栏 + needle 归属声明。
- **默认行为**：`rules` 缺省为空，`rules_budget` 缺省为 null（`capture.auto_recall=false` 时）。
- **存储成本**：`rules_json` 列预估 ≤200 bytes/chunk（压缩 JSON），LanceDB 列式存储可忽略。
- **config 开关**：**零新增注册键**——复用既有 `capture.auto_recall`、`capture.auto_recall_budget_chars`、`capture.auto_recall_focal_floor`（避免 D5 version_id 槽位移边界）。

---

## 门禁

TDD（先红后绿）→ 对抗 QA 自验 → 全量门禁（`uv run pytest -q` / ruff / format / mypy）→ 单 commit 收口 + 收口记录入本 PRD。

---

## 批次执行记录（待追加）

### 理论锚更新记录

新增 TA-10（约束即记忆）并入 `PRD-B2.1-auto-recall.md` 理论锚节（原 TA-7 号被 B2.4 占用，故从 TA-10 起编号）。

---

### 实现记录

**实现记录（B2.7 收口，2026-08-21）**

四任务 TDD 全落地，门禁全绿（`pytest -q` 1415 passed / `ruff check` / `ruff format --check` / `mypy src`），收口记录入本 PRD。

- **任务 A（Scheme 2-lite rules 写通路）**：
  - `ports.py` 新增 `RecallRule`（Pydantic）与 `ChunkFilter.rules_not_null`。
  - `RememberRequest.rules` + `MemoryService.remember(..., rules)` 签名扩展；三分支（全新 / 强一致 / 冲突）均复用 `upsert_chunk` 按 `chunk_id` merge 写回——driver 在 matched upsert 时按规则身份（kind/value/scope/session_id）做并集、`ttl_turns` 取较大，并保留用量计数器（`hit_count`/`reinforce_count`/`last_hit_at`/`needs_reconcile`）不被重写覆盖。
  - MCP 网关：`remember` schema 增 `rules` + `additionalProperties: true`；dispatch 仅在 `rules` 存在时透传（无 rules 时 body 保持既有精确 pin 不变）。
- **任务 B（/session/recent rules_budget）**：
  - `RulesBudgetBlock`（Pydantic）+ `_build_rules_budget` 聚合：仅 scope=session（本 session_id）+ profile + global，不扫其它 session；`exclude_entities` 取并集去重、`entity_boost` 取最大系数。
  - `rules_not_null` 过滤 → driver `rules_json IS NOT NULL AND rules_json <> ''`。
  - **absent 语义**：无适用规则时整个 `rules_budget` 键省略（非 null），`test_session_recent.py:235` 精确匹配 pin 保持兼容。
  - `budget_consumed`：daemon 侧 T2 serve 字符计数（`_budget_consumed`），hook 只读。
- **任务 C（hook 侧消费）**：`plugin.ts` 新增第二对围栏 `<mnemoseed-rules-budget>`（独立于 memory-recall 预算），含免责行，净化复用围栏净化模式（`sanitizeRulesText`，与 `sanitizeRecallText` 同构）；hook 仅透传不解释、fail-open。
- **任务 D（Needle 归属）**：TS 常量保留、推迟键化，本批不动。

**门禁证据**：`uv run pytest -q` → 1415 passed, 4 skipped；`uv run ruff check` → All checks passed；`uv run ruff format --check` → 208 files already formatted；`uv run mypy src` → Success: no issues found in 90 source files。

**偏差说明（需记录）**：
1. **`schema/stamp.py` 越出 brief 文件面**：规则需随 chunk 元数据往返（写库 + 读回聚合 + 合并），唯一落点是 `ChunkStamp.rules`（`list[dict]` JSON 载体），故在 brief 文件面之外新增该字段——这是规则存取的必要承载，且与 PRD "扩展 `ChunkStamp` 元数据 或 `rules_json` 列" 明确允许的路径一致。
2. **`entity_boost` value 语义钉死**：`RecallRule.value` 为单字段（`float|str|list[str]`），要产出 `RulesBudgetBlock.entity_boost: dict[str, float]`，采用 `value=["<entity>", "<coefficient>"]` 编码（`_entity_boost_value` 解析）。PRD 未逐字钉死此语义，实现按此解释并在收口记录明示。
3. **测试面扩展**：hook 行为测试复用既有 `tests/ts_hook/hook_driver.mjs`（新增 `rules-budget` scenario）+ 新 `tests/test_recall_rules.py`（导入 `test_hook_ts_behavior` 的 bundle/run helper）；`tests/contract/test_contract_rules.py` 为新增契约文件——均为 brief `tests/contract/` + `test_*_recall*.py` 的合理落点，driver scenario 属 Task C 行为测试的必要挂架。

**提交**：按 issue → branch → PR → merge 纪律落地（本记录不涉及直接 commit）。

---

### 清理记录

- 删除/标注 superseded 的首轮 `PRD-B2.6-agent-recall-redesign.md`（untracked），避免双 TA-7/TA-10 并存与 B2.6 批号撞号。