# PRD brief: recall 热路径 P0 延迟修复(record_hits 异步化 + index_residue TTL 缓存)v1

- **状态**: CANDIDATE — 由两轮实测钉死的问题定义(非猜测),设计门随批 QA。
- **实测依据**: `eval/2026-11-28T04-18-07Z-latency-drilldown.{json,md}`(wire 5,976ms:retriever 1,013 / assemble 682 / **record_hits 3,078(52%)** / record_served 4.5 / **index_residue 1,256(21%)**,残差 −58ms 闭合)与 `eval/2026-11-27T17-13-41Z-corpus-baseline.*`(90 探针)、`eval/2026-11-28T02-19-36Z-corpus-latency-profile.*`(embed 123ms/ANN 837ms——排除模型与向量库)。

## 修复 1:`_record_hits` 兑现自身的 fire-and-forget 契约
`memory.py:870-881` docstring 明言 "fire-and-forget, never failing or blocking recall",但实现同步执行:每次 recall 对 ≤8 个 served chunk 逐个 `get_chunk`(LanceDB 4.8GB 点读)+ `update_weights` 批写 → 实测 3.1s/次。
**机制**:MemoryService 增设单一后台强化工作线程 + 有界队列(maxdepth 64,drop-oldest + 溢出计数 + 一次性 warning);`recall()` 只入队即返回;service `close()` 时 flush(join 带 timeout)+ 剩余项同步落盘。语义:**强化是 best-effort**——突发下丢弃部分强化事件是可接受降级(记忆衰减略早),由溢出计数披露。`_record_served`(消费台账)保持同步(实测 4.5ms,且是账本完整性要求)。

## 修复 2:`_index_residue` TTL 缓存
`memory.py:1024-1049` 每次 recall 全量扫 `_RESIDUE_SCAN_LIMIT` 条最新 chunk 找 dead-zone pin → 实测 1.26s/次,而 pin 集合仅在 `remember()`/decay sweep/reinforce rebound 时变化。
**机制**:per-profile TTL 缓存(60s,clock 可注入),TTL 内直接返回缓存块;`remember()` 显式失效(新 pin 立即可见);报告字段加 `cached: True/False` 与 `cache_age_s` 诚实披露滞后。residue 本身是 Koriat 式 feeling-of-knowing 钩子(design/09 §3.6),60s 滞后不损害其语义。

## 理论锚
无新锚、不改既有锚:纯机制性能修复(异步化兑现既有 fire-and-forget 契约 + memoization),不触任何检索语义/评分/排序。明确不借:无。

## 验收
1. TDD:两修复各有红→绿测试(异步化:recall 先于强化落盘返回 + close flush 完整 + 有界丢弃;TTL:TTL 内不重扫(spy 计数)+ remember 失效 + 过期重算)。
2. 实测:drilldown 前后对照——record_hits 离线后 wire 应降至 ~2.9s 以内(index_residue 缓存命中后趋近 0);基线探针集不回归。
3. 全门禁绿;独立 QA CLOSABLE;PR 合并。

## 收口记录(2026-11-28)

- **修后实测**(`eval/2026-11-28T05-33-26Z-latency-drilldown.*`):wire **5,976 → 2,259ms(−62%)**;record_hits 离线(0ms 入队);index_residue TTL 命中(p50 0ms,含一次 ~550ms 重算);新主导 = retriever 本体 1,352ms(embed 123 + ANN 837 + graph 轨道)——后续批次可选目标。
- **QA round 1 判词 CLOSABLE(0/2/7,两 IMPORTANT probe 实证)→ 已修**:IMPORTANT-1 stamps 直通使 drain 窗口内的重复命中合并为单次 rebound(0.5→0.6,旧路径 0.6→0.7)——decay 动力学的小幅披露性变更,已在 reinforce.py docstring 记录(顺带近似了 FR-4.2 刻意未实现的 spacing-effect cooldown);IMPORTANT-2 enqueue-after-close TOCTOU 静默丢失 + 二次 close 15s 停滞 → 已修(after-close 丢弃计数 + close join 后 drain-and-count)。
- 门禁终态:5487 passed / 23 skipped,ruff ×2,mypy 199 files;修后加 7 项新测试全绿。
