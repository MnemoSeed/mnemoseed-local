# PRD-345：opencode 宿主验证的环境隔离方法重建（调查批次）

日期：2026-11-05。状态：调查执行完毕；首轮独立 QA 判定 NOT CLOSABLE（1 BLOCKER + 3 IMPORTANT + 4 NIT），修复轮已逐项落实（边界事件 1 重写、配方验证步收紧、两件证据补存、措辞修正），待主控复核。基线：`eada4d6`（分支 `investigation/345-opencode-isolation`，worktree `.mnemoseeding/worktrees/w345`，= 当前 main）。本轮为 #345 的调查单元（可证伪问题 → 有界实验 → 结论），不是代码修复批次：除本文档外零仓库写入，全部实验证据保存在临时证据树 `%LOCALAPPDATA%\Temp\w345-isolation\`。调查对象为实机安装的 opencode **v2.0.32**（npm `@opencode/cli`，二进制 `…\npm\node_modules\@opencode\cli\bin\opencode.exe`，213,722,504 字节）；owner 的 **2.2.5 常驻服务**（桌面服务 PID 460 + serve PID 39260 @ 127.0.0.1:40924）全程零停止、零连接、零配置触碰。7788/4096 全程空置（每个实验前后 netstat 实测）。

## 可证伪问题

#345 原始陈述：Windows 上 opencode 2.0.32 对 `OPENCODE_CONFIG_DIR` / `XDG_CONFIG_HOME` / `XDG_DATA_HOME` 只有部分遵守（CLI 日志遵守 `XDG_DATA_HOME`；插件发现、会话 db 与 auth 仍解析真实家），破坏了后续所有 opencode 宿主验证的 env 隔离方法。本轮将其分解为四个可判定的子问题：

- **Q1**：`HOME`/`USERPROFILE` 重定向能否移动插件发现 / 会话 db / auth 的解析落点？
- **Q2**：`opencode serve` 面对隔离 env 的遵守是否完整（插件加载、会话 db、auth、日志各落何处）？
- **Q3**：2.0.32 是否存在凌驾于 env 之上的原生覆盖（config 键、CLI 旗标、env 键）？
- **Q4**：Windows 2.0.32+ 的最小可靠隔离配方是什么（env/重定向集合 + 机械化验证步骤），以及 2.2.5 常驻服务偏斜如何免疫？

## 机制性发现（二进制字符串取证 + CLI help + 实机复核）

**首要发现（改变 #345 的定性）**：2.0.32 是**客户端/服务器架构**——`opencode run`/`plugin`/`auth`/`session` 等 CLI 子命令是**客户端**，默认连接**后台服务**（background service），全部服务器面工作（插件扫描、会话 db、auth、模型回合）由服务进程完成。服务发现经 `<state>/opencode/service.json` 注册文件（`{id, version, url, pid, password}`）；发现落空时客户端**自举**一个 `opencode serve --service`（继承调用方 env）。第二、三轮观察到的「部分忽略 env」实为：**第二/三轮的隔离 env 未含 `XDG_STATE_HOME`（也未重定向 homedir），客户端的 state 目录解析落真实家 → 读到 owner 服务的注册 → 会话被驱动进 owner 环境**（真实家 db/auth/插件），而客户端自己的 CLI 日志（写 `data/log`）遵守了 `XDG_DATA_HOME`。全部既有观察在此机制下自洽。证据：内嵌 troubleshooting 文档逐字（原文现保全于证据树 `sessions\troubleshooting-doc-excerpt.txt`，含 `opencode service status`、`opencode --standalone`、role=cli/role=server 日志分层——QA 修复轮补存；首轮记录仅引用未保全，如实改注）、注册文件实测（见「2.2.5 偏斜」节）、EXP-B2 自举服务实测。

**路径解析模块（二进制逐字，`strings-extraction.txt` 段 [1]）**：

```
n = a.homedir()
g = process.env.XDG_DATA_HOME  || join(n, ".local", "share")
h = process.env.XDG_CACHE_HOME || join(n, ".cache")
y = process.env.XDG_CONFIG_HOME|| join(n, ".config")
u = process.env.XDG_STATE_HOME || join(n, ".local", "state")
data=join(g,"opencode") cache=join(h,"opencode") config=join(y,"opencode")
state=join(u,"opencode") log=join(data,"log") db=data 下 opencode.db
// 服务层覆盖：config = process.env.OPENCODE_CONFIG_DIR ?? Path.config
// home getter：process.env.OPENCODE_TEST_HOME ?? os.homedir()
```

**原生覆盖面清单（Q3 答，实测 + strings）**：

| 覆盖面 | 形态 | 效果 | 证据 |
| --- | --- | --- | --- |
| `--standalone` | CLI 旗标（root / `run` / `api`；`plugin`/`auth` 等不接受） | 私有服务器替代后台服务，随进程退出 | root/run/api `--help`；EXP-D1/D2 |
| `--server <url>` | CLI 旗标（root/run/api） | 显式连接指定服务器 URL | run/api `--help`；**未实测**（残留未知） |
| `OPENCODE_PASSWORD` / `OPENCODE_SERVER_PASSWORD` | env | `--server` 连接的鉴权密码 | strings 段 [3] |
| `OPENCODE_CONFIG_DIR` | env | 服务层 config 目录覆盖（凌驾 `XDG_CONFIG_HOME`） | EXP-A1 config 行；EXP-C1 config dirs |
| `OPENCODE_TEST_HOME` | env | `Path.home` getter 兜底 | strings 段 [1]；**不能**移动二级 `~/.opencode` 目录（EXP-C4） |
| `OPENCODE_LOG_LEVEL` | env | 日志级别 | strings；`--log-level` 全局旗标等价 |
| `opencode debug paths` | 子命令 | **直接打印** data/cache/config/state/home/log/db 解析结果——隔离验证的机械断言工具 | EXP-A1–A3 |
| `opencode service status/start/stop` | 子命令 | 后台服务管理 | `service --help`（本轮未对 owner 服务使用） |

版本哈希端口：无显式端口的后台服务监听 `10000 + hash(version)%50000`（win32 下 ≥49152 时重映射 `45056|(port&4095)`）——2.0.32 实测自举端口 **45278**；这是「2.2.5 服务与 2.0.32 自举服务永不撞端口」的结构性原因。

## 有界实验与在盘证据（全部隔离 env 下执行，PATH 剥离预检 `command -v mnemoseed-local` 必须失败）

env 变体记号：**A1** = `OPENCODE_CONFIG_DIR`+`XDG_DATA_HOME`+`XDG_CONFIG_HOME`+`XDG_STATE_HOME` → 证据树（`w345-child-env.sh`）；**A2** = A1 + `HOME`/`USERPROFILE` → `home-r\`；**A3** = 仅 `HOME`/`USERPROFILE` → `home-r\`（XDG/OPENCODE 全 unset）。

| 实验 | 变体 | 判定点 | 结果（measured） |
| --- | --- | --- | --- |
| EXP-A1 `debug paths` | A1 | 路径解析落点 | data/state/db/log → xdg 树；config → `OPENCODE_CONFIG_DIR`（**凌驾** XDG_CONFIG_HOME）；cache → **真实** `~/.cache`；home → 真实家 |
| EXP-A2 `debug paths` | A2 | homedir 与 XDG 优先级 | XDG 全胜；home/cache/bin 行随 homedir 走（`home-r\.cache`）——**Bun `os.homedir()` 遵守 USERPROFILE/HOME 重定向（实测）** |
| EXP-A3 `debug paths` | A3 | homedir-only 是否全移 | **全部移入 `home-r\`**（data/cache/config/state/log/db）——Q1 对解析面的答案为是 |
| EXP-B1 `--standalone plugin list` | A1 | 旗标接受度 | `plugin` 子命令**拒绝** `--standalone`（仅 root/run/api 接受） |
| EXP-B2 `plugin list` | A1 | 客户端面行为 | 输出 `No plugins found`（客户端列表面的渲染结果，面向 package/`plugin` 数组插件清单——与服务器面目录扫描发现是两个表面，本实验的发现判据取服务端 loading 行而非列表输出）；**自举**后台服务（`serve --service`，PID 38088，端口 45278），注册文件落在**隔离** state 树；服务日志（隔离 xdg 树）出现 `loading plugin id="…cfg\plugin\probe-w345-cfg.ts"`——服务器面插件扫描遵守 `OPENCODE_CONFIG_DIR` |
| EXP-B3 自举服务 `/api/config` | A1 | 服务面 config 解析 | config 目录清单 = [`cfg`（隔离），真实 `~\.opencode`]；Basic `opencode:<password>` 鉴权复现成立 |
| EXP-C1 serve 全集审计 | A1 | Q2 各面 | serve（端口 4579，PID 30812）：config dirs 同 B3；`POST /api/session` 会话创建成功（`ses_e57025183ffeh2pZizoq30umXE`，cwd=隔离目录）；**db（opencode.db+wal+shm）落 xdg 树**；日志落 xdg 树；插件加载行指向 cfg 探针 |
| EXP-C3 serve 最小配方审计 | A3 | Q4 最小集合 | serve（端口 4581，PID 12100）：config dirs = [`home-r\.config\opencode`，真实 `~\.opencode`]；插件加载行指向 home-r 探针；db/log 全落 `home-r\.local\share\opencode`；`auth list` 输出 `No authenticated integrations`（**owner 真实集成未出现——真实 auth.json 未被读**）；`auth export` 返回 `[]`（同负证）；自举服务（PID 27812）同样隔离 |
| EXP-C4 `OPENCODE_TEST_HOME` | A1+该 env | 二级目录可否移动 | `/api/config` 第二项仍为真实 `~\.opencode`——**不可移**（残留未知：其解析机制未在 strings 中定位） |
| EXP-D1 `run --standalone` 真实会话 | A3 | Q4 run 面 | exit 0，模型回答 `OK`（hosted 默认 provider）；`loading plugin` 行指向 home-r 探针；隔离 db（home-r）记录该会话（slug `eager-wolf`，directory=隔离 cwd）；role=server 行确认私有服务器；进程随 CLI 退出 |
| EXP-D2 `run --standalone` 真实会话 | A1 | Q4 run 面全集变体 | exit 0，`OK`；插件行指向 cfg 探针；xdg db 更新（08:01） |

auth 面判据（Q1/Q2 的 auth 子格）：2.0.32 的凭据主存储为**会话 db**（strings 段 [2]：迁移 `20260805200742_import_legacy_credentials` 从 `join(data,"auth.json")` 导入；legacy 兜底路径之一为 `join(homedir(), ".opencode", "auth.json")`，亦受 `XDG_DATA_HOME` 支配）。实测负证：隔离 env 下 `auth list`/`auth export` 均未出现 owner 真实集成（若读真实 auth.json 必然出现）→ auth 隔离由 data 目录隔离承载。哑元 marker（`w345-probe-*`）未在列表出现，属哑元形状被迁移/schema 过滤——导入路径未逐字段验证（残留未知）。

## 结论：最小可靠隔离配方（Q4）

**配方 A（推荐，最小集合——homedir-only）**：

1. `HOME` 与 `USERPROFILE` 同时指向临时树根（如 `%TEMP%\w3xx-<batch>\home-r\`）；`Path.home()` 类工具（mnemoseed installer、ZCode bundle 的 `HOME` 读取）同步受益。
2. PATH 剥离全部 mnemoseed 载体（`.venv/Scripts`、`Roaming/uv`、`~/.local/bin`、`~/bin`、系统 Python310），预检 `command -v mnemoseed-local` 必须失败后才启动任何宿主子进程（第二轮 7788 事件纪律，不变）。
3. run 面：`opencode run --standalone …`（私有服务器，随进程退出，无端口占用）。serve 面：`opencode serve --hostname 127.0.0.1 --port <备用端口>`（备用端口清单继续排除 7788/4096/27788/27789/27790/4577/40924/45278/28894）。
4. 客户端面（`plugin list`/`auth list`/`session list` 等）会**自举常驻后台服务**（隔离 env 下该服务亦隔离，但**进程存活**）：用毕必须 `opencode service stop`（同 env）或按 PID taskkill，并记入进程台账。
5. 机械验证步（QA 修复轮收紧——本轮自身的前后快照方法即反例）：(a) 启动前 `opencode debug paths` 断言 `data/config/state` **加 `cache` 与 `tmp`** 各行全部落在临时树或批内可接受面（cache 必须隔离，见边界事件 1 的硬链接残留；tmp 为共享 Temp 区，可选加固为将 `TMP`/`TEMP` 一并指向批树）；(b) 7788/4096 前后 netstat；(c) 结束后真实家快照 diff，**前后两次快照必须同范围**：同一显式目录清单（`~/.config/opencode`、`~/.local/share/opencode`（递归）、`~/.opencode`、`~/.local/state/opencode`、`~/.cache/opencode`，同 maxdepth、同格式、同顺序），禁止「top-level only」式的中途缩圈（本轮 after 快照恰因遗漏 cache 段而放过了 213 MB 残留）；(d) diff 按**路径名 + size + 链接数**比对，禁止以 mtime 判写——硬链接自拷贝继承源 mtime，mtime 判据对之结构性失明（本轮 `opencode-service-38088-*.exe` 即实例）。

**配方 B（显式 env 集，与轮三 serve 方法论兼容）**：`OPENCODE_CONFIG_DIR` + `XDG_DATA_HOME` + `XDG_STATE_HOME` + `XDG_CACHE_HOME` 全部指向临时树，且**仍建议叠加** `HOME`/`USERPROFILE` 重定向——否则 `cache`（含自举服务写出的 ~213MB `opencode-service-<pid>-*.exe` 硬链接自拷贝与 models.json 缓存）落真实 `~/.cache/opencode`（本轮实测的写入事件，含一处持久残留，见边界事件 1）。`XDG_CONFIG_HOME` 可设可不设（`OPENCODE_CONFIG_DIR` 凌驾之，EXP-A1）。

**两配方共同残留（只读触及，不可移）**：二级全局配置目录 `~/.opencode` 在一切 env 变体下仍解析真实家（EXP-C4）——接触面为目录扫描 + fs watcher subscribe（role=server 日志可见），实测无写入（快照 diff）。配方无法消除该只读触及，验证轮如实记录即可。

**2.2.5 常驻服务偏斜（免疫条件）**：owner 服务注册于真实 `~/.local/state/opencode/service.json`（`version 2.2.5`、`url http://127.0.0.1:28894`、`pid 460`；文件内容含服务密码，按秘密边界只记结构与位置、不引值）。**任何未隔离 state 解析的客户端面都会发现它**——轮二实测 run 会话即被驱动进该服务（#345 原始现象）。免疫=配方 A/B 任一（state 解析落临时树 → 发现落空 → 自举隔离服务；或 `--standalone`/`--server` 显式化）。未来验证轮应先 `debug paths` 确认 state 行落临时树，即天然免疫。

## V305 协议修正案（提案引文；PRD-305 本体由 V305 座位修订）

对 PRD-305 第三轮「方法论修正」第 1 条的**替换文本**：

> 1. **`opencode serve` 与 `opencode run --standalone` 为 2.0.32 的两个自托管面**：`opencode run` 是客户端，默认经 `<state>/opencode/service.json` 连接后台服务（owner 环境在场时即 owner 服务）——这是第二/三轮「env 部分忽略」现象的机制（#345 已裁定，见 PRD-345）；serve 与 `run --standalone` 自托管并完整遵守调用方 env。opencode 实机验证一律使用自托管面：serve 用 `--hostname 127.0.0.1 --port <备用端口>`（API 鉴权 HTTP Basic `opencode:<password>`，前缀 `/api/`，spec 在 `/openapi.json`）；run 用 `opencode run --standalone`。

对第三轮方法论修正 2（子进程 PATH 剥离）后**追加**：

> 2bis. **opencode 隔离 env 集（PRD-345 配方）**：`HOME`+`USERPROFILE` 重定向到临时树（最小集合，全部解析面随动，含 cache/bin）；或显式集 `OPENCODE_CONFIG_DIR`+`XDG_DATA_HOME`+`XDG_STATE_HOME`+`XDG_CACHE_HOME`。启动前以 `opencode debug paths` 断言 data/config/state 落临时树；客户端面子命令（`plugin list` 等）会自举常驻后台服务，用毕必须同 env `opencode service stop` 或按 PID 终止并记台账；二级目录 `~/.opencode` 仍解析真实家（只读触及，如实记录）。`XDG_STATE_HOME` 必须隔离——否则客户端将发现并驱动 owner 常驻服务（2.2.5 偏斜，#345 原始现象）。

## 边界事件与偏差（全部如实记录）

1. **A1-env 探针在真实 `~/.cache/opencode` 的写入事件（含持久残留，QA 修复轮改写——原记录的「无持久残留」判定错误，以此为准）**：B2 自举服务（PID 38088，2026-11-04T23:52:54Z = 本地 07:52:54 启动）在真实 `~/.cache/opencode` 写入服务自拷贝 `opencode-service-38088-6fa07392.exe`（213,722,504 字节），与同机制的其他拷贝同为对已安装二进制的**硬链接**（实测与 `…\npm\node_modules\@opencode\cli\bin\opencode.exe` 及隔离树内 `home-r\.cache\opencode\opencode-service-27812-08d379f7.exe` 共享同一 inode，links=4）。**该文件在真实家持续存在至今（QA 后复核在盘，`post-qa-cache-state.txt`）**——本批不删除，去留归 owner（硬链接语义下删除该链接不影响源二进制内容，清理由此安全）。原判定失效的两个结构性原因如实记录：(a) 无残留判据基于 mtime（「现存文件 mtime 全部早于本轮」），而硬链接拷贝**继承源文件 mtime**（2026-10-18T02:50），判据对硬链接自拷贝结构性失明；(b) 后像快照（`real-home-after.txt`）把 `.local/share/opencode` 降为 top-level-only 且**整体遗漏 cache 段**，前像的 cache 清单（`real-home-before.txt` 第 320 行，other-candidates 段 maxdepth-1 截断至 8 行）未进入 diff。中间批 `ls | head -6` 抽查亦被截断，未见该行。
   **swap（19880→38088）**：前像在盘的 `opencode-service-19880-4c278524.exe`（同 213,722,504 字节、同源 mtime，属同一硬链接簇）在本轮窗口内消失（观察窗：前像 23:43:24Z 仍在、cache 目录 mtime 23:52:54Z）——归因**推断**为 opencode 服务自举例程对同版本死 PID 旧拷贝的清道（38088 创建与目录 mtime 同秒）；owner 常驻服务的生命周期清理为备选解释；本批无任何主动删除动作（边界禁止仓库外删除），两解释均非本批意图行为，如实并陈。
   根因与配方修正不变：A1 集未含 cache 面；配方 B 补 `XDG_CACHE_HOME`、配方 A 由 homedir 重定向承载（实测 27812 的同型拷贝因此落在隔离树内）。**修正后的结论：本轮在真实家有一处持久写残留（B1 的硬链接拷贝）+ 一处本批窗口内的旧链接消失（swap，非本批主动动作）；其余真实家表面零写入。**
2. **真实家只读接触台账**（全部为元数据/配置读取）：(a) 前后快照的 `find` 元数据遍历（`~/.config/opencode`、`~/.local/share/opencode`、`~/.opencode`、候选目录存在性）；(b) `~/.config/opencode/service.json` 读取一次（141 字节，服务发现结构；其 password 字段值已进入本轮会话记录但**未写入任何证据文件、未写入本文档、未用于连接**）；(c) `~/.local/state/opencode/service.json` 读取一次（owner 服务注册，同上处理）；(c2) 探针自托管面的服务密码（serve 4579/4581/4583 与自举服务 45278 的启动行打印值）为一次性凭据，所属进程全部已终止，值仅存在于临时证据树的日志文件内；owner 服务密码经全文核对**不在**任何证据文件或本文档中（N3）；(d) 每个 opencode 进程的 config 目录扫描对真实 `~/.opencode` 的只读触及（与轮三记录同型，含 fs watcher subscribe）；(e) role=server 进程的 fs watcher 订阅面远大于 (d)：隔离 cwd 位于 Temp 下，宿主沿目录链上溯建立订阅，实测覆盖真实 `C:\`、`C:\ProgramData`、`C:\Users`、`C:\Users\Little Star` 根链、沿途真实 `AGENTS.md` 与真实仓库 `.opencode` 目录（证据：`exp-d1-run.err`、`exp-d2-run.err`、`xdg/opencode/log/opencode.log` 的 watcher subscribe 行去重清单）——该面为只读订阅，无写入证据（除边界事件 1 的 cache 残留外，真实家快照 diff 无本批可归因变化）。真实 `auth.json` 内容零读取（auth list/export 负证）。`~/.config/opencode` 前后快照逐字节 diff 一致；`~/.local/state/opencode`、`~/.opencode`、`~/.codex`、`~/.claude`、`~/.mnemoseed-local` 零触碰；`~/.local/share/opencode` 的 log/db 变化均为 owner 常驻服务自身活动（7:49 的 +1457 字节日志行与其服务运行窗口一致，无本轮可归因写入）。tmp 面（N1）：`debug paths` 的 `tmp` 行在全部 env 变体下均解析 `%LOCALAPPDATA%\Temp\opencode`（`os.tmpdir()` 基于 `TMP`/`TEMP`，本批配方未重定向）——该面为用户 Temp 区内的共享临时工作区（非用户档案家），在盘现存文件全部早于本轮（.bun dll 等 Sep 日期），探针的瞬态 temp 写入属临时区常规语义且事后无法按 mtime 归因；如需零共享接触，配方可叠加将 `TMP`/`TEMP` 一并指向批树（未实测，记为加固项）。
3. **进程台账**：全部探针进程即杀即录（`process-ledger.txt`）：自举服务 38088（B2）、serve 30812（C1）、serve 12100 + 自举服务 27812（C3）、serve 42432（C4）；D1/D2 为进程内私有服务器，随 CLI 正常退出。终态仅余 owner 的 460/39260，全部探针端口（45278/4579/4581/4583）确认释放，7788/4096 全程空置。无任何探针连接 owner 服务（28894/40924 零请求）。
4. **实验自障一次**：首轮 D1 因 `timeout` 在剥离 PATH 上解析为 Windows `timeout.exe` 报语法错误——未产生任何会话，改 `/usr/bin/timeout` 重试成功。
5. **模型回合披露**：D1/D2 两个真实回合走 opencode 自带托管 provider（与轮三同型：隔离数据目录内无真实凭据 auth.json——树内仅有本轮植入的哑元 marker 文件（`w345-probe-*`），实测、`cost: 0` 面、无凭据创建、无计费面）；提示词均为合成单句。
6. **证据树为新建**：`%LOCALAPPDATA%\Temp\w345-isolation\`（本轮全部隔离 env 落点与日志所在），第二/三轮旧证据树（`w305-v305-r2/`、`w305-v305-r3/`）仅只读引用，未触碰。按边界未执行任何仓库外删除。

## 理论锚

本批为调查与文档批次，不引入新特性、新记忆机制，无新增借用理论；不以上一轮未验证的神经科学类比支持任何结论。V305 注入机制的理论锚（T1/T2、budget 语义）注册地位不变。

## 残留未知

1. **二级 `~/.opencode` 目录的解析机制**：XDG_*/`OPENCODE_CONFIG_DIR`/homedir 重定向/`OPENCODE_TEST_HOME` 四者均无效（EXP-C4 实测）；strings 中定位到的 `.opencode` 引用均为 bin/升级路径与项目级目录扫描，全局二级 config 目录的构造点未定位。影响面：只读触及（扫描+watcher），无写入证据。若未来批次需要零接触，需上游机制调查或进程级文件监控。
2. **`--server <url>` + `OPENCODE_PASSWORD` 显式连接**：strings 证实存在，未实测（本轮两个自托管面已覆盖需求）；如需「一个 serve 实例 + 多客户端」形态可实测补证。
3. **2.0.32 客户端对 2.2.5 注册的版本兼容判定**：轮二实测连接成立；strings 的版本检查函数（`service.json` 读取路径）如何放行跨版本未完全解码。对本轮结论无影响（配方使 discovery 落空），仅机制完整性待上游确认。
4. **auth 哑元导入路径**：迁移/schema 过滤了本轮哑元形状（导出 `[]`），legacy `auth.json` 导入的逐字段验证未做；auth 隔离结论由负证（owner 集成未出现）+ strings（主存储=db）支撑，判据等级 inferred+负证实测。
5. **`XDG_CACHE_HOME` 单变量隔离**：未单独实测（homedir 兜底实测 + strings 支撑）；配方 B 若不叠加重定向，建议先 `debug paths` 复核 cache 行。

## 验收记录

文档编写核对的证据在盘（`%LOCALAPPDATA%\Temp\w345-isolation\`，清单见 `sessions\evidence-index.txt`）：三个 `debug paths` 输出、B2/C1/C3/C4 的 config JSON 与服务日志（插件加载行原文）、隔离注册文件两份、D1/D2 运行日志与 stderr（role=server 行、插件行）、auth 判据输出、真实家前后快照、二进制逐字取证（`strings-extraction.txt`）、进程台账、探针与哑元文件本体。**QA 修复轮修订与补存**：(a) 原「证据全部在盘」的绝对化表述不准确——前像快照的 cache 清单**确实存在**（`real-home-before.txt` 第 320 行，other-candidates 段），失效在 after 快照的范围缩圈（drop cache 段 + `.local/share/opencode` 降为 top-level-only）使该段从未进入 diff；(b) 内嵌 troubleshooting 文档当时仅引用未保全，现补存为 `sessions\troubleshooting-doc-excerpt.txt`（I2）；(c) 补存 `sessions\post-qa-cache-state.txt`（残留文件在盘实证 + 硬链接 inode 证据 + swap 记录，修复轮只读取证）。**未以文件留存件**（仅存在于本轮会话记录）：各命令的即时回显中间值、C1/C3/C4 的 curl 逐步响应头、38088 终止时刻的 netstat 读数（台账为回填时间）、中间批对 `~/.cache/opencode` 的 `ls | head -6` 抽查回显（截断输出恰未含 exe 行；该抽查的「无持久残留」判定方法学错误——mtime 判据对硬链接失明 + 截断 + after 快照缩圈三者叠加，判定被 QA 推翻，见边界事件 1）。独立实现 QA（首轮 NOT CLOSABLE 判定 + 本修复轮）、完整本地 gates、本 PR CI、merge 与最终验收均待主控记录；本文档为调查记录，不声明 #345 的实现面关闭。
