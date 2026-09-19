# 短期激活设计锚点

## 理论锚

ACT-R 的快速层与慢速层在本设计中只作为机制映射：快速层对应会话范围内的易失性激活，慢速层对应既有长期记忆检索与衰减字段。该映射不改变既有长期存储的语义，也不把理论参数当作生产默认值。

所有数值均为 **TBD-pending-owner-preregistration**，须在所有者预注册后才能注入测试或未来配置；本阶段不提供数值默认值。

## 合同

- 激活只存在于内存快照，按 `(profile, session)` 隔离，并只用于已准入候选的排序加分。
- 它永远不参与过滤、准入、池大小、floor、rescue、配对或 top-k。
- 同一记忆再次命中时刷新时间，不叠加激活量；未实际命中的记忆不得刷新。
- 任何损坏、非法时间、非法数值或关闭状态都 fail-open 为零加分。
- memory ID 必须带 kind namespace，例如 `chunk:<id>` 或 `graph:<id>`。
- 操作只使用短时、非阻塞的字典操作；不访问 store、持久化、审计、遥测或 Reinforcer。

## 生命周期与边界

容量、半衰期、激活量和淘汰规则均为 TBD-pending-owner-preregistration。容量达到上限时淘汰最旧条目；重启后激活为空。快照不可变，调用方可安全地把同一快照用于确定性排序。

会话标识是激活组件的普通参数；本阶段不改变 recall endpoint schema。生产路径默认关闭，关闭时输出必须与当前基线逐字节相同。

## 验收标准

1. 关闭或无快照时，既有 recall golden 输出 byte-identical。
2. 固定时钟、固定快照和固定输入产生 byte-identical 输出。
3. 开启激活只能改变已准入候选的次序；成员、计数、pool、floor、rescue、配对和 top-k 不变。
4. 验证 profile/session 隔离、刷新不叠加、单调衰减、容量淘汰、重启清空和 fail-open。
