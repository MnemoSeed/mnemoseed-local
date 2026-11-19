# MnemoSeed Local guide

[Back to the overview](../README.md) · [Website](https://mnemoseed.tech) ·
[Report an issue](https://github.com/MnemoSeed/mnemoseed-local/issues)

This guide covers the current single-user edition. Start with source installation,
then connect a host and verify capture and recall with a real session.

- [Install from source](#install-from-source)
- [Host setup](#host-setup)
- [Dream model setup](#dream-model-setup)
- [Uninstall](#uninstall)
- [Memory behavior and status](#status)
- [MCP gateway](#mcp-gateway)
- [Surfaces without a capture hook](#surfaces-without-a-capture-hook)
- [Daemon credential](#daemon-credential)
- [Daemon lifecycle](#daemon-lifecycle)
- [Development](#development) and [evaluation](#evaluation-maintainers)

## Install from source

The `mnemoseed-local` package is not yet published on PyPI. Use a source checkout;
the package-index installers described below are currently unavailable.

Prepare [Git](https://git-scm.com/downloads),
[uv](https://docs.astral.sh/uv/getting-started/installation/), and Python 3.14 or
newer. The command below asks uv to use Python 3.14; uv can provision it when
needed. The `[codex]` extra includes `zstandard`, which is required to read
compressed Codex transcripts.

```sh
git clone https://github.com/MnemoSeed/mnemoseed-local.git
cd mnemoseed-local
uv tool install --python 3.14 --force --with zstandard .
mnemoseed-local init --mode capture-recall
mnemoseed-local doctor
```

For a local-path install, `uv tool install --with zstandard .` is the working
form for that extra. `uv tool install --from <path> "mnemoseed-local[codex]"`
fails because uv rejects a package requirement provided together with `--from`.
Without the extra, `mnemoseed-local host install --host codex_cli` refuses with
the working command and `mnemoseed-local host status --host codex_cli` reports
`not-installed (missing extra)`.

If the installed CLI is not on PATH, follow uv's tool PATH guidance before
continuing. User configuration and memory live in `~/.mnemoseed-local`, separate
from the source checkout; `MNEMOSEED_LOCAL_HOME` overrides this location.

A fresh `capture-recall` setup enables capture auto-recall and keeps the legacy
automatic dream pass off (`capture.auto_recall = true`,
`dream.auto_trigger = false`). It does not require Ollama. An existing config is
not silently rewritten; inspect the CLI's guidance before changing its setup
mode. Mode-less `init` retains the legacy configuration defaults, so the explicit
mode matters here.

Capture uses a small local ONNX encoder, `bge_m3_onnx`, on each turn. Its model
assets must be available; initial provisioning can require a model download.
Resolve missing embedder/model assets and other readiness failures reported by
`doctor` before testing a host. Installing the CLI does not itself install
Ollama, pull its dream model, register login startup, or connect a host.

Start the daemon in a separate terminal:

```sh
mnemoseed-local up
```

Then run `mnemoseed-local status` and `mnemoseed-local doctor`, and continue with
[host setup](#host-setup). This source route is not a claim of a verified fresh
install on every platform.

## Host setup

Keep the daemon running. Choose your host below and restart that host after
registration. One command group covers all four adapters:
`mnemoseed-local host {install|uninstall|status} --host <host>`, where `<host>`
is `claude_code`, `codex_cli`, `opencode`, or `zcode`. Install only the hosts
you choose. The original `hook` commands for OpenCode and Claude Code remain
supported aliases; their `disable`/`enable` switches stay on the `hook` surface.

Each wired host reports hooks, MCP, and trust outcomes separately. Manual
trust steps remain printed next steps and are never automated. A config entry
or successful MCP handshake alone does not prove that prompts are captured or
memory can be recalled.

### OpenCode

```sh
mnemoseed-local host install --host opencode
mnemoseed-local host status --host opencode
```

Restart OpenCode to load the plugin. Lifecycle:
`mnemoseed-local host {install|uninstall|status} --host opencode`.
The original `mnemoseed-local hook {install|uninstall|status|disable|enable} opencode`
remains a supported alias.
The plugin's config hook registers the loopback MCP entry described in
[MCP gateway](#mcp-gateway). OpenCode connects to the daemon; it does not own
daemon startup.

### Claude Code

```sh
mnemoseed-local host install --host claude_code
mnemoseed-local host status --host claude_code
```

`mnemoseed-local host install --host claude_code` merges marked MnemoSeed hook entries
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
Lifecycle: `mnemoseed-local host {install|uninstall|status} --host claude_code`.
The original `mnemoseed-local hook {install|uninstall|status|disable|enable} claude_code`
remains a supported alias and is the only surface for `disable`/`enable`.

### ZCode

```sh
mnemoseed-local host install --host zcode
mnemoseed-local host status --host zcode
```

Registration adds four ZCode hook events and the loopback MCP entry in
`~/.zcode/cli/config.json`. Lifecycle:
`mnemoseed-local host {install|uninstall|status} --host zcode`.

### Codex CLI

```sh
mnemoseed-local host install --host codex_cli
mnemoseed-local host status --host codex_cli
```

The source install above includes `zstandard`. Without it, registration refuses
before writing anything and status reports `not-installed (missing extra)`,
because a Codex rollout is compressed and cannot be read. Registration
writes user-level `hooks.json` and the stdio gateway entry in `config.toml`.
The hooks file keeps one top-level `hooks` object: event names map to matcher
groups, each holding `type: command` handlers. Installation adds one MnemoSeed
group per subscribed event and rewrites only its own marked handlers; unrelated
groups, matcher/metadata fields, and the rest of `config.toml` retain their bytes.
Lifecycle: `mnemoseed-local host {install|uninstall|status} --host codex_cli`.

`host status` distinguishes `not-installed`, `partial`, and `malformed`
structures; a complete install is still *trust unverified*. Daemon reachability
is reported separately. MnemoSeed never reads or writes a Codex trust record.
Inside Codex CLI, run `/hooks` and review the installed hooks: the one-time trust
step belongs to you. Codex Desktop app-server mode remains unverified.

### Verify a real session

1. Check `mnemoseed-local status` and `mnemoseed-local doctor` after restarting
   your host; resolve configuration, model, or gateway failures.
2. Send a harmless prompt with a distinctive phrase in that host. Use the local
   web console or MCP `recent_sessions` to confirm the captured turn appears.
3. Ask the host to recall that phrase through MCP `recall` and check the result.
   Confirm capture and recall separately; a registration or handshake is not
   end-to-end evidence.

The local web console is served at the daemon root (default
`http://localhost:7788/`). If nothing is captured, inspect host status, the
Codex trust step where applicable, config-home consistency, and daemon logs.

## Import historical sessions

`mnemoseed-local import --host codex_cli` backfills rollouts that were created
before hook capture was installed. One rollout file is one import session; the
run uuid in its filename is the session id, while the conversation id in
`session_meta` is kept only as lineage metadata.

```
mnemoseed-local import --host codex_cli [--source <dir>] [--profile <id>]
                       [--limit N] [--dry-run] [--json] [--yes]
```

`--source` defaults to `~/.codex/sessions`; `--profile` defaults to `default`.
`--dry-run` enumerates and counts turns without making any request. A long
import asks for confirmation unless `--yes` is given (and refuses to run
unattended without it). Progress lines go to stderr, the final report to
stdout, and `--json` emits that report as JSON. The report reconciles
`sessions_scanned`, `sessions_imported`, `skipped` by reason, `turns_ingested`,
`turns_deduped`, `new_chunk`, `reinforced`, `needs_reconcile`,
`pool_points_suppressed`, and `elapsed_s`.

Resume is client-side: `~/.mnemoseed-local/import-ledger-<host>.json` records a
rollout only after `/session/end` returned 200, together with the ordinal it
settled through. Re-running after an interruption, a Ctrl-C, or a daemon
restart continues past that ordinal; a completed rollout is reported as
`already-imported` without new requests. Sessions the daemon currently holds
in its capture buffer are reported as `skipped-active` and are never settled.
That check is best-effort: a live session that has not closed a turn yet is not
in the buffer and cannot be detected.

Historical turns are stored verbatim. Codex records can contain plaintext
secrets and injected host scaffolding such as `<environment_context>`; import
does no redaction, so that content enters the profile as-is. Import also
settles with pool credit disabled, so it produces no consolidation pressure
and **does not trigger dreams**. Run `mnemoseed-local dream --once` yourself if
you want consolidation of the imported material.

## Dream model setup

Dreaming is optional and off until you bind a model: a fresh setup runs
capture, recall, embeddings, and decay without any dream route, `doctor`
reports the unbound route as a readiness note, and scheduled dream work stays
honestly deferred instead of calling a model that is not there. Nothing
installs, waits for, or pulls Ollama by default.

Bind a model through the local web console's provider catalog at
`http://localhost:7788/#/config`:

- an OpenAI-compatible preset (OpenAI, Anthropic, Google Gemini, DeepSeek,
  Moonshot Kimi, xAI Grok, OpenRouter, NVIDIA NIM),
- a custom provider (your own base URL plus key, OpenAI-compatible), or
- a locally detected Ollama, which appears automatically when its service is
  running.

Once a model is bound, a local Ollama route works like this: start the local
service, and pull the bound dream model if it is not present. Hardware
capacity and download size depend on the chosen model.

```sh
ollama pull <bound dream model>
mnemoseed-local doctor
mnemoseed-local dream --once
```

Manual `dream --once` reports `launched: false` with a reason when the route is
unavailable. In an existing config with legacy auto dream enabled, scheduled
work remains pending while the bound model is unavailable and retries on
a later scheduler cycle without a daemon restart. Capture, recall, and other
memory APIs remain available when their own dependencies are ready.

The legacy scheduler uses a pool-score floor, idle window, and 24-hour hard
deadline. `dream.auto_trigger = false` disables it; `capture.auto_recall = false`
disables focal recall on prompts. Do not interpret the installer's `refinement`
mode as a delivered new engine: refinement v2 is a design proposal, and that mode
does not re-enable the legacy automatic dream pass.

A bound remote provider endpoint receives the memory sent to it. API keys use
the OS credential store when available, with a restricted local-file fallback.

## Package-index installers (currently unavailable)

The checked-in [Windows installer](../install.ps1) and
[POSIX installer](../install.sh) pin `mnemoseed-local[codex]==0.1.0` from the
default package index. They fail at package installation until that package is
published. Use the source route above; the installer descriptions here explain
their behavior and existing installations, not a working one-command install.

They never install, wait for, or pull Ollama by default, in any mode: they
install uv if missing, install the CLI, and run `init` and `doctor`. The
Ollama install step and the model pull run only behind the explicit
`--install-ollama` / `-InstallOllama` switch. Host wiring
requires your selection, as described below. Existing configs are not silently
rewritten. With the switch, model pulling requires confirmation
(`--yes` / `-Yes` skips that prompt). `--dry-run` / `-DryRun` prints a plan
without side effects.

### Host selection

Pass `--hosts a,b,c` to `install.sh` or `-Hosts a,b,c` to `install.ps1`, using any
of `claude_code`, `claude_desktop`, `codex_cli`, `cursor`, `opencode`, and `zcode`.
Only the named adapters are wired; repeated names are deduplicated. Unknown names,
including `all`, are refused before any installation step writes anything.

Without an explicit list, a terminal session offers a numbered menu of detected
hosts. Detection checks the host CLI on PATH or its adapter config directory,
including `MNEMOSEED_CLAUDE_DESKTOP_CONFIG`, `CODEX_HOME`, `OPENCODE_CONFIG_DIR`,
or `XDG_CONFIG_HOME` overrides where applicable. Only valid offered menu numbers
select hosts; an empty selection wires none. A non-interactive run without a list
wires nothing, reports detected hosts, and prints the exact per-host commands as
next steps. The daemon can be used without a host adapter.

The installers currently invoke the OpenCode `hook install opencode` alias;
other selected hosts use `host install --host <host>`. The unified `host` group
also works for OpenCode. Each wired host reports hooks, MCP, and trust outcomes
separately. Codex `/hooks` approval and other manual trust steps are never
automated. An adapter failure prints a warning and its exact manual retry
command; the installer continues, so inspect each host's outcome rather than
treating overall installation completion as proof of a working host.

### Ollama options

The Ollama steps are opt-in on both installers via `--install-ollama` (POSIX)
or `-InstallOllama` (Windows); without the switch nothing installs, waits for,
or pulls Ollama in any mode. With the switch, the POSIX installer can install
missing Ollama via the official Linux script or, on macOS with Homebrew
available, offer `brew install ollama` behind explicit consent. `--yes` grants
the Homebrew consent as well as skipping the model-pull prompt. If Homebrew is
absent or you decline, the script exits non-zero with manual setup commands.

The switch also enables the confirmation-gated model pull after
`mnemoseed-local doctor`. Without Ollama or a bound dream model, capture and
recall still work out of the box and dreaming stays off until a model is
bound in the console provider catalog. See
[dream model setup](#dream-model-setup).

### Windows startup and helpers

The Windows installer resolves its helper module and two logon scripts from
the same raw `main` source when invoked remotely. It downloads and parses the
module before dot-sourcing it, using a unique temporary directory removed on
success and failure. A complete local checkout uses sibling scripts instead.
A helper download or parse failure aborts setup.

The installer does not manage Ollama: it registers no Ollama task, removes no
task, and never stops Ollama or changes its native tray/startup configuration.
Machines carrying the earlier hidden `\OllamaHeadlessServe` task keep it; this
software no longer manages that task.

The Windows installer registers the current-user `MnemoSeedLocalDaemon`
AtLogOn task. On login, the daemon starts directly; there is no wait for any
external service. There is no periodic trigger or crash
restart policy. See [daemon lifecycle](#daemon-lifecycle) for persistent on/off
and manual recovery.

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

## Status

MnemoSeed Local is CLI-first, single-machine, and single-user, with no accounts
or cloud defaults. Its core loop is capture → dream → decay → retrieve.
Importance scoring does not call an LLM, while embedding uses the local ONNX
encoder. Chunks are stored verbatim and history is append-only. Reconciling a
conflicting decision appends a new version whose `prev_version_id` points back
to the revision it replaces, rather than overwriting that revision.

Memory has per-profile namespaces. The conventional `default` namespace works
out of the box; use `mnemoseed-local profile {create|list|archive|unarchive}` on
the running daemon and `profiles.agent_bindings` to bind agents. Archiving does
not delete data or unbind agents.

Current technical surfaces include:

- Config, secrets, schema/migrations, storage ports, and embedded drivers:
  `sqlite_meta`, `sqlite_graph`, `lancedb_embedded`, `bge_m3_onnx`, and
  `synthetic_embedder`.
- Session continuity: start-of-session replay, mid-turn focal auto-recall, and
  reinforcement backed by consumption evidence. Injection alone is not evidence
  of consumption.
- Conversation visibility: one fixed, non-directive capture/replay/recall line.
  `[visibility] mode` accepts `compact` (default), `off`, or `verbose` (adds
  count lines), and applies without restarting.
- `origin_agent` attribution from ingest through recall/recent: inert provenance
  that does not affect scoring or ranking.
- Separate score-pool values: `balance` is pending work; `filed_points_total` is
  the lifetime ledger, preserved across restarts.
- Retention with cue-based rescue and fade-to-index.
- Recall/Atlas provenance: `provenance_source`, `explicit_pin`, and
  `needs_reconcile`; injected pinned lines are marked.
- MCP handshake beacons, registered-but-never-connected gateway warnings in
  doctor, and the `/api/v1/observability` snapshot.
- An append-only `error_events` ledger and query plumbing.
- A loopback daemon, MCP gateway, CLI, web console, durable `daemon.log`, and
  an in-process TCP-probe watchdog with a socket-alive veto against spurious kills.

### What it does NOT do yet

- Dream emits structured `prefers` / `has_habit` / `decided` / `believes` facts,
  not typed lessons. Experience learning is unfinished and off; the error-event
  ledger is scaffolding and no detector decides anything.
- The current adapters are OpenCode, Claude Code, ZCode, and Codex CLI. Codex
  Desktop app-server mode is unverified, and Codex CLI hooks require trust review.
- Windows is the primary tested platform; Linux runs in CI. macOS dream use
  needs Ollama setup, either manually or through the installer's explicit
  `--install-ollama` Homebrew consent. Coverage varies across host/platform combinations.
- Windows logon startup, where registered, has no later crash supervision.
- Multi-session mutual awareness is research. Multi-device sync and a hosted
  cloud daemon belong to the broader [MnemoSeed direction](https://github.com/MnemoSeed),
  not the delivered Local surface.
- Stored memory has no confidentiality guarantee: no E2EE, no TEE, and no
  encryption at rest. Remote model use is an explicit configuration choice.

## Feature verification

[Memory Atlas verification](../features/atlas.md) records isolated browser proof
and retained evidence. It does not establish all-host installation success.

## MCP gateway

The CLI ships a zero-config MCP stdio gateway (newline-delimited JSON-RPC,
daemon REST proxy with audit actor `mcp`). Register it in `opencode.json`:

```json
{"mcp": {"mnemoseed": {"type": "local", "command": ["mnemoseed-local", "mcp"]}}}
```

The running daemon also serves the same gateway over loopback HTTP on
**`POST /mcp`** (`application/json` only, stateless — no session id, no
server-side session table). The OpenCode plugin's config hook registers the
remote entry for you (`oauth: false` plus an explicit 30s timeout per bucket (startup, catalog, execution); an existing
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
  "disabled": false,
  "timeout": {"startup": 30000, "catalog": 30000, "execution": 30000}
}}}
```

Any third-party local client speaks the same contract with plain HTTP: read
the credential from `~/.mnemoseed-local/secrets/daemon.credential.key`,
present it as the bearer header, carry `X-MnemoSeed-Profile`, and POST the
JSON-RPC body to `/mcp`.

Tools: `recall(query, top_k?)`, `remember(text, rules?)`, `capture(text)`,
`supersede(superseded_node_id, successor_node_id)`,
`dream_once()`, `recent_sessions(resume_query, n_sessions?, n_per_session?, self_parent_id?, caller_cwd?)`,
`session_windows(n_sessions?)`, `recall_feedback(kind, observed_at)`. The handshake works even when the daemon is
down; only tool calls report the connectivity error.

A `recall` result can be empty when no stored candidate clears the semantic
relevance floor; that is a meaningful answer ("no relevant memory"), not a
transport failure. In `coverage`, `vector_hits` is the count after the floor is
applied and `floor_dropped` is how many candidates it excluded.

The handshake also carries a short instruction block (`initialize.result.instructions`) that states the four
execution rules — use an injected memory block instead of re-querying, recall before answering about past work,
`capture` a durable statement the user makes, `remember` when the user asks for a pin. The MCP specification
defines that field as optional and says a client MAY use it, so it is best-effort delivery: a client that ignores
it loses nothing, and no surface here claims the block reached a model. `remember` writes a pin; `capture` routes
to the same write service with a capture stamp (source `mcp-chat`, no pin marker), so the two stay
distinguishable by `explicit_pin` exactly as the skill describes.

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

## Surfaces without a capture hook

Claude chat and ChatGPT web cannot install a hook: no hook command exists in
those products. Memory there works through the MCP tools the assistant calls
itself, plus a protocol block you paste once.

```sh
mnemoseed-local host install --host claude_desktop
mnemoseed-local instructions --surface claude_chat
```

`host install --host claude_desktop` merges one `mcpServers.mnemoseed` entry
into the app's `claude_desktop_config.json`: `{"command": "<absolute path>",
"args": ["mcp"]}`, where the command is the installed CLI's console script
beside its interpreter (falling back to that interpreter with `args: ["-m",
"mnemoseed_local.cli", "mcp"]` when no launcher was shipped). The file is
resolved by directory existence: the `MNEMOSEED_CLAUDE_DESKTOP_CONFIG` override,
then whichever side already carries a config file — the Windows MSIX package
directory or the platform classic path — and, when neither does, the MSIX
directory if it exists, else the classic path. Every other key in that file is
preserved, and a file that is not strict JSON is refused untouched; an existing
`mcpServers.mnemoseed` entry of the wrong shape refuses too, instead of being
replaced. One caveat of any rewrite: duplicate JSON keys in the original file
are silently merged by `json.loads`, which keeps only the last value. The
command is written only from an installed runtime — a development checkout
refuses, because the app cannot spawn a build environment's interpreter.
`host status --host claude_desktop` reports `not-installed` / `installed` /
`stale` (the configured command and arguments are not the ones this surface
would write now) / `unreadable` (the file, or the entry inside it, cannot be
parsed). Close Claude Desktop before restarting it: a running app can rewrite
the file from memory on exit.

`instructions --surface <surface>` prints the paste-ready block for
`claude_chat`, `chatgpt`, `claude_code`, or `generic` (the block the MCP
handshake itself serves). Claude's personal preferences and ChatGPT's custom
instructions are account-level and cloud-synced, so no installer can write them:
pasting is the boundary, not a gap.

## Agent skill

The repository ships [an agent skill](../skills/mnemoseed/SKILL.md) covering MCP
usage, proactive recall, scoped `recent_sessions` resume, and pinned-vs-captured
discrimination. Copy the `skills/mnemoseed/` directory into your host's skills
directory (for example `~/.claude/skills/` or `.opencode/skills/`) and restart
the host. The repository's own `.opencode/skills/` automation does not ship as
this product skill.

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

Source installation does not register login startup. Where the Windows
installer has registered the `MnemoSeedLocalDaemon` current-user task, the daemon
starts once at user logon. It does not auto-restart after a later
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
by default, but refuses an unknown or foreign same-name task. See
[uninstall](#uninstall) for adapter handling, data retention, explicit purge,
and the separate package-removal step.

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

## Development

The project uses test-driven work and an adversarial verifier. From a source
checkout, the development gates are:

```sh
uv run pytest -q
uv run ruff check
uv run ruff format --check
uv run mypy src
```

## Evaluation (maintainers)

The harness lives at `python -m mnemoseed_local.eval`, run from a source checkout:

- `canary`: self-check the harness with stub seats.
- `matrix`: run the material catalog.
- `rescore`: re-judge a v1.1 report offline without GPU.
- `recall`: T4b live calibration coordinate descent; the locked focal floor is
  0.5 and recall budget is 2400 characters (accepted 2026-08-23).

## Support and license

See [Chinese documentation](zh/README.md),
[report an issue](https://github.com/MnemoSeed/mnemoseed-local/issues), or contact
[hello@mnemoseed.tech](mailto:hello@mnemoseed.tech). Voluntary support is welcome
through [GitHub Sponsors](https://github.com/sponsors/MnemoSeed).

MnemoSeed Local is [MIT licensed](../LICENSE).
