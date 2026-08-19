# MnemoSeed Local — Agent Guide

## Memory: session-start discipline

This repository runs its own local memory daemon (`mnemoseed-local up`,
loopback on `http://localhost:7788`; the OpenCode capture hook feeds it every
turn). When the `mnemoseed` MCP server is registered in `opencode.json`:

1. **START of a new session**: call `recent_sessions(n_sessions=2)` FIRST to
   re-anchor on where the previous conversation ended — the tails are
   time-ordered verbatim chunks (newest session group first, ascending inside
   each group). The newest group is usually the current session itself; the
   second group is the previous conversation's tail.
2. **During the session**: use `recall(query)` for topical memory over merged
   knowledge + fresh unconsolidated chunks. Use `remember(text)` ONLY for
   facts the user explicitly asks to pin.
3. **Trust order**: memory primes context, the repository decides. Never let
   a remembered plan override the code, the tests, or the current PRDs.

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
- Public code and comments are English-only; Chinese docs live in `docs/zh`.
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

## Useful entry points

- `docs/zh/prd/PRD-B2-roadmap.md` — the Phase B master plan and batch log.
- `README.md` — product surface (install, MCP registration, verbs).
