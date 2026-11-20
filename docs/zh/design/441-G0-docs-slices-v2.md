# 分片 441-G0-docs-slices v2

> 状态: DRAFT / DESIGN_PENDING。配合 design v2 阅读。文件面与其它 Wave 不重叠,可完全并行。
> 相对 v1 变更: D0 由 normalize 改为浏览器内整数解析 + 有界重试;红相强制含单 U+FFFD 与 "10 items" 两形状;D3 开放数执行时实测;D3 增 G0-4 跟进登记。

## 分片表
| 片 | 产物(写集) | 依赖 | 验收门 | 备注 |
| D0 atlas 计数解析 | scripts/atlas_verify.py: 新增 parse_counts(text)->list[int]|None;read_counts(page) 用 page.evaluate 取 DOM 文本解析整数,None 时有界重等;:535-537/:575/:587 改用整数判定(raw 仅诊断) | 无 | AC1-AC3;隔离 atlas PASS | 不改 app.js;oracle 三 token 全断言(整数) |
| D1 README import | README.md:68-83 补四 reader 与 --dry-run | 无(可与 D0 并行) | AC5 | 保留 ledger/verbatim |
| D2 design 14 | docs/zh/design/14 §2 zcode 行 + §6 setup 注记 | 无 | AC6/AC7 | 不编造 setup 动词 |
| D3 PRD-309 refresh | docs/zh/prd/PRD-309: baseline、open 数(执行时实测)、#385/#432、新增 #440/#390/#441、G0-2 备注、G0-4 后 S6 对账跟进 | 无 | AC7/AC8;as of 2026-11-19 | in-flight 不写 closed |
| D4 verify | 无编辑:跑 atlas + 四门 + 文档 grep + git diff -- src/ | D0-D3 | AC9 | 机械核验 |

## 合并顺序与提交
- 单批 D0+D1+D2+D3+D4 -> 一次提交(Closes #441 与 G0 文档项)。可与其它 Wave 并行。

## 红相要求(TDD,强制)
1. D0 红相 A(原始失败形状): mock page 的 inner_text() 返回单个 U+FFFD,而 page.evaluate 的 DOM 文本为 "0 items ... Rescue 0 ... Fading 0";旧实现(normalize/子串)RED,新实现解析 [0,0,0] PASS。此例必须存在,覆盖真实 CI 字节。
2. D0 红相 B(边界形状): DOM 文本 "10 items ... Rescue 0 ... Fading 0";旧子串断言把 "0 items" 当空态误判,新实现解析首整数 10 -> 非空,RED/PASS 分离(关闭 I-D1)。
3. D0 削弱 mutant: 只断言 items==0 而省略 rescue/fading -> 三 token 测试 RED(oracle 未放宽)。
4. D0 重试 mutant: DOM 首帧无数字 -> read_counts 在超时内重试拿到整数;超时仍 None -> 断言 RED。
5. D3: QA 逐行核对 open 数与 gh issue list 实测一致(本席实测 23),禁止 21。
