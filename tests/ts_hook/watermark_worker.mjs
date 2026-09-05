// Two-process watermark convergence worker for issue #171 (P2).
// Usage: node watermark_worker.mjs <bundle> <dataDir> <workerIndex> <rounds>
// Shares one DATA_DIR with a sibling worker process. Uses the env-gated
// test seam (__watermarkTest) to note synthetic keys and persist through the
// real persist path. Synthetic IDs only; no network, no user data.
import { pathToFileURL } from "node:url";

const [bundle, dataDir, workerArg, roundsArg] = process.argv.slice(2);
if (!bundle || !dataDir || workerArg === undefined || !roundsArg) {
  console.error("usage: node watermark_worker.mjs <bundle> <dataDir> <workerIndex> <rounds>");
  process.exit(64);
}
const workerIndex = Number(workerArg);
const rounds = Number(roundsArg);

process.env.MNEMOSEED_LOCAL_DATA_DIR = dataDir;
process.env.MNEMOSEED_LOCAL_WATERMARK_TEST = "1";

globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

const fakeClient = { session: { messages: async () => ({ data: [] }) } };
const pluginFactory = (await import(pathToFileURL(bundle))).default;
const hooks = await pluginFactory({ client: fakeClient });
const seam = hooks.__watermarkTest;
if (!seam) {
  console.error("watermark test seam missing");
  process.exit(50);
}

const OVERLAP_KEYS = 8;
for (let round = 0; round < rounds; round += 1) {
  for (let k = 0; k < OVERLAP_KEYS; k += 1) {
    seam.note(`wm-test-shared-${k}`, 1000 + round * 10 + workerIndex);
  }
  seam.note(`wm-test-w${workerIndex}-own`, 2000 + round * 10);
  seam.note(`wm-test-w${workerIndex}-round-${round % 4}`, 3000 + round);
  await seam.persist();
}
console.log(JSON.stringify({ workerIndex, rounds, shardPath: seam.shardPath() }));
