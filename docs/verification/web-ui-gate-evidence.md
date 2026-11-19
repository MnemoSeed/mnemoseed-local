# Web-UI verification gate (V gate) — W1-B #381 S6

> Scope: changed console front-end surface for the provider-catalog wizard.
> Base: merged main `500147037ff97fd1a9f25f8e42e50b375ed4d9bd`.
> Declared model: `opencode/hy4-flash-free`; physical attestation UNKNOWN.

## Exact command

```powershell
$env:DISABLE_TELEMETRY='1'
npx --yes -p node@22 node "$HOME\.config\opencode\skills\modern-web-guidance\modern-web.mjs" legacy-scan --json src/mnemoseed_local/console/static/app.js src/mnemoseed_local/console/static/index.html src/mnemoseed_local/console/static/style.css
```

- Package: `node@22` (measured `v22.23.5` via `npx --yes -p node@22 node --version`).
- Skill tool: `C:\Users\Little Star\.config\opencode\skills\modern-web-guidance\modern-web.mjs`.
- Raw JSON: `docs/verification/w381-evidence/legacy-scan.raw.json` (verbatim stdout).
- `DISABLE_TELEMETRY=1` set as a PowerShell env var (bash inline `VAR=1 cmd` is not valid PowerShell).

## Guides consulted (modern-web-guidance, retrieved this session)

`search "searchable input with search type"` and `search "radio card selection"` returned:
- `forms` — search input uses `type="search"` + `enterkeyhint="search"`; AJAX results announced via `role="status"`.
- `card` — choice cards use `<label class="card">` with a radio as direct child; selection styled with `:has(> :checked)` using `outline`/border-color only (never `border-width`, `padding` or `font-weight`).
- `autofill-sign-in-form` — password input with `autocomplete` and a Show/Hide toggle (`aria-pressed`, `aria-label`), results via `role="status"`.

Guides saved to temp (`mwg-guide-61820-1795086539448.md`, 37.8 KB / 700 lines) and read in full. Baseline-dates from the round-3 D-gate remain `[needs verification]`; no invented dates. `.mwgrc` is default-off: `mwgrcHash=ABSENT`, no `.mwgrc` in repo or home.

## legacyFindings (raw, 6 total; none introduced by S6)

| # | guideId / ruleId | file:line | finding | In HEAD? | disposition |
|---|---|---|---|---|---|
| 1 | ime-safe-enter-submit | app.js:412 | keydown Enter without IME composition guard | yes (pre-existing) | EXCEPTION (pre-existing) |
| 2 | ime-safe-enter-submit | app.js:1831 | keydown Enter (`row.click()`) without IME guard | yes (pre-existing) | EXCEPTION (pre-existing) |
| 3 | js/copying-array-methods | app.js:668 | `[...versions].sort()` → `toSorted()` | yes (pre-existing) | EXCEPTION (pre-existing) |
| 4 | js/copying-array-methods | app.js:1306 | `[...atlasState.items].sort()` → `toSorted()` | yes (pre-existing) | EXCEPTION (pre-existing) |
| 5 | js/replace-children-innerhtml | app.js:374 | `body.innerHTML = ""` → `replaceChildren()` | yes (pre-existing) | EXCEPTION (pre-existing) |
| 6 | js/replace-children-innerhtml | app.js:2167 | `editor.innerHTML = ""` → `replaceChildren()` | yes (pre-existing) | EXCEPTION (pre-existing) |

## exceptions

Every finding above is EXCEPTION, justified as follows (not a loosened rubric, not a deleted finding):

1. **Pre-existing / outside S6 write surface.** Each finding string appears verbatim in `git show HEAD:.../app.js` at base `5001470`; `git diff --unified=0` confirms none of the six lines is an addition. The S6 wizard adds zero legacy-pattern lines: its new markup uses `type="search"` + `enterkeyhint="search"`, `<label class="card">` radio cards styled with `:has(> :checked)` (outline/border-color only), password Show/Hide with `aria-pressed`/`aria-label` and `role="status"`.
2. **Minimal-diff discipline.** Fixing six legacy lines in untouched config/atlas/dialog code would mass-edit surfaces unrelated to the frozen S6 scope and violates the repository's "do not mass-edit untouched legacy code" rule.
3. **Regression safety.** These exact strings are inside legacy code exercised by other suites; rewriting them this batch would expand the blast radius for no S6 benefit.

No exception was created by weakening a `keepWhen`/`dropWhen` rule; raw JSON retained unmodified.

## verdict

V gate **PASS with recorded exceptions**. Zero findings were introduced by the changed surface; all six are pre-existing and carry a justified exception above. `mwgrcHash=ABSENT` (default-off). No undisposed finding.
