# PRD-179 发布记录：配置恢复、Codex 安装兼容、打包与干净 runner（A+B+C）

状态：主控已验收本机整合候选：全套门禁、隔离 wheel 安装验证及具名独立对抗 QA 均通过。**干净 Linux/Windows CI、PR 合并、tag、PyPI 发布与默认索引安装验证尚待执行**；真实宿主信任与模型推理不属于本批自动验证的完成声明。

本文件记录 #179 的冻结范围和验收事实；发布闭环由 GitHub issue #179 及关联 PR 的检查和发布记录承接。

范围：0.2.0 的发布链（打包、安装器 pin、干净 runner 验证）、配置缺省恢复（A）、Codex 安装兼容（B）。不新增记忆算法，不改动捕获、巩固、检索、衰减任何行为。

## 1. 理论锚：本批不借用

本批**不借用**任何神经科学或心理学理论。

- 不借用清单：间隔重复与遗忘曲线、检索练习、双编码、情境依赖记忆、认知负荷等均与本批无关。
- 理由：本批全部改动是配置缺省取值、宿主 hooks 文件结构、打包与安装路径与 CI 证据，不产生任何记忆行为变化。
- 理论与实现严格分离：既有理论锚仍只存在于 `docs/zh/design/REFERENCES.md`，本 PRD 不新增、不改写、不据此推设计规则；本批验收项全部是工程事实（文件字节、退出码、JSON 结构、JSON-RPC 报文、进程隔离）。

## 2. A 批 — 配置缺省恢复

交付文件：`src/mnemoseed_local/config.py`、`tests/test_config.py`。

- 矩阵：`dream.auto_trigger` 与 `capture.auto_recall` × missing-file / missing-table / missing-key / explicit true / explicit false。
- 残留 mutant：缺省分支返回 `False`。先跑 RED：`uv run pytest -q` → **2 failed / 50 passed**（capture missing-key 用例命中该 mutant），随后仅把 `False` 恢复为 `True`，GREEN **52 passed**。
- 显式 `false` 仍解析为 false，且 `init` 拒绝覆盖既有配置文件（字节不变）；版本面四文件保持原样。

## 3. B 批 — Codex 安装兼容

交付文件：`src/mnemoseed_local/hosts/codex_cli/install.py`、`tests/test_hosts_codex.py`、`tests/test_host_adapters_e2e.py`、`README.md`。

- JSON 正式形状：根 `hooks` → 事件 → `groups` → `handlers`。install / status / uninstall 三类操作统一按该容器读写。
- 迁移与幂等：只移除或替换带明确 marker 的旧处理器（nested 或历史 flat）；每事件插入一个当前规范 handler；保留第三方字段、group metadata 与非我方 handler 的值；重复安装幂等；第三方 flat 项既不迁移也不删除。
- 原子性：先校验全部输入（hooks 容器、已知事件类型、JSON/TOML 可解析）再写任何文件；拒绝时 `hooks.json` 与 `config.toml` 原始字节均不变。成功时 JSON 仅承诺外来值保留，TOML 外来文本保持。
- PowerShell：可执行路径经 call operator + 安全单引号字面量引用传入，含空格、apostrophe、`$`、反引号的路径均可执行，且路径不会被拼成可执行表达式；POSIX 保留可执行的 shell 引用；shim 与 `-m` fallback 均覆盖。实际 shell 引用用例 **8/8** 通过。
- trust 语义（旧文案 "untrusted" 按实际实现更正）：`host status` 把结构结果（not-installed / partial / malformed）与 daemon 可达性分开表示；完整规范安装统一报 **`trust unknown`**（installed、未验证、附 `/hooks` 一次性授权提示），不写 `trusted_hash`，不因全文件 hash 或任何无关 `trusted_hash` 宣称 trusted/stale。真实 trust 不由本批写入。
- 设计依据：[Codex Hooks 官方规范](https://learn.chatgpt.com/docs/hooks)及既有真实宿主验证记录；本批测试使用隔离宿主 home 与假守护进程，不以 schema 测试冒充真实宿主信任。
- 门禁：`uv run pytest -q` → **4156 passed / 6 skipped**；`uv run ruff check`、`uv run ruff format --check`、`uv run mypy src` 全部通过。冻结计划已声明 B 不再改动。

## 4. C 批 — 打包、解释器请求与干净 runner 验证

交付文件：`install.sh`、`install.ps1`、`tests/test_version_single_source.py`、`.github/workflows/ci.yml`、`scripts/verify_release_install.py`、`docs/zh/prd/PRD-179-release.md`。

- 版本面：根 `pyproject.toml` / `uv.lock` 为 **0.2.0**；两个一键安装器精确 pin **`mnemoseed-local[codex]==0.2.0`**（含 extras，保证所宣传的 Codex 压缩日志依赖可用），`tests/test_version_single_source.py` 同步适配 extras 且尾部守卫不放宽，版本一致断言未被弱化。
- 解释器请求：安装分支、升级分支、两者的 dry-run 输出与隔离 wheel harness 均显式请求 **`--python 3.12`**；uv 可自行 provision 该解释器，因此不依赖宿主默认解释器。
- 验证器 `scripts/verify_release_install.py`（纯标准库）：`uv build` 产出唯一 wheel + 唯一 sdist（记录文件名/大小/sha256）→ artifact metadata（Name/Version、`Provides-Extra: codex`、`Requires-Dist: zstandard; extra == "codex"`、console script、`RECORD`、sdist `PKG-INFO`）→ 隔离 `UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` / `UV_CACHE_DIR` 中 `uv tool install "<wheel>[codex]"` → 已安装 CLI `--version` → 以 tool 环境自身解释器（`-I`、cwd 为临时 scratch）导入 `zstandard` 且包文件不在检出目录内 → newline JSON-RPC `initialize` / `notifications/initialized` / `tools/list`（不发送 `tools/call`）→ 隔离 home `init` 断言 `dream.auto_trigger` 与 `capture.auto_recall` 默认 ON、isolated graph 存在 → 预置显式 false 时 `init` 退出 1 且配置字节不变、加载仍为 false → 沙箱 Codex `host status` / `install` / `uninstall` 与生成命令经宿主 shell（Windows PowerShell 调用运算符 / POSIX sh）执行。
- 隔离约束：子进程 HOME / USERPROFILE / XDG / APPDATA / LOCALAPPDATA / `MNEMOSEED_LOCAL_HOME` 均指向各自临时目录，baseurl 只用临时端口，任何进程都不绑定也不访问 7788，不触碰注册表、计划任务、钥匙串，不下载任何模型，每个子进程都有显式超时。
- 测试证据：最新一轮 `uv run pytest -q tests/test_version_single_source.py` 先 RED **4 failed / 3 passed**（新回归先红），最小实现后 GREEN **7 passed**；harness 自身 `uv run ruff check` 通过。
- CI：`.github/workflows/ci.yml` 新增 `release-install-verify`（ubuntu + windows，30 分钟上限），`release-proof/verify-release-install.json` 在成功与失败时都作为 artifact 上传且不含密钥；任何断言失败、超时或非零退出都让 job 失败，不存在「记录失败但放过」的分支。

## 5. Doctor 容量提示（保留，不掩盖）

`dream.reflect_batch_max_tokens = 8000` 是**分组目标**，不是单次调用上界：`dream/delta.py` 中超 cap 的单 chunk 仍单独成批并按 pack 的 ceiling 裁剪，长对话下 `prefix+delta+margin` 仍可能超过 `num_ctx = 16384`（隔离实测告警 34407 > 16384）。本批：

- 不修改 `delta.py`，不引入 `min(cap, ceiling)` 之类会掩盖大 chunk 的「修法」；
- 现有 `tests/test_cli.py` 明确钉住 full-ceiling 告警，保持不动；
- 不把「完整 doctor 全绿」当作无 Ollama 干净 runner 上的验收条件，验证器因此不运行 doctor，只把该容量提示作为已知边界记录在此；
- 该提示是配置容量建议（调低 ceiling 或调高 `num_ctx` / 换更大窗口路由），不是安装失败。

## 6. 门禁与整合状态

| 项 | 证据 | 状态 |
| --- | --- | --- |
| A 配置矩阵 + mutant 修复 | RED 2 failed / 50 passed → GREEN 52 passed | 完成（本机） |
| B 全量门禁 | 4156 passed / 6 skipped；ruff check、ruff format --check、mypy src 绿 | 完成（本机） |
| B shell 引用实测 | 8/8 用例通过 | 完成（本机） |
| C 解释器回归 | RED 4 failed / 3 passed → GREEN 7 passed；harness ruff 绿 | 完成（本机） |
| 整合全套门禁 | 4170 passed / 6 skipped；ruff、format、mypy 通过；C 独立树 4122 passed / 6 skipped 且静态门全绿 | 主控验收通过（本机） |
| 本机隔离 artifact 安装验证 | 构建、安装、CLI/extra、MCP、默认配置、显式 false、Codex 沙箱七步全部 PASS | 主控验收通过（本机） |
| 具名独立对抗 QA | B 与 A+C 分别取得 CLOSABLE，0 BLOCKER / 0 IMPORTANT；非阻断改进另记队列 #286、#287 | 主控验收通过 |
| clean Linux / Windows CI（`release-install-verify`） | 未跑 | 待执行 |
| 单 commit → PR → merge → tag → publish → 索引 pin 安装验证 | 未做 | 待执行 |
| 发布闭环记录 | 本机候选已验收；CI、合并及索引发布以关联 PR 和发布记录为准 | 待发布 |

## 7. 边界与不宣称

- 历史失败均保留：元数据引号断言、误选 Python 3.10、验证器自身禁止获取 3.12、遗漏宿主 base URL 环境变量。最后一项造成一次对真实 7788 的只读健康探测，流程随即失败；修正后七步验证全通过，三个状态查询均指向临时端口 62026。
- 未发布：无 tag、无 PyPI 上传、无「默认索引 pin 可解析安装」验证；若权限真实阻断则保留待用户动作，PyPI 404 或环境缺失只说明当前观察，不证明 trusted publisher 不存在。
- 未达到真实宿主激活：只观察到 `trust unknown` 与 `/hooks` 提示，一次性的 trust 授权是用户侧动作。
- 未拉取、未加载、未调用任何 Ollama 模型，无推理证据；doctor 未在无 Ollama 的干净 runner 上全绿。
- 安装器（`install.sh` / `install.ps1`）未被端到端执行，其语法与零副作用 dry-run 仍由既有 `install-script-smoke` 覆盖。
- 本机运行验证器的结果只能称「本机隔离安装」，不能称干净 runner 或完整激活。
- `tests/` 与版本面在本批确有变更：`tests/test_version_single_source.py` 新增实际 install/upgrade argv 与 dry-run 一致性的解释器回归，安装器 pin 表面升级为 0.2.0 + `codex` extra（旧版「tests/versions 未改动」的说法不再成立）。

## 8. 后续（pending）

- 本机候选验收完成，继续 PR → 双平台 CI → merge → tag → PyPI → 默认索引 pin 安装验证。
- 发布后的索引安装需与当前本地 wheel 证明分开记录；不能用本机 PASS 替代。
- 如果发布权限实际阻断，保留失败和待处理动作，不宣称 #179 发布闭环完成。

## 9. 主控验收记录（2026-10-27T15:13:56+08:00）

- 源码与独立 QA 审查的 13 文件清单逐项哈希一致；此处仅更新验收事实，不改变工程实现或设计范围。
- 已验证 wheel：mnemoseed_local-0.2.0-py3-none-any.whl，SHA-256 23e4db31486a64d59db10afa6a00c31a0e9cfdd9e00e99df85ad4cd62c8c9644；sdist 未夹带私有派发记录。
- Windows PowerShell 实际路径引用用例 8/8；旧 MCP 配置替换/卸载与伪造表头拒绝检查通过。两个安装器的语法/dry-run、隔离 uv tool upgrade --python 3.12 也已验证。
- 验证报告的个别 detail 字段为失败说明模板，应与 ok 布尔结果一起阅读；该文案改进已记录为 #287。

## 10. Ubuntu CI 失败修复（2026-10-28）

PR #288 首push后 CI：6/7 绿，仅 `release-install-verify (ubuntu-latest)` 失败——验证器 provenance 检查对已安装解释器做符号链接解析，指向共享基础 Python 后误拒合法安装；WSL 真实符号链接 venv 机械复现。修复冻结于 `.release179-linux-contract.md` + `.release179-linux-design.md`（独立设计审查 CLOSABLE 0B/0I/0N）。

- 修复面（严格两文件）：`scripts/verify_release_install.py`（探针增报 `prefix`/`base_prefix`；六路径字段先验绝对路径再构造；`sys.prefix` 必须精确等于 `tool_dir/<project>` 而非 UV_TOOL_DIR 下任意兄弟；`prefix != base_prefix`；调用解释器身份用宿主词法 + 原生大小写归一化、不解析符号链接，同一共享二进制的别名必拒；package/dist/zstandard 各自独立收敛于精确环境内且在检出外；版本与其余检查不变）+ 新增 `tests/test_release_install.py`。
- TDD：测试先行 RED（Windows/Linux 双平台实跑旧源），实现后 GREEN 77/77 × 双平台。
- 独立对抗 QA（GLM-5.3-Flash 会话内席位，对 GPT-6 作者跨族；具名子代理 QA 席两次并发上限，如实披露）：**CLOSABLE 0B/0I/0N**；三变异探针（resolve 式比较=原 CI 缺陷复现、tool_dir 兄弟容忍、venv 判别删除）全部被测试击杀且其余保持绿。记录：`.release179-linux-repair-qa.md`。
- 本会话新鲜 Linux 证据：WSL2 artifact harness 七步全 PASS、failures 0（`.release179-linux-zcode-verify-20261028.json`）。
- 已知无关本地门禁失败：`tests/test_windows_autostart.py` 9 例在中文 locale Windows 上因 schtasks GBK 输出按 UTF-8 解码而失败，主仓 d5658c7 无修复时同样复现——预存环境敏感，与本修复无关，另行开 queue issue；干净 runner CI 为准。
- 修复按纪律 amend 回原发布提交（该提交自身被修绿），不折入后续合并。
