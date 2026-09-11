# MnemoSeed Local

**A local, single-user AI memory layer for coding agents.**

MnemoSeed Local is the local-first edition of MnemoSeed: one machine,
CLI-first, no accounts, no cloud defaults, with a local web console served at
the daemon root. Memory lives in isolated per-profile namespaces: the
conventional `default` namespace works out of the box, and extra profiles can
be registered for other agents
(`mnemoseed-local profile {create|list|archive|unarchive}`, bound via the
`profiles.agent_bindings` config key). The core loop is capture -> dream ->
decay -> retrieve, with dream inference running against a local model (ollama
by default, with an OpenAI-compatible fallback driver). Both automations ship
ON by default:
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
hook adapter, the MCP gateway, and the local web console.

Phase B has landed through main:

- **Session continuity** (T1/T2/T3): start-of-session replay injection, mid-turn
  focal auto-recall, and consumption-evidence reinforcement (cited memories are
  reinforced; injection alone is not).
- **Two Tier-1 hosts**: the OpenCode plugin plus a first-class Claude Code hook
  adapter (B2.10), both capturing every turn.
- **origin_agent attribution** (B2.9): memories record which in-host agent
  produced them (`ingest -> turn -> stamp -> recall/recent`), as inert
  provenance that never affects scoring or ranking.
- **Score-pool split** (B2.11): `balance` is a true pending gauge while
  `filed_points_total` is the lifetime ledger, so "how far to the next dream"
  is honest across restarts.
- **Retention redesign**: one retention dynamic with cue-based rescue and
  fade-to-index instead of a promise of immortality.
- **Provenance trust surface**: recall/Atlas expose pinned-vs-captured signals
  (`provenance_source` / `explicit_pin` / `needs_reconcile`), and injection
  marks pinned lines.
- **Observability beacons** (B2.12): MCP handshake beacon, doctor warnings for
  registered-but-never-connected gateways, and an `/api/v1/observability`
  snapshot.
- **Error-event ledger** (B2.13 E0/E1): append-only `error_events` table and
  query primitives — plumbing only, no detectors yet.
- **Daemon reliability**: TCP-probe watchdog with forensic dump, durable
  `daemon.log`, persistent on/off, and a socket-alive veto against spurious
  watchdog kills.

The eval harness ships with T4b live calibration, thresholds locked at
focal_floor=0.5 / budget_chars=2400 (accepted 2026-08-23).

### What it does NOT do yet

- **Dream emits structured facts, not typed lessons.** Consolidation produces
  `prefers` / `has_habit` / `decided` / `believes` triples; it does not yet
  learn from mistakes or produce the typed lesson artifacts sketched for later
  experience-learning phases. It is not claimed to.
- **Experience learning is under construction and off.** The error-event ledger
  and scoping work (B2.13 E0/E1) is scaffolding; no detector decides anything,
  and none of it is on by default.
- **Hosts are OpenCode and Claude Code only.** Those are the two integrations
  that ship.
- **No auto-restart.** Nothing relaunches the daemon for you; see below.
- **Platform coverage is uneven.** Windows is the primary tested platform,
  Linux runs in CI, and macOS requires manual ollama setup.

Multi-session mutual awareness is in pre-PRD research; it is not a feature yet.

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

> **Install availability.** The one-command installer pins `mnemoseed-local`
> from the default package index, which must first publish the package there.
> Publishing is planned but not yet done: `mnemoseed-local` is not on PyPI
> today and origin has no release tag, so the installer fails cleanly at its
> package-install step until that lands. From a source checkout, install the
> current work and the CLI without a package index:
> `uv tool install --force .` (then `mnemoseed-local up`). The one-command
> installers pick this up automatically once the release is published.

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

Your sponsorship covers domain/infra for cloud TEE validation and funds the daily burn of building in public. The shipped dream route is a local small model via ollama; pointing it at a larger cloud model is optional user configuration, not a default.

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

## Daemon lifecycle

The daemon does not auto-restart. There is no supervisor process, scheduled
task, or restart chain — MnemoSeed Local intentionally ships none. If the
daemon dies, start it again yourself:

```sh
mnemoseed-local up
```

`up` respects the `daemon.off` marker, so it will not revive a service you
deliberately disabled with `mnemoseed-local off`; use `mnemoseed-local on` to
re-enable the service and start the daemon.

Health is observable rather than silently "handled":

- `mnemoseed-local status` reports whether the daemon is reachable and the
  resolved config.
- `mnemoseed-local doctor` runs the self-check checklist (stores, migrations,
  gate, host hook, gateway connectivity).
- The daemon runs an in-process watchdog: it probes the served listener, and
  when the listener is lost beyond a grace window it writes its last words and a
  forensic thread dump to `daemon.log` (under `MNEMOSEED_LOCAL_HOME`) before
  exiting. That leaves evidence for diagnosis — it does not bring the daemon
  back.

Because nothing restarts the daemon, do not build a restart wrapper around it.
An unattended restart loop masks crashes and can fight the `daemon.off` marker;
run `up` explicitly after checking `status` / `doctor`.
