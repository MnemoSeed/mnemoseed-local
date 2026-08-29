# 11 · 溯源透明的召回（Provenance-Transparent Recall）— R2 信任面 UX 规格

> 归属：产品经理 R2 完善度评审 top-2 建议 —— 第 7 天流失头号原因「噪声 → 不信任 → 卸载」。
> 一句话定位：用**既有溯源**，给被注回的上下文一条「用户可审计的来龙去脉」，让「为什么冒出这条 / Pin 的还是抓取的」一目了然 —— **不新建置信模型、不新排名、不新 LLM 调用**。纯 UI 信任面，骑在已落地 provenance 上。
> 目标：G1（可解释性「为何冒出它 / 为何不是另一些」）+ G5（「哪条是用户钉的 vs 推断的」）。
> 非目标：**不是 RAG 调试器、不是模型内部、不是检索体检台**——不做 top-k 逻辑哪个候选被切、不做 embedding 相似度、不做 ScoreBreakdown 逐项加权解说（那些是 `docs/zh/design/ux/08` §4 Scores 区段的检索自畸形，不是本面）。
> 状态基线：设计规格（docs-only）。行号引注钉在基线工作树；实现批开工时按当时基线重钉。
> 主要依据：`daemon/memory.py`（召回/Atlas wire）、`retrieve/assemble.py`（AssembledEntry/EntryFlag）、`schema/stamp.py`（Provenance/is_explicit_pin/EXPLICIT_PIN_SOURCE）、`hosts/opencode/plugin.ts`（T1/T2/T3 注入）、`console/static/{index,app,style}`、`docs/zh/design/ux/08-memory-atlas-spec.md`。

---

## 1. 问题陈述与用户

### 1.1 用户与心理模型

- 已使用若干天、对「记忆在后台自动沉淀」有模糊感知的本地用户（开发者 / 重度对话用户）。
- 他们的心智模型是：「我这个决策/这句偏好在当时被我自己明确『钉速』过吗？还是模型在瞎猜？」——不是「`source` 是 `memory.remember`」。
- **信任的来源不是技术标签，而是「这段文字是我给过它的，还是它自作主张记住的」**。

### 1.2 ONE thing

在 **30 秒内**对任何一条被注回的上下文得到两类答案：**① 它从哪来**（哪次会话、多旧、是用户钉的还是抓取的——G5），以及 **② 为什么此刻冒出它**（关联到本次提问的哪句话——G1，从可用的 `score`/`flags`/`recent_evidence` 派生，不解剖排名）。

### 1.3 当前失败模式（day-7 流失链）

1. hook 在会话开头（T1）与回合中（T2）注入一段 `<mnemoseed-memory-recall>` 块，用户只看到「警告这是记忆回放」，**看不到每条回放文字的来源** → 出现一条「跟当前问题无关甚至过时」的内容时，用户无从判断是记忆错了还是抓错了 → 恐慌 → 卸载。
2. 用户钉速的东西和自动抓取的东西**看起来一样** → 无法区分「我的意志」与「机器的推断」→ 模型引用一条「抓来的片段」时，用户误以为是「我当初告诉它的」，产生归属混淆 → 对系统产生「它篡改了我的意思」的误判。
3. 现有控制台 Atlas 抽屉已渲染 `asserted_by / source / confidence / at`（`index.html:333`），但 Atlas wire 的 chunk 项**根本没带这些字段**，所以目前全渲染成 `—`——信任面存在但数据是空的，等于不存在。

---

## 2. 数据地基：逐字段映射（不臆造）

> 以下映射全部基于真实 wire。**凡当前 wire 未暴露的字段，一律以 `⚠️ 后端缺口` 显式标注**（§10 汇总），实现批不得在无缺口的表面白等。

### 2.1 召回 wire（`daemon/memory.py:550-567` `_entry_payload` → `POST /memory/recall` 的 `entries[]`）

| 视觉/语义需要 | 映射字段 | wire 是否已暴露 | 备注 |
|---|---|---|---|
| 条目类型（chunk vs graph） | `kind` | ✅ | `"chunk"` / `"graph"` |
| 条目原文 | `text` | ✅ | verbatim 红线，不回写 |
| 来源标签（钉速判定 | `source` | ✅ | Pin ⇔ `source == "memory.remember"`（`stamp.py:63` `EXPLICIT_PIN_SOURCE`），实现批可据此渲 `Pinned`（无需等布尔） |
| **用户钉速 vs 抓取（G5）** | `is_explicit_pin`（派生 `source=="memory.remember"`，`stamp.py:66-69`） | ⚠️ 缺口 | wire 未下发该布尔；**前端用 `source` 现判**（`EXPLICIT_PIN_SOURCE` 一份常量），不再依赖布尔下发 |
| **断言主体（谁的话）** | `provenance.asserted_by` | ⚠️ 缺口 | `_entry_payload` 未下发；召回路径的 `AssembledEntry` 也不携带 `asserted_by`（`assemble.py:130-151`）。见 §10 缺口 A |
| 原始会话归属 | `session_id` | ✅ | chunk 有；graph 为 `None` |
| 注入时间/新鲜度 | `ingested_at`（epoch，wire 已 ISO） | ✅ | 相对时间本地算（`fmtRel`） |
| 生效起始（graph） | `valid_from` | ✅ | chunk 为 `None` |
| 来源 agent / host | `origin_agent` / `host` | ✅ | 仅 chunk 有值 |
| 得分（为什么是它 — G1 的代理） | `score` | ✅ | 融合分，**只作「相关度」提示，不解剖构成**（非 RAG 调试器） |
| 冲突标记 | `flags[]`（`EntryFlag.CONFLICT_PAIR/OMITTED/READ_CONFLICT`） | ✅ | `assemble.py:119-127` |
| 新鲜证据 / 待合并 | `flags[]`（`FRESH_EVIDENCE/PENDING_CONSOLIDATION`）+ `recent_evidence[]` | ✅ | `recent_evidence` 为原知片段（verbatim，UI 只引用不夸大） |
| 救援带 | `flags[]`（`RESCUED`） | ✅ | `assemble.py:119-127` |
| **conflict 相关布尔 `needs_reconcile`** | `ChunkStamp.needs_reconcile` | ⚠️ 缺口 | `_entry_payload` 未下发；召回面上通常只有 `read_conflict` flag。见 §10 缺口 B |
| **置信度 `confidence`** | `provenance.confidence` | ⚠️ 缺口 | `_entry_payload` 未下发；本面**不应展示**（见 §3.2 不展示声明） |

### 2.2 Atlas wire（`daemon/memory.py:1396-1451` `atlas()` → `POST /memory/atlas` 的 `items[]`）

| 视觉/语义需要 | 映射字段 | wire 是否已暴露 | 备注 |
|---|---|---|---|
| 条目类型 | `kind` | ✅ | `"chunk"` / `"node"` |
| 文本头 | `text_head` | ✅ | ≤120 字 |
| **用户钉速 vs 抓取（G5）** | `is_explicit_pin` + `flags.explicit_pin` | ✅ | chunks 有；nodes 恒 `False` |
| 衰减 | `decay_weight` | ✅ | |
| 抓取时间（chunks）/ 生效（nodes，仅现行 `valid_to IS NULL`） | `ingested_at`（chunks）/ `valid_from`（nodes） | ✅ | `updated_at` 仅 tooltip |
| 冲突/待合并/待调和/周边缺口 | `flags{conflict, pending, needs_reconcile, read_conflict, peripheral_gaps}` | ✅ | **nodes 有**（`memory.py:1442-1448`）；**chunk 只有 `explicit_pin`**（`memory.py:1418-1421`） |
| 得分/置信 | `score`（chunks）/ `confidence`（nodes） | ✅ | |
| **断言主体/来源/会话** | `provenance.asserted_by` / `source` / `session_id` / `origin_agent` / `host` / `history` | ⚠️ 缺口 | AtlasItem **chunks 未下发**任何 provenance 明细（`memory.py:1406-1423`）。抽屉固有 §2 `Provenance` 区段因此全空。见 §10 缺口 A |
| **里程碑 `needs_reconcile`（chunks）** | `ChunkStamp.needs_reconcile` | ⚠️ 缺口 | Atlas chunk 项未下发；见 §10 缺口 B |

### 2.3 注入 wire（opencode `plugin.ts` — 唯一注入宿主）

> **重要边界判定**：`hosts/claude_code/events.py`（`238` 行全文）**只有 `/ingest + /flush + /session/end` 归一化，没有任何 T1/T2 召回注入**。即：**剪远端注入信任面只落在 opencode 插件**（`plugin.ts` 的 `buildRecallInjection` / `buildT2Injection` / `buildRulesBudgetInjection`）。本规格的注入后缀只约束 opencode；claude_code 宿主不受本面影响（未来若加入注入，照抄同后缀）。

- **T1 会话起始回放**（`plugin.ts:333-408` `buildRecallInjection`）：`<mnemoseed-memory-recall>` + disclaimer + `<session-tail id=… ended=…[ started=…]>` 组头 + 逐条 verbatim 文本。预算 `MAX_INJECT_CHARS = 4000`（`plugin.ts:65`）**含** fence+disclaimer+组头+self-line。
- **T2 回合中召回**（`plugin.ts:427-465` `buildT2Injection`）：同一 fence+disclaimer 信封，**无组头**，逐条 verbatim。预算 = daemon `capture.auto_recall_budget_chars`（默认 `2400`，`config.py:258`），wire 报 `budget_chars`。
- **注入源数据**：T1 来自 `POST /session/recent`（`daemon/memory.py:855-922`），条目字段为 `session_id / text / ingested_at / turn_start / turn_end / origin_agent / host`（`memory.py:389-392`）；**没有 `source`/`asserted_by`**。T2 来自 `POST /session/recall-pending`（`memory.py:1169-1255`），条目字段为 `kind / id / text`（`memory.py:1250-1255`）；**没有 `source`**。

> **注入面的关键缺口：`/session/recent` 和 `/session/recall-pending` 两个 wire 都不带 `source`（钉速判定）**，且 `/session/recent` 不带 `asserted_by`。因此要在注入后缀里显示「Pinned」只能靠 **枚举附近追加 `source`（备忘 + 组内 `is_explicit_pin`）** —— 见 §10 缺口 C（最小、最廉价的追加点）。

---

## 3. 信任原则（为什么这样呈现）

1. **呈现的是「来源」，不是「机制」** —— 用户要的是「这段是你要我记的 / 这是我从你对话里抓的 / 这是老早的事实」，不是「embedding 距离 0.83」。
2. **可及性 ≠ 可信度（强红线）** —— 永不展示 `confidence` 数值或「我对这条有多确定」；`Neisser & Harsch 1992`（REFERENCES R16）已证「感觉确定 ≠ 准确」，`docs/zh/design/10 §2.2` 重申「升温/高可及 ≠ 更可信」。信任面回答**来源**，不回答**正确性**。
3. **Pin vs 抓取是第一个也是最重要的差异**（G5）—— 一眼可辨，二次确认可展开「谁在何时钉的」。
4. **注入面克制** —— 注入是写进系统提示的真实 token，信任后缀必须小、必须能丢。控制台可以丰富，注入必须极薄。
5. **诚实 bouncane** —— 没有 provenance 就明说「无来源信息」，不假装。

---

## 4. 注入面：极薄 provenance 后缀（budget-safe）

### 4.1 设计目标

在**不实质消耗注入预算**的前提下，让被注入的每一条回放文字带一个 1–2 词来源标注，用户瞄一眼就知道「钉的 / 抓的 / 老会话的」。

### 4.2 形态：组头行内的「pinned」标记 + 可丢后缀

**现状（T1 组内逐条 verbatim）：**
```
<mnemoseed-memory-recall>
The block below is an automatic memory replay...
<session-tail id="a3f9c2" ended="2026-08-29T10:00:00Z" started="...">
<原文本行1>
<原文本行2>
</session-tail>
</mnemoseed-memory-recall>
```

**改造（组头加 `flashbulb` 标记 + 每行可选 `⟵ pinned`）：**
```
<session-tail id="a3f9c2" ended="..." started="..." flashbulb>
<原文本行1> ⟵ pinned
<原文本行2>
</session-tail>
```

- `flashbulb`：组内**任一条**是钉速时加在**组头**，组内非钉行不加。语义=「这一组里至少一条是当初你自己钉的」。取值来自 `/session/recent` 枚举组内 `is_explicit_pin`（缺口 C 补齐后可得）。
- `⟵ pinned`：仅钉速那一行加；抓取行**不加任何后缀**（低频词 `pinned` 全对称出现反而淹没信号，只有「次 ↑ 钉速」才值得逐条标注，其余静默）。
- 抓取的逐条**不标注**，用「组头无 `flashbulb` = 全是自动抓取」的缺口语义表达 — 缺省即抓取，最省 token。

### 4.3 预算纪律（可度量）

| 常量 | 现值 | 本面占位 | 说明 |
|---|---|---|---|
| T1 `MAX_INJECT_CHARS` | 4000（`plugin.ts:65`） | 后缀受**组头预算**约束 | `flashbulb` 9 字/组 + `⟵ pinned` 9 字/钉行，已含在 `groupFixed`/`lineCost` 预扣内（见 §4.4） |
| T2 daemon 项预算 | `auto_recall_budget_chars` 默认 2400（`config.py:258`、`plugin.ts:100-101`） | 后缀受**项预算**约束 | T2 复用 daemon 项预算，hook 用 `budget_chars` 判定 fail-closed |

**预算承压时的丢弃顺序（TIGHT → 谁先被丢）：**
1. **先丢逐条 `⟵ pinned` 后缀**（它只是加固信号，`flashbulb` 组头已含同信息）——后缀改由「行内偏移」实现，丢时回退为无后缀纯文本，**不破坏 verbatim 通道**（绝不对 `text` 本身做任何切片/拼接，只加可见装饰）。
2. **再丢 `flashbulb` 组头标记**（9 字）——组内只剩纯 verbatim。
3. 到 `lineCost > remaining` 时，**照旧**按 `_MIN_SLICE_CHARS`（`plugin.ts:66`）丢边界项及更老项 —— 后缀从不改变现有项取舍语义；**后端不做任何变更**。

**字符预扣实现（不触碰 `text`）：**
- `flashbulb` 计 9 字，进 `groupFixed`（`plugin.ts:369`）预扣。
- 每钉行 `⟵ pinned` 计 9 字，进 `lineCost`（`plugin.ts:378`）预扣；承压时 `lineCost -= 9` 即行后缀，`groupFixed -= 9` 即组标记。
- 承诺红线：**verbatim `text` 永不因后缀被改写**——后缀是同一系统提示内的新增装饰元素，不是 `text` 的一部分（`docs/zh/design/03` verbatim 通道红线不覆盖注入信封装饰）。

### 4.4 localization / 面板文案

- 注入面文案全部英文（UI 通道）。括号内是 ASCII 原文，不带 emoji。

---

## 5. 控制台 Overview：召回「为什么回来」卡（新增最小区段）

> **现状核查**：当前「Overview」是 `healthz / observability / config / profiles` 状态板（`app.js:124-128`、`index.html:50-115`），**没有召回结果清单**。召回由 hook 注入 + CLI/MCP 消费。因此「Overview 召回面」最佳形态 = 在 Atlas 里给「最近一次注入过的召回」一个**只读回放**，而不是往 Overview 塞一张新的召回表。

### 5.1 归属

**不做新路由**。在 `Atlas` 页面新增一个 **Drawer 区段** 「Where this came from」（并入现有 Provenance 区段或紧随其后），以及一个 **StatusBar 内的最近注入摘要行**。以下 5.2/5.3 均落在 Atlas。

### 5.2 最近注入摘要（StatusBar 行，只读）

- 位置：`atlas-statusbar`（`index.html:297`）追加一段只读文本，数据来自 `POST /session/recent`（复用 daemon 已有端点，零新增后端）。
- 文案（示例）：`Last auto-recall served 2 chunks · 1 pinned · 3 sessions ago — see Atlas to audit.`
- 字段映射：`sessions[].chunks[].{session_id, is_explicit_pin, ingested_at, origin_agent}` —— `is_explicit_pin` 需缺口 C。
- 目的：把「你被注回的这一批」的可审计性带到控制台一级，是注入事件的可视化镜像。

### 5.3 召回复原卡（Atlas Drawer 的「Why this surfaced」区段，只读）

当一条 Atlas 条目可能被注入过（或有召回来源字段）时，显示一个**只读**「召回来源」块，回答 G1：

- **Pinned 徽标**：`is_explicit_pin`（Atlas chunk 已暴露）→ 绿 `Pinned` 徽标（沿用 `index.html:320` `badge-pin`），tooltip `You pinned this yourself — re-pinning reinforces it.`
- **来源会话与时间**：`session_id` / `ingested_at` → `From session <code>a3f9c2…</code> · 3 days ago`（诚实：session 有则给，无则 `From an unlabelled capture` / `— no session`）。
- **召回可得性提示（G1 代理）**：`score`（Atlas 已有）→ 不做数字恐惧，用徽标带：`related · returned by relevance`，tooltip `Ranked by how well it matched your recent query — not a certainty judgement.`（**不展示 confidence/分解**，见 §3.2）。
- **冲突/待合并**：`flags.conflict` / `flags.pending` → 沿用现有 `badge-warn`/`badge-pending` 徽标（`index.html:357`），tooltip 说明「the stored fact disagrees with a sibling / fresh evidence overlaps it」。
- **救援带**：`decay_weight ∈ [0.15, 0.4)` → `Recovered — below the usual floor but strong cues brought it back`（沿用 `badge-warn`，见 `index.html:356`）。
- 该区段**只读**，无新动词；「Pin again / Forget」等已有动词不动（`08 §11` 决议 3）。

---

## 6. Atlas Drawer Provenance 区段补全（数据打通）

现状 `index.html:333` 的 `Asserted by / Source / Confidence / At` 因 Atlas wire 缺字段而全 `—`。**打通但不扩大**：

- **chunks**：缺口 A 补齐后渲 `asserted_by`（`user` / 模型 id）、`source`（`memory.remember` 渲 `Pinned` 徽标，其余渲原始值），**`confidence` 一律不渲数值**（渲染 `Source monitoring — source is shown, certainty is not` 或留空），`asserted_at` 渲相对时间。
- **nodes**：已有 `confidence`（`memory.py:1449`）——按 §3.2 仍不展示数值。
- **chunk `needs_reconcile`**：缺口 B 补齐后加 `badge-muted` `Reconcile` 徽标 + tooltip `You revised this and it now conflicts with older wording — review.`

---

## 7. 各表面改动清单（file + component）

| # | 文件 | 组件/位置 | 改动 |
|---|---|---|---|
| 1 | `hosts/opencode/plugin.ts` | `buildRecallInjection`（:333-408） | 组头加 `flashbulb` 标记（枚举组内 `is_explicit_pin`，需缺口 C）；钉行加 `⟵ pinned` 后缀；`groupFixed`+`lineCost` 预扣（§4.3/4.4） |
| 2 | `hosts/opencode/plugin.ts` | `buildT2Injection`（:427-465） | 无组头，无 `flashbulb`；钉行加 `⟵ pinned`（T2 项需 `source`/`is_explicit_pin`，需缺口 C）；预扣入 `lineCost` |
| 3 | `hosts/opencode/plugin.ts` | `buildRulesBudgetInjection`（:270-281） | 不动（规则预算块与召回溯源无关） |
| 4 | `console/static/index.html` | `atlas-statusbar`（:297） | 追加最近注入摘要行（§5.2） |
| 5 | `console/static/index.html` | Drawer Provence 区段（:330-337） | chunks 打通 `asserted_by/source/session/asserted_at`；补 `Reconcile` 徽标；不渲 confidence 数值 |
| 6 | `console/static/index.html` | Drawer 新增/并入「召回来源」块（§5.3） | Pinned/会话/可得性/冲突/救援 徽标 |
| 7 | `console/static/app.js` | `openDrawer()`（:1783-1797） | 从 AtlasItem 派生的 provenance 填充（缺口 A/B 后字段就位） |
| 8 | `console/static/app.js` | `renderListRow()`（:1699-1722）/StatusBar | Pinned 角标已存在（:1713）；新增最近注入摘要的计数填充 |
| 9 | `console/static/style.css` | 徽标/簇 | 沿用既有 `badge*`/`pill*`，不新增视觉世界、不引入紫色/Inter；仅可能加一个 `badge-pin` tooltip 微调 |

**设计系统一致性**：全部复用 `08-memory-atlas-spec.md` §8 `Restrained` 令牌（`--atlas-pin #E07A5F` 等）+ 既有 `badge/pill/button` 语汇；无紫色、无 Inter、无新字体、无新动效（`prefers-reduced-motion` 尊重现有）。

---

## 8. 文案清单（English Copy Deck — UI strings verbatim）

> 注入面与控制台一律英文 API（UI 通道）。下表直接用于实现。

**注入（plugin）**
- `⟵ pinned`（钉速行后缀；ASCII，无 emoji）
- 组头 `flashbulb`（无文案，仅属性标记）

**Atlas StatusBar 摘要**
- `Last auto-recall served {n} chunk(s) · {p} pinned · {s} session(s) ago — see Atlas to audit.`

**Drawer「召回来源」块**
- `Why this surfaced`
- `From session {id} · {relTime}` / `From an unlabelled capture` / `No session recorded`
- `Pinned — you set this yourself; re-pinning reinforces it.`（tooltip）
- `Captured automatically from conversation — not pinned.`（tooltip，用于非钉行）
- `Returned by relevance — how well it matched your query, not a certainty judgement.`（tooltip）
- `Recovered — below the usual floor, but strong cues brought it back.`（tooltip）
- `Disagrees with a sibling fact — review.`（conflict tooltip）
- `Fresh evidence overlaps this fact — awaiting consolidation.`（pending tooltip）
- `You revised this and it now conflicts with older wording.`（reconcile tooltip）

**Drawer Provenance 区段**
- `Asserted by {value} · Source {value} · At {relTime}`
- `Note: source is shown; certainty is not judged.`（confidence 区占位，忠于 §3.2）
- `Reconcile`（`badge-muted`）

**空 / 边界**
- `No pinned suffix — this block is automatic recall, not your instructions.`（注入块整体无 flashbulb 时的兜底，尽头不重复）
- `No source info — captured before provenance was recorded.`（诚实 bouncane，无 `asserted_by`/`source` 时）

---

## 9. A11y + 空 / 冲突 / 衰减边界态

1. **屏读**：Pinned / 徽标用 `aria-label` 承载 tooltip 文案（`icon + text` 双通道，不依赖颜色）；`flashbulb` 组头标记在注入面本就无 DOM —— 不涉及。
2. **对比度**：`badge-pin` `#E07A5F` 上黑字 ≥ 4.5:1 需实测；不满足则加深底/加描边，不换色（沿用 `08 §13`）。
3. **空 / 无 provenance**：`No source info — captured before provenance was recorded.`（或 `—`），不抛 raw JSON。
4. **conflict**：`badge-warn` + 上述 tooltip；`conflict_pair/read_conflict` 同徽标注记，**两边都标**，不替用户裁决哪边对（`read_conflict` 从不判对错，`assemble.py:564-592`「under-flag posture」）。
5. **fading / stale**：`decay_weight < 0.15` 沿用 `badge-fading` + tooltip `Fading — rarely re-surfaced.`，与 08 §8 一致。
6. **reconcile 冲突**：`badge-muted` `Reconcile` + tooltip，不进 Alert 打断。
7. **偏好还原**：`prefers-reduced-motion` 下无新动效；徽标显隐是即时状态，不需动效。

---

## 10. 后端需求（已具备 vs 待建）— 显式列出，无猜测

### 10.1 已具备（直接复用）

- `POST /memory/atlas`（`memory.py:1356-1538`）chunk 已暴露 `is_explicit_pin` / `flags.explicit_pin` / `score` / `ingested_at` / `decay_weight`；node 已暴露 `flags{conflict,pending,needs_reconcile,read_conflict,peripheral_gaps}` / `confidence` —— 控制台 §5.3 旗标全部可用（除缺口 A/B）。
- `POST /memory/recent`（`memory.py:855-922`）→ StatusBar 摘要（除 `source` 缺口 C）。
- `POST /memory/recall`（`memory.py:459-502`）的 `_entry_payload` 已暴露 `source / session_id / ingested_at / origin_agent / host / flags / score / recent_evidence / conflict_group` —— 注回内容若需控制台/注入侧解读回溯，字段齐全（除缺口 A/B）。
- 既有 `badge/pill/button` 令牌与 `08` 设计系统全套。

### 10.2 待建（设计依赖、当前 wire 未暴露的字段 — ⚠️ 缺口）

> **这三处是唯一需要工程改动的后端面**；其余全部是纯前端/注入信笺，不碰 daemon 检索逻辑。

- **⚠️ 缺口 A：`asserted_by` / `source`（钉速主体）未上召回与 Atlas wire**
  - 召回 `_entry_payload`（`memory.py:551-567`）未下发 `provenance.asserted_by` / `source`（仅 `source` 有，`asserted_by` 无）。Asheld chunk 的 AtlasItem（`memory.py:1406-1423`）**既无 `asserted_by` 也无 `source`**（只有派生 `is_explicit_pin`）。
  - 最小改法：
    - **召回通道**：`assemble.py` 的 `AssembledEntry` 目前不携带 `asserted_by`（`assemble.py:130-151`），需在 `_entry`（`assemble.py:440-487`）从 `ChunkStamp.provenance.asserted_by` 带出并让 `_entry_payload` 下发 `asserted_by`。⚠️ 这是对 `assemble.py` 输出类型 + `memory.py:551` 的两行改动。
    - **Atlas 通道**：在 `atlas()` 的 chunk `items[]`（`memory.py:1406-1423`）追加 `asserted_by: chunk.provenance.asserted_by` 与 `source: chunk.provenance.source`（`source` 一行即补 Pinned 判定，无需额外布尔）。
  - 若实现批想最小：**`source` 就够 G5 判定**（`is_explicit_pin = source == "memory.remember"`）；`asserted_by` 只补「谁钉的」细粒度。

- **⚠️ 缺口 B：chunk 的 `needs_reconcile` 未上召回/Atlas wire**
  - `ChunkStamp.needs_reconcile` 是存储字段（`stamp.py`），召回 `_entry_payload` 与 Atlas chunk item 均未下发。Atlas node 有 `flags.needs_reconcile`（`memory.py:1444`），chunk 没有。
  - 最小改法：Atlas chunk `items[]` 追加 `flags.needs_reconcile: bool(chunk.needs_reconcile)`；召回面如需，在 `_entry_payload` 追加同 boolean。

- **⚠️ 缺口 C（注入宿主 — opencode）：`/session/recent` 与 `/session/recall-pending` 未下发 `source`（钉速判定）**
  - `/session/recent` 的 chunk payload（`memory.py:389-392`）字段为 `text/ingested_at/turn_start/turn_end/origin_agent/host`，**无 `source`**；`/session/recall-pending` 的 item（`memory.py:1132,1250`）字段为 `kind/id/text`，**无 `source`**。opencode `plugin.ts` 的 `buildT1/T2Injection` 因此无法逐钉 `is_explicit_pin`。
  - 最小改法：两端各追加 `source`（chunk 的 `provenance.source`）；`plugin.ts` 用 `source === "memory.remember"` 判定钉速，映射为组头 `flashbulb` / 行后缀 `⟵ pinned`。⚠️ 这**触及两个已 pin 测试 pin 的 wire**（`tests/test_recall_pending.py`、`tests/test_hosts_opencode.py` 等解析 wire 形状），实现批须同步更新 pins。

**非缺口（明确不建）**：不新建置信模型 / 排名 / LLM；不渲染 `confidence` 数值；不改 `Provenance` schema；不在 claude_code 宿主加注入（`events.py:238` 无注入面）。

---

## 11. T1 / T2 / T3 可交付分解 + QA 可验收标准

> 分期原则：**先打通数据（T1）→ 再注入面（T2）→ 最后控制台补全与打磨（T3）**。每期独立可验收。

### T1 — 后端缺口补齐（仅 A/B，最小面）

交付：
- 召回 `_entry_payload` + Atlas chunk item 暴露 `source`（必）与 `asserted_by`（可选，见 §10 缺口 A）；Atlas chunk item 暴露 `flags.needs_reconcile`（缺口 B）。
- 同步更新对应 wire 形状的既有测试 pins（不破坏 `assemble.py` 确定性契约，`score`/`flags` 字节不变）。

验收（QA 可验证）：
- `POST /memory/atlas {kind:"chunks"}` 的 chunk item 含 `source`、`asserted_by`（若做）、`flags.needs_reconcile`；`memory.remember` 钉的条目 `source=="memory.remember"`、抓取条目 `source != "memory.remember"`。
- `POST /memory/recall` 的 `entries[].asserted_by` / `source` 就位（若做 A 召回侧）。
- `uv run pytest -q` 全绿；`design/03` verbatim 与 `assemble.py` 确定性既存测试不回归。
- **不做**：此刻不渲染任何 UI。

### T2 — 注入面 provenance 后缀（opencode only）

交付：
- `plugin.ts` 两端 wire（`/session/recent`、`/session/recall-pending`）追加 `source`（缺口 C）+ pins 更新）。
- `buildT1`：组头 `flashbulb`（组内任一钉速）+ 钉行 `⟵ pinned` 后缀；`groupFixed`/`lineCost` 预扣含后缀；承压时按 §4.3 顺序丢（先行 → 组标记 → 项），**verbatim `text` 零改写**。
- `buildT2`：钉行 `⟵ pinned`（无组头），预扣入 `lineCost`。

验收（QA 可验证，用 temp `MNEMOSEED_HOME` + 空闲端口的独立 daemon，绝不碰 dogfood 7788）：
- 制造一条 `memory.remember` 钉速 + 若干抓取，跑一次 T1 session-start：注入块中钉速行带 `⟵ pinned`，组头带 `flashbulb`；抓取行**无**行后缀。
- 压预算到 `auto_recall_budget_chars` 极小值：先丢 `⟵ pinned`，再丢 `flashbulb`，`text` 逐字不变（字节比对注入前注入后）。
- `EXPLICIT_PIN_SOURCE` 判定正确（不引第二种判定：单一比较，`stamp.py:66-69`）。
- 新增 `tests/test_hosts_opencode.py` / `test_recall_pending.py` wire pins 更新后全绿。

### T3 — 控制台补全 + 打磨（纯前端）

交付：
- `atlas-statusbar` 最近注入摘要行（§5.2，读 `/session/recent`）。
- Drawer Provenance 区段打通（§6，用缺口 A/B 字段，不渲 confidence 数值）。
- Drawer「召回来源」块（§5.3：Pinned / 会话 / 相关 / 冲突 / 待合并 / 救援徽标）。
- 空/冲突/衰减边界态 + tooltip + aria-label（§9）；沿用 `08` 令牌，无紫色/无 Inter。

验收（QA 可验证，Playwright 实页，temp daemon + 空闲端口）：
- 钉一条 + 抓取多条，进 `#/memory/atlas`：
  - 钉的条目 List 行带 `P`/`Pinned` 角标（既有，:1713）+ Drawer Showcase 内徽标正确（`source=="memory.remember"` 渲 `Pinned`，抓取渲 `Captured automatically`）。
  - Drawer `Asserted by` 不再 `—`（缺口 A 生效）；`Confidence` 区不显示任何数值（只显 §8 的 note）。
  - `needs_reconcile` 的 chunk 出现 `Reconcile` 徽标（缺口 B 生效）。
  - 揉一匹配（注入一条）后 StatusBar 摘要行出现并计数正确。
  - tooltip / aria-label 屏读通过；`prefers-reduced-motion` 下无新动效。
- 完整 `pwsh -File scripts/gate.ps1`（pytest/ruff/format/mypy）全绿。

---

## 12. 越界机会（仅记录，不展开）

- 把注回事件本身（`session_id` → 注入块）作为可审计事件补进 Audit/Telemetry，让「这条注入被我引用过吗」（TA-6 consumption）也可见。
- 若未来 claude_code 增加注入，把同信笺后缀复制过来（本规格已为其留痕）。
- `read_conflict` 冲突对的「两侧对比」视图（冲突集群收敛视图），归属 Audit 而非本面。

---

## 13. 理论锚（本面）

> **本面为控制台/注入**信任面（control-plane 信笺 + UI 呈现），不新增记忆机制的理论借用。

| 锚点 | 状态 | 说明 |
|---|---|---|
| `Neisser & Harsch 1992`（REFERENCES R16） | 引用不借用 | 「感觉确定 ≠ 准确」→ 信任面只呈现**来源**，永不渲染 `confidence` 作可信度（§3.2）。 |
| `Johnson Source Monitoring`（来源监控） | 引用不借用 | 信任面把每个记忆条目带回来源（Pin/抓取/会话/时间）——心理层面与「来源监控」直觉一致，但**不以此为机制依据**，回落为纯 UI 呈现。 |
| `Tulving & Thomson 1973`（编码特异性） | 引用不借用 | 仅作「会话归属能帮助再提取」的文案依据，不新增机制。 |

**不借清单（重申，`docs/zh/design/10 §2.2` 对齐）**：
- Miller 7±2 不得作为任何条数/预算常量出处（REFERENCES R13）。
- 「向量越近越可信」不借：分数只作可得性提示，不涉可信度。
- 「闪回记忆较可信」不借（R16 反证）——绝不渲染 confidence 数值。

---

## 14. 校验计划（Verification Plan）

### 14.1 需验证的真实页面能力

- `POST /memory/atlas` chunk item 的 `source / flags.needs_reconcile`（及可选 `asserted_by`）就位（T1）。
- 注入块（temp daemon + 空闲端口触一次 session-start）钉速/抓取标记正确、预算承压丢弃顺序正确、verbatim 零改写（T2）。
- Atlas Drawer Proveance / 召回来源 / StatusBar 摘要三块渲染与边界态（T3）。

### 14.2 Playwright 实页步骤（QA 用，隔离规范同 `08 §21.2`）

```powershell
# 1) 临时 MNEMOSEED_HOME + 空闲端口（与 dogfood 7788 隔离）
$env:MNEMOSEED_HOME = Join-Path $env:TEMP "mnemoseed-trust-verify-$(Get-Random)"
$env:MNEMOSEED_PORT = "17889"        # 空闲端口，探活后写入
mnemoseed-local up --port $env:MNEMOSEED_PORT

# 2) 注入数据：一条钉速 + 多条抓取（制造会话）
mnemoseed-local memory remember --profile default --text "Preference: ship zero-copy data paths over duplication"
mnemoseed-local --host opencode hook event ...   # 或用测试夹具造 /session/recent 尾巴

# 3) Playwright 打开 http://localhost:17889/#/memory/atlas
#    - 断言：钉速行带 Pinned 徽标；Drawer Asserted by 非 "—"；Confidence 区无数值
#    - 断言：needs_reconcile 条目出现 Reconcile 徽标；StatusBar 摘要计数正确
#    - 断言：工具提示/aria-label 屏读；prefers-reduced-motion 无新动效

# 4) 注入面：temp daemon 下跑一次合成 T1，验证 flashbulb/⟵ pinned 标记与预算丢弃

# 5) 清理
Remove-Item -Recurse -Force $env:MNEMOSEED_HOME
mnemoseed-local down
```

- **硬性隔离**：始终 `MNEMOSEED_HOME` 临时目录 + 空闲端口；**绝不触真实 `~/.mnemoseed-local` 与 dogfood 7788**。

### 14.3 门禁

- 实现后 `pwsh -File scripts/gate.ps1`（pytest/ruff/format/mypy）全绿；本规格 docs-only，T1 的 backend 两行改动属实现批，非本批。

---

## 15. 校验清单（Spec 自检）

- [x] 每一呈现字段都有 wire 出处（§2 表逐行），⚠️ 缺口三处均已显式标出（§10）
- [x] 不新建置信模型/排名/LLM；不渲 confidence 数值（§3.2 红线）
- [x] 注入面预算纪律与丢弃顺序可度量（§4.3），verbatim 红线零改写（§4.4）
- [x] claude_code 无注入面（`events.py:238` 核验），注入面只约束 opencode（§2.3）
- [x] 控制台改动全部沿用 08 令牌（§7），无紫色/无 Inter/无新动效
- [x] 空/冲突/衰减/无来源边界态齐全（§9），文案为英文原文（§8）
- [x] T1/T2/T3 分解 + QA 可验收标准逐期可独立验证（§11）
- [x] 后端缺口显式列出并给出最小改法（§10.2），实现批无须猜