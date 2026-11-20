# W1-B #381 B7 PR3 — implementation evidence (S5 HTTP + S6 wizard)

> Base: merged main `500147037ff97fd1a9f25f8e42e50b375ed4d9bd` (PR #432 merged). Branch `feat/381-b7-pr3-http-wizard`, single-writer claim `senior-software-engineer`.
> Declared model: `opencode/hy4-flash-free`; physical attestation UNKNOWN (declared-model QUALITY only).
> Isolation: temp `MNEMOSEED_LOCAL_HOME`, spare ports via `unused_loopback_port` (7788 only observed occupied by the ambient daemon, never used), installed runtime untouched, no live provider API calls (MockTransport / node VM ad-hoc drivers only).

## 1. Files + final SHA-256 (post-format)

| File | SHA-256 | Baseline → final | What/why |
|---|---|---|---|
| `src/mnemoseed_local/llm/routes.py` | `DACA08981F8705A99800085B1D1805B3ACD426E5AB56F4299FE386F4CEC4ACA9` | new | four HTTP endpoints, whitelist scrubbing, ad-hoc driver seam |
| `src/mnemoseed_local/daemon/app.py` | `13CAA0753C047DCE1C06248D0D7C1D6B4195F759CCD4126866A11F38EEA93CF0` | `AE95EB74…` → | import + one `include_router(llm_router)` line |
| `src/mnemoseed_local/console/static/app.js` | `01553393A7EB3DD5298848258ED60615FCE4FD899067AC87FDDDE0A73C2DC9D6` | `D7F66731…F1F6` → | config-page wizard, shared `writeRoleBatch`, driver hints |
| `src/mnemoseed_local/console/static/index.html` | `2199C9B2DD4582E3B4B241F4944E256B5933C63C96200D9578FA50B0E4AFFC74` | `0CECFB40…` → | wizard open button + `<dialog>` modal |
| `src/mnemoseed_local/console/static/style.css` | `CB336577F6AB15B506B49AD132A16161E0DC9E9E383ECB92E8E32476AB198E5A` | new edits | radio-card `:has(:checked)` + search + model list CSS |
| `tests/test_llm_routes.py` | `BAA83E8BB652A2A5989732BECB3D59D3C2CFA5808F88290A38454AF87FC64B7A` | new | S5 contract + four-sink key-outflow tests |
| `tests/test_console_role_batch.py` | `4D677BDB78EA8435CFA40C00F90C800293A3C4F1E9D6894921195D8CD6957002` | `F9746713…` → | `test_preselection` mandatory wrapper (no skip, `check=True`) |
| `tests/ts_hook/provider_preselection.mjs` | `E128D7B3FC96D473A5CCFEDEA12EC16CE3A06E116DA7146C5374071333969A23` | new | node VM first-render zero-preselection oracle |
| `tests/test_dream_status_payload.py` | `F75FD5CD35B569FE7C02D5390E108EF9170F08AA262FD2FF8D74144033C6D8EB` | `E77BE1CF…FB1C8` → | frozen HTTP set +4 LLM routes |
| `docs/zh/design/04-trust-boundary-and-config.md` | (doc) | §1.4 +4 endpoint rows | trust-boundary pin |
| `docs/zh/design/wave01-parallel-conflict-table-v3.md` | (doc) | new | N-B4 v3 conflict table |
| `docs/verification/web-ui-gate-evidence.md` | (doc) | new | V gate raw JSON + exceptions + verdict |

## 2. RED → GREEN pairs (verbatim failure lines)

1. **S5 contract absent (natural RED).** `uv run pytest -q tests/test_llm_routes.py` → `ImportError: cannot import name 'routes' from 'mnemoseed_local.llm'`, `1 error during collection`, EXIT 2. Implemented routes.py → 12 passed (now 13).
2. **Frozen list absent (natural RED).** `uv run pytest -q tests/test_dream_status_payload.py -k test_external_surface_inventory_equals_frozen_baseline` → `Extra items in the right set: '/api/v1/llm/catalog','/api/v1/llm/probe','/api/v1/llm/models','/api/v1/llm/detect-local'`, `1 failed`, EXIT 1. Added `include_router` → green.
3. **Key-outflow mutant (I-8 / S5-AC3).** Mutated `routes._safe_report` to echo `detail["error"]` (which carried the api key and base_url query). `uv run pytest -q tests/test_llm_routes.py -k no_key_in_response` → `AssertionError: assert 'sk-live-secret-9f3a2b' not in '{"ok":false...model=gpt-x"}'`, 2 failed, EXIT 1. Restored from `routes.py.GREEN.bak` → SHA `511E8868…CE82` MATCH, 2 passed. (Final bytes re-formatted by ruff to `DACA0898…`.)
4. **Frozen-route mutant.** Removed `app.include_router(llm_router)` → frozen inventory `1 failed`, EXIT 1. Restored → SHA `4A78C32F…EA38` MATCH (final ruff-sorted `13CAA075…`).
5. **S6 preselection absent (natural RED).** `uv run pytest -q tests/test_console_role_batch.py -k preselection` → `CalledProcessError ... returned non-zero exit status 1`; direct driver: `provider_preselection.mjs FAIL: providerWizardHtml is not a function`, EXIT 1. Implemented wizard → `1 passed, 1 deselected`, EXIT 0.
6. **Preselection mutant (E-B4 / default-effect anchor).** Injected `checked` into a provider radio → gate `CalledProcessError ... returncode 1`, EXIT 1. Restored app.js → SHA `01553393…D6` MATCH.
7. **Binding-sequence mutant (S6-AC4).** Replaced `writeRoleBatch` set-many with per-key `config/set` → `console_role_batch.mjs FAIL: expected exactly one set-many call, got 0`, wrapper `1 failed`, EXIT 1. Restored → SHA `01553393…D6` MATCH.
8. **I-B4 gate command.** No test named preselection would collect → exit 5 safe RED (round-3 empirical); with wrapper: `1 passed`, EXIT 0. Direct debug `node tests/ts_hook/provider_preselection.mjs <app.js>` → `provider_preselection.mjs OK`, EXIT 0.
9. **I-B5 re-pin.** Merged-byte anchors: `assert http_paths` `:489`, set-many `:499`, `assert tools` **`:523`**, `assert page_routes` **`:529`**, HTTP set `:489-519`. After S5 +4 insertion they shift to tools `:527`, page_routes `:533`, HTTP set `:489-523`; semantic SET authoritative, literal number not a contract.
10. **I-B6 node-missing red (real gate).** Stripped `nodejs` from PATH → `uv run pytest -q -p no:cacheprovider tests/test_console_role_batch.py -k preselection` → `Failed: node is required for the mandatory preselection gate`, `1 failed, 1 deselected`, EXIT 1. Counterfactual with reintroduced `pytest.skip` + node missing → `1 skipped`, EXIT 0 (false green) — hence the oracle demands exactly `1 passed`, not skipped. Real test file SHA unchanged `4D677BDB…`.
11. **V gate.** legacy-scan over app.js/index.html/style.css → 6 findings, all pre-existing (present in `git show HEAD`, none added by S6); recorded justified EXCEPTIONs in `web-ui-gate-evidence.md`; raw JSON retained; `mwgrcHash=ABSENT`.

Byte-exact restoration backups: `docs/verification/w381-evidence/{routes.py,app.js}.GREEN.bak`.

## 3. No-key-in-four-sinks proof

`tests/test_llm_routes.py` uses `KEY='sk-live-secret-9f3a2b'` and `KEY_QUERY_URL` embedding it in `base_url` query. Sinks asserted:
- **response body** — `test_probe_no_key_in_response_when_driver_echoes_key` / `..._when_key_embedded_in_base_url_query` / `test_models_returns_scrubbed_model_list` assert `KEY not in resp.content` (full bytes, not just `api_key` field).
- **audit** — `_SpyAudit` attached to `app.state.audit`; `test_probe_no_key_in_audit_or_daemon_log` asserts no entry carries KEY. Routes write no audit at all.
- **daemon.log** — same test wraps call in `caplog.at_level(DEBUG)` and asserts `KEY not in caplog.text`; request-log on/off both covered (router-level no middleware + daemon credential-boundary suite 164 passed).
- **exception `str(exc)`** — `test_probe_no_key_in_exception_text_when_constructor_raises` calls `probe()` directly with a driver whose constructor raises with KEY; asserts `KEY not in str(HTTPException)` (`raise ... from None` suppresses the chained key-bearing LLMError).
Probe uses an ad-hoc `LLMRegistry.build()` + `check()` and drops the instance; no RoleRouter import/cache, no persistence. Mutation #3 above proves the response-sink oracle kills a key-echo regression.

## 4. Frozen route set (exact members, post-S5)

`{ /healthz, /health, /api/v1/observability, /api/v1/audit, /api/v1/host/capture-evidence, /api/v1/profiles, /api/v1/profiles/archive, /api/v1/config, /api/v1/config/set, /api/v1/config/set-many, /api/v1/config/versions, /api/v1/config/rollback, /api/v1/llm/catalog, /api/v1/llm/probe, /api/v1/llm/models, /api/v1/llm/detect-local, /daemon/shutdown, /ingest, /session/end, /session/active, /flush, /session/recall-pending, /session/recent, /session/windows, /memory/atlas, /memory/audit, /memory/error_events, /memory/export, /memory/forget_this, /memory/reinforce, /memory/timeline, /mcp, *expected_paths }`
Gains exactly the four LLM routes; set-many (from #432) retained. Page-route set unchanged `{overview, atlas, config, profiles, dream}`; no `#/wizard`. MCP tool-name set unchanged. No `app.mount()` route added.

## 5. Verbatim final gate outputs

```
uv run pytest -q tests/test_llm_routes.py tests/test_console_role_batch.py tests/test_dream_status_payload.py tests/test_configwrite_setmany.py
  33 passed, 1 warning in 7.40s                                   EXIT 0

uv run pytest -q tests/test_console_role_batch.py -k preselection
  1 passed, 1 deselected in 0.32s                                 EXIT 0

legacy-scan --json app.js index.html style.css (node v22.23.5)
  totalFindings 6; all pre-existing; exceptions recorded; verdict PASS; mwgrcHash=ABSENT   EXIT 0

uv run ruff check                    All checks passed!                                      EXIT 0
uv run ruff format --check            640 files already formatted                             EXIT 0
uv run mypy src                       Success: no issues found in 181 source files            EXIT 0
(additional) credential boundary + caller guard suites                                         EXIT 0
  164 passed, 1 warning in 16.80s
```

Modern Web Guidance section (per skill): retrieved `forms`, `card`, `autofill-sign-in-form` and applied: `type="search"`/`enterkeyhint="search"` for the model filter, `<label class="card">` radios styled with `:has(> :checked)` via outline/border-color only ("Never change layout-affecting properties such as `border-width`, `padding`, or `font-weight`"), and password Show/Hide with `aria-pressed`/`role="status"` — preventing layout jump on selection and IME/ARIA pitfalls.

## 6. Residual risks / out-of-scope

- The console binds the `secrets:mnemoseed/dream/dream` reference via set-many and sends the raw key only to `/probe`; no dedicated HTTP secret-value write endpoint exists in the frozen 4-route surface, so persisting the key value itself to SecretStore needs a follow-up endpoint (not in PR3 scope). Four-sink absence is proven.
- API key is scrubbed by dropping raw error text; provider error detail is limited to status + model ids.
- S7 OAuth, runtime provider downgrade, eval roster, G0-4 README reconciliation remain out of scope.
- Full 5000+ test suite not run this batch (frozen command list is the four target files + preselection + lint/type); credential-boundary suites run additionally.

## 7. v4 re-freeze (I-1 secret write/read channel) — implementation evidence

> Scope: owner-mandated re-freeze `freeze-wave01-w1b-v4-refreeze.md`; design/slices/brief/delta v4 and round-4 design QA CLOSABLE. Implemented on top of the B-1 fix commit `f6e7c481` in the same worktree/branch; B-1 is unchanged and remains in the diff.
> Base drift audit: main advanced to `ebf955b` (PR #446/#441) touching README/design14/PRD-309/atlas/test only — no W1-B file surface. File baselines re-taken; `secrets/refs.py` git blob `954D31EE…` (worktree on-disk differs only by CRLF). No merge/rebase required.
> Isolation: temp `MNEMOSEED_LOCAL_HOME`, in-memory/temp stores injected, no keyring probe in bare callers, port 7788 only observed, installed runtime untouched, no live provider call.

### 7.1 Files (v4 delta)
| File | Change |
|---|---|
| `src/mnemoseed_local/daemon/secrets_routes.py` | NEW: `POST /api/v1/secrets/set`, manual body parse + fixed detail, name allowlist, full-value four-sink absence, `LazySecretStore` |
| `src/mnemoseed_local/daemon/app.py` | 2nd `include_router(secrets_router)`; `create_app(secret_store=)` seam; `app.state.secret_store`; `_build_capture(..., secret_store)` → `RoleRouter(secrets=secret_store)` |
| `src/mnemoseed_local/console/static/app.js` | `wizardBind()`: secret write before ref bind, input cleared only after secret write succeeds |
| `tests/test_secrets_routes.py` | NEW: 27 tests (write contract, four sinks, allowlist, credential gate, read wiring, wizard order, I-R4-1 isolation) |
| `tests/test_dream_status_payload.py` | frozen HTTP set +1 (`/api/v1/secrets/set`) |
| `docs/zh/design/04-trust-boundary-and-config.md` | §1.4 +1 endpoint row |
| `docs/zh/design/wave01-parallel-conflict-table-v4.md` | NEW: v4 conflict table (N-R4-2) |

Frozen surfaces byte-unchanged: `daemon/auth.py` (no OPEN_PATHS change/exemption), `secrets/{store,refs,__init__}.py`, `config.py`, `configwrite/service.py`, `llm/routing.py`. No `app.mount()` added; no `#/wizard`.

### 7.2 RED → GREEN pairs (verbatim failures)
1. **New module absent (natural RED).** `uv run pytest -q tests/test_secrets_routes.py` → `ModuleNotFoundError: No module named 'mnemoseed_local.daemon.secrets_routes'`, `1 error during collection`. Implemented module → 26 passed (27 with wizard-order test).
2. **Frozen 39-set absent (natural RED).** `-k test_external_surface_inventory_equals_frozen_baseline` → `Extra items in the right set: '/api/v1/secrets/set'`, `1 failed`. Added include_router → 1 passed.
3. **I-R4-1 eager keyring probe (scratch mutation).** Temporarily made `create_app` call `default_secret_store(CONFIG_DIR)` with `_probe_keyring_store` forced to fail → `AssertionError: OS keyring probed by bare create_app()`, EXIT 1; `app.py` restored to `13CAA075…` byte-exact. Implemented `LazySecretStore` → `test_bare_create_app_does_not_probe_keyring` / `test_lazy_default_defers_probe_until_first_use` / `test_injected_store_skips_default_resolution` pass.
4. **M1 masked_tail** (`masked_tail` added to success dict): `assert {'backend','name','ok','masked_tail'} == {'backend','name','ok'}` + `KEY[-4:] in body`, `2 failed`, EXIT 1 → restored `0A96CD6E…` byte-exact.
5. **M2 `str(exc)`** (error detail concatenated exception text): `secret write failed: backend refused sk-live-secret-9f3a2b for mnemoseed/dream/dream`, `1 failed`, EXIT 1 → restored byte-exact.
6. **M3 422 echo** (raw submitted value in 422 detail): `tests/test_secrets_routes.py:186 AssertionError`, `1 failed`, EXIT 1 → restored byte-exact.
7. **M4 allowlist guard removed**: `9 failed, 1 passed`, EXIT 1 (daemon/credential + aliases accepted) → restored byte-exact.
8. **M5 wizard order** (ref bound before secret write / secret write after bind): `tests/test_secrets_routes.py:340 AssertionError`, `1 failed`, EXIT 1 both variants → restored `921CB416…` byte-exact.
9. **M6 read wiring `secrets=None`**: `assert '' == 'sk-live-secret-9f3a2b'`, `1 failed`, EXIT 1 → restored `F9FFB2D1…` byte-exact.

All mutants applied only to a TEMP shadow copy (`PYTHONPATH`); repo source never mutated; byte-exact restoration verified each time.

### 7.3 I-R4-1 isolation policy
Secret-store resolution is lazy and injectable. `create_app(secret_store=...)` accepts the shared instance; omitted, `app.state.secret_store` is a `LazySecretStore` that resolves `default_secret_store(config.CONFIG_DIR)` only on the first get/set/exists call. Bare `create_app()` callers (route-inventory, boot, auth fixtures) never construct a store and never probe the OS keyring; `@asynccontextmanager lifespan` reuses the injected/lazy store and passes it into `_build_capture`. Tests inject in-memory stores or set `MNEMOSEED_SECRET_BACKEND=file`; no real keyring, no installed runtime.

### 7.4 Findings disposition
- I-R4-1 CLOSED (lazy/injectable store; forced-failure probe shows bare callers boot).
- N-R4-1 CLOSED: `app.state.secret_store` explicit; `_build_capture` takes `secret_store` param (no `app` in scope at old line 867); param passed from lifespan `app.state.secret_store`.
- N-R4-2 CLOSED: `docs/zh/design/wave01-parallel-conflict-table-v4.md` pinned.
- B-1 carried forward unchanged from `f6e7c481`.

### 7.5 Frozen HTTP set (exact 39, verified from `create_app()`)
`{/healthz, /health, /api/v1/observability, /api/v1/audit, /api/v1/host/capture-evidence, /api/v1/profiles, /api/v1/profiles/archive, /api/v1/config, /api/v1/config/set, /api/v1/config/set-many, /api/v1/config/versions, /api/v1/config/rollback, /daemon/shutdown, /ingest, /session/end, /session/active, /flush, /session/recall-pending, /session/recent, /session/windows, /memory/atlas, /memory/audit, /memory/error_events, /memory/export, /memory/forget_this, /memory/reinforce, /memory/timeline, /mcp, /memory/recall, /memory/remember, /memory/supersede, /memory/dream_once, /memory/dream_status, /memory/recall_feedback, /api/v1/llm/catalog, /api/v1/llm/probe, /api/v1/llm/models, /api/v1/llm/detect-local, /api/v1/secrets/set}`. Runtime enumeration count = 39. MCP tool-name set and page-route set `{overview,atlas,config,profiles,dream}` unchanged; no `#/wizard`.

### 7.6 V gate (legacy-scan, v4 re-run)
Exact command over app.js/index.html/style.css: `totalFindings=6`, all pre-existing justified exceptions. Raw JSON SHA-256 `2888D9C45F229BA97715D7CEF8BD3E63C886E10FD509814AB02BEEADF261E1E1`, byte-identical to committed baseline; no finding added/deleted, no rule relaxed. `mwgrcHash=ABSENT`.

### 7.7 Final gate outputs (verbatim)
```
uv run pytest -q tests/test_llm_routes.py tests/test_console_role_batch.py tests/test_dream_status_payload.py tests/test_configwrite_setmany.py tests/test_secrets_routes.py
  63 passed, 1 warning in 8.71s      EXIT 0
uv run pytest -q tests/test_console_role_batch.py -k preselection
  1 passed, 1 deselected in 0.76s    EXIT 0
legacy-scan --json (node v22): 6 findings, raw SHA 2888D9C4…E1E1, byte-identical to baseline, verdict PASS   EXIT 0
uv run ruff check                    All checks passed!                          EXIT 0
uv run ruff format --check            646 files already formatted                 EXIT 0
uv run mypy src                       Success: no issues found in 182 source files EXIT 0
```
(N-1/N-CARRIED-1 refresh: format count is 646 in this run, not the stale 640/641/642 noted earlier.)

### 7.8 Residual risks
- Independent implementation QA (distinct author, declared-model attestation, Web-UI binary checklist, v4 mutants) required before PR/merge; I-1 is DESIGN-ADDRESSED and implementation-green but QA-CLOSE pending.
- Full 5000+ suite not run in this batch (frozen command list only).
- Shared `mnemoseed/llm/<name>` slot and S7 OAuth remain deferred; wizard writes only the `dream` role slot; `dream_verifier` reuses the same ref without a second value write.
