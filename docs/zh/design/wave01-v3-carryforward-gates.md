# Wave01 v3 跨包 carry-forward 门

> 状态: DESIGN NOTE(实施期门登记,非新设计轮)。本文件不授权工程、不冻结。
> 目的: W1-A(#440)与 W1-D(#441+G0)在 D1/D2 round-2 判 CLOSABLE 且本轮不做新设计轮,冻结时需携带以下实施期门;W1-C 与 W1-B 本轮另出 v3。声明模型 opencode/hy4-flash-free,物理身份未自证。

## 1. W1-A #440 owner 裁定绑定
owner 已接受字面 per-item marker 跨层写集(fence.py / recall.py / t2.py / plugin.ts 的每 item 前缀 + 金色字节重钉),本轮不再重开 option (a)。原 v2 的四表面预算(generic 1746 / claude_chat 1795 / chatgpt 1789 / claude_code 1771,均小于等于 1800;D1 round-2 独立复算一致)仍为设计基线。以下两项为实施期 IMPORTANT 门,冻结时必须携带。
### A-I1(IMPORTANT): 表面 preamble 的短语约束被设计漏记
- 声称: v2 设计称 SURFACE_PREAMBLES 只受 tests/test_mcp_instructions.py:183 的字节上限约束、无字节金色可安全缩短。
- 证据: tests/test_mcp_instructions.py:289-299 还要求各非 generic 表面保留短语:claude_chat 含 personal preferences、chatgpt 含 custom instructions、claude_code 含 capture hook(对 .lower() 断言)。D1 round-2 section 6。
- 门: 实施时缩短 preamble 必须保留上述短语;AC5/section 9 需承认 :289-299,并在金色中逐字引用缩短后的 preamble 或断言短语仍在。若缩短到 136/130/112 时丢短语,:289-299 立即 RED。
- 关闭判据: 四表面同时满足 :183 字节上限(全小于等于 1800)和 :289-299 短语断言;两者皆绿才关 A-I1。
### A-I2(IMPORTANT, silent-failure): 每 item marker 字节未进预算
- 声称: v2 的每 item marker 前缀改变注入块字节,但预算核算未更新任何写集。
- 证据: recall.py:230/258/265-272(fixed 与 len(text)+1 与切片预算)不含任何每 item 前缀;plugin.ts:1096 lineCost=text.length+1、:1106 sliceBudget、:1127 if(block.length > MAX_INJECT_CHARS) return null 会在满预算块加前缀后静默丢掉整个 injection,无错误轨迹。D1 round-2 section 6。
- 门: 把 marker 每 item 长度加入 recall.py 两处预算计算与切片预算、以及 plugin.ts:1070-1076/1096-1106 的 lineCost 与 sliceBudget;并加 oracle 断言满预算 payload 仍产生带标记且非 drop 的块。
- 关闭判据: 满预算输入下 plugin.ts:1127 不返回 null、块中每 item 均有 marker、相关金色重钉绿。

## 2. W1-D #441 + G0 docs NIT
### N-D2(NIT,带重开阈值): 根因散文过述确定性传输边界丢失
- 声称: 441 v2:12 称信息在进入 Python 前已丢失(确定性 transport-boundary loss)。
- 证据: D2 round-2 section 3.3 探针 parse_counts(U+FFFD)=None;正常空 DOM 解析为 [0,0,0];原始 CI 单 U+FFFD 同样兼容 transient no-digit/render 状态;CI 失败间歇、runner 装了完整字体集,持续 DOM 损坏不太可能。D2 round-2 section 6。
- 门(无重设计): 散文软化为「与传输或编码边界、或瞬时无数字状态一致;持续 DOM 侧丢失由 raw diagnostic 检测,须升级」;机制不变(red phase A + 4 已覆盖)。
- 重开阈值: 若有人拿出原始 CI 字节证明每个 frame 的 textContent 本身持续为 U+FFFD,使重试永不取到数字、修复永不转绿,则 B-D1 重开、退回设计;否则仅为散文清理,NIT 不阻塞冻结。

## 3. carry-forward 汇总(冻结携带)
| 包 | round-2 结论 | 本轮是否新设计轮 | 冻结携带项 |
| W1-A #440 | CLOSABLE(0/6/1) | 否 | A-I1(短语约束)/ A-I2(每 item marker 进预算);owner 已接受跨层写集 + 金色重钉 |
| W1-D #441+G0 | CLOSABLE(0/0/1) | 否 | N-D2(散文软化 + 重开阈值) |
| W1-C #390 | NOT CLOSABLE(1/4/1) | 是(v3) | B-1 执行侧重设计 + G0 重基线门,见 390 v3 |
| W1-B #381 | CLOSABLE(0/1/1) | 是(v3) | I-B4 门可执行化 + N-B3 全限定,见 381 v3 |
