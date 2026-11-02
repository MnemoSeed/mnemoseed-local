// Minimal behavioral driver for the shipped opencode plugin (B2.2 loose-end:
// regex pins cannot pin BEHAVIOR). Node feeds the bundled plugin a fake V2
// plugin ctx + a recording fetch; scenarios print a JSON transcript for
// pytest to assert on. No daemon, no network, no LLM — everything is canned.

import { pathToFileURL } from "node:url"
import { mkdir, mkdtemp, readFile, readdir, stat, unlink, utimes, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { dirname, join } from "node:path"

const [bundle, scenario, fixtureDirArg] = process.argv.slice(2)
if (!bundle || !scenario) {
  console.error("usage: node hook_driver.mjs <bundle> <scenario> [fixture_dir]")
  process.exit(64)
}

// provider-failure scenarios consume the committed synthetic bus_event
// fixtures; the runner passes the directory explicitly so nothing depends on
// cwd or machine paths.
async function loadFixture(name) {
  if (!fixtureDirArg) throw new Error(`scenario ${scenario} needs a fixture_dir argument`)
  return JSON.parse(await readFile(join(fixtureDirArg, name), "utf8"))
}

// isolation: the plugin roots its artifacts under MNEMOSEED_LOCAL_DATA_DIR
process.env.MNEMOSEED_LOCAL_DATA_DIR = await mkdtemp(join(tmpdir(), "mnemo-hook-test-"))
const WATERMARKS = join(process.env.MNEMOSEED_LOCAL_DATA_DIR, "hook-watermarks.json")

const posts = []
const failing = new Set()
const throwing = new Set()
// The daemon credential lane: the driver seeds the same file the daemon would
// write (inside the plugin's own data dir), so the recorded requests prove the
// plugin attaches the credential header rather than merely promising to.
const CREDENTIAL_PATH = join(
  process.env.MNEMOSEED_LOCAL_DATA_DIR,
  "secrets",
  "daemon.credential.key",
)
globalThis.fetch = async (url, init) => {
  const body = JSON.parse(init.body)
  const accepted = !failing.has(url)
  posts.push({ url, body, headers: init?.headers ?? null, accepted })
  // thrown failures ("network down") take precedence over the 503 lane
  if (throwing.has(url)) throw new Error("network down")
  if (!accepted) return { ok: false, status: 503 }
  if (url.endsWith("/session/recent")) {
    return { ok: true, status: 200, json: async () => recentPayload }
  }
  if (url.endsWith("/session/recall-pending")) {
    return { ok: true, status: 200, json: async () => recallPayload }
  }
  return { ok: true, status: 200 }
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const SES = "sess-behavior"
// outage-hole: after the daemon rejects the live turn, the HOST's own store
// still carries it — the fake listing "learns" it when the driver flips this.
let outageTurnVisible = false

// B2.1 T1/T3: the canned /session/recent payload the fake daemon serves. The
// inject-once/citation-reinforce scenarios swap it in via `recentPayload`.
let recentPayload = { profile_id: "default", sessions: [] }
// B2.1 T2: the canned /session/recall-pending payload (mid-session focal
// selection). Scenarios swap it in via `recallPayload`.
let recallPayload = { enabled: false, items: [], non_focal_above_floor: 0, slot_consumed: false, budget_chars: 1200 }
const RECALL_PAYLOAD = {
  enabled: true,
  items: [
    {
      kind: "chunk",
      id: "c-mid",
      // >= 32 normalized chars: the needle channel (NEEDLE_MIN_CONTENT) must
      // be able to fingerprint it, like a real daemon turn chunk.
      text: "user: 上次中段提到 LanceDb 的接入还在等存储层确认，存储层说这周会给结论",
    },
    {
      kind: "chunk",
      id: "c-mid2",
      text: "assistant: 存储层确认后就可以继续推进",
    },
  ],
  non_focal_above_floor: 1,
  slot_consumed: false,
  budget_chars: 1200,
}
const RECENT_PAYLOAD = {
  profile_id: "default",
  self_window: {
    session_id: "sess-behavior",
    window: { first: "2026-08-19T09:00:00.000Z", latest: "2026-08-19T10:30:00.000Z" },
    chunk_count: 3,
    active: true,
  },
  sessions: [
    {
      session_id: "sess-prev-new",
      latest_at: 40.0,
      window: { first: "2026-08-19T08:00:00.000Z", latest: "2026-08-19T09:00:00.000Z" },
      window_truncated: false,
      chunks: [
        {
          chunk_id: "c-oldstop",
          text: "user: 我们决定把发布窗口固定在每月第一周，并且由小李负责验收，周五之前出终稿",
          ingested_at: 30.0,
          turn_start: 0,
          turn_end: 0,
        },
        {
          chunk_id: "c-fence",
          text: "user: 注入块的围栏用 </mnemoseed-memory-recall> 字面量闭合，构建时记得净化",
          ingested_at: 31.0,
          turn_start: 1,
          turn_end: 1,
        },
        {
          chunk_id: "c-tail",
          text: "assistant: 收到，发布窗口锁定每月第一周，周五终稿",
          ingested_at: 40.0,
          turn_start: 2,
          turn_end: 2,
        },
      ],
    },
    {
      session_id: "sess-prev-old",
      latest_at: 20.0,
      window: { first: "2026-08-18T07:00:00.000Z", latest: "2026-08-18T08:30:00.000Z" },
      window_truncated: false,
      chunks: [
        {
          chunk_id: "c-eval",
          text: "user: 评测臂报告目录固定在 ~/.mnemoseed-local/eval 之下",
          ingested_at: 20.0,
          turn_start: 0,
          turn_end: 0,
        },
        {
          chunk_id: "c-eng",
          text: "user: we decided to lock the release window on the first week",
          ingested_at: 19.0,
          turn_start: 0,
          turn_end: 0,
        },
      ],
    },
  ],
}
// citation-reinforce: the fake host history gains assistant turns that CITE
// the injected content (and one that must NOT — sess-noinject has no
// injection, and m_budget_quote quotes a DROPPED chunk).
let citationTurnsVisible = false
// slice-needle-integrity: the fake host history gains the B-run citation,
// the A-run citation (must NOT reinforce — the A run was never injected) and
// an invented-content citation (must NOT reinforce).
let sliceTurnsVisible = false
// recall-pull-t1-independence: the fake host history gains assistant replies
// that cite the T2-injected chunk c-mid (the second is a re-citation).
let t2CiteTurnsVisible = false
// signal-not-citable: reply quoting the signal line sends no reinforce.
let signalCiteTurnVisible = false

function historyEntries() {
  const entries = [
    {
      // V2 SessionMessageInfo: user text rides the message itself; the V2
      // host record carries no per-message agent on user messages.
      id: "m_old_user",
      type: "user",
      sessionID: SES,
      time: { created: 1_000 },
      text: "历史用户消息",
    },
    {
      id: "m_old_assistant",
      type: "assistant",
      sessionID: SES,
      time: { created: 1_000, completed: 2_000 },
      model: { providerID: "opencode", id: "legacy-model" },
      content: [{ type: "text", text: "历史助手回复" }],
    },
  ]
  if (outageTurnVisible) {
    entries.push({
      id: "m_live_outage",
      type: "user",
      sessionID: SES,
      time: { created: Math.round(Date.now() - 1000) },
      text: "宕机轮",
    })
  }
  if (citationTurnsVisible) {
    entries.push({
      id: "m_cite_1",
      type: "assistant",
      sessionID: SES,
      time: { created: 2_000, completed: 3_000 },
      content: [
        {
          type: "text",
          text: "好的，回顾一下上次的决定：我们决定把发布窗口固定在每月第一周，并且由小李负责验收，周五之前出终稿。我现在就排期。",
        },
      ],
    })
    entries.push({
      id: "m_cite_2",
      type: "assistant",
      sessionID: SES,
      time: { created: 3_000, completed: 4_000 },
      content: [
        {
          type: "text",
          text: "再确认一次：我们决定把发布窗口固定在每月第一周，并且由小李负责验收，周五之前出终稿。",
        },
      ],
    })
    entries.push({
      id: "m_cite_3",
      type: "assistant",
      sessionID: "sess-noinject",
      time: { created: 4_000, completed: 5_000 },
      content: [
        {
          type: "text",
          text: "参考旧决定：我们决定把发布窗口固定在每月第一周，并且由小李负责验收，周五之前出终稿。",
        },
      ],
    })
    entries.push({
      id: "m_budget_quote",
      type: "assistant",
      sessionID: SES,
      time: { created: 5_000, completed: 6_000 },
      content: [{ type: "text", text: "user: " + "甲".repeat(3000) }],
    })
    entries.push({
      id: "m_cite_4",
      type: "assistant",
      sessionID: SES,
      time: { created: 6_000, completed: 7_000 },
      content: [
        {
          type: "text",
          text: "As discussed, WE DECIDED  to   lock the release window...",
        },
      ],
    })
  }
  if (sliceTurnsVisible) {
    entries.push({
      id: "m_slice_1",
      type: "assistant",
      sessionID: "sess-het",
      time: { created: 50_000, completed: 51_000 },
      content: [{ type: "text", text: "B".repeat(30) }],
    })
    entries.push({
      id: "m_slice_2",
      type: "assistant",
      sessionID: "sess-het",
      time: { created: 51_000, completed: 52_000 },
      content: [{ type: "text", text: "A".repeat(30) }],
    })
    entries.push({
      id: "m_slice_3",
      type: "assistant",
      sessionID: "sess-het",
      time: { created: 52_000, completed: 53_000 },
      content: [{ type: "text", text: "丙".repeat(30) }],
    })
  }
  if (t2CiteTurnsVisible) {
    entries.push({
      id: "m_cite_t2_1",
      type: "assistant",
      sessionID: SES,
      time: { created: 7_000, completed: 8_000 },
      content: [
        {
          type: "text",
          text: "上次中段提到 LanceDb 的接入还在等存储层确认，我们继续推进。",
        },
      ],
    })
    entries.push({
      id: "m_cite_t2_2",
      type: "assistant",
      sessionID: SES,
      time: { created: 8_000, completed: 9_000 },
      content: [
        {
          type: "text",
          text: "再确认一次：上次中段提到 LanceDb 的接入还在等存储层确认。",
        },
      ],
    })
  }
  if (signalCiteTurnVisible) {
    entries.push({
      id: "m_sig_cite_1",
      type: "assistant",
      sessionID: SES,
      time: { created: 9_000, completed: 10_000 },
      content: [{ type: "text", text: "[mnemoseed] recalled related memories" }],
    })
  }
  return entries
}

// V2 system arrays carry text parts; the transcripts map them back to plain
// strings so the pytest assertions read the injected block text directly.
function sysTexts(system) {
  if (!Array.isArray(system)) return system
  return system.map((part) => (typeof part === "string" ? part : part?.text ?? ""))
}



async function readWatermarks() {
  // Merged legacy+shard view (max per key): new code keeps the legacy file
  // read-only and converges through per-process shards, so the ack-clock
  // oracle must read the merged maxima, not the legacy bytes alone. The
  // selector is the strict production predicate (legacy exact or final-shard
  // regex), never a loose prefix filter.
  const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
  const merged = {}
  let entries = []
  try {
    entries = await readdir(dataDir)
  } catch {
    return null
  }
  const isValid = (v) => typeof v === "number" && Number.isFinite(v) && v >= 0
  for (const name of entries) {
    if (name.includes(".tmp.")) continue
    if (name !== LEGACY_BASENAME && !FINAL_SHARD_RE.test(name)) continue
    let parsed = null
    try {
      parsed = JSON.parse(await readFile(join(dataDir, name), "utf8"))
    } catch {
      continue
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) continue
    for (const [k, v] of Object.entries(parsed)) {
      if (!isValid(v)) continue
      if (merged[k] === undefined || v > merged[k]) merged[k] = v
    }
  }
  return merged
}

// Independent strict final-shard matcher: byte-identical predicate to the
// production shard selector (legacy exact or
// hook-watermarks.<pid>.<uuid>.json). Decoy names must never match.
const LEGACY_BASENAME = "hook-watermarks.json"
const FINAL_SHARD_RE = /^hook-watermarks\.(\d+)\.([0-9a-fA-F-]{8,})\.json$/

async function readOwnShard(seam) {
  try {
    return JSON.parse(await readFile(seam.shardPath(), "utf8"))
  } catch {
    return null
  }
}

async function main() {
  // V2 driver surface: the bundled plugin default-exports { id, setup } and
  // the named __watermarkTest seam. setup(ctx) registers on this fake
  // ctx; the driver exposes the same callbacks as fire.* emitters so
  // scenarios drive the production handlers under the V2 shapes.
  const pluginModule = await import(pathToFileURL(bundle))
  const definition = pluginModule.default

  const pendingEvents = []
  let wakeWaiters = []
  let aborted = false
  let prevResolve = null
  const wake = () => {
    const waiters = wakeWaiters
    wakeWaiters = []
    for (const waiter of waiters) waiter()
  }
  const eventStream = {
    [Symbol.asyncIterator]() {
      return {
        next: async () => {
          // resolving the PREVIOUS fire here is the completion barrier: the
          // plugin only pulls again after its handler for this event settled
          if (prevResolve) {
            const resolve = prevResolve
            prevResolve = null
            resolve()
          }
          while (pendingEvents.length === 0) {
            if (aborted) return { value: undefined, done: true }
            await new Promise((resolve) => wakeWaiters.push(resolve))
          }
          const item = pendingEvents.shift()
          prevResolve = item.resolve
          return { value: item.event, done: false }
        },
      }
    },
  }

  const makeMcpEditor = (map) => ({
    list: () => Object.entries(map),
    get: (name) => map[name],
    set: (name, config) => {
      map[name] = config
    },
    update: (name, update) => update(map[name]),
    remove: (name) => {
      delete map[name]
    },
  })

  const makeCtx = () => {
    const sessionHooks = new Map()
    const toolHooks = new Map()
    const mcpTransforms = []
    const registrations = []
    const ctx = {
      options: undefined,
      location: { directory: process.cwd() },
      session: {
        hook: async (name, cb) => {
          sessionHooks.set(name, cb)
          registrations.push(`session.hook:${name}`)
          return { dispose: async () => {} }
        },
        context: async ({ sessionID }) =>
          historyEntries().filter((entry) => entry.sessionID === sessionID),
      },
      tool: {
        hook: async (name, cb) => {
          toolHooks.set(name, cb)
          registrations.push(`tool.hook:${name}`)
          return { dispose: async () => {} }
        },
      },
      mcp: {
        transform: async (cb) => {
          mcpTransforms.push(cb)
          registrations.push("mcp.transform")
          return { dispose: async () => {} }
        },
      },
      event: {
          subscribe: () => {
            registrations.push("event.subscribe")
            return eventStream
          },
        },
    }
    return { ctx, sessionHooks, toolHooks, mcpTransforms, registrations }
  }

  let current = null
  const cleanupFns = []
  const activate = async (options = undefined) => {
    const fresh = makeCtx()
    fresh.ctx.options = options
    const cleanup = await definition.setup(fresh.ctx)
    if (typeof cleanup === "function") cleanupFns.push(cleanup)
    current = fresh
    return fresh.registrations
  }

  const fire = {
    // Resolves once the plugin has fully processed this event: a no-op
    // sentinel queued behind it is pulled only after the handler settled.
    event: async (event) => {
      const done = new Promise((resolve) => {
        pendingEvents.push({ event, resolve })
        pendingEvents.push({ event: { type: "session.driver.sentinel", data: {} }, resolve: null })
      })
      wake()
      await done
    },
    prompt: async (event) => {
      const cb = current.sessionHooks.get("prompt")
      if (typeof cb !== "function") throw new Error("prompt hook not registered")
      await cb(event)
    },
    context: async (event) => {
      const cb = current.sessionHooks.get("context")
      if (typeof cb !== "function") throw new Error("context hook not registered")
      await cb(event)
    },
    compaction: async (event) => {
      const cb = current.sessionHooks.get("compaction")
      if (typeof cb !== "function") throw new Error("compaction hook not registered")
      await cb(event)
    },
    toolAfter: async (event) => {
      const cb = current.toolHooks.get("execute.after")
      if (typeof cb !== "function") throw new Error("execute.after hook not registered")
      await cb(event)
    },
    mcp: async (map) => {
      if (map === null || typeof map !== "object") return
      for (const cb of current.mcpTransforms) {
        try {
          await cb(makeMcpEditor(map))
        } catch (error) {
          console.debug("driver: mcp transform failed:", error)
        }
      }
    },
    // A completed assistant reply under the V2 shapes: the text parts land
    // via session.text.ended, the boundary via session.step.ended. The text
    // comes from the same canned session record the reconcile read serves.
    assistantReply: async (id, completed) => {
      const entry = historyEntries().find((candidate) => candidate.id === id)
      const sessionID = entry?.sessionID ?? SES
      const text = (entry?.content ?? [])
        .filter((part) => part.type === "text")
        .map((part) => part.text)
        .join("\n")
      await fire.event({
        type: "session.text.ended",
        data: { sessionID, assistantMessageID: id, ordinal: 0, text },
      })
      await fire.event({
        type: "session.step.ended",
        created: completed,
        data: { sessionID, assistantMessageID: id, finish: "stop" },
      })
    },
  }

  await activate()

  switch (scenario) {
    case "credential-header": {
      // The daemon credential lane: the file the daemon would write is seeded
      // inside the plugin's own data dir, so the recorded requests prove every
      // daemon fetch carries the bearer header. The negative leg clears the
      // file afterwards: without it the header must be absent (the daemon then
      // refuses the request, which is the honest answer for a daemon that
      // never booted).
      await mkdir(dirname(CREDENTIAL_PATH), { recursive: true })
      await writeFile(CREDENTIAL_PATH, "driver-issued-credential\n", "utf8")
      recentPayload = RECENT_PAYLOAD
      recallPayload = RECALL_PAYLOAD
      await fire.context({ sessionID: SES, system: ["BASE"] })
      await fire.prompt({ sessionID: SES, messageID: "m_cred_1", prompt: { text: "带凭据的一轮" } })
      await delay(100)
      await unlink(CREDENTIAL_PATH)
      await fire.prompt({ sessionID: SES, messageID: "m_cred_2", prompt: { text: "没有凭据的一轮" } })
      await delay(100)
      const authorized = posts.filter((post) => post.body.content?.text === "带凭据的一轮")
      const unauthenticated = posts.filter((post) => post.body.content?.text === "没有凭据的一轮")
      console.log(
        JSON.stringify({
          authorized: authorized.map((post) => ({
            url: post.url,
            authorization: post.headers?.authorization ?? post.headers?.Authorization ?? null,
            contentType: post.headers?.["content-type"] ?? null,
          })),
          unauthenticatedCount: unauthenticated.length,
          unauthenticatedAuthorization: unauthenticated.map(
            (post) => post.headers?.authorization ?? post.headers?.Authorization ?? null,
          ),
        }),
      )
      break
    }

    case "ack-watermark": {
      // seed a watermark so reconcile considers the session known
      await writeFile(WATERMARKS, JSON.stringify({ [SES]: 500 }), "utf8")
      // daemon REJECTS the user turn's POST: the mark must NOT advance past it
      failing.add("http://localhost:7788/ingest")
      await fire.prompt({ sessionID: SES, messageID: "m_live_1", prompt: { text: "拒绝轮" } })
      await delay(100)
      await fire.event({ type: "session.idle", data: { sessionID: SES } })
      await delay(50)
      const marksRejected = await readWatermarks()
      // daemon HEALTHY again: the next accepted turn must advance the mark
      failing.clear()
      await fire.prompt({ sessionID: SES, messageID: "m_live_2", prompt: { text: "接受轮" } })
      await delay(100)
      await fire.event({ type: "session.idle", data: { sessionID: SES } })
      await delay(50)
      const marksAccepted = await readWatermarks()
      console.log(JSON.stringify({ posts, marksRejected, marksAccepted }))
      break
    }

    case "replay-before-live": {
      // watermark behind the two history messages -> they replay; the live
      // user prompt must arrive AFTER them, or the segmenter mis-binds turns.
      await writeFile(WATERMARKS, JSON.stringify({}), "utf8")
      await writeFile(WATERMARKS, JSON.stringify({ [SES]: 5 }), "utf8")
      await fire.prompt({ sessionID: SES, messageID: "m_live_1", prompt: { text: "重启后的新消息" } })
      await delay(200) // let the reconcile chain settle
      console.log(
        JSON.stringify({
          order: posts.map((post) => ({
            event: post.body.event,
            text: post.body.content?.text ?? "",
            ts: post.body.ts,
            agent: post.body.agent ?? null,
          })),
        }),
      )
      break
    }

    case "assistant-dedup": {
      // completed assistant lands via message.updated; replay of the SAME
      // message must not double-post it.
      await writeFile(WATERMARKS, JSON.stringify({ [SES]: 0 }), "utf8")
      await fire.assistantReply("m_old_assistant", 3_000)
      await delay(50)
      await fire.event({ type: "session.idle", data: { sessionID: SES } })
      await delay(200)
      const assistantPosts = posts.filter((post) => post.body.event === "assistant_message")
      console.log(JSON.stringify({ assistantCount: assistantPosts.length, texts: assistantPosts.map((p) => p.body.content.text) }))
      break
    }

    case "outage-hole": {
      // re-review IMPORTANT-NEW-1: daemon rejects turn A (outage), turn B is
      // accepted after recovery — the rejected ingest must have re-armed
      // reconciliation IN-PROCESS, so a fresh reconcile replays the hole
      // (host history still holds it) BEFORE B leaps the watermark past it.
      await writeFile(WATERMARKS, JSON.stringify({ [SES]: 500 }), "utf8")
      failing.add("http://localhost:7788/ingest")
      await fire.prompt({ sessionID: SES, messageID: "m_live_outage", prompt: { text: "宕机轮" } })
      await delay(100)
      failing.clear()
      // opencode's own store now carries the outage turn (simulated: the
      // fake session read gains it after the fact — see historyEntries below)
      outageTurnVisible = true
      await fire.prompt({ sessionID: SES, messageID: "m_live_recovery", prompt: { text: "恢复轮" } })
      await delay(100)
      await fire.event({ type: "session.idle", data: { sessionID: SES } })
      await delay(100)
      const outagePosts = posts.filter((post) => post.body.content?.text === "宕机轮")
      const outageAccepted = posts.filter(
        (post) => post.body.content?.text === "宕机轮" && post.accepted,
      ).length
      const recoveryIndex = posts.findIndex((post) => post.body.content?.text === "恢复轮")
      const replayedOutageIndex = posts.findIndex(
        (post) => post.body.content?.text === "宕机轮" && post.accepted,
      )
      console.log(
        JSON.stringify({
          outageAttempts: outagePosts.length,
          outageAccepted,
          replayedOutageIndex,
          recoveryIndex,
        }),
      )
      break
    }

    case "inject-once": {
      // T1: the first valid transform call of a session injects exactly once;
      // invalid shapes burn nothing, repeats burn nothing, and concurrent
      // first calls can never double-inject (attempt gate is synchronous).
      recentPayload = RECENT_PAYLOAD
      await fire.context({ sessionID: SES, system: undefined })
      const o1 = { system: ["BASE SYSTEM PROMPT"] }
      await fire.context({ sessionID: SES, system: o1.system })
      const o2 = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o2.system })
      const c1 = { system: ["BASE1"] }
      const c2 = { system: ["BASE2"] }
      await Promise.all([
        fire.context({ sessionID: "sess-conc", system: c1.system }),
        fire.context({ sessionID: "sess-conc", system: c2.system }),
      ])
      const o3 = { system: ["BASE3"] }
      await fire.context({ sessionID: "sess-other", system: o3.system })
      // IMPORTANT-3 (QA): an empty sessionID must burn NOTHING — the attempt
      // gate is not consumed, so the real session id right after still injects.
      const e1 = { system: ["B"] }
      await fire.context({ sessionID: "", system: e1.system })
      const e2 = { system: ["B2"] }
      await fire.context({ sessionID: "sess-emptyid", system: e2.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(o1.system), sysTexts(o2.system), sysTexts(c1.system), sysTexts(c2.system), sysTexts(o3.system), sysTexts(e1.system), sysTexts(e2.system)],
          recentRequests: posts
            .filter((post) => post.url.endsWith("/session/recent"))
            .map((post) => post.body),
          reinforcePosts: posts.filter((post) => post.url.endsWith("/memory/reinforce")),
        }),
      )
      break
    }

    case "inject-time-windows": {
      // B2.4 T3: the injected block carries the self-anchor line exactly once,
      // inside the fence right after the disclaimer; group headers gain
      // started= only when the group has a window that was not truncated.
      recentPayload = {
        profile_id: "default",
        self_window: {
          session_id: "sess-window",
          window: { first: "2026-08-20T01:00:00.000Z", latest: "2026-08-20T02:00:00.000Z" },
          chunk_count: 1,
          active: true,
        },
        sessions: [
          {
            session_id: "sess-window-full",
            latest_at: 40.0,
            window: { first: "2026-08-19T01:00:00.000Z", latest: "2026-08-19T02:00:00.000Z" },
            window_truncated: false,
            chunks: [
              {
                chunk_id: "w-full",
                text: "user: full window group content",
                ingested_at: 40.0,
                turn_start: 0,
                turn_end: 0,
              },
            ],
          },
          {
            session_id: "sess-window-trunc",
            latest_at: 30.0,
            window: { first: "2026-08-18T01:00:00.000Z", latest: "2026-08-18T02:00:00.000Z" },
            window_truncated: true,
            chunks: [
              {
                chunk_id: "w-trunc",
                text: "user: truncated window group content",
                ingested_at: 30.0,
                turn_start: 0,
                turn_end: 0,
              },
            ],
          },
          {
            session_id: "sess-window-none",
            latest_at: 20.0,
            chunks: [
              {
                chunk_id: "w-none",
                text: "user: no window group content",
                ingested_at: 20.0,
                turn_start: 0,
                turn_end: 0,
              },
            ],
          },
        ],
      }
      const w = { system: ["BASE"] }
      await fire.context({ sessionID: "sess-window", system: w.system })
      console.log(
        JSON.stringify({
          block: sysTexts(w.system)[1],
          recentRequests: posts
            .filter((post) => post.url.endsWith("/session/recent"))
            .map((post) => post.body),
        }),
      )
      break
    }

    case "inject-old-daemon": {
      // B2.4 T3 fallback: a payload with no window/self_window fields renders
      // byte-identical to the pre-feature block — no self line, no started=.
      recentPayload = {
        profile_id: "default",
        sessions: [
          {
            session_id: "sess-old-new",
            latest_at: 40.0,
            chunks: [
              {
                chunk_id: "old-a",
                text: "user: hello world alpha",
                ingested_at: 30.0,
                turn_start: 0,
                turn_end: 0,
              },
              {
                chunk_id: "old-b",
                text: "assistant: hello world beta",
                ingested_at: 40.0,
                turn_start: 0,
                turn_end: 0,
              },
            ],
          },
          {
            session_id: "sess-old-old",
            latest_at: 20.0,
            chunks: [
              {
                chunk_id: "old-c",
                text: "user: hello world gamma",
                ingested_at: 20.0,
                turn_start: 0,
                turn_end: 0,
              },
            ],
          },
        ],
      }
      const d = { system: ["BASE"] }
      await fire.context({ sessionID: "sess-old-daemon", system: d.system })
      console.log(JSON.stringify({ block: sysTexts(d.system)[1] }))
      break
    }

    case "inject-fail-open": {
      // T1 fail-open: a daemon failure (503 OR a thrown network error) leaves
      // the system untouched, and the attempt-once gate still holds — exactly
      // one /session/recent attempt per session.
      recentPayload = RECENT_PAYLOAD
      failing.add("http://localhost:7788/session/recent")
      const sys503 = { system: ["BASE503"] }
      await fire.context({ sessionID: "sess-fail-503", system: sys503.system })
      await fire.context({ sessionID: "sess-fail-503", system: ["BASE503b"] })
      failing.clear()
      throwing.add("http://localhost:7788/session/recent")
      const sysThrow = { system: ["BASEthrow"] }
      await fire.context({ sessionID: "sess-fail-throw", system: sysThrow.system })
      await fire.context({ sessionID: "sess-fail-throw", system: ["BASEthrowb"] })
      const attempts503 = posts.filter(
        (post) =>
          post.url.endsWith("/session/recent") && post.body.exclude_session_id === "sess-fail-503",
      ).length
      const attemptsThrow = posts.filter(
        (post) =>
          post.url.endsWith("/session/recent") && post.body.exclude_session_id === "sess-fail-throw",
      ).length
      // IMPORTANT-1 (QA): the transform is the only handler the host AWAITS on
      // the model-call path — a handler fault must fail open. Healthy payload
      // + a FROZEN system array: the awaited call must resolve and leave the
      // array alone (any throw here would reject the model call).
      recentPayload = RECENT_PAYLOAD
      failing.clear()
      throwing.clear()
      const frozen = { system: Object.freeze(["BASE"]) }
      await fire.context({ sessionID: "sess-freeze", system: frozen.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(sys503.system), sysTexts(sysThrow.system)],
          attempts503,
          attemptsThrow,
          frozenSystem: [...sysTexts(frozen.system)],
        }),
      )
      break
    }

    case "citation-reinforce": {
      // T3: reinforcement lands only when the assistant's own reply text
      // actually cites the injected slice; a re-citation of the same chunk is
      // counted once per session, and a session that never received an
      // injection can never cite one.
      citationTurnsVisible = true
      recentPayload = RECENT_PAYLOAD
      const o = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o.system })
      const injectedBlock = sysTexts(o.system)[1]
      await fire.assistantReply("m_cite_1", 3_000)
      await delay(100)
      await fire.assistantReply("m_cite_2", 4_000)
      await delay(100)
      await fire.event({ type: "session.idle", data: { sessionID: SES } })
      await delay(100)
      await fire.assistantReply("m_cite_3", 5_000)
      await delay(100)
      // IMPORTANT-3 (QA): normalization equivalence — uppercase + irregular
      // spacing in the reply must still cite the English chunk c-eng.
      await fire.assistantReply("m_cite_4", 7_000)
      await delay(100)
      console.log(
        JSON.stringify({
          injectedBlock,
          reinforcePosts: posts.filter((post) => post.url.endsWith("/memory/reinforce")),
        }),
      )
      break
    }

    case "inject-budget": {
      // T1 budget: the char cap drops the OLDEST content (c-big-old's 3000
      // 甲-chars and the older session's chunk), and a dropped chunk is never
      // registered as a needle — quoting it later must produce no reinforce.
      recentPayload = {
        profile_id: "default",
        sessions: [
          {
            session_id: "sess-big",
            latest_at: 50.0,
            chunks: [
              {
                chunk_id: "c-big-old",
                text: "user: " + "甲".repeat(3000),
                ingested_at: 40.0,
                turn_start: 0,
                turn_end: 0,
              },
              {
                chunk_id: "c-big-new",
                text: "assistant: " + "乙".repeat(3900),
                ingested_at: 50.0,
                turn_start: 0,
                turn_end: 0,
              },
            ],
          },
          {
            session_id: "sess-older",
            latest_at: 10.0,
            chunks: [
              {
                chunk_id: "c-older",
                text: "user: 老session的内容",
                ingested_at: 10.0,
                turn_start: 0,
                turn_end: 0,
              },
            ],
          },
        ],
      }
      const o = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o.system })
      const block = sysTexts(o.system)[1]
      recentPayload = {
        profile_id: "default",
        sessions: [
          {
            session_id: "sess-solo",
            latest_at: 60.0,
            chunks: [
              {
                chunk_id: "c-solo",
                text: "user: " + "丙".repeat(9000),
                ingested_at: 60.0,
                turn_start: 0,
                turn_end: 0,
              },
            ],
          },
        ],
      }
      const o2 = { system: ["BASE2"] }
      await fire.context({ sessionID: "sess-solo", system: o2.system })
      const block2 = sysTexts(o2.system)[1]
      citationTurnsVisible = true
      await fire.event({
          type: "message.updated",
          data: {
            info: {
              id: "m_budget_quote",
              role: "assistant",
              sessionID: SES,
              time: { completed: 6_000 },
            },
            sessionID: SES,
          },
        })
      await delay(100)
      console.log(
        JSON.stringify({
          block,
          blockLength: block.length,
          block2Head: block2.slice(0, 80),
          block2,
          block2Length: block2.length,
          reinforcePosts: posts.filter((post) => post.url.endsWith("/memory/reinforce")),
        }),
      )
      break
    }

    case "slice-needle-integrity": {
      // IMPORTANT-2 (QA): heterogeneous boundary slice + dropped-run needle
      // integrity. The boundary chunk (A-run then B-run) is tail-sliced wholly
      // inside the B run; needles must derive from the EXACT included slice,
      // so quoting the A run (never injected) or invented content reinforces
      // nothing — only the actually-injected B run can be cited.
      recentPayload = {
        profile_id: "default",
        sessions: [
          {
            session_id: "sess-het",
            latest_at: 50.0,
            chunks: [
              {
                chunk_id: "c-het-bound",
                text: "assistant: " + "A".repeat(40) + "B".repeat(400),
                ingested_at: 40.0,
                turn_start: 0,
                turn_end: 0,
              },
              {
                chunk_id: "c-het-new",
                text: "assistant: " + "乙".repeat(3500),
                ingested_at: 50.0,
                turn_start: 0,
                turn_end: 0,
              },
            ],
          },
        ],
      }
      const o = { system: ["BASE"] }
      await fire.context({ sessionID: "sess-het", system: o.system })
      const block = sysTexts(o.system)[1]
      sliceTurnsVisible = true
      await fire.assistantReply("m_slice_1", 51_000)
      await delay(100)
      await fire.assistantReply("m_slice_2", 52_000)
      await delay(100)
      await fire.assistantReply("m_slice_3", 53_000)
      await delay(100)
      console.log(
        JSON.stringify({
          block,
          blockLength: block.length,
          reinforcePosts: posts.filter((post) => post.url.endsWith("/memory/reinforce")),
        }),
      )
      break
    }

    case "recall-pull-gating": {
      // T2 gating: the pull fires only after an ACKED user ingest. A
      // transform before any user prompt, and a transform racing the post
      // before its ack microtask, must NOT pull; the armed+acked transform
      // pulls exactly once, injects the selection and clears the flags — a
      // later transform pulls nothing more.
      recallPayload = RECALL_PAYLOAD
      const g1 = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: g1.system })
      const posted = fire.prompt({ sessionID: SES, messageID: "m_pull_1", prompt: { text: "LanceDb 现在什么状态" } })
      await posted // the handler resolves; the ack microtask may not have run
      const g2 = { system: ["BASE2"] }
      await fire.context({ sessionID: SES, system: g2.system })
      await delay(50) // now the ack has landed
      const g3 = { system: ["BASE3"] }
      await fire.context({ sessionID: SES, system: g3.system })
      const g4 = { system: ["BASE4"] }
      await fire.context({ sessionID: SES, system: g4.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(g1.system), sysTexts(g2.system), sysTexts(g3.system), sysTexts(g4.system)],
          pullBodies: posts
            .filter((post) => post.url.endsWith("/session/recall-pending"))
            .map((post) => post.body),
          pullCount: posts.filter((post) => post.url.endsWith("/session/recall-pending")).length,
        }),
      )
      break
    }

    case "recall-pull-empty-rearm": {
      // an empty selection serves nothing but keeps the arm — the next acked
      // user turn re-pulls (the daemon slot may have rotated); never append.
      recallPayload = {
        enabled: true,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 1200,
      }
      const e1 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_e1", prompt: { text: "第一轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: e1.system })
      const e2 = { system: ["BASE2"] }
      await fire.prompt({ sessionID: SES, messageID: "m_e2", prompt: { text: "第二轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: e2.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(e1.system), sysTexts(e2.system)],
          pullCount: posts.filter((post) => post.url.endsWith("/session/recall-pending")).length,
        }),
      )
      break
    }

    case "recall-pull-fail-open": {
      // a failed pull (503 or thrown network) leaves the system untouched AND
      // keeps the arm — the next acked user turn retries; an enabled:false
      // lane serves nothing either.
      recallPayload = RECALL_PAYLOAD
      failing.add("http://localhost:7788/session/recall-pending")
      const f1 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_f1", prompt: { text: "失败轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: f1.system })
      failing.clear()
      const f2 = { system: ["BASE2"] }
      await fire.prompt({ sessionID: SES, messageID: "m_f2", prompt: { text: "恢复轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: f2.system })
      recallPayload = {
        enabled: false,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 1200,
      }
      const f3 = { system: ["BASE3"] }
      await fire.prompt({ sessionID: SES, messageID: "m_f3", prompt: { text: "禁用轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: f3.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(f1.system), sysTexts(f2.system), sysTexts(f3.system)],
          pullCount: posts.filter((post) => post.url.endsWith("/session/recall-pending")).length,
        }),
      )
      break
    }

    case "recall-pull-t1-independence": {
      // T1 and T2 are INDEPENDENT injections: the first transform injects the
      // session-tail block (T1); after a user prompt is posted AND acked, the
      // next transform ALSO pulls the pending-recall selection (T2) — both
      // blocks coexist, and the T1 chunk ids ride the pull as seen_chunk_ids.
      // Consumption: citing the T2-injected chunk reinforces it once per
      // session (a re-citation is suppressed).
      recentPayload = RECENT_PAYLOAD
      recallPayload = RECALL_PAYLOAD
      const i1 = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: i1.system })
      await fire.prompt({ sessionID: SES, messageID: "m_pull_2", prompt: { text: "继续 LanceDb 的话题" } })
      await delay(50)
      // the host hands the transform the ACCUMULATED system array (i1's block
      // is already in it): the T2 block must COEXIST with the T1 block, never
      // replace or re-inject it. The copy keeps the transcript snapshot of
      // i1.system honest (a fresh array per call, like the real host).
      const i2 = { system: [...sysTexts(i1.system)] }
      await fire.context({ sessionID: SES, system: i2.system })
      t2CiteTurnsVisible = true
      await fire.assistantReply("m_cite_t2_1", 8_000)
      await delay(100)
      await fire.assistantReply("m_cite_t2_2", 9_000)
      await delay(100)
      console.log(
        JSON.stringify({
          systems: [sysTexts(i1.system), sysTexts(i2.system)],
          pullBodies: posts
            .filter((post) => post.url.endsWith("/session/recall-pending"))
            .map((post) => post.body),
          reinforcePosts: posts.filter((post) => post.url.endsWith("/memory/reinforce")),
        }),
      )
      break
    }

    case "recall-pull-budget-equality": {
      // QA BLOCKER-1 + NIT-7: the hook's item budget is the DAEMON's WIRE
      // budget_chars, never a hardcoded cap. A daemon-legal selection whose
      // item cost lands inside (1200 - wrapper, 2000] must still be appended
      // — a mutant hardcoding the old 1200 cap drops it whole. Items total
      // exactly 1902 chars of item cost (950+1 + 950+1); the assembled block
      // must be 2058 chars (156 wrapper + 1902) — pinned EXACTLY.
      recallPayload = {
        enabled: true,
        items: [
          { kind: "chunk", id: "c-big1", text: "x".repeat(950) },
          { kind: "chunk", id: "c-big2", text: "y".repeat(950) },
        ],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 2000,
      }
      const b1 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_budget", prompt: { text: "预算轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: b1.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(b1.system)],
          blockLength: sysTexts(b1.system).length > 1 ? sysTexts(b1.system)[1].length : 0,
        }),
      )
      break
    }

    case "recall-pull-low-budget": {
      // QA IMPORTANT-3: the daemon owns the WHOLE positive-int budget range —
      // a budget below the T1 slice floor (200) still serves full items whose
      // cost fits; the hook must append them instead of re-imposing a slicing
      // floor it does not own. One 100-char item (101 item cost) under
      // budget_chars:150; block = 156 wrapper + 101 = 257 — pinned exactly.
      recallPayload = {
        enabled: true,
        items: [{ kind: "chunk", id: "c-low", text: "x".repeat(100) }],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 150,
      }
      const b1 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_lowbudget", prompt: { text: "低预算轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: b1.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(b1.system)],
          blockLength: sysTexts(b1.system).length > 1 ? sysTexts(b1.system)[1].length : 0,
        }),
      )
      break
    }

    case "recall-pull-slot-consumed": {
      // QA IMPORTANT-2: the daemon answers that the slot was ALREADY consumed
      // (an earlier serve whose response was lost in transit): the transform
      // must clear the arm — a later transform must not keep pulling into the
      // void (endless empty pulls), and the system stays untouched.
      recallPayload = {
        enabled: true,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: true,
        budget_chars: 1200,
      }
      const s1 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_consumed", prompt: { text: "已消费轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: s1.system })
      const s2 = { system: ["BASE2"] }
      await fire.context({ sessionID: SES, system: s2.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(s1.system), sysTexts(s2.system)],
          pullCount: posts.filter((post) => post.url.endsWith("/session/recall-pending")).length,
        }),
      )
      break
    }

    case "recall-pull-pinmarkers": {
      // R2 provenance (design/11 §11 T2): a served item whose source IS the
      // explicit pin source (memory.remember) gains the `⟵ pinned` line affix;
      // captured and source-less items carry NO affix — absence = captured.
      recallPayload = {
        enabled: true,
        items: [
          {
            kind: "chunk",
            id: "c-pin",
            source: "memory.remember",
            text: "user: 这条是用户自己钉的偏好，零拷贝数据路径优先于重复",
          },
          {
            kind: "chunk",
            id: "c-cap",
            source: "capture.auto",
            text: "assistant: 存储层确认后就可以继续推进",
          },
          {
            kind: "chunk",
            id: "c-plain",
            text: "assistant: 没有 source 字段的抓取项",
          },
        ],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 1200,
      }
      const p1 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_pin", prompt: { text: "偏好的事" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: p1.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(p1.system)],
          block: sysTexts(p1.system).length > 1 ? sysTexts(p1.system)[1] : "",
        }),
      )
      break
    }

    case "recall-pull-pin-budget": {
      // R2 provenance budget discipline (design/11 §4.3): under a tight item
      // budget the per-line affix is shed FIRST — the bare verbatim line still
      // appends and the block-level fail-closed check holds; a pinned line that
      // exceeds the budget even bare drops the WHOLE selection unchanged.
      const a = { system: ["BASE"] }
      recallPayload = {
        enabled: true,
        items: [
          {
            kind: "chunk",
            id: "c-pin-bnd",
            source: "memory.remember",
            text: "z".repeat(149), // bare line cost 150 == budget; with affix 159 > 150
          },
        ],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 150,
      }
      await fire.prompt({ sessionID: SES, messageID: "m_pinbnd", prompt: { text: "紧预算轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: a.system })
      const b = { system: ["BASE2"] }
      recallPayload = {
        enabled: true,
        items: [
          {
            kind: "chunk",
            id: "c-over",
            source: "memory.remember",
            text: "w".repeat(1201), // bare line cost 1202 > budget 1200
          },
        ],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 1200,
      }
      await fire.prompt({ sessionID: SES, messageID: "m_pinover", prompt: { text: "超预算轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: b.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(a.system), sysTexts(b.system)],
          blockLength: sysTexts(a.system).length > 1 ? sysTexts(a.system)[1].length : 0,
        }),
      )
      break
    }

    case "recall-pull-pin-shed-kept-affix": {
      // IMPORTANT-1 QA repro (design/11 §4.3): a KEPT affix on an EARLY pinned
      // line must never squeeze a LATER captured line out. Budget 100, [pinned
      // A("a"*50)+affix, captured B("b"*41)]: A affixed 60<=100 (rem 40), then
      // B 42>40 — the old code returned null (whole drop) though BARE A+B=93
      // <=100. The kept affix is shed first so the daemon-legal selection is
      // SERVED; fail-closed only when the bare total exceeds budget.
      const a = { system: ["BASE"] }
      recallPayload = {
        enabled: true,
        items: [
          { kind: "chunk", id: "c-pin-a", source: "memory.remember", text: "a".repeat(50) },
          { kind: "chunk", id: "c-cap-b", source: "capture.auto", text: "b".repeat(41) },
        ],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 100,
      }
      await fire.prompt({ sessionID: SES, messageID: "m_shed_a", prompt: { text: "紧并排轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: a.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(a.system)],
          block: sysTexts(a.system).length > 1 ? sysTexts(a.system)[1] : "",
        }),
      )
      break
    }

    case "recall-pull-pin-post-shed-bare-accounting": {
      // NIT-2 mutation guard (TS half): the shed branch must decrement
      // `remaining` by the BARE line cost. Budget 100, [captured Z("z"*89)→rem
      // 10, pinned X("x"*5): affixed 15>10∪bare 6<=10 → shed rem 10-6=4,
      // captured Y("y"*3): cost 4<=4 served]. If the shed branch used the
      // affixed 15 instead, remaining→-5 and Y(4) overruns → null.
      const a = { system: ["BASE"] }
      recallPayload = {
        enabled: true,
        items: [
          { kind: "chunk", id: "c-cap-z", source: "capture.auto", text: "z".repeat(89) },
          { kind: "chunk", id: "c-pin-x", source: "memory.remember", text: "x".repeat(5) },
          { kind: "chunk", id: "c-cap-y", source: "capture.auto", text: "y".repeat(3) },
        ],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 100,
      }
      await fire.prompt({ sessionID: SES, messageID: "m_shed_x", prompt: { text: "裸计账轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: a.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(a.system)],
          block: sysTexts(a.system).length > 1 ? sysTexts(a.system)[1] : "",
        }),
      )
      break
    }

    case "recall-pull-pin-two-sheds": {
      // BLOCKER (TS half, verbatim corruption): shedding the affix a SECOND time
      // in one call must rebuild every kept-affix line by its ABSOLUTE line
      // index. The old affixFlags reset + `lines[2 + i]` re-enumeration sliced 9
      // chars off the WRONG already-bare committed line after the first shed.
      // Budget 2400, [pinned P1("a"*1800), P2("b"*582), P3("c"*6), P4("e"*1)]:
      // P1 affixed 1810<=2400 commits, P2 affixed 592>rem 590 sheds P1's affix,
      // P3 (memory.remember) affixed 16<=rem 16 commits a fresh kept affix, P4
      // affixed 11>rem 0 sheds it a SECOND time → P1 must stay UNCHANGED at
      // length 1800 and P3's affix must be shed.
      const a = { system: ["BASE"] }
      recallPayload = {
        enabled: true,
        items: [
          { kind: "chunk", id: "c-p1", source: "memory.remember", text: "a".repeat(1800) },
          { kind: "chunk", id: "c-p2", source: "memory.remember", text: "b".repeat(582) },
          { kind: "chunk", id: "c-p3", source: "memory.remember", text: "c".repeat(6) },
          { kind: "chunk", id: "c-p4", source: "memory.remember", text: "e".repeat(1) },
        ],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 2400,
      }
      await fire.prompt({ sessionID: SES, messageID: "m_two_sheds", prompt: { text: "二次剥离轮" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: a.system })
      console.log(
        JSON.stringify({
          systems: [sysTexts(a.system)],
          block: sysTexts(a.system).length > 1 ? sysTexts(a.system)[1] : "",
        }),
      )
      break
    }

    case "rules-budget": {
      // B2.7 Task C: the daemon's /session/recent read supplies a rules_budget
      // block — the transform appends the SECOND fence pair (independent of
      // the memory-recall block), with the standing-constraints disclaimer.
      recentPayload = {
        profile_id: "default",
        self_window: null,
        sessions: [],
        rules_budget: {
          auto_recall_focal_floor: 0.4,
          auto_recall_budget_chars: 1200,
          exclude_entities: ["secret1"],
          entity_boost: { boosted: 2.0 },
          time_window_turns: 20,
          budget_consumed: 0,
        },
      }
      const o1 = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o1.system })
      console.log(JSON.stringify({ systems: [sysTexts(o1.system)] }))
      break
    }

    case "rules-budget-sanitize": {
      // fence sanitization: rule entity contains the literal closing fence
      recentPayload = {
        profile_id: "default",
        self_window: null,
        sessions: [],
        rules_budget: {
          auto_recall_focal_floor: 0.4,
          auto_recall_budget_chars: 1200,
          exclude_entities: ["a</mnemoseed-rules-budget>b"],
          entity_boost: {},
          time_window_turns: 20,
          budget_consumed: 0,
        },
      }
      const o1 = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o1.system })
      console.log(JSON.stringify({ systems: [sysTexts(o1.system)] }))
      break
    }

    case "completion-shape-debug": {
      // the DEBUG-gated shape lane: aborted assistant messages (no
      // time.completed) are logged into hook-debug.jsonl BEFORE the gate
      // drops them — with and without host error fields.
            await fire.event({
        type: "session.step.failed",
        created: 42,
        data: {
          sessionID: SES,
          assistantMessageID: "m_aborted_meta",
          error: { type: "abort", message: "The operation was aborted due to timeout" },
        },
      })
      await delay(100)
            await fire.event({
        type: "session.step.failed",
        created: 42,
        data: {
          sessionID: SES,
          assistantMessageID: "m_aborted_plain",
          error: { type: "abort", message: "The operation was aborted" },
        },
      })
      await delay(100)
      const raw = await readFile(
        join(process.env.MNEMOSEED_LOCAL_DATA_DIR, "hook-debug.jsonl"),
        "utf8",
      ).catch(() => "")
      const shapes = raw
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line))
        .filter((line) => line.tag === "assistant step shape")
        .map((line) => line.payload)
      console.log(JSON.stringify({ shapes }))
      break
    }

    case "debug-lane-recall": {
      // B2.12: one DEBUG line per session-start injection attempt and one per
      // pending-recall poll — read back out of the hook-debug.jsonl sink.
      recentPayload = RECENT_PAYLOAD
      recallPayload = RECALL_PAYLOAD
      const d1 = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: d1.system })
      await fire.prompt({ sessionID: SES, messageID: "m_dbg", prompt: { text: "调试轮" } })
      await delay(50)
      const d2 = { system: ["BASE2"] }
      await fire.context({ sessionID: SES, system: d2.system })
      await delay(100)
      const raw = await readFile(
        join(process.env.MNEMOSEED_LOCAL_DATA_DIR, "hook-debug.jsonl"),
        "utf8",
      ).catch(() => "")
      const events = raw
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line))
      console.log(JSON.stringify({ events }))
      break
    }

    case "hook-debug-rotate": {
      // An oversized opt-in sink rotates once to .1 at append time
      // and the triggering line still lands in the fresh file.
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const logPath = join(dataDir, "hook-debug.jsonl")
      const archivePath = join(dataDir, "hook-debug.jsonl.1")
      await mkdir(dataDir, { recursive: true })
      const cap = 10 * 1024 * 1024
      const oversized = "x".repeat(cap + 4096)
      await writeFile(logPath, oversized, "utf8")
      await fire.assistantReply("m_rot_1", 1000)
      await delay(150)
      const archived = await readFile(archivePath, "utf8").catch(() => "")
      const fresh = await readFile(logPath, "utf8").catch(() => "")
      console.log(
        JSON.stringify({
          rotated: archived.length > 0,
          archivedBytes: archived.length,
          capBytes: cap,
          freshHasLine: fresh.includes("assistant step shape"),
          freshBytes: fresh.length,
        }),
      )
      break
    }

    case "hook-debug-no-rotate": {
      // A below-cap sink is appended in place — no .1 invented.
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const logPath = join(dataDir, "hook-debug.jsonl")
      const archivePath = join(dataDir, "hook-debug.jsonl.1")
      await mkdir(dataDir, { recursive: true })
      await writeFile(logPath, "seed\n", "utf8")
      await fire.assistantReply("m_norot_1", 1000)
      await delay(150)
      const archiveExists = await stat(archivePath).then(
        () => true,
        () => false,
      )
      const fresh = await readFile(logPath, "utf8").catch(() => "")
      console.log(
        JSON.stringify({
          rotated: archiveExists,
          linePresent: fresh.includes("assistant step shape"),
        }),
      )
      break
    }

    case "hook-debug-rotate-replace": {
      // One generation only — a pre-existing .1 is replaced.
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const logPath = join(dataDir, "hook-debug.jsonl")
      const archivePath = join(dataDir, "hook-debug.jsonl.1")
      const secondArchive = join(dataDir, "hook-debug.jsonl.2")
      await mkdir(dataDir, { recursive: true })
      await writeFile(archivePath, "old-generation", "utf8")
      const cap = 10 * 1024 * 1024
      const oversized = "x".repeat(cap + 4096)
      await writeFile(logPath, oversized, "utf8")
      await fire.assistantReply("m_rot_rep_1", 1000)
      await delay(150)
      const archiveBytes = await readFile(archivePath, "utf8").catch(() => "")
      const secondExists = await stat(secondArchive).then(
        () => true,
        () => false,
      )
      const fresh = await readFile(logPath, "utf8").catch(() => "")
      console.log(
        JSON.stringify({
          replaced: archiveBytes.length > "old-generation".length,
          noSecondGeneration: !secondExists,
          freshHasLine: fresh.includes("assistant step shape"),
        }),
      )
      break
    }

    case "hook-debug-rotate-concurrent": {
      // Two debug emissions launched against an oversized sink WITHOUT awaiting
      // between them must serialize through the sink chain: exactly one rotation, both
      // lines land in the fresh file, and no .2 generation is invented. A dropped
      // chain would let both observes see the oversized file and double-rotate — the
      // second rename clobbers the archive with the freshly-truncated file.
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const logPath = join(dataDir, "hook-debug.jsonl")
      const archivePath = join(dataDir, "hook-debug.jsonl.1")
      const secondArchive = join(dataDir, "hook-debug.jsonl.2")
      await mkdir(dataDir, { recursive: true })
      const cap = 10 * 1024 * 1024
      const oversized = "x".repeat(cap + 4096)
      await writeFile(logPath, oversized, "utf8")
      await Promise.all([
        fire.assistantReply("m_conc_1", 1000),
        fire.assistantReply("m_conc_2", 2000),
      ])
      await delay(200)
      const archiveBytes = await readFile(archivePath, "utf8").catch(() => "")
      const archiveHoldsPreRotation = archiveBytes.length === oversized.length && archiveBytes === oversized
      const secondExists = await stat(secondArchive).then(
        () => true,
        () => false,
      )
      const fresh = await readFile(logPath, "utf8").catch(() => "")
      const shapeLines = fresh
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line))
        .filter((l) => l.tag === "assistant step shape" || (l.payload && l.payload.tag === "assistant step shape"))
      const bothLines = shapeLines.length >= 2
      console.log(
        JSON.stringify({
          rotated: archiveHoldsPreRotation,
          bothLines,
          noSecondGeneration: !secondExists,
          freshStillSmall: fresh.length < cap,
          shapeCount: shapeLines.length,
        }),
      )
      break
    }

    case "config-inject": {
      // B2.6: the config hook registers cfg.mcp["mnemoseed"] create-if-absent
      // — an empty cfg, a cfg without the mcp map, and a cfg carrying a manual
      // registration (which must win untouched); a null cfg is a no-op.
      const empty = { mcp: {} }
      await fire.mcp(empty.mcp)
      const bare = { mcp: {} }
      await fire.mcp(bare.mcp)
      const manual = {
        mcp: { mnemoseed: { type: "remote", url: "http://mcp.example", headers: { "X-Custom": "1" } } },
      }
      await fire.mcp(manual.mcp)
      // a null editor map is a no-op (the callback guards the shape)
      await fire.mcp(null)
      const noThrow = true
      console.log(JSON.stringify({ empty, bare, manual, noThrow }))
      break
    }

    case "config-inject-migrate": {
      // The one rewrite: the factory stdio entry this bundle's older versions
      // injected (command == ["mnemoseed-local","mcp"]) becomes the remote HTTP
      // entry, preserving the user's enabled flag. A foreign stdio entry and a
      // manual remote entry are never touched.
      const factory = {
        mcp: { mnemoseed: { type: "local", command: ["mnemoseed-local", "mcp"], enabled: true } },
      }
      await fire.mcp(factory.mcp)
      const disabled = {
        mcp: { mnemoseed: { type: "local", command: ["mnemoseed-local", "mcp"], enabled: false } },
      }
      await fire.mcp(disabled.mcp)
      const foreign = {
        mcp: { mnemoseed: { type: "local", command: ["other-tool", "mcp"], enabled: true } },
      }
      await fire.mcp(foreign.mcp)
      const manual = { mcp: { mnemoseed: { type: "remote", url: "http://mcp.example" } } }
      await fire.mcp(manual.mcp)
      console.log(JSON.stringify({ factory, disabled, foreign, manual }))
      break
    }

    case "config-inject-credential": {
      // The remote entry reads the daemon credential file at config-hook time:
      // present -> the authorization header rides the entry; absent -> the
      // entry still injects (the daemon refuses it honestly).
      await mkdir(dirname(CREDENTIAL_PATH), { recursive: true })
      await writeFile(CREDENTIAL_PATH, "driver-issued-credential\n", "utf8")
      const seeded = { mcp: {} }
      await fire.mcp(seeded.mcp)
      await unlink(CREDENTIAL_PATH)
      const bare = { mcp: {} }
      await fire.mcp(bare.mcp)
      console.log(JSON.stringify({ seeded, bare }))
      break
    }

    case "config-inject-frozen": {
      // B2.6 I3: the config hook must be fail-open on frozen objects — a frozen
      // cfg and a frozen cfg.mcp must not throw, and must not overwrite other
      // keys. Both cases exercise the ??= paths with Object.freeze.
      const noThrow = { value: true }
      let frozenCfgOther = null
      let frozenCfgMcp = null
      let frozenCfgHasMnemoseed = null
      let frozenMcpOther = null
      let frozenMcpHasMnemoseed = null
      try {
        // a frozen mcp map: the editor's set throws inside the transform
        // callback, which must surface as a swallowed fail-open, not a throw
        const frozenMcpMap = Object.freeze({ otherMcp: "keep1" })
        const frozenCfg = { other: "keep", mcp: frozenMcpMap }
        await fire.mcp(frozenCfg.mcp)
        frozenCfgOther = frozenCfg.other
        frozenCfgMcp = frozenCfg.mcp
        frozenCfgHasMnemoseed = frozenCfg.mcp?.mnemoseed ?? null
      } catch {
        noThrow.value = false
      }
      try {
        const frozenMcp = { other: "keep2", mcp: Object.freeze({ otherMcp: "keep2", mnemoseed: { type: "remote", url: "http://frozen.example" } }) }
        await fire.mcp(frozenMcp.mcp)
        frozenMcpOther = frozenMcp.other
        frozenMcpHasMnemoseed = frozenMcp.mcp?.mnemoseed ?? null
      } catch {
        noThrow.value = false
      }
      // also check that a normal map still works after frozen attempts
      const after = { other: "keep3", mcp: {} }
      try {
        await fire.mcp(after.mcp)
      } catch {
        noThrow.value = false
      }
      console.log(
        JSON.stringify({
          noThrow: noThrow.value,
          frozenCfgOther,
          frozenCfgMcp,
          frozenCfgHasMnemoseed,
          frozenMcpOther,
          frozenMcpHasMnemoseed,
          afterMnemoseed: after.mcp?.mnemoseed,
        }),
      )
      break
    }

    case "switch-short-circuit": {
      // B2.6: the {enabled:false} options entry short-circuits the WHOLE
      // bundle — setup registers nothing (no mcp transform, no hooks, no
      // event subscription); enabled:true, empty options and absent options
      // all register the full bundle.
      const off = await activate({ enabled: false })
      const on = await activate({ enabled: true })
      const bare = await activate({})
      const none = await activate()
      console.log(
        JSON.stringify({
          offKeys: off,
          onKeys: on,
          bareKeys: bare,
          noneKeys: none,
        }),
      )
      break
    }

    case "provider-error-nomination": {
      // B1 joined seam: the REAL bundled plugin sees the committed synthetic
      // V2 event fixtures; the transcript carries the exact POST bodies the
      // daemon must accept. True 429 (with a numeric status on the structured
      // error) must nominate quota; a status-less abort must nominate timeout;
      // a build failure with provider/model tracked must emit NOTHING.
      const fx429 = await loadFixture("provider_error_429.json")
      const fxTimeout = await loadFixture("provider_error_timeout_no_status.json")
      const fxBuild = await loadFixture("provider_build_error_negative.json")
      await fire.event(fx429.session_event)
      await fire.event(fx429.bus_event)
      await delay(100)
      await fire.event(fxTimeout.session_event)
      await fire.event(fxTimeout.bus_event)
      await delay(100)
      const beforeBuild = posts.filter((post) => post.body.event === "provider_error").length
      await fire.event(fxBuild.bus_event)
      await delay(100)
      const afterBuild = posts.filter((post) => post.body.event === "provider_error").length
      console.log(JSON.stringify({ posts, buildErrorPosts: afterBuild - beforeBuild }))
      break
    }

    case "provider-error-storm": {
      // Issue #167 behavioral storm oracle over the REAL bundled plugin: the
      // same profile/session/provider/model/error_id replayed with an
      // ALTERNATE normalized status (quota vs the committed timeout payload) is
      // debounced to ONE nomination, while a DISTINCT error_id in the same
      // storm is independently nominated — exactly two provider_error POSTs.
      // Kills a removed-debounce mutant (three posts), a key missing error_id
      // (one post), and a key incorrectly including status (three posts).
      const fx429 = await loadFixture("provider_error_429.json")
      const fxTimeout = await loadFixture("provider_error_timeout_no_status.json")
      const alternateError = fxTimeout.bus_event.data.error.message
      const base = JSON.parse(JSON.stringify(fx429.bus_event))
      const alt = JSON.parse(JSON.stringify(fx429.bus_event))
      alt.data.error.message = alternateError
      const distinct = JSON.parse(JSON.stringify(fx429.bus_event))
      distinct.data.assistantMessageID = "msg_storm_b"
      distinct.data.error.message = alternateError
      await fire.event(fx429.session_event)
      await fire.event(base)
      await delay(100)
      await fire.event(alt)
      await delay(100)
      await fire.event(distinct)
      await delay(150) // let the fire-and-forget provider_error POSTs land
      console.log(JSON.stringify({ posts }))
      break
    }

    case "provider-error-secret": {
      // B1 M4: secret-bearing raw hook input must reach the plugin
      // normalization/redaction and leave NO secret bytes in the POST body.
      // The message still carries a real 429 signal so it nominates.
      await fire.event({
        type: "session.model.selected",
        data: { sessionID: "sess-b1-secret", model: { providerID: "openai", id: "gpt-4o" } },
      })
      await fire.event({
        type: "session.step.failed",
        created: 789,
        data: {
          sessionID: "sess-b1-secret",
          assistantMessageID: "m-b1-secret",
          error: {
            type: "APIError",
            message:
              "HTTP 429: rate limit exceeded for key sk-live-abcdef1234567890 " +
              "(Bearer abcdef, token=hunter2, see https://api.provider.example/v1/keys)",
          },
        },
      })
      await delay(100)
      console.log(JSON.stringify({ posts }))
      break
    }

    case "provider-error-advisory": {
      // B2 M-B2-1 (BLOCKER-1, production-path seam): a T2 pull whose
      // /session/recall-pending response carries a matched standing-rule
      // advisory must render it into hookOutput.system inside the SAME
      // existing RULES fence with the exact canonical bytes
      // (RULES_FENCE_OPEN + RULES_DISCLAIMER + sanitizeRulesText(JSON.stringify(value))
      // + RULES_FENCE_CLOSE). The advisory rides the existing T2 pull (no new
      // network call); the RECALL block is kept byte-identical.
      const ADVISORY = JSON.stringify({
        if: "provider call fails",
        then: "retry same provider once; 配额用尽则切换到已批准的清单模型；否则升级",
        match: {
          family: "provider_error",
          provider: "openai",
          model: "gpt-4o",
          status: ["quota"],
          retryable: 0,
        },
      })
      recallPayload = {
        enabled: true,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 2400,
        detector_fired: true,
        rule_served: true,
        rule_count: 1,
        unresolved: false,
        provider: "openai",
        model: "gpt-4o",
        status: "quota",
        reason: null,
        rule_advisory: ADVISORY,
        matched_rule_source: "memory.remember",
        matched_chunk_id: "chunk-standing-1",
        conflict_reason: null,
      }
      const adv0 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_adv_1", prompt: { text: "quota hit, what should I do" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: adv0.system })
      console.log(JSON.stringify({ system: sysTexts(adv0.system), advisory: ADVISORY }))
      break
    }

    case "provider-error-advisory-truncate": {
      // B2 M-B2-14/M-B2-22: an advisory whose content exceeds the per-turn
      // remaining capacity (here B2_ADVISORY_MAX_CHARS on an empty-recall
      // turn) is TRUNCATED fence-valid at a rule boundary (rule_partial) —
      // never dropped, and the RECALL block is never reduced.
      const longThen = "E1E2E3E4 ".repeat(200)
      const ADVISORY = JSON.stringify({
        if: "provider call fails",
        then: longThen,
        match: {
          family: "provider_error",
          provider: "openai",
          model: "gpt-4o",
          status: ["quota"],
          retryable: 0,
        },
      })
      recallPayload = {
        enabled: true,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 2400,
        detector_fired: true,
        rule_served: true,
        rule_count: 1,
        unresolved: false,
        provider: "openai",
        model: "gpt-4o",
        status: "quota",
        reason: null,
        rule_advisory: ADVISORY,
        matched_rule_source: "memory.remember",
        matched_chunk_id: "chunk-standing-trunc",
        conflict_reason: null,
      }
      const tr0 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_trunc_1", prompt: { text: "quota hit" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: tr0.system })
      console.log(
        JSON.stringify({
          system: sysTexts(tr0.system),
          fullContent: ADVISORY,
        }),
      )
      break
    }

    case "provider-error-advisory-with-recall": {
      // B2 M-B2-20: a T2 turn with recall items + advisory keeps the RECALL
      // fence block byte-identical to the advisory-free render; the advisory
      // rides the same pull from the remaining per-turn capacity.
      const ADVISORY = JSON.stringify({
        if: "provider call fails",
        then: "retry same provider once; on quota switch to the approved inventory model; else escalate",
        match: {
          family: "provider_error",
          provider: "openai",
          model: "gpt-4o",
          status: ["quota"],
          retryable: 0,
        },
      })
      recallPayload = {
        enabled: true,
        items: RECALL_PAYLOAD.items,
        non_focal_above_floor: 1,
        slot_consumed: false,
        budget_chars: 2400,
        detector_fired: true,
        rule_served: true,
        rule_count: 1,
        unresolved: false,
        provider: "openai",
        model: "gpt-4o",
        status: "quota",
        reason: null,
        rule_advisory: ADVISORY,
        matched_rule_source: "memory.remember",
        matched_chunk_id: "chunk-standing-recall",
        conflict_reason: null,
      }
      const wr0 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_wr_1", prompt: { text: "quota hit with recall" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: wr0.system })
      console.log(JSON.stringify({ system: sysTexts(wr0.system), advisory: ADVISORY }))
      break
    }

    case "recall-only": {
      // Baseline for M-B2-20: the same recall items without any advisory.
      recallPayload = {
        enabled: true,
        items: RECALL_PAYLOAD.items,
        non_focal_above_floor: 1,
        slot_consumed: false,
        budget_chars: 2400,
        detector_fired: false,
        rule_served: false,
        rule_count: 0,
        unresolved: false,
        provider: null,
        model: null,
        status: null,
        reason: null,
        rule_advisory: null,
        matched_rule_source: null,
        matched_chunk_id: null,
        conflict_reason: null,
      }
      const ro0 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_ro_1", prompt: { text: "recall only" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: ro0.system })
      console.log(JSON.stringify({ system: sysTexts(ro0.system) }))
      break
    }

    case "provider-error-advisory-drop": {
      // B2 M-B2-21: an oversized RECALL block (raised item budget edge) leaves
      // no per-turn room, so the advisory drops and the RECALL block stays.
      const bigItems = Array.from({ length: 10 }, (_, i) => ({
        kind: "chunk",
        id: `c-big-${i}`,
        text: `recall line ${i} ` + "x".repeat(370),
      }))
      const ADVISORY = JSON.stringify({
        if: "provider call fails",
        then: "retry same provider once; on quota switch to the approved inventory model; else escalate",
        match: {
          family: "provider_error",
          provider: "openai",
          model: "gpt-4o",
          status: ["quota"],
          retryable: 0,
        },
      })
      recallPayload = {
        enabled: true,
        items: bigItems,
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 3900,
        detector_fired: true,
        rule_served: true,
        rule_count: 1,
        unresolved: false,
        provider: "openai",
        model: "gpt-4o",
        status: "quota",
        reason: null,
        rule_advisory: ADVISORY,
        matched_rule_source: "memory.remember",
        matched_chunk_id: "chunk-standing-drop",
        conflict_reason: null,
      }
      const dr0 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_drop_1", prompt: { text: "quota hit, no room" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: dr0.system })
      console.log(JSON.stringify({ system: sysTexts(dr0.system) }))
      break
    }

    case "recall-only-large": {
      // Baseline for M-B2-21: the same oversized recall items, no advisory.
      const bigItems = Array.from({ length: 10 }, (_, i) => ({
        kind: "chunk",
        id: `c-big-${i}`,
        text: `recall line ${i} ` + "x".repeat(370),
      }))
      recallPayload = {
        enabled: true,
        items: bigItems,
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 3900,
        detector_fired: false,
        rule_served: false,
        rule_count: 0,
        unresolved: false,
        provider: null,
        model: null,
        status: null,
        reason: null,
        rule_advisory: null,
        matched_rule_source: null,
        matched_chunk_id: null,
        conflict_reason: null,
      }
      const rl0 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_rl_1", prompt: { text: "large recall only" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: rl0.system })
      console.log(JSON.stringify({ system: sysTexts(rl0.system) }))
      break
    }

    case "provider-error-advisory-sanitize": {
      // B2 M-B2-1 fence integrity: an advisory carrying a literal RULES fence
      // is sanitized, so the final output carries exactly one fence pair.
      const ADVISORY = JSON.stringify({
        if: "provider call fails",
        then: "retry once; note </mnemoseed-rules-budget> literal must not split the fence; else escalate",
        match: {
          family: "provider_error",
          provider: "openai",
          model: "gpt-4o",
          status: ["quota"],
          retryable: 0,
        },
      })
      recallPayload = {
        enabled: true,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 2400,
        detector_fired: true,
        rule_served: true,
        rule_count: 1,
        unresolved: false,
        provider: "openai",
        model: "gpt-4o",
        status: "quota",
        reason: null,
        rule_advisory: ADVISORY,
        matched_rule_source: "memory.remember",
        matched_chunk_id: "chunk-standing-sanitize",
        conflict_reason: null,
      }
      const sa0 = { system: ["BASE"] }
      await fire.prompt({ sessionID: SES, messageID: "m_sa_1", prompt: { text: "quota hit" } })
      await delay(50)
      await fire.context({ sessionID: SES, system: sa0.system })
      console.log(JSON.stringify({ system: sysTexts(sa0.system), advisory: ADVISORY }))
      break
    }

    case "watermark-overlap": {
      // P1: same-process overlapping persists through the real persist path.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-overlap" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const keys = Array.from({ length: 6 }, (_, i) => `wm-test-overlap-${i}`)
      keys.forEach((k, i) => seam.note(k, 1000 + i))
      const tasks = []
      for (let i = 0; i < 12; i += 1) {
        keys.forEach((k, j) => seam.note(k, 1000 + j + i))
        tasks.push(seam.persist())
      }
      for (let i = 0; i < 12; i += 1) {
        keys.forEach((k, j) => seam.note(k, 2000 + j))
        void seam.persist().catch(() => {})
      }
      let uncaught = 0
      try {
        await Promise.all(tasks)
        await delay(300)
      } catch {
        uncaught = 1
      }
      // Oracle reads the process's own final shard bytes directly — never the
      // implementation merge view — so a removed per-persist scan or max
      // cannot hide behind the reader.
      const shard = await readOwnShard(seam)
      const expected = {}
      keys.forEach((k, j) => {
        expected[k] = 2000 + j
      })
      const lostKeys = keys.filter((k) => !shard || shard[k] !== expected[k])
      const shardValid = shard !== null && typeof shard === "object" && !Array.isArray(shard)
      const files = await readdir(dataDir)
      const legacyTmpOrphans = files.filter(
        (f) => f.startsWith("hook-watermarks.json.") && f.endsWith(".tmp"),
      ).length
      const ownTmpLeftovers = files.filter((f) => f.includes(".tmp.")).length
      console.log(
        JSON.stringify({
          persistCount: 24,
          uncaught,
          shardValid,
          lostKeys,
          legacyTmpOrphans,
          ownTmpLeftovers,
          shard,
          maxConcurrency: seam.maxConcurrency(),
        }),
      )
      break
    }

    case "watermark-fold-cases": {
      // Own-shard-bytes pins for the per-persist fold: a legacy-only key lands
      // in the own shard; a peer shard added AFTER initial load is discovered
      // on the next persist; the higher value wins by max; a later smaller
      // value never regresses the shard. Removing enumeration, the legacy
      // read, or max fails here.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-fold-cases" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const legacyPath = join(dataDir, "hook-watermarks.json")
      await writeFile(
        legacyPath,
        JSON.stringify({ "wm-test-fold-legacy": 700, "wm-test-fold-shared": 700 }),
        "utf8",
      )
      seam.note("wm-test-fold-legacy", 100)
      seam.note("wm-test-fold-shared", 750)
      seam.note("wm-test-fold-local", 500)
      await seam.persist()
      const afterLegacy = await readOwnShard(seam)
      const legacyFolded =
        afterLegacy !== null &&
        afterLegacy["wm-test-fold-legacy"] === 700 &&
        afterLegacy["wm-test-fold-shared"] === 750 &&
        afterLegacy["wm-test-fold-local"] === 500
      // late peer shard: higher shared value plus a new key
      const latePath = join(
        dataDir,
        "hook-watermarks.999992.aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.json",
      )
      await writeFile(
        latePath,
        JSON.stringify({ "wm-test-fold-shared": 800, "wm-test-fold-late": 800 }),
        "utf8",
      )
      await seam.persist()
      const afterLate = await readOwnShard(seam)
      const lateFolded =
        afterLate !== null &&
        afterLate["wm-test-fold-shared"] === 800 &&
        afterLate["wm-test-fold-late"] === 800 &&
        afterLate["wm-test-fold-legacy"] === 700
      // decreasing values on disk and via note must not regress the shard
      const lowPath = join(
        dataDir,
        "hook-watermarks.999993.bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb.json",
      )
      await writeFile(
        lowPath,
        JSON.stringify({ "wm-test-fold-shared": 600, "wm-test-fold-legacy": 50 }),
        "utf8",
      )
      seam.note("wm-test-fold-shared", 10)
      await seam.persist()
      const afterLow = await readOwnShard(seam)
      const noRegress =
        afterLow !== null &&
        afterLow["wm-test-fold-shared"] === 800 &&
        afterLow["wm-test-fold-legacy"] === 700 &&
        afterLow["wm-test-fold-late"] === 800
      console.log(JSON.stringify({ legacyFolded, lateFolded, noRegress, shard: afterLow }))
      break
    }

    case "watermark-eperm": {
      // P3: owned-target rename EPERM injection then bounded recovery.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-eperm" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      let uncaught = 0
      seam.note("wm-test-eperm-a", 1000)
      try {
        await seam.persist()
      } catch {
        uncaught += 1
      }
      let before = null
      try {
        before = JSON.parse(await readFile(seam.shardPath(), "utf8"))
      } catch {
        before = null
      }
      seam.setRenameFault("once")
      seam.note("wm-test-eperm-a", 2000)
      try {
        await seam.persist()
      } catch {
        uncaught += 1
      }
      let singleFaultRecovered = false
      try {
        const cur = JSON.parse(await readFile(seam.shardPath(), "utf8"))
        singleFaultRecovered = cur["wm-test-eperm-a"] === 2000
      } catch {
        singleFaultRecovered = false
      }
      seam.setRenameFault("always")
      seam.note("wm-test-eperm-b", 3000)
      try {
        await seam.persist()
      } catch {
        uncaught += 1
      }
      let duringContinuous = null
      try {
        duringContinuous = JSON.parse(await readFile(seam.shardPath(), "utf8"))
      } catch {
        duringContinuous = null
      }
      const keptGoodDuringFailure =
        duringContinuous !== null && duringContinuous["wm-test-eperm-a"] === 2000
      seam.setRenameFault("off")
      try {
        await seam.persist()
      } catch {
        uncaught += 1
      }
      let continuousFaultRecovered = false
      let targetValidAfter = false
      try {
        const cur = JSON.parse(await readFile(seam.shardPath(), "utf8"))
        targetValidAfter = true
        continuousFaultRecovered =
          cur["wm-test-eperm-a"] === 2000 && cur["wm-test-eperm-b"] === 3000
      } catch {
        targetValidAfter = false
      }
      const files = await readdir(dataDir)
      const ownTmpLeftovers = files.filter((f) => f.includes(".tmp.")).length
      console.log(
        JSON.stringify({
          uncaught,
          singleFaultRecovered,
          keptGoodDuringFailure,
          continuousFaultRecovered,
          targetValidAfter,
          ownTmpLeftovers,
          beforeKeys: before ? Object.keys(before) : [],
        }),
      )
      break
    }

    case "watermark-merge": {
      // P6: legacy read-only + peer shard maxima fold into the own shard.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-merge" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const legacyPath = join(dataDir, "hook-watermarks.json")
      const legacyPayload = { "wm-test-m-a": 100, "wm-test-m-b": 50 }
      await writeFile(legacyPath, JSON.stringify(legacyPayload), "utf8")
      const legacyBefore = await readFile(legacyPath, "utf8")
      const peerPath = join(dataDir, "hook-watermarks.999998.11111111-1111-4111-8111-111111111111.json")
      await writeFile(
        peerPath,
        JSON.stringify({ "wm-test-m-a": 80, "wm-test-m-b": 200, "wm-test-m-c": 300 }),
        "utf8",
      )
      seam.note("wm-test-m-a", 90)
      seam.note("wm-test-m-d", 400)
      await seam.persist()
      const shard = await readOwnShard(seam)
      const mergedOk =
        shard !== null &&
        shard["wm-test-m-a"] === 100 &&
        shard["wm-test-m-b"] === 200 &&
        shard["wm-test-m-c"] === 300 &&
        shard["wm-test-m-d"] === 400
      const legacyAfter = await readFile(legacyPath, "utf8")
      console.log(
        JSON.stringify({ mergedOk, legacyUnchanged: legacyBefore === legacyAfter, shard }),
      )
      break
    }

    case "watermark-corrupt": {
      // P4: corrupt files/keys skipped and preserved; good keys converge.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-corrupt" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const badJsonPath = join(
        dataDir,
        "hook-watermarks.999997.22222222-2222-4222-8222-222222222222.json",
      )
      const badJsonBytes = "{ not valid json {{{"
      await writeFile(badJsonPath, badJsonBytes, "utf8")
      const badKeysPath = join(
        dataDir,
        "hook-watermarks.999996.33333333-3333-4333-8333-333333333333.json",
      )
      const badKeysPayload = JSON.stringify({
        "wm-test-good": 500,
        "wm-test-bad-str": "oops",
        "wm-test-bad-neg": -5,
        "wm-test-bad-null": null,
      })
      await writeFile(badKeysPath, badKeysPayload, "utf8")
      seam.note("wm-test-good", 400)
      seam.note("wm-test-local", 600)
      await seam.persist()
      const shard = await readOwnShard(seam)
      const corruptPreserved =
        (await readFile(badJsonPath, "utf8")) === badJsonBytes &&
        (await readFile(badKeysPath, "utf8")) === badKeysPayload
      const badKeysExcluded =
        shard !== null &&
        shard["wm-test-bad-str"] === undefined &&
        shard["wm-test-bad-neg"] === undefined &&
        shard["wm-test-bad-null"] === undefined
      const goodKeysConverged =
        shard !== null && shard["wm-test-good"] === 500 && shard["wm-test-local"] === 600
      console.log(
        JSON.stringify({ corruptPreserved, badKeysExcluded, goodKeysConverged, shard }),
      )
      break
    }

    case "watermark-gc": {
      // P5: GC dead/alive/EPERM/unknown rules and fold-before-delete.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-gc" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const oldDate = new Date(Date.now() - 25 * 3600 * 1000)
      const deadPath = join(
        dataDir,
        "hook-watermarks.999990.44444444-4444-4444-8444-444444444444.json",
      )
      await writeFile(deadPath, JSON.stringify({ "wm-test-gc-dead": 777 }), "utf8")
      await utimes(deadPath, oldDate, oldDate)
      const alivePid = process.pid
      const alivePath = join(
        dataDir,
        `hook-watermarks.${alivePid}.55555555-5555-4555-8555-555555555555.json`,
      )
      await writeFile(alivePath, JSON.stringify({ "wm-test-gc-alive": 111 }), "utf8")
      await utimes(alivePath, oldDate, oldDate)
      const epermPath = join(
        dataDir,
        "hook-watermarks.4.66666666-6666-4666-8666-666666666666.json",
      )
      await writeFile(epermPath, JSON.stringify({ "wm-test-gc-eperm": 222 }), "utf8")
      await utimes(epermPath, oldDate, oldDate)
      const unknownPath = join(dataDir, "hook-watermarks.notapid.77777777-7777-4777-8777-777777777777.json")
      await writeFile(unknownPath, JSON.stringify({ "wm-test-gc-unknown": 333 }), "utf8")
      await utimes(unknownPath, oldDate, oldDate)
      const corruptGcPath = join(
        dataDir,
        "hook-watermarks.999998.88888888-8888-4888-8888-888888888888.json",
      )
      const corruptBytes = "{ broken"
      await writeFile(corruptGcPath, corruptBytes, "utf8")
      await utimes(corruptGcPath, oldDate, oldDate)
      const exists = async (p) => {
        try {
          await stat(p)
          return true
        } catch {
          return false
        }
      }
      // create >20 extra eligible dead shards to pin the <=20 bound
      const extraPaths = []
      for (let i = 0; i < 25; i += 1) {
        const uuid = `99999999-9999-4999-8999-9999999999${String(i).padStart(2, "0")}`
        const p = join(dataDir, `hook-watermarks.999995.${uuid}.json`)
        await writeFile(p, JSON.stringify({ [`wm-test-gc-extra-${i}`]: 10 + i }), "utf8")
        await utimes(p, oldDate, oldDate)
        extraPaths.push(p)
      }
      // young dead-owner shard: fresh mtime, dead PID, valid content — the age
      // gate must preserve it while still folding its key.
      const youngDeadPath = join(
        dataDir,
        "hook-watermarks.999991.cccccccc-cccc-4ccc-8ccc-cccccccccccc.json",
      )
      await writeFile(youngDeadPath, JSON.stringify({ "wm-test-gc-young": 444 }), "utf8")
      seam.note("wm-test-gc-local", 50)
      await seam.persist()
      const afterFirst = await readOwnShard(seam)
      const deadDeleted = !(await exists(deadPath))
      const foldedBeforeDelete = afterFirst !== null && afterFirst["wm-test-gc-dead"] === 777
      const youngFolded = afterFirst !== null && afterFirst["wm-test-gc-young"] === 444
      const youngPreserved = await exists(youngDeadPath)
      const alivePreserved = await exists(alivePath)
      const epermPreserved = await exists(epermPath)
      const unknownPreserved = await exists(unknownPath)
      const corruptPreserved =
        (await exists(corruptGcPath)) && (await readFile(corruptGcPath, "utf8")) === corruptBytes
      let deletedExtras = 0
      for (const p of extraPaths) {
        if (!(await exists(p))) deletedExtras += 1
      }
      const totalDeleted = (deadDeleted ? 1 : 0) + deletedExtras
      // old own shard: backdate the current process's own file and force the
      // liveness check to report ESRCH — the name exclusion must still win.
      await utimes(seam.shardPath(), oldDate, oldDate)
      seam.setLivenessFault("dead")
      await seam.runGc()
      seam.setLivenessFault("off")
      const ownPreserved = await exists(seam.shardPath())
      const ownBytesKept = (await readOwnShard(seam)) !== null
      console.log(
        JSON.stringify({
          deadDeleted,
          foldedBeforeDelete,
          youngFolded,
          youngPreserved,
          ownPreserved,
          ownBytesKept,
          alivePreserved,
          epermPreserved,
          unknownPreserved,
          corruptPreserved,
          deleteBound: totalDeleted <= 20,
          totalDeleted,
          shard: afterFirst,
        }),
      )
      break
    }

    case "watermark-mixed": {
      // Mixed-version: an old whole-map legacy write folds forward; new code
      // never overwrites the legacy file.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-mixed" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const legacyPath = join(dataDir, "hook-watermarks.json")
      seam.note("wm-test-mix-new", 900)
      await seam.persist()
      // simulate an old-version process whole-map overwriting the legacy file
      const legacyMixed = { "wm-test-mix-old": 800, "wm-test-mix-new": 100 }
      await writeFile(legacyPath, JSON.stringify(legacyMixed), "utf8")
      const legacyBeforeNewPersist = await readFile(legacyPath, "utf8")
      seam.note("wm-test-mix-new", 950)
      await seam.persist()
      const shard = await readOwnShard(seam)
      const legacyAfter = await readFile(legacyPath, "utf8")
      console.log(
        JSON.stringify({
          foldedOk: shard !== null && shard["wm-test-mix-old"] === 800 && shard["wm-test-mix-new"] === 950,
          legacyNotOverwritten: legacyAfter === legacyBeforeNewPersist,
          shard,
        }),
      )
      break
    }

    case "watermark-tmp-sweep": {
      // Bounded own-tmp recovery: injected unlink failures orphan owned tmps
      // on a failed persist; the next serialized persist sweeps the exact own
      // prefix. Legacy Date.now tmps and unrelated files are never touched.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-tmp-sweep" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const legacyTmp = join(dataDir, "hook-watermarks.json.1234567890123.tmp")
      await writeFile(legacyTmp, "legacy-tmp-sentinel", "utf8")
      const unrelatedTmp = join(dataDir, "other-tool.tmp.1")
      await writeFile(unrelatedTmp, "unrelated-sentinel", "utf8")
      const legacyBefore = await readFile(legacyTmp, "utf8")
      const unrelatedBefore = await readFile(unrelatedTmp, "utf8")
      seam.note("wm-test-tmp-a", 100)
      await seam.persist()
      const ownPrefix = `.${seam.shardBasename()}.tmp.`
      const ownTmps = async () =>
        (await readdir(dataDir)).filter((f) => f.startsWith(ownPrefix))
      const cleanStart = (await ownTmps()).length === 0
      seam.setRenameFault("always")
      seam.setUnlinkFault("always")
      seam.note("wm-test-tmp-a", 200)
      await seam.persist()
      const staleAfterFailure = await ownTmps()
      seam.setRenameFault("off")
      seam.setUnlinkFault("off")
      await seam.persist()
      const staleAfterSweep = await ownTmps()
      const shard = await readOwnShard(seam)
      console.log(
        JSON.stringify({
          cleanStart,
          orphanedOnFailure: staleAfterFailure.length >= 1,
          sweptOnNextPersist: staleAfterSweep.length === 0,
          shardRecovered: shard !== null && shard["wm-test-tmp-a"] === 200,
          legacyTmpPreserved: (await readFile(legacyTmp, "utf8")) === legacyBefore,
          unrelatedPreserved: (await readFile(unrelatedTmp, "utf8")) === unrelatedBefore,
        }),
      )
      break
    }

    case "watermark-early-ack": {
      // Pre-load ACK retention through production paths: invalid timestamps
      // are rejected at the ack-clock entry; the first valid early note
      // survives the lazy load via the pending map; a real acked turn through
      // chat.message/idle converges into the same own shard bytes.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-early-ack" }))
        break
      }
      seam.note("wm-test-early-bad-neg", -5)
      seam.note("wm-test-early-bad-inf", Infinity)
      seam.note("wm-test-early-bad-nan", NaN)
      seam.note("wm-test-early-keep", 650)
      await fire.prompt({ sessionID: "wm-test-early-session", messageID: "m_early_1", prompt: { text: "early ack turn" } })
      await delay(100)
      await fire.event({ type: "session.idle", data: { sessionID: "wm-test-early-session" } })
      await delay(100)
      const shard = await readOwnShard(seam)
      console.log(
        JSON.stringify({
          earlyRetained: shard !== null && shard["wm-test-early-keep"] === 650,
          invalidIgnored:
            shard !== null &&
            shard["wm-test-early-bad-neg"] === undefined &&
            shard["wm-test-early-bad-inf"] === undefined &&
            shard["wm-test-early-bad-nan"] === undefined,
          realAckConverged:
            shard !== null &&
            typeof shard["wm-test-early-session"] === "number" &&
            shard["wm-test-early-session"] > 0,
          shard,
        }),
      )
      break
    }

    case "watermark-note-during-io": {
      // A note that advances while a persist is parked inside its write
      // window must be flushed by the same serialized persist call via a
      // bounded follow-up cycle — deterministic via the seam I/O barrier.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-note-during-io" }))
        break
      }
      seam.note("wm-test-dio-a", 100)
      seam.armIoBarrier()
      const pending = seam.persist()
      await seam.whenPaused()
      seam.note("wm-test-dio-a", 999)
      seam.note("wm-test-dio-b", 555)
      seam.releaseBarrier()
      await pending
      const shard = await readOwnShard(seam)
      console.log(
        JSON.stringify({
          sameCycleFlushed:
            shard !== null && shard["wm-test-dio-a"] === 999 && shard["wm-test-dio-b"] === 555,
          shard,
        }),
      )
      break
    }

    case "watermark-selectors": {
      // Strict filename predicate: decoys are excluded from the merged view
      // and preserved on disk; legacy plus valid shards still converge.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-selectors" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const decoys = [
        "hook-watermarks.json.bak",
        "hook-watermarks-foo.json",
        "hook-watermarks.json.123.tmp",
        "hook-watermarks.123.json",
        "hook-watermarks.abc.aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.json",
        "xhook-watermarks.1.aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.json",
        "hook-watermarks.1.aaa.json.tmp",
        "hook-watermarks.1.short.json",
      ]
      for (const name of decoys) {
        await writeFile(join(dataDir, name), JSON.stringify({ [`wm-test-decoy-${name}`]: 42 }), "utf8")
      }
      const oldDate = new Date(Date.now() - 25 * 3600 * 1000)
      for (const name of decoys) {
        await utimes(join(dataDir, name), oldDate, oldDate)
      }
      await writeFile(join(dataDir, "hook-watermarks.json"), JSON.stringify({ "wm-test-sel-a": 11 }), "utf8")
      seam.note("wm-test-sel-b", 22)
      await seam.persist()
      const view = await readWatermarks()
      const decoyKeys = decoys.map((name) => `wm-test-decoy-${name}`)
      const decoysExcluded = decoyKeys.every((k) => view[k] === undefined)
      const shard = await readOwnShard(seam)
      const converged =
        shard !== null && shard["wm-test-sel-a"] === 11 && shard["wm-test-sel-b"] === 22
      const decoysPreserved = (
        await Promise.all(
          decoys.map(async (name) => {
            try {
              await stat(join(dataDir, name))
              return true
            } catch {
              return false
            }
          }),
        )
      ).every(Boolean)
      console.log(JSON.stringify({ decoysExcluded, converged, decoysPreserved, shard }))
      break
    }

    case "watermark-stale-sweep": {
      // Age-based sweep of stale watermark tmp artifacts. An old
      // legacy `.tmp` is reaped; a young tmp and an unrelated tmp survive; the
      // interval guard suppresses a second sweep until forced.
      const seam = pluginModule.__watermarkTest
      if (!seam) {
        console.log(JSON.stringify({ seamMissing: true, scenario: "watermark-stale-sweep" }))
        break
      }
      const dataDir = process.env.MNEMOSEED_LOCAL_DATA_DIR
      const oldDate = new Date(Date.now() - 8 * 24 * 3600 * 1000)
      const youngDate = new Date(Date.now() - 60 * 1000)
      const oldTmp = join(dataDir, "hook-watermarks.json.1700000000000.tmp")
      const youngTmp = join(dataDir, "hook-watermarks.json.1799999999999.tmp")
      const unrelatedTmp = join(dataDir, "other-tool.tmp")
      await writeFile(oldTmp, "old-stale", "utf8")
      await writeFile(youngTmp, "young-live", "utf8")
      await writeFile(unrelatedTmp, "unrelated", "utf8")
      await utimes(oldTmp, oldDate, oldDate)
      await utimes(youngTmp, youngDate, youngDate)
      await utimes(unrelatedTmp, oldDate, oldDate)
      const exists = async (p) => {
        try {
          await stat(p)
          return true
        } catch {
          return false
        }
      }
      // first persist sweeps immediately (last-sweep clock starts at 0)
      seam.note("wm-test-stale-a", 100)
      await seam.persist()
      const oldRemoved = !(await exists(oldTmp))
      const youngKept = await exists(youngTmp)
      const unrelatedKept = await exists(unrelatedTmp)
      // drop a NEW old tmp, then run a guarded (non-forced) sweep: the guard
      // must suppress it, leaving the new artifact in place.
      const guardedTmp = join(dataDir, "hook-watermarks.json.1711111111111.tmp")
      await writeFile(guardedTmp, "guard-sentinel", "utf8")
      await utimes(guardedTmp, oldDate, oldDate)
      await seam.runStaleSweep(false)
      const guardHeld = await exists(guardedTmp)
      // forced sweep must reap it
      await seam.runStaleSweep(true)
      const forcedRemoved = !(await exists(guardedTmp))
      console.log(JSON.stringify({ oldRemoved, youngKept, unrelatedKept, guardHeld, forcedRemoved }))
      break
    }

    case "visibility-t1-compact": {
      // compact T1: exactly one signal line, right after the fence-open
      // line, before the disclaimer; the block stays within the 4000 cap.
      recentPayload = { ...RECENT_PAYLOAD, visibility: { mode: "compact" } }
      const o = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o.system })
      console.log(JSON.stringify({ block: sysTexts(o.system)[1], length: sysTexts(o.system)[1].length }))
      break
    }

    case "visibility-t1-verbose": {
      // verbose T1: the compact signal line plus the count line
      // (sessions / memories / chars of the included content).
      recentPayload = { ...RECENT_PAYLOAD, visibility: { mode: "verbose" } }
      const o = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o.system })
      console.log(JSON.stringify({ block: sysTexts(o.system)[1], length: sysTexts(o.system)[1].length }))
      break
    }

    case "visibility-t1-budget-shed": {
      // T1 budget discipline: when the signal does not fit the 4000 cap
      // it sheds FIRST and the resulting block is byte-identical to the
      // off-mode build of the same payload (existing content never sheds to
      // make room for the signal).
      const filled = {
        profile_id: "default",
        self_window: null,
        sessions: [
          {
            session_id: "sess-vis-big",
            latest_at: 50.0,
            chunks: [
              { chunk_id: "c-vis-a", text: "assistant: " + "乙".repeat(3900), ingested_at: 50.0, turn_start: 0, turn_end: 0 },
              { chunk_id: "c-vis-b", text: "user: " + "丙".repeat(9000), ingested_at: 40.0, turn_start: 0, turn_end: 0 },
            ],
          },
        ],
      }
      recentPayload = { ...filled, visibility: { mode: "compact" } }
      const withVis = { system: ["BASE"] }
      await fire.context({ sessionID: "sess-vis-on", system: withVis.system })
      recentPayload = filled
      const withoutVis = { system: ["BASE"] }
      await fire.context({ sessionID: "sess-vis-off", system: withoutVis.system })
      console.log(
        JSON.stringify({
          withVis: sysTexts(withVis.system)[1],
          withoutVis: sysTexts(withoutVis.system)[1],
          withVisLength: sysTexts(withVis.system)[1]?.length ?? 0,
          withoutVisLength: sysTexts(withoutVis.system)[1]?.length ?? 0,
        }),
      )
      break
    }

    case "visibility-t2-compact": {
      // compact T2: exactly one signal line inside the block.
      recallPayload = { ...RECALL_PAYLOAD, visibility: { mode: "compact", capture: false } }
      await fire.prompt({ sessionID: SES, messageID: "m_vis_t2", prompt: { text: "触发一轮" } })
      await delay(50)
      const o = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o.system })
      console.log(JSON.stringify({ systems: [sysTexts(o.system)] }))
      break
    }

    case "visibility-t2-verbose": {
      // verbose T2: the compact signal line plus the count line
      // (memories / chars of the served items).
      recallPayload = { ...RECALL_PAYLOAD, visibility: { mode: "verbose", capture: false } }
      await fire.prompt({ sessionID: SES, messageID: "m_vis_t2v", prompt: { text: "触发一轮" } })
      await delay(50)
      const o = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o.system })
      console.log(JSON.stringify({ systems: [sysTexts(o.system)] }))
      break
    }

    case "visibility-t2-budget-shed": {
      // T2 budget discipline: a tight budget sheds the signal first; the
      // shed result is byte-identical to the no-field build.
      const tight = {
        enabled: true,
        items: [{ kind: "chunk", id: "c-vis-tight", text: "z".repeat(149) }],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 150,
      }
      recallPayload = { ...tight, visibility: { mode: "compact", capture: false } }
      await fire.prompt({ sessionID: SES, messageID: "m_vis_shed", prompt: { text: "紧预算轮" } })
      await delay(50)
      const withVis = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: withVis.system })
      recallPayload = tight
      await fire.prompt({ sessionID: "sess-vis-t2b", messageID: "m_vis_shed2", prompt: { text: "紧预算轮" } })
      await delay(50)
      const withoutVis = { system: ["BASE"] }
      await fire.context({ sessionID: "sess-vis-t2b", system: withoutVis.system })
      console.log(
        JSON.stringify({
          withVisSystems: sysTexts(withVis.system),
          withoutVisSystems: sysTexts(withoutVis.system),
        }),
      )
      break
    }

    case "visibility-capture-compact": {
      // compact capture: the first acked ingest arms the per-session
      // latch; the next transform renders the line exactly once, later
      // transforms and later captures never re-arm it.
      recallPayload = {
        enabled: false,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 1200,
        visibility: { mode: "compact", capture: false },
      }
      await fire.prompt({ sessionID: SES, messageID: "m_cap1", prompt: { text: "第一轮" } })
      await delay(50)
      const c1 = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: c1.system })
      const c2 = { system: ["BASE2"] }
      await fire.context({ sessionID: SES, system: c2.system })
      await fire.prompt({ sessionID: SES, messageID: "m_cap2", prompt: { text: "第二轮" } })
      await delay(50)
      const c3 = { system: ["BASE3"] }
      await fire.context({ sessionID: SES, system: c3.system })
      console.log(JSON.stringify({ systems: [sysTexts(c1.system), sysTexts(c2.system), sysTexts(c3.system)] }))
      break
    }

    case "visibility-capture-off": {
      // off: a pending capture latch renders nothing and stays armed.
      recallPayload = {
        enabled: false,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 1200,
        visibility: { mode: "off", capture: true },
      }
      await fire.prompt({ sessionID: SES, messageID: "m_capo1", prompt: { text: "关灯轮" } })
      await delay(50)
      const o1 = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o1.system })
      const o2 = { system: ["BASE2"] }
      await fire.context({ sessionID: SES, system: o2.system })
      console.log(JSON.stringify({ systems: [sysTexts(o1.system), sysTexts(o2.system)] }))
      break
    }

    case "visibility-capture-verbose": {
      // verbose capture: the daemon's capture count rides the pull; each
      // count>0 pull renders one count line (the once-only latch stays armed
      // for a later compact flip and renders nothing in verbose).
      recallPayload = {
        enabled: false,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 1200,
        visibility: { mode: "verbose", capture: 2 },
      }
      await fire.prompt({ sessionID: SES, messageID: "m_capv1", prompt: { text: "计数轮一" } })
      await delay(50)
      const v1 = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: v1.system })
      recallPayload = {
        enabled: false,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 1200,
        visibility: { mode: "verbose", capture: false },
      }
      await fire.prompt({ sessionID: SES, messageID: "m_capv2", prompt: { text: "计数轮二" } })
      await delay(50)
      const v2 = { system: ["BASE2"] }
      await fire.context({ sessionID: SES, system: v2.system })
      console.log(JSON.stringify({ systems: [sysTexts(v1.system), sysTexts(v2.system)] }))
      break
    }

    case "visibility-signal-not-citable": {
      // Signal-line quote: zero reinforce. c-mid quote: exactly one (positive control).
      recallPayload = { ...RECALL_PAYLOAD, visibility: { mode: "compact", capture: false } }
      await fire.prompt({ sessionID: SES, messageID: "m_vis_cite", prompt: { text: "引用轮" } })
      await delay(50)
      const o = { system: ["BASE"] }
      await fire.context({ sessionID: SES, system: o.system })
      citationTurnsVisible = true
      t2CiteTurnsVisible = true
      signalCiteTurnVisible = true
      await fire.assistantReply("m_cite_t2_1", 8_000)
      await delay(100)
      await fire.assistantReply("m_sig_cite_1", 10_000)
      await delay(100)
      console.log(
        JSON.stringify({
          systems: [sysTexts(o.system)],
          reinforcePosts: posts.filter((post) => post.url.endsWith("/memory/reinforce")),
        }),
      )
      break
    }

    case "agent-attribution": {
      // B2.9 on the V2 surface: the V2 host has no per-message agent, so the
      // plugin tracks the SESSION-level agent (session.created data.agent,
      // then session.agent.selected) and rides it on the canonical user_prompt
      // body field; a session with no tracked agent stays unattributed.
      await fire.event({
        type: "session.created",
        data: { sessionID: "sess-agent", agent: "build" },
      })
      await fire.prompt({ sessionID: "sess-agent", messageID: "m_ag_1", prompt: { text: "归属轮" } })
      await delay(100)
      await fire.event({
        type: "session.agent.selected",
        data: { sessionID: "sess-agent", agent: "review" },
      })
      await fire.prompt({ sessionID: "sess-agent", messageID: "m_ag_2", prompt: { text: "切换轮" } })
      await delay(100)
      await fire.prompt({ sessionID: "sess-untracked", messageID: "m_ag_3", prompt: { text: "无归属轮" } })
      await delay(100)
      console.log(
        JSON.stringify({
          agents: posts
            .filter((post) => post.body.event === "user_prompt")
            .map((post) => ({ text: post.body.content?.text, agent: post.body.agent ?? null })),
        }),
      )
      break
    }

    default:
      console.error(`unknown scenario: ${scenario}`)
      process.exit(64)
  }

  // Release the event subscription so node can exit cleanly (the loop's
  // pending pull would otherwise leave the top-level await unfinished).
  aborted = true
  wake()
  for (const cleanup of cleanupFns) {
    try {
      await cleanup()
    } catch {}
  }
  await delay(20)
}

await main()
