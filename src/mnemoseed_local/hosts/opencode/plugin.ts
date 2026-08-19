// MnemoSeed Local — OpenCode host hook adapter (A3 T2, design/01 §4.5).
//
// Shipped as package data of the `mnemoseed-local` Python distribution and
// installed by `mnemoseed-local hook install` into
// <opencode-config-root>/plugin/mnemoseed-local.ts.
//
// Wire contract (pinned — keep in sync with tests/fixtures/opencode_hook/ and
// tests/test_hosts_opencode.py, which parses this table):
//
//   chat.message                        -> user_prompt       -> POST /ingest
//   event message.updated (assistant)   -> assistant_message -> POST /ingest
//   tool.execute.after                  -> tool_use          -> POST /ingest
//   event session.idle|error            -> flush             -> POST /flush
//   event session.deleted               -> session_end       -> POST /session/end
//   experimental.session.compacting     -> flush             -> POST /flush
//
// Invariants: HostId is always "opencode"; every daemon call is
// fire-and-forget (the host session is never blocked); every failure is
// swallowed into console.debug UNLESS the opt-in observability lane is armed
// (env MNEMOSEED_LOCAL_DEBUG: failures escalate to console.error + a JSONL
// sink, and every daemon POST's status is inspected — a swallowed non-2xx is
// how the settle-sealing bug hid); requests time out after 2s via
// AbortSignal.
//
// Type reference only (NOT a runtime import — the plugin is dependency-free):
//   import type { Plugin } from "@opencode-ai/plugin"
// The module's default export is the plugin function; OpenCode's loader
// treats every exported function-valued value as a plugin instance.

import { appendFile, mkdir } from "node:fs/promises"
import { dirname } from "node:path"

const HOST_ID = "opencode"
const BASE_URL: string = process.env.MNEMOSEED_LOCAL_BASEURL || "http://localhost:7788"
const PROFILE_ID: string = process.env.MNEMOSEED_LOCAL_PROFILE_ID || "default"
const TIMEOUT_MS = 2000
const FETCH_TIMEOUT_MS = 1500
const DEDUP_CAP = 1000
const MAX_TOOL_OUTPUT_CHARS = 20000

// ---- opt-in observability lane (senior QA finding 12b, 2026-08-19): three
// silent-failure dogfoods in one day proved console.debug alone is
// invisible. Arm with MNEMOSEED_LOCAL_DEBUG (any non-empty value): failures
// escalate to console.error AND a JSONL sink next to the daemon data dir.
const DEBUG: boolean = Boolean(process.env.MNEMOSEED_LOCAL_DEBUG)
const DEBUG_LOG_PATH: string =
  `${process.env.USERPROFILE || ""}\\.mnemoseed-local\\hook-debug.jsonl`

function debugLog(tag: string, payload: unknown): void {
  console.debug(`mnemoseed-local: ${tag}:`, payload)
  if (!DEBUG) return
  console.error(`mnemoseed-local: ${tag}:`, payload)
  const line = JSON.stringify({ ts: new Date().toISOString(), tag, payload })
  void mkdir(dirname(DEBUG_LOG_PATH), { recursive: true })
    .then(() => appendFile(DEBUG_LOG_PATH, line + "\n", "utf8"))
    .catch((error: unknown) => console.debug("mnemoseed-local: debug sink failed:", error))
}

type JsonRecord = { [key: string]: unknown }

// Fire-and-forget POST of a single-line JSON body; never throws. The daemon's
// STATUS is inspected: a non-2xx lands in the debug lane (409 = rejected,
// e.g. an already-settled session — losing it silently once cost a day).
function post(endpoint: string, body: JsonRecord): void {
  try {
    void fetch(BASE_URL + endpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    })
      .then((response) => {
        if (!response.ok) {
          debugLog("daemon POST rejected", { endpoint, status: response.status })
        }
      })
      .catch((error: unknown) => {
        console.debug("mnemoseed-local: POST", endpoint, "failed:", error)
        debugLog("daemon POST failed", { endpoint, error: String(error) })
      })
  } catch (error) {
    console.debug("mnemoseed-local: POST", endpoint, "aborted:", error)
  }
}

// ---- bounded FIFO dedup (Map preserves insertion order; the oldest key is
// evicted first past the cap) ---------------------------------------------

function seen(mark: Map<string, true>, key: string): boolean {
  if (mark.has(key)) return true
  mark.set(key, true)
  if (mark.size > DEDUP_CAP) {
    const oldest = mark.keys().next().value
    if (oldest !== undefined) mark.delete(oldest)
  }
  return false
}

// assistant_message marks its (sessionID, messageID) fingerprint up front
// (message.updated re-fires per part flush; the mark suppresses duplicate
// posts AND concurrent in-flight duplicates). A failed or textless fetch does
// NOT roll the mark back — it PARKS the id in pendingAssistant instead, and
// the next session sweep (idle/error/deleted) retries deterministically.
const sentAssistant = new Map<string, true>()
// session_end settles ONCE per session_id (session.deleted is the only
// terminal signal — see onBusEvent).
const settledSessions = new Map<string, true>()

// Per-session set of assistant messageIDs whose parts fetch has not yet
// succeeded with text (pending retry at the next sweep point).
const pendingAssistant = new Map<string, Set<string>>()

function parkAssistant(sessionID: string, messageID: string): void {
  let set = pendingAssistant.get(sessionID)
  if (set === undefined) {
    set = new Set<string>()
    pendingAssistant.set(sessionID, set)
  }
  if (set.size < DEDUP_CAP) set.add(messageID)
}

function unparkAssistant(sessionID: string, messageID: string): void {
  const set = pendingAssistant.get(sessionID)
  if (set === undefined) return
  set.delete(messageID)
  if (set.size === 0) pendingAssistant.delete(sessionID)
}

function textOf(parts: unknown): string {
  if (!Array.isArray(parts)) return ""
  return parts
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => String(part.text))
    .join("\n")
    .trim()
}

async function fetchAssistantText(
  client: { session?: { messages?: (options: unknown) => Promise<unknown> } } | undefined,
  sessionID: string,
  messageID: string,
): Promise<{ ok: boolean; text: string }> {
  // The opencode SDK (gen client, hey-api): session.messages({ path: { id } })
  // lists [{ info, parts }]. Call it ON ITS RECEIVER — extracting the method
  // unbinds `this` and the body `(options.client ?? this._client).get(...)`
  // throws TypeError reading '_client' (dogfood 2026-08-19: BOTH the original
  // singular-endpoint failure AND this unbound extraction died silently into
  // console.debug before probe instrumentation exposed them). The call is
  // timeout-raced so a hung SDK promise parks in the pending set instead of
  // leaking forever (senior QA finding 1).
  if (typeof client?.session?.messages !== "function") return { ok: false, text: "" }
  try {
    const response: any = await Promise.race([
      client.session.messages({ path: { id: sessionID } }),
      new Promise((_resolve, reject) =>
        setTimeout(() => reject(new Error("assistant parts fetch timeout")), FETCH_TIMEOUT_MS),
      ),
    ])
    const entries = Array.isArray(response?.data) ? response.data : response
    if (!Array.isArray(entries)) return { ok: false, text: "" }
    const found = entries.find((entry: any) => entry?.info?.id === messageID)
    // Not yet retrievable (parts not flushed): fail the fetch so the caller
    // parks the id for the next sweep (宁可重复不丢).
    if (!found) return { ok: false, text: "" }
    return { ok: true, text: textOf(found.parts) }
  } catch (error) {
    console.debug("mnemoseed-local: fetch message list failed:", error)
    debugLog("assistant parts fetch failed", { sessionID, messageID, error: String(error) })
    return { ok: false, text: "" }
  }
}

function modelIdOf(info: any): string | undefined {
  const provider = typeof info?.providerID === "string" ? info.providerID : ""
  const model = typeof info?.modelID === "string" ? info.modelID : ""
  if (provider && model) return `${provider}/${model}`
  if (model) return model
  return undefined
}

function sessionIdOfEvent(event: any): string {
  const props = event?.properties ?? {}
  if (typeof props.sessionID === "string") return props.sessionID
  // session.deleted carries the session record instead of a bare sessionID.
  if (typeof props.info?.id === "string") return props.info.id
  return ""
}

// opencode fires session.idle after EVERY completed reply — idle means the
// agent went quiet, NOT that the conversation ended (dogfood 2026-08-19:
// mapping idle -> /session/end settled the session at the first reply; all
// later ingest then answered 409 and was swallowed silently). Flush closes
// and drains the in-flight turn while keeping the session ingestable.
function flushSession(sessionID: string): void {
  if (!sessionID) return
  post("/flush", { session_id: sessionID, profile_id: PROFILE_ID })
}

function settle(sessionID: string): void {
  if (!sessionID) return
  if (seen(settledSessions, sessionID)) return
  post("/session/end", {
    session_id: sessionID,
    profile_id: PROFILE_ID,
    ts: Date.now() / 1000,
  })
}

function postAssistantIngest(sessionID: string, messageID: string, info: any, text: string): void {
  const content: JsonRecord = { text }
  const modelId = modelIdOf(info)
  if (modelId) content.model_id = modelId
  post("/ingest", {
    host: HOST_ID,
    event: "assistant_message",
    session_id: sessionID,
    profile_id: PROFILE_ID,
    ts: Date.now() / 1000,
    content,
    raw: { messageID },
  })
}

// Deterministic retry for parked assistant fetches (senior QA findings 1+2):
// the host is NOT relied on to re-fire message.updated (the last reply of a
// session never re-fires), and settle can never overtake an outstanding
// fetch — the sweep is AWAITED before /flush and /session/end are posted.
// A fetch that stays textless AT the sweep is final (tool-only reply).
async function sweepPendingAssistant(
  client: { session?: { messages?: (options: unknown) => Promise<unknown> } } | undefined,
  sessionID: string,
): Promise<void> {
  const parked = pendingAssistant.get(sessionID)
  if (parked === undefined || parked.size === 0) return
  for (const messageID of Array.from(parked)) {
    try {
      const fetched = await fetchAssistantText(client, sessionID, messageID)
      if (!fetched.ok) continue // stays parked for the next sweep
      unparkAssistant(sessionID, messageID)
      if (!fetched.text) continue // final verdict: genuinely textless reply
      postAssistantIngest(sessionID, messageID, undefined, fetched.text)
    } catch (error) {
      console.debug("mnemoseed-local: pending sweep failed:", error)
    }
  }
}

export default async function MnemoSeedLocalPlugin(input: { client?: unknown }) {
  const client = input?.client as
    | { session?: { messages?: (options: unknown) => Promise<unknown> } }
    | undefined

  async function onChatMessage(hookInput: any, hookOutput: any): Promise<void> {
    try {
      const text = textOf(hookOutput?.parts)
      if (!text) return
      const sessionID = String(hookInput?.sessionID ?? "")
      if (!sessionID) return
      const raw: JsonRecord = {}
      const messageID = hookInput?.messageID ?? hookOutput?.message?.id
      if (typeof messageID === "string" && messageID) raw.messageID = messageID
      if (typeof hookInput?.agent === "string" && hookInput.agent) raw.agent = hookInput.agent
      post("/ingest", {
        host: HOST_ID,
        event: "user_prompt",
        session_id: sessionID,
        profile_id: PROFILE_ID,
        ts: Date.now() / 1000,
        content: { text },
        raw,
      })
    } catch (error) {
      console.debug("mnemoseed-local: chat.message hook failed:", error)
    }
  }

  async function onMessageCompleted(info: any): Promise<void> {
    if (info?.role !== "assistant") return
    if (typeof info?.time?.completed !== "number") return
    const sessionID = String(info?.sessionID ?? "")
    const messageID = String(info?.id ?? "")
    if (!sessionID || !messageID) return
    const fingerprint = `${sessionID}:${messageID}`
    if (seen(sentAssistant, fingerprint)) return
    const fetched = await fetchAssistantText(client, sessionID, messageID)
    if (!fetched.ok || !fetched.text) {
      // Not retrievable yet (or still textless at completion): park it — the
      // next session sweep retries (a genuinely tool-only reply is dropped
      // for good at sweep time, after one retry).
      parkAssistant(sessionID, messageID)
      return
    }
    postAssistantIngest(sessionID, messageID, info, fetched.text)
  }

  async function onBusEvent(event: any): Promise<void> {
    try {
      switch (event?.type) {
        case "message.updated":
          await onMessageCompleted(event?.properties?.info)
          break
        case "session.idle":
        case "session.error": {
          const sessionID = sessionIdOfEvent(event)
          await sweepPendingAssistant(client, sessionID)
          flushSession(sessionID)
          break
        }
        case "session.deleted": {
          const sessionID = sessionIdOfEvent(event)
          await sweepPendingAssistant(client, sessionID)
          settle(sessionID)
          break
        }
        default:
          break
      }
    } catch (error) {
      console.debug("mnemoseed-local: event hook failed:", error)
    }
  }

  async function onSessionCompacting(hookInput: any): Promise<void> {
    try {
      const sessionID = String(hookInput?.sessionID ?? "")
      if (sessionID) post("/flush", { session_id: sessionID, profile_id: PROFILE_ID })
    } catch (error) {
      console.debug("mnemoseed-local: session.compacting hook failed:", error)
    }
  }

  function stringifyToolOutput(value: unknown): string {
    let text: string
    if (typeof value === "string") text = value
    else if (value === undefined || value === null) text = ""
    else {
      try {
        text = JSON.stringify(value)
      } catch {
        text = String(value)
      }
    }
    if (text.length > MAX_TOOL_OUTPUT_CHARS) {
      return `${text.slice(0, MAX_TOOL_OUTPUT_CHARS)}\n[... truncated at ${MAX_TOOL_OUTPUT_CHARS} chars]`
    }
    return text
  }

  async function onToolExecuteAfter(hookInput: any, hookOutput: any): Promise<void> {
    try {
      const sessionID = String(hookInput?.sessionID ?? "")
      if (!sessionID) return
      const args =
        hookInput?.args && typeof hookInput.args === "object" ? (hookInput.args as JsonRecord) : {}
      const raw: JsonRecord = {}
      if (typeof hookInput?.callID === "string" && hookInput.callID) raw.callID = hookInput.callID
      post("/ingest", {
        host: HOST_ID,
        event: "tool_use",
        session_id: sessionID,
        profile_id: PROFILE_ID,
        ts: Date.now() / 1000,
        content: {
          tool_name: String(hookInput?.tool ?? ""),
          input: args,
          output: stringifyToolOutput(hookOutput?.output),
        },
        raw,
      })
    } catch (error) {
      console.debug("mnemoseed-local: tool.execute.after hook failed:", error)
    }
  }

  return {
    "chat.message": async (hookInput: any, hookOutput: any) => onChatMessage(hookInput, hookOutput),
    // The bus dispatcher voids this promise: awaiting the parts fetch here
    // never blocks the host.
    event: async ({ event }: { event: unknown }) => onBusEvent(event),
    "tool.execute.after": async (hookInput: any, hookOutput: any) =>
      onToolExecuteAfter(hookInput, hookOutput),
    "experimental.session.compacting": async (hookInput: any) => onSessionCompacting(hookInput),
  }
}
