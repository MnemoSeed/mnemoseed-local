# PRD · B2.1-T4 阈值标定（floor/budget 评测定版）

> 依据：PRD-B2.1-auto-recall.md §"后续挂起" 与 PRD-B2-roadmap.md "下一刀候选：T4 阈值标定（吃 B3 评测臂）"。
> 
> B3 评测 harness（PRD-B3-eval-harness.md）已收口。**T2 注入管线评测需要一条独立评测臂**（T2 回忆 rig + 回复型 assistant 座 + 消费测量 + 回忆结构材料）。T4 任务拆两阶段：
> 1. **T4a 建臂（门禁内 stub TDD）**：新建 `eval/recall_harness.py` + `eval/recall_matrix.py` + `eval/recall_metrics.py`，**不复用 B3 `EvalRig`（B3 是 dream 链 rig，不含 capture 评分）**，而是**从零构建 T2 管线 rig**：daemon ingest → `_focal_scan` 驻槽 → serve/mark-seen → hook pull，**全程确定性、不接 ollama**。单测入 `tests/test_eval_recall_*.py`，随 CI 全量门禁跑（无排除 marker），**1:1 镜像 B3 的隔离模式与 run-id 命名空间（B4b 契约）**，而非复用 B3 类结构。
> 2. **T4b 标定跑（手动子命令）**：新增 `python -m mnemoseed_local.eval recall` 子命令（扩展 `__main__.py`，B3 `matrix` 同款），报告写 `<CONFIG_DIR>/eval/`。**不进 pytest、无 CI 排除 marker、无全局 addopts**。仅标注入两参数（`focal_floor`、`auto_recall_budget_chars`）；`needle_*` 三参数归属裁决推迟到 B2.7 之后。

---

## 理论锚（复用 PRD-B2.1 现有，无新增）

- TA-1 ACT-R 激活方程：阈值是激活函数的截断参数，标定 = 找到使 `P(recall | relevant)` 与 `P(recall | irrelevant)` 分离最大的截断点。
- TA-2 编码特异性：floor 过高 → 漏掉真线索（FN）；过低 → 注入噪声（FP）。
- TA-4 多加工框架：focal floor = 自发提取闸门；non-focal floor 仅报告度量（不自动注入），为 T4 备数据。

---

## 范围（分阶段任务）

### 阶段 T4a · 建臂（门禁内 stub TDD，独立 T2 管线 rig，非复用 B3 EvalRig）

**材料结构（新立"回忆结构材料"，非 canary 复用）：**
- Session A：存入事实（含实体标注）的 `ingest` 序列 + **噪声 chunk**（两类：(a) entity-miss 无关实体——被 focal 过滤永不可 serve，仅进 non-focal 计数；(b) entity-collision 同名干扰实体——经 focal 过滤可被 serve，构成 Floor-FP 分母）+ **至少一对 needle 撞串噪声 chunk**（两 chunk 归一化文本共享同一 24 字符头/中窗，用于行使 Detector-FP）。
- Session B：引用同名实体的线索轮（user prompt 含实体名）+ 模拟 assistant 回复（含/不含目标实体；另加一种"含目标实体且意外含他 chunk needle"的回复模板，供 Detector-FP）
- 维度：双语（en/zh）× 4 类事实 × 3 长度档（短/中/长，~50/200/800 chars）= 24 材料点
- 材料入库 `eval/recall_materials.py`（新），不污染现有 canary 目录。

**Rig（新建 `eval/recall_harness.py` + `eval/recall_matrix.py` + `eval/recall_metrics.py`，复用 B3 隔离模式与 run-id 命名空间，不复用 B3 类结构）：**
- 启动 daemon + 注入 rig，`capture.auto_recall=True`
- **T2 管线走 HTTP**：`POST /ingest`（`daemon/ingest.py:64` 触发 `note_user_prompt` → `_focal_scan` 驻槽） → `POST /session/recall-pending`（`memory.py:1169` → `recall_pending` serve = mark-seen 锁内原子，产出 `pending` 槽） → hook pull（armed∧acked 门控，300ms timeout、fail-open）→ `POST /memory/reinforce`（`memory.py:1183` → `985`，读 `last_reinforced` 判定消费证据）。**armed∧acked 门是 hook 内部态，对 serve 集合无影响（等价一次非空 pull）**。
- **回复型 assistant 座**：模拟 assistant 回复（模板化，含/不含目标实体；另加一种"含目标实体且意外含他 chunk needle"的模板），不调用真实 LLM（token 红线）。
- **消费检测（跨语言 oracle 钉）**：Python 侧复现 TS needle 逻辑（`sanitizeRecallText` + 归一化 + 窗口），**常量与语义逐字节对齐 TS**（`plugin.ts:58-60,223-238`），并在 `tests/test_needle_oracle.py` 以逐字节比对钉死（含非 BMP emoji 用例钉 UTF-16 vs UTF-8 长度语义差）；读取 `last_reinforced` 判定消费证据。
- **指标计算**：每材料点独立 rig 实例（复用 B3 run-id namespace + idempotent rig 隔离）。

**单测（门禁内，无排除 marker）：**
- `tests/test_eval_recall_harness.py`：材料工厂、指标数学、矩阵展开、隔离契约
- `tests/test_eval_recall_matrix.py`：rig 构造、run-id namespace、within-run 无串料
- `tests/test_eval_recall_metrics.py`：指标计算手算合成 fixture、Floor-FP/Detector-FP/Recall/Precision/FN 数学钉死
- `tests/test_needle_oracle.py`：Python needle 逻辑与 TS 逐字节一致性（常量、归一化、头/中窗、中窗偏移 center−12、不对称归一化、非 BMP emoji 用例）
- **随 `uv run pytest -q` 全量门禁跑**，与 B3 既有 eval 单测同级保护。

### 阶段 T4b · 标定跑（手动子命令，坐标下降 + 置信区间）

**子命令**：`python -m mnemoseed_local.eval recall`（新增 `__main__.py` 分支，B3 `matrix` 同款）
- 参数：`--floor 0.4 --budget 1200 --runs 1 --config <toml>` 等
- 输出：`<CONFIG_DIR>/eval/t4_calibration_<timestamp>.json` + 摘要 stdout
- **全程确定性、不接 ollama**；“live”仅指人工触发的手动参数扫描。T2 管线全确定性（无 LLM、贪心预算、tie-break 三键钉死 `memory.py:861-874`），**每参数组 N=1**（材料固定则指标逐字节相同），**材料变体由 24 点异质性承担方差**；删除空转重复（N>1 无信息增益）。

**参数空间（仅注入两参数，needle 推迟）：**
| 参数 | 现起步值 | 搜索范围 |
|------|----------|----------|
| `focal_floor` | 0.4 | [0.4, 0.6] step 0.05（下限 0.4，避免与 non-focal floor 0.4 交叠语义扭曲） |
| `auto_recall_budget_chars` | 1200 | [600, 2400] step 200 |

**执行设计（坐标下降，避免全交叉爆炸）：**
- 每组 2 参数固定扫 1 → 5+10 ≈ 15 组/轮 × 2 轮 = 30 组
- **单材料 seed 贯穿整轮扫描**（30 组看同一 24 点，比较无混淆），删除空转重复
- 单点观测如实标注；Pareto 前沿以 24 点中位数
- **空前沿回退规则**：若联合可行域无解，按加权损失序（Recall@5 0.4、Precision@5 0.3、Floor-FP 0.2、overhead 0.1）选最近可行点，如实记录降档路径。

**指标（每参数组跑完整矩阵）：**
1. **Recall@k**（k=1,3,5,10）—— 针对 focal 线索命中率（候选池中被 serve 的比例）
2. **Precision@k** —— 注入 chunk 中真被 assistant 引用的比例（needle 消费证据）
3. **Floor-FP** —— `served-noise ratio` = 被 serve 的噪声 chunk / 候选噪声池（Floor-FP，floor 过低的主信号）
4. **Detector-FP** —— 未被引用但被强化的 chunk 比例（TA-6 检测器诚信度量，针对 needle 撞串/幻觉复述，材料含撞串对保证非零）
5. **FN rate** —— 被引用但未发 needle 的 chunk 比例（消费漏检）
6. **Token overhead** —— 平均每轮注入字符数 / 预算上限
7. **Non-focal above floor count** —— 仅报告（TA-4），供事后分析

**指标聚合口径（钉死前沿比较标量，DRY）：**
- 每材料点先算 7 项指标
- 参数组取 **24 点中位数** 作 Pareto 前沿标量
- 双语/四类方差单独列鲁棒性表（中位数跨材料方差）
- 单点观测如实标注；报告带检测器固有误差带（TA-6，T3 已记）：幻觉 FP（+0.1 有界）、复述 FN、<32 字符永不可记（短 chunk 盲）。

**标定目标（Pareto 前沿，取工程可接受点）：**
- `Recall@5 ≥ 0.75` 且 `Precision@5 ≥ 0.60`
- `Floor-FP ≤ 0.15`、`Detector-FP ≤ 0.15`、`FN rate ≤ 0.20`
- Token overhead ≤ 0.8（预算利用率）
- 参数组合在双语/四类材料上方差最小（鲁棒性）

**交付物：**
1. `<CONFIG_DIR>/eval/t4_calibration_report.md` —— 网格热力图、Pareto 前沿、推荐参数组 + 置信区间、降档记录（注明"坐标下降非全网格，交互点可能未扫到"）
2. `docs/zh/prd/PRD-B2.1-auto-recall.md` 收口记录追加：定版参数值 + 评测摘要
3. `src/mnemoseed_local/config.py` `default_config_toml` 同步新默认值（仅 `focal_floor`、`auto_recall_budget_chars`）
4. 回归测试：新默认值下门禁全绿

---

## 实现机制（复用 B3 基建模式，非复用 B3 rig）

- **材料**：新建 `eval/recall_materials.py`（24 点回忆结构材料 + 噪声 + 撞串对 + 干扰回复模板）
- **Rig**：新建 `eval/recall_harness.py` + `eval/recall_matrix.py` + `eval/recall_metrics.py`（复用 B3 隔离模式与 run-id namespace，不复用 B3 `EvalRig`）
- **执行**：live 子命令 `python -m mnemoseed_local.eval recall`（手动触发）
- **并行**：每参数组独立 rig 实例（run-id namespace + idempotent rig）

---

## 边界（如实）

- **不改变**：auto-recall 管线逻辑、focal/non-focal 语义、needle 归一化、tombstone、seen-set。
- **仅标定**：2 个数字常量的默认值（`focal_floor`、`auto_recall_budget_chars`）。
- **needle 三参数**：归属裁决推迟到 B2.7 之后（避免并行插件面冲突 + version_id 槽位移代价），本批**不交付**。
- **非目标**：模型本身的 recall 能力、跨 session 持久化、non-focal 自动注入（TA-4 红线）。
- **B2.7 耦合**：T4 标定跑**先于 B2.7 落地**；B2.7 落地后对 floor/budget 重验并据实补记（B2.7 新增过滤面/rules 覆盖可运行期覆盖默认值）。

---

## 门禁

- T4a stub 单测：`tests/test_eval_recall_*.py` + `tests/test_needle_oracle.py` **无 marker、随 CI 全量门禁跑**（1:1 镜像 B3 隔离模式）
- T4b live 子命令：`python -m mnemoseed_local.eval recall`（手动触发，不进 pytest、无 CI 排除 marker、**无全局 addopts**）
- 标定报告人工审阅后更新 PRD + config 默认值
- 更新后全量门禁（`uv run pytest -q` / ruff / format / mypy）必须绿

---

## 批次执行记录（待追加）

### T4a 建臂收口记录（2026-08-21）

- **批次执行（TDD 红先行 → 绿，51 条新单测全绿）**：
  - 材料工厂（`eval/recall_materials.py`）：24 点全网格覆盖（2 语言 × 4 类 × 3 长度，逐点唯一实体）；噪声三类齐备（entity-miss 永不可 serve 仅进 non-focal 计数 / entity-collision 构成 Floor-FP 分母 / needle-collision 与 fact 共享 24 字符头窗行使 Detector-FP）；needle 撞串由构造保证（fact 与碰撞 chunk 同以 P 句开头，归一化头窗逐字节重合）+ 测试钉死；实体可抽取性双侧断言（cue 与 fact 文本经 `extract_cues` 均命中实体）；同 seed 逐字节复现、异 seed 实质不同；回复模板四型（cite/stray/no_cite/paraphrase）needle 机制逐点断言（中窗引用句由装配搜索保证覆盖中心窗，stray 必然误触碰撞 chunk 的 needle，paraphrase 必然零触发）。
  - needle oracle（`tests/test_needle_oracle.py`）：常量 24/32/48 与 `plugin.ts:58-60` 源码级比对（免 node）；`normalizeRecallText`/`needlesOf`/`sanitizeRecallText` 提取 shipped 源码在 node 下逐字节交叉验证（`tests/ts_hook/needle_oracle.mjs`，无 node 时跳过、其余测试全门禁跑）；**自纠 1 枚**：消费匹配必须走 UTF-16 单位空间包含（代码点包含会把代理对拆成假阴性——非 BMP emoji 用例钉死）；`consumption_normalize` 与 needle 构建的不对称（回复侧无角色前缀剥离）钉死。
  - T2 管线 rig（`eval/recall_harness.py`）：真实 daemon（`create_app` + TestClient）全程走 HTTP `/ingest`（cue 轮触发 `_focal_scan` 驻槽）→ `/session/recall-pending`（serve=mark-seen 锁内原子）→ hook pull（armed∧acked 门为 hook 内部态，等价一次非空 pull）→ 模板化回复 → 消费匹配 → `/memory/reinforce` → 读 `last_reinforced` 判定消费证据；消费证据判定利用 driver 的 `last_reinforced=ingested_at` 回退语义（强化过的 chunk 其 `last_reinforced` 必然刷新为更晚 epoch）；每回复为一独立检测器观测（hook 的 per-chunk-per-session 去重会掩蔽 stray/paraphrase 的指标信号）。
  - 指标（`eval/recall_metrics.py`）：Recall@k / Precision@k / Floor-FP / Detector-FP / FN / Token overhead / Non-focal 七项手算合成 fixture 钉死（含 0/0 → None 诚实语义、per-observation FN/Detector 分母）。
  - 矩阵（`eval/recall_matrix.py`）：参数空间 `[0.4, 0.6] step 0.05` × `[600, 2400] step 200`；坐标下降 30 组轨迹（5+10）×2 轮钉死（round-2 以 round-1 最优重锚）；24 点中位数聚合（None 排除、点数如实）；Pareto bars（Recall@5≥0.75 等六条）；加权损失序（0.4/0.3/0.2/0.1）与空前沿降档路径。
  - 隔离契约（B4b 1:1 镜像）：`RecallRig` 构造幂等 wipe（同 root 两跑 store 不累积、逐项等值）+ run-id namespace（`root/runs/<run-id>/<point_id>`）+ within-run 跨材料无串料。
- **门禁**：**1466 passed / 4 skipped**（T4a 新增 51 条；工作树含 B2.7 未收口测试），ruff / ruff format / mypy（94 files）全净。
- **不实现（划线，PRD 既定）**：T4b live 子命令（`__main__.py` recall 分支，手动触发）、`config.py` 默认值同步（T4b 标定后交付）、needle 三参数裁决（B2.7 后）。

### 评测记录（待填充）

| 日期 | 参数组 | Recall@5 | Precision@5 | Floor-FP | Detector-FP | FN rate | Token overhead | 备注 |
|------|--------|----------|-------------|----------|-------------|---------|----------------|------|
|      |        |          |             |          |             |         |                |      |

### 定版参数（待填充）

- `focal_floor` = 
- `auto_recall_budget_chars` = 
- `needle_min_len` = （推迟，B2.7 后裁决）
- `needle_mid_threshold` = （推迟，B2.7 后裁决）
- `needle_mid_offset` = （不存在，代码为 center−12 固定，B2.7 裁决）

### 配置同步记录（待填充）

- `default_config_toml` 更新：是/否
- 回归门禁：passed / failed