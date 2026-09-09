# P-008 M5prep Report — seq-2 one-shot blind run (values-only outcome record)

Status: the unique seq-2 one-shot blind run was consumed on 2026-09-08 and
recorded a `scored` outcome with exit code 0. The A2 one-shot state table is
satisfied: one reservation marker, exactly one `scored` line in the one-shot
log, zero `void` lines, and the report OUT object written. Every value below
is a verification hash, a count, or a synthetic score on the sealed synthetic
corpus — none of these is proposed as a threshold, bar, attainment, readiness,
or any stronger claim.

## 1. Unique invocation

- Command (run exactly once, cwd = the G2 worktree): `uv run python -m
  mnemoseed_local.eval.m5prep_run_blind tests\fixtures\p008_m5prep\
  sequestered_inputs.json tests\fixtures\p008_m5prep\sequestered_truth.json
  G:\Development\MnemoSeed\org\workspace\dream-experience-refinement\evidence\
  p008-m5prep-seal-registry.jsonl G:\Development\MnemoSeed\org\workspace\
  dream-experience-refinement\evidence\p008-m5prep-oneshot-log.jsonl
  .phase2-a2-scratch\p008-m5prep-real-run-seq2.json --seal-seq 2`
- Environment: `M5PREP_G_SESSION=ses_f82644f77ffeDK1N87KGW92UE2`,
  `M5PREP_G_WORKTREE=<resolved G2 cwd>`,
  `MNEMOSEED_LOCAL_HOME=<resolved .phase2-a2-home>`.
- stdout: `one-shot scored: seal_seq=2 report=.phase2-a2-scratch\
  p008-m5prep-real-run-seq2.json`. Exit code 0. No stderr.

## 2. Sealed corpus reference (verified after registration)

- Seal registry file SHA256: `983b12798766e708781377a4994bbae57bdc32cef9b25645139bebd55bcb8b79`.
- `parse_registry`: 3 physical rows, 2 effective entries, 0 errors; seq 1
  `void`, seq 2 `active`.
- Inputs SHA256: `8439456aea4e0ddd53e2edfe82200c3d974dcf068745eab79574fe34e4ee75e8`.
- Truth SHA256: `cc8af940113d2b4d385a979817e82dc25aea0ea425a09959ff7665aef9ce8bbb`.
- Public dev split SHA256: `d3f5faff84ad89607d9261f0f67317e57959a729799068ab6ae30f3cf429aba4`.

## 3. One-shot artifacts (post-run, per the runner contract)

- Reservation marker filename (adjacent to the one-shot log):
  `p008-m5prep-oneshot-log.jsonl.reserve-2-cc8af940113d2b4d385a979817e82dc25aea0ea425a09959ff7665aef9ce8bbb.json`
  with SHA256 `7243e00cd0b86782ffcac7bfb6d2057c1d4f8ac8da0a4706dc66e0b8e3b843fa`.
- One-shot log SHA256: `f8260c96e96e5a60af5ebdacbf468b2e720936e763d96090cfdbd3d31944243f`.
  Exactly one `scored` line and zero `void` lines; `g_session`
  `ses_f82644f77ffeDK1N87KGW92UE2`; `run_utc` `2026-09-08T00:48:11Z`; the four
  guarded `ops_counters` are all 0. The log's `canonical_sha256` is `null`,
  which is schema-legal for the one-shot log.
- OUT object path: `.phase2-a2-scratch\p008-m5prep-real-run-seq2.json`, raw
  SHA256 `5a67378ce05127f1ddfb243f06311d57029826b0606cd7deb42bf95852a31f88`,
  git object `1eea657980000eca08e55f7c6c56cf46de350a26`, stored
  `canonical_sha256` `9f4ebc732a013d7ef5fe744c3d6100352b360c9572b876c480078fe8c6975f63`.
- Isolation (from the OUT object): phase2 HOME writes empty, forbidden modules
  empty, and the phase2 HOME directory stayed empty throughout.

### Canonical hash clarification

The stored `canonical_sha256` value is `canonical_sha256` computed over a copy
of the loaded OUT object with that object's own `canonical_sha256` field
removed. Recomputing `canonical_sha256` over the OUT object while retaining its
own `canonical_sha256` field yields `f74944f1b66a87f6b83330a9a46d317514dbdfece7750f1560c7113fb8dca50b`
by construction (the self-referential value changes the hashed bytes) and is
not tamper evidence.

## 4. Synthetic score counts (frozen scorer over the sealed synthetic corpus)

- score `fact`: P 1.0 / R 1.0 / tp 6 / fp 0 / fn 0.
- score `experience`: P 0.8461538461538461 / R 1.0 / tp 11 / fp 2 / fn 0.
- score `lesson`: P 0.6666666666666666 / R 1.0 / tp 4 / fp 2 / fn 0.
- score `intention`: P 1.0 / R 1.0 / tp 4 / fp 0 / fn 0.
- score `skill_sequence`: P 1.0 / R 1.0 / tp 3 / fp 0 / fn 0.
- `coercion_loss_count` 0; `disposition` errors 6 confined to `mq-c08` and
  `mq-c09`; extra predictions 2; other error channels 0; `misclass` 0.

## 5. SDT-shape counts (no thresholds)

- `fact`: hit 6 / miss 0 / false_alarm 0.
- `experience`: hit 11 / miss 0 / false_alarm 2.
- `correct_rejection` 8; `misclass` empty.
- `zero_reason_histogram`: `malformed-input` 5 / `no-match` 3.
- `confidence_deciles` bin sizes `[0, 0, 0, 0, 1, 2, 5, 5, 5, 1]` with
  accuracies `[null, null, null, null, 1.0, 1.0, 0.8, 0.8, 1.0, 1.0]`.
- `unscored_units` 0.

All the above are synthetic count/output facts produced by the deterministic
frozen scorer on synthetic fixtures. None is proposed as a threshold, bar,
attainment, readiness, or production outcome.

## 6. Portable baseline reference (Batch-1, values only)

Portable canonical baseline SHA256 `defaefa12dd177ae6a6ee9c0978ca3aa110c1c6c522f97c0b58d46509c887ff0`
(method: `canonical_sha256` over `canonical_baseline`; re-verified under two
isolated HOME roots at preflight). No bar claim attaches to this value here.

## 7. Topics explicitly unobserved in this batch

- natural-language extraction quality: NOT_OBSERVED
- real model quality: NOT_OBSERVED
- real-data coverage/eligibility: NOT_OBSERVED
- live resource impact: NOT_OBSERVED

No model calls, no network calls, no live reads or writes in the M5prep
scratch gates; ops counters observed at 0 over the four guarded entry points.

## 8. Explicit non-claims

- The v1 corpus stays parser-conditioned and out of bar evidence; any
  adjudication of its two corrected cases is deferred and advisory only.
- M5 NOT RATIFIED; M6 locked.
- Batch-1 remains on its branch, not merged; no production enablement is claimed.
- Issue `MnemoSeed/mnemoseed-local#113` and #75 stay open; nothing here closes either.