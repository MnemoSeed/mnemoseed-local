// MnemoSeed Local — B2.6 host-plugin bundling probe (OBSERVATION ONLY).
//
// A sibling of the shipped plugin.ts (opencode loads every exported
// function-valued module under its scanned plugin dirs side-by-side). This
// probe answers the B2.6 research questions from
// docs/zh/design/research-opencode-plugin-bundling.md: does the `config`
// hook fire, do cfg.mcp mutations stick, and does an options tuple reach the
// plugin. It is inert unless MNEMOSEED_LOCAL_DEBUG is set, and even then it
// only appends JSON lines to ~/.mnemoseed-local/probe-b26.jsonl plus one
// disabled cfg.mcp sentinel.
//
// NOT installed by `mnemoseed-local hook install` (that verb writes only the
// shipped plugin.ts). Copy this file by hand to the global plugins dir:
//
//   <opencode-config-root>/plugins/mnemoseed_b26_probe.ts
//
// <opencode-config-root> = OPENCODE_CONFIG_DIR, else $XDG_CONFIG_HOME/opencode,
// else ~/.config/opencode (see src/mnemoseed_local/hosts/install.py). The
// host scans {plugin,plugins}/*.{ts,js} there at startup.
//
// To answer the options-tuple question, ALSO register it via the plugin array
// in opencode.json (a plain file copy sees `options` as undefined):
//
//   { "plugin": [["file:///ABSOLUTE/PATH/TO/mnemoseed_b26_probe.ts", { "enabled": true }]] }
//
// Observation protocol:
//   1. arm the sink: MNEMOSEED_LOCAL_DEBUG=1
//      (Windows PowerShell: $env:MNEMOSEED_LOCAL_DEBUG = "1")
//   2. restart opencode (plugins load once at startup)
//   3. run one session, then exit opencode
//   4. collect ~/.mnemoseed-local/probe-b26.jsonl and fold the findings into
//      the B2.6 design record (docs/zh/design/research-opencode-plugin-bundling.md)
//
// Remove this file (and the opencode.json tuple) once the observation is done.

import { appendFile, mkdir } from "node:fs/promises"
import { homedir } from "node:os"
import { dirname, join } from "node:path"

// Opt-in sink (the shipped plugin's seam): every firing appends one JSON
// line { ts, seq, tag, payload }; `seq` is a process-wide monotonic counter
// shared by every hook entry, so the log pins relative startup ordering.
const DEBUG: boolean = Boolean(process.env.MNEMOSEED_LOCAL_DEBUG)
const DATA_DIR: string =
  process.env.MNEMOSEED_LOCAL_DATA_DIR || join(homedir(), ".mnemoseed-local")
const SINK_PATH: string = join(DATA_DIR, "probe-b26.jsonl")
const SENTINEL_NAME = "b26-probe-sentinel"
const EVENT_CAP = 50

let seq = 0
let eventsSeen = 0

function log(tag: string, payload: unknown): void {
  if (!DEBUG) return
  const line = JSON.stringify({ ts: new Date().toISOString(), seq: ++seq, tag, payload })
  void mkdir(dirname(SINK_PATH), { recursive: true })
    .then(() => appendFile(SINK_PATH, line + "\n", "utf8"))
    .catch((error: unknown) => console.debug("mnemoseed-b26-probe: sink failed:", error))
}

function keysOf(value: unknown): string[] {
  return value !== null && typeof value === "object"
    ? Object.keys(value as Record<string, unknown>)
    : []
}

// V1 plugin entry: the loader calls (input, options) — the SECOND argument is
// the `["spec", { ... }]` tuple's options object (undefined for a bare spec).
export default async function MnemoseedB26Probe(
  _input: unknown,
  options: unknown,
): Promise<Record<string, unknown>> {
  log("load", { plugin: "mnemoseed_b26_probe", options: options === undefined ? null : options })

  return {
    config: async (cfg: Record<string, unknown>): Promise<void> => {
      if (!DEBUG) return
      const mcp = (cfg?.mcp ?? {}) as Record<string, unknown>
      const mcpBefore = keysOf(mcp)
      const sentinelPresent = mcp[SENTINEL_NAME] !== undefined
      if (!sentinelPresent) {
        cfg.mcp = mcp
        mcp[SENTINEL_NAME] = { type: "local", command: ["b26-probe-noop"], enabled: false }
      }
      log("config", {
        cfgKeys: keysOf(cfg),
        mcpBefore,
        sentinelPresent,
        mutation: sentinelPresent ? "already-present" : "appended",
        mcpAfter: keysOf(mcp),
      })
    },
    event: async ({ event }: { event: unknown }): Promise<void> => {
      if (eventsSeen >= EVENT_CAP) return
      eventsSeen += 1
      const eventType =
        event !== null && typeof event === "object" ? (event as { type?: unknown }).type : undefined
      log("event", { eventOrdinal: eventsSeen, eventType: eventType ?? null })
    },
    "chat.message": async (): Promise<void> => log("hook", { name: "chat.message" }),
    "chat.system.transform": async (): Promise<void> =>
      log("hook", { name: "chat.system.transform" }),
    "tool.execute.after": async (): Promise<void> => log("hook", { name: "tool.execute.after" }),
    "experimental.session.compacting": async (): Promise<void> =>
      log("hook", { name: "experimental.session.compacting" }),
  }
}