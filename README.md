# MnemoSeed Local

**A local, single-user AI memory layer for coding agents.**

MnemoSeed Local is the local-first edition of MnemoSeed: one machine,
CLI-first, no accounts, no cloud defaults, with a local web console served at
the daemon root. The daemon's HTTP surface is guarded by a local daemon
credential it issues itself: no accounts, no OAuth flow, no cloud round-trip. Memory lives in isolated per-profile namespaces: the
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
Capture scores importance without an LLM call, but the default embedder runs a
small local ONNX encoder, `bge_m3_onnx`, on each turn.

Everything is local-first: chunks are stored verbatim and history is
append-only. Reconciling a conflicting decision appends a new version whose
`prev_version_id` points back at the revision it replaces, instead of
overwriting it.

> **Scope.** Stored memory has no confidentiality guarantees here: no E2EE, no
> TEE, no encryption at rest. API keys are stored in the OS credential store
> when available, with a restricted local-file fallback.
> Memory plaintext stays on your disk, except for the dream pass, which by
> default runs against a local model but can be pointed at a remote endpoint.
> Multi-device sync, the hosted cloud daemon, and the full product vision live in
> the main repo, [MnemoSeed/mnemoseed](https://github.com/MnemoSeed/mnemoseed).

## Status

Phase A (MVP core) including its A3 packaging batch is fully shipped: config +
secrets + storage ports + embedded drivers (sqlite_meta / sqlite_graph /
lancedb_embedded / bge_m3_onnx / synthetic_embedder), schema, migrations,
capture/retrieve/dream/decay pipelines with a config-driven dream scheduler
(pool-score floor + idle window + 24h hard deadline), a credential-guarded
loopback daemon, the `mnemoseed-local` CLI, install orchestration, the OpenCode
host hook adapter, the MCP gateway, and the local web console.

Phase B has landed through main:

- **Session continuity** (T1/T2/T3): start-of-session replay injection, mid-turn
  focal auto-recall, and consumption-evidence reinforcement (cited memories are
  reinforced; injection alone is not).
- **Conversation visibility**: each action states itself with one fixed,
  non-directive line in the conversation (capture, replay, recall). Default
  `compact`; `[visibility] mode` selects `off` (silent) or `verbose` (adds
  count lines), hot-applied without a restart.
- **Four Tier-1 hosts**: the OpenCode plugin plus first-class hook adapters for
  Claude Code (B2.10), ZCode and Codex CLI, all capturing every turn.
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
- **Hosts are OpenCode, Claude Code, ZCode and Codex CLI.** Those are the
  integrations that ship. The Codex Desktop app-server mode is unverified, and
  Codex hooks need a one-time in-app trust review before they fire.
- **Windows starts at user logon, without crash supervision.** The installer registers one current-user Task Scheduler entry. It does not restart the daemon after a later crash.
- **Platform coverage is uneven.** Windows is the primary tested platform,
  Linux runs in CI, and macOS requires manual ollama setup.

Multi-session mutual awareness is in pre-PRD research; it is not a feature yet.

## Feature verification

- [Memory Atlas verification](features/atlas.md) — isolated browser proof and retained evidence.

## Install

One command, zero dependencies to prepare: the orchestrator detects and
installs ollama + uv when missing, leaves Ollama's native startup/tray
configuration unchanged, installs the `mnemoseed-local` CLI via `uv tool`,
runs `init` + `doctor`, and — only after your confirmation — pulls the dream
model. Idempotent; pass `--dry-run` / `-DryRun` to preview the plan with no
side effects, and `--yes` / `-Yes` to skip the model-pull prompt.

Windows (PowerShell 5.1+):

```powershell
irm https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.ps1 | iex
```

Linux/macOS (POSIX sh):

```sh
curl -fsSL https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.sh | sh
```

When the Windows one-liner is piped to `iex`, the installer first resolves its
self-contained helper module and the two Windows logon helper scripts from the
same `main` raw source used by `install.ps1`; the module is downloaded and
parsed before it is dot-sourced at script scope. Remote files are staged in a
unique per-process temporary directory (`%TEMP%\\mnemoseed-local-helpers-<pid>-<id>`),
then that directory is removed after migration and daemon registration, including
all failure exits. Any download or parse failure aborts setup; a local checkout
continues to use its sibling scripts directly when all three expected siblings
are present. During upgrade it removes only the exact
`\OllamaHeadlessServe` task created by the earlier MnemoSeed installer (exact
current-user AtLogOn shape, `ollama.exe serve` action, and original description).
A same-name foreign or drifted task is never removed and instead fails setup
with manual guidance. It never stops Ollama or changes its native tray/startup
configuration.

On Windows, the installer also registers the current-user `MnemoSeedLocalDaemon`
AtLogOn task. At the next user login it waits boundedly for Ollama's API, then
starts the MnemoSeed daemon even if Ollama is unavailable. Ollama remains
independently managed by its native tray/startup behavior. The task has no
periodic trigger or restart policy; it does not recover the daemon after a later
crash or logoff. OpenCode/MCP only connects to the daemon and does not own its
startup. `mnemoseed-local off` stops the daemon and persistently gates only the
MnemoSeed task; it never stops Ollama. While the configured local Ollama model
is unavailable, automatic dream work remains pending and resumes on a later
scheduler cycle without a daemon restart; capture, recall, and other memory APIs
remain available. Manual `dream once` reports `launched: false` with a reason
while the route is unavailable. Use `mnemoseed-local on` to re-enable and start
MnemoSeed immediately.

The installer's final setup step is host adapter wiring, and it is never
automatic. Pass `--hosts a,b,c` (install.sh) or `-Hosts a,b,c` (install.ps1)
with any of `claude_code`, `codex_cli`, `opencode`, `zcode` to wire exactly
those adapters, or answer the interactive offer shown in a terminal session:
the installer detects installed hosts by their CLI on PATH or their config
directory and offers them as a numbered menu. A non-interactive run without a
selection wires nothing — the daemon-only install stays fully functional and
the exact per-host commands are printed as next steps. Each wired host reports
its hooks, MCP and trust outcome on separate lines; manual trust steps (for
example approving the hooks inside Codex via `/hooks`) stay printed next steps
and are never automated.
Hook lifecycle:
`mnemoseed-local host {install|uninstall|status} --host opencode` — the `host`
verb group is the one entry for all four adapters. The original
`mnemoseed-local hook {install|uninstall|status|disable|enable} opencode`
remains working as the documented alias (it also serves claude_code).
The ZCode and Codex CLI adapters install through the `host` command group:
`mnemoseed-local host install --host zcode` registers four ZCode hook events
plus the loopback MCP entry in `~/.zcode/cli/config.json`, and
`mnemoseed-local host install --host codex_cli` writes the user-level Codex
`hooks.json` plus the stdio gateway entry in `config.toml`. The hooks file
keeps one top-level `hooks` object — event name to matcher groups, each group
holding `type: command` handlers; install adds one MnemoSeed group per
subscribed event and rewrites only its own marked handlers, so your own
groups, their matcher and metadata fields, and the rest of `config.toml` keep
their bytes.
`host status` reports the structure of that install as `not-installed`,
`partial` or `malformed`, and a complete install as *trust unverified*:
MnemoSeed never reads or writes a Codex trust record, and daemon reachability
is reported separately from the structure. Approving the hooks is your own
one-time step inside Codex — nothing fires as trusted before you run `/hooks`
and review them there. Lifecycle:
`mnemoseed-local host {install|uninstall|status} --host claude_code|codex_cli|opencode|zcode`.
The Codex adapter needs the optional `[codex]` extra
(`uv tool install --force '.[codex]'`), which provides `zstandard` for
compressed rollout transcripts.
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
into `~/.claude/settings.json` for `SessionStart`, `UserPromptSubmit`, `Stop`,
`PostToolUse`, `SubagentStop`, `PreCompact` and `SessionEnd` — your own hook
entries are never touched, and
`uninstall` removes only the marked ones (`disable`/`enable` flip a flag on our
entries). The hooks pipe Claude Code's stdin payloads through the hidden
`mnemoseed-local _hook-event --host claude_code` transformer into the daemon
(fire-and-forget with a ~2s timeout; stdout stays empty except a served
auto-recall block and the visibility signal line when `[visibility] mode` is
not `off`).
Settings files must stay plain JSON (Claude Code documents plain JSON) —
commented (JSONC) files are refused untouched with manual-edit guidance.
Lifecycle: `mnemoseed-local host {install|uninstall|status} --host claude_code`
(`hook {install|uninstall|status|disable|enable} claude_code` keeps working as
the documented alias, and is the only surface for its `disable`/`enable`
switch).

## Uninstall

`mnemoseed-local uninstall` removes the MnemoSeed-owned Windows logon task and
reports what remains, and it never deletes your memory data unless you ask:

- By default (no `--purge`) your data home is kept exactly as it is, including
  the project binding registry. Installed host adapters are detected and their
  exact per-host removal commands are printed as next steps, for example
  `mnemoseed-local host uninstall --host opencode` — one `host` verb covers
  all four adapters (`hook ... opencode` stays as the alias).
- `mnemoseed-local uninstall --purge` is the destructive path and it stays
  explicit plus confirmed: it lists the detected host adapters and every data
  file it will delete (the config home, the daemon credential and the project
  binding registry included), asks for confirmation, then removes the adapters
  before the data. `--yes` skips the confirmation. Adapter removal touches
  only MnemoSeed's own marked entries — your own hook groups, MCP servers and
  config files are preserved.
- A data home that is not the default one under your user profile (an isolated
  or `MNEMOSEED_LOCAL_HOME` override install) reports the detected adapters
  without removing them — their wiring may serve a different install — and
  prints the exact per-host commands instead.
- A partial failure never reads as completion: whatever MnemoSeed still owns
  is named with the exact command to finish by hand, and the exit code is
  non-zero. Nothing is claimed removed while something it owns remains.
- The CLI never uninstalls its own package and never claims that done: the
  final step is printed for you to run — `uv tool uninstall mnemoseed-local`.

## MCP gateway

The CLI ships a zero-config MCP stdio gateway (newline-delimited JSON-RPC,
daemon REST proxy with audit actor `mcp`). Register it in `opencode.json`:

```json
{"mcp": {"mnemoseed": {"type": "local", "command": ["mnemoseed-local", "mcp"]}}}
```

The running daemon also serves the same gateway over loopback HTTP on
**`POST /mcp`** (`application/json` only, stateless — no session id, no
server-side session table). The OpenCode plugin's config hook registers the
remote entry for you (`oauth: false` plus an explicit 30s timeout; an existing
factory stdio entry is shape-migrated, a manual entry wins untouched):

```json
{"mcp": {"mnemoseed": {
  "type": "remote",
  "url": "http://localhost:7788/mcp",
  "headers": {
    "Authorization": "Bearer <the daemon credential>",
    "X-MnemoSeed-Profile": "default",
    "X-MnemoSeed-Actor": "mcp"
  },
  "oauth": false,
  "enabled": true,
  "timeout": 30000
}}}
```

Any third-party local client speaks the same contract with plain HTTP: read
the credential from `~/.mnemoseed-local/secrets/daemon.credential.key`,
present it as the bearer header, carry `X-MnemoSeed-Profile`, and POST the
JSON-RPC body to `/mcp`.

Tools: `recall(query, top_k?)`, `remember(text, rules?)`, `supersede(superseded_node_id, successor_node_id)`,
`dream_once()`, `recent_sessions(resume_query, n_sessions?, n_per_session?, self_parent_id?)`,
`session_windows(n_sessions?)`. The handshake works even when the daemon is down; only tool calls report the
connectivity error.

Honest boundaries of the HTTP surface:

- `X-MnemoSeed-Profile` is per-request namespace selection, not
  authorization: the credential is the only boundary, exactly like the REST
  surface. A missing or blank profile header is a constant 400 (no silent
  `default`).
- `X-MnemoSeed-Actor` is self-declared attribution only. The daemon pins
  every self-call from this surface to the `mcp` audit actor, so a
  client-declared value never changes attribution.
- Non-POST methods on `/mcp` are 405; `/mcp/` is not the route (the console
  mount answers it); the surface is not in the console's open document set.
- One tool call is two authenticated requests (the client leg plus the
  daemon's own self-call leg); the since-boot `mcp` counter moves by two
  when the client leg also declares `X-MnemoSeed-Actor: mcp`, as the
  first-party entry does.
- The remote entry reads the credential once at config-hook time; a rotated
  credential leaves the entry stale until the hook runs again (restart the
  host after `mnemoseed-local credential rotate`).
- `"oauth": false` keeps a first-party entry from opening a browser
  OAuth/DCR loop when the daemon answers 401.

## Agent skill

The repo ships an agent skill at `skills/mnemoseed/SKILL.md` that teaches a
coding agent when and how to call the MCP tools above: proactive-recall
etiquette, the scoped `recent_sessions` resume, and pinned-vs-captured
discrimination. Install it manually by copying the `skills/mnemoseed/`
directory into your host's skills directory (for example `~/.claude/skills/`
for Claude Code or `.opencode/skills/` for OpenCode) and restarting the host.
This `skills/` tree is the shipped product artifact; `.opencode/skills/` is
this repository's own in-repo automation and does not ship.

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

One-time tips also welcome via Polar (below).

### Channels

[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub-%23EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/MnemoSeed)
[![Polar](https://img.shields.io/badge/Donate-Polar-%230066FF?logo=polar&logoColor=white)](https://buy.polar.sh/polar_cl_KoNqAsMI8IQGy5RRE8MLbX7I5TBGUMh9Kn8CC35YfP7)

> **Transparency promise**: monthly dream-cost figures, evaluation data, and sponsor counts/amounts are published in the Backer brief and quarterly sync. No feature gating, no license upsell, no roadmap over-promising.

## Evaluation (maintainers)

The eval harness lives at `python -m mnemoseed_local.eval`, run from a source
checkout:

- `canary`: self-checks the harness with stub seats.
- `matrix`: runs the material catalog.
- `rescore`: re-judges a v1.1 report offline without GPU.
- `recall`: T4b live calibration coordinate-descent (locked config: focal
  floor 0.5, budget 2400 chars).

General contact: [hello@mnemoseed.my](mailto:hello@mnemoseed.my).

## License

MIT.

## Daemon credential

The daemon's HTTP surface refuses every caller that cannot present the
**daemon credential** it issues for itself. There is no account, no OAuth flow,
and no cloud round-trip: the daemon writes one high-entropy value to
`~/.mnemoseed-local/secrets/daemon.credential.key` on its first boot (restarting
never re-signs it), the CLI / MCP gateway / host hooks / console read it from
there, and it rides the wire as a bearer header.

- `mnemoseed-local credential rotate` writes a fresh credential. The daemon
  resolves it per request, so the new value is live at once; a request that raced
  the write is refused once. Every client holding the previous value is refused
  until it restarts.
- `mnemoseed-local uninstall --purge` deletes the config home, the credential
  included — an implicit rotation path: the next `up` issues a new one.
- The liveness probes (`/healthz`, `/health`) stay open so the installer, the
  watchdog, and the CLI can still tell "daemon is up" from "daemon is down".
  Everything else — `/api/v1/*`, `/ingest`, `/session/*`, `/memory/*`,
  `/daemon/shutdown`, `/mcp` — is closed to a caller without the
  credential. The console authenticates with a same-origin `HttpOnly` cookie
  granted only on loopback; the value never enters the page.
- A refused credential is reported as an authentication failure and names the
  two causes that actually happen: the client and the daemon resolving different
  config homes (`MNEMOSEED_LOCAL_HOME`), or a rotated credential.

What this does **not** defend against: another process running as the same user
can read the credential file and impersonate a client; root/admin can read
anything; the stored data itself (meta.db / chunks.lance) stays plaintext; and
binding the daemon to a wildcard address without TLS would put the credential on
the LAN in the clear (loopback-only is the supported configuration).

## Daemon lifecycle

On Windows, the daemon starts once at user logon through the
`MnemoSeedLocalDaemon` current-user task. It does not auto-restart after a later
crash, and no persistent supervisor or periodic polling task is installed. If
the daemon dies, start it again yourself:

```sh
mnemoseed-local up
```

`up` respects the `daemon.off` marker, so it will not revive a service you
deliberately disabled with `mnemoseed-local off`; use `mnemoseed-local on` to
re-enable the service and start the daemon. `off` writes the marker before its
short cross-process coordination window; if the listener is still possibly
alive at timeout, it returns non-zero and leaves the marker in place rather than
claiming the daemon stopped. Uninstall removes the MnemoSeed-owned login task
by default, but refuses an unknown or foreign same-name task.

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

Because nothing restarts the daemon after logon startup, do not build a restart
wrapper around it. An unattended restart loop masks crashes and can fight the
`daemon.off` marker; run `up` explicitly after checking `status` / `doctor`.
