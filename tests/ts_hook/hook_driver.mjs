// Minimal behavioral driver for the shipped opencode hook (B2.2 loose-end:
// regex pins cannot pin BEHAVIOR). Node feeks the bundled plugin a fake SDK
// client + a recording fetch; scenarios print a JSON transcript for pytest
// to assert on. No daemon, no network, no LLM — everything is canned.

import { pathToFileURL } from "node:url"
import { mkdtemp, readFile, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

const [bundle, scenario] = process.argv.slice(2)
if (!bundle || !scenario) {
  console.error("usage: node hook_driver.mjs <bundle> <scenario>")
  process.exit(64)
}

// isolation: the plugin roots its artifacts under MNEMOSEED_LOCAL_DATA_DIR
process.env.MNEMOSEED_LOCAL_DATA_DIR = await mkdtemp(join(tmpdir(), "mnemo-hook-test-"))
const WATERMARKS = join(process.env.MNEMOSEED_LOCAL_DATA_DIR, "hook-watermarks.json")

const posts = []
const failing = new Set()
const throwing = new Set()
globalThis.fetch = async (url, init) => {
  const body = JSON.parse(init.body)
  const accepted = !failing.has(url)
  posts.push({ url, body, accepted })
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

function historyEntries() {
  const entries = [
    {
      // opencode's UserMessage carries a required per-message agent; the
      // crash-replay lane must lift it into the canonical ingest field.
      info: { id: "m_old_user", role: "user", sessionID: SES, agent: "build", time: { created: 1_000 } },
      parts: [{ type: "text", text: "历史用户消息" }],
    },
    {
      info: {
        id: "m_old_assistant",
        role: "assistant",
        sessionID: SES,
        time: { completed: 2_000 },
      },
      parts: [{ type: "text", text: "历史助手回复" }],
    },
  ]
  if (outageTurnVisible) {
    entries.push({
      info: {
        id: "m_live_outage",
        role: "user",
        sessionID: SES,
        time: { created: Math.round(Date.now() - 1000) },
      },
      parts: [{ type: "text", text: "宕机轮" }],
    })
  }
  if (citationTurnsVisible) {
    entries.push({
      info: {
        id: "m_cite_1",
        role: "assistant",
        sessionID: SES,
        time: { completed: 3_000 },
      },
      parts: [
        {
          type: "text",
          text: "好的，回顾一下上次的决定：我们决定把发布窗口固定在每月第一周，并且由小李负责验收，周五之前出终稿。我现在就排期。",
        },
      ],
    })
    entries.push({
      info: {
        id: "m_cite_2",
        role: "assistant",
        sessionID: SES,
        time: { completed: 4_000 },
      },
      parts: [
        {
          type: "text",
          text: "再确认一次：我们决定把发布窗口固定在每月第一周，并且由小李负责验收，周五之前出终稿。",
        },
      ],
    })
    entries.push({
      info: {
        id: "m_cite_3",
        role: "assistant",
        sessionID: "sess-noinject",
        time: { completed: 5_000 },
      },
      parts: [
        {
          type: "text",
          text: "参考旧决定：我们决定把发布窗口固定在每月第一周，并且由小李负责验收，周五之前出终稿。",
        },
      ],
    })
    entries.push({
      info: {
        id: "m_budget_quote",
        role: "assistant",
        sessionID: SES,
        time: { completed: 6_000 },
      },
      parts: [{ type: "text", text: "user: " + "甲".repeat(3000) }],
    })
    entries.push({
      info: {
        id: "m_cite_4",
        role: "assistant",
        sessionID: SES,
        time: { completed: 7_000 },
      },
      parts: [
        {
          type: "text",
          text: "As discussed, WE DECIDED  to   lock the release window...",
        },
      ],
    })
  }
  if (sliceTurnsVisible) {
    entries.push({
      info: {
        id: "m_slice_1",
        role: "assistant",
        sessionID: "sess-het",
        time: { completed: 51_000 },
      },
      parts: [{ type: "text", text: "B".repeat(30) }],
    })
    entries.push({
      info: {
        id: "m_slice_2",
        role: "assistant",
        sessionID: "sess-het",
        time: { completed: 52_000 },
      },
      parts: [{ type: "text", text: "A".repeat(30) }],
    })
    entries.push({
      info: {
        id: "m_slice_3",
        role: "assistant",
        sessionID: "sess-het",
        time: { completed: 53_000 },
      },
      parts: [{ type: "text", text: "丙".repeat(30) }],
    })
  }
  if (t2CiteTurnsVisible) {
    entries.push({
      info: {
        id: "m_cite_t2_1",
        role: "assistant",
        sessionID: SES,
        time: { completed: 8_000 },
      },
      parts: [
        {
          type: "text",
          text: "上次中段提到 LanceDb 的接入还在等存储层确认，我们继续推进。",
        },
      ],
    })
    entries.push({
      info: {
        id: "m_cite_t2_2",
        role: "assistant",
        sessionID: SES,
        time: { completed: 9_000 },
      },
      parts: [
        {
          type: "text",
          text: "再确认一次：上次中段提到 LanceDb 的接入还在等存储层确认。",
        },
      ],
    })
  }
  return entries
}

function messageUpdatedAssistant(id, completed) {
  return {
    type: "message.updated",
    properties: {
      info: { id, role: "assistant", sessionID: SES, time: { completed } },
      sessionID: SES,
    },
  }
}

async function readWatermarks() {
  try {
    return JSON.parse(await readFile(WATERMARKS, "utf8"))
  } catch {
    return null
  }
}

async function main() {
  const fakeClient = {
    session: {
      // history: user u_old then assistant a_old (both completed)
      messages: async () => ({ data: historyEntries() }),
    },
  }

  const pluginFactory = (await import(pathToFileURL(bundle))).default
  const hooks = await pluginFactory({ client: fakeClient })

  switch (scenario) {
    case "ack-watermark": {
      // seed a watermark so reconcile considers the session known
      await writeFile(WATERMARKS, JSON.stringify({ [SES]: 500 }), "utf8")
      // daemon REJECTS the user turn's POST: the mark must NOT advance past it
      failing.add("http://localhost:7788/ingest")
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_live_1" },
        { parts: [{ type: "text", text: "拒绝轮" }] },
      )
      await delay(100)
      await hooks.event({ event: { type: "session.idle", properties: { sessionID: SES } } })
      await delay(50)
      const marksRejected = await readWatermarks()
      // daemon HEALTHY again: the next accepted turn must advance the mark
      failing.clear()
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_live_2" },
        { parts: [{ type: "text", text: "接受轮" }] },
      )
      await delay(100)
      await hooks.event({ event: { type: "session.idle", properties: { sessionID: SES } } })
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
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_live_1" },
        { parts: [{ type: "text", text: "重启后的新消息" }] },
      )
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
      await hooks.event({ event: messageUpdatedAssistant("m_old_assistant", 3_000) })
      await delay(50)
      await hooks.event({ event: { type: "session.idle", properties: { sessionID: SES } } })
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
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_live_outage" },
        { parts: [{ type: "text", text: "宕机轮" }] },
      )
      await delay(100)
      failing.clear()
      // opencode's own store now carries the outage turn (simulated: the
      // fake SDK listing gains it after the fact — see fakeClient shim below)
      outageTurnVisible = true
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_live_recovery" },
        { parts: [{ type: "text", text: "恢复轮" }] },
      )
      await delay(100)
      await hooks.event({ event: { type: "session.idle", properties: { sessionID: SES } } })
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
      await hooks["chat.system.transform"]({ sessionID: SES }, { system: undefined })
      const o1 = { system: ["BASE SYSTEM PROMPT"] }
      await hooks["chat.system.transform"]({ sessionID: SES }, o1)
      const o2 = { system: ["BASE"] }
      await hooks["chat.system.transform"]({ sessionID: SES }, o2)
      const c1 = { system: ["BASE1"] }
      const c2 = { system: ["BASE2"] }
      await Promise.all([
        hooks["chat.system.transform"]({ sessionID: "sess-conc" }, c1),
        hooks["chat.system.transform"]({ sessionID: "sess-conc" }, c2),
      ])
      const o3 = { system: ["BASE3"] }
      await hooks["chat.system.transform"]({ sessionID: "sess-other" }, o3)
      // IMPORTANT-3 (QA): an empty sessionID must burn NOTHING — the attempt
      // gate is not consumed, so the real session id right after still injects.
      const e1 = { system: ["B"] }
      await hooks["chat.system.transform"]({ sessionID: "" }, e1)
      const e2 = { system: ["B2"] }
      await hooks["chat.system.transform"]({ sessionID: "sess-emptyid" }, e2)
      console.log(
        JSON.stringify({
          systems: [o1.system, o2.system, c1.system, c2.system, o3.system, e1.system, e2.system],
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
      await hooks["chat.system.transform"]({ sessionID: "sess-window" }, w)
      console.log(
        JSON.stringify({
          block: w.system[1],
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
      await hooks["chat.system.transform"]({ sessionID: "sess-old-daemon" }, d)
      console.log(JSON.stringify({ block: d.system[1] }))
      break
    }

    case "inject-fail-open": {
      // T1 fail-open: a daemon failure (503 OR a thrown network error) leaves
      // the system untouched, and the attempt-once gate still holds — exactly
      // one /session/recent attempt per session.
      recentPayload = RECENT_PAYLOAD
      failing.add("http://localhost:7788/session/recent")
      const sys503 = { system: ["BASE503"] }
      await hooks["chat.system.transform"]({ sessionID: "sess-fail-503" }, sys503)
      await hooks["chat.system.transform"]({ sessionID: "sess-fail-503" }, { system: ["BASE503b"] })
      failing.clear()
      throwing.add("http://localhost:7788/session/recent")
      const sysThrow = { system: ["BASEthrow"] }
      await hooks["chat.system.transform"]({ sessionID: "sess-fail-throw" }, sysThrow)
      await hooks["chat.system.transform"]({ sessionID: "sess-fail-throw" }, { system: ["BASEthrowb"] })
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
      await hooks["chat.system.transform"]({ sessionID: "sess-freeze" }, frozen)
      console.log(
        JSON.stringify({
          systems: [sys503.system, sysThrow.system],
          attempts503,
          attemptsThrow,
          frozenSystem: [...frozen.system],
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
      await hooks["chat.system.transform"]({ sessionID: SES }, o)
      const injectedBlock = o.system[1]
      await hooks.event({ event: messageUpdatedAssistant("m_cite_1", 3_000) })
      await delay(100)
      await hooks.event({ event: messageUpdatedAssistant("m_cite_2", 4_000) })
      await delay(100)
      await hooks.event({ event: { type: "session.idle", properties: { sessionID: SES } } })
      await delay(100)
      await hooks.event({
        event: {
          type: "message.updated",
          properties: {
            info: {
              id: "m_cite_3",
              role: "assistant",
              sessionID: "sess-noinject",
              time: { completed: 5_000 },
            },
            sessionID: "sess-noinject",
          },
        },
      })
      await delay(100)
      // IMPORTANT-3 (QA): normalization equivalence — uppercase + irregular
      // spacing in the reply must still cite the English chunk c-eng.
      await hooks.event({ event: messageUpdatedAssistant("m_cite_4", 7_000) })
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
      await hooks["chat.system.transform"]({ sessionID: SES }, o)
      const block = o.system[1]
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
      await hooks["chat.system.transform"]({ sessionID: "sess-solo" }, o2)
      const block2 = o2.system[1]
      citationTurnsVisible = true
      await hooks.event({
        event: {
          type: "message.updated",
          properties: {
            info: {
              id: "m_budget_quote",
              role: "assistant",
              sessionID: SES,
              time: { completed: 6_000 },
            },
            sessionID: SES,
          },
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
      await hooks["chat.system.transform"]({ sessionID: "sess-het" }, o)
      const block = o.system[1]
      sliceTurnsVisible = true
      await hooks.event({
        event: {
          type: "message.updated",
          properties: {
            info: {
              id: "m_slice_1",
              role: "assistant",
              sessionID: "sess-het",
              time: { completed: 51_000 },
            },
            sessionID: "sess-het",
          },
        },
      })
      await delay(100)
      await hooks.event({
        event: {
          type: "message.updated",
          properties: {
            info: {
              id: "m_slice_2",
              role: "assistant",
              sessionID: "sess-het",
              time: { completed: 52_000 },
            },
            sessionID: "sess-het",
          },
        },
      })
      await delay(100)
      await hooks.event({
        event: {
          type: "message.updated",
          properties: {
            info: {
              id: "m_slice_3",
              role: "assistant",
              sessionID: "sess-het",
              time: { completed: 53_000 },
            },
            sessionID: "sess-het",
          },
        },
      })
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
      await hooks["chat.system.transform"]({ sessionID: SES }, g1)
      const posted = hooks["chat.message"](
        { sessionID: SES, messageID: "m_pull_1" },
        { parts: [{ type: "text", text: "LanceDb 现在什么状态" }] },
      )
      await posted // the handler resolves; the ack microtask may not have run
      const g2 = { system: ["BASE2"] }
      await hooks["chat.system.transform"]({ sessionID: SES }, g2)
      await delay(50) // now the ack has landed
      const g3 = { system: ["BASE3"] }
      await hooks["chat.system.transform"]({ sessionID: SES }, g3)
      const g4 = { system: ["BASE4"] }
      await hooks["chat.system.transform"]({ sessionID: SES }, g4)
      console.log(
        JSON.stringify({
          systems: [g1.system, g2.system, g3.system, g4.system],
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
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_e1" },
        { parts: [{ type: "text", text: "第一轮" }] },
      )
      await delay(50)
      await hooks["chat.system.transform"]({ sessionID: SES }, e1)
      const e2 = { system: ["BASE2"] }
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_e2" },
        { parts: [{ type: "text", text: "第二轮" }] },
      )
      await delay(50)
      await hooks["chat.system.transform"]({ sessionID: SES }, e2)
      console.log(
        JSON.stringify({
          systems: [e1.system, e2.system],
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
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_f1" },
        { parts: [{ type: "text", text: "失败轮" }] },
      )
      await delay(50)
      await hooks["chat.system.transform"]({ sessionID: SES }, f1)
      failing.clear()
      const f2 = { system: ["BASE2"] }
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_f2" },
        { parts: [{ type: "text", text: "恢复轮" }] },
      )
      await delay(50)
      await hooks["chat.system.transform"]({ sessionID: SES }, f2)
      recallPayload = {
        enabled: false,
        items: [],
        non_focal_above_floor: 0,
        slot_consumed: false,
        budget_chars: 1200,
      }
      const f3 = { system: ["BASE3"] }
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_f3" },
        { parts: [{ type: "text", text: "禁用轮" }] },
      )
      await delay(50)
      await hooks["chat.system.transform"]({ sessionID: SES }, f3)
      console.log(
        JSON.stringify({
          systems: [f1.system, f2.system, f3.system],
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
      await hooks["chat.system.transform"]({ sessionID: SES }, i1)
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_pull_2" },
        { parts: [{ type: "text", text: "继续 LanceDb 的话题" }] },
      )
      await delay(50)
      // the host hands the transform the ACCUMULATED system array (i1's block
      // is already in it): the T2 block must COEXIST with the T1 block, never
      // replace or re-inject it. The copy keeps the transcript snapshot of
      // i1.system honest (a fresh array per call, like the real host).
      const i2 = { system: [...i1.system] }
      await hooks["chat.system.transform"]({ sessionID: SES }, i2)
      t2CiteTurnsVisible = true
      await hooks.event({ event: messageUpdatedAssistant("m_cite_t2_1", 8_000) })
      await delay(100)
      await hooks.event({ event: messageUpdatedAssistant("m_cite_t2_2", 9_000) })
      await delay(100)
      console.log(
        JSON.stringify({
          systems: [i1.system, i2.system],
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
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_budget" },
        { parts: [{ type: "text", text: "预算轮" }] },
      )
      await delay(50)
      await hooks["chat.system.transform"]({ sessionID: SES }, b1)
      console.log(
        JSON.stringify({
          systems: [b1.system],
          blockLength: b1.system.length > 1 ? b1.system[1].length : 0,
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
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_lowbudget" },
        { parts: [{ type: "text", text: "低预算轮" }] },
      )
      await delay(50)
      await hooks["chat.system.transform"]({ sessionID: SES }, b1)
      console.log(
        JSON.stringify({
          systems: [b1.system],
          blockLength: b1.system.length > 1 ? b1.system[1].length : 0,
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
      await hooks["chat.message"](
        { sessionID: SES, messageID: "m_consumed" },
        { parts: [{ type: "text", text: "已消费轮" }] },
      )
      await delay(50)
      await hooks["chat.system.transform"]({ sessionID: SES }, s1)
      const s2 = { system: ["BASE2"] }
      await hooks["chat.system.transform"]({ sessionID: SES }, s2)
      console.log(
        JSON.stringify({
          systems: [s1.system, s2.system],
          pullCount: posts.filter((post) => post.url.endsWith("/session/recall-pending")).length,
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
      await hooks["chat.system.transform"]({ sessionID: SES }, o1)
      console.log(JSON.stringify({ systems: [o1.system] }))
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
      await hooks["chat.system.transform"]({ sessionID: SES }, o1)
      console.log(JSON.stringify({ systems: [o1.system] }))
      break
    }

    case "completion-shape-debug": {
      // the DEBUG-gated shape lane: aborted assistant messages (no
      // time.completed) are logged into hook-debug.jsonl BEFORE the gate
      // drops them — with and without host error fields.
      await hooks.event({
        event: {
          type: "message.updated",
          properties: {
            info: {
              id: "m_aborted_meta",
              role: "assistant",
              sessionID: SES,
              time: { error: 42 },
              metadata: { error: "The operation was aborted due to timeout" },
            },
            sessionID: SES,
          },
        },
      })
      await delay(100)
      await hooks.event({
        event: {
          type: "message.updated",
          properties: {
            info: {
              id: "m_aborted_plain",
              role: "assistant",
              sessionID: SES,
              time: { error: 42 },
            },
            sessionID: SES,
          },
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
        .filter((line) => line.tag === "assistant completion shape")
        .map((line) => line.payload)
      console.log(JSON.stringify({ shapes }))
      break
    }

    case "config-inject": {
      // B2.6: the config hook registers cfg.mcp["mnemoseed"] create-if-absent
      // — an empty cfg, a cfg without the mcp map, and a cfg carrying a manual
      // registration (which must win untouched); a null cfg is a no-op.
      const empty = {}
      await hooks.config(empty)
      const bare = { mcp: {} }
      await hooks.config(bare)
      const manual = { mcp: { mnemoseed: { type: "remote", url: "http://mcp.example" } } }
      await hooks.config(manual)
      let noThrow = true
      try {
        await hooks.config(null)
      } catch {
        noThrow = false
      }
      console.log(JSON.stringify({ empty, bare, manual, noThrow }))
      break
    }

    case "config-inject-frozen": {
      // B2.6 I3: the config hook must be fail-open on frozen objects — a frozen
      // cfg and a frozen cfg.mcp must not throw, and must not overwrite other
      // keys. Both cases exercise the ??= paths with Object.freeze.
      let noThrow = true
      let frozenCfgOther = null
      let frozenMcpOther = null
      let frozenCfgMcp = null
      let frozenCfgHasMnemoseed = null
      let frozenMcpHasMnemoseed = null
      try {
        const frozenCfg = Object.freeze({ other: "keep" })
        await hooks.config(frozenCfg)
        frozenCfgOther = frozenCfg.other
        frozenCfgMcp = frozenCfg.mcp ?? null
        frozenCfgHasMnemoseed = frozenCfg.mcp?.mnemoseed ?? null
      } catch {
        noThrow = false
      }
      try {
        const frozenMcp = { other: "keep2", mcp: Object.freeze({ otherMcp: "keep2" }) }
        await hooks.config(frozenMcp)
        frozenMcpOther = frozenMcp.other
        frozenMcpHasMnemoseed = frozenMcp.mcp?.mnemoseed ?? null
      } catch {
        noThrow = false
      }
      // also check that a normal cfg still works after frozen attempts
      const after = { other: "keep3", mcp: {} }
      try {
        await hooks.config(after)
      } catch {
        noThrow = false
      }
      console.log(
        JSON.stringify({
          noThrow,
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
      // B2.6: the ["spec", {enabled:false}] tuple short-circuits the WHOLE
      // bundle — the factory returns {} (no config hook, no hooks);
      // enabled:true, empty options and absent options all load normally.
      const off = await pluginFactory({ client: fakeClient }, { enabled: false })
      const on = await pluginFactory({ client: fakeClient }, { enabled: true })
      const bare = await pluginFactory({ client: fakeClient }, {})
      const none = await pluginFactory({ client: fakeClient })
      console.log(
        JSON.stringify({
          offKeys: Object.keys(off),
          onKeys: Object.keys(on),
          bareKeys: Object.keys(bare),
          noneKeys: Object.keys(none),
        }),
      )
      break
    }

    default:
      console.error(`unknown scenario: ${scenario}`)
      process.exit(64)
  }
}

await main()
