# 设计 441-G0-docs v2

> 状态: DRAFT / DESIGN_PENDING(待独立设计 QA CLOSABLE)。本文件不授权工程。
> 关联: PRD-441-G0-docs brief v2;issue #441;PRD-309 第 G0 节。基线 main 8ad2d6ace54c1fca467796cc6ddfcbf596318168。
> 本版针对 D2 verdict(sha256 29af2006...276)关闭 B-D1 / I-D1 / I-D2 / I-D3 / N-D1。声明模型 opencode/hy4-flash-free,物理身份未自证。

## 1. 核心决定(steelman)
把 runner 编码假象挡在 harness 的匹配层,并把 G0 文档事实层与当前 main 对齐。v2 关键变更:不再"normalize 后字符串匹配",改为在浏览器内把计数解析成整数经 CDP/JSON 通道返回,从根上避开 locale 敏感的编码边界。

## 2. Caller sketch(根因重写,关闭 B-D1 / N-D1)
- 原始证据: gh run view 44097403996 --log-failed 得 AssertionError: Atlas empty state missing: 单个 U+FFFD(repr 只有一个替换字符)。
- 确定性根因陈述: 该失败串是 count.inner_text()(atlas_verify.py:446)返回的字符串跨越 runner 的 locale/编码边界后,三个计数 token 信息已被替换为单个 U+FFFD;信息在进入 Python 前已丢失,故任何 normalize 都无法恢复,AC2 三 token 断言仍会抛错。
- 机制切换: 新增 read_counts(page) -> list[int] | None,用 page.evaluate 在浏览器内对 #atlas-count 的 textContent 做正则解析,返回 [items, rescue, fading] 三个整数(JSON 通道,UTF-8 安全,不经过 inner_text());无数字时返回 None 并做有界重等重试(关闭 B-D1)。
- 失败诊断: 错误文本同时含 best-effort raw(仅诊断,不参与判定)与解析出的整数,保留诊断力。
- 入口 B: 读者读 README / design 14 / PRD-309 -> 四 reader 与 --dry-run 可见;zcode 回填标更新;setup 标设计目标;baseline/facts 为实测值。

## 3. 合同 / AC
- AC1(重写,取代 v1 的 normalize_display): 新增纯函数 parse_counts(text: str) -> list[int] | None:从文本解析首个 "N items ... Rescue M ... Fading K" 的整数三元组;对 DOM 文本保持确定性;对无法解析(含单 U+FFFD)返回 None。供 page.evaluate 内调用或等价内联表达式;不做非 ASCII 折叠为空格的归一化(该做法对单 U+FFFD 无信息可恢复)。
- AC2(重写) :535-537 用 read_counts(page) 的返回值判定:items==0 且 rescue==0 且 fading==0 才算空态;None 或任一不符即 AssertionError,错误文本含 raw 与解析值。三 token 仍全部断言(oracle 不削弱),但以整数而非子串表达(关闭 I-D1)。
- AC3 :575 的 filled_count 用同一 read_counts:首个整数 >0 才算已更新;不再用 "0 items" in filled_count(子串误判)。:587 报告输出记录解析值与 raw。
- AC4 不改 app.js:1255 / fmtNum / Intl.NumberFormat / index.html charset;不改产品 src。
- AC5 README.md:68-83 import 段列四个 reader 与 --dry-run(同 v1)。
- AC6 design 14 §2 zcode 行 backfill 更新;§6 setup 注记(同 v1)。
- AC7 PRD-309 刷新:版本行 hash -> 8ad2d6ac(as of 2026-11-19);开放 issue 数按执行时 gh issue list 实测写入(本席 2026-11-19 实测 23,不得写死 21,关闭 I-D2);#385 OPEN / #432 OPEN under QA;登记 #440/#390/#441 为 queue;G0-2 注明 design 14 由本批承接。
- AC8(新增,关闭 I-D3) 后 S6 文档对账:登记 G0-4 跟进项(在 PRD-309 显式记录):README:47-53 仍宣称 provider catalog 可用,而 PRD-309:59 标 CONTRADICTED;S5/S6 排在 D 之后,故本批不改该声明,但必须在 PRD-309 登记"待 W1-B/S6 合并后对账 README:47-53 与 PRD-309:59 并关闭 G0-4",不得静默遗留。
- AC9 全变更限于 docs + scripts/atlas_verify.py;无产品 src 改动,git diff -- src/ 空。

## 4. 理论锚
无新增借用。harness 文本/整数解析与文档事实对账是工程稳健性,不引入认知科学规律。不借用 pop-neuro / 记忆有效性结论 / CI flake 治理方法论包装。

## 5. Denylist
- 不把 oracle 放宽为只判 "0 items" 或删断言;三计数 token 仍全部以整数断言。
- 不改 app.js / index.html / fmtNum / Intl.NumberFormat / console 运行时文本。
- 不把 in-flight 项(#432/#385/#440/#390/#441)写成已关闭;不 retag 版本、不改 pyproject。
- 不改端口策略,不碰 7788/4096 与已装运行时;不折叠进其它 PR。
- 不再依赖 normalize_display 把非 ASCII 折成空格作为修复机制(对单 U+FFFD 无效)。

## 6. 基线哈希(SHA-256, main 8ad2d6a,本席复核,均未变)
- scripts/atlas_verify.py 515bb4048554a0801a358db72a1552151e7d1fa65f898fbd4b726ff20f058873 (24296 B;等待 :438-446,失败点 :536-537,报告 :587)
- README.md fac4bc677f58cf25a0b546acd66cdaba89450b75e77194351e97526e6267fd42 (6270 B;import :68-83;catalog 声明 :47-53)
- docs/zh/design/14-product-architecture.md fdefac14af9b83f8450684ae18ab7ebc30abea3d09863f9bcbd3adb1c3575fc8 (10646 B)
- docs/zh/prd/PRD-309-public-alpha.md e607cb057f583739142447a0050ff64c2a90241b7ea914420937ba34b70e6c43 (18754 B;baseline :4,open :69,#385 :61,#catalog 矛盾 :59)

## 7. 验证命令(精确)
    uv run pytest -q tests/test_atlas_preflight.py    # 若存在;否则跳过
    uv run python scripts/atlas_verify.py             # 隔离 run dir + free port,输出 PASS
    ruff check scripts/atlas_verify.py; ruff format --check scripts/atlas_verify.py
    Select-String README.md -Pattern 'import --host codex_cli|import --host claude_code|import --host opencode|import --host zcode|--dry-run'
    Select-String docs/zh/design/14-product-architecture.md -Pattern 'zcode|setup|init --mode capture-recall'
    Select-String docs/zh/prd/PRD-309-public-alpha.md -Pattern '8ad2d6ac|2026-11-19|G0-4|#432'
    git diff -- src/    # 必须空

## 8. D2 关闭台账
| ID | v2 关闭方式 |
| B-D1 | section 2 从原始单 U+FFFD 重建根因(编码边界丢信息),机制切到浏览器内整数解析 page.evaluate;red phase 强制含单 U+FFFD 形状 |
| I-D1 | AC2/AC3 用首个整数 ==0 / >0 判定,parse_counts 边界解析,不再用子串 "0 items" in "10 items" |
| I-D2 | AC7 开放 issue 数改执行时实测(本席实测 23),禁止写死 21 |
| I-D3 | AC8 新增 G0-4 后 S6 文档对账跟进项并登记在 PRD-309,不静默遗留 README:47-53 矛盾 |
| N-D1 | section 2 用单条确定性陈述锚定原始 CI 字节,删除自相矛盾的问句链 |

## 9. 关键取舍
- 选项 1(v1 采用,现放弃): 断言前非 ASCII 归一化为空格 + 三 token 子串匹配。D2 实测单 U+FFFD 无信息可恢复,归一后三 token 仍缺,AC2 仍抛错,放弃。
- 选项 2(v2 采用): page.evaluate 在浏览器内解析整数经 JSON 返回。赢:绕开编码边界、确定性强、三 token 以整数全断言;代价:新增 DOM 侧解析表达式与有界重试逻辑,harness 改动略大。
- 选项 3(不采用): 钉 LANG / Chromium locale。代价:依赖 runner ICU/字体,难证明根因消除。
