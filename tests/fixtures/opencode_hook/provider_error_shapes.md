# Provider-error entry-gate shapes (B1 Section 9.4, non-live)

Sanitized fixtures pinning WHERE provider/model/status arrive on current
OpenCode payloads. All values synthetic; no secrets, no live captures.

Provenance: shapes read from the shipped `plugin.ts` call-sites
(`modelIdOf`, `onMessageCompleted`, `session.error` branch) against the
local OpenCode 1.18.21 toolchain on 2026-09-03. No PP-0 daemon, no live
store, no port 7788 was touched.

## message.updated (assistant turn failure)

- `info.providerID` / `info.modelID` — combined by `modelIdOf` into
  `provider/model`, split on the first `/` for the fingerprint.
- `info.metadata.error` (fallback `info.error`) — the actual error
  payload/text. Leads classification; the numeric `info.time.error`
  stamp is fallback-only and must never shadow real text.
- 429 text with quota wording → `quota` / `provider_429_quota`
  (fixture `provider_error_429.json`).

## session.error (status-less hang)

- `properties.info.providerID` / `properties.info.modelID`.
- `properties.error` abort/no-response wording, no numeric status →
  `timeout` / `provider_timeout_no_status`
  (fixture `provider_error_timeout_no_status.json`).

## Negative (never nominates)

- Compile/build/tool text on `info.metadata.error` — even with
  `providerID`/`modelID` present — returns no nomination
  (fixture `provider_build_error_negative.json`).
