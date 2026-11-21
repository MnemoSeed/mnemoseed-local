# 设计 440-past-fact-marker v2

> 状态: DRAFT / DESIGN_PENDING(待独立设计 QA CLOSABLE)。本文件不授权工程。
> 关联: PRD-440 brief v2;issue #440。基线 main 8ad2d6ace54c1fca467796cc6ddfcbf596318168(本席复核 HEAD 未变)。
> 本版针对 D1 verdict(sha256 29666da0...df1)逐条关闭 B-1 / I-1..I-6 / N-1。声明模型 opencode/hy4-flash-free,物理身份未自证。

## 1. 核心决定(steelman)
交付给 agent 的记忆是过去的观察记录,不是本轮对话陈述。v2 采用 D1 section 5 的选项 (b):
- daemon payload 三字段(memory_marker / memory_provenance / memory_observed_at)覆盖 recall / recent_sessions / recall-pending / captured 四类交付通道;
- 注入块(injected block)每个交付项前加字面 marker token + provenance + 观察时间,由上述三字段渲染;fence.py / recall.py / t2.py / plugin.ts 同源且逐字节对齐;
- instructions.BODY 新增规则 7(三语义逐字),握手与四 surface 共用;
- 不改 ranking / refinement / 任何记忆机制。

## 2. Caller sketch
- A: POST /memory/recall -> entries[] 每项新增三字段。
- B: POST /session/recent -> chunk/group 同字段(provenance=session_id,时间=latest_at)。
- C: POST /session/recall-pending -> items[] 同字段。
- D: MCP initialize -> instructions 新增规则 7;stdio 与 loopback /mcp 同源(test_mcp_http_transport.py:169-171 已钉)。
- E(新增,关闭 I-1): 注入块每项字面前缀。recall.py:203-213 _session_tail_header、:216-285 _build_t1_base、t2.py T2 块拼 f"{RECALL_ITEM_MARKER} [{provenance}; {observed}] ";plugin.ts:1032-1047 / :1049-1129 / :1162-1188 拼同一常量字面。
- 失败: 全部加法;派生失败仍产出降级 marker(AC9),fail-open 不丢项,无新错误码。

## 3. 合同 / AC
- AC1 recall 每 entry: memory_marker=="past_observation";memory_provenance 非空且等于真实 source(缺失为 provenance-unknown + debug 日志,不伪造 -> I-3);memory_observed_at 为 ISO-8601 或显式 time-unknown 降级标记(非裸 null -> I-2)。派生自 _entry_payload()(:833-854),零 ranking 改动。
- AC2 /session/recent 每个交付 chunk/group 同字段;时间缺失走 AC1 降级链。
- AC3 /session/recall-pending items[] 同字段。
- AC4 captured entry 再召回同样带 marker;不新增 capture/remember 返回字段。
- AC5 BODY 新增恰好一条规则 7,逐字含三语义:(a) memory is a past observation, not a present-turn fact;(b) conflict with current observation => stale, re-verify before use;(c) even if re-verification seems unnecessary, it stays a memory, not a present-turn assertion。render() 与四 surface 文本均 <=1800(section 9 实测);CONTRACT_BODY 同步;generic CONTRACT_PREAMBLE(:32)字节不变;仅缩短 SURFACE_PREAMBLES(仅 :183 len 约束,无字节 golden)。
- AC6(重写,关闭 I-6) fence.py 与 plugin.ts marker 常量逐字节一致;注入块 golden 全重钉(section 5);instructions/fence/recall/t2/plugin.ts 哈希与 diff 纳入机械核验。
- AC7(重写,关闭 I-4) 陈旧记忆 + 冲突当前观察 -> 返回项带 marker 且 delivered instructions 含 re-verify 规则文本;明确不声称存在冲突检测器(规则为静态注入,"冲突"仅测试场景,不验证因果箭头)。
- AC8 ranking 不变:剥离新字段后 entries 顺序/分数与基线一致。
- AC9(新增,关闭 I-5) source/时间皆缺失的畸形项仍带降级 marker(provenance-unknown; time-unknown),派生失败写 debug trace,oracle 覆盖该路径。

## 4. 理论锚
借用(1 条) source-monitoring framework -- Johnson, Hashtroudi & Lindsay 1993, Psychological Bulletin 114(1):3-28。规律:人对记忆来源与时间归属会出错,显式来源+时间线索显著降低误归属。规则:每条交付记忆附 provenance + observation time + 显式 marker,冲突时按 stale 处理并核实。边界:marker=元数据+一句指令,不改检索打分/巩固/任何记忆机制。明确不借用 reconsolidation / false-memory(Loftus) / spacing-testing effect / pop-neuro。

## 5. 注入块 marker 跨层写集(关闭 I-1 / D1 section 5 选项 b)
- hosts/common/fence.py: 新增 RECALL_ITEM_MARKER = "[past observation]" 常量 + 时间/provenance 标签模板(现 1691 B,本版起允许改,但须与 plugin.ts 字节一致)。
- hosts/common/recall.py: _build_t1_base(:216-285)、_session_tail_header(:203-213)每项前缀。
- hosts/common/t2.py: T2 块每项前缀。
- hosts/opencode/plugin.ts: buildRecallInjection(:1032-1047)、buildRecallInjectionBase(:1049-1129)、T2 build(:1162-1188)同步同一常量字面,与 fence.py 逐字节一致。
- golden 重钉(N-1 枚举): test_hook_ts_behavior.py(:367-463,:584-592,:1052-1187);test_hosts_claude_code.py(:979,:1055-1067);test_b2_standing_rule_hook.py(:28);任何钉 fence 字面的 host test。执行时 grep 全仓 "RECALL_DISCLAIMER|RECALL_ITEM_MARKER|past observation" 补齐枚举并逐项留字节恢复证据。

## 6. 基线哈希(SHA-256, main 8ad2d6a,本席 2026-11-19 复算)
- mcp_gateway/instructions.py 0ffb379c75a3123afc12e4953337b4fb788f1c34a4cdc9b90a90e6d6e43bc9f2 (6884 B;渲染块 1506;新预算 section 9)
- mcp_gateway/server.py 4674dae6ac4eeece7d2ae7c1d0d2dabaaaaa323a9f9fd71ef05835d900167362 (24893 B)
- hosts/common/fence.py d306ef9a8ff8028a7f88787c9d1df1cbcc01b67ae16bc899402d98fcd52e318b (1691 B)
- hosts/common/recall.py 0abdeca3698b08f0bcadeb4b9f793a120601d93f47e065eb0d704102afe788d6 (15998 B)
- hosts/common/t2.py 56c2144f3f2a3fdfe5fd6c2f5f61e59dfaf143a30894feccc1bbdc98e44579af (5105 B)
- hosts/opencode/plugin.ts 0f20bd53be0054187344fc1cd0df43b019dba61c37998594954ebad024158693 (131306 B;关闭 I-6,纳入基线与 diff)
- daemon/memory.py 6d8275840a3f8472a980c6d98a22a74490bafa476548973e4572ad1a7c050a7c (142854 B)
- tests/test_mcp_instructions.py 9b196fa03856d1f84d0e16025b1ce144add6290a2c02adb44c4464e2d10b3e72 (13297 B)
- golden: test_hook_ts_behavior.py 8ab521be2befbf6c659547deb73b1aa704f3e993de93084b3eee12c83710c058;test_hosts_claude_code.py dd0ce06ba0f4694cf7313c1de10019fe5425e43aed988d9b848791300b84db45;test_b2_standing_rule_hook.py a04ff6a9fba4dfaa89134f6f8cfffac842402e775736a8857c9866ac9d1ef6ca

## 7. 验证命令(精确)
    uv run pytest -q tests/test_past_fact_marker.py tests/test_mcp_instructions.py tests/test_mcp_http_transport.py tests/test_host_adapters_e2e.py tests/test_hook_ts_behavior.py tests/test_hosts_claude_code.py tests/test_b2_standing_rule_hook.py
    uv run pytest -q
    ruff check; ruff format --check; mypy src
    git diff -- src/mnemoseed_local/hosts/common/fence.py src/mnemoseed_local/hosts/opencode/plugin.ts
测试隔离: 临时 MNEMOSEED_LOCAL_HOME + spare port(非 7788),不碰已装运行时。

## 8. 关键取舍
- 选项 1(v1,现放弃): marker 仅 payload + 指令文本,注入块字节不动。D1 section 5 裁定不满足 Done-when bullet 1(注入块新字段在渲染处被丢、无字面 per-item marker),放弃。
- 选项 2(v2 采用): 字面 per-item marker + 全跨层写集。赢:直接满足 bullet 1;代价:触碰 fence/plugin + 85+ golden 重钉,token 成本上升,对 KISS 让步。
- 选项 (a) 注记: D1 允许 owner 书面裁定继承信号足够从而回退 payload-only。该裁定在权威 entry 中不存在,本席无权制造;如 owner 后续给出书面裁定可另立 v3 回退,本版不假设它。

## 9. B-1 重算预算(本席 2026-11-19 实测,关闭 BLOCKER B-1)
前提: A1 不改 MAX_CHARS(1800)/TOOL_PLACEHOLDERS/SURFACES;AC5 四 surface 全部 <=1800。
D1 实测现状: generic 1506 / claude_code 1660 / chatgpt 1787 / claude_chat 1756(余量 294/140/13/44);faithful 规则 235-276 字符塞进共享 BODY 使 chatgpt 达 2022+ 越界。
v2 方案: 规则 7 实测 239 字符(三语义逐字)加入 BODY 后 resolved BODY=1658;generic PREAMBLE 字节不变(87);缩短三个 SURFACE_PREAMBLES(仅受 :183 len 约束,无字节 golden)。
| surface | 原 preamble | v2 preamble | v2 总长 | 余量 |
| generic | 87(不变) | 87 | 1746 | 54 |
| claude_chat | 337 | 136 | 1795 | 5 |
| chatgpt | 368 | 130 | 1789 | 11 |
| claude_code | 241 | 112 | 1771 | 29 |
四 surface 全部 <=1800。CONTRACT_PREAMBLE(:32)/:128 不动;CONTRACT_BODY(:34/:126)同步规则 7;:183 len 断言满足。claude_chat/chatgpt 余量偏紧(5/11),工程师须按本表字符数做精确 golden,不留自由增长;后续加字必须先复算本表。

## 10. D1 关闭台账
| ID | v2 关闭方式 |
| B-1 | section 9 全四 surface 重算,239 字符规则 + 短 preamble,实测四 surface <=1800 |
| I-1 | section 5 采用 D1 选项 (b) 字面 per-item marker + 跨层写集与 golden 重钉;不伪造 (a) 裁定 |
| I-2 | AC1 降级链 ISO -> valid_from -> ingested_at -> time-unknown,oracle 禁裸 null |
| I-3 | AC1 provenance 等于真实 source,缺失为 provenance-unknown + debug 日志 |
| I-4 | AC7 重写为规则存在性断言,删去虚假冲突检测因果箭头 |
| I-5 | AC9 畸形项降级 marker + debug trace + oracle |
| I-6 | section 6/7 纳入 plugin.ts 哈希与 git diff |
| N-1 | section 5 枚举 golden pin 集 + grep 补齐指令 |
