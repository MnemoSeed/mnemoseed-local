# PRD-228：分支/工作树单写者卫兵——O_EXCL 认领、加性台账与显式释放（设计冻结）

日期：2026-11-05。状态：设计交付（W228-WRITER，重冻结版），待 slice-1 实现批与独立 QA；同日设计 QA 裁决 CLOSABLE-conditional（0 BLOCKER，绑定全 TRUE），其三项修订（I-1/I-2/I-3）与两条注记（N-1/N-2）已并入本版。审查基线：`78478edb96fd68856a4f5c7645ff77324405e85c`。范围为**仅设计**：本批不写代码、不改任何行为；slice-1（程序性认领/台账/释放）实现批的写面冻结见验收记录小节；slice-2（fail-closed 控制器）是独立未来批，本 PRD 只冻结其边界与红线，不含其实现验收。承接 [#228](https://github.com/MnemoSeed/mnemoseed-local/issues/228)（含 issue 评论中已记录的架构审查六项已定决策——trailer 身份、认领时点前置、CI+控制器执行、零迁移、拒绝清单、过期认领显式释放），并受 launch-plan-309 冻结单元验收约束：**O_EXCL 认领 / 加性记录 / 显式释放三件套原样保留；无 TTL 自动释放；slice 1 与 slice 2 显式分离；slice 1 不宣称任何预防力；本设计不释放 G00 历史 unknown**。

## 证据与结论边界

| 标记 | 来源与能够支持的结论 | 不能支持的结论 |
| --- | --- | --- |
| S：源码/工件检查 | 本文件全部 file:line 绑定来自基线 `78478ed` 的实际仓库与 org HQ 实际文件（AGENTS.md、PROTOCOL.md、roster.md、ledger.json、work-packages.json、`~/.config/opencode/agents/senior-software-engineer.md`、org 插件源 ledger.mjs/schema.mjs），本人已逐点读过；`git worktree list`/`git log`/issue 正文与评论为 2026-11-05 当日实测 | 未实现、未运行 claim 脚本（设计批红线）；org 插件源读的是 org 源树，不保证与 ZCode 插件缓存 0.4.1 安装副本逐字节一致 [unverified] |
| R：历史作者报告 | issue #228 评论（solution-architect 审查记录）的事故探针结论："两席位在同一 worktree 内重叠"、"remote 从未移动"、"git 已拒绝同分支双 worktree 检出" | 事故现场未重演；哪些提交属于哪个席位不可从 git 判定（两席共用同一邮箱身份，见现状节） |
| U：UNKNOWN | 事故中两席各自的编辑/提交/配额耗尽精确时序——git 证据只给出提交序与共用身份，席位归属缺失正是 #227（仍 OPEN） | 不据此指控任何具体席位；不补写事故叙事细节 |

## 现状机制绑定（S，全部基线 78478ed）

**写者拓扑（本设计的对象）**：工程席位无 git 提交权——`senior-software-engineer.md:34`（用户全局 agent 配置）明文 "NEVER git commit, push, create branches, or touch git config"；实现归席位、git 操作归主会话（[AGENTS.md:38](../../../AGENTS.md) 委派执行指令），全部提交经主会话 closeout 路径落地（[AGENTS.md:26](../../../AGENTS.md) issue→branch→PR→merge、[AGENTS.md:89](../../../AGENTS.md) `/closeout`）。**closeout 路径是 100% 可提交面的唯一咽喉**——这决定 slice 2 的执行点只能选在纪律路径（派发 reservation 与 closeout），而不是任何席位侧的钩子。

**工作树现实（2026-11-05 实测 `git worktree list`）**：主仓挂 **24 个检出**——22 个在 `.mnemoseeding/worktrees/w<N>`（含 `w305b`/`w305c`、`w293b`、`w281read` 等派生后缀；本 PRD 自身就在该约定的 `w228` 内撰写）+ 主检出 + 1 个约定外检出（`G:/Development/MnemoSeed/marketing/plans/brand-presence-20261031-control/checkouts/mnemoseed-local`，分支 `codex/323-clear-readme`）。规则字母与现实已漂移：[AGENTS.md:94-100](../../../AGENTS.md)（用户指令 2026-08-23）prescribed `git worktree add ..<name> -b batch/<name>`，实际命名是 `.mnemoseeding/worktrees/wNNN` + `<type>/<issue>-<slug>` 分支，且合并后清理（[AGENTS.md:98-100](../../../AGENTS.md)）明显滞后——22 个树里大量已合并批次的残留。**今天不存在任何认领工件**：org HQ 无 `claims/` 目录（实测不存在）；本仓 `.mnemoseeding/ledger.json` 无 `writerClaims` 集合（实测键清单在案）；[AGENTS.md](../../../AGENTS.md) 全文无认领步骤。分支 today 唯一的占用保护是 git 自身的 worktree 分支互斥——同一分支不能检出到两个 worktree（git 工具文档行为；R 探针同结论）：它防的是**第二个 worktree**，防不了**同一 worktree 里的第二个会话**。

**事故力学（#228，2026-09-26）**：分支 `batch/221-windows-autostart` 上 `875db9b`（09-25 20:16）→ `2725ba2`（09-26 03:43）→ `4944055`/`49383d1`/`7fab36e`（09-26）线性相接（S：git log 实测）；全部提交共用一个 git 身份族（`kumhou` / `Siew Kum Hou`，同一邮箱——席位不可分辨，归因缺口即 #227）。R（issue 评论探针）：两席位在同一 worktree 内重叠约 30 分钟，一席在配额耗尽时留下未推送提交（`4944055`），另一席在其上继续提交，一次 push 把两席的提交一并发布；**remote 在事故期间从未移动**，因此"push 时分支头意外移动就拒绝"在该事故形状下根本不会触发。终局历史（`2b016fa` 10-09 把 origin/main 合入分支、`1075896` PR #222 合入 main）显示交错提交被原样发布、无重写——损害发生在 commit 时点，任何 push 时点的检查都晚了一步。

**同类事故的第二实例（git 之外）**：org `_state.md:896`（2026-10-10 并发冲突红旗）——同一工作区两个活跃 owner 重叠，另一宿主在会话进行中改写 `ledger.mjs`，主会话按共享态并发预检纪律停止一切派发/写入。既有程序性纪律是 PROTOCOL.md:253（§10 共享态并发预检，D-2026-09-02-03）：凡 runtime/config/实时数据相关工作须先预检重叠——它是**纯纪律**，无任何工件与机械；本设计把同一思想在 git 分支面上机械化。

**运行时既有原语（org HQ 插件源，仓外文件，plain path 引用）**：`org/plugins/mnemoseeding/skills/mnemoseeding/scripts/runtime/ledger.mjs:53-59` `exclusiveJson` 以 `fs.open(file, 'wx', 0o600)` 实现原子 create-or-fail（`wx` = O_CREAT|O_EXCL；`EEXIST`→false，其余→`STORE_IO`）——本设计认领文件的创建语义与它同型，作用对象改为 org HQ 的 per-branch 文件；`:214` `locked`（state 目录单写者锁）、`:1137` `reserveRun`、`:1160` `RESOURCE_OWNED` refusal 先例（slice 2 的新 refusal 码与之同型）。`schema.mjs:157-158` `case 'reservation'` 的 exact-arity 字段表（10 字段）——slice 2 的 schema 红线来源：新增记录类型，绝不动 reservation，否则旧账 `STORE_CORRUPT`。本仓 `ledger.json` 的加性集合先例（`nativeNoEffectReconciliations` 等键，实测在案）证明"新增集合、旧账不迁移"在此 ledger 上成立。

**G00 关联（只登记、不触碰）**：launch-plan-309 `accepted-v1/work-packages.json:236` `G00-ADMISSION`（kind=design，status=**BLOCKED_ADMISSION**，priority=P0，登记 issues [228, 309]，unblock 条件 = "Actual legal route and resource ownership verified; current 309 exception is not reusable"）。本 PRD 与它的边界见冻结设计 R-5 的 G00 声明。

## 冻结设计（v1）

**R-1 CLAIM 工件（per-branch O_EXCL 认领文件，org HQ）**：
- **位置**：org HQ `claims/<repo>/`（本仓即 `G:/Development/MnemoSeed/org/claims/mnemoseed-local/`；org HQ 定位承 [AGENTS.md:104-106](../../../AGENTS.md) 的 `../org/` 指针约定）。**默认解析（冻结，防 worktree 落点）**：`git rev-parse --git-common-dir`（任意 worktree 内都指向共享 `.git`）→ 取其父 = 仓库主根 → 默认 = `<仓库主根>/../org/claims/`（AGENTS.md 指针约定的机械展开）；显式禁止 `git rev-parse --show-toplevel` 作默认——batch worktree 内它返回 worktree 路径，claims 会落进仓库内部（gitignored 后不可见、per-worktree 碎片化，QA 实证）。`--claims-dir`/`MNEMOSEED_WRITER_CLAIMS_DIR` 显式覆盖优先于默认解析。认领工件**绝不放进 git 仓**（机制选型 A 的拒绝理由）。
- **命名**：`<encoded-branch>.json`。编码规则冻结：分支名的每个字节，凡不在 `[a-z0-9._-]` 内一律百分号编码（`%XX`；大写字母、`/`、`%` 本身都在内）。选这个偏严集合是为了在**大小写不敏感文件系统（Windows/NTFS）上保持单射**——`Docs/X` 与 `docs/X` 是两个合法分支，必须映射到两个文件。**十六进制位冻结为大写**（RFC 3986 §2.1 canonical 形式：`/`→`%2F`，永不 `%2f`）——百分号编码的合规实现本可自由选大小写，QA 探针证实两个都合规的编码器在含 `/` 的分支名上即已发散（覆盖本仓全部分支形态），slice 2 控制器重算文件名后会在大小写错配上静默作废每一次拒绝；钉大写 + 唯一编码函数消除这个自由度。**编码/解码各有且只有一个共享函数**（实现批：`scripts/writer_claim.py` 的 `branch_claim_filename()`/`branch_from_claim_filename()`）作为 slice 1 与 slice 2 的唯一真源——控制器与任何校验器不得重写编码逻辑，只调用它；文件名↔内容一致性校验（本条）与验收 oracle (4) 都锚在该函数上。可读性代价只落在含大写/特殊字符的分支名上；本仓实际分支约定（全小写 `<type>/<issue>-<slug>`，worktree list 实测）不受影响。文件名只是寻址便利，**内容里的 `branch` 字段逐字节原文才是权威**，两者不一致视为工件损坏。**残余（Windows 保留设备名，as-is）**：全由 `[a-z0-9._-]` 组成的分支名不经编码原样落盘，末段恰为 Windows 保留设备名（`aux`/`nul`/`con`/`com1`…）时该文件名 Windows 无法创建——O_EXCL 创建即失败，走既有失败路径（认领被拒、零副作用）；本仓分支约定（`<type>/<issue>-<slug>`）不产生此类名字，不引入名称转换。
- **内容（schema 冻结，`mnemoseed.writer-claim/1`）**：`schema`、`repo`、`branch`（原文）、`seat`（org 名册席位 slug 或 `main`；名册是动态的（PROTOCOL §3），故为自由非空字符串、不钉枚举）、`worktree`（该认领绑定的 worktree 绝对路径——派发流先认领后建树，故是"将创建"的路径）、`base_head`（认领时解析的完整 sha：分支已存在即其当前头；新分支即 `git worktree add -b` 将采用的起点提交）、`issue`（可选整数）、`claimed_at`（UTC ISO-8601）。**schema 无任何 expiry/TTL 字段**；`claimed_at` 只作审计取证，任何逻辑不得读它做过期判断。
- **创建语义**：`os.open(..., O_CREAT|O_EXCL|O_WRONLY)`（Windows 瞬态 `EPERM/EACCES/EBUSY` 有界重试 N≤5，同 B2.2 水位线先例的形状）；`EEXIST` → 认领被拒，按 R-2 记 `refused` 事件。认领失败必须**零副作用**（不建 worktree、不动分支、不写半截工件）。
- **派发流冻结**：先 `claim`（含意图 worktree 路径与 base_head），成功后才 `git worktree add`；认领失败则派发中止。认领寿命 ⊇ worktree 寿命。

**R-2 加性台账 RECORD（append-only 事件史）**：`claims/<repo>/history.jsonl`，一行一个 JSON 事件，**永不删除、永不重写**。事件三类：`claim`（O_EXCL 成功，全字段 + `seq`）、`refused`（O_EXCL `EEXIST`，记录 `attempted_by` 与当时持有者快照——**冲突本身可审计**，这正是台账存在的理由）、`release`（`released_by` + `mode: "owner"|"main"` + `reason`）。`seq` 单调：reader 取既有 `max(seq)+1`（空/缺文件从 0 起）；追加用 `O_APPEND` 单次 `os.write`（单行 < 1KB）。slice 1 的实际写者只有主会话派发/closeout 流（单写者 by construction）；多宿主并发追加的交错残余如实记为 advisory 限度（`_state.md:896` 证明多宿主同工作区是真实场景）。**损坏尾部 fail-loud**：读到非 JSON 行 → `status` 报 `HISTORY_CORRUPT`，且 `claim`/`release` 拒绝执行（审计面 fail-closed；修复是 owner 的人工动作，脚本永不截断）。首认领时创建空文件即可，无迁移。

**R-3 显式 RELEASE（唯一解除路径）**：脚本动词 `release`，两种形态，都把 `release` 事件追加进台账后删除认领文件：
- **owner 释放**（`--repo --branch --seat <slug>`）：仅当认领文件 `seat` 与调用声明一致才执行。**释放主体冻结为主会话在 closeout/acceptance 时执行**——席位无 git 权、closeout 是主会话的路径、PR 合并后的修复轮仍写分支（提前释放重开窗口）；日常收口即"合并 + 移除 worktree + release"三步，对应 [AGENTS.md:98-100](../../../AGENTS.md) 的合并后清理点。
- **main 释放**（`--repo --branch --main --reason "<stated reason>"`）：过期/弃置认领（席位崩溃、批次废弃）的唯一出口；`--reason` 非空强制、逐字入台账。**无 TTL 自动释放**（issue 评论 owner 决策 5 的机械执行）：任何久远的 `claimed_at` 都不构成解除依据，解除只认显式动作。
- 释放后同分支可立即重认领（O_EXCL 重新成功）；台账保留完整 claim→release→claim 轨迹。

**R-4 slice 1 = 程序性（advisory），能力声明精确钉死**：slice 1 交付 = R-1/R-2/R-3 工件 + `scripts/writer_claim.py`（动词 `claim`/`status`/`release`）+ [AGENTS.md](../../../AGENTS.md) 一条认领流程 bullet（入派发与 closeout 清单）+ 测试。它**只使冲突可见**：双 claim 在文件系统层被拒且被记录；`status` 随时可读"哪个分支被谁在哪个 worktree 认领、base_head 是什么"；它**不宣称任何预防**——未认领照常工作、提交照常落地、push 照常发生，没有 git 钩子、没有 CI 门、没有任何东西拒绝什么。issue 评论对 slice 1 的诚实措辞（"procedural/scripted single-writer"）原样继承。**切片对齐（防误读）**：本文的 "slice 1" = issue #228 评论切片表的 **S1b**（认领/台账/释放机制）一项；S1a（`Seat:` trailer 约定 + CI 形状检查）属 #227（席位归因）设计，S1c/S3（CI 必检与归属交叉核对）各自交叉引用——R-4 的"没有 CI 门"措辞只描述 S1b 的机械，不否定也不描述 S1a/S1c 的 CI 面。

**R-5 slice 2 = fail-closed 控制器（独立未来批，本 PRD 只冻结边界）**：
- **拒绝点两处，都在纪律路径**：(a) workflow 运行时 `reserveRun`（ledger.mjs:1137）在目标 `(repo, branch)` 持有他席认领时返回新 refusal 码 `WRITER_CLAIM_HELD`（与 `RESOURCE_OWNED`（:1160）同型——拒绝的是派发，即"写编排"的起点）；(b) closeout 从"报告"翻转为"拒绝"：分支无认领、或认领 `seat` 既非本批席位也非 `main`（主代理 ≤3 行小修随批走，不触发拒绝——owner 决策 4 的形状）→ 非零退出，**不 commit、不 push**。这就是"refuses writes/pushes"的落地形状：在主会话自己的编排路径上 fail-closed，而不是在被约束方可以绕过的点上装门。
- **红线（slice 2 实现批的边界，逐条冻结）**：不改 R-1/R-2 工件格式与语义；不引入 TTL/liveness 探测——"他席活跃认领"的定义就是认领文件在场且 `seat` ≠ 调用方，崩溃恢复走 R-3 main 释放（owner 决策 5 接受的代价）；不用 git 钩子作门（`--no-verify`/`core.hooksPath` 可绕过——被约束方能绕过的执行是演戏，且钩子是 per-clone 未版本化宿主状态）；schema 上**新增 `writer_claim` 记录类型 + backfill `writerClaims` 集合**（加性，旧 ledger 原样加载），绝不动 `schema.mjs:157-158` reservation 的 exact-arity；不做 push 时 head-moved 拒绝（机制选型 C）。`base_head` 在 slice 2 的角色是取证锚（对比认领时与收口时的分支状态，供人审与归属交叉核对），不是自动拒绝条件——合法批次本就会移动分支头，"意外移动"无法从 sha 差异机械推导。
- **G00 声明**：本 PRD（含 slice 2 的纸面边界）**不释放 `G00-ADMISSION` 的历史 unknown**（work-packages.json:236，status=BLOCKED_ADMISSION；其登记关联 issue #228 不改变这一点）。slice 2 的实现派发本身仍受全局 admission 门约束，不因本设计获得任何通行；该单元按原样保持 BLOCKED_ADMISSION，其 unblock 条件（实际合法 route 与资源所有权验证）不因本文满足一分。

**有界性/残余限（如实钉位，本批不改）**：(1) 认领文件是**无鉴权本地状态**——任何进程都能写/删它；本卫兵针对的是**意外碰撞**（事故的等级），不是伪造；归属完整性属 prior art S3（trailer × ledger `authorSeatIds`/`selection.agent` 交叉核对，schema.mjs:144/:175-176，另一批）。(2) 单机注册表——org HQ 无远端，跨机并发不在模型内（本地-first，issue 评论 "not built" 清单）。(3) slice 1 的可见性依赖**自愿使用**：绕过者不留痕，AGENTS.md bullet 是纪律不是机制。(4) Windows `O_APPEND` 多写者交错理论上可能——advisory slice、单写者 by construction 的现实下接受，R-2 的 fail-loud 损坏检测兜底。

**机制选型（备选对照，定案 D）**：
- **A. 仓库内 per-branch lock 文件**：双输——纳入版本控制则 org 机制进产品 diff（每次锁操作都是一次提交噪声）；不纳入则对其他 worktree/clone 不可见，直到 push 为时已晚。弃（承 issue 评论裁决 2）。
- **B. 纯工作树注册表（worktree→owner 登记）**：`git worktree list` 已机械完备（现状节实测 24 检出）但不记 owner/意图，且对事故形状——**同一 worktree 里的第二个会话**——结构性失明：worktree 登记得再全也看不见第二个会话。作为主机制弃；worktree 路径降为 claim 的一个字段，保留其可见性价值。
- **C. push 时 head-moved 拒绝**：git 已拒 non-fast-forward；事故中 remote 从未移动（R）——交错发生在本地 commit 时点，push 门看得见的一切都晚了一步。作为 push 门弃；其漂移检测价值折入 `base_head` 取证字段。
- **D（定案）**：(repo, branch) 认领前置（org HQ per-branch O_EXCL）+ 加性台账 + 显式释放；slice 2 在纪律路径 fail-closed。理由：与写者拓扑咽喉点对齐；工件在仓外，产品仓零污染、零迁移；回滚 = 删 `claims/mnemoseed-local/` 目录 + 撤一条 AGENTS.md bullet；O_EXCL 原语仓库运行时已有同型先例（`exclusiveJson`）。

### owner 可见解释登记（对既有裁决的三处解释，owner 可见性优先、逐条可否决）

1. **认领目录形状偏离 S1b 字面**：#228 评论 S1b 字面为 org HQ `claims/<repo>.json`（每仓单文件注册表）；本冻结改为 per-branch `claims/<repo>/<encoded-branch>.json` + 独立 `history.jsonl`。理由：O_EXCL create-or-fail 需要每分支一个原子创建对象——单文件要么把整仓串行化（一文件一锁，无关分支互相挡），要么引入 read-modify-write + 自身锁（复刻 `locked`，违背 issue 评论 "not built" 清单的"无锁服务/无常驻进程"）；per-branch 文件做到分支内原子互斥、分支间零协调，正是并行批次的形状。
2. **释放主体选 main-at-closeout**：release 主体存在"owning seat at closeout"与"main at acceptance"两种候选；冻结为**主会话在 closeout/acceptance 时释放**。理由：席位无 git 权（`senior-software-engineer.md:34`），closeout 是主会话的路径，且 PR 合并后的修复轮仍写分支——席位在自家收口时提前释放会重开冲突窗口；认领寿命 ⊇ worktree 寿命，与 [AGENTS.md:98-100](../../../AGENTS.md) 的合并后清理同点。
3. **≤3 行小修豁免的标签形态**：R-5(b) 拒绝条件放行 `claim.seat == "main"` 与调用方 batch 席位匹配两种形态——这是 issue 评论 owner 决策 4（多席位同一 range：报告不阻断，合法批含主代理 ≤3 行小修）的机械执行；"≤3 行"本身是纪律约束（PROTOCOL.md:27），认领工件不度量行数，此外无第三种豁免形态。

三处均是对既有裁决的解释/字面偏离登记，不构成新裁决；owner 任一条可否决，否决即回到对应 prior art 字面重新冻结。

## 验收记录（slice-1 实现批）

**写面提案（全部、且仅这些）**：新文件 `scripts/writer_claim.py`（stdlib-only；`uv run python scripts/writer_claim.py {claim,status,release} …`；认领目录默认按 R-1 位置节的 common-dir 规则解析（`<仓库主根>/../org/claims/`），`--claims-dir` 参数与 `MNEMOSEED_WRITER_CLAIMS_DIR` 环境变量可覆盖）；新测试 `tests/test_writer_claim.py`；[AGENTS.md](../../../AGENTS.md) 追加一条认领流程 bullet。**机械证明**：`git diff --stat src/` 为空（纯流程工具，产品代码零改动）；任何 claim/release 操作前后 `git status --porcelain` 对仓库保持干净（工件只在 org HQ）。

**实现批必须证明（oracles）**：(1) **O_EXCL 双认领失败**：同 `(repo, branch)` 第二次 `claim` 得到定义错误（EEXIST→REFUSED），原认领文件逐字节不变，台账追加 `refused` 事件且持有者快照正确；(2) **台账追加单调**：N 次事件后文件恰 N 行、`seq` 严格递增无重复，任一操作前后快照 diff = 纯追加行（无删除/改写）；**无 TTL 行为钉**：`claimed_at` 设为任意久远过去的认领依然拒绝新 claim；(3) **释放后重认领**：owner `release` 后认领文件消失、同分支新 `claim` 成功，台账呈现 claim→release→claim 有序三事件；main 释放缺非空 `--reason` 时非零退出；既非 owner 也非 main 的 `release` 被拒；(4) **编码单射（经唯一共享函数）**：仅大小写不同的两个分支、含 `/` 与含 `%` 的分支映射到不同文件名，十六进制位恒为大写（`%2F` 形），内容 `branch` 逐字节原文；文件名与内容不一致报工件损坏；(5) **认领内容 schema**：必填字段非空、`base_head` 等于认领时 `git rev-parse` 结果、无任何 expiry 形字段；(6) **测试隔离**：全部测试经 `--claims-dir`/env 指向 tmp（[AGENTS.md:76](../../../AGENTS.md) 2026-08-31 指令），从不触碰真实 org HQ；(7) **损坏尾部 fail-loud**：手造一行非 JSON 尾行后 `status` 报 `HISTORY_CORRUPT`、`claim`/`release` 非零退出且不追加；(8) 全量 gates（pytest/ruff/format/mypy）绿；(9) **默认解析跨 worktree 一致**：在 batch worktree（`.mnemoseeding/worktrees/wNNN`）内不带 `--claims-dir` 运行，默认认领目录与主检出的解析结果逐字节相同（= `<仓库主根>/../org/claims/`，经 `--git-common-dir` 推导），且绝不落在调用方 worktree 的 toplevel 之内；测试在 tmp 自建 git 布局（主仓 + worktree + 姊妹 `org/` 目录）上验证，不触碰真实 org HQ（承接 oracle 6）。**保持 open**：slice 2 的任何实现（reserveRun refusal、closeout 翻转、ledger `writerClaims` 集合）不在本批——必须独立批立项，且立项时同时受 R-5 红线与 G00 admission 门约束。

## 理论锚

**无新增借用理论**。本 PRD 是多席位协作的流程/文件系统机制（互斥创建、追加式审计日志、显式释放协议）：不触及记忆机制（capture/recall/decay/reinforce/注入），不改变任何面向用户的行为，无记忆规律可借。O_EXCL 原子性、append-only 日志、认领-释放协议是 OS/工程机制，按纪律不入本节。

### 不借用清单

- 全部记忆类规律（间隔重复/遗忘曲线、提取练习、前瞻记忆 R26、干扰论 R29、ACT-R 衰减 R23、序列位置 R24）——不适用：本设计无记忆行为对象，引用任何一条都属范围伪造。
- 流行神经（学习风格、左右脑、10-20-30）——不借用：同上，且本不入选。
- 分布式共识/租约理论（lease、quorum、fencing token）——不借用为理论锚：认领文件是单机文件系统机制，且**故意不引入租约**——无 TTL 是冻结决策，租约的续约-过期模型与之相反；引用它们会暗示错误的形式化程度。
- 可用性/安全启发式（如"最小惊讶"）——不借用：属工程惯例，非实证规律。
