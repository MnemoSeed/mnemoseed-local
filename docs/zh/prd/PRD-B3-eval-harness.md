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

