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
//   provider failure (message/session.error) -> provider_error -> POST /ingest
//   event session.idle|error            -> flush             -> POST /flush
//   event session.deleted               -> session_end       -> POST /session/end
//   experimental.session.compacting     -> flush             -> POST /flush
//   chat.system.transform (first call per session) -> session_recall_read -> POST /session/recent
//   chat.system.transform (armed∧acked user ingest) -> session_recall_pending -> POST /session/recall-pending
//   citation guard in postAssistantIngest -> memory_reinforce -> POST /memory/reinforce
//
// Invariants: HostId is always "opencode"; every daemon call is
// fire-and-forget (the host session is never blocked); every failure is
// swallowed into console.debug UNLESS the opt-in observability lane is armed
// (env MNEMOSEED_LOCAL_DEBUG: failures escalate to console.error + a JSONL
// sink, and every daemon POST's status is inspected — a swallowed non-2xx is
// how the settle-sealing bug hid); requests time out after 2s via
// AbortSignal. The ONLY awaited network calls in the transform handler are
// the session-tails read (bounded by AbortSignal and fail-open, at most once
// per session — B2.1 T1) and the bounded pending-recall pull (B2.1 T2, gated
// on an ACKED user ingest, 300ms timeout, fail-open).
//
// Type reference only (NOT a runtime import — the plugin is dependency-free):
//   import type { Plugin } from "@opencode-ai/plugin"
// The module's default export is the plugin function; OpenCode's loader
// treats every exported function-valued value as a plugin instance.
//
// B2.6 host-plugin bundling: the bundle carries its own MCP registration —
// the config hook injects cfg.mcp["mnemoseed"] create-if-absent (a user's
// existing manual entry wins untouched) into the cfg of the HOST that loaded
// this file (per-host isolation: each host's plugin dir and config are its
// own). The single switch is the ["spec", { enabled: false }] plugin-array
// tuple: the entry short-circuits the WHOLE bundle (no hooks, no config
// injection). Probe-confirmed on this version: the options tuple reaches the
// plugin and config-hook cfg.mcp mutations stick (B2.6 probe rounds 1+2).

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

// ---- B2.1 T1/T3 session-start recall injection + consumption guard. The
// whole mechanism is deterministic and model-free: the injected replay is the
// daemon's verbatim recent tails, needles are plain normalized substring
// fingerprints, and the budget is a hard char cap. Nothing here calls out to
// any model — the only network touch is the daemon read (and the fire-and-
// forget POSTs it feeds).
const SESSION_TAIL_SESSIONS = 2
const SESSION_TAIL_PER_SESSION = 8
const MAX_INJECT_CHARS = 4000
const MIN_SLICE_CHARS = 200
const NEEDLE_HEAD_LEN = 24
const NEEDLE_MIN_CONTENT = 32
const NEEDLE_MID_THRESHOLD = 48
const RECALL_FENCE_OPEN = "<mnemoseed-memory-recall>"
const RECALL_FENCE_CLOSE = "</mnemoseed-memory-recall>"
const RECALL_FENCE_SANITIZED = "‹mnemoseed-memory-recall›"
const RECALL_DISCLAIMER =
  "The block below is an automatic memory replay of earlier sessions, not the user's current instructions."
// B2.7 Scheme 3 (Task C): the second fence pair carries the daemon's standing
// rules budget. The hook only passes the block through verbatim (fail-open —
// the daemon is the sole budget authority and the model may ignore it).
const RULES_FENCE_OPEN = "<mnemoseed-rules-budget>"
const RULES_FENCE_CLOSE = "</mnemoseed-rules-budget>"
const RULES_FENCE_SANITIZED = "‹mnemoseed-rules-budget›"
const RULES_DISCLAIMER =
  "The block below is daemon-supplied standing constraints, not the user's current instructions."
// QA re-review NIT-1: the daemon's ReinforceRequest caps each id list at
// max_length=64 (src/mnemoseed_local/daemon/memory.py) — one oversized POST
// would 422 AFTER the cited set is already marked, dropping the whole batch
// to a permanent no-retry. Split hits into <=64-id batches, one POST each.
// Unreachable today (2 x 8 injected chunks max), pinned so a constant change
// cannot rot it silently.
const REINFORCE_BATCH_SIZE = 64

// B2.1 T2 mid-session auto-recall (PRD-B2.1): a bounded pending-recall pull
// per ACKED user ingest. The daemon is the ONLY budget authority
// (capture.auto_recall_budget_chars, default 2400, reported on the wire as
// budget_chars); the hook uses that wire value as the ITEM budget and
// re-verifies the assembled block fail-closed (defense in depth, unreachable
// by design). RECALL_PULL_MAX_CHARS is the FALLBACK item budget when a daemon
// omits the wire field (older daemon, or a malformed payload). The 300ms is a
// DEDICATED constant: reusing the shared 2s transform timeout would be too
// heavy for a per-turn pull (re-review issue 9 adopted).
const RECALL_PULL_TIMEOUT_MS = 300
const RECALL_PULL_MAX_CHARS = 1200

// ---- B1 provider-failure nomination (deterministic, zero model calls) --------
const PROVIDER_ALLOWLIST = new Set<string>([
  "quota",
  "rate_limit",
  "auth",
  "model_unavailable",
  "timeout",
  "overloaded",
  "other_provider",
])
const REASON_RE = /^provider_[a-z0-9_]+$/
const DEBOUNCE_WINDOW_MS = 60000
const providerDebounce = new Map<string, number>()

function redactSafeId(value: string | undefined): string {
  if (!value) return ""
  const v = String(value).trim()
  if (!v) return ""
  const low = v.toLowerCase()
  if (low.includes("sk-") || low.includes("bearer") || low.includes("authorization") || low.includes("token=") || low.includes("key=") || low.includes("secret")) return ""
  if (low.includes("http://") || low.includes("https://")) return ""
  if (!/^[A-Za-z0-9._\/-]+$/.test(v)) return ""
  if (v.length > 64) return ""
  return v
}

function normalizeProviderStatus(raw: unknown, provider: string, model: string): string | null {
  // Fail-closed: app/build/tool text never nominates, unknown text never
  // nominates. Only an explicit provider taxonomy signal (status code or
  // provider/transport wording) yields a token.
  let s = ""
  if (typeof raw === "number") s = String(raw)
  else if (typeof raw === "string") s = raw
  else if (raw && typeof raw === "object") {
    const r: any = raw
    s = String(r.status ?? r.code ?? r.message ?? "")
  }
  const low = s.toLowerCase()
  // App / build / tool failures are B-type candidates, never provider failure —
  // even when a providerID/modelID happens to be present on the message.
  if (
    low.includes("exit status") ||
    low.includes("exit code") ||
    low.includes("traceback") ||
    low.includes("compilation failed") ||
    low.includes("build error") ||
    low.includes("build failed") ||
    low.includes("build failure") ||
    low.includes("tool failure") ||
    low.includes("tool failed") ||
    low.includes("tool error") ||
    low.includes("command failed") ||
    low.includes("tests failed") ||
    low.includes("test failed") ||
    low.includes("enoent") ||
    low.includes("eacces") ||
    low.includes("npm err") ||
    low.includes("tsc ") ||
    low.includes("eslint")
  )
    return null
  // 410 Gone -> model_unavailable
  if (low.includes("410") || low.includes("gone") || low.includes("eol")) return "model_unavailable"
  if (low.includes("429")) {
    if (low.includes("quota") || low.includes("usage") || low.includes("402") || low.includes("exceeded")) return "quota"
    return "rate_limit"
  }
  if (low.includes("401") || low.includes("403") && low.includes("auth") || low.includes("invalid key") || low.includes("forbidden")) return "auth"
  if (low.includes("402") || low.includes("quota") || low.includes("usage-limit") || low.includes("usage limit")) return "quota"
  if (low.includes("400") || low.includes("404") || low.includes("409")) return "model_unavailable"
  if (low.includes("408") || low.includes("504") || low.includes("timeout") || low.includes("timedout") || low.includes("etimedout") || low.includes("abort")) return "timeout"
  if (low.includes("500") || low.includes("502") || low.includes("503") || low.includes("overloaded") || low.includes("unavailable")) return "overloaded"
  if (!s) return null // blank/ambiguous: fail closed, never a nomination
  if (low.includes("rate")) return "rate_limit"
  if (low.includes("auth")) return "auth"
  if (low.includes("model")) return "model_unavailable"
  if (low.includes("overload")) return "overloaded"
  // other_provider only for provable provider/transport failures, never for
  // unknown or ambiguous text.
  if (
    low.includes("transport") ||
    low.includes("connection") ||
    low.includes("socket") ||
    low.includes("network") ||
    low.includes("econn") ||
    low.includes("enotfound") ||
    low.includes("eai_again") ||
    low.includes("epipe") ||
    low.includes("fetch failed") ||
    low.includes("upstream") ||
    low.includes("gateway") ||
    low.includes("proxy") ||
    low.includes("ssl") ||
    low.includes("tls") ||
    low.includes("certificate")
  )
    return "other_provider"
  return null
}

function reasonForStatus(status: string): string {
  const map: Record<string, string> = {
    quota: "provider_429_quota",
    rate_limit: "provider_429_rate",
    auth: "provider_401",
    model_unavailable: "provider_404",
    timeout: "provider_timeout_no_status",
    overloaded: "provider_5xx",
    other_provider: "provider_transport",
  }
  return map[status] ?? "provider_transport"
}

function shouldDebounce(key: string): boolean {
  const now = Date.now()
  const last = providerDebounce.get(key)
  if (last !== undefined && now - last < DEBOUNCE_WINDOW_MS) return true
  providerDebounce.set(key, now)
  if (providerDebounce.size > DEDUP_CAP) {
    const oldest = providerDebounce.keys().next().value
    if (oldest !== undefined) providerDebounce.delete(oldest)
  }
  return false
}

function buildErrorId(sessionID: string, provider: string, model: string, rawId: unknown, rawError: unknown): string {
  if (typeof rawId === "string" && rawId && rawId.length <= 64 && /^[A-Za-z0-9._\/:-]+$/.test(rawId)) {
    // charset-constrained; secret check
    const low = rawId.toLowerCase()
    if (!low.includes("sk-") && !low.includes("bearer") && !low.includes("token=")) return rawId
  }
  // fallback: bounded deterministic hash of safe slice
  const base = `${provider}:${model}:${sessionID}:${String(rawError ?? rawId ?? "").slice(0, 80)}`
  let h = 0
  for (let i = 0; i < base.length; i++) h = (h * 31 + base.charCodeAt(i)) >>> 0
  return `err_${h.toString(16).slice(0, 16)}`
}

function noteProviderFailure(
  sessionID: string,
  providerRaw: string | undefined,
  modelRaw: string | undefined,
  statusRaw: unknown,
  rawId: unknown,
  rawError: unknown,
): void {
  const provider = redactSafeId(providerRaw)
  if (!provider) return
  const model = redactSafeId(modelRaw) || ""
  const status = normalizeProviderStatus(statusRaw, provider, model)
  if (!status || !PROVIDER_ALLOWLIST.has(status)) return
  const reason = reasonForStatus(status)
  if (!REASON_RE.test(reason) || reason.length > 64) return
  const error_id = buildErrorId(sessionID, provider, model, rawId, rawError)
  // validation: error_id bounded and charset-constrained
  if (error_id.length > 64 || !/^[A-Za-z0-9._\/:-]+$/.test(error_id)) return
  const debounceKey = `${PROFILE_ID}:${sessionID}:${provider}:${model}:${error_id}`
  if (shouldDebounce(debounceKey)) return
  const body: JsonRecord = {
    host: HOST_ID,
    event: "provider_error",
    session_id: sessionID,
    profile_id: PROFILE_ID,
    ts: Date.now() / 1000,
    content: { provider, model: model || undefined, status, reason, error_id },
  }
  const ingestPath = "/ingest"
  post(
    ingestPath,
    body,
    () => debugLog("provider_error nominated", { sessionID, provider, model, status, reason, error_id }),
    () => debugLog("provider_error nack (old daemon or failure)", { sessionID, provider, status }),
  )
}

// ---- R2 provenance trust (design/11): the T2 injected recall carries a
// per-line provenance affix so the model can tell an explicit user pin from an
// automatic capture — G5 trust, no new confidence model. Pin ⇔ source ==
// EXPLICIT_PIN_SOURCE (schema/stamp.py:63, the single comparison). The affix is
// a decoration on the appended line (never part of the verbatim text) and is
// the FIRST budget shed under pressure (design/11 §4.3).
const EXPLICIT_PIN_SOURCE = "memory.remember"
// design/11 §8 copy deck: ASCII/unicode, 9 chars with the leading separator.
const PIN_SUFFIX = " ⟵ pinned"

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

// ---- B2.1 T1/T3 session-start recall injection (attempt-once + consumption
// guard). `injectedSessions` is the per-session attempt gate: marked
// SYNCHRONOUSLY before the first await so concurrent first-turn transforms can
// never double-inject; it never evicts (no seen()-style cap) — the only
// removal is the explicit session.deleted cleanup.
const injectedSessions = new Map<string, true>()
// sessionID -> needle -> chunk ids: the consumption fingerprint registry, one
// entry per INJECTED slice (needles derive from the exact sanitized/sliced
// text that actually entered the block — never from budget-dropped content).
const injectedRegistry = new Map<string, Map<string, Set<string>>>()
// sessionID -> chunk ids already counted as consumed: at most one reinforce
// per chunk per session (bounded FP, PRD-B2.1).
const citedChunks = new Map<string, Set<string>>()

// B2.1 T2: per-session pending-recall gate. ARMED when a user prompt is
// posted (postUserIngest), ACKED when the daemon answered 2xx — the ingest
// handler parks the focal slot before answering, so the ack is a happens-
// before edge (ack-implies-ready). The transform pulls only armed∧acked; a
// non-empty serve clears the flag, an empty or failed pull keeps it armed for
// the next transform (D8).
const pendingPull = new Map<string, { armed: boolean; acked: boolean }>()
// sessionID -> chunk ids the T1 session-start injection served: the T2 pull
// rides them as seen_chunk_ids so a focal hit never re-serves a T1 replay
// (D2 — the daemon merges them into its selection).
const t1InjectedChunkIds = new Map<string, string[]>()

function normalizeRecallText(text: string): string {
  // One role-prefix strip (the verbatim channel labels turns), then collapse
  // whitespace to single spaces and lowercase — needle building and the
  // consumption matcher share this exact shape.
  return String(text)
    .replace(/^(user|assistant|tool|system):\s*/, "")
    .replace(/\s+/g, " ")
    .toLowerCase()
}

function needlesOf(text: string): string[] {
  // Substring fingerprints of the normalized content: a 24-char head window
  // once the content is long enough, plus a centered 24-char window for
  // longer content (a mid-quote citation still matches). JS string length
  // semantics; dedupe via a Set.
  const normalized = normalizeRecallText(text)
  if (normalized.length < NEEDLE_MIN_CONTENT) return []
  const needles = new Set<string>()
  needles.add(normalized.slice(0, NEEDLE_HEAD_LEN))
  if (normalized.length >= NEEDLE_MID_THRESHOLD) {
    const center = Math.floor(normalized.length / 2)
    const start = Math.max(0, center - Math.floor(NEEDLE_HEAD_LEN / 2))
    needles.add(normalized.slice(start, start + NEEDLE_HEAD_LEN))
  }
  return Array.from(needles)
}

function sanitizeRecallText(text: string): string {
  // Fence integrity (TA-5): chunk text may literally carry the fence markers
  // (self-dogfood hits this); replace BOTH literals with the ‹› form in one
  // pass so the assembled block carries exactly one open/close fence pair.
  return text.replaceAll(/<\/?mnemoseed-memory-recall>/g, RECALL_FENCE_SANITIZED)
}

function sanitizeRulesText(text: string): string {
  // Same fence-integrity job for the rules budget block (B2.7): rule content
  // (entity names, etc.) must never inject a second budget fence pair.
  return text.replaceAll(/<\/?mnemoseed-rules-budget>/g, RULES_FENCE_SANITIZED)
}

function buildRulesBudgetInjection(rulesBudget: unknown): string | null {
  // B2.7 Scheme 3 (Task C): the daemon caps the block at ~800 chars; the hook
  // appends it verbatim (JSON) behind the disclaimer, never interpreting it.
  // Absent (undefined) -> no block; explicit null -> no block as well (daemon
  // never emits null, but we distinguish for clarity: null would be an explicit
  // empty budget, undefined is absent).
  if (rulesBudget === undefined) return null
  if (rulesBudget === null) return null
  if (typeof rulesBudget !== "object") return null
  const content = sanitizeRulesText(JSON.stringify(rulesBudget))
  return [RULES_FENCE_OPEN, RULES_DISCLAIMER, content, RULES_FENCE_CLOSE].join("\n")
}

function sessionTailId(group: any): string {
  // The group header's short id: the session_id's last dash-segment (long
  // opencode session ids are unreadable noise in the system prompt).
  const id = typeof group?.session_id === "string" ? group.session_id : ""
  const dash = id.lastIndexOf("-")
  return dash >= 0 ? id.slice(dash + 1) : id
}

function isoEnded(group: any): string {
  const latestAt = typeof group?.latest_at === "number" ? group.latest_at : NaN
  return Number.isFinite(latestAt) ? new Date(latestAt * 1000).toISOString() : ""
}

function escapeAttr(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
}

function groupStarted(group: any): string {
  const first = typeof group?.window?.first === "string" ? group.window.first : ""
  if (!first || group?.window_truncated === true) return ""
  return ` started="${escapeAttr(first)}"`
}

function sessionSelfLine(selfWindow: any): string {
  const first = typeof selfWindow?.window?.first === "string" ? selfWindow.window.first : ""
  const tail = sessionTailId(selfWindow)
  if (!first || !tail) return ""
  return `<session-self id="${escapeAttr(tail)}" started="${escapeAttr(first)}"/>`
}

function registerNeedles(
  registry: Map<string, Set<string>>,
  text: string,
  chunkId: string,
): void {
  if (!chunkId) return
  for (const needle of needlesOf(text)) {
    let ids = registry.get(needle)
    if (ids === undefined) {
      ids = new Set<string>()
      registry.set(needle, ids)
    }
    ids.add(chunkId)
  }
}

function buildRecallInjection(
  groups: any[],
  selfWindow: any,
): { block: string; registry: Map<string, Set<string>>; includedIds: string[] } | null {
  // Accrue newest-first (groups in payload order, chunks within a group
  // reversed) against a char budget whose final assembled block must stay
  // within MAX_INJECT_CHARS INCLUDING the fence, the disclaimer line, the
  // self-anchor line (when present) and the group headers — the wrapper is
  // accounted up front. A boundary chunk keeps only its newest tail slice
  // (prefixed with the "…" marker) when the leftover can still hold
  // MIN_SLICE_CHARS; otherwise it is dropped along with everything older.
  // Dropped chunks register NO needles AND no seen id. Needles derive from
  // the EXACT included (sanitized, possibly sliced) text per chunk;
  // includedIds lists every ADMITTED chunk id (needle or not) — the T2 pull's
  // seen list must match what the session already holds.
  if (!Array.isArray(groups)) return null
  const registry = new Map<string, Set<string>>()
  const includedIds: string[] = []
  const lines: string[] = []
  const selfLine = sessionSelfLine(selfWindow)
  let remaining =
    MAX_INJECT_CHARS -
    (RECALL_FENCE_OPEN.length +
      1 +
      RECALL_DISCLAIMER.length +
      1 +
      (selfLine ? selfLine.length + 1 : 0) +
      RECALL_FENCE_CLOSE.length)
  if (remaining < MIN_SLICE_CHARS) return null
  lines.push(RECALL_FENCE_OPEN, RECALL_DISCLAIMER)
  if (selfLine) lines.push(selfLine)
  let committedGroup = false
  for (const group of groups) {
    const chunks = Array.isArray(group?.chunks) ? [...group.chunks].reverse() : []
    if (chunks.length === 0) continue
    const header = `<session-tail id="${escapeAttr(sessionTailId(group))}" ended="${escapeAttr(isoEnded(group))}"${groupStarted(group)}>`
    const groupFixed = header.length + 1 + "</session-tail>".length + 1
    if (groupFixed > remaining) break
    remaining -= groupFixed
    const groupChunks: string[] = [] // collected newest-first, rendered ascending
    let gotChunk = false
    for (const chunk of chunks) {
      const text = sanitizeRecallText(typeof chunk?.text === "string" ? chunk.text : "")
      if (!text) continue
      const chunkId = typeof chunk?.chunk_id === "string" ? chunk.chunk_id : ""
      const lineCost = text.length + 1
      if (lineCost <= remaining) {
        registerNeedles(registry, text, chunkId)
        if (chunkId) includedIds.push(chunkId)
        groupChunks.push(text)
        remaining -= lineCost
        gotChunk = true
        continue
      }
      const sliceBudget = remaining - 2 // reserve the "…" marker and the newline
      if (sliceBudget < MIN_SLICE_CHARS) break
      const slicedText = text.slice(-sliceBudget)
      registerNeedles(registry, slicedText, chunkId)
      if (chunkId) includedIds.push(chunkId)
      groupChunks.push("…" + slicedText)
      remaining = 0
      gotChunk = true
      break
    }
    if (!gotChunk) break
    lines.push(header)
    for (let i = groupChunks.length - 1; i >= 0; i--) lines.push(groupChunks[i])
    lines.push("</session-tail>")
    committedGroup = true
  }
  if (!committedGroup) return null
  lines.push(RECALL_FENCE_CLOSE)
  const block = lines.join("\n")
  if (block.length > MAX_INJECT_CHARS) return null
  return { block, registry, includedIds }
}

function mergeRegistry(
  target: Map<string, Set<string>>,
  addition: Map<string, Set<string>>,
): void {
  // D7: T2 injections share the SAME needle registry as T1 — later citations
  // of a T2-served chunk reinforce it through the existing consumption guard
  // (TA-6 by construction). Never replaces the T1 entries.
  for (const [needle, ids] of addition) {
    let existing = target.get(needle)
    if (existing === undefined) {
      existing = new Set<string>()
      target.set(needle, existing)
    }
    for (const id of ids) existing.add(id)
  }
}

function buildT2Injection(
  items: any[],
  itemBudget: number,
): { block: string; registry: Map<string, Set<string>> } | null {
  // B2.1 T2 (D7): same fence + disclaimer envelope and same needle channel as
  // T1 — no group headers (the daemon already shaped the payload). The DAEMON
  // is the only budget authority (capture.auto_recall_budget_chars, reported
  // on the wire as budget_chars, default 1200); the hook re-checks the item
  // cost fail-closed against that budget: an oversized line or block is
  // dropped WHOLE (defense in depth, unreachable by design — QA BLOCKER-1: the
  // old fixed cap under-accounted the fence+disclaimer wrapper, silently
  // dropping daemon-legal selections whose item cost landed inside
  // (cap - wrapper, cap]). QA IMPORTANT-3: there is NO slicing floor here —
  // the daemon's _MIN_SLICE_CHARS governs only TAIL-SLICING of a boundary
  // item; full items under ANY positive budget are served and must append.
  if (!Array.isArray(items)) return null
  const wrapper =
    RECALL_FENCE_OPEN.length + 1 + RECALL_DISCLAIMER.length + 1 + RECALL_FENCE_CLOSE.length
  const registry = new Map<string, Set<string>>()
  const lines: string[] = []
  let remaining = itemBudget
  lines.push(RECALL_FENCE_OPEN, RECALL_DISCLAIMER)
  let committed = false
  // design/11 §4.3: the per-line affix is the FIRST budget shed under pressure.
  // `keptAffix` sums the affix cost already committed in `lines`, and
  // `affixIndices` records the ABSOLUTE `lines` positions of the lines that
  // carry it — so a later overrun can refund their cost and rebuild exactly
  // those lines bare instead of dropping a daemon-legal selection (IMPORTANT-1).
  // Indices (not flags) are tracked so a rebuild can never be mis-indexed
  // against `lines[2 + i]` when the affix is shed a second time in one call.
  let keptAffix = 0
  const affixIndices: number[] = []
  for (const item of items) {
    const text = sanitizeRecallText(typeof item?.text === "string" ? item.text : "")
    if (!text) continue
    const chunkId = typeof item?.id === "string" ? item.id : ""
    // R2 provenance: the affix rides ONLY an explicitly-pinned item; a captured
    // (different source) or source-less item is NOT annotated — absence is the
    // captured signal, the most token-lean rendering (design/11 §4.2).
    const pinned = typeof item?.source === "string" && item.source === EXPLICIT_PIN_SOURCE
    const affixLen = pinned ? PIN_SUFFIX.length : 0
    const lineCost = text.length + 1 + affixLen
    if (lineCost > remaining) {
      // design/11 §4.3 drop order: a kept affix must never change item choice
      // semantics — so on an overrun shed EVERY kept affix (refund their cost
      // and rebuild the committed lines bare) BEFORE dropping. Only a line whose
      // BARE cost still exceeds the sheddable-recovered budget stays fail-closed
      // (drops the whole selection, unchanged).
      if (text.length + 1 <= remaining + keptAffix) {
        if (keptAffix) {
          remaining += keptAffix
          keptAffix = 0
          for (const index of affixIndices) {
            lines[index] = lines[index].slice(0, -PIN_SUFFIX.length)
          }
          affixIndices.length = 0
        }
        registerNeedles(registry, text, chunkId)
        lines.push(text)
        remaining -= text.length + 1
        committed = true
        continue
      }
      return null
    }
    registerNeedles(registry, text, chunkId)
    lines.push(pinned ? text + PIN_SUFFIX : text)
    remaining -= lineCost
    keptAffix += affixLen
    if (pinned) affixIndices.push(lines.length - 1)
    committed = true
  }
  if (!committed) return null
  lines.push(RECALL_FENCE_CLOSE)
  const block = lines.join("\n")
  if (block.length > itemBudget + wrapper) return null
  return { block, registry }
}

function pollCandidates(served: any): number {
  // The daemon's above-floor candidate count from the recall-pending payload.
  return typeof served?.non_focal_above_floor === "number" ? served.non_focal_above_floor : 0
}

async function fetchSessionTails(sessionID: string): Promise<any> {
  // The ONE awaited network call in the whole hook (invariant): the daemon
  // read that feeds the injection, bounded by AbortSignal and fail-open.
  try {
    const response = await fetch(BASE_URL + "/session/recent", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        profile_id: PROFILE_ID,
        sessions: SESSION_TAIL_SESSIONS,
        per_session: SESSION_TAIL_PER_SESSION,
        exclude_session_id: sessionID,
        self_session_id: sessionID,
      }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    })
    if (!response.ok) {
      debugLog("session/recent rejected", { sessionID, status: response.status })
      return null
    }
    const data = await response.json()
    if (!Array.isArray(data?.sessions)) {
      debugLog("session/recent malformed", { sessionID })
      return null
    }
    return data
  } catch (error) {
    console.debug("mnemoseed-local: session/recent failed:", error)
    debugLog("session/recent failed", { sessionID, error: String(error) })
    return null
  }
}

async function pullPendingRecall(sessionID: string): Promise<any | null> {
  // B2.1 T2 (D6/D8): the bounded mid-session pull — an awaited fetch with its
  // own 300ms timeout, fail-open (null = nothing to inject, the flag stays
  // armed). The seen list is the session's T1-injected chunk ids (flat,
  // <=16); the daemon merges them into the selection so a focal hit never
  // re-serves a T1 replay. NOT a post() call site — the arity invariants
  // stay untouched.
  try {
    const response = await fetch(BASE_URL + "/session/recall-pending", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        profile_id: PROFILE_ID,
        session_id: sessionID,
        seen_chunk_ids: t1InjectedChunkIds.get(sessionID) ?? [],
      }),
      signal: AbortSignal.timeout(RECALL_PULL_TIMEOUT_MS),
    })
    if (!response.ok) {
      debugLog("session/recall-pending rejected", { sessionID, status: response.status })
      return null
    }
    const data = await response.json()
    if (!Array.isArray(data?.items)) {
      debugLog("session/recall-pending malformed", { sessionID })
      return null
    }
    return data
  } catch (error) {
    console.debug("mnemoseed-local: session/recall-pending failed:", error)
    debugLog("session/recall-pending failed", { sessionID, error: String(error) })
    return null
  }
}

async function onChatSystemTransform(hookInput: any, hookOutput: any): Promise<void> {
  // B2.1 T1 gate semantics (PRD): a falsy session or a non-array system burns
  // NOTHING (the host may invoke the transform with an empty shape on a
  // warmup call — that must not cost the session its first-turn replay); once
  // armed, the attempt is consumed no matter what the read returns. The body
  // is wrapped in try/catch because this is the ONLY handler the host awaits
  // on the model-call path — a fault (out-of-range latest_at in isoEnded, a
  // frozen system array, a malformed payload) must fail open like every
  // sibling: debug-only, never reject the model call, never mutate.
  const sessionID = String(hookInput?.sessionID ?? "")
  if (!sessionID) return
  if (!Array.isArray(hookOutput?.system)) return
  try {
    // T1 (session-start replay) and T2 (mid-session recall) are INDEPENDENT
    // branches: the T1 attempt gate must NEVER early-return past the T2 pull
    // (D8 — the pre-T2 `injectedSessions.has` return silently strangled the
    // whole mid-session lane).
    if (!injectedSessions.has(sessionID)) {
      injectedSessions.set(sessionID, true) // SYNCHRONOUS, before the first await
      const data = await fetchSessionTails(sessionID)
      const built = data !== null ? buildRecallInjection(data.sessions, data.self_window) : null
      // B2.12: one observability line per session-start injection attempt.
      if (DEBUG) {
        debugLog("session-start injection", {
          sessionID,
          groups: Array.isArray(data?.sessions) ? data.sessions.length : 0,
          injectedChars: built !== null ? built.block.length : 0,
          reason:
            built === null
              ? data === null
                ? "session/recent unavailable"
                : "no injectable recall content"
              : undefined,
        })
      }
      if (built !== null) {
        hookOutput.system.push(built.block)
        injectedRegistry.set(sessionID, built.registry)
        // the T2 seen list = every chunk the T1 injection ADMITTED (needle
        // or not): the daemon merges it so a focal hit never re-serves a T1
        // replay. The daemon caps the list at 16 ids; the T1 budget
        // (2 x 8 chunks) stays under it.
        t1InjectedChunkIds.set(sessionID, built.includedIds)
      }
      // B2.7 Scheme 3 (Task C): the standing rules budget rides the same
      // session-start read; append it (absent key -> no block). Independent
      // of the memory-recall block's budget.
      if (data !== null) {
        const rulesBlock = buildRulesBudgetInjection(data.rules_budget)
        if (rulesBlock !== null) hookOutput.system.push(rulesBlock)
      }
    }
    const pull = pendingPull.get(sessionID)
    if (pull !== undefined && pull.armed && pull.acked) {
      // T2: the awaited pull is bounded (300ms) and fail-open; a timeout, a
      // 503 or an empty selection keeps the arm for the next transform —
      // the daemon's slot survives an empty serve (D6).
      const served = await pullPendingRecall(sessionID)
      if (served === null) {
        if (DEBUG) debugLog("recall-pending poll", { sessionID, reason: "pull failed" })
        return
      }
      let injectedChars = 0
      if (served.enabled === true && served.items.length > 0) {
        // QA BLOCKER-1: the daemon's budget_chars IS the item budget — the
        // old fixed cap (RECALL_PULL_MAX_CHARS minus the fence+disclaimer
        // wrapper) silently dropped daemon-legal selections in (1044, 1200].
        // The arm is cleared only AFTER a successful build+append: a dropped
        // block must not burn the arm while the daemon already consumed the
        // slot — the next acked turn re-pulls and slot_consumed on the retry
        // clears it.
        const itemBudget =
          typeof served.budget_chars === "number" && served.budget_chars > 0
            ? served.budget_chars
            : RECALL_PULL_MAX_CHARS
        const built = buildT2Injection(served.items, itemBudget)
        if (built === null) {
          debugLog("recall pull dropped: selection exceeds the item budget", {
            sessionID,
            items: served.items.length,
            budget_chars: served.budget_chars,
          })
          if (DEBUG) {
            debugLog("recall-pending poll", {
              sessionID,
              candidatesAboveFloor: pollCandidates(served),
              injectedChars: 0,
              slotConsumed: served.slot_consumed === true,
              reason: "selection exceeds the item budget",
            })
          }
          return
        }
        hookOutput.system.push(built.block)
        pendingPull.delete(sessionID) // a non-empty serve consumes the flags (D8)
        injectedChars = built.block.length
        let registry = injectedRegistry.get(sessionID)
        if (registry === undefined) {
          registry = new Map<string, Set<string>>()
          injectedRegistry.set(sessionID, registry)
        }
        mergeRegistry(registry, built.registry)
      } else if (served.enabled === true && served.slot_consumed === true) {
        // IMPORTANT-2: a serve whose response was lost in transit must not
        // leave the arm pulling into the void — the daemon already consumed
        // the slot, so this empty answer is terminal; clear the flags.
        pendingPull.delete(sessionID)
      } else if (served.enabled !== true) {
        // enabled:false — the daemon owns the switch; zero append, and the
        // (consumed) slot clears the flag so the next prompt's arm is fresh.
        pendingPull.delete(sessionID)
      }
      // items empty ∧ enabled ∧ not consumed: keep the arm — a fresh pull can
      // still consume the surviving slot (D8).
      // B2.12: one observability line per pending-recall poll outcome.
      if (DEBUG) {
        debugLog("recall-pending poll", {
          sessionID,
          candidatesAboveFloor: pollCandidates(served),
          injectedChars,
          slotConsumed: served.slot_consumed === true,
          armCleared: !pendingPull.has(sessionID),
        })
      }
    }
  } catch (err) {
    console.debug("mnemoseed-local: chat.system.transform failed:", err)
    debugLog("chat.system.transform failed", { sessionID, error: String(err) })
  }
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

function agentOf(info: any): string | undefined {
  return typeof info?.agent === "string" && info.agent ? info.agent : undefined
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

function postUserIngest(
  sessionID: string,
  text: string,
  ts: number,
  raw: JsonRecord,
  agent?: string,
): void {
  // B2.1 T2: posting a user prompt ARMS the mid-session pending-recall pull;
  // the daemon's 2xx is the ACK (ack-implies-ready — the ingest handler parks
  // the focal slot before answering). A rejected/nacked prompt stays armed but
  // un-acked: the transform cannot pull a slot that never existed.
  const pull = pendingPull.get(sessionID) ?? { armed: false, acked: false }
  pull.armed = true
  pull.acked = false
  pendingPull.set(sessionID, pull)
  const body: JsonRecord = {
    host: HOST_ID,
    event: "user_prompt",
    session_id: sessionID,
    profile_id: PROFILE_ID,
    ts,
    content: { text },
    raw,
  }
  if (agent) body.agent = agent
  post(
    "/ingest",
    body,
    () => {
      noteWatermark(sessionID, ts)
      const armed = pendingPull.get(sessionID)
      if (armed !== undefined) armed.acked = true
    },
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
  // B2.1 T3 consumption guard: the reply text just left the host — run the
  // citation check on EVERY ingest of an assistant turn (live, sweep, replay).
  noteConsumption(sessionID, text)
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

function noteConsumption(sessionID: string, text: string): void {
  // B2.1 T3 (TA-6): only an assistant reply that actually CITES an injected
  // slice counts as consumption. The matcher normalization is collapse +
  // lowercase, no role-prefix strip (the reply text is raw). One reinforce
  // per chunk per session; the POST carries NO ack (a usage event is not
  // content — it never advances the replay watermark) and its failure is a
  // debug log only (never scheduleRecovery).
  const registry = injectedRegistry.get(sessionID)
  if (registry === undefined || registry.size === 0) return
  if (!text) return
  const normalized = String(text).replace(/\s+/g, " ").toLowerCase()
  let cited = citedChunks.get(sessionID)
  if (cited === undefined) {
    cited = new Set<string>()
    citedChunks.set(sessionID, cited)
  }
  const hits: string[] = []
  for (const [needle, chunkIds] of registry) {
    if (!normalized.includes(needle)) continue
    for (const chunkId of chunkIds) {
      if (cited.has(chunkId)) continue
      cited.add(chunkId)
      hits.push(chunkId)
    }
  }
  if (hits.length === 0) return
  // NIT-1: batch to the daemon's 64-id cap (REINFORCE_BATCH_SIZE) — one post
  // per batch, so an overflow can never 422 the whole cited set at once.
  for (let i = 0; i < hits.length; i += REINFORCE_BATCH_SIZE) {
    const batch = hits.slice(i, i + REINFORCE_BATCH_SIZE)
    post("/memory/reinforce", { profile_id: PROFILE_ID, chunk_ids: batch }, undefined, () =>
      debugLog("reinforce POST failed", { sessionID, hits: batch }),
    )
  }
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
        typeof info?.time?.completed === "number"
          ? info.time.completed / 1000
          : typeof info?.time?.error === "number"
            ? info.time.error / 1000
            : undefined
      if (role === "user" && createdS !== undefined && createdS > sinceSeconds) {
        const text = textOf(entry?.parts)
        if (text) {
          const raw: JsonRecord = { replayed: true }
          if (typeof info?.id === "string") raw.messageID = info.id
          // The host history carries a per-message agent, so crash-recovery
          // tails keep turn-level attribution like the live lane.
          const agent = agentOf(info)
          if (agent) raw.agent = agent
          postUserIngest(sessionID, text, createdS, raw, agent)
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

export default async function MnemoSeedLocalPlugin(
  input: { client?: unknown },
  options?: unknown,
) {
  // B2.6 single bundle switch: the ["spec", { enabled: false }] plugin-array
  // tuple short-circuits the WHOLE bundle — no hooks, no config injection
  // (the config hook lives in the return object, so returning {} skips it).
  if (
    options !== null &&
    typeof options === "object" &&
    (options as JsonRecord)["enabled"] === false
  ) {
    return {}
  }
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
      // Origin attribution rides the canonical body `agent`; raw.agent stays
      // one transition generation so an older daemon still sees the label.
      const agent = agentOf(hookInput)
      if (agent) raw.agent = agent
      const stamp = Date.now() / 1000
      // B2.2 lazy reconcile: enqueued FIRST so the replayed host-history tail
      // reaches the daemon strictly before this session's live posts; both
      // legs are fire-and-forget for the handler (T4 hot-path red line).
      enqueueForSession(sessionID, () => reconcileSession(client, sessionID))
      enqueueForSession(sessionID, () => postUserIngest(sessionID, text, stamp, raw, agent))
    } catch (error) {
      console.debug("mnemoseed-local: chat.message hook failed:", error)
    }
  }

  async function onMessageCompleted(info: any): Promise<void> {
    if (info?.role !== "assistant") return
    const completedAt = typeof info?.time?.completed === "number" ? info.time.completed : undefined
    const errorAt = typeof info?.time?.error === "number" ? info.time.error : undefined
    // DEBUG: log the completion shape for abort diagnosis (senior QA finding 12b)
    if (DEBUG) {
      debugLog("assistant completion shape", {
        sessionID: info?.sessionID,
        messageID: info?.id,
        role: info?.role,
        hasCompleted: typeof info?.time?.completed === "number",
        completed: info?.time?.completed,
        hasError: !!info?.metadata?.error,
        error: info?.metadata?.error ?? info?.error ?? null,
      })
    }
    // B1 provider-failure nomination gate (message.updated with time.error or metadata.error).
    // Fail-closed: a bare numeric time.error with no error payload never
    // nominates — the actual error payload must be present. The classifier
    // drops app/build/tool text and unknown status (returns null).
    const rawError = info?.metadata?.error ?? info?.error ?? null
    const hasProviderError = rawError != null
    if (hasProviderError) {
      const combined = modelIdOf(info)
      let provider: string | undefined
      let model: string | undefined
      if (combined && combined.includes("/")) {
        const slash = combined.indexOf("/")
        provider = combined.slice(0, slash)
        model = combined.slice(slash + 1)
      } else if (combined) {
        model = combined
        provider = typeof info?.providerID === "string" ? info.providerID : undefined
      } else {
        provider = typeof info?.providerID === "string" ? info.providerID : undefined
        model = typeof info?.modelID === "string" ? info.modelID : undefined
      }
      // The actual error payload/text leads; the numeric time.error stamp is
      // consulted last so it can never shadow a real 429/timeout message.
      const statusRaw = (rawError as any)?.status ?? (rawError as any)?.code ?? (rawError as any)?.message ?? (rawError as any)?.type ?? rawError ?? errorAt
      const sessionIDForError = String(info?.sessionID ?? "")
      if (sessionIDForError) {
        noteProviderFailure(sessionIDForError, provider, model, statusRaw, info?.id, rawError)
      }
    }
    if (completedAt === undefined && errorAt === undefined) return
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
          // B1: session.error may carry provider failure; nominate before flush
          if (event?.type === "session.error") {
            const props: any = (event as any)?.properties ?? {}
            const info: any = props.info ?? props
            const rawError = props.error ?? info?.metadata?.error ?? info?.error ?? props
            const combined = modelIdOf(info)
            let provider: string | undefined
            let model: string | undefined
            if (combined && combined.includes("/")) {
              const slash = combined.indexOf("/")
              provider = combined.slice(0, slash)
              model = combined.slice(slash + 1)
            } else if (combined) {
              model = combined
              provider = typeof info?.providerID === "string" ? info.providerID : undefined
            } else {
              provider = typeof info?.providerID === "string" ? info.providerID : (typeof props.providerID === "string" ? props.providerID : undefined)
              model = typeof info?.modelID === "string" ? info.modelID : (typeof props.modelID === "string" ? props.modelID : undefined)
            }
            const statusRaw = (rawError as any)?.status ?? (rawError as any)?.code ?? (rawError as any)?.message ?? rawError
            const rawId = (info as any)?.id ?? (props as any)?.sessionID ?? sessionID
            if (sessionID) {
              // fire-and-forget nomination; debounce is hook-local
              noteProviderFailure(sessionID, provider, model, statusRaw, rawId, rawError)
            }
          }
          enqueueForSession(sessionID, () => sweepPendingAssistant(client, sessionID))
          await persistWatermarks()
          enqueueForSession(sessionID, () => flushSession(sessionID))
          break
        }
        case "session.deleted": {
          const sessionID = sessionIdOfEvent(event)
          // B2.1 T1/T2/T3 lifecycle cleanup: a settled session's injection
          // gate, consumption registry, cited set, pending-recall arm and T1
          // seen list are dropped with it.
          injectedSessions.delete(sessionID)
          injectedRegistry.delete(sessionID)
          citedChunks.delete(sessionID)
          pendingPull.delete(sessionID)
          t1InjectedChunkIds.delete(sessionID)
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
    // B2.6 bundling: register the daemon's MCP server into the loading host's
    // config. Create-if-absent at BOTH levels — cfg.mcp ??= {} (the map) and
    // mcp["mnemoseed"] ??= {..} (the entry) — so a user's existing manual
    // "mnemoseed" registration (README §MCP gateway) wins untouched. The
    // entry shape matches the A3 README sample plus an explicit enabled flag.
    "config": async (cfg: any): Promise<void> => {
      try {
        if (cfg === null || typeof cfg !== "object") return
        const mcp = (cfg.mcp ??= {}) as Record<string, unknown>
        mcp["mnemoseed"] ??= {
          "type": "local",
          "command": ["mnemoseed-local", "mcp"],
          "enabled": true,
        }
      } catch (error) {
        console.debug("mnemoseed-local: config hook failed:", error)
      }
    },
    "chat.message": async (hookInput: any, hookOutput: any) => onChatMessage(hookInput, hookOutput),
    "chat.system.transform": async (hookInput: any, hookOutput: any) =>
      onChatSystemTransform(hookInput, hookOutput),
    // The bus dispatcher voids this promise: awaiting the parts fetch here
    // never blocks the host.
    event: async ({ event }: { event: unknown }) => onBusEvent(event),
    "tool.execute.after": async (hookInput: any, hookOutput: any) =>
      onToolExecuteAfter(hookInput, hookOutput),
    "experimental.session.compacting": async (hookInput: any) => onSessionCompacting(hookInput),
  }
}
