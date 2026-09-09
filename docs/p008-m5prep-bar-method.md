# P-008 M5prep Bar Method (definitions as infrastructure; no number proposed)

Status: this note defines the future M5 ratification method only. No number
is proposed here, no criterion is set, and nothing here claims M5 attainment,
production enablement, or closure of any issue.

## 1. Future ratification method

A future M5 claim would require all four of the following, in order:

1. A sequestered corpus held out of development, frozen under a
   pre-registered dual-control seal before any bar work starts.
2. Exactly one blind scoring run through the registry-logged path. A second
   run against the same truth needs a freshly authored and resealed corpus.
3. An SDT-shape summary computed by `report_sdt`: per-channel counts for
   `fact` versus `experience`, a `correct_rejection` count, a `misclass`
   list, a `zero_reason_histogram`, a confidence-decile table, and an
   `unscored_units` count. Counts only; the summary records no verdict.
4. The product gate P4 plus an independent QA verdict before any claim.

## 2. SDT shape (counts, never a bar)

Unit equivalence mirrors the scorer: normalized structural payload equality
or a shared nonempty `paraphrase_group_id`, always fenced by class and
scope, matched one-to-one; predicted evidence must cover the expected
evidence ids or the unit does not match.

- An expected accepted unit that matches counts as a `hit` on its channel.
- An expected accepted unit left unmatched counts as a `miss`.
- A predicted accepted unit left unmatched (including wrong provenance)
  counts as a `false_alarm` on its channel.
- A cross-class accepted match records one `misclass` entry
  (`case_id`, `expected_class`, `predicted_class`) plus a `miss` on the
  expected channel, and no hit anywhere.
- A truth-zero case with a predicted zero counts as one
  `correct_rejection`; a truth-zero case with predicted accepted units
  counts `false_alarm` per unit.
- Confidence is passthrough only: the fixed 10-bin table over the unit
  interval covers predicted accepted units; missing, non-numeric, or
  out-of-range confidence moves the unit to `unscored_units`. The bins
  carry no criterion and mask no provenance gap.

## 3. What a future numeric proposal must contain

A future proposal is a method note for later review, never a
self-executing verdict. It must use only the benign allowlist keys
(`method`, `required_gates`, `forbidden_claims`, `not_observed`,
`report_fields`, `version`, `n_cases`, `min_cases`, `corpus_size`).
`validate_bar_proposal` rejects any other key whose normalized form equals
or contains a banned token (for example `target_p`, `cut_off`, `min_p`, or
`quality_gate`). Values under allowlisted keys are never scanned.

## 4. Explicit non-claims

- The v1 corpus stays parser-conditioned and out of bar evidence.
- M5 NOT RATIFIED; M6 locked.
- Batch-1 remains on its branch, not merged; promotion remains locked and
  no production enablement is claimed.
- NOT_OBSERVED topics, each explicitly unobserved in this batch:
  - natural-language extraction quality: NOT_OBSERVED
  - real model quality: NOT_OBSERVED
  - real-data coverage/eligibility: NOT_OBSERVED
  - live resource impact: NOT_OBSERVED
