# P-008 golden synthetic corpus (synthetic-only)

This directory holds the frozen synthetic fixtures for the Batch-1
parser/contract/scorer baseline: parser inputs plus independently frozen
truth. Everything here is hand-built synthetic text for deterministic
evaluation. Nothing here is production data, model output, or a quality
claim about natural-language extraction.

Planned files (later lanes fill them; this README only records the layout):

- `inputs.json`: `{version: 1, cases: [...]}` with raw candidates and the
  authoritative `SourceCatalog` per case.
- `truth.json`: `{version: 1, cases: [...]}` with expected status,
  zero reasons, coercion flags, conflict flags, and expected units.
- `README.md` (this file): layout and honesty notes.

Honesty notes:

- The near-context-window delta is a small-size placeholder and is labeled
  as such in truth; it is not a real window-pressure claim.
- Confidence/weight fields are carried through but never scored.
- Provenance is by explicit evidence id only; span/order/default fallback
  is malformed.
