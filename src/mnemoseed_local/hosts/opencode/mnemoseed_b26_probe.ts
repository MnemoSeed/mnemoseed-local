// MnemoSeed Local — B2.6 host-plugin bundling probe (OBSERVATION ONLY).
//
// A sibling of the shipped plugin.ts on the V2 plugin API. This probe
// observes the V2 loading surface: does setup(ctx) run, what does ctx.options
// carry, do ctx.mcp transform mutations stick, and does the event
// subscription deliver. It is inert unless MNEMOSEED_LOCAL_DEBUG is set, and
// even then it only appends JSON lines to ~/.mnemoseed-local/probe-b26.jsonl
// plus one disabled mcp sentinel.
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
// To answer the options question, ALSO register it via the plugins array in
// opencode.json (a plain file copy sees ctx.options as undefined):
//
//   { "plugins": [{ "package": "./plugins/mnemoseed_b26_probe", "options": { "enabled": true } }] }
//
// Observation protocol:
//   1. arm the sink: MNEMOSEED_LOCAL_DEBUG=1
//      (Windows PowerShell: $env:MNEMOSEED_LOCAL_DEBUG = "1")
//   2. restart opencode (plugins load once at startup)
//   3. run one session, then exit opencode
//   4. collect ~/.mnemoseed-local/probe-b26.jsonl and fold the findings into
//      the B2.6 design record (docs/zh/design/research-opencode-plugin-bundling.md)
//
// Remove this file (and the opencode.json entry) once the observation is done.

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

// V2 plugin entry: the loader validates the default export's id + setup and
// runs setup once with the plugin ctx (ctx.options carries the
// { package, options } entry's options object).
export default {
  id: "mnemoseed-b26-probe",
  setup(ctx: any): () => void {
    log("setup", {
      plugin: "mnemoseed_b26_probe",
      options: ctx?.options === undefined ? null : ctx.options,
      locationKeys: keysOf(ctx?.location),
      locationDirectory: (ctx?.location as { directory?: unknown } | undefined)?.directory ?? null,
    })

    try {
      void ctx.mcp.transform((editor: any): void => {
        if (!DEBUG) return
        const before = editor.list().map(([name]: [string]) => name)
        const sentinelPresent = editor.get(SENTINEL_NAME) !== undefined
        if (!sentinelPresent) {
          editor.set(SENTINEL_NAME, { type: "local", command: ["b26-probe-noop"], disabled: true })
        }
        log("mcp", {
          mcpBefore: before,
          sentinelPresent,
          mutation: sentinelPresent ? "already-present" : "appended",
          mcpAfter: editor.list().map(([name]: [string]) => name),
        })
      })
    } catch (error) {
      log("mcp-transform-error", { error: String(error) })
    }

    const controller = new AbortController()
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          if (eventsSeen >= EVENT_CAP) break
          eventsSeen += 1
          const eventType =
            event !== null && typeof event === "object"
              ? (event as { type?: unknown }).type
              : undefined
          log("event", { eventOrdinal: eventsSeen, eventType: eventType ?? null })
        }
      } catch (error) {
        log("event-subscription-error", { error: String(error) })
      }
    })()
    return () => controller.abort()
  },
}
