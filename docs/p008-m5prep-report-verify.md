# P008 M5-prep blind report verifier

Synthetic canonical-first engineering reference only.

## API

`verify_blind_report(report: object) -> list[str]` in
`src/mnemoseed_local/eval/m5prep_report_verify.py`.
Pure zero-I/O check over an in-memory blind report. Imports only
`json`, `re`, and the frozen `canonical_sha256`.

## Ordered findings

First match wins; success is `[]`:

1. `report-not-mapping` when the report is not a dict.
2. `report-missing-key:canonical_sha256` when the own key is absent.
3. `report-bad-canonical-field` when the stored value is not a lowercase `[0-9a-f]{64}` string.
4. `canonical-malformed:files_read` when a present `files_read` is not `list[str]`.
5. `canonical-unserializable` when `json.dumps(stripped, sort_keys=True, allow_nan=False)` raises `TypeError`/`ValueError`, or when the frozen `canonical_sha256` raises the same.
6. `canonical-mismatch` when the recomputed digest differs from the stored one.
7. `[]` on match.

The verifier shallow-copies the input dict, pops its own
`canonical_sha256`, and never mutates the caller.

## Canonical scope

JSON member order and whitespace do not affect the digest.
`started_at`, `duration_s`, `out_path`, `isolation.home`, and
`files_read` OS-native path prefixes are canonical: only file basenames
and the `<isolated-home>` token participate. Backslash paths are not
universally portable; compare by basename only.

Any protected change mismatches: `score`, `sdt`, `claims`, `seal`,
`note`, `ops` counters, `isolation.home_writes`, key insertion, or key
deletion.

## Limits

`[]` means checksum-consistent only. It is not schema validation, score
correctness, provenance closure, or authenticity. A correctly rehashed
malicious object is undetectable by this check; immutable artifact byte
hashes remain the authenticity anchor.

No cognitive claims, quality bar, threshold, or verdict are expressed.

## Status

M5 NOT RATIFIED. M6 locked. Issues #113 and #75 remain open.
No production use and no merge.
