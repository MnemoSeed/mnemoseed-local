# PRD-B4d · i-matrix 低比特量化 profile 评测（IQ3 门禁 + 本地资源占用优化）

> 元信息：2026-08-26 用户提出引入 "TurboQuant" 类极低比特量化以降低本地资源占用；product-manager 与 solution-architect 联合评审裁定 **SHIP-WITH-ADJUSTMENTS（有条件可行）**，本 PRD 即条件化落地。
> 名词定版（架构师事实核查）：**TurboQuant ≠ GGUF IQ3**。TurboQuant 是研究性在线向量量化方法（论文级技术路线，非 GGUF 生态发布格式）；HuggingFace 上以 "TurboQuant" 命名的 GGUF 仅系上传者借名。本批次落地物 = **llama.cpp i-matrix 量化（IQ3_XS / IQ3_S，~3.3 bpw）**。**2.5-bit（IQ2 系）明确排除**——mvp-design §7 风险 10 既定红线："IQ2 损失不掩饰"。
> 基线：B4c 收口后（squash `7d923e3`）；当前门禁水位 **1671 passed / 5 skipped**。
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

## 7. 收口记录

（待批次启动后填写）
