# PRD · Phase B2 时序接续面

> 依据：`docs/zh/prd/PRD-B2-roadmap.md` 批次 B2 + 2026-08-18 当日实证（新 session 开头用户须手贴上一 session 引用才能对接）。"能够按时间判断前一 session 最后沟通内容"是已拍板的重要体验。
> 基线：commit `c53e3b4`，1073 passed / 3 skipped。
> 门禁：TDD；子代理从不提交；全量门禁收口（`uv run pytest -q` / ruff / format / mypy）。

## 语义定版（本包拍板，写死进测试）

1. **只读、verbatim、零推断**：端点不做"该 session 是否已结束"的启发式判定——返回**最近 K 个 session（按最近 chunk 时间倒序）各自的尾部 N 条 chunk 原文**，由调用方（agent/用户）自行识别哪个是当前会话（当前会话通常就是那个还在增长的最新一组）。宁给两组的诚实冗余，不给一组的武断截断。
2. **组内升序、组间倒序**：每组 chunk 按时间/turn 升序排列（阅读序）；组间以各组最新 chunk 时间倒序（先看到"最近"）。
3. **数据源零改动**:`vector.list_chunks(profile)` 已是 `ingested_at` 倒序（lancedb 驱动钉死）——服务层只取足够页框内存分组，不动存储层、不动 capture/dream 任何核心链。
4. **体量护栏**：页框 `min(sessions_cap × per_session_cap × 4, 500)` 上限拉取，防长会话稀释；`per_session` 默认 20（≤100）,`sessions` 默认 2（≤5）。
5. **隐私/红线**：一切本地；actor 仅审计常规 read 面（与其他 GET/POST 读同例，不新增审计键）。

## 任务 T1 · daemon `POST /session/recent`

- 范围：
  1. `MemoryService.session_recent(profile_id, *, per_session=20, sessions=2)`：按定版语义分组返回。chunk 行只带读侧字段：`text`、`ingested_at`、`turn_start`、`turn_end`、`chunk_id`。
  2. 端点 `POST /session/recent`,pydantic 请求体 `{profile_id (required), per_session?, sessions?}`，非法参数照常 422。
- AC：两组/单组/空 profile/上限截断/组内升序/组间倒序六枚用例；daemon 集成（两 session 各 ingest+end 后取回，verbatim 原文命中、顺序正确）。

## 任务 T2 · MCP 工具 `recent_sessions`

- 范围：
  1. `tools/list` 增 `recent_sessions(n_sessions?, n_per_session?)`（schema 与端点同键名）;
  2. `tools/call` 映射 → `POST /session/recent`;daemon 不可达照 isError 先例。
- AC：工具名列出、参数透传正反用例；握手序列测试同步更新。

## 完成定义

- 两任务 QA 过；全量门禁干净；单 commit 收口：`phase B2: session recent surface -- daemon endpoint + MCP recent_sessions`。
- 收口后在 `PRD-B2-roadmap.md` 的批次启动记录回填。远期"hook 自动注入"形态（依赖宿主插件上下文注入能力验证）不在本包，存路线图挂起项。
