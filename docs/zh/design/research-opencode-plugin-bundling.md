# 调研文档 · opencode 插件(plugins)能力全景 —— 宿主"统一安装面"打包可行性

> 性质：纯网络调研落盘（2026-08-20），非 PRD、不产生代码承诺；作为后续 **B2.6 宿主 plugin 统一安装面设计专题**的依据资料。
> 调研范围：opencode（https://opencode.ai ，开源仓库 `anomalyco/opencode`）的插件能力，聚焦"每个宿主一个定制插件 = 面向本地 daemon 的接口，且以**一个 bundle（MCP + hooks 一起）**可安装/可整体开关"这一目标（对标 Claude Code 插件可打包 MCP server + hooks 并整体启停）。
> 引用原则：每条论断附来源 URL；源码级证据标注文件路径与所在仓库；凡因版本/竞态/未合并而无法定论处，一律标 **needs-probe** 并给出探针协议。
> 调研快照版本：opencode dev 分支（检索日期 2026-08-20，文档页面 Last updated 2026-08-19；issue 引用含 2026-08-19 的最新 PR）。

---

## 摘要：逐问结论表

| # | 问题 | 结论 |
|---|------|------|
| 1 | 单插件启用/禁用（不删文件/配置条目） | **can（有代价）** —— 无 `enabled` 字段；唯一不删文件的自包含开关是 `[spec,{enabled}]` 元组**自实现**短路；TUI 的 `/plugins` 激活开关在 PR #42410（**未合并**） |
| 2 | 插件运行时注册 MCP（`config(cfg)` 改 `cfg.mcp` 是否被接纳） | **needs-probe** —— 机制存在、有真实用户成功（1.18.15，"Works"）；但 MCP 层与 Plugin 层为 sibling layer，MCP 在 state lookup 一次性读 `cfg.mcp`，与 plugin config hook 的先后存在**竞态**，必须本地探针确认 |
| 3 | 分发形式（global/project/npm/本地路径/git URL；可携带内容） | **can** —— 四种来源全支持；`plugin` 数组支持 `[string, options]` 元组；npm 插件包可带依赖，并可通过 config hook 自注入 config 默认值（MCP 可；skills 不行，见 #41234） |
| 4 | 对比 Claude Code 的"一个 bundle = MCP + hooks" | opencode **没有**该原生概念 —— MCP 与 hook 是两套独立子系统；Agent Plugins 标准（agent-plugins.org）尚未落地（#40993/#41561） |
| 5 | 今日真实开关 UX 与重启要求 | 删 `plugin:` 条目 / 移出 plugins 目录（改成非 `.ts/.js` 后缀）；**启动时一次性加载，须重启** |
| 6 | 逃生舱（全局 kill-switch） | **确认是全局、非 per-plugin** —— `OPENCODE_PURE`、`OPENCODE_DISABLE_DEFAULT_PLUGINS`、`OPENCODE_DISABLE_EXTERNAL_SKILLS`；**`OPENCODE_DISABLE_EXTERNAL_PLUGINS` 不存在** |
| 7 | 每-host 定制插件先例 / options 元组自开关 | **can** —— `plugin: ["spec", {enabled:false}]` 元组 + 插件读 `ctx.options` 短路，可作为"一个 bundle 整体开关"的自实现载体 |

---

## 1. 单插件启用/禁用 —— CAN（有代价）

- **schema 层面**：`plugin` 数组条目为 `string`（npm 包名/本地路径），或 `[string, object]` 元组（第二项为 options 对象）；**没有任何 `enabled` 布尔字段**。见 https://opencode.ai/config.json 中 `Config.plugin` 定义（item 为 `anyOf[string, [string, object]]`，`prefixItems` 第二项 `type: object`）。源码同源：`packages/core/src/v1/config/plugin.ts` 的 `Spec = Union[String, Tuple[String, Options]]`（`Options = Record<string, unknown>`），https://github.com/anomalyco/opencode/blob/dev/packages/core/src/v1/config/plugin.ts
- **没有 `.disabled` 命名约定**：插件目录扫描用 `Glob.scan("{plugin,plugins}/*.{ts,js}")`，只匹配 `.ts/.js`，不识别 `.disabled` 后缀。见 `packages/opencode/src/config/plugin.ts`，https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/config/plugin.ts
- **真实可用开关 = 删/移**：插件从 ①全局目录 `~/.config/opencode/plugins/`、②项目目录 `.opencode/plugins/`、③`plugin:` 数组 三处来源加载（https://opencode.ai/docs/plugins/#use-a-plugin）。去掉数组条目即禁用。

**新近进展 —— TUI 激活开关（尚未合并）**：PR https://github.com/anomalyco/opencode/pull/42410 「fix(tui): persist plugin activation toggles」引入 `/plugins` 对话框，可在 UI 内启用/禁用单个插件；禁用通过向配置的 `plugin` 列表**追加一条 `-plugin.<id>` 指令**实现并持久化。该 PR `state=open, merged_at=null`（2026-08-13 创建），**未合并到主线**。

> 结论：**can（有代价）**。今天唯一不删文件的自包含开关是 `plugin: ["spec",{enabled:false}]` 元组——但需插件自己读 options 并短路（hook 内 `if (options?.enabled === false) return {}`），这是**自实现约定**而非 opencode 原生语义（见 §7）。

---

## 2. 插件运行时注册 MCP —— NEEDS-PROBE（存在竞态）

**机制存在**：V1 插件 API 的 `config` hook 签名为 `config?: (input: Config) => Promise<void>`，其中 `Config` 含 `mcp` 字段。见 `packages/plugin/src/index.ts` 的 `Hooks.config` 类型，https://github.com/anomalyco/opencode/blob/dev/packages/plugin/src/index.ts 。插件加载器在加载完所有插件后**逐个**调用 `config?.(cfg)`，传入的是**同一个** Config service 实例（`packages/opencode/src/plugin/index.ts` 中 "Notify plugins of current config" 循环），https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/plugin/index.ts

**真实用户证据**：issue https://github.com/anomalyco/opencode/issues/41234 （1.18.15）展示在 `config` hook 中写 `config.mcp["dodo-knowledge"] = { type:"local", command:[...], enabled:true }` **"Works"**（MCP 服务器被注册）；同 issue 确认 `config()` 对 `skills.paths` 的改动**不可见**（引用 #20940，仍复现）。

**但存在顺序竞态（需探针）**：
- MCP 服务在 `InstanceState.make` 的 `lookup` 里**一次性**读取 `cfgSvc.get()` 并据此创建 client —— `packages/opencode/src/mcp/index.ts` 的 `MCP.state`：`const config = cfg.mcp ?? {}` 后 `Effect.forEach(Object.entries(config))` 逐个 `create(key, mcp)`，https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/mcp/index.ts
- Plugin 的 config hook 也在其 `InstanceState.make` 里执行（同一文件 plugin/index.ts）。
- 两者是 **sibling layer**：`MCP.node` deps 为 `[CrossSpawnSpawner, McpAuth, EventV2Bridge, Config, McpBrowser]`（不依赖 Plugin）；`Plugin.node` deps 为 `[EventV2Bridge, Config, RuntimeFlags]`（不依赖 MCP）。Effect 默认**并发**构建 sibling layer。
- MCP state 是 **lazy**（`ScopedCache` 按 directory 懒加载，`packages/opencode/src/effect/instance-state.ts`，https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/effect/instance-state.ts）。若 MCP state lookup 触发时 plugin config hook 已对同一 `cfg` 对象完成原地突变，则 MCP 会看到；否则不会。**谁先跑不保证**。

> 结论：**needs-probe**。机制存在且有人用过成功，但存在不确定的顺序竞态，且随版本（V1/V2）变化。必须本地验证。

### 探针协议（T0 先例：只读临时插件 + 一次用户重启）

1. 写临时插件 `~/.config/opencode/plugins/t0-mcp-probe.ts`：
   ```ts
   export const T0McpProbe = async () => ({
     config: async (cfg) => {
       cfg.mcp ??= {}
       cfg.mcp["t0-mcp-probe"] ??= { type: "local", command: ["node", "<probe-server.mjs>"], enabled: true }
     },
   })
   ```
2. `<probe-server.mjs>` 为最小 stdio MCP server：连接成功时在 stderr 打一行 `T0-MCP-CONNECTED`，并暴露一个工具 `t0_probe`。
3. **重启一次 opencode**（全新进程，配置文件一次性读取）。
4. 观察：①`T0-MCP-CONNECTED` 是否出现；②会话中是否能列出/调用 `t0_probe` 工具。
5. 判定：能连接 + 能调用 → **can**（config-hook MCP 注册可靠）；不能 → **cannot**（竞态落败或需换路径）；能连但调用异常 → 边界情况，记录。
6. 收尾：删除临时插件与探针 server（观察式、无残留）。

---

## 3. 分发形式 —— CAN

- **global**：`~/.config/opencode/plugins/*.ts|js`，启动自动加载（https://opencode.ai/docs/plugins/#from-local-files）。
- **project**：`.opencode/plugins/*.ts|js`，同文档。
- **npm**：`plugin: ["mnemoseed-opencode@x.y.z"]`，启动时 Bun 自动安装到 `~/.cache/opencode/node_modules/`（https://opencode.ai/docs/plugins/#from-npm）。
- **本地路径 / git URL**：issue #42724 的插件列表展示了绝对路径 `"D:/opencode/ponytail/.opencode/plugins/ponytail.mjs"` 与 git URL `"superpowers@git+https://..."` 均可用（https://github.com/anomalyco/opencode/issues/42724）。
- **可携带内容**（除 `.ts` 模块外）：
  - **依赖**：npm 插件包自带 `package.json` 声明，opencode 启动时 `bun install`（https://opencode.ai/docs/plugins/#dependencies）。
  - **config 默认值**：通过 `config` hook 注入（含 `mcp`、`skills.paths` 等）——这是"自带 config"的主要途径。注意 **MCP 可注入、skills 不行**（#41234 引用 #20940，`config()` 对 `skills.paths` 不可见；#33896 v2 `ctx.skill.transform` 不可发现）。
  - **V2 插件**：可用 `ctx.skill.transform` 提供 skills（https://github.com/anomalyco/opencode/blob/dev/packages/plugin/src/v2/effect/README.md），但 V2 目前**没有 MCP transform**（开放 feature request https://github.com/anomalyco/opencode/issues/39937）。
- **options 元组**：`plugin: [["mnemoseed-opencode", { ... }]]` 可给插件传 options，插件通过 `load.options`（V1，见 `packages/opencode/src/plugin/index.ts` 的 `applyPlugin` 传 `load.options`）或 `ctx.options`（V2，见 v2/effect/README）读取。**此元组无预定义语义**，完全由插件自己解释 —— 正好可作自开关载体。

---

## 4. 对比 Claude Code 的"一个 bundle = MCP + hooks" —— 差异

Claude Code 插件把 `hooks.json` 与 `.mcp.json` 放在同一插件目录，通过 enablement 整体开关。opencode **没有**这样的"插件单元内含 MCP 声明"原生模型：

- opencode 的 hook 是 TS 模块（代码），MCP 是 JSON 配置（`mcp:` key），**两套独立子系统**。
- MCP 的"整体开关"只有 `enabled` 字段 + `tools` glob（https://opencode.ai/docs/mcp-servers/#manage），插件 hook 没有对应开关。
- opencode 社区正朝**行业标准 Agent Plugins（agent-plugins.org）**靠拢 —— 一个 `plugin.json` 把 `skills/` + `mcp.json` 绑成单一可安装单元，且 VS Code/Copilot/Cursor/Codex 均已支持；opencode 侧仍是开放请求：https://github.com/anomalyco/opencode/issues/40993 与 https://github.com/anomalyco/opencode/issues/41561。**尚未落地**。
- 所以今天，opencode 实现"一个 bundle"只能靠"单个 npm 插件包 + 其 config hook 注入 `mcp`"（即 §2 机制），而非声明式 bundle。

---

## 5. 今日真实开关 UX 与重启

- 真实关法：①从 `plugin:` 数组删除条目；②把文件从 `~/.config/opencode/plugins/` 或 `.opencode/plugins/` 移走/改名成非 `.ts/.js` 后缀（如 `.ts.disabled`，因 glob 只匹配 `.ts/.js`，见 §1）。
- 重启要求：配置在**启动时一次性加载**（https://opencode.ai/docs/config/ 的 precedence 描述；插件目录文件"startup 自动加载"，https://opencode.ai/docs/plugins/#from-local-files 明确 "Files in these directories are automatically loaded at startup"）。**改完必须重启进程**；无运行时热开关（除 #42410 的 in-flight PR 与 V2 热重载行为，后者还有 bug，见 https://github.com/anomalyco/opencode/issues/42898）。

---

## 6. 逃生舱 —— 全局 kill-switch，非 per-plugin（确认）

源码 `packages/opencode/src/effect/runtime-flags.ts`（https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/effect/runtime-flags.ts）与 `packages/opencode/src/plugin/index.ts`：

- `OPENCODE_PURE` → `flags.pure`，令 `plugins = flags.pure ? [] : cfg.plugin_origins`，即**禁止所有外部插件**。
- `OPENCODE_DISABLE_DEFAULT_PLUGINS` → `flags.disableDefaultPlugins`，`internalPlugins(flags)` 不加载（内置 auth 插件）。
- `OPENCODE_DISABLE_EXTERNAL_SKILLS` → 禁用外部 skill 发现。
- **`OPENCODE_DISABLE_EXTERNAL_PLUGINS` 不存在**。`Flag`（V1 遗留）与 V2 runtime-flags 中均无此名；issue https://github.com/anomalyco/opencode/issues/36990 的 V1→V2 兼容性清单也只列 `OPENCODE_PURE` 与 `OPENCODE_DISABLE_DEFAULT_PLUGINS`。用户侧若想"全外部插件关掉"，真实对应是 **`OPENCODE_PURE`**（外部插件）+ **`OPENCODE_DISABLE_DEFAULT_PLUGINS`**（内置插件）的组合。
- 这些均为**全局** kill-switch，非 per-plugin。另见 #35859：这些开关对**编译进二进制的内置插件**（`INTERNAL_PLUGINS`）无效，后者无条件加载（https://github.com/anomalyco/opencode/issues/35859）。

---

## 7. 每-host 定制插件先例 —— CAN，且 options 元组是天然自开关载体

- opencode 文档无"daemon 接口打包"的专门模式，但 schema 的 `plugin: [string, object]` 元组（https://opencode.ai/config.json）加上 `load.options` / `ctx.options` 读取（源码 §3）提供了**官方的 options 传递通道**。
- 因此 hosts/ 目录的"每 host 一个定制插件"可做成 `plugin: [["mnemoseed-opencode", { enabled:false }]]`，插件入口读 `ctx.options.enabled` 短路：

  ```ts
  export const M = async (input, options) => {
    if (options?.enabled === false) return {}        // 自开关：整包关闭（hook 与 config 全不注册）
    return {
      config: async (cfg) => { cfg.mcp["mnemoseed"] = { type:"local", command:["mnemoseed-local","mcp"], enabled:true } },
      // ...capture / system.transform hooks
    }
  }
  ```

  这一个条目同时控制 MCP（config hook）与 hooks，达到"一个 bundle 整体开关"的效果。注意：若 `enabled:false` 返回 `{}`，config hook 也不跑，MCP 不会注册 —— 正好是"一个开关控制整体"。
- 局限：这仍是**自实现约定**，非 opencode 原生语义；TUI/文档不会把它当官方开关；且依赖 §2 的 config-hook MCP 竞态（需探针确认）才成立。

---

## 给 mnemoseed-local 的建议

- **短期可行方案**：把现有 host 插件改为 **npm 分发 + `config` hook 注入 `mcp.mnemoseed` + `[spec,{enabled}]` 元组自开关** → 单条目统一控制 MCP+hooks。**先跑 §2 探针确认 config-hook MCP 在当前版本可靠**。
- **长期**：跟踪 https://github.com/anomalyco/opencode/issues/39937 （V2 MCP transform）与 #40993 / #41561（Agent Plugins 标准），一旦落地即可用声明式 bundle 取代自实现。

---

## 附：未能核实/易变项清单（明确标注）
- **config-hook MCP 的先后顺序竞态**：源码层面确认是 sibling layer 并发 + MCP state 一次性读取，但具体版本下谁先跑未实证 —— **needs-probe**（见 §2 探针协议）。
- **PR #42410（TUI `/plugins` 激活开关）**：`merged_at=null`，2026-08 时点**未合并**；合入时间/语法可能变化（**易变**）。
- **Agent Plugins（agent-plugins.org）在 opencode 的落地**：仅开放 issue（#40993/#41561），无排期（**未落地**）。
- **`OPENCODE_DISABLE_EXTERNAL_PLUGINS`**：经源码与 issue 检索确认**不存在**（真实开关见 §6）。
