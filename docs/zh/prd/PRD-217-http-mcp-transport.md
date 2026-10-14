# PRD · loopback HTTP MCP transport（#217）

> 依据：issue #217 + solution-architect 设计评审 CLOSABLE-WITH-CONDITIONS（六问裁定 + D1–D6 owner 决策，已记录在 #217 评论）。
> 前置：#244 凭据边界、#241 测试隔离、#248 caller 凭据守卫已合入 `origin/main`（a4333e9）；批分支必须从该点切。
> 理论锚：无借用——工程控制面与传输语义。不为"无状态传输"编造认知词汇。

## 冻结设计（评审裁定，不重新 litigation）

### 传输（Q1）
- 唯一路径 **`/mcp`**（精确等值，无尾斜杠变体）。`POST /mcp` = JSON-RPC 2.0；`GET`/`DELETE` → 405。不发 `Mcp-Session-Id`，无服务端会话表——**无状态**。
- 注册：`app.include_router(mcp_router)` 与其余 router 并列，**必须在 console `StaticFiles` 挂载之前**（Starlette 按注册序匹配；次序有测试钉）。
- 信封层直调既有 `handle_message` / `call_tool` / `TOOLS`（`mcp_gateway/server.py`），**零改动**：工具集、isError 文案、归因字节不变，stdio 契约测试与工具集 pin 继续有效。`initialize` 答既有 `PROTOCOL_VERSION`；协议版本回声协商仅作为 adapter 层的备选缝，本批不启用。
- stdio 网关原样保留（`serve()`、`handle_line`、UTF-8 线钉不动）。

### per-request 身份契约（Q2）
| 头 | 语义 | 层 |
|---|---|---|
| `Authorization: Bearer <credential>` | 授权（边界唯一入口） | 既有 gate，不动 |
| `X-MnemoSeed-Profile: <profile_id>` | 每请求 namespace 选择，**仅 `/mcp` 解释**；缺失/空白 → **HTTP 400 常量 detail**（不回显请求材料），不静默默认 | 本批新增 |
| `X-MnemoSeed-Actor: mcp` | 归因（自声明，不参与授权） | 既有语义；MCP 面对 daemon REST 的自调用**一律钉 `ACTOR="mcp"`**，与 stdio 逐字节一致 |

- 工具 arguments 冻结（`additionalProperties: False`），不得为 profile 改 schema；profile 走 header。
- profile 不是授权（与 REST 面同款边界，如实文档化，不伪装）。

### MCP 客户端认证（Q3）
- 零新增安全机制：`/mcp` 不进 `OPEN_PATHS`、不进 console documents——默认拒绝依建构继承；401 常量 detail + `WWW-Authenticate: Bearer`（不带 resource metadata，那是未来 OAuth seam）。
- 第一方 remote 条目由插件 config hook 注入，**必须**带 `"oauth": false` 与显式 `timeout`（30000）；条目在 config-hook 时读取 `<home>/secrets/daemon.credential.key` 的字面值。D4：`create-if-absent` + **形状迁移**（现存 `command == ["mnemoseed-local","mcp"]` 的本厂 stdio 条目改写为 remote，保留用户 `enabled`；其它 manual 条目不碰）。
- 第三方本地客户端与 `DaemonClient` 同构（`client_for` 带 bearer），README 文档化。
- 云 seam 不动：`TokenVerifier` Protocol + `create_app(token_verifier=...)`；HTTP 面不引入任何"识别签发方"分支。

### 并发与生命周期（Q4）
- **tools/call 必须离环**：`await anyio.to_thread.run_sync(...)` + 每 app 一个 `anyio.CapacityLimiter(4)`（anyio 默认 limiter 为 40，4 ≪ 40 无饥饿；不得改 semaphore 或 `asyncio.to_thread`）。同步 inline = 自调用死锁，有测试断言 tools/call 远小于 30s 完成。
- 执行模型：每请求构造 `GatewayClient.wrap(DaemonClient(base_url=app.state.config.baseurl, profile_id=<header>, actor=ACTOR, token=read_credential_or_none()))`——`call_tool` 零改动，路由校验/审计/sighting 日志全部复用。
- 重启/重连：无状态 → 无可失状态；loopback refused 由 `GatewayClient` 既有单次快重试兜。`/daemon/shutdown` respond-then-exit 接缝零触碰；不新增中间件（`len(factories)==2` 的 pin 不许破）、不新增常驻线程。
- 观测诚实记账：每次 tool call = 2 笔已认证 `mcp` 请求（客户端腿 + 自调用腿），语义写进文档；doctor guard 为 `> 0` 不受影响。

### 测试策略（Q5）
- 真 daemon 测试走 `tests/_test_daemon.py::daemon_for_test`（凭据读同一 seam：`read_credential_at(daemon.home)`）；TestClient 进程内 boot 覆盖契约面（401/400/405/initialize/tools-list）。
- 新 `tests/test_mcp_http_transport.py`：无凭据 401；initialize 形状 + tools/list == 六工具；缺失/空白 profile → 400 常量；真 daemon 上 tools/call `isError:false` 且审计 actor==`mcp`；双客户端 profile 隔离并发；自声明 actor 不改变内部归因；GET/DELETE → 405；`/mcp/` 带凭据 404/不带 401（路径陷阱 pin）；离环完成时限；凭据 canary（复用 #239 模式）。
- `tests/test_dream_status_payload.py` 的冻结路由集合**必须**加入且仅加入 `"/mcp"`。
- caller-credential guard（#248）：新增任何引用 `/mcp` 的 in-repo caller 必须同 PR 进 `KNOWN_CALLERS`。
- **验收探针（PR 必交证据）**：真 OpenCode 经 remote 条目连 spare 端口 + 隔离 home 的 daemon，列出工具并成功调用一次（隔离纪律：禁 7788、禁碰安装运行时）。

### 范围外（明确）
非 loopback 暴露；OAuth 2.1 流程（DCR/PKCE/token 端点——只留 seam）；MCP resources/prompts；subscriptions/progress/cancellation；SSE server→client 流；per-profile/scoped 凭据；REST 面加 profile 头；对 gate 的任何改动；stdio 下线。

## 切片（TDD，按序）

- **S0 契约与路由骨架**：`src/mnemoseed_local/mcp_gateway/http_transport.py`（新增）+ `daemon/app.py` 注册一行 + `tests/test_mcp_http_transport.py` + 路由 pin 更新。进程内 RED→GREEN。
- **S1 tools/call 真回路**：每请求 client + 离环 dispatch；真 daemon 测试（round-trip、actor 归因、双客户端隔离并发、离环时限）。
- **S2 生命周期与诚实边界**：重启/断连、canary、观测双计文档；README/design 04/05 增补 HTTP 面。
- **S3 第一方接线 + 验收探针**：`plugin.ts` config hook 注入 remote 条目（D4/D5）；`hook_driver.mjs` 期望更新 + ts 行为测试；doctor 对旧 stdio 条目的提示；真 OpenCode 探针入 PR 证据。

## 验收判据（冻结）

1. `/mcp`：无凭据 401；带凭据 initialize/tools/list/tools/call 全通；tools/call 审计 actor==`mcp`；`/mcp/` 与 GET/DELETE 按设计拒绝；默认跑零网络往返（探针除外）。
2. 缺失/空白 profile → 400 常量；双并发客户端 profile 隔离成立。
3. 离环：tools/call 在远小于 30s 完成（死锁变异体 oracle）；重启后客户端可恢复。
4. 凭据 canary 四不出（log/audit/响应体/console 载荷）；中间件数 pin 不破；路由 pin 恰加 `/mcp`。
5. 插件 remote 条目含 `oauth:false`+显式 timeout；旧 stdio 条目形状迁移且 manual 条目不动；真 OpenCode 探针通过并留证。
6. 门禁全绿：`pwsh -File scripts/gate.ps1`（pytest/ruff/format/mypy/changed comments）。

## 纪律
隔离（spare port + 临时 `MNEMOSEED_LOCAL_HOME`；禁 7788；禁碰安装运行时）；TDD 先 RED；DRY 最小 diff；英文代码注释且无禁词（注释政策前科）；提交在批次分支，不推不 PR；stop rule：若 tools/call 无法在不改 `call_tool` 的前提下离环（即需要 async 孪生 dispatch），停并上报，不得破坏 DRY 平价。
