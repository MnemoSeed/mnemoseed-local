// Role-bind batch harness: loads the SHIPPED console app.js into a node VM and
// drives the LLM-role Save controls with stubbed DOM/fetch. Asserts the bind
// flow writes driver+model in one atomic set-many batch, driver first.
//
// Exit code 0 (all assertions pass) or non-zero with a message on stderr.
// Usage: node console_role_batch.mjs <app.js path>

import { readFile } from "node:fs/promises"
import vm from "node:vm"

const [appPath] = process.argv.slice(2)
if (!appPath) {
  console.error("usage: node console_role_batch.mjs <app.js path>")
  process.exit(64)
}

const INPUTS = new Map([
  ['[data-key="dream.llm.dream.driver"]', "ollama"],
  ['[data-key="dream.llm.dream.model"]', "llama3.1:8b"],
  ['[data-key="dream.llm.dream_verifier.driver"]', "openai_compatible"],
  ['[data-key="dream.llm.dream_verifier.model"]', "gpt-x"],
])

function makeEl(attrs = {}) {
  const el = {
    className: "", textContent: "", title: "", innerHTML: "", hidden: false,
    value: attrs.value ?? "", dataset: attrs.dataset ?? {}, children: [],
    style: new Proxy({}, { get: () => "", set: () => true }),
    classList: { contains: () => false, toggle: () => true, add: () => {}, remove: () => {} },
  }
  return new Proxy(el, {
    get(t, p) { if (p in t) return t[p]; return () => {} },
    set(t, p, v) { t[p] = v; return true },
  })
}

const documentStub = {
  addEventListener() {}, removeEventListener() {},
  createElement() { return makeEl() },
  querySelector(s) { return INPUTS.has(s) ? makeEl({ value: INPUTS.get(s) }) : makeEl() },
  querySelectorAll() { return [] },
  activeElement: makeEl(),
}

function loadApp(appSource, fetch) {
  const location = { hash: "#/config" }
  const window = { addEventListener() {}, removeEventListener() {}, location }
  const sandbox = {
    window, document: documentStub, location,
    navigator: { clipboard: { writeText: async () => {} } },
    fetch, AbortController,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Intl, console, confirm: () => false, requestAnimationFrame: (fn) => setTimeout(fn, 0),
    URL, Blob, Date, Math, JSON, RegExp, Error, Promise, Number, String, Array, Object, Boolean,
  }
  sandbox.globalThis = sandbox
  new vm.Script(appSource, { filename: "app.js" }).runInNewContext(sandbox)
  new vm.Script(
    "globalThis.__h = { saveConfigKey, setGen: (n) => { configGeneration = n } };",
  ).runInContext(sandbox)
  return sandbox.__h
}

function fail(msg) {
  console.error(`console_role_batch.mjs FAIL: ${msg}`)
  process.exit(1)
}

const appSource = await readFile(appPath, "utf8")
let recorded = []
const H = loadApp(appSource, async (path, opts) => {
  recorded.push({ path, opts })
  return { ok: true, status: 200, json: async () => ({}), text: async () => "" }
})

function assertBatch(role, driver, model) {
  const many = recorded.filter((c) => c.path === "/api/v1/config/set-many")
  if (many.length !== 1) fail(`expected exactly one set-many call, got ${many.length}`)
  const opts = many[0].opts
  const body = JSON.parse(opts.body)
  if (!Array.isArray(body.updates) || body.updates.length !== 2) {
    fail(`updates must hold driver+model, got ${JSON.stringify(body.updates)}`)
  }
  const expected = [
    { key_path: `dream.llm.${role}.driver`, value: driver },
    { key_path: `dream.llm.${role}.model`, value: model },
  ]
  for (let i = 0; i < expected.length; i++) {
    if (body.updates[i].key_path !== expected[i].key_path || body.updates[i].value !== expected[i].value) {
      fail(`updates[${i}] = ${JSON.stringify(body.updates[i])}, expected ${JSON.stringify(expected[i])}`)
    }
  }
  for (const call of recorded.filter((c) => c.path === "/api/v1/config/set")) {
    const single = JSON.parse(call.opts.body)
    if (typeof single.key_path === "string" && single.key_path.startsWith("dream.llm.")) {
      fail(`role field written one key at a time via config/set: ${single.key_path}`)
    }
  }
  const headers = opts.headers || {}
  if (headers["if-match"] !== "7" || headers["If-Match"] !== "7") {
    fail(`set-many must carry the generation as If-Match: ${JSON.stringify(headers)}`)
  }
}

for (const [key, role, driver, model] of [
  ["dream.llm.dream.driver", "dream", "ollama", "llama3.1:8b"],
  ["dream.llm.dream.model", "dream", "ollama", "llama3.1:8b"],
  ["dream.llm.dream_verifier.driver", "dream_verifier", "openai_compatible", "gpt-x"],
]) {
  recorded = []
  H.setGen(7)
  await H.saveConfigKey(key)
  assertBatch(role, driver, model)
}

console.log("console_role_batch.mjs PASS: driver+model batched in one set-many write, driver first")
