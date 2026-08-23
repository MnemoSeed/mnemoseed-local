---
description: Land the current batch — gates, adversarial QA review, branch, explicit-path staging, PR
---

Run the disciplined landing sequence for the current batch:

1. Run `pwsh -File scripts/gate.ps1` from the repo root (the script resolves its own root). If any gate fails, STOP and report — do not proceed.
2. Dispatch the senior-qa-reviewer subagent for an adversarial review of the working-tree diff vs main. Require verdict CLOSABLE with no BLOCKER; otherwise fix and repeat from step 1.
3. If not already on a batch branch, create or switch to one BEFORE staging anything (never land on main).
4. Stage ONLY explicit paths belonging to this batch (never `git add -A`).
5. Make a single squash-style commit with a concise message.
6. Push (never force-push) and open a PR with base `main` and body `Closes #<N>`, where N is the doing issue belonging to THIS batch; if several doing issues are open, ask the user which one.
7. Relabel the issue: remove `doing` (`gh issue edit <number> --remove-label doing`).
8. Remind the orchestrator to append the closeout record to the relevant PRD under docs/zh/prd/.
9. After merge, clean up leftovers: `git worktree list` (remove stale task worktrees) and `git branch --merged main` (delete merged batch branches).
