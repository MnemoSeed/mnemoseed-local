# PRD-B7 · 多 Provider Dream 配置系统（拆除默认 Ollama）

- 状态：planning（设计评审 CLOSABLE-with-adjustments，待 owner 冻结 + 开工授权）
- 关联：issue #381（本批次章程）；吸收 #55（BYOK 提前立项）、#310（安装器无 Ollama 半边）
- 日期：2026-11-10 owner 指令立项，2026-11-10 solution-architect 独立设计评审通过
- 设计评审记录：`solution-architect` 2026-11-10 —— SHIP-WITH-ADJUSTMENTS，BL-1/BL-2/BL-3 已折入本 PRD

## 1. 背景与目标（owner 指令 2026-11-10）

1. **mnemoseed-local 不再默认安装 Ollama**。Ollama 改为安装器里的显式可选项（`--install-ollama`）。
2. **Dreaming 的模型选择改为 OpenCode 式多 Provider 配置**：
   - 内置知名 Provider 预设：OpenAI、Anthropic、Google Gemini、DeepSeek、Moonshot Kimi、xAI Grok、OpenRouter、NVIDIA NIM + **Custom Provider**（自填 base_url + key，OpenAI 兼容）。
   - 配置流：选 Provider → 提供_API Key_（存 SecretStore）或 _OAuth_（支持的 Provider，OpenAI 先行）→ **Test Connection** 确认可用 → 模型清单（过长可搜索）→ 绑定给 Dreaming。
3. **本地 Ollama 自动检测**：本机已在运行的 Ollama 自动出现在已配置 Provider/模型列表中，可直接选用。
4. **开发机 dogfood**：dream 路由经新配置流指向本机 Ollama 的 `heretic-9b`（检测清单提供精确 tag）。
5. **取消为"默认捆绑 Ollama 不让用户察觉"而设的全部遗留设置**（登录等待、遗留隐藏任务迁移等）。

### 被推翻的既有记录（需在 roadmap 中修订）

- B4d 批次注记"不改 `DEFAULT_LLM_ROUTES` 测试锚、Ollama 保持默认宿主"——本指令取代之（roadmap 条目末尾补记修订）。
- #55 BYOK"Phase B 后才立项"——提前立项并扩大为本批次，#55 随本批次落地 PR 关闭。
- #310"无 Ollama 的 capture-and-recall 安装路径"——安装器半边被本批次吸收，验收条件并入本批次 Done-when。

## 2. 理论锚

**借用的经验规律（1 条）**

1. **默认效应 / 现状偏差（default effect / status quo bias）**
   - 出处：Johnson & Goldstein 2003（《Science》，器官捐献默认选项跨国自然实验：默认加入 vs 默认退出导致同意率个位数 ↔ 90%+ 的系统差异）；Samuelson & Zeckhauser 1988（status quo bias 受控实验）。
   - 已验证规律：面对预置选项，绝大多数人保持默认、不做主动选择。
   - 设计规则：安装器与向导**绝不静默预装或预选付费 provider**；新装出厂不带 dream 路由（显式配置是接入推理宿主的唯一路径）；向导不预选任何 provider 卡片。这也是"取消默认 Ollama"的理论对应面：把默认从"替用户选择推理宿主"改为"无默认、显式绑定"。

**明确不借用（not borrowed）**

- 决策疲劳（decision fatigue，Vohs et al. 2008）——重复验证失败、元分析结论不稳；
- 选择过载（choice overload，Iyengar & Lepper 2000）——Scheibehenne et al. 2010 元分析无可靠均值效应；"可搜索模型清单"是纯 UX 决策，不挂理论锚；
- 一切 pop-neuro 叙事——不入册。

**机制边界**：本批次全部是配置管线（catalog / schema / HTTP / console / installer），不触碰记忆机制本身，不声明任何记忆侧效应。

## 3. 设计

### 3.1 Provider 预设目录（S1，纯数据零 I/O）

新文件 `src/mnemoseed_local/llm/providers.py`：冻结的 `ProviderPreset` dataclass + `PROVIDER_PRESETS` 表。

| preset id | 协议 | base_url | 备注 |
|---|---|---|---|
| openai | openai_compatible | `https://api.openai.com/v1` | preset 旋钮 `token_param=max_completion_tokens`（推理模型拒收 `max_tokens`，评审 I-3）；模型清单走策展为主 |
| anthropic | **anthropic（新驱动）** | `https://api.anthropic.com` | 原生 `/v1/messages`（见 3.2） |
| google | openai_compatible | `https://generativelanguage.googleapis.com/v1beta/openai` | 官方 GA 兼容层，**v1 不做原生驱动**（评审 I-4）；模型 id 带 `models/` 前缀为无害字符串 |
| deepseek | openai_compatible | `https://api.deepseek.com/v1` | |
| moonshot | openai_compatible | `https://api.moonshot.ai/v1` | 默认国际端点，base_url 可编辑（国内 `api.moonshot.cn`） |
| xai | openai_compatible | `https://api.x.ai/v1` | |
| openrouter | openai_compatible | `https://openrouter.ai/api/v1` | 模型 id 形如 `vendor/name`（字符串安全）；清单巨大→策展默认+搜索 |
| nvidia | openai_compatible | `https://integrate.api.nvidia.com/v1` | 托管 NIM 端点；自建 NIM 走 custom |
| custom | openai_compatible | 用户填 | 自填 base_url + key |
| ollama(local) | ollama（既有驱动） | 检测结果 | 非预设，来自 detect-local |

preset 字段：`id / display / protocol / base_url / auth_methods (api_key|oauth) / model_list_source (curated|api|curated+api) / curated_models / docs_url / token_param`。

**模型清单策略**（评审 I-5）：裸 `/models` 有噪音（OpenAI 返回 tts/dall-e/embedding 等，绑定即垃圾）。策展静态 chat 模型清单为主（可搜索），API 清单为辅（`curated+api`）。

### 3.2 驱动层（S2）

- **新驱动 `anthropic`**（`llm/drivers/anthropic.py`）：原生 `/v1/messages`，`x-api-key` + `anthropic-version` 头，`system` 为顶层参数（非消息），`max_tokens` 必填，响应 `content[]` blocks，usage `input_tokens/output_tokens`；`check()` → GET `/v1/models`。
- **同片清理**（评审 I-9）：删除 `llm/types.py` 的 `OAuthNotImplemented` 占位与 `llm/__init__.py` 的失实 docstring（"anthropic / oauth … 复用宿主 Codex/Grok 登录态"是被本仓库设计史明确砍掉的父项目残留，`docs/zh/design/00-overview-and-philosophy.md` L66）。
- **openai_compatible 小改**：token 字段名由 preset 的 `token_param` 决定（默认 `max_tokens` 不变，OpenAI 官方 preset 置 `max_completion_tokens`）。
- usage 记账：各驱动把自家 usage 形状归一到 `Usage`（既有先例：ollama 根字段 / openai usage block）。

### 3.3 配置 schema（方案 b，评审 BL-1 修正）

**保持角色路由 `[dream.llm.<role>]` 不变**，preset 只做预填，不引入 `[providers.*]` 二级 schema。

密钥引用双命名空间（refs.py 正则扩一条分支 + 校验器抽公共 helper，DRY）：

- `secrets:mnemoseed/dream/<role>` —— 既有角色槽，不变（兼容）。
- `secrets:mnemoseed/llm/<name>` —— **新增共享槽**，向导每 provider 写一次（`<name>` = preset id 或自定义标签），多角色共用一把 key，轮换 = 一次 `SecretStore.set`。OAuth refresh token 落 `secrets:mnemoseed/llm/openai-oauth`。
- 优先级不变：ref > env 链（routing.py 现行为）。字面 key 仍然禁止、审计仍打码。

```toml
# 向导写入的云路由（preset "deepseek"，双座共用一把 key）：
[dream.llm.dream]
driver = "openai_compatible"
model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "secrets:mnemoseed/llm/deepseek"
max_tokens = 8192
provider = "deepseek"          # 信息性 preset 戳（休眠字段，无语义，评审 N-2）

[dream.llm.dream_verifier]
driver = "openai_compatible"
model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "secrets:mnemoseed/llm/deepseek"
max_tokens = 4096

# 向导写入的本地路由（自动检测到的 Ollama）：
[dream.llm.dream]
driver = "ollama"
model = "heretic-9b"           # 检测清单给出的精确 tag
base_url = "http://localhost:11434"
think = false
num_ctx = 16384
```

全部字段骑既有注册键（`_role_key_specs`），**零新增 CONFIG_KEY_REGISTRY 键** → 无槽位扰动。

### 3.4 槽位冻结（S0，评审 I-1，纯加固）

`configwrite/service.py` 的 `_SLOT_KEYS = tuple(sorted(CONFIG_KEY_REGISTRY))` 有潜伏陷阱：未来插键会重排槽位、让存量库 rollback 的 version_id 错位解码。修法：把今日排序结果冻结为显式常量 `_FROZEN_LEGACY_ORDER`，新键一律追加（`_SLOT_KEYS = _FROZEN_LEGACY_ORDER + tuple(sorted(new))`）+ 钉住测试（legacy 前缀不变 + 冻结映射计算的 id 回滚往返解码正确）。不建 DB 槽位映射机制（KISS；删键场景若将来出现需专门迁移）。

### 3.5 默认移除语义（S3+S4，评审 BL-2/BL-3/I-6/I-7 修正）

`DEFAULT_LLM_ROUTES = {}`（`Config.llm = {}` for fresh loads）。**settings DB 是主库位**：每个已安装实例在首次 boot 已把旧默认导入 DB（`reconcile_boot`，service.py L1240-1248）且 DB 覆盖文件 —— 所以清空默认对存量安装**零影响**（无需迁移），"新装无 dream 模型"只作用于从未 boot 过的配置。

**BL-2 修正**：`cli.py` 六处 `config.llm["dream"]` 直接下标（L131 init 副本、L297 up、L849/L913/L963/L1037 doctor）全部加 route-optional 守卫——缺路由 → 建议性文案/skip，永不异常。init next-steps 从 `ollama pull <model>` 改为向导指引（`http://localhost:7788/#/config`）。

**BL-3 修正（readiness 三态）**：`dream/readiness.py` 现状 route=None → ready=True 是谎言。改为：

- route 缺失 → `ready=False, detail="no dream model configured"`；
- ollama 路由 → 现行为（/api/tags + 模型 tag 在册）；
- 云路由 → 经驱动 `check()`（模型清单 GET，免费）探测 + TTL 节流（60s 调度 tick 不打爆 provider，评审 I-2；1s httpx 超时保留）。

复用现成延迟链：调度暂停（trigger.py L610-618）→ pool 事件门暂扣 → worker 以 `provider_status` 理由拒绝 → 路由恢复后延迟事件自动释放。**零新机制**。

**行为矩阵**：

| 场景 | 行为 |
|---|---|
| 全新安装（无配置从未 boot） | 无 dream 路由；`/healthz dream_route {ready:false, detail:"no dream model configured"}`；调度暂停、pool 照常积分但延迟发放；`dream --once` → "no dream model configured"。capture/recall/embeddings/decay 不受影响 |
| 存量安装（ollama 烘焙在 DB） | **原样**（DB 主库位覆盖）；ollama doctor 检查与 up 注记照旧；零迁移 |
| `init --mode capture-recall` | `dream.auto_trigger=false` + 无路由；与今日可观察姿态一致，仅少掉 ollama 步骤 |
| `init --mode refinement` | 同 capture-recall（mode 块本就关 dream）；`ollama pull` 副本只存在于 legacy 无 mode 路径，改指向导 |
| auto_trigger=true 且无路由 | dream 诚实延迟；向导是启用路径；配置写入热应用后一个 tick 内释放延迟窗口 |
| provider 中途死亡（key 吊销 / ollama 停） | readiness 刷新失败 → not ready → dream 延迟；doctor 报探测错误（与今日 ollama 退化同形） |

**Doctor**：全部路由检查 route-optional；缺 dream 路由 → check 通过 + 建议详情（"no dream route configured; dreams deferred — bind a model in the console"）；ctx-window / verifier / model-presence 检查在对应路由缺失时 skip（ensemble-on + 缺 verifier 路由不得 KeyError）；云路由用驱动 `check()` 探测取代"not ollama → skip"措辞。

**up**：路由摘要行 `dream route: <driver>/<model> @ <base_url>` 或 `dream route: not configured (dreams deferred)`；ollama 专属注记仅对 ollama 路由打印。

**Windows 遗留**（owner "这方面的Ollama设置全都取消"，评审 I-7 边界照录）：

- `scripts/windows-logon.ps1` 的 30s `:11434/api/tags` 登录等待**删除**（连带重写/删除 `tests/test_windows_autostart.py` L167/L198 两个钉住测试，不留沉默）。
- `OllamaHeadlessServe` 遗留任务迁移代码（windows-logon-task.ps1 L124-154、windows-install-helpers.ps1 L69）**删除**。如实边界：更早安装的机器上若仍有该隐藏自启任务，将永久保留——本软件不再管理它。
- 安装器：默认路径不安装/不等待/不拉取 Ollama；新增显式 `--install-ollama`（替代语义反转的 `--skip-ollama`，评审 N-4）；`install.ps1` winget 步骤变 opt-in。

### 3.6 HTTP 面（S5）

新文件 `src/mnemoseed_local/llm/routes.py` + `daemon/app.py` 一行 `include_router`：

- `GET  /api/v1/llm/catalog` —— preset 目录（S1 数据）。
- `POST /api/v1/llm/probe` —— **Test Connection**：ad-hoc 经 `LLMRegistry.build` 构造临时 driver 调 `check()`，**绝不触碰 RoleRouter 缓存、绝不持久化/审计/日志 key**（错误详情白名单字段；S5 验收断言 key 字节在响应、audit、daemon.log 三通道缺席——评审 I-8）。
- `POST /api/v1/llm/models` —— 指定 preset/base_url/key 拉模型清单（策展+API 合成）。
- `POST /api/v1/llm/detect-local` —— 探测本机 Ollama（`127.0.0.1:11434/api/tags`，尊重 `OLLAMA_HOST` env）；LM Studio 仅当同一探测可廉价复用时纳入。

边界钉进 design/04：daemon loopback-only + 既有凭据门覆盖全部路由；probe 是新的密钥出流面，按 I-8 验收。

### 3.7 Console 向导（S6）

`console/static/app.js` + `index.html`：

- Dream 页未配置态：从 `/healthz dream_route` 渲染"未配置/已延迟"横幅 + 进向导入口。
- 向导流：**不预选**任何 provider 卡（理论锚规则）→ 选 preset → 输 key（写 SecretStore 经既有 config/probe 通道）或 OAuth（S7 落地后出现）→ Test Connection 按钮（调 probe）→ 可搜索模型清单（客户端过滤足够）→ 绑定：经既有 `POST /api/v1/config/set` 按 key 逐个写（每写之间重取 generation 处理 If-Match 409，评审风险 4）。向导绑定 **dream 角色**，可选一个复选框"同样用于 dream_verifier"；vote 座位保持手动。
- 按 wire protocol 隐藏 ollama 专属字段（think/num_ctx/num_predict，评审 I-10）；driver 提示列表更新为真实注册集（N-1）。
- Custom Provider 表单 = preset "custom"（base_url + key + 模型名）。
- 验收含隔离环境 Playwright 手检（spare port + 临时 MNEMOSEED_LOCAL_HOME）。

### 3.8 OAuth（S7，OpenAI 先行）

**全新 OAuth 2.0 PKCE + loopback 回调**，不复用宿主登录态（复用是父项目残留设计，已被本仓库砍掉，评审 §4）：

- `POST /api/v1/llm/oauth/start` → 生成 state + PKCE verifier/challenge，`127.0.0.1:<ephemeral>` 一次性监听，返回 authorize_url；`GET /api/v1/llm/oauth/status?state=` 供向导轮询（completed/failed/expired）。
- 回调 → 换 token → refresh_token 存 `secrets:mnemoseed/llm/openai-oauth`。
- 驱动侧：openai_compatible 变体的 api_key 经 token provider 解析（SecretStore access token + 过期/401 刷新授予）；`DreamLLM` 端口不变。
- preset `auth_methods` 门控向导按钮；本片可独立移除不伤向导主体。

## 4. 切片分解与派发计划

**PR1（并行批，纯增量、main 恒绿）**：
- **S0 槽位冻结** — `configwrite/service.py` + `tests/test_configwrite_service.py`。
- **S1 预设目录** — 新 `llm/providers.py` + 新 `tests/test_llm_providers.py`（唯一 id、协议/base_url 钉住、零 I/O）。
- **S2 anthropic 驱动** — 新驱动 + `llm/drivers/__init__.py` + docstring 修正 + `OAuthNotImplemented` 移除（连测试）+ 新 `tests/test_llm_drivers_anthropic.py`（MockTransport 模式）。
- 三片文件面两两不相交（service.py / providers.py / drivers+types），可并行派发 senior-software-engineer。

**PR2（原子：拆默认必须整组落地）**：
- **S3 安装器+脚本+文档** — install.sh / install.ps1 / windows-logon.ps1 / windows-logon-task.ps1 / windows-install-helpers.ps1 / README.md / docs（+ 两份安装器字符串钉住测试重写）。
- **S4 拆默认核心** — config.py（空 DEFAULT_LLM_ROUTES、简化 load 回退、模板重写）+ readiness.py（三态+节流）+ cli.py（六守卫+副本+doctor）+ app.py（仅 boot 日志措辞）。测试：test_config / test_llm_routing / test_cli / test_dream_readiness / **新增 DB-主库位覆盖钉住测试**（存量安装跨变更保留路由）。
- S3 与 S4 必须同一 PR 合并：只改安装器不改核心 → 新装出现指向不存在 Ollama 的默认路由（烂窗）；反之亦然。

**PR3**：
- **S5 HTTP 面**（新 routes.py + app.py 一行 + test_llm_routes，隔离环境）。
- **S6 向导**（app.js/index.html）。S4 先合保持 app.py 单写者；S5、S6 串行（S6 依赖 S5 端点）。

**PR4**：
- **S7 OAuth**（llm/oauth.py + token provider 变体 + routes 扩展 + app.js 按钮）。

**范围外**：服务端 key 管理；provider 间运行期自动降级；eval 矩阵 roster 变更（eval 自驱路由，ollama 锚仍有效）；Ollama 之外的本地服务检测（LM Studio 仅限廉价复用）；`dream_vote` 座位向导化。

## 5. 验收条件（Done-when，issue #381）

1. 全新安装全程零 Ollama 安装/下载/等待；capture/recall 正常；dream 面明确显示未配置直到用户配置。
2. Console：选 preset → API key（入 SecretStore，不出现在明文配置/日志/审计）→ Test Connection → 可搜索模型清单 → dream 周期实际用上该 provider。
3. Custom provider（自 base_url + key，OpenAI 兼容）达成同样结果。
4. 运行中的本机 Ollama 自动出现为已配置 provider 及其模型。
5. OpenAI OAuth 登录路径可用（API key 仍可用）。
6. 存量 ollama 配置原样工作；仅新装默认变化。
7. 开发机经新流 dogfood：dream → 本机 Ollama `heretic-9b`。
8. 全门禁绿（pytest/ruff/format/mypy）+ 对抗性 QA CLOSABLE；roadmap 修订 + #55/#310/#381 随落地 PR 关闭。

## 6. 风险（评审 §8 摘录，按序）

1. **DB-主库位遮蔽拆默认**——行为变化只在全新 DB 可观察；reconcile 回归会静默翻转存量路由 → S4 专设覆盖钉住测试。
2. **probe 端点是新密钥出流面**——S5 验收断言 key 字节三通道缺席。
3. **云 readiness 探测配额**——TTL 缓存 + 仅探测已配置路由 + 失败退避。
4. **向导多写 vs generation 锁**——写间重取 generation，序列钉进验收。
5. **密钥正则扩展回归**——共享 helper + 对抗测试（ref+env 混合、畸形名、字面 key 依旧拒绝）。

## 7. 外部 API 事实的验证边界

各 preset base_url/兼容层/token 字段规则来自 API 文档级知识（评审标注 `[unverified]`）：S5 实施时用 probe 对活跃端点冒烟验证后方可合入；Anthropic 原生协议形状以 driver 测试的 MockTransport 契约为准。
