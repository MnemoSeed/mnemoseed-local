"""Lane A parser and contract tests for P-008 Batch-1 (T-EX3/4/5/6/7/9).

Exercises parse_experience_raw and build_e2_candidates over inline
independently frozen fixtures. No daemon, config, provider, port, or
model use: the owned module is stdlib-only.
"""

from __future__ import annotations

import copy
import inspect
import os
import subprocess
import sys

import pytest

from mnemoseed_local.eval.experience_contract import (
    ParseResult,
    SourceCatalog,
    build_e2_candidates,
    parse_experience_raw,
)

_PARSE_KEYS = {
    "status",
    "candidates",
    "zero_result",
    "zero_reason",
    "coercion_loss",
    "conflict",
    "conflict_reason",
    "report",
}
_REPORT_KEYS = {"reason", "disposition", "run_id", "conflicts"}
_ZERO_REASONS = {"no-match", "malformed-input", "over-budget-overflow", "all-filtered"}


def _src(kind="chunk", sid="s1", fixture_id="fx-001", text="Ada likes tea.", span="0:13"):
    return {"kind": kind, "id": sid, "fixture_id": fixture_id, "text": text, "span": span}


def _catalog(run_id="run-001", entries=None) -> SourceCatalog:
    if entries is None:
        entries = [_src()]
    return {"run_id": run_id, "sources": entries}


def _ptr(kind="chunk", sid="s1"):
    return {"kind": kind, "id": sid}


def _fact(cid="c-fact-1", scope="scope-1", evidence=None, payload=None, **extra):
    if evidence is None:
        evidence = [_ptr()]
    if payload is None:
        payload = {
            "subject": "Ada",
            "predicate": "likes",
            "object": "tea",
            "polarity": "positive",
        }
    candidate = {
        "candidate_id": cid,
        "class": "fact",
        "scope": scope,
        "payload": payload,
        "evidence": evidence,
    }
    candidate.update(extra)
    return candidate


def _lesson(cid="c-lesson-1", scope="scope-1", text="Retry after backoff.", evidence=None, **extra):
    if evidence is None:
        evidence = [_ptr()]
    candidate = {
        "candidate_id": cid,
        "class": "lesson",
        "scope": scope,
        "payload": {"text": text},
        "evidence": evidence,
    }
    candidate.update(extra)
    return candidate


def _intention(cid="c-int-1", scope="scope-1", status="pending", evidence=None, **extra):
    if evidence is None:
        evidence = [_ptr()]
    candidate = {
        "candidate_id": cid,
        "class": "intention",
        "scope": scope,
        "payload": {"trigger_condition": "If late", "action": "Page oncall", "status": status},
        "evidence": evidence,
    }
    candidate.update(extra)
    return candidate


def _skill(cid="c-skill-1", scope="scope-1", evidence=None, **extra):
    if evidence is None:
        evidence = [_ptr()]
    candidate = {
        "candidate_id": cid,
        "class": "skill_sequence",
        "scope": scope,
        "payload": {"task_type": "backup", "tool_chain": ["snapshot", "copy"], "success_rate": 0.5},
        "evidence": evidence,
    }
    candidate.update(extra)
    return candidate


def _assert_envelope(result: ParseResult, run_id="run-001"):
    assert isinstance(result, dict)
    assert set(result) == _PARSE_KEYS
    assert result["status"] in ("accepted", "unresolved")
    assert isinstance(result["candidates"], list)
    assert isinstance(result["zero_result"], bool)
    assert isinstance(result["coercion_loss"], bool)
    assert isinstance(result["conflict"], bool)
    if result["candidates"]:
        assert result["status"] == "accepted"
        assert result["zero_result"] is False
        assert result["zero_reason"] is None
    else:
        assert result["status"] == "unresolved"
        assert result["zero_result"] is True
        assert result["zero_reason"] in _ZERO_REASONS
    if result["conflict"]:
        assert isinstance(result["conflict_reason"], str) and result["conflict_reason"]
    else:
        assert result["conflict_reason"] is None
    report = result["report"]
    assert isinstance(report, dict)
    assert set(report) == _REPORT_KEYS
    assert report["disposition"] == result["status"]
    assert isinstance(report["reason"], str) and report["reason"]
    assert report["run_id"] == run_id
    assert isinstance(report["conflicts"], list)


def _assert_accepted_candidate(candidate, cid, scope, klass, fixture_id="fx-001", span="0:13"):
    assert candidate["candidate_id"] == cid
    assert candidate["class"] == klass
    assert candidate["scope"] == scope
    assert isinstance(candidate["payload"], dict)
    assert candidate["evidence"]
    for pointer in candidate["evidence"]:
        assert set(pointer) == {"kind", "id"}
    assert candidate["source_ref"] == {"fixture_id": fixture_id, "span": span}


# -- T-EX4 no-silent-zero -----------------------------------------------------


def test_none_is_no_match():
    result = parse_experience_raw(None, sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "no-match"
    assert result["report"]["reason"] == "no-match: empty input"
    assert result["coercion_loss"] is False
    assert result["conflict"] is False


def test_blank_text_is_no_match():
    for blank in ("", "   ", "\t\n "):
        result = parse_experience_raw(blank, sources=_catalog())
        _assert_envelope(result)
        assert result["zero_reason"] == "no-match"


def test_empty_list_is_no_match():
    result = parse_experience_raw([], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "no-match"


def test_json_empty_array_is_no_match():
    result = parse_experience_raw("[]", sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "no-match"


def test_json_null_is_no_match():
    result = parse_experience_raw("null", sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "no-match"


def test_no_match_with_invalid_catalog_reports_null_run_id():
    result = parse_experience_raw(None, sources={"run_id": 42, "sources": []})
    _assert_envelope(result, run_id=None)
    assert result["zero_reason"] == "no-match"


@pytest.mark.parametrize("scalar", ["42", "true", '"hi"', "3.5"])
def test_json_scalar_is_malformed(scalar):
    result = parse_experience_raw(scalar, sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["candidates"] == []


@pytest.mark.parametrize("scalar", [42, True, 5.5])
def test_python_scalar_is_malformed(scalar):
    result = parse_experience_raw(scalar, sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"


def test_empty_dict_is_malformed():
    for raw in ({}, "{}"):
        result = parse_experience_raw(raw, sources=_catalog())
        _assert_envelope(result)
        assert result["zero_reason"] == "malformed-input"
        assert result["coercion_loss"] is False


def test_malformed_envelope_reports_catalog_run_id():
    result = parse_experience_raw("not json", sources=_catalog(run_id="run-7"))
    _assert_envelope(result, run_id="run-7")
    assert result["zero_reason"] == "malformed-input"
    assert result["report"]["reason"].startswith("malformed-input: ")


def test_parser_never_reports_budget_overflow():
    cases = [
        None,
        [],
        "   ",
        "broken",
        [_fact()],
        [_lesson(), {"candidate_id": "bad"}],
    ]
    for raw in cases:
        result = parse_experience_raw(raw, sources=_catalog())
        _assert_envelope(result)
        assert result["zero_reason"] != "over-budget-overflow"


# -- T-EX3 provenance closure --------------------------------------------------


def test_accepted_fact_carries_catalog_source_ref():
    result = parse_experience_raw([_fact()], sources=_catalog())
    _assert_envelope(result)
    assert result["report"]["reason"] == "accepted"
    assert len(result["candidates"]) == 1
    _assert_accepted_candidate(result["candidates"][0], "c-fact-1", "scope-1", "fact")


def test_session_and_node_kinds_resolve():
    catalog = _catalog(
        entries=[
            _src(kind="session", sid="sess-9", fixture_id="fx-s", text="Session note.", span="s"),
            _src(kind="node", sid="n-3", fixture_id="fx-n", text="Node note.", span=None),
        ]
    )
    first = _lesson(cid="c-a", evidence=[_ptr(kind="session", sid="sess-9")])
    second = _lesson(cid="c-b", text="Different lesson words.", evidence=[_ptr(kind="node", sid="n-3")])
    result = parse_experience_raw([first, second], sources=catalog)
    _assert_envelope(result)
    assert len(result["candidates"]) == 2
    by_id = {item["candidate_id"]: item for item in result["candidates"]}
    assert by_id["c-a"]["source_ref"] == {"fixture_id": "fx-s", "span": "s"}
    assert by_id["c-b"]["source_ref"] == {"fixture_id": "fx-n", "span": None}


def test_null_span_source_resolves():
    catalog = _catalog(entries=[_src(span=None)])
    result = parse_experience_raw([_fact()], sources=catalog)
    _assert_envelope(result)
    assert result["candidates"][0]["source_ref"] == {"fixture_id": "fx-001", "span": None}


def test_missing_evidence_key_is_malformed():
    candidate = _fact()
    del candidate["evidence"]
    result = parse_experience_raw([candidate], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["coercion_loss"] is False


def test_empty_evidence_is_malformed():
    result = parse_experience_raw([_fact(evidence=[])], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"


def test_bogus_evidence_id_is_malformed():
    result = parse_experience_raw([_fact(evidence=[_ptr(sid="ghost")])], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["candidates"] == []


def test_bogus_evidence_kind_is_malformed():
    result = parse_experience_raw([_fact(evidence=[{"kind": "span", "id": "s1"}])], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"


def test_evidence_without_id_is_malformed():
    result = parse_experience_raw([_fact(evidence=[{"kind": "chunk"}])], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"


def test_high_confidence_cannot_mask_missing_provenance():
    candidate = _fact(evidence=[], confidence=0.99, weight=10)
    result = parse_experience_raw([candidate], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["candidates"] == []


def test_high_confidence_cannot_mask_bogus_pointer():
    candidate = _fact(evidence=[_ptr(sid="ghost")], confidence=0.99)
    result = parse_experience_raw([candidate], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["candidates"] == []


def test_forged_source_ref_claim_is_rejected():
    candidate = _fact(source_ref={"fixture_id": "fx-forged", "span": "9:99"})
    result = parse_experience_raw([candidate], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["coercion_loss"] is True
    assert "source_ref" in result["report"]["reason"]


def test_mixed_valid_and_bogus_has_no_partial_success():
    catalog = _catalog(entries=[_src(), _src(sid="s2", fixture_id="fx-002", text="Second.")])
    valid = _fact(cid="c-good", evidence=[_ptr()])
    bogus = _fact(cid="c-bad", evidence=[_ptr(sid="ghost")])
    result = parse_experience_raw([valid, bogus], sources=catalog)
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["candidates"] == []


def test_json_text_candidate_is_accepted():
    import json as _json

    raw = _json.dumps([_fact(cid="c-json-1")])
    result = parse_experience_raw(raw, sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 1
    _assert_accepted_candidate(result["candidates"][0], "c-json-1", "scope-1", "fact")


def test_single_dict_candidate_is_accepted():
    result = parse_experience_raw(_lesson(cid="c-solo"), sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 1


# -- T-EX5 malformed and coercion fail-closed -----------------------------------


def test_invalid_json_is_malformed():
    result = parse_experience_raw("{oops", sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["coercion_loss"] is False


def test_duplicate_json_keys_are_rejected():
    raw = (
        '[{"candidate_id": "c-d", "class": "lesson", "scope": "s", '
        '"payload": {"text": "Go."}, "scope": "t", "evidence": [{"kind": "chunk", "id": "s1"}]}]'
    )
    result = parse_experience_raw(raw, sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert "duplicate" in result["report"]["reason"]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_constants_are_rejected(constant):
    raw = (
        '[{"candidate_id": "c-n", "class": "lesson", "scope": "s", '
        '"payload": {"text": "Go."}, "confidence": ' + constant + ", "
        '"evidence": [{"kind": "chunk", "id": "s1"}]}]'
    )
    result = parse_experience_raw(raw, sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"


@pytest.mark.parametrize("field", ["candidate_id", "class", "scope", "payload", "evidence"])
def test_missing_required_field_is_malformed_without_coercion(field):
    candidate = _fact()
    del candidate[field]
    result = parse_experience_raw([candidate], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["coercion_loss"] is False
    assert field in result["report"]["reason"]


def test_wrong_envelope_types_are_malformed():
    for candidate in (
        _fact(cid=7),
        _lesson(payload=["not", "a", "dict"]),
        _fact(evidence={"kind": "chunk", "id": "s1"}),
        _fact(scope=None),
    ):
        result = parse_experience_raw([candidate], sources=_catalog())
        _assert_envelope(result)
        assert result["zero_reason"] == "malformed-input"
        assert result["coercion_loss"] is False


@pytest.mark.parametrize("field", ["debug", "answer", "truth", "score"])
def test_unknown_envelope_field_is_coercion_loss(field):
    result = parse_experience_raw([_fact(**{field: "x"})], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["coercion_loss"] is True
    assert field in result["report"]["reason"]


def test_unknown_evidence_field_is_coercion_loss():
    pointer = {"kind": "chunk", "id": "s1", "span": "0:13"}
    result = parse_experience_raw([_fact(evidence=[pointer])], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert result["coercion_loss"] is True
    assert "span" in result["report"]["reason"]


def test_payload_extensions_are_preserved_not_coercion():
    payload = {
        "subject": "Ada",
        "predicate": "likes",
        "object": "tea",
        "polarity": "positive",
        "note": "kept",
    }
    result = parse_experience_raw([_fact(payload=payload)], sources=_catalog())
    _assert_envelope(result)
    assert result["coercion_loss"] is False
    assert result["candidates"][0]["payload"]["note"] == "kept"


def test_confidence_and_weight_are_preserved_never_scored():
    first = _lesson(cid="c-w-1", text="Same words.", confidence=0.1, weight=1)
    second = _lesson(cid="c-w-2", text="Same words.", confidence=0.99, weight=9)
    result = parse_experience_raw([first, second], sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 1
    kept = result["candidates"][0]
    assert kept["candidate_id"] == "c-w-1"
    assert kept["confidence"] == 0.1
    assert kept["weight"] == 1


def test_missing_fact_polarity_is_malformed_not_defaulted():
    payload = {"subject": "Ada", "predicate": "likes", "object": "tea"}
    result = parse_experience_raw([_fact(payload=payload)], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert "polarity" in result["report"]["reason"]


def test_valid_intention_and_skill_shapes_are_accepted():
    result = parse_experience_raw([_intention(cid="c-i-ok"), _skill(cid="c-s-ok")], sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 2
    by_id = {item["candidate_id"]: item for item in result["candidates"]}
    assert by_id["c-i-ok"]["payload"]["status"] == "pending"
    assert by_id["c-s-ok"]["payload"]["success_rate"] == 0.5


def test_bad_intention_status_is_malformed():
    for status in ("archived", "Pending", None, 3):
        result = parse_experience_raw([_intention(status=status)], sources=_catalog())
        _assert_envelope(result)
        assert result["zero_reason"] == "malformed-input"


def test_bad_skill_rate_is_malformed():
    for rate in (None, True, "0.9"):
        payload = {"task_type": "backup", "tool_chain": [], "success_rate": rate}
        candidate = _skill()
        candidate["payload"] = payload
        result = parse_experience_raw([candidate], sources=_catalog())
        _assert_envelope(result)
        assert result["zero_reason"] == "malformed-input"


def test_skill_int_rate_matches_float_rate():
    base = _skill(cid="c-int-rate")
    base["payload"] = {"task_type": "t", "tool_chain": ["a"], "success_rate": 1}
    same = _skill(cid="c-float-rate")
    same["payload"] = {"task_type": "t", "tool_chain": ["a"], "success_rate": 1.0}
    result = parse_experience_raw([base, same], sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 1


def test_nonfinite_float_in_dict_input_is_rejected():
    candidate = _lesson(confidence=float("nan"))
    result = parse_experience_raw([candidate], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"


def test_non_json_leaf_is_rejected():
    payload = {"text": "Go.", "tags": ("a", "b")}
    result = parse_experience_raw([_lesson(payload=payload)], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"


@pytest.mark.parametrize(
    "catalog",
    [
        {"sources": []},
        {"run_id": 42, "sources": []},
        {"run_id": "", "sources": []},
        {"run_id": "r", "sources": {}},
        {"run_id": "r", "sources": [{"kind": "chunk", "id": "s1"}]},
        {
            "run_id": "r",
            "sources": [{"kind": "span", "id": "s1", "fixture_id": "f", "text": "t", "span": None}],
        },
        {
            "run_id": "r",
            "sources": [
                {"kind": "chunk", "id": "s1", "fixture_id": "f", "text": "t", "span": None},
                {"kind": "chunk", "id": "s1", "fixture_id": "g", "text": "u", "span": None},
            ],
        },
        {
            "run_id": "r",
            "sources": [{"kind": "chunk", "id": "s1", "fixture_id": "f", "text": "t", "span": 7}],
        },
        {"run_id": "r", "sources": [], "extra": True},
    ],
)
def test_invalid_catalog_is_malformed_with_null_run_id(catalog):
    result = parse_experience_raw([_fact()], sources=catalog)
    _assert_envelope(result, run_id=None)
    assert result["zero_reason"] == "malformed-input"
    assert result["candidates"] == []


# -- T-EX6 deterministic dedup --------------------------------------------------


def test_byte_same_triple_collapses_with_evidence_union():
    catalog = _catalog(entries=[_src(), _src(sid="s2", fixture_id="fx-002", text="Second.")])
    raws = [
        _lesson(cid="c-dup-3", evidence=[_ptr()]),
        _lesson(cid="c-dup-1", evidence=[_ptr(sid="s2")]),
        _lesson(cid="c-dup-2", evidence=[_ptr(), _ptr(sid="s2")]),
    ]
    result = parse_experience_raw(raws, sources=catalog)
    _assert_envelope(result)
    assert len(result["candidates"]) == 1
    only = result["candidates"][0]
    assert only["candidate_id"] == "c-dup-1"
    assert only["evidence"] == [{"kind": "chunk", "id": "s1"}, {"kind": "chunk", "id": "s2"}]
    assert only["source_ref"] == {"fixture_id": "fx-002", "span": "0:13"}


def test_paraphrase_group_collapses_with_multi_source_evidence():
    catalog = _catalog(entries=[_src(), _src(sid="s2", fixture_id="fx-002", text="Second.")])
    first = _lesson(cid="c-p-2", text="Retry after backoff.", evidence=[_ptr()], paraphrase_group_id="g-1")
    second = _lesson(
        cid="c-p-1", text="Retry once backoff passes.", evidence=[_ptr(sid="s2")], paraphrase_group_id="g-1"
    )
    result = parse_experience_raw([first, second], sources=catalog)
    _assert_envelope(result)
    assert len(result["candidates"]) == 1
    only = result["candidates"][0]
    assert only["candidate_id"] == "c-p-1"
    assert only["evidence"] == [{"kind": "chunk", "id": "s1"}, {"kind": "chunk", "id": "s2"}]


def test_ungrouped_rewrite_is_not_merged():
    first = _lesson(cid="c-u-1", text="Retry after backoff.")
    second = _lesson(cid="c-u-2", text="Retry once backoff passes.")
    result = parse_experience_raw([first, second], sources=_catalog())
    _assert_envelope(result)
    assert [item["candidate_id"] for item in result["candidates"]] == ["c-u-1", "c-u-2"]


def test_scope_fences_dedup():
    first = _lesson(cid="c-s-1", scope="scope-a", text="Same words.", paraphrase_group_id="g-x")
    second = _lesson(cid="c-s-2", scope="scope-b", text="Same words.", paraphrase_group_id="g-x")
    result = parse_experience_raw([first, second], sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 2


def test_class_fences_dedup():
    lesson = _lesson(cid="c-c-1", scope="scope-1", text="Shared.", paraphrase_group_id="g-x")
    intention = _intention(cid="c-c-2", scope="scope-1", paraphrase_group_id="g-x")
    result = parse_experience_raw([lesson, intention], sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 2


def test_transitive_groups_collapse():
    first = _lesson(cid="c-t-1", text="Retry after backoff.")
    second = _lesson(cid="c-t-2", text="Retry after backoff.", paraphrase_group_id="g-t")
    third = _lesson(cid="c-t-3", text="Totally different words.", paraphrase_group_id="g-t")
    result = parse_experience_raw([first, second, third], sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["candidate_id"] == "c-t-1"


def test_output_order_is_deterministic_despite_input_order():
    first = _fact(cid="c-z-9")
    second = _lesson(cid="c-a-1", text="Other words here.")
    forward = parse_experience_raw([first, second], sources=_catalog())
    backward = parse_experience_raw([second, first], sources=_catalog())
    _assert_envelope(forward)
    _assert_envelope(backward)
    assert forward["candidates"] == backward["candidates"]
    assert [item["candidate_id"] for item in forward["candidates"]] == ["c-a-1", "c-z-9"]


def test_repeated_calls_are_equal_but_independent():
    raw = [_fact(cid="c-r-1"), _lesson(cid="c-r-2", text="Other words here.")]
    first = parse_experience_raw(raw, sources=_catalog())
    second = parse_experience_raw(raw, sources=_catalog())
    assert first == second
    assert first is not second
    assert first["candidates"][0] is not second["candidates"][0]


def test_identical_id_repeats_are_allowed():
    twin = _lesson(cid="c-tw", text="Retry after backoff.")
    result = parse_experience_raw([twin, copy.deepcopy(twin)], sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["candidate_id"] == "c-tw"


def test_conflicting_id_reuse_is_malformed():
    first = _lesson(cid="c-re", text="Retry after backoff.")
    second = _lesson(cid="c-re", text="Never retry.")
    result = parse_experience_raw([first, second], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert "c-re" in result["report"]["reason"]


def test_conflicting_id_reuse_across_scopes_is_malformed():
    first = _lesson(cid="c-re2", scope="scope-a", text="Same.")
    second = _lesson(cid="c-re2", scope="scope-b", text="Same.")
    result = parse_experience_raw([first, second], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"


def test_payload_is_preserved_not_normalized_in_place():
    payload = {"text": "  Retry   AFTER backoff. "}
    raw = [_lesson(cid="c-raw", payload=payload)]
    snapshot = copy.deepcopy(raw)
    catalog = _catalog()
    catalog_snapshot = copy.deepcopy(catalog)
    result = parse_experience_raw(raw, sources=catalog)
    _assert_envelope(result)
    assert result["candidates"][0]["payload"] == {"text": "  Retry   AFTER backoff. "}
    assert raw == snapshot
    assert catalog == catalog_snapshot


def test_duplicate_pointers_within_candidate_dedup():
    candidate = _fact(evidence=[_ptr(), _ptr()])
    result = parse_experience_raw([candidate], sources=_catalog())
    _assert_envelope(result)
    assert result["candidates"][0]["evidence"] == [{"kind": "chunk", "id": "s1"}]


# -- T-EX7 conflict fail-closed --------------------------------------------------


def test_lesson_pair_filters_both_with_all_filtered():
    first = _lesson(cid="c-x-1", text="Do Y before X.", conflict_pair_id="p-1")
    second = _lesson(cid="c-x-2", text="Never do Y before X.", conflict_pair_id="p-1")
    result = parse_experience_raw([first, second], sources=_catalog())
    _assert_envelope(result)
    assert result["zero_reason"] == "all-filtered"
    assert result["candidates"] == []
    assert result["conflict"] is True
    assert "c-x-1" in result["conflict_reason"]
    assert "c-x-2" in result["conflict_reason"]
    assert result["report"]["conflicts"] == [["c-x-1", "c-x-2"]]


def test_mixed_conflict_and_valid_keeps_unrelated():
    first = _lesson(cid="c-m-1", text="Do Y before X.", conflict_pair_id="p-9")
    second = _lesson(cid="c-m-2", text="Never do Y before X.", conflict_pair_id="p-9")
    third = _fact(cid="c-m-3")
    result = parse_experience_raw([first, second, third], sources=_catalog())
    _assert_envelope(result)
    assert result["report"]["reason"] == "accepted-with-conflicts-filtered"
    assert [item["candidate_id"] for item in result["candidates"]] == ["c-m-3"]
    assert result["conflict"] is True
    assert result["report"]["conflicts"] == [["c-m-1", "c-m-2"]]


def test_same_side_repetition_is_not_a_second_side():
    twin = _lesson(cid="c-1side", text="Do Y before X.", conflict_pair_id="p-1")
    result = parse_experience_raw([twin, copy.deepcopy(twin)], sources=_catalog())
    _assert_envelope(result)
    assert result["conflict"] is False
    assert len(result["candidates"]) == 1


def test_undeclared_pair_is_never_conflict():
    first = _lesson(cid="c-n-1", text="Do Y before X.")
    second = _lesson(cid="c-n-2", text="Never do Y before X.")
    result = parse_experience_raw([first, second], sources=_catalog())
    _assert_envelope(result)
    assert result["conflict"] is False
    assert len(result["candidates"]) == 2


def test_pair_across_scopes_is_not_conflict():
    first = _lesson(cid="c-q-1", scope="scope-a", text="Do Y.", conflict_pair_id="p-1")
    second = _lesson(cid="c-q-2", scope="scope-b", text="Skip Y.", conflict_pair_id="p-1")
    result = parse_experience_raw([first, second], sources=_catalog())
    _assert_envelope(result)
    assert result["conflict"] is False
    assert len(result["candidates"]) == 2


def test_conflict_propagates_to_unflagged_alias():
    first = _lesson(cid="c-k-1", text="Do Y before X.", conflict_pair_id="p-5")
    second = _lesson(cid="c-k-2", text="Never do Y before X.", conflict_pair_id="p-5")
    alias = _lesson(cid="c-k-3", text="Do Y before X.")
    kept = _fact(cid="c-k-4")
    result = parse_experience_raw([first, second, alias, kept], sources=_catalog())
    _assert_envelope(result)
    assert [item["candidate_id"] for item in result["candidates"]] == ["c-k-4"]
    assert result["conflict"] is True
    for cid in ("c-k-1", "c-k-2", "c-k-3"):
        assert cid in result["conflict_reason"]
    assert result["report"]["conflicts"] == [["c-k-1", "c-k-2"]]


def test_pair_id_on_non_lesson_is_not_conflict():
    other_payload = {
        "subject": "Ada",
        "predicate": "likes",
        "object": "coffee",
        "polarity": "positive",
    }
    first = _fact(cid="c-f-1", conflict_pair_id="p-1")
    second = _fact(cid="c-f-2", payload=other_payload, conflict_pair_id="p-1")
    result = parse_experience_raw([first, second], sources=_catalog())
    _assert_envelope(result)
    assert result["conflict"] is False
    assert len(result["candidates"]) == 2


# -- T-EX9 E2 candidate channel (default-off) ------------------------------------


def _e2_context():
    catalog = _catalog()
    candidate = _lesson(cid="c-e2-1", text="Retry after backoff.")
    event = {"error_kind": "timeout", "evidence_ptr": {"kind": "chunk", "id": "s1"}}
    return catalog, candidate, event


def test_e2_default_off_returns_empty_with_valid_context():
    catalog, candidate, event = _e2_context()
    assert build_e2_candidates(event, candidate, sources=catalog) == []


def test_e2_switch_defaults_to_false():
    params = inspect.signature(build_e2_candidates).parameters
    assert params["enable_test_candidates"].default is False


def test_e2_off_ignores_invalid_context():
    assert build_e2_candidates({}, {}, sources=None) == []
    assert build_e2_candidates(None, None, sources="nope") == []


def test_e2_enabled_requires_valid_sources():
    _catalog_unused, candidate, event = _e2_context()
    assert build_e2_candidates(event, candidate, sources=None, enable_test_candidates=True) == []
    assert build_e2_candidates(event, candidate, sources={"nope": True}, enable_test_candidates=True) == []


def test_e2_enabled_requires_matching_evidence_ptr():
    catalog, candidate, _event = _e2_context()
    missing = {"error_kind": "timeout"}
    assert build_e2_candidates(missing, candidate, sources=catalog, enable_test_candidates=True) == []
    mismatch = {"error_kind": "timeout", "evidence_ptr": {"kind": "chunk", "id": "ghost"}}
    assert build_e2_candidates(mismatch, candidate, sources=catalog, enable_test_candidates=True) == []
    malformed_ptr = {"error_kind": "timeout", "evidence_ptr": {"kind": "chunk"}}
    assert build_e2_candidates(malformed_ptr, candidate, sources=catalog, enable_test_candidates=True) == []


def test_e2_enabled_returns_in_memory_candidates_with_provenance():
    catalog, candidate, event = _e2_context()
    snapshot_event = copy.deepcopy(event)
    snapshot_candidate = copy.deepcopy(candidate)
    snapshot_catalog = copy.deepcopy(catalog)
    produced = build_e2_candidates(event, candidate, sources=catalog, enable_test_candidates=True)
    assert len(produced) == 1
    only = produced[0]
    assert only["candidate_id"] == "c-e2-1"
    assert only["source_ref"] == {"fixture_id": "fx-001", "span": "0:13"}
    assert only["evidence"] == [{"kind": "chunk", "id": "s1"}]
    assert event == snapshot_event
    assert candidate == snapshot_candidate
    assert catalog == snapshot_catalog
    produced[0]["payload"]["text"] = "mutated"
    assert candidate["payload"]["text"] == "Retry after backoff."


def test_e2_enabled_reuses_parser_validation():
    catalog, _candidate, event = _e2_context()
    malformed = _lesson(cid="c-e2-bad", evidence=[_ptr(sid="ghost")])
    assert build_e2_candidates(event, malformed, sources=catalog, enable_test_candidates=True) == []
    incomplete = {"candidate_id": "c-e2-half"}
    assert build_e2_candidates(event, incomplete, sources=catalog, enable_test_candidates=True) == []


def test_e2_result_matches_parser_output():
    catalog, candidate, event = _e2_context()
    expected = parse_experience_raw([candidate], sources=catalog)
    produced = build_e2_candidates(event, candidate, sources=catalog, enable_test_candidates=True)
    assert produced == expected["candidates"]


def test_e2_ignores_environment_config():
    catalog, candidate, event = _e2_context()
    before = build_e2_candidates(event, candidate, sources=catalog, enable_test_candidates=True)
    import os as _os

    old = _os.environ.get("MNEMOSEED_LOCAL_HOME")
    _os.environ["MNEMOSEED_LOCAL_HOME"] = "C:\\nonexistent-lane-a-home"
    try:
        after = build_e2_candidates(event, candidate, sources=catalog, enable_test_candidates=True)
        parsed = parse_experience_raw([candidate], sources=catalog)
    finally:
        if old is None:
            del _os.environ["MNEMOSEED_LOCAL_HOME"]
        else:
            _os.environ["MNEMOSEED_LOCAL_HOME"] = old
    assert after == before
    assert parsed["candidates"] == before


# -- determinism, isolation, and module surface -----------------------------------


def test_parser_never_mutates_inputs_on_error_paths():
    catalog = _catalog()
    raw = [_fact(cid="c-ok"), {"candidate_id": "broken"}]
    raw_snapshot = copy.deepcopy(raw)
    catalog_snapshot = copy.deepcopy(catalog)
    result = parse_experience_raw(raw, sources=catalog)
    _assert_envelope(result)
    assert result["zero_reason"] == "malformed-input"
    assert raw == raw_snapshot
    assert catalog == catalog_snapshot


def _assert_fresh_import_clean(module_name, exercise):
    """Import the owned module in a fresh interpreter and reject forbidden imports.

    The check runs in a new process, so earlier suites in the same pytest run
    cannot pollute the result through ambient sys.modules.
    """
    script = (
        "import sys; "
        f"import {module_name} as target; "
        f"{exercise}; "
        "bad = sorted(n for n in sys.modules "
        "if n.startswith(('mnemoseed_local.daemon', 'mnemoseed_local.config')) "
        "or (n.startswith('mnemoseed_local') and 'provider' in n)); "
        "sys.exit('forbidden imports: ' + ','.join(bad) if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_module_has_no_daemon_config_provider_footprint():
    _assert_fresh_import_clean(
        "mnemoseed_local.eval.experience_contract",
        "target.parse_experience_raw([], sources={'run_id': 'r', 'sources': []})",
    )


def test_large_valid_case_has_no_budget_inference():
    payload = {"text": "Word " * 5000}
    result = parse_experience_raw([_lesson(cid="c-big", payload=payload)], sources=_catalog())
    _assert_envelope(result)
    assert len(result["candidates"]) == 1
    assert result["zero_reason"] is None
