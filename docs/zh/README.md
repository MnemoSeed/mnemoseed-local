# MnemoSeed Local

**单机、单用户、本地的 AI 记忆层** —— 给 coding agent 用。

MnemoSeed Local 是 MnemoSeed 的本地单机版：没有账号体系（localhost 隐式信任）、
CLI 优先，本机 Web 控制台随 daemon 在根路径提供；记忆按 profile 命名空间隔离——
开箱即用的是约定命名空间 `default`，可用 `mnemoseed-local profile create` 注册更多
命名空间，并经配置键 `profiles.agent_bindings` 绑定到具体 agent（design/04 §3.6）。
核心闭环：**capture → dream → decay → retrieve**，dream 推理走本地模型
（默认 ollama；保留 openai-compatible 回退驱动），自动做梦与自动回忆出厂默认开启
（`dream.auto_trigger` / `capture.auto_recall`，各可用一个配置开关回滚；
`dream --once` 为手动兜底）。

## 为什么需要它

Agent 每次新开会话都会遗忘。Local 版在本机替你保存"值得记的东西"：会话里
的高价值片段按原文入库（verbatim 通道不丢失），consolidate 成结构化的知识图，
下次检索时把对的上下文还给 agent。所有数据只在本机流转。

## 定位

- **本地优先**：默认零云依赖，dream 走本地模型
- **无账号**：localhost 即信任边界，默认使用约定的 `default` 命名空间（多 profile 管理面见 design/04 §3.6）
- **CLI 优先**：capture / retrieve / dream / decay / daemon（自动做梦为默认，`dream once` 为手动兜底）
- **生产血统**：从 mnemoseed 主仓库移植而来，存储层、schema、迁移完全同源

## 状态

A1（地基）与 A3 打包批已全部交付：config、secrets、存储端口 + 嵌入式驱动
（sqlite_meta / sqlite_graph / lancedb_embedded / bge_m3_onnx /
synthetic_embedder）、schema（stamp + graph）、迁移、CLI、安装编排、OpenCode
宿主 hook、MCP 网关、本机 Web 控制台。

Phase B 已经 main 落地：

- **会话接续（T1/T2/T3）**：会话起始回放注入、回合中 focal 自动回忆、
  消费证据强化（被引用的记忆才强化，注入本身不强化）。
- **两个一等宿主**：OpenCode 插件 + Claude Code hook 适配器（B2.10），
  逐轮捕获。
- **origin_agent 来源归属（B2.9）**：记忆记录宿主内是哪个 agent 产出的；
  惰性溯源元数据，不参与评分与排序。
- **score-pool 拆分（B2.11）**：`balance` 是真实 pending gauge，
  `filed_points_total` 是终身账本。
- **保留机制重设计**：统一保留动力学 + 线索钓回 + 淡出留痕。
- **溯源信任面**：recall/Atlas 暴露 pinned-vs-captured 判别信号，
  注入面标注 pinned 行。
- **可观测 beacon（B2.12）**：MCP 握手 beacon、doctor 的"已注册但从未连接"
  警告、`/api/v1/observability` 快照端点。
- **错误事件账本（B2.13 E0/E1）**：append-only `error_events` 表与查询原语，
  仅管道，尚无检测器。
- **daemon 可靠性**：TCP 探针 watchdog + 法医 dump、持久 `daemon.log`、
  on/off 开关、socket 存活否决误杀。

评测臂与 T4b live 标定已交付，阈值锁在 focal_floor=0.5 / budget_chars=2400
（2026-08-23 验收）。

**尚未做到**：dream 产出的是 `prefers` / `has_habit` / `decided` / `believes`
结构化事实，尚没有类型化 lesson 工件，也**不从错误中学习**；体验学习仍在建设、
默认关闭、无任何主张；宿主只有 OpenCode 与 Claude Code；**没有任何自动重启**，
daemon 死亡后手动运行 `mnemoseed-local up`；平台覆盖不均——Windows 为主测试平台，
Linux 跑 CI，macOS 需手动配置 ollama。多 session 互认知处于 pre-PRD 研究阶段，尚非功能。

开发文档见 [MVP.md](MVP.md)（范围冻结）。
