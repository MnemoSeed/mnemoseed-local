---
description: Show the work frontier (queue/doing issues, open PRs, fresh PRD activity) and propose one pick
---

Show the current work frontier:

1. Run `gh issue list --label queue --state open --limit 50` and `gh issue list --label doing --state open --limit 50`.
2. Run `gh pr list --state open`.
3. Check for in-flight PRD work under docs/zh/prd/: `git log --since="3 days ago" --name-only --pretty=format: -- docs/zh/prd/` plus `git status --porcelain -- docs/zh/prd/` (uncommitted PRD edits are in-flight signal too).
4. Present a compact frontier summary: queue items, in-flight items, open PRs.
5. Propose exactly ONE pick with a one-line rationale.
6. Wait for explicit user confirmation before starting anything. Never start work unprompted.
