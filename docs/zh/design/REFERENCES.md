# REFERENCES · 本仓理论与文献登记表

> 本系列架构设计文档（`docs/zh/design/00…08`）引用的理论与工程文献统一登记于此。
> 核验状态图例（与主仓三值图例一致）：
> **✅** = Crossref 核验命中（author/year/venue）
> **📕** = 经典专著（无 DOI，教科书级常识引用）
> **⚠️** = 高置信经典但本轮未直接核验——标注待抽查（诚实规则：未核验绝不当已核验）
>
> 「同主仓 Rxx」指主仓 `mnemoseed/docs/REFERENCES.md` 的对应编号与状态；本地编号独立、从 R1 起重排。凡与主仓同文献的条目，状态照抄主仓（含 📕/⚠️），不擅自升级。

---

## 记忆系统架构理论

| # | Citation | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| R1 | McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems in the hippocampus and neocortex. *Psychological Review*, 102(3), 419–457. DOI: 10.1037/0033-295X.102.3.419 | 互补学习系统（双存储架构：海马/皮层分工） | 00、02 | ✅ 同主仓 R1 |
| R2 | Wilson, M. A., & McNaughton, B. L. (1994). Reactivation of hippocampal ensemble memories during sleep. *Science*, 265(5172), 676–679. | 睡眠期海马重放巩固（梦境引擎） | 00、02 | ✅ 同主仓 R2 |
| R3 | Frey, U., & Morris, R. G. M. (1997). Synaptic tagging and long-term potentiation. *Nature*, 385, 533–536. | 捕获选择性编码门 | 00 | ✅ 同主仓 R3 |
| R4 | Tulving, E., & Thomson, D. M. (1973). Encoding specificity and retrieval processes in episodic memory. *Psychological Review*, 80(5), 352–373. | 编码特异性；cue 元数据面；09 救援准入主锚 | 00、01、03、05、06、09 | ✅ 同主仓 R4 |
| R5 | Nader, K., Schafe, G. E., & LeDoux, J. E. (2000). Fear memories require protein synthesis in the amygdala for reconsolidation after retrieval. *Nature*, 406, 722–726. | 再巩固改写协议 | 00 | ✅ 同主仓 R5 |
| R6 | Tononi, G., & Cirelli, C. (2003). Sleep and synaptic homeostasis: A hypothesis. *Brain Research Bulletin*, 62(2), 143–150.（extended 2014, *Neuron*） | 突触稳态（SHY）；衰减引擎/深睡清扫 | 00、03 | ✅ 同主仓 R6 |
| R7 | Johnson, M. K., Hashtroudi, S., & Lindsay, D. S. (1993). Source monitoring. *Psychological Bulletin*, 114(1), 3–28. | 来源监控（写入侧 provenance/审计永不衰减，不依赖事后归因） | 00、04、05、06 | ✅ 同主仓 R7 |
| R8 | Ebbinghaus, H. (1885/1913). *Memory: A Contribution to Experimental Psychology*. | 艾宾浩斯遗忘曲线（只锚曲线形状） | 00、03、09 | 📕 同主仓 R8 |
| R9 | Cepeda, N. J., Pashler, H., Vul, E., Wixted, J. T., & Rohrer, D. (2006). Distributed practice in verbal recall tasks: A review and quantitative synthesis. *Psychological Bulletin*, 132(3), 354–380. | 间隔效应（强化回弹） | 00、03 | ✅ 同主仓 R9 |
| R10 | Hebb, D. O. (1949). *The Organization of Behavior*. Wiley. | 赫布定律（近重复命中即回弹，编码时强化） | 00、01、03 | 📕 同主仓 R10 |
| R11 | Godden, D. R., & Baddeley, A. D. (1975). Context-dependent memory in two natural environments: On land and underwater. *British Journal of Psychology*, 66(3), 325–331. | 上下文依赖记忆 | 00 | ✅ 同主仓 R12 |
| R12 | Brainerd, C. J., & Reyna, V. F. (1990). Gist is the grist: Fuzzy-trace theory and the new intuitionism. *Developmental Review*, 10(1), 3–47. DOI: 10.1016/0273-2297(90)90003-M | verbatim/gist 双轨架构命名 | 00、01 | ✅ 同主仓 R13 |
| R13 | Miller, G. A. (1956). The magical number seven, plus or minus two. *Psychological Review*, 63(2), 81–97. DOI: 10.1037/h0043158 + Cowan, N. (2001). The magical number 4 in short-term memory. *Behavioral and Brain Sciences*, 24(1), 87–114. | 不借清单依据（7±2 不得作任何数字常量出处）；工作记忆容量替代锚 | 00、01、03（Cowan）、06 | ✅ 同主仓 R15（组合条目） |

## 情绪与记忆

| # | Citation | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| R14 | McGaugh, J. L. (2000). Memory—A century of consolidation. *Science*, 287(5451), 248–251. | 情绪调制巩固强度（不决定记什么） | 00、01 | ✅ 同主仓 R16 |
| R15 | Kensinger, E. A., & Corkin, S. (2003). Memory enhancement for emotional words: Are emotional words more vividly remembered than neutral words? *Memory & Cognition*, 31, 1169–1180. | arousal 驱动轴（valence 降级为线索） | 00、01 | ✅ 同主仓 R17 |
| R16 | Neisser, U., & Harsch, N. (1992). Phantom flashbulbs: False recollections of hearing the news about Challenger. In Winograd & Neisser (Eds.), *Affect and Accuracy in Recall*. | flashbulb 悖论：情绪分永不喂 confidence | 00、01 | ✅ 同主仓 R19 |
| R17 | Yerkes, R. M., & Dodson, J. D. (1908). The relation of strength of stimulus to rapidity of habit-formation. *Journal of Comparative Neurology and Psychology*, 18(5), 459–482. | 倒 U 型饱和（arousal cap） | 00、01 | ✅ 同主仓 R20 |
| R18 | Easterbrook, J. A. (1959). The effect of emotion on cue utilization and the organization of behavior. *Psychological Review*, 66(3), 183–201. | 注意窄化（peripheral_gaps 标记） | 00、01 | ✅ 同主仓 R21 |
| R19 | Christianson, S.-Å. (1992). Emotional stress and eyewitness memory: A critical review. *Psychological Bulletin*, 112(2), 284–309. | 武器聚焦（强中心/弱外围） | 00、01 | ✅ 同主仓 R22 |
| R20 | Russell, J. A. (1980). A circumplex model of affect. *Journal of Personality and Social Psychology*, 39(6), 1161–1178. DOI: 10.1037/h0077714 | V/A 二维环形情感模型 | 00、01 | ✅ 同主仓 R23 |
| R21 | Mohammad, S. M. (2018). Obtaining reliable human ratings of valence, arousal, and dominance for 20,000 English words. *Proceedings of ACL 2018*. | NRC VAD 词典（自动文本情绪评分） | 00、01 | ✅ 同主仓 R27 |
| R22 | Craik, F. I. M., & Lockhart, R. S. (1972). Levels of processing: A framework for memory research. *Journal of Verbal Learning and Verbal Behavior*, 11(6), 671–684. DOI: 10.1016/S0022-5371(72)80001-X | 深度加工（importance_hint 显式用户加权） | 00、01 | ✅ 同主仓 R28 |

## 前瞻记忆与再认

| # | Citation | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| R23 | Anderson, J. R., & Schooler, L. J. (1991). Reflections of the environment in memory. *Psychological Science*, 2(6), 396–408. DOI: 10.1111/j.1467-9280.1991.tb00174.x | ACT-R base-level 排序动力学（频率/近因/间隔环境规律） | 00、01、03、05、06 | ✅ Crossref 核验命中（DOI 直接解析） |
| R24 | Murdock, B. B., Jr. (1962). The serial position effect of free recall. *Journal of Experimental Psychology*, 64(5), 482–488. DOI: 10.1037/h0045106 | 近因优势（时序邻近检索依据） | 00、05、06 | ✅ Crossref 核验命中 |
| R25 | Howard, M. W., & Kahana, M. J. (2002). A distributed representation of temporal context. *Journal of Mathematical Psychology*, 46(3), 269–299. DOI: 10.1006/jmps.2001.1388 | 时序语境（TCM；时间邻近加权） | 00、05、06 | ✅ Crossref 核验命中 |
| R26 | McDaniel, M. A., & Einstein, G. O. (2000). Strategic and automatic processes in prospective memory retrieval: A multiprocess framework. *Applied Cognitive Psychology*, 14(7/S1), S127–S144. DOI: 10.1002/acp.775 | 前瞻记忆多进程框架 | 00、05、06 | ✅ Crossref 核验命中（DOI 直接解析） |
| R27 | Yonelinas, A. P. (2002). The nature of recollection and familiarity: A review of 30 years of research. *Journal of Memory and Language*, 46(3), 441–517. DOI: 10.1006/jmla.2002.2864 | 双加工再认（recollection/familiarity） | 00、06 | ✅ Crossref 核验命中 |

## 提取、遗忘与注意

| # | Citation | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| R28 | Anderson, M. C., Bjork, R. A., & Bjork, E. L. (1994). Remembering can cause forgetting: Retrieval dynamics in long-term memory. *Journal of Experimental Psychology: Learning, Memory, and Cognition*, 20(5), 1063–1087. DOI: 10.1037/0278-7393.20.5.1063 | 提取诱发遗忘（检索-衰减正反馈；消费证据强化反向项） | 00、03、05、06 | ✅ 同主仓 R40 |
| R29 | Wixted, J. T. (2004). The psychology and neuroscience of forgetting. *Annual Review of Psychology*, 55, 235–269. | 干扰论（遗忘主因是干扰非时间） | 00、03 | ✅ 同主仓 R41 |
| R30 | MacLeod, C., Mathews, A., & Tata, P. (1986). Attentional bias in emotional disorders. *Journal of Abnormal Psychology*, 95(1), 15–20. | 捕获中立红线（评分不读 anima/偏好） | 01 | ✅ 同主仓 R45 |

## 梦境预算与系统动力学

| # | Citation | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| R31 | Borbély, A. A. (1982). A two process model of sleep regulation. *Human Neurobiology*, 1(3), 195–204. | 睡眠二过程（睡眠压力→预算动机注脚） | 00、02 | ⚠️ 同主仓 R48（状态照抄：原刊已停刊、未在 Crossref 直接命中；PubMed PMID 7185792） |
| R32 | Little, J. D. C. (1961). A proof for the queuing formula: L = λW. *Operations Research*, 9(3), 383–387. DOI: 10.1287/opre.9.3.383 | Little 定律（到达率须 ≤ 排空能力） | 00、02 | ✅ 同主仓 R49 |
| R33 | Dement, W. (1960). The effect of dream deprivation. *Science*, 131(3415), 1705–1707. | REM 反弹（积压期预算扩张的生理对位） | 00、02 | ✅ 同主仓 R50 |

## 促进入口门与反馈

| # | Citation | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| R34 | Bartlett, F. C. (1932). *Remembering: A Study in Experimental and Social Psychology*. Cambridge. | 重建性记忆：巩固引入失真，写入前须验证 | 00、02 | ✅ 同主仓 R51（1995 重印版 DOI 10.1017/cbo9780511759185） |
| R35 | Loftus, E. F. (2005). Planting misinformation in the human mind. *Learning & Memory*, 12(4), 361–366. DOI: 10.1101/lm.94705 | 误导信息效应：重建=失真风险，verify-before-commit | 00、02 | ✅ 同主仓 R52 |
| R36 | Nelson, T. O., & Narens, L. (1990). Metamemory: A theoretical framework and new findings. *Psychology of Learning and Motivation*, 26, 125–173. DOI: 10.1016/s0079-7421(08)60053-5 | 元记忆：用户 pin/correction = 最高权威信号（钉住的显著性信号来源） | 00、09 | ✅ 同主仓 R53 |
| R37 | Stickgold, R., & Walker, M. P. (2013). Sleep-dependent memory triage. *Nature Neuroscience*, 16(2), 139–145. DOI: 10.1038/nn.3303 | 睡眠依赖记忆 triage（选择性巩固，非全量回放） | 00、02 | ✅ 同主仓 R54 |

## 多主体协调与任务切换（B2.8 互认知）

| # | Citation | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| R38 | Wegner, D. M. (1987). Transactive memory: A contemporary analysis of the group mind. In B. Mullen & G. R. Goethals (Eds.), *Theories of Group Behavior* (pp. 185–208). Springer. DOI: 10.1007/978-1-4612-4634-3_9 | 交互记忆系统：目录是索引不是副本（TA-10） | B2.8 | ✅ Crossref 核验命中（book-chapter，pp. 185–208） |
| R39 | DeChurch, L. A., & Mesmer-Magnus, J. R. (2010). The cognitive and motivational mechanisms underlying the relation between transactive memory systems and team performance. *Journal of Applied Psychology*, 95(2), 352–373. | TMS 结构质量与团队绩效正相关的团队层元分析（TA-10 实证补强） | B2.8 | ⚠️ 高置信经典但本轮未直接核验（两次 DOI 试探均未命中）——待抽查晋级或替换 |
| R40 | Clark, H. H., & Marshall, C. R. (1981). Definite reference and mutual knowledge. In A. K. Joshi, B. L. Webber, & I. A. Sag (Eds.), *Elements of Discourse Understanding*. Cambridge University Press. | 相互知识/共同基础：协调所需的最小相互知识（TA-11） | B2.8 | 📕 经典专著章节（教科书级常识引用） |
| R41 | Clark, H. H., & Brennan, S. E. (1991). Grounding in communication. In L. B. Resnick, J. M. Levine, & S. D. Teasley (Eds.), *Perspectives on Socially Shared Cognition*. APA. | grounding 成本及其介质依赖（TA-11） | B2.8 | 📕 经典专著章节（教科书级常识引用） |
| R42 | Monsell, S. (2003). Task switching. *Trends in Cognitive Sciences*, 7(3), 134–140. DOI: 10.1016/S1364-6613(03)00028-7 | 任务切换代价综述（TA-12；pull 优于 push 的理论根据之一） | B2.8 | ✅ Crossref 核验命中（DOI 直接解析） |
| R43 | Altmann, E. M., & Trafton, J. G. (2002). Memory for goals: An activation-based model. *Cognitive Science*, 26(1), 39–83. DOI: 10.1207/s15516709cog2601_2 | 目标激活模型：中断恢复代价随挂起时长增长（TA-12） | B2.8 | ✅ Crossref 核验命中（DOI 直接解析） |


## 保留动力学重设计（钉住条目去永久化）

| # | Citation | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| R44 | Roozendaal, B., & McGaugh, J. L. (2011). Memory modulation. *Behavioral Neuroscience*, 125(6), 797–824. DOI: 10.1037/a0026187 | 显著性调制巩固：显著事件巩固得更持久（慢衰减档的实证主锚） | 09 | ✅ Crossref 核验命中（DOI 直接解析，author/year/venue 全对） |
| R45 | McGaugh, J. L. (2018). Emotional arousal regulation of memory consolidation. *Current Opinion in Behavioral Sciences*, 19, 55–60. DOI: 10.1016/j.cobeha.2017.10.003 | 同上补强：调制作用于编码后时间窗、决定持久性（不改变内容与真实性） | 09 | ✅ Crossref 核验命中。注：任务书原引"McGaugh 2018 Annual Review of Psychology"经查为混淆——McGaugh 的 ARP 综述实为 2015《Consolidating memories》；本条是同年可核验的署名综述 |
| R46 | Roediger, H. L., III, & Karpicke, J. D. (2006). Test-enhanced learning: Taking memory tests improves long-term retention. *Psychological Science*, 17(3), 249–255. DOI: 10.1111/j.1467-9280.2006.01693.x | 提取练习/测试效应：提取行为本身巩固记忆（召回即强化的主锚） | 09 | ✅ Crossref 核验命中（摘要含规律原文级表述） |
| R47 | Tulving, E., & Pearlstone, C. (1966). Availability versus accessibility of information in memory for words. *Journal of Verbal Learning and Verbal Behavior*, 5(4), 381–391. DOI: 10.1016/S0022-5371(66)80048-8 | 可用 ≠ 可及：在册但暂时取不回；给合适线索可达性恢复（索引残迹 + 线索救援的存在论依据） | 09 | ✅ Crossref 核验命中 |
| R48 | Koriat, A. (1993). How do we know that we know? The accessibility model of the feeling of knowing. *Psychological Review*, 100(4), 609–639. DOI: 10.1037/0033-295X.100.4.609 | 知道感："认得出但想不起"可触发翻找行为（残迹一行钩子的元认知依据） | 09 | ✅ Crossref 核验命中 |

> 本节未入册说明：McGaugh (2004) "The memory consolidation hypothesis"（*Learning & Memory* 11(6), 668–673）为本轮**未能核验**的经典——试探 DOI 10.1101/lm.75404 实际解析到同刊他文（Quirk, G. J., 11(2), 125–126），标题检索亦未命中该文。按诚实规则不入册；其假说陈述已由 R44/R45 覆盖。

## 工程与行业文献（I 类）

| # | Citation | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| I1 | Zhong, W., Guo, L., Gao, Q., Ye, H., & Wang, Y. (2023). MemoryBank: Enhancing large language models with long-term memory. arXiv:2305.10250（AAAI 2024）. | 语料形态参考（SiliconFriend；非理论锚） | 08 | ✅ arXiv 命中（10.48550/arXiv.2305.10250） |
| I2 | He, J., Zhu, L., Wang, R., Wang, X., Haffari, R., & Zhang, J. (2024). MADial-Bench: Towards real-world evaluation of memory-augmented dialogue generation. arXiv:2409.15240（NAACL 2025）. | 语料形态参考（记忆增强对话，主动+被动回忆；非理论锚） | 08 | ✅ arXiv 命中（10.48550/arXiv.2409.15240） |

## 工程事实（非文献条目）

| # | Fact | Used for | 对应本篇 | Status |
|---|---|---|---|---|
| — | OpenCode 宿主持久化完整会话史（`client.session.messages` 可读回），daemon 捕获为派生视图——源头不丢，视图即可重建 | 会话续传/重建的宿主侧事实 | 07 | 非文献条目/工程事实（宿主行为，无文献引用） |

---

*维护规则：任何引用必须在同一次变更中登记并带核验状态；⚠️ 条目后续轮次抽查晋级或替换。*
