# PRD-B2.14 · Windows 用户登录自动启动

## 理论锚：未借用

本批是 Windows 工程控制面：登录触发、任务注册、依赖就绪探测与进程生命周期编排；不借用任何神经科学或心理学规律。`on/off` 仅是工程启停状态，不映射为注意力、显著性、记忆衰退或认知恢复。

## 决定与边界

- 安装器只注册当前用户 `MnemoSeedLocalDaemon` 的 AtLogOn 任务，参数固定为 `MultipleInstances=IgnoreNew`、隐藏动作、无周期触发器、无 restart policy。
- 动作先检查 `daemon.off`，以命名 mutex 合并重复/并发触发；先发现监听者并确认可检查的 MnemoSeed PID 在 health 前后稳定，且进程可执行文件必须等于当前安装解析出的 `mnemoseed-local` 路径、命令行形状必须精确为该路径后跟 `up`；未知/不可检查进程、可执行文件不匹配或命令行不匹配均拒绝。Ollama API 最多等待 30 秒，超时后仍启动 MnemoSeed daemon。Ollama 的原生 tray/startup 配置不由 MnemoSeed 修改，安装器不注册 Ollama 任务、不修复 Ollama executable、不执行 `ollama serve`。
- daemon 已健康且监听 PID 可检查、可执行文件/命令行身份匹配、health 前后稳定则退出；监听 PID 未知/不可检查、PID 变化、非 MnemoSeed 进程，或 MnemoSeed 进程存在却不健康，均诚实失败，不把“端口有人监听”当成功。
- 仅在用户登录时启动。后续崩溃或注销后不自动恢复；不存在持久 supervisor / 周期轮询 / restart loop。
- OpenCode/MCP 只负责连接，不拥有 daemon 启动所有权。
- `off` 只门控 MnemoSeed daemon；不停止 Ollama。Ollama 继续独立服务其他应用。
- `cmd_up` 不以 Ollama API 或模型存在为启动前置条件；capture、recall 和其它 memory API 在 dream route 离线时仍可用。doctor/status 给出可行动的 provider/model 状态，绝不静默 pull。
- 本地 Ollama dream route 使用有界 `/api/tags` readiness 与配置模型存在性检查。scheduler 是自动 dream 的单点门控：离线时不 drain pool、不消费 retry/give-up budget，最迟一个 scheduler cycle 后恢复。queued job 还有 worker final guard；真正开始后遇到服务消失按实际失败记录并保留 journal。
- 升级安装只删除旧版安装器创建的精确 `\OllamaHeadlessServe` 任务：任务名/根路径、原始 Description、当前用户 Interactive/InteractiveToken principal、单一 AtLogOn trigger，以及 `$LOCALAPPDATA\Programs\Ollama\ollama.exe serve` 单一动作必须全部匹配。任一漂移、同名外来任务或多个候选均保持不变并以可操作的人工检查指引令安装失败关闭；不停止 Ollama 进程，不修改原生 tray/Startup 链接。
- 远程单文件 `irm ... | iex` 不依赖空的 `$PSScriptRoot`：安装器先在自身边界解析自包含 helper module；缺失本地 sibling 时，从与入口相同的 `main` raw base 下载 module、logon bootstrap 与 task registrar 到唯一临时 staging，先下载并 AST parse module，再在脚本作用域 dot-source 其返回路径，随后解析另外两个 helper。staging 仅在 CLI 安装后的 autostart 阶段创建，并在迁移、daemon 注册完成或任一失败退出后清理；下载/解析失败不得注册任务。本地 checkout 仅在三个预期 sibling 全部存在时直接使用，不复制 PowerShell 实现。注册动作内嵌的 bootstrap 内容仍与 wheel 打包 helper 是同一仓库文件，CLI 卸载按精确内容判定。远程 staging 位置为 `%TEMP%\\mnemoseed-local-helpers-<pid>-<unique>`。
- Task Scheduler 注册携带稳定产品身份 `urn:mnemoseed-local:task:windows-logon-daemon:v1`（动作参数中的精确 marker），并绑定专用路径、当前用户 principal、精确单动作形状，以及 `-EncodedCommand` 解码后经换行规范化的当前发行包 bootstrap 源码。安装修复与卸载使用同一内容所有权判定；只有完整身份和当前 bootstrap 内容均可验证时，才允许修复 settings/restart 漂移。未识别的旧版或不同 bootstrap 内容会保守拒绝，绝不 force 覆盖或卸载；用户须先人工检查并移除/重命名该任务，再重新安装，除非未来以显式已知内容版本增加兼容。卸载发现 owned path 外的同名任务时报告歧义并保持其不变。信任边界是同一 Windows 用户权限：该用户若能改写任务 XML/动作 marker 或替换安装 executable，Task Scheduler/CIM 没有额外签名可验证，脚本不能把该权限提升误报为更强身份；本实现的保证是未知、动作内容漂移和外来任务不被误删/覆盖。
- 手动 `dream once` 在 route 不可用时返回 `launched:false` 和明确 reason，不保留隐藏 future；reason 随每个请求以不可变结果传递，不从共享 worker 状态读取；route 恢复后用户再次调用即可。
- 保持 B2.3 `up` 前台生命周期（不从 `up` spawn 子进程）与 B2.5 持久 `off` 闸门。
- Task Scheduler 的 `Hidden` 不能证明交互登录时绝对无 console；隔离 Windows 验证必须测量该行为。若无法证明，收口时标记未验证，不作已证实声明。

## 验收与测试预言

- 安装器测试钉住当前用户 AtLogOn、IgnoreNew、零执行时限、无周期/restart。
- 生命周期测试钉住 off 前置与等待 Ollama 后复检、重复/并发互斥、canonical executable + 精确 `up` 命令身份、稳定 PID 与健康检查、未知/伪装端口 owner 拒绝、Ollama 超时仍启动、无 model pull、任务身份缺失/外来任务拒绝、匹配元数据与 action 形状但 bootstrap 内容不同者在安装修复和卸载时均拒绝、当前 bootstrap 的 owned settings 漂移修复、精确路径卸载与同名歧义保持不变。
- scheduler/worker 测试钉住本地 Ollama 离线时不 drain pool、不消费 retry/give-up budget，provider 恢复后同一 pending window 自动恢复；手动 route 不可用时明确拒绝且不隐藏排队，不同并发请求的 reason 不串台。
- 所有运行态验证使用临时 `MNEMOSEED_LOCAL_HOME`、隔离 profile、伪 Ollama health endpoint 和非 7788 daemon 端口。

## 批次执行记录（2026-09-24）

- 实现：新增 `scripts/windows-logon.ps1` 登录动作与 `scripts/windows-logon-task.ps1` 任务注册器；`install.ps1` 不再管理 Ollama 启动，仅注册 MnemoSeed 当前用户任务。
- 文档：README/PRD 明确 daemon 与 Ollama 的所有权边界、离线 dream defer/manual reason 与 no-console 未验证限制。
- 红阶段：先新增可执行隔离 PowerShell fixture 与 scheduler/provider 测试，确认旧实现因错误 Ollama 任务/启动门控失败，再实现。
- 未验证：尚未执行真实交互登录的 console-window 测量；Task Scheduler 配置文本或 Hidden 属性本身不构成该证据。也未在本机访问 live daemon、Ollama、7788 或生产配置。

## 收口记录（2026-09-25）

- 实现边界：本批仅完成 Windows 当前用户登录自动启动、任务身份与内容所有权、daemon 启动前身份/健康校验、Ollama 有界 readiness，以及离线 dream 的 scheduler/worker/manual 行为；安装器不接管 Ollama 原生启动，不执行模型拉取，不提供持久 supervisor、周期轮询或 restart loop。
- 历史候选证据：追加文档前候选树完整门禁为 `3158 passed, 5 skipped`，QA 聚焦终检为 `251 passed, 1 warning`；此前文档后树曾记录 `3162 passed, 5 skipped, 1 warning`。
- PR #222 Linux CI 修复：Linux-target mypy 原先因 4 个 `msvcrt` attr-defined 报错失败；源码改为动态导入 `msvcrt`/`fcntl`，锁语义不变。
- 验证增强：新增 6 个 hermetic daemon coordination 测试；`LK_NBLCK`→`LK_UNLCK` 变异被测试杀死。
- 本次仅文档编辑前的权威完整门禁：`3168 passed, 5 skipped, 1 warning`；ruff clean；format `438`；mypy `127`。
- 最新独立 QA：`CLOSABLE`，`0 BLOCKER / 0 IMPORTANT / 3 NIT`。三项 NIT 分别为：真实交互登录的 no-console/fresh-host 行为仍未验证；release ordering（close before explicit unlock）未被测试预言钉住；retry/sleep 与 unlock seek/suppression 的覆盖尚未被测试预言完整钉住，且 suppressed unlock errors 没有诊断 trace。
- 环境边界：未触碰 live daemon、Ollama、task 或 7788；未在真实全新主机执行安装。上述 NIT 均未修复，不得表述为 blocker/important 或已验证。
