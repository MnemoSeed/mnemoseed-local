# 设计 390-legacy-dream-route v3

> 状态: DRAFT / DESIGN_PENDING(待独立设计 QA CLOSABLE)。本文件不授权工程、不冻结。
> 关联: PRD-390 brief v3;slices v3;issue #390(OPEN)。基线 main 8ad2d6ace54c1fca467796cc6ddfcbf596318168(本席实测 HEAD=origin/main=8ad2d6a;PR #432 仍 OPEN,mergeable)。
> 本版针对 D1 round-2 verdict(sha 9e0760cc…a76d7)重开 BLOCKER B-1 给出机制级修复:迁移路径改为进程内本地批量写,不依赖已启动 daemon、不走 HTTP;并逐条重新关闭 I-1..I-4 / N-1。声明模型 opencode/hy4-flash-free,物理身份未自证。

## 0. 绑定 owner 裁定(不重新辩论)
- W1-C: owner 接受「统一迁移行」作为 Done-when #3「一次写即恢复 boot」的澄清,但明确该行必须在不能 boot 的 home 上真正可执行。本 v3 据此把执行路径由 HTTP config set 改为进程内 ConfigWriteService.set_many 本地写;不放宽任何 AC、不引入默认模型常量、不静默改配置。
- W1-A: owner 接受字面 per-item marker 跨层写集(fence/recall/t2/plugin.ts + golden 重钉),本轮不再重开 option (a);A-I1/A-I2 作为实施期 IMPORTANT 门记入 wave01-v3-carryforward-gates.md。

## 1. Steelman 核心决定
升级后曾启用 dream 的旧 home 因 [dream.llm.<role>] 表 driver/model 任一缺失而在 load 阶段被拒:config.py:788-791 对任一缺失抛同一个 ConfigError(只有 key + message,无形状位,message 见 config.py:55)。因此形状检测继续用 key 前缀 + message 的统一判定(不伪造 driver/model 存在位,关闭 D1 B-1 的形状侧)。但 round-2 证明 v2 执行的迁移命令不可行:config set 是 HTTP POST(cli.py:1630;rest_client.py:113-120),而 daemon 只能经严格 load_config() 启动(app.py:1109),在坏 home 上 daemon 根本不存在,会抛 DaemonUnavailableError;且即便 daemon 在内存里已完整,单键 set 的 _cross_validate_role_route(service.py:254-264,注册 :538-539,调用 :1176-1180)在半路由上拒绝首写(round-2 实测:both-missing 两写全拒、driver-only 拒 driver 写、model-only 拒 model 写)。

v3 的机制决定:新增一个显式 CLI 动词做进程内本地批量写,在一次调用内用 PR #432 的 set_many 同时落 driver + model,绕开 daemon 与 HTTP。用户显式执行;不自动执行;不静默变更。

## 2. 迁移机制(逐层可证伪)

### 2.1 检测(保留统一识别,不读 TOML 猜形状)
共享 helper _legacy_route_migration_hint(exc) -> str | None:exc.key 以 dream.llm. 开头且 ROLE_ROUTE_REQUIRED_MESSAGE 在 str(exc) 中才命中;命中则打印一条迁移命令与简短说明;其它 ConfigError 原样抛出。消息只作附带说明,不再只吐裸消息。

### 2.2 迁移命令(shell-safe 占位符,一行)
    mnemoseed-local config repair-dream-route --role dream --driver YOUR_DRIVER --model YOUR_MODEL_ID
占位符纯字母数字,POSIX / PowerShell 5.1 均无重定向/保留语义;不出现尖括号(关闭 I-1)。附提示:值从 console provider catalog / detect-local 选,例 --driver openai --model gpt-4o-mini(providers.py:30/:38);示例仅为提示,命令不会替用户填值。这是 owner 接受的「统一迁移行」在 v3 的可执行化身:一条 shell 行恢复 boot,但作用在不能 boot 的 home 上。

### 2.3 进程内本地写
cmd_repair_dream_route 全部在 CLI 进程内完成,不启动/连接 daemon,不调用 rest_client:
1. 读真实 config.toml(路径 = CONFIG_PATH,即 config.py:597 的 path or CONFIG_PATH);用与 load 同款 tomllib.loads 取 raw(config.py:602)。
2. 在临时文件里构造 sanitized 副本:只把目标 role 的残缺 [dream.llm.<role>] 表整表删除(不是填默认值);依据 config.py:784-785「an unconfigured role carries no route」缺表是 load-valid。其余 raw 原样保留。若 raw 中还有其它残缺 role 表,则 load_config(temp) 会为那个 role 抛同一个 ConfigError,命令原样中止、零写入、并指名该 role。
3. cfg = load_config(temp):preset / baseurl / storage 在 dream 校验(config.py:657 起,判定 :788-791)之前已按真实文件解析(config.py:604-655),live_home_dir() 绑定基于环境,路径正确;load_config 回写 source=path、raw=raw(config.py:939-940)。
4. cfg.source = real_config_path:关键点是 ConfigWriteService._config_path() 用 self._config.source(service.py:1375-1377),必须把写目标指回真实 config.toml,否则会写进临时文件。
5. stores = build_stores(cfg)(factory.py:85-109,只用 preset/storage,不依赖 dream 路由);svc = ConfigWriteService(cfg, stores 的 meta store),与 cli.py:333/343 完全相同的既有单一写者构造模式,不新建 store API。
6. svc.set_many 传入两元组:(dream.llm.<role>.driver, driver) 与 (dream.llm.<role>.model, model),actor=migration。PR #432 的 set_many(branch 实测 service.py:1209-1258)先 deepcopy(self._config)(:1229),把两键全部 spec.apply 到 simulated(:1239),再对 simulated 跑 cross_validate(:1244)与 _assert_reload_valid(:1247),最后才 _land(:1250)。因此 _cross_validate_role_route(branch :256-266)看到完整路由而通过;_assert_reload_valid(branch :1016-1035)读 raw 的 dream/llm 子表,_role_apply(branch :491-511,:509-511)已把两值镜像进 raw 而通过。_land(branch :1260-1307)经 _patch_toml(service.py:950-997)直接改真实文件并写 meta DB。
7. asyncio.run(stores.close())(同 cli.py:345)。写后自检 load_config(real) 成功;失败则显式报错(覆盖 #444 中途写失败窗口:两键同表、值是简单非空串,窗口窄但不声称零)。
### 2.4 失败语义
- 非目标形状 ConfigError(坏 preset / 未知存储层 / 坏值 / TOML 解析失败):步骤 2-3 中止,零写入,原样报错。
- set_many 校验失败(如另一 role 残缺、未知键):ConfigWriteError,零 patch(校验在所有 _land 之前)。
- daemon 已在跑的完整 home:无残缺路由可修;若仍执行,本地写不会热应用到在跑 daemon 的内存 Config,须按 restart_required 提示重启,如实登记,不声称热更新。

## 3. 合同 / AC
- AC1 命中形状时打印一条 shell-safe 迁移命令(repair-dream-route + 占位符),不再只输出裸 ROLE_ROUTE_REQUIRED_MESSAGE。
- AC2 迁移经进程内 ConfigWriteService.set_many 本地写 config.toml + meta DB;不启动/连接 daemon、不调用 HTTP config-set 路径、不直接改 DB 行、不新建 store API。对迁移 helper 不引用 rest_client 或 HTTP config-set 调用加 grep/单测断言。
- AC3 不静默变更:检测到形状时零写入(隔离 HOME 全树字节 + generation 前后相等);只有显式执行 repair 动词才写(关闭 I-4)。
- AC4 冻结 fixture 三形状:legacy_driver_only.toml / legacy_model_only.toml / legacy_both_missing.toml(均含合法 storage 段)。三者经同一条 repair 命令 + 具体值(openai / gpt-4o-mini)后 load_config 成功、up 在 spare port boot 成功。
- AC5 隔离测试 tests/test_legacy_dream_route.py:临时 HOME + spare port;每形状 → 命令 → 断言无 daemon 监听/无进程(证明 daemon-independent)→ 执行 → config.toml 两键字节断言 + meta DB 两记录断言 → boot 成功;覆盖 up/on/doctor 的提示路径。不碰 7788。
- AC6 其它 ConfigError 不被误吞:坏值/未知键/其它 message 走原路径。
- AC7 不改 config.py message 与 load 判定;不编辑 configwrite/service.py(除 #432 已合字节外);不引入默认模型常量(repair 只接受显式 --driver/--model)。
- AC8 所有用户可达 load 调用点经同一共享 helper _legacy_route_migration_hint:cli.py:274/:325/:656 及 up/on/doctor;cli.py:2194/:2366 两处吞 ConfigError 并返回默认值的站点不是错误展示点,不接 helper(round-2 I-3 caveat 已登记)。

## 4. 理论锚
无新增借用。纯配置迁移 UX 与错误语义工程;不借用 default effect / 自愈隐喻 / Nudge。迁移命令是显式用户动作。

## 5. Denylist
不静默改用户配置、不自动执行 repair、不写默认值进配置、不直接改 settings DB 行、不走 HTTP config-set、不改 config.py load 语义、不改 #432 service.py 写集、不引入模型调用、不吞其它 ConfigError、不改端口/真实 home。
## 6. 基线哈希与重基线门(显式)
本席实测 HEAD=origin/main=8ad2d6a;PR #432 仍 OPEN(gh pr view 432: state OPEN / mergeable MERGEABLE / head fix/385-console-role-batch)。因此 v3 显式重基线,不把 pre-merge 数字当常量:
- config.py 86b2eaec507359b2e477cd98ec2107265342d8e87c7b9700f22554f34a07ac91(53686 B;message :55;判定 :788-791)
- cli.py a59efc0e5ed2e3285a103060be9dfd1c3ee9decbf025d2eab0f388eca158142c(113351 B;load 点 :192/255/324/580/654/2194/2366;catch :274/:325/:656;HTTP config-set :1630)
- configwrite/service.py(本席从 origin/fix/385-console-role-batch 读取的候选字节,非 main):set_many :1209-1258 / _land :1260-1307 / _assert_reload_valid :1016-1035 / cross_validate :256-266 / 注册键 :540-541。ELIGIBILITY 门 G0:#432 合并后须在合并 HEAD 重取 service.py 字节并复算哈希;若 set_many 的签名/语义(_assert_reload_valid、批量 apply-then-validate、任意顺序 role pair)与此处不符,本 v3 机制须重新核验后才可进入实施。对 service.py 不做常量哈希断言,以对合并 HEAD 的 git diff -- src/mnemoseed_local/configwrite/service.py 为空为准。

## 7. 验证命令(精确、可执行)
    uv run pytest -q tests/test_legacy_dream_route.py tests/test_config.py tests/test_cli.py tests/test_configwrite_service.py
    uv run pytest -q
    ruff check; ruff format --check; mypy src
    git diff -- src/mnemoseed_local/config.py src/mnemoseed_local/configwrite/service.py
    grep -n 'load_config()' src/mnemoseed_local/cli.py
    grep -n 'rest_client\|config/set' src/mnemoseed_local/cli.py
测试隔离:临时 MNEMOSEED_LOCAL_HOME + spare port,不碰已装运行时 7788。

## 8. D1 round-2 关闭台账(重新映射)
| ID | round-2 状态 | v3 关闭方式 |
| B-1 | REOPENED,现关闭 | 执行路径改进程内本地写:2.3 步骤 1-7 完全不经 daemon/HTTP;set_many 批量 apply-then-validate 让 both-missing / driver-only / model-only 都能一次落两键;AC4/AC5 三形状 + 无 daemon 断言 + 合并后 G0 重基线门 |
| I-1 | 已关(机制) | 占位符改 YOUR_DRIVER / YOUR_MODEL_ID 纯字母数字;新命令单行;oracle 用具体 gpt-4o-mini 验 boot |
| I-2 | 已关 | section 6 显式重基线;G0 门 + 对合并 HEAD 的 git diff,不做 service.py 常量哈希断言 |
| I-3 | 已关(caveat) | AC8 共享 helper 覆盖 :274/:325/:656 + up/on/doctor;:2194/:2366 吞错站点登记为非展示点 |
| I-4 | 已关 | AC3 隔离 HOME 全树逐字节快照;检测阶段零写入与显式执行阶段写入分离 |
| N-1 | 已关 | AC5 命名具体值 openai / gpt-4o-mini(providers.py:30/:38) |
## 9. 选项考虑(显著决策的取舍)
- 选项 A(v3 采用):进程内 set_many 本地写 + 新显式 CLI 动词。赢:在不能 boot 的 home 上真正可执行,绕开 daemon/HTTP;复用既有单一写者与 #432 已审 set_many,不新建 store API;三形状一次覆盖。代价:新增一个 CLI 动词(非写接口/非 store API),引入 sanitize 临时文件 + 改 cfg.source 的薄适配层;对在跑 daemon 不热更新(已登记)。
- 选项 B:保留 v2 的 HTTP config set 行,再加先手动起 daemon 前置。赢:零新代码。代价:round-2 已证伪,起 daemon 本身需 load 成功,在坏 home 上循环依赖,不可行,弃。
- 选项 C:两键分别用两次单键 set()(不经 set_many)。赢:不依赖 #432。代价:round-2 实测 cross_validate 在半路由上拒首写(both-missing 两写全拒),且无 reload-valid 批量校验;不可行,弃。
- 选项 D:把默认模型常量写进 load 契约以接受半路由。赢:一条 config set 即可。代价:owner 红线禁止默认模型常量,且需改 config.py/#432 写集;弃。
- 推荐:A。唯一同时满足非 boot home 可执行、复用既有写者、不放宽红线的机制。
## 10. 盲点 / 失败模式 / 扩展性
- set_many 的 _land 不是全有全无(#444 follow-up):两键同表、简单非空串,失败窗口窄;写后 load_config 自检兜底并显式报错,不声称零窗口。
- 多残缺 role:2.3 步骤 2 只 sanitize 目标 role,其它残缺 role 由 load 抛错中止,诚实但要求用户逐 role 修复;oracle 须覆盖。
- raw 解析与 mirror 一致:临时文件用 tomllib(same as load),cfg.source 指回真实文件,确保 _patch_toml 改的是用户配置而非临时副本;此项为实施期门(断言写后真实 config.toml 字节变化、临时副本不残留)。
- 10x 到 1000x:迁移是一次性人工动作,不随数据量增长;无热路径影响。

## 11. 风险 / 反对意见(含对自己结论的)
- 反对:若 #432 合并后的 set_many 语义与候选字节不同(如 _assert_reload_valid 被移除、批量不再 apply-before-validate),本机制可能失败。缓解:G0 门强制合并后重核验,不成立则退回本席出 v4,不硬实施。
- 反对:load_config(temp) 若把目标 role 补成 DEFAULT_LLM_ROUTES 默认值,可能引入隐含默认。这不写进任何文件:默认值只存在于临时内存 Config,_patch_toml 只写用户显式给的两键;oracle 断言真实文件只有这两键变化。若该行为导致写后字节超出两键,AC7 失败,退回。
- 本轮为设计评审,未跑全量 pytest(无实现);B-1 结论建立在实测源码语义 + round-2 已执行的 validator 探针 + PR #432 候选字节上。

## 12. 置信度
HIGH(B-1 执行侧):机制建立在实测的 set 到 _patch_toml 无 HTTP、set_many apply-then-validate、load_config 解析顺序与 Config.source 契约之上;可证伪 oracle 明确。MEDIUM:依赖 #432 合并后字节不变(G0 门覆盖)。模型身份:LOW(UNKNOWN,声明模型 QUALITY 判词)。
