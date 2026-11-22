# PRD-448 recall 自然措辞 / 跨语言语义桥 — 设计 brief v1

- **状态**: CANDIDATE — 设计门未过(对抗 QA 待跑),未冻结,未执行任何工程切片。
- **来源**: issue #448(owner R-1 裁决 2026-11-20 将 #447 AC2 重划至独立门);solution-architect 设计提案 v1(2026-11-21,本 brief 为其忠实转录)。
- **前置证据**: `org/workspace/recall-weakness-diagnosis-20261120.md`(实测);S2 别名表方案已被设计 QA 以三个实测阻断项否决(`org/workspace/recall-weakness-design-20261120/`),本 brief 不复活 S2。
- **问题一句话**: 已存储的事实用自然措辞(zh/en)取不回,只有精确 token(15/468、rollout、ledger、340)能召回(score 0.84–1.02);根因收窄为 tokenization 不对称 + 语义桥缺失,不是别名表问题。

## 0. 判定与 steelman

**SHIP-WITH-ADJUSTMENTS(提案自评)**。机制达到可冻结门槛,但两个决策点必须在冻结前的校准切片(W1)以实测解决,不得在实现期静默调参:

- **D1**: sparse-drag 地板塌缩假说必须在 replica fixture 上实测验证或证伪(当前是代码推导,标记 UNVERIFIED)。
- **D2**: probe-2 公式常数(词汇表种子集、append vs replace)必须从实测输出钉死。

Steelman:owner 的记忆存在且可取——但只在捕获时的精确词汇下。系统的承诺是自然语言下 recall 可用。核心设计决策:**停止在查询侧 cue token 上动手术(S2 已撤),转而修复真正承载"已存数据取回"的两个面——search 级相似度路径(结构性惩罚跨语言查询)与存储侧 cue 词汇(因为 extractor 从不产出小写英文 token 而贫化)——全部用只读、单调、fail-closed 的机制,不改写存储元数据,不降低任何既有候选的分数。**

## 1. 根因(全部 file:line 本 session 实读,extractor 输出为仓库字节上第一手复现)

**(a) CueExtractor 在 zh 与 en 之间不对称。**
`CueExtractor.extract`(`src/mnemoseed_local/retrieve/cues.py:906-1005`)实体来源:反引号标识符(:921-934)、大写开头 ASCII token(`_CAP_TOKEN` :259——要求首字母大写+后部小写,故 `import`/`rollout`/`ledger`/`codex` 永不命中)、全大写 ID 形 token(`_CAP_ID_TOKEN` :262-267——要求数字或连字符)、CJK 名词连续段(:944-945)、裸文件名(:948-951)、路径(:954-973)、点分/MCP 工具(:982-987)。实测:

```
'把 codex 对话记录导入 mnemoseed'          -> ['话记录导入']
'codex 历史回填进度和账本完成情况'          -> ['历史回填进度', '账本完成']
'full history import progress ledger'     -> []            # 零实体
'the history of rome'                     -> []
'把一部分 codex 的对话记录导入 mnemoseed 记忆里了吗' -> ['话记录导入', '记忆里']
'全量 import 账本 15/468 340 rollout settle'      -> ['全量', '账本']
```

- zh:最大 CJK 连续段在胶水字符处切分并两端去胶(`_polish_cjk` :704-722;胶水集 `_CJK_GLUE_STR` :676 含对/把/的/里邻接虚词)。对 是胶水字符,查询段「对话记录导入」塌缩成 `话记录导入`——一个 5 字粘连块,不是短语,对连续段边界脆弱。
- en:小写词不被任何规则提取。英文自然探针零实体。
- 数字形 token(`15/468`、`340`)不被任何规则提取。

**(b) 查询 cue 与存储 cue 的 join 是精确 token、precision 基,且被贫化的存储 cue 供血。**
捕获路径用同一 extractor 打标:daemon ingest 拼接 user/assistant 文本调 `extract_cues(text)`(`src/mnemoseed_local/daemon/app.py:364-369`),`StampWriter._assemble` 把 `ctx.entities` 原样抄进 `ChunkStamp.cues.entities`(`src/mnemoseed_local/capture/stamper.py:410-458`);`remember()` 同(`src/mnemoseed_local/daemon/memory.py:945`)。所以目标 chunk 的存储 cue 词汇是 `['全量','账本']`——其 verbatim 文本里的英文术语(import、rollout、settle)和数字从未入标。join 在 `_chunk_cue_overlap` → `_ratio(query, stored) = |q∩s|/|q|`(`src/mnemoseed_local/retrieve/hybrid.py:534-538`,:526-531),以 0.6 entity / 0.25 tool / 0.15 context(:67-69)入 beta,`weight_cue_overlap=1.0`(:95)。自然 zh 查询(`['话记录导入','记忆里']`)对存储 `['全量','账本']` 交集为空——beta 对目标 chunk 恰好贡献 0。同一 extractor 不对称让 graph 轨道同样死掉:`_graph_track` 在查询零实体时立刻返回 `[]`(`hybrid.py:346-348`),种子要求与 graph 节点精确实体相等(`hybrid.py:383-393`)——粘连 CJK 段匹配不到任何节点;graph 节点只在 dream 固化与 reinforcement 时创建(`src/mnemoseed_local/dream/merge.py:239-257`、`src/mnemoseed_local/decay/reinforce.py:192-201`),捕获时不建,所以 graph 通道几乎无种子。与诊断 Finding 2 的 `graph_hits=0` 吻合。

**(c) 本可语义桥接的 vector 通道,被 store 级相似度融合结构性削弱,然后被地板砍掉。**
`HybridRetriever._vector_track` 对原始查询文本做 embed(`hybrid.py:273`),driver 以固定 0.5/0.5 融合 dense 与 sparse 相似度(`_DENSE_FUSION_WEIGHT = 0.5`,`src/mnemoseed_local/storage/drivers/lancedb_embedded.py:68`;融合分 :355-361;sparse 相似度是 token-id 索引的点积余弦 :845-856)。BGE-M3 sparse 向量按 XLM-R token id 索引(`src/mnemoseed_local/storage/drivers/bge_m3_onnx.py:12-16`):同一概念的 zh 与 en 表层形式 token id 不相交,所以跨语言/远改述查询的 sparse 分量结构性 ≈0,融合相似度塌缩到 ≈0.5·dense。`min_similarity` 地板 0.45(`hybrid.py:99`)随后作用在该融合相似度上(`hybrid.py:312-314`),先于任何预算分配。这机械地解释了诊断 Finding 1 的实测不对称:zh 自然措辞(与存储文本同语言共 token)存活过地板但排在 cue 富集竞争者之后(DECISION 0.72、rule 0.68);英文探针塌缩到 2 个 0.69 的 vector 命中,其余全部被地板砍掉。**"sparse 拖拽把跨语言候选压到 0.45 地板之下"是承重的未验证步骤**——由融合公式+诊断实测推出,不是逐分量 live 探针。校准切片(W1)先测它;**[UNVERIFIED — 标记,冻结前 W1 实测]**。

**(d) #447 已落的 S1 为什么不关本 issue。** dedup(`assemble.py:325-357`)释放花在字节级重复 chunk 上的预算,可能帮助 zh 自然措辞情形的尾部准入,但它不改相似度、不改 cue overlap、不改地板——跨语言与改述缺口原样未动。W1 量化残余。

结论:失败不是"排序权重错"、不是"缺别名条目"。是 (i) tokenization 不对称把两侧词汇桥面清空,(ii) 语义通道的相似度公式把一个结构性单语的词汇分量平均进了准入地板读的那个分数。

## 2. 机制选项(逐项复查 S2 三阻断项)

S2 三阻断项:(B-1) 零实体探针使实体驱动的机制够不着;(B-2) 共享 `CueExtractor.extract` 的插入点会在捕获路径改写存储 cue;(B-3) precision 基 `_ratio` 让查询侧加非匹配 token 反而降分(本 session 复现:q=(进度,账本) vs stored(全量,账本):0.500 → 加 progress、ledger → 0.250)。

- **M1 — 查询侧别名/规范扩展喂 beta(S2 重做)。否决。** B-1:英文探针实体为 `[]`。B-3:稀释算术如上。B-2:`extract()` 内任何插入都会在捕获路径触发(`memory.py:945`、`app.py:369`)。即上一波撤回的方案,列全起见。
- **M2 — 捕获侧规范打标 + 重处理存量行。否决(作为主案)。** 重打标存量 chunk(或重跑 import)改写已存数据的 cue 元数据——约束禁止,且 live import 出范围。只标未来捕获无法满足 AC("已存储的状态事实")。R-1 认可"capture-side canonical tagging"作为本门方向;它仍是合法的未来切片,但对 AC 非必需(与 `s2-path-decision.md` §5 同理),为最小化约束面不进推荐包。
- **M3 — store driver 相似度融合改造(`lancedb_embedded.py` 里 `max(dense, fused)`)。可行备选,非主案。** 无词汇表、无额外 embed 成本地结构性修复跨语言地板塌缩;逐行相对现 fused 值单调不减。代价:对每次查询是全局评分语义变化(任何 sparse>dense 处同语排序都动),需要对 synthetic embedder 下钉分数的 hybrid/storage 测试做大面积重钉;决定性的是——其跨语言效果 **在 CI 中不可确定性测试**(synthetic embedder 的 dense 相似度是整段文本的 hash,`synthetic_embedder.py:39-94`,zh-vs-en dense 相似度是噪声;CI oracle 只能测公式,测不出效果)。留作校准(D1/D2)表明桥够不着 oracle 时的明文应急案。
- **M4 — graph 轨道桥。否决。** 依赖 dream/refinement 的节点构建(出范围)和 extractor 不对称清空的精确实体种子;它继承它本要修的每个阻断项。
- **M5(选定)— 读侧双件套桥:存储侧 cue shadow + 词汇表增强第二探针 + 单调 max 合并。** §3 详述。三阻断项复查:
  - B-1(零实体探针):glossary 匹配器作用于**原始查询文本**,不作用于 extractor 实体——采纳 QA round 1 对 B-1 的最小修复("在 RAW 查询文本上匹配别名短语")于 search 级。英文探针逐词命中 import→导入、ledger→账本、history→历史;zh 探针按子串命中 导入→import、记忆→memory。extractor 零实体与机制无关。
  - B-2(捕获路径改写):**什么都不写**。shadow 在读时从不可变 verbatim 文本重算;捕获路径(`memory.py:945`、`app.py:369`、`stamper.py:410-458`)不动;`cues.py` 只新增一个加性纯函数,`extract()` 行为不变。装饰器只在 recall 路径包 vector store(`memory.py:756-760`——经 grep 验证是产品内唯一 `retriever.recall` 调用点;`remember` 及相邻捕获、`_focal_scan` `memory.py:1763-1830`、`snapshot_read` 消费者全部不经过)。
  - B-3(非匹配新增降分):两条结构性保证。(i) shadow 只增富**存储**侧:在 `_ratio(q,s)=|q∩s|/|q|` 下 `|q|` 查询期固定、`|q∩s|` 对 `s` 单调不减——增富对每个候选只能抬分或持平,永不降分。beta 的查询侧本设计完全不碰。(ii) probe-2 逐 chunk 按 max 合并:双探针都取回的候选取 `max(probe-1 相似度, probe-2 相似度)`,否则取自己探针的相似度——任何候选相似度不降,probe-1 独占候选字节级不变。加进 probe-2 **文本**的 glossary token 对 beta 无任何影响。

## 3. 选定机制 M5 — 精确设计

### 3.1 新模块与插入点

| 产物 | 类别 | 内容 |
|---|---|---|
| `src/mnemoseed_local/retrieve/bridge.py` | 新,纯,无 IO | 双语领域 glossary(冻结的 zh↔en 规范对元组)、确定性 raw-text 匹配器(最长匹配优先、首次出现序、去重、上限 8 term)、`augment_query(text) -> str \| None`(无 glossary 命中返回 `None` → 单探针行为,与今日字节一致)。无时钟、无随机、无网络。 |
| `src/mnemoseed_local/retrieve/cues.py` | 仅加性 | 一个新纯函数 `lexical_shadow_tokens(text, config) -> tuple[str, ...]`:extractor 实体(经现有 `extract()`)+ 小写 ASCII 实义词(词边界 `[a-z][a-z0-9_-]{2,}`,casefold,减现有 `_CAP_STOPWORDS` :392-488,上限 24 常量)。`extract()` 本体不动——捕获路径永不调用新函数。 |
| `src/mnemoseed_local/retrieve/readview.py` | 新 | `RecallVectorView`——实现 `VectorStore` port 的读侧装饰器,包真实 store。只覆写 `search()`:(1) 内层 probe-1 search 用传入的 dense/sparse(不重算);(2) 若 recall 入口处 `augment_query` 触发,用注入的 embedder embed probe-2 文本并以同一 `ChunkFilter`、`top_k` 跑内层 probe-2 search;(3) 逐 chunk `max(相似度)` 合并,按 (相似度降序, chunk_id 升序) 排序,截断到 `top_k`;(4) 用 `lexical_shadow_tokens(chunk.text)` 增富每个返回 `ChunkStamp.cues.entities`——仅内存,永不持久化。其余 port 方法(`snapshot_read`、`list_chunks`、`near_duplicate`、`upsert_*`…)原样透传。 |
| `src/mnemoseed_local/daemon/memory.py` | 一处接线 | `MemoryService.__init__`(:660-673 后)构造 `self._recall_view = RecallVectorView(inner=self._stores.vector, embedder=self._stores.embed, bridge=BridgePolicy())`;`recall()` :760 传 `vector_store=self._recall_view` 替代 `self._stores.vector`。`recall()` 其余行不变;assembler、meta store、graph store、activation、residue 路径全部留在原始 store。 |

装饰器实现既有 port——不改 port、不改 schema、无存储写、无 config 键(冻结先例:config 不加新可调项)。

### 3.2 端到端数据流

1. `POST /memory/recall`(`memory.py:2873-2886`)→ `MemoryService.recall`(:742)。`self._cues.extract(query, ...)` :755 **不变**——beta 的查询 cue 与今日完全一致。
2. `augment_query(query)` 在 recall 入口跑一次(`RecallVectorView` 内,从 retriever 透传的原始查询文本计算——retriever 已把 `query_text` 传进 `_vector_track`,`hybrid.py:273`;视图按调用绑定原始查询文本,见 3.4 绑定点——所以 hybrid.py 签名零改动)。
3. `_vector_track`(`hybrid.py:263-335`,不改动)embed 查询文本调 `view.search(...)`。视图跑 probe-1(与今日相同的内层 search),再跑 probe-2(embed 增强文本,内层 search)。逐 chunk max 合并、截断 `top_k`、shadow 增富 stamps,返回。
4. Hybrid 原样消费合并命中:地板 :312-314 作用于合并相似度;rescue 带 :298-311 作用于对 shadow 增富后存储 cue 算出的 `_chunk_cue_overlap`;`_ratio` 算术不动。
5. Assembler、预算门、冲突配对、freshness guard:不变。`assemble._probe` 用 `snapshot_read`(:485-492),透传不增富——freshness 语义字节级稳定。

**存储 vs 查询期计算**:存储 = verbatim 文本、cues、embeddings、provenance——全不变,零改写。查询期计算 = probe-2 embedding(glossary 触发时多一次 embedder 推理)+ 一次额外 ANN search + shadow 重提取(对 ≤ `candidate_k = max(top_k*4, top_k+50)` 行 `lancedb_embedded.py:345` 的正则,微秒级)。无跨调用缓存;同输入确定性。

### 3.3 为什么不可能降任何东西(单调性契约)

- `_ratio` 分母是查询集(固定);shadow 增大存储集 ⇒ 每个候选 beta 的 entity 分量单调不减。tool、context 分量不动。
- probe-2 合并逐 chunk 取 max ⇒ 每候选相似度不减;probe-1 独占候选字节级不变。
- 推论:每个现役候选的融合分只升或持平;新候选只能靠**超过地板并反超**进入——rescue 是严格竞争性的。先前被服务的候选被挤出 top-k,仅当另一候选的分数真实升过它——披露,并由位移性质测试钉死(probe-1 成员只能被合并相似度严格更高的 chunk 挤出合并 top-k)。
- rescue 带增宽:shadow 增富可把主地板之下的显式 pin 推过 `rescue_cue_min`(`hybrid.py:307-311`)。这是 R4 对齐行为(强 cue 取回弱痕迹),专测钉死;无 shadow 命中时零变化。

### 3.4 fail-closed 行为

- `augment_query` 返回 `None`(无 glossary 命中)→ 单探针;视图直通;recall 行为与现 main 字节一致。
- probe-2 embed 或 search 抛错 → 视图捕获、记 warning、原样返回 probe-1 结果集(fail-open 到现状;recall 永不因桥而破)。
- shadow 提取抛错 → 该结果集跳过增富(返回原始 stamps);recall 永不失败。
- 空查询文本 → 无 glossary 命中、无 probe-2、无 shadow(extractor 输出与今日一致)。
- 无 config、无 IO、除 recall 路径已有的 embedder 外无模型调用——token 经济尊重(glossary 是静态表,不是 LLM)。

### 3.5 成本 / 时延预算影响

- 无 glossary 命中的同语查询:零新增成本(直通)。
- 桥触发查询(zh 与 en——owner 的主导用法):vector 轨道 +1 embedder 推理 +1 ANN search。绝对毫秒数 **[UNVERIFIED — W1 实测]**;冻结时钉实测数与 recall 路由 p95 前后对比。超预算应急:收窄触发(probe-1 返回命中数低于固定常数才发 probe-2)——设计门决策记在校准处,绝不是实现期调参。
- shadow 重提取:对返回候选行的有界正则;零新增 store 读。

## 4. 测试 oracle

两层 oracle,对齐仓库既有 CI/本地分层(CI 用 synthetic embedder,500MB 模型不得下载——`synthetic_embedder.py:1-6`、`tests/test_eval_recall_harness.py:64`;真模型效果跑 eval rig——`eval/recall_harness.py:1-16`、`eval/rescue_harness.py:215-240`)。

### 4.1 固定 zh/en 效果 oracle(本地,真实 embedded preset——AC 门)

**Fixture 捕获流(真实漏斗,无合成 cue)。** 全新 rig root(fail-loud fresh-root 契约,`eval/rig_freshness.py`)、临时 `MNEMOSEED_LOCAL_HOME`、spare port(绝不 7788);起真 daemon(`daemon.app.create_app`,`eval/recall_harness.py:14-15`);经真实 ingest 漏斗播种(`POST /ingest` → `_daemon_write_context` `app.py:348-381` → `StampWriter._assemble` `stamper.py:410-458`——与给 owner `opencode-chat` chunk 打标的同一漏斗,source 标签 `f"{turn.host.value}-chat"` :432)。播种语料(双语,镜像诊断 Finding 1 家族):

- 目标状态事实文本:zh/en 混排执行状态,如「codex 全量 import 后台跑 15/468,账本从 161 起,340 轮 rollout settle 完成」族;
- DECISION 干扰项(「已决定把一部分 codex 的对话记录导入 mnemoseed 记忆…」族);
- past-fact rule 干扰项;6-10 条无关双语噪声 chunk(entity-miss 与 entity-collision 类,循 `eval/recall_materials.py` 惯例)。

**Oracle 从真实捕获的 cue 实体推导。** 播种后 harness 读回存储 chunk,**断言每条 chunk 的存储 `cues.entities` 等于测试内现算的 `CueExtractor.extract(text).cues.entities`**——oracle 钉在真实 extractor 实际打的东西上(目标的期望标即 `['全量','账本']` 类),查询实体集同样测试内现算,不手写。任何地方无合成 cue store。

**固定查询与钉死断言**(top_k=5、budget_tokens=800 默认):

- zh 自然:「之前有没有把一部分 codex 的对话记录导入 mnemoseed 记忆里」 — GREEN:目标 chunk id 在 5 条服务条目中(kind=chunk);RED 基线在 main:目标缺席(W1 实测并钉住精确基线排序)。
- en 自然:"did we already import part of the codex chat history into mnemoseed?"(不含 rollout/ledger/15/468/340)— 同 GREEN/RED。
- 回归控制:(a) 精确 token zh 查询(Finding-1 的 1.02 类查询)仍以 top-1 取回目标——工作情形零回归;(b) 无关查询(「the history of rome」、「progress of the disease」、无关 zh 句)不取回目标、不引入新噪声进 top-5;(c) 预算丢弃指标(`dropped_count`、`tokens_used`)与 main 同 fixture 相比不恶化;(d) `hybrid.py` config 钉原样(`tests/test_retrieve_hybrid.py:829-831`)。

### 4.2 CI 机制 oracle(synthetic embedder,确定性 pytest)

synthetic embedder 下跨语言 dense 相似度是 hash 噪声,所以 CI 层证明**安全包络与机制**,不证明语言语义:

1. 单调性性质:随机 cue 集下,增富存储侧永不降低 `_ratio` 推导的 beta(shadow 开/关对照)。
2. 不降分性质:视图安装后,每个 probe-1 候选相似度不变或升高;probe-1 独占候选字节级不变。
3. union-rescue RED→GREEN 机制 fixture:存储文本 token 集等于 probe-2 增强文本(synthetic sparse 余弦 1.0 ⇒ fused ≈ 0.5 + 0.5·dense 噪声——fixture 文本一次校准使合并相似度确定性过 0.45),probe-1 单独留在地板下 → main 上 RED(目标缺席),装视图后 GREEN(目标被服务)。这是机制 fixture;cue 元数据仍是真实漏斗所打。
4. fail-closed 注入:augmenter/embedder/内层 search 抛错 → recall 返回 probe-1 结果;shadow 抛错 → 原始 stamps;全部记日志,无一致命。
5. 触发诚实性:无 glossary 命中 → `search` 行为与 main 字节一致(hash 命中列表对照);QA round 1 I-1 的负控制(「the history of rome」经 glossary 得 历史——断言池增长有界、无无关目标浮现,地板有界)。
6. shadow 单元测试(`tests/test_retrieve_cues.py` 加性):extractor 实体保留、小写 token ≥3 字符拼入、stopword/短 token 排除、上限遵守、确定性。
7. `snapshot_read` 透传钉死(freshness 探针不受影响)。

## 5. 理论锚

**复用(既有注册表条目;不重审其表述):**

- **R4 — Tulving & Thomson (1973), encoding specificity**(`docs/zh/design/REFERENCES.md:20`;注册于 `docs/zh/design/00-overview-and-philosophy.md:161,:234`、`docs/zh/design/03-storage-and-retrieval.md:99-101,:235`;rescue 带引于 `hybrid.py:299-306`)。已验证规律:取回成功取决于取回线索与编码情境的重叠。本设计落的设计规则:增宽既有 overlap 算术读的 cue 面(存储侧 shadow)+ 增加一条取回路径(probe-2)——评分理论与 beta 语义不动。
- **R47 — Tulving & Pearlstone (1966), availability vs accessibility**(`REFERENCES.md:99`,现 scope 于 design 09)。已验证规律:存储中可得的信息可暂时不可及;合适线索恢复可及性。设计规则:目标事实可得(精确 token 下 0.84-1.02 可取)但自然措辞下不可及;桥供给合适的取回路径而非重编码记忆。PRD 期注册动作:把 R47 的 对应本篇 覆盖扩展到新设计文档——注释,不是新条目。

**候选新条目(仅在 Crossref 核验后注册;核验前 ⚠️):**

- **R-NEW 候选 — 跨语言 / 翻译等价 cue 访问。** 已验证规律(双语记忆):以一种语言编码的概念经另一语言的翻译等价线索保持行为可达(translation priming / cross-language semantic access)。注册前必须核验的候选来源——不得仅凭本提案引文注册:Kroll & Stewart (1994), *Journal of Memory and Language*(不对称跨语言访问),和/或 Francis (1999), *Psychological Bulletin*(共享语义表征综述)。精确卷页 **[UNVERIFIED]** ——工程批必须 Crossref 核验作者/年份/出处并按维护规则以诚实状态注册(`REFERENCES.md:144`)。设计规则:有界、确定性的翻译等价 term 集可在 search 面增强查询,仅取回侧、单调(max 合并),永不改写存储内容。
- 诚实退路:核验失败则 glossary 作为纯工程机制落地(I 型工程条目,`REFERENCES.md:133-134` 的 `I1/I2` 模式),无理论声明;设计立在 R4 + R47 上即足,机制不依赖锚。

**明确不借:**BIA+/RHM 作为架构(是模型不是规律);关键期/习得年龄神话;学习风格;Mozart 效应;大脑 10%;阈下编码;"跨语言 embedding 证明人类共享语义"(工程巧合);LLM 查询改写(plumbing 无模型调用——token 经济);间隔/重复流行声明(承自 #447 v2 不借清单);语义 embedding 模型替换或更换;对 R4/R47/R48 表述的任何改动。不重审既有锚。

## 6. 滚动切片与禁触清单

| 切片 | 内容 | 门 |
|---|---|---|
| W1 | 校准 replica:bridge eval 材料 + harness(rig 惯例循 `eval/recall_harness.py` / `rescue_harness.py`);在现 main 上以真实 embedded preset 跑;对固定 zh/en oracle 查询逐候选实测 dense/sparse/fused 相似度与 top-k;验证或证伪 sparse-drag 地板假说;钉 RED 基线与存储 cue 打标断言;从实测输出裁决 D1(假说成立)与 D2(词汇表种子集、append-vs-replace) | 仅设计工件;零产品改动 |
| W2 | 桥核心:`retrieve/bridge.py` + `retrieve/readview.py` + `memory.py` 接线 + CI 机制测试(§4.2 条 1-5、7) | TDD RED→GREEN;门禁全净 |
| W3 | Shadow:`cues.py` 加性 `lexical_shadow_tokens` + 视图增富 + CI 性质测试(§4.2 条 1、6) | TDD RED→GREEN |
| W4 | 效果 oracle eval 门:W1 harness 以 W2/W3 重跑——固定 zh/en oracle 绿(双语言目标进 top-5)+ 全部回归控制绿;eval 报告工件 | 本地 eval 跑,真 preset,隔离 home |
| W5 | 文档 + 注册:`docs/zh/design/` 设计文档、PRD brief、`REFERENCES.md` 注册编辑(R47 覆盖注释;R-NEW 候选仅当 Crossref 核验通过) | doc-only |

**工程批禁触:**`retrieve/hybrid.py`(冻结;钉在 `tests/test_retrieve_hybrid.py:829-831`);`CueExtractor.extract` 的行为(捕获共享——`memory.py:945`、`app.py:369`);`capture/stamper.py`;`daemon/app.py`;任何 storage driver(`lancedb_embedded.py`、`sqlite_*.py`、`bge_m3_onnx.py`、`synthetic_embedder.py`);`config.py`(不加键、不改默认);`schema/*`;dream/refinement;`hosts/*`;MCP wire 表(不加字段——coverage 字段含义不变,只有其**值**可动,回归控制钉住);已安装 runtime、7788 端口、任何 live import/daemon PID(测试/live 隔离指令 2026-08-31);任何 shadow 或增重工件的捕获路径写入。

## 7. 开放风险(对抗设计 QA 应最猛攻击处)

1. **sparse-drag 地板塌缩假说 UNVERIFIED**(§1c)。代码推导,非 live 实测。攻击:强制 W1 在任何冻结前于 fixture 上逐分量实测;若地板不是 en 探针的绑定者,桥的目标移位,门重跑。
2. **zh top-5 钉无解析保证。** 机制单调抬目标相似度,但对 cue 富集竞争者(DECISION chunk)的排序是经验的。攻击:要求校准产出实测排序;冻结后任何常数改动(词汇表 term、append-vs-replace、触发器)按门重跑处理,不按补丁处理。
3. **glossary 过扩展污染**(QA round-1 I-1 类):无关查询中的命中 term 改变 probe-2。攻击:负控制(「the history of rome」「progress of the disease」)+ 地板有界的池增长;逐一种子 glossary 对实测正当性审查或移除(D2)。
4. **Metamemory 漂移**:`vector_hits` 与 `floor_dropped` 值会动(命中更多、计入更多 sub-floor probe-2 行)。字段定义不变;#447 Q1 可观测(预算丢弃指标)必须钉为不受影响。
5. **轨道容量位移语义**:合并截断只能经严格更高的合并相似度挤出 probe-1 尾部成员——攻击性质测试的强度。
6. **时延**:桥触发查询多一次 embed 是真实热路径成本——要求冻结时给实测前后 p95。
7. **CI 机制 fixture 边缘性**:fused ≈ 0.5 + 0.5·dense 噪声对 0.45 地板——fixture 必须一次校准并钉死;攻击其构造性 flaky。
8. **装饰器包围**:grep 验证 `memory.py:756` 是产品内唯一 `retriever.recall` 调用点、`snapshot_read`/捕获路径永不经过视图。

## 附录:并行批次冲突检查(solution-architect 已复核)

Task A(#444/#445,config 写原子性)在同一工作树并行工程,面为 `src/mnemoseed_local/configwrite/**` 与 `tests/test_configwrite_*`。本设计的面为 `src/mnemoseed_local/retrieve/{bridge.py(新), readview.py(新), cues.py(加性)}`、`src/mnemoseed_local/daemon/memory.py`(`__init__` 一处接线 + `recall()` :760 一处实参)、`src/mnemoseed_local/eval/{bridge_materials.py(新), bridge_harness.py(新)}`、tests `tests/test_retrieve_bridge.py(新)`、`tests/test_retrieve_readview.py(新)`、`tests/test_retrieve_cues.py(加性)`、`tests/test_eval_bridge_*.py(新)`。**面不相交:与 Task A 无共享文件**(`configwrite/**` 不含 `daemon/memory.py` 与 `retrieve/**`;无 test-oracle 碰撞——Task A 的测试在 `tests/test_configwrite_*`)。操作注意:两批同树跑全门,适用同树小批规则(2026-08-23,已验证不相交文件面);任一批的范围长进对方文件即串行化。
