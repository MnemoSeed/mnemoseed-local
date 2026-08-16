# MnemoSeed-Local · MVP 设计稿（v1.3，定稿）

> 本文汇总本轮所有方向讨论的可执行结论，为 Phase A 已实施部分做设计确认，并约束后续 Phase A2.5 / A3+ 展开。未含任何具体人名；决策日期只标版本发布时间。
> v1.3 相对 v1.2：吸收盲审结论（PASS-WITH-CONDITIONS，9 项 MUST 全部处置）——vote 模式改为诚实成本版、isolated 实例必需化、dream 事件循环卸载与 consolidated 检索过滤列入基线修正；budget 概念整体移除（账本只记录 token）；硬件档位扩为 standard/lite/advanced 三档；新增 Phase A2.5 基线修正包。

## 1. 定位与边界

- **产品定位**：本地单用户记忆引擎 MVP——本地模型驱动（默认 ollama，可切换任意 OpenAI-compatible 端点），捕捉宿主对话、本地巩固、新鲜检索，逐步评估记忆使用体感。
- **独立路线**：不回流 main（mnemoseed/ 主仓）；主仓与本仓按同一设计哲学并行演化，不允许"本仓为主、主仓跟随"。
- **裁掉（MVP 不做）**：console（静态 + 后端）、账号体系（localhost 隐式信任）、多 profile、PG 驱动组、云端（含 BYOK，推迟至 Phase B 之后）、一切用户管理、provider registry、产品营销平面、**任何形式的平台 budget 上限**。
- **保留核心**：记忆管线五阶段原则（capture → consolidate（dream）→ decay → retrieve → provenance/审计）、mark-consolidated（合并后的 raw chunk 标记退出搜索面但不删除）、全盘按产品红线守护。

## 2. 核心用户体验

1. 一行安装（A3 阶段交付）：`iwr/irm script.irm | iex` 或 `curl | sh`，编排壳完成 ollama/uv 安装 → `uv tool install` → `init` → 硬件探测荐档 → 用户确认后拉取模型。
2. Daemon（`mnemoseed-local up`）常驻 localhost 运行（仅 loopback 绑定，非回环地址拒绝启动），capture 由 daemon REST 接宿主事件（A3 起由宿主 hook 自动投喂），**dream 在独立工作线程执行，不阻塞 daemon 响应**。
3. CLI 全量访问：`init / up / status / doctor / recall / remember / dream --once / forget / config {get,set,rollback}`。
4. 按需触发：`dream --once` 当场跑一节梦境（宿主侧 auto 触发模式留待后续追加；daemon 侧 `dream.auto_trigger` 已是注册表热键）。

## 3. 架构（与主仓同源、移植瘦身）

| 层 | 内容 | 裁剪选择 |
|---|---|---|
| config + configwrite | 单一写者：registry → 校验 → 外科式 TOML patch → 版本化 meta-store 记录 → 审计（actor）→ 热生效；config.toml 为生成镜像，手改漂移侦测 | 保留全部；**移除 budget 键** |
| secrets | FileSecretStore + ChainSecretStore + KeyringSecretStore 三件套，config 只存引用（`secrets:mnemoseed/dream/<role>`） | 保留全部 |
| storage | VectorStore / GraphStore / MetaStore / Embedder 四接口 + sqlite（meta/graph） + lancedb_embedded 默认 + bge-m3 ONNX + synthetic（测试用）；**isolated graph 实例为必需件**（init 模板默认写入，启动/doctor 硬检查） | 去 PG 驱动与 openai-compatible embedder |
| schema | Stamp（情绪/动机/线索/生命周期）、GraphNode/Edge（双时态 + provenance 版本链） | 保留全部 |
| capture | stripper → scorer（S = 情绪 × 新颖 × 因果 三组件）→ stamper → ScorePool | 保留全部 |
| retrieve | cues + hybrid（双轨 + 融合 rerank）+ budget/assemble + Freshness Guard（**主检索轨与 probe 均过滤 `consolidated=false`**，合并后 chunk 退出搜索面，仅可按 provenance 追溯取回） | 保留全部 |
| dream | snapshot（原子落盘）→ delta（动态预算分包）→ reflect（三分流 core/isolated/salvage + 幂等 resume；**可选 ensemble 校验层**：单快照双 reflect 相位 + 确定性 combiner，分歧 → isolated）→ merge（写回 graph nodes）+ ledger（**纯 token 记录，无 budget**） | 保留全部；**深化：源 chunk 一律 mark_consolidated（不删除）** |
| decay | sweep + reinforce + consolidated ×3 λ | 保留全部 |
| llm driver | `ollama`（默认，localhost:11434）+ `openai_compatible`（Phase B 后 BYOK 预留）+ `stub`（测试）；**驱动转发 params→options（num_ctx 等）** | 去 anthropic/oauth |
| daemon | FastAPI（仅 loopback，非回环地址拒绝绑定）+ REST 最小面；**dream 链在 worker 线程执行** | 去 identities/accounts/tokens/admin |
| cli | `mnemoseed-local` | 统一以 REST client 面向 daemon；`init/up/doctor/uninstall` 为本地操作；config 操作强制 loopback |

## 4. 关键设计决策（含数次推翻重拟的痕迹；只有最终项计入）

1. **模型策略：单角色单路由 + 可选 ensemble 校验层（诚实成本版）**
   - 只有一个 dream consolidation 角色——单一 `llm.dream` 路由（driver + model + params）全包。SI/DR 两角色（short_increment / deep_reflection）是首轮草案的历史遗留，已删除（不设独立角色层；config 对旧键给出明确的 deprecation 报错）。仅当后续实测表明单一模型无法完成深度摘要时，再评估重新分支。
   - **可选 ensemble 校验层**（`dream.ensemble`，默认 `off`，档位门控见决策 8）：
     - `verify`：模型 B 逐条验证模型 A 的产出（判定任务比生成简单、省一半 token；缺点是 B 被 A 锚定，多样性收益弱）。**B 失败/格式崩坏的回退：采用 A 原样 + 审计记录**。
     - `vote`：A、B 各自对同一 delta 全量生成。**实现形状为单快照 + journal 扩展**：`REFLECT_A_DONE` / `REFLECT_B_DONE` 双相位 + `COMBINE_DONE` 边界，journal payload 携带来源 model_id（triple 级归因）；**确定性 combiner 消费 A/B 结果后单次 merge**——双 run 各跑一次完整 pipeline（双 merge）的形状**明确否决**（会双写图节点、绕开 combiner）。resume 语义：B 失败只重跑 B 相位；账本两次 reflect 各自记录 token。
     - combiner 语义（附录级确定性）："一致" = casefold + 归一化后 (subject, predicate, object) 相等；合成 confidence 沿用现有折叠公式（max + 0.05/重复提及，0.95 封顶）；分歧 → **isolated**（隔离保存，绝不投票消灭）；单方独有且低价值 → salvage。
     - **成本如实标注**：vote 是 journal/恢复语义/归因粒度的**机制改动**，不是零成本叠加层。
     - **MVP 不做 LLM 裁判**（第三个推理失败面更大、provenance 变浑）；高配仲裁留 Phase B 选配（见 advanced 档）。
     - ensemble 收益如实标定：滤格式崩坏、纠正误路由、滤抽样噪声/个体幻觉；**系统性幻觉同源相关，投票无效**——根本防线仍是 verbatim 通道 + provenance 回溯 + isolated 结构。
   - **budget 概念整体移除**（终版语义）：MVP 无任何 budget 上限；`dream.token_budget_usd` 键移除（旧配置给出 deprecation 报错）；token 账本只**记录**每个 run/路由的 token 消耗（append-only 遥测），不做封顶、无"超支后 capture-only"；移除 delta 的云端价目 USD 估算（SaaS 时代遗物）。未来 BYOK 阶段只保留一种例外：**用户自设用量上限**（防成本暴增，opt-in）；平台侧 budget 属于未来 SaaS（SaaS 无 BYOK），与本仓无关。
2. **底座选择**：照搬主仓 pluggable / ports-adapters 结构；schema / consolidate / decay / 审计红线一律保留原样，不允许私改；**room for refactor**：基线代码与本稿冲突处，以本稿为准做修正（含必要重构），修正单元列 Phase A2.5。
3. **做梦触发器**（用户拍板版本）：
   - `dream.floor_pool_points`（float，默认 `10.0`）：ScorePool 积分下限——每个 durable 捕获轮次按 S = 情绪 × 新颖 × 因果（0–10 分制）打分入池，余额达下限且空闲 ≥ idle 才做梦；
   - `dream.idle_min_sec`（默认 `900`）；
   - `dream.hard_deadline_sec`（默认 `86400`）：自最老 pending chunk 入池计时，满 24h 不论下限是否到达都强制做梦一次；**池内无 pending 则完全不跑**。daemon 构造 ScorePool 时直接绑定上述 config 值；触发即 drain（同批分数永不重复触发）。
   - **失败退避**：reflect 失败 / LLMUnavailable 时重置触发指纹并按指数退避重发（带上限 + 审计），杜绝"池已 drain、指纹不变、pending 永久积压"的死锁。
   - ScorePool 的防溢出强制触发上限曾硬编码 `forced_cap=50`：注册表化为 `dream.pool_forced_cap`（默认 `50.0`，须 ≥ floor，校验拦截）。
4. **Verbatim 直达**（已写入 MVP spec）：每个 turn 原文逐字进 chunk；合并前即可检索（Freshness Guard 探测 `consolidated=false`），dream merge 后退出搜索面（`consolidated=true`，原文保留为证据链、可按 provenance 追溯取回但不进向量召回）。**主检索轨（hybrid vector track）同样过滤 `consolidated=false`**，杜绝 chunk+node 双表示重复命中。检索面 = 合并产物 graph nodes + 未合并 verbatim chunks——MVP 阶段记忆一定可找回。
5. **摄取主通道**（优先级已拍板）：
   - ① **宿主 hook**：**OpenCode 为开发/测试默认宿主、首发适配**；Claude Code 与 Codex 第二优先级；Cursor 类 IDE 第三优先级。**hook 适配必须映射宿主会话生命周期**：消息事件 → `/ingest`；会话结束/空闲 → `/session/end`（或 pre-compact → `/flush`），只推 `/ingest` 会永不 drain。
   - ② 宿主方言文件观察（jsonl/sqlite 文件 watch，闲时约 30s 级轮询校验）：备胎第一位；
   - ③ MCP 网关：备胎第二位，骨架随 A3 交付。骨架表面定义：stdio 传输 + `recall` / `remember` / `dream_once` 工具集；capture 自动通道仍归 hook 主线。
   - 去重单元：`host_id + session_id + turn_range` 精确匹配（A3 实现约束；跨通道 turn_range 不一致时的兜底：宁可重复摄入由近重复检测吸收，也不丢）。
6. **许可 / 审计红线**：捕获中立（评分不读 anima/偏好）；provenance 只追加；记忆明文不离开本机（MVP 纯本地；BYOK 属 Phase B 后显式 opt-in）；审计 actor 显式归因；`api_key_env` 在一切展示面只显示 env 变量名 / `secrets:` 引用（字面量渲染为 `<redacted>`，写入侧直接校验失败）。
7. **Config 键全注册热切**：下列各键全部为 configwrite 注册表键，热生效，无需重启 daemon——**热生效含消费端 seam**（Merger / DeltaPacker / 调度器 / ScorePool 持 config 活引用或构造时绑定活读路径），逐键有回归测试。本轮新增：`dream.ensemble`、`dream.core_confidence_floor`、`dream.delta_budget_ceiling_tokens`、`dream.hardware_tier`、`dream.pool_forced_cap`；移除：`dream.token_budget_usd`。
8. **硬件档位与退阶运行**（以 RTX 3070 8GB + 32GB RAM 为 standard 档设计基准）：
   - **三档**（档位锚 = 新注册表键 `dream.hardware_tier`；init/doctor 硬件探测荐档，ensemble 门控挂 configwrite 校验：lite + `ensemble≠off` → 拒绝写入）：
     - `standard`（8GB+ 独显，或 32GB+ RAM 的耐心用户）：`qwen3.5:9b`（官方，6.6GB / 256K ctx），ensemble 全开，delta 上限 32k；
     - `lite`（纯集显超极本，16–32GB RAM）：候选锚点 `qwen3.5:4b`（官方，3.4GB / 256K ctx；型号 Phase B 实测定版），**ensemble 锁 off**，delta 上限 8k；
     - `advanced`（高配预留：24GB+ VRAM 级）：首选官方 `qwen3.8:27b`（18GB / 256K ctx，官方原生支持 `preserve_thinking` / `reasoning_effort`——跨批连贯性能力的关键供给）；备选官方 `qwen3.5:27b`（17GB / 256K）；低显存变体第三方 `smtek/Qwen3.8-27B` IQ2 系（IQ2_M-12gb 10.3GB 但仅 32K ctx）。**Phase B 先实测可行性与质量，不达标即移除该档**，达标后可作 ensemble 仲裁位。
   - **lite 档低置信降级通道**：新注册表热键 `dream.core_confidence_floor`——reflect 路由为 core 但 confidence 低于 floor 的 triple，在 merge 边界**确定性降级为 isolated**。standard 档默认 `0.0`（现状不变），lite 档抬高（具体数值 Phase B 实测标定）。**预期值如实标低**：它过滤的是模型自报的不确定度，自信的幻觉照样穿过；幻觉真防线 = isolated 结构 + ensemble 交叉验证。
   - **isolated graph 实例必需化**："分歧 → isolated"与"floor 降级 → isolated"都依赖 isolated 实例存在。init 模板与三档默认配置写入 `storage.graph.instances.isolated`；启动/doctor 做 capability 硬检查（缺失 = 明确报错，**绝不静默丢弃**），并有断言测试钉住"无 isolated 时不丢数据"。
   - **退阶的尊严退路：capture-only**。`dream.auto_trigger=false` 即"自动不合并"模式：chunk 不被自动合并，Freshness Guard 让原文搜索面常开，系统优雅退化为纯 verbatim 记忆；status/doctor 显示 dream disabled。注意语义边界：**手动 `dream --once` 仍会合并**；硬模式键（禁止手动 dream）待 Phase B 事实结论后再定。它是 lite 档的对照基准：**Phase B 实测 4B 巩固的事实准确率过不了 bar 的档位，直接推荐 capture-only，不发布毒药**。
   - **BYOK 推迟**：MVP 纯本地，`openai_compatible` 驱动仅作代码内保留；未来 BYOK 为显式 opt-in（用户知情、key 走 secrets ref），唯一可带的限制是用户自设用量上限（决策 1）。
   - **上下文窗口一致性**：doctor 校验公式为 `cache_prefix + delta + 生成余量 ≤ num_ctx`；ollama 驱动需转发 options（num_ctx、生成上限）。KV cache 量级写入档位说明：9B@32k ctx 在 16GB 机上不可行（lite 必须 4B@8k 的原因之一）；advanced 档注意 IQ2_M-12gb 的 27B 仅 32K ctx、256K 档需 16–24GB VRAM。delta 预算档位化键 `dream.delta_budget_ceiling_tokens`（standard `32000` / lite `8192`）；下限 5k 不档位化（4B CPU 灌 5k prompt 是分钟级，可忍）。
   - 明确的边界：调度感知（电源/负载/夜间跑 dream）**out of MVP scope**。

## 5. 流程图

```
hooks（OpenCode 首发，映射 /ingest + /session/end；
      jsonl/sqlite 文件 watch 备选）                →
  /api/v1/ingest（verbatim chunk）                 →
  ScorePool（S 分累计；pool_forced_cap 防溢出）      →
  DreamScheduler（floor+idle 或 hard-deadline；
      失败退避重发）                                 →
  ── worker 线程（不阻塞 daemon 事件循环）────────
  snapshot（原子落盘）                               →
  delta（动态预算分包，上限随档位）                   →
  dream（ollama；
        可选 ensemble：单快照双 reflect 相位 A/B
        + 确定性 combiner，单次 merge）              →
  三分流（core / isolated / salvage；
        core 经 core_confidence_floor 确定性降级；
        isolated 实例必需，缺失即报错）               →
  merge（写回 graph nodes）                          →
  mark_consolidated(chunk)                           →
  recall：graph nodes（长期）+ 未合并 chunks（新近；
        主检索轨过滤 consolidated=false）
  ++ decay sweep（按 decay.sweep_interval_s，consolidated ×3 λ）
  ++ ledger（纯 token 记录，无 budget）
  ++ audit（全程，actor 归因）
```

## 6. 阶段

- **Phase A1（已完成）**：config/secrets/schema/storage + embedder 移植，包名 `mnemoseed_local`。
- **Phase A2（已完成）**：capture/retrieve/dream/decay/llm drivers/daemon/CLI；触发器由"轮次计数"改为"积分池"（审查中指出后修复）；848 tests 全绿。基线 commit `a5adff9`。
- **Phase A2.5（基线修正包，先于 A3）**——盲审 MUST 的代码侧处置：
  1. 基线 commit `a5adff9` 全量重跑测试复验留档；
  2. dream 链（snapshot→reflect→merge）移出事件循环（`asyncio.to_thread` 或专用 worker；relay/trigger 只投递 job）；
  3. 主检索轨补 `consolidated=False` 过滤；
  4. 调度器失败退避（指纹重置 + 指数退避 + 审计）；
  5. 五键注册表化与消费端热读 seam：`dream.ensemble` / `dream.core_confidence_floor` / `dream.delta_budget_ceiling_tokens` / `dream.hardware_tier` / `dream.pool_forced_cap`（含取值校验：floor∈[0,1]、ceiling≥5000 且 ≤ ctx−余量、cap ≥ floor），逐键回归测试；
  6. `dream.token_budget_usd` 移除（含 deprecation 报错）与账本纯 token 化（移除 USD 估算）；
  7. ollama 驱动转发 params→options（num_ctx、生成上限）；
  8. isolated 实例必需化（init 模板写入 + 启动/doctor 硬检查 + 断言测试）；
  9. 默认路由对齐：`llm.dream` 默认 `ollama/qwen3.5:9b`。
- **Phase A3**：零依赖安装脚本（编排壳：装 ollama/uv → uv tool install → init → doctor 硬件探测荐档 → 用户确认后拉模型）、**OpenCode 宿主 hook 适配（首发，含会话生命周期映射 /ingest + /session/end）**、MCP 网关骨架（stdio + recall/remember/dream_once）、`.github` 工作流对齐（ci 触发器改 main + PR，删 development；release 保留 tag → PyPI trusted publishing）。包主体走 PyPI（`uv tool install mnemoseed-local` 本身即一行安装）。模型缺失 UX：init/doctor 引导 + `up` 启动检查、缺失时报错附 `ollama pull` 提示，**绝不静默拉取**（复用 bge-m3 懒加载先例）。
- **Phase B（后续，不纳入本稿）**：评测矩阵（档位 × off/verify/vote）与 bar 立项；lite 档 4B 型号定版（候选锚点 `qwen3.5:4b`）；advanced 档 27B 实测（首选官方 `qwen3.8:27b`，备选官方 `qwen3.5:27b` 与第三方 `smtek/Qwen3.8-27B` IQ2 系，不达标即移除）；`core_confidence_floor` 数值标定；`dream.capture_only` 硬模式裁定；BYOK opt-in（可带用户自设用量上限）；`needs_reconcile` 与 vote 分歧两套冲突机制的协同；ensemble 高配仲裁位。其余子项立项时再定。

## 7. 已识风险（主动承认）

1. dream 角色物化的审计记录（`llm_role_configured`）曾出现 env 名/引用混显；已统一为只渲染 env 名与 `secrets:` 引用、字面量 `<redacted>`，并有 `tests/test_audit_redaction.py` 钉住。
2. ScorePool 阈值必须完全 config-driven：daemon 构造 Pool 时直接绑定 `dream.floor_pool_points` / `dream.idle_min_sec`，与调度器同源（app.py 已钉住）；残余硬编码 `forced_cap` 已列 A2.5 注册表化。
3. 本地模型上下文窗口有限：跨批 digest 连贯性依赖模型能力（如 ollama 的 `preserve_thinking` 支持度）；delta 动态预算控制单次调用体量；摘要质量对模型选型敏感。**已知部署坑：ollama 驱动目前不传 params（连 num_ctx 都进不了请求体）**——A2.5 补 options seam，doctor 按 `prefix + delta + 生成余量 ≤ num_ctx` 校验；生成侧补输出上限防 32k JSON 截断。
4. `.github` 工作流仍是主仓 v0.1.1 形态（ci 触发 development/main 双分支、release 为 tag → PyPI trusted publishing）；与本仓单分支开发及 A3 安装形态的对齐重写放在 Phase A3。
5. 公开发布面最小化：AGPL-3.0 + 英文 README；其余发布物料待 A3 再定。
6. lite 档 4B 模型的事实准确率**未经实测**：`core_confidence_floor` 降级（预期值标低）+ isolated 结构 + ensemble 交叉验证是防线，Phase B 评测臂是最终把关；过不了 bar 就推荐 capture-only。
7. ensemble 合并逻辑若写得不干净会引入新的不一致：约束为**纯确定性 combiner**（无 LLM 裁判），分歧进 isolated 而非投票消灭；vote 的 journal 扩展成本已如实计价（决策 1），双 run 双 merge 形状已否决。
8. **dream 阻塞事件循环**（盲审发现）：现状整链同步跑在 daemon 事件循环上，lite 档单节 dream 可冻结 daemon 数分钟——A2.5 第 2 项修复，修法为 worker 线程化。
9. **调度器失败死锁**（盲审发现）：reflect 失败/LLMUnavailable 后指纹不变、池已 drain 则永不重发，pending 永久积压——A2.5 第 4 项修复。
10. **第三方量化包来源**：advanced 档首选官方 `qwen3.8:27b`（官方 library，18GB / 256K，`preserve_thinking` / `reasoning_effort` 原生支持）；低显存变体 `smtek/Qwen3.8-27B` IQ2 系属第三方命名空间——引入时须标注来源、tag pin 到具体量化版本，Phase B 实测质量后方可启用；IQ2（2-bit）损失不掩饰。
11. triple 级归因在 vote 模式下依赖 journal 扩展落地（决策 1）——工程单里必须带 resume/归因的对抗测试（A/B 分别中断、重复 consume、半态账本）。

## 8. 附注

- 本 spec 覆盖 Phase A 全部决策点。已实施部分以 848 tests / ruff / mypy / format 全绿收尾，基线 commit `a5adff9`；基线复验已完成（848 passed, 5 skipped, 47.94s，2026-08-16 留档）。A3 动工须经 A2.5 完成且 QA 通过。
- 本地模型阵容：`qwen3.5:9b`（standard 档默认，官方，6.6GB / 256K ctx）；`gemma 4 12b`（ensemble 校验位，Q4 ≈ 7.6GB，估算值，启用时懒拉取）；`qwen3.5:4b`（lite 档候选锚点，官方，3.4GB / 256K，型号 Phase B 定版）；advanced 档首选官方 `qwen3.8:27b`（18GB / 256K ctx，`preserve_thinking` / `reasoning_effort` 原生支持；备选官方 `qwen3.5:27b` 17GB），低显存变体第三方 `smtek/Qwen3.8-27B` IQ2 系（IQ2_M-12gb 10.3GB / 32K ctx），Phase B 实测定去留。选型落地只改 `llm.dream` 路由配置，无需改代码。
- 当前代码默认路由仍为 `ollama/llama3.1:8b`（A2.5 第 9 项对齐）。
- ensemble 的 reflect prompts 与单模型共用同一 `PROMPT_VERSION` 演进机制，不另立。
- budget 终版语义：本地无上限，账本只记录 token；BYOK 阶段仅保留用户自设上限（opt-in）；平台 budget 属未来 SaaS 且 SaaS 无 BYOK。
