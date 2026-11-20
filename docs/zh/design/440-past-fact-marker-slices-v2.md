# 分片 440-past-fact-marker-slices v2

> 状态: DRAFT / DESIGN_PENDING。配合 design v2 阅读。全部工程一个 senior-software-engineer 批次;一次性提交。
> 相对 v1 变更: A1 增跨层注入块 marker 写集与四 surface 预算;A3 扩为跨层 golden/哈希核验。

## 分片表
| 片 | 产物(写集) | 依赖 | 验收门 | 备注 |
| A0 payload marker | daemon/memory.py: 纯函数派生 marker(source,observed_at)->{memory_marker,memory_provenance,memory_observed_at};挂 _entry_payload()(:833-854)、/session/recent chunk/group、/session/recall-pending item | 无 | AC1-AC4,AC8;降级链覆盖 AC1 时间/provenance | 零 ranking 改动;畸形项仍产出降级 marker(AC9) |
| A1 instruction rule + 预算 | mcp_gateway/instructions.py: BODY 新增规则 7(239 字符三语义);test_mcp_instructions.py CONTRACT_BODY 同步;仅缩短 SURFACE_PREAMBLES(claude_chat 136/chatgpt 130/claude_code 112);generic PREAMBLE 与 CONTRACT_PREAMBLE 字节不变 | 无(可与 A0 并行) | AC5;四 surface <=1800(1746/1795/1789/1771) | 不改 MAX_CHARS/TOOL_PLACEHOLDERS/SURFACES;精确 golden 防余量回涨 |
| A1b injection marker | hosts/common/fence.py(RECALL_ITEM_MARKER 常量)、recall.py(:203-213/:216-285)、t2.py、hosts/opencode/plugin.ts(:1032-1047/:1049-1129/:1162-1188);重钉 test_hook_ts_behavior.py/test_hosts_claude_code.py/test_b2_standing_rule_hook.py | A0,A1 | AC6;fence 与 plugin.ts marker 常量字节一致;注入块 golden 全绿 | grep 全仓枚举钉 fence 字面的 test;逐项留字节恢复证据 |
| A2 isolated oracle | 新文件 tests/test_past_fact_marker.py:临时 home + spare port;陈旧记忆 + 冲突观察 -> marker + re-verify 规则;畸形项降级 marker + debug trace | A0,A1 | AC7/AC9;红相:去 marker 字段/改规则/去降级 marker 必红 | 不碰 7788 / 已装运行时 |
| A3 golden/denylist verify | 无编辑:跑哈希、git diff、全 golden | A0-A2 | AC6:哈希对齐 section 6;plugin.ts diff 覆盖;全 golden 绿 | 机械核验 |

## 合并顺序与提交
- 单批: A0+A1+A1b 并行 -> A2 -> A3 -> 一次提交(issue -> branch -> PR,Closes #440)。
- 文件面与其它 Wave 不冲突(见 conflict table v2);可与 W1-D / W1-C / W1-B/S5 并行工作树。

## 红相要求(TDD)
1. 先写 A2,无 A0/A1 时 RED(字段/规则缺失)。
2. A0 mutant: 时间降级链返回裸 null -> AC1 oracle RED;provenance 用常量 -> I-3 oracle RED。
3. A1 mutant: 规则 7 三子句缺一 -> 文本断言 RED;加超预算字符 -> 四 surface <=1800 RED。
4. A1b mutant: plugin.ts 未同步 marker 常量或改 fence 字面 -> 跨字节一致/golden RED。
5. A2 mutant: 畸形项无降级 marker -> AC9 RED。
6. 恢复后全绿;每红相留 raw 失败行 + 字节精确恢复证据。
