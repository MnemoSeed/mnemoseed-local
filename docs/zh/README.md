# MnemoSeed Local

**单机、单用户、本地的 AI 记忆层** —— 给 coding agent 用。

MnemoSeed Local 是 MnemoSeed 的本地单机版：没有账号体系（localhost 隐式信任）、
没有控制台、CLI 优先；profile 固定为 `default`（框架内部保留 profile 机制）。
核心闭环：**capture → dream --once（手动）→ decay → retrieve**，dream 推理走
本地模型（默认 ollama；保留 openai-compatible 回退驱动）。

## 为什么需要它

Agent 每次新开会话都会遗忘。Local 版在本机替你保存"值得记的东西"：会话里
的高价值片段按原文入库（verbatim 通道不丢失），consolidate 成结构化的知识图，
下次检索时把对的上下文还给 agent。所有数据只在本机流转。

## 定位

- **本地优先**：默认零云依赖，dream 走本地模型
- **无账号**：localhost 即信任边界，profile 固定 `default`
- **CLI 优先**：capture / retrieve / dream --once / decay / daemon
- **生产血统**：从 mnemoseed 主仓库移植而来，存储层、schema、迁移完全同源

## 状态

A1（地基）已完成：config、secrets、存储端口 + 嵌入式驱动
（sqlite_meta / sqlite_graph / lancedb_embedded / bge_m3_onnx /
synthetic_embedder）、schema（stamp + graph）、迁移。
CLI 表面（capture / retrieve / dream / daemon）在 A2。

开发文档见 [MVP.md](MVP.md)（范围冻结）。
