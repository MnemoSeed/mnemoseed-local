// R2 trust runtime harness: loads the SHIPPED console app.js into a node VM
// with a minimal browser/DOM stub, then drives the provenance surfaces that
// static string-pins cannot: recallSummary (statusbar session-tail line) and
// the Drawer provenance/why-surfaced branch, over a chunk item.
//
// Gate guarded by node availability — see tests/test_console_provenance.py.
// Exit code 0 (all assertions pass) or non-zero with a message on stderr.
//
// Usage: node console_provenance.mjs <app.js path>

import { readFile } from "node:fs/promises"
import vm from "node:vm"

const [appPath] = process.argv.slice(2)
if (!appPath) {
  console.error("usage: node console_provenance.mjs <app.js path>")
  process.exit(64)
}

// ---- minimal mutable DOM stub: stores props so the harness can read back ----
function makeEl() {
  const el = {
    className: "", textContent: "", title: "", innerHTML: "", hidden: false,
    value: "", children: [],
    style: new Proxy({}, { get: () => "", set: () => true }),
    classList: { contains: () => false, toggle: () => true, add: () => {}, remove: () => {}, },
  }
  const handler = {
    get(t, p) {
      if (p === "nodeType") return 1
      if (p in t) return t[p]
      // any method used on drawer elements no-ops (handlers are assigned, not run)
      return () => {}
    },
    set(t, p, v) { t[p] = v; return true },
  }
  return new Proxy(el, handler)
}
makeEl.addEventListener = () => {}

const documentStub = {
  addEventListener() {},
  removeEventListener() {},
  createElement() { return makeEl() },
  querySelector() { return makeEl() },
  querySelectorAll() { return [] },
  activeElement: makeEl(),
}

function statusbarStub() {
  const sb = { children: [], appendChild(el) { this.children.push(el) } }
  return sb
}

// ---- bootstrap app.js in a fresh context, return the shared handles ----
function loadApp(appSource, { fetch }) {
  const window = {
    addEventListener() {}, removeEventListener() {},
    location: { hash: "#/memory/atlas" },
    __mnemoseed_console: undefined,
  }
  const sandbox = {
    window,
    document: documentStub,
    location: window.location,
    navigator: { clipboard: { writeText: async () => {} } },
    fetch,
    AbortController,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Intl, console, confirm: () => false, requestAnimationFrame: (fn) => setTimeout(fn, 0),
    URL, Blob, Date, Math, JSON, RegExp, Error, Promise, Number, String, Array, Object, Boolean,
  }
  sandbox.globalThis = sandbox
  new vm.Script(appSource, { filename: "app.js" }).runInNewContext(sandbox)
  new vm.Script(
    "globalThis.__h = { recallSummary, openDrawer, atlasState, EXPLICIT_PIN_SOURCE };",
  ).runInContext(sandbox)
  return sandbox.__h
}

function fail(msg) {
  console.error(`console_provenance.mjs FAIL: ${msg}`)
  process.exit(1)
}

const appSource = await readFile(appPath, "utf8")

// -------- 1) recallSummary exists and app.js loads (undefined-call guard) ----
let served = { sessions: [{ session_id: "s1", chunks: [{ text: "a" }, { text: "b" }] }, { session_id: "s2", chunks: [{ text: "c" }] }] }
const H = loadApp(appSource, {
  fetch: async () => ({ ok: true, json: async () => served }),
})
if (typeof H.recallSummary !== "function") fail("recallSummary is not a function")
if (typeof H.openDrawer !== "function") fail("openDrawer is not a function")
if (H.EXPLICIT_PIN_SOURCE !== "memory.remember") fail("EXPLICIT_PIN_SOURCE is not memory.remember")

// -------- 2) statusbar line: honest session-tail copy, F1/N4 ----------------
{
  const sb = statusbarStub()
  await H.recallSummary(sb, "default", new AbortController().signal)
  const line = sb.children.map((c) => c.textContent).join("")
  if (!line) fail("recallSummary appended nothing")
  if (!line.includes("Newest session tail: 3 chunk(s) across 2 recent session(s)"))
    fail(`unexpected statusbar line: "${line}"`)
  if (line.includes("auto-recall served"))
    fail('statusbar still claims "auto-recall served" despite F1')
  if (/pinned/i.test(line))
    fail("statusbar should not count pins (no source on /session/recent)")
}

// -------- 3) F2 race: a stale resolve after abort is discarded --------------
{
  let resolver
  const gate = new Promise((r) => { resolver = r })
  const H2 = loadApp(appSource, {
    fetch: async () => ({ ok: true, json: () => gate }),
  })
  const sb = statusbarStub()
  const ac = new AbortController()
  const p = H2.recallSummary(sb, "default", ac.signal)
  ac.abort() // re-fetch / profile-switch aborts the previous controller
  resolver({ sessions: [{ chunks: [{ text: "stale" }] }] }) // resolves late
  await p
  if (sb.children.length !== 0)
    fail(`stale resolve must be discarded after abort; got ${sb.children.length} append(s)`)
}

// -------- 4) Drawer provenance/why-surfaced branch over a chunk (N7) ---------
{
  const chunkItem = {
    id: "chunk-abc123",
    kind: "chunks",
    text_head: "prefer zero-copy data paths",
    flags: { explicit_pin: true, needs_reconcile: false },
    source: "memory.remember", // EXPLICIT_PIN_SOURCE → single-comparison pin
    asserted_by: "user",
    ingested_at: Date.now() / 1000 - 3600,
    decay_weight: 0.5,
    score: 0.9,
  }
  const Hn = loadApp(appSource, { fetch: async () => ({ ok: true, json: async () => ({ sessions: [] }) }) })
  Hn.atlasState.items = [chunkItem]
  try {
    await Hn.openDrawer(chunkItem.id)
  } catch (e) {
    fail(`openDrawer threw on a pinned chunk item: ${e.message}`)
  }
}

console.log("console_provenance.mjs OK")