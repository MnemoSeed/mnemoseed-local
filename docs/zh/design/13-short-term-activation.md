# 短期激活设计锚点

## 理论锚

ACT-R 的快速层与慢速层在本设计中只作为机制映射：快速层对应会话范围内的易失性激活，慢速层对应既有长期记忆检索与衰减字段。该映射不改变既有长期存储的语义，也不把理论参数当作生产默认值。

以下数值由 owner 于 **2026-09-20** 预注册（source: **pre-registered 2026-09-20**），不是可调参数；本阶段总开关仍关闭。

| 参数 | 数值 | owner | 日期 | validating eval |
| --- | --- | --- | --- | --- |
| 半衰期 | 10 分钟 | owner | 2026-09-20 | warm-needle delayed re-query |
| 单记忆最大加分 | base score 的 +15% | owner | 2026-09-20 | warm-needle re-relevance delta |
| 会话状态容量 | 200 条 | owner | 2026-09-20 | capacity eviction |
| partner rule | 只刷新已返回的同组成员，不扩池 | owner | 2026-09-20 | membership-preserving golden |
| refresh | refresh-not-stack | owner | 2026-09-20 | fixed-clock decay |
| failure | fail-open，零加分 | owner | 2026-09-20 | corrupt-state/failure isolation |

## 合同

- 激活只存在于内存快照，按 `(profile, session)` 隔离，并只用于已准入候选的排序加分。
- 它永远不参与过滤、准入、池大小、floor、rescue、配对或 top-k。
- 同一记忆再次命中时刷新时间，不叠加激活量；未实际命中的记忆不得刷新。
- 任何损坏、非法时间、非法数值或关闭状态都 fail-open 为零加分。
- memory ID 必须带 kind namespace，例如 `chunk:<id>` 或 `graph:<id>`。
- 操作只使用短时、非阻塞的字典操作；不访问 store、持久化、审计、遥测或 Reinforcer。

## 生命周期与边界

容量达到上限时淘汰最旧条目；重启后激活为空。快照不可变，调用方可安全地把同一快照用于确定性排序。

Recall endpoint schema 允许 additive optional `session_id`（默认 `None` 表示 bypass），向后兼容，不新增 route 或 tool。生产路径默认关闭，关闭时输出必须与当前基线逐字节相同。

## 验收标准

1. 关闭或无快照时，既有 recall golden 输出 byte-identical。
2. 固定时钟、固定快照和固定输入产生 byte-identical 输出。
3. 开启激活只能改变已准入候选的次序；成员、计数、pool、floor、rescue、配对和 top-k 不变。
4. 验证 profile/session 隔离、刷新不叠加、单调衰减、容量淘汰、重启清空和 fail-open。
