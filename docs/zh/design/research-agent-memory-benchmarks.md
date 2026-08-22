# 调研文档 · Agent 记忆系统评测基准全景（外部基准选型依据）

> 性质：纯网络调研落盘（2026-08-19），非 PRD、不产生代码承诺；作为后续"外部基准选型 PRD"的依据资料，与 `PRD-B3-eval-harness.md`（内部 canary 通道）互补。
> 所有数字均为**公开来源**（GitHub / arXiv / ACL / ICLR / 厂商博客 / 第三方评测平台）摘录；凡无法核验或仅见于二手摘要处，一律标注（**未核实**）。
> 引用原则：每条论断附来源 URL；厂商自报数字 vs 第三方复现数字明确区分。

## 1. MemPalace 的做法：一个"评测翻车又翻回"的活教材

### 1.1 它是什么
MemPalace（`MemPalace/mempalace`）是 2026-04-05 上线的**本地优先开源 AI 记忆系统**（GitHub，~57K stars，Python），核心卖点是"逐字（verbatim）存储对话 + 语义检索，不抽取、不摘要"。"Palace 架构"（wing/room/hall 分层）是市场包装；48 小时内拿到 7,000+ star，靠的是一套激进的 benchmark 数字。
- 仓库: https://github.com/MemPalace/mempalace
- 基准文档: https://github.com/MemPalace/mempalace/blob/main/benchmarks/BENCHMARKS.md
- 官网基准页: https://mempalaceofficial.com/reference/benchmarks

### 1.2 发布数字（全部自报，均有复现脚本）
| Benchmark | 指标 | 自报数字 |
|---|---|---|
| LongMemEval | R@5（检索召回） | raw（纯 ChromaDB + all-MiniLM-L6-v2）**96.6%**；hybrid v4 + Haiku rerank **100%**；held-out 450q **98.4%** |
| LoCoMo | R@10 | session 无 rerank 60.3%；hybrid v5 88.9%（早期草稿"100%"是用 top_k=50 > 语料 19–32 个 session 刷出，已撤） |
| ConvoMem（Salesforce） | 平均召回 | 92.9%（仅用 5 类 × 50 = 250 题） |
| MemBench（ACL 2025） | R@5 | 80.3%（8,500 题） |

发布形式：`benchmarks/` 全套脚本 + `results_*.jsonl` 每题明细（可审计），README/官网做对比表（列了 Supermemory ~99%、Mastra 94.87%、Hindsight 91.4%、Mem0 30–45% 等）。

### 1.3 第三方审计结论（对本项目最重要的一课）
- **Issue #214（hugooconnor）**: 头条 96.6% 是"ChromaDB 的分数"——`build_palace_and_retrieve()` 只是 `collection.add()+query()`，未调用 palace 逻辑；每题新建仅 ~50 session 的临时集合检索，BM25 无模型也能到 93.8%；rooms/AAAK 模式反而掉到 89.4%/84.2%。https://github.com/MemPalace/mempalace/issues/214
- **Issue #39（gizmax，M2 Ultra 复现）**: 复现 raw=0.966 / rooms=0.894 / aaak=0.842；sonnet 做 reader+judge 跑端到端 QA 得整体 0.826（judge 非官方 GPT-4o，有 caveat）。https://github.com/MemPalace/mempalace/issues/39
- **vectorize.io 评论文章**: 指出**指标错配**（R@5"正确 session 进 top-5" vs 别人端到端 QA 准确率混排）、LoCoMo top_k=50 超语料规模、ConvoMem 250 题统计功效不足、"30x 无损压缩"是 token 估算 bug。https://vectorize.io/articles/mempalace-review
- **官方反应**: 项目方后续在 README/BENCHMARKS.md 公开纠正（撤 100% 头条、承认 teaching to the test、明确 R@5≠QA 准确率），是 2026 年记忆系统圈最出名的"自曝式"诚实改版案例。

**可直接复用的教训**：推广性评测的三个雷区 = ①指标定义错配（检索召回 vs QA 准确率混排）；②用 top_k > 语料规模 / 每题单独小 haystack 刷分；③自报数字不经第三方复现。mnemoseed 应主动避开，并公开脚本 + 每题明细。

## 2. Agent 记忆基准全景图

### 2.1 LongMemEval（当前"事实标准"）
- 论文: Di Wu, Hongwei Wang, Wenhao Yu, Yuwei Zhang, Kai-Wei Chang, Dong Yu（UCLA/Tencent AI Lab Seattle/UCSD），**ICLR 2025**，arXiv:2410.10813
- 测什么: 500 道人工精修题目，5 大核心能力（信息抽取、跨会话推理、时间推理、知识更新、弃答 abstention；官方按 6 类报告：single-session-user / single-session-assistant / single-session-preference / knowledge-update / temporal-reasoning / multi-session）
- 规模: 每题配一套"用户-助手"多会话历史；**S 版 ~115K tokens（约 40 个 session）/题，M 版 500 个 session（~150 万 tokens）**。500 题 S 版合计约 **5700 万 tokens**
- 构造: LLM 模拟生成 + 人工编辑（受 NIAH 启发做属性可控管线）
- 语言: 英文（历史与问题均为英文，无中文）
- 公开: GitHub https://github.com/xiaowu0162/LongMemEval ；HF https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned （S 版约 265–278MB JSON，M 版 ~2.7GB）；2025-09 cleaned 版（清理历史防干扰）；2026-05 出 LongMemEval-V2（转向 web agent 轨迹）https://github.com/xiaowu0162/LongMemEval-V2
- 指标: 官方用 LLM judge（论文用 gpt-4o，与人类标注 >97% 一致）做二元 QA 正确率；业界普遍再报 R@1/R@5/R@10 检索召回
- 接入: 官方 `src/evaluation/evaluate_qa.py` 提供逐题 judge prompt；社区（Mem0、EverMemOS、MemPalace）都有"灌入→检索→作答→判分"四段式 harness。Mem0 的 harness: https://github.com/mem0ai/memory-benchmarks

### 2.2 LoCoMo
- 论文: Adyasha Maharana 等（UNC/Amazon/Snap），**ACL 2024**，arXiv:2402.17753；站点 https://snap-research.github.io/locomo/
- 测什么: 超长多会话对话上的 QA（single-hop / multi-hop / temporal / open-domain / adversarial 五类）+ 事件图摘要 + 多模态对话生成
- 规模: **10 段极长对话**（每段 19–32 个 session、400–600 轮、平均约 16K tokens），官方标注 **1,986 个 QA 对**（初始 50 段，为控制闭源评测成本裁剪到 10 段）
- 构造: 机器-人工混合管线（LLM 生成、人工校订一致性）；含图片
- 语言: 英文
- 公开: https://github.com/snap-research/locomo （`data/locomo10.json`，CC BY-NC 4.0）；HF 镜像 `Percena/locomo-mc10`
- 指标: F1（答案来自对话原文，归一化 F1 部分匹配）；RAG 路线另报检索 R@k。人类水平 87.9，gpt-4-turbo 全上下文仅 51.6
- **judge 可靠性问题（重要）**: LoCoMo-Refined（mem-eval-suite，2026）审计发现原版 GPT-4o-mini judge 与人类标注一致性仅 **43.67%**，修订 judge（Qwen3-14B + 更严规则）达 **86.33%**；重打分：MemoraX 82.65、EverMemOS 58.25、MemOS 63.60、MemPalace 58.68、Mem0 48.91。https://github.com/mem-eval-suite/LoCoMo_refined

### 2.3 MemBench
- 论文: Haoran Tan 等（MSRA/USTC），**Findings of ACL 2025**，arXiv:2506.21605
- 测什么: 事实记忆 + 反思记忆（factual/reflective）两层级 × 参与（participation）与观察（observation）两场景；多指标 = 准确率/召回/容量/时间效率
- 规模: 每题对话平均 >100K tokens；子数据集 1（普通规模）约 480（参与）+340（观察）条；100K 规模子集更少
- 构造: 由 News 噪声灌入控制难度；LLM 生成
- 公开: https://github.com/import-myself/Membench （data2test 含 0–10k 与 100k 两档）；官方用 Qwen2.5-7B 跑 7 种记忆机制（FullMemory/RetrievalMemory/MemoryBank/MemGPT 等）
- 接入: 官方"按时间流"灌入（第 t 轮只能靠记忆访问 t-1 之前），检索用 multilingual-e5
- 语言: 英文（是否有中文子集未核实）

### 2.4 MSC 与 DMR
- **MSC**: Jing Xu, Arthur Szlam, Jason Weston，**ACL 2022**，arXiv:2107.07567（"Beyond Goldfish Memory"）。众包人类-人类多会话闲聊，5 个 session × 最多 14 轮，每会话带摘要标注；~5K 段对话，每段 ~1K tokens。英文。HF: `gonced8/multi-session_chat`
- **DMR（Deep Memory Retrieval）**: 由 MemGPT 论文（Packer 等，**arXiv:2310.08560**）提出——MSC 取 **500 段子集**，LLM 自指令生成每题 1 个 QA，5 session × ~12 条/会话。指标 = LLM judge 准确率 + ROUGE-L。MemGPT 93.4%（gpt-4-turbo）；Zep 94.8%（gpt-4-turbo）/98.2%（gpt-4o-mini）。数据: HF `MemGPT/MSC-Self-Instruct`（Apache 2.0）
- 批评: DMR 语料太小（60 条消息轻松进上下文窗口）、无多跳无时间推理，**已饱和**，Zep 自己在论文里承认局限并转向 LongMemEval

### 2.5 MemoryBank / MADial / MemGPT/Letta
- **MemoryBank**（Wanjun Zhong 等，**AAAI 2024**，arXiv:2305.10250）: SiliconFriend 陪伴机器人；ChatGPT 模拟 15 个虚拟用户 × 10 天对话，**人工手写 194 道探测题（97 英文 + 97 中文，少数双语记忆基准）**；引入艾宾浩斯遗忘曲线做记忆更新机制（可直接引用的心理学锚）。指标：检索准确率/回答正确性/连贯性/排名。仓库: https://github.com/zhongwanjun/MemoryBank-SiliconFriend
- **MADial-Bench**（Junqing He 等，**NAACL 2025**，arXiv:2409.15240）: 记忆增强对话生成，主动+被动回忆范式（认知科学/心理学锚）；171 条历史记忆 + 160 段对话 1474 轮，GPT-4 生成 + 人工精修；**英中双语**。检索指标 MAP/MRR/nDCG/Recall/Precision，另有人工打分（memory injection/ES proficiency/intimacy）。仓库: https://github.com/hejunqing/MADial-Bench
- **MemGPT/Letta**: DMR 93.4% 历史招牌；Letta 官方博客反讽式评测——**"用文件系统工具就把 LoCoMo 做到 74.0%"，并公开质疑 Mem0 声称用 MemGPT 跑 LoCoMo 的 68.5% 无法复现**（无 backfill 方案）。另推 Letta Memory Benchmark（动态记忆交互，非静态检索）。https://www.letta.com/blog/benchmarking-ai-agent-memory/ ；Letta 无官方 LongMemEval/LoCoMo 标准数字（issue #3115 讨论中，社区建议接 EverMemBench + MemoryArena）
- 相邻参考: PerLTQA（Du 等）3,409 对话 8,593 题；DialSim 以 TV 角色扮演测记忆+时间约束

### 2.6 2025–2026 新品阵营（均为厂商自报，勿当公证数字）
| 系统 | LongMemEval | LoCoMo | BEAM | 备注/来源 |
|---|---|---|---|---|
| **Mem0**（ECAI 2025 论文 arXiv:2504.19413） | 94.4（2026 新算法） | 92.5（2026）/2025 论文 J=67.13 | 1M: 64.1；10M: 48.6 | 单次检索 ~6,900 tokens；harness 开源 https://github.com/mem0ai/memory-benchmarks ；https://mem0.ai/research |
| **MemOS**（MemTensor） | 89.20 | 88.83 | BEAM-10M: 56.75 | OmniMemEval 框架（10 数据集、14 产品、add/search 双接口）https://github.com/MemTensor/OmniMemEval |
| **EverMemOS**（EverMind-AI，ACL 2026） | 82.00 | 92.32（统一 gpt-4.1-mini 作答） | — | 四段式 Add→Search→Answer→Evaluate，judge = GPT-4o-mini + 2 辅助模型三判盲评，κ>0.89 https://github.com/EverMind-AI/EverMemOS/tree/main/evaluation |
| **Hindsight**（Vectorize，arXiv:2512.12818） | OSS-20B 83.6 / OSS-120B 89.0 / Gemini-3 91.4 | 83.18/85.67/89.61 | 10M: **64.1**（次优 Honcho 40.6） | 声称 Virginia Tech + 华盛顿邮报独立复现；基准全开源 https://github.com/vectorize-io/hindsight-benchmarks （复现报告链接**未核实**） |
| **Memora（Microsoft）**（arXiv:2602.03315） | 87.4 | 0.863（LLM judge） | — | GPT-4.1-mini 全栈 |
| **HiMem**（arXiv:2601.06377） | — | GPT-Score 80.71（GPT-4o-mini 作答） | — | 报 GPT-Score + F1 + 延迟/token |

### 2.7 更大规模的下一批（趋势信号）
- **BEAM**（Mohammad Tavakoli 等，**ICLR 2026**，arXiv:2510.27246）: 100 段对话（128K/500K/1M/10M tokens 四档），2,000 道人工校验题，覆盖 10 类记忆能力（新增矛盾消解、事件排序、指令遵循等）。全开源: https://github.com/mohammadtavakoli78/BEAM ；HF: `Mohammadta/BEAM`、`Mohammadta/BEAM-10M`。10M 段约 2 万条消息/7,757 轮。已被 Mem0/Hindsight/MemOS 用作旗舰数字
- **EverMemBench**（arXiv:2602.01313，Letta issue 中引用）: 2,400 QA/10K 轮/~1M tokens；Mem0 37.09、MemOS 42.55、Zep 39.97，而 Gemini-3-Flash 全上下文 72.61（"记忆系统反而不如长上下文直读"现象，业界叫 MemoryArena 效应——LoCoMo 高分系统在 MemoryArena 掉到 40–60%）

## 3. 长上下文评测 vs 记忆评测

### 3.1 长上下文类（测"模型窗口内找得到"）
- **NIAH**（Greg Kamradt, 2023）: 单针检索烟囱测试，**已饱和**——RULER 论文点名"近乎满分但有效长度远低于宣称值"；业界共识是 smoke test 而非严格评测
- **RULER**（Hsieh 等，NVIDIA，**arXiv:2404.06654**）: 13 任务/4 类（NIAH 变体、多跳追踪、聚合、QA），程序化生成→**结构性杜绝污染**；提出"有效上下文长度"概念
- **LongBench**（THUDM/清华，**ACL 2024**，arXiv:2308.14508）: **中英双语**、21 任务、4,750 题，平均英文 6,711 词/中文 13,386 字；自动指标（ROUGE-L/F1）；LongBench-V2（2025，~503 题长 260k）与 LongBench-Lite 后来出现
- **InfiniteBench / LongBench-V2 / LMEB** 等新一批：多数仅见于二手表格，未逐一打开原文核验（**未核实**）

### 3.2 关键区分
- 长上下文评测测"**给定完整窗口，模型能否在噪音中定位/聚合**"；记忆评测测"**超过任何可用窗口后，外部存储+检索+编排层能否把它变成可用的局部上下文**"
- 语境长度增加但需检索内容不变会稀释能力（Lost-in-the-Middle，arXiv:2307.03172）——这正是记忆层的用武之地
- **"记忆层跑长上下文基准"是被认可的做法**：LongBench 论文本身把 retrieval 式压缩作为对比方法（对弱长上下文模型有增益）；Mem0/MemOS 的 OmniMemEval 用 BEAM 各档位评估记忆系统；MemDelta（arXiv:2606.29914）用 LongMemEval-S 做受控对比并证明**换 embedding 就能翻转结论（+6.2pp）**——在记忆存储上做 RULER/NIAH 式检索可行，但必须固定 embedding 与 judge 并公开

## 4. 各团队端到端跑法的实际方法论

1. **Ingestion（灌入）**: 每段对话按 user/assistant 轮次灌进记忆存储（Mem0/EverMemOS/MemPalace 都是 `add` 阶段）。Mem0 2026 算法强调单次传递分层抽取 + 多信号检索
2. **Retrieval（检索）**: 每题用 top_k（Mem0 top_200 预算；EverMemOS 默认 top-10；MemPalace top-5/10/20）。**top_k 不能超过语料规模**（MemPalace 翻车点）
3. **Answer（作答）**: 统一 reader LLM 以隔离记忆层贡献——EverMemOS 用 GPT-4.1-mini，MemOS 用 GPT-4o-mini，Hindsight 用 OSS-20B/120B/Gemini-3，MemPalace 用 sonnet/minimax；社区（Atlaso）甚至要求 reader 完全一致（Qwen3.5-9B）才能公平对比
4. **Judge（判分）**: LLM-as-judge 是主流。常见 judge：GPT-4o（LongMemEval 官方）、GPT-4o-mini（Mem0/MemOS）、GPT-5/GPT-5.2（Mem0 新）、Qwen3-14B（LoCoMo-Refined，86.33% 人类一致）、GPT-OSS-120B（Hindsight）；EverMemOS 三判盲评取均值校验 κ>0.89；MemGPT 用 ROUGE-L + LLM judge 双轨
5. **成本**（供估算）: 500 题 = 500×(作答+判分) ≈ 1,000–1,500 次 API 调用；Mem0 单次检索 ~6.9K tokens vs 全上下文 25K+；MemPalace rerank ~$0.001–0.003/题；MemDelta 测算 Mem0 写路径每实例 ~120 分钟/1,000+ LLM 调用/$0.50+（**写路径成本在 accuracy-only 评测里完全隐形**）
6. **已知坑与批评**:
   - LoCoMo 原 judge 松散（43.67% 人类一致性）→ 用 LoCoMo-Refined 或更严 judge
   - NIAH 饱和、RULER 有效长度低于宣称值
   - 指标错配（R@5 vs QA acc 混排）
   - teaching to the test（MemPalace 靠盯 3 道错题刷到 100%）
   - 污染与复现性：Mem0 自报 93.4% 被 Atlaso 用其自家 judge 复现为 44.2%（methodology+pipeline 双因素）；Hindsight 数字称被 Virginia Tech 独立复现（**未核实**）
   - **embedding/reader/judge 三者任一变动都会让结论翻转**（MemDelta 核心建议：固定 embedding、按模型族分层、报告写路径成本）

## 5. 本地单用户场景的可行性分析（8–9B 本地整合 + 云端作答题/裁判）

| 基准 | 数据规模（灌入量） | 本地可行性 | 语言 | 说明 |
|---|---|---|---|---|
| **LongMemEval-S（检索 R@5）** | 500 题，合计约 5,700 万 tokens | ✅ 高 | 英文 | 只做 embedding+检索完全本地（M2 Ultra 上 500 题 raw R@5 全流程约 5 分钟）；每题独立 40-session 语料更是轻量 |
| **LongMemEval-S（端到端 QA）** | 同上 | ⚠️ 需采样 | 英文 | 500 题全跑本地 9B 做 reader+judge 精度堪忧；建议云端 reader/judge 跑 50–100 题子集做 extra-route 锚，或本地 9B 只做 judge 的对照 |
| **LongMemEval-M** | ~1.5M tokens/题 × 500 | ❌ | 英文 | 仅供"极限背书"，跳过 |
| **LoCoMo** | 10 段对话 ≈ **16–26 万 tokens 总量** | ✅✅ 极高 | 英文 | 数据极小，检索几十分钟内灌完；1,986 题；务必 top_k≤10 与修正 judge |
| **DMR（MSC 子集）** | 500 段 × 60 条消息 | ✅✅ 极高 | 英文 | 已饱和，适合当冒烟测试/校准而非卖点 |
| **MemoryBank 风格（194 题）** | 需自建 15 用户×10 天语料 | ✅✅ 极高 | **英+中** | 与双语 canary 语料天然契合；原版用 ChatGPT 模拟用户，可用本地 9B 复刻 |
| **MADial-Bench** | 171 记忆 + 160 对话 | ✅✅ 极高 | **英+中** | 规模小、有人工标注，主动回忆维度独特 |
| **MemBench（普通档）** | 480+340 题，每题 ~10K tokens | ✅ | 英文 | 官方用 Qwen2.5-7B 跑过，7B 档已有先例 |
| **ConvoMem（采样）** | 75,336 对，预混 15 档上下文 | ⚠️ 采样跑 | 英文 | 全量太大；抽 300–1,000 题可测知识更新/弃答/偏好——**最贴合 dream/decay 设计** |
| **BEAM 128K/500K 档** | 20/35 段对话，每段 2–5 千轮 | ✅ | 英文 | 128K 档可本地灌入；1M/10M 档灌入可行但 10M 档本地 9B 整合不可行（57–570 天级） |
| **LongBench（检索式）** | 4,750 题，平均 6.7K 词 | ✅ 作补充 | **英+中** | 长上下文基准做检索式评测被认可，但推广价值弱于对话记忆类 |

**灌入时间推论**（供 PRD 写预估）：
- 纯检索类（不做本地整合）：embedding 吞吐 ~50–200K tokens/分钟（CPU/GPU 相关），LongMemEval-S 全量 embedding 约 0.5–2 小时量级
- 含本地 dream 整合：9B 本地模型 ~50–100 tok/s → 115K tokens/实例 ≈ 20–40 分钟/题；**500 题全 dream 到数天级，必须采样或云端整合**
- 云端作答题/裁判：1,000 次 API 调用 ≈ 数十元到数百元级，可控

**语言分布结论**：LongMemEval / LoCoMo / ConvoMem / MemBench / BEAM / DMR 全英文；**双语（EN+ZH）只有 MemoryBank（194 题）、MADial-Bench、LongBench**。展示中文能力只能走 MemoryBank 风格/MADial/LongBench，或自建双语 canary（即现有 B3 通道）。

## 6. 推荐方案（按 社区可信度 × 本地可行性 × 推广价值 排序）

对 mnemoseed-local 的 API 面（capture 用户/助手轮次 → dream-once 整合 → recall(query,top_k) → recent_sessions + 双语确定性 canary + 云端 extra-route 锚），四个卖点：①诚实（脚本+每题明细开源）；②本地优先（检索零 API）；③逐字存储 vs 抽取式存储正面对比；④双语能力（竞品普遍只有英文）。

### 🥇 第一选择：LongMemEval-S —— 检索召回轨（R@5/R@10）+ 云端 QA 子集锚
- **为什么**: ICLR 2025，被 Zep/Mem0/Hindsight/Supermemory/MemPalace 全部引用，行业"通行证"；检索轨 100% 本地可跑（embedding + recall API）
- **怎么接**: 每题 ~40-session 历史按轮次 capture 灌入 → 每题（或整库）recall(query, top_k=5/10) → 报 R@5/R@10/NDCG@10；再抽 50–100 题用云端 reader+judge（extra-route 锚）跑端到端 QA，报"检索 vs QA"两条独立曲线
- **纪律**: 不用"每题新建小 haystack"话术；公布 embedding 模型、judge 模型、每题 JSONL；QA 轨明确标 judge 非官方 GPT-4o 时的 caveat
- **成本**: 检索轨本地 ~1–2 小时灌入；QA 子集 ~100–150 次云端调用
- **参考实现**: Mem0 harness https://github.com/mem0ai/memory-benchmarks ；LongMemEval 官方 evaluate_qa.py

### 🥈 第二选择：LoCoMo（top_k≤10）+ LoCoMo-Refined 修订 judge
- **为什么**: ACL 2024，数据极小（~20 万 tokens）当天能跑完；可同时展示跨会话时间推理
- **怎么接**: 10 段对话灌入 → recall(top_k=10) 报 R@10；端到端 QA 用云端 reader + **Qwen3-14B 修订 judge（86.33% 人类一致）**或云端 judge
- **纪律**: 绝不 top_k>语料规模（MemPalace 翻车点）；同时报 F1 与 judge 分数
- **成本**: 本地灌入 1 小时级；judge ~2,000 次调用（可本地 Qwen3-14B 也可云端）

### 🥉 第三选择：MemoryBank 风格双语 canary（194 题 / 自扩）
- **为什么**: 唯一带中文+人工手写题的主流记忆基准（AAAI 2024）；与双语确定性 canary 无缝拼接；记忆更新的艾宾浩斯锚与 dream/decay 设计同源
- **怎么接**: 自建 15 用户 × 10 天语料（本地 9B 模拟），capture+dream-once，194 题 recall+QA；可扩到几百题
- **成本**: 全本地；是"双语卖点"的唯一主流背书
- **注意**: 原版自评（无第三方），作为"自家双语 canary"讲，不冒充国际排行

### 第四选择：ConvoMem 采样（知识更新/弃答/偏好赛道）
- **为什么**: 2025-11 最新（Salesforce），75K 对、6 类证据、15 档上下文长度；知识更新与弃答正是 dream/decay 想证明的能力；官方已把 LongMemEval/LoCoMo 转成 legacy 格式
- **怎么接**: 抽 300–1,000 题（固定 seed），预混测试用例直接灌入
- **成本**: 采样后本地可行
- **注意**: 抽样需固定种子并公开，否则重蹈 MemPalace 250 题功效不足之讥

### 第五选择（备选/远期）：BEAM 128K–1M 档
- **为什么**: ICLR 2026，新一代规模基准（Mem0/Hindsight/MemOS 都在报），可讲"记忆系统在塞不进上下文时的价值"；128K/500K 档本地可跑，1M 档作 stretch
- **注意**: 太新、门槛高、10M 档本地不可行；建议核心三项跑通后再上

### 共性"防翻车"清单（写进评测 PRD）
1. 指标分离：检索召回（R@k）与端到端 QA 准确率**永不混排**
2. top_k 恒 ≤ 语料规模；公布每题 haystack 规模
3. 固定并公开 embedding / reader / judge 三件套（MemDelta 教训：换 embedding 翻转 +6.2pp）
4. 训练/调参隔离：dev 50 题 vs held-out 450 题（MemPalace 教训）
5. 每题明细 JSONL + 复现脚本入库
6. 写路径成本透明（灌入时间、dream 次数、token 消耗）——这是 vs Mem0 等抽取式系统的**差异化优势**（MemDelta：Mem0 写路径每实例 1,000+ LLM 调用）
7. 本地 9B 做的 judge 结果只作内部对照；对外数字用云端 judge 或已校验的开源 judge（Qwen3-14B）

---

## 附：未能核实项清单（明确标注）
- **InfiniteBench** 的论文出处与指标细节：仅见于二手表格（**未核实**）
- **MemBench 是否含中文子集**：论文/仓库未明示（**未核实**）
- **Hindsight 的"Virginia Tech + 华盛顿邮报独立复现"**：厂商声明，未见可核实复现报告链接（**未核实**）
- **MemoraX 详细方法**：仅见 LoCoMo-Refined 榜单数字（82.65%），未见论文细节（**未核实**）
- **MemPalace 当前 star 数**：搜索快照间（57K–58K）有波动（**未核实**）
- **LongMemEval-V2 / EverMemBench / MemoryArena / CloneMem / EMemBench 等 2026 新基准**：多数仅从 Letta issue #3115 与二手摘要获得（**未核实**）
