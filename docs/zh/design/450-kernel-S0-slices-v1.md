# 450-S0 · 内核 S0 工作分片（v1 · PROPOSAL）

> 状态：**PROPOSAL / DESIGN ONLY / 无门权**。配套契约文档：`450-kernel-S0-recall-contract-v1.md`（下称「契约文」）。归属 issue #457 路径 A。
> 本轮**只写两份 450-* 文档**；下文所有 `src/`、`tests/`、`docs/zh/eval/` 路径均为**未来工程提案的文件白名单**，须 owner start + 设计门 + 独立设计 QA CLOSABLE 后方可落盘，本轮一律不创建、不修改。
> 本片**零产品检索行为变更**：所有工作单元只新增离线测量面（additive eval surface），不触碰 `retrieve/`、`daemon/memory.py` 的运行时行为，不改配置、不绑端口、不写 `~/.mnemoseed-local`。
> 隔离纪律：`MNEMOSEED_LOCAL_HOME` 临时目录 + 备用 loopback 端口（非 7788/4096）；先复制 `~/.mnemoseed-local` 到临时目录再分析（`mnemoseed-local/AGENTS.md:76-79`）。

---

## 0. 依赖总览与范围声明

- 目标：把契约文的「captured implies retrievable」测量面从设计落到可运行回归工具，且全程可证伪、反自指、产品绑定。
- 依赖链：`W0（owner 冻结集/阈值）` then `W1（语料副本+隔离）` then `W2（产品绑定回放）` then `W3（度量+契约判分）` then `W4（runner+报告+文档）` then `W5（独立设计 QA + 冻结）`。
- W0 是 **owner 人工决策**，非工程；W1/W2/W3/W4 是工程（各自文件面不相交）；W5 是独立 QA 席。
- 理论锚与反自指纪律全部继承契约文 §2/§4.4，不在本文重述；本文只管工作拆分与门。
---

## 1. 工作单元（work units）

### W0 · owner 冻结集与阈值 ratify（非工程，owner 席位）

- **目标**：交付冻结的 `queries-v1.json` + `manifest`（含整集 SHA-256），并 ratify 契约文 §6.2 的 B1-B5 数值、N、离散界、holdout 比例；确认 D1 的种子 miss 与其 `ingestion_verified`。
- **依赖**：契约文本身（本文与契约文经独立 QA CLOSABLE 后冻结）作为输入。
- **文件白名单（未来产物）**：`docs/zh/eval/realcorpus-recall/queries-v1.json`、`queries-v1.manifest.json`、`README.md`（owner 署名区）。
- **验收门**：每条 query 含契约文 §4.2 全部字段；target_chunk_id 在语料副本中可直接读到且 `ingestion_verified` 有结论；miss_seed 唯一且为 owner 确认的活体样本；整集哈希写入冻结记录（append-only）。
- **owner 决策点**：D1（种子与 query 全集）、D2（阈值/N/离散界/holdout）。

### W1 · 真语料副本与隔离脚手架（工程席，可独立起）

- **目标**：把 `~/.mnemoseed-local` 复制到临时目录（含全量 `chunks.lance`），产出 `corpus_manifest.sha256`；封装临时 `MNEMOSEED_LOCAL_HOME` + 备用端口的 daemon 启动/停止与 `INVALID_RUN` 判定。
- **测量中立支撑**：保留不可变 pristine 基线，提供「每 run 前原子恢复可写状态」原语（`meta.db`+WAL/SHM、vector weight/state；activation 为前向项——短期激活默认关闭且进程内易失，`activation.py:49,70`，当前无持久化激活可重置），供 W3 每 run 调用（契约文 §5.1/§5.4；`memory.py:792`）。
- **依赖**：W0 的冻结哈希作为钉入参数（仅需 manifest 结构，不依赖 query 文本细节）。
- **文件白名单**：`src/mnemoseed_local/eval/realcorpus_recall/{__init__,corpus,isolation,manifest}.py`；`tests/test_eval_realcorpus_recall/test_corpus_isolation.py`。
- **验收门**：副本四要素钉（manifest/config/embedder 版本/代码 revision）齐全；起停全程不写 live home、不绑 7788/4096（以断言固化）；哈希或副本不完整时抛 `INVALID_RUN` 而非 PASS/FAIL；复制空间/耗时被量测并记录（R-3 解除）；pristine 基线可在每 run 前原子恢复且恢复后与基线逐字节一致（断言固化）。
- **owner 决策点**：若全量副本不可行，是否接受代表性冻结切片并接受覆盖局限声明（D3 的子项）。

### W2 · 产品绑定回放适配器（工程席，依赖 W1 接口）

- **目标**：封装对真实 `MemoryService.recall`（`memory.py:748-799`）或 `POST /memory/recall`（`memory.py:2879-2890`）的调用，按 `RecallRequest`（`memory.py:189-199`）注入 `{profile_id, query, top_k, budget, session_id=None}`（关闭激活写回与打分，`memory.py:769,793-794`），从产品返回的 `payload.entries / dropped_count / tokens_used / coverage`（`memory.py:838-847`）提取观测，不自造打分器。
- **依赖**：W1 提供的隔离 daemon 句柄与 manifest 钉。
- **文件白名单**：`src/mnemoseed_local/eval/realcorpus_recall/replay.py`；`tests/test_eval_realcorpus_recall/test_replay_product_binding.py`。
- **验收门**：
  - 正向：对一条已知 query，回放输出含 served entries 与 token 字段，且经 `CueExtractor.extract`（`cues.py:906`）与 `_recall_view`（`memory.py:766`）的真实路径（可借 observability 计数佐证）。
  - **绑定 killer（必须 KILLED）**：把打分源变异为自算 cue 重合的伪路径时，回放适配器测试必须 RED；证明不是 #206 式自报仪器（`inv1:272-282`）。
  - 不变量：不 import 任何 synthetic embedder / stub dream（#391 rig 元素，`inv1:88-100`）。
### W3 · 度量、契约判分与反 gaming（工程席，依赖 W0/W1/W2）

- **目标**：按契约文 §5.3 计算 recall@K / miss_seed / honest-empty / 噪声准入 / rank / token / p95；按 §6.3 做 N 次独立冷启动、取中位数、离散界判定，产出 PASS/FAIL/UNKNOWN。
- **依赖**：W0 的 query 集 + 阈值；W2 的产品输出。
- **文件白名单**：`src/mnemoseed_local/eval/realcorpus_recall/{metrics,contract,report}.py`；`tests/test_eval_realcorpus_recall/test_contract_scoring.py`。
- **验收门**：
  - `ingestion_verified=false` 样本不进 B2 分母、单列 diagnostic（区分没 ingest 与 cue 不出来，`first-principles-kernel-gap.md:209`）。
  - `INVALID_RUN` 或离散超界一律 UNKNOWN，永不写成 PASS（无声不等于通过，`inv2:9-15`）。
  - holdout 与 seen/unseen 记账：bar 只在 holdout 判定；seen-only 子集不得触发 PASS（teaching-to-test 防线，`competitor-memory-mechanisms.md:58`）。
  - B1 单独 must-pass；B2/B3 全过才 PASS（契约文 §6.3）。
   - **测量中立性（必须 KILLED 的变异）**：同一 query 在 N 次独立冷启动中重复回放，recall@8 / miss_seed / honest-empty / rank / `tokens_used` 跨 run 漂移 = 0（p95 不作漂移判据）；变异 A = 跳过每-run pristine 重置、复用上一条 run 写后副本（`memory.py:792` → `decay/reinforce.py:120,126-132` 的 decay 回弹累积），必须 RED（现行唯一可观测的 run 间污染通道）。传非空 `session_id` 不是 killer：短期激活默认关闭且进程内易失（`activation.py:49,70`），不改变分数也不累积；原第二 killer 声明已删除，`session_id=None` 仅为前向正确性/稳健性选择。binding killer（W2）与本条正交，仍独立持有。
   - 每 run 前调用 W1 的 pristine 恢复原语并在报告中记录该 run 的基线哈希；基线不一致判 `INVALID_RUN`。

### W4 · 端到端 runner、报告与 as-is 指针（工程席，依赖 W3）

- **目标**：提供可复跑入口（独立 `python -m` eval 面，不进 pytest 慢门禁，参照 `08:23`），产出追加式 JSON 报告；在 `docs/zh/eval/realcorpus-recall/README.md` 记录四要素钉、阈值来源与运行手册；按 owner D3 决定是否给 `08-eval-harness.md:24` 加一条 as-is 指针。
- **依赖**：W3 判分器；D3 的 gate 形态裁决。
- **文件白名单**：`src/mnemoseed_local/eval/realcorpus_recall/{__main__,cli}.py`；`docs/zh/eval/realcorpus-recall/README.md`；（可选，仅 D3 授权时）`docs/zh/design/08-eval-harness.md` 追加一行 as-is 指针，不改其原结论。
- **验收门**：同 manifest/同 query/同阈值两次跑输出逐字节可比（除 UTC 时间戳与 run id）；报告含逐 query 明细 + holdout/seen 标记 + PASS/FAIL/UNKNOWN；不新增产品 CLI verb（`08:9` 边界）；不改 `src/mnemoseed_local/eval/` 现有 #391 子命令（物理分离）。

### W5 · 独立设计 QA 与冻结（独立 senior-qa-reviewer 席）

- **目标**：对本设计两文档做对抗性设计审查；CLOSABLE（无 BLOCKER）后才允许冻结并交 owner 决策菜单。
- **依赖**：W0-W4 设计全部就绪（QA 审的是设计，不是已实现代码）。
- **文件白名单**：审查判词落 `org/workspace/kernel-first-principles-20261122/`（QA 席产物，不属本席写面）；本席不写任何产品文件。
- **验收门**：逐条复核契约文 §10 的 ISSUE-1..5 是否被设计文本或 owner 裁决处置；复核「零产品行为变更」「拒用 #391」「产品绑定」「holdout 反 gaming」四条硬约束；复核 file:line 证据（尤其 §3.2 的 `hybrid.py:39-40` 修正）。
---

## 2. 文件白名单汇总与当前轮写面

- **本轮（DESIGN ONLY）写面**：仅 `docs/zh/design/450-kernel-S0-recall-contract-v1.md` 与 `docs/zh/design/450-kernel-S0-slices-v1.md`。与 S1（451-*）、S3（452-*）文件面互斥（`design-batch-brief.md:91-93`）。
- **未来工程提案写面（需 owner start，本轮不创建）**：
  - `src/mnemoseed_local/eval/realcorpus_recall/**`（新包，additive；不改 `src/mnemoseed_local/retrieve/**`、`daemon/memory.py`、`eval/` 现有模块）
  - `tests/test_eval_realcorpus_recall/**`（新测试面，避免与既有 eval 测试的 oracle 碰撞）
  - `docs/zh/eval/realcorpus-recall/**`（新文档面）
  - `docs/zh/design/08-eval-harness.md`：仅 D3 授权时追加一行 as-is 指针，不改原结论
- **禁止写面**：`~/.mnemoseed-local`、`src/mnemoseed_local/retrieve/**`、`src/mnemoseed_local/daemon/**`、`src/mnemoseed_local/eval/{recall_matrix,recall_harness,...}.py`、任何 config、任何产品 CLI verb。

## 3. 并行性 / 冲突检查（供 main 编排）

- W1 与 W0 可并行准备（W1 只需 manifest 结构，不依赖 query 文本；但 W1 的库落盘须等 owner start）。
- W2 依赖 W1 的隔离句柄接口；W3 依赖 W0 阈值与 W2 输出；W4 依赖 W3。顺序明确。
- 文件面不相交：W1=`{corpus,isolation,manifest}.py`+`test_corpus_isolation.py`；W2=`replay.py`+`test_replay_product_binding.py`；W3=`{metrics,contract,report}.py`+`test_contract_scoring.py`；W4=`{__main__,cli}.py`+README。无共享文件、无测试 oracle 碰撞；满足并行化纪律（`AGENTS.md:47-53`）。
---

## 4. Owner 决策点映射（与契约文 §11 对齐）

| 决策 | 阻塞的工作单元 | 若不裁决的默认动作 |
|---|---|---|
| D1 种子 miss 与 query 全集 | W0（冻结集） | 不冻结、不激活 gate；W1-W4 可写工具但不可判 PASS |
| D2 阈值/N/离散界/holdout | W0 + W3 判分 | B1-B5 保持「提议」，W3 输出标 UNKNOWN |
| D3 硬发船门 + 全量副本成本 + 08 指针 | W1（副本规模）、W4（gate 形态/指针） | 副本用代表性切片并显式记录覆盖局限；不写 08 指针 |

## 5. 硬约束自检（每片都须持有）

- 零产品检索行为变更：所有 `src/` 新增限于 `eval/realcorpus_recall/`，不改运行时行为。
- 拒用 #391 synthetic rig 作 oracle：不得 import synthetic embedder / stub dream；不复用 `TARGET_BARS`（`recall_matrix.py:46-52`）作质量门。
- 产品绑定：回放必须调用 `MemoryService.recall`，绑定 killer 必须 KILLED（W2）。
- 反 self-reference / teaching-to-test：owner 标签 + 副本真值直读 + holdout 判定 + seen/unseen 记账（W0/W3）。
- 量纲分离：recall bar 与 #206 裁决参数、与延迟/token 护栏分开（`inv1:86`；契约文 §5.3 注）。
- 失败可见 / fail-open：`INVALID_RUN` 与 UNKNOWN 永不写成 PASS；报告只增不减。
- 测量中立性：每 run 从 pristine 基线重置可写状态（激活为前向项，当前默认关闭且易失，`activation.py:49,70`）+ `session_id=None`（前向正确性/稳健性选择，当前不改变分数）；「重复回放无漂移」回归 + 唯一 killer 变异 A 必须 KILLED（W1/W2/W3）。

---

*完。本文为 PROPOSAL（无门权）；当前轮只写两份 450-* 文档；零产品字节/commit/push/端口/live 改动。*
