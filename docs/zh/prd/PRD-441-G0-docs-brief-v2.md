# PRD-441-G0-docs brief v2 (atlas U+FFFD flake + G0 文档对齐)

- 状态: PLANNING / DESIGN_PENDING。本 brief 不授权工程。
- 关联: issue #441 (queue, OPEN);PRD-309 G0 节;PRD-B7 3.6/3.7 节。基线 main 8ad2d6ace54c1fca467796cc6ddfcbf596318168。
- v2 相对 v1: 根因重写为原始 CI 单 U+FFFD(信息在编码边界丢失),修复机制由 normalize+子串改为浏览器内整数解析;拒绝写死 open 数;登记 README catalog 矛盾的后续对账。

## What
1. atlas-browser CI 偶发 AssertionError: Atlas empty state missing: 单个 U+FFFD(run 44097403996,2026-11-17)。原始字节显示 repr 只有一个替换字符。inner_text() 字符串跨 runner locale/编码边界后三 token 信息已丢,normalize 无法恢复。
2. G0 文档对齐:README import 段只列 codex_cli;design 14 §2 zcode 回填 ❌ 与已合并 reader 冲突;design 14 §6 setup 流程标设计目标;PRD-309 baseline/issue 事实刷新。

## Done when
1. atlas 断言对原始单 U+FFFD 形状稳健:计数在浏览器内解析为整数经 JSON 返回,不经 inner_text();三计数 token 仍全断言(整数),不裸奔成只匹配 "0 items"。red phase 必须含原始单 U+FFFD 与 "10 items" 两形状。
2. README import 段列 codex_cli / claude_code / opencode / zcode 四 reader 与 --dry-run。
3. design 14 §2 zcode 回填更新;§6 setup 标设计目标并注明当前动词。
4. PRD-309 baseline 与主 hash 刷新;开放 issue 数按执行时 gh issue list 实测写入(2026-11-19 实测 23,不写 21);in-flight 项不写成 closed;登记 G0-4 后 S6 对账 README:47-53 与 PRD-309:59 的 catalog 矛盾。
5. 隔离验证:atlas_verify 用 free port(排除 7788/4096)+ 临时 MNEMOSEED_LOCAL_HOME。

## 取舍(设计给理由)
- 采用: page.evaluate 浏览器内整数解析。理由:绕开 Python 侧编码边界,确定性强、oracle 不削弱。
- 不采用: normalize 非 ASCII 为空格(对单 U+FFFD 无信息可恢复,AC2 仍抛错);钉 Chromium locale(依赖字体/ICU,难证明)。

## 理论锚
纯工程(harness 稳健性 + 文档对账),无新增理论借用;不借用 pop-neuro / 记忆有效性叙事。

## 范围外
改 console 运行时 / app.js 文本 / Intl.NumberFormat;改产品 src;折叠进其它 PR;改 issue/PR 线上状态;固定端口或真实 home。
