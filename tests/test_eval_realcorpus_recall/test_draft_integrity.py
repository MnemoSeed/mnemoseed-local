"""Query-set integrity across its DRAFT and owner-signed FROZEN lifecycle states."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

QUERIES_PATH = Path(__file__).resolve().parents[2] / "docs/zh/eval/realcorpus-recall/queries-v1.json"

_EXPECTED_QUERY_KINDS = {
    "Q-001": "miss_seed",
    "Q-002": "positive",
    "Q-003": "honest_empty",
    "Q-004": "honest_empty",
}


def _load() -> dict[str, Any]:
    return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reproduce_draft_sha256(document: dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "draft_sha256"}
    return _digest(payload)


def _reproduce_frozen_sha256(document: dict[str, Any]) -> str:
    # The whole-set fingerprint covers each entry's frozen_sha256, so only the top-level field is excluded.
    payload = {key: value for key, value in document.items() if key != "frozen_sha256"}
    return _digest(payload)


def _reproduce_entry_sha256(query: dict[str, Any]) -> str:
    payload = {key: value for key, value in query.items() if key != "frozen_sha256"}
    return _digest(payload)


def _assert_draft(document: dict[str, Any]) -> None:
    assert document["signed"] is False
    assert document["owner_signature"] is None
    for query in document["queries"]:
        assert query["author"] == "draft"
        assert query["frozen_sha256"] == ""
    assert "draft_sha256_algorithm" in document
    assert document["draft_sha256"] == _reproduce_draft_sha256(document)


def _assert_frozen(document: dict[str, Any]) -> None:
    assert document["signed"] is True
    signature = document["owner_signature"]
    assert isinstance(signature, dict) and signature
    assert signature["verbatim"]
    assert signature["date"]
    assert "frozen_sha256_algorithm" in document
    assert document["frozen_sha256"] == _reproduce_frozen_sha256(document)
    queries = document["queries"]
    assert {query["qid"]: query["kind"] for query in queries} == _EXPECTED_QUERY_KINDS
    for query in queries:
        assert query["frozen_sha256"]
        assert query["author"] == "owner"
        assert isinstance(query["ingestion_verified"], bool)
        assert query["frozen_sha256"] == _reproduce_entry_sha256(query)


def test_query_set_integrity_for_current_lifecycle_state() -> None:
    document = _load()
    if document.get("signed") is True:
        _assert_frozen(document)
    else:
        _assert_draft(document)


def test_frozen_integrity_rejects_downgrade_and_tampering() -> None:
    document = _load()
    if document.get("signed") is not True:
        pytest.skip("query set is currently DRAFT")

    downgraded = copy.deepcopy(document)
    downgraded["signed"] = False
    with pytest.raises(AssertionError):
        _assert_frozen(downgraded)

    tampered_whole = copy.deepcopy(document)
    tampered_whole["frozen_sha256"] = "0" * 64
    with pytest.raises(AssertionError):
        _assert_frozen(tampered_whole)

    tampered_entry = copy.deepcopy(document)
    tampered_entry["queries"][0]["frozen_sha256"] = "0" * 64
    with pytest.raises(AssertionError):
        _assert_frozen(tampered_entry)
