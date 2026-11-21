# Freeze · W1-C · #390 legacy driver-only dream route

> Status: **FROZEN** (2026-11-19). Main-authored scope/AC/commands lock. DESIGN QA CLOSABLE (round 3).
> Owner authority: main chat local reference `localref-opencode-20261119-wave01`, verbatim 「1）A 2）可以 3）授权」; follow-up owner ruling (question tool, 2026-11-19): accept the uniform migration line as the clarification of Done-when #3, on the condition that it is actually executable on a non-booting home.
> Baseline: merged main `5001470` (PR #432 merge). G0 eligibility SATISFIED on that HEAD.

## Pinned artifacts
| Kind | Path | SHA-256 |
|---|---|---|
| Brief | `docs/zh/prd/PRD-390-legacy-dream-route-brief-v3.md` | `d9d211f0e4ee909b4271777dfba7a57edab096ec4c799d1660385b8211518ac1` |
| Design | `docs/zh/design/390-legacy-dream-route-v3.md` | `272201dfd2ce4e61c90e58c5116bf763e7c9c20afc5b01aba989e130fdd8e2c3` |
| Slices | `docs/zh/design/390-legacy-dream-route-slices-v3.md` | `9312c94fd884ddf595ff3fdfcf37aa0fa5fe5dd396963b36e4370d17786522aa` |
| Carryforward note | `docs/zh/design/wave01-v3-carryforward-gates.md` | `bc07b3766da5d25c6db9d03721fe18309571381d6c0e3bcbbe346790e393f81b` |
| Design QA round 2 | `docs/verification/wave01-design-qa-D1-round2-verdict.md` | `9e0760ccc184cfc29ffa75cdf0ca689c4ec6dc51c8ac9081b56ef55ab49a76d7` |
| Design QA round 3 | `docs/verification/wave01-design-qa-D1-round3-verdict.md` | `f0eca4576651f4c02f4004dcf582dc045391e0f8f0e997d618739929187c032b` |

Round-3 verdict: **CLOSABLE, B/I/N 0/1/1** (declared model `opencode/hy4-flash-free`, physical attestation UNKNOWN). The round-2 BLOCKER B-1 was repaired by moving execution off HTTP/daemon: new CLI verb `config repair-dream-route` → in-process `ConfigWriteService.set_many` over a sanitized temp config with `cfg.source` repointed at the real `config.toml`. QA executed the real merged-main code against driver-only / model-only / both-missing homes: all three repaired, real config.toml and real SqliteMetaDriver both updated (`applied count: 2`, reboot OK), order-independent, no default value, no silent mutation.

## G0 eligibility (merely SATISFIED on merged main — re-check at first edit)
Re-measured `configwrite/service.py` = `1832d8bcc40d7f749a325f892bdc83b4846004fc0fa218a6ca3cca3dea0e1110` (65,051 B). Four assumed properties verified on merged main: deepcopy (`:1230`), apply-to-simulated (`:1240`), `cross_validate` + `_assert_reload_valid` before `_land` (`:1242-1251`), arbitrary role-pair order (`:1219-1223`). If any differs at implementation time, STOP and report; do not hard-implement around a drift.

## Scope (frozen AC1–AC8)
- AC1 detect the legacy shape uniformly by the error key prefix + message; no fabricated shape bit; no TOML introspection workaround that bypasses the load contract.
- AC2 fix driver-only, model-only, and both-missing homes with one uniform, shell-safe migration line.
- AC3 no configuration is written during detection; no silent mutation.
- AC4 new explicit CLI verb `config repair-dream-route --driver <DRIVER> --model <MODEL>` (no default model constant).
- AC5 isolated tests: temp HOME + spare port (never 7788); for each shape, simulate daemon down / no rebuilt daemon to prove daemon-independence; assert config.toml + meta DB updated and boot succeeds; assert up/on/doctor show the repaired route.
- AC6 no change to `config.py` load contract; no default-value injection; no HTTP `config set`; no direct settings-DB write.
- AC7 the load-error hint goes through one shared helper `_legacy_route_migration_hint` at every load site; no silently swallowed `ConfigError`.
- AC8 post-write self-check with `load_config(real)`; failure is a non-zero exit with a clear message (never silent green); no default-model constant.

## Denylist (frozen)
No silent config mutation; no auto-repair on load; no default-model constant; no direct settings-DB write; no HTTP `config set`; no relaxation of `config.py` load rejection; no port 7788 / installed runtime; no folding into another PR; no AC lowering; do not edit `configwrite/service.py` (it is a G0 dependency only).

## Carryforward implementation gates (round 3 — mandatory)
- **I-C1 (IMPORTANT)**: multi-incomplete-role deadlock. Sanitizing only the target table means a second incomplete role aborts the command; the design's "fix roles one-by-one" claim was falsified by a surviving probe. The implementation must either (a) handle multiple incomplete roles in one repair, or (b) detect the second incomplete role and emit an explicit, actionable message that the command must be repeated — never silently claim one-by-one success. Add a red phase for the two-incomplete-roles fixture.
- **N-C1 (NIT)**: the "exactly two key lines change" oracle is ambiguous across fixtures (driver_only/model_only add only one new line). Assert the final two-key route content, not a raw added-line count.

## #444 residual (record, do not hide)
PR #432's `set_many._land` is not all-or-nothing under an injected mid-batch I/O fault (follow-up issue #444). For this slice the post-write `load_config(real)` self-check surfaces the failure with a non-zero exit; the zero-window property is explicitly NOT claimed. Add a red phase forcing a mid-batch second-write failure and verify the non-zero exit + clear message.

## Exact verification commands
```
uv run pytest -q tests/test_legacy_dream_route.py tests/test_config.py tests/test_cli.py tests/test_configwrite_service.py
ruff check; ruff format --check; mypy src
git diff -- src/mnemoseed_local/config.py src/mnemoseed_local/configwrite/service.py   # must be empty
grep -n 'load_config()' src/mnemoseed_local/cli.py
grep -n 'rest_client\|config/set' src/mnemoseed_local/cli.py
```
Isolation: temp `MNEMOSEED_LOCAL_HOME`, spare port, no installed runtime.

## Closeout predicate
TDD RED→GREEN (including the three shapes, I-C1 two-role fixture, and the mid-batch failure phase); all gates green; independent implementation QA (distinct author, declared-model attestation) CLOSABLE with no BLOCKER; main re-runs gates; issue → branch → PR (`Closes #390`) → CI green → merge; writer claim released after worktree removal.
