---
name: verify-atlas
description: Run the repository's isolated synthetic Atlas verification and retain proof.
version: 1.0.0
user-invocable: true
argument-hint: "[preflight|verify]"
allowed-tools:
  - Bash(uv run python scripts/atlas_preflight.py)
  - Bash(uv run python scripts/atlas_verify.py)
---

# Verify Atlas

Use this skill for the first Memory Atlas feature. It runs the repository-owned
synthetic proof, not a live-memory or real-home check.

## Run

From the repository root, run the preflight before the verifier:

```bash
uv run python scripts/atlas_preflight.py
uv run python scripts/atlas_verify.py
```

The harness covers Launch, Doctor, Drive, Evidence, and Cleanup. It uses a
worktree-owned Chromium path, synthetic embedding and dream stubs, an
isolated `MNEMOSEED_LOCAL_HOME`, and a free port that is never 7788 or 4096.
A missing browser, failed preflight, failed browser/API oracle, or failed
cleanup is a failure. `evidence.txt` containing `PASS` is the oracle; no
PASS is inferred from logs or screenshots.

The run directory is retained. In CI, sanitized proof is uploaded even when
verification fails. Local output is synthetic and must not contain real user
paths, text, or secrets.
