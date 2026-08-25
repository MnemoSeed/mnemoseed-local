# PRD · B2.9 捕获记忆的来源 agent 归属（origin_agent）

> 依据：
> - GitHub issue #105（2026-08-25 用户提问）：审计记忆历史时无法分辨一条记忆来自宿主内的哪个 agent——opencode 的 Build 主 agent 还是一个 subagent。插件已把 `hookInput.agent` 塞进未触碰的 raw 载荷，但服务端直接丢弃：IngestEvent/Turn 无规范字段，capture chunk 一律 `persona_id = null`。
> - solution-architect 综合评审（2026-08-25）verdict：**SHIP-WITH-ADJUSTMENTS**——目标正确且理论锚定，但 issue 原案"把 agent 写入 persona_id"是错的（语义冲突 + 冻结契约），改为新增独立 `origin_agent` 字段走同一链路。产品侧评审（2026-08-25）：批次内最高优先级，#106 的前置条件。
> - 执行顺序（用户 2026-08-25 确认）：本批 → #75 观察钟启动 → #106 Claude Code 适配器；#94 并行插入；#80 作为下个 eval 重型批次的 gate。

## 理论锚

### TA-13 来源监控 · 写入侧 provenance（继承既有锚 R7/TA-5/TA-8 同框架）

- 来源：Johnson, Hashtroudi & Lindsay（1993），Source monitoring（本地登记 **R7 ✅**，见 `docs/zh/design/REFERENCES.md`；同框架已锚 PRD-B2.1 TA-5、PRD-B2.4 TA-8、design/04/05/06）。
- 已验证规律：内容来源的判别天然不可靠，来源混淆是常态；无外显线索时系统性偏斜。推论规则（既有表述）：**provenance 只追加、永不覆盖、永不依赖事后归因**。
- → 本专题设计规则：来源归属必须在**写入时**随捕获落盘（agent 标签随 ingest 事件进入 Turn、盖章进 chunk），读面逐项携带；缺省显式呈现为 null（未知是一等状态），绝不事后猜测补填。

## 产品边界（定案）

- **打标可以，分区不做（label now; partition never）**：v1 只给记忆贴来源标签（元数据）；不按 agent 物理分库/分区记忆。多租户式 per-agent 记忆隔离在项目 non-goals 内，除非未来 PRD 有证据推翻。
- **字段是惰性元数据（红线）**：评分、衰减、检索排序一律不读 `origin_agent`（R30 捕获中立纪律）；由 `tests/test_capture_neutrality.py` 的扫描模式作为执法载体。
- **捕获成功率 > 归属完整度**：ingest 缺 agent 字段绝不失败；旧 chunk 保持 NULL 不回填（provenance 不可变）。
- 错标比不标更糟：host 未报告 agent 时诚实 null，不做启发式推断。

## 设计定案（机制层）

### 链路（issue 原案的管线直觉保留，落点改名）

1. **IngestEvent**：增可选字段 `agent: str | None = None`（additive——pydantic 对旧载荷缺键忽略，双向 wire 兼容）。
2. **Turn**：增 `origin_agent: str | None`，分段时从**锚定本轮的 user_prompt 事件**取值（第一方锚定义轮次归属；会话中 Build↔Plan 中途切换按轮粒度存活）。assistant/tool 事件不覆盖。
3. **WriteContext**：增 `origin_agent: str | None = None`（与既有 `agent_label` 平行、**绝不替换**——后者是 soul/anima 载体）。
4. **盖章**：`StampWriter._assemble` 把它写进**新的顶层可空 stamp 列 `origin_agent`**（走既有 `add_columns` 模式，rules_json/explicit_pin 先例）。不复用 `persona_id`（soul 载体 + dream 证据边界 + `test_schema_freeze.py` 冻结契约 + LanceDB 里与 `anima_id` 双列别名），也不复用 `Provenance.agent_id`（今天填的是 model_id）。
5. **读面**：recent/recall 条目载荷增 `origin_agent`（additive JSON）；顺带暴露 `cues.host` 的 host（今日 recent 条目连 host 都没返回，一并补齐 issue 的 "alongside host/session" 要求）。
6. **插件**：把 `raw.agent` 提升为规范 body 字段（user-prompt 路径）；`raw.agent` 留一个过渡代。

### 明确不做（v1 出界）

- 按 agent 过滤/打分的任何策略（"忽略子 agent 记忆"是 v2 决策）。
- 历史 chunk 回填推断——旧数据显示为 unknown/null。
- agent 分类学规范化——host 报什么就原样映射存什么。
- UI 面扩展——仅 recall/recent 载荷加标签。

### 迁移 / 兼容

- 新列经 `AddColumn(store=..., ...)` 出生即 NULL，旧行保持 NULL 直至重写（explicit_pin 先例）；无回填。
- wire 无破坏变更：旧 daemon 忽略新键、新 daemon 接受缺键。
- 若 #94 并行落地各自涉及存储迁移：协调 migration 版本号，避免两分支撞同一版本。

### 测试预言（防回归的最小集合）

- fixture 往返：不带 `agent` 的旧 ingest 载荷照常验证；带 `agent` 的新载荷通过。
- 分段器：轮次归属跟随锚定 prompt；会话中段切换 agent 后归属按轮正确。
- 盖章器：`origin_agent` 仅在有值时落章；`persona_id` 保持 soul 独占（护住 `test_stamper.py` / `test_writing_pipeline.py` 既有断言）。
- dream：设置 `origin_agent` 但 `persona_id=None` 时 origin 判类不变（护住 dream/prompts 证据边界）。
- 读面：recall/recent 暴露标签；缺席时一致 null。
- 中立扫描：`origin_agent` 不进入评分/排序读取面（neutrality scanner 增列检查）。

## 已知边界（如实记录）

- opencode assistant ingest 今天不带 agent——归属粒度必然是"prompt 锚定的轮次"，PRD 如实声明，不虚构更细粒度。
- Claude Code 原生携带 `agent_id`/`agent_type`（SubagentStart/Stop）——这是 #106 先落 #105 的原因：CC 适配器从第一天就能填此字段。

## 批次执行记录

- **B2.9 来源 agent 归属（origin_agent）**：2026-08-25 立项并收口（issue #105；PR 号/hash 合并后回填）。solution-architect + product-manager 联合评审定方向（SHIP-WITH-ADJUSTMENTS：否决写入 persona_id，改独立 origin_agent 列）；senior-software-engineer TDD 实现（红测试先行）。落地链路：`IngestEvent.agent`（可选、双向 wire 兼容）→ `Turn.origin_agent`（user_prompt 锚定，会话中切换按轮存活）→ `WriteContext.origin_agent` → stamp 新顶层可空列 `origin_agent`（add_columns DRY 化为 `_add_nullable_column`，rules_json/explicit_pin 三份拷贝折叠）→ recent/recall 条目暴露 `origin_agent`+`host`。插件把 `hookInput.agent` 提升为规范 body 字段：live 与 crash-replay 双车道（SDK `UserMessage.agent` 原生存在，replay 可归因），`raw.agent` 保留一个过渡代。空白/纯空白 agent 归一化为 null（ProfileRef 先例）。中立红线执法：inert 扫描器覆盖 scorer/pool/hybrid/cues/decay/* + 序列化文件（assemble/lancedb）内函数级 allowlist，路径漂移 fail-loud。
- 验收：gates 全绿 **1658 passed / 5 skipped**（ruff / format / mypy 干净）；对抗性 QA 两轮——首轮 CLOSABLE（0 BLOCKER / 1 IMPORTANT / 5 NIT），微修后增量复核 **CLOSABLE（0 BLOCKER / 0 IMPORTANT / 3 NIT**，均为扫描器可选加固：qualname 键控、glob 非空断言、字符串字面量读取探测）。
- 边界如实记录：assistant ingest 不带 agent——归属粒度为 prompt 锚定的轮次；旧 chunk 保持 NULL 无回填；host 未报告即诚实 null。
- 下一步衔接：#75 观察钟随本批合并启动（与 #106 开发并行）；#106 Claude Code 适配器从第一天即可填 agent 字段。
