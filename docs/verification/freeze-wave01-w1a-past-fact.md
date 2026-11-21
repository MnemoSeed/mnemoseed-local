# Freeze · W1-A · #440 past-fact marker

> Status: **FROZEN** (2026-11-19). Main-authored scope/AC/commands lock. DESIGN QA CLOSABLE.
> Owner authority: main chat local reference `localref-opencode-20261119-wave01`, verbatim owner turn 「1）A 2）可以 3）授权」; follow-up owner ruling (question tool, 2026-11-19): accept literal per-item marker cross-layer write set (fence/recall/t2/plugin.ts + golden re-pins); reject the payload-only fallback.
> Baseline: `main@8ad2d6ace54c1fca467796cc6ddfcbf596318168`. PR #432 does not touch this file surface; re-verify baselines on the post-merge HEAD before the first edit.

## Pinned artifacts
| Kind | Path | SHA-256 |
|---|---|---|
| Brief | `docs/zh/prd/PRD-440-past-fact-marker-brief-v2.md` | `55259891d68f6ff5514a14795298d286a212e573c6eeead8368a73d120d5943e` |
| Design | `docs/zh/design/440-past-fact-marker-v2.md` | `91e7013bfefb707451627eba27dac6c82f478a3db489033ccae7ff6aac4b5f13` |
| Slices | `docs/zh/design/440-past-fact-marker-slices-v2.md` | `1e914d8eb976d4b062ca7bd9ed3a46c5738cd8962ff297f4e5069aceec01c05f` |
| Design QA round 1 | `docs/verification/wave01-design-qa-D1-verdict.md` | `29666da04483ceb4505d24357723da83b70cb278fb8567a82c7b0bfadfc83df1` |
| Design QA round 2 | `docs/verification/wave01-design-qa-D1-round2-verdict.md` | `9e0760ccc184cfc29ffa75cdf0ca689c4ec6dc51c8ac9081b56ef55ab49a76d7` |

Design QA round 2: **CLOSABLE, B/I/N 0/6/1** (declared model `opencode/hy4-flash-free`, physical attestation UNKNOWN). Round-1 BLOCKER B-1 independently re-measured and closed (four surfaces predicted 1746 / 1795 / 1789 / 1771, all ≤ 1800 with `MAX_CHARS=1800` unchanged; `tests/test_mcp_instructions.py` 20 passed).

## Scope (frozen AC1–AC9)
- AC1 recall entry fields: `memory_marker=="past_observation"`, non-empty provenance equal to real source (or `provenance-unknown` + debug log), ISO-8601 `memory_observed_at` or explicit `time-unknown` (never bare null). Derived from `_entry_payload()`; no ranking change.
- AC2 `/session/recent` chunks/groups; AC3 `/session/recall-pending` items[]; AC4 captured entries re-recalled; same marker chain.
- AC5 exactly one BODY rule containing the three semantics (past observation / conflict→stale re-verify / stays memory even if re-verification seems unnecessary); `render()` and all four surface texts ≤ 1800; generic `CONTRACT_PREAMBLE` byte-unchanged; only `SURFACE_PREAMBLES` shortened.
- AC6 `fence.py` and `plugin.ts` marker constants byte-identical; injection-block goldens fully re-pinned; hashes and diffs of instructions/fence/recall/t2/plugin.ts mechanically checked.
- AC7 stale memory + conflicting live observation → marked item plus the re-verify rule in delivered instructions; no claim of a conflict detector (static rule, causal arrow not verified).
- AC8 ranking unchanged: after stripping new fields, entry order/scores equal to baseline.
- AC9 malformed entries missing source/time still get degraded marker (`provenance-unknown`, `time-unknown`) plus debug trace; oracle covers it.

## Denylist (frozen)
No change to `MAX_CHARS` / `TOOL_PLACEHOLDERS` / `SURFACES`; no ranking/dream/decay mechanism change; no fabricated provenance or observation time; no port 7788 / installed runtime; no folding into another PR; no AC lowering.

## Carryforward implementation gates (from round 2 — mandatory, not new design)
- **A-I1**: AC5 is wrong to claim preambles are constrained only by `test_mcp_instructions.py:183`; `:289-299` also requires surface phrases. The implementation must verify both constraints before shortening any preamble.
- **A-I2**: the per-item marker bytes were not included in the B-1 budget accounting; `plugin.ts:1127` can silently drop the whole injection. The implementation must add the literal marker bytes to the measured surface lengths and re-run the ≤1800 check on the final rendered texts.

## Exact verification commands
```
uv run pytest -q tests/test_past_fact_marker.py tests/test_mcp_instructions.py tests/test_mcp_http_transport.py tests/test_host_adapters_e2e.py tests/test_hook_ts_behavior.py tests/test_hosts_claude_code.py tests/test_b2_standing_rule_hook.py
uv run pytest -q
ruff check; ruff format --check; mypy src
git diff -- src/mnemoseed_local/hosts/common/fence.py src/mnemoseed_local/hosts/opencode/plugin.ts
```
Isolation: temporary `MNEMOSEED_LOCAL_HOME`, spare port (never 7788), installed runtime untouched.

## Closeout predicate
TDD RED→GREEN with byte-exact restoration evidence for each oracle; four gates green on isolated worktree; independent implementation QA (distinct author, declared-model attestation) CLOSABLE with no BLOCKER; main re-runs gates; issue → branch → PR (`Closes #440`) → CI green → merge; writer claim released after worktree removal.
