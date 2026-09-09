# MnemoSeed Local

**A local, single-user AI memory layer for coding agents.**

MnemoSeed Local is the local-first edition of MnemoSeed: one machine,
CLI-first, no accounts, no console, no cloud defaults. Memory lives in
isolated per-profile namespaces: the conventional `default` namespace works
out of the box, and extra profiles can be registered for other agents
(`mnemoseed-local profile {create|list|archive|unarchive}`, bound via the
`profiles.agent_bindings` config key). The core loop is capture -> dream ->
decay -> retrieve, with dream
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
Profile namespaces are managed on the running daemon:
`mnemoseed-local profile {create|list|archive|unarchive}` (archiving never
deletes data and does not unbind agents).

### Claude Code

`mnemoseed-local hook install claude_code` merges marked MnemoSeed hook entries
into `~/.claude/settings.json` for `UserPromptSubmit`, `Stop`, `PostToolUse`,
`PreCompact` and `SessionEnd` — your own hook entries are never touched, and
`uninstall` removes only the marked ones (`disable`/`enable` flip a flag on our
entries). The hooks pipe Claude Code's stdin payloads through the hidden
`mnemoseed-local _hook-event --host claude_code` transformer into the daemon
(fire-and-forget with a ~2s timeout; zero stdout, so prompts stay clean).
Settings files must stay plain JSON (Claude Code documents plain JSON) —
commented (JSONC) files are refused untouched with manual-edit guidance.
Lifecycle: `mnemoseed-local hook {install|uninstall|status|disable|enable}
claude_code`.

## MCP gateway

The CLI ships a zero-config MCP stdio gateway (newline-delimited JSON-RPC,
daemon REST proxy with audit actor `mcp`). Register it in `opencode.json`:

```json
{"mcp": {"mnemoseed": {"type": "local", "command": ["mnemoseed-local", "mcp"]}}}
```

Tools: `recall(query, top_k?)`, `remember(text, rules?)`, `supersede(superseded_node_id, successor_node_id)`,
`dream_once()`, `recent_sessions(n_sessions?, n_per_session?)`, `session_windows(n_sessions?)`. The
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

> **Transparency promise**: monthly dream-cost figures, evaluation data, and sponsor counts/amounts are published in the Backer brief and quarterly sync. No feature gating, no license upsell, no roadmap over-promising.

[![Thanks.dev](https://img.shields.io/badge/Support-Thanks.dev-%23FF6B35)](https://thanks.dev/github/MnemoSeed/mnemoseed-local)

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
`daemon.log` (under `MNEMOSEED_LOCAL_HOME`) and exits with code 1. Relaunching
stays user-side.

Windows Task Scheduler records a non-zero action result but does not reliably
treat it as a launch failure, so `RestartCount` alone does not relaunch a daemon
that exits after it started. Use a user-side wrapper that waits for `up`, checks
the explicit `daemon.off` marker, and bounds rapid failures to three restarts:

```powershell
$configHome = if ($env:MNEMOSEED_LOCAL_HOME) {
  $env:MNEMOSEED_LOCAL_HOME
} else {
  Join-Path $env:USERPROFILE ".mnemoseed-local"
}
New-Item -ItemType Directory -Force -Path $configHome | Out-Null
$supervisor = Join-Path $configHome "supervise.ps1"
@'
param(
    [ValidateRange(0, 100)]
    [int]$MaxRestarts = 3,

    [ValidateRange(0, 3600)]
    [int]$RestartDelaySeconds = 60,

    [ValidateRange(1, 86400)]
    [int]$StableRunSeconds = 600,

    [string]$ConfigHome = "",

    [string]$ShimPath = "",

    [string[]]$ShimArguments = @("up")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$configHome = if (-not [string]::IsNullOrWhiteSpace($ConfigHome)) {
    $ConfigHome
} elseif ([string]::IsNullOrWhiteSpace($env:MNEMOSEED_LOCAL_HOME)) {
    Join-Path $env:USERPROFILE ".mnemoseed-local"
} else {
    $env:MNEMOSEED_LOCAL_HOME
}
$disabledMarker = Join-Path $configHome "daemon.off"
$logPath = Join-Path $configHome "supervisor.log"
$shim = if ([string]::IsNullOrWhiteSpace($ShimPath)) {
    Join-Path $env:USERPROFILE ".local\bin\mnemoseed-local.exe"
} else {
    $ShimPath
}

function Write-SupervisorLog {
    param([string]$Message)

    try {
        if (-not (Test-Path -LiteralPath $configHome)) {
            New-Item -ItemType Directory -Path $configHome -Force | Out-Null
        }
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $Message" -Encoding utf8
    } catch {
        # Logging must never block daemon recovery.
    }
}

$restartCount = 0
while ($true) {
    if (Test-Path -LiteralPath $disabledMarker) {
        Write-SupervisorLog "disabled marker present; supervisor exiting"
        exit 0
    }
    if (-not (Test-Path -LiteralPath $shim -PathType Leaf)) {
        Write-SupervisorLog "mnemoseed-local shim missing; supervisor exiting"
        exit 127
    }

    $startedAt = Get-Date
    $exitCode = 1
    Write-SupervisorLog "starting daemon (restart_count=$restartCount)"
    try {
        $process = Start-Process -FilePath $shim -ArgumentList $ShimArguments -PassThru -Wait -WindowStyle Hidden
        $exitCode = $process.ExitCode
    } catch {
        Write-SupervisorLog "daemon launch failed ($($_.Exception.GetType().Name))"
    }

    $runtimeSeconds = ((Get-Date) - $startedAt).TotalSeconds
    Write-SupervisorLog "daemon exited (code=$exitCode runtime_seconds=$([math]::Round($runtimeSeconds, 1)))"

    if (Test-Path -LiteralPath $disabledMarker) {
        Write-SupervisorLog "disabled marker present after exit; supervisor exiting"
        exit 0
    }
    if ($exitCode -eq 0) {
        Write-SupervisorLog "daemon exited cleanly; supervisor exiting"
        exit 0
    }
    if ($runtimeSeconds -ge $StableRunSeconds) {
        $restartCount = 0
        Write-SupervisorLog "stable-run threshold reached; restart budget reset"
    }
    if ($restartCount -ge $MaxRestarts) {
        Write-SupervisorLog "restart budget exhausted; supervisor exiting"
        exit $exitCode
    }

    $restartCount += 1
    Write-SupervisorLog "retrying in $RestartDelaySeconds seconds (restart_count=$restartCount)"
    Start-Sleep -Seconds $RestartDelaySeconds
}
'@ | Set-Content -LiteralPath $supervisor -Encoding utf8

$pwsh = (Get-Command pwsh).Source
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pwsh `
  -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$supervisor`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$settings = New-ScheduledTaskSettingsSet `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -MultipleInstances IgnoreNew -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $identity `
  -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "MnemoSeedLocalDaemon" `
  -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Force
```

Honest caveats:

- Use **AtLogOn + one waiting wrapper, never a periodic trigger**. The task stays
  running with the daemon, and `IgnoreNew` rejects duplicate task instances.
- **`-ExecutionTimeLimit` must be 0 (unlimited)** — the Task Scheduler default
  3-day cap would hard-kill a healthy long-running daemon.
- The wrapper launches **`up`, never `on`**. `up` respects `daemon.off`; `on`
  clears it and would revive a service the user deliberately disabled.
- Three rapid restarts are allowed at 60-second intervals. A run lasting at
  least ten minutes resets that budget, so sparse failures remain recoverable
  without creating an infinite crash loop.

The watchdog releases the port before exiting, then the waiting wrapper delays
for 60 seconds and relaunches. With the service off (`mnemoseed-local off`), the
wrapper observes `daemon.off` and exits 0 without launching or retrying `up`.
