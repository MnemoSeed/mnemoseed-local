# 分片 381-B7-PR3-slices v4

> 状态: DESIGN CANDIDATE v4 / DESIGN_PENDING。配合 design v4 阅读。硬序: B-pre -> S5(B-S5a/B-S5b) -> S5c(新写路由) -> S6(B-S6a/B-S6b/S6c)。S7 OAuth 不在本表。
> 相对 v3 变更: 新增 B-S5c(第五路由 + SecretStore 实例接线)、S6c(向导 secret-first 顺序 + 读侧 RoleRouter 接线);冻结集从 +4 到 +5;其余分片、I-B4 强制门、红相要求继承 v3。
> 所有 v3/v4 文件为新文件,不覆盖 v1/v2/v3/判词。

## 分片表(v3 全保留 + v4 新增)
| 片 | 产物(写集) | 依赖 | 验收门 | 备注 |
|---|---|---|---|---|
| B-pre | 无编辑: 对合并 main 5001470 重取全部基线哈希 + 重钉行号锚点 | #432 已合 | I-B5 关闭 | 语义 SET 权威、字面行号非契约 |
| B-S5a | 新 llm/routes.py(四端点 + key 脱敏白名单 + 有界超时);daemon/app.py 一行 include_router | B-pre | S5-AC1/3/4/5/6 | B-1 修复由工程并行、本表不改其机制 |
| B-S5b | test_dream_status_payload.py HTTP 清单 +4 行;design/04 §1.4 +4 行;新 tests/test_llm_routes.py | B-pre,B-S5a | S5-AC1/2;四 sink grep | MCP 工具名集/页路由集不变 |
| B-S5c(v4 新增) | 新 daemon/secrets_routes.py(POST /api/v1/secrets/set);daemon/app.py 第二行 include_router + create_app 的 secret_store 注入缝 + RoleRouter secrets= 实参;新 tests/test_secrets_routes.py;test_dream_status_payload.py 冻结清单 +1 行;design/04 §1.4 +1 行 | B-S5a,B-S5b | 新四 sink grep + 变异 M1-M4;401 默认拒绝;名称允许表;读侧解析证明 | 不放进 llm/routes.py / configwrite / secrets 包;不改 auth.py |
| B-S6a | app.js + index.html config 页向导(radio 无预选/probe/可搜索清单/set-many 绑定/隐藏 ollama 字段/真实 driver 提示) | B-S5a,S5b,S5c | S6-AC1/3/4/5/6/7/8;Web D gate 1-4 | 不新增页路由 |
| B-S6b | 新 tests/ts_hook/provider_preselection.mjs + pytest 包装 test_console_role_batch.py::test_preselection;隔离 Playwright 手检 | B-S6a | S6-AC1 强制机器 oracle;S6-AC7 | 全 radio unchecked;非 if added;node 缺失 RED 不 skip |
| B-S6c(v4 新增) | app.js wizardBind(): secret 写先于 set-many、成功后才写 ref、成功后才清空输入 | B-S5c,B-S6a | S6-AC2 字面满足;ref/name 映射;O-v4-1 读侧证明 | ref 固定 secrets:mnemoseed/dream/dream;名 mnemoseed/dream/dream |

## 强制门精确命令(I-B4 保持,必须逐字执行)
    uv run pytest -q tests/test_console_role_batch.py -k preselection
禁止把 .mjs 直接喂给 pytest。包装固定 argv: subprocess.run([node, tests/ts_hook/provider_preselection.mjs, src/mnemoseed_local/console/static/app.js], check=True, capture_output=True, encoding=utf-8, timeout=120);断言 returncode==0 且 stdout 含 provider_preselection.mjs OK。

## 冻结集增量
- pre-W1-B(main 5001470): 34 条 HTTP path(逐条见 w1b-v4-refreeze-delta.md §2)。
- post-S5: 34 + 4 LLM = 38 条。
- post-v4: 38 + 1 = 39 条,新增成员恰为 POST /api/v1/secrets/set。
- 精确相等断言仍在 tests/test_dream_status_payload.py;MCP 工具名集与页路由集断言不变。

## 红相要求(TDD,v3 全保留 + v4 追加)
1. probe key 出流 mutant(错误详情/异常 str 回显原始 key 或 key 嵌 base_url query): 四 sink grep RED。B-1 model 字段红相由工程并行修,设计不改。
2. 冻结清单 mutant: 不加入五路由: test_external_surface_inventory RED。
3. S6 绑定 mutant: 逐键 config/set 或 model-first: 调用序列断言 RED。
4. 预选 mutant: 默认选中某 provider radio: 包装门 RED(default effect 锚)。
5. 强制门 mutant: 删包装测试或把 node 不可用改 skip: RED。
6. v4-M1: 成功响应加 masked_tail: response grep RED。
7. v4-M2: 错误 detail 拼 str(exc): response/exception grep RED。
8. v4-M3: Pydantic value 模型触发 422 回显: response grep RED。
9. v4-M4: 删 daemon/credential 名称守卫: 名称测试 RED。
10. v4-M5: 向导先 set-many 后写 secret,或写入未 provisioning 的 ref: 顺序断言 + 读侧解析(api_key 空串)RED。
11. 每红相留 raw 失败行 + 字节精确恢复证据。

## 依赖与文件面冲突检查
- daemon/app.py 同时被 B-S5a(include_router llm)、B-S5c(include_router secrets + create_app 缝 + RoleRouter secrets=)写: 必须同工作树串行 S5a -> S5c,不得并行(同文件面)。
- app.js 被 B-S6a 与 B-S6c 写: 必须同工作树串行 S6a -> S6c。
- tests/test_dream_status_payload.py 被 B-S5b 与 B-S5c 写: 同工作树串行,合并为一次 +5 行编辑更稳。
- tests/test_secrets_routes.py 仅 B-S5c 写,与其它分片文件面不相交。
- secrets/store.py、refs.py、config.py、configwrite/service.py、llm/routing.py 本轮不改(共享槽正则扩张明确排除)。

## 提交与合并
- 批次: B-pre -> S5(B-S5a+B-S5b) -> S5c -> S6(B-S6a+B-S6b+S6c);同 worktree 串行、issue -> branch -> PR(Closes #381) -> CI -> merge;writer_claim 在移除 worktree 后释放。