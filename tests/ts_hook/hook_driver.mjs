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
globalThis.fetch = async (url, init) => {
  const body = JSON.parse(init.body)
  const accepted = !failing.has(url)
  posts.push({ url, body, accepted })
  if (!accepted) return { ok: false, status: 503 }
  return { ok: true, status: 200 }
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const SES = "sess-behavior"
// outage-hole: after the daemon rejects the live turn, the HOST's own store
// still carries it — the fake listing "learns" it when the driver flips this.
let outageTurnVisible = false

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

    default:
      console.error(`unknown scenario: ${scenario}`)
      process.exit(64)
  }
}

await main()
