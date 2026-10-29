# #299 仓库预检与遗留处置记录

日期：2026-10-28。范围：仅记录本仓库的保全、遗留归属与整理结果。本批为维护性变更，当前产品源码及行为不变；不恢复旧候选，不启动 reconciliation，也不将未知历史执行宣告为已关闭。

## 已记录的处置事实

- 未跟踪的私人组织研究副本已逐字节保全，迁移前后 SHA-256 一致，产品文档中的原副本已移除。本记录不收录其内容或私人组织信息。
- 旧本地分支的补丁已通过 [PR #297](https://github.com/MnemoSeed/mnemoseed-local/pull/297) 落地；删除前已创建并验证 Git bundle。已核实为空的孤立目录已移除。
- 五个旧 stash 全部保留并有归属证据：前四组完整文件集可在 main 历史找到；第五组的三个场景已存在于当前代码，其中 header 保全覆盖更强。未回灌或覆盖当前源码。
- 26 个救援文件保留原始存档，分类如下；分类表示历史归属，不替代本批独立复核。

| 分类 | 件数 | 处置 |
| --- | ---: | --- |
| landed-current | 13 | 当前版本已有，保留存档 |
| landed-history | 3 | main 历史已有，保留存档 |
| superseded-comment | 2 | 仅旧注释差异，不回灌 |
| superseded | 4 | 已被后续功能或修复取代，不回灌 |
| explicitly-pending | 4 | 归属既有 [#220](https://github.com/MnemoSeed/mnemoseed-local/issues/220)，继续保留候选 |

## 明确保留的待办边界

- [#220](https://github.com/MnemoSeed/mnemoseed-local/issues/220) 的四件候选仍待独立审查；本批不声称其实现通过 QA、已合并或已完成。
- [#179](https://github.com/MnemoSeed/mnemoseed-local/issues/179) 的源码与 tag 已落地；PyPI trusted publisher 及默认索引安装证明仍阻塞，不能据此声明发布验收完成。
- [#271](https://github.com/MnemoSeed/mnemoseed-local/issues/271) 的观察边界仍待完成。
- [#294](https://github.com/MnemoSeed/mnemoseed-local/issues/294) 的 refinement v2 仍处于设计阶段，不表示设计 QA 或实现验收已通过。
- 历史 pending/unknown 记录按原事实保留；本批不激活 reconciliation，不重放、不虚构历史执行关账。

## 验收契约（编写时仍待完成）

本记录依据已冻结的整理结果编写。完整本地门禁、独立实现 QA、PR 必需 CI、合并和主控验收仍是后续接受条件；工作席完成不等于独立验证或最终接受。此次可逆文档与整理变更不新增测试。

冻结的验收命令为 `pwsh -File scripts/gate.ps1` 和 `git diff --check`。合并并同步后，最终 main 必须干净（`git status --porcelain` 无输出）且与 `origin/main` 相等；不得遗留本批未合入改动。上述条件在编写时均不预先声明通过。

本批不引入功能设计或理论借用，理论锚无新增。
