# 450 · 内核 S0：真实语料 recall 质量契约（v1 · PROPOSAL）

> 状态：**PROPOSAL / DESIGN ONLY / 无门权**。归属 issue #457 路径 A（`org/_state.md:15`）。阶段纪律见 `org/workspace/kernel-first-principles-20261122/design-batch-brief.md:6-11`。
> 本片**零产品检索行为变更、零模型调用进 plumbing**：只新增离线测量面（additive eval surface）。不改 `retrieve/`、`daemon/memory.py` 行为，不改配置/端口，不写 `~/.mnemoseed-local`。
> 本文是 `docs/zh/design/08-eval-harness.md:24` 所推迟的「retrieval recall 评测（另行立项）」的**单独授权后续**，与 #391 synthetic rig 物理分离、量纲分离。
> 诚实标记：产品行为断言均带 file:line；未取证项标 `[未验证]` / `[needs verification]`。

---

## 0. 结论（≤5 行）与裁决

1. 内核 gap review 的 **#1 BLOCKER**（`first-principles-kernel-gap.md:171-174`）是「captured ≈ retrievable 是信条、不是带测量的不变量」；本片把它落为**可证伪的真实语料 recall 回归契约**。
2. 契约三件套：**owner 冻结的自然查询集**（含 ingest 了却自然措辞召不回的真实 miss）、**回放真实 daemon recall 路径的回归 harness**（真语料副本 + 真 embedder + 真 cue/融合/装配）、**预注册质量 bar**（阈值 + 测量法，未达则内核不可发船）。
3. **拒用 #391 synthetic rig 作质量 oracle**：其 `TARGET_BARS` 是 recall@5/precision@5 等召回量纲（`recall_matrix.py:46-52`），无真语料输入端口（`inv1:88-100`），与 #206 裁决参数不同量纲（INV-1 量纲错位，`inv1:86`）。
4. **反自指靠产品绑定**：harness 必须 import 并调用产品 `MemoryService.recall`（`memory.py:748`），观测取自产品返回的 served entries / coverage / dropped_count / tokens_used（`memory.py:838-847`），标签由 owner 人工给出、真值由语料副本直接核验；不得像 #206 retention observer 只 import argparse/json 而自报结论（`inv1:272-282`）。
5. 本设计自身裁决：**SHIP-WITH-ADJUSTMENTS**（见 §10），作为地基可进入独立设计 QA，但 gate 激活前须解决 owner 决策 D1-D3 与一处证据定位修正。
---

## 1. 假设 / 前提

- A1：三条 investigation 的 measured 项已被 main 验收（`org/_state.md:7`）；`reconcile_nominations=0`（`inv1:218,240`）、graph 37 节点/0 边（`inv3:28`）、`ingest_applied=84,245`（`inv3:154`）、无 usage 字段（`inv2:99`）为事实。
- A2：owner 已裁定路径 A（S0 先行、均不改产品行为，`org/_state.md:15`）；本片是设计工件，工程派发需 owner start + 设计门 + 独立 QA CLOSABLE（`org/PROTOCOL.md:176-190`）。
- A3：理论锚固定不可重议；本片只检查机制是否忠实实现锚点（`00:140-145`；`org/PROTOCOL.md` 理论锚纪律）。
- A4：评测隔离纪律为准：临时 `MNEMOSEED_LOCAL_HOME` + 备用端口，永不 7788/4096，永不触碰 `~/.mnemoseed-local`，产物只落临时目录与本仓新 eval 面（`mnemoseed-local/AGENTS.md:76-79`）。
- A5：真语料副本可完整复制（含 `chunks.lance`）；规模成本见 §9 风险 R-3，`[needs verification]`。
- A6：owner 活体 miss 的具体检索轨迹本席未复现（`first-principles-kernel-gap.md:209` 标 `[未验证]`）；`recall-weakness-diagnosis-20261120.md:7-17` 提供可考证候选 miss（自然措辞「把一部分 codex 的对话记录导入 mnemoseed 记忆里」召不回执行态记忆，用存储词表「rollout/账本/settle」可命 0.95-0.99）。**确切 query 文本与目标 chunk 须 owner 冻结时确认**（D1）。

---

## 2. 理论锚（经验规律与实现机制严格分离）

### 2.1 借用的已验证规律

| 锚 | 来源 | 已验证规律 | 本片设计规则 |
|---|---|---|---|
| R47 | Tulving & Pearlstone 1966，`REFERENCES.md:99` | **可用 != 可及**：在册但暂时取不回；给合适线索可达性恢复 | 成功判据从「已 ingest（availability）」改为「自然线索下可 surface（accessibility）」；availability 只作前置核验 |
| R4 | Tulving & Thomson 1973，`REFERENCES.md:20` | **编码特异性**：检索取决于编码期与提取期 cue 的重合 | 查询集刻意用与存储词表**不同**的自然措辞（owner 原话），miss 样本即 cue 失配活样本；不得用存储词表反推 query |
| R29 | Wixted 2004，`REFERENCES.md:60` | 遗忘主因是**干扰**而非时间 | 查询集纳入近重复/干扰样本（`recall-weakness-diagnosis:19-24`：4 个逐字重复 chunk 吃掉 800 token 预算的 692）；不把预算挤占误记为「没记」 |
机制分离声明：上表是**被借的理论**；harness、chunk-id 命中判定、bar 数值是**实现机制**，不冒充理论，也不从理论推出具体数字。

### 2.2 明确不借（not borrowed）

- **厂商 benchmark 数字**：mempalace R@5 96.6%/hybrid 98.4%、Mem0 LoCoMo 92.5、各家 CLAIMED（`competitor-memory-mechanisms.md:58,111,202-211`）。量纲不同、不可横比（同文 `:236`），mempalace 自曝末段 0.6% 是 teaching-to-test（`:58`）。
- **#391 synthetic recall@5/precision@5 bar 作为质量 oracle**（INV-1 量纲错位，`inv1:86,260`）。
- **自生成标签 / teaching-to-test**：标签不得由被测系统产出或按已见查询调参（§4.4）。
- **流行神经神话**：「只用了 10% 大脑」「记忆存在单一 engram」「置信度=真实」「召回越多越好」：无实证门槛，不入册。
- **「协议强制先查」当作质量证据**：mempalace 用 protocol 逼宿主查询（`competitor-memory-mechanisms.md:65-66`），是行为规训而非被动质量测量；本片测产品真实路径。

---

## 3. 问题陈述与绑定约束

### 3.1 绑定约束（binding constraint）

内核把「verbatim 已全量入库」当作「一定可找回」的充分条件（`00:56,59`；`03:217`），与自身锚点 R47/R4 矛盾（`00:161`；`REFERENCES.md:20,99`）。可召回性从未成为 head invariant；recall 的 accessibility 失败不可检测，任何局部补丁（#448 影子、`beta/top_k/budget` 调参）都只能在合成代理量上变绿（`first-principles-kernel-gap.md:16,129-132`）。
### 3.2 现状证据链（逐条 file:line）

- 存储存在：`ingest_applied=84,245`（`inv3:154`）；真 miss 样本在库且高分却自然措辞召不回（`recall-weakness-diagnosis:9-17`）。
- 检索路径：`CueExtractor.extract` 确定性零 LLM（`cues.py:1-12,906`）；query cue 与 chunk cue 的实体重合按 **casefold** 计算（`hybrid.py:522-531`），实体/工具/上下文以 0.6/0.25/0.15 进 beta（`hybrid.py:534-538`）；图轨靠 `NodeFilter(profile_id, entities)` 取种子（`hybrid.py:383-393`），真语料 graph 为 0 边（`inv3:28`），故实际扛召回的是向量轨 + beta + 装配门。
- #448 是读侧词法影子视图：`daemon/memory.py:766` 把 `self._recall_view` 传给 retriever；`readview.py:1-17,71-95` 用 `lexical_shadow_tokens`（`cues.py:1035-1054`）只丰富读侧 stamp，fail-closed（`readview.py:79-85`）。它是表示层补丁，不解决任意措辞/跨语言（`first-principles-kernel-gap.md:69`）。
- 装配预算：`Assembler` 默认 `top_k=8`、`budget_tokens=800`（`retrieve/assemble.py:82-83`），预算内逐个收盘（`assemble.py:402-436`）；重复 chunk 未在预算前折叠的证据见 `recall-weakness-diagnosis:19-24`。
- 评测面缺口：`08:24` 明写不做 retrieval recall 评测；#391 rig 用 synthetic embedder + stub dream、无语料端口（`inv1:88-100`）。
> **证据定位修正（IMPORTANT，见 §10 ISSUE-2）**：batch brief 与 `first-principles-kernel-gap.md:40` 把「实体预过滤 exact-match」钉在 `hybrid.py:39-40`；本席逐字核对，当前源码 `:39-42` 是 **docstring**（述图候选只带 beta 的 entity 分量），并非预过滤代码。真实机制分散在：beta 的 `_fold/_ratio`（`hybrid.py:522-531`，大小写不敏感）、图种子 `NodeFilter(..., entities=entities)`（`hybrid.py:390`，其存储 driver 精确匹配语义 `[needs verification]`）、docstring 明言「stored entity cues never exclude a chunk from the vector pool」（`hybrid.py:7,41-42`）。设计不得沿用不精确的 `:39-40` 断言；机制根因精确归因须工程期取证。

---

## 4. 冻结自然查询集（owner-authored, frozen, non-self-referential）

### 4.1 来源（queries 从哪来）

三类，均由 **owner 人工署名**，禁止由模型生成或由检索输出反推：

1. **活体 miss 样本（must-pass 种子）**：owner 报告的「ingest 了却自然措辞召不回」对话。当前可考证候选：`recall-weakness-diagnosis:7-17`（自然中文「把一部分 codex 的对话记录导入 mnemoseed 记忆里」召不回，存储词表「全量 import / rollout / 账本 / settle」可命 0.95-0.99）。**owner 须确认它是 brief 所指的那条 zcode miss，或替换为确切样本（D1）**。
2. **真语料已知事实（positive set）**：owner 从语料副本指认「这条我确实说过、应能被某自然问法召回」的 chunk（以 chunk_id + 内容哈希标识），覆盖 preference / decision / habit / state 类，含 EN/ZH 对照问法（针对 `recall-weakness-diagnosis:15` 英文更弱的发现）。
3. **诚实空负样本（honest-empty set）**：owner 认定语料中**确无**对应记忆的自然问法（如确定不存在的主题），用于量测「无匹配不得填充」，防「永远返回点什么」作弊。
### 4.2 条目 schema（每条 query 必须写全）

```
{
  "qid": "Q-001",
  "text": "<owner 逐字自然语言，不做词表替换>",
  "lang": "zh|en",
  "kind": "miss_seed|positive|honest_empty",
  "target_chunk_ids": ["<chunk_id>"],
  "target_content_sha256": "<从语料副本直接读取的正文哈希>",
  "ingestion_verified": true|false,
  "rationale": "<owner 一句话：为何应被召回/应为空>",
  "author": "owner",
  "date": "YYYY-MM-DD",
  "holdout": true|false,
  "frozen_sha256": "<整集冻结哈希内的本条指纹>"
}
```

### 4.3 冻结机制（how frozen）

- 集合落为新 eval 面内不可变文件 `docs/zh/eval/realcorpus-recall/queries-v1.json`（未来工程产物），带整集 SHA-256 与 manifest；冻结后只允许**追加新版本** `queries-v2.json`，不允许就地改 v1。
- 冻结哈希经 owner 裁决后写入冻结记录（append-only），harness 启动时校验：哈希不符则判 `INVALID_RUN` 并停止，不得降级为「用当前文件继续跑」。
- 每条 `target_chunk_id` 以**语料副本**为准，不用 live home 的易变 id；副本再以 manifest 内容哈希钉住（§5.1）。
### 4.4 反自指（避免自证式仪器）

| 自指风险 | 本集对策 |
|---|---|
| 标签由被测系统自生成（#206 retention observer 自报 retained/repair/protected，`inv1:272-282`） | 标签 = owner 人工；真值 chunk 由语料副本**直接读**核验，不经过 recall |
| 把「已 ingest」当成「应能召回」 | `ingestion_verified` 独立核验；契约分母只含已核验正样本，未核验样本单列诊断（区分没 ingest 与 cue 不出来，`first-principles-kernel-gap.md:209`） |
| teaching-to-test（mempalace 自曝末段 0.6%，`competitor-memory-mechanisms.md:58`） | 冻结集分 `tuning` / `holdout`；**bar 在 holdout 上判定**；记录每条是否曾在调参中被看过（seen/unseen），seen-only 子集不得充当 bar |
| 「注入/服务即成功」混同消费（INV-2，`inv2:96,99-105`） | 本片只测**服务集里是否出现目标**（recall），不宣称被消费；consumption 归 S1，量纲分开 |
| 单跑数字当 bar（`08:149` 方差自证） | N 次独立重启取中位数 + 离散界（§6.3），单跑不作 gate |

---

## 5. 回归 harness（真语料副本 + 产品绑定）

### 5.1 隔离与可复现

- 先把 `~/.mnemoseed-local` **复制**到临时目录（`%TEMP%/opencode/...`），含 `config.toml`、`meta.db`(+WAL/SHM)、**全量 `chunks.lance`**、graph.db。本 harness **有语料端口**，不能像 INV-1 那样省掉全量 chunk 副本（INV-1 deviation 见 `inv1:116,305`，因其 rig 无端口，本片相反）。
- **可写状态每 run 从 pristine 基线重置（测量中立性硬约束）**：`MemoryService.recall` 并非只读--`memory.py:792` 无条件调用 `_record_hits` → `Reinforcer.record_hits`（`memory.py:834`；`decay/reinforce.py:90-120,126-132`）把 `decay_weight` 回弹并刷新 `last_reinforced` 写回向量/meta store，且 `decay_weight` 是排序输入（`memory.py:905,1903`）。这是当前唯一会在 run 间累积并污染测量的可写状态：短期激活默认关闭（`activation.py:49`）、状态为进程内易失 `OrderedDict`（`activation.py:70`），`boost()`/`refresh()` 未启用时直接返回（`activation.py:27-29,72-74`），今天不存在被持久化的激活，`session_id` 是否非空不改变分数（`memory.py:769,793-794` 的激活路径当前为空操作）。**每个冷启动 run 必须从同一份 pristine 基线重新恢复可写状态**（`meta.db`+WAL/SHM、`chunks.lance` 的 weight/state 列；激活列为前向项，仅未来显式启用时才纳入），不可跨 run 复用上一次 run 的 `_record_hits` 写后副本；不可变正文可只读共享。此约束使 §5.4 的「N 次独立冷启动」字面成立，消除「§5.1 只复制一次 → N run 复用被污染副本 → recall@8 系统性偏高」的歧义。
- 起 daemon 用临时 `MNEMOSEED_LOCAL_HOME` + 内核分配备用 loopback 端口（port 0 立即释放，非 7788/4096）；live daemon 全程不动。
- 钉四要素：`corpus_manifest.sha256`、`config` 快照、embedder 模型版本（真 `bge_m3_onnx`，非 synthetic）、代码 revision。任一漂移则判 `INVALID_RUN`。
- 复制规模/耗时 `[needs verification]`（§9 R-3）。

### 5.2 产品绑定（product binding，反自指的硬机制）

harness **必须**走真实 recall 路径，不得自造打分器：

1. import 产品模块并调用 `MemoryService.recall`（`memory.py:748-799`）或等价 `POST /memory/recall`（`memory.py:2879-2890`），按 `RecallRequest`（`memory.py:189-199`）传 `{profile_id, query, top_k, budget, session_id=None}`；`session_id=None` 关闭激活快照打分与 `_refresh_activation` 写回（`memory.py:769,793-794`）——这是前向的正确性/稳健性选择（未来启用激活时关闭该通道）；当前激活默认关闭且易失（`activation.py:49,70`），传 `None` 不改变分数。
2. 该路径内部固定经过：`_cues.extract`（`memory.py:761` 到 `cues.py:906`）to `_retriever.recall` 带 `vector_store=self._recall_view`（#448 影子视图，`memory.py:762-774,766`）to `_assembler.assemble` 预算装配（`memory.py:785`）to 返回 `{"memory": payload}`，含 `entries / dropped_count / tokens_used / coverage`（`memory.py:795-799,838-847`）。
3. **观测只取产品输出**：目标是否 served = `target_chunk_id` 属于 `payload.entries[].id`（`memory.py:842`）；覆盖/丢弃/token 取自同一 payload。**不另算 cue 重合、不自建融合分数**。
4. **绑定 killer（建议实现）**：加一条回归，把 harness 打分源从 `MemoryService.recall` 变异为「自算 cue 重合的伪路径」，必须使该回归 RED；以此证明 harness 真绑在产品路径上，防退化为 #206 式自报仪器。
5. **测量中立 killer（必须实现）**：回放传 `session_id=None` 且每 run 从 §5.1 pristine 基线重置可写状态；加一条回归对同一 query 在 N 次独立冷启动中重复回放，断言 recall@8 / miss_seed / honest-empty / rank / `tokens_used` 逐 run 恒等（p95 不作漂移判据）。**唯一 killer 变异 = 复用上一次 run 写后副本**（`memory.py:792` → `decay/reinforce.py:126-132` 的 `decay_weight` 回弹累积），必须使该回归 RED；这是现行唯一可观测的 run 间污染通道。传 `session_id=None` 是前向的正确性/稳健性选择，当前不改变分数，不构成 killer：短期激活默认关闭且易失（`activation.py:49,70`），传非空 `session_id` 不改变分数也不累积，不得声明其「必须 RED」。此 killer 与第 4 条正交：第 4 条证 scorer 来源，本条证测量不污染自身。
### 5.3 指标与预算（在真实端口上量测）

| 类别 | 指标 | 定义（产品输出） |
|---|---|---|
| 主指标 | **recall@K**（K=top_k=8，`assemble.py:82`） | 已核验正样本中，target chunk id 出现在 served entries 的比例 |
| 主指标 | **miss_seed 必过** | 冻结的 live miss 样本 individually = served（1/1） |
| 主指标 | **honest-empty 保真** | 负样本 served entries 为空的比例；填充即违约 |
| 次指标 | precision@K / 噪声准入 | served entries 中非任何 target 集的比例 |
| 次指标 | 目标 rank | target 在 served entries 中的位次（诊断 cue 失配 vs 预算挤占） |
| 护栏 | token 预算 | `tokens_used <= 800`（`assemble.py:83`）；预算挤占证据见 `recall-weakness-diagnosis:19-24` |
| 护栏 | p95 延迟 | 相对同一副本基线的比值；参考 #448 W4 的 1.5x 上界口径（`org/_state.md:37`），但属独立面，不复用 #391 bar |

量纲纪律：以上 recall bar 与 #206 的 downweight/quorum/retention 不同量纲（`inv1:86`）；与 #391 的 `TARGET_BARS` 物理分离。延迟/token 是**护栏**，不是质量 oracle。

### 5.4 可重复性与 fail-closed

- N 次（建议 N=3，owner 定）**独立冷启动**重复：每次 run 都从 §5.1 的 pristine 基线重新恢复可写状态并传 `session_id=None`（前向正确性/稳健性选择；当前激活默认关闭且易失，`activation.py:49,70`，传 `None` 不改变分数），run 之间不共享 decay 写回；报每次 recall、rank、token、p95 与离散度。
- **测量中立性回归（必须）**：同一 query 在 N 次独立冷启动中重复回放，recall@8 / miss_seed / honest-empty / rank / `tokens_used` 跨 run 漂移须为 0（p95 不作漂移判据，其方差另按护栏处理）；§5.2.4 第 5 条的变异 A 必须 KILLED（原第二 killer = 传非空 `session_id` 已删除：当前激活默认关闭且易失，不改变分数、杀不死）。运行内冻结查询集按随集冻结的固定顺序回放；本文明示按 run 重置而非按 query 重置，这是声明的测量边界，不得静默改为复用写后状态。
- 任一失败为 port 占用、embedder 不可用、哈希漂移、副本不完整，则判 `INVALID_RUN`：不计入 bar，也不写成 PASS/FAIL（无声不等于通过，继承 INV-2 纪律 `inv2:9-15`）。
- 报告为追加式 JSON（UTC 时间戳 + 四要素钉 + 逐 query 明细 + seen/holdout 标记），只增不减。
---

## 6. 质量契约（quality contract，阈值 + 测量法）

### 6.1 价值单元（what good recall means）

> 一条**已被 capture 且 ingestion-verified** 的事实，在 owner 后来给出一个人类会认为合理的自然语言线索时，能在**小服务预算内（top_k=8 / <=800 token）**被 surface，且不填充噪声、不伪造匹配。

### 6.2 预注册 bar（v1 提议值，owner 在 D2 裁决后冻结）

| id | 判据 | 门槛（提议，待 owner 裁决） | 类别 |
|---|---|---|---|
| B1 | miss_seed 回放 | **1/1 命中**（必过，这是 blocker 本体） | must-pass |
| B2 | recall@8（holdout 正样本，N 次中位数） | **>= 0.80** | 主 bar |
| B3 | honest-empty 保真（holdout 负样本） | **100%**（填充 0 容忍） | 主 bar |
| B4 | 噪声准入（served 非 target 占比） | <= 0.20（诊断 + guard） | 次 bar |
| B5 | token / 延迟护栏 | tokens_used <= 800；p95 <= 1.5x 基线 | 护栏 |
### 6.3 判定规则（measurement method）

1. 每次 run 对每条 query 按 §5.3 打分；`ingestion_verified=false` 样本**不进 B2 分母**，单列 diagnostic。
2. N 次独立 run 后每个指标取**中位数**；离散超预注册界（提议：max-min <= 0.10，owner 定）或出现 `INVALID_RUN`，则结果 = `UNKNOWN`，不得计 PASS。
3. **B1 且 B2 且 B3 全过**（且无 UNKNOWN）则契约 PASS；任一不过则 FAIL。
4. **failing which the kernel is not shippable**：本契约 FAIL 时，内核不满足 head invariant「captured implies retrievable」，不得发船；PASS 是本片对内核的最低发船条件（gate 是否强制激活由 owner 在 D3 裁决，本 PROPOSAL 不自启门）。

### 6.4 闭环可观测（诚实边界）

本契约测「召回是否 surface 目标」，**不测**「是否被消费/引用」，后者属 S1 消费仪表（`inv2`）。任何「recall 成功」措辞不得外推为「记忆被使用」（`inv2:101-105`）。自洽但无产品绑定的仪器不得计闭合（`inv1:272-282` 通用纪律）。
---

## 7. 与既有评测面的关系（拒用 #391 / 对账 08:24）

- **拒用 #391 synthetic rig 作 oracle**：`recall_matrix.py:46-52` 的 `TARGET_BARS = {recall@5:0.75, precision@5:0.60, detector_fp:0.15, fn_rate:0.20, token_overhead:0.8}` 是 T2 召回排序指标；rig 用 `driver="synthetic"` embedder + `stub` dream、无真语料端口（`inv1:88-100`）；`warm` 的 30 秒窗不是 30 天 retention（`inv1:101`）。把其 recall@5 当真实语料质量契约即 INV-1 量纲错位。
- **对账 `08-eval-harness.md:24`**：该行明写「不做 retrieval recall 评测（另行立项）」。本片正是那个**另行立项**：owner 路径 A（`org/_state.md:15`）+ issue #457 + 本设计门，单独授权、物理独立于 `src/mnemoseed_local/eval/` 现有子命令。二者无矛盾：`08` 声明自己不做，S0 补上；不改 `08` 既有结论，只在冻结后由 owner 决定是否加一条 as-is 指针。
- **与 #206 分仪器分 bar**：#206 的 downweight/quorum/retention 是 dream reconciliation 参数（`inv1:24-33`），真实副本 `reconcile_nominations=0`（`inv1:218`）无数据可回放；本片不碰、也不冒充其闭合（IMPORTANT-2，`first-principles-kernel-gap.md:188-190`）。
---

## 8. 证据表（file:line / `[未验证]`）

| 断言 | 证据 |
|---|---|
| verbatim 全量入库被当可找回信条 | `00:56,59`；`03:217` |
| 可用与可及之别 / 遗忘主因线索失败（锚） | `REFERENCES.md:99`(R47)、`:20`(R4)、`:60`(R29)；`00:161` |
| 无真语料 recall 评测 | `08:24` |
| #391 bar 量纲 + 无语料端口 | `recall_matrix.py:46-52`；`inv1:86,88-100,260` |
| 自指仪器不得闭合（#206 教训） | `inv1:272-282` |
| live miss（自然措辞召不回） | `recall-weakness-diagnosis:7-17`（确切 zcode 样本 `[未验证]`，D1） |
| 确定性零 LLM cue 提取 | `cues.py:1-12,906` |
| 实体重合 casefold / beta 权重 | `hybrid.py:522-538` |
| 图轨种子实体过滤 + 真语料图 0 边 | `hybrid.py:383-393`；`inv3:28` |
| #448 读侧影子视图 + fail-closed | `memory.py:766`；`readview.py:1-17,71-95`；`cues.py:1035-1054` |
| 装配默认 top_k=8 / budget=800 | `assemble.py:82-83,402-436` |
| 真实 recall 路径 + 输出字段 | `memory.py:748-799,838-847,2879-2890` |
| recall 非只读 / decay 写回污染排序（测量中立 killer 依据） | `memory.py:792,813-834,905,1903`；`decay/reinforce.py:90-132`（激活通道默认关闭且易失，`activation.py:49,70`，非现行 killer） |
| serve 与 consume 之别 / 无 usage 字段 | `inv2:96,99-105` |
| teaching-to-test 自曝 / benchmark 不可横比 | `competitor-memory-mechanisms.md:58,236` |
| 全量语料副本规模（4.81GB/52,848 文件） | `inv1:116,305`（本片需全量复制，`[needs verification]` 空间） |
---

## 9. 风险与反对意见（含对自己结论的）

- **R-1 标注主观 / bar 可被 gaming**：owner 人工标 target + holdout 密封 + 预注册 + 禁自指闭合（§4.4）。对冲：miss_seed 必过 + holdout 判定，防 owner 只挑容易 query 导致 bar 虚高。
- **R-2 过度工程（KISS）**：内核 gap 已预占此反对（`first-principles-kernel-gap.md:210`）。回应：S0 是**测量工具**而非新能力；刻意切到最小（一条 miss 种子 + 小查询集），复用已有 copy 纪律，不铺大评测面。
- **R-3 全量语料副本成本 `[needs verification]`**：`chunks.lance` 4.81 GB / 52,848 文件（`inv1:116`）。若临时盘不足，须定义**代表性冻结切片**并显式声明覆盖局限；不得静默缩小成合成 rig（那会退回 INV-1）。
- **R-4 真 embedder 首次加载/超时**：标 `INVALID_RUN` 而非 FAIL；固化模型版本与 warm-up。
- **R-5 反对我把 miss 归因为 cue 失配**：owner 样本未由本席复现（`first-principles-kernel-gap.md:209`，`[未验证]`）；可能是没 ingest。契约的 `ingestion_verified` 前置核验正是为区分二者：若发现是 ingest 失败，BLOCKER-1 仍成立，但修复路径不同，须回 owner。
- **R-6 延迟/token 护栏被误当 oracle**：明确标注为护栏、与 recall bar 量纲分离（§5.3 注）。
- **R-7 证据定位不精确**：brief 的 `hybrid.py:39-40` 指针与当前源码不符（§3.2 修正）；工程期须先取证再谈机制根因。
---

## 10. Verdict 与 issues（按严重度；claim / evidence / 最小调整）

**Verdict（对本设计地基）= SHIP-WITH-ADJUSTMENTS**：可作为 S0 地基进入独立设计 QA；gate 激活前须闭合下列项。本文为 PROPOSAL，不构成工程派发依据（`org/PROTOCOL.md:176-190`）。

- **ISSUE-1（BLOCKER · owner 决策）** 查询集与 bar 尚未 owner 署名/裁决，契约当前不完整。
  - Claim：§4 query 列表、§6.2 的 0.80/0.20/0.10 数值均为**提议**，非冻结。
  - Evidence：`recall-weakness-diagnosis:7-17` 仅为候选；D1/D2 未定。
  - 最小调整：owner 冻结前确认 miss 样本与正/负 query 文本并 ratify B1-B5 数值与 N/离散界；未定前不得激活 gate。
- **ISSUE-2（IMPORTANT · 证据修正）** `hybrid.py:39-40` 指针不精确，机制归因不可直接沿用。
  - Claim：brief 与 `first-principles-kernel-gap.md:40` 的「entity prefilter exact-match at :39-40」与源码不符。
  - Evidence：`hybrid.py:39-42` 为 docstring；真实行为在 `:522-531`（casefold）、`:390`（`NodeFilter` entities，driver 语义 `[needs verification]`）、`:7,41-42`（chunk 不硬排除）。
  - 最小调整：引用改钉真实行；「精确匹配致 cue 切掉」断言标 `[needs verification]` 待取证。
- **ISSUE-3（IMPORTANT · 可行性）** 全量语料副本成本未验证，且本片必须全量复制（与 INV-1 省副本相反）。
  - Claim：临时盘/复制耗时未知。
  - Evidence：`inv1:116,305`；本 harness 有语料端口（§5.1）。
  - 最小调整：工程期先量测空间/耗时；不足则用代表性冻结切片并显式记录覆盖局限，禁止改合成 rig。
- **ISSUE-4（IMPORTANT · 反 gaming）** 冻结集若被全程调参则退化为 teaching-to-test。
  - Claim：mempalace 自曝末段 0.6% 靠 inspect wrong answers（`competitor-memory-mechanisms.md:58`）；`08:149` 单跑方差。
  - 最小调整：强制 tuning/holdout 分离 + seen/unseen 记账 + bar 只在 holdout 判定。
- **ISSUE-5（NIT · 措辞）** 延迟/token 护栏易被误读为质量 oracle。
  - Evidence：§5.3 护栏行；`recall_matrix.py:46-52` 的 token_overhead 是 #391 量纲。
  - 最小调整：报告与冻结记录中显式标注「护栏非 oracle」。
- **ISSUE-6（IMPORTANT · 测量中立性，本修订闭合）** 回放路径自身写回排序状态，N-run 复用会污染 recall@8。
  - Claim：修订前 §5.1 只复制一次、§5.4 要求 N 次冷启动，默认读法复用被 `_record_hits`（`memory.py:792,813-834` → `decay/reinforce.py:90-132`）写后的副本；`decay_weight` 是排序输入（`memory.py:905,1903`），同一 query 重复回放使 recall@8 系统性偏高，仪器污染自身测量；原 binding killer（§5.2.4 第 4 条）只证 scorer 来源，不证测量中立。激活路径默认关闭且易失（`activation.py:49,70`），不构成现行污染通道。
  - Evidence：`memory.py:748-799`（:792、:793-794、:769、:845）；`decay/reinforce.py:90-132`。
  - 最小调整（已落实于 §5.1 / §5.2.1 / §5.2.4 第 5 条 / §5.4）：每 run 从 pristine 基线重置可写状态（重置原语把激活列为前向项，当前无持久化激活可重置）+ `session_id=None`（前向正确性/稳健性选择，当前不改变分数）；新增「重复回放无漂移」回归，其唯一 killer 变异 A（复用 decay 写后副本）必须 KILLED；slices W1/W3 增验收门。

---

## 11. Owner 决策点（交 owner，非承诺）

- **D1**：确认哪条 live miss 是冻结种子（brief 所指 zcode miss 是否即 `recall-weakness-diagnosis:7-17` 的 codex-import 样本），确认其 `ingestion_verified`；据此锁定 query 全集文本。
- **D2**：ratify 预注册数值：B2 recall 阈值（提议 0.80）、B4 噪声界、离散界（提议 0.10）、N（提议 3）、holdout 比例。
- **D3**：本契约是否作为**硬发船门**（内核冻结门）及其生效范围；是否批准全量语料副本的一次性成本与 holdout 密封策略。

---

*完。本片为 PROPOSAL（无门权）；零产品检索行为变更；未写产品源码/配置、未做 git 操作、未绑端口、未写 `~/.mnemoseed-local`。*
