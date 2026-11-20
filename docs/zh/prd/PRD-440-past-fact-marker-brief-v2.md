# PRD-440 · 交付记忆的过去观察标记 (brief v2)

- 状态: PLANNING / DESIGN_PENDING。本 brief 只定义目标与验收,不授权工程。
- 关联: issue #440 (queue, OPEN)。基线 main 8ad2d6ace54c1fca467796cc6ddfcbf596318168。
- v2 相对 v1: 修正"当前渲染块 1506 => 新文本预算 <=294"这一仅按 generic 计算的错误约束(见下);明确注入块需逐项字面 marker(D1 section 5 选项 b)。

## What
交付给 agent 的记忆(injected block / recall 命中 / recent_sessions 尾 / captured entry)读起来像本轮刚说的话;与现实冲突时被当当前事实。

## Done when
1. 每一类交付记忆项都带显式过去观察标记:稳定 marker token + provenance + 观察时间。注入块(injected block)内必须是逐项字面 marker,D1 已裁定 payload-only 不满足此条。
2. instructions.py BODY 新增一条规则,逐字覆盖三语义:记忆是过去事实;与当前观察冲突属 stale 须核实;即便判断无需核实仍是记忆而非本轮断言。四 surface 文本全部 <=1800。
3. 隔离测试:陈旧记忆 + 冲突当前观察 => 返回项带标记 + re-verify 规则;畸形项带降级标记。
4. 无固定端口、无真实 runtime home(临时 MNEMOSEED_LOCAL_HOME + spare port)。
5. 不改 ranking / dream refinement;不要求每次都重新核实。

## 关键约束(已实地核验,v2 修正)
- instructions.py:35 MAX_CHARS=1800,A1 不得改。v1 按 generic 实测 1506 得出"预算 <=294"是错误的:四 surface 实测 generic 1506 / claude_code 1660 / chatgpt 1787(余 13) / claude_chat 1756(余 44)。faithful 规则必须全四 surface <=1800;v2 以 239 字符规则 + 缩短 SURFACE_PREAMBLES 达成(见 design v2 section 9 实测表),generic CONTRACT_PREAMBLE 字节不变。
- test_mcp_instructions.py:34 CONTRACT_BODY 是 BODY 测试副本,须同步;:128 仅钉 generic preamble 字节;:183 仅检查四 surface len <= MAX_CHARS。SURFACE_PREAMBLES 无字节 golden,可安全缩短(关闭 D1 B-1)。
- fence/disclaimer 与 opencode TS plugin 逐字节一致(fence.py:11-23);注入块 golden 钉死确切字节。v1 禁止改注入块字节 -> D1 裁定不满足 Done-when #1。v2 改为 fence/recall/t2/plugin.ts 跨层同改并重钉 golden(切断跨语言漂移风险)。
- daemon/memory.py recall 项已含 source/provenance_source/ingested_at/valid_from/host/session_id(:833-854),可零新机制派生 marker。

## 理论锚
见 design v2。借用 Johnson et al. 1993 source-monitoring;不借用 reconsolidation / false-memory / spacing effect。元数据 + 指令,不改记忆机制。

## 范围外
ranking 重建;dream refinement;新增 MCP 工具名;改端口/真实 home;任何模型调用。注入块跨层写集与 golden 重钉在范围内(v2 修正)。
