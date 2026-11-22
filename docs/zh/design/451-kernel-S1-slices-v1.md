# 分片 451-kernel-S1-consumption-instrument v1

> 状态：**PROPOSAL / DESIGN_PENDING（无门权）**。配合 `451-kernel-S1-consumption-instrument-v1.md` 阅读。DESIGN ONLY；本文件不授权工程。
> 前置门：独立跨族设计 QA CLOSABLE + 冻结 + owner 工程 start 授权（PROTOCOL.md:176-190）。冻结≠派发。
> **全局红线：本分片集群不改变任何 retrieval ranking / reinforce 行为；只加观测面。测试用 spare port + temp `MNEMOSEED_LOCAL_HOME`，绝不触 7788 与 `~/.mnemoseed-local`（AGENTS.md:76-79）。**

## 1. 依赖图

```
设计冻结（本文 + 设计 v1）
  |
  +-- S1-A  daemon / store 单元（migration + port + writer + endpoint + feedback + read）
  +-- S1-B  host 单元（plugin.ts emission）        [与 A 文件面互斥，可并行]
              |
              v
           S1-C  oracle + 精度标定规格 + denylist
              |
              v
           S1-D  全门 + 机械核验（无编辑）
```

并行约束：S1-A 与 S1-B 仅在合同（payload/schema/per-chunk `serve_id` 形状，含 registry 结构改动约定）冻结后并行；两者文件面零重叠（A 不碰 `hosts/**`，B 不碰 `daemon/**`、`storage/**`）。S1-C/D 必须在 A+B 均落地后启动（oracle 依赖真实产线）。按 AGENTS.md:47-53，多席位并行须独立 worktree + writer claim；若不愿承担 worktree 开销，A/B 可串行在同一树（其文件面已验证互斥）。

## 2. 分片表

| 片 | 产物（写集） | 依赖 | 验收门 | 备注 |
|---|---|---|---|---|
| S1-A daemon/store | `src/mnemoseed_local/storage/drivers/_migrations.py`（v18 `consumption_events` 表 + 2 触发器 + 2 索引）；`storage/ports.py`（`ConsumptionEvent`/`ConsumptionEventFilter`/append+read port）；`storage/drivers/sqlite_meta.py`（实现）；`daemon/memory.py`（served writer @792 旁、`serve_id` additive 字段、`POST /memory/consumption` router、feedback 追加行 + `RecallFeedbackRequest` 可选 `correlation_id` additive 字段与旧调用方兼容接纳）；`daemon/observability.py`（聚合） | 设计冻结；OD-2/OD-3 裁决（关联键与增长策略） | AC-1,2,5,6,7,10,11；migration 幂等；UPDATE/DELETE 触发器 abort；旧调用方（无 `correlation_id`）feedback 仍 200；ruff/format/mypy/pytest 全绿 | 零 ranking 改动；`serve_id` 为 additive 字段，剥离后顺序/分数逐字节一致 |
| S1-B host | `src/mnemoseed_local/hosts/opencode/plugin.ts`（**结构改动**：registry 值 `Set<chunkId>` → 按 chunk 携带 `serve_id`（如 `Map<needle, Map<chunkId, serveId>>`），`buildRecallInjection`/`buildT2Injection` 返回按 chunk 的 `serve_id`，`mergeRegistry` 保留 per-chunk `serve_id`；`injectedRegistry` 构建处 emit injected、按 `(chunkId,serveId)` emit consumed_attested；新增 additive POST，复用 64 batch + fire-and-forget） | 设计冻结；OD-1 裁决（信号精度档）；A 的 payload 合同 | AC-2,3,4,5,6,7,9；不传正文；不 advance watermark；block/needle/判定器/`/memory/reinforce` 调用字节不变（AC-4）；hook 行为测试重钉留字节恢复证据 | 与 A 互斥文件面；hook 注册不改（`plugin.ts:3011-3014` 保持）；使主键 join 在主导 auto-inject 面可构造 |
| S1-C oracle | 新文件 `tests/test_consumption_instrument.py`（temp home + spare port）、精度标定规格（作为测试内 fixture 与文档内 §6 的实现，不新增 docs 文件） | A + B | AC-1..11 逐条可复现；红相变异体全 RED（含多 serve 归属、弱 join 标注、旧调用方兼容三个新增变异体）；标定报告只记区间不记门限 | 不复用 #391 synthetic rig（INV-1 量纲错位）；标定用真实语料副本 + 人工判读小样本 |
| S1-D verify | 无编辑 | C | 全门（pytest / ruff / format / mypy）；denylist grep 零命中；逐字节恢复证据 | `pwsh -File scripts/gate.ps1`（AGENTS.md:91-93） |

## 3. 文件 allowlist（严格）

- S1-A 允许：`storage/drivers/_migrations.py`、`storage/ports.py`、`storage/drivers/sqlite_meta.py`、`daemon/memory.py`（含 `RecallFeedbackRequest` additive `correlation_id` 字段与 `/memory/recall_feedback` 请求体兼容接纳）、`daemon/observability.py`、`daemon/app.py`（仅 router 挂载必需时）、对应 `tests/test_*consumption*` / `tests/test_*migration*` / `tests/test_*observability*` / feedback 旧调用方兼容测试。
- S1-B 允许：`hosts/opencode/plugin.ts`、对应 `tests/test_hook_ts_behavior.ts` 等已钉 hook 行为测试（若需重钉，须逐项留字节恢复证据）。
- S1-C 允许：新建 `tests/test_consumption_instrument.py` 及 temp 标定 fixture；**不得**写入 `docs/zh/design/`（本批 S1 只有 451-* 两份文档）。
- 全集群禁止：产品配置、`~/.mnemoseed-local`、7788/4096 端口、git 操作于本设计阶段；任何 ranking/reinforcer 参数。

## 4. TDD 红相 / 变异体（必须逐一留 raw 失败行 + 字节恢复证据）

1. **触发器变异**：删除 `trg_consumption_no_update` / `no_delete` ⇒ UPDATE/DELETE 不再 abort ⇒ S1-C 红。
2. **verdict 列变异**：加 `used BOOLEAN` 列 ⇒ schema 断言红（AC-1 / I-3）。
3. **正文外发变异**：payload 携带回复或查询原文 ⇒ AC-3 grep 红。
4. **排序读变异**：在 retriever/assembler/reinforcer 读取 `consumption_events` ⇒ AC-8 红。
5. **沉默即 used 变异**：无 consumed 行时读面返回 used ⇒ AC-9 红。
6. **feedback 权重写变异**：feedback 端点写 `decay_weight`/`last_reinforced` ⇒ AC-10 红。
7. **判定器改动变异**：改 `noteConsumption` 匹配语义或改 `/memory/reinforce` 调用集合 ⇒ 排序序列对比红（B-2 / AC-4）。
8. **幂等变异**：去掉 dedup_key 唯一约束 ⇒ 重复 POST 产生重复行 ⇒ AC-7 红。
9. **多 serve 归属变异**：折叠 per-chunk `serve_id`（registry 退回 `Set<chunkId>`）⇒ 一 needle 多 serve 的 consumed 行归到单一 serve ⇒ AC-5 红。
10. **弱 join 标注变异**：弱回退行读面不标 `join=weak`（或把歧义行当 exact）⇒ AC-5 红。
11. **feedback 兼容变异**：把 `correlation_id` 设为必填而拒绝旧调用方 ⇒ AC-11 红。
12. 恢复后全绿；每个红相留 witness 文件名 + 失败行 + 恢复后 SHA。

## 5. 验收门总表（设计层映射，实现以 S1-C/D 为准）

- AC-1 schema/append-only ✅ S1-A/C
- AC-2 三类写入齐备 ✅ S1-A/B/C
- AC-3 零正文外发 ✅ S1-B/C
- AC-4 排序零改变 ✅ S1-A/B/C（剥离 additive 字段后逐字节对比）
- AC-5 关联主键（per-chunk serve_id；多 serve 同 needle 各自归属）+ 弱回退标注 ✅ S1-A/B/C
- AC-6 fire-and-forget 降级 ✅ S1-A/B/C
- AC-7 幂等 ✅ S1-A/C
- AC-8 排序路径零 reader ✅ S1-C（变异体）
- AC-9 无声=unknown ✅ S1-C（变异体）
- AC-10 feedback 零排序写 ✅ S1-C（变异体）
- AC-11 feedback 向后兼容（旧调用方无 correlation_id 仍 200 / audit 不变）✅ S1-A/C（变异体）

## 6. Owner-decision points（须 owner 显式裁决，本设计不代决）

- **OD-1 信号精度档**：v1 只 instrument literal `citation_substring_v1`（推荐，零模型调用），还是同时要求 token-overlap v1 更稳判定器？二者都不在本片改 ranking；若选后者须给离线标定样本与阈值出处（机制值，非理论锚）。拒绝模型语义裁判（token economy + 自证仪器）。
- **OD-2 语义漂移修复时点**：本片只测量 served/consumed 漂移、保持 reinforce 现状（推荐，与 no-ranking-change 一致），还是授权后续 D5 切片把回弹门控到消费事件？后者须独立设计门 + 排序验收，不得夹带。
- **OD-3 增长与暴露策略**：v1 仅 `(profile_id, observed_at)` 索引 + 分页读、rollup/留存另案（推荐），还是现在就定留存期与 rollup？并确认台账只在本地 meta store、零外发（local-first；竞品默认遥测均拒）。

## 7. 合并顺序与提交纪律

1. 设计 QA CLOSABLE → 冻结 → owner start。
2. S1-A 与 S1-B 并行（独立 worktree + writer claim）或串行；S1-C → S1-D。
3. 单批 closeout：issue → branch → PR（`Closes #<S1 实现 issue>`；#457 为本批 tracking，不直接 close）；每 push commit 绿；squash 前全门绿 + 对抗 QA CLOSABLE（AGENTS.md:20-36）。
4. 清理 worktree 与 merged batch 分支（AGENTS.md:94-109）。

## 8. as-is boundary（冻结前必读）

本分片集群**不修复**任何召回失败或语义漂移：owner 之痛（zcode 自然措辞召回不着）属召回失败上游面，由 S0 契约处理；本片只解决"消费失败下游面无仪表"（`org/_state.md:7`）。竞品"无被动消费信号"是 INFERRED 且未穷尽（`competitor-memory-mechanisms.md:233,242`），**不得**据此对外宣称"品类首个"直到补查。全部实现级行为以 file:line 为准；本设计未验证项已在设计文档 §12 标 `[未验证]`。
