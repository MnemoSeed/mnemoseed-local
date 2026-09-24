"""Emit a strict, allowlisted Atlas proof summary."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_STATUS = re.compile(r"^(PASS|FAIL|INCONCLUSIVE)$")
_COUNT = re.compile(r"^(empty_count|filled_count)=([0-9]+)(?: (?:item|items).*)?$")
_ORACLE = re.compile(r"^(profile_isolation|remember_to_atlas)=PASS$")


def sanitize(path: Path, source_sha: str) -> str:
    status = "INCONCLUSIVE"
    lines: list[str] = []
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if text and _STATUS.fullmatch(text[0]):
            status = text[0]
        for line in text[1:]:
            if _STATUS.fullmatch(line):
                status = "INCONCLUSIVE"
            elif match := _COUNT.fullmatch(line):
                lines.append(f"{match.group(1)}={match.group(2)}")
            elif _ORACLE.fullmatch(line):
                lines.append(line)
    return (
        "\n".join(
            [
                f"status={status}",
                f"source_sha={source_sha}",
                "run_id=<redacted>",
                *lines,
            ]
        )
        + "\n"
    )


if __name__ == "__main__":
    evidence_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("missing-evidence")
    sha = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    print(sanitize(evidence_path, sha), end="")
