// T4a needle oracle driver: extracts the SHIPPED needle functions straight
// from plugin.ts source (consts + function bodies), evals them, and prints
// normalize/needles/sanitize results for a canned corpus. pytest compares
// the output byte-for-byte with the Python oracle — the pin is the shipped
// source itself, never a re-implementation.
//
// usage: node needle_oracle.mjs <plugin.ts path> <corpus.json path>
// stdout: one JSON line: [{ normalized, needles, sanitized }, ...]

import { readFile } from "node:fs/promises"

const [pluginPath, corpusPath] = process.argv.slice(2)
if (!pluginPath || !corpusPath) {
  console.error("usage: node needle_oracle.mjs <plugin.ts> <corpus.json>")
  process.exit(64)
}

const src = await readFile(pluginPath, "utf8")

function grab(pattern, label) {
  const match = src.match(pattern)
  if (!match) throw new Error(`needle oracle: ${label} not found in plugin.ts`)
  return match[1]
}

function extractFunction(name) {
  // From the `function name(` line to its balanced closing brace. The bodies
  // carry one nested brace level (the if-block in needlesOf); plain regex
  // cannot balance, so count braces char by char.
  const startMatch = src.match(new RegExp(`function ${name}\\(`))
  if (!startMatch) throw new Error(`needle oracle: function ${name} missing`)
  const open = src.indexOf("{", startMatch.index)
  let depth = 0
  let i = open
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++
    else if (src[i] === "}") {
      depth--
      if (depth === 0) {
        i++
        break
      }
    }
  }
  let body = src.slice(startMatch.index, i)
  // strip the TS parameter/return annotations: these functions all take a
  // single `text: string` and return a string/array — the eval needs JS.
  body = body.replace(/^(function \w+)\([^)]*\)(?::[^{]*)?\{/, "$1(text) {")
  // strip the TS generic on the local Set (needlesOf); any NEW generic in
  // the extracted bodies fails the node run loudly (source-level pin).
  body = body.replace(/<string>/g, "")
  return body
}

const constants = [
  `const NEEDLE_HEAD_LEN = ${grab(/const NEEDLE_HEAD_LEN = (\d+)/, "NEEDLE_HEAD_LEN")}`,
  `const NEEDLE_MIN_CONTENT = ${grab(/const NEEDLE_MIN_CONTENT = (\d+)/, "NEEDLE_MIN_CONTENT")}`,
  `const NEEDLE_MID_THRESHOLD = ${grab(/const NEEDLE_MID_THRESHOLD = (\d+)/, "NEEDLE_MID_THRESHOLD")}`,
  `const RECALL_FENCE_SANITIZED = ${grab(/const RECALL_FENCE_SANITIZED = ("[^"]*")/, "RECALL_FENCE_SANITIZED")}`,
]
const functions = [
  extractFunction("normalizeRecallText"),
  extractFunction("needlesOf"),
  extractFunction("sanitizeRecallText"),
]
eval([...constants, ...functions, "globalThis.__oracle = { normalizeRecallText, needlesOf, sanitizeRecallText }"].join("\n"))

const corpus = JSON.parse(await readFile(corpusPath, "utf8"))
const out = corpus.map((text) => ({
  normalized: __oracle.normalizeRecallText(text),
  needles: __oracle.needlesOf(text),
  sanitized: __oracle.sanitizeRecallText(text),
}))
console.log(JSON.stringify(out))