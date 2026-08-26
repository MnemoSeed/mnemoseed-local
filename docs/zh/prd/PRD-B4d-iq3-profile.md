# PRD-B4d · i-matrix 低比特量化 profile 评测（IQ3 门禁 + 本地资源占用优化）

> 元信息：2026-08-26 用户提出引入 "TurboQuant" 类极低比特量化以降低本地资源占用；product-manager 与 solution-architect 联合评审裁定 **SHIP-WITH-ADJUSTMENTS（有条件可行）**，本 PRD 即条件化落地。
> 名词定版（架构师事实核查）：**TurboQuant ≠ GGUF IQ3**。TurboQuant 是研究性在线向量量化方法（论文级技术路线，非 GGUF 生态发布格式）；HuggingFace 上以 "TurboQuant" 命名的 GGUF 仅系上传者借名。本批次落地物 = **llama.cpp i-matrix 量化（IQ3_XS / IQ3_S，~3.3 bpw）**。**2.5-bit（IQ2 系）明确排除**——mvp-design §7 风险 10 既定红线："IQ2 损失不掩饰"。
> 基线：B4c 收口后（squash `7d923e3`）；当前门禁水位 **~1803 collected / 1799 passed**（以 `pwsh -File scripts/gate.ps1` 为准）。
> 立项：2026-08-26 用户拍板（issue #118）；live 矩阵实测另行待用户授权。
> 依据：`docs/zh/design/08-eval-harness.md`（评测臂 + bar 纪律）、`PRD-B3-eval-harness.md`（B4 前置排查定案）、`PRD-B2-roadmap.md` B4b/B4c 收口记录、`docs/zh/design/mvp-design.md` §6 Phase B / §7 风险 10。

## 0. 价值主张口径修正（先诚实，后干活）

外部实测来源（Gemini 对话）仅作**假设来源，不作证据**。两个前提修正入档：

1. **"省资源"口径反了**：相对现有 4B-Q4 lite 锚点（~3.4GB），9B-IQ3（~4.2–4.8GB）是**更大且更慢**的方案。真实故事是两条：
   - 对 standard 档默认 `qwen3.5:9b` Q4_K_M（~6.6GB）：IQ3 省 **~2GB 权重**；
   - 对 16GB 无独显机：提供一个"9B 智力、4.5GB 体积"的第三选项，跳出"4B 质量 vs 装不下 9B"的二选一。
2. **只换权重是半套方案**：dream 路由默认 `num_ctx=16384` 下 f16 KV cache 约 ~2.6GB（32k 时 ~5.2GB）。KV 量化（`OLLAMA_KV_CACHE_TYPE=q8_0` + `OLLAMA_FLASH_ATTENTION=1`，Ollama 服务端 env，非 driver option 白名单内）零质量损失省约一半——**无论本批是否采纳都必须写进部署文档**。

## 1. 目标与非目标

### 目标

- **T1（主目标）**：9B-IQ3_XS 在目标硬件（用户办公机：无独显、纯 CPU、16GB RAM 画像）过 canary 矩阵门禁 → 通过则写入 model profile 表，作为可选 profile（不改任何默认路由）。
- **T2（条件目标，仅当 T1 通过且有需求）**：4B-IQ3 lite 瘦身候选评测。风险预登记：小模型冗余度低，低比特伤害比大模型重（4B-Q4 off 态 recall=0.00 前科）；若 T1 通过，9B-IQ3 很可能直接优于 4B 系，T2 可裁撤。
- **T3（无条件目标，零代码）**：KV cache q8_0 部署文档面 + 实测对照一组。

### 非目标（明确不做）

- 不改 `DEFAULT_LLM_ROUTES` 测试锚（`qwen3.5:9b` 默认钉死于 test_cli.py AC3；改默认是数据到手后的 PRD 级决策）。
- 不做运行期自动降级（内存不足自动回落 4B）——同一记忆库被不同智力等级模型交替巩固是一致性陷阱；降级 = doctor 建议下 configwrite 显式变更（F2 机制已就绪）。
- 不接任何非标准量化格式（TurboQuant 借名包一律不用）；部署文档白名单只列标准 GGUF quant 类型。
- 不新增 LLM driver。llama.cpp 接入走既有 `openai_compatible` 面（llama-server 暴露 OpenAI 兼容 API，base_url 配置即可，零代码）；**默认宿主仍为 Ollama**（安装体验最优，doctor `/api/tags` 探活链路已就绪），llama.cpp 仅作高级选项写入文档。

## 2. 任务分解

### Phase 0 · 材料与导入（零代码，供应链纪律照 mvp-design §7 风险 10 先例）

1. 选定 HF 信誉源 IQ3_XS 打包：来源 URL + commit pin + sha256 三件套入档；绝不静默拉取（doctor 只提示先例 `cli.py:325`）。
2. Modelfile `FROM ./x.gguf` + `ollama create mnemoseed-local/qwen3.5-9b-iq3xs` 导入为本地 tag；确认新版 Ollama 支持 i-quants（旧版可能拒载，install 编排的 ollama 版本须核对并记录）。

### Phase 1 · 实测矩阵（零代码，全部走既有评测臂）

3. 双臂对比：`python -m mnemoseed_local.eval matrix --models <iq3-tag> ...` vs `qwen3.5:9b` Q4_K_M 基线，同一材料集（canary 定版 seed + replay 库）、同一硬件画像。
4. **bar 纪律（design/08 §4 照抄）**：每 cell ≥3 跑取共识；单跑数值不当 bar。
5. **退化形状率单列统计**（本批核心闸门）：字面 `[]` 坍缩指纹、predicate 打包整句、object='None' 串的出现频率，对照 Q4_K_M 基线。依据：design/08:150 —— qwen3.5:9b verify 席曾 ~67% 概率坍缩吐 `[]`；低比特量化预期加剧输出形状不稳，通用 benchmark 测不出这个。
6. collapse ladder 行为确认：坍缩触发 → `seed=base+attempt` 重试 → `recovered=True` 链路在 IQ3 权重下正常工作。
7. 资源面实测（目标机真数字，替代一切二手数据）：文件体积、任务管理器峰值内存（含 KV@8k/16k 两档、开/关 q8_0 对照即 T3）、tok/s（i-quant 内核在老 CPU 上历史慢于 K-quant，Gemini 的 "15–25 tok/s" 未验证）。
8. 速度预期管理：dream 为空闲触发后台批处理（`idle_min_sec=900`、24h 硬期限），延迟容忍度高；速度 bar 只要求吞吐撑得住 backlog，不追实时对话指标。

### Phase 2 · 文档面（仍零代码）

9. **model profile 表**（docs）：tier × 推荐 tag × 来源 URL × commit pin × sha256 × 实测体积/tok/s/峰值内存 × bar 结果。
10. 部署文档新增：KV cache q8_0 建议（T3 结论）、llama.cpp via `openai_compatible` 高级选项说明。
11. doctor hint（可选小代码，仅当 Phase 0–1 通过且确有需求）：`probe_ram_gb()` + `/api/tags` size 字段 → "当前 RAM 下 dream 模型峰值预估 X GB，考虑 Y profile"——hint only，照 `_hardware_tier_check` mismatch-is-hint 先例。

## 3. Bar（一票否决门禁，数值定版后钉死进本文）

| # | 维度 | bar | 依据 |
|---|---|---|---|
| 1 | 结构化输出 schema 合法率 | **≥99%**（多跑共识） | dream 负载是结构化 JSON 抽取非自由推理；低比特下结构化输出最先塌 |
| 2 | 退化形状率 | 不得劣于 Q4_K_M 基线 | design/08:150 形状不稳前科 |
| 3 | canary_recall（多跑均值） | ≥0.6（gemma4:e4b 干净基线 0.625 同尺） | B4c live bar 延续 |
| 4 | collapse recovery | ladder 触发后 `recovered=True` 正常收敛 | B4c A1 机制 |
| 5 | 后台吞吐 | ≥15 tok/s @目标机纯 CPU | backlog 可消化即可 |
| 6 | 峰值内存 | 权重 + KV@16k(q8_0) + embedder(~2GB) + daemon 在 16GB 机可容纳 | hardware.py 16GB→lite 画像 |

## 4. 判定树

- 全过 → IQ3_XS 入 model profile 表作可选档位；文档如实写"+~2GB 内存换更可靠整合"或"省 ~2GB vs 9B-Q4"，按对照基线选措辞，不夸大。
- bar 1/2 塌（schema/形状）→ **否决 IQ3**；折中候选 = 8B-Q4_K_M 进下一轮评测。
- bar 5 塌（速度）但质量过 → 如实记录 i-quant CPU 内核性能数据，profile 表标注硬件要求，交用户自决。
- T1 通过 → 再议是否立 T2（4B-IQ3）；默认倾向裁撤。

## 5. 红线与诚实边界

- 单跑数值不能当 bar；方差类 cell 仅单点观测。
- 评测臂产数据，数据不得反向包装成"理论"。
- 报告累积只增不减，落 `<CONFIG_DIR>/eval/` 不入 git。
- 外部实测（含 Gemini 对话、社区 benchmark）一律只是假设来源；本仓验收只认内部 eval rig + 目标机实测。
- live 矩阵为人工动作，不进 pytest 门禁；live 运行待用户授权。

## 6. 本篇引用

- `docs/zh/design/08-eval-harness.md`（评测臂语义 + bar 纪律 + 60s 超时墙 + 坍缩记录）
- `docs/zh/prd/PRD-B3-eval-harness.md`（B4 前置排查定案 + seed 政策）
- `docs/zh/prd/PRD-B2-roadmap.md`（B4b/B4c 收口记录 + bar 表）
- `docs/zh/design/mvp-design.md`（§6 Phase B、§7 风险 10 第三方量化包治理先例、§7 档位表）
- `src/mnemoseed_local/eval/matrix.py`（`ROSTER_DEFAULT` / `--extra-route` 参面）、`src/mnemoseed_local/llm/drivers/openai_compatible.py`（llama.cpp 零代码接入面）

## 7. Phase 0 供应链与模型档案（零代码，照 mvp-design §7 风险 10 纪律）

### 7.1 Modelfile 供应链模板

i-matrix 量化 GGUF 通过 Ollama Modelfile 以本地文件导入，不经过 `ollama pull` 远端拉取，供应链三件套必须入档：

```dockerfile
# Modelfile.iq3xs — 本地导入模板（示例文件名需与实际 gguf 一致）
FROM ./Qwen3.5-9B-IQ3_XS.gguf
PARAMETER num_ctx 16384
PARAMETER num_predict -1
```

导入命令（在 gguf 所在目录执行）：

```powershell
# 1. 校验文件完整性（与 HF 页面 sha256 对照）
Get-FileHash .\Qwen3.5-9B-IQ3_XS.gguf -Algorithm SHA256

# 2. 创建本地 tag（命名空间保留 mnemoseed-local 前缀，便于 doctor / eval 探活区分）
ollama create mnemoseed-local/qwen3.5-9b-iq3xs -f Modelfile.iq3xs

# 3. 验证 tag 已就绪
ollama list | Select-String "mnemoseed-local/qwen3.5-9b-iq3xs"
ollama show mnemoseed-local/qwen3.5-9b-iq3xs

# 4. 记录 Ollama 版本（i-quants 需较新 Ollama，旧版会拒载）
ollama --version
```

要求：

- 绝不静默拉取：doctor 仅提示（`cli.py:325` 先例），安装需用户显式确认。
- Ollama 版本需记录入档：本机实测 `0.32.13` 已支持 i-quants；若目标办公机版本更旧，需先升级 Ollama（install 编排核对）。
- 基线权重 `qwen3.5:9b` 为 Ollama 官方库 tag（`6488c96fa5fa`，6.6GB，`ollama pull qwen3.5:9b`），同样记录 commit pin 与本地 `ollama list` 的 SIZE/MODIFIED 以便对照。

### 7.2 模型档案表（Model Profile）

| tier | 推荐 tag | 量化 | 来源 URL | commit pin | sha256 | 实测体积 | tok/s (CPU) | 峰值内存 (KV@16k) | bar 结果 |
|---|---|---|---|---|---|---|---|---|---|
| standard (基线) | `qwen3.5:9b` | Q4_K_M (~4.5 bpw) | `https://ollama.com/library/qwen3.5:9b`（官方库，digest `6488c96fa5fa`） | `6488c96fa5fa` (ollama list) | —（官方库不暴露单文件 sha256，以 `ollama show` digest 为准） | 6.6GB (ollama list) | 待办公机实测（dev 机 32GB+RTX3070 非目标画像，不代入） | 待实测：权重 6.6GB + KV@16k f16 ~2.6GB + embedder ~2GB + daemon；q8_0 后 ~1.3GB 下降见 §9 | hardware gate pending（见 §8） |
| 16GB 第三选项 (候选) | `mnemoseed-local/qwen3.5-9b-iq3xs` | IQ3_XS (~3.3 bpw) | 候选 HF 信誉源（择一，需三件套落定后更新）：<br>• `https://huggingface.co/bartowski/Qwen2.5-14B-GGUF` 同类发布者形态（示例，需换为 Qwen3.5-9B 对应 repo）<br>• `https://huggingface.co/lmstudio-community/Qwen3-8B-GGUF` 同类<br>• 实际选中后填写：`https://huggingface.co/<owner>/Qwen3.5-9B-GGUF` 分支 `iq3_xs` | 待导入后填写（HF commit hash，`git rev-parse HEAD` 或页面 `commit` 短 hash） | 待下载后 `Get-FileHash -Algorithm SHA256` 填写 | 预期 ~4.2–4.8GB（官方 IQ3_XS 体积，待 `ollama list` / 文件大小实测覆写） | 待实测：i-quant 内核在老 CPU 上历史慢于 K-quant，bar ≥15 tok/s（§3 #5） | 待实测：权重 ~4.5GB + KV@16k(q8_0) ~1.3GB + embedder ~2GB = ~7.8GB 理论可容 16GB；需任务管理器峰值验证 | hardware gate pending |
| lite (现行) | `qwen3.5:4b` | Q4_K_M | `https://ollama.com/library/qwen3.5:4b` (digest `2a654d98e6fb`，3.4GB) | `2a654d98e6fb` | — | 3.4GB | 待实测 | 已在 16GB 画像可用 | 非本批对象 |
| T2 候选 (条件) | `mnemoseed-local/qwen3.5-4b-iq3xs` | IQ3_XS (4B) | 同上 HF 源的 4B 同系列文件 | 待定（仅当 T1 通过且立 T2 时填写） | 待定 | 预期 ~2.0GB | 待定 | 待定 | 默认裁撤（见 §1 T2） |

> 诚实声明：本表 commit pin / sha256 / 实测体积 / tok/s / 峰值内存列当前为**占位**；IQ3_XS 文件尚未在目标办公机导入，**不伪造数字**。供应链纪律要求导入当日：来源 URL + commit pin + sha256 三件套一次补齐，`ollama list` 与 `ollama show` 截图或文本一并入档。2.5-bit IQ2 系无论何时均不入表（mvp-design §7 风险 10 红线）。

供应链核验步骤（办公机执行时逐项打勾入档）：

- [ ] HF 页面记录文件 commit hash（页面 `History` → `commit` 短 hash + 完整 hash）。
- [ ] 本地 `Get-FileHash -Algorithm SHA256` 与 HF 页面 `sha256` 一致。
- [ ] `ollama create` 成功，`ollama list` 可见新 tag，`ollama show` 的 `Modelfile` 回显 `FROM` 路径。
- [ ] `ollama --version` 记录（≥0.32 已验证支持 i-quants）。

## 8. Phase 1 实测矩阵 — 硬件门禁待办公机执行（Hardware Gate Pending）

### 8.1 当前环境与待执行说明

- **当前执行环境**：dev 机 `32GB RAM + NVIDIA RTX 3070 8GB + Ollama 0.32.13`（`ollama list` 已验证 `qwen3.5:9b 6.6GB / qwen3.5:4b 3.4GB` 等在位）。该画像**不是** PRD-B4d 目标画像（16GB RAM、无独显、纯 CPU），因此本批次**不以 dev 机数据冒充 16GB 门禁结果**。
- **判定**：`hardware gate pending on office machine` —— T1 的 6 项 veto bar 需在用户办公机（16GB、无独显、纯 CPU）上复跑后方能 verdict。本节给出**可一键复现的精确命令 + bar 检查清单**，owner 在办公机执行后将实测值回填 §7.2 表并更新 §10 收口记录。

### 8.2 精确复现命令（办公机 PowerShell）

所有矩阵命令走既有评测臂（`src/mnemoseed_local/eval/matrix.py` / `harness.py`），不新增代码，不改 `DEFAULT_LLM_ROUTES`。

**前置**：确保 `ollama serve` 已启动且 `ollama list` 可见 `qwen3.5:9b` 与 `mnemoseed-local/qwen3.5-9b-iq3xs`（Phase 0 导入后）。

```powershell
# 0. 自检：harness 本身是否健康（stub 席秒级，recall 必须 1.0 / pollution 0）
uv run python -m mnemoseed_local.eval canary

# 1. 预览将跑的 cell（零副作用，不建 store）
uv run python -m mnemoseed_local.eval matrix --models qwen3.5:9b,mnemoseed-local/qwen3.5-9b-iq3xs --ensemble off,verify --list

# 2. 双臂对比：Q4_K_M 基线 vs IQ3_XS，同一材料集、同一硬件画像
#    每个 cell 在同一办公机上 ≥3 跑取共识（PRD-B4d §2 任务 4 纪律；单跑不当 bar）
#    报告累积写入 <CONFIG_DIR>/eval（默认 %USERPROFILE%\.mnemoseed-local\eval），不入 git
for ($i=1; $i -le 3; $i++) {
  uv run python -m mnemoseed_local.eval matrix --models qwen3.5:9b,mnemoseed-local/qwen3.5-9b-iq3xs --ensemble off,verify
}

# 3. 可选：若办公机同时想对比 llama.cpp 权重（零代码，经既有 openai_compatible 面）
#    先在另一终端起 llama-server：llama-server -m Qwen3.5-9B-IQ3_XS.gguf --host 127.0.0.1 --port 8080
#    再以 extra-route 入阵（与 ollama 席同场对比，同一 rig 代码路径）
uv run python -m mnemoseed_local.eval matrix --models qwen3.5:9b --extra-route "openai_compatible|qwen3.5-9b-iq3xs|http://127.0.0.1:8080/v1" --ensemble off,verify --list
uv run python -m mnemoseed_local.eval matrix --models qwen3.5:9b --extra-route "openai_compatible|qwen3.5-9b-iq3xs|http://127.0.0.1:8080/v1" --ensemble off,verify

# 4. 离线重判（可选）：对已落盘的 v1.1 报告重算 recall（零 GPU）
uv run python -m mnemoseed_local.eval rescore --report <CONFIG_DIR>/eval/<timestamp>-<slug>.json
```

参数说明：

- `--models` 逗号分隔 ollama tags；`matrix.py:48 ROSTER_DEFAULT` 的 6 模型仅为默认值，本批显式传入双 tag 即可。
- `--ensemble off,verify` 展开为 off 席与 verify 席（B 席默认 `gemma4:e4b`，可另用 `--verifier gemma4:e4b` 显式指定）。
- `--extra-route` 形态 `driver|model|base_url[|key_env[|timeout[|max_tokens]]]`，llama.cpp 场景 `driver=openai_compatible`，`api_key_env` 留空亦可。
- `rig_freshness` 自检（#132）在 `EvalRig.__init__` 对 `RigPaths.root` 做 fail-loud 校验；`run_matrix` 已为每 cell 分配 `root/runs/<run_id>/<cell_id>` 全新命名空间，跨跑报告已在 PRD-B2-roadmap B4b 记录中验证无污染。

### 8.3 六项一票否决 Bar 检查清单（每 cell ≥3 跑取共识）

执行后对 `<CONFIG_DIR>/eval` 下的 3 份报告做 `rescore` 均值，按下表逐项打勾；任一项否决即按 §4 判定树执行。

| # | 维度 | bar | 测量方式 | 判定 |
|---|---|---|---|---|
| 1 | 结构化输出 schema 合法率 | ≥99%（多跑共识） | 统计 `CellRun.reflect_result` / `reflect_payload` 的 JSON 可解析率；dream 负载为结构化抽取，低比特下最先塌 | IQ3 席 <99% → 否决 |
| 2 | 退化形状率 | 不得劣于 Q4_K_M 基线 | 单列统计三指纹出现频率：字面 `[]` 坍缩（`harness.py:137 _COLLAPSE_TEXT`）、predicate 打包整句、object='None' 串；对照 qwen3.5:9b 基线同批数据 | IQ3 劣于基线 → 否决 |
| 3 | canary_recall（多跑均值） | ≥0.6 | `score_canary` 主指标；gemma4:e4b 干净基线 0.625 同尺（B4c 延续） | 均值 <0.6 → 否决 |
| 4 | collapse recovery | ladder 触发后 `recovered=True` | `CellRun.reflect_collapse_attempts` 与 `reflect_recovered`；B4c A1 机制 `seed=base+attempt` 重试，`harness.py:167 _reflect_recovery_factory` | 触发后未 `recovered=True` → 否决 |
| 5 | 后台吞吐 | ≥15 tok/s @目标机纯 CPU | `cost.duration_s` / `cost.token_usage` 推算；i-quant 内核在老 CPU 上历史慢于 K-quant，需真机数 | <15 tok/s → 记录为硬件要求，不自动否决质量（§4 判定树第三条） |
| 6 | 峰值内存 | 16GB 机可容纳 | 权重（`ollama list` SIZE）+ KV@16k（f16 ~2.6GB / q8_0 ~1.3GB 见 §9）+ embedder ~2GB + daemon；以任务管理器峰值 + `ollama ps` 对照为准 | 超 16GB 预算 → 否决或标注硬件要求 |

补充记录项（非 bar，但需一并入档）：

- 文件体积（`Get-ChildItem *.gguf | Select Length` / `ollama list` SIZE）。
- 任务管理器峰值内存（KV@8k / KV@16k 两档，开/关 q8_0 对照即 T3）。
- 每 cell 的 `core_yield` / `noise_pollution` / `verify judged/accepted/rejected/fallbacks` / `seat_seed_policy`（报告 `summary_lines` 已含）。

## 9. T3 无条件项：KV Cache q8_0 + Flash Attention（零质量损失，~1.3GB 节省）

> 无论 T1 是否通过，本节结论**无条件写入部署文档**（PRD-B4d §0 口径修正第 2 条）。只换权重是半套方案；KV cache 量化是另一半。

### 9.1 机制与收益

- **KV cache 是什么**：dream 推理时为上下文中每个 token 缓存的 attention 状态，随 `num_ctx` 线性增长；`num_ctx=16384` 下 f16 约 ~2.6GB，`num_ctx=32768` 时 ~5.2GB——长上下文下可超过权重本身。
- **q8_0 含义**：将 KV cache 从 f16（2B/element）量化为 q8_0（1B/element），**省约一半**；`q4_0` 省约 3/4 但有可感知精度损失，**本批次不推荐**。
- **质量影响**：q8_0 为 Ollama 官方推荐的"safe default"，perplexity 增量 0.002–0.05，属不可感知范围；`q4_0` 在高上下文下有小到中等损失。本 PRD 仅采纳 `q8_0`。
- **Flash Attention 依赖**：KV cache 量化仅在 Flash Attention 启用时生效。自 Ollama `0.31.2`（2025-10-01 起三态化，FAQ 2026-07-03 修正）起，Ollama 在支持的后端/设备上**自动启用** Flash Attention；显式设 `OLLAMA_FLASH_ATTENTION=1` 为强制开启（无害），`0` 为强制关闭。启动日志中 `OLLAMA_FLASH_ATTENTION:false` 仅表示"变量未设"，不等同于"行为关闭"。
- **实测对照**：在目标办公机上做开/关对照（见 §9.3），预期节省 **~1.3GB**（16k 上下文 f16 2.6GB → q8_0 1.3GB），**零质量损失**。

### 9.2 部署配置（Ollama 服务端 env，非 driver option）

`OLLAMA_KV_CACHE_TYPE` 与 `OLLAMA_FLASH_ATTENTION` 均为 **Ollama 服务端全局环境变量**，在 `ollama serve` 启动时读取，**对该 Ollama 实例加载的所有模型生效**（无 per-model 覆盖）；修改后需**重启 Ollama**。

Windows PowerShell（开发机 / 办公机通用，服务端 env）：

```powershell
# 方式 A：当前终端 + 重启 ollama serve（推荐用于验证）
$env:OLLAMA_FLASH_ATTENTION = "1"
$env:OLLAMA_KV_CACHE_TYPE = "q8_0"
ollama serve
# 另开终端验证：ollama run qwen3.5:9b / 观察任务管理器内存

# 方式 B：系统级（对 Windows 服务 / 登录任务持久）
[System.Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE", "q8_0", "User")
# 重启 Ollama（若以计划任务常驻，需重启任务；若以 ollama app 需退出重开）
```

Linux (systemd)：

```ini
# sudo systemctl edit ollama.service
[Service]
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
```

Docker：

```bash
docker run -e OLLAMA_FLASH_ATTENTION=1 -e OLLAMA_KV_CACHE_TYPE=q8_0 -p 11434:11434 ollama/ollama
```

验证：

```powershell
# 启动日志应含 flash attention enabling / kv cache type；若见 "quantized kv cache requested but flash attention disabled" 则表示 FA 未生效需排查
ollama serve 2>&1 | Select-String -Pattern "flash attention|kv cache"
# 峰值内存对照：分别在 f16 与 q8_0 下跑同一 16k 上下文，任务管理器 / ollama ps 观察内存差
```

### 9.3 办公机实测对照（待执行，模板）

| 条件 | 权重 | num_ctx | KV cache | 任务管理器峰值 | tok/s | 备注 |
|---|---|---|---|---|---|---|
| f16 对照 | 6.6GB (Q4_K_M) | 16384 | f16 ~2.6GB | 待填 | 待填 | 基线 |
| q8_0 | 6.6GB | 16384 | q8_0 ~1.3GB | 待填 | 待填 | 预期 -1.3GB，质量无损 |
| q8_0 + IQ3_XS | ~4.5GB | 16384 | q8_0 ~1.3GB | 待填 | 待填 | 权重 -2GB + KV -1.3GB 合计 |
| f16 对照 | 6.6GB | 8192 | f16 ~1.3GB | 待填 | 待填 | lite 窗口对照 |
| q8_0 | 6.6GB | 8192 | q8_0 ~0.65GB | 待填 | 待填 |  |

> 执行后将实测峰值回填本表；若 `q8_0` 在办公机上未生效（日志含 fallback 提示），需记录原因与 Ollama 版本。

### 9.4 llama.cpp 高级选项（经既有 `openai_compatible` 面，零代码）

默认宿主仍为 Ollama；llama.cpp 仅作高级选项，**不新增 driver**：

```powershell
# 另起 llama-server（示例）
llama-server -m Qwen3.5-9B-IQ3_XS.gguf --host 127.0.0.1 --port 8080 --ctx-size 16384 --flash-attn --cache-type-k q8_0 --cache-type-v q8_0
```

```toml
# config.toml — 将 dream 路由指向 llama-server（OpenAI 兼容 API）
[dream.llm.dream]
driver = "openai_compatible"
model = "qwen3.5-9b-iq3xs"
base_url = "http://127.0.0.1:8080/v1"
# api_key_env 留空或指向 env 名；llama-server 默认无需 key
```

探活：`openai_compatible` 席由 `matrix.py:probe_routes` 走 `driver.check()` 探活，失败为 `route_unavailable:` loud failure（exit 1），与 ollama 席的 `missing_model:` 区分。

## 10. 收口记录

- 2026-08-27 批次 `batch/b4d-iq3-eval` 在 worktree `../mnemoseed-b4d` 启动（基线 `c772b22`，#132 收口后）。本批次为**零代码硬件评测 + 文档**批次，不改 `src/` 与 `DEFAULT_LLM_ROUTES`。
- Phase 0：Modelfile 模板与导入命令已入档（§7.1）；模型档案表已建（§7.2），IQ3_XS 的 HF commit pin / sha256 / 实测体积 / tok/s / 峰值内存列为**占位**，待办公机导入后一次补齐（mvp-design §7 风险 10 供应链纪律）。Ollama 版本已记录：dev 机 `0.32.13` 支持 i-quants。
- Phase 1：当前 dev 机为 32GB + RTX 3070，非 16GB 纯 CPU 目标画像，**不伪造门禁数据**；`hardware gate pending on office machine`（§8），精确复现命令与六项 veto bar 检查清单已就绪，owner 在办公机执行后回填 §7.2 与 §9.3 并按 §4 判定树 verdict。
- T3：KV cache q8_0 + Flash Attention 的机制、部署 env、实测对照模板已无条件入档（§9），无论 T1 是否通过均保留；预期节省 ~1.3GB 零质量损失，已在 Ollama 官方 FAQ 与 `0.31.2` 三态化行为上取证。
- 非目标守卫：未改 `config.py:360 DEFAULT_LLM_ROUTES`，未加运行时自动降级，未引非标 GGUF，未新增 driver（llama.cpp 仅走既有 `openai_compatible` 面）。
- 门禁：`pwsh -File scripts/gate.ps1`（pytest/ruff/format/mypy）在 worktree 保持绿（docs-only，无 src 改动）。
