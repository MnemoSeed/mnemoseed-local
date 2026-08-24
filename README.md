# MnemoSeed Local

**A local, single-user AI memory layer for coding agents.**

MnemoSeed Local is the local-first edition of MnemoSeed: one profile
(`default`), one machine, CLI-first. No accounts, no console, no cloud
defaults. The core loop is capture -> dream -> decay -> retrieve, with dream
inference running against a local model (ollama by default, with an
OpenAI-compatible fallback driver). Both automations ship ON by default:
dream consolidation fires on its own under its schedule triggers (`--once`
is the manual fallback), and the focal recall scan runs on every prompt —
each rolls back with a single config switch
(`dream.auto_trigger = false` / `capture.auto_recall = false`).

Everything is local-first: chunks are stored verbatim, history is
append-only, and memory plaintext never leaves the machine.

## Status

Phase A (MVP core) including its A3 packaging batch is fully shipped: config +
secrets + storage ports + embedded drivers (sqlite_meta / sqlite_graph /
lancedb_embedded / bge_m3_onnx / synthetic_embedder), schema, migrations,
capture/retrieve/dream/decay pipelines with a config-driven dream scheduler
(pool-score floor + idle window + 24h hard deadline), no-accounts loopback
daemon, the `mnemoseed-local` CLI, install orchestration, the OpenCode host
hook adapter, and the MCP gateway.

Phase B is well underway and landed through main: dream ensemble verification
(B1); cross-session time awareness (`session_windows`), an OpenCode capture
hook that ingests every turn with consumption-evidence reinforcement, crash
durability, daemon reliability (TCP-probe watchdog, durable `daemon.log`),
persistent daemon on/off, an agent recall redesign, and plugin bundling (B2.x);
plus the eval harness and T4b live calibration with thresholds locked at
focal_floor=0.5 / budget_chars=2400 (accepted 2026-08-23). Multi-session
mutual awareness is in pre-PRD research; it is not a feature yet.

## Install

One command, zero dependencies to prepare: the orchestrator detects and
installs ollama + uv when missing, registers ollama as a headless background
server (on Windows a logon scheduled task runs `ollama serve`; Linux gets the
systemd service from ollama's own installer; the stock tray GUI stays a
user-owned, optional surface — the installer only hints at it, never
relocates another product's autostart), installs the `mnemoseed-local` CLI
via `uv tool`, runs `init` + `doctor`, and — only after your confirmation —
pulls the dream model. Idempotent; pass `--dry-run` / `-DryRun` to preview
the plan with no side effects, and `--yes` / `-Yes` to skip the model-pull
prompt.

Windows (PowerShell 5.1+):

```powershell
irm https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.ps1 | iex
```

Linux/macOS (POSIX sh):

```sh
curl -fsSL https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.sh | sh
```

Afterwards: `mnemoseed-local up` starts the daemon. The installer's final setup
step is `mnemoseed-local hook install opencode`, so the OpenCode hook is installed
automatically (restart OpenCode to load it). `mnemoseed-local off` stops the
daemon and disables the memory service persistently; `mnemoseed-local on`
re-enables it and starts the daemon again. Hook lifecycle:
`mnemoseed-local hook {install|uninstall|status|disable|enable} opencode`.

## MCP gateway

The CLI ships a zero-config MCP stdio gateway (newline-delimited JSON-RPC,
daemon REST proxy with audit actor `mcp`). Register it in `opencode.json`:

```json
{"mcp": {"mnemoseed": {"type": "local", "command": ["mnemoseed-local", "mcp"]}}}
```

Tools: `recall(query, top_k?)`, `remember(text, rules?)`, `dream_once()`,
`recent_sessions(n_sessions?, n_per_session?)`, `session_windows(n_sessions?)`. The
handshake works even when the daemon is down; only tool calls report the
connectivity error.

## Development

Test-driven, with an adversarial verifier on every task: failing tests first.
Gates: `uv run pytest -q`, `ruff check`, `ruff format --check`, `mypy src`.

## Sponsor MnemoSeed

**One memory under all your coding agents.**  
MnemoSeed Local is an MIT, single-machine MVP that proves the core pipeline: capture → dream → decay → retrieve. Every prompt and response is stored verbatim, scored for importance, distilled by an offline dream pass, and forgotten on an Ebbinghaus curve — so the next session starts with what matters, not a blank slate.

Your sponsorship keeps the dream model running (Opus-class inference), covers domain/infra for cloud TEE validation, and funds the daily burn of building in public.

### Tiers

| Tier | Monthly | What you get |
|------|---------|--------------|
| **Supporter** | $5 | Name in README Contributors |
| **Backer** | $25 | Priority issue response + monthly dream-cost/eval brief (real numbers) |
| **Sponsor** | $100 | README logo + quarterly roadmap sync (no feature promises) |
| **Enterprise** | $500 | Direct support channel + private deployment consult (pre-warms Cloud) |

One-time tips also welcome via Polar (below) or Buy Me a Coffee.

### Channels

[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub-%23EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/MnemoSeed)
[![Polar](https://img.shields.io/badge/Donate-Polar-%230066FF?logo=polar&logoColor=white)](https://buy.polar.sh/polar_cl_KoNqAsMI8IQGy5RRE8MLbX7I5TBGUMh9Kn8CC35YfP7)
[![Buy Me a Coffee](https://img.shields.io/badge/Tip-Buy%20Me%20a%20Coffee-%23FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/mnemoseed)

> **透明度承诺**：月度梦境成本、评估数据、赞助人数/金额将在 Backer 简报与季度 sync 中公开。不锁功能、不卖 License、不透支 Roadmap。

[![Thanks.dev](https://thanks.dev/badge.svg?package=github%3AMnemoSeed%2Fmnemoseed-local)](https://thanks.dev/github/MnemoSeed/mnemoseed-local)

## Evaluation (maintainers)

The eval harness lives at `python -m mnemoseed_local.eval`, run from a source
checkout:

- `canary`: self-checks the harness with stub seats.
- `matrix`: runs the material catalog.
- `rescore`: re-judges a v1.1 report offline without GPU.
- `recall`: T4b live calibration coordinate-descent (locked config: focal
  floor 0.5, budget 2400 chars).

## License

MIT.

## Daemon supervision (optional)

The daemon supervises itself: a watchdog thread probes the served listener
and, when the listener is lost beyond a grace window, writes its last words to
`daemon.log` (under `MNEMOSEED_LOCAL_HOME`) and exits with code 1; relaunching
stays user-side. For automatic relaunch, register a logon scheduled task
(mirrors the installer's ollama precedent):

```powershell
$shim = Join-Path $env:USERPROFILE ".local\bin\mnemoseed-local.exe"
$action   = New-ScheduledTaskAction -Execute $shim -Argument "up"
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "MnemoSeedLocalDaemon" `
  -Action $action -Trigger $trigger -Settings $settings -Force
```

Honest caveats:

- Use **AtLogOn + RestartCount, never a periodic trigger** — a "run every X
  minutes" trigger would start a second `up` next to a healthy daemon; restart
  on failure only fires when the action is not running and exited non-zero.
- **`-ExecutionTimeLimit` must be 0 (unlimited)** — the Task Scheduler default
  3-day cap would hard-kill a healthy long-running daemon.
- The watchdog's exit code 1 is what makes RestartCount effective — the
  scheduler only sees "not running + non-zero exit" as a failure, and the
  watchdog converts a hung daemon into exactly that.
- The watcher checks the **LISTEN state, not /healthz** — a hung-but-bound
  daemon would make /healthz time out and spuriously relaunch.

Or run the watcher one-liner when you do not want a scheduled task:

```powershell
while ($true) {
  if (-not (Get-NetTCPConnection -LocalPort 7788 -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -WindowStyle Hidden mnemoseed-local -ArgumentList 'up'
  }
  Start-Sleep -Seconds 15
}
```

The watchdog releases the port within its grace window, so the next loop
naturally relaunches; a bind race with a still-releasing port is absorbed by
the new `up` failing fast with a non-zero exit (the uncaught bind error
propagates, not a custom code).

With the service off (`mnemoseed-local off`), `up` exits 1 immediately (a
harmless no-op) — remove the scheduled task / watcher, or accept the no-op.
