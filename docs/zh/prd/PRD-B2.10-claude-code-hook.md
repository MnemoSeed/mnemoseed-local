# PRD · B2.10 Claude Code hook 适配器（安装 + 摄取映射）

> 依据：
> - GitHub issue #106（2026-08-25 用户指令）：宿主适配器目前仅 OpenCode；Claude Code 应获得一等捕获待遇（规范 session id、模型 id、prompt/stop hooks），让"一个记忆库服务所有编码 agent"成立。
> - solution-architect 评审（2026-08-25）verdict：**SHIP-WITH-ADJUSTMENTS**——事件映射正确且 wire 层早已预留 `HostId.CLAUDE_CODE` 与事件注释；但"镜像 opencode 安装生命周期"的承重假设错误：opencode 靠自动发现目录做文件拷贝，Claude Code 是用户自有 `settings.json` 的合并手术，生命周期必须重设计。产品侧：本批是队列中最大采用杠杆，依赖 #105 的 agent 字段（已合并，5db3613）。

## 理论锚（无新增；继承既有锚）

- R7 来源监控（写入侧 provenance 永不衰减）与 R30 捕获中立照旧约束摄取链路——本批只是新增宿主入口，不改语义。
- **不借**清单：CC 官方文档之外的一切"hook 行为"民间说法；任何把 Stop 当会话结束的直觉（opencode dogfood 已证伪："idle ≠ ended"，plugin.ts:759-763 同类教训直接继承为设计规则）。

## 设计定案（机制层）

### 安装生命周期（settings.json 合并手术，非文件拷贝）

1. **install** = 幂等合并带 marker 的 handler 条目进 `hooks.{UserPromptSubmit,Stop,PostToolUse,PreCompact,SessionEnd}`（marker：command 含保留前缀 `mnemoseed-local`）；绝不触碰外来条目。
2. **status** = marker 探测（present/absent/stale）+ 既有 `/healthz` 探针复用。
3. **disable/enable** = 我们条目上的 disabled 标志（不可用 opencode 的改名技巧——CC 无 glob 发现机制）。
4. **uninstall** = 仅移除 marker 匹配条目，外来 hooks 原样保留。
5. CLI verb 扩展 `--host claude_code`（`hosts/install.py` 的 choices 断言随之放开）；在 `hosts/` 内引入最小 host 适配协议，opencode 模块保持原函数不金镀。

### 转换器（隐藏 CLI verb）

- `mnemoseed-local _hook-event --host claude_code`：读 CC stdin JSON → 构造规范 IngestEvent POST 到 daemon。
- **stdout 纪律（红线）**：UserPromptSubmit 的 stdout 会泄入用户上下文——转换器成功/失败路径一律零 stdout；fire-and-forget（~2s 超时、吞失败、opt-in debug lane，镜像 plugin.ts:20-29 纪律）。
- profile 绑定：`MNEMOSEED_LOCAL_PROFILE_ID || "default"` 环境约定，daemon 从不猜身份。

### 事件映射 v1

| CC 事件 | 映射 | 备注 |
|---|---|---|
| UserPromptSubmit (async) | user_prompt | stdin 带 session_id/cwd；agent 字段有则填（#105 规范字段从第一天生效） |
| Stop (async) | assistant_message | **→ /flush 语义由 segmenter 自然承担，绝不 /session/end**；模型 id 从 transcript_path 尾部 JSONL 解析，解析失败降级为无模型事件、绝不丢事件 |
| PostToolUse | tool_use | |
| PreCompact | POST /flush | |
| SessionEnd | POST /session/end | fire-and-forget 不等待 drain（CC 1.5s 共享预算 vs drain 等待冲突；teardown flush_all 兜底） |
| SubagentStart/Stop | v1 不摄取 | raw 记录 agent_type 备将来采用 |

### 出界（v1 不做）

- MCP 自动注册进 CC（对齐 opencode 的手动文档步骤）；项目级 hooks；历史会话回填；子代理流量摄取。
- 会话结构如实扁平：CC 载荷若无父子链接就诚实记录（同 #75-P1 姿态）。
- 已知边界：SessionEnd settle POST 采用 0.5s×4 相位预算（最坏 ~2s），极慢 loopback 下仍可能超过 CC ~1.5s 共享 teardown 时钟而被宿主先杀——daemon teardown flush_all 兜底，已落盘数据不丢。

### JSONC 风险（实现时先验证）

settings.json 若含注释（JSONC）：容忍解析后合并；不可行则 stderr 输出手动编辑指引（install CLI 的输出不涉 stdout 红线）。

### 测试预言

- fixtures 镜像 `tests/fixtures/opencode_hook/*` 模式 → `tests/fixtures/claude_code_hook/*`，逐一样本过 IngestEvent 验证 + daemon 接受。
- stdout 沉默 oracle：转换器成功与失败路径输出均为空。
- 幂等合并 oracle：装两遍 settings 字节稳定；uninstall 后外来条目 bit-for-bit 保留。
- Stop→flush 回归钉（防重演 idle≠ended 事故类）。
- transcript 解析失败降级路径：assistant 事件仍到达（无 model id）。
