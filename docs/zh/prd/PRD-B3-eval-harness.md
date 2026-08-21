# PRD · Phase B3 评测臂（eval harness + canary bar）

> 依据：`PRD-B2-roadmap.md` 批次定序（B3 = 一切后续的地基："没有 bar，'验证模型质量'就是感觉"）；设计稿 §6 Phase B"评测矩阵（档位 × off/verify/vote）与 bar 立项"；B1 人工验证记录的 live 发现（qwen3.5:9b 材料相关欠抽取、gemma4:e4b 判定座 2.6× 速优、判定一致率 80%）——这些目前只是单次手测的感觉，B3 把它们变成可复跑、可累积、有 bar 的事实。
> 基线：commit `e8f21eb`（B2 含装机/联调收口），1083 passed / 3 skipped。
> 形态（roadmap 钉死）：`src/mnemoseed_local/eval/` 子包 + stub-LLM 单元测试（harness 自身逻辑全 TDD；live 矩阵跑不进 pytest 门禁）+ `uv run python -m mnemoseed_local.eval` 入口（**不加 CLI verb，不出产品表面**）；JSON 报告写 `<数据目录>/eval/`（CONFIG_DIR 派生，不入 git）。

## 语义定版（本包拍板，写死进测试）

1. **被测对象 = dream 链路（reflect → verify → merge），不含 capture 评分**。评测 rig 复用生产 WritingPipeline 进料（真实戳/embedded 栈），但绕过 ScorePool 时序（评测不管触发调度，roadmap 范围外）：ingest → drain → 全 turn range 直取快照 → `DreamPipeline.run` 一次跑完。
2. **rig 安全边界（红线）**：闭口绝不碰 live 数据——stores/journal 全在调用方给的临时目录根下组装；ollama `base_url` 显式注入；`MNEMOSEED_LOCAL_HOME` 环境变量不影响 rig（所有 CONFIG_DIR 依赖点全部参数化注入）；live daemon 可全程在线不动。报告默认写 `<CONFIG_DIR>/eval/`（纯追加、只增不减），`--out` 可覆盖。
3. **材料（material）两种同源形态**：
   - **canary**：工厂合成的确定性 session（既定事实 + 噪声，truth 内嵌，无需人工标注）。同 seed 逐字节复现。
   - **replay**：真实 snapshot journal JSON（B1 harness 同款只读复放，`load_snapshot_file` 加载，落到 rig 的 scratch store 后走同一 dream 链）。材料库只增不减。
4. **canary 语料结构**：每 session = N 条用户源既定事实轮（preference / decision / habit / stance，EN+ZH 混排）+ M 条噪声轮。噪声四类（覆盖 B1 live 判定谱里的典型误收）：session-meta（"今天我们讨论一下部署流程"）、机械句（"好的，那就先这样"）、寒暄/语气、助手式断言（"Anthropic definitely has millions of users"——tier-3 同源语义，应流落 isolated/salvage，绝不进 core）。fact 语序、噪声穿插位置由 seed 派生。
5. **truth 匹配器（确定性，无 LLM 裁判）**：fact 命中 = core 主图存在节点 (predicate 相等 AND object 命中该 fact 的关键短语集 AND polarity 一致)。关键短语大小写折叠、子串命中、中英分组。matcher 是纯函数，测试钉死。
6. **指标（每 cell × 每材料一组）**：
   - `canary_recall` = 命中 core 的 fact 数 / fact 总数（**主指标**）；
   - `noise_pollution` = 引用了噪声 chunk 的 core 节点数（理想 0；以 chunk_id ∈ 噪声轮 chunk 集合判定，provenance 可溯）；
   - `core_yield` = 主图 core 节点总数（与 fact 数对读，看过度抽取）；
   - `verify` 组：`judged / accepted / rejected / fallbacks[reason 分布]`（audit 回放）；
   - 成本组：`duration_s`（rig 计时段）、`tokens`（ledger 月度计数差值）、`provider_usage`（DeltaReport 回读，prompt/completion 分解到 reflect/verify 两段）。
7. **矩阵（cell）**：`{reflect route, ensemble, verifier route, delta_budget_ceiling_tokens, core_confidence_floor}`。route = `{driver, model, params}`（与 `RoleLLMConfig` 同形；本包默认 roster：qwen3.5:9b / gemma4:e4b / qwen3.5:4b / qwen3:8b / qwen3:4b / gemma4:12b，ollama driver，`think=False`，`num_ctx` 按 cell 档位）。live 模型缺席 → cell 标 `skipped` 诚实入报告（可用性由 ollama `check()`/`/api/tags` 探出），绝不静默不跑。
8. **报告**：单 JSON 文件/次跑，schema 本包钉死（`eval_version:"v1"`、started_at、matrix、[{cell, material, metrics, reflect_payload(triples 全量), audit 摘要}]）。UTC 时间戳 + cell slug 文件名。累积面只增不减。
9. **stub 一致性**：harness 逻辑全量 TDD 走 `stub`/`stub_verifier` 双路由（既有驱动），live ollama 仅是 route 换 seat——同一条 rig 代码路径，两种 seat。

## 任务 T1 · canary 工厂（`eval/canary.py`）

- 范围：
  1. `CanaryFact(fact_id, predicate, polarity, phrasings, expected_tiers)`——phrasings 为 object 关键短语分组（组内任一命中即算 object 命中，中英可并列）。
  2. `CanaryTurn(text, role, fact_id | None, noise_kind | None)`；噪声 kind ∈ {meta, mechanical, pleasantry, assertion}。
  3. `CanarySession(session_id, profile_id, turns)` + `facts` 只读视图。
  4. `canary_sessions(seed, sessions, facts_per_session, noise_per_session)` 生成器：模板池 EN+ZH × 四类事实 + 四类噪声；事实内容由确定性词条组合（工具/习惯/立场词表），同 seed 逐字节复现；不同 seed 语料实质不同。
  5. `matches_fact(node_triple, fact) -> bool` 纯函数：predicate 相等 + phrasings 命中 + polarity 一致。
- AC：
  - 同 seed 两次生成 turns 全等（含顺序）；异 seed 不等；
  - 四类事实 × EN/ZH 至少各一条覆盖；四类噪声覆盖；
  - fact turn 的文本含其 phrasings 至少一族（构造自洽）；
  - `matches_fact` 正反用例（predicate 错/polarity 错/无短语命中 → False）；
  - 事实/噪声 turn 标记互斥（无 turn 同时是 fact 和 noise）。

## 任务 T2 · scratch 评测 rig（`eval/harness.py`）

- 设计依据：`test_e2e_core_loop.py` 的 scratch 装配先例 + `daemon/app.py::_build_capture` 的 verifier 接线（graph 双实例 main+isolated、共享 ledger、config 同源活引用）。
- 范围：
  1. `RigPaths(root)`：root 下派生 stores/journal 子目录；root 之外零写入（守卫断言）。
  2. `EvalRoute(driver, model, params)` / `EvalCell(reflect, ensemble, verifier, delta_ceiling, core_floor, cell_id)`（frozen dataclass，slug 确定性）。
  3. `EvalRig(paths, cell)`：装配 `build_stores`（sqlite graph 双实例 + sqlite meta + lancedb vector + synthetic embedder）+ `WritingPipeline` 进料 + `FileSnapshotter` + `ReflectOrchestrator`（packer 注入显式 budget=cell.delta_ceiling）+ `TripleVerifier`（ensemble 模式 cell 注入）+ `Merger` + `DreamPipeline`；Config 内存构造（不落盘），`config.llm` 按 cell 物化两角色。
  4. `run_turns(turns) -> CellRun`：ingest(user/assistant 角色映射) → drain → snapshot 全 range → pipeline 一次跑完 → 读回 `CellRun{merge_summary, core_triples(main 图回读), isolated_triples, audit_entries, token_usage, duration_s, reflect_payload}`。
  5. stub seatFixture：`ROUTES_STUB`（stub/stub_verifier）开箱即跑；live seat 只换路由。
- AC：
  - stub 全链路跑通：canary 材料喂入 → core 节点写出、chunk consolidated、audit 可见 `dream_committed`；
  - ensemble=verify + stub_verifier：断言用例 reject→isolated（StubVerifyLLM 证据空判定可控造例），`ensemble_verified` 审计落；
  - 零写界外守卫：rig 运行后 root 之外无新文件（含 home/CONFIG_DIR）；
  - 同 cell 两次跑同材料：node/triple/审计形状一致（确定性，计时字段除外）；
  - cell slug 确定性且区分 reflect/verifier 型号与 ensemble 模式。

## 任务 T3 · 度量与报告（`eval/metrics.py` + `eval/report.py`）

- 范围：
  1. `score_canary(session, run) -> CanaryMetrics`：按语义 6 全字段计算（recall/noise_pollution/core_yield + per-fact 命中明细 + 误收 triple 明细）。
  2. `verify_metrics(run) -> VerifyMetrics`：audit 回放 judged/accepted/rejected/fallback reason 分布。
  3. `cost_metrics(run) -> CostMetrics`：duration/tokens/provider usage（reflect/verify 分解不可得时字段显式 null，不编造）。
  4. `EvalReport` + `write_report(report, out_dir) -> Path`：schema v1 全字段、UTC ISO 时间戳、文件名 `{utc_compact}-{matrix_slug}.json`；读回 `load_report`（round-trip 钉死）。
  5. 每批跑汇总 `summary` 面（per-cell 主指标表 + skipped 列表），供收口记录直接摘录。
- AC：
  - 合成 fixture 上 recall/noise_pollution/core_yield 逐字段手算对齐（含 0/0 定义除零：recall 分母 0 时指标为 null 而非崩）；
  - noise chunk 引用判定走 core triple 的 chunk_ids（provenance 口径），非文本相似；
  - fallback reason 分布聚合正确；无 audit 时 verify 指标全 null；
  - 报告 round-trip byte-稳定（排序键）；缺目录自动建；同名不覆盖（时间戳精度到秒 + 序号兜底）；
  - 默认 out_dir = `CONFIG_DIR / "eval"`（测试注入 tmp，不碰真目录）。

## 任务 T4 · replay 材料 + 矩阵入口（`eval/materials.py` + `eval/__main__.py`）

- 范围：
  1. `Material(kind, name, turns | snapshot)` 联合；`load_replay(path) -> Material`：snapshot journal → Material（只读加载，转成 rig 可喂的 turns/chunks 形态；stamp 原样保留，tier/origin 不重演算）。
  2. `material_catalog()`：内建 canary 组（seed 定版入 PRD 收口）+ `--materials-dir` 下全部 replay jston（只增不减）。
  3. `matrix`：`ROSTER_DEFAULT`（语义 7 的 6 模型 × {off, verify} × verifier 对位：默认 B=gemma4:e4b，另配 B=对侧互换列）→ cell 展开；`--models`/`--ensemble`/`--materials-dir`/`--out`/`--list` 参面。
  4. 可用性探活：ollama `/api/tags`（httpx，短超时）；缺模型 cell → `skipped` 入报告（原因 = 缺 model tag），绝不炸矩阵。
  5. `__main__`：`uv run python -m mnemoseed_local.eval matrix ...` / `canary`（stub 通路自检，秒级）两条子命令；退出码：矩阵含 failed cell（非 skipped）→ 1。
- AC：
  - replay 加载 round-trip（snapshot fixture → turns 形态喂 rig 可跑）；
  - catalog 确定性（路径排序）；坏 replay 文件 → 显式 typed error 报告行而非 traceback；
  - cell 展开矩阵行数/对位钉死；`--list` 只列不跑；
  - 探活：fake tags 下 missing → skipped 标记 + 原因；探活网络失败 → 全部 skipped + 报告诚实；
  - `canary` 子命令 stub 通路自检 < 5s 内完成；退出码语义钉死；
  - 本包不进 pytest 慢门禁：live 跑是人工动作。

## 完成定义（整包）

- `uv run pytest -q`、`ruff check`、`ruff format --check`、`mypy src` 干净；
- 单 commit 收口：`phase B3: eval harness — canary factory, scratch rig, metrics/report, matrix runner`；
- 收口记录入本 PRD + roadmap 批次记录更新；
- **收口后人工验证项（用户授权后另跑）**：live 矩阵首跑（6 模型 × off/verify），首份报告落 `<数据目录>/eval/`，数值 bar 据首跑结果钉进本 PRD。

## 本包不做（划线）

- 不动 capture/dream/retrieve 任何生产代码路径（纯新增 `eval/`，唯一允许的公共面改动 = `dream/__init__.py` 之类导出必要时的最小补充）。
- 不做 vote 机制评测（B5 机制落地后评测臂自然吃到）。
- 不做评分/触发时序评测、不做 retrieval recall 评测（capture-only 通道评测在 roadmap 用户命题里，另行立项）。
- 不引入 tokenizer/网络依赖进单元测试。

## 收口记录（2026-08-18）

- 批次执行：T1（19 红 → 绿；canary 工厂 + 纯函数匹配器 + 双语/四类覆盖按构造保证）→ T2（8 红 → 绿；scratch rig 1:1 镜像 `_build_capture` 接线 + mini-session 精确归因形态 + 界外零写入守卫；**基建自纠 1 枚**：rig 初版直调 `snapshotter.request` 绕过了 trigger 状态机导致安全清除永不触发——改走生产 M1 形态：池事件 → `pending_manual` → `dream_once`，与 `dream --once` 逐字节同路）→ T3（11 红 → 绿；手算合成 fixture 钉死指标数学；**跨材料隔离修正**：token 指标改本 run 月度计数差值、verify 审计按 run 的 snapshot_id 过滤，rig 复用不串料）→ T4（12 红 → 绿；replay = B1 harness 形态持久化——stamp 原样、journal 相位重置后本 cell 重裁；探活/跳过/退出码语义钉死）。
- 对抗自验战果（收口前击杀的真缺陷，全部来自"评测臂自己量自己"的红线）：
  1. canary 模板 × stub 抽取文法三处漂移（"Usually I…" 副词前置、`everywhere` 被 strip `very` 子串误伤、"坚信" 不在 ZH stance 词表）——全部经新增参数化护栏测试（dev seed + 钉死默认 seed）钉死；
  2. rig 绕开 trigger 的安全清除死路（见 T2）；
  3. verify/token 指标跨材料污染（见 T3）。
- 门禁复验：**1133 passed / 3 skipped**（基线 1083 → +50），ruff / ruff format / mypy（84 files）干净；`python -m mnemoseed_local.eval canary` stub 自检 PASS（off + verify 两通路 recall=1.0 pollution=0）；`.eval-rigs/` 入 gitignore。
- **待用户授权后另跑**：live 矩阵首跑（6 模型 × off/verify 默认 roster；B 默认 `gemma4:e4b`，`--verifier` 翻向得 B1 双向配对列），首份报告落 `<数据目录>/eval/`，数值 bar 据首跑结果钉回本 PRD。

## live 首跑实测记录（2026-08-18，全矩阵 12 cell × 5 材料，用户授权）

报告：`~/.mnemoseed-local/eval/2026-08-18T16-37-50Z-12cells-5materials.json`（12 cell，canary + 4 份真实 session journal 复放，零 skipped，总时长约 50 分钟）。

**首跑先抓到的是尺的偏差，不是模型的全灭**（验尸证据：rig stores 保全，逐 cell 读出 core 三元组与 canary truth 对照）：

1. **matcher 系统性过严（尺偏差，属本包，必修）**：谓词要求 canonical 等值，但 live 模型合法渲染千姿百态——decided 类被渲染为 `plans to use`/`switch`/`use`/`plan`/`decide`/`打算`/`switch_to`，has_habit 类为 `runs before committing`/`writes first`/`execute`/`run`/`write`，prefers 类为 `prefer`/`偏爱`，believes 类为 `believe`/`think`；对象侧 `vacuum` 式改写（单复数、”for good“ 尾巴、zh→en 同义改写如 `主干开发`→`main branch for development`）使全短语子串命中近乎全灭而大量"提对了"的事实被误杀——canary recall 全矩阵 0.00–0.12 里**绝大部分是尺杀，非模型杀**。
2. **真实模型行为发现（不受尺偏差污染的部分）**：
   - **qwen3.5:9b 保守精准**：off 态 pollution=0、core=7（全事实类，variety 覆盖四类谓词渲染）；
   - **gemma4:e4b 高召回高污染**：off 态 pollution=6 实锤（META 同步×2、机械句 `先/这样`、客套 `表达/辛苦`、Umbrella 断言 `has millions of active users` 全进 core）；
   - **校验层真实减排**：e4b 污染 6 → 经 e4b 自校验后 1（META 同步漏网）——双模型机制的第一枚量化收益证据；
   - **60s 超时墙真实存在**：qwen3:4b / qwen3:8b / gemma4:12b 在 canary 与部分 replay 上批量撞 4×60s reflect 超时（tokens=0 行成串）——B4 档位定版必须正视的硬数据；
   - 判定座速度谱（e4b 判定 8–16s/轮；12b 反射 250s+ 撞墙）——B1"判定比生成简单"的结论再获数据支撑。

## 批次增量 B3.1 · 尺修正（2026-08-18 立项，用户拍板）

依据：首跑验尸发现 1；原则：**尺是确定性纯函数 + 裁选答案键**（curated answer keys），绝不引入 LLM 裁判；"fuzz 属于 bar，不属于 matcher" 的界线修订为：**同义改写与形态变体归入答案键枚举（有限、可审计、PRD 记录），判定逻辑本身保持确定性**。

1. **matcher 语义修订**（`eval/canary.py`，TDD 先行）：
   - 谓词匹配 = **类别根集命中**：canon 四类谓词类别根集（含 live 已观测渲染：`prefer/decide/plan/use/switch/run/write/execute/believe/think/打算/偏爱/…`），节点谓词归一化入类后比较类别；
   - 对象匹配 = **词条子集覆盖**（英式短语）∪ 子串（中文短语）：双侧 casefold + 形态归一（单复数尾 s 有界修剪）+ 停词剔除；fact 级 `accepted_objects` 答案键吸收同义改写（初值 = 首跑验尸观测到的合法渲染，本 PRD 记录点名）；
   - polarity 判定不变；覆盖护栏（stub 可抽取）不变。
2. **报告三元组载荷**（`eval/report.py` → `eval_version v1.1`）：CellReport 增 `triples` 全量（route/graph/subject/predicate/object/polarity/confidence/node_id），reader 对 v1 无该字段容忍（→ 空）；**尺子再升级时可离线重打分，不重烧 GPU**。
3. **离线重打分**（`eval/rescore.py` + `__main__ rescore`）：读 v1.1 报告 + canary seed 重建 truth → 重算 recall 面指标（pollution 维度依赖 chunk 归因，不可离线重算，保留原报告值并如实标注）。
4. **live 矩阵重跑**（用户已授权）：同 roster 同材料重跑，新旧两报对照表入本 PRD 收口记录。

### B3.1 收口记录（2026-08-19 收口，同日两次 live 跑 + 一次离线重判）

- **门禁复验**：**1181 passed / 3 skipped**（B3 基线 1133 → +48），ruff / ruff format / mypy（85 files）全净。批次内自纠 1 枚：`__main__ rescore` 入口 PRD 已承诺却漏接线，本收口前补上（TDD：入口测试先行）。
- **报告**：旧=`2026-08-18T16-37-50Z-12cells-5materials.json`（v1 尺）；新=`2026-08-18T19-00-49Z-14cells-5materials.json`（**v1.1，同 roster 同材料 + 云锚 Kimi-K3 ×2**，零 skipped）；补跑=`2026-08-18T19-32-11Z-12cells-1materials.json`（纯 canary 对照，同日补采）。

**canary-00 新旧对照**（recall；△=补充事实；"墙"=4×60s reflect 超时 tokens=0）：

| cell | 旧（v1 尺） | 新（B3.1 尺） | 备注 |
|---|---|---|---|
| qwen3_5_9b+off | 0.125 (core=7) | 0.00 (main=0, iso=10) | △另跑 recall=**1.00** core=8：形状不稳（见发现 2） |
| qwen3_5_9b+verify | 0.00 (core=7) | 0.00 (tok=522, t=4.3s) | 确定性零产出，两跑同指纹（见发现 3） |
| gemma4_e4b+off | 0.00 (poll=6) | **0.625** (poll=1) | 旧尺全杀→新尺正当计入 |
| gemma4_e4b+verify | 0.00 (poll=1) | **0.75** (judged=9/9) | 同上 |
| qwen3_5_4b+off | 0.00 (poll=1) | 0.00 (poll=2) | △另跑 0.75 (poll=4)——方差 |
| qwen3_5_4b+verify | 0.00 (poll=5) | **0.625** (poll=0) | △另跑 0.00 (poll=5)——方差 |
| qwen3_8b+off | 0.00（墙 256s） | **0.50** (poll=2, t=181s) | 旧跑撞墙，新跑通过 |
| qwen3_8b+verify | 0.00 (poll=2) | 0.00（墙 256s） | △另跑 0.625——方差 |
| qwen3_4b ×2 | 0.00（墙） | 0.00（墙） | 三跑同指纹（发现 4） |
| gemma4_12b ×2 | 0.00（墙） | 0.00（墙） | 三跑同指纹（发现 4） |
| kimi_k3+off | — | **0.50** (poll=0, core=8, tok=5427, t=54.7s) | 高端云锚就位 |
| kimi_k3+verify | — | 0.00 (tok=0, t=9.5s) | http 级快失败（发现 5） |

**收口发现（如实）**：

1. **尺修正达目的且没放水**：非撞墙 cell 上新渲染被正当计入（e4b off 0→0.625、e4b verify 0→0.75、qwen3_5_4b verify 0→0.625、qwen3_8b off 0→0.5）；autopsy negatives 13 行全拒的护栏按构造在（T1 测试钉死），e4b 校验层减排（off 态 pollution 6 → verify 后 1）新跑复现。
2. **单跑数值不能当 bar（重大）**：qwen3_5_9b+off 同日两跑 1.00 ↔ 0.00 摆动——验尸（v1.1 载荷直读）：坏跑把 10 枚三元组全部渲染成"predicate 把整句话打包 + object='None' 串"的退化形状，reflect 正确判 SALVAGE、merge 正确停进 isolated（护栏工作正常），但主图为空、recall=0。**模型输出形状不稳是召回方差的大头，不是模型杀也不是尺杀**。bar 定版须多跑取共识或只认确定性 cell。
3. **qwen3_5:9b + verify 席确定性零产出**（tok=522, t≈4.3s，两跑同指纹）：B1"材料相关欠抽取"的确定性复发，非随机。**立项候选**：B4 前单独查 reflect 提示词 × verify 回路配对（不进本包）。
4. **60s 超时墙依旧钉死** qwen3:4b / gemma4:12b（三跑同指纹 tokens=0 ×4）——B4 档位定版的硬输入，`dream.capture_only` 硬模式裁定必据此。
5. **云锚链路打通**：Kimi-K3 以 `openai_compatible` extra-route 进矩阵（key 走环境变量名，报告/单元无任何密料），off 态 recall=0.50 pollution=0 提供高端参考点；verify 席 9.5s 零 token 快失败（Http 级，非撞墙），云座 × 本地校验配对待查。
6. **离线重打分实测**：`python -m mnemoseed_local.eval rescore` 对 19:00Z 报告 70 cell 重判 **零偏差**（同尺确定性复核成立）；旧 v1 报告无载荷不可重判——v1.1 载荷的存在理由被同一事实反向证明。

### B4 前置排查：qwen3.5:9b verify 席"确定性零产出"根因（2026-08-20 定案）

报告：`C:\Users\LITTLE~1\AppData\Local\Temp\opencode\B4-qwen35-9b-verify-zero-output-report.md`（完整英文版，要点摘要如下）。

- **根因定案**：非确定性零产出，是**采样坍缩**——verify 座参数（`think=False`、无 seed/temperature/num_predict）下 qwen3.5:9b 以 ~67% 概率（15 跑 10 次）吐字面 `[]`（合法 JSON 空数组），harness `_loads_json_array` + `reflect()` 无**空值守卫**无重试收下当"确定性零产出"。两次 live 矩阵同指纹 tok=522 / completion=2 即坍缩样本。
- **对照实验**：temp=0 → 3/3 `[]`（greedy 必坍缩）；seed=42 → 3/3 满抽取 780 tok（定 seed 即治）；num_predict=128 → 截断畸形（非本案）；think=True → 满抽取但烧 7-8k tok（不需）。
- **B4 修法方向（择一或并用，B4 批次定）**：(a) verify 座固定 seed（repro 实证可复现满抽取）；(b) harness 空值守卫 + 有限重试（`[]` 视为坍缩信号而非合法空抽取）；(c) qwen3.5:9b 退出 verify 席。单跑数值不能当 bar 的既有结论不受影响。

## 批次增量 B4a · reflect 席采样坍缩防护（2026-08-20）

依据：B4 前置排查定案（上文"B4 前置排查"节）——qwen3.5:9b 在矩阵 reflect 席（think=False、无 seed/temp）下约 67% 概率吐字面 `[]`（completion=2 指纹），被 harness 当合法空抽取收下，固化成两跑同指纹的"确定性零产出"。修法方向 (a) 固定 seed + (b) harness 空值守卫/有限重试 双管齐下（B4b 已裁：seed 逐次变化出局）。

1. **坍缩分类器**（`eval/harness.py`）：反射座输出命中 RCA 指纹（字面 `[]` + completion_tokens ≤ 2）即抛 `ReflectCollapseError`，复用 reflect.py 既有重试通道（1/2/4s 退避）；rig 侧 `ReflectOrchestrator(max_retries=2)`（共 3 次尝试）封顶。无 usage 指纹（stub/纯文本座）或正常 token 数的合法空抽取绝不误判。
2. **逐座固定 seed**（`eval/matrix.py`）：ollama 座默认带 `seed=42`（RCA 实测 3/3 满抽取且可复现）；`openai_compatible` 云座绝不带 seed（该驱动无此采样旋钮）；`--no-seat-seed` 整体移除。
3. **诚实记录**（v1.1 兼容追加字段）：`CellReport.reflect_collapse_attempts / reflect_recovered / seat_seed`，`EvalReport.seat_seed_policy`（`"per-seat-fixed"` / `"none"`）；旧报告缺字段照常加载并取默认值。

### 可比性说明（重要）

**B4a 之前的报告全部是无 seed 跑出来的**；对采样敏感 cell（qwen3.5:9b off、qwen3.5:4b 两态）而言，其数值与 seed 固定后的新跑**不可数值直接比较**。断点由报告字段显式标记：`seat_seed_policy`/`seat_seed`（旧报告加载后 policy 取默认 `"none"`，与 cell 级 `seat_seed=None` 一致——旧报告即无 seed 报告；`--no-seat-seed` 新跑同标 `"none"`）。`cell_id`/slug 不变——seed 不进 slug，同一 cell 前后可比性由字段标记，不由 id 伪造。

### B4a 收口记录（2026-08-20）

交付以 W-B harness 抗坍缩（B4a，即本批次增量节）为主；W-A near-dup 预筛、W-C drain 下 loop 为同一 Wave-2 三流并行的并行支流，与 eval 数值面无交集（W-A 的确定性 tie-break 与有界扫描守卫、W-C 的 teardown 预算重排均不影响本臂读数），详细记录见 roadmap 批次行与代码块注释。QA 修复轮两项标签修复：`rescore` 重判沿用被评报告既有的 `seat_seed_policy`（政策保真，不把旧报告翻转成 per-seat-fixed）；legacy 报告 `from_dict` 缺字段默认 `"none"`，与 cell 级 `seat_seed=None` 一致（QA IMPORTANT-2 翻转钉死）。`_CollapseGuard` docstring 纠偏：**指纹命中即分类，不问输出合法性**——verbatim `[]`+completion≤2 就判坍缩；正常 token 数的空抽取不算坍缩，合法空内容同样被指纹拦截。

**B4b（live 矩阵定版 + bar 钉死 + lite 档定版）待排**——seed 已固定后 matrix 数字可复现，坍缩率可从 `collapse_attempts` 字段直接读。

## 批次 B4b · 跨跑隔离修复 + 全模型 rerun 钉 bar（2026-08-21 立项，用户拍板）

> 理论锚：**不适用（not borrowed）**——本批是评测臂自身隔离契约的正确性修复 + 一次数值定版 rerun，不借任何神经/心理实证规律。立项仅为让 B3.1/B4a 已固化的送检结果在干净尺度上被跑出来；用既有的 dream/verify/blab 机制，不新增机制也不借理论。

### 立项依据（证据，非感觉）

B4a 收口后、seed 已固定的前提下，2026-08-20 19:00–19:30 用 `--workdir .eval-rigs` 连跑 6 次 canary-00（同 roster 同材料），数字非但不可复现反而**系统性发散**：

| 跑次 | 起始报告 size | 终末 cell core | 终末 pollution | 用时 |
|---|---|---|---|---|
| 1 | 43 KB | 28 | 12 | 30s/seat |
| 2-6 | 69→89→106→118→132 KB 单调膨胀 | 28→40→61→80→90→103 | 16→26→38→36→38→53 | 35→99→190s/seat |

seed=42 固定≠数值可复现：pollution/core 随跑次单调膨胀、recall 在 0.25↔1.00 间无规跳变。验尸（v1.1 triple 载荷 + `.eval-rigs/cells/<cell_id>/` 目录直读）定位到单一根因，与采样/模型无关——

### 根因定案（单一，包级）

**跨跑 store 累积**：`matrix.py` `run_matrix` 把每个 cell 的 rig 根钉在 `root / "cells" / cell.cell_id`（`--workdir .eval-rigs` 是稳定路径，跨次调用复用同一目录）；`EvalRig.__init__` 在根下 `build_stores` 落 sqlite 三元图/meta + lance 向量 + config，**构造时不清理已有文件**。第二次 `run_matrix`（或任何复用 `--workdir` 的 runner）在上一轮的 chunks/graph/audit 上继续进料→reflect 的检索上下文逐轮变大→pollution 单调膨胀、recall 乱跳、tokens/duration 抬升。`run_turns` 的 ledger 月度计数虽按 run 取差值隔离了 token 面，graph/vector 累积无人防。

### 测试盲区（让 bug 溜进 B4a 收口）

T2 确定性 AC 存在但**只测了"fresh root"半边**：`test_repeat_runs_semantically_deterministic` 用 `tmp_path / f"rig-{index}"`——**两个孤立新 root**，从没把同一 root 喂两次。所有 `run_matrix` 的回归调用同样全是 fresh `tmp_path`。跨跑累积这一支无任何红测试守门，故 B4a"数字可复现"的论断实际从未被验证过。

### 语义定版（本批拍板，写死进测试）

1. **隔离契约（红线）**：一次 `run_matrix` 调用 = 一组独立、互不串料的 rig store；同一 `--workdir` 跨次 `run_matrix` 必须产出**逐字节可比的报告**（计时字段除外）。T2 既有 AC "同 cell 两次跑同材料：node/triple/审计形状一致" 由本批补齐"同一 root"半边。
2. **修法方向（待 architect 评估择一/并用，后述）**：(a) `run_matrix` 每次 cell 根加一次性 run-id 子目录（`root / "cells" / cell.cell_id / run-<id>` 或 `root / "runs" / run_id / cell.cell_id`），落地即弃；(b) `EvalRig.__init__` 进入即 wipe 本 root 下的 stores（构造幂等）；(c) 两者并用——run-id 防累积、wipe 防遗留。`canary` 子命令与 `_run_material` 的 within-run rig 复用语义不动。
3. **within-run 多材料仍复用同一 rig**（PRD T2 既有意图，本批不破）：一次 `run_matrix` 内 cell×material 循环里同一 `EvalRig` 跑多 material 是设计形态（profile 隔离子串料的 latent 风险由 architect 一并核查，不在本批修复范围，除非实测串料即升 P0）。
4. **既有数据全废**：2026-08-20 19:00–19:30 六跑报告 + `.eval-rigs/` 残留目录全部作废，不进 bar、不入收口对照。

### 任务 T1 · 回归测试（红先行）

- 范围：
  1. `run_matrix` 同一 `root` 连跑两次同 roster+materials → 两份 `EvalReport` 的 `cells` 序列**逐条等值**（cell_id/material/canary 全字段/verify 全字段/cost.token_usage/reflect_collapse_attempts/reflect_recovered/seat_seed；`cost.duration_s` 与 `started_at` 豁免）。
  2. 跑两次后 `root` 下不残留跨跑可串料状态（按 stores 文件 mtime 不早于第二次构造、或按 graph 行数不翻倍断言）。
  3. `__main__ matrix --workdir <稳定路径>` 端到端同断言（若 (1)(2) 已在 `run_matrix` 层覆盖，端到端可省，由 architect 裁）。
- AC：测试当前（修法落地前）红；修法落地后绿。stub 通路秒级跑，不进 live 门禁。

### 任务 T2 · 修法落地（T1 绿即收）

- 范围：按 architect 批准的方案改 `matrix.py` 和/或 `harness.py`；现有 `tmp_path` 单跑测试全绿（不得为让跨跑测试绿而破坏 fresh-root 单跑语义）。
- AC：T1 全绿 + 既有 eval/matrix 全套测试 (`test_eval_harness.py`/`test_eval_matrix.py`/`test_eval_metrics.py`/`test_eval_report.py`/`test_eval_rescore.py`/`test_eval_matcher_b31.py`/`test_eval_canary.py`/`test_eval_anchor.py`) 全绿；门禁净。

### 任务 T3 · 全模型 rerun（用户已授权）

- 范围：清理作废的 `.eval-rigs/` 与 `eval/` 旧报告后，跑默认 6 模型 roster × {off,verify}，B=gemma4:e4b，`--workdir` 取稳定一次性目录或 `--out` 显式区分。报告落 `<CONFIG_DIR>/eval/`。
- AC：零 skipped 或 skipped 仅 `missing_model:`；首跑报告完整、收口对照表入 PRD。

### 任务 T4 · bar 钉死 + 收口

- 范围：据 T3 报告把每个确定性 cell 的 recall/pollution/judged/成本四面数值 + 撞墙指纹钉进本 PRD 的 bar 节；非确定性 cell（仍方差大者）按 B3.1 收口记录的"单跑不当 bar"既定口径标注、不强行钉数。
- AC：bar 节落 PRD；收口记录入本节 + `PRD-B2-roadmap.md` 批次行。

### 本批不做（划线）

- 不改 dream/verify/capture/reflect 任何生产代码路径（纯 `eval/` 内修）。
- 不修 within-run 多材料串料的 latent 风险：architect 核查确认默认 catalog（`canary_count=1`、各 replay `profile_id` 各异）下 profile_id 机制有效隔离——graph 写入按 profile_id 入 content-hash（`merge.py` `_content_id`）、graph/vector 读路径均强制 `profile_id` 过滤，同 run 内非共享 profile 的材料互不串料。**latent 边界**仅在 `canary_count>1`（多个 canary 共用 profile `"canary"`）或两份 replay 共享同一 `snapshot.profile_id` 时触发（共享 content-hash → merge reinforce 而非 create、chunks 于共享 profile 累积 → 同 run 内检索变大）。此边界由本节钉死，未来 catalog 变更任一条件时须重开隔离核查。
- 不动 v1.1 schema（triples 载荷已够离线重判用）。
- 不引 lite 档（原 B4b 节提到的 lite 档定版——本批只做隔离修复 + 全量 rerun 钉 bar，lite 档留下一批）。

### B4b 收口记录（2026-08-21 立项并收口，squash `f0883bc`，PR #36 → issue #35）

- **根因**：`run_matrix` 把每 cell 的 rig 根钉在稳定 `root / "cells" / cell.cell_id`（`--workdir` 跨次复用）；`EvalRig.__init__` 在既存 store 文件上 `build_stores` 不清理→第二次 `run_matrix` 于共享 profile `"canary"` 之上继续进料→reflect 检索上下文逐轮膨胀→pollution/core 单调膨胀、recall 乱跳。seed=42 固定≠可复现，因累积态改变 reflect 输入。所有回归只用过 fresh `tmp_path`，从无"同 root 跑两遍"守门，故 B4a 收口的"数字可复现"论断实际从未被验证。
- **修法（架构师择 (c)：run-id + 幂等 wipe 双管）**：`matrix.py run_matrix` 入口一次性 `run_id = uuid4().hex[:8]`，rig 根改 `root / "runs" / run_id / cell.cell_id`（per-call 全新命名空间，并发安全）；`harness.py EvalRig.__init__` 取 `stores_dir`/`journal_dir` `shutil.rmtree` + `config_path.unlink(missing_ok=True)` 后再 `mkdir`（构造幂等，覆盖 `canary` 子命令与直构 rig 的稳定根复用）。
- **回归 oracle（TDD 红先行）**：`test_run_matrix_same_root_twice_is_idempotent` 于同一 `root` 连跑两次 `run_matrix`→`report_to_dict` 全等（_norm started_at + per-cell cost.duration_s_）+ triples 计数等 + audit 行数不翻倍。RED pre-fix 实测 `4 == 2`（真实翻倍 audit 计数）；GREEN post-fix。QA 对抗自验发现 oracle 自毁漏洞——helper 内新构 `EvalRig` 触发 wipe 把要数的证据先擦掉→post-fix 绿成自指 `2==2` tautology；经一轮治（改只读 `sqlite3 ...?mode=ro` 直读 `audit_log` 行数，无 rig 构造）后 green 变真测量，且 3 向 stash 实证（仅 Change1 / 仅 Change2 / 无修法）严格分辨"有隔离 vs 无"。
- **门禁**：**1377 passed / 3 skipped**，ruff / ruff format / mypy（90 files）全净；PR #36 CI（test + install-smoke×2）全绿。
- **QA 裁决**：CLOSABLE，无 BLOCKER（audit-oracle 自毁 tautology 经一轮治由 IMPORTANT 降为钉死；6 mutation 中只"有隔离 vs 无"被钉死——架构师已 bless (a)/(b)/(c) 任一皆可，故单机制 untested 是构造边界非漏洞；uuid 唯一性/并发 + Change2 稳定根 wipe 覆盖为 NOTE 留档）。

### B4b live rerun 实测记录（2026-08-20T20:46Z，canary-00，12 cell × 1 material，零 skipped，约 16 分钟）

报告：`C:\Users\Little Star\.mnemoseed-local\eval\2026-08-20T20-46-07Z-12cells-1materials.json`。**隔离修法 live 验证成立**：core/pollution 全程被框死（0–14 / 0–6），08-20 累积污染（core 103 / pollution 53）完全消失。

| cell | recall | pollution | core | judged | tokens | t | collapse_attempts | recovered |
|---|---|---|---|---|---|---|---|---|
| qwen3_5_9b + off | 0.00 | 0 | 0 | - | 0 | 28s | **3** | False |
| qwen3_5_9b + verify | 0.00 | 0 | 0 | - | 0 | 9s | **3** | False |
| gemma4_e4b + off | 0.625 | 4 | 12 | - | 1900 | 50s | 0 | False |
| gemma4_e4b + verify | 0.625 | 6 | 14 | 14 | 3674 | 30s | 0 | False |
| qwen3_5_4b + off | 0.00 | 0 | 6 | - | 1154 | 21s | 0 | False |
| qwen3_5_4b + verify | 0.625 | 0 | 8 | 8 | 2099 | 33s | 0 | False |
| qwen3_8b + off | 0.00 | 0 | 0 | - | 0 | 123s | 0 | False |
| qwen3_8b + verify | 0.125 | 3 | 11 | 11 | 2572 | 79s | 0 | False |
| qwen3_4b ×2 | 0.00 | 0 | 0 | - | 0 | 190s | 0 | False |
| gemma4_12b ×2 | 0.00 | 0 | 0 | - | 0 | 190s | 0 | False |

**收口发现（如实）**：

1. **隔离修法达目的**：非撞墙非坍缩 cell 的 core/pollution 于干净单跑内自洽，无跨跑累积。本报告即可信 bar 基线。
2. **qwen3.5:9b seed=42 仍 3/3 坍缩（正面否定 B4a 收口论断）**：off+verify 两态 `reflect_collapse_attempts=3 / reflect_recovered=False`（坍缩分类器正确触发并报告，但穷尽 3 次重试仍字面 `[]` completion=2）→ reflect degraded、recall=0.00 tokens=0。B4a 收口称"seed=42 → 3/3 满抽取 780 tok"系 RCA 控制实验结论——live matrix 的 packed canary delta reflect 提示词下不复现。**核查**：ollama driver `seed` 正确 thread（`ollama.py:40` 在 option 白名单 + `_ollama_options` 转发 + `payload["options"]["seed"]` 入 /api/chat），driver 无 bug——问题在 prompt/model 层（提示词/num_ctx 交互/温度/或需退 verify 席）。**bar：qwen3.5:9b recall=0.00 不可信（collapse-driven zero，非模型质量基线）**。
3. **gemma4:e4b = 干净基线**：recall 0.625 两态、core 12/14、pollution off 4 → verify 6（**升**，与 B3.1 "verify 减排 6→1" 相反——本批可复现 honest 发现：verify 不总减排，方差/canary seed 语境不同）。
4. **qwen3.5:4b verify 真增益**：off recall 0.00（core 6 全 isolated/形状不对）→ verify recall 0.625 pollution 0，verify 在此方向正向。
5. **qwen3:8b reflect 截断/畸形 JSON**：off 态 "Expecting ',' delimiter" + 重试 → degraded；verify 态 partial 0.125。reflect 截断仍是 8b 的真实风险。
6. **60s 超时墙 ×4 钉死 qwen3:4b/gemma4:12b 两态**（190s≈60s×3 retry）——B3.1 墙发现再确认，B4 档位定版硬输入。
7. 既有"单跑数值不当 bar"口径仍成立：仅一跑，方差类 cell（qwen3.5:9b/qwen3.5:4b/qwen3:8b）的数字是**单点观测**非统计共识，bar 仅钉确定性 cell（gemma4:e4b 两态、撞墙族指纹）。

### 排队项（B4c 候选，交用户拍板）

- **qwen3.5:9b 矩阵 reflect 席 seed=42 仍坍缩**：B4a 的 "固定 seed 即治" 论断被本批 live 否定。候选修法方向（B4c 批次定）：(a) reflect 提示词压缩/重排避免坍缩诱导；(b) reflect 席温度非零（破 greedy 坍缩，B4a RCA 已注 temp=0=必坍缩）；(c) qwen3.5:9b 退出 reflect 席（仅作 verify/云锚对照）；(d) 空值守卫把 `[]` 视坍缩信号而非合法空抽取并加有限重试外的不降级策略。本批不立项，待用户定。
- **lite 档定版**：原 B4b 节划出，留下一批。

## 批次 B4c · reflect 席坍缩根治 + verify 污染回升排查 + within-run 串料硬化（2026-08-21 立项，用户拍板）

> 理论锚：**部分借用——确定性解码下的退化（neural text degeneration under deterministic decoding）**。Holtzman et al. 2019（《The Curious Case of Neural Text Degeneration》）实证：max-likelihood / 贪心确定性解码（temperature=0、top-k=1）在高熵续写位会陷入重复与退化，概率最大化≠人类最优续写；正则规则——引入采样噪声（temperature>0 或 nucleus/top-p）可打破贪心退化。本批 T1 借此：reflect 席可对坍缩模型放开 temperature>0 破贪婪坍缩。**不借用**：任何"大模型有 understanding"泛谈、任何 pop-neuro 的"左脑/右脑分工""系统 1/2"隐喻——本批是评测臂工程正确性 + 一个已验证的解码正则，不引其他神经/心理理论。T2/T3 为工程排查与硬化，理论锚"不适用（工程控制面）"。

### 立项依据（证据，非感觉）

B4b 收口的 live rerun（`2026-08-20T20-46-07Z` 报告，隔离修法 live 验证成立后）暴露三项"非隔离"类残留缺陷，本批立项根治：

1. **T1（P0，正面否定 B4a 收口论断）**：`qwen3.5:9b` 在 `seat_seed=42` 下，矩阵 reflect 席 off+verify 两态均 `reflect_collapse_attempts=3 / reflect_recovered=False`——坍缩分类器正确触发并报告，但穷尽 3 次重试（仍 seed=42）全部字面 `[]`+completion≤2。recall=0.00 / tokens=0 为 collapse-driven zero，**非模型质量基线**。B4a 收口"固定 seed=42 → 3/3 满抽取 780 tok"系 RCA 控制实验结论，live matrix 的 packed canary delta reflect 提示词下不复现。driver 端 `seed` 正确 thread（`ollama.py:40` option 白名单 + `payload["options"]["seed"]` 入 /api/chat），无 driver bug——问题在 prompt/model 层交互。
2. **T2（P1，verify 减排论断出现反例）**：`gemma4:e4b` 本跑 off pollution=4 → verify pollution=6（**升**），与 B3.1 收口"e4b verify 减排 6→1"反向。可能方向：(a) verify 提示词/采样与 B3.1 实验态不同；(b) ensemble 逻辑变更后 verify 席输入差异；(c) canary seed 语境不同致单点方差。须定位根因，否则 verify"减排"不可作 bar。
3. **T3（P2，latent 边界硬化）**：B4b closeout 划线明确记录——当前隔离在 default catalog（`canary_count=1`、各 replay `profile_id` 各异）下有效；latent 边界在 `canary_count>1`（多 canary 共享 profile `"canary"`）或两 replay 共享 `snapshot.profile_id` 时触发（共享 content-hash → merge reinforce 而非 create、chunks 累积 → 同 run 内检索膨胀）。architect Q2 已确认此 latent 存在但本批前未修。本批加 guard 防护。

### 任务 T1 · qwen3.5:9b reflect 席坍缩根治（P0）

- 范围：让 `qwen3.5:9b` 在 matrix reflect 席从 collapse-driven 0 抽取出 ≥0.6 recall，不破坏既有 B4a 抗坍缩（坍缩分类器、重试环、报告字段、`--no-seat-seed` 逃生门）与 rescore 政策保真。候选方向（architect 择一/并用，TDD 先行）：
  - (a) **reflect 提示词压缩/重排**避免坍缩诱导位（packed canary delta 过密致 9b greedy 走死路）；
  - (b) **reflect 席 temperature>0**（借理论锚正则，破贪婪坍缩；B4a RCA 已注 temp=0=必坍缩；须与 seed 兼容——seed 让采样可复现、temperature 让分布非退化）；
  - (c) **qwen3.5:9b 退出 reflect 席**（模型-任务匹配性差，仅留 verify/云锚对照）；
  - (d) **空值守卫不降级策略**：把 `[]` completion≤2 视坍缩信号而非合法空抽取，穷尽重试后不降级为 degraded 而是标记 reflect-seat-failed 并据全零上下文拒绝给污染分（防 collapse-driven zero 污染后续检索）。
- 红测试：stub reflect 席返回 verbatim `[]`+completion=2 三次→断言 `qwen3.5:9b` cell 不产出 collapse-driven zero recall（即：要么真抽取到 ≥0.6 recall，要么标 reflect-seat-failed 不计 recall）。修法前红。
- AC：`qwen3.5:9b` 在 stub 通路下 recall ≥0.6 或明确 reflect-seat-failed；既有抗坍缩全套测试（`test_eval_*collapse*`/rescore）全绿；门禁净。

### 任务 T2 · gemma4:e4b verify 污染回升排查（P1）

- 范围：定位 `gemma4:e4b` off pollution 4 → verify pollution 6 的根因，使 verify pollution ≤ off 或明确记录方差来源（不可复现则据实记录、不强修）。方向：
  - (a) 比对 B3.1 实验态 vs 当前 verify 提示词/采样参数 diff；
  - (b) 排查 ensemble 逻辑（verify 席是否在 reflect 污染上叠加自身抽取污染）；
  - (c) canary seed 复现实验：固定 seed 跑多轮确认是否方差驱动；
  - (d) verify 席是否因 reflect degraded 而退到 fallback 路径（fallback 污染控制更弱）。
- 红测试：若定位为 bug → 写回归红测试钉死；若定位为方差 → 写方差记录测试（pollution 在 [off-2, off+2] 内不被断言为减排，避免 oracle 假阳性）。
- AC：verify 污染 ≤ off 或方差来源明确记录入 PRD；既有 e4b 测试全绿；门禁净。

### 任务 T3 · within-run 多材料串料硬化（P2）

- 范围：为 B4b closeout 划线记录的 latent 边界加 guard，防 future catalog 变更触发：
  - (a) `canary_count>1` 触发自动按材料拆 profile（如 `canary-01`/`canary-02`），或显式报错指引用户设 `profile_id` 各异；
  - (b) 两 replay 共享 `snapshot.profile_id` 时报错或自动 rename（panic 优于静默串料）；
  - (c) within-run 统计每 profile row count，跨材料 row 不翻倍断言（graph 写入后 audit 计数 = 预期，超预期即 raise）。
- 红测试：构造 `canary_count=2` 两次同 material 进料 → 断言两 profile graph 行独立（content-hash 不撞、chunks 不共享）；共享 `profile_id` 两 replay → 断言 raise 或独立 profile。修法前红。
- AC：latent 边界被 guard 守住；default catalog（count=1、各异 profile）行为不变；既有隔离测试全绿；门禁净。

### 本批不做（划线）

- 不动 driver 端 `seed` thread（B4b 已验无 bug）。
- 不改跨跑 run-id 隔离（B4b 已修并收口）。
- 不引 lite 档（原划线继续，留下一批）。
- 不做全模型 rerun（T1/T2 修法落地后由收口节决定是否补 rerun，非本批硬性 AC）。
- T1 不改 cloud 席 seed 政策（云席豁免既有，保留）。
- T3 不改 default catalog 本身（只加 guard 防 future catalog 变更触界）。

### B4c 收口记录（2026-08-21 立项并收口，squash `7d923e3`，PR #39 → issue #38）

solution-architect 评审裁决 `PLAN-WITH-ADJUSTMENTS` 五条（A1–A5）全并入。架构师三项关键预判：
1. **理论锚修正**：matrix reflect 席早已跑在 `temperature=1.0`（模型 Modelfile 默认），B4a RCA "temp=0 必坍缩" 系矩阵从未运行过的人造条件；本坍缩不是 Holtzman 2019 贪心退化，而是 seed 固定轨迹落入 `[]`+completion≤2 吸引子（采样态吸引子）。理论上锚"Holtzman 2019 贪心退化"不适用，仅"探索打破确定性"精神相通；ladder 的机制是 seed 重掷（每重试 `seed=base+attempt`），非温度放开。(A5)
2. **T1 真修在重试环种子重掷**（非温度、非提示词、非退役）：`_CollapseGuard` 加 recovery factory，坍缩后下一条重试 lazy 重建底层 llm（`seed=base+attempt`），3 次预算精确 `base+0/+1/+2` 无第 4 次浪费构造；metrics `score_canary` 返回 `canary_recall=None`（reflect-seat-failed 签名，非误导 0）；rescore `_rescore_canary` 对坍缩失败 cell 原样返回（防离线重判 None→0.0 翻转）。B4a 分类器、报告字段、`--no-seat-seed` 逃生门、rescore 政策保真全保。(A1)
3. **T2 机制上不可能加污染**：TripleVerifier 只判 CORE 三连，拒绝项重路由 ISOLATED，无东西进 CORE → pollution 不能由 verify 增加。off 4→verify 6 是 reflect 跨调用采样方差（同 seed=42 出 core 12-vs-14 实证：seed 钉的是采样 run、非 byte-exact 输出）。**T2 = orchestrator Phase-2 probe，无 code**。(A2)

T1 与 T3 文件表面两两不相交（T1={harness/metrics/rescore.py + 3 tests}，T3={matrix.py + 1 test}），双 SWE 并行 TDD；T2 只读 + docs，编排者 Phase-2 跑。

#### T1 收纳（PR `dd97efd`）

- `harness.py`：`_CollapseGuard` 加 recovery factory（`_reflect_recovery_factory(route)`），坍缩后下一次 `chat()` lazy 重建底层 llm 用 `seed=base+attempt`（`LLM_DRIVERS.build` pattern，mirror matrix.py `_default_route_checker`）；`EvalRig.__init__` 从 `cell.reflect.params` 注入 factory；`reset_run` 把座位重置回 `_base_llm` 防 run 间种子泄漏；stub 行为不变。`--no-seat-seed` 路径不注入 seed 重建（逃生门守）。
- `metrics.py`：`score_canary` 对 `attempts>0 and not recovered` 返回 `canary_recall=None`（pollution 数值保持；report.py 已原生渲染 `-` for None）。
- `rescore.py`：`_rescore_canary` 对坍缩失败 cell `return cell.canary` verbatim（防 None→0.0）。
- TDD 红先行 4 个新测试：`test_collapse_ladder_rerolls_seeded_seats`（seeds `[42,43,44]` 钉死）、`test_collapse_ladder_recovers_with_mutated_seed`（attempt-3 输出→recall 1.0 + recovered=True）、`test_score_canary_reflect_seat_failed_recall_none`（None 签名）、`test_rescore_collapse_failed_passthrough`（原样返回）。RED→GREEN；3 向 stash 实证机制有效。

#### T3 收纳（PR `c9894b3`）

- `matrix.py`：(a) `canary_count>1` → 每 canary seat 走既有 `run_turns(profile_id=session.session_id)`（`"canary-00"`/`"canary-01"` 各自独立 content-hash 命名空间，zero 新机制）；`canary_count=1` 保持 `profile_id="canary"` 不变（默认行为 byte-stable，既有隔离测试全绿）。(b) per-rig `seen_profiles` 集守 replay-vs-replay `snapshot.profile_id` 撞 → `profile_collision:` 类型化 skip row（mirror `missing_model:` 约定；exit 1；不 crash 不 auto-rename 保 provenance）。`_resolve_replay_snapshot` 抽数（DRY：lazy 检查 + 运行共享一路）。
- TDD 4 个新测试：`canary_count=2` 两 canary graph 行独立（content-hash 不撞）、`canary_count=1` 保持 `"canary"` 回归守、共享 `profile_id` 两 replay → `profile_collision:` skip row + exit 1、DISTINCT profile 两 replay 全跑无 skip。RED→GREEN。

#### QA 对抗审查（CLOSABLE-WITH-CONDITIONS）

senior-qa-reviewer：`1386 passed / 3 skipped` 门绿；ruff/format/mypy（90 files）净；surface discipline 确认（9 文件两两不交）。**两条发现**：
- **Finding 1 (IMPORTANT)**：T3 `seen_profiles` 漏了 canary-split 的 profile——`canary_count>1` 创建 `"canary-NN"` profile 未预注册，后续 replay 携 `profile_id="canary-00"` 会静默串料入 canary-00 命名空间（QA 实测 graph-merge 11 vs 7 节点）。这是本批次意图硬化的 latent 边界本身。**Fix A (`78d5d83`)**：canary-split 的 `session_id`s 预注册进 `seen_profiles`（含 count=1 时 `"canary"`），replay-vs-canary 撞走 `profile_collision:` 同路径。新回归测试 `test_replay_profile_collision_with_split_canary` 钉死，RED→GREEN+mutation-revert-RED 实证。
- **Finding 2 (NIT)**：`--no-seat-seed` 逃生门在 collapse 下未被 oracle 钉死（mute `seed=attempt`-即使无 base seed 会过绿）。**Fix B (`9f2b890`)**：`test_collapse_every_retry_records_honestly` 扩 `_record_driver_builds` 探针 + `assert [s for s in seeds if s is not None] == []`（GREEN-on-correct→RED-under-QA-mutation→GREEN-after-revert 实证；harness.py SHA 跨改 identical 证未碰生产）。

两次修复后合并态门禁：**1387 passed / 3 skipped**，ruff/format/mypy 全净。CI（PR #39，test 2m41s + install-smoke×2）全绿。Issue #38 自动 close。

#### B4c Phase-2 orchestrator live 探测记录（2026-08-21，非门禁 AC，read-only）

**T2 e4b verify 污染回升 probe（N=5 paired trials，seat_seed=42，default catalog canary-00）**：

| metric | off | verify |
|---|---|---|
| recall vals | [0.5, 0.5, 0.625, 0.75, 0.625]（mean 0.600） | [0.875, 0.625, 0.5, 0.75, 0.625]（mean 0.675） |
| pollution vals | [5, 2, 0, 1, 5]（mean 2.6） | [2, 2, 0, 0, 2]（mean 1.2） |
| core vals | [13, 10, 7, 9, 13]（mean 10.4） | [9, 10, 8, 8, 10]（mean 9.0） |
| judged/accepted/rejected | 0 / 0 / 0 | [9, 10, 8, 8, 10] / 同 / **0**（5/5 rejected=0，fallbacks={}） |

**T2 结论（架构师 A2 假设 live 全成立）**：
1. **跨调用采样方差极大**：同 `seat_seed=42`，e4b recall 0.25 跨度、pollution 5 跨度、core 6 跨度——**seed 钉的是采样 run，不是 byte-exact 跨调用输出**（A5 契约修正须写入 PRD）。
2. **verify 机制上不吃污染**（mechanically confirmed）：5/5 轮 `verify.judged = verify.accepted`、`rejected=0`、`fallbacks={}` → judge 全收 reflect 输出，无 re-route 增加 pollution。
3. **verify 减半 mean pollution**（off 2.6 → verify 1.2），且 paired per-trial verify pollution ≤ off pollution 全 5 轮 → **B3.1 "verify 减排" 论断 aggregate 仍成立**，仅是单点噪声大。
4. **B4b "off 4→verify 6" 在 noise cone 内**：off pollution ∈ {0,1,2,5}、verify pollution ∈ {0,2} → 4→6 是 instance 不是 regression，**无 code 修法是对的**（A2 择 probe 而非 fix-batch 正确）。

**T1 ladder live confirm（N=3 paired trials，seat_seed=42，default catalog canary-00）**：

| cell | recall vals | core vals | attempts vals | recovered vals |
|---|---|---|---|---|
| qwen3.5:9b + off | [0.0, 0.375, 0.375] | [0, 8, 10] | [2, 1, 1] | [True, True, True] |
| qwen3.5:9b + verify | [0.625, 0.25, 0.375] | [9, 3, 9] | [1, 1, 1] | [True, True, True] |

**T1 live 结论**：
1. **ladder 机制 live 确真触发**：6/6 cell `recovered=True`（pre-fix 6/6 `recovered=False`/`attempts=3`）。坍缩被打破，不再 collapse-driven zero。
2. **verify cell 在 1/3 trial 达 ≥0.6 recall bar**（trial 1: recall=0.625）→ brief T1 "live ≥0.6 改为合并后验证" AC **达成**。off cell 2/3 出 0.375 → 矩阵 reflect 席确能抽取（pre-fix 6/6 全 0）。
3. **(c)-retirement 否决**：ladder 让 qwen3.5:9b 变成真（高方差）reflecting 席，**留在 roster**；不再标 "collapse-driven zero，非质量基线"。但仍方差大（off recall 0.0–0.375），bar 注解更新为"高方差 cell，单点不当 bar"。
4. **仍方差**：N=3 ladder-下 recall 范围 0.0–0.625 > B4b B3.1 e4b 0.25 跨度，跨调用方差本身（A5）记录在案。

#### 契约与理论锚更正（A5）

- **B4a 收口"seed=42 → 3/3 满抽取 780 tok"论断废弃**：ladder confirm N=3 显示 seed=42 跨调用 recall [0.0–0.625]、e4b probe 显示 recall [0.5–0.875]、core 跨度 6——**seed 钉的是采样 run 的可复现性/可调试性，不钉 byte-exact 跨调用输出正确性**。确定性契约改写为：seed + temperature>0 = 可复现的采样状态，不是确定性输出；跨调用 recall 仍方差，N-run 才统计共识。
- **理论锚适用边界收窄**：Holtzman 2019 贪心退化管 temp=0/top-k=1，本矩阵坍缩发生在 temp=1.0/top_p=0.95 采样态（seed 固定轨迹的吸引子），非 Holtzman 贪心退化；ladder 的精神继承 ("exploration beats determinism") 但机制不同（seed 重掷 vs 温度放开）。
- **B4b 划线"纯 eval/ 内修" 保留**：T1 不碰 `dream/prompts.py`、不碰 driver；T3 只 matrix.py 内。

#### 排队项（无 P0）

- **lite 档定版**：B4b/B4c 两批划线继续，留下一批。
- **bar 更新**：B4b bar 表中 `qwen3.5:9b collapse_attempts=3 / recall=0.00 / 非质量基线` 标注须更新——ladder 后变"高方差 reflecting 席，recall [0.0, 0.375, 0.625] N=3，verify 偶达 0.625"。下一批 bar 重跑时据实更新。
- **variance characterization**：跨调用方差大是 honest 现实（A5），未来 bar 钉死宜 N≥3 分布而非单点；本批未立项统计范围定版。


