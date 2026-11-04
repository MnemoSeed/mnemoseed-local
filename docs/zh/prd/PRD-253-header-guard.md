# PRD-253：文本调用方凭据卫兵——头参精度修订（设计冻结）

日期：2026-11-03。状态：设计交付（H253-HEADERS-DESIGN），待头参实现批与独立 QA。审查基线：`74f82f10447bd7d9e1663a794c824c1ac82c3d27`。范围为**仅设计**：本批不写代码、不改任何行为；实现批次写面冻结见专门小节。定性：**有界 tripwire 精度修订**——只收紧仓库内调用方扫描的呈现判定，**daemon 凭据边界不变**（见下节末）。承接 #251 closeout QA 的非阻塞精度限发现（[issue #253](https://github.com/MnemoSeed/mnemoseed-local/issues/253)）。

## 证据与结论边界

| 标记 | 来源与能够支持的结论 | 不能支持的结论 |
| --- | --- | --- |
| S：源码检查 + 字符串级验证 | 本文件全部 file:line 绑定来自基线 `74f82f1` 的实际代码，本人已逐点读过；另以字符串级函数调用（`uv run python -I` 导入测试模块，未写任何文件）验证下述复现形在今日扫描下的行为 | 未起 daemon、未跑 pytest 套件（设计批红线）；未改任何代码 |
| R：历史作者报告 | #251 closeout QA 的发现（issue 正文）：`curl -d 'Authorization: Bearer x'` 类体参数可满足语句级凭据检查 | #251 的发现过程未重演；仅采用其结论形状，#251 的冻结验收口径不因此追溯改写 |
| U：UNKNOWN | 复现形是否存在于任何真实 in-repo caller（见现状节：全部 text caller 现为 OPEN_SET）——今日为假想形状，夹具属预防性钉位 | 不据此外推"某现有脚本有洞"；不补写 PASS/FAIL |

## 现状机制绑定（S，全部基线 74f82f1）

**卫兵的位置**：整个卫兵（扫描器 + 注册表）就是 [tests/test_daemon_caller_credential_guard.py](../../../tests/test_daemon_caller_credential_guard.py) 这一个测试模块（`KNOWN_CALLERS` :427-447、类别常量 `CREDENTIALED`/`OPEN_SET`/`PROTECTED_WITHOUT_CREDENTIAL` :422-424、扫描函数 :482-1193）；src 侧仅提供开放路径集（`OPEN_PATHS` [auth.py:51](../../../src/mnemoseed_local/daemon/auth.py)）与 console 文档集（auth.py:70），经 `_scan`（:1149）读入。模块 docstring 是卫兵的规格面（规则 1 :14-22，limits 段 :41-46）。

**text 侧呈现判定管线**：`_scan`（:1146-1171，text 分支 :1158-1171）→ `acquires = _text_acquires_credential(source, suffix)`（:859-862）→ 语句化 `_statements`（:827-856：`_without_comments` :749-786 去注释、heredoc 体剔除 :834-842、续行拼接 :844-849、分隔符切分 `_separated` :789-818、cmd `rem`/`::` 注释语句 :821-824）→ 过滤出 dialing 语句（`_dialing_statements` :865-867，判据 `_dials_a_surface` :878-889）→ 每条 dialing 语句过 `_presents_in_statement`（:870-875）：**头标志正则 `HEADER_FLAGS`（:139）在整条语句上命中，或 `ACQUISITION_MARKERS`（:110，含 `Authorization`/`Bearer`）作为子串在整条语句上出现，二者任一即算呈现**。文件级判定 `_classify`（:1174-1180）：`hits` 非空且 `acquires` 且无 unattached → `CREDENTIALED`。

**精度限（两个方向都有，字符串级已验证）**：

1. **体参数标记误判为呈现（issue #253 复现形）**：`curl -d 'Authorization: Bearer x' http://127.0.0.1:7788/memory/remember` ——`-d` 是体通道（curl 手册；仓内闭合表 `CURL_VALUE_FLAGS` :258-307 亦把 `-d`/`--data`/`--data-raw`/`--json` 列为取值 flag），daemon 从体里读不到任何头；但标记子串在整条语句上命中 → 今日 `presents=True` → 该文件判 `CREDENTIALED`。`--data-raw '{"Authorization":…}'`、fetch `{body:"Authorization:…"}` 同理。**注意对称的第二个洞**：`curl <route> -H 'X-Track: x'`（头标志在场但头值无凭据）今日也 `presents=True`——"标志在场"本身就满足规则，与头值内容无关。
2. **`wget -H` 误开头位**：`HEADER_FLAGS` 的 `-H\b` 命中 wget 的 span-hosts 开关（wget 取值表 `WGET_VALUE_FLAGS` :308-352 只含 `--header` :335，不含 `-H`）→ `wget -H <url>` 今日 `presents=True`。

**措辞不对称（intro 措辞要修的点）**：呈现判定只读去注释后的语句（:859-862），而保守路由扫对**原样源文**计数——`_scan` :1164-1166 以未去注释的 `source` 调 `_text_protected_hits`（:537-539，正则 `_PROTECTED_TEXT` :178-180）。字符串级验证：`# see /memory/remember docs` 注释行进 `hits`，但不进语句表。即：**注释文本不能呈现凭据，注释中的路由提及仍会被保守路由扫报告**——docstring :44-46 "A mention inside a comment is not read at all" 未作此区分。

**引用点（today 正确、保持不变）**：注释/块注释/heredoc/分隔符机制（上管线）；目标位判定（`_target_operand` :1053-1068、`_shell_target` :1013-1037、`_powershell_target` :1040-1050、可读性 `_target_is_readable` :1098-1113、绑定 `_target_bindings` :1071-1095、函数内位置 `_function_statements` :1122-1131、fail-closed `_text_unreadable` :1134-1143）；下载器提及既有判据（引号掩蔽 `_mask_quoted` :907-932、存在性探针 `_AVAILABILITY_PROBE` :250-254、`_shell_dial` :1003-1010）。Python 侧（AST）已有精确头位判定可作先例：`_presents_in_headers` :710-716、`_header_entries` :672-680、体搭载反例 `_BODY_BORNE_CALLER` 测试 :1355-1363、头位形状表 `_HEADER_POSITION_SHAPES` :1698-1725（含 opaque passthrough 呈现 :1705 与"不可读解包算呈现"的规格语 :17-19）。

**daemon 凭据边界（不变，绑定）**：中间件门 `auth_middleware._entry`（auth.py:83-107）对非开放路径调 `_authorized`（auth.py:146-148），只读 `Authorization: Bearer …`（`_bearer_value` auth.py:161-168）或凭据 cookie（`_cookie_value` auth.py:171-179），其余一律 401（`_refuse` auth.py:127）。凭据在体里送达时 daemon 读不到——本 PRD 只修仓库内扫描器对"调用方是否把它放进了头位"的证明精度，**不改 daemon 任何一行**。

## 冻结设计（v1）

**R-1 头参识别（闭合表驱动，语言无关地施加于每条 dialing 语句）**：在去注释、切分后的语句上用既有 `_word_spans`（:935-970，引号/括号组保持整词）分词。头位只在下列词元处打开：curl `-H`/`--header`（值 = 下一词，或 `--header=` 的 `=` 后部分）；wget 仅 `--header`（**`-H` 是 span-hosts 开关，永不头位**）；PowerShell dial cmdlet 的 `-Headers`/`-Header` 参数（值 = 下一整词，括号表达式整体）；fetch 调用实参内 `headers` 键的值（键须在调用实参文本内可定位：值为对象字面量 → R-2(a) 施于其文本，值为裸名等不可读表达式 → R-2(b)；位置实参形态 `fetch(url, h)` 不开头位——fc-(iii)）。词元须精确匹配：捆绑短 flag（如 `curl -sH …`）不打开头位——fc-(ii)。多次出现逐个检查。表闭合性由 `CURL_VALUE_FLAGS`/`WGET_VALUE_FLAGS` 同源保证；未见的新头通道（如 curl `-K/--config` 配置文件可设 header）不在头位集内，如实记录为不可读通道 → fail-closed（仅经配置文件呈现的调用方会得 `PROTECTED_WITHOUT_CREDENTIAL`，今日无此类 in-repo caller）。

**R-2 呈现证据（I-1 修订：键部闸门）**：某头参呈现凭据，当且仅当：(a) 头参文本内键值结构可读时——冒号形（curl/wget 头文本与 JS 对象字面量：键部为首个 `:` 前的段）或 PS `@{…}` 形（键部为 `=` 前的段）——任一条目的键部 casefold 后被 `authorization|cookie` **fullmatch**（镜像 Python 侧 `CREDENTIAL_KEYS` :120 与 `_carries_credential_in_a_header_position` :683-696 的键闸；混合大小写正例 :1724、`X-Authorization`/`Authorization-Trace` 反例 :1709-1718），**且**该条目值部（分隔符后）原样文本含任一 `ACQUISITION_MARKERS`，或该条目值部不可读（裸变量/空，如 `Authorization: $CRED`——文本侧无缝追踪，保守计呈现，承接 `binding.ps1` 先例 :1631-1642）；键部 fullmatch 不中时值部任何标记都不呈现——`curl <route> -H 'X-Authorization: Bearer x'` 走 daemon 是 401（`_bearer_value` auth.py:161-168 只读 `authorization` 头名），与 #253 同类缺陷只窄一个 span，今日整语句子串规则下 presents=True（字符串级已验证），冻结后不得算呈现；或 (b) 头参整体不可读（裸变量、splat、空、无键值分隔符的整体词元）——按呈现计（保守方向，镜像“读不懂的解包算呈现”规格语 :17-19 与 `opaque-passthrough` 先例 :1705）。

**R-3 移除的两条旧规则**：标记子串的**整语句**命中（:874）与**头标志在场即呈现**（:873）都废除；标记在头位之外（体/输出/URL/裸文本）呈现为零。

**R-4 类别映射（注册表机制不变）**：`pin` = `CREDENTIALED`（至少一条 dialing 语句在头位呈现/头值不可读）；`allow` = `OPEN_SET`（无受保护 hits 且目标可读）；`refuse` = `PROTECTED_WITHOUT_CREDENTIAL`——**不可 pin**（registry 测试 :1205-1223 的词表 :1210-1212 只认两类；hole 测试 :1195-1202 独立于注册表直接失败）。`KNOWN_CALLERS`（:427-447）逐字节不动。

**R-5 命令位提及规则（新 dial 判据子句）**：shell dial 词（`_SHELL_DIAL_WORD` :249）算调用，仅当它处于命令位——语句首词，或环境赋值前缀（`NAME=value`）之后，或调用算子 `&`/`!`/`command`/`exec` 之后——或紧跟存在性探针（`_AVAILABILITY_PROBE` 既有判据）；否则为**提及**。字符串级验证：未加引号的 `echo curl is installed` 今日 `dials=True` 且目标不可读（假阳性 fail-closed），R-5 落地后归 `OPEN_SET`；引号内提及（`install-prose.sh` :1520-1522）、探针（:1511-1518）、heredoc 用法（:1524-1531）既有判据保持。措辞澄清随 docstring 修订落地：注释文本不能呈现凭据（R-2 只作用于去注释语句），注释/原样源文中的路由提及仍被保守路由扫计入 `hits`（:1164-1166）——两层面各量各的事。

**机制选型（备选对照，定案 C）**：A. 保留整语句标记、追加体参数黑名单——清单不闭合（`-d`/`--data*`/`--json`/`-F`/`-T`/`--form`… 需逐个追），且"-H 在场即呈现"反向洞仍在；弃。B. 弱位置规则（只看 `-H` 后一词）——主干同 R-1/R-2 但 wget `-H`、`--header=`、PS `-Headers (…)`、fetch `headers:` 各需特判，规则分裂且 `binding.ps1`（不可读头值）翻红违反保守方向；弃。C（定案）：闭合表头位 + 标记限定头参内 + 不可读头值保守呈现 + `wget -H`/命令位提及两个精度负例；复用既有词元/闭合表机械，改动面最小；采纳。

**有界性声明（registry 对头/体区分的抵抗点，如实记录、本批不改）**：(1) 文件级 `any()` 粒度——`_text_acquires_credential`（:859-862）对全部 dialing 语句取 any，text 侧无 Python 侧 per-call-site `unattached`（text 分支恒 ()，:1163-1171）：同文件一条 `-H` 呈现 + 另一条 `-d` 体标记语句 → 文件仍 `CREDENTIALED`；本批不引入 text 级 unattached（超范围），残余限如实记录。(2) 注册表只有文件级两类值，不记录"呈现发生在哪条语句"，QA 无法从注册表读出证据位置。(3) 今日 `KNOWN_CALLERS` 全部 text caller（.yml/.sh/.ps1）均为 `OPEN_SET`，`CREDENTIALED` 三项皆 .py——本修订不可能翻转任何现有注册表项，registry 测试原样全绿即回归神谕。

**fail-closed 边缘声明（如实钉位，本批不修；扩集只能以改版冻结立项）**：fc-(i) PS cmdlet 兄弟形状——未加引号的输出动词提及 `echo Invoke-RestMethod is installed` 今日即假阳性 fail-closed（字符串级验证：dials=True 且目标不可读；`_PS_DIAL` :242 无命令位/探针判据），R-5 本批只冻结 shell dial 词，PS 侧保持原状；fc-(ii) 捆绑短 flag——`curl -sH 'Authorization: Bearer x'` 的 `-sH` 为单一词元（`_word_spans` :935-970 已验证），R-1 精确匹配不开头位，标记躺在扫描未打开的头参里 → 不得呈现；今日整语句规则下 presents=True，冻结后对真实呈现的调用方是 fail-closed 洞报——精度换覆盖的已声明代价；fc-(iii) JS 位置实参头集——`fetch(url, h)`（调用文本无 `headers` 键）今日与冻结后同判 PROTECTED_WITHOUT_CREDENTIAL（字符串级验证 presents=False），对照 `fetch(url, {headers: h})`（键在、值为裸名）经 R-2(b) 保守计呈现，两形分流如实声明。

## 用例冻结（每行 = 输入形状 → 期望判定 → 夹具）

| # | 输入形状 | 期望判定 | 夹具 |
| --- | --- | --- | --- |
| 1 | curl `-H 'Authorization: Bearer $CREDENTIAL'` + daemon 路由，URL 引号内含分隔符 | `CREDENTIALED` | 既有 `quoted-separator.sh`（:1835-1838） |
| 2 | 同形 plain 变体：curl `-H 'Authorization: Bearer x'` + 字面路由，无引号内分隔符 | `CREDENTIALED` | 既有 `quoted-block.sh`（:1840-1843） |
| 3 | curl 续行语句带 `-H 'Authorization: Bearer …'` | `CREDENTIALED` | 既有 `continued.sh`（:1645-1654） |
| 4 | PS `-Headers (credential_headers $Credential)` | `CREDENTIALED` | 既有 `presenting.ps1`（:1619-1628） |
| 5 | PS 先绑定后引用 `-Headers $headers`（值本语句不可读） | `CREDENTIALED` | 既有 `binding.ps1`（:1631-1642）——钉 R-2(b) |
| 6 | fetch `{headers:{Authorization:"Bearer x"}}` | `CREDENTIALED` | 既有 `presenting-block.js`（:1845-1848） |
| 7 | wget `--header='Authorization: Bearer x'` + daemon 路由 | `CREDENTIALED` | 新增 |
| 8 | curl `-H 'aUtHoRiZaTiOn: Bearer x'`（键部 fullmatch 大小写不敏感） | `CREDENTIALED` | 新增（钉 R-2(a)；镜像 :1724） |
| 9 | `curl -d 'Authorization: Bearer x' http://127.0.0.1:7788/memory/remember` | `PROTECTED_WITHOUT_CREDENTIAL` | 新增（#253 复现形；TDD 先红后绿） |
| 10 | `curl <route> -d 'Authorization: Bearer x'`（flag 在 target 后） | 同上 | 新增（次序无关钉） |
| 11 | `curl --data-raw '{"Authorization":"Bearer x"}' <route>` | 同上 | 新增 |
| 12 | `curl --json '{"Authorization":"Bearer x"}' <route>` | 同上 | 新增（体通道） |
| 13 | `Invoke-RestMethod -Uri "$DaemonUrl/memory/remember" -Body 'Authorization: Bearer x'` | 同上 | 新增（PS 体参数） |

| 14 | `fetch(<daemon url>, {method:"POST", body:"Authorization: Bearer x"})` | 同上 | 新增（JS 体键） |
| 15 | `curl <route> -H 'X-Track: x'`（头标志在场、头值无凭据） | 同上 | 新增（钉 R-3） |
| 16 | `wget -H <daemon 路由>`（span-hosts 开关） | 同上 | 新增（钉 R-1 wget 例外） |
| 17 | `curl <route> -H 'X-Authorization: Bearer x'`（键部不可 fullmatch） | 同上 | 新增（钉 R-2(a) 键闸；镜像 Python `wrong-key-x-authorization` :1709-1713） |
| 18 | 行/块注释含标记（js/ts/mjs/ps1 六形；bat/cmd `rem`/`::`/拼接四形） | 同上 | 既有 `_TEXT_STATEMENT_SHAPES` 注释行（:1762-1812）+ `commented.ps1`/`trailing.ps1`（:1657-1676） |
| 19 | 标记在同线他段（`;`/`&&`/`\|` 后的 echo/Write-Host/管道） | 同上 | 既有 `separated.sh`/`separated.ps1`/`and-separated.sh`/`pipeline.ps1`（:1814-1833） |
| 20 | 注释内路由提及（如 `# see /memory/remember`） | `PROTECTED_WITHOUT_CREDENTIAL` 且 `hits` 非空（保守路由扫） | 新增（钉 R-5 措辞） |
| 21 | 目标藏在非目标位（输出/头值/头体/并排第二调/整目标隐藏） | `unreadable` → `PROTECTED_WITHOUT_CREDENTIAL` | 既有 `_HIDDEN_TARGET_BY_POSITION`（:1434-1451）+ `_HIDDEN_TARGET_SHAPES`（:1411-1414）+ 函数签名形（:1470-1488） |
| 22 | 引号内提及 `die "neither curl nor wget…"` | `OPEN_SET` | 既有 `install-prose.sh`（:1520-1522） |
| 23 | 存在性探针 `have curl` / `Test-CommandExists 'curl.exe'` | `OPEN_SET` | 既有 `install-probe-shell.sh`/`install-probe.ps1`（:1511-1518） |
| 24 | heredoc 用法文本含 curl | `OPEN_SET` | 既有 `install-heredoc.sh`（:1524-1531） |
| 25 | 未加引号输出动词提及 `echo curl is installed` | `OPEN_SET` | 新增（钉 R-5 命令位） |
| 26 | 其余 in-repo 安装调用形（`-fsSL -o`、scriptblock 转发、字面目标、续行探针） | `OPEN_SET` | 既有 `_SHIPPED_INVOCATION_SHAPES`（:1495-1544） |

**不变量（性质声明，非输入行）**：呈现修订不得改变目标可读性判定——行 21 全族及全部既有目标位夹具的判定须与基线逐字节一致（机械口径见验收记录 (2)）。

行 9/10 即 issue 复现形回归夹具；行 15/16 为对称反向洞与 wget 例外钉位；行 17 为 I-1 键闸钉位。**如实声明**：今日无 in-repo caller 落入行 7-17、20、25 形状（注册表全 text 为 `OPEN_SET`），这些行是预防性钉位，不构成任何现有文件的缺陷指控。

## 验收记录

**实现批必须证明**：(1) 用例表 26 行逐行有夹具且全绿，新增行（7-17、20、25）落在同一测试模块的 parametrize 结构内；(2) 既有全部形状表（`_TEXT_STATEMENT_SHAPES`、`_HIDDEN_TARGET_*`、`_SHIPPED_INVOCATION_SHAPES`、`_HEADER_POSITION_SHAPES`、root 表）判定与基线逐字节一致，上表不变量成立；(3) `test_known_caller_registry_matches_the_tree`（:1205-1223）与 hole 测试（:1195-1202）在 `KNOWN_CALLERS` 未动的前提下全绿；(4) `git diff --stat src/` 为空且 `tests/test_daemon_credential_boundary.py` 全绿（daemon 凭据边界不变的机械证明）；(5) 行 9 复现形先红后绿（TDD：修订前确实判 `CREDENTIALED`、修订后翻 `PROTECTED_WITHOUT_CREDENTIAL`），行 17 键闸钉同口径先红后绿；(6) 全量 gates（pytest/ruff/format/mypy）绿。**保持 open**：text 级 per-statement `unattached`（本批有意不做的有界性决定，见冻结设计节）；头通道扩集（如 `-K/--config` 配置文件通道、fc-(ii) 捆绑短 flag 解析）只能以修订本冻结的方式立项，不得在实现批内即兴。

## 理论锚

**无新增借用理论**。本 PRD 是仓库内静态扫描器（tripwire 工具）的判定精度修订：不触及记忆机制（capture/recall/decay/reinforce/注入），不改变任何面向用户的行为，无记忆规律可借。机制层（闭合 flag 表、词元扫描、保守方向规则、fail-closed）是工程机制，按纪律不入本节。

### 不借用清单

- 全部记忆类规律（间隔重复/遗忘曲线、提取练习、前瞻记忆 R26、意图 superiority TA-14、干扰论 R29、ACT-R 衰减 R23、序列位置 R24）——不适用：本设计无记忆行为对象，引用任何一条都属范围伪造。
- 流行神经（学习风格、左右脑、10-20-30）——不借用：同上，且本不入选。
- 可用性/安全启发式（如"最小惊讶"）——不借用为理论锚：属工程惯例，非实证规律。
