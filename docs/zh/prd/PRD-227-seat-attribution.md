# PRD-227：席位提交归因约定（冻结）

日期：2026-11-05。状态：设计交付（W227-ATTRIBUTION，owner 已批 launch-plan-309），待独立 QA 与主控验收；实现批次为 **C227-SEAT-TRAILER**（写面冻结见验收记录）。审查基线：`78478edb96fd68856a4f5c7645ff77324405e85c`。范围为**仅设计**：本批不写代码、不改 CI、不动任何仓库设置；org 级约定、全局运行时与其它仓库只引用、不修改（309 红线）。

## 证据与结论边界

| 标记 | 来源与能够支持的结论 | 不能支持的结论 |
| --- | --- | --- |
| S：源码与历史检查 | 全部 file:line 与 git 计数来自基线 `78478ed`（HEAD == origin/main tip），本人逐点读过、逐条实测；trailer 解析行为经 stdin 实测钉死 | 未运行新检查、未实测 CI 行为——那是实现批与后续 PR 的事 |
| R：owner 报告 | 2026-09-26 PR #222 整改期间的"第二写者"误报（issue #227 简报） | 误报当日过程未独立复核；本设计只钉机制成因，不裁断该日细节 |
| U：UNKNOWN | 无 | — |

## 现状机制绑定（S）

**单一 git 身份 + 显示名漂移是事实，不是假设**。main 全历史 453 提交实测：author 侧 `kumhou <kumhou.siew@gmail.com>` 240 个、`Siew Kum Hou <kumhou.siew@gmail.com>` 213 个——同一 email、两个显示名；committer 侧 `kumhou` 240 个、`GitHub <noreply@github.com>` 213 个。PR #222 自身八个提交全部 author=kumhou；两个显示名并存于同一仓历史，任何跨提交的作者名扫描都会把同一人读成两个写者——这是 #222 式误报的机制成因。

**提交流**：[AGENTS.md:26-37](../../../AGENTS.md) 钉死 issue → branch → PR → merge、禁止直推 main；"每个被推的提交都是绿的"，整改 = amend 或声明性后续提交，merge 不是修复面。[check_commit_range.py:27-31](../../../scripts/check_commit_range.py) 定义逐提交廉价门，[:93-105](../../../scripts/check_commit_range.py) 逐提交物化独立 worktree、[:126-132](../../../scripts/check_commit_range.py) 逐提交红绿裁决（merge 提交**不**豁免既有廉价门），由 CI 的 `commit-range-gates` 任务对 PR 区间整体调用（[ci.yml:40-54](../../../.github/workflows/ci.yml)，仅 `pull_request` 触发，:41）。本地 [gate.ps1:5-11](../../../scripts/gate.ps1) 不含区间检查（CI 兜底）。合并形状混合：main 上 merge 提交 88 个（GitHub merge 按钮产物 + 分支内同步 merge 如 `f6fcea7`、`8e6b658`），GitHub committer 的非 merge 提交 126 个（squash 产物，最新 `9b81071` 2026-11-02，PR #340）。现行提交消息**零 trailer**（抽样实测；`Closes #N` 按约定只写 PR body）。测试面：[test_commit_range_gate.py:28-37](../../../tests/test_commit_range_gate.py) 给草稿仓固定身份、[:71-74](../../../tests/test_commit_range_gate.py) `_commit` 助手、[:117-138](../../../tests/test_commit_range_gate.py) merge 用例，共十个用例钉住 tripwire 行为；[test_daemon_caller_credential_guard.py:427-447](../../../tests/test_daemon_caller_credential_guard.py) 把 `scripts/` 下每个脚本文件钉进封闭集 KNOWN_CALLERS（:1207-1210 断言集合相等）——新增脚本文件必须同步登记，否则该测试先红。

**关键不可实现性**：git 身份字段在本仓结构上无法区分席位（单一 email、显示名漂移、GitHub 代理 committer）。"只要求委派席位提交带标"的任何按 author 过滤的规则都无从机械执行——这决定了下述冻结形状。

## 冻结设计

- **FD1 trailer 语法**：`Seat: <seat-id>`，置于提交消息的 git trailer 块（最后一段）。判定分两步：先用 git 原生解析 `git show -s --format=%B <sha> | git interpret-trailers --parse` 识别 trailer 块（实测钉死：消息中段的 `Seat:` 行不被解析、按缺失处理）；再对**原始消息字节**判定——提交对象存的是原样字节，而 `--parse` 输出会规范化（实测 `Seat:main`、`Seat:  main` 均被输出为 `Seat: main`），形状检查必须读原行、不得读解析输出。要求 trailer 块内**恰好一行**原始行匹配 `^Seat: ([a-z][a-z0-9-]*)$`（键大小写与冒号后单空格敏感）且 `<seat-id>` ∈ FD4 词表；零条、多条、形状不符（含 `Seat:main`、`Seat:  main`）、值不在词表均判红。块外杂散 `Seat:` 行不参与计数（恰一语义：归因面只有 trailer 块）。git 身份字段（author/committer）**完全不参与**判定——显示名不是归因信号，这是 #222 的教训。
- **FD2 载体范围**：PR 区间内**每个非 merge 提交**（父数 ≥2 豁免）都必须带 Seat，包括主控会话直接做的提交（docs、closeout 记录等），seat-id 用保留值 `main`。冻结理由：身份字段无法筛选席位，选择性规则不可机械执行；统一规则同时消灭"主控提交无归因"的残余盲区。GitHub merge 按钮产物落在 main、不进任何后续 PR 区间，结构上天然出界；分支内同步 merge 按父数豁免。orphan/根提交在区间内，同规则。
- **FD3 执行点：扩展现有 [check_commit_range.py](../../../scripts/check_commit_range.py)**（不建新脚本、不加 pre-commit 钩子）。精确算法：区间（`commits_of`，:47-49）逐提交先做 seat 预检（纯读 git 对象，无需物化 worktree）——(a) 读父数（扩展 :52-55 的 `_parent_of` 取父数），≥2 → 豁免；(b) 否则按 FD1 判定，失败 → 该提交记红、标签 `seat trailer`、stderr 指名 sha + 原因 + 修复指令，且**不再物化该提交的 worktree** 跑其余廉价门（该提交已被判必须 amend，补 trailer 后 CI 重读全部门；stderr 注明其余门未读，沿 :21-23 UNREAD 的"未读不等于通过"诚实语义）；(c) seat 通过 → 照旧跑 CHEAP_GATES。CI 面不变：仍是 `commit-range-gates` 一个任务一次调用。直推 main 机械上仍无 seat 检查（:41 仅 pull_request 触发）——直推 main 由 AGENTS.md:26-28 纪律禁止，本设计不扩权，如实留痕。基点语义：CI 传入的 `pull_request.base.sha` 即 base 侧锚点，`base..head` 恰扫分支新增提交、不误扫 main 侧提交（独立 QA 于 PR #347/#340 实测零误扫）；实现批可防御性改用 `git merge-base` 自算基点，若实施须以测试钉死与 CI 传入值的等价性。
- **FD4 词表**：repo 内钉死常量（check_commit_range.py 内 frozenset——CI runner 读不到用户级全局配置，词表必须在仓内自足）：`main`、`general`、`product-manager`、`mnemoseeding-explore`、`senior-marketing-executive`、`senior-qa-reviewer`、`senior-software-engineer`、`senior-software-engineer-muse`、`senior-web-designer`、`solution-architect`（= 现行注册席位 `~/.config/opencode/agents/*.md` 全集 + 内置 general + 主控会话保留值 `main`）。增长规则：org 侧注册新席位后由普通 PR 改一行常量。词表只做拼写守卫（防 `Senior-Software-Engineer` 等变体），**不做真值担保**：trailer 是自我声明，跨席位冒名属委派契约审计（org 层），不是本机械检查的目标，如实声明。
- **FD5 失败行为与修复**：缺 trailer = 该 PR 的 `commit-range-gates` 红在具体提交上，stderr 修复指令：tip 提交 `git commit --amend` 补 trailer 后 `git push --force-with-lease`；区间中段提交在**未合并 PR 分支**上 `git commit --fixup=amend:<sha>` + `git rebase -i --autosquash` 后 force-with-lease。**永不重写已达 main / 已合并历史**；检查本身纯读、永不改写任何历史（冻结验收）。**与 AGENTS.md:29-32 的有意偏离，显式登记**：trailer 是逐提交元数据，后续提交不能修复先前提交的缺失——其它门的"声明性后续提交"修复面在此不成立，Seat 是全部门中唯一 amend-only 的门。实现批落 PR 的自身提交即约定首次实跑，必须已带 Seat（引导要求见验收记录）。
- **FD6 合并形状与归因损耗（诚实边界）**：本仓现行混合使用 merge-commit 与 squash（126 个 squash 产物，最新 2026-11-02）。PR 分支提交在合并前被逐提交检查，两种形状都覆盖；但 squash 产物落在 main 后**丢失逐提交 trailer**（GitHub squash 消息不含分支 trailer），归因退化为 PR 级：squash 提交主题的 `(#N)` 反指 PR，席位记录活在 PR 分支历史里，可经 GitHub 查询。这是不重写历史前提下的最优可达面。若 owner 要求 main 上逐提交 trailer 无损，唯一途径是把仓库 merge 策略钉为 merge-commit——GitHub 仓库设置属 owner 动作、出 309 写面，本设计只登记该选项与代价（每 PR 多一条 merge 节点），不实施。
- **FD7 本仓采纳 vs org 后续采纳（引用不修改）**：本仓现在采纳：Seat trailer + check_commit_range.py 扩展检查 + FD4 词表。org 层（`G:\Development\MnemoSeed\org\PROTOCOL.md`）后续可采纳（本批不触碰）：全家族仓库推广同一约定与席位词表注册表；把"每个仓库 CI 有 seat 检查"写进 delegated-execution 条款；规整 author 显示名漂移（user 级 git 配置，出本仓写面）；席位冒名的契约审计。PROTOCOL.md 现行文本无任何 git 归因/身份条款（grep 实证），本设计与其零冲突。

## #222 式误报的反事实

误报机制：按 author 显示名扫描 → `kumhou` 与 `Siew Kum Hou` 并存 → 同一 email 被读成第二写者。约定生效后：(1) 归因查询面换成 trailer（如 `git log --format=%(trailers:key=Seat,valueonly) | %an` 的逐提交对照），同一 PR 的提交呈现为若干席位而非"两个作者名"；(2) 显示名漂移被约定显式降级为非信号（FD1 不读身份字段）；(3) 若 #222 整改分支当时带 Seat，整改报告按席位归因，"第二写者"假设在数据面直接不成立。代价是每个提交一行 trailer——token 经济上可忽略，零模型调用（纯 git 管道，符合"plumbing 无模型调用"红线）。

## 备选与取舍

- **每席位独立 git 身份**：赢面是 `git shortlog -sne` 原生归因、GitHub 面板天然分列。代价：身份是 user 级配置（出 309 写面）；每个席位需要可写凭据（密钥扩散）；author 字段可任意伪造，身份的"真实性"是幻觉；历史提交仍是旧身份，双轨期制造更多假写者。冻结验收已判死；FD1 以自我声明 + 词表守卫取其归因之利、去其密钥与伪造之弊。
- **pre-commit 钩子**：钩子不随 clone 传播、只护本地新提交、护不了 CI 已见的区间，本仓无钩子基建。否决；CI 区间检查是唯一执行点（冻结验收）。
- **新建 `scripts/check_seat_trailer.py`**：与既有区间 tripwire 平行，但 KNOWN_CALLERS 封闭集（:1207-1210）要求同步登记新文件，ci.yml 还要加一步。KISS 否决：扩展现有脚本一处改动、既有测试文件自然延伸。
- **GitHub squash 消息注入 trailer**：GitHub 无可靠的 squash 消息 trailer 注入机制，本地手工 squash 增加仪式。否决，以 FD6 的 PR 级归因边界消化。

## 理论锚

**无新增借用理论**（沿 PRD-293 §理论锚的工程接线面先例）：本设计是工作流与工具精确化（git trailer 原生机制 + 机械校验），不引入任何记忆机制，不做任何心理学/neuroscience 主张。**不借用清单**：不借社会心理学归因理论（attribution theory）为"席位归因"背书——trailer 是工程自我声明，与心理归因无关；不借"分布式认知/团队认知"类流行说法为多席位写作背书；不以"身份混同降低信任"之类的说法支持设计。唯一的经验依据是**工程事实**（453 提交、单一 email、两个显示名的实测漂移），登记于现状机制绑定一节，不构成也不需要理论锚。

## 验收记录（C227-SEAT-TRAILER）

**写面冻结**：修改仅 [check_commit_range.py](../../../scripts/check_commit_range.py)（seat 预检 + 词表常量 + 失败标签 + 未读语义）与 [test_commit_range_gate.py](../../../tests/test_commit_range_gate.py)（`_commit` 助手补默认 trailer、新增 seat 用例）；**不改**：ci.yml、gate.ps1、KNOWN_CALLERS 名单（无新文件）、check_changed_comments.py、`src/` 全部。若实现中确需新脚本文件，同步登记 KNOWN_CALLERS 是硬前提（集合相等断言会先红），且属对写面的显式扩展，须在 PR 说明。

**oracle 要求**：(1) 缺 trailer 的非 merge 提交 → 该提交 RED、标签 `seat trailer`、stderr 含 sha 与修复指令、exit 1；(2) 词表外值（大小写变体、未知名）→ RED；(3) 多条 Seat trailer → RED；(4) 消息中段的 `Seat:`（非 trailer 块）→ 按缺失 RED（`--parse` 行为已实测钉死）；(5) 原始字节形状不符（`seat: main`、`Seat:main`、`Seat:  main`）→ RED（规范化只发生在 `--parse` 输出，判定读原行——该项必须用提交后对象实测，不得只测解析输出）；(6) merge 提交（父数 ≥2，含 octopus merge）无 trailer → green（豁免显式）；(7) 词表内合法值全 green，含 `main`；(8) 既有十个用例在助手补 trailer 后全绿（行为回归钉）；(9) 解析、规范化与键大小写语义由上述测试逐条钉死并记录，不两可；(10) 中段杂散 `Seat:` 行与块内合法 Seat 并存 → green：计数只在 trailer 块内（FD1 钉死的恰一语义——块外行不参与计数、也不判红），恰一个用例；(11) FD3(b) 跳过语义：seat-red 提交的失败标签只含 `seat trailer`，不得出现 `changed comments`/`ruff check`/`ruff format` 标签，且 stderr 注明其余廉价门未读（未读不等于通过的诚实性用例）；(12) 行尾空白与 CRLF 作者痕迹（`Seat: main `、`Seat: main\r`）→ RED（原始字节严格匹配的命名 RED 用例）；(13) 对抗自验：词表常量清空、豁免条件把单父误判双父、形状检查误用解析输出（`Seat:main` 被规范化放行）、块外计数误开（中段杂散行被判红）各须被测试击杀。全 gates + 资深对抗 QA 无 BLOCKER 后方可收口；实现批自身提交必须已带 `Seat: senior-software-engineer` / `Seat: main`（约定首次实跑即引导样本）。

**保持 open（不随实现批关闭）**：org 层采纳（FD7，归 org 协议修订）；merge 策略钉死与否（FD6，owner 动作）；直推 main 的机械禁令（独立议题）；跨席位冒名的契约审计（org 层）。

## 范围外（明确不做）

不改写任何已达 main 的历史（含补打历史 trailer）；不动 user 级/全局 git 配置与 `~/.config/opencode` 注册表；不改 org PROTOCOL.md 与其它 mnemoseed 仓库（309 红线）；不引入 pre-commit 钩子；不做 author 显示名规整；不把 trailer 语义接入记忆系统（与 mnemoseed 产品面零交集，纯工作流设施）。
