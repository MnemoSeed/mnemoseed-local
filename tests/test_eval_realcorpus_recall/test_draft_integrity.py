"""Draft query-set integrity: unsigned attribution and a reproducible fingerprint."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

QUERIES_PATH = Path(__file__).resolve().parents[2] / "docs/zh/eval/realcorpus-recall/queries-v1.json"


def _load() -> dict[str, Any]:
    return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))


def test_draft_is_unsigned_with_neutral_author() -> None:
    document = _load()
    assert document["signed"] is False
    assert document["owner_signature"] is None
    for query in document["queries"]:
        assert query["author"] == "draft"
        assert query["frozen_sha256"] == ""


def test_draft_sha256_is_reproducible() -> None:
    document = _load()
    assert "draft_sha256_algorithm" in document
    payload = {key: value for key, value in document.items() if key != "draft_sha256"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert document["draft_sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
