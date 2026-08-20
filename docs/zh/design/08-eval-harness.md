# 08 · 评测臂（Eval Harness）

> 元信息：评测臂是把"验证模型质量"从感觉变成可复跑、可累积、有 bar 的事实的测量工具——canary 工厂（确定性双语既定事实 + 噪声）、scratch rig（1:1 生产接线）、度量/报告/离线重判、矩阵入口（A 模型 × 校验层 × 判定座）。它是测量工具，不是记忆功能。
> 状态基线：B3（squash `phase B3: eval harness` 收口）与 B3.1 尺修正（squash 收口）；门禁 1349 passed / 3 skipped（本仓基线 commit `e95921b` 与 `02ca93d` 同基线）。行号引用一律钉在基线 commit（`02ca93d` 代码内容）之上；B6/B4a 等在途批次合入 src 后，行号引注须随基线推进重钉。
> 主要依据 PRD：`PRD-B3-eval-harness.md`（含 B3.1 修正与收口记录、B4 前置排查定案）、`PRD-B2-roadmap.md`（B3/B4 位）、`docs/zh/design/research-agent-memory-benchmarks.md`（工程文献源）。

## 0. 功能定位与边界

评测臂是 `src/mnemoseed_local/eval/` 子包 + `uv run python -m mnemoseed_local.eval` 入口（**不加 CLI verb，不出产品表面**）。语义定版（PRD-B3 §语义定版，拍板写死进测试）：

1. **被测对象 = dream 链路（reflect → verify → merge），不含 capture 评分**。评测 rig 复用生产 WritingPipeline 进料（真实戳 / embedded 栈），但绕过 ScorePool 时序（评测不管触发调度，roadmap 范围外）：ingest → drain → 全 turn range 直取快照 → `DreamPipeline.run` 一次跑完。
2. **rig 安全边界（红线）**：闭口绝不碰 live 数据——stores/journal 全在调用方给的临时目录根下组装；ollama `base_url` 显式注入；`MNEMOSEED_LOCAL_HOME` 环境变量不影响 rig（所有 CONFIG_DIR 依赖点全部参数化注入）；live daemon 可全程在线不动。报告默认写 `<CONFIG_DIR>/eval/`（纯追加、只增不减），`--out` 可覆盖。
3. **材料（material）两种同源形态**：canary（工厂合成的确定性 session，truth 内嵌，无需人工标注，同 seed 逐字节复现）；replay（真实 snapshot journal JSON，只读复放，落到 rig scratch store 后走同一 dream 链）。材料库只增不减。
4. **canary 语料结构**：每 session = N 条用户源既定事实轮（preference / decision / habit / stance，EN+ZH 混排）+ M 条噪声轮。噪声四类（覆盖 B1 live 判定谱里的典型误收）。
5. **truth 匹配器（确定性，无 LLM 裁判）**：fact 命中 = core 主图存在节点（predicate 类别相等 AND object 命中该 fact 的关键短语集 AND polarity 一致）。matcher 是纯函数，测试钉死。
6. **指标**（每 cell × 每材料一组）：`canary_recall`（**主指标**，既定事实进 core 的比率）、`noise_pollution`（噪声误进 core 率）、`core_yield`（主图 core 节点总数，看过度抽取）、`verify` 组（judged / accepted / rejected / fallbacks[reason 分布]）、成本组（duration_s / tokens / provider_usage，honest nulls）。
7. **矩阵（cell）**：`{reflect route, ensemble, verifier route, delta_budget_ceiling_tokens, core_confidence_floor}`。route = `{driver, model, params}`。
8. **报告**：单 JSON 文件/次跑，schema 本包钉死（`eval_version`、started_at、matrix、cell 块 + skipped）。UTC 时间戳 + cell slug 文件名。
9. **stub 一致性**：harness 逻辑全量 TDD 走 `stub` / `stub_verifier` 双路由（既有驱动），live ollama 仅是 route 换 seat——同一条 rig 代码路径，两种 seat。

边界（诚实）：

- 本包不进 pytest 慢门禁：live 矩阵是人工动作，跑得慢。
- 不做 vote 机制评测（B5 落地后评测臂自然吃到）；不做评分/触发时序评测、不做 retrieval recall 评测（另行立项）。
- 不引入 tokenizer / 网络依赖进单元测试。
- 本包产出数据；**数据不得反向包装成"理论"**（见红线）。

## 1. 流程（mermaid + 走查）

### 1.1 canary 工厂（`eval/canary.py`）

```
flowchart TD
    SEED[seed] --> RNG[random.Random 派生]
    RNG --> FT[既定事实轮<br/>preference/decision/habit/stance<br/>EN+ZH 混排]
    RNG --> NT[噪声轮<br/>meta/mechanical/pleasantry/assertion]
    FT --> S[CanarySession<br/>fact 语序 + 噪声穿插由 seed 派生]
    NT --> S
    S --> M[matches_fact 纯函数<br/>predicate 类根集 + object 词条覆盖 + polarity]
```

走查：

- **CanaryFact(fact_id, predicate, polarity, phrasings)**（`canary.py:45`）：phrasings 为 object 关键短语分组（组内任一命中即算 object 命中，中英可并列）；`predicate` 取四类事实类（prefers / has_habit / decided / believes）。
- **CanaryTurn(text, role, fact_id | None, noise_kind | None)**；噪声 `NoiseKind` ∈ {meta, mechanical, pleasantry, assertion}（`canary.py:35`）：session-meta（"今天我们讨论一下部署流程"）、机械句（"好的，那就先这样"）、寒暄/语气、助手式断言（tier-3 同源语义，应流落 isolated/salvage，绝不进 core）。
- **canary_sessions(seed, ...)** 生成器：模板池 EN+ZH × 四类事实 + 四类噪声；事实内容由确定性词条组合，同 seed 逐字节复现；不同 seed 语料实质不同（`canary.py:431`）。
- **matches_fact 纯函数（B3.1 修订词根集与覆盖）**：谓词匹配 = **类别根集命中**（`PREDICATE_CLASS_ROOTS`，`canary.py:99`——含 live 已观测渲染：prefer/decide/plan/use/switch/run/write/execute/believe/think/打算/偏爱/…），节点谓词归一化入类后比较类别；对象匹配 = **EN 词条子集覆盖**（casefold、有界单数尾 s 修剪、停词剔除）∪ **ZH 子串**（whitespace-free）；fact 级 `accepted_objects` 答案键吸收同义改写（初值 = B3.1 首跑验尸观测到的合法渲染）；polarity 判定不变。覆盖护栏（stub 可抽取）不变。

### 1.2 scratch rig（`eval/harness.py`）

```
flowchart LR
    P[RigPaths root] --> S[stores/journal 子目录<br/>root 之外零写入守卫]
    C[EvalCell] --> R[EvalRig]
    R --> W[WritingPipeline 喂料<br/>同构生产接线]
    R --> D[DreamPipeline.run 一次跑完<br/>全 turn range 直取快照]
    R --> B[绕过 ScorePool 时序<br/>经 trigger pending-manual dream_once 生产 M1 形态]
    W --> O[读回 CellRun<br/>core/isolated triples + audit + token + duration]
```

走查：

- **RigPaths(root)**：root 下派生 stores/journal 子目录；root 之外零写入（守卫断言）（`harness.py:175`）。
- **EvalRoute(driver, model, params)** / **EvalCell(reflect, ensemble, verifier, delta_ceiling, core_floor, cell_id)**（frozen dataclass，slug 确定性）（`harness.py:80` / `101`）。
- **EvalRig(paths, cell)**：装配 `build_stores`（sqlite graph 双实例 main+isolated + sqlite meta + lancedb vector + synthetic embedder）+ `WritingPipeline` 进料 + `FileSnapshotter` + `ReflectOrchestrator`（packer 注入显式 budget=cell.delta_ceiling）+ `TripleVerifier`（ensemble 模式 cell 注入）+ `Merger` + `DreamPipeline`；Config 内存构造（不落盘），`config.llm` 按 cell 物化两角色。
- **run_turns(turns) -> CellRun**（`harness.py:353`）：ingest(user/assistant 角色映射，每 turn 一个 mini-session 保 chunk 归因精确) → settle → drain → **经 trigger pending-manual `dream_once` 生产 M1 形态**（`harness.py:391`——驱动 `TRIGGERED -> DREAMING -> MERGING` 完整状态机，含安全清除，与 `dream --once` 逐字节同路；基建自纠 1 枚：rig 初版直调 `snapshotter.request` 绕过 trigger 导致安全清除永不触发，改走此形态）→ snapshot 全 range → pipeline 一次跑完 → 读回 `CellRun`（merge_summary / core_triples（main 图回读）/ isolated_triples / audit_entries / token_usage / duration_s / reflect_payload）。
- **stub seatFixture**：`ROUTES_STUB`（stub / stub_verifier）开箱即跑；live seat 只换路由。

### 1.3 度量与报告（`eval/{metrics,report,rescore}.py`）

```
flowchart TD
    RUN[CellRun] --> SC[score_canary<br/>canary_recall / noise_pollution / core_yield]
    RUN --> VM[verify_metrics<br/>judged / accepted / rejected / fallback 原因]
    RUN --> CM[cost_metrics<br/>duration / token / provider_usage honest nulls]
    SC --> CR[CellReport]
    VM --> CR
    CM --> CR
    CR --> WR[write_report<br/>eval_version v1.1 + UTC slug 文件名<br/>累积写 CONFIG_DIR/eval 不入 git]
    WR --> RS[rescore 离线重判<br/>v1.1 triple 载荷 + 重建 truth 零 GPU]
```

走查：

- **score_canary(session, run) -> CanaryMetrics**（`metrics.py:75`）：按语义全字段计算；噪声归因是**精确的**——turn 索引 → mini-session id → citing chunk id 集合，core 节点引用噪声 chunk 即污染（`noise_pollution`），与对象文本长什么样无关。0/0 除零：recall 分母 0 时指标为 null 而非崩。
- **verify_metrics(run)**：audit 回放 judged / accepted / rejected / fallback reason 分布；无 audit 时 verify 指标全 null；verify 审计按 run 的 snapshot_id 过滤（跨材料隔离修正）。
- **cost_metrics(run)**：duration / tokens（本 run 月度计数差值，非 rig 累计月）/ provider_usage（reflect / verify 分解不可得时字段显式 null，不编造）。
- **EvalReport + write_report**（`report.py`）：schema `eval_version = "v1.1"` 全字段、UTC ISO 时间戳、文件名 `{utc_compact}-{matrix_slug}.json`（同名不覆盖，时间戳到秒 + 序号兜底）；读回 `load_report` round-trip 钉死（byte-稳定、排序键）。**v1.1 增 `triples` 全量**（route/graph/subject/predicate/object/polarity/confidence/node_id），reader 对 v1 无该字段容忍（→ 空）。
- **离线重打分 `rescore`**（`rescore.py` + `__main__ rescore`）：读 v1.1 报告 + canary seed 重建 truth → 重算 recall 面指标（pollution 维度依赖 chunk 归因，不可离线重算，保留原报告值并如实标注）。实测：对 19:00Z 报告 70 cell 重判**零偏差**（同尺确定性复核成立）。

### 1.4 矩阵入口（`eval/{materials,matrix,__main__}.py`）

```
flowchart TD
    ROSTER[ROSTER_DEFAULT<br/>6 模型] --> MAT[default_matrix<br/>A × {off, verify} × 判定座]
    EXTRA[--extra-route 云锚<br/>Kimi-K3 openai_compatible] --> MAT
    MAT --> PROBE[ollama /api/tags 探活<br/>短超时]
    PROBE -->|缺模型| SKIP[cell → skipped 入报告<br/>missing_model 退出码 0]
    PROBE -->|有模型| RUN[run_matrix 每 cell 每材料]
    RUN -->|failed cell| EXIT[退出码 1]
    MAT --> LIST[--list 只列不跑]
```

走查：

- **Material(kind, name, turns | snapshot)** 联合；`load_replay(path)`：snapshot journal → Material（只读加载，转成 rig 可喂的 turns/chunks 形态；stamp 原样保留，tier/origin 不重演算；journal 相位重置后本 cell 重裁）（`materials.py:49`）。`material_catalog()`：内建 canary 组（seed 定版入 PRD 收口）+ `--materials-dir` 下全部 replay jston（只增不减）。坏 replay 文件 → 显式 typed error 报告行而非 traceback。
- **matrix**：`ROSTER_DEFAULT`（`matrix.py:44` = qwen3.5:9b / gemma4:e4b / qwen3.5:4b / qwen3:8b / qwen3:4b / gemma4:12b，ollama driver，`think=False`，`num_ctx` 按 cell 档位）→ cell 展开：A 模型 × {off, verify} × verifier 对位（默认 B = gemma4:e4b，另配 B = 对侧互换列，得 B1 双向配对列）。`--models` / `--ensemble` / `--verifier` / `--materials-dir` / `--out` / `--list` / `--extra-route` 参面。
- **可用性探活**：ollama `/api/tags`（httpx，短超时）；缺模型 cell → `skipped` 入报告（原因 = 缺 model tag，`missing_model:*` 是 skip，退出码 0），绝不炸矩阵；探活网络失败 → 全部 skipped + 报告诚实。
- **`--extra-route` 云锚席位**：任意 OpenAI-compatible provider（Modal 托管 Kimi-K3 等）以 `parse_extra_route` 入阵（`matrix.py:111`）；API key 走 ENV-VAR NAME（`api_key_env`），报告 / 单元无任何密料；failed anchor 是响亮的 `route_unavailable:` 失败（退出码 1）。
- **`__main__`**（`__main__.py`）：`matrix` / `canary`（stub 通路自检，秒级，recall=1.0 pollution=0，pre-live gate）/ `rescore` 三条子命令；退出码语义：矩阵含 failed cell（非 skipped）→ 1；`matrix_exit_code` 显式分类（missing_model:* = skip → 0；其它 skipped reason = failure → 1）。`canary` 子命令 stub 通路自检 < 5s 内完成。
- **live 矩阵不进 pytest 门禁**（慢，人工动作）。

## 2. 理论锚

评测臂是测量工具，不是记忆功能，**如实"无借用"**。允许的唯一理论性一句（非理论锚）：评测语料形态参照已发表 agent-memory 基准学——research 文档里 MemoryBank AAAI 2024 艾宾浩斯更新、MADial-Bench NAACL 2025 主动 + 被动回忆范式（工程文献 I 类引用，非理论锚；一手性按 research 文档注记抄）。

**红线（照抄）**：评测臂产数据，数据不得反向包装成"理论"——评测指标（recall / pollution / core_yield）度量的是被测模型在该语料上的行为事实，不构成任何神经科学 / 心理学规律；"重复学习 + 校验改善固化"的工程结论是实测事实陈述，绝不冒充认知理论锚。

> 依据出处：`PRD-B3-eval-harness.md`（评测臂语义定版与 B3.1 收口）与 `docs/zh/design/research-agent-memory-benchmarks.md`（工程文献源）。

## 3. 实施方式（code-level）

本文为纯新增 `eval/` 子包；唯一允许的公共面改动 = `dream/__init__.py` 之类导出必要时的最小补充。不动 capture / dream / retrieve 任何生产代码路径。

| 模块 | 文件 | 职责 |
|---|---|---|
| canary 工厂 | `eval/canary.py` | 确定性 EN+ZH 合成会话 + 纯函数 truth matcher |
| scratch rig | `eval/harness.py` | 1:1 生产接线 + 界外零写入守卫 + stub/stub_verifier 席位 |
| 度量 | `eval/metrics.py` | canary_recall / noise_pollution / core_yield / verify / cost |
| 报告 | `eval/report.py` | JSON schema v1/v1.1 全量 triple 载荷 + UTC slug 文件名 |
| 离线重判 | `eval/rescore.py` | v1.1 报告 + 重建 truth，零 GPU 重打 recall |
| 材料 | `eval/materials.py` | replay snapshot journal 只读加载 + material_catalog |
| 矩阵 | `eval/matrix.py` | roster 展开 / 探活跳过 / 云锚席位 / 退出码 |
| 入口 | `eval/__main__.py` | matrix / canary / rescore 子命令 |

关键代码取证：

- 类根集：`canary.py:99` `PREDICATE_CLASS_ROOTS`；`canary.py:225` `matches_fact`。
- 噪声四类：`canary.py:35` `NoiseKind`。
- 生产 M1 形态：`harness.py:391` `self._trigger.handle_event(...) + dream_once`。
- 指标：`metrics.py:75` `score_canary`；`metrics.py:48` `VerifyMetrics`；`metrics.py:60` `CostMetrics`。
- 报告 schema：`report.py:24` `REPORT_SCHEMA_VERSION = "v1.1"`；`report.py:36` `ReportedTriple`。
- 矩阵 roster：`matrix.py:44` `ROSTER_DEFAULT`；`matrix.py:73` `default_matrix`。
- 入口：`__main__.py:43` `_matrix_command` / `:73` `_canary_command` / `:103` `_rescore_command`。

## 4. 红线与诚实边界

- **"fuzz 属于 bar，不属于 matcher" 界线修订**：同义改写与形态变体归入答案键枚举（有限、可审计、PRD 记录），判定逻辑本身保持确定性——绝不引入 LLM 裁判。
- **单跑数值不能当 bar（重大，如实）**：qwen3.5:9b+off 同日两跑 1.00 ↔ 0.00 摆动——验尸（v1.1 载荷直读）：坏跑把 10 枚三元组全部渲染成 "predicate 把整句话打包 + object='None' 串" 的退化形状，reflect 正确判 SALVAGE、merge 正确停进 isolated（护栏工作正常），但主图为空、recall=0。**模型输出形状不稳是召回方差的大头，不是模型杀也不是尺杀**。bar 定版须多跑取共识或只认确定性 cell。
- **确定性零产出（B3.1 收口发现 3 + B4 前置排查定案）**：qwen3.5:9b + verify 席确定性零产出（tok=522, t≈4.3s，两跑同指纹）——**非随机，是采样坍缩**：verify 座参数（`think=False`、无 seed/temperature/num_predict）下 qwen3.5:9b 以 ~67% 概率（15 跑 10 次）吐字面 `[]`，harness `_loads_json_array` + `reflect()` 无空值守卫无重试收下当"确定性零产出"。对照实验：temp=0 → 3/3 `[]`；seed=42 → 3/3 满抽取 780 tok；num_predict=128 → 截断畸形；think=True → 满抽取但烧 7-8k tok。**B4 修法方向（择一或并用，B4 批次定）**：(a) verify 座固定 seed；(b) harness 空值守卫 + 有限重试（`[]` 视为坍缩信号）；(c) qwen3.5:9b 退出 verify 席。列为 **B4 前置候选**。
- **60s 超时墙**：qwen3:4b / gemma4:12b 三跑同指纹 tokens=0 ×4（撞 4×60s reflect 超时）——B4 档位定版的硬输入，`dream.capture_only` 硬模式裁定必据此。
- **云锚链路**：Kimi-K3 以 `openai_compatible` extra-route 进矩阵，off 态 recall=0.50 pollution=0 提供高端参考点；verify 席 9.5s 零 token 快失败（Http 级，非撞墙）——云座 × 本地校验配对待查。
- **评测臂产数据，数据不得反向包装成"理论"**（见 §2）。
- **重复 + 教导只滤抽样噪声 / 个体幻觉**：同源系统性幻觉投票无效（设计稿决策 1 原文）；小模型长期记忆质量最终防线仍是 verbatim 通道 + provenance + isolated 结构（不因评测数据而更改）。
- **报告累积只增不减**：写 `CONFIG_DIR/eval/` 纯追加；`.eval-rigs/` 入 gitignore；报告不入 git 防膨胀。

## 5. 本篇引用

工程文献（I 类，一手性按 `docs/zh/design/research-agent-memory-benchmarks.md` 注记抄录）：

- I1 — MemoryBank：Wanjun Zhong et al., AAAI 2024, arXiv:2305.10250 — SiliconFriend 陪伴机器人；人工手写 194 道探测题（97 英文 + 97 中文，少数双语记忆基准）；引入艾宾浩斯遗忘曲线做记忆更新机制。仓库: https://github.com/zhongwanjun/MemoryBank-SiliconFriend（research 文档 §2.5；语料形态参考，非理论锚）。
- I2 — MADial-Bench：Junqing He et al., NAACL 2025, arXiv:2409.15240 — 记忆增强对话生成，主动 + 被动回忆范式；171 条历史记忆 + 160 段对话 1474 轮，GPT-4 生成 + 人工精修；英中双语。仓库: https://github.com/hejunqing/MADial-Bench（research 文档 §2.5；语料形态参考，非理论锚）。

仓库 PRD / 文档（同主仓 Rxx 状态：本仓库自己）：

- `docs/zh/prd/PRD-B3-eval-harness.md`（评测臂 PRD，含 B3.1 修正与收口记录、B4 前置排查定案）
- `docs/zh/prd/PRD-B2-roadmap.md`（B3/B4 位与批次记录）
- `docs/zh/design/research-agent-memory-benchmarks.md`（工程文献源，2026-08-19 落盘；MemoryBank / MADial-Bench 一手性按此抄录）

实现代码取证源（均在仓库 `src/mnemoseed_local/eval/` 下）：`canary.py`、`harness.py`、`metrics.py`、`report.py`、`rescore.py`、`materials.py`、`matrix.py`、`__main__.py`。
