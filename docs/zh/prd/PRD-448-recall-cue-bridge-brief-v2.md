# PRD-448 recall 自然措辞 / 跨语言语义桥 — 设计 brief v2

- **状态**: FROZEN(2026-11-21)— 设计门 QA round 1 判词 `NOT CLOSABLE (BLOCKER=1 IMPORTANT=4 NIT=5)` → v2 应用修订 R1-R14 → round 2 判词 `CLOSABLE (BLOCKER=0 IMPORTANT=2 NIT=0)`,其两个 IMPORTANT(NNEW-1 计数器语义、NEW-2 回归控制范围)已按 QA 给定文本落为两处修订(§3.6 / §4.1c)后冻结。**W1 校准实测后按 §2 M3 预注册条款重走设计门 → §8 v3 修订(round 3 判词 `CLOSABLE BLOCKER=0 IMPORTANT=3 NIT=2`,披露修正已并入)为现行治理版本**;§1-§7 中与 §8 冲突的条目(glossary/probe-2、per-call 绑定、no-glossary 字节一致断言、W2/W4 切片)以 §8 为准。工程准入:W4b 真实语料实测门 + R-2 记录(见 §8.5)。
- **v2 相对 v1 的变更**:查询文本绑定缝(按调用构造视图,§3.1/§3.2/§3.4)、位移不变量改为全序键形式(§3.3)、桥可观测性计数器(新增 §3.6)、增富 copy 纪律(§3.1)、预算指标钉死范围收窄(§4.1c/§7.4)、W1/W5 门措辞与 M3 再门声明(§2/§6)、四处事实修正(§1)。
- **来源**: issue #448(owner R-1 裁决 2026-11-20 将 #447 AC2 重划至独立门);solution-architect 设计提案 v1+v2(2026-11-21,本 brief 为其忠实转录)。
- **前置证据**: `org/workspace/recall-weakness-diagnosis-20261120.md`(实测);S2 别名表方案已被设计 QA 以三个实测阻断项否决(`org/workspace/recall-weakness-design-20261120/`),本 brief 不复活 S2。
- **问题一句话**: 已存储的事实用自然措辞(zh/en)取不回,只有精确 token(15/468、rollout、ledger、340)能召回(score 0.84–1.02);根因收窄为 tokenization 不对称 + 语义桥缺失,不是别名表问题。

## 0. 判定与 steelman

**SHIP-WITH-ADJUSTMENTS(提案自评)**。机制达到可冻结门槛,但两个决策点必须在冻结前的校准切片(W1)以实测解决,不得在实现期静默调参:

- **D1**: sparse-drag 地板塌缩假说必须在 replica fixture 上实测验证或证伪(当前是代码推导,标记 UNVERIFIED)。
- **D2**: probe-2 公式常数(词汇表种子集、append vs replace)必须从实测输出钉死。

Steelman:owner 的记忆存在且可取——但只在捕获时的精确词汇下。系统的承诺是自然语言下 recall 可用。核心设计决策:**停止在查询侧 cue token 上动手术(S2 已撤),转而修复真正承载"已存数据取回"的两个面——search 级相似度路径(结构性惩罚跨语言查询)与存储侧 cue 词汇(因为 extractor 从不产出小写英文 token 而贫化)——全部用只读、单调、fail-closed 的机制,不改写存储元数据,不降低任何既有候选的分数。**

## 1. 根因(全部 file:line 经 QA round 1 对照实码复核)

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

- zh:最大 CJK 连续段在胶水字符处切分并两端去胶(`_polish_cjk` `cues.py:704-722`;胶水集 `_CJK_GLUE_STR` `cues.py:676`——含 的/了/吗/吧/是/把/对/向/从/到/这/那 之类虚词,**不含 里**)。对 是胶水字符,查询段「对话记录导入」塌缩成 `话记录导入`——一个 5 字粘连块,不是短语,对连续段边界脆弱。「记忆里」能作为 3 字连续段存活,仅因 里 不在胶水集。
- en:小写词不被任何规则提取。英文自然探针零实体。
- 数字形 token(`15/468`、`340`)不被任何规则提取。

**(b) 查询 cue 与存储 cue 的 join 是精确 token、precision 基,且被贫化的存储 cue 供血。**
捕获路径用同一 extractor 打标:daemon ingest 拼接 user/assistant 文本调 `extract_cues(text)`(`src/mnemoseed_local/daemon/app.py:364-369`,`extract_cues` 在 :369),`StampWriter._assemble` 把 `ctx.entities` 原样抄进 `ChunkStamp.cues.entities`(`src/mnemoseed_local/capture/stamper.py:410-454`,:424 处复制);`remember()` 同(`src/mnemoseed_local/daemon/memory.py:945`)。所以目标 chunk 的存储 cue 词汇是 `['全量','账本']`——其 verbatim 文本里的英文术语(import、rollout、settle)和数字从未入标。join 在 `_chunk_cue_overlap` → `_ratio(query, stored) = |q∩s|/|q|`(`src/mnemoseed_local/retrieve/hybrid.py:534-538`,:526-531),以 0.6 entity / 0.25 tool / 0.15 context(:67-69)入 beta,`weight_cue_overlap=1.0`(:95)。自然 zh 查询(`['话记录导入','记忆里']`)对存储 `['全量','账本']` 交集为空——beta 对目标 chunk 恰好贡献 0。同一 extractor 不对称让 graph 轨道同样死掉:`_graph_track` 在查询零实体时立刻返回 `[]`(`hybrid.py:346-348`),种子要求与 graph 节点精确实体相等(`hybrid.py:383-393`)——粘连 CJK 段匹配不到任何节点;graph 节点只在 dream 固化时创建(`src/mnemoseed_local/dream/merge.py:235-245`:`_write_triple` 经 `graph.upsert_node(self._new_node(...))` :241 创建),reinforcement(`src/mnemoseed_local/decay/reinforce.py:188-201`)只对**既有节点** upsert `model_copy` 强化副本、从不创建;捕获路径不建任何 graph 节点——承重论断成立。与诊断 Finding 2 的 `graph_hits=0` 吻合。

**(c) 本可语义桥接的 vector 通道,被 store 级相似度融合结构性削弱,然后被地板砍掉。**
`HybridRetriever._vector_track` 对原始查询文本做 embed(`hybrid.py:273`)并本地调用 `vector_store.search(embedding.dense, embedding.sparse, ChunkFilter(...), config.vector_top_k)`(:274-292)——原始文本是 `_vector_track` 栈帧内的局部实参,**不越过 port 缝**。driver 以固定 0.5/0.5 融合 dense 与 sparse 相似度(`_DENSE_FUSION_WEIGHT = 0.5`,`src/mnemoseed_local/storage/drivers/lancedb_embedded.py:68`;融合分 :355-361;sparse 相似度是 token-id 索引的点积余弦 :845-856)。BGE-M3 sparse 向量按 XLM-R token id 索引(`src/mnemoseed_local/storage/drivers/bge_m3_onnx.py:11-12`):同一概念的 zh 与 en 表层形式 token id 不相交,所以跨语言/远改述查询的 sparse 分量结构性 ≈0,融合相似度塌缩到 ≈0.5·dense。`min_similarity` 地板 0.45(`hybrid.py:99`)随后作用在该融合相似度上(`hybrid.py:312-314`),先于任何预算分配。这机械地解释了诊断 Finding 1 的实测不对称:zh 自然措辞(与存储文本同语言共 token)存活过地板但排在 cue 富集竞争者之后(DECISION 0.72、rule 0.68);英文探针塌缩到 2 个 0.69 的 vector 命中,其余全部被地板砍掉。**"sparse 拖拽把跨语言候选压到 0.45 地板之下"是承重的未验证步骤**——由融合公式+诊断实测推出,不是逐分量 live 探针。校准切片(W1)先测它;**[UNVERIFIED — 标记,冻结前 W1 实测]**。

**(d) #447 已落的 S1 为什么不关本 issue。** dedup(`assemble.py:325-357`)释放花在字节级重复 chunk 上的预算,可能帮助 zh 自然措辞情形的尾部准入,但它不改相似度、不改 cue overlap、不改地板——跨语言与改述缺口原样未动。W1 量化残余。

结论:失败不是"排序权重错"、不是"缺别名条目"。是 (i) tokenization 不对称把两侧词汇桥面清空,(ii) 语义通道的相似度公式把一个结构性单语的词汇分量平均进了准入地板读的那个分数。

## 2. 机制选项(逐项复查 S2 三阻断项)

S2 三阻断项:(B-1) 零实体探针使实体驱动的机制够不着;(B-2) 共享 `CueExtractor.extract` 的插入点会在捕获路径改写存储 cue;(B-3) precision 基 `_ratio` 让查询侧加非匹配 token 反而降分(实测复现:q=(进度,账本) vs stored(全量,账本):0.500 → 加 progress、ledger → 0.250)。

- **M1 — 查询侧别名/规范扩展喂 beta(S2 重做)。否决。** B-1:英文探针实体为 `[]`。B-3:稀释算术如上。B-2:`extract()` 内任何插入都会在捕获路径触发(`memory.py:945`、`app.py:369`)。即上一波撤回的方案,列全起见。
- **M2 — 捕获侧规范打标 + 重处理存量行。否决(作为主案)。** 重打标存量 chunk(或重跑 import)改写已存数据的 cue 元数据——约束禁止,且 live import 出范围。只标未来捕获无法满足 AC("已存储的状态事实")。R-1 认可"capture-side canonical tagging"作为本门方向;它仍是合法的未来切片,但对 AC 非必需(与 `s2-path-decision.md` §5 同理),为最小化约束面不进推荐包。
- **M3 — store driver 相似度融合改造(`lancedb_embedded.py` 里 `max(dense, fused)`)。可行备选,非主案。** 无词汇表、无额外 embed 成本地结构性修复跨语言地板塌缩;逐行相对现 fused 值单调不减。代价:对每次查询是全局评分语义变化(任何 sparse>dense 处同语排序都动),需要对 synthetic embedder 下钉分数的 hybrid/storage 测试做大面积重钉;决定性的是——其跨语言效果 **在 CI 中不可确定性测试**(synthetic embedder 的 dense 相似度是整段文本的 hash,`synthetic_embedder.py:39-94`,zh-vs-en dense 相似度是噪声;CI oracle 只能测公式,测不出效果)。留作校准(D1/D2)表明桥够不着 oracle 时的明文应急案。**M3 的启用是独立设计门:显式修订禁触清单(hybrid.py/driver 评分冻结)并重跑本 QA;绝不是批内补丁,绝不是实现期决策。**
- **M4 — graph 轨道桥。否决。** 依赖 dream/refinement 的节点构建(出范围)和 extractor 不对称清空的精确实体种子;它继承它本要修的每个阻断项。
- **M5(选定)— 读侧双件套桥:存储侧 cue shadow + 词汇表增强第二探针 + 单调 max 合并。** §3 详述。三阻断项复查:
  - B-1(零实体探针):glossary 匹配器作用于**原始查询文本**,不作用于 extractor 实体——采纳 QA round 1 对 B-1 的最小修复("在 RAW 查询文本上匹配别名短语")于 search 级。英文探针逐词命中 import→导入、ledger→账本、history→历史;zh 探针按子串命中 导入→import、记忆→memory。extractor 零实体与机制无关。
  - B-2(捕获路径改写):**什么都不写**。shadow 在读时从不可变 verbatim 文本重算;捕获路径(`memory.py:945`、`app.py:369`、`stamper.py:410-454`)不动;`cues.py` 只新增一个加性纯函数,`extract()` 行为不变。装饰器只在 recall 路径包 vector store(`memory.py:756-760`——经 grep 验证是产品内唯一 `retriever.recall` 调用点;`remember` 及相邻捕获、`_focal_scan` `memory.py:1763-1830`、`snapshot_read` 消费者全部不经过)。
  - B-3(非匹配新增降分):两条结构性保证。(i) shadow 只增富**存储**侧:在 `_ratio(q,s)=|q∩s|/|q|` 下 `|q|` 查询期固定、`|q∩s|` 对 `s` 单调不减——增富对每个候选只能抬分或持平,永不降分。beta 的查询侧本设计完全不碰。(ii) probe-2 逐 chunk 按 max 合并:双探针都取回的候选取 `max(probe-1 相似度, probe-2 相似度)`,否则取自己探针的相似度——任何候选相似度不降,probe-1 独占候选字节级不变。加进 probe-2 **文本**的 glossary token 对 beta 无任何影响。

## 3. 选定机制 M5 — 精确设计

### 3.1 新模块与插入点

| 产物 | 类别 | 内容 |
|---|---|---|
| `src/mnemoseed_local/retrieve/bridge.py` | 新,纯,无 IO | 双语领域 glossary(冻结的 zh↔en 规范对元组)、确定性 raw-text 匹配器(最长匹配优先、首次出现序、去重、上限 8 term)、`augment_query(text) -> str \| None`(无 glossary 命中返回 `None` → 单探针行为,与今日字节一致)。无时钟、无随机、无网络。 |
| `src/mnemoseed_local/retrieve/cues.py` | 仅加性 | 一个新纯函数 `lexical_shadow_tokens(text, config) -> tuple[str, ...]`:extractor 实体(经现有 `extract()`)+ 小写 ASCII 实义词(词边界 `[a-z][a-z0-9_-]{2,}`,casefold,减现有 `_CAP_STOPWORDS` :392-488,上限 24 常量)。`extract()` 本体不动——捕获路径永不调用新函数。 |
| `src/mnemoseed_local/retrieve/readview.py` | 新 | `RecallVectorView`——实现既有 `VectorStore` protocol(`storage/ports.py:1005-1046`)的读侧装饰器,包真实 store;`search` 保持精确签名 `(dense, sparse, filter, top_k)`(`ports.py:1039-1046`),port 零改动。只覆写 `search()`:(1) 内层 probe-1 search 用传入的 dense/sparse(不重算);(2) 若绑定查询文本的 `augment_query` 触发,用注入的 embedder embed probe-2 文本并以同一 `ChunkFilter`、`top_k` 跑内层 probe-2 search;(3) 逐 chunk `max(相似度)` 合并,按 (相似度降序, chunk_id 升序) 排序,截断到 `top_k`;(4) 用 `lexical_shadow_tokens(chunk.text)` 增富返回 stamps——**增富纪律:绝不改写从内层 store 收到的任何对象;每个增富命中是新建 `ChunkStamp`:`stamp.model_copy(update={"cues": stamp.cues.model_copy(update={"entities": enriched_list})})`;从内层 store 收到的对象被视图原样丢弃。** LanceDB driver 恰好每次调用返回新 stamps(`lancedb_embedded.py:362`),但契约不依赖这一点——CI fake 与未来 driver 可能共享对象,copy 纪律 + §4.2 增富同一性断言使共享安全成为承重行为。仅内存,永不持久化。其余 port 方法(`snapshot_read`、`list_chunks`、`near_duplicate`、`upsert_*`…)原样透传。 |
| `src/mnemoseed_local/daemon/memory.py` | 一处接线 | `MemoryService.__init__` 只构造一次不可变无状态策略:`self._bridge = BridgePolicy()`(冻结 glossary 元组 + 常量;无 per-call 状态)。`recall()`(:742)内、retriever 调用之前,按调用构造视图并以 per-call store 实参传入(retriever 本就按调用收 `vector_store=`,`hybrid.py:190-215`;现实参点 `memory.py:760`):`RecallVectorView(inner=self._stores.vector, embedder=self._stores.embed, bridge=self._bridge, observability=self._observability, query_text=query)`。`recall()` 其余行不变;assembler、meta store、graph store、activation、residue 路径全部留在原始 store。**v1 的 service 级 `self._recall_view` 构造删除——按调用实例无共享可变状态(`memory.py:679-681` 的共享服务隐患不可能给查询槽起别名)。** |
| `src/mnemoseed_local/daemon/observability.py` | 仅加性 | 两个小计数器方法(§3.6;与 :62-75 同锁纪律)。 |

装饰器实现既有 port——不改 port、不改 schema、无存储写、无 config 键(冻结先例:config 不加新可调项)。

### 3.2 端到端数据流

1. `POST /memory/recall`(`memory.py:2873-2886`)→ `MemoryService.recall`(:742)。`self._cues.extract(query, ...)` :755 **不变**——beta 的查询 cue 与今日完全一致。
2. `augment_query(query)` 从按调用构造的视图所绑定的文本计算(绑定点=§3.1 接线行的 `recall()` 局部构造;**不存在 service 级视图,不存在可变 current-query 槽**)。计算是惰性的:首次 `search()` 调用时视图推导 probe-2 文本或 `None`;`None` ⇒ 单探针直通,与今日字节一致。
3. `_vector_track`(`hybrid.py:263-335`,不改动)embed 查询文本并调 `view.search(dense, sparse, filter, top_k)`。视图跑 probe-1(与今日相同的内层 search),再跑 probe-2(embed 增强文本,同 `ChunkFilter`、同 `top_k` 的内层 search)。
4. 视图对两探针命中列表的并集做逐 chunk `max(相似度)` 合并,按 (相似度降序, chunk_id 升序) 排序,截断到 `top_k`,然后用 `lexical_shadow_tokens(chunk.text)` 经 R4 的 copy 纪律增富 ≤ `top_k` 条返回 stamps(`vector_top_k=20` 时 ≤ 20 行,`hybrid.py:102`)——仅内存、永不持久化,且**只在截断之后**(不在 `candidate_k` 预滤窗 `lancedb_embedded.py:345-351` 之上)。
5. Hybrid 原样消费合并命中:地板 :312-314 作用于合并相似度;rescue 带 :298-311 作用于对 shadow 增富后存储 cue 算出的 `_chunk_cue_overlap`;`_ratio` 算术不动。assembler、预算门、冲突配对、freshness guard 不变;`assemble._probe` 的 `snapshot_read`(:485-492)经视图透传不增富——freshness 语义字节级稳定。

**存储 vs 查询期计算**:存储 = verbatim 文本、cues、embeddings、provenance——全不变,零改写。查询期计算 = probe-2 embedding(glossary 触发时多一次 embedder 推理)+ 一次额外 ANN search + shadow 重提取 + ≤ 20 行返回 stamps 的 `model_copy`。无跨调用缓存;同输入确定性。

### 3.3 为什么不可能降任何东西(单调性契约)

- `_ratio` 分母是查询集(固定);shadow 增大存储集 ⇒ 每个候选 beta 的 entity 分量单调不减。tool、context 分量不动。
- probe-2 合并逐 chunk 取 max ⇒ 每候选相似度不减;probe-1 独占候选字节级不变。
- **位移不变量(按并列情形重述——字节级重复 chunk 带相同相似度是实测现实,诊断 Finding 2,四个 chunk 同为 1.296878186645345 类等分)**:合并排序是全序键 (相似度降序, chunk_id 升序)——与 `_merge`/`_select`/`_dedup_exact` 已用的纪律相同(`hybrid.py:399-412`、`assemble.py:375-384`、:340-347)。probe-1 成员离开合并 top-k,当且仅当某候选在该 (相似度, id) 键下严格排在它前面;等相似度时 id 更小者胜,与哪个探针取回无关。位移因此由全序决定,不由相似度单独决定;确定性的,并由含等相似度/更小 id 并列情形的位移性质测试钉死。
- 推论:每个现役候选的融合分只升或持平;新候选只能靠在全序键下**严格排前**进入——rescue 是严格竞争性的。
- rescue 带增宽:shadow 增富可把主地板之下的显式 pin 推过 `rescue_cue_min`(`hybrid.py:307-311`)。这是 R4 对齐行为(强 cue 取回弱痕迹),专测钉死;无 shadow 命中时零变化。

### 3.4 查询文本绑定与 fail-closed 行为

绑定点(规范性):视图按 `recall()` 调用在 §3.1 接线点构造,接收 `recall()` 本就持有的实参 `query_text=query`;port 签名永不携带文本(`ports.py:1039-1046`),hybrid.py 永不改(`hybrid.py:190-215`)。`augment_query` 在首次 `search()` 惰性运行;无 service 级视图、无可变 current-query 槽,`MemoryService` 的 threadpool/async-lane 共享(`memory.py:679-681`)不可能竞态。

- `augment_query` 返回 `None`(无 glossary 命中)→ 单探针;视图直通;recall 行为与现 main 字节一致;`bridge_probe2_fired_total` **不**递增。
- probe-2 embed 或内层 search 抛错 → 视图捕获、`bridge_fallback_total` 递增、记 warning、原样返回 probe-1 结果集(fail-open 到现状;recall 永不因桥而破)。
- shadow 提取抛错 → 该结果集跳过增富(返回原始 stamps),`bridge_fallback_total` 递增;recall 永不失败。
- 空查询文本 → 无 glossary 命中、无 probe-2、无 shadow(extractor 输出与今日一致)。
- 无 config、无 IO、除 recall 路径已有的 embedder 外无模型调用——token 经济尊重(glossary 是静态表,不是 LLM)。
- observability 可为 `None`(service 构造时可不带,`memory.py:657`):所有计数器调用 None-guard 空操作;recall 行为绝不依赖 observability。

### 3.5 成本 / 时延预算影响

- 无 glossary 命中的同语查询:零新增成本(直通)。
- 桥触发查询(zh 与 en——owner 的主导用法):vector 轨道 +1 embedder 推理 +1 ANN search。绝对毫秒数 **[UNVERIFIED — W1 实测]**;冻结时钉实测数与 recall 路由 p95 前后对比。超预算应急:收窄触发(probe-1 返回命中数低于固定常数才发 probe-2)——设计门决策记在校准处,绝不是实现期调参。
- shadow 重提取:对返回候选行的有界正则;零新增 store 读。
- 按调用视图构造(每次 recall 一个小对象)、惰性单遍 glossary 扫描、§3.6 计数器递增、≤ 20 行返回 stamps 的增富 `model_copy`,相对实测 embed+search 预算皆可忽略;主导成本仍是桥触发查询的 probe-2 embedder 推理,W1 冻结时实测钉死。

### 3.6 桥的可观测性(fail-closed 必须可观测)

仅日志的 fallback 会让 AC 在生产中静默失效。视图携带 service 的 `Observability` 实例(`memory.py:657`、:662),在既有 observability 面上维护两个线程安全 since-boot 计数器(`daemon/observability.py:61-75`,先例:T2 recall 注入计数器):

- `bridge_probe2_fired_total` — 递增一次,当且仅当 glossary 触发且 probe-2 search **成功完成**;已尝试但回落只计 `bridge_fallback_total`(尝试数 = fired + fallback,信息对恰好互补)。每次 recall 至多一次,不按 track 线程重试计。(round 2 NEW-1:与 §3.6 W4 断言及 §4.2 条 4 对齐,撤回 v2 初稿"或已尝试并回落"措辞。)
- `bridge_fallback_total` — 任何 fail-closed 降级递增(probe-2 错误、shadow 错误)。

实现:`Observability` 上两个小加性方法(计数器读/递增,与 :62-75 同锁纪律);视图 None-guard 调用。这些是 observability 面的数字,**不是** recall wire 字段——不加不改任何冻结 wire 字段(`memory.py:840-852` payload 不动)。W4 断言两个计数器:强制回落注入使 `bridge_fallback_total` 递增且 `bridge_probe2_fired_total` 保持注入前值;glossary 触发的正探针使 `bridge_probe2_fired_total` 递增。

## 4. 测试 oracle

两层 oracle,对齐仓库既有 CI/本地分层(CI 用 synthetic embedder,500MB 模型不得下载——`synthetic_embedder.py:1-6`、`tests/test_eval_recall_harness.py:64`;真模型效果跑 eval rig——`src/mnemoseed_local/eval/recall_harness.py:1-16`、`src/mnemoseed_local/eval/rescue_harness.py:215-240`)。

### 4.1 固定 zh/en 效果 oracle(本地,真实 embedded preset——AC 门)

**Fixture 捕获流(真实漏斗,无合成 cue)。** 全新 rig root(fail-loud fresh-root 契约,`src/mnemoseed_local/eval/rig_freshness.py`)、临时 `MNEMOSEED_LOCAL_HOME`、spare port(绝不 7788);起真 daemon(`daemon.app.create_app`,`src/mnemoseed_local/eval/recall_harness.py:14-15`);经真实 ingest 漏斗播种(`POST /ingest` → `_daemon_write_context` `app.py:348-381` → `StampWriter._assemble` `stamper.py:410-454`——与给 owner `opencode-chat` chunk 打标的同一漏斗,source 标签 `f"{turn.host.value}-chat"` :432)。播种语料(双语,镜像诊断 Finding 1 家族):

- 目标状态事实文本:zh/en 混排执行状态,如「codex 全量 import 后台跑 15/468,账本从 161 起,340 轮 rollout settle 完成」族;
- DECISION 干扰项(「已决定把一部分 codex 的对话记录导入 mnemoseed 记忆…」族);
- past-fact rule 干扰项;6-10 条无关双语噪声 chunk(entity-miss 与 entity-collision 类,循 `src/mnemoseed_local/eval/recall_materials.py` 惯例)。

**Oracle 从真实捕获的 cue 实体推导。** 播种后 harness 读回存储 chunk,**断言每条 chunk 的存储 `cues.entities` 等于测试内现算的 `CueExtractor.extract(text).cues.entities`**——oracle 钉在真实 extractor 实际打的东西上(目标的期望标即 `['全量','账本']` 类),查询实体集同样测试内现算,不手写。任何地方无合成 cue store。

**固定查询与钉死断言**(top_k=5、budget_tokens=800 默认):

- zh 自然:「之前有没有把一部分 codex 的对话记录导入 mnemoseed 记忆里」 — GREEN:目标 chunk id 在 5 条服务条目中(kind=chunk);RED 基线在 main:目标缺席(W1 实测并钉住精确基线排序)。
- en 自然:"did we already import part of the codex chat history into mnemoseed?"(不含 rollout/ledger/15/468/340)— 同 GREEN/RED。
- 回归控制:(a) 精确 token zh 查询(Finding-1 的 1.02 类查询)仍以 top-1 取回目标——工作情形零回归;(b) 无关查询(「the history of rome」、「progress of the disease」、无关 zh 句)不取回目标、不引入新噪声进 top-5;(c) **预算指标回归范围(round 2 NEW-2 收窄)**:glossary 匹配运行于原始查询文本,故 §4.1 全部探针(含精确 token 探针与无关探针)都会触发 probe-2,而 §3.3 全序位移不变量在这些查询上**显式允许**服务集变化——字节一致断言只属于**无 glossary 命中查询**(那里 §3.4 保证视图字节级直通)。落法:(i) 新增专项**无 glossary 命中回归探针**(zh/en 各一条,不含冻结 glossary 表的任何 term;W1 校准时以实际冻结表实测确认零命中并钉死探针文本),在其上断言服务结果集与预算指标(`dropped_count`、`tokens_used`)与现 main 字节一致——按构造成立,是 §4.2 条 6 的 W4 级孪生;(ii) 在触发 glossary 的控制查询上,只断言机制结构性保证的结论——精确 token 探针:目标保住 top-1((a) 已钉);无关探针:目标缺席((b) 已钉)——服务集差值与正探针同样冻结时实测记录,不钉死。(v1 的"不恶化"与 v1 §7.4 的"钉为不受影响"措辞均因自相矛盾而撤回。)(d) `hybrid.py` config 钉原样(`tests/test_retrieve_hybrid.py:829-831`)。

### 4.2 CI 机制 oracle(synthetic embedder,确定性 pytest)

synthetic embedder 下跨语言 dense 相似度是 hash 噪声,所以 CI 层证明**安全包络与机制**,不证明语言语义:

1. 单调性性质:随机 cue 集下,增富存储侧永不降低 `_ratio` 推导的 beta(shadow 开/关对照)。
2. 不降分性质:视图安装后,每个 probe-1 候选相似度不变或升高;probe-1 独占候选字节级不变。
3. union-rescue RED→GREEN 机制 fixture(硬化):fixture 文本在测试内**字节级钉死**;fixture 在自身内部断言校准后的合并相似度阈值(计算目标的合并相似度,断言它以精确钉死值清过地板常数 0.45——任何 embedder/driver/hash 漂移大声失败而非静默通过);总播种 chunk 数严格低于 dense-ANN 候选窗(`candidate_k = max(top_k*4, top_k+50) = 80` @ `vector_top_k=20`;`lancedb_embedded.py:345-351`),hash 噪声永远不能把目标排除出预滤。main 上 RED(目标 sub-floor、缺席),装视图后 GREEN(目标被服务)。仅机制 fixture——语言语义在 §4.1 本地 oracle。
4. fail-closed 注入 + 可观测性断言:augmenter/embedder/内层 search 抛错 → recall 返回 probe-1 结果且 `bridge_fallback_total` 恰好递增一次而 `bridge_probe2_fired_total` 不动;shadow 抛错 → 返回原始 stamps,同计数器行为。全部记日志,无一致命,`observability=None` 时行为一致(None-guard 路径被演练)。
5. 增富同一性断言(新):spy 内层 store 返回**共享** stamp 对象;增富 `search()` 后断言 (a) 每个返回命中的 stamp 是带增富 `cues.entities` 的独立对象,(b) 每个内层 store stamp 对象前后字节一致(零改写),(c) 同输入重复 search 产出相同输出(共享下确定性)。
6. 触发诚实性:无 glossary 命中 → `search` 行为与 main 字节一致(hash 命中列表对照);QA round 1 I-1 的负控制(「the history of rome」经 glossary 得 历史——断言池增长有界、无无关目标浮现,地板有界)。
7. shadow 单元测试(`tests/test_retrieve_cues.py` 加性):extractor 实体保留、小写 token ≥3 字符拼入、stopword/短 token 排除、上限遵守、确定性。
8. `snapshot_read` 透传钉死(freshness 探针不受影响)。

## 5. 理论锚

**复用(既有注册表条目;不重审其表述):**

- **R4 — Tulving & Thomson (1973), encoding specificity**(`docs/zh/design/REFERENCES.md:20`;注册于 `docs/zh/design/00-overview-and-philosophy.md:161,:234`、`docs/zh/design/03-storage-and-retrieval.md:99-101,:235`;rescue 带引于 `hybrid.py:299-306`)。已验证规律:取回成功取决于取回线索与编码情境的重叠。本设计落的设计规则:增宽既有 overlap 算术读的 cue 面(存储侧 shadow)+ 增加一条取回路径(probe-2)——评分理论与 beta 语义不动。
- **R47 — Tulving & Pearlstone (1966), availability vs accessibility**(`REFERENCES.md:99`,现 scope 于 design 09)。已验证规律:存储中可得的信息可暂时不可及;合适线索恢复可及性。设计规则:目标事实可得(精确 token 下 0.84-1.02 可取)但自然措辞下不可及;桥供给合适的取回路径而非重编码记忆。注册动作:把 R47 的 对应本篇 覆盖扩展到新设计文档。

**候选新条目(仅在 Crossref 核验后注册;核验前 ⚠️):**

- **R-NEW 候选 — 跨语言 / 翻译等价 cue 访问。** 已验证规律(双语记忆):以一种语言编码的概念经另一语言的翻译等价线索保持行为可达(translation priming / cross-language semantic access)。注册前必须核验的候选来源——不得仅凭本提案引文注册:Kroll & Stewart (1994), *Journal of Memory and Language*(不对称跨语言访问),和/或 Francis (1999), *Psychological Bulletin*(共享语义表征综述)。精确卷页 **[UNVERIFIED]** ——工程批必须 Crossref 核验作者/年份/出处并按维护规则以诚实状态注册(`REFERENCES.md:144`)。设计规则:有界、确定性的翻译等价 term 集可在 search 面增强查询,仅取回侧、单调(max 合并),永不改写存储内容。
- 诚实退路:核验失败则 glossary 作为纯工程机制落地(I 型工程条目,`REFERENCES.md:133-134` 的 `I1/I2` 模式),无理论声明;设计立在 R4 + R47 上即足,机制不依赖锚。

**明确不借:**BIA+/RHM 作为架构(是模型不是规律);关键期/习得年龄神话;学习风格;Mozart 效应;大脑 10%;阈下编码;"跨语言 embedding 证明人类共享语义"(工程巧合);LLM 查询改写(plumbing 无模型调用——token 经济);间隔/重复流行声明(承自 #447 v2 不借清单);语义 embedding 模型替换或更换;对 R4/R47/R48 表述的任何改动。不重审既有锚。

## 6. 滚动切片与禁触清单

| 切片 | 内容 | 门 |
|---|---|---|
| W1 | 校准 replica:bridge eval 材料 + harness(rig 惯例循 `src/mnemoseed_local/eval/recall_harness.py` / `rescue_harness.py`;**新文件仅 `src/mnemoseed_local/eval/` 下 + tests**)。**门范围:仅 eval-harness/test 新增;`retrieve/`、`storage/`、`daemon/`、`capture/`、`config` 零改动。** 在现 main 上以真实 embedded preset 跑;对固定 zh/en oracle 查询逐候选实测 dense/sparse/fused 相似度与 top-k;验证或证伪 sparse-drag 地板假说;钉 RED 基线与存储 cue 打标断言;从实测输出裁决 D1(假说成立)与 D2(词汇表种子集、append-vs-replace) | 设计工件 + eval/test 新增;零产品改动 |
| W2 | 桥核心:`retrieve/bridge.py` + `retrieve/readview.py` + `memory.py` 接线 + `observability.py` 加性计数器 + CI 机制测试(§4.2 条 1-5、8) | TDD RED→GREEN;门禁全净 |
| W3 | Shadow:`cues.py` 加性 `lexical_shadow_tokens` + 视图增富 + CI 性质测试(§4.2 条 1、7) | TDD RED→GREEN |
| W4 | 效果 oracle eval 门:W1 harness 以 W2/W3 重跑——固定 zh/en oracle 绿(双语言目标进 top-5)+ 全部回归控制绿 + §3.6 两计数器断言绿;eval 报告工件 | 本地 eval 跑,真 preset,隔离 home |
| W5 | 文档 + 注册:`docs/zh/design/` 设计文档、PRD brief、`REFERENCES.md` 编辑。**REFERENCES.md 编辑范围:仅覆盖列(对应本篇)注释——任何注册锚的引文文本、状态标记、声明文本零改动;R-NEW 候选条目仅在 Crossref 核验通过后加入,核验前不出现在注册表** | doc-only |

**工程批禁触:**`retrieve/hybrid.py`(冻结;钉在 `tests/test_retrieve_hybrid.py:829-831`);`CueExtractor.extract` 的行为(捕获共享——`memory.py:945`、`app.py:369`);`capture/stamper.py`;`daemon/app.py`;任何 storage driver(`lancedb_embedded.py`、`sqlite_*.py`、`bge_m3_onnx.py`、`synthetic_embedder.py`);`config.py`(不加键、不改默认);`schema/*`;dream/refinement;`hosts/*`;MCP wire 表(不加字段——coverage 字段含义不变,只有其**值**可动,回归控制钉住);已安装 runtime、7788 端口、任何 live import/daemon PID(测试/live 隔离指令 2026-08-31);任何 shadow 或增重工件的捕获路径写入。**加性面例外:`daemon/observability.py` 仅加性两个计数器方法(§3.6)。**

## 7. 开放风险(对抗设计 QA 应最猛攻击处)

1. **sparse-drag 地板塌缩假说 UNVERIFIED**(§1c)。代码推导,非 live 实测。攻击:强制 W1 在任何冻结前于 fixture 上逐分量实测;若地板不是 en 探针的绑定者,桥的目标移位,门重跑。
2. **zh top-5 钉无解析保证。** 机制单调抬目标相似度,但对 cue 富集竞争者(DECISION chunk)的排序是经验的。攻击:要求校准产出实测排序;冻结后任何常数改动(词汇表 term、append-vs-replace、触发器)按门重跑处理,不按补丁处理。
3. **glossary 过扩展污染**(QA round-1 I-1 类):无关查询中的命中 term 改变 probe-2。攻击:负控制(「the history of rome」「progress of the disease」)+ 地板有界的池增长;逐一种子 glossary 对实测正当性审查或移除(D2)。
4. **预算指标行为(范围化,不全局钉死)**:回归控制查询(精确 token 探针、无关探针)必须与 main 字节一致,含 `dropped_count`/`tokens_used`;正 zh/en 探针合法移动这些指标——实测差值冻结时记录,永不钉死(见 §4.1c)。`vector_hits` 与 `floor_dropped` 值会动(命中更多、计入更多 sub-floor probe-2 行);字段定义不变、不加 wire 字段(`memory.py:840-852`)。#447 Q1 可观测(dedup fixture 上的预算丢弃指标)必须钉为不受桥影响。
5. **轨道容量位移语义(全序形式)**:合并截断只能在 (相似度降序, chunk_id 升序) 全序(§3.3)下挤出 probe-1 成员——含等相似度/更小 id 并列情形,确定性的且性质测试钉死;桥的 fail-closed 降级经 §3.6 计数器(`bridge_fallback_total`/`bridge_probe2_fired_total`)可观测,AC 的静默生产回归不可能藏在日志行后面。
6. **时延**:桥触发查询多一次 embed 是真实热路径成本——要求冻结时给实测前后 p95。
7. **CI 机制 fixture 边缘性**:fused ≈ 0.5 + 0.5·dense 噪声对 0.45 地板——fixture 字节级钉死 + 阈值内嵌断言(§4.2 条 3)后漂移大声失败;总播种数低于候选窗(80)排除预滤排除目标。
8. **装饰器包围**:grep 验证 `memory.py:756` 是产品内唯一 `retriever.recall` 调用点(QA round 1 复核确认:其余命中是 eval rig 调 `rig.recall`,不同面)、`snapshot_read`/捕获路径永不经过视图。

## 附录:并行批次冲突检查(solution-architect 已复核,QA round 1 确认)

Task A(#444/#445,config 写原子性)已于 2026-11-21 经 PR #455 合并落地;其面(`src/mnemoseed_local/configwrite/**`、`tests/test_configwrite_*`)与本设计无共享文件。本设计的面为 `src/mnemoseed_local/retrieve/{bridge.py(新), readview.py(新), cues.py(加性)}`、`src/mnemoseed_local/daemon/memory.py`(`__init__` 一行策略构造 + `recall()` 一处 per-call 接线)、`src/mnemoseed_local/daemon/observability.py`(仅加性两计数器)、`src/mnemoseed_local/eval/{bridge_materials.py(新), bridge_harness.py(新)}`、tests `tests/test_retrieve_bridge.py(新)`、`tests/test_retrieve_readview.py(新)`、`tests/test_retrieve_cues.py(加性)`、`tests/test_eval_bridge_*.py(新)`。**面不相交,无 test-oracle 碰撞。**

## 8. 设计门重跑 v3(W1 校准后,2026-11-21)— 现行治理修订

W1 校准(`eval/2026-11-21T11-38-21Z-bridge-calibration.{json,md}`,真实 bge-m3 preset,门禁绿 5391 passed / 23 skipped)触发 §2 M3 预注册应急条件,同时**证伪 M3 原案**并选出实测可行的替代机制。设计 QA round 3 判词 **CLOSABLE(BLOCKER=0 IMPORTANT=3 NIT=2)**,三 IMPORTANT(披露卫生)已并入本节。

### 8.1 不可满足证明(对 k=5 的任何一致单调公式)

W1 记录的服务算术:total = sim + 0.6·|q∩s|/|q| + 0.2·decay(fixture 全部行 decay=1.0,由 floor_dropped=6 与已服务分数算术互证)。zh_natural 探针上,全部五个非目标池成员在 dense 与 sparse 双通道**严格 Pareto 压制目标**(decision 0.8665/0.7982、rule 0.8294/0.6783、zh_host 0.6527/0.2784、en_daemon 0.6151/0.3217、zh_upgrade 0.5698/0.2093 对 目标 0.5527/0.1696)——任何逐行单调非降融合下目标排位 ≥6,**k=5 时无解**。probe-2(查询侧改动,同样移动竞争者行)救不了;beta 零交集、graph 无种子、元数据打分与 LLM 改写被禁——**排名杠杆与准入杠杆在数学上分离,记录实测了两者**:准入杠杆 = gated dense 下界;排名杠杆 = 服务深度。

### 8.2 机制 v3(取代 §2 M5 的 glossary/probe-2 半部、§3.1/§3.4 per-call 绑定、§4.1c no-glossary 字节一致断言)

1. **驱动(W2b)**:`lancedb_embedded.py` 仅 search() 融合分表达式(:355-361)改为调用新模块级纯函数 `_fused_similarity(dense_sim, sparse_sim)`:`sparse_sim is None → dense_sim`(现 None 路径不变);否则 `fused = 0.5·dense + 0.5·sparse`,`sparse_sim > 0.0 → max(dense, fused)`,否则 `fused`。常量 :68、sparse 余弦 :845-856、dense ANN 预滤 :344-352、near-duplicate 路径全部不变。逐行非降(设计门 round 3 独立探针 201,402 点、随批 CI 网格 401×401=160,801 点,均零违例:s=None 与 s=0.0 时与今日相等,s>0 时 max ≥ fused);**门的存在性实证**:无条件 max 会让无关 zh 探针(「帮我把会议室的投影仪预约到明天下午」)以 dense 0.4893 在 rank 4 服务目标——零门消除该灾难性误报。bge-m3 sparse 头对词汇不相交对输出**精确 0.0**(token id 无共享,结构事实非调参阈值);微型非零尾部(0.0001-0.0953)开门 admitted at dense = 已披露的 honest-empty 回归。
2. **深度(owner R-2)**:`assemble.py` 仅 `top_k: int = 5` → `8`(:82)+ docstring :76-77。预算 800 不变。issue #448 done-when 写的是 **top-k**(k=5 从来只是设计 oracle 钉);QA round 3 确认深度 8 不降低 AC,且 grep 证实无任何测试以裸默认构造 `AssembleConfig()`(全部显式传参;daemon 测试 11 条 /memory/recall 全部显式 top_k: 5)。
3. **shadow(W3)保留**:v2 §3.1 的 `lexical_shadow_tokens` + stateless service 级视图(一次构造于 `__init__`;无查询绑定、无 embedder——per-call 绑定的存在理由(probe-2)已删除)+ copy 纪律 + §4.2 条 5 增富同一性断言原样保留;`bridge_probe2_fired_total` 删除,`bridge_fallback_total` 更名 `bridge_shadow_fallback_total`(shadow 失败 → 跳过增富、计数器递增、recall 继续);W4 仅在强制回落注入下断言更名后的计数器。**N-2 义务**:W1 探针上 shadow 零实测效应(无探针查询携带小写 ASCII 实体),保留该面必须同时保留 v2 §3.3 的专用 rescue-band fixture(shadow 把 pin 推过 rescue_cue_min)使机制保持 oracle 覆盖。
4. **单调性契约重述(取代 §3.3 的 probe-merge 语言)**:(i) 驱动 gated-max 逐行非降;(ii) shadow 只增存储侧,beta 非降;(iii) 服务序仍是 (相似度降序, chunk_id 升序) 全序;曾被服务成员仅在全序键下被严格排前的候选挤出(v2 R6 并列语言不变)。
5. **可观测性**:除 §3.6 计数器更名与 probe-2 计数器删除外不变;wire 字段零改动(`memory.py:840-852` 字面 dict,QA 复核)。
6. **hybrid.py 保持冻结**(地板 :312-314 现在读取 gated-max 相似度;权重、beta、rescue 带不动)。done-when 的"除非独立设计门授权"豁免仅对 driver 调用。

### 8.3 披露修正表(QA round 3 I-1/I-3 修正后的 per-probe 服务数差值,gated+top_k=8 vs main)

| 探针 | main | v3 | 备注 |
|---|---|---|---|
| zh_natural | 4 | 7 | 目标 rank 6 ≤ 8,180 tokens(round 4 QA 勘误) |
| en_natural | 4 | 6 | 目标 rank 6 ≤ 8,~160 tokens |
| zh_exact | 1 | **2** | 目标保住 top-1(1.5883);**rule_distractor 以 dense 0.5259/sparse 0.1377 进 rank 2(I-3,须披露)** |
| rome / disease | 0 | 0 | 空(全 gated sim < 0.45) |
| irrelevant_zh | 0 | 4 | 目标**不**被服务(门关:sparse 0.0 → fused 0.2447) |
| no_glossary_zh | 0 | **2**(非 architect 摘要的 3) | noise_miss_zh_pilot 0.7138、noise_collision_zh_host 0.6522(I-1 修正) |
| no_glossary_en | 0 | 1 | en_pilot gated 0.5003(round 4 QA 勘误:初稿误写 0.5703) |

### 8.4 W4b 接受界限(I-2:先于 W2b merge 注册)

真实语料只读副本扫描(隔离 home、绝不 7788、已装 runtime 零触碰),merge 前置门,全部可测:
(i) 修正后的 per-probe 服务数差值全记录;(ii) 任何 no-hit/无关探针不服务目标;(iii) rome/disease 保持空;(iv) 精确 token 控制目标 top-1 保持;(v) depth-8 尾行强化效应实测(`memory.py:807-830` 每条被服务行触发 usage+reinforcement)记录 hit/reinforcement 差值;(vi) `floor_dropped`/`pool_size` 等 metamemory 移动按值变更记录;
真实语料量化界限:**词汇不相交查询(sparse 精确 0.0)永不服务新行**(honest-empty 类保持诚实空);中位 tokens_used 差值 ≤ +25%;recall 路由 p95 ≤ 1.5× main。任一越界 → 不 merge,回到 owner。

### 8.5 R-2 记录

owner R-2(服务深度 5→8)于 2026-11-21 经交互提问征求,未获答复;按会话主指令「推进已定工作」以 architect 推荐项为工作默认继续执行,**W4b 为 merge 前置门、PR 与收尾汇报显著披露、单行可回退(assemble.py:82)**,owner 保留事后否决权。R-2 问答全文与修正披露表归档于本节与收尾记录。

### 8.6 切片重排

W1 COMPLETE(两轮校准并入冻结记录);W2(v2 桥核心)**删除**;**W2b** = 驱动 `_fused_similarity` + 一处表达式 + CI 公式性质测试(穷举 (d,s) 网格:门控 ≥ fused 恒成立、等值分支钉死、None 路径 dense)+ 重钉(`tests/test_lancedb_embedded.py:222-236` 与 `tests/contract/test_contract_vector.py:183-192` 验证不破——QA 算术判定保留;`tests/test_eval_bridge_harness.py:75-81` 的 sparse 恢复代数 `2·fused − dense` 仅在门闭分支成立,改按分支重钉)+ 全套件重跑为深度变更爆炸半径检查;**W3** = shadow(§8.2.3);**W4** = W1 rig 重跑钉 AC 排位与差值;**W4b** = §8.4 语料扫描(merge 前置);**W5** = 文档 + R-2 记录 + REFERENCES 注释(v2 范围不变)。

### 8.7 设计 QA round 3 应攻击清单(已执行,判词 CLOSABLE)

复核要点存档:rank 表独立复算(decay 修正 I-1)、(d,s) 网格:设计门独立探针 201,402 点、随批 CI 网格 160,801 点,均零违例、denylist 逐行核对、零门语义参数无关性论证、depth-8 爆炸半径(T2 focal-scan `memory.py:1763-1830` 分离确认)、R-2 非降 AC 确认(issue 文本未动)、probe-2 删除正当性(+0.0837 仍差 0.0051;en 全负)、W1 文本缺陷两处修正确认。

### 8.8 v3 冻结

本节冻结于 2026-11-21;QA round 3 判词 CLOSABLE(0/3/2,披露修正并入);W1 JSON 的 `brief_freeze` SHA-256 与 v2 冻结字节逐字吻合(校准针对冻结 v2 运行);read-only 模型搬运认证(model_quantized.onnx `0826f8c1…`、tokenizer.json `6710678b…`、sparse_linear.pt `45c93804…`,只读复制自已装缓存)随记录归档。

### 8.9 执行记录(W1-W4b,2026-11-21/22)

- **W4 AC 重跑**(`eval/2026-11-21T14-55-25Z-bridge-calibration.{json,md}`,真实 bge-m3,v3 树):**AC 全绿**——zh_natural 目标 rank 6 ≤ 8(score 0.7527,gated dense 0.5527);en_natural 目标 rank 6 ≤ 8(0.6615);zh_exact 目标 top-1(1.5883)+ rule_distractor@rank2(披露的 I-3 差值);rome/disease 空;irrelevant_zh 目标**不**被服务(零门分支 gated 0.2447 < 0.45——门的干净性在真实驱动上成立);no_glossary_zh 服务 2 行、no_glossary_en 1 行(与 §8.3 修正披露表一致);stored-cue oracle 10/10;聚合延迟 p95 365.3ms(main 基线 348.6ms,+5%)。
- **W4b 真实语料扫描**(`eval/2026-11-21T16-18-15Z-bridge-sweep-main.json`、`2026-11-21T16-41-28Z-bridge-sweep-v3.json`、`...-verdict.json`;基线 worktree `mnemo-448-main-sweep`@53d6561,语料只读副本):**按注册字面 FAIL**——(a) 三条 topical 不相交查询 `honest_empty_stays_empty=true` ×3(v3 服务 0→0,意图层保持诚实空)但 `zero_sparse_signature=false` ×3:真实语料 top 窗(sweep 签名宽 100)内恒有微量 sparse 质量,§8.4 的 "(sparse 精确 0.0)" 类操作化定义在真实语料上类为空(vacuous)——注册规格缺陷,非服务回归;(b) 中位 tokens_used 差值 +291 > +25% 界:main 在多数 paraphrase 查询上服务 0 行(正是 #448 要修的 honest-empty 缺陷),0→744/795/597 为修复生效;界限对 honest-empty 基线算术无意义——注册规格缺陷;(c) p95 1.46× ≤ 1.5× PASS;(d) 每查询 v3_served ≤ 8(=top_k)、tokens ≤ 800 预算,逐查询成立。**按 §8.4 字面条款"任一越界 → 不 merge,回到 owner"执行:本批次停在 merge 前,owner 裁决见收尾记录。**
- **round 4 实现 QA(CLOSABLE 0/2/6,两 IMPORTANT 已修)**:IMPORTANT-2(recall 接线无 CI oracle)→ 已修:`tests/test_retrieve_readview.py::test_service_recall_wiring_rescues_shadow_only_pin` 经真实 `MemoryService.recall` 判别(wiring-revert 突变体被杀,逐字证据归档);IMPORTANT-1(W4 计数器注入义务未在 eval rig 执行)→ **偏差记录**:实质已 CI 覆盖(`tests/test_retrieve_readview.py:171-209` 强制回落注入 + `test_observability.py` 计数器断言),eval rig 不回装;NIT-5 已修(`Observability.snapshot()` 暴露 `bridge_shadow_fallback_total`);§8.3/§8.7/§8.9 记录数字勘误(0.5003、网格点数、签名窗宽)。修复后全套件门禁 **5424 passed / 23 skipped + ruff×2 + mypy 186 files 全绿**。W4b 两条弱服务(zh_gate_checks、zh_hook_watermark)随裁决包呈 owner。merge 仍 owner-gated(§8.4)。
