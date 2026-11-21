# 设计 381-B7-PR3-http-wizard v4

> 状态: DESIGN CANDIDATE v4 / DESIGN_PENDING(待独立设计 QA round-4 CLOSABLE;未冻结、未派工程、未提交)。本文件不授权工程。
> 关联: brief v4; slices v4; w1b-v4-refreeze-delta.md; PRD-381 / PRD-B7 §3.3/§3.6/§3.7。基线 main 500147037ff97fd1a9f25f8e42e50b375ed4d9bd(PR #432 已合)。
> owner 直裁(2026-11-19,经 question 工具): W1-B 不按已知断键落地,须 RE-FREEZE 并增加密钥值写通道;本版为 v4。声明模型 opencode/hy4-flash-free,物理身份未自证。

## 0. Steelman 与核心决定
目标: S5 四查询/探测端点 + S6 config 页 provider 向导,让用户选 provider、输 key、Test Connection、绑定 dream,且 key 值永不进入明文 config / 审计 / 日志 / 响应。核心决定(v1-v3 不变): 四端点纯查询、向导原子 set-many 绑定、default effect 为唯一理论锚、零预选。
v4 新增决定(owner 裁定驱动): 增加**一个** credential-gated、loopback-only 的 SecretStore 值写 HTTP 端点 POST /api/v1/secrets/set,让 S6 输入的 key 值先落到 SecretStore、再写 secrets: 引用;并把 daemon 的 RoleRouter 接上同一个 SecretStore,使绑定的 dream 路由真正解析出 key。这是 ADD,不替换、不放松任何 v3 AC。

## 1. v3→v4 变更边界(严格增量)
- 冻结路由集: 相对 pre-W1-B 从 +4 改为 +5;精确成员见 w1b-v4-refreeze-delta.md §2。
- 新增写集: src/mnemoseed_local/daemon/secrets_routes.py(新)、daemon/app.py 的第二个 include_router 行 + create_app 的 secret_store 注入参数 + RoleRouter 构造处 secrets= 实参、llm/routing.py 不改、secrets 目录不改、config 页 app.js 向导改、tests/test_dream_status_payload.py 冻结清单 +1 行、新增 tests/test_secrets_routes.py、docs/zh/design/04-trust-boundary-and-config.md §1.4 +1 行。
- B-1 修复范围不动: 独立实现 QA 的 BLOCKER B-1(params.model 等非 api_key 参数回显)由工程并行修复,本 v4 不重设计它的机制、不改它的 AC 文字;仅把它作为 carryforward 门保留(§9)。
- 其余 S5/S6 机制、行号语义集权威、Web-UI 双门、隔离约束全部继承 v3/freeze 原文。

## 2. 新端点精确契约 POST /api/v1/secrets/set
- 放置: src/mnemoseed_local/daemon/secrets_routes.py 定义 APIRouter(tags=["secrets"]),daemon/app.py 加一行 app.include_router(secrets_router)。不放进 configwrite(职责不同、避免污染 generation/审计语义),不放进 secrets 包(保持该包无 HTTP 依赖),不并入 llm/routes.py(避免混淆「+4 LLM 路由」的冻结计数)。
- 请求体: 手动解析的 dict,字段 name / value 均为非空字符串。禁止用带 value 字段的 Pydantic 模型: FastAPI 的 422 RequestValidationError 会回显输入字节,value 会进响应 sink(§4 变异 M3)。畸形 JSON 亦以固定 detail 处理,不回显解析片段。
- 名称允许表(本 slice 收紧): 只接受 mnemoseed/dream/ROLE,ROLE 必须命中既有 LLM_ROLES(config 常量)。显式拒绝 daemon/credential 及任何 sanitize_name 后等于 daemon.credential 的别名。理由: 阻止已认证调用方覆盖 daemon 自身凭据;保持 design/04 第199行「凭据轮换不新增 HTTP 路由」字面为真。本端点写 provider key,不写凭据。
- 响应成功: JSON 对象只含 ok、name、backend 三字段。不含 masked_tail: 尾 4 字符是 value 字节,属四 sink 违规。错误: HTTPException 固定 detail(如 invalid secret name / invalid request / secret write failed),绝不拼 str(exc);SecretsError 的 name 信息也不外泄,统一固定串。
- 语义: 幂等 last-write-wins,SecretStore.set 原子替换;不进 config generation 锁。写顺序由向导保证(§5),secret 先、set-many 后;set-many 409 时留下未引用的孤儿 secret,定义为可接受(无害),UI 提示仅重试绑定;secret 写失败必须中止绑定,绝不写未 provisioning 的引用。
- 凭据门: daemon/auth.py OPEN_PATHS 为 /healthz 与 /health 加枚举 console 文档(default-deny,精确相等、无前缀推断);POST /api/v1/secrets/set 不在两者内,自动被 auth_middleware 覆盖,不需任何豁免、不需改 auth.py。loopback 由 boot 非回环 baseurl 拒绝(app.py:1111-1113)与 cookie grant 的 loopback Host 约束(auth.py:196-206)双重保证。
- 日志 sink: _request_logging_middleware(app.py:1297-1319)只记 method+path+status,从不记 body;本路由不写审计行(不经过 configwrite)。请求日志开/关两种配置都测。

## 3. SecretStore 实例与读侧接线(关键发现)
- 实测证据: secrets/__init__.py:50-62 提供 default_secret_store(home);但全仓生产代码从未调用它,daemon/app.py:867-872 以 secrets=None 构造 RoleRouter,llm/routing.py:98-101 在 self._secrets is None 时把 api_key 置空。即 I-1 不止「值没写」,还有「写了也读不到」: 当前绑定的 secrets:mnemoseed/dream/dream 路由永远解析为 api_key 空串。
- v4 要求(owner「ensure the bound dream route resolves」的直接落实):
  1. create_app 增加 secret_store 注入缝(与现有 token_verifier 缝同形);默认 default_secret_store(CONFIG_DIR),写入 app.state.secret_store。
  2. daemon/app.py:867-872 的 RoleRouter 构造改为 secrets=app.state.secret_store。
  3. 新路由从 request.app.state.secret_store 取同一实例,写与读同源。测试可用注入缝传入内存/临时 store,或 MNEMOSEED_SECRET_BACKEND=file 加临时 home,避免真用户目录与 keyring 探测。
- configwrite/service.py:28「Secrets are never written or read」的适用范围限于 configwrite 服务;新路由在 daemon 层、不经过该服务,该句保持为真,不构成矛盾。
## 4. key 出流零值与四 sink grep oracle(新路由,同 S5 纪律)
哨兵值 sk-live-secret-9f3a2b 经 value 字段写入,四 sink 全字节 grep:
| sink | 断言 | 机制 |
|---|---|---|
| HTTP response | 成功/错误/422/500 全部无哨兵 | 固定 detail + 白名单字段;无 Pydantic value 模型 |
| audit | 无哨兵 | 本路由不写审计行 |
| daemon.log | 无哨兵(请求日志开/关两配置) | 中间件只记 method/path/status |
| str(exc) | 无哨兵 | 不回显异常;SecretsError 统一固定 detail |

变异(必须 RED):
- M1 成功响应加 masked_tail(name): response grep 命中哨兵尾段,RED。
- M2 错误 detail 拼 str(exc): exception sink / response grep 命中,RED。
- M3 把手动 dict 校验换成 Pydantic 模型触发 422 回显: response grep 命中,RED。
- M4 名称允许表删掉 daemon/credential 拒绝: 覆盖凭据文件,名称守卫测试 RED。
- 每变异留 raw 失败行 + 字节精确恢复证据;与 S5 四 sink 证明并列、不合并、不削弱。

## 5. S6 向导接线(S6-AC2 的字面满足)
- app.js wizardBind() 在 writeRoleBatch 之前先 await fetchJson(POST /api/v1/secrets/set,body 含 name=mnemoseed/dream/dream 与 value=key);仅在该调用成功后才写 api_key_env = secrets:mnemoseed/dream/dream 并 writeRoleBatch;失败则中止绑定并显示非具体错误。
- 输入清空(keyInput.value 置空)移到 secret 写成功之后。现状无条件清空是 I-1 的直接成因。
- ref/name 映射(精确): 配置引用 secrets:mnemoseed/dream/dream,经 secret_name_from_ref() 得 SecretStore name mnemoseed/dream/dream,文件后端落 mnemoseed.dream.dream.key,keychain 后端用同名 credential。路由允许表覆盖 mnemoseed/dream/ROLE;本 slice 向导只写 dream 一个槽。dream_verifier 复选框沿用同一 ref(共享一把 key),不新增第二次值写。
- 明文 config 零 key 的既有守卫(redact_key_ref_for_display / configwrite 校验)保持不变。

## 6. 与共享 provider 设计的对账(本 slice 的 canonical 决定)
- PRD-B7 §3.3 定义两种命名空间: 既有角色槽 secrets:mnemoseed/dream/ROLE 与新增共享槽 secrets:mnemoseed/llm/NAME(为多角色共用 + S7 OAuth refresh token 预留)。
- 实测: 生产 secrets/refs.py:26 的 SECRETS_REF_RE 只接受 secrets:mnemoseed/dream/ 后接小写角色;§3.3 所述正则扩分支未实现;config.py:558-561、configwrite/service.py:330-331、llm/routing.py:98-101 全部消费这一正则/helper。改走 llm/NAME 需跨 refs.py + config.py + service.py + routing.py + test_secrets_refs.py 的写集扩张,触碰已冻结 schema 面,且与零新增 CONFIG_KEY_REGISTRY 键的 PRD 约束相张力。
- **决定(canonical for this slice)**: 采用既有角色槽 secrets:mnemoseed/dream/dream,对应 SecretStore name mnemoseed/dream/dream。理由: 已实现、零 schema 扩张、单 dream 角色无需共享槽、与 S7 OAuth 解耦。§3.3 共享槽保留为后续切片选项,本片不启用、不实现、不声明为已支持;不做 S7 OAuth。
## 7. 理论锚(无新借用)
default effect / status quo bias(Johnson & Goldstein 2003;Samuelson & Zeckhauser 1988)仍是唯一借用,已注册于 PRD-B7 §2 与 v3 §5;规律与规则逐字不变。新端点与 SecretStore 写通道是纯 plumbing,无模型调用、不挂理论锚、不产生新的记忆侧效应声明。明确不借用: decision fatigue / choice overload / 一切 pop-neuro(清单不变)。

## 8. Denylist 更新(v3 全保留 + 本版追加)
v3 denylist 全部保留(不复活 f691637、不 cherry-pick 3f56e9e、不做 S7/S3/S4、不新增 MCP 工具名/页路由、key 不进明文 config/audit/daemon.log/console、不引入模型调用到 plumbing、不把 I-B4 门降级)。追加:
1. 新端点不得成为 daemon 凭据轮换路由,不得写 daemon/credential 或其别名;
2. 新端点响应不得含 value 或 masked_tail;
3. 不得为写通道新增凭据豁免或放开 OPEN_PATHS;
4. 不得借本片扩展 secrets: 语法到 mnemoseed/llm 命名空间;
5. 不得把新路由挂到 app.mount() 绕过 exact-set 冻结 pin。

## 9. Carryforward 门(全部保留,不降低)
- I-B4: 强制门 uv run pytest -q tests/test_console_role_batch.py -k preselection,node 缺失 RED 不 skip(包装 subprocess 调 node)。
- I-B5: B-pre 在合并字节上重钉全部行号锚点,语义 SET 权威、字面行号非契约。
- I-B6: 红相强制 node-missing、要求 collected count = 1 passed,pytest.skip 假绿必须被抓。
- Web-UI D gate(guides / Baseline 日期诚实 / mwgrcHash ABSENT)与 V gate(legacy-scan raw JSON、六项既有 EXCEPTION、不删 finding、不放宽规则)。
- 四 sink 证明(S5 既有 + 新路由 §4 并列)、B-1 修复范围(工程并行、本设计不改其机制/AC)、I-B4/B5/B6、N-B4 冲突表 v3 升级 + 包装测试入文件面。
- 冻结清单 exact-set equality: +5 后仍逐字相等;MCP 工具名集与页路由集不变。

## 10. 精确验证命令(增量)
    # 既有全部命令继续跑(freeze 精确命令 + v3 §10)
    uv run pytest -q tests/test_secrets_routes.py tests/test_dream_status_payload.py tests/test_llm_routes.py tests/test_console_role_batch.py tests/test_configwrite_setmany.py
    uv run pytest -q tests/test_console_role_batch.py -k preselection
    uv run pytest -q tests/test_secrets_routes.py -k four_sink
    uv run pytest -q tests/test_secrets_routes.py -k reserved_name
    uv run pytest -q tests/test_secrets_routes.py -k credential_gate
    <modern-web-guidance legacy-scan --json over changed web surface>
    ruff check; ruff format --check; mypy src
隔离: 临时 MNEMOSEED_LOCAL_HOME、spare port(禁 7788)、已装 runtime 不碰;测试用 secret_store 注入缝或 MNEMOSEED_SECRET_BACKEND=file,禁真 keyring 探针与真用户目录。
## 11. 基线哈希(SHA-256,main 5001470 本席重取)
    D7F66731D9E400AA14D038AB91CC930A80E75A50AD3E9322FF563227A7EDF1F6  app.js
    E77BE1CFABD15223E2AEBE75D2F66C08CB695BDA254944313B35DF8E715FB1C8  tests/test_dream_status_payload.py
    AE95EB74F28850A1A2CBCD70ECA613720094D2E3EC37C3DE543835AC8CAEC0B7  daemon/app.py
    D7470A59498735749B1B96ABA3BF1DBB6E29C1C56EA8587769A835A52EC27A64  daemon/auth.py
    1832D8BCC40D7F749A325F892BDC83B4846004FC0FA218A6CA3CCA3DEA0E1110  configwrite/service.py
    03F20C4BA8FAD71582E5BFC0D9B01F99FB14581FE7419E16136FC2EA8A1D62EE  secrets/store.py
    954D31EE6691DAAFF2BE635FAD547A1A128EB426983E6275754D93E559600DDC  secrets/refs.py
    4A1048E5178648401F51391BA68CF2E2C4F330342B2829492739BB3F99205BA6  secrets/__init__.py
    BE0D32B22502D12B3720B52405E7E233C1C059CC04D99307308D07AEE5621E6B  llm/routing.py
    CA6F138F8D3AF21E552175834F58414670F350CE3CC8DDB8DA8A7828287DC0EB  design/04
B-pre 实施时须在合并字节上重钉行号锚点(I-B5);上表仅供冻结复算。

## 12. 关键取舍(选项空间)
- 路由放置(采用): daemon/secrets_routes.py + 一行 include_router。代价: daemon/app.py 第二个 include_router 行 + 冻结集 +1。备选 A: 并入 configwrite_router(少一行 include,但混淆 configwrite 的 generation/审计语义、违背「Secrets never written」的局部诚实)。备选 B: 放进 llm/routes.py(复用现有 router,但把 secret 写混进 LLM 查询面、破坏 +4 LLM 路由的冻结计数清晰度)。选采用项。
- ref 命名空间(采用): 角色槽 mnemoseed/dream/ROLE。备选: 共享槽 mnemoseed/llm/NAME(§3.3 意图,利于多角色共用与 S7,但需四文件正则扩张 + 触碰已冻结 schema 面 + 本片单角色用不上)。选采用项,共享槽留后续切片。
- 读侧接线(采用): create_app 的 secret_store 注入缝 + app.state 共享。备选: 路由与 RoleRouter 各自 new 一个 store(测试不可控、双实例语义脆弱);备选: 作为 follow-up(直接违反 owner「ensure the bound dream route resolves」)。选采用项。
- 请求校验(采用): 手动 dict + 固定 detail。备选: Pydantic 模型(422 回显 value,四 sink 违规)。选采用项。

## 13. 实施期开放项(进冻结记录,不降 AC)
- O-v4-1: 向导 secret 写成功后、set-many 前的窗口若 daemon 重启,RoleRouter 按 generation 重建时会读到新 secret,行为正确;需在测试中以注入 store 验证一次(红相: 未注入 store 时 dream 解析 api_key 为空串必须被抓)。
- O-v4-2: dream_verifier 复选框复用同一 ref 属可选增强,不新增值写;若实施时选择写第二槽,须同步扩大路由允许表并加测试,不得静默。
- O-v4-3: N-1(implementation-evidence「640 files」陈旧)由工程刷新证据行,无门影响。

## 14. 结论
SHIP-WITH-ADJUSTMENTS(设计候选,待独立设计 QA round-4)。本版把 I-1 从「未 provisioned 引用」扩为「写 + 读两侧断链」并给出最小闭合: 一个第五路由 + app.state 同源 store + 向导 secret-first 顺序;所有前版 AC/门原样保留。物理身份 UNKNOWN,模型身份置信 LOW;代码与契约证据置信 HIGH。未改产品/测试字节、未提交、未冻结、未派工程。