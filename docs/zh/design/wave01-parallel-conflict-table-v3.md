# Wave 0+1 并行工程批冲突对账表 (v3)

> Status: IMPLEMENTATION EVIDENCE. v3 upgrades v2 after PR #432 merged (base `500147037ff97fd1a9f25f8e42e50b375ed4d9bd`). Machine-checkable conflict accounting only; no new engineering authorization.
> v2 不覆盖、不改写。v3 关闭 round-3 I-B5（重钉行号）/ I-B6（node-missing 假绿红相）/ N-B4（S6 包装测试入文件面）。
> 语义 SET 权威，字面行号仅为导航定位（I-B5 裁定）。

## 1. File surface / test-oracle / ordering matrix (v3)

| Item | Primary write surface | Test-oracle write surface | Overlaps with | Nature | Verdict |
| W1-B S5 #381 | NEW llm/routes.py, daemon/app.py(+1 include_router), docs/zh/design/04 §1.4 | test_dream_status_payload.py HTTP 清单 +4;NEW tests/test_llm_routes.py | PR #432(同 frozen HTTP 清单) | same file same list | #432 merged first; merged bytes re-pinned |
| W1-B S6 #381 | console/static/app.js, index.html, style.css | NEW tests/ts_hook/provider_preselection.mjs;NEW tests/test_console_role_batch.py::test_preselection(pytest 包装,subprocess 调 node);reuse console_role_batch.mjs | PR #432(same app.js role-bind);S5(endpoints) | same file adjacent logic + runtime dep | #432 first, S5 before S6;包装测试写面已登记 |

（W1-A #440 / W1-C #390 / W1-D #441 行同 v2，本片未改，略。）

### Frozen shared oracles (v3 re-pinned on merged bytes)

1. `tests/test_dream_status_payload.py::test_external_surface_inventory_equals_frozen_baseline`: HTTP path 集(`:489-519`; set-many 在 `:499`; S5 四路由加入后集合扩展)、MCP tool-name 集(`assert tools` 在 `:523`)、console page-route 集(`assert page_routes` 在 `:529` = {overview,atlas,config,profiles,dream})一起钉。`app.js` 的 `Number.isNaN` 数值校验不是路由集锚点。v2 的 `:528`/`:520-522`/`:489-518` 在 #432 合并后漂为 `:529`/`:521-523`/`:489-519`;语义 SET 未变,字面行号非契约。
   - S5 四路由: `GET /api/v1/llm/catalog`, `POST /api/v1/llm/probe`, `POST /api/v1/llm/models`, `POST /api/v1/llm/detect-local`。
   - S6 向导是 config 页内 modal,禁新增 `#/wizard` 页路由,否则 page-route 集破裂。
2. `tests/test_daemon_caller_credential_guard.py` 扫描受保护路由的凭据获取;S5 四路由继承 loopback + 既有凭据门,不改该文件但须过扫描。
3. R1-R4 同 v2:fence.py/plugin.ts 字节一致;MAX_CHARS=1800;S6 config 页 modal;G0-4 README 对账延后。

## 2. Parallel groups and serial chains (v3)

- 强制串行: `#432 → S5` (同 frozen HTTP 清单文件) `→ S6` (S6 调 S5 四端点;PRD-B7 §4 声明 S5/S6 串行)。S5 与 S6 在同一 worktree 顺序链,符合本派发单任务真序依赖。
- 无同树并行冲突:W1-B/S6 包装测试写入 `tests/test_console_role_batch.py`(#432 已合,无并发写者);S6 的 `.mjs` 为新文件,不与 W1-A 的 TS plugin 文件面重叠。

## 3. Design QA round-3 finding status (v3 closures)

| ID | round-3 状态 | v3 关闭方式(实施期) |
|---|---|---|
| I-B4 | CLOSED(机制) | 强制门 `uv run pytest -q tests/test_console_role_batch.py -k preselection`,包装内 `subprocess.run([node, DRIVER, APP_JS], check=True, ...)`;实测绿相 `1 passed`、无匹配 `exit 5` |
| I-B5 | IMPORTANT → 实施期关闭 | 本表把所有行号重钉到合并字节: page-route `:529`, tool `:523`, HTTP set `:489-519`;语义 SET 权威 |
| I-B6 | IMPORTANT → 实施期关闭 | 新增 node-missing 红相: PATH 去 nodejs 后真门 `1 failed`/exit 1;反事实 `pytest.skip` → `1 skipped`/exit 0(假绿),故 oracle 断 `1 passed` 非 skipped |
| N-B4 | NIT → 本文件关闭 | 冲突表升 v3;S6 包装测试 `tests/test_console_role_batch.py::test_preselection` 已入第 1 节文件面矩阵 |

## 4. Remaining open risks

- R1(关闭): #432 已合,S5 +4 在合并字节重做并全绿。
- R6(登记): README:47-53 catalog 矛盾为 G0-4,待 S6 合并后对账,不在本片 AC。
