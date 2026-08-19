# PRD · B2.2 崩溃耐久（crash durability）：宿主会话史重生回放（单机制）

> 依据：
> - B2.1 基线修正④收口的用户质询（2026-08-19）：QA-4 的 lifespan drain 只盖体面退出，硬崩溃（断电/系统 crash/杀进程）时内存状态机照样全灭——"重启后的 daemon 有能力继续做相关动作吗"；
> - 用户工程原则（2026-08-19）：KISS、做减法、后台处理高度优化（速度/资源/token 三维）、模块化、环境可复原（随时拆除即回到现状）；
> - 既有设计拍板：跨通道去重兜底"宁可重复摄入由近重复检测吸收，也不丢"（design/01 §4.5）；
> - B2.1 基线修正②后的既有节律：opencode 每个 idle 都 flush drain，硬崩溃暴露面已压到 ≤ 当前进行中的一轮。

## 理论锚

**本功能不借用任何心理学/神经科学理论**——它是确保核心功能正常运作的纯工程可靠性层（崩溃恢复），不属于记忆功能设计，理论锚纪律的"不借清单"照常适用：不得把任何耐久性工程手段包装成记忆理论动机。唯一援引的是一条工程性事实（非理论）：**OpenCode 宿主自身持久化完整会话史**（`client.session.messages` 可读回），daemon 的捕获是宿主会话存储的派生视图——源头不丢，视图即可重建。

## 设计判据（KISS 减法定案，先砍后立）

| 候选机制 | 判定 | 理由 |
|---|---|---|
| daemon 侧 WAL 日志 | **不做** | "daemon 已接受 POST、未及 drain 即崩"的窗口经 idle-flush 节律已 ≤ 一轮；一整套日志+水位簿记盖半轮数据，成本收益不成比例（残余缺口如实声明，见边界） |
| hook 侧 spool（双写+ack 协议） | **不做** | 最重的候选；防御面与重生回放重叠 |
| idle-flush debounce | **不做** | senior QA 已否决（QA-8） |
| **宿主会话史重生回放（单机制）** | **做** | 唯一的恢复机制；源头（宿主存储）本身持久，回放即复原 |

## 范围（批次任务）

- **T1 hook 水位线（ack 钟，非发送钟）**：`~/.mnemoseed-local/hook-watermarks.json`（`{session_id: last_acked_ts}`）——推钟只发生在 POST 收到 daemon **2xx** 的回执里（fire-and-forget 的 `.then` 链），发出≠到达；宕机期间水位停摆在最后被确认的轮次，重叠窗口绝不会吞掉未达内容。刷写 = 各 cadence 点（idle/error/deleted/compacting）**原子落盘**（tmp+rename，崩溃撕文件不毁上一好版本），读写故障一律吞没降级，绝不侵入主链路。
- **T2 会话级重生回放**：hook 进程内**每个 session 首次见到其事件时**（惰性，不扫全宿主）执行一次对账：`client.session.messages` 拉该 session 宿主侧消息史 → 映射为既有 ingest 载荷（ts 取 `info.time.created`/`completed`）→ **只重放 `ts > 水位 - 30s` 的尾部** → 按时间序逐条 POST。**到达序保证 = 每 session 一条 FIFO promise 链**：replay 段插队在该 session 一切 live 内容之前（分片器按到达序切 turn，乱序即错绑），replay 成功才标 reconciled（失败留待下次事件重试）；replay 的 assistant 与 live 通道共用指纹守卫 + 成功即解 pending。**无水位 session 跳过回放**（特性前历史本就不可重放；跳过决策进 debug lane）。
- **T3 回放幂等性钉死（双层测试）**：daemon 侧 e2e——crash 前的尾部在 daemon 重启后重放**零新增 chunk**（近重复吸收），live 新轮照常入库；hook 侧 node 行为挂架——esbuild 打包真插件 + node 驱动假 SDK/假 fetch，钉死 ack 钟（拒收不推钟、到达才推钟）、replay-先于-live 到达序、同消息跨通道只入一次。
- **T4 工程约束钉死**：(a) **token 红线**：回放路径全程无 LLM（静态 pin）；(b) **热路径零新增**：chat.message/ingest 同步段无任何 await 的新增 I/O（队列入队为内存操作，静态 pin + 行为挂架验证 void 形态）；(c) **环境可复原**：全部新增物 = 单水位文件（POSIX 安全根：`MNEMOSEED_LOCAL_DATA_DIR` > 平台 home），删除后行为回到无回放原型。

## 边界（如实）

- **残余缺口（放弃 WAL 的代价）**：daemon 接受了 POST 但崩溃在 drain 之前、且宿主侧尚未走到下一轮 idle——最多丢当前一轮。加上一条 ack 钟特例：daemon 持续 **202 但永不 drain**（flush 链路坏死）时，acked 轮次不会被重放——属另一类故障，如实声明，不归本批。
- **水位是 ack 钟的如实语义**：水位=最后一次 daemon 2xx 确认的 ts（含 cadence 未落盘的最近 ack——崩溃前最后一个 cadence 内的 acked 轮次会被多重放，由近重复吸收，代价良性）；到达序保证是**派发序**（loopback 亚毫秒级乱序实践不可达），各内容通道经 per-session FIFO 链串行；replay 失败不置位（下次事件重试）+ ingest 被拒即解除 reconciled（宕机空洞无法被后续 ack 跳跃覆盖）。
- **吸收界限（如实）**：近重复吸收对"切分一致的字节级重放"是确定性的；切分不同的重放（compacting 中途 flush 的 user-only chunk vs 回放合成的 user+assistant chunk）可能落一条内容重复的 chunk——容忍（噪声非丢失），召回面代价有限，不设计去重加强。
- **无水位 session 的降级方向（如实）**：跳过 = 该类 session 在"特性启用后、首次成功持久水位前"的 crash 尾巴不可回放，损失以最后一次成功持久为界；与"全量重放"相比判定为 KISS 可承受。
- **回放尾巴会作为最新内容浮在 `/session/recent` 尾部**（按 ingested_at 排序）——但窗口本就 ≤ 一轮 + 重叠，误导面有限；更严格的按事件 ts 重排不做（成本）。
- 多 opencode 实例并发同一 session 不设计（单用户桌面现实），冲突由近重复吸收兜底，如实记录。

## 门禁（不变）

TDD（先红后绿）→ 对抗 QA 复审（无 BLOCKER 方可收口）→ 全量门禁（`uv run pytest -q` / ruff / format / mypy）→ issue → branch → PR（`Closes #N`）→ merge。

## 批次执行记录（随批追加）

### 首轮 QA 复审（2026-08-19）：NOT CLOSABLE —— 2 BLOCKER 已修

- **BLOCKER-1（水位=发送钟）**：noteWatermark 原在 POST 发出同步段执行——fire-and-forget 语义下 30s 以上宕机 = 无限量静默丢失，PRD 边界宣告直接失真。修复：水位只在 daemon 2xx 回执里推进（ack 钟）；node 行为挂架场景 `ack-watermark` 钉死"拒收不推 / 到达才推"。
- **BLOCKER-2（live/replay 到达序错绑）**：reconcile 先于内容 POST 发出，但内部 await（文件+SDK）保证 live 必先到——旧 user_prompt 中途切 turn 且产出 `ended_at < started_at` 倒置区间。修复：每 session FIFO promise 链串行一切内容投递（replay 段先于 live），行为挂架场景 `replay-before-live` + `assistant-dedup` 钉死。
- **随批修（IMPORTANT）**：POSIX 路径根 + `MNEMOSEED_LOCAL_DATA_DIR`、tmp+rename 原子写、无水位跳过进 debug lane、`textOf` 滤 `ignored`/`synthetic` 部件、replay tool_use 带原事件 ts、reconcile 失败不置位（下次事件重试）、replay assistant 指纹守卫 + 成功解 pending。
- **复审 Round-2（2026-08-19）：0 BLOCKER、2 个同族新 IMPORTANT 已修**：NEW-1（道中宕机空洞：拒收后的下一个 ack 会把水位跃过空洞窗口，reconciled 一旦置位本进程永不再对账）→ POST 新增 `nack` 通道，ingest 被拒/失败即解除该 session 的 reconciled 标记，下一事件自动按最后 ack 水位重放（行为场景 `outage-hole` 钉死"重放先于恢复轮到达"）；NEW-2（replay assistant 指纹在 POST 发出时置位、TOP 前置 unpark——单发失败即真丢失）→ 指纹置位保留（抑在飞重复）但 nack 回滚指纹并重挂 pending。另有 NIT-6（persist tmp 唯一后缀）、NIT-7（session.deleted 的 info.id 幻影键——优先 info.sessionID）随批修；其余 NIT（派发序 vs socket 序、链长、秒级 tmp 冲突已修外余项）评价后记录在案不阻收口。
- **T4 红绿灯形态（如实调整）**：原 T4(b)/(c) 承诺的"基准对比/删文件行为测试"超出静态 pin 能力——以"静态 pin + node 行为挂架"替代交付（挂架本身就是对"TS 裸奔"的结构性回应），如实在本节声明。

## 收口记录（2026-08-19）

- 收口 commit：`c9040ac`（PR #4 → Closes #3，squash 合入；7 文件，+822/-74）。收口时 1199 passed / 3 skipped，ruff/format/mypy 干净；合并后 main 全量复验 **1200 passed / 3 skipped**，三大静态门禁全净。
- TDD 批次：T1（ack 水位）+ T2（per-session FIFO 重生回放）+ T3（双层幂等：daemon e2e 零新增 chunk + node 行为挂架）+ T4（工程红线：token 零 LLM / 热路径零新增 I/O / 删水位文件可回退，静态 pin）同包交付。
- 对抗 QA 两轮：首轮 NOT CLOSABLE——2 BLOCKER 打中要害（发送钟水位、live/replay 到达序错绑），修复并以挂架场景 `ack-watermark` / `replay-before-live` / `assistant-dedup` 钉死；复审 0 BLOCKER，揪出 2 个同族新 IMPORTANT（NEW-1 宕机空洞被后续 ack 跳跃覆盖 → post 增 nack 通道解除 reconciled；NEW-2 replay 指纹发出即置位 → nack 回滚指纹重挂 pending）与 NIT-6/7（tmp 唯一后缀、幻影键优先序）随批修净，补行为场景 `outage-hole`（挂架 4 场景 36 断言全绿）。
- 装机同步：`uv tool install --force .` 自合并后 main 重建，daemon 换新构建重启（/healthz gate ok）；`hook install` 后 `hook status` 报 match（deployed 与 shipped 逐字节一致）。opencode 插件仅启动时加载——2026-08-19 14:23 之后启动的宿主进程已在跑 B2.2 版本，老进程一次重启即激活。
- 边界声明保持"边界（如实）"节原文不松弛：WAL / hook spool / debounce 的不做判定、残余 ≤ 一轮缺口、ack 钟 cadence 内 acked 轮次的多重放由近重复吸收。
- 下一刀候选（路线图）：B2.1 挂起的 T1 起始回放、T2 中段 auto-recall（T0 探针已于前批观测完成：注入面定案 system.transform 追加）；B4 前置候选 qwen3.5:9b verify 席零产出排查。
