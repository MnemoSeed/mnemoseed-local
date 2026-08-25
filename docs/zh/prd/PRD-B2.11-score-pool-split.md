# PRD · B2.11 score-pool 持久余额与触发量表拆分

> 依据：
> - GitHub issue #94（2026-08-24 用户提问）：`profile_score_pool.balance` 同时充当 (a) 跨 dream 的终身积分账本和 (b) 重启后的触发量表种子——角色 (b) 让数字只涨不清，三次 dream 后读数 2.05 对 1.0 触发地板，"离下次 dream 还有多远"完全失真。
> - solution-architect 评审（2026-08-25）verdict：**SHIP-WITH-ADJUSTMENTS**——拆分方向正确；真正的 bug 比 issue 表述更具体：调度器触发的 dream 从不排水（自排水 tick 在服务路径零调用者，主导路径直读持久行后发射合成事件，无人扣减），重启又用终身总额做种子。

## 理论锚（无新增；工程记账诚实性）

- 本专题属数据诚实性修理（与 retention 重设计同族："queryable number answers honestly" 家规），无可借的经验规律；**不借**清单：任何"奖励池/多巴胺"式隐喻动力学——无实证基础，且会把记账问题伪装成建模问题。

## 设计定案（机制层）

### 语义拆分

1. **balance = 当前待触发量（gauge）**：调度器发射 dream 的路径原子排空该行——新增 meta 方法 `pool_drain(profile_id, window)` 单事务内：balance 归零 + 本次积分计入新账本列。带内触发路径（现 `pool_add` + `pool_credit(0)` 两笔事务，存在崩溃窗口）折叠进同一原子方法。
2. **filed_points_total = 终身账本**：`AddColumn(store="meta")` 新列 `profile_score_pool.filed_points_total REAL NOT NULL DEFAULT 0`，注册为全局迁移序列 **v9**；增量写入只在 `pool_drain` 内发生（单一写点）。
3. **重启种子恢复诚实**：boot 时 gauge 从 balance 播种（如今起即真实 pending，而非终身总额）。
4. **查询面**：扩展 `POST /memory/dream_status` 的 `pool` 块（注意：仓库无 `/memory/status` 端点，勿发明路由），additive 增加 pending + lifetime_filed 两个键；`dream status` CLI 打印器兼容不受破坏。

### 契约义务

- `storage/ports.py`：`PoolBackend.pool_drain` 协议成员 + `PoolState` 账本字段。
- `tests/contract/method_mapping.py`：补 `pool_drain` 映射行 + `test_contract_meta.py` 覆盖（契约套件强制 parity）。

### 清理义务（同批完成）

- `ScorePool.evaluate()` 服务路径死代码：**删除**（KISS；不留第二个含糊的触发权威）。
- 修正说谎的注释块（trigger.py 中"fired dream 消耗池子/持久余额归零"——仅对小众带内路径成立）与 `pool_state` 的 hedge 注释。

### 出界（v1 不做）

- 触发数学、地板值、调度时序零改动。
- 账本无分析面（内部持有，不加产品面）。
- 除正确的 gauge 种子外不做追溯对账。

### 测试预言

- 双 dream 回归：N 次调度器触发的 dream ⇒ balance == 0 且 ledger == Σ 积分；地板规则不能对已消费积分再触发（固化"同一积分永不二次触发"意图）。
- 重启 oracle：fired dream 后重启，gauge 种子为 0（非终身总额）；累积中途重启，数字以 pending 存活。
- 崩溃窗口：并发 oracle 镜像既有 `test_pool_add_atomic_under_concurrent_writers`，证明 drain+记账单事务。
- dream_status 载荷 additive：旧消费者断言不变，新键在场。
- 迁移 v9 渲染；旧行 born-empty 为 0。

## 批次执行记录

- **带内触发路径为两笔事务，而非字面单事务**：先以普通 `pool_credit` 镜像包含触发积分的完整余额，再以一次原子 `pool_drain`（归零 + 计入终身账本）完成拆分。旧的 `pool_add` + `pool_credit(0)` 对中"有害的"归零写已删除；崩溃窗口经排序变为良性——持久化始终先于事件投递完成，崩溃最多留下待触发积分（重启后恰好发射一次），绝不会让已发射的 dream 二次消费同一批积分。顺序由录制后端 oracle 固化（credit 先于 drain）。
- **残留窗口（本分支之前即存在，行为不变，现如实记录）**：若进程在 `pool_drain` 提交之后、sink 事件投递之前崩溃，则该批积分已入终身账本但没有 dream 启动——积分被消费且无对应 dream。修复该窗口需要跨"存储事务 + 进程内事件投递"的事性边界（outbox 类机制），超出本批范围。
- **`pool_add` 保留说明**：拆分后 `pool_add` 在生产路径零调用者（单一写点收敛于 `pool_drain`），作为存储 API 面保留、仅测试播种使用；契约套件继续覆盖。
