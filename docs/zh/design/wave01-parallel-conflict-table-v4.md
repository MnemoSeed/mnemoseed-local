# Wave 0+1 并行工程批冲突对账表 (v4)

> Status: IMPLEMENTATION EVIDENCE. v4 upgrades v3 for the W1-B v4 re-freeze (I-1 secret write/read channel). Machine-checkable conflict accounting only; no new engineering authorization.
> Base: branch `feat/381-b7-pr3-http-wizard` on `f6e7c481` (B-1 fix, QA-closed). Main `ebf955b` (PR #446/#441) touched only README/design14/PRD-309/atlas/test — no W1-B file surface.
> Semantic SET authoritative; literal line numbers are navigation aids only.

## 1. File surface / test-oracle / ordering matrix (v4)

| Item | Primary write surface | Test-oracle write surface | Overlaps with | Nature | Verdict |
|---|---|---|---|---|---|
| W1-B S5 #381 | `llm/routes.py`, `daemon/app.py` (+1 include_router), design/04 §1.4 | `test_dream_status_payload.py` HTTP set +4; `test_llm_routes.py` | PR #432 (same frozen HTTP set) | same file same list | #432 merged first; merged bytes re-pinned |
| W1-B B-1 fix | `llm/routes.py`, `test_llm_routes.py` | (same file) | S5 (same file) | same file, different regions | committed `f6e7c481`, QA-closed; carried forward unchanged |
| W1-B S5c/v4 (new) | NEW `daemon/secrets_routes.py`; `daemon/app.py` (2nd include_router + `create_app(secret_store=)` seam + `app.state.secret_store` + `_build_capture` param + `RoleRouter(secrets=)`); `app.js` `wizardBind()`; design/04 §1.4 +1 | NEW `test_secrets_routes.py`; `test_dream_status_payload.py` HTTP set +1 | S5/S6 (same app.py, app.js, frozen-set file) | same file, serial points | S5 → S5c → S6 in one worktree; no parallel dispatch |
| W1-B S6 #381 | `console/static/app.js`, `index.html`, `style.css` | `ts_hook/provider_preselection.mjs`, `test_console_role_batch.py::test_preselection` (pytest wrapper, subprocess node) | PR #432; S5 (endpoints); S5c (wizard secret-first) | same file adjacent logic + runtime dep | #432 first; S5 → S5c → S6 serial chain; wrapper write surface registered |
| W1-A / W1-C / W1-D | disjoint from W1-B | disjoint test files | none | no overlap | parallel groups unchanged from v3 |

## 2. Serial file-surface points (v4, hard)

1. `daemon/app.py`: S5a `include_router(llm_router)` → S5c 2nd `include_router(secrets_router)` + `create_app` seam + `_build_capture`/`RoleRouter` wiring. One worktree, sequential; no two writers.
2. `console/static/app.js`: S6a wizard → S6c `wizardBind()` secret-first. One worktree, sequential.
3. `tests/test_dream_status_payload.py`: S5b +4 HTTP rows → S5c +1 (`/api/v1/secrets/set`) as one serial edit.
4. `tests/test_secrets_routes.py`: S5c-only new file; disjoint from every other write surface.

## 3. Frozen shared oracles (v4 re-pinned)

1. `test_dream_status_payload.py::test_external_surface_inventory_equals_frozen_baseline`: exact HTTP path set (now **39** members, +4 LLM +1 secrets), MCP tool-name set, console page-route set `{overview,atlas,config,profiles,dream}` (no `#/wizard`). Semantic SET authoritative.
2. `test_daemon_caller_credential_guard.py`: protected-route credential acquisition; the new `/api/v1/secrets/set` inherits default-deny (no `OPEN_PATHS` change, no exemption).
3. Four-sink key-outflow oracle (response / audit / daemon.log / `str(exc)`) over full value bytes; mutants M1 masked_tail, M2 str(exc), M3 422 echo, M4 allowlist bypass + M5 wizard order + M6 read wiring all KILLED with byte-exact restore.
4. I-R4-1 isolation: bare `create_app()` callers never probe the OS keyring (lazy/injectable store).
5. Preselection gate I-B4/I-B6 unchanged; V gate (legacy-scan) re-run on the changed web surface.

## 4. Finding status (v4)

| ID | Source | Disposition |
|---|---|---|
| I-R4-1 | design QA round 4 | CLOSED: lazy `LazySecretStore` + `create_app(secret_store=)` injection; keyring-forced-failure probe shows bare callers boot |
| N-R4-1 | design QA round 4 | CLOSED: `app.state.secret_store` made explicit; `_build_capture` takes a `secret_store` param; evidence states scope |
| N-R4-2 | design QA round 4 | CLOSED: this v4 conflict table pinned |
| I-1 | implementation QA | ADDRESSED: write route + read wiring + secret-first wizard order (v4 implementation QA to CLOSE) |
| B-1 | implementation QA | CLOSED on `f6e7c481`; carried forward, not reworked |

## 5. Remaining open risks

- Full 5000+ suite not run in this batch (frozen command list only); independent implementation QA required before PR.
- Installed runtime and port 7788 untouched; all probes used temp home and isolated clients.
