# 设计 460-kernel-recall-remediation-slices v1 - 分片 / 依赖 / allowlist / 验收门

> 状态：**PROPOSAL / DESIGN_PENDING（无门权）**。DESIGN ONLY。
> 配套主设计：`460-kernel-recall-remediation-v1.md`（裁决 SHIP-WITH-ADJUSTMENTS，发现 I-1..I-6）。
> 声明：本席 solution-architect（declared model opencode/hy4-flash-free，物理身份未自证）；access 2026-11-23。

## 0. 授权边界（硬）

1. **工程需独立 start 授权**：本批（含本文）是 PROPOSAL，无门权；独立设计 QA CLOSABLE + 冻结 + owner 显式 start 之前，**不得**派工程、不得改产品源码/配置、不得 git ops、不得绑定端口、不得写 ~/.mnemoseed-local、不得改冻结 450/451/452 文档与签名查询集 a43e94cb...9728172。
2. 唯一 oracle = 冻结查询集 + S0 harness（`450-*`），真语料副本 + MemoryService.recall 真路径，N=3 冷启动、pristine 重置、session_id=None、离散=0。验收 bar：B1 miss_seed 1/1、B3 honest-empty 100%、B2 recall@8 >= target（0.80 为目标，见 OD-3）。禁 #391 synthetic rig、禁代理指标、禁自指打分（`450-*:14,182`；`s0-baseline-run1.md:5,12-19`）。
3. **D3 硬门**（`org/decisions/2026-11-23-recall-remediation-direction.md:14`）：S0 契约 FAIL 期间内核 not-shippable。当前基线 FAIL（B1 0/3、B2 0.0000、B3 0.5）。以下每片收口都跑 oracle；**在 B1=1/1、B3=100%、B2>=target 全过之前，内核保持不可发船**。
4. **ship-readiness 语义（D3 解除 not-shippable 的充要条件）**：S0 harness 无宿主消费信号（`460-v1.md:74,112`），M2 在 harness 必走 fail-open、永不执行其消费驱动回弹路径，故 S0 三数全过只是必要条件而非充分条件。ship-readiness = (S0 三数 B1=1/1 ∧ B3=100% ∧ B2>=target 全过) **且 (M0-M4 每片自身验收全 PASS，尤其含 M2 的独立排序验收 `460-v1.md:76` / W2.6)**。M2 的 B2 量化贡献因 harness 无消费行仍为 `[未验证]`；该不确定性必须作为 owner-visible 风险随 D3 裁决材料一并呈报，不得因 S0 三数全过而宣称 R-decay 修复已被 oracle 验证。

## 1. 切片总表

| 片 | 目标 / 根因 | work units | 依赖 | 文件 allowlist | 验收门 | 对 oracle 的数字迁移 | owner 决策 | 工程 start |
|---|---|---|---|---|---|---|---|---|
| M0 | oracle 接线（无产品行为变更） | W0.1-W0.3 | 无 | 仅 harness/eval 配置与报告（additive，不进运行时） | S0 harness 在冻结集上稳定复现基线；测量中立 killer A 绿 | 数字不变（B1 0/3 / B2 0.0000 / B3 0.5）但确立准绳 | 无 | 需（本批不授权） |
| M1 | R-cue 线索对称 | W1.1-W1.5 | M0 | `retrieve/cues.py`、新 `capture/cue_alias_v1.py`、`retrieve/readview.py` + 测试 | B1 1/1、B2 上升、B3 不退化；store 字节一致变异 RED；teaching-to-test 变异 RED | B1 0/3 -> 1/1；B2 0.0000 -> 上升（目标 >=0.80）；B3 0.5 不变 | 无（I-1/I-2 已定方案） | 需（本批不授权） |
| M2 | R-decay 消费驱动强化 | W2.1-W2.6 | M0 + **S1 已落地且 owner 授权 S1 工程** | `daemon/memory.py`、`decay/reinforce.py`、`daemon/config.py` + 测试（复用 S1 端口） | 独立排序验收（A 回弹/B 不回弹）+ S0 B1/B2/B3 不退化；沉默即 not_used 变异 RED | 直接贡献 B2 可持续性与 B1（消费回弹）；harness 内无消费行，量化贡献 [未验证] | OD-2（S1 start）、OD-1（本片 start） | **需，且 S1 先行** |
| M3 | R-empty honest-empty 内容证据门 | W3.1-W3.5 | M0 + M1（读时工具） | `retrieve/assemble.py` + 测试（只读用 M1 工具，不改 M1 文件） | B3 0.5 -> 1.0；B1=1/1、B2>=target 不退化；pin 旁路变异 RED | B3 0.5 -> 1.0；B1/B2 不退化 | 无（I-4 已定方案） | 需（本批不授权） |
| M4 | 全量 re-baseline + 裁决材料 | W4.1-W4.4 | M1 + M2 + M3 | 仅 harness 配置/报告（additive） | N=3 全量重跑、测量中立 killer A、离散=0、追加 JSON 报告 | 汇总最终 B1/B2/B3/B4/p95 供 D3 裁决 | OD-3（B2 发船语义） | 需（本批不授权） |

## 2. 每片 work units

### M0 · oracle 接线
- **W0.1** 把 S0 harness（`450-*`）接为每片收口唯一验收闸；固定冻结集 a43e94cb...9728172，4 条全 holdout（Q-001 miss_seed / Q-002 positive / Q-003/Q-004 honest_empty）；产品绑定 = `MemoryService.recall` 真路径（`memory.py:760-799`）。
- **W0.2** 定义每根因成功判据只从 B1/B2/B3 派生：R-cue -> B1/B2，R-decay -> B2（消费子面另见 M2 独立验收），R-empty -> B3；不引入任何代理量（禁 #206/#391 自报仪器）。
- **W0.3** 固化测量中立：每 run pristine 重置可写状态（`memory.py:792 -> reinforce.py:126-132` 写回 decay_weight）+ session_id=None；唯一 killer A = 复用写后副本必须 RED（`450-*:117,130,148`）。eval 代码 additive、不进运行时。
- 验收：harness 在冻结集稳定复现基线（B1 0/3、B2 0.0000、B3 0.5、离散 0）；killer A RED；无产品字节改动。

### M1 · 线索对称（读侧双向）
- **W1.1** `cues.py` 增 `query_shadow_tokens(text)`：复用 `_SHADOW_TOKEN_RE`（:1029）/ `_CAP_STOPWORDS`，把查询侧低大小写 ASCII 显著词并入 `ExtractedCues.cues.entities`，与 `lexical_shadow_tokens`（:1035-1057）对称。
- **W1.2** 新 `capture/cue_alias_v1.py`：仿 `Lexicon` 加载缝（`lexicon_v1.py:546-596`）的 frozen 有界别名数据 `AliasEntry(surface, canonical, lang)`，只收通用 dev-memory 词汇对；canonical 必须显式在数据中，禁止生成式发明；展开上限 <=8。
- **W1.3** 查询侧 + `RecallVectorView._enriched`（`readview.py:87-95`）各应用一次同一别名函数，使 β entity casefold 交集（`hybrid.py:526-538`）对任一侧措辞成立；全程 `model_copy`，stored cues 与 stamper 落盘字节一致（`readview.py:7-8`）。
- **W1.4** fail-open：别名模块异常退回现有 shadow，走 `note_bridge_shadow_fallback`（`readview.py:79-85`）；verbatim-channel 不改（`cues.py:10`）；pin/rescue 排序纪律保留（`hybrid.py:296-311`）；零模型调用。
- **W1.5** 验收：S0 B1 0/3 -> 1/1、B2 上升、B3 不退化；被拒变异 - 别名写回 stamp / 冻结 query 整句作别名 / 别名触发 store 写，三者皆 RED；held-out 释义探针（中英互译 + 工具名/大小写变体一族，工具名须与冻结查询无关）只报告覆盖率不作 bar；别名含冻结目标派生工具名变体（如 codex↔codex_cli）=> RED。
- 数字迁移：B1 0/3 -> 1/1；B2 0.0000 -> 上升（目标 >=0.80，[未验证] 实测值）；B3 0.5 不变。

### M2 · 消费驱动强化（S1 依赖）
- **W2.1** 前置门：确认 S1（`451-*`）已落地且 owner 已授权 S1 工程；否则本片不启动。S1 建 append-only `consumption_events`（migration v18）与只读 `ConsumptionEventFilter`（`451-*:§4.1,:83`）；M2 是排序路径第一个 reader（S1 AC-8 自身零 reader，:140,179），故独立切片。
- **W2.2** `daemon/memory.py:813-836` 的 `_record_hits` 增一路：按本次 `serve_id`（correlation_id）join served 与 `consumed_attested`/`consumed_explicit` 行；每条 served entry 得 consumed_status - 有 consumed 行 => 消费证据；仅 served/injected => unknown（永不 not_used，S1 AC-9，`451-*:101,180`）。
- **W2.3** `Reinforcer.record_hits`（`reinforce.py:90-132`）接收 `consumed_chunk_ids`/`consumed_node_ids`：只对有消费证据者回弹 `min(1.0, w+bonus)` 并刷新 `last_reinforced`（:126-132,144-...）；served-but-unused 仍 `update_chunk_state(hit_increment=1)` 计 usage 不回弹（:121-124）；below-floor 与 rescue 规则不变（:16-21,117）。
- **W2.4** fail-open（never-infer-from-silence 操作化）：显式 config 旗标区分「宿主具备 S1 仪表 / 不具备」；仪表缺席 => 回退现有 served 驱动（无插件宿主现状，`memory.py:813-836` 原样）；仪表在场但无 consumed 行 => unknown => 不回弹（行为变更但不宣称未使用）；禁从沉默推断 not_used（`451-*:118,143`）。旗标自检：observability 计数/警告记录旗标状态（present/absent）与「旗标=present 但 consumed 行数=0」次数并纳入 snapshot；后者仍属 unknown（`451-*:118`，不改 verdict/不回弹），连续出现须告警以防误配致全员 unknown / 静默 recall 退化；与跨宿主回传 `[未验证]` 绑定。
- **W2.5** S0 测量中立保持：harness 每 run pristine 重置且无宿主消费行，M2 不得以自身写回破坏 `450-*:117,130,148` 逐 run 恒等回归。
- **W2.6** 验收（独立排序验收，非 S0）：A(consumed)/B(served-only) 同 decay，一次 reinforce 后 A 回弹 B 不变（去门控 => B 也回弹 = RED）；移除仪表缺席回退 => 无插件宿主零回弹 = RED；unknown 写 verdict => RED；S0 B1/B2/B3 不退化。
- 数字迁移：直接贡献 B2 可持续性与 B1（消费回弹）；harness 无消费行，B2 量化贡献 [未验证]。

### M3 · honest-empty 内容证据门
- **W3.1** `retrieve/assemble.py:301-321` 增设「无真实匹配」判据：查询显著内容 token 与 chunk 正文读时重叠（复用 M1 shadow/alias 输出，不引入 embedder/模型）；整个 admitted 集皆无显著重叠证据 => 返回显式空 `AssembledContext`（现有结构 :31-33,188-197 已支持，含 coverage 自报），不填充。
- **W3.2** 与现存 dense 下界（`hybrid.py:99,312-314`，0.45）叠加而非替代；泛化 #448 显式失败不静默填充原则（`competitor-memory-mechanisms.md:173,187`），明确拒绝 zero-sparse 字面（`bridge_sweep.py:146-160`、org/_state.md 的 #448 批次终态条目「zero_sparse_signature ×3 真实语料 vacuous」关键字；追加写活文档勿用行号）。
- **W3.3** false-empty 保护：显式 pin / rescue 旁路（`hybrid.py:307-311`）；门槛只在全部 admitted 零重叠时触发；阈值（最少重叠 token 数 / 是否要求别名 canonical）[未验证]。标定面与冻结 oracle 分离（禁教学到测试，承接 I-5）：阈值只在独立 tuning/held-out 探针上标定，即独立 honest-empty 语料（与冻结 Q-003 不同域、无真实匹配）+ 已知真实命中正例，二者均不来自冻结 4 条；数值形式可参考 dense 下界 `hybrid.py:312-314`（min_similarity=0.45）确定量级，但标定数据只来自独立探针。冻结集只作最终裁决面，阈值定稿后不得因冻结集结果回调，报告把冻结集 B3/B1 结果显式标为 in-sample（小固定集、非独立泛化证据）。被拒变异：在冻结集上就地调阈值使 Q-003 空且 Q-001/Q-002 不空 => RED（teaching-to-test）。
- **W3.4** 验收：B3 0.5 -> 1.0（Q-003 空、Q-004 保持空），B1=1/1、B2>=target 不退化；被拒变异 - 门槛设 0 使 Q-003 仍返回 6 chunk => RED；旁路失效使 pin 记忆被误清空 => RED；照搬 zero-sparse 字面门 => 诊断 FAIL 不计通过。
- 数字迁移：B3 0.5 -> 1.0；B1/B2 不退化。

### M4 · 全量 re-baseline
- **W4.1** 冻结集在 pristine 副本上 N=3 冷启动全量重跑（与基线同一四要素钉 + 同一 harness）。
- **W4.2** 测量中立 killer A + 离散=0 校验；任一 port 占用/embedder 不可用/哈希漂移/副本不完整 => INVALID_RUN，不计 bar、不写 PASS/FAIL（`450-*:149`）。
- **W4.3** 追加式 JSON 报告（`450-*:150`，UTC + 四要素钉 + 逐 query 明细 + seen/holdout）；向 owner 报 B1/B2/B3/B4/p95 与逐 query 明细。
- **W4.4** 供 D3 裁决：契约 PASS 的充要条件 = (B1=1/1 且 B3=100% 且 B2>=target 且无 UNKNOWN) **且 (M0-M4 各片自身验收全过，尤其 M2 独立排序验收 `460-v1.md:76` / W2.6)**；否则内核继续 not-shippable。因 S0 harness 无宿主消费信号（`460-v1.md:74,112`），M2 在 harness 必走 fail-open、其消费驱动回弹路径从未被 S0 oracle 触达，M2 的 B2 量化贡献为 `[未验证]`；此不确定性必须作为 owner-visible 风险随 D3 材料一并呈报，不得据 S0 三数全过宣称 R-decay 修复已获 oracle 验证。不放宽 bar、不复用 tuning/seen 子集充当 bar。
- 数字迁移：汇总最终 B1/B2/B3/B4/p95，供 D3 裁决（OD-3 定 B2 是否硬门）。

## 3. 依赖 / 并行性 / 文件面冲突检查

- 依赖链：M0 -> (M1 与「等待 S1」并行) -> M2（S1 后）与 M3（M1 后）-> M4。
- 文件面互斥（可并行）：M1 = {cues.py, cue_alias_v1.py, readview.py}；M2 = {memory.py, reinforce.py, config.py}；M3 = {assemble.py}；三者两两不交叠。
- 读依赖（非写冲突）：M3 读用 M1 的 shadow/alias 工具，故排在 M1 之后；M2 复用 S1 端口（只读 `consumption_events`），不改 S1 文件。
- 测试 oracle 隔离：各片 tests 独立文件；S0 harness additive（`450-*:184`），不把评测代码引入运行时；测试/实测用 spare port + temp home（`AGENTS.md:76-79`），不碰 7788 与 ~/.mnemoseed-local。
- 跨片不变量（主设计 §6）：provenance / verbatim / append-only / never-infer-from-silence 保留；M1 读时 model_copy、M2 无 verdict 推断、M3 不改 stamp；全链路零模型调用。
- 冲突预警：本批无 `ErrorSignalType` 成员追加，不与 S1/S3 的 signal_type 族碰撞（S1 建独立 consumption_events 表零成员；S3 仅追加 EXTRACTION_FAILURE，`org/_state.md:23`）。M2 只**读取**台账，不新增迁移版本。

## 4. S0 oracle 作为每片验收闸（不造代理）

- 每片 W4/W1.5/W2.6/W3.4 收口都在同一冻结集 + 同一 harness 上跑，报 B1/B2/B3 三元组；**不允许**用 #391 synthetic rig、cue 重合自算、serve 计数或 agent 自陈反馈充当质量判据（`450-*:14,182`；`451-*:141`）。
- B2=0.80 是目标数值不是当前门槛（`org/decisions/2026-11-23-recall-remediation-direction.md:12`）；基线 0.0000 如实保留（`s0-baseline-run1.md:15`）。ratify 后不得下调 bar。

## 5. D3 硬门与基线数字迁移汇总

| 片 | B1 miss_seed（基线 0/3） | B2 recall@8（基线 0.0000） | B3 honest-empty（基线 0.5） | D3 状态 |
|---|---|---|---|---|
| M0 | 0/3（不变） | 0.0000（不变） | 0.5（不变） | FAIL，not-shippable |
| M1 | 1/1（目标） | 上升（目标 >=0.80，[未验证]） | 0.5（不变） | 仍取决于 M2/M3 |
| M2 | 不退化 | 贡献可持续性，量化 [未验证] | 不退化 | 仍取决于 M3 |
| M3 | 不退化（1/1 保持） | 不退化 | 1.0（目标） | 仍取决于 B2 |
| M4 | 1/1（裁决输入） | >=target（裁决输入） | 100%（裁决输入） | S0 三数全过 **且** 各片验收（含 M2 独立排序验收 `460-v1.md:76`/W2.6）全过才解除 not-shippable；M2 的 B2 贡献 `[未验证]` 作为 owner-visible 风险随 D3 材料呈报（S0 harness 无消费信号，M2 回弹路径未被 oracle 触达） |

## 6. 验收变异（killer）清单

1. M0 killer A：复用 decay 写后副本 => 逐 run 恒等回归 RED（`450-*:130,148`）。
2. M1 killer：别名写回 store stamp / 冻结 query 整句作别名 / 别名触发 store 写 / 冻结目标派生工具名变体（codex↔codex_cli）作别名 => RED。
3. M1 killer（teaching-to-test）：别名命中仅覆盖冻结 holdout query 且 held-out 释义探针失败 => RED。
4. M2 killer：去消费门控使 served-only B 也回弹 => RED；移除仪表缺席回退 => RED；unknown 写 verdict => RED。
5. M2 killer（排序零 reader 反向）：S1 自身阶段在召回路径读台账 => S1 AC-8 RED（属 S1，非本批）。
6. M3 killer：门槛设 0 使 Q-003 仍返回 6 chunk => RED；pin 旁路失效 => RED；在冻结集上就地标定阈值使 Q-003 空/Q-001-Q-002 不空 => RED（teaching-to-test，承接 I-5 / W3.3）。

## 7. Denylist（hard）

- 不改冻结 450/451/452 文档、签名查询集、#448 既有行为（除非设计门显式授权）。
- 不引入云/遥测/专有模型/模型调用（`competitor-memory-mechanisms.md:198-211`；token economy）。
- 不把 #391 synthetic rig 或任何自报仪器用作 oracle。
- 不动 live daemon / 7788 / ~/.mnemoseed-local；测试用 spare port + temp home。
- 不新增 `ErrorSignalType` 成员或迁移版本（避免与 S1/S3 碰撞）。
- 不静默删除 append-only 台账数据；不推断 silent => used / not_used。

## 8. Owner 决策点

- **OD-1（工程 start，必答）**：设计 QA CLOSABLE + 冻结后，是否授权本整改批次工程 start？本批 DESIGN ONLY，**不授权**。
- **OD-2（S1 前置 start）**：是否先授权 S1 仪表自身工程（`451-*`）？M2 依赖 `consumed_attested`，**S1 未落地不得启用 M2**，不得以 served 冒充 consumed。
- **OD-3（B2 发船语义）**：整改后 B2>=0.80 是否为硬发船门，还是作为目标、由 M4 re-baseline 后 owner 重新 ratify？

## 9. 证据 / 置信度

- 证据（file:line）：`s0-baseline-run1.md:5,12-32`；`450-*:117,128,130,148-150`；`451-*:61,79,83,101,140,145-147,179-186`；`retrieve/cues.py:10,896-1005,1029,1035-1057`；`retrieve/readview.py:7-8,79-95`；`retrieve/hybrid.py:67-69,99,296-314,526-538`；`retrieve/assemble.py:31-33,188-197,301-321`；`daemon/memory.py:792,813-836`；`decay/reinforce.py:16-28,90-132`；`capture/lexicon_v1.py:1-15,546-596`；`capture/rulesets_v1.py:1-10`；`eval/bridge_sweep.py:146-160`；`org/_state.md:23 + 关键字 "zero_sparse_signature ×3"`；`competitor-memory-mechanisms.md:173,187,198-211`；`REFERENCES.md:20,23,49,59,99`。
- `[未验证]`：M1 别名覆盖率、M2 对 B2 量化贡献、M3 阈值、p95 比值、S1 hook 在 Claude Code / Codex 的等价回传（均需 harness/宿主实测）。
- 置信度：高（根因 file:line 与依赖/文件面/红线）；中（跨宿主信号、阈值标定）；低（各片对 B2 的量化提升）。时效：源码/调查 access 2026-11-22/23；理论锚 1966-1994 经典已核验、非快变；厂商 benchmark 全标 CLAIMED，不作门限。
