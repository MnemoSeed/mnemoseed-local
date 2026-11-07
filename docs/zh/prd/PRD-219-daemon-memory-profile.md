# PRD-219：常驻守护进程内存画像（观测单元：量测、归因、增长签名、削减建议）

日期：2026-11-07。状态：观测执行完毕，待主控复核；#219 的削减半批（reduce）**不在本批内**，其路径以本文 §削减建议 的排序建议移交，每条各需独立评审批次。基线：`7d93e4f`（分支 `observation/219-daemon-memory-profile`，worktree `.mnemoseeding/worktrees/w219`，= 当前 main）。本批为 #219（"Diagnose and reduce daemon resident memory usage"）的**观测半批**：对生产守护进程（真实家、真实存储、7788 常驻、全程未停启）做只读画像 + 有界合成负载响应 + 隔离对照实验，归因驻留内存的构成、给出增长签名判定与按预期收益排序的削减建议。除本文档外**零仓库写入、零产品代码改动**；全部探针脚本/采样表/边界台账保存在新建证据树 `%LOCALAPPDATA%\Temp\w219-memprof\`（台账 `ledger.md`：27 条边界事件 + 控制台读数转录块 + 证据哈希块）。

## 证据与结论边界

两层内存口径贯穿全文，混读会得出错误结论：**工作集（WS，WorkingSet64）**= 物理驻留；**私有提交（private bytes，PrivateMemorySize64）**= 已提交的私有记账（含已换出的冷页），计入系统提交上限。观测到的核心形态是两者的巨大分裂：常驻 WS ~0.6–0.9 GB，私有提交 ~10.8 GB——**驻留成本温和、提交成本极端**，削减批次必须对准后者。"空闲"基线窗口内 owner 自己的 hook 流量（约每分钟一次 `/ingest` + `/session/recall-pending`）持续在线，这是**诚实生产空闲**，不是无菌实验室空闲；该流量对内存无可见驱动（§基线）。归因读源用的是 worktree 0.3.0 的端点 schema（实测响应逐字段吻合），机制行已在**已装 0.2.0 源码**中逐行核对一致（`max_length=8192`、懒加载锁、`intra_op_num_threads=2`、`candidate_k` 公式）——运行主体是 0.2.0，读源差异已收敛为零。无调试器级堆取证：归因是**行为级**（采样 + 隔离对照实验），不是符号级（§覆盖边界）。

## 机器与环境清单

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows 11 Pro 25H2，build 10.0.26200（与 PRD-305/320 各轮同一主机） |
| 观测主体 | 生产守护进程 PID 22260：`python.exe`（cpython 3.12，uv 托管）运行 `C:\Users\Little Star\.local\bin\mnemoseed-local.exe up`；进程链 28208（exe 启动器）→ 26716（uv tools python）→ 22260，**无子进程、无 worker**——embedded 单进程形态与设计一致 |
| 版本 | 已装 `mnemoseed_local 0.2.0`（uv tool site-packages dist-info；入口 exe 时间 Nov 6 00:56）；worktree 源 0.3.0 @ `7d93e4f` 仅用于端点 schema 与机制读源 |
| 启动时刻 | lifespan `boot_started_at=1793897892.182`（= 2026-11-06 00:58:12.182 +08，epoch 反推，**在盘** `load-responses/obs-before.json`）；进程 CreationDate 2026-11-06 00:58:08 +08（Win32_Process 查询，会话记录，已转录台账）——差 ~4 s 为 python 导入/uvicorn 启动时长，两源互证；探针首读 healthz ≈ 2026-11-07 17:12:51 +08（uptime 144,879.8 s 反推，会话记录），uptime ≈ **40.2 h** |
| healthz（探针始） | preset embedded；stores：vector main `lancedb_embedded`、graph isolated+main `sqlite_graph`、meta `sqlite_meta`、embed `bge_m3_onnx`；migrations 16；gate ok；dream_route not ready（ollama 不可达，dream paused——与 daemon.log 一致） |
| 真实家在盘尺寸 | `chunks.lance` **3.0 GB**；`models/bge-m3` 560 MB（`model_quantized.onnx` **544 MB** + `tokenizer.json` 17 MB + `sparse_linear.pt` 4 KB）；`backups` 153 MB；sqlite 三库均 <1 MB（meta 324 KB + wal 610 KB、graph 172 KB、isolated 115 KB）——**磁盘上向量库是绝对主体，sqlite 可忽略** |
| 开机以来活动（observability） | load 前快照（**在盘** `load-responses/obs-before.json`）：capture_ingest 868、actor hook 1213 / console 3 / mcp 1、recall_injection 12（10,708 chars）；load 后（**在盘** `obs-after.json`）：ingest 870、hook 1216、console 77（+74 = 本批 73 请求 + after 快照自身计入）；更早一次未存档清单快照（≈17:12，会话记录，已转录台账转录块）读得 861 / hook 1199 / console 2，与在盘前值之差为两次读数间的 ambient ingest |
| 装机级负载历史 | 前一日志轮 `daemon.log.1`（28 MB，Sep 29 归档）含 **371,362** 行 `POST /ingest -> 202`（该日志轮 `POST /ingest` 全状态行计数 371,368）——本安装经历过年量捕获窗口；当前 boot 至 load 前 868 次（在盘 obs-before.json） |
| 隔离对照 | iso1：端口 7795、家 `%LOCALAPPDATA%\Temp\w219-memprof\isohome\`；iso2（反事实首轮）：端口 7796、家 `isohome2\`；iso3（反事实**复测存档**，修正轮）：端口 7797、家 `isohome3\`；均 `MNEMOSEED_LOCAL_HOME` + `init`（isolated 实例路径改指 iso 家）+ `up --port`，boot 后即杀、端口确认释放 |
| 证据树 | `%LOCALAPPDATA%\Temp\w219-memprof\`：`ledger.md`（27 条边界台账 + 控制台读数转录块 + 证据哈希块）、`sample.ps1`/`privws.ps1`（采样器）、`drive-load.sh`/`cleanup-pins.sh`/`isolated-boot.sh`（负载/清理/隔离启动）、`idle.csv`/`load.csv`/`decay.csv`/`iso-boot.csv`/`iso2-recheck.csv`（采样表）、`load-timing.tsv`/`cleanup-timing.tsv`/`iso2-recheck-timing.tsv`、`load-responses/`+`cleanup-responses/`+`bodies/`（请求/响应留档）、`iso-daemon.log`/`iso2-daemon.log`/`iso3-daemon.log` + `iso-healthz.json`/`iso3-healthz.json`、`synthetic-chunk-ids.txt` |

## 基线（生产空闲，10 min × 30 s，20 样本）

| 指标 | 区间（20 样本） | 判读 |
| --- | --- | --- |
| WS | 636.6 → 637.9 MB（Δ+1.3 MB/10 min） | 平稳 |
| 私有提交 | 10,804.7 → 10,804.2 MB（平） | 平稳 |
| 虚拟地址空间 | 16,440.1 MB（恒定） | 平稳 |
| 线程 / 句柄 | 56 / 490–508 | 平稳 |

**基线判定：空闲稳定，无空闲增长**；ambient hook 流量在此窗口内不构成内存驱动。私有 WS 细分（WMI `Win32_PerfFormattedData_PerfProc_Process` 单次读；读数为控制台输出，已按声明出处转录进台账转录块）：605 MB private-WS / 637.2 MB WS——即 **~10.2 GB 的提交是已提交但已换出的冷页**（该进程 40 h 期间被系统逐步修剪）。生产 daemon 的 ~637 MB 驻留 ≈ bge-m3 权重（544 MB）+ 运行时，合理自洽。

## 负载响应（有界合成负载，全部 200）

负载配方（`drive-load.sh`，与 V305 轮先例同型、全部明确标记合成）：**20 pins**（`/memory/remember`，文本带 `[w219-memprof SYNTHETIC]` 标记）+ **50 recalls**（自然主题查询，只读）+ 分页读三连：`/session/recent`（sessions=5 × per_session=100，页读路径上限）+ `/memory/atlas`（limit 500）+ `/memory/export`（limit 500）。单请求耗时（时间戳差）：pin ~1 s、recall ~2.1 s（3 GB 库上）、atlas-500 ~4 s、export-500 ~1 s。

| 阶段 | WS | 私有提交 | 线程 |
| --- | --- | --- | --- |
| 负载前 | 638.0 | 10,804.2 | 56 |
| pins 期间（5 样本，17:28:00–20） | 638.0–948.9 | 10,804.2–**11,252.7**（全负载窗私有最大值出现在此阶段） | 56→83 |
| recalls 期间（21 样本） | 802.9–**1,066.4**（周期性 +~430 MB 瞬时尖峰） | 10,825.8–11,107.8 | 85→**113**（峰值） |
| 分页读三连（2 样本，17:30:10/15） | 857.2–866.8 | 10,863.3–10,898.2 | 113→58 |
| 负载结束平台（17:30:25 起，44 样本） | **856.1（恒）** | **10,860.6（恒）** | 58，句柄 511 |

pins 阶段 17:28:15 单样本（WS 948.9 / 私有 11,252.7）是整个负载窗的私有提交最大值：与 `remember` 写路径执行（含近重复 band 向量检索，3 GB lance 表读路径）同时点，且下一样本即全额回落（+446.0 → −444.4 MB）——一次性瞬态，不构成留存；其余 ~64 个请求再未越过 11,107.8，该单样本的精确构成未做符号级取证（§覆盖边界）。

**衰减观测（cleanup 后 5 min × 30 s）**：WS 856.9 / 私有 10,860.6 / 线程 58——**五分钟逐样本恒定，零进一步衰减**；终检两次（17:48:23 +08 uptime 反推 [在盘 healthz 数值，会话记录输出]；≈17:52:48 复读）：WS 741.3 → 971.9 / 私有 10,831.3 → 10,848.5（转录块在案）——ambient 流量下 WS 在 ~0.7–1.0 GB 带内随近期活动波动，提交稳定在 ~10.8 GB。负载留存的净台阶：**WS +218 MB、私有 +56 MB**（73 请求合计），此后不再变化。

## 隔离对照（分解实验，iso1 端口 7795 / iso2 端口 7796 / 复测 iso3 端口 7797）

iso1（干净家，boot 后健康 ~4 s；首次 embed 时模型被**下载**进临时家——isohome 模型文件在盘可证）私有提交阶梯，除标注外全部 **CSV 在盘**（`iso-boot.csv`，5 s × 60）：

| 态 | WS (MB) | 私有提交 (MB) | 步进 |
| --- | --- | --- | --- |
| boot 空闲（模型懒加载，未触；17:40:02–17:42:02） | 125.2 | 580.8 | —— |
| 首次短 recall（17:42:32 样本；20.6 s 含模型下载 + 加载） | 972.7 | 1,446 | **+865** = bge-m3 权重 + ORT 运行时/工作区 + tokenizer |
| **一次** 450k 字符 pin（tokenize 截断到 8192；17:43:32–17:44:33，含请求内与落定段） | 8,461–8,544.6 | **10,075.6–10,075.8** | **+8,629.8 ≈ 8.6 GB** |
| 平台无衰减（CSV 段内 17:43:32→17:44:33） | （段内已含上行） | 10,075.7 → 10,075.6 | 0 |

（请求返回后的单次控制台 privws 复核（≈17:44–46，会话记录，已转录台账转录块）读得 WS 8,677.2 / 私有 10,081.1、数分钟后 8,677.6 / 10,081.0——与 CSV 平台差 ≤5.3 MB（0.05%），佐证同一平台。）

iso2 反事实，**复测存档**（`iso2-recheck.csv`，5 s × 54，台账 #24：全新家 isohome3、模型目录自带 `.complete` 侧车复制免下载、boot → 20 短 pins + 10 短 recalls（18:24:15–28，`iso2-recheck-timing.tsv` 全 200）→ 平台）：boot 基线 **126.8 / 580.8** → load 后平台 **1000.3 / 1470.7，末 6 样本逐样本恒定**，全程 private max 1,472.0——**无多 GB 尖峰**，平台与 iso1 模型加载台阶（1,446）同级（+25 MB 为 20 条 pin 落库与召回路径的增量）。首轮同配方读数（会话记录，转录块：580.6 → 1,472.5 / WS 126.8 → 1,002.7）与复测一致（0.2% 内）。加载的原生模块清单（`Get-Process …Modules`，模块内存口径，控制台表已转录台账）：`_lancedb.pyd` **185.1 MB**、onnxruntime pybind 19.1 MB、openblas 19.7 MB、arrow 系五件 ~41.9 MB、tokenizers 7.5 MB、python312 6.7 MB、pydantic_core 5.5 MB、sqlite3 1.55 MB——原生模块合计 ~290 MB 量级，**解释不了 8.6 GB 步进；唯一能解释它的是单次 8192-token 前向的推理内存**。

## 归因结论

**主导项：嵌入式 bge-m3 embedder 的 ONNX Runtime 推理内存，触发条件是单条超长输入，且不归还。** 机制链：(a) `bge_m3_onnx` 懒加载（boot 不含模型——iso1 boot 580 MB 证实）；(b) `_tokenize` 把批内最长序列 pad/truncate 到 `max_length=8192`，bge-m3 主干为 XLM-RoBERTa-large 形态（1024 维，24 层 × 16 头），seq=8192 下**单层注意力分数矩阵 ≈ 8192²×16×4 B ≈ 4.3 GB**，若干层换算的瞬态推理内存即多 GB 量级；(c) ORT CPU EP 默认 BFC arena **只增不减**（high-water 保留），一次尖峰即永久抬升提交；(d) 写路径无批处理（所有调用点单文本 embed），尖峰由**单条**巨大 chunk 文本（一次巨型粘贴/日志转储被捕获为一个 chunk）触发。生产 daemon 的 10.8 GB 提交与 iso1 单 pin 后的 10.08 GB 同量级（差 ~0.7 GB = LanceDB 连接/页缓存 + 40 h ambient 搅动），一次事件即足以解释全部量级。次要项排序：LanceDB 读路径留存（73 请求 +56 MB 私有 / +218 MB WS，`candidate_k=80` 行含全文+稀疏列，有界）、框架底座 580 MB、sqlite 三库可忽略（<1 MB 磁盘，无可见内存贡献）。

**增长签名判定：稳定-台阶留存（stable-with-step-retention），非经典泄漏。** 判据：空闲零增长（10 min Δ+0.5 MB 私有）；负载瞬时尖峰回落到平台而非累积（72 样本包络内反复 +430 MB 尖峰均回落）；平台期零漂移（decay 5 min 恒定）。但**不是无风险**：提交记账按长输入事件**棘轮式抬升且永不归还**——每次新的超长捕获都会把提交推上更高的台阶，且 10.8 GB 计入系统提交上限，在小内存/小页面文件机器上是 OOM/抖动形态；生产主机当前仅表现为页面文件压力。驻留（WS）与提交（commit）必须分开治理：R3/R4 治驻留，R1/R2 治提交。

## 削减建议（按预期收益排序；每条各需独立评审批次，本批零改动）

| # | 建议 | 预期收益 | 依据 | 批次要点 |
| --- | --- | --- | --- | --- |
| R1 | **cap embedder 输入长度**：`bge_m3_onnx` `max_length` 8192 → 512/1024（或按 chunk 尺寸分档） | 消灭多 GB 台阶的触发面（收益最大、改动最小） | iso1 单 pin +8.6 GB vs iso2 短文本零尖峰；正常 chunk 文本由 segmenter 约束、远低于 8k token，超长本就是异常输入 | 需召回质量 eval（warm/rescue harness）定档；理论锚沿用 segmenter 的 chunk 尺寸设计，不新借 |
| R2 | **ORT arena 配置**：`options.enable_cpu_mem_arena = False`（或 `arena_extend_strategy=kSameAsRequested`） | 把 high-water 留存变为归还；单行改动直击留存机制 | ORT BFC arena 只增不减是步进台阶的直接机制 | 推理延迟换内存，需基准对比 |
| R3 | embedder 空闲卸载：N 分钟无 embed 调用则 drop session，下次调用懒重建 | 驻留 WS −~865 MB（对提交棘轮无效） | 生产 idle WS 637 MB 中 ~544 MB 是权重 | 懒加载锁已存在；重建成本 = 模型在盘时 ~2 s（iso3 复测首 pin 实测；20.6 s 是空家含下载的首建） |
| R4 | 服务拆分：embed 下放子进程 | 尖峰/棘轮与 daemon 提交隔离，顺带隔离未来 embedder 侧泄漏 | 单进程 embedded 是当前形态根因 | 架构级改动，收益最大面也最大，最后评估 |
| R5 | 观测面：healthz/doctor 暴露**私有提交**（不只 WS）并阈值告警 | 让下次棘轮早可见；零风险 | 本次 10.8 GB 隐匿 40 h 未被任何表面报告 | 可并入任意小批 |
| R6 | LanceDB 读路径（低优先级）：维持现有 `candidate_k`/limit 约束 | 实测留存小（+56 MB/73 请求） | 分页读包络有界 | 库规模显著增长后复测 |

**未来 reduce 批次的验收记录（预登记）**：隔离家 + 备用端口复跑本批协议——(i) 同一条 450k 字符 pin 后私有提交 ≤ ~2 GB（R1+R2 生效判据）；(ii) 短负载平台私有提交 ≤ ~1.6 GB；(iii) 召回质量 eval 在 cap 档位不劣化（现有 harness 通过线）；(iv) 生产 7788 升级后 idle 平台 WS ≤ ~700 MB。

## 覆盖边界（本批证明什么、不证明什么）

- **证明**：单机、单守护进程（0.2.0 已装体，embedded 单进程）在真实 3 GB 存储上的空闲基线、有界负载响应、衰减形态；私有提交阶梯的**行为级**归因（boot → 模型 → 长输入三步，每步有隔离实验背书）；增长签名判定；短文本反事实。
- **不证明**：(1) 8.6 GB 步进的符号级构成（无 VMMap/堆走查；arena 只增不减是 ORT 已知默认行为 + 尖峰时序与 embed 调用逐点吻合的行为证据，非调试器证据）；(2) 生产 daemon 历史上**哪一条**捕获触发了它的台阶（行为上不可知，量级上单事件已充分）；(3) dream 链路内存（ollama 全程不可达、dream paused，未测——与生产当前态一致）；(4) 其他 OS/版本/更大库规模下的形态；(5) ORT 版本升级对 arena 行为的影响。
- **不动面**：捕获中性、溯源不可变、verbatim 通道三条红线零触碰；对真实存储的全部写触仅 20 条标记合成 pin（其中 19 条按设计并入第 1 条的 chunk）+ 对该单一 chunk 的定向 `forget_this` 删除；端点契约零改动。

## 边界事件与偏差（全部如实记录，台账 27 条为完整清单）

1. **首次清理尝试全部 422（无删除发生）**：python 文本模式把 chunk id 清单写成 CRLF，`forget_this` body 带控制字符被校验拒绝；修正后重试，未产生任何真实存储变更。
2. **pin 塌缩（设计行为的现场演示）**：20 条仅差计数器的 pin 触发 near-duplicate band，2–20 号全部 `superseded` 并入 1 号 chunk——最终合成足迹为**单一 chunk** `dabe043937e44f4f96180ae5fd2343e6`，`forget_this` 物理删除（响应 `removed.chunks=[dabe0439…]`）；对同一 id 的 18 次重复请求 404（已删）。真实存储最终**零本批残留**。
3. **init 模板的 isolated 图实例路径指向真实家**：模板默认 `~/.mnemoseed-local/isolated.db`；boot 前**已改写**指向 iso 家——真实 `isolated.db` 从未被探针进程打开。
4. **首次 iso boot 干净失败**：临时家无 config.toml → `RuntimeError: no 'isolated' graph instance configured`，无监听残留；`init` 后复跑成功。
5. **生产守护进程全程未停启**：boot 时刻不变（uptime 连续 40.2 h → 40.9 h，三次 healthz）；探针负载留下 WS +218 MB / 私有 +56 MB 的稳定平台（§负载响应），17:33 后 WS 缓慢松弛，终检两次读数 741.3 → 971.9 MB（ambient 流量波动带，私有提交稳定 ~10.8 GB）；observability 的 console actor +74 为本批请求，审计在案。
6. **单请求耗时精度损失**：`bc` 缺失导致 load-timing.tsv 第 4 列为 0.000，耗时改由时间戳差导出（~1 s/pin、~2.1 s/recall、atlas ~4 s）；判定不受影响。
7. **文档修正轮（QA 判定后）+ 一次自身杂散写触**：首轮 PRD 经 QA 判 0 BLOCKER / 3 IMPORTANT / 6 NIT（阶梯头条数值未绑在盘 CSV、iso2 反事实无采样留档、observability 行与其在盘文件矛盾）；修正轮将 #16-17 改绑 `iso-boot.csv`（步进 +8,629.8 ≈ 8.6 GB，结论不变）、iso2 反事实以 isohome3/7797 **复测存档**（`iso2-recheck.csv`，结果与首轮 0.2% 内一致，台账 #24）、observability 行改引 `obs-before.json` 在盘值、计数（75 件 / 371,362）/ 时标（boot 双源、终检 uptime 反推）逐项更正——**生产侧零新增探针**。修正轮自身发生一次杂散写触（台账 #26）：iso3 boot 调用把整条 `cd && up` 链后台化，前台 healthz 重定向落在了主仓根 `G:\Development\MnemoSeed\mnemoseed-local\iso3-healthz.json`（w219 写域之外）；该文件已字节一致复制进证据树（`iso3-healthz.json`，sha256 在台账）后从主仓根删除，主仓 `git status` 复核干净。

## 理论锚

本批为量测与归因批次，不引入新特性、新记忆机制，**无新增借用理论**；不引入任何神经科学/心理学规律支持任何削减建议——R1 的长度约束沿用 segmenter/chunk 尺寸的既有设计锚，R2–R6 均为平台机制（ORT arena、进程内存记账）工程事实。本批触到的记忆行为（pin near-duplicate supersede band）是既有已锚机制（design/03/09）的现场观测，非新借用。

## 残留未知

1. 生产 daemon 提交台阶的历史触发者（哪条超长捕获）——无堆取证不可知；如需符号级证据，须立一个含 VMMap/heap snapshot 授权的诊断批次。
2. 8.6 GB 步进中 arena 与其他分配器（python pymalloc/UCRT、Rust 侧）的精确占比。
3. dream 链路（ollama 恢复后）的内存形态——本批 dream 全程 paused。
4. 更大库规模（chunks.lance 增长后）下 LanceDB 页缓存驻留的重新量测点。
5. 证据树清理：`%LOCALAPPDATA%\Temp\w219-memprof\`（含 iso 三家、采样表、脚本、台账；isohome 内含 1 条长合成 chunk，isohome2/isohome3 各含 20 条标记合成 pin——均在临时树内而非真实存储）由 owner 按需删除；本批按边界未执行任何仓库外删除（修正轮对自身杂散写的清除见边界事件 7）。

## 验收记录

文档核对的证据**在盘**（`%LOCALAPPDATA%\Temp\w219-memprof\`，逐件可开；21 件证据的 sha256 块见台账）：采样表五件（`idle.csv` 20 样本 / `load.csv` 72 样本 / `decay.csv` 10 样本 / `iso-boot.csv` 60 样本 / `iso2-recheck.csv` 54 样本）、`ledger.md`（27 条边界台账 + 控制台读数转录块 + 哈希块）、负载与清理响应留档（`load-responses/` 75 件含 observability 前后对照、`cleanup-responses/` 19 件）、`iso2-recheck-timing.tsv`、三脚本 + 两采样器、iso 三家 boot 日志与 healthz 快照、`synthetic-chunk-ids.txt`。关键结论的原始行（全部 CSV 在盘）：idle 20 样本恒定段（636.6–637.9 / 10,804.2–10,804.7）；load 阶段包络 pins 638.0–948.9 WS / 10,804.2–11,252.7 私有（最大值在 pins 阶段 17:28:15 单样本，与 remember 写路径的 band 检索同时点，下一样本全额回落）、recalls 802.9–1,066.4 / 10,825.8–11,107.8、平台 856.1 / 10,860.6 恒定；decay 856.9 恒定段；**iso1 阶梯 580.8 → 1,446 → 10,075.8（步进 +8,629.8 ≈ 8.6 GB，`iso-boot.csv`）**；**iso2 复测平台 1,470.7 恒定（`iso2-recheck.csv`）**；forget 响应 `removed.chunks=["dabe0439…"]`（`cleanup-responses/forget-1.json`）。控制台-only 的读数（privws 各次、healthz 各次、模块清单）已按声明出处转录进台账转录块，凡与 CSV 同量的以 CSV 为准。独立 QA（首轮 NOT CLOSABLE 判定 + 本修正轮落实）、主控复核与 #219 整体收口待主控记录；本批不声明 #219 完成（观测半批，reduce 为独立评审批次）。
