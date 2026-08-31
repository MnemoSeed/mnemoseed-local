# MnemoSeed Local — Agent Guide

## Memory

This repository runs its own local memory daemon (`mnemoseed-local up`,
loopback on `http://localhost:7788`). Memory behavior is automatic:
session-start anchoring and mid-session recall injection are performed by the
capture hook — no manual invocation ritual is required or expected. Use
`remember(text)` ONLY for facts the user explicitly asks to pin. Trust order:
memory primes context, the repository decides. Never let a remembered plan
override the code, the tests, or the current PRDs.

Work-queue orientation is a workflow concern, not a memory concern: run
`/next` at the start of work sessions, and re-query the queue (`gh issue
list`) before answering any planning-surface question — never answer from a
listing captured earlier in the session.

## Development conventions

- Gates (all must stay clean): `uv run pytest -q`, `ruff check`,
  `ruff format --check`, `mypy src`.
- TDD: failing tests first; adversarial self-verification before closeout;
  and per user directive 2026-08-19, closeouts additionally require a senior
  adversarial QA review (no BLOCKER left) — once gates AND the QA verdict
  are green, committing is pre-authorized.
- Commit discipline (user directive 2026-08-19): land work via
  **issue → branch → PR → merge** (PR body: `Closes #<issue>`), never a
  direct commit to `main`.
- **Delegated execution (user directive 2026-08-19, designed in from the
  start)**: the main session only orchestrates (decompose, brief, accept,
  run gates). ALL execution goes to the named subagents (registered as
  global opencode agents under `~/.config/opencode/agents/`, model pinned
  there): development to **senior-software-engineer**, QA to
  **senior-qa-reviewer** (adversarial), design/trade-off discussion to
  **solution-architect** for comprehensive evaluation, general chores to the
  built-in **general** agent. Do NOT substitute the built-in general agent
  with a role prompt when a named agent exists.
- **Parallelized execution (user directive 2026-08-19)**: decompose batches
  into the smallest tasks that can run asynchronously in parallel; multiple
  senior-software-engineer sessions may run concurrently on DISJOINT
  file/test surfaces, while steps with true ordering dependencies are chained
  inside one task. Every parallel task plan is checked by solution-architect
  for conflicts (file-surface overlap, test-oracle collisions, ordering)
  BEFORE execution starts.
- Public code and comments are English-only; Chinese docs live in `docs/zh`; GitHub issues, PRs, and comments are English-only; conversation with the owner is in Chinese.
- **Code style (user directive 2026-08-20)**: DRY — extract shared logic,
  never copy-paste. Keep features modular and decoupled so problems are easy
  to isolate and features easy to move later. Comments stay minimal: names
  and structure should carry the meaning; a comment may state the code's
  purpose and background only, and must never reference issues, PRs, QA
  rounds, people, or incident history (history lives in PRDs and git, not
  in code). Applies to new and touched code; do not mass-edit untouched
  legacy comments.
- Phase work is PRD-driven: brief in `docs/zh/prd/` → batched TDD execution →
  single-commit closeout → closeout record in the PRD.
- **Theory-anchor discipline (user directive 2026-08-19, applies to the whole
  mnemoseed family)**: every feature design must document its borrowed theory
  in a 理论锚 section of its PRD/design doc — only empirically validated
  neuroscience/psychology regularities (source + validated regularity +
  design rule), strictly separated from implementation mechanisms; an
  explicit "not borrowed" list keeps pop-neuro myths out. Design docs are
  mandatory for the theories behind every feature; they are the cornerstone
  of the system's core design.
- Never commit secrets; `opencode.json` lives outside this repo (global user
  config) and must not leak into git.
- **Test/live isolation (user directive 2026-08-31)**: tests, evals, sweeps,
  debug runs and console/Playwright checks must use a spare port (never 7788)
  and an isolated data dir (temp `MNEMOSEED_HOME` / profile). The installed
  runtime at `~/.mnemoseed-local` is never touched by development or testing.

## Work queue & capture discipline

- All work starts as a `queue`-labeled GitHub issue; capture-over-execute: any
  emergent todo gets `/capture`d immediately, then resume prior work.
- Lifecycle: `queue` (intent) → `doing` (in flight) → closed by the landing PR
  (`Closes #N`). Never create a second issue for the same work.
- Session orientation: run `/next` at start of work sessions to see the frontier.
- Issues follow the lean template in `/capture`: short plain-English bullets, no jargon.
- Landing: `/closeout` (gate script → adversarial QA → explicit-path staging →
  branch → PR).
- Local gates: run `pwsh -File scripts/gate.ps1` from the repo root (the
  script resolves its own root; wraps pytest/ruff/format/mypy; CI backstops
  on PR).
- **Parallel-batch & cleanup rule (user directive 2026-08-23)**: large batches,
  long-running eval jobs, or any pair of tasks that both touch tests must use
  isolated git worktrees (`git worktree add ..<name> -b batch/<name>`) with
  gates run per-tree; same-tree parallelism is allowed only for small batches
  with verified disjoint file surfaces. After merge, clean up leftovers via
  `git worktree list` (remove stale task worktrees) and `git branch --merged main`
  (delete merged batch branches).

## Org pointer

The umbrella multi-agent org protocol lives in the private HQ: `../org/PROTOCOL.md`
(relative to this repo's parent layout). On session start read `../org/_state.md`
for in-flight batches and blockers; refresh it when a batch lands. Plans and their
estimates live in `../org/plans/`.

## Useful entry points

- `docs/zh/prd/PRD-B2-roadmap.md` — the Phase B master plan and batch log.
- docs/zh/design/00…08 + REFERENCES.md — 架构设计文档系列（理论锚注册表在 REFERENCES.md）。
- `README.md` — product surface (install, MCP registration, verbs).
