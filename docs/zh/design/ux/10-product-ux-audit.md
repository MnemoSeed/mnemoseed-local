# 10 · 产品完整体验审计（install → 首记忆 → 召回 → 信任）— UX 规格

> 归属：跨表面审计（CLI + 控制台 Overview / Atlas / Config / Profiles / Dream + README 引导）。这是**设计规格与整改清单**，供工程按批次实现；本文不落一行产品代码。
> 依据（均已核读，行号以当前工作树为准）：`src/mnemoseed_local/cli.py`（1355 行）、`src/mnemoseed_local/console/static/index.html`（613 行）、`app.js`（2104 行）、`style.css`（333 行）、`docs/zh/design/ux/08-memory-atlas-spec.md`、`README.md`（202 行）。
> 方法：先"用户心理模型"，其次"实现名"；每条整改均落到 `文件:行号`。本文**不改代码、不提交**。
>
> 已按 `impeccable` Setup 读取既定视觉系统（style.css 的 Restrained 语汇、`--atlas-*` 色板、btn/badge/pill/progress 组件），本审计为 **Operate 模式**的 refine/harden 型整改，不替换既有视觉世界。

---

## 0. 摘要（Top-3 可立即绿灯）

按用户影响从高到低，三条最低成本、最高回报的整改可先批：

| # | 整改 | 用户影响 | 涉及面 | 成本 |
|---|---|---|---|---|
| **T1** | 修复 Control 核心重复 `id` 导致的 "Config 页边编辑边鬼影"：`id="config-body"` 与 `id="config-foot"` 各出现两次，`app.js` 的 `$("#config-body")` 命中隐藏的 Overview 卡片而非 Config 表单容器。重命名 Config 页的容器 id（如 `cfg-fields-body` / `cfg-fields-foot`），`app.js:347/414` 同步。 | 用户在 Config 页改配置，实际文字/报错/脚注写进**不可见的 Overview 卡片**；刷新瞬间丢失、版本历史不联动。信任受损的第一来源。 | `index.html:105/111 vs 422/423`；`app.js:347,372,414` | <1h |
| **T2** | Overview 死输入：未建任何 Profile 时 `Dream` 标签的 `dream-profile` 与 Atlas 的 `atlas-profile` 面临空态误导；且「Everything is captured automatically」的价值主张在 UI 中**无任何处可见**。在 Overview 加一个"第一条记忆"引导卡（空态当道：`0 memories → 抛一个最小 remember 示例 + 到 Atlas 看全景`）。 | 新用户 60 秒内无法确认"我的记忆真的存进来了"——这是从 install 到信任的最大断层。 | `index.html` Overview 区块（约 :56 前）+ `app.js:loadOverview` | <2h |
| **T3** | 一条"路径一致性"整改：CLI 输出（`cli.py:736,760,798,822`）与控制台（`app.js:1960-2043`）对同一动作（remember/recall/forget/supersede）文案不一致；统一为同一句可粘贴的成功反馈，并让 One 条目的 CLI recall 行为与控制台 Atlas 的 `role="row"` 行同模式（一条=一行）。 | 动力面（CLI）与管控面（控制台）换着用的人会被两种"记住了"语言割裂。 | `cli.py` 各 cmd_* 打印处 + `app.js` action-feedback | <2h |

> 上述三条只动 `console/static/` 与 `cli.py` 的**打印文案级**改动；`T2` 的引导卡若需后端空库计数可先只读现有 `/api/v1/observability.capture_ingest_count`（`app.js:198` 已消费）。

---

## 1. 用户与任务总览

产品是 **CLI-first 的本地记忆层**，控制台是视力镜（Observe）＋个别动词（Act）的管控面。用户是**开发者/重度对话用户**，心理模型是"我告诉过一个 AI 某件事，它现在替我记住了"——而不是"vector chunks 与 graph nodes 的技术分层"。

任务闭环：**install → 首次对话被记 → 想查被记到什么 → 信任它真在记、真能召回、真不会漏**。

- 谁：单机开发者，已有 Coding Agent（opencode 默认、claude_code 可选），不想要云、不想要账号、不想读架构文档才知道怎么用。
- ONE thing：**"我选的 Agent，用完一轮后，我明天回来，它还能接上我昨天的手。"**
- 当前失败模式：记忆是**隐式沉淀**（hook 每轮自动 ingest，`README.md:12-16`），但用户**无法在 60 秒内亲眼确认它发生了**；控制台里 Overview 讲的是"driver 名 + 计数器"，不是"你记住了什么"。

---

## 2. 旅程断层地图（install → 首记忆 → 召回 → 信任）

每格给用户困惑 → 责任表面 → 落点。

### 2.1 Install 段：README 强、UI 空转

| 断层 | 用户困惑 | 责任面 | 落点 |
|---|---|---|---|
| `README.md:43-70` 引导完备（一条命令、`up`、`hook install opencode`），但**无任何"启动后去哪验证"**的桥。文档以 `doctor`/`status` 收尾，闭口不提控制台也是入口。 | "装完了，然后呢？我怎么知道它工作？" | README 引导 + CLI 的 `init` 尾声 | `cli.py:46-61` `cmd_init` 只打印三行；建议末行加 `4. 浏览器打开 http://localhost:7788（控制台看全景）` |
| 控制台 `index.html:43-46` 的 daemon 不可达 banner 文案是**强引导**（`run mnemoseed-local up`），但 Overview 三个卡片在 daemon down 时全部 `muted` 占位（`app.js:164-166`），没有"先装 hook 才有数据"的提示。 | "我起了 daemon，为什么 Overview 全是占位/无记忆？" | Overview 空态 | `app.js:158-169` 下行分支 + Overview 无数据分支 |

### 2.2 首记忆段：隐式沉淀看不到

| 断层 | 用户困惑 | 责任面 | 落点 |
|---|---|---|---|
| 记忆靠 hook 每轮自动 ingest（`README.md:12-16`），但**控制台没有任何一处说"你的每个对话正在被记录"**。Overview 的 `/api/v1/observability.capture_ingest_count`（`app.js:198-205`）是唯一证据，但藏在"Observability"卡片里，文案是"Capture ingests"，不是一个"你已记下 N 条"的事实句。 | "这玩意到底在记吗？" | Overview 空态/计数 | `app.js:200-206`；建议文案 `You've captured N memories so far`，并在 0 时给引导卡 |
| Atlas 空态文案（`index.html:243`）`No memories yet — start a conversation, then return here.` 是对的，但**无 CLI 兜底**——控制台里没有任何写入口（设计决议 3 禁止就地改写，三动词都在抽屉里，`index.html:396-399`）。新用户若不用 Agent 而只想试 CLI，得回去读 README 才知道 `mnemoseed-local remember`。 | "我没有 Agent 会话，也想先塞一条试试，怎么塞？" | Atlas 空态 + README | 空态引导卡加一行 `CLI: mnemoseed-local remember "We decided to use LanceDB for vector storage"` 示例（见 T2） |

### 2.3 召回段：CLI 结果无法与"记忆体"对上

| 断层 | 用户困惑 | 责任面 | 落点 |
|---|---|---|---|
| `cmd_recall`（`cli.py:713-744`）输出 `[{kind}] (0.93) text [flags]`——**没有 id，没有衰减，切换不到控制台对应条目**。控制台 recall 是 Atlas 抽屉（read-only 明细）。CLI 回忆结果与可视化记忆体之间没有桥。 | "它召回的是哪一条？我能在 Atlas 里点开它吗？" | CLI recall 输出 | `cli.py:729-744` 若非 `--json`，可加 `#<short id>` 与 URL 屏（`http://localhost:7788/#/memory/atlas?...`）；或至少提示 `see it in the Atlas` |
| recall 无覆盖上下文：`coverage` 那行（`cli.py:737-743`）是 `vector_hits/graph_hits/profile_chunks`，对用户是黑话。 | "vector_hits=12 跟我上午说的话什么关系？" | CLI recall 输出 | 非 `--json` 时覆盖行改一句人话，例如 `Matched 3 memories across your conversations.` |

### 2.4 信任段：衰减/遗忘的诚实度够，但「找回」入口断层

| 断层 | 用户困惑 | 责任面 | 落点 |
|---|---|---|---|
| Forget（`index.html:583-587` 对话框 + `app.js:1931-1943`）、Supersede 都是**不可逆/高风险**，但 CLI `cmd_forget`（`cli.py:803-823`）**无确认**，直接 `POST /memory/forget_this`。控制台可逆性语义（nodes 保留版本链）与 CLI 剑即斩之间不一致。 | "CLI 一句 `forget` 就删了？那 `--kind node` 和 `chunk` 有什么区别?" | CLI forget | `cli.py:803-823` 增加 `--yes`/`--dry-run` 默认交互确认；`--kind` 帮助语（`cli.py:1242-1247`）解释 chunk/node/entity 差异 |
| 衰减曲线/forecast（`index.html:342-350`）、Rescue 文案（`index.html:356`）是诚实且具启发教育性，但**Drivers 卡（`index.html:84-95`）把 vector/graph/meta/embed driver 名糊上去**（`app.js:216-221`），这是实现名不是人话。 | "Vector: lancedb_embedded —— 我该关心吗？" | Overview Drivers 卡 | 缺 driver 时的降级语义（`/health` degradations，`app.js:187-188`）保留；driver 名只作为**降级警示**出现，健康时代以 `All memories backends healthy` 替代 |

---

## 3. 各表面整改方向（按用户影响优先级）

> 五个设计原则为标尺：渐进披露 / 无死输入 / 文案即教学 / 早验证说人话 / 一模式多用。

### 3.1 【P0】修复 Control 核心重复 id（T1 之因）

**实锤**：`id="config-body"`（`index.html:105` 与 `:422`）、`id="config-foot"`（`:111` 与 `:423`）各重复。
`app.js:347` `const body = $("#config-body")`（`$` = `querySelector` 首个命中）→ **命中隐藏的 Overview 卡片**（:105）；`renderConfigForm`（`app.js:372`）`body.innerHTML=...` 写进 Overview；`app.js:414` `$("#config-foot").textContent=...` 同样写 Overview。Config 页真正想驱动的表单容器（:422/:423）**从未被驱动**，config 编辑、错误信息、脚注全部鬼影般地落进 Overview。

**整改**：把 Config 页的 `id="config-body"`→`id="cfg-fields-body"`、`id="config-foot"`→`id="cfg-fields-foot"`（各 1 处 HTML + `app.js:347/422`、`app.js:414` 共 3 处 JS 引用）；Overview 的保留原名。另把 `loadConfigPage`/`renderConfigForm`/`cfgFieldError` 里所有 `$("#config-body")`/`$("#config-foot")` 的引用集中为常量，杜绝再被 `querySelector` 首命中传染。

### 3.2 【P0】Overview 成为"记忆第一眼"，而非"driver 状态板"

当前 Overview（`index.html:56-112`）把三个卡片给了 Status/Observability/Drivers，最底部才有一个 read-only Config 大卡片——**用户要的"我记住了什么"完全缺席**。

- **新引导卡（空态当道，`loadOverview` 在 `app.js:124-250` 无数据时渲染）**：当 `capture_ingest_count === 0`（`app.js:198`），在三个卡片上方放一张 Hero 引导卡：
  - 主句 `You haven't captured anything yet.`
  - 副句 `MnemoSeed records every conversation automatically — use your agent normally and memories appear here.`
  - 两个动作：`View Memory Atlas`（`#/memory/atlas`）+ `Try CLI: mnemoseed-local remember "we use pydantic for config parsing"`
  - 计数句 `You've captured N memories so far`（`capture_ingest_count` 改为事实句，替掉"Capture ingests"）。
- **Drivers 卡（`index.html:84-95`，`app.js:212-224`）**：健康时（`gate.ok`），driver 名折叠进 `<details>`/tooltip，第一眼只给 `Memories backends: healthy`；仅当 `/health` 有 degradations/hard_missing（`app.js:187-188`）才展开 driver 表并说人话 `Embed search degraded — memories may come back incomplete`.
- **Config read-only 卡（`index.html:99-112`）**：48 行扁平的 dot-path（`app.js:230-245`）对用户是配置转储。改为 Top-5 关键状态（`dream.auto_trigger / decay.enabled / capture.auto_recall / profile count / storage health`）+ `Open full Config →`，把 Config 深判交给已存在的 Config 页一致递进披露模式。

### 3.3 【P1】Atlas 信息架构漂移：spec 说二级，实现是顶级且无 Search

`08-memory-atlas-spec.md §3`（`08-memory-atlas-spec.md:55-66`）规划 **Memory 下的二级 `Search | Atlas`**；当前实现把 Atlas 升为**顶级 tab**（`index.html:31` "Memory Atlas"），且**顶级导航里没有任何 Search 表面**（Overview 里也没有召回框——spec 说的 "Overview（保留：召回搜索框 + 5 条结果 + coverage）" 未落地）。

- 矛盾点：控制台唯一可手动召回/查询的科研入口走 CLI `recall`（`cli.py:713`）与 MCP `recall`（`README.md:99`），控制台内无查询框。
- **整改（适度，不照搬 spec 重构图）**：在 Atlas 头部 FilterBar（`index.html:140-208`）上方加一个轻量 `Search memories…` 输入（→ `POST /memory/recall`），回车把结果作为 Atlas 视图外下拉展示，点中某条跳 `#/memory/atlas?...&selected=<id>` 开抽屉。复用 `#drawer` 而非新建查询页。此项依赖已具备端点 `POST /memory/recall`（`cli.py:718` ⇒ `daemon/memory.py:127-137`），**后端零新增**。

### 3.4 【P1】Config 页：带走"防抖/即存即验"但去黑话

Config 页（`index.html:409-436`，表单渲染 `app.js:372-415`）已经做对：无死输入（boot-scoped 只读插徽标，`app.js:428-439`）、早验证（`cfgFieldError` 对 409/422/403 说人话 `app.js:544-577`）、版本历史 append-only（`app.js:593-644`）。**后续只补三处文案教学**：

- `recall.rescue_floor / rescue_cue_min`（`app.js:304-306`）标 `readonly`，hint 是 `(0,1] — lower bound of the rescue band`——对用户无意义。改为教学句 `Memories below this floor still resurface when current conversation cues overlap enough — file-scoped for now.`
- `dream.llm.*.api_key_env`（`app.js:312`）hint 已有 `Env-var NAME only`——很好；但 driver=ollama 时该路由根本不需要 key（见下 3.5）。
- 版本历史表（`index.html:425-435`，`app.js:600-616`）列 `Version ID`（`vid`）是内部主键，用户点了 `Restore` 就要理解它；建议把 `vid` 折叠进 tooltip，按钮保持 `Restore`，只在 `conflict` 时才需要给 `vid`（`app.js:546` 的 409 提示已�明确）。

### 3.5 【P1】无死输入：Config LLM 路由按 driver 收敛字段（progressive disclosure）

`LLM_FIELDS`（`app.js:308-318`）对每个角色渲染**全部 9 个字段**（`app.js:393-396` 逐字段 `cfgRowHtml`）。但 driver 是 `ollama` 时，`api_key_env / base_url / provider / think` 是空转（ollama 不走 key、端点固定 `localhost:11434`）；driver 是 stub（`app.js:309`）时 model/ctx 无意义。

- **整改**：`renderConfigForm` 在渲染 `dream.llm.{role}.*` 前先读当前 `cfg[driver]`，driver=ollama 只显示 `model / num_ctx / num_predict / max_tokens / think`；driver=openai_compatible 显示 `base_url / api_key_env / model / provider / max_tokens`；stub 只给 `model`。**在用户切 driver（已有一个 `select[data-key="dream.llm.dream.driver"]`）后按新 driver 前端重渲染同组行**，实现"改 driver→字段即时收敛"的早验证闭环。这与设计原则 2/3（No dead inputs、Provider-first）一致。
- **Flag**：这是控制台前端收敛；后端 `CONFIG_KEY_REGISTRY` 无需改（写仍按 key_path 落库，多余字段只是不展示）。请在评审会确认这是纯前端约束（我判断是）。

### 3.6 【P2】Profiles 页：绑定编辑器缺即时孤儿提示

Profiles（`index.html:438-496`）：`Create` 表单、列表「Archive 永不删除」banner（`index.html:446-448`）、Orphan banner（`:449-451`）都诚实。但 `renderBindings`（`app.js:727-751`）把孤儿标红只有在**绑定行加载后**才触发 `refreshOrphanBanner`（`app.js:795-809`）；用户添加新行（`bindings-add`，`app.js:2024-2039`）时 `.is-orphan` 不会实时刷新——选一个被删 profile 的孤儿行，`Save` 前无提前警示。

- **整改**：`bindings-add`（`app.js:2033`）与 `binding-pid` `change` 上挂 `refreshOrphanBanner`；`Save bindings`（`app.js:765-793`）若仍含孤儿，在 `profiles-orphan-banner` 明确要求修复而不是静默落盘。

### 3.7 【P2】Dream 页：手动跑一次的最大断层是"没有进度感"

`runDreamOnce`（`app.js:929-960`）：点 `Run now` 后 30s 无超时中止，`fb` 文案只 `Running… this can take a while.`，没有中间反馈；长梦境让用户怀疑卡死（`app.js:949` 超时提示 `may still be running` 是犹疑剂）。Dream status（`app.js:828-850`）只有 `state` 名称，无"此刻在做哪一桶"的进度粒度。

- **整改（后端如需则列 §5）**：`Run now` 期间每 5s 轮询 `POST /dream_status` 把 `state`/`current_range`（`app.js:842` 已存在字段）显示到 `dream-run-feedback`（`index.html:537`），输出 `Extracting… (12/87 turns)`；按钮给 `aria-busy`。后端无需新增——`current_range` 已返回。

### 3.8 【P2】一模式多用：CLI 输出与控制台文案统一

`cli.py` 的各 cmd_* 打印（`:736 recall`、`:760 remember`、`:798 dream launched`、`:822 forget`、`:928 config set`、`:977/997 profile create/archive`）与控制台 action-feedback（`app.js:1900/1921/1927/1939`）对同一动词文案不一致（CLI `remembered: reinforced (chunk …)` vs 控制台 `Reinforced.`；CLI `launched: true` vs 控制台 `Launched — check status above.`）。

- **整改**：抽一套动词级反馈句（见 §7 文案清单），CLI 与控制台共用；`--json` 分支维持机器可解析不变（`cli.py:728/775/819/926` 等仅包装人读层）。

---

## 4. 聚类整改：一模式、多表面、不新建设计系统

- **空态语汇**（`app.js` 分散在 Atlas `:241-249`、Overview `:164-166`、Profiles `:674`、Config `:596`）已用 `.canvas-empty`/`.muted` 统一——延续即可，不新造。
- **按钮/徽标/进度条**（`style.css:120-137,196-200`）已是一套；本审计所有新元素都从这套取。
- **后门入口**：控制台顶部无 `doctor`/`status` 对应 surface；用户诊断只能回 CLI。建议在 Overview 的 `Drivers` 卡放一个 `Run doctor`（复用 CLI 已实现的检查，但控制台则要新增 `POST`——见 §5 待建），或至少放一个 `<details>` 把 `/health` 的 `hard_missing`/`degradations`（`app.js:187-188`）说人话。

---

## 5. 后端需求（设计依赖 vs 已存在）

> 标记「待建」= 本设计若要落地，须同步新增后端能力；「已有」= 直接复用现状，零新增。

| 設計需求 | 后端 | 状态 | 证据 |
|---|---|---|---|
| Config 页正确驱动自己的 body/foot | 无 | **已有**（纯前端 id 修复） | `index.html:105/111 vs 422/423` |
| Overview 引导卡计数 | `/api/v1/observability.capture_ingest_count` | **已有** | `app.js:198` |
| Atlas 顶部 recall 搜索框（§3.3） | `POST /memory/recall` | **已有** | `cli.py:718` ⇒ `daemon/memory.py:127-137` |
| Dream 进度轮询 | `POST /memory/dream_status` 的 `state/current_range` | **已有** | `app.js:828-850,842` |
| Config driver 级字段收敛（§3.5） | 无（前端只展示约束） | **已有**（需评审确认为纯前端） | `app.js:308-318,393-396` |
| Profiles 孤儿即时刷新 | 无 | **已有**（前端事件挂接） | `app.js:2024-2039,795-809` |
| CLI forget 交互确认/`--dry-run` | 无（纯 CLI 交互层） | **已有** | `cli.py:803-823,1240-1249` |
| CLI recall 结果带 id/Atlas 桥 | `AtlasItem.id`（recall entry 是否已含 id 需核） | **待核** | `cli.py:730-736` entry 字段；请先看 `daemon/memory.py` recall 出参 |
| Overview「Run doctor」按钮 | 若放控制台需新增 `POST /diagnostics/doctor`（复用 `cli.py:cmd_doctor` 的检查函数） | **待建**（或先降级为 `<details>` 呈现 `/health`，零后端） | `cli.py:258-367`、`app.js:187-188` |

> §3.5 的 driver 收敛是**设计决策**（Do we keep unheard fields saved though hidden?——我建议隐藏即不展示但保留已存值不被 UI 清空；见 §6 决策项 D1）。

---

## 6. 待 owner/编排者拍板的决策（Decisions needed）

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| **D1** | Config driver 收敛后，用户把 driver 从 openai_compatible 切回 ollama，已填的 `base_url/api_key_env` 是保留（DB 仍在，UI 隐藏）还是清除？ | A) 保留已存值仅在换 driver 时隐藏（推荐，符合"记忆永久"心智，避免误删凭据） / B) driver 切换即清空不适用字段 | **A**（隐藏不删除；`cfgSecretDisplay` 已只读展示 env 名，`app.js:469-473`） |
| **D2** | Overview 应否在空库时用「假数据」点亮 Atlas，还是要用户真实对话才见点云？ | A) 严格真实：空则空态引导（推荐，符合"诚实呈现"理论锚，`08-memory-atlas-spec.md:562-563` 强调不借"炫即真"） / B) 造 3-5 条 demo 样例演示 3D 效果 | **A**（诚实优先；引导卡 + CLI 示例足够） |
| **D3** | CLI recall 结果加不加「控制台 Atlas 深链 URL」？ | A) 加（复制即开，闭环「召回→可视化」） / B) 只提示 `see it in Atlas` 文本 | **A**（成本低，闭环价值高；但 URL 需带 profile/selected，`app.js:1028-1040` 已有 hash 契约可复用） |
| **D4** | `doctor` 是否上控制台变成可点击动作？ | A) 暂不上，Overview 用 `<details>` 呈现 `/health` 人话（推荐，零后端，渐进） / B) 新增 `POST /diagnostics/doctor`（复用 `cmd_doctor` 逻辑，`cli.py:258-367`） | **A**（先零后端；真用户诉求再 B） |

---

## 7. 英文文案清单（Copy Deck — 实现时原样粘贴）

> 公开产品文案一律英文，无内部黑话、无实现名、无日期/决策人。

**Overview / 空态引导**

- `You haven't captured anything yet.`
- `MnemoSeed records every conversation automatically — use your coding agent normally and memories appear here.`
- `View Memory Atlas`
- `Try it on the CLI:` `mnemoseed-local remember "we use pydantic for config parsing"`
- `You've captured {N} memories so far`
- `Memories backends: healthy`
- `Embed search degraded — memories may come back incomplete.`

**Atlas / 召回搜索**

- `Search memories…`
- `Open in Atlas`

**Config（去黑话教学）**

- `Below this floor, memories can still resurface when the current conversation's cues overlap enough. (file-scoped for now)`
- `This route runs on ollama — key not needed.`
- `Configuration is versioned; every save is audit-logged.`

**CLI（统一动词反馈，与控制台 `app.js:1900-1939` 同款）**

- remember → `remembered: {outcome} ({chunk})` · 控制台同 `Reinforced.` 当经 remember 走 reinforce 分支（`app.js:1895-1906`）
- recall 人读行 → `[{kind}] ({score:.2f}) {text}` · 覆盖句 `Matched {n} memories across your conversations.`
- forget 确认 → `Forget {kind} {id}? This cannot be undone. Run again with --yes to skip.`
- supersede → `Superseded.`（与 `app.js:1927` 同）

---

## 8. 可访问性与粘合性（A11y / Cohesion，当前实测）

### 8.1 重复 id（已核，实锤）

- `id="config-body"`：`index.html:105`（Overview）与 `:422`（Config）重复。
- `id="config-foot"`：`index.html:111`（Overview）与 `:423`（Config）重复。
- 后果：`app.js:347,372,414` 的 `$()` 首命中 Overview 隐藏元素；见 T1。**已实证**。
- 老生常态建议：给全局 `id` 做唯一性检查进 CI（`scripts/gate.ps1` 可加一道 `grep -E 'id="[^"]+"' | uniq -d` 审计；gate 在 `AGENTS.md` 满绿要求内）。

### 8.2 ARIA / 语义（已读 `index.html` 逐行）

- 顶级导航 `nav.tabs` 用 `aria-current="page"`（`index.html:30` 与 `app.js:108`）正确；但 `tab` 用 `<a>` 而非 `role="tab"`/`tablist`——**可接受**（是超链接导航不是 ARIA tab 模式），无需改。
- Atlas 分段 `segmented`（`index.html:123-126`）用 `aria-pressed`，语义正确（不是 radio）。`role="group"` + `aria-label="View mode"` 正确。
- `atlas-canvas-wrap`（`index.html:237`）`role="img"` + `aria-label` 键盘引导，`tabindex="0"`：正确的操作面（Operate）；但**内部两个 `<canvas>` 都 `aria-hidden="true"`**（`:238,:239`）→ 屏幕阅读器只能靠 wrap 的 label，悬停/点选内容不可读。spec (§13) 要求 `aria-live` 列出选中项标题——`#a11y-live`（`index.html:609`，`app.js:63-69`）已存在但 openDrawer 未向它广播选中标题。**建议**：`openDrawer` 成功后 `a11yLive(\`Showing details: ${title}\`)`。
- `dialog`（`index.html:579-603`）用原生 `<dialog>`：焦点陷阱/`::backdrop` 均原生，正确。但 `dlg-forget`/`dlg-supersede` 的 `<form method="dialog">` 的 confirm/cancel 用 `value="confirm"`（`:585/:585/:600`），`close` 事件判 `returnValue`（`app.js:1921/1935`）——可用；建议按钮加 `aria-label` 明确（虽文本已有）。
- List 虚拟行：`role="row"`/`role="cell"`/`role="columnheader"`（`index.html:276-285`，`app.js:1712-1721`）+ 容器 `role="table"`（`:287`）——无 `<table>` 的"表格"ARIA 模型可接受；但**没有 `<caption>`**（spec §13 要求），且行是 `<div tabindex=0 role=row>` 而非 `role="row"` 内嵌可聚焦交互。
- 图标徽标：Forget×/回退按钮（`index.html:305,308` 的 `×`；`index.html:307` drawer-close；`app.js:1711` 的 `⬡/●`）——`drawer-close` 有 `aria-label="Close details"`（`:305`），**好**；但 List 行内 `●/◍/RC/PIN`（`app.js:1706-1709,1713`）纯色/字符无文本替代，且 `.badge` 默认不含 `aria-hidden`，读屏会逐个读符号。**建议**给这些行内标志 `aria-hidden="true"` 并放 `title` 语义（字面已有 `title="Conflict"` 等，但因 `aria-hidden` 缺失读屏仍先读符号字符）。

### 8.3 焦点可见 / 颜色 token

- 全局 `:focus-visible{box-shadow:var(--focus)}`（`style.css:57`），`:focus`（input/select `style.css:144`）也是 box-shadow——**焦点不靠颜色**（有 box-shadow 环），符合 spec §13；`--focus` 是 `rgba(15,118,110,.35)`（`style.css:26`）——在浅底 OK、在深灰 `--atlas-canvas` 上不够醒目。**建议**：canvas 内可聚焦 wrap 用亮一点的焦点环（覆盖类）。
- 徽标/色板一致性（`style.css:130-137 / 273-276`）：`badge-muted / badge-warn / badge-restart / badge-live / badge-source / badge-boot / badge-pin / badge-pending / badge-archived` 已一套，但 `badge-restart`（`:273`）与 `badge-pin`（`:132`）**颜色几乎相同**（都是 `#FFF1E6`/`#FFD8B8`/`#9A3412`）——两个语义完全不同的徽标同色，属 cohesion 缺陷。`progress-bar.is-healthy`（`:198`，`#16A34A`）与 `--ok`（`:21`，`#15803D`）双绿共存。**建议**：把"重启"语义移出 `#9A3412` 暖橙（那是 pin/危险），或给 restart 换琥珀（danger-adjacent）且给 pin 保留原色；为 `progress-bar` 补充注释区分 `--ok`（daemon 绿）与 healthy 绿。

### 8.4 空态（现状盘点）

- Atlas：`atlas-empty`（`index.html:241-245`，空星 + 引导）与 `atlas-filtered-empty`/`list-empty`（`:246-249,291-294`，过滤空 + Clear）双态齐全，文案可操作。
- 但 **Overview 无数据态**（`app.js:233-235` 只 `Config empty.` / `:164-166` 全 muted）**没有引导卡**——T2 补齐。
- **Dream 空态**：`loadDreamStatus`（`app.js:846-849`）失败给 muted 文本，但 `Dream Run` 卡（`index.html:520-539`）在无任何记忆时仍可点 `Run now`（虽然 `dream_once` 会因空池 no-op）——建议 when pool empty 时按钮禁用 + 文案 `Nothing to consolidate yet — memories appear after a few conversations.`（`dream_status.pool.balance`，`app.js:855`）。

### 8.5 移动端（<768px）

`style.css:325-332` 把 Drawer 变底部 sheet、filterbar 横向滚动；但**手势**：spec §6.2 要单指旋转/双指缩放，实现里 canvas 只有 OrbitControls 鼠标（`app.js:1373-1378`）+ Pointer 拖抽屉（`app.js:1963-1988`）——**无触摸轨道**。且 `filterbar` 横向滚动（`:327`）需要 touch，原生 `<select>` 在窄屏是原生下拉，可接受；建议至少确认 `three` 的 OrbitControls 支持 touch（默认是，验证即可）。

---

## 9. 验证计划（Playwright 实页 + 门禁）

> 遵循 `08-memory-atlas-spec.md §21.2` 的硬隔离：临时 `MNEMOSEED_HOME` + 空闲端口 daemon（如 `17888`），**绝不触碰 dogfood 7788** 与真实 `~/.mnemoseed-local`。

这批整改的实页断言：

1. **T1（重复 id）**：`page.goto("http://<spare>:<port>/#/config")`，改任一 key → `#cfg-fields-body` 收到 value/错误，而 `#view-overview`（hidden）下 `#config-body` **无**改动；`page.evaluate(()=>document.querySelectorAll('[id]'))` 断言无重复。
2. **T2（Overview 引导卡）**：fresh temp home、0 capture → Overview 出现 `You haven't captured anything yet.`；跑一次 `--remember` 后刷新消失。
3. **A11y**：每 tab 用 Playwright 键盘走查（`Tab` 顺序 TopBar→内容→Drawer）、`aria-live` 在 openDrawer 后收到标题；`axe-core` 扫描 Atlas/Config/Profiles（`role=row` 无 caption、focus 环在深底、双击 id 均会被 axe 抓到）。
4. 回归：`pwsh -File scripts/gate.ps1` 保持绿（pytest/ruff/format/mypy；本文 docs-only，预期零 `src` 变更，除 T1 的 id 重命名属产品代码，单列 PR）。

---

## 10. 越界机会（仅记录，不展开，交编排者分流）

- 控制台顶部缺一个跨帅的 `doctor` 可视化入口（§6 D4 的 B 方案）。
- 「召回→可视化」闭环的 CLI 深链 URL（D3 A 方案）若做，需与 `app.js:1028-1040` 的 hash 契约对齐、并加后退恢复。
- Overview 的 Observability counters（`app.js:195-209`）与 doctor 的 `_daemon_activity`（`cli.py:402-408`）各读一遍同一份 `/api/v1/observability`——可在后端收敛为一次，但属公共库，不属本审计范围。
- `Embed search degraded` 的 doctor 级恢复指引（何时 rerun embed）需要后端 `doctor` 逻辑复用，留给 D4 一起拍。

---

## 11. 验证锚（核读依据）

- 重复 id：`index.html:105/111/422/423`；`app.js:347,372,414`。
- Overview 计数/Drivers/健康：`index.html:56-95`；`app.js:158-250`（尤其 `:187-188,198-206,216-221`）。
- Config 架构（无死输入/早验证/版本）：`index.html:409-436`；`app.js:273-306,372-415,424-467,497-577,593-644`。
- Profiles 孤儿：`index.html:438-496`；`app.js:727-751,765-809,2024-2039`。
- Dream 进度：`index.html:498-575`；`app.js:814-873,929-960`。
- Atlas 搜索/空态/抽屉：`index.html:116-407`；`app.js:1028-1744,1751-1960`。
- CLI 动词：`cli.py:46-61,258-367,713-823,928,961-1001,1015-1104`；parser `1189-1312`。
- Spec：`08-memory-atlas-spec.md:55-66,359-388,440-549`（IA、后端需求、文案、a11y）。