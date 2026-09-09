"""Blind report verifier for isolated baseline reports."""

import json
import re

from mnemoseed_local.eval.experience_baseline import canonical_sha256


def verify_blind_report(report: object) -> list[str]:
    """Check a blind report against its frozen canonical digest."""
    if not isinstance(report, dict):
        return ["report-not-mapping"]
    if "canonical_sha256" not in report:
        return ["report-missing-key:canonical_sha256"]
    stripped = dict(report)
    stored = stripped.pop("canonical_sha256")
    if not isinstance(stored, str) or re.fullmatch(r"[0-9a-f]{64}", stored) is None:
        return ["report-bad-canonical-field"]
    if "files_read" in stripped:
        files_read = stripped["files_read"]
        if not isinstance(files_read, list) or any(not isinstance(entry, str) for entry in files_read):
            return ["canonical-malformed:files_read"]
    try:
        json.dumps(stripped, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return ["canonical-unserializable"]
    try:
        computed = canonical_sha256(stripped)
    except (TypeError, ValueError):
        return ["canonical-unserializable"]
    if computed != stored:
        return ["canonical-mismatch"]
    return []
