# Re-Freeze · W1-B v4 · #381 secret write/read channel

> Status: **RE-FROZEN** (2026-11-20). Supersedes `freeze-wave01-w1b-b7-pr3.md` for the I-1 scope only. The earlier freeze and all design-QA/implementation-QA verdicts remain as history and are NOT erased.
> Owner authority chain: main chat `localref-opencode-20261119-wave01` verbatim 「1）A 2）可以 3）授权」; owner ruling (question tool 2026-11-19) after implementation QA I-1: **re-freeze and add a secret-value write channel rather than land with a broken binding**.
> Main adjudications bound into this freeze: **OWNER-1** include the read-side wiring (write-only cannot make the bound route resolve); **OWNER-2** allowlist only `mnemoseed/dream/<role>`, reject `daemon/credential`, defer shared `mnemoseed/llm/<name>`.

## Pinned artifacts
| Kind | Path | SHA-256 |
|---|---|---|
| Brief v4 | `docs/zh/prd/PRD-381-B7-PR3-brief-v4.md` | `a7e5e456f6e9ab8f4ee3c102310a2800ae2430de10b86be174a86aae73f42bb0` |
| Design v4 | `docs/zh/design/381-B7-PR3-http-wizard-v4.md` | `96ff68de7fb09adacda7d0d4ddc42326b8baede5084fdb0aacc65ebf3e3570b3` |
| Slices v4 | `docs/zh/design/381-B7-PR3-slices-v4.md` | `20fbc3b437b02f2b03162c3277ba3516f96ac562b8d9e4a594ac0e7b50eacc87` |
| Re-freeze delta | `docs/zh/design/w1b-v4-refreeze-delta.md` | `2389efb97eebc177f0d5bd40e745954950bc7c00e43c48fa761a9659d9890865` |
| Prior implementation QA (trigger) | `.mnemoseeding/worktrees/w381/docs/verification/w381-implementation-qa-verdict.md` | `6dd2409ee6116789a2702b8a30be83099bed21c76e9df4356a8f45e38127af28` |
| Design QA round 4 | `docs/verification/w381-design-qa-round4-verdict.md` | `3b0ae635a40a6af2f196709bd715ebd69704f298e040683636f8dd857bee4b80` |

Round-4 verdict: **CLOSABLE, B/I/N 0/1/2** (declared model `opencode/hy4-flash-free`, physical attestation UNKNOWN). Nine destruction experiments all KILLED (read-side-empty, masked_tail, `str(exc)`, 422 echo, allowlist bypass, 40th route, `#/wizard`, default-deny removal, order mutant). Web-UI D-gate remains from round 3 (PASS); V-gate is still post-engineering and NOT yet passed for v4.

## Frozen scope (delta on top of the already-reviewed S5+S6 + B-1 fix)
- **Write**: new route `POST /api/v1/secrets/set` in new `src/mnemoseed_local/daemon/secrets_routes.py` + a second `include_router` line in `daemon/app.py`. Not in `llm/routes.py`, `configwrite`, or `secrets/` package; no `app.mount()`. Default-deny credential middleware covers it automatically — no auth exemption, no `auth.py` change.
- **Write security**: four-sink oracle (response / audit / daemon.log / `str(exc)`) over FULL value bytes; mutants M1 `masked_tail`, M2 `str(exc)`, M3 Pydantic 422 echo, M4 removing the daemon/credential guard must all be KILLED. No value persisted to config.toml; no audit row; request logging stays method/path/status only.
- **Allowlist**: only `mnemoseed/dream/<role>` names accepted; `daemon/credential` and aliases rejected; shared `mnemoseed/llm/<name>` deferred. No edit to frozen `secrets/refs.py` / `config.py` / `configwrite/service.py` / `routing.py` schema surfaces.
- **Read wiring**: `create_app(secret_store=...)` injection seam + `app.state.secret_store` same source + `RoleRouter(secrets=...)` instead of the current `secrets=None` at `app.py:867-872`; read-side RED oracle proving `secrets=None → api_key=''` and injected store → key resolves. `dream_verifier` reuses the same ref without a second value write.
- **ref/name mapping**: `secrets:mnemoseed/dream/dream` → SecretStore name `mnemoseed/dream/dream` → file backend `mnemoseed.dream.dream.key` (or same keychain credential).
- **Frozen HTTP set**: 34 → 38 (S5) → **39** members, exact-set equality; full member list in `w1b-v4-refreeze-delta.md` §2. MCP tool-name set and page-route set unchanged; no `#/wizard`.

## MANDATORY implementation gate (round 4)
- **I-R4-1 (IMPORTANT)**: eager construction of a default SecretStore would trigger a real OS-keyring probe in the ~19 bare `create_app()` callers. The implementation must keep secret-store resolution LAZY / explicitly injectable so ordinary test and boot callers do not touch the OS keyring. Add a red phase with the keyring probe forced to fail and verify those callers still boot; record an isolation policy in the implementation evidence.
- **N-R4-1**: `app.state` scope shorthand at design line 867 must be made explicit in code/evidence.
- **N-R4-2**: pin a v4 conflict table in the engineering evidence.
- Carryforwards unchanged: B-1 fix (commit `f6e7c481`, already QA-closed) must NOT be reworked or folded away; I-B4 mandatory preselection gate, I-B5 semantic re-pinning, I-B6 node-missing RED, Web-UI D gate, and the V gate at verification time all remain in force.

## Denylist (frozen)
No AC lowering; no fifth LLM route beyond the one secrets route; no `app.mount()`; no auth exemption; no literal key in config/log/audit; no shared-slot schema expansion; no OS-keyring probe in bare `create_app()` callers; no folding B-1 away; no port 7788 / installed runtime; no push/PR/merge by the engineering seat.

## Exact verification gates
```
uv run pytest -q tests/test_llm_routes.py tests/test_console_role_batch.py tests/test_dream_status_payload.py tests/test_configwrite_setmany.py tests/test_secrets_routes.py     # new secrets-route tests
uv run pytest -q tests/test_console_role_batch.py -k preselection     # mandatory machine gate, node-missing RED
<modern-web-guidance legacy-scan --json over changed web surface>     # V gate, re-run for v4
ruff check; ruff format --check; mypy src
```
Isolation: temp `MNEMOSEED_LOCAL_HOME`, spare port, OS-keyring probe guarded (I-R4-1), installed runtime untouched.

## Closeout predicate
Implement v4 on top of commit `f6e7c481` in the existing w381 worktree; TDD RED→GREEN (including I-R4-1 and all four sink mutants); all gates + V gate green; independent implementation QA (distinct author, declared-model attestation, Web-UI binary checklist) CLOSABLE with no BLOCKER; main re-runs gates; issue → PR (`Closes #381`) → CI green → merge; claim released after worktree removal.
