# PRD-381-B7-PR3 brief v4 (S5 HTTP + S6 Console Provider Catalog 向导 + SecretStore 值写通道)

- 状态: PLANNING / DESIGN_CANDIDATE v4。本 brief 不授权工程。
- 章程: issue #381;权威 PRD docs/zh/prd/PRD-B7-providers.md §3.3/§3.6/§3.7/§4。基线 main 500147037ff97fd1a9f25f8e42e50b375ed4d9bd(PR #432 已合)。
- owner 直裁(2026-11-19): W1-B 不按已知断键落地,须 RE-FREEZE 并增加密钥值写通道;设计回到 v4。范围仍仅 PR3 = S5 + S6,S7 OAuth / S3 / S4 范围外。
- v4 相对 v3: 新增 1 条 credential-gated loopback HTTP 路由 POST /api/v1/secrets/set;冻结集 +4 改为 +5;S6 先写 SecretStore 值再写 secrets: 引用;daemon RoleRouter 接同一 SecretStore 使绑定路由真正解析。v3 全部 AC 原样保留,本版是 ADD 不是放宽。

## 前置(硬)
1. PR #432 已独立 QA CLOSABLE 并合并(merge commit 5001470):S6 原子绑定依赖 POST /api/v1/config/set-many。
2. 陈旧分支 f691637 禁止复活;3f56e9e 只作意图参考不 cherry-pick。
3. 本 brief 须与 design v4 / slices v4 / refreeze delta 一起经独立设计 QA round-4 CLOSABLE 后才可重冻结、派工程。

## What
S5: 四端点 GET /api/v1/llm/catalog、POST /api/v1/llm/probe、POST /api/v1/llm/models、POST /api/v1/llm/detect-local;probe 新密钥出流面,response / audit / daemon.log / 异常 str 四 sink 零 key 字节;冻结清单 +4。B-1(model 等非 api_key 参数回显)由工程并行修复,设计不改其 AC。
S5c(v4): 新 daemon/secrets_routes.py,POST /api/v1/secrets/set,手动校验 + 固定 detail + 名称允许表 mnemoseed/dream/ROLE + 拒绝 daemon/credential;成功响应只含 ok/name/backend,无 value、无 masked_tail;凭据门自动覆盖、不需豁免;daemon/app.py 第二个 include_router 行 + create_app 的 secret_store 注入缝 + RoleRouter secrets= 实参;冻结清单再 +1。
S6: config 页面向导:provider 卡无预选(强制机器 oracle)、Test Connection 调 probe、可搜索清单、经 #432 set-many 绑定(driver-first 客户端约定)、隐藏 ollama 字段、真实 driver 提示;并在 writeRoleBatch 之前先把 key 值 POST 到 secrets/set,成功后写 secrets:mnemoseed/dream/dream 引用、成功后清空输入;失败则中止绑定。

## Done when
S5(全部继承 v3,不降低)
1. 四端点契约与 PRD-B7 §3.6 一致;冻结清单 +4 行仍绿;MCP 工具名集、页路由集不变。
2. probe/models/detect-local 请求 key 字节在四 sink 缺席(开/关请求日志都测),grep 针对完整请求字节含 base_url/query/model。
S5c(v4 新增)
3. POST /api/v1/secrets/set 存在且仅此一条新增路由;未带凭据返回 401;无豁免、无 app.mount 绕过;loopback-only 保持。
4. 哨兵 key 在 response / audit / daemon.log / str(exc) 四 sink 全缺席;成功响应不含 value 或 masked_tail;422/500 错误 detail 固定、不回显输入;M1-M4 变异全部 RED 且字节精确恢复。
5. 名称允许表只收 mnemoseed/dream/ROLE,拒绝 daemon/credential 及别名;provider key 落 SecretStore,config.toml 零 value。
6. 写入后 daemon RoleRouter 能解析该 ref(api_key 非空),以注入 store 的红相证明;写与读为同源实例。
S6(v3 全保留 + v4 追加)
7. 向导不预选任何 provider:所有 provider radio 首屏 checked===false,强制 ts_hook 机器 oracle;门可执行 uv run pytest -q tests/test_console_role_batch.py -k preselection,非 if added,node 缺失 RED 不 skip。
8. key 只经 SecretStore;明文 config / console / daemon.log / probe response 零 key。
9. Test Connection 调 POST /api/v1/llm/probe;模型清单客户端过滤(input type=search)。
10. 绑定经 #432 set-many,driver+model 同批、客户端固定顺序(非 server 约束);带 If-Match,冲突按 409 处理。
11. 按 wire protocol 隐藏 think/num_ctx/num_predict;driver 提示为真实注册集。
12. 隔离 Playwright 手检(spare port + 临时 home);Web-UI D gate 证据落设计文档。
13. (v4) secret 写先于 set-many;ref 固定 secrets:mnemoseed/dream/dream;ref/name 映射即 SecretStore name mnemoseed/dream/dream;输入仅在写成功后清空。

## 理论锚 / 取舍
复用 PRD-B7 §2 default effect(Johnson & Goldstein 2003;Samuelson & Zeckhauser 1988)为唯一新增借用。不借用 decision fatigue / choice overload。新写通道是纯 plumbing,无模型调用、不挂理论锚。I-B4 取舍:强制门用 pytest 包装(node 缺失 RED)而非直接 node 命令或不可执行的 .mjs pytest 命令。canonical ref 取舍:采用既有角色槽 secrets:mnemoseed/dream/dream,不采用 §3.3 共享槽 secrets:mnemoseed/llm/NAME(需跨四文件正则扩张、触碰冻结 schema 面),共享槽留后续切片并明确不做 S7 OAuth。

## 范围外
S7 OAuth;S3/S4;provider 运行期降级;eval roster;counts.py/boot-notice 附带项;secrets: 共享命名空间扩张;daemon 凭据轮换 HTTP 路由。