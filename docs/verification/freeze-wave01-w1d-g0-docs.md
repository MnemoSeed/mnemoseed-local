# Freeze · W1-D · #441 + G0 docs alignment

> Status: **FROZEN** (2026-11-19). Main-authored scope/AC/commands lock. DESIGN QA CLOSABLE.
> Owner authority: main chat local reference `localref-opencode-20261119-wave01`, verbatim owner turn 「1）A 2）可以 3）授权」.
> Baseline: `main@8ad2d6ace54c1fca467796cc6ddfcbf596318168`. PR #432 does not touch this file surface; re-verify baselines on post-merge HEAD.

## Pinned artifacts
| Kind | Path | SHA-256 |
|---|---|---|
| Brief | `docs/zh/prd/PRD-441-G0-docs-brief-v2.md` | `919a5286efc260ee7553cada87df235ae65d751f630afdf2a69369440eaa5b00` |
| Design | `docs/zh/design/441-G0-docs-v2.md` | `b1c7ed2873f9f4820629affbbef98707c02365f4f15de4d30abc0563cc30e31e` |
| Slices | `docs/zh/design/441-G0-docs-slices-v2.md` | `7f7b704a43d86ba40776b349430bb4e16cd6427d267e6ed040c31257d0e15a9f` |
| Design QA round 1 | `docs/verification/wave01-design-qa-D2-verdict.md` | `29af20067469698037204017d459602dd65dd072d19c22a2522a813b62111276` |
| Design QA round 2 | `docs/verification/wave01-design-qa-D2-round2-verdict.md` | `d3f1d80bd5175a70ebcca26aa3c17cc0bf06482f94be169f295b577f1c8fb69e` |

Design QA round 2: **CLOSABLE, B/I/N 0/0/1** (declared model `opencode/hy4-flash-free`, physical attestation UNKNOWN). Round-1 BLOCKER B-D1 independently repaired and verified: raw CI failure (`gh run view 44097403996 --log-failed`) is a single U+FFFD; new `page.evaluate` DOM-side integer parse + bounded retry, red phase A includes the raw shape, normal empty state parses `[0,0,0]`.

## Scope (frozen AC1–AC9)
- AC1 pure `parse_counts(text) -> list[int] | None`; deterministic on DOM text; returns None for unparseable text including a lone U+FFFD; no non-ASCII→space normalization as a repair mechanism.
- AC2 `:535-537` uses `read_counts(page)`: empty iff items==0 and rescue==0 and fading==0; None or any mismatch → AssertionError containing raw value and parsed values; all three count tokens still asserted as integers.
- AC3 `:575` uses the same `read_counts` for filled state: first integer > 0; no `"0 items" in filled_count` substring check; `:587` report records parsed values and raw.
- AC4 no change to `app.js:1255` / `fmtNum` / `Intl.NumberFormat` / `index.html` charset; no product src change.
- AC5 README import section lists four readers and `--dry-run`.
- AC6 design 14 §2 zcode backfill row updated; §6 `setup` marked as design goal, current verb `init --mode capture-recall`.
- AC7 PRD-309 refresh: version line hash → `8ad2d6ac` (as of 2026-11-19); open-issue count re-measured at execution time via `gh issue list` (architect measured 23; never hard-code 21); #385 OPEN / #432 OPEN under QA; #440/#390/#441 registered as queue; G0-2 notes design 14 is picked up by this batch.
- AC8 post-S6 doc reconciliation: explicitly register G0-4 in PRD-309 — README:47-53 still claims provider catalog works while PRD-309:59 marks it CONTRADICTED; S5/S6 are sequenced after this batch, so do not change the claim now, but record "after W1-B/S6 merges, reconcile README:47-53 and PRD-309:59 and close G0-4" — never leave it silent.
- AC9 all changes limited to docs + `scripts/atlas_verify.py`; `git diff -- src/` empty.

## Denylist (frozen)
Never loosen the oracle to a single `"0 items"` token or delete an assertion; no changes to app.js/index.html/fmtNum/console runtime text; do not mark in-flight items (#432/#385/#440/#390/#441) as closed; no retag / pyproject change; no port-policy change, no 7788/4096 or installed runtime; no folding into another PR; no `normalize_display` collapse-to-space as the repair mechanism.

## Owner-authorized AC9 amendment (2026-11-19 — append-only)
Owner (question tool, direct answer 2026-11-19) chose to add a committed regression test rather than accept the residual. AC9 is amended from "all changes limited to docs + `scripts/atlas_verify.py`" to "docs + `scripts/atlas_verify.py` + a new committed `tests/test_atlas_count_parse.py`". The test must pin `parse_counts` / `read_counts` and the U+2014 shape so that the four QA mutants M1–M4 are KILLED by the repository suite (not only by scratch oracles). All other AC and the denylist are unchanged; `git diff -- src/` must remain empty.

## Post-freeze byte-level correction (2026-11-19, append-only; do not erase the above)
Main and the independent implementation QA independently re-pulled `gh run view 44097403996 --log-failed` at the byte level. The raw failure line is `AssertionError: Atlas empty state missing: '\xe2\x80\x94'` — **U+2014 EM DASH, zero U+FFFD**. The earlier design-QA rounds' "lone U+FFFD" root cause was a decode misreading. The true mechanism is a race: `index.html:207` `#atlas-count` placeholder is an em dash and `wait_for_count` (`:467-475`) lets it through because `/Loading/` does not match. The frozen mechanism still functionally covers the real byte: the engineer's byte-exact scratch probe (`w441-u2014-red-probe/probe.py`, sha `a52c269e…9a07`) confirms OLD assertion RED on U+2014, NEW `read_counts` bounded retry reaches digits → GREEN, and a DOM stuck at U+2014 exhausts to `None` → RED with `raw=… parsed=None` diagnostic (never silently green). No code change and no new commit resulted. This correction supersedes the U+FFFD wording in the root-cause paragraph above for evidentiary purposes; the AC and denylist are unchanged.

## Carryforward NIT (round 2, record at freeze)
- **N-D2**: root-cause prose overclaims deterministic transport-boundary loss (`parse_counts('\ufffd')=None`; green depends on DOM-intact read or retry, both covered by red phases A + 4). Keep the honest scope statement; reopen if a raw byte trace shows persistent DOM corruption surviving retry.

## Exact verification commands
```
uv run pytest -q tests/test_atlas_preflight.py    # if absent, skip and record
uv run python scripts/atlas_verify.py             # isolated run dir + free port, PASS
ruff check scripts/atlas_verify.py; ruff format --check scripts/atlas_verify.py
Select-String README.md -Pattern 'import --host codex_cli|import --host claude_code|import --host opencode|import --host zcode|--dry-run'
Select-String docs/zh/design/14-product-architecture.md -Pattern 'zcode|setup|init --mode capture-recall'
Select-String docs/zh/prd/PRD-309-public-alpha.md -Pattern '8ad2d6ac|2026-11-19|G0-4|#432'
git diff -- src/    # must be empty
```
Isolation: spare port and isolated run dir; never the installed runtime or 7788.

## Closeout predicate
TDD RED→GREEN including the raw single-U+FFFD red phase; all verification commands green; independent implementation QA (distinct author, declared-model attestation) CLOSABLE with no BLOCKER; main re-runs checks; issue → branch → PR (`Closes #441`) → CI green → merge; writer claim released after worktree removal.
