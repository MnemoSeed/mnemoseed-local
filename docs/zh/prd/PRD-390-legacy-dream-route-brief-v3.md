# PRD-390 legacy driver-only dream route (brief v3)

- 状态: PLANNING / DESIGN_PENDING。本 brief 不授权工程。
- 关联: issue #390 (queue, OPEN)。前置: PR #432 合并 + G0 重基线门。基线 main 8ad2d6ace54c1fca467796cc6ddfcbf596318168。
- v3 相对 v2: 修正 v2 迁移命令的执行路径。round-2 证明 config set 是 HTTP POST 且 daemon 只能在 load 成功后启动,在坏 home 上命令不可执行;改为主进程内 ConfigWriteService.set_many 本地写,三形状一次覆盖。

## What
升级后的旧 home 里 [dream.llm.dream] 只设 driver、没设 model(或两者皆缺、或只设 model),load 阶段被拒:config.py:55 ROLE_ROUTE_REQUIRED_MESSAGE。config.py:788-791 对任一缺失抛相同的 ConfigError(仅 key + message,无形状位)。

## Done when
1. CLI 检测到该形状时给出一条可直接执行、shell-safe 的迁移命令(repair-dream-route + 占位符),不再只吐裸配置错误。
2. 不静默改用户配置:命令由用户显式执行,经进程内 ConfigWriteService.set_many 本地写 config.toml + meta DB,不经 daemon/HTTP。
3. 复用 #432 的 reload guard 与 set_many 批量校验:用户填值并执行后,路由成为 reload-valid,一条 shell 行恢复 boot;三种缺失形状(driver-only / model-only / 两者皆缺)都恢复;此澄清不放宽任何红线,且必须在不 boot 的 home 上可执行。
4. 冻结 fixture 固化 driver-only、model-only、both-missing 三形状。
5. 隔离测试:临时 home + spare port;每形状 -> 断言无 daemon -> 命令 -> 执行 -> boot 成功;覆盖 up/on/doctor。
6. ELIGIBILITY 门 G0:#432 合并后在合并 HEAD 重取 configwrite/service.py 并核验 set_many 语义(apply-then-validate + _assert_reload_valid + 任意顺序 role pair);不符则退回设计,不硬实施。
7. 全门禁绿 + 独立设计 QA CLOSABLE。

## 选项(设计给取舍)
- 选项 A(v3 采用): CLI 统一识别 + 一条 repair 动词经进程内 set_many 本地写。KISS、无静默变更、非 boot home 可执行、复用既有写者。
- 选项 B(不采用,round-2 证伪): 保留 HTTP config set 行 + 先手动起 daemon;起 daemon 本身需 load 成功,循环依赖。
- 选项 C(不采用): 两次单键 set();cross_validate 在半路由上拒首写。
- 选项 D(不采用): load 接受半路由 + 默认模型常量;违反红线。

## 理论锚
纯工程迁移 UX,无新增理论借用;不借用 default effect / 自愈类比。

## 范围外
改 config.py load 语义;改 configwrite/service.py(除 #432 已合字节);自动执行迁移命令;改 DB 行;HTTP config-set 路径;固定端口/真实 home;模型调用。
