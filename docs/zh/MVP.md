# MnemoSeed Local — MVP 范围冻结

> 本文档从 A1 任务简报整理而来，是 mnemoseed-local 的**范围冻结**依据。
> 与简报冲突时以简报为准；本文件只做记录，不包含个人署名。

## 项目意图（锁定）

mnemoseed-local = **本地单用户 MVP 守护进程**：

- 无账号：localhost 隐式信任，无 identity/owner gate
- profile 硬编码为 `default`（框架内部保留）
- CLI 优先（无 console）
- dream 走**本地模型**（ollama 默认；保留 openai-compatible 回退驱动）
- 核心闭环：capture → dream --once（手动）→ decay → retrieve
- 一键安装属后续阶段（A3）

## A1 范围（本轮，仅地基，无用户可见表面）

1. **包骨架**：`pyproject.toml`（dist 名 `mnemoseed-local`，模块
   `mnemoseed_local`，CLI 入口 `mnemoseed-local`），python_requires>=3.12，
   最小依赖集；uv 管理；`src/mnemoseed_local/__init__.py`
   （`__version__="0.1.0"`）。
2. **config**：`config.py` + `configwrite/`（注册表裁剪到 MVP 实际使用：
   storage + dream + decay + llm 路由键；不含 registry/providers）。
   单 profile `default` 硬编码（只参考主仓库 identity 理解签名，不移植账号）。
3. **secrets**：原样移植（FileSecretStore + ChainSecretStore +
   KeyringSecretStore），改名 `mnemoseed_local`。
4. **storage**：`ports.py` + 驱动 `sqlite_meta` / `sqlite_graph` /
   `lancedb_embedded` / `synthetic_embedder`（测试臂）/ `bge_m3_onnx`
   （生产默认）+ `storage/registry` + 迁移 + contract 测试。
   **PG 驱动跳过**。保留 capability 标记（含 graph.edge_list）。
5. **schema**：`stamp.py` + `graph.py` 移植。
6. **docs 骨架**：`docs/zh/README.md`（中文 MVP 定位）+ `docs/zh/MVP.md`（本文件）。
7. **验证**：uv sync 通过；`uv run pytest` 全绿；ruff check + format + mypy
   干净；`uv run python -c "import mnemoseed_local; print('substrate OK')"`。

## 约束

- CLI 可见表面属于 A2（capture/retrieve/dream/daemon/cli）；本轮到地基为止。
- 公开代码只使用英文（含注释）。
- TDD：测试随代码一起移植，最小化改写。

## A2 补充：dream 触发规则（score-pool 版）

dream 触发基于**分数池**（主仓库 design/01 + PRD-02），不是轮次计数：

- 每个 durable 捕获轮次把其 S 重要性（arousal / novelty / causal 分量，
  0..10 分制）累加进该 profile 的 ScorePool（MetaStore `profile_score_pool`
  每行持久化，`pool_state` 读取余额）。
- **floor+idle**：池余额 ≥ `dream.floor_pool_points`（默认 10.0）且空闲 ≥
  `dream.idle_min_sec`（900s）→ 触发 dream。
- **hard deadline**：最老 pending（未合并）chunk 等待 ≥ `dream.hard_deadline_sec`
  （24h）→ 强制触发；无 pending 时完全跳过。
- dream 触发即消耗池（drain，余额归 0）：同一批分数永不重复触发；再次触发
  需要池重新积累到阈值（合并后的 watermark 前进路径由 chunk 的
  `consolidated` 标记体现，pending 读只取未合并 chunk）。

三键（`floor_pool_points` / `idle_min_sec` / `hard_deadline_sec`）均为
configwrite 注册表键，热应用。守护进程构造 ScorePool 时直接使用
`floor_pool_points` / `idle_min_sec` 作为其自触发阈值（`dream_threshold` /
`idle_window_sec`），与调度器同源，无硬编码阈值。

## 红线（继承自主仓库架构级约束）

- 捕获中立：评分不读 anima/偏好
- 溯源不可变：历史只追加
- verbatim 通道永不有损；gist 只是派生，永不当源头
- BYOK/E2EE：记忆明文对运维不可见，只有加密块 + 元数据
