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
const RECENT_PAYLOAD = {
  profile_id: "default",
  sessions: [
    {
      session_id: "sess-prev-new",
      latest_at: 40.0,
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

function historyEntries() {
  const entries = [
    {
      info: { id: "m_old_user", role: "user", sessionID: SES, time: { created: 1_000 } },
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

    default:
      console.error(`unknown scenario: ${scenario}`)
      process.exit(64)
  }
}

await main()
