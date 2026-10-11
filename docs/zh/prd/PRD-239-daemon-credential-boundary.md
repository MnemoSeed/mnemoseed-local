# PRD · daemon 本地凭据边界（#239）

> 依据：owner 2026-10-10 方向（#239 帖）+ solution-architect 设计评审 CLOSABLE-WITH-CONDITIONS（全文存 #239 评论，B1–B6 已折入）+ owner 全盘接受五问推荐。
> 前置：#234 测试端口隔离批先合（其 tests 面轻）；本 PRD S2 在 #234 合并后开工。

## 理论锚

**无借用——工程信任面。** 明确不借用清单：不为 bearer 边界编造记忆衰退/注意力隐喻；不借 OAuth 术语膨胀威胁模型（本批不做 OAuth 2.1）；"credential"一词在本仓专指本机制（不与 LLM token 混用，B6）。

## 背景与动机

daemon HTTP 面（默认 bind 127.0.0.1:7788）今天接受任何调用者。要防的三件事：

1. **同机进程边界**——任何同机进程可读写真记忆数据（`/memory/*`、`/ingest` 等）；今天连"停掉 daemon"（`/daemon/shutdown`）和"写观测计数"（`/mcp/handshake`）都对任意进程开放。
2. **浏览器来源边界**——任意网页可 `fetch` loopback 端点（CSRF/DNS-rebinding 类）。
3. **云端迁移 seam**——将来 daemon 上云时客户端零重建。

非目标（明确）：非 loopback 暴露（`up --host` 通配符场景仅如实记录，是否单独切片另议——owner 已选"推荐"=单独切片拒绝，见 S0 注）；完整 OAuth 2.1（待 #217 loopback HTTP MCP transport 落地）；per-profile/scoped 凭据；记忆语义任何改动。

## 冻结设计

### 凭据与校验
- 新增 `src/mnemoseed_local/credential.py`：
  - `TokenVerifier` Protocol（`verify(presented) -> bool`）+ `LocalFileTokenVerifier`（今天：`CONFIG_DIR/secrets` 文件比对，复用 `FileSecretStore`：`os.open(0o600)` + 原子替换；Windows 沿用"用户 profile ACL 为强制边界，不 chmod/不 icacls"）。
  - `read_credential()`：**只读不建**（客户端语义；缺失/损坏抛 typed `DaemonCredentialError`）。
  - **call-time 取 `CONFIG_DIR`**（函数内导入，照 `daemon_state.py:18-22`）——B2 BLOCKER：`app = create_app()` 是模块级的，解析时机放错 = import 即写真实 home。
- 签发：daemon lifespan 启动时 create-if-absent，**只在 daemon 侧写**；重启不重签（持久文件）；轮换 = 新 CLI 动词 `credential rotate`（不新增 HTTP 路由）；`uninstall --purge` 连带删除（隐性轮换路径，文档写明）。

### 客户端唯一通道（B4）
- Python：`DaemonClient` 增 `token: str | None = None`（排在 `timeout` 之后，frozen dataclass 兼容全部现有关键字构造；`dataclasses.replace` 自动携带）；`_headers()` 在可用时加 `Authorization: Bearer`。五个构造点（`resolve_client`、`cli.py:406`、`cli.py:1150`、`hosts/claude_code/events.py:396`、`mcp_gateway/server.py:189`）零改动漂移。
- TS 插件：`hosts/opencode/plugin.ts` 三处裸 fetch（`:359-364`/`:817-822`/`:848-857`）统一 helper 补 header；凭据从 `~/.mnemoseed-local/secrets` 以 `node:fs` 只读。
- 反向钉死：测试断言 `_headers()` 除 `Authorization` 外不新增任何签发方相关头（客户端契约冻结）。

### 受保护面（B1，三条机械规则）
1. **默认拒绝**：未命中开放集合的请求一律 401。
2. **精确等值匹配**：禁前缀匹配、禁任何路径规范化（`rstrip`/`normpath`/大小写折叠都不行）——`/healthz/../api/v1/audit` 必须 401 而非 200。
3. **开放集合 = enumerated set**：`/healthz`、`/health` + console 三文件（`/`、`/index.html`、`/app.js`、`/style.css`，lifespan 启动时枚举；新增文件未枚举 = console 白屏，fail-closed 且当场可见，配测试钉住）。
- 关闭：`/mcp/handshake`（任意进程可写计数器）、`/daemon/shutdown`（任意进程可停 daemon，DoS 面）、全部 `/api/v1/*`、`/ingest` `/session/*` `/memory/*`、configwrite 路由。
- /healthz 开放的取舍（owner 已定）：watchdog 走裸 TCP、announce_ready/daemon_reachable/console 圆点四消费者依赖它；代价 = 基建元数据（preset/drivers/migrations/gate/uptime）对同机进程与可 DNS-rebinding 的网页可见——侦察价值、非记忆内容、其上无写。**如实写进威胁模型**。

### console 认证（B3，冻结方案 b）
中间件在**文档响应**上种 cookie：条件 = 路径 ∈ 文档集合 且 `Host` 主机名 ∈ `{127.0.0.1, ::1, localhost, 127.*}` 且（无 `Origin` 头或 `Origin` 与 `Host` 同源）。cookie = `<credential>`，`HttpOnly; SameSite=Strict; Path=/`；中间件认该 cookie 为 bearer 等价物。三段机制收益：跨站 fetch 不带 cookie（SameSite=Strict）→ 401；`Authorization: Bearer` 非 CORS-safelisted → 跨源必 preflight 且无凭据 preflight 返 401；DNS-rebinding 页 Host 非回环 → 拿不到 cookie 且被拒。**token 从不进 DOM。** 测试须显式 `headers={"host": "127.0.0.1:7788"}`；"非回环 Host 的文档请求不得被种 cookie"本身是一条断言。**浏览器语义 `[unverified]`**：S0 加一条 Playwright 跨源/rebinding 实证（隔离端口 + 隔离 home）。

### 中间件形态与顺序（I8）
纯 ASGI（禁 `BaseHTTPMiddleware`——会重排 respond-then-run，`/daemon/shutdown` 依赖它）；认证中间件包住日志中间件外层（401 不产生日志行，顺带守泄露面）；顺序用测试钉死。

### 失败语义
- daemon：401 + **常量** detail（禁回显任何请求材料）+ `WWW-Authenticate: Bearer`（不带 token 摘要）。
- 客户端：`DaemonCredentialError`（文件缺失/损坏；文案照 `DAEMON_DOWN_HINT` 房子风格给动作）；`DAEMON_AUTH_HINT`（401；点名最常见真实原因：client 与 daemon 用不同 `MNEMOSEED_LOCAL_HOME`，或凭据被轮换）。
- **401 不得重试**（`reliable_client.py:88-96` 只重试 ConnectError，保持不动 + 测试钉死）；**401 不得说成"daemon 没起"**（`_daemon_reachable` catch-all 会让 `cmd_off` 打假话——由 /healthz 开放规避）。

### 泄露守门（I9，四条全做）
1. canary 测试：已知凭据起 app，做带凭据成功 + 无凭据 401，断言该字符串**不出现在** daemon.log、`/api/v1/audit` 与 `/memory/audit` 行、401 响应体、`GET /` 与 `/app.js` 返回体。
2. 401 detail 常量。
3. 中间件日志只写 method+path+status，禁 header 日志。
4. 源级禁令测试（ast/子串）：`credential.py` 与中间件内无 logger/print 接收凭据值。

### issuer-swap 可演示（I6）
Protocol + 两实现 + 对照测试：stub verifier（模拟云端 AS：只认自己 token、拒本地文件 token）经**完全未改的** `DaemonClient`/`GatewayClient` 路径 + 相同 header 契约，断言 200/401 边界与本地 verifier 一致。`create_app(*, token_verifier=None)`：None = 每请求 CONFIG_DIR 解析的本地 verifier；显式传入即换签发方。客户端无任何"哪种签发方"分支。

## 切片（TDD，按序）

- **S0 契约与 seam（先行）**：`credential.py`；`DaemonClient` token 字段 + `_headers`；`create_app(token_verifier=None)` + ASGI 中间件（default-deny/精确集合/cookie grant/认证外层）；`tests/auth_support.py`（`seed_credential(home)` / `credential_headers(token)` / `boot(home)` 三 helper，**唯一入口**）；Playwright 跨源实证。RED→GREEN：401/200 边界、`..` 路径 401、枚举外静态 401、cookie 仅回环 Host+同源、重启后有效、401 不重试、canary 四不出、stub verifier 对照。
- **S1 TS 插件通道**：plugin.ts 三处统一 helper + `hook_driver.mjs` 断言 header；doctor 增"已安装插件与出厂字节不一致"提示（复用 `hook_status`，零新机制，I5 防旧插件静默停捕）。
- **S2 测试迁移（依赖 S0 合入，且在 #234 批之后）**：28 文件/166 处 `TestClient(...)`，按**互不相交文件面**切 3–4 组并行给 senior-software-engineer；每组自带"去掉 header 就 401"反向断言防假绿。**守门：不允许任何绕过认证的测试开关；`auth_support.boot()` 是唯一入口。**
- **S3 文档与诚实边界**：README（"no accounts" 口径改）、design/04、design/05、actor 注释、威胁模型（原样写下边界）、`test_docs_honesty.py` 加 pin、命名消歧段。

## 威胁模型诚实边界（设计文档原样写明）

不防：①同用户恶意软件/任意同用户进程可读凭据文件并冒充客户端（`design/04:319` 同款边界）；②root/admin/SeDebug、home 备份、内存与 pagefile；③**不区分客户端身份**——持凭据即等价，`X-MnemoSeed-Actor` 仍是自声明归因、不参与授权；④静态数据（meta.db/chunks.lance）仍明文；⑤console 同源 JS（XSS）等于用户本人；⑥**通配符 bind + 无 TLS = 凭据局域网裸奔**（今天是完全无认证暴露；有凭据后至少是有认证的；owner 已定：单独切片让非回环 `--host` 拒绝启动，S0 注记并入）。

## 验收判据（冻结）

1. 未带凭据 401 / 带 200 边界按开放集合逐条成立；`/healthz/../api/v1/audit` 401。
2. 凭据不出现在 log/audit/错误体/console 载荷（canary + 源级禁令）。
3. console：非回环 Host 不种 cookie；跨源 fetch 被机械拒绝（Playwright 实证）。
4. daemon 重启后凭据仍有效；`credential rotate` 后新凭据生效、在途 401 窗口有文档。
5. issuer-swap：stub verifier 下未改客户端代码路径行为一致（boundary 断言逐像素）。
6. 401 不重试、不进审计、detail 为常量；MCP isError 文案指向 `up`/config home。
7. doctor 提示 stale 插件；TS 插件三处 fetch 带 header（hook_driver 断言）。
8. 门禁全绿：`uv run pytest -q` / `ruff check` / `ruff format --check` / `mypy src`。

## 文件面与隔离纪律

- 允许面：`src/mnemoseed_local/**`（credential/rest_client/daemon app/cli）、`hosts/opencode/plugin.ts`、`docs/**`、README、`tests/**`（S2，在 #234 之后）。
- 隔离纪律：测试一律 spare port + 临时 `MNEMOSEED_LOCAL_HOME`；`test_hosts_claude_code.py` 的 `ENV_VARS` **必须**含 `MNEMOSEED_LOCAL_HOME` 并显式给 tmp home（B5）。
- Stop rules：出现"为通过测试而跳过认证"的倾向（最大腐化路径，见 S2 守门）；any 测试/运行路径写真实 home；威胁模型被写成"防恶意本地进程"（过度承诺，QA 抓）。
