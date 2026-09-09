# P-008 M5prep 评定方法（仅定义基础设施；此处不提出任何数值）

状态：本文仅定义未来 M5 评定的方法。此处不提出任何数值、不设定任何
判定标准，也不主张 M5 达成、生产启用或任何议题的关闭。

## 1. 未来的评定方法

未来的 M5 主张必须按顺序满足以下四项：

1. 与开发隔离的 sequestered 语料，在任何评定工作开始之前以双人复核
   seal 冻结并预先登记。
2. 恰好一次经登记日志路径的盲评。对同一 truth 的第二次评分需要重新
   编写并重新密封的语料。
3. 由 `report_sdt` 计算的 SDT 形态汇总：`fact` 与 `experience` 双通道
   计数、`correct_rejection` 计数、`misclass` 列表、
   `zero_reason_histogram`、confidence-decile 表格以及 `unscored_units`
   计数。仅为计数，不记录任何 verdict。
4. 产品门 P4 与独立 QA 裁决，之后方可形成任何主张。

## 2. SDT 形态（只有计数，没有评定线）

单元等价沿用 scorer 语义：归一化结构 payload 相等，或共享非空
`paraphrase_group_id`，始终以 class 与 scope 为界，一一匹配；predicted
evidence 必须覆盖 expected evidence id，否则该单元不算匹配。

- 匹配的 expected accepted 单元在其通道记一次 `hit`。
- 未匹配的 expected accepted 单元记一次 `miss`。
- 未匹配的 predicted accepted 单元（含 provenance 错误）在其通道记
  一次 `false_alarm`。
- 跨类 accepted 匹配记一条 `misclass`（`case_id`、`expected_class`、
  `predicted_class`），并在 expected 通道记一次 `miss`，任何位置都不
  记 hit。
- truth-zero 且 predicted-zero 的 case 记一次 `correct_rejection`；
  truth-zero 但 predicted 有 accepted 单元时按单元记 `false_alarm`。
- Confidence 仅透传：单位区间上的固定 10-bin 表格只覆盖 predicted
  accepted 单元；缺失、非数值或越界的 confidence 归入
  `unscored_units`。分 bin 不携带任何标准，也不掩盖 provenance 缺口。

## 3. 未来的数值提案必须包含的内容

未来的提案是供后续评审的方法说明，不是自动执行的结论。键必须仅取自
benign allowlist（`method`、`required_gates`、`forbidden_claims`、
`not_observed`、`report_fields`、`version`、`n_cases`、`min_cases`、
`corpus_size`）。`validate_bar_proposal` 会拒绝任何归一化后等于或包含
禁用 token 的键（例如 `target_p`、`cut_off`、`min_p`、`quality_gate`）。
allowlist 键之下的取值不受扫描。

## 4. 明确的非主张

- v1 语料保持 parser-conditioned，不进入评定证据。
- M5 NOT RATIFIED；M6 locked。
- Batch-1 留在其分支上，not merged；promotion remains locked，不主张
  任何生产启用。
- NOT_OBSERVED 主题，本批次均明确未观察：
  - natural-language extraction quality：NOT_OBSERVED
  - real model quality：NOT_OBSERVED
  - real-data coverage/eligibility：NOT_OBSERVED
  - live resource impact：NOT_OBSERVED
