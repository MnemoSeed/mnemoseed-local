# 设计 460-kernel-recall-remediation v1 - S0 为准绳的三根因召回整改

> 状态：**PROPOSAL / DESIGN_PENDING（无门权）**。DESIGN ONLY；本文件不授权工程、不改产品源码/配置、不碰端口、不写 ~/.mnemoseed-local、不改冻结的 450/451/452 文档与签名查询集。
> 关联：issue #457 后续；owner Option A（org/decisions/2026-11-23-recall-remediation-direction.md）；派单 org/workspace/kernel-first-principles-20261122/remediation-design-batch-brief.md。
> 输入：s0-baseline-run1.md、d2-ratification-decision.md、三份 investigation、competitor-memory-mechanisms.md、first-principles-kernel-gap.md、冻结 450-*/451-*/452-*、产品源码逐字复核。
> 声明：本席 solution-architect（declared model opencode/hy4-flash-free，物理身份未自证）；access 2026-11-23。
> 配套分片：460-kernel-recall-remediation-slices-v1.md。

## 0. 裁决（verdict）与发现

**SHIP-WITH-ADJUSTMENTS** - 派单的 M0-M4 骨架成立，但六个必须调整点（下 I-1..I-6）在冻结前闭合；否则会产生错误实现或契约越界。本轮 PROPOSAL 无门权，工程需另行 start 授权。

| # | 严重度 | claim（若不调整会发生的错误） | 证据 | 最小调整（落实位置） |
|---|---|---|---|---|
| I-1 | IMPORTANT | M1 若按 capture-time 写回 cue 实现，会污染 stored cues 的 provenance/append-only 红线（stored cues 必须与 capture funnel 落盘字节一致） | readview.py:7-8,1035-1057（shadow 明言 capture 路径不调用、store 不改）；cues.py:1038-1040（stored cues 保持 stamper 原样）；s0-baseline-run1.md:29-32 | M1 改为纯读侧双向扩展：查询侧 union 低大小写 shadow + 有界 alias，存储侧经 RecallVectorView 同样的 alias 规范化；永不写回 stamp（§5.1） |
| I-2 | IMPORTANT | 把 lexicon_v1.py/rulesets_v1.py 当语义别名表是范畴错误：前者是情感 VAD 数据、后者是文本 stripper 数据，均非 cue 别名 | lexicon_v1.py:1-15,63-65,546-596（NRC VAD arousal/valence + Lexicon 加载缝）；rulesets_v1.py:1-10（F1 Local Stripper 规则） | 仅借用 Lexicon 的加载缝形态，新建有界数据 capture/cue_alias_v1.py（generic 别名对），绝不从 VAD 词表推别名（§5.1） |
| I-3 | BLOCKER（M2 正确性） | M2 若直接把 reinforce 门控到 consumed_attested，会在宿主无 S1 回传时把无消费行当成未使用，违反 never-infer-from-silence，且 S0 harness 无法验收 M2（harness 不驱动宿主 hook） | memory.py:792,813-836；reinforce.py:90-132；451-*:145-147（语义漂移修复被显式递延为 D5）；450-*:117（每 run pristine 重置，reinforce 写回被清） | M2 增仪表缺席则回退 served 驱动的 fail-open 分支 + 独立排序验收（非 S0）+ 明确只在 S1 落地后启用（§5.2、§5.5） |
| I-4 | IMPORTANT | M3 若照搬 #448 的 zero-sparse 字面门，会在真实语料上 vacuous（W4b 已证伪），且单纯抬分数门会给 B1/B2 造 false-empty | bridge_sweep.py:146-160（gated==0.5*dense 签名）；org/_state.md 的 #448 批次终态条目（关键字 "zero_sparse_signature ×3" / "真实语料 vacuous"；追加写活文档勿用行号）；hybrid.py:99,312-314（唯一现存的 dense 下界 0.45）；s0-baseline-run1.md:24（Q-003 6 个 0.61-0.72 filler 过了 0.45 门） | M3 只借显式失败/非零填充原则，落内容重叠证据门，rescue/pin 旁路，阈值 [未验证] 待独立探针标定（禁冻结集就地调参，§5.3） |
| I-5 | IMPORTANT | 若 alias 表写入冻结查询的整句映射，就是 teaching-to-test：B1/B2 在 holdout 上虚高，oracle 失去意义 | 450-*:106（holdout 纪律）；competitor-memory-mechanisms.md:58（mempalace 自曝末段 teaching-to-test）；s0-baseline-run1.md:44 | alias 条目必须是通用 dev-memory 词汇（禁冻结目标派生工具名变体）；设整句命中冻结 query 为被拒变异（RED）；补非 gating 的 held-out 释义探针（含工具名/大小写变体族，§5.1、§7）；同一禁教学到测试立场适用于 M3 阈值标定（§5.3，禁在冻结集就地调参） |
| I-6 | NIT（升级为 owner 决策） | B2=0.80 是目标还是发船硬门未定；D3 硬门与已 ratify 的 B1/B3 纪律门需分清 | org/decisions/2026-11-23-recall-remediation-direction.md:11-14；s0-baseline-run1.md:12-19 | 列为 OD-3：整改后 B2 数值是否硬发船门，或重新 ratify（§10） |

## 1. Steelman（目标与核心决定）

目标：把已 capture 约等于可找回的错误信条替换为以冻结 S0 harness 为准绳的可证伪闭环；针对三个已验证根因（非假设）做第一性原理整改，使内核在 D3 硬门下由不可发船转为可裁决。

核心决定：保留 M0（oracle 接线）-> M1（线索对称，读侧有界扩展）-> M2（消费驱动强化，S1 依赖、独立排序验收）-> M3（honest-empty 内容证据门）-> M4（全量重跑 + re-baseline）五片骨架；不新增代理指标、不放宽任何 bar、不把评测代码引入运行时。M0 无产品行为变更；M1-M3 为真实产品行为变更，各自独立 allowlist 与验收；M2 在 S1 落地之后才启用。

## 2. 三根因 -> 机制映射（3 行，绑定 file:line）

1. R-cue（线索失配）-> M1：s0-baseline-run1.md:29-32 目标 039d7d2... 的 stored cues「状态已记/当前状态/全量」与自然查询「把 codex 对话记录导入 mnemoseed 记忆里」几乎不重合，措辞更近的 4fff4366... 抢 rank1；机制根在 cues.py:896-1005（查询侧只收特殊形状 ASCII，普通 codex/mnemoseed 不入 entities）、readview.py:87-95（存储侧 shadow 只加小写 ASCII 词、无跨语言别名）、hybrid.py:534-538（beta entity 0.6 只做 casefold 交集）。M1 用读侧对称 shadow + 有界跨语言别名抬高正确 chunk 的 cue_overlap，不动 store。
2. R-decay（衰减恶性循环）-> M2：s0-baseline-run1.md:30 目标 decay_weight=0.836（从未 Recall->未强化->衰减），rank1=1.0；强化主路径由被服务驱动（memory.py:792 _record_hits -> :813-836 -> reinforce.py:90-132 对每条 served entry 回弹 min(1.0, w+0.1)），与真实消费解耦（451-*:32）。M2 依赖 S1 的 consumed_attested 信号（451-*:61,97），把回弹门控到有消费证据的条目，served-but-unused 只计 usage 不回弹。
3. R-empty（honest-empty 未强制）-> M3：s0-baseline-run1.md:24 Q-003（氦三）返回 6 个 0.61-0.72 弱相关 chunk；唯一现存 dense 下界 min_similarity=0.45（hybrid.py:99,312-314）对弱相关但非全不相干无能为力，且 assemble 的 honest-empty（assemble.py:31-33,188-197）只在零候选时触发。M3 用确定性无真实匹配内容证据门在装配末端返回空，泛化 #448 的显式失败原则而非其 vacuous 字面（bridge_sweep.py:146-160、org/_state.md 的 #448 批次终态条目（关键字 "zero_sparse_signature ×3"；追加写活文档勿用行号））。

S1 前置声明（硬）：S1 仪表设计虽已冻结于 451-*，但 S1 自身需要独立的 owner start 授权（本批未授权，见 remediation-design-batch-brief.md:8-9,26 与 org/decisions/2026-11-23-recall-remediation-direction.md:20-22）。M2 依赖 S1 产出的 consumed_attested 台账行；S1 未落地前 M2 不得启用，也不得以 served 冒充 consumed。本轮 DESIGN ONLY，M0-M4 均未获工程授权。

## 3. 理论锚（严格分离：已验证规律 -> 设计规则；实现机制另列）

| 锚 | 已验证规律（来源，REFERENCES.md） | 设计规则 | 实现机制（非理论，本批产物） |
|---|---|---|---|
| R4 编码特异性（Tulving & Thomson 1973，REFERENCES.md:20 已核验） | 检索成功取决于检索线索与编码时线索的匹配，而非痕迹强度本身 | 取回执行态记忆的首要义务是让查询线索覆盖其编码语境，不是抬高全局分数 | M1 查询侧/存储侧读时对称 shadow + 有界别名交集（cues.py/readview.py/cue_alias_v1.py） |
| R47 可用不等于可及（Tulving & Pearlstone 1966，REFERENCES.md:99 已核验） | 信息在册（available）不等于当下可提取（accessible）；合适线索恢复可及性 | verbatim 已入库不等于可召回；不得用存储完整性代替可及性判据 | M1 的 cue 可及性扩展示例；S0 以 target 是否 served 而非是否入库判定（450-*:128） |
| R23 ACT-R base-level（Anderson & Schooler 1991，REFERENCES.md:49 已核验） | 可用性随使用频度/近因的环境规律变化 | 排序动力学必须由真实使用事件驱动，仪表须先能分辨 served 与 consumed | M2 门控 Reinforcer.record_hits 到消费证据（451-*:43） |
| R28 提取诱发遗忘（Anderson, Bjork & Bjork 1994，REFERENCES.md:59 已核验） | 提取会压制竞争者并强化被提取项；注入本身不应算强化 | 注入不等于强化：served-but-unused 不得回弹；无消费证据不得推断已用 | M2 回弹门 + S1 的 unknown 语义（无 consumed 行既非 used 也非 not_used，451-*:44,101） |
| R7 来源监控（Johnson et al. 1993，REFERENCES.md:23 已核验） | 来源判别天然易混淆，写入侧 provenance 比事后归因可靠 | 消费证据只追加、只指针、带 source/method，不作 verdict；原有 cue/provenance 不被读时扩展改写 | M1 读时 model_copy（readview.py:95）；S1 append-only 台账（451-*:79）；M3 不改 stamp |

明确不借用（not borrowed）：①每次载入都会强化记忆的流行说法 - 与 R28/TA-6 直接冲突，451-*:47 已列红线；②间距效应冷却（reinforce.py:24-28 明示未实施，不得借来当 M2 参数）；③Miller 7+-2 / 任何工作记忆容量 myth；④被动消费信号等于云遥测 - 全程 local-first、零外发（competitor-memory-mechanisms.md:198-199）；⑤厂商 benchmark 数字（全为 CLAIMED，competitor-memory-mechanisms.md:204-211）- 不作任何门限出处；⑥capture 即 recall（与 R47 冲突，是本次整改的根因信条）；⑦#391 synthetic rig 与任何自报仪器（450-*:14,182）；⑧模型内部注意可被 hook 观测（S1 B-1 已降级为证据分层，451-*:14）；⑨对冻结 query 整句做别名映射的 teaching-to-test。

## 4. S0 作为永久回归 oracle（M0，无产品行为变更）

- 唯一验收面 = 冻结查询集 a43e94cb...9728172（s0-baseline-run1.md:5，4 条全 holdout：Q-001 miss_seed / Q-002 positive / Q-003/Q-004 honest_empty）+ 回放产品 MemoryService.recall 真路径的 harness（450-*:5.1-5.4），真语料副本、真 bge-m3、真 daemon、N=3 冷启动、离散=0。B1 miss_seed 1/1（基线 0/3，s0-baseline-run1.md:14,22）、B3 honest-empty 100%（基线 0.5，:16,24）、B2 recall@8 >= target（基线 0.0000，:15）。禁用 #391 synthetic rig、禁用自指打分（450-*:14,182）。
- M0 工作仅为：把该 harness 接成每片的验收闸、定义每根因成功判据都从 B1/B2/B3 派生（不造代理量）、在分片文档固化 oracle 只增不改、评测代码不进运行时。
- 测量中立性不变量：MemoryService.recall 非只读（memory.py:792 -> reinforce.py:126-132 写回 decay_weight），每 run 必须从 pristine 基线重置可写状态 + session_id=None；唯一 killer = 复用写后副本（450-*:117,130,148）。M1-M3 的任何产品变更不得破坏该重置语义。
- D3 硬门（org/decisions/2026-11-23-recall-remediation-direction.md:14）：整改通过前内核 not-shippable；M0-M4 不放宽任何 bar。B1/B3 为已 ratify 纪律门，B2=0.80 为目标数值（OD-3 定其是否为硬门）。ship-readiness 充要条件：S0 三数全过只是必要条件，还须 M0-M4 各片自身验收全过（尤其 M2 独立排序验收 §5.2/§5.5）；S0 harness 无消费信号使 M2 回弹路径不被 oracle 触达，M2 的 B2 贡献 [未验证] 必须作为 owner-visible 风险随 D3 材料呈报，不得据 S0 三数全过宣称 R-decay 已获 oracle 验证（详见 slices §0.4 / W4.4）。

## 5. 切片设计（机制与验收）

### 5.1 M1 · 线索对称（R-cue）

机制（全部读侧，零 store 写、零模型调用）
1. 查询侧对称 shadow：在 cues.py 增 query_shadow_tokens(text)，复用现有 _SHADOW_TOKEN_RE（cues.py:1029）与 _CAP_STOPWORDS，把低大小写 ASCII 显著词（如 codex/mnemoseed/import）并入 ExtractedCues.cues.entities；与 lexical_shadow_tokens（:1035-1057）同源，确保查询侧与存储侧对称。
2. 有界跨语言别名数据（新 capture/cue_alias_v1.py）：仿 Lexicon 加载缝（lexicon_v1.py:546-596），定义 frozen tuple[AliasEntry(surface, canonical, lang)]，只收通用 dev-memory 词汇对（如 导入/import、对话记录/conversation/chat、账本/ledger/记账、状态/status），每条须有与冻结查询无关的通用词汇依据。**明确排除** codex↔codex_cli 这类工具名规范化：该对派生自冻结目标 039d7d2 文本（s0-baseline-run1.md:29，含 codex_cli），属 EN 工具名变体而非通用跨语言词汇，纳入即犯 I-5 teaching-to-test，列入被拒变异。约束：canonical 必须是数据里显式存在的 surface，禁止生成式发明；别名展开上限（如 <=8 新增 token）、大小写折叠、全确定性。查询侧与 RecallVectorView._enriched（readview.py:87-95）各应用一次同一函数，使 beta entity 交集（hybrid.py:526-538）在任一侧措辞下成立。
3. fail-open：别名模块加载/展开异常时退回现有 shadow 路径并走 note_bridge_shadow_fallback（readview.py:79-85 同形），recall 不阻断。
4. 不变量：verbatim-channel 不改（cues.py:10）；stored cues 与 stamper 落盘字节一致（readview.py:7-8）；显式 pin/rescue 排序纪律保留（hybrid.py:296-311）；不做 wire 字段变更。

验收：S0 B1 0/3->1/1、B2 由 0.0000 上升（目标 >=0.80），B3 不退化；被拒变异：把别名写回 store stamp（违反字节一致）=> RED；把冻结 query 整句作为别名条目（teaching-to-test）=> RED；别名展开触发 store 写 => RED；非 gating 的 held-out 释义探针（中到英、英到中，另含工具名/大小写变体一族，但工具名须取自与冻结查询无关的通用 dev 工具词汇）报告覆盖率但不作 bar；别名含冻结目标派生工具名变体（如 codex↔codex_cli）=> RED。

### 5.2 M2 · 消费驱动强化（R-decay，S1 依赖）

机制（在 S1 落地后启用）
1. 复用 S1 的 append-only consumption_events（451-*:§4.1，migration v18）与只读 ConsumptionEventFilter 端口（:83）；M2 是排序路径第一个 reader（S1 AC-8 保证 S1 自身零 reader，:140,179），因此必须独立切片 + 独立排序验收。
2. memory.py:813-836 的 _record_hits 增一路：按本次 serve_id（correlation_id）join served 与 consumed_attested/consumed_explicit 行；对每条 served entry 得 consumed_status：有 consumed 行 => 消费证据成立；仅 served/injected => unknown（永不 not_used，S1 AC-9，451-*:101,180）。
3. Reinforcer.record_hits（reinforce.py:90-132）接收 consumed_chunk_ids/consumed_node_ids：只对有消费证据者回弹 min(1.0, w+bonus) 并刷新 last_reinforced（:126-132,144-...）；served-but-unused 仍走 update_chunk_state(hit_increment=1)（:121-124）计 usage 但不回弹；below-floor 与 rescue 规则（:16-21,117）保持不变。
4. fail-open（never-infer-from-silence 的操作化）：以显式 config 旗标区分宿主具备 S1 仪表与不具备。仪表缺席 => 回退现有 served 驱动（保住无插件宿主现状，:813-836 原样）；仪表在场但无 consumed 行 => unknown => 不回弹（这是行为变更，但不宣称未使用，只是无正证据不强化）。禁止用沉默推断 not_used（451-*:118,143）。旗标自检：为旗标状态（present/absent）与「旗标=present 但 consumed 行数=0」次数加 observability 计数/警告并纳入 snapshot；后者按 451-*:118 仍属 unknown（不回弹、不改 verdict），连续出现须告警以防旗标误配致全员 unknown / 静默 recall 退化；与跨宿主回传 [未验证] 绑定。
5. S0 harness 测量中立性保持：harness 每次从 pristine 重置且无宿主消费行，M2 在 harness 中要么走仪表缺席回退、要么回弹被重置 - 都不得破坏 450-*:117,130,148 的逐 run 恒等回归。

验收（独立排序验收，非 S0）：给 A(consumed)/B(served-only) 同 decay 的构造事件，一次 reinforce 后 A 回弹、B 不变（去掉门控 => B 也回弹 = RED）；移除仪表缺席回退 => 无插件宿主零回弹 = RED；把 unknown 当 not_used 写 verdict => RED；S0 B1/B2/B3 不退化。启用条件：S1 已落地且 owner 授权 M2 工程（本批不授权）。

### 5.3 M3 · honest-empty 内容证据门（R-empty）

机制（装配末端，确定性）
1. 在 assemble.py 的最终组装（assemble.py:301-321）增设无真实匹配判据：对候选集计算查询显著内容 token 与 chunk 正文的读时重叠（复用 M1 的 shadow/alias 输出；不引入 embedder/模型），若整个 admitted 集皆无任何显著重叠证据，则返回显式空 AssembledContext（现有结构 :31-33,188-197 已支持，含 coverage 自报），而非填充。
2. 与现存 dense 下界（hybrid.py:99,312-314）叠加、不替代：0.45 仍是候选准入下界；M3 是无匹配则空的额外内容证据条件。泛化 #448 显式失败不静默填充原则（competitor-memory-mechanisms.md:173,187），明确拒绝其 zero-sparse 字面（真实语料 vacuous，org/_state.md 的 #448 批次终态条目（关键字 "zero_sparse_signature ×3"；追加写活文档勿用行号）、bridge_sweep.py:146-160）。
3. false-empty 保护：显式 pin / rescue 候选旁路（hybrid.py:307-311，pinned 记忆即使词法弱也须可及）；门槛只在全部 admitted 零重叠时触发；B1/B2 回归强制。阈值（最少重叠 token 数 / 是否要求别名 canonical）[未验证]。阈值标定面与冻结 oracle 分离（禁教学到测试，承接 I-5）：阈值必须在与冻结查询集 a43e94cb...9728172 分离的 tuning/held-out 探针上标定，即独立 honest-empty 语料（与冻结 Q-003 氦三不同域、无真实匹配的查询）+ 已知真实命中的正例，二者均不来自冻结 4 条；数值形式可参考 dense 下界 hybrid.py:312-314 的语义间隔（min_similarity=0.45）确定量级，但标定数据只来自独立探针。冻结集只作最终裁决面：阈值在探针上定稿后不得因冻结集结果回调；报告必须把冻结集上的 B3/B1 结果显式标为 in-sample（小固定集、非独立泛化证据）。被拒变异：在冻结集上直接调阈值使 Q-003 空且 Q-001/Q-002 不空 => RED（teaching-to-test，与 I-5 同罪）。

验收：S0 B3 0.5->1.0（Q-003 空、Q-004 保持空），B1=1/1、B2>=target 不退化；被拒变异：门槛设 0 使 Q-003 仍返回 6 chunk => RED；旁路失效使 pin 记忆被误清空 => RED；照搬 zero-sparse 字面门 => 在冻结集上无区分力（诊断 FAIL，不得计入通过）；在冻结集上就地标定阈值使指定 Q 分叉 => RED（teaching-to-test，承接 I-5/§5.3）。

### 5.4 M4 · 全量 re-baseline

冻结集在 pristine 副本上 N=3 冷启动全量重跑（与基线同一四要素钉 + 同一 harness），做测量中立 killer A、离散=0 校验；按 450-*:150 追加式 JSON 出报告，向 owner 报 B1/B2/B3/B4/p95 四要素与逐 query 明细，供 D3 发船裁决，并附各片自身验收状态（含 M2 独立排序验收）与 M2 B2 贡献 [未验证] 的 owner-visible 风险一并呈报。不放宽 bar、不复用 tuning/seen 子集充当 bar。B2 数值是否硬门 = OD-3。

### 5.5 跨片依赖与文件面（详见 slices 文档）

- M0：无产品文件；M1：retrieve/cues.py、新 capture/cue_alias_v1.py、retrieve/readview.py + 测试；M2：daemon/memory.py、decay/reinforce.py、daemon/config.py + 测试（依赖 S1 已落地）；M3：retrieve/assemble.py + 测试（读用 M1 工具，但只改 assemble.py，文件面与 M1 不交叠）；M4：无产品文件（harness 配置/报告）。
- 排序：M0 -> (M1 并行 等待 S1) -> M2（S1 后）与 M3（M1 后）-> M4。M2 与 M3 文件面不交叠，可在各自 start 授权后并行；M3 依赖 M1 的读时工具存在。
- 三根因都需覆盖，S1 前置已在 §2/§5.2 明示；派单 Suggested slices 被完整保留，I-1..I-6 为必要精化。

## 6. 跨片不变量（红线）

- provenance/verbatim/append-only/never-infer-from-silence 全部保留；M1 读时 model_copy、M2 无 verdict 推断、M3 不改 stamp。
- KISS：M1 只加数据 + 两个读时函数；M2 复用 S1 端口不另起表；M3 复用现有空 context 结构；无新服务/无云。
- fail-open：别名/台账/门控异常均退化到现状、recall 不阻断（readview.py:79-85、memory.py:835-836、451-*:§9.2）。
- token economy：全链路零模型调用（别名是数据查表，消费是事件 join，空门是 token 重叠）。
- 诚实 as-is：M1 的跨语言覆盖、M2 的消费证据精度、M3 的门阈值均标 [未验证] 待独立探针标定；不得宣称已测。

## 7. 重大决策的 option space（含推荐）

| 决策 | 选项 A | 选项 B | 推荐 / 理由 |
|---|---|---|---|
| M1 扩展落点 | capture 写回 cue（全量覆盖但破 provenance/append-only） | 读时双向 shadow+alias（store 字节不变） | B - 保红线、KISS、可审计；A 违 I-1 |
| M1 别名来源 | 复用 lexicon_v1.py VAD 词表推别名 | 新建有界 cue_alias_v1.py 数据（仅借 Lexicon 加载缝） | B - A 是范畴错误（I-2） |
| M2 回弹触发 | served 即回弹（现状） | 消费证据回弹 + 仪表缺席回退 served | B - R28 忠实 + fail-open + never-infer-from-silence（I-3）；A 是根因本身 |
| M2 验收面 | 并入 S0 bar | 独立排序验收 + S0 不退化 | B - S0 无消费信号，无法验收（I-3） |
| M3 门依据 | #448 zero-sparse 字面 | 内容重叠证据门（原则泛化） | B - 字面在真实语料 vacuous（I-4） |
| B2 发船语义 | 0.80 硬发船门 | 目标 + re-baseline 后 owner 重 ratify | 留 OD-3 给 owner（I-6） |

## 8. 失败模式与规模（10x-1000x）

- M1：别名表有限且有上限，最坏退化为现状 shadow；store 不可变故无增长风险。
- M2：consumption_events 随 serve 线性增长（某 boot 86 serve，451-*:158）；复用 (profile_id, observed_at) + corr 索引与分页读（451-*:§4.1,9.3），rollup 属 OD-3、本片不静默删数据。
- M3：内容重叠计算是 per-admitted-entry 的 token 集合运算，O(top_k x 平均 token)，无 I/O 放大；空 context 直接走覆盖自报，规模无害。
- 共同数据丢失包络：最坏丢失读时扩展/消费证据/空门判据 => 退回现状行为，不丢记忆内容、不改 provenance。

## 9. [未验证] 清单 / 反对意见

- [未验证]（须 harness）：M1 别名集对冻结 miss 与 held-out 释义的实际覆盖率；M2 对 B2 的量化贡献；M3 最少重叠阈值；p95 延迟相对基线比值。精确数字不在设计轮宣称。
- [未验证]（宿主）：S1 hook 在 Claude Code / Codex 的等价回传（451-*:186）- 影响 M2 的仪表在场判定。
- 反对 1 又是表示层补丁：回应 - M1 改的是查询侧可及性（此前无人做），M2/M3 分别改强化驱动与空门，三者合起来才闭合线索->消费->空判据；不是 #448 的同词形态。
- 反对 2 M2 改排序违反 S1 no-ranking-change：回应 - S1 冻结边界只在 S1 自身；M2 是独立切片、独立验收（I-3），且 owner Option A 明确要求消费驱动强化（:17）。
- 反对 3 M3 会误伤真实召回：回应 - pin/rescue 旁路 + B1/B2 强制回归 + 阈值独立探针标定（§5.3，禁冻结集就地调参）。

## 10. Owner 决策点（详见 slices 文档）

- OD-1（工程 start，必答）：独立设计 QA CLOSABLE + 冻结后，是否授权本整改批次的工程 start？（本批 DESIGN ONLY，不授权。）
- OD-2（S1 前置 start）：是否先授权 S1 仪表自身工程（451-*）？M2 不得在 S1 落地前启用。
- OD-3（B2 发船语义）：整改后 B2>=0.80 是否为硬发船门，还是作为目标、由 re-baseline 后重新 ratify？

## 11. 证据时效与置信度

- 时效：产品源码、三 investigation、竞品、S0 基线均为 2026-11-22/23 同批一手读取（access 2026-11-23）。理论锚为 1966-1994 经典已校验研究，REFERENCES.md:20,23,49,59,99 标已核验（Crossref），>90 天仍有效理由：基础认知理论非快变领域，本仓已逐条核验。无厂商 CLAIMED benchmark 或快变数字被当作门限（竞品材料 access 2026-11-22，全部标 CLAIMED）。
- 置信度：高（三根因 file:line 事实、机制映射、S1 前置、红线保持 - 逐字复算源码）；中（M1 跨语言覆盖度、M2 宿主仪表在场判定、M3 阈值 - 依赖 harness/宿主，[未验证]）；低（各片对 B2 的量化提升数值，未做实测）。

## 12. 建议（下一步，需要谁）

1. main 把本文 + slices 交独立 design QA（跨族）取 CLOSABLE 后冻结；冻结不等于工程派发。
2. owner 就 OD-1/OD-2/OD-3 裁决；尤其 S1 须单独 start 授权，M2 方可排期。
3. 各片工程走 issue -> branch -> PR，spare port + temp home（AGENTS.md:76-79），TDD + 对抗 QA；S0 harness 在每片收口作 oracle，D3 硬门保持至 B1/B2/B3 全过。
