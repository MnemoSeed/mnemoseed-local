# PRD-303 · Unix 自启动：Linux systemd user unit + macOS launchd LaunchAgent

日期：2026-11-07。状态：设计冻结 v2（含设计 QA 修订：I-1 锚点改正、I-2 闸门改判、N-1/N-2/N-3 边界声明），待主控复核后进入工程批次。基线：`7d93e4f`（分支 `docs/303-unix-autostart-design`）。Owner 决定（2026-11-07，记录于 [#303](https://github.com/MnemoSeed/mnemoseed-local/issues/303)）：Linux = systemd user unit，macOS = launchd LaunchAgent，与 Windows 登录任务语义对齐（卸载即移除；daemon 以用户身份随用户会话运行，不是系统服务）。Windows 行为不变。

## 理论锚：未借用

本批是 OS 机制接线（登录调度、任务/单元注册、进程生命周期编排），不借用任何神经科学/心理学规律。不借清单：登录自动启动不是"记忆巩固"或"离线重放"的类比；on/off 闸门不映射注意力或显著性；会话域生命周期不映射记忆衰退。措辞纪律同 B2.14：一切 daemon 活性表述均为工程语义，永不入认知词汇。

## 对齐基准：Windows 登录任务的冻结语义（逐条对齐，不改判）

- 仅登录时启动；崩溃或注销后不自动恢复；无持久 supervisor / 周期轮询 / restart loop（PRD-B2.14 决定与边界第 4 条，B2.14:12；README.md:71 明文）。
- 安装器注册但不立即启动：install.ps1 `[5/10]` 注册任务（install.ps1:513-536），最终指引仍是 `mnemoseed-local up`（install.ps1:641-645）。
- 启动前 daemon.off 闸门：bootstrap disabled 时 rc 0 退出（windows-logon.ps1:120,137），`up` 自身早拒绝构成第二闸（cli.py:327-344；PRD-B2.5「up 早拒绝」）。
- 同用户信任边界：无额外签名可验，保证是外来/漂移内容不被误改误删（windows-logon-task.ps1:190,204 的保守拒绝；PRD-B2.14 第 11 条，B2.14:19）。
- 卸载由 `mnemoseed-local uninstall` 承担，且先于宿主 adapter / 数据面执行（cli.py:1266-1310 的 win32 分支先跑任务移除，cli.py:1307 才进入 adapter 流程）。

## Linux：systemd user unit

单元文件 `~/.config/systemd/user/mnemoseed-local.service`，内容冻结：

```ini
[Unit]
Description=MnemoSeed Local memory daemon (user session)
# mnemoseed-local:autostart:unix-daemon:v1
ConditionPathExists=!%h/.mnemoseed-local/daemon.off

[Service]
ExecStart=<CLI_ABS_PATH> up
Restart=no

[Install]
WantedBy=default.target
```

- **CLI 解析（冻结：安装时解析写入绝对路径）**：顺序 `command -v mnemoseed-local` → `$HOME/.local/bin/mnemoseed-local`（uv tool shim，`UV_BIN_DIR`，install.sh:67）；此时 install.sh:611 的 `have mnemoseed-local` 检查已保证可解析；解析失败按安装器约定 fail-closed（install.sh:49-50）。裸名被否：launchd 的 `ProgramArguments` 无 PATH 解析、systemd user manager 的 PATH 随发行版而变（UNKNOWN 1）；纯 shim 路径硬编码被否：非 uv 安装形态（pipx/venv）会注册错误路径。`uv tool upgrade` 不改 shim 路径（install.sh:600-610 升级流），重跑 install.sh 重解析重写（幂等）；含单引号或空白字符的 CLI 路径拒绝（macOS 内联 sh 字符串与 systemd argv 解析的统一安全边界）。
- **Restart=no（显式写出，冻结）**：与 Windows 任务无 restart policy 精确对齐（PRD-B2.14 第 1 条，B2.14:9）。崩溃后单元如实进入 failed 态（`systemctl --user status` 可见），不复活；恢复通道 = `systemctl --user start`、`mnemoseed-local up`，或串行会话域内下一登录的全新 manager 起跑（重试语义的精确边界见 UNKNOWN 7）。
- **ConditionPathExists= 即 daemon.off 闸门（冻结；I-2 改判自 ExecCondition 方案，否决记录见备选 7）**：[Unit] 段条件指令——marker 在场 → 条件不满足 → 单元 skipped（干净跳过，非 failed），对齐 windows-logon.ps1:120 的 disabled → exit 0；`up` 早拒绝（cli.py:327）构成第二闸，与 Windows 的 bootstrap+up 双闸同构。默认装用 `%h` 形态；`MNEMOSEED_LOCAL_HOME` 非空时（与 install.sh:65 同一读取）闸门路径与 `Environment=` 行按安装时烘焙：`ConditionPathExists=!<abs-override>/daemon.off` + `Environment=MNEMOSEED_LOCAL_HOME=<path>`——烘焙是唯一正确机制（user manager 不继承 shell rc env；QA 轮确认），故目标 home 集合在安装时即静态可解析，条件指令无变量展开需求面。无 shell、无引号转义、版本下限远低于 ExecCondition（备选 7）。
- **WantedBy=default.target（冻结）**：任何登录会话（含 SSH）起跑，对应 AtLogOn 的"任何登录"。无沙箱化指令（ProtectHome 等）：daemon 必须读写用户家（config.py:26-35），加硬化属隔离变更，越出本批。
- **生命周期 = 会话域（冻结：不启用 lingering，不运行 `loginctl enable-linger`）**：与 Windows 登录任务的登录域生命周期对齐（PRD-B2.14 第 4 条，B2.14:12「注销后不自动恢复」）。tradeoff 如实记档：无活动登录会话的机器（纯 headless 服务器、容器）无 daemon，hook 捕获本就静默 fail-open（既有语义，无新损伤）；串行会话下每登录重新起跑 = AtLogOn 对齐（manager 存续窗口内的重试边界差见 UNKNOWN 7）；lingering 反而有害——failed 单元在常驻 manager 中滞留、串行重试语义被取消，需手工处置。SSH 登录经 default.target 起跑，覆盖无 GUI 开发机主场景。

## macOS：launchd LaunchAgent

plist 文件 `~/Library/LaunchAgents/mnemoseed-local-daemon.plist`，键面冻结（XML 骨架常规，写盘时对 XML 特殊字符做实体转义；注释 `<!-- mnemoseed-local:autostart:unix-daemon:v1 -->` 随文件写入作 identity marker）：

- `Label` = `mnemoseed-local-daemon`（冻结：扁平 label，镜像 Windows 任务名 `MnemoSeedLocalDaemon`；反 DNS `my.mnemoseed.*` 被否——Label 是升级期所有权匹配的身份键，发行后不可再改，本批冻结扁平 v1，见备选 5）。
- `ProgramArguments` = `["/bin/sh", "-c", "if [ -f \"${MNEMOSEED_LOCAL_HOME:-$HOME/.mnemoseed-local}/daemon.off\" ]; then exit 0; fi; exec '<CLI_ABS_PATH>' up"]`（冻结）：内联 sh 闸门给出与 Windows 完全一致的 disabled → exit 0（windows-logon.ps1:120），不新增 wrapper 脚本文件（KISS）。Windows 需要 bootstrap 脚本还因 Ollama wait 与端口身份检查；Unix 侧 `up` 本就不以 Ollama 为前置（PRD-B2.14 第 7 条，B2.14:15），端口冲突诚实交给 bind 失败（诊断强度差在下节显式声明）。CLI 解析与 Linux 同一函数（`command -v` → `~/.local/bin`），单引号路径拒绝。
- `RunAtLoad` = true（冻结）：登录加载即起跑。
- **`KeepAlive` 不写（= false，冻结）**：与 Windows 无 restart policy 对齐；崩溃后 agent 退出不拉起，下一登录重启。`KeepAlive=true` / `SuccessfulExit` 变体被否（同备选 1）。
- `StandardOutPath` / `StandardErrorPath` = `<home>/launchd.out.log`（冻结）：launchd 默认丢弃 stdout；journald 在 Linux 免费给出等价物，macOS 以这两键补齐可观测面；日志随 `--purge` 数据面清理。
- `EnvironmentVariables` 仅在安装时 `MNEMOSEED_LOCAL_HOME` 非空时写入（同 Linux 的 `Environment=`）。
- **launchctl 语义冻结**：安装只写 plist，**不 bootstrap**——LaunchAgent 的磁盘存在即注册（下次登录自动加载），对应 Windows schtasks 注册不运行；当前会话立即生效的手动命令打印在 next steps。卸载 = `launchctl bootout gui/$(id -u)/mnemoseed-local-daemon`（未加载的失败容忍）+ 删 plist。`bootstrap gui/UID` / `bootout` 为 macOS 10.11（2015）起的现代语义；弃用的 `load` / `unload` 不使用、不留兼容分支（备选 6）；更早版本不声明支持（UNKNOWN 3）。SSH 安装时 gui 域可能不存在 → 降级注记（UNKNOWN 4）。

## install.sh 接线（Linux/Darwin 分支）

- 新步骤插在 CLI 安装步（install.sh:597-611）之后、init（install.sh:615-623）之前，镜像 install.ps1 的 4→5→6 顺序；步骤计数 `[n/8]` → `[n/9]`，dry-run 计划同步（install.sh:506-574 模式）。不新增 CLI 旗标、不设确认提示（对齐 Windows：注册无 opt-out，daemon.off 哨兵与 uninstall 是控制通道）。
- 幂等 + marker 判定（冻结）：目标文件存在且含 identity marker → 原子重写（tmp + rename）→ reload/enable（Linux）或仅重写（macOS，无 bootstrap）；存在但无 marker → 外来文件，fail-closed 并打印人工检查指引（镜像 windows-logon-task.ps1:190,204 的保守拒绝，不覆盖不删除）；不存在 → 写入。
- 可用性闸门（冻结：探测先行、两档降级）：Linux 无 systemd user manager（WSL 未开 systemd、容器）→ 单行注记「autostart 需 systemd 用户会话；已跳过」+ 安装继续（宿主 adapter 的 note-and-continue 先例，install.sh:703-711）；manager 在场而 enable 失败 → fail-closed 非零。macOS launchctl 恒在、失败即 fail-closed；gui 域缺席（SSH 安装）→ 注记 + 继续磁盘注册。
- next steps 增一行原生状态指针（`systemctl --user status mnemoseed-local` / `launchctl print gui/$(id -u)/mnemoseed-local-daemon`）；`mnemoseed-local up` 首行指引不变（install.sh:726-736）。

## 卸载对称（`mnemoseed-local uninstall`）

- cmd_uninstall（cli.py:1266）的 win32 分支（cli.py:1274-1306）之后加 Unix 分支（`sys.platform == "linux"` / `"darwin"`）：在宿主 adapter 检测 / purge 流程（cli.py:1307-1310）**之前**执行，与 Windows 先后次序一致。
- Linux（冻结）：单元文件在场 → `systemctl --user disable --now mnemoseed-local.service` → 删文件 → `systemctl --user daemon-reload`；systemctl 缺席或失败 → fail-closed rc 1 + 手工命令（对齐 cli.py:1277 的 helper 缺失信封）；文件缺席 → 无事，继续。
- macOS（冻结）：`launchctl bootout gui/$(id -u)/mnemoseed-local-daemon`（未加载容忍）→ 删 plist；launchctl 失败 → rc 1。
- 所有权判定同安装：marker 匹配才删；同路径外来内容 → 报告歧义、保持不动 + 指引（Windows 同款保守语义，PRD-B2.14 第 11 条，B2.14:19）。
- 实现 seam（路径/命令注入）必须可离线测试；落点由工程批次定（cli.py 内联 vs 小模块），monkeypatch `sys.platform` 有先例（tests/test_uninstall_lifecycle.py:56）。

## 手动路径与端口冲突（对齐既有答案；诊断强度差显式声明）

- `up` 保持手动前台路径（PRD-B2.14:21；B2.3 生命周期不变）。`_up_locked` 无端口预检（cli.py:272-317），第二 `up` 在 uvicorn bind 处失败并退出非零（runner.py:237-245，无自定义冲突处理）——这就是 Windows 的既有答案（手动 `up` 撞上登录任务 daemon = 同样 bind 失败），Unix 精确对齐。`on` 是幂等的已运行通道（cli.py:509-511 "already on" rc 0）；`up` 无 `--force`（PRD-B2.5 设计定案）。
- 安装期无冲突窗口：安装只注册不启动，当次会话 daemon 未起，`up` 即手动路径。
- **诊断强度差（N-3 显式声明）**：Windows 登录路径启动前检查 7788 监听者身份（可执行文件 == 安装解析的 CLI、命令行精确为 `up`、PID 前后稳定，windows-logon.ps1:138-166）——健康 owned-daemon 在场则安静 exit 0，未知/伪装占主诚实拒绝；Unix 登录路径无等价检查，`up` 直接尝试 bind，任何在场监听（外来进程、残留监听）都表现为裸 bind 失败，无所有权裁定。启动去重与端口诊断因此弱于 Windows，属逐条对齐下的已声明差；补齐身份检查属新增独立面，不在本批。

## 状态/doctor 面（决定：本批不加）

Windows 现状：doctor（cli.py:570-660 检查清单）与 cmd_status（cli.py:539-567）均无登录任务检查项。对齐 + KISS → 本批不新增 autostart 状态面；原生 OS 动词（`systemctl --user status` / `launchctl print`）是报告路径，install.sh next steps 给指针。可选后续（独立 issue，不在本批）：doctor 增加一行 autostart 状态。

## Windows 不变

install.ps1、scripts/windows-logon*.ps1、cli.py 的 win32 分支零改动；README 的 Windows 自启动句（README.md:71）语义原文保留。工程批次唯一触及的跨平台共享文件是 cli.py 的 cmd_uninstall（纯增量 Unix 分支）与 README 的 what-it-does-not-do 文案（Windows 句式扩展为三平台句式）。

## 信任边界

与 Windows 同构（PRD-B2.14 第 11 条，B2.14:19）：unit/plist 与 CLI 可执行文件都在同一 Unix 用户权限下，systemd/launchd 无额外语义可验；保证是外来/漂移文件不被误改误删（marker 判定），不声称更强身份。同用户攻击者可改写自启项 = 可改写一切用户文件，无新增暴露面；daemon 读写的配置与数据仍是同一用户家（config.py:26-35），无隔离变更（PRD-345 的隔离配方只属验证轮，不属产品运行面）。

## UNKNOWN 与边界（不猜测）

1. 发行版 systemd 版本谱系：`ConditionPathExists` 的版本下限为 QA 轮评估的 ~209（2014）量级——条件指令族是 systemd 最老机制之一，实际下限以工程批次红测试实测复核；已知降解：极老版本忽略未知指令并告警（单元仍加载），闸门消失 → disabled 用户得到 failed 单元而非 skipped（degraded-but-honest，如实记档）。支持面声明下限由工程批次定。
2. WSL / 容器无 user manager 的探测动词（`systemctl --user is-system-running` 等）及其退出码谱系随版本有差，工程批次以探测结果定案分支条件。
3. macOS 10.11 之前不声明支持；`bootstrap gui/UID` 各大版本的容错行为以 V305 实机轮为准。
4. SSH 安装（gui 域缺席 / user manager 经 pam 起跑的时差）的降级路径行为以实机为准；CI 无法证。
5. GH runner 的 `systemd-analyze verify` 可用性不预设：探测后用，缺席则以内容 pin 为准。
6. **安装后 shell rc 新设的 `MNEMOSEED_LOCAL_HOME`（N-1 边界声明）**：unit/plist 的闸门路径与 daemon 的 home 均为安装时烘焙（默认 `~/.mnemoseed-local`），其后在 shell rc 中新设的覆写不进入 user manager / launchd 环境——自动启动面用烘焙 home、手动 shell 面用自定义 home 的分裂窗真实存在。Windows 登录任务同构（任务环境来自任务定义，不含其后 shell rc 的改动），对齐成立，显式声明而非修复。
7. **「每登录重新起跑」的会话域边界（N-2）**：无 lingering 时 user manager 的存活跨度是首登录 → 末注销——串行会话（中间完全注销）下每次登录都是全新 manager、dead/failed 单元重新起跑 = AtLogOn 对齐；但 manager 存续窗口内（并行会话、未完全注销的会话切换）的新登录不会重试已 failed 的单元，而 Windows AtLogOn 对每次登录事件都重新触发。此重试边界差如实声明；多会话重试语义不属本批。

## 验收与测试预言（工程批次）

- **写面**：`install.sh`（新步骤 + dry-run 计划 + next steps）；`src/mnemoseed_local/cli.py`（cmd_uninstall Unix 分支）；自启内容生成与所有权判定逻辑（小模块或 cli 内联，工程批次定）；`README.md`（自启动句式扩展）。**零 daemon / 存储 / config 代码改动**。
- **测试文件**：新增 `tests/test_unix_autostart.py`（平台无关 pin：单元/plist 内容键面、marker 所有权判定、幂等重装、env 烘焙、fail-closed 与降级分支；install.sh 函数抽取执行沿用 tests/test_macos_install_paths.py:3-16 的受控 PATH + shim 模式，shim systemctl/launchctl）；扩展 `tests/test_uninstall_lifecycle.py`（Unix 分支；平台 monkeypatch 先例 :56）；install.sh dry-run 计划行并入既有 smoke 断言面。
- **CI 边界（诚实记档）**：`install-script-smoke` 与 `release-install-verify` 三腿在位（ci.yml:102,123，含 ubuntu/macos 腿）。语法层真实验证：ubuntu 腿 `systemd-analyze verify`（探测后用）、macOS 腿 `plutil -lint`。**CI 只能证语法与内容 pin，不能证 systemd/launchd 运行时行为**——真实登录、注销、崩溃、卸载净度必须 V305 实机轮。
- **V305 轮（真实 Linux + 真实 macOS 主机）必须验证**：(a) 真实登录后 daemon 起跑且 `/healthz` 可达；(b) 重启机器后登录仍起跑；(c) `off` 后登录 → 单元 skipped / agent exit 0 不起跑，`on` 后恢复；(d) kill daemon → 不自动复活，串行会话下一登录重跑；(e) `mnemoseed-local uninstall` 移除单元/plist 且 daemon 停止，重复卸载幂等；(f) 安装后同会话 `up` 正常；下登录后（单元 daemon 在跑）`up` 以 bind 失败退出非零；(g) 注销后 daemon 消失（会话域生命周期实测，未启用 lingering 的负证）；(h) SSH 安装的降级注记路径；(i) 配置面 = 用户家（`MNEMOSEED_LOCAL_HOME` 未设时读 `~/.mnemoseed-local`，config.py:26-35）。
- 所有运行态测试用临时 `MNEMOSEED_LOCAL_HOME`、非 7788 端口、受控 PATH（测试/live 隔离纪律）。

### 预言表（对抗式，反变异体）

| # | 预言 | 反变异体 |
|---|---|---|
| 1 | 生成的单元含 ConditionPathExists 闸门（默认装 `%h` 形态 / 覆写装烘焙绝对路径）+ `Restart=no` + `WantedBy=default.target` + identity marker + 绝对路径 ExecStart | 裸名 ExecStart / 缺 marker / `Restart=always` / gate 路径与烘焙 home 不一致 → 红 |
| 2 | plist 含 Label / RunAtLoad / gate / exec 绝对路径，且无 `KeepAlive` 键 | KeepAlive 在场 → 红 |
| 3 | marker 在场重装 = 原子重写幂等，无重复注册 | 追加/双写 → 红 |
| 4 | 同路径无 marker 外来文件 → fail-closed，不覆盖不删除 | 静默覆盖或删除 → 红 |
| 5 | `MNEMOSEED_LOCAL_HOME` 非空 → `Environment=` 行 + gate 绝对路径同指覆写 home；未设 → `%h` gate 且无 env 行 | 烘焙空值 / gate 与 env 指向不同 home → 红 |
| 6 | 无 user manager / gui 域 → 单行注记 + 安装继续；manager 在场而操作失败 → rc 非零 | 反向（缺席硬炸 / 失败静默）→ 红 |
| 7 | Unix uninstall：自启移除先于 adapter 流；文件缺席继续、工具失败 rc 1、外来不动 | 顺序颠倒 / 失败吞掉 → 红 |
| 8 | dry-run 打印自启计划且零副作用（不写任何文件） | dry-run 写盘 → 红 |

## 备选与取舍（主决策显式化）

1. **`Restart=on-failure` / `KeepAlive=true`** vs 冻结的 `Restart=no` / 无 KeepAlive：前者崩溃自愈更"可用"，但引入平台内 supervisor（PRD-B2.14 第 4 条，B2.14:12 冻结排除的形态）、三平台失败语义分裂（Windows 无恢复）、`off`/crash 边界复杂化。冻结维持；真实主机数据若显示崩溃缺口伤及主场景，作为 owner 级独立决定重开。
2. **启用 lingering** vs 冻结的会话域：前者 daemon 跨注销常驻（headless 可用），但偏离 owner 决定的 "lives with the user session"、failed 单元滞留、语义向系统服务漂移。冻结维持。
3. **ExecStart 裸名 / 纯 shim 硬编码** vs 安装时解析：裸名依赖 manager PATH（launchd 无解析、systemd 依发行版）；硬编码 shim 对非 uv 安装形态失真。安装时解析 + 重装重写胜。
4. **安装期 `enable --now` / bootstrap 立即起跑** vs 冻结的只注册：立即起跑与 next steps 的 `up` 指引直接冲突（bind 失败），且偏离 Windows 注册不运行形态。冻结维持。
5. **Label 采用 `my.mnemoseed.*` 反 DNS** vs 冻结的扁平 `mnemoseed-local-daemon`：反 DNS 更惯例，但 Label 是升级期所有权身份键、发行后不可改；扁平名与 Windows 任务名同构、最小惊讶。冻结维持；未来若需反 DNS 须走带迁移的身份版本化，不做静默换名。
6. **`load`/`unload` 兼容回退分支** vs 单一 bootstrap/bootout：双路径徒增测试面且拖入弃用 API；早于 10.11 直接不声明。冻结维持。
7. **`ConditionPathExists=!<baked-path>/daemon.off` vs `ExecCondition=`（I-2 改判，冻结：切换到 ConditionPathExists）**：QA 轮指出 ExecCondition 方案的 `$MNEMOSEED_LOCAL_HOME` 分支在运行时是死支——user manager 不继承 shell rc env，覆写 home 只在安装时烘焙后才存在，而烘焙使路径静态可解析，条件指令（无变量展开需求）功能等价；且 Condition 族版本下限远低（~209 量级 vs ~243）、无 shell、无引号转义、无 `$$` 逃逸风险。本版据此把 Linux 主闸门从 ExecCondition 改判为 ConditionPathExists，ExecCondition 方案保留于此作否决记录；plist 侧不受影响（launchd 无条件机制，内联 sh 闸门维持）。

## 批次执行记录（工程批 C303-UNIX-LIFECYCLE，2026-11-07）：实现按本冻结执行，gate 机制 = ConditionPathExists（v2 改判）；红→绿三轮（span 不存在 / 模块缺失 / [7/8] 锚点重编号），全量 4830 passed / 23 skipped（平台门控 skip 全部带理由并钉死）；QA 变异 5/5 在设计层被杀（Restart 改写、引号路径、外来文件、fail-closed 信封、KeepAlive 注入）；独立 QA CLOSABLE 0B/0I/5N（N1 oracle 分层、N2 probe-then-bootout 等价偏差、N3 手动命令引号、N4 读错误裸栈、N5 本记录回填——均不阻塞）。真实 systemd/launchd 行为验证归 V305（CI 仅语法面）。

- **2026-11-07 设计 QA 修订（v2 re-freeze）**：设计 QA 判定 NOT CLOSABLE（0 BLOCKER / 2 IMPORTANT / 3 NIT），本版逐项落实：I-1 —— 全部 PRD-B2.14 条目号改正并补行号锚（原「第 3 条」→ 第 4 条/B2.14:12、原「第 4 条」→ 第 7 条/B2.14:15、原「第 8 条」→ 第 11 条/B2.14:19；实质主张经 QA 复核全部为真）；I-2 —— Linux 主闸门改判 `ConditionPathExists`，ExecCondition 方案以否决记录保留（备选 7）；N-1 —— 安装后 shell rc 覆写的 home 分裂窗声明入 UNKNOWN 6；N-2 —— manager 存续窗口内 failed 单元不随新登录重试的边界声明入 UNKNOWN 7；N-3 —— Unix 端口诊断弱于 Windows 的差显式声明入手动路径节。
- （待工程批次填：TDD 红绿记录、门禁数字、QA 判定、收口记录。）
