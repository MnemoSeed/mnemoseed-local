# PRD · Phase B 路线图（总立项）

> 依据：`docs/zh/design/mvp-design.md` v1.3 §6 Phase B（子项"立项时再定"，本文件即立项）+ B1 收口实弹发现（PRD-B1 人工验证记录）+ 用户优先级拍板（2026-08-18）：**按原计划续完整个 Phase B；验证不同模型的 dream 质量；积累小模型效能数据；优化细节配置让低配电脑也有高质量 dream 与长期记忆；长期记忆靠"反复观察 + 校验"固化正确知识**。
> 基线：commit `018967a`（B1.1 含），1073 passed / 3 skipped。

## 用户命题的工程对位（先诚实标注边界）

"小模型记忆会有偏差，但经长时间重复学习和教导应能改善并固化"——本架构已有对应机制：

- **折叠强化**：同一事实多次被提取会折叠合并且 confidence 逐次 +0.05（封顶 0.95）；命中即 reinforce 对抗 decay。→ "反复学习"的量产通道已在。
- **verify 交叉校验**（B1 刚交付）：每轮 dream 由第二模型过滤噪声/幻觉。→ "教导"的第一形态已在。
- **隔离与溯源**：分歧 → isolated 永不灭档；verbatim 原文永在，provenance 可回放。

**边界（如实）**：这套"重复 + 教导"只能滤掉**抽样噪声/个体幻觉**；**同源系统性幻觉投票无效**（设计稿决策 1 原文）。小模型长期记忆质量的最终防线仍是 verbatim 通道 + provenance + isolated 结构。Phase B 的所有实测工作，本质是把这句话从"设计预期"变成"有 bar 的事实"。

## 批次序列（定序 + 理由）

### B2 · 时序接续面（UX 小刀）——第一刀
- 痛点当日实证：新 session 无法按时间接上上一 session 结尾（本次 session 开头用户需手贴引用）。
- 范围：daemon `POST /session/recent`（profile 维度、倒序、最近已关闭 session 的尾部 N 轮 verbatim 原文）+ MCP 工具 `recent_sessions(n?)`；CLI `recall` 类表面不动。
- 风险面小（只读端点 + 网关映射），不触 capture/dream 核心链。
- 依据已验构件：chunk 带 `session_id` + epoch `ingested_at`（D3 已修）；`/memory/timeline` 的 recent-first 先例。

### B3 · 评测臂（eval harness + canary bar）——一切后续的地基
- 设计依据：§6 Phase B"评测矩阵（档位 × off/verify/vote）与 bar 立项"。没有 bar,"验证模型质量"就是感觉。
- 范围：
  1. **canary 工厂**：合成 user turns 语料（EN+ZH 偏好/决定/习惯类既定事实 + 噪声轮），写入 scratch store——被测事实集是确定性的，无需人工标注。
  2. **真实材料 replay**:B1 harness 同款只读快照复放（随数据积累，材料库只增不减）。
  3. **矩阵运行器**:A 模型 × 校验层（off/verify/判定座型号）× 档位参数；本机已在位模型全上（qwen3.5:9b / gemma4:e4b / qwen3.5:4b / qwen3:8b / qwen3:4b / gemma4:12b）。
  4. **指标与 bar**:canary recall（既定事实进 core 的比率）、junk 率（噪声/机械句误进 core)、判定质量、时延、token、fallback 率；首跑后把数值 bar 钉死进 PRD。
  5. **数据积累**:JSON 报告写 `~/.mnemoseed-local/eval/`（数据目录，不入 git 防膨胀）；每次批次收口摘要入 PRD。
- 形态：`src/mnemoseed_local/eval/` 子包 + stub-LLM 单元测试（harness 自身逻辑全 TDD;live 矩阵跑的慢，不进 pytest 门禁）+ `uv run python -m mnemoseed_local.eval` 入口（不加 CLI verb，先不出产品表面）。

### B4 · lite 档定版与档位标定——吃 B3 数据
- qwen3.5:4b（官方 lite 锚点）+ gemma4:e4b 作 A 候选，lite 窗口（8k ceiling）实测 → 型号定版；
- `core_confidence_floor` 数值标定（A 自报 confidence × B 判定分歧统计）；
- `dream.capture_only` 硬模式裁定（过不了 bar 的档位只推 capture-only——不发布毒药，设计原文）。

### B5 · vote 机制 + needs_reconcile 协同
- 单快照双相位 journal + 确定性 combiner（设计决策 1 的机制改动，诚实成本版）。体量与反射核对齐，须独立 PRD 立项；排在 B3/B4 数据到手后。

### B6 · 性能：drain 写序列化 / 批量提交
- A2.5 QA 观察 5：重载下事件循环瞬时 stall(/healthz p95≈609ms,dream worker 竞争 +35-40%)。收紧 drain 存储写路径。

### 挂起项（如实记录）
- **advanced 27B 档**：本机 8GB VRAM 跑不动 27B（设计线 16-24GB)；硬件到位前挂起。
- **BYOK**：设计已定"Phase B 后"才立项。
- **ensemble 高配仲裁位**：依赖 vote + advanced 档，随 B5 再议。

## 门禁（每包不变）

每批次独立 PRD → TDD → 对抗 QA 自验 → 全量门禁（`uv run pytest -q` / ruff / format / mypy)→ 单 commit 收口 + 收口记录。

## 批次启动记录

- **B2 时序接续面**：2026-08-18 开工并收口（commit `1edda80`，1082 passed / 3 skipped）。`POST /session/recent` + MCP `recent_sessions(n_sessions?, n_per_session?)` 落位；hook 自动注入形态存挂起（依赖宿主插件上下文注入能力验证）。
- **装机实测（同日，用户授权）**：版本线归位 `0.0.1`（`1c9fe80`）；`uv tool install --force .` 装机；daemon 换新构建重启（B1+B1.1+B2 全部在位：config 表面见 `dream_verifier` 路由、`/session/recent` 对 live 数据返回真实 session 分组尾部）；`opencode.json` 注册 MCP 网关（绝对 exe 路径，主机名含空格走数组直传）；**live smoke 抓出真缺陷并修复**：stdio 道宿主页码（cp936）下 ensure_ascii=False 帧成乱码、text-mode \n→\r\n 双坑——`7023746` 强制双道 UTF-8 + 不换行翻译（回归测试复现了 live 同指纹的解码错位 byte 0xa1@1035）；仓根新增 `AGENTS.md`（session-start 记忆纪律 + 开发门禁，`c6e9db3`）。门禁复验 1083 passed / 3 skipped。**待用户动作：重启 opencode 使 `mcp` 配置生效**（配置仅在启动时加载）。
- **B3 评测臂**：（待开工）
