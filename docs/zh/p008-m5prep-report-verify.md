# P008 M5-prep 盲报告校验器

仅为合成 canonical-first 工程参考。

## API

`verify_blind_report(report: object) -> list[str]`，位于
`src/mnemoseed_local/eval/m5prep_report_verify.py`。
对内存中的盲报告做纯零 I/O 检查，仅导入 `json`、`re` 与冻结的
`canonical_sha256`。

## 有序 findings

命中第一条即返回；成功为 `[]`：

1. 报告不是 dict 时返回 `report-not-mapping`。
2. 缺少自身键时返回 `report-missing-key:canonical_sha256`。
3. 存量值不是小写 `[0-9a-f]{64}` 字符串时返回 `report-bad-canonical-field`。
4. 已存在的 `files_read` 不是 `list[str]` 时返回 `canonical-malformed:files_read`。
5. `json.dumps(stripped, sort_keys=True, allow_nan=False)` 抛出
   `TypeError`/`ValueError`，或冻结的 `canonical_sha256` 抛出同样异常时，
   返回 `canonical-unserializable`。
6. 重算摘要与存量值不一致时返回 `canonical-mismatch`。
7. 一致时返回 `[]`。

校验器浅拷贝输入 dict 后弹出自身的 `canonical_sha256`，永不修改调用方。

## 规范化范围

JSON 成员顺序与空白不影响摘要。
`started_at`、`duration_s`、`out_path`、`isolation.home` 与
`files_read` 的 OS 原生路径前缀属于规范化范围：仅文件名基名与
`<isolated-home>` 占位符参与计算。反斜杠路径不具备跨平台可移植性，
仅按基名比较。

任何受保护变更都会 mismatch：`score`、`sdt`、`claims`、`seal`、
`note`、`ops` 计数器、`isolation.home_writes`、新增键或删除键。

## 局限

`[]` 仅表示校验和一致，不表示 schema 有效、分数正确、来源闭合或
真实性。正确重算哈希的恶意对象无法被本检查发现；不可变产物字节哈希
仍是真实性锚点。

不表达任何认知主张、质量门槛、阈值或结论。

## 状态

M5 未批准。M6 已锁定。#113 与 #75 仍开放。
不用于生产，不合并。
