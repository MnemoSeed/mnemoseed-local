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

import { appendFile, mkdir, readFile, rename, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import { dirname, join } from "node:path"

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
// escalate to console.error + a JSONL sink next to the daemon data dir.
const DEBUG: boolean = Boolean(process.env.MNEMOSEED_LOCAL_DEBUG)
// Artifact root: env override (tests, exotic layouts) > the platform home —
// POSIX-safe (B2.2 QA: the USERPROFILE\\ literal scattered stray files under
// per-project CWDs on non-Windows hosts).
const DATA_DIR: string =
  process.env.MNEMOSEED_LOCAL_DATA_DIR || join(homedir(), ".mnemoseed-local")
const DEBUG_LOG_PATH: string = join(DATA_DIR, "hook-debug.jsonl")

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
// ack runs ONLY on 2xx — watermarks are an ACK-clock, never a send-clock
// (B2.2 BLOCKER). nack runs on rejection/failure: ingest lanes use it to
// un-reconcile the session and roll assistant fingerprints back (B2.2 QA
// re-review — a rejected ingest must schedule recovery, not leapfrog it).
function post(endpoint: string, body: JsonRecord, ack?: () => void, nack?: () => void): void {
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
          if (nack !== undefined) nack()
          return
        }
        if (ack !== undefined) ack()
      })
      .catch((error: unknown) => {
        console.debug("mnemoseed-local: POST", endpoint, "failed:", error)
        debugLog("daemon POST failed", { endpoint, error: String(error) })
        if (nack !== undefined) nack()
      })
  } catch (error) {
    console.debug("mnemoseed-local: POST", endpoint, "aborted:", error)
    if (nack !== undefined) nack()
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

// ---- B2.2 crash durability (PRD-B2.2, single mechanism): the host persists
// the full session history itself, so crash recovery = replay the missing
// tail from client.session.messages into the same ingest lane. The only new
// artifact is one watermark file; the overlap margin feeds the daemon's
// near-dup absorb (repeats are absorbed by design: 宁可重复不丢).
const WATERMARKS_PATH: string = join(DATA_DIR, "hook-watermarks.json")
const REPLAY_OVERLAP_MS = 30000

const reconciledSessions = new Map<string, true>()
let watermarksCache: Record<string, number> | null = null

async function loadWatermarks(): Promise<Record<string, number>> {
  if (watermarksCache !== null) return watermarksCache
  try {
    const raw = await readFile(WATERMARKS_PATH, "utf8")
    const parsed: unknown = JSON.parse(raw)
    watermarksCache =
      parsed !== null && typeof parsed === "object" ? (parsed as Record<string, number>) : {}
  } catch {
    // Missing/corrupt file degrades to "no marks" — never a main-lane fault.
    watermarksCache = {}
  }
  return watermarksCache
}

function noteWatermark(sessionID: string, tsSeconds: number): void {
  // ACK-clock only (B2.2 BLOCKER): callers reach here exclusively through
  // post()'s 2xx branch, so the mark can never outrun the daemon's receipt.
  if (!sessionID || typeof tsSeconds !== "number") return
  if (watermarksCache === null || typeof watermarksCache !== "object") return
  const prev = watermarksCache[sessionID] ?? 0
  if (tsSeconds > prev) watermarksCache[sessionID] = tsSeconds
}

async function persistWatermarks(): Promise<void> {
  if (watermarksCache === null) return
  try {
    await mkdir(dirname(WATERMARKS_PATH), { recursive: true })
    // crash-atomic: a torn write must never corrupt the last good marks; the
    // unique suffix keeps concurrent cadence persists from clobbering each
    // other's temp file (re-review NIT-6).
    const tempPath = `${WATERMARKS_PATH}.${Date.now()}.tmp`
    await writeFile(tempPath, JSON.stringify(watermarksCache), "utf8")
    await rename(tempPath, WATERMARKS_PATH)
  } catch (error) {
    console.debug("mnemoseed-local: watermark persist failed:", error)
    debugLog("watermark persist failed", { error: String(error) })
  }
}

function textOf(parts: unknown): string {
  if (!Array.isArray(parts)) return ""
  return parts
    .filter(
      (part) =>
        part &&
        part.type === "text" &&
        typeof part.text === "string" &&
        part.synthetic !== true &&
        part.ignored !== true,
    )
    .map((part) => String(part.text))
    .join("\n")
    .trim()
}

async function listSessionMessages(
  client: { session?: { messages?: (options: unknown) => Promise<unknown> } } | undefined,
  sessionID: string,
): Promise<any[] | null> {
  // The opencode SDK (gen client, hey-api): session.messages({ path: { id } })
  // lists [{ info, parts }]. Call it ON ITS RECEIVER — extracting the method
  // unbinds `this` and the body `(options.client ?? this._client).get(...)`
  // throws TypeError reading '_client' (dogfood 2026-08-19: BOTH the original
  // singular-endpoint failure AND this unbound extraction died silently into
  // console.debug before probe instrumentation exposed them). The call is
  // timeout-raced so a hung SDK promise never leaks (senior QA finding 1).
  if (typeof client?.session?.messages !== "function") return null
  try {
    const response: any = await Promise.race([
      client.session.messages({ path: { id: sessionID } }),
      new Promise((_resolve, reject) =>
        setTimeout(() => reject(new Error("session messages fetch timeout")), FETCH_TIMEOUT_MS),
      ),
    ])
    const entries = Array.isArray(response?.data) ? response.data : response
    return Array.isArray(entries) ? entries : null
  } catch (error) {
    console.debug("mnemoseed-local: session messages fetch failed:", error)
    debugLog("session messages fetch failed", { sessionID, error: String(error) })
    return null
  }
}

async function fetchAssistantText(
  client: { session?: { messages?: (options: unknown) => Promise<unknown> } } | undefined,
  sessionID: string,
  messageID: string,
): Promise<{ ok: boolean; text: string }> {
  const entries = await listSessionMessages(client, sessionID)
  if (entries === null) return { ok: false, text: "" }
  const found = entries.find((entry: any) => entry?.info?.id === messageID)
  // Not yet retrievable (parts not flushed): fail the fetch so the caller
  // parks the id for the next sweep (宁可重复不丢).
  if (!found) return { ok: false, text: "" }
  return { ok: true, text: textOf(found.parts) }
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
  // session.deleted carries the session record — prefer its sessionID field;
  // bare info.id is a MESSAGE id and must never seed phantom session keys
  // into the watermark file (re-review NIT-7).
  if (typeof props.info?.sessionID === "string") return props.info.sessionID
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

function scheduleRecovery(sessionID: string): () => void {
  // nack: a rejected/lost ingest re-arms reconciliation for this session —
  // the next event replays from the last ACKED mark, so an outage hole can
  // never be leapfrogged by a later acked turn (re-review IMPORTANT-NEW-1).
  return () => {
    reconciledSessions.delete(sessionID)
  }
}

function postUserIngest(sessionID: string, text: string, ts: number, raw: JsonRecord): void {
  post(
    "/ingest",
    {
      host: HOST_ID,
      event: "user_prompt",
      session_id: sessionID,
      profile_id: PROFILE_ID,
      ts,
      content: { text },
      raw,
    },
    () => noteWatermark(sessionID, ts),
    scheduleRecovery(sessionID),
  )
}

function postAssistantIngest(
  sessionID: string,
  messageID: string,
  info: any,
  text: string,
  ts?: number,
): void {
  const stamp = ts ?? Date.now() / 1000
  const content: JsonRecord = { text }
  const modelId = modelIdOf(info)
  if (modelId) content.model_id = modelId
  post(
    "/ingest",
    {
      host: HOST_ID,
      event: "assistant_message",
      session_id: sessionID,
      profile_id: PROFILE_ID,
      ts: stamp,
      content,
      raw: { messageID },
    },
    () => {
      noteWatermark(sessionID, stamp)
      if (messageID) unparkAssistant(sessionID, messageID)
    },
    () => {
      // failure: the fingerprint was marked at issue — roll it back and
      // re-park so the sweep gets a retry (re-review IMPORTANT-NEW-2).
      if (messageID) {
        sentAssistant.delete(`${sessionID}:${messageID}`)
        parkAssistant(sessionID, messageID)
      }
      scheduleRecovery(sessionID)()
    },
  )
}

function postToolIngest(
  sessionID: string,
  toolName: string,
  args: JsonRecord,
  output: string,
  ts: number,
  raw: JsonRecord,
): void {
  post(
    "/ingest",
    {
      host: HOST_ID,
      event: "tool_use",
      session_id: sessionID,
      profile_id: PROFILE_ID,
      ts,
      content: { tool_name: toolName, input: args, output },
      raw,
    },
    () => noteWatermark(sessionID, ts),
    scheduleRecovery(sessionID),
  )
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

// Per-session FIFO task chain (B2.2 BLOCKER): turn boundaries at the daemon
// are cut on ARRIVAL order, so every lane that posts turn CONTENT for one
// session — live ingest, crash replay, parked sweep — is serialized through
// one promise chain. The replay leg is enqueued at the session's first event
// and therefore lands strictly before that session's live posts; the hook
// handlers themselves still await nothing on the hot path.
const sessionChains = new Map<string, Promise<void>>()

function enqueueForSession(sessionID: string, task: () => unknown): void {
  const chain = sessionChains.get(sessionID) ?? Promise.resolve()
  const next = chain
    .then(() => task())
    .then(() => undefined)
    .catch((error: unknown) => console.debug("mnemoseed-local: session chain task failed:", error))
  sessionChains.set(sessionID, next)
}

// B2.2 T2: crash-resume reconciliation. Runs ONCE per session per hook
// process, lazily at the session's first event — never eagerly scanning the
// host. Replays the tail after (watermark - overlap) from the host's own
// persisted history through the SAME ingest lane; the overlap rides the
// daemon's near-dup absorb. A session with NO watermark predates the feature
// (or lost the file): skip replaying whole histories — its already-drained
// turns live in the daemon's store, and the FIRST acked turn from now on
// writes the watermark that makes the NEXT crash recoverable (KISS boundary;
// the skip decision is logged in the debug lane, not silent).
async function reconcileSession(
  client: { session?: { messages?: (options: unknown) => Promise<unknown> } } | undefined,
  sessionID: string,
): Promise<void> {
  if (!sessionID) return
  if (reconciledSessions.has(sessionID)) return
  try {
    const marks = await loadWatermarks()
    const mark = marks[sessionID]
    if (typeof mark !== "number") {
      debugLog("reconcile skipped: no watermark", { sessionID })
      noteWatermark(sessionID, Date.now() / 1000)
      reconciledSessions.set(sessionID, true)
      return
    }
    const entries = await listSessionMessages(client, sessionID)
    if (entries === null) return // stays unflagged: retried at the next event
    const sinceSeconds = mark - REPLAY_OVERLAP_MS / 1000
    for (const entry of entries) {
      const info = entry?.info
      const role = info?.role
      const createdS =
        typeof info?.time?.created === "number" ? info.time.created / 1000 : undefined
      const completedS =
        typeof info?.time?.completed === "number" ? info.time.completed / 1000 : undefined
      if (role === "user" && createdS !== undefined && createdS > sinceSeconds) {
        const text = textOf(entry?.parts)
        if (text) {
          const raw: JsonRecord = { replayed: true }
          if (typeof info?.id === "string") raw.messageID = info.id
          postUserIngest(sessionID, text, createdS, raw)
        }
      } else if (role === "assistant" && completedS !== undefined && completedS > sinceSeconds) {
        const text = textOf(entry?.parts)
        const messageID = typeof info?.id === "string" ? info.id : ""
        // The live lane (completion handler / parked sweep) owns a fingerprint
        // the moment it touches the message — replay defers to it, and a
        // successful replay resolves anything still parked.
        if (text && !(messageID && seen(sentAssistant, `${sessionID}:${messageID}`))) {
          postAssistantIngest(sessionID, messageID, info, text, completedS)
          if (messageID) unparkAssistant(sessionID, messageID)
        }
        if (Array.isArray(entry?.parts)) {
          for (const part of entry.parts) {
            if (part && part.type === "tool") {
              const args =
                part.state?.input && typeof part.state.input === "object"
                  ? (part.state.input as JsonRecord)
                  : {}
              postToolIngest(
                sessionID,
                String(part.tool ?? "tool"),
                args,
                stringifyReplayedToolOutput(part),
                completedS,
                { replayed: true },
              )
            }
          }
        }
      }
    }
    reconciledSessions.set(sessionID, true)
  } catch (error) {
    console.debug("mnemoseed-local: reconcile failed:", error)
    debugLog("session reconcile failed", { sessionID, error: String(error) })
  }
}

function stringifyReplayedToolOutput(part: any): string {
  const output = part?.state?.output
  let text: string
  if (typeof output === "string") text = output
  else if (output === undefined || output === null) text = ""
  else {
    try {
      text = JSON.stringify(output)
    } catch {
      text = String(output)
    }
  }
  if (text.length > MAX_TOOL_OUTPUT_CHARS) {
    return `${text.slice(0, MAX_TOOL_OUTPUT_CHARS)}\n[... truncated at ${MAX_TOOL_OUTPUT_CHARS} chars]`
  }
  return text
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
      const stamp = Date.now() / 1000
      // B2.2 lazy reconcile: enqueued FIRST so the replayed host-history tail
      // reaches the daemon strictly before this session's live posts; both
      // legs are fire-and-forget for the handler (T4 hot-path red line).
      enqueueForSession(sessionID, () => reconcileSession(client, sessionID))
      enqueueForSession(sessionID, () => postUserIngest(sessionID, text, stamp, raw))
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
      // B2.2: lazily reconcile this session once per hook process (the
      // session's first event of any kind is the trigger), chained BEFORE
      // the event's own content posts so replayed history lands first.
      const triggerSession = sessionIdOfEvent(event)
      if (triggerSession) enqueueForSession(triggerSession, () => reconcileSession(client, triggerSession))
      switch (event?.type) {
        case "message.updated": {
          const sessionID = sessionIdOfEvent(event)
          if (sessionID) {
            enqueueForSession(sessionID, () => onMessageCompleted(event?.properties?.info))
          } else {
            await onMessageCompleted(event?.properties?.info)
          }
          break
        }
        case "session.idle":
        case "session.error": {
          const sessionID = sessionIdOfEvent(event)
          enqueueForSession(sessionID, () => sweepPendingAssistant(client, sessionID))
          await persistWatermarks()
          enqueueForSession(sessionID, () => flushSession(sessionID))
          break
        }
        case "session.deleted": {
          const sessionID = sessionIdOfEvent(event)
          enqueueForSession(sessionID, () => sweepPendingAssistant(client, sessionID))
          await persistWatermarks()
          enqueueForSession(sessionID, () => settle(sessionID))
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
      void persistWatermarks()
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
      const stamp = Date.now() / 1000
      const toolName = String(hookInput?.tool ?? "")
      const output = stringifyToolOutput(hookOutput?.output)
      enqueueForSession(sessionID, () => reconcileSession(client, sessionID))
      enqueueForSession(sessionID, () => postToolIngest(sessionID, toolName, args, output, stamp, raw))
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
