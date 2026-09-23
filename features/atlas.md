# Memory Atlas verification

The repository-owned `scripts/atlas_verify.py` is the executable proof for the
first Atlas feature. It performs Launch, Doctor, Drive, Evidence, and Cleanup:
it starts one owned daemon on a checked free port, verifies health, drives the
real browser and API, writes a retained evidence report, and stops only its own
child process tree. It refuses to reuse an existing run directory and refuses
reserved ports 7788/4096, so it never double-drives someone else's instance.

Setup (pinned browser, worktree-owned path only):

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = ".verification-runs/playwright-browsers"
uv run playwright install chromium  # Linux CI: install --with-deps chromium
```

Run preflight first, then the proof:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = ".verification-runs/playwright-browsers"
uv run python scripts/atlas_preflight.py
uv run python scripts/atlas_verify.py
```

The harness defaults to the same worktree-owned browser path when the variable
is unset, and fails when the browser is missing instead of skipping. It uses a
`MNEMOSEED_LOCAL_HOME` directory under `.verification-runs/`, never port 7788,
and never the real user home. Retained `evidence.txt` under the run directory
is the PASS oracle; browser screenshots or traces are optional and are not the
oracle. The proof uses the synthetic embedder and stub dream configuration, so
it does not measure live model quality.
