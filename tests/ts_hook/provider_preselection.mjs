// Mandatory preselection gate (default-effect anchor): the provider wizard's
// FIRST render must have zero preselected provider radios. Loads the shipped
// app.js into a node VM, renders the wizard first screen with a stub catalog,
// and asserts every provider radio has checked === false.
//
// Exit 0 with "provider_preselection.mjs OK" when all assertions pass;
// non-zero with a message on stderr otherwise.
// Usage: node provider_preselection.mjs <app.js path>

import { readFile } from "node:fs/promises"
import vm from "node:vm"

const [appPath] = process.argv.slice(2)
if (!appPath) {
  console.error("usage: node provider_preselection.mjs <app.js path>")
  process.exit(64)
}

function makeEl() {
  const el = {
    className: "", textContent: "", title: "", innerHTML: "", hidden: false,
    value: "", dataset: {}, children: [],
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
  querySelector() { return makeEl() },
  querySelectorAll() { return [] },
  activeElement: makeEl(),
}

function loadApp(appSource) {
  const location = { hash: "#/config" }
  const window = { addEventListener() {}, removeEventListener() {}, location }
  const sandbox = {
    window, document: documentStub, location,
    navigator: { clipboard: { writeText: async () => {} } },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    AbortController,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Intl, console, confirm: () => false, requestAnimationFrame: (fn) => setTimeout(fn, 0),
    URL, Blob, Date, Math, JSON, RegExp, Error, Promise, Number, String, Array, Object, Boolean,
  }
  sandbox.globalThis = sandbox
  new vm.Script(appSource, { filename: "app.js" }).runInNewContext(sandbox)
  new vm.Script(
    "globalThis.__h = { providerWizardHtml: (typeof providerWizardHtml !== 'undefined') ? providerWizardHtml : undefined };",
  ).runInContext(sandbox)
  return sandbox.__h
}

function fail(msg) {
  console.error(`provider_preselection.mjs FAIL: ${msg}`)
  process.exit(1)
}

const appSource = await readFile(appPath, "utf8")
const H = loadApp(appSource)
if (typeof H.providerWizardHtml !== "function") fail("providerWizardHtml is not a function")

const catalog = {
  presets: [
    { id: "openai", display: "OpenAI" },
    { id: "anthropic", display: "Anthropic" },
    { id: "custom", display: "Custom" },
  ],
}
const html = H.providerWizardHtml(catalog)

const radios = [...html.matchAll(/<input\b[^>]*\btype="radio"[^>]*>/g)].map((m) => ({
  tag: m[0],
  checked: /\bchecked\b/.test(m[0]),
}))
if (radios.length === 0) fail("no provider radios rendered (vacuous pass)")
for (const id of ["openai", "anthropic", "custom"]) {
  if (!html.includes(`value="${id}"`)) fail(`missing provider radio for ${id}`)
}
for (const radio of radios) {
  if (radio.checked === true) fail(`provider radio is preselected: ${radio.tag}`)
}

console.log("provider_preselection.mjs OK")
