# 真实语料 recall 质量契约评测面（S0 · issue #457）

> 本目录是**独立新增的离线评测面**：`python -m mnemoseed_local.eval.realcorpus_recall`。
> 不是产品 CLI verb，不进入 pytest 慢门禁，不触碰 `~/.mnemoseed-local`，不绑定 7788/4096。
> 与 #391 synthetic rig 物理分离；不导入 synthetic embedder / stub dream，不复用其 `TARGET_BARS`。

## 1. 状态与授权边界

- 查询集 `queries-v1.json` 是**草案（DRAFT）**，等待 owner 最终署名冻结；本批**不写冻结记录**。
- B1（miss_seed 1/1）与 B3（honest-empty 100%）为已 ratify 的纪律判据；B2 recall@8（提议 0.80）、B4 噪声界（提议 0.20）、离散界（提议 0.10）、N（提议 3）、p95 比值（提议 1.5）**均未 ratify**，只是可配置参数。
- 契约 FAIL 时内核不得发船（owner D3 硬门）；本评测面本身不自启 CI 门。

## 2. 四要素钉

`corpus_manifest.sha256` 是纯 JSON，钉住四个要素：

| 要素 | 含义 | 漂移后果 |
|---|---|---|
| `corpus_manifest_sha256` | 语料副本全树内容哈希（逐文件路径 + 大小 + 正文哈希） | `INVALID_RUN` |
| `config_sha256` | `config.toml` 快照 | `INVALID_RUN` |
| `embedder_model_version` | 真 embedder 驱动 + `models/` 内容哈希（非 synthetic） | `INVALID_RUN` |
| `code_revision` | 取语料副本时的 git revision | `INVALID_RUN` |

钉文件：`docs/zh/eval/realcorpus-recall/corpus_manifest.sha256`。任何一项不符抛 `InvalidRun`，报告写 `UNKNOWN`，绝不写成 PASS/FAIL。

## 3. 测量中立性

`MemoryService.recall` 会经 `_record_hits` → `Reinforcer.record_hits` 回弹 `decay_weight` 并写回 store，而 `decay_weight` 参与排序。因此：

- 保留不可变 pristine 基线；**每个冷启动 run 前**用 `restore_writable_state` 从基线原子恢复可写状态（`meta.db`+WAL/SHM、`chunks.lance` 权重/状态列）。
- 回放固定传 `session_id=None`。短期激活当前默认关闭且进程内易失（`activation.py:49,70`），无持久化状态可重置；这是前向正确性选择，不改变当前分数。
- 同一查询在 N 次独立重启中 `recall@8 / miss_seed / honest-empty / rank / tokens_used` 漂移必须为 0；每 run 记录基线哈希，不一致即 `INVALID_RUN`。p95 不作漂移判据。

两个变异体均已验证被杀（见提交证据）：
1. 产品绑定变异：把分数源改成自算 cue 重合 → replay 测试 RED。
2. 测量中立变异 A：跳过每 run 重置、复用写后副本 → 无漂移回归 RED（decay 回弹污染）。

## 4. 反 gaming

- `ingestion_verified=false` 样本不进 B2 分母，在报告中单列 diagnostic。
- 只有 `holdout=true` 的样本参与 bar 判定；`seen-only` 子集不能触发 PASS。
- B1 单独 must-pass；B3 100% 填充零容忍；无 UNKNOWN 才可能 PASS。
- 报告追加式（JSONL，一行一个 run），逐 query 含 `holdout/seen/ingestion_verified/hit/rank/tokens_used` 与整集判定。

## 5. 运行手册

```powershell
# 1) 量测全量副本并写四要素钉（唯一一次重活：约 4.8 GB / 5 万+文件）
uv run python -m mnemoseed_local.eval.realcorpus_recall copy `
  --source "$env:USERPROFILE\.mnemoseed-local" `
  --pin docs/zh/eval/realcorpus-recall/corpus_manifest.sha256

# 2) 校验某语料 home 是否与钉一致
uv run python -m mnemoseed_local.eval.realcorpus_recall verify `
  --home <copy-home> --pin docs/zh/eval/realcorpus-recall/corpus_manifest.sha256

# 3) 跑完整契约（N 次独立冷启动；报告只增不减）
#    推荐用 --home 指向第 1 步量测过的钉住副本，避免从 live 重复制时日志漂移。
uv run python -m mnemoseed_local.eval.realcorpus_recall run `
  --home <pinned-copy-home> `
  --queries docs/zh/eval/realcorpus-recall/queries-v1.json `
  --thresholds <optional-override.json> `
  --out <report.jsonl>
```

不给 `--home` 时 `run` 会先复制 live home 并校验四要素钉；任一漂移即 `INVALID_RUN`。

`run` 使用临时 `MNEMOSEED_LOCAL_HOME` 与内核分配的备用端口启动临时 daemon，跑完即停；live daemon 全程不动。
启动前把副本 `config.toml` 中指向 live home 的 `~`/绝对路径改写为临时 home（否则 isolated graph 会写回 live home），并读取该 home 的 daemon credential 调用 `POST /memory/recall`。
`--thresholds` 是 JSON 对象，可覆盖未 ratify 的提议值；已 ratify 的 B1/B3 无开关，始终强制。

## 6. 查询集（DRAFT）

`queries-v1.json` 的 confirmed miss seed 来自 2026-11-20 诊断记录并经 owner D1 确认：自然中文「把一部分 codex 的对话记录导入 mnemoseed 记忆里」召不回执行态记忆，存储词表「全量 import / rollout / 账本 / settle」可命 0.95–0.99。

诊断记录只给出 top-hit 分数，**没有精确 chunk id**。因此 `target_chunk_ids` / `target_content_sha256` 全部从语料副本直接读取，绝不编造。文件头 `status` 字段标明 DRAFT；整集哈希待 owner 署名后另行冻结，本批不生成冻结记录。

## 7. 实测记录（2026-11-22）

| 项 | 值 |
|---|---|
| 源 | `~/.mnemoseed-local`（只读；评测只写临时目录） |
| 副本文件数 | 53,005 |
| 副本字节 | 5,815,055,472（≈5.42 GiB） |
| 复制耗时 | 113.01 s |
| 四要素全树哈希耗时 | 429.85 s（一次性） |
| `corpus_manifest_sha256` | `f3e1761be065c9a34a36f0a378be9e16ccd613b8d94f065d71352ea20f858153` |
| `config_sha256` | `87146bcb0368e566ab13adde72a1744536f0a045fa40e3c96232672c212e5505` |
| `embedder_model_version` | `bge_m3_onnx@7e3d169cca88c313`（真 bge-m3 模型产物哈希） |
| `code_revision` | `d6808f47f75d0e33b6fffc3e57c1df44527a0dfa` |

miss seed 的 `target_chunk_id`（从副本直读，非编造）：
`039d7d239382480ea5108d76b389c3f8`（主目标，`target_content_sha256=27b85db34c54e94373005243a823bf934c6ca2438a24731b22e64fc10505f791`）、
`16ece6f0d7cb45aba8e7198b3b001a0c`、`51318607b0eb42009b9797ab03fc0faa`。
诊断记录只给出分数，故草案以副本中真实执行态 chunk 作为待 owner 确认的候选，不冒充 owner 已冻结的精确 id。

诚实空负样本探针的「无对应记忆」经副本全表扫描确证：氦三/量子退火/南极/冰淇淋均 0 命中。
