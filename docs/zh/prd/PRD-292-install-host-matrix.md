# PRD-292：安装与宿主矩阵审查

日期：2026-10-29。状态：文档交付待独立 QA 与主控验收。审查基线：`2f3dbb61c6e876ef13606cfe061fef64af3fc371`。范围为既有安装、宿主接线与生命周期证据，不改变产品行为、配置、默认值或验证工具。

## 证据与结论边界

本轮真实宿主完整流程为 **0/12**：Windows 11、Ubuntu、macOS × Claude Code、Codex CLI、OpenCode、ZCode。配置存在、守护进程可达、MCP 握手、真实捕获与回忆注入、工具调用是不同检查点；前一项成功不能替代后一项。

| 标记 | 来源与能够支持的结论 | 不能支持的结论 |
| --- | --- | --- |
| H：直接隔离 harness | 主控在 Windows 11 执行下述两个既有用例，`2 passed, 1 warning in 49.22s`；隔离数据目录、备用端口、真实 embedded daemon、synthetic embedder、真实 transformer 子进程与 golden events，检查捕获、T1/T2 输出及卸载恢复 | 未运行真实宿主 app；未证明宿主消费注入结果、用户信任、真实模型或 MCP `tools/call` |
| C：既有 CI | Windows/Ubuntu 的 `install-script-smoke` 检查语法/dry-run；`release-install-verify` 从源码构建 wheel/sdist 并隔离安装，检查版本、extra、配置、MCP initialize/tools/list、沙箱 Codex 生命周期 | 未端到端运行安装编排器；没有模型推理、真实 Codex 信任或 MCP 工具调用；不能称新机一键激活 |
| S：源码检查 | 既有安装分支、注册逻辑和生命周期命令的静态行为 | 未执行的平台、宿主和环境结果 |
| R：历史作者报告 | [原 #292 评论](https://github.com/MnemoSeed/mnemoseed-local/issues/292#issuecomment-6476215796)保留原始审查证据；其中 Windows 安装、可达与握手陈述仅属历史报告 | 不视为本轮直接观察，不跨平台外推，不据此认定四宿主装完即用 |
| U：UNKNOWN | 尚无相应层级证据 | 不补写 PASS 或 FAIL |

H 的命令为 `uv run pytest -q tests/test_host_adapters_e2e.py --junitxml=.mnemoseeding/jobs/install-matrix-292-20261029/host-e2e.xml`，对应 [test_zcode_host_chain_end_to_end_on_a_real_daemon / test_codex_host_chain_end_to_end_on_a_real_daemon](../../../tests/test_host_adapters_e2e.py)。这是主控提供的本轮执行记录；本文件编写期间未重跑测试。唯一 warning 为依赖的 Starlette 弃用提示。

C 的来源为[源码 CI 成功运行](https://github.com/MnemoSeed/mnemoseed-local/actions/runs/40624030405)（`8ce0bac2279cb642a4a9c5f156a50803f4d864c8`）和[清理文档 CI：7/7 成功](https://github.com/MnemoSeed/mnemoseed-local/actions/runs/40800391949)（`a70d9fad21f860b1b8f849627f85846281b4031c`）；这些均是既有运行，不是本 PR 的 CI。任务冻结记录说明当前基线相对源码 CI 仅有 #299 文档差异。覆盖定义见 [ci.yml](../../../.github/workflows/ci.yml) 与 [verify_release_install.py 的 COVERAGE/LIMITATIONS](../../../scripts/verify_release_install.py)。本地 wheel PASS 保留其安装产物与协议表面信用，不替代发布验收。

**所有平台的默认索引新机一键安装仍由 [#179](https://github.com/MnemoSeed/mnemoseed-local/issues/179) 阻断**：2026-10-29T01:33:28.2863050Z，`https://pypi.org/pypi/mnemoseed-local/json` 返回 HTTP 404。这是带时间戳的索引观察，并非本轮在每个平台重跑安装器所得结果。

## 十二格审查矩阵

“捕获/回忆”栏关注真实宿主流程；H 仅保留其隔离链路证明。C 的共享 wheel/MCP 证明不等于某个宿主已完成注册或调用。每格均受 #179 的默认索引阻断。

| 平台 / 宿主 | 安装与注册证据 | 捕获 / 回忆证据 | MCP 证据 | 限制与归属 |
| --- | --- | --- | --- | --- |
| Windows 11 / Claude Code | S：手动 adapter 安装仅 hooks；R：安装/可达报告 | U：真实宿主未验证 | S：该 installer 不注册 MCP；R：未注册 | #301 接线与 MCP 设置；#305 实宿主验证 |
| Windows 11 / Codex CLI | S：hooks + stdio 网关；C：沙箱生命周期；R：重装报告 | H：捕获与 T1/T2；真实 app 为 U | C：隔离网关握手/列工具；真实宿主工具调用 U | #301 安装入口；用户 `/hooks` 重信任待办，#305 验证 |
| Windows 11 / OpenCode | S：一键脚本接线 `hook install opencode`；R：插件工作报告 | U：真实宿主未验证 | S：脚本提示按需另行注册；R：配置报告；调用 U | #301 接线；#302 动词分裂；#305 验证 |
| Windows 11 / ZCode | S：hooks + stdio 网关；R：安装/可达报告 | H：捕获与 T1/T2；真实 app 为 U | R：历史握手报告；真实工具调用 U | #301 安装入口；#305 实宿主验证 |
| Ubuntu / Claude Code | C：共享脚本 smoke/wheel；S：手动 hooks installer；实际注册 U | U | S：installer 不注册 MCP；调用 U | #301、#303、#305 |
| Ubuntu / Codex CLI | C：共享脚本 smoke/wheel、沙箱 Codex 生命周期；实际注册 U | U：没有本轮该平台 H 或实宿主证据 | C：隔离网关握手；实际宿主调用 U | #301、#303；用户信任与真实流程 #305 |
| Ubuntu / OpenCode | C：共享脚本 smoke/wheel；S：脚本接线插件；实际注册 U | U | S：需按提示另行注册；调用 U | #301、#302、#303、#305 |
| Ubuntu / ZCode | C：共享脚本 smoke/wheel；S：adapter 注册逻辑；实际注册 U | U | S：stdio 注册逻辑；实际握手/调用 U | #301、#303、#305 |
| macOS / Claude Code | S：脚本/手动 hooks 逻辑；无 macOS CI，实际安装 U | U | S：installer 不注册 MCP；调用 U | #300 条件分支与 runner；#301、#303、#305 |
| macOS / Codex CLI | S：脚本/adapter 逻辑；实际安装与用户信任 U | U | S：stdio 注册逻辑；实际握手/调用 U | #300、#301、#303、#305 |
| macOS / OpenCode | S：脚本接线插件；实际安装 U | U | S：需按提示另行注册；实际握手/调用 U | #300、#301、#302、#303、#305 |
| macOS / ZCode | S：脚本/adapter 逻辑；实际安装 U | U | S：stdio 注册逻辑；实际握手/调用 U | #300、#301、#303、#305 |

静态依据：[install.ps1](../../../install.ps1)、[install.sh](../../../install.sh)、[Claude Code installer](../../../src/mnemoseed_local/hosts/claude_code/install.py)、[Codex CLI installer](../../../src/mnemoseed_local/hosts/codex_cli/install.py)、[ZCode installer](../../../src/mnemoseed_local/hosts/zcode/install.py)。Claude Code installer 管理七个 hook 事件，但不写 MCP 注册；Codex 完整结构状态仍为 `trust unverified`，用户须在宿主 `/hooks` 审阅信任，配置重写后的历史重信任提示不能记作已完成。ZCode 的静态启用与注册也不能证明真实 app 已加载。

macOS 的明确代码缺口是 **Ollama 命令缺失时**，Darwin 分支打印手动安装提示并非零退出；已有 Ollama 的 macOS 环境不因此被判全部失败，其后续结果仍为 U。

## 生命周期与问题归属

以下为 S，除特别注明的 H/C 外均未执行真实宿主或系统操作。命令入口见 [cli.py](../../../src/mnemoseed_local/cli.py)。

| 环节 | 既有边界 | 缺口归属 |
| --- | --- | --- |
| 安装 / 宿主选择 | 两个一键脚本固定安装 OpenCode 插件；Claude Code、Codex、ZCode 需各自手动接线。Claude Code hooks 安装不含 MCP；OpenCode 脚本也提示 MCP 按需注册 | [#301](https://github.com/MnemoSeed/mnemoseed-local/issues/301) |
| 启动 / 自启 | `up` 为守护进程启动入口；Windows 脚本含登录任务注册，当前仅 S/R，未在本轮实测登录启动。Unix 脚本提示运行 `up`，没有 systemd/launchd 接线。hook 注册或 `/healthz` 可达不证明 hook 自动启动 daemon | [#303](https://github.com/MnemoSeed/mnemoseed-local/issues/303)；真实启动流程归 #305 |
| 宿主启停 / 卸载 | OpenCode 在 `hook install/status/disable/enable/uninstall opencode`；`host install/status/uninstall --host …` 为 Python adapters，Claude Code 另有 `hook` 入口。OpenCode 插件变更提示重启宿主；不能把动词存在视为统一生命周期 | [#302](https://github.com/MnemoSeed/mnemoseed-local/issues/302) |
| 数据与应用卸载 | `uninstall` 默认保留数据；Windows 先处理自有登录任务，失败会返回。`--purge` 才删除应用数据，并要求确认，`--yes` 跳过确认。此入口不等于逐宿主清理、停止全部进程或卸载 uv tool；这些不能从数据保留语义推出 | #302 |
| Adapter 卸载验证 | H：Codex/ZCode 隔离链恢复宿主配置并检查幂等；C：Codex 沙箱配置恢复。各真实宿主、三平台结果仍 U | #302 生命周期边界；[#305](https://github.com/MnemoSeed/mnemoseed-local/issues/305) 实宿主证据 |
| macOS / 发布 | 缺 Ollama 的 Darwin 退出与无 macOS runner 属 [#300](https://github.com/MnemoSeed/mnemoseed-local/issues/300)；默认索引发布及新机安装属 #179 | #300、#179 |

#305 目前仅捕获验证工作，要求区分实际宿主捕获、回忆注入、信任及 MCP 工具调用；此 PRD 不启动该工作或 #300–#303 工程。原报告保留于既有 issue 评论作为历史来源；此公开文档不复制私人报告文本、路径、身份、账户信息或真实记忆。

## 理论锚与验收记录

本批只校正既有证据与覆盖，不引入新特性、记忆机制或新理论；无新增借用理论，也不以神经科学类比支持安装结论。

文档编写已核对冻结基线与上述来源；独立实现 QA、完整本地 gates、本 PR CI、merge、同步与最终验收均待主控记录，当前不声明 #292 关闭、发布完成或十二格通过。
