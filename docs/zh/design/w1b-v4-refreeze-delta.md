# W1-B v4 重冻结增量说明 (w1b-v4-refreeze-delta)

> 状态: DESIGN DELTA NOTE。配合 design v4 / slices v4 / brief v4 阅读。本文件不授权工程、不冻结、未提交。
> 基线: merged main 500147037ff97fd1a9f25f8e42e50b375ed4d9bd(PR #432 已合)。原冻结记录 docs/verification/freeze-wave01-w1b-b7-pr3.md 保持历史,不被覆盖;本说明给出 v4 需重冻结的精确差异。
> owner 直裁: 不按已知断键落地,RE-FREEZE 并增加 SecretStore 值写通道。

## 1. 一句话差异
冻结 HTTP 路由集从「pre-W1-B + 四 S5 路由」改为「pre-W1-B + 四 S5 路由 + 一条 SecretStore 值写路由」。相对 pre-W1-B 由 +4 变为 +5;相对现行冻结(已含 +4)只做 +1 ADD,不删、不改、不放宽任何既有 AC。

## 2. 精确冻结集(39 条,逐字)
pre-W1-B 基线 34 条(取自定义 tests/test_dream_status_payload.py 的 assert http_paths == {...} 加参数化 expected_paths,main 5001470 实测):
1. /healthz
2. /health
3. /api/v1/observability
4. /api/v1/audit
5. /api/v1/host/capture-evidence
6. /api/v1/profiles
7. /api/v1/profiles/archive
8. /api/v1/config
9. /api/v1/config/set
10. /api/v1/config/set-many
11. /api/v1/config/versions
12. /api/v1/config/rollback
13. /daemon/shutdown
14. /ingest
15. /session/end
16. /session/active
17. /flush
18. /session/recall-pending
19. /session/recent
20. /session/windows
21. /memory/atlas
22. /memory/audit
23. /memory/error_events
24. /memory/export
25. /memory/forget_this
26. /memory/reinforce
27. /memory/timeline
28. /mcp
29. /memory/recall
30. /memory/remember
31. /memory/supersede
32. /memory/dream_once
33. /memory/dream_status
34. /memory/recall_feedback

S5 已冻结 +4(现行冻结记录):
35. /api/v1/llm/catalog
36. /api/v1/llm/probe
37. /api/v1/llm/models
38. /api/v1/llm/detect-local

v4 唯一新增(第五):
39. /api/v1/secrets/set

方法 POST。完整集合 = 上述 39 条 exact-set equality。MCP 工具名集与页路由集断言逐字不变;页路由仍 {overview, atlas, config, profiles, dream},无 #/wizard。

## 3. 必须重冻结的工件与动作
1. 五元组更新: brief v4 / design v4 / slices v4 的新 SHA-256 + 独立设计 QA round-4 CLOSABLE 判词,连同本 delta note 一起入新冻结记录;旧冻结记录与 round-3 判词作为历史保留,不可覆盖。
2. 基线重取(main 5001470,本席已实测,实施时须在合并字节上复算并重钉行号锚点 I-B5):
    D7F66731D9E400AA14D038AB91CC930A80E75A50AD3E9322FF563227A7EDF1F6  src/mnemoseed_local/console/static/app.js
    E77BE1CFABD15223E2AEBE75D2F66C08CB695BDA254944313B35DF8E715FB1C8  tests/test_dream_status_payload.py
    AE95EB74F28850A1A2CBCD70ECA613720094D2E3EC37C3DE543835AC8CAEC0B7  src/mnemoseed_local/daemon/app.py
    D7470A59498735749B1B96ABA3BF1DBB6E29C1C56EA8587769A835A52EC27A64  src/mnemoseed_local/daemon/auth.py
    1832D8BCC40D7F749A325F892BDC83B4846004FC0FA218A6CA3CCA3DEA0E1110  src/mnemoseed_local/configwrite/service.py
    03F20C4BA8FAD71582E5BFC0D9B01F99FB14581FE7419E16136FC2EA8A1D62EE  src/mnemoseed_local/secrets/store.py
    954D31EE6691DAAFF2BE635FAD547A1A128EB426983E6275754D93E559600DDC  src/mnemoseed_local/secrets/refs.py
    4A1048E5178648401F51391BA68CF2E2C4F330342B2829492739BB3F99205BA6  src/mnemoseed_local/secrets/__init__.py
    BE0D32B22502D12B3720B52405E7E233C1C059CC04D99307308D07AEE5621E6B  src/mnemoseed_local/llm/routing.py
    CA6F138F8D3AF21E552175834F58414670F350CE3CC8DDB8DA8A7828287DC0EB  docs/zh/design/04-trust-boundary-and-config.md
3. 冻结新写通道契约: 路径、请求字段、名称允许表、成功响应字段、固定错误 detail、四 sink grep oracle 与变异 M1-M4、401 默认拒绝、读侧同源 store 解析证明。
4. 冻结 canonical ref 决定: secrets:mnemoseed/dream/dream 对应 SecretStore name mnemoseed/dream/dream;共享槽 secrets:mnemoseed/llm/NAME 本片明确不启用,S7 OAuth 不做。
5. 继承并逐条列回: I-B4/B5/B6、N-B4、Web-UI D gate 与 V gate、四 sink 证明、B-1 修复范围(工程并行、设计不改)、N-1 证据刷新。
6. denylist 追加五条(见 design v4 §8),原 denylist 不动。
7. 冲突表升 v4: daemon/app.py 与 app.js 与 test_dream_status_payload.py 均为同文件面串行点,不得并行派发(见 slices v4 冲突检查)。

## 4. QA verdict 到设计的映射(供 round-4 核验)
- B-1(model 参数回显): 不在本 v4 设计改动范围,工程并行修复;v4 仅保留为 carryforward 门,不得以此降低或替换任何 AC。
- I-1(值未写入 SecretStore): v4 以 S5c 新路由 + S6c secret-first 顺序 + 读侧 RoleRouter secrets= 接线闭合;QA 原文「main may accept it as a follow-up」的选项已被 owner 裁定否决,必须实修。
- N-1(640/641 文件计数): 工程刷新证据行,无门影响。
- 新增发现(本席,QA 未覆盖): daemon/app.py:867-872 以 secrets=None 构造 RoleRouter,且生产代码从未调用 default_secret_store;即使写入成功,绑定路由的 api_key 仍为空串。v4 §3 已把它纳入 I-1 实修范围,owner「ensure the bound dream route resolves」即对此项。

## 5. 仍需 owner 裁决的开放项(本席不伪造)
- OWNER-1: 读侧接线是否确定纳入本 slice。理由: owner 原话「ensure the bound dream route resolves」已明确要求,本席据实设计纳入;但它把 daemon/app.py 的写面从「一行 include_router」扩到「第二行 include_router + create_app 注入缝 + RoleRouter secrets= 实参」,并新增 tests/test_secrets_routes.py 的读侧红相。若 owner 认为该扩张越过授权边界,须明确书面改判;本席不擅自缩小。
- OWNER-2: 新端点名称允许表是否只收 mnemoseed/dream/ROLE。本席按 KISS 与 §3.3 对账收窄;若 owner 要求本片即支持共享槽 mnemoseed/llm/NAME,须另行批准跨 refs.py/config.py/service.py/routing.py/test_secrets_refs.py 的正则扩张设计轮,不得在 v4 内静默加。
- 其余(路径命名、ref/name 映射、路由放置、请求校验方式)由本席按 owner 授权「name your exact path」「decide which is canonical」决定并已落到 design v4,不待 owner 再裁。

## 6. 结论
SHIP-WITH-ADJUSTMENTS(设计增量候选,待 round-4 独立设计 QA)。v4 是严格 ADD:一个第五路由、一档同源 SecretStore 接线、向导 secret-first 顺序;冻结集 34 -> 39(+4 S5 +1 v4),所有既有 AC/门不变。本席未改产品/测试字节、未提交、未冻结、未派工程;owner 身份与物理身份均 UNKNOWN,代码与契约证据置信 HIGH。