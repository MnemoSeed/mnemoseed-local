"""Append-only JSON report with a deterministic comparison shape."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

VOLATILE_FIELDS = frozenset({"utc", "run_id"})


def append_report(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n")


def read_report(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def deterministic_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in VOLATILE_FIELDS}
