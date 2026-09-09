"""Serial integration tests for the P-008 Batch-1 baseline runner.

Runs the frozen golden corpus end to end (real parser plus real scorer),
pins the two declared ABI boundaries (catalog-provenance split and the
conflict_reason contract), and checks determinism, isolation, zero model
or network use, and honest NOT_OBSERVED reporting.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

from mnemoseed_local.eval.experience_baseline import (
    BANNED_CLAIM_PHRASES,
    BASELINE_VERSION,
    GUARDED_ENTRY_POINTS,
    canonical_baseline,
    canonical_sha256,
    check_banned_claims,
    main,
    run_baseline,
)
from mnemoseed_local.eval.experience_contract import parse_experience_raw
from mnemoseed_local.eval.experience_metrics import score_experience

ROOT = Path(__file__).resolve().parents[1]
INPUTS_PATH = ROOT / "tests" / "fixtures" / "p008_golden" / "inputs.json"
TRUTH_PATH = ROOT / "tests" / "fixtures" / "p008_golden" / "truth.json"
EMPTY_ERRORS = {"missing": [], "malformed": [], "disposition": [], "provenance": [], "duplicate": []}

ENVELOPE_KEYS = (
    "status",
    "candidates",
    "zero_result",
    "zero_reason",
    "coercion_loss",
    "conflict",
    "conflict_reason",
    "report",
)


def _load_inputs():
    with open(INPUTS_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _load_truth():
    with open(TRUTH_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _fact_unit(unit_id="cand-b1", evidence_id="src-b1"):
    return {
        "id": unit_id,
        "class": "fact",
        "scope": "scope-a",
        "payload": {
            "subject": "Ava",
            "predicate": "prefers",
            "object": "morning reviews",
            "polarity": "positive",
        },
        "evidence": [{"kind": "chunk", "id": evidence_id}],
        "disposition": "accepted",
    }


def _fact_truth(case_id="fact-b1"):
    return {
        "case_id": case_id,
        "category": "fact",
        "source": {"fixture_id": "fx-b1", "text": "Ava prefers morning reviews.", "span": "0:28"},
        "expected_status": "accepted",
        "zero_reason": None,
        "coercion_loss": False,
        "conflict": False,
        "expected_units": [_fact_unit()],
    }


def _candidate(unit, candidate_id=None, evidence=None, source_ref=None):
    candidate = {
        "candidate_id": unit["id"] if candidate_id is None else candidate_id,
        "class": unit["class"],
        "scope": unit["scope"],
        "payload": copy.deepcopy(unit["payload"]),
        "evidence": copy.deepcopy(unit["evidence"]) if evidence is None else evidence,
        "source_ref": {"fixture_id": "fx-b1", "span": "0:28"} if source_ref is None else source_ref,
    }
    return candidate


def _result(
    status="accepted",
    candidates=None,
    zero_result=False,
    zero_reason=None,
    coercion_loss=False,
    conflict=False,
    conflict_reason=None,
):
    if candidates is None:
        candidates = []
    return {
        "status": status,
        "candidates": candidates,
        "zero_result": zero_result,
        "zero_reason": zero_reason,
        "coercion_loss": coercion_loss,
        "conflict": conflict,
        "conflict_reason": conflict_reason,
        "report": {
            "reason": zero_reason if zero_reason else status,
            "disposition": status,
            "run_id": "run-b1",
            "conflicts": [],
        },
    }


def _zero_truth(case_id="noise-b1"):
    return {
        "case_id": case_id,
        "category": "noise",
        "source": {"fixture_id": "fx-b1", "text": "Static.", "span": "0:7"},
        "expected_status": "unresolved",
        "zero_reason": "all-filtered",
        "coercion_loss": False,
        "conflict": True,
        "expected_units": [],
    }


# -- B1: catalog-provenance boundary -------------------------------------------


def test_scorer_signature_has_no_catalog_input():
    params = list(inspect.signature(score_experience).parameters)
    assert params == ["predicted", "truth"]


def test_scorer_rejects_catalog_keyword():
    truth = [_fact_truth()]
    with pytest.raises(TypeError):
        score_experience([], truth, sources={"run_id": "run-x", "sources": []})  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        score_experience([], truth, catalog={})  # type: ignore[call-arg]


def test_scorer_ignores_source_ref_content():
    truth = [_fact_truth()]
    candidate = _candidate(
        truth[0]["expected_units"][0], source_ref={"fixture_id": "fx-elsewhere", "span": "9:99"}
    )
    result = score_experience([{"case_id": "fact-b1", "result": _result(candidates=[candidate])}], truth)
    assert result["fact"]["tp"] == 1 and result["fact"]["fp"] == 0
    assert result["errors"] == EMPTY_ERRORS


def test_forged_evidence_id_never_matches_end_to_end():
    truth = [_fact_truth()]
    candidate = _candidate(truth[0]["expected_units"][0], evidence=[{"kind": "chunk", "id": "src-forged"}])
    result = score_experience([{"case_id": "fact-b1", "result": _result(candidates=[candidate])}], truth)
    assert result["fact"]["tp"] == 0
    assert result["fact"]["fp"] == 1 and result["fact"]["fn"] == 1
    assert len(result["errors"]["provenance"]) == 1
    assert result["errors"]["provenance"][0]["case_id"] == "fact-b1"


def test_frozen_forged_cases_stay_unresolved():
    report = run_baseline(INPUTS_PATH, TRUTH_PATH)
    by_case = {entry["case_id"]: entry["result"] for entry in report["predictions"]}
    for case_id in ("fact-07", "fact-07-r", "lesson-04", "lesson-04-r"):
        assert by_case[case_id]["status"] == "unresolved"
        assert by_case[case_id]["zero_result"] is True


# -- B2: conflict_reason boundary ----------------------------------------------


@pytest.mark.parametrize("reason", [None, "", []])
def test_conflict_true_requires_truthy_reason(reason):
    truth = [_zero_truth()]
    bad = _result(
        status="unresolved",
        zero_result=True,
        zero_reason="all-filtered",
        conflict=True,
        conflict_reason=reason,
    )
    result = score_experience([{"case_id": "noise-b1", "result": bad}], truth)
    assert any(e["reason"] == "empty-conflict-reason" for e in result["errors"]["malformed"])
    assert result["fact"]["tp"] == 0


def test_conflict_false_with_reason_is_malformed():
    truth = [_fact_truth()]
    candidate = _candidate(truth[0]["expected_units"][0])
    bad = _result(candidates=[candidate], conflict=False, conflict_reason="spurious reason")
    result = score_experience([{"case_id": "fact-b1", "result": bad}], truth)
    assert any(e["reason"] == "unexpected-conflict-reason" for e in result["errors"]["malformed"])
    assert result["fact"]["tp"] == 0 and result["fact"]["fn"] == 1


def test_frozen_corpus_conflict_invariant_holds_for_every_case():
    inputs = _load_inputs()
    for case in inputs["cases"]:
        result = parse_experience_raw(case["raw"], sources=case["sources"])
        assert isinstance(result["conflict"], bool)
        if result["conflict"]:
            assert isinstance(result["conflict_reason"], str) and result["conflict_reason"]
            assert result["report"]["conflicts"]
            for group in result["report"]["conflicts"]:
                for cid in group:
                    assert cid in result["conflict_reason"]
        else:
            assert result["conflict_reason"] is None
            assert result["report"]["conflicts"] == []


# -- baseline runner ------------------------------------------------------------


def test_baseline_freezes_frozen_corpus_numbers():
    report = run_baseline(INPUTS_PATH, TRUTH_PATH)
    assert report["baseline_version"] == BASELINE_VERSION
    assert report["corpus"] == {
        "inputs_version": 1,
        "truth_version": 1,
        "cases": 50,
        "base": 25,
        "robustness": 25,
    }
    assert report["score"]["fact"] == {
        "P": 1.0,
        "R": 1.0,
        "tp": 13,
        "fp": 0,
        "fn": 0,
        "P_reason": None,
        "R_reason": None,
    }
    assert report["score"]["experience"] == {
        "P": 1.0,
        "R": 1.0,
        "tp": 18,
        "fp": 0,
        "fn": 0,
        "P_reason": None,
        "R_reason": None,
    }
    assert report["score"]["zero_reasons"] == {"malformed-input": 17, "all-filtered": 2, "no-match": 6}
    assert report["score"]["coercion_loss_count"] == 3
    assert report["score"]["misclass"] == []
    assert report["score"]["extra"] == []
    assert len(report["score"]["units"]) == 35
    assert report["score"]["errors"] == EMPTY_ERRORS


def test_baseline_preserves_per_case_parse_results():
    report = run_baseline(INPUTS_PATH, TRUTH_PATH)
    assert len(report["predictions"]) == 50
    seen = set()
    for entry in report["predictions"]:
        assert set(entry) == {"case_id", "result"}
        assert entry["case_id"] not in seen
        seen.add(entry["case_id"])
        result = entry["result"]
        assert tuple(sorted(result)) == tuple(sorted(ENVELOPE_KEYS))
        assert tuple(sorted(result["report"])) == ("conflicts", "disposition", "reason", "run_id")
        assert "candidates" not in entry


def test_baseline_deterministic_byte_comparable(tmp_path):
    first_path = tmp_path / "baseline-first.json"
    second_path = tmp_path / "baseline-second.json"
    assert main([str(INPUTS_PATH), str(TRUTH_PATH), str(first_path)]) == 0
    assert main([str(INPUTS_PATH), str(TRUTH_PATH), str(second_path)]) == 0
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    assert first["seed_policy"] == second["seed_policy"]
    assert first.pop("out_path") == str(first_path)
    assert second.pop("out_path") == str(second_path)
    for payload in (first, second):
        assert isinstance(payload.pop("started_at"), str)
        assert isinstance(payload.pop("duration_s"), float)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_baseline_ops_counters_are_observed_zero():
    report = run_baseline(INPUTS_PATH, TRUTH_PATH)
    assert report["ops"]["guarded"] == list(GUARDED_ENTRY_POINTS)
    assert set(report["ops"]["counts"]) == set(GUARDED_ENTRY_POINTS)
    assert all(count == 0 for count in report["ops"]["counts"].values())


def test_baseline_isolation(tmp_path):
    home = tmp_path / "isolated-home"
    home.mkdir()
    old = os.environ.get("MNEMOSEED_LOCAL_HOME")
    os.environ["MNEMOSEED_LOCAL_HOME"] = str(home)
    try:
        report = run_baseline(INPUTS_PATH, TRUTH_PATH)
    finally:
        if old is None:
            del os.environ["MNEMOSEED_LOCAL_HOME"]
        else:
            os.environ["MNEMOSEED_LOCAL_HOME"] = old
    assert report["isolation"]["forbidden_modules"] == []
    assert report["isolation"]["home"] == str(home)
    assert report["isolation"]["home_writes"] == []
    assert sorted(report["files_read"]) == [str(INPUTS_PATH), str(TRUTH_PATH)]


def test_baseline_isolation_ignores_ambient_imports():
    import types

    injected = {
        "mnemoseed_local.daemon": types.ModuleType("mnemoseed_local.daemon"),
        "mnemoseed_local.config": types.ModuleType("mnemoseed_local.config"),
    }
    saved = {name: sys.modules.get(name) for name in injected}
    sys.modules.update(injected)
    try:
        report = run_baseline(INPUTS_PATH, TRUTH_PATH)
    finally:
        for name, module in saved.items():
            if module is None:
                del sys.modules[name]
            else:
                sys.modules[name] = module
    assert report["isolation"]["forbidden_modules"] == []


def test_baseline_not_observed_and_claims():
    report = run_baseline(INPUTS_PATH, TRUTH_PATH)
    joined = " ".join(report["NOT_OBSERVED"])
    for topic in (
        "natural-language extraction quality",
        "real model quality",
        "real-data coverage",
        "live resource impact",
    ):
        assert topic in joined
    assert report["banned_claims"]["passed"] is True
    assert report["banned_claims"]["hits"] == []
    assert report["banned_claims"]["checked"] == list(BANNED_CLAIM_PHRASES)


def test_banned_claim_checker_bites():
    verdict = check_banned_claims("this run proves real model quality is high")
    assert verdict["passed"] is False
    assert verdict["hits"]


def test_baseline_main_writes_report(tmp_path):
    out_path = tmp_path / "report.json"
    assert main([str(INPUTS_PATH), str(TRUTH_PATH), str(out_path)]) == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["baseline_version"] == BASELINE_VERSION
    assert payload["score"]["fact"]["tp"] == 13
    assert payload["out_path"] == str(out_path)


# -- truth/contract consistency regressions ------------------------------------


def _source(sid, fixture_id):
    return {
        "kind": "node",
        "id": sid,
        "fixture_id": fixture_id,
        "text": "Carol keeps notebooks.",
        "span": "0:23",
    }


def _catalog_two_sources():
    return {"run_id": "run-b1", "sources": [_source("src-a", "fx-a"), _source("src-b", "fx-b")]}


def _same_id_fact(cid, evidence_id):
    return {
        "candidate_id": cid,
        "class": "fact",
        "scope": "scope-a",
        "payload": {
            "subject": "Carol",
            "predicate": "keeps",
            "object": "notebooks",
            "polarity": "positive",
        },
        "evidence": [{"kind": "node", "id": evidence_id}],
    }


def test_conflicting_id_reuse_with_different_evidence_is_malformed():
    raw = [_same_id_fact("cand-x", "src-a"), _same_id_fact("cand-x", "src-b")]
    result = parse_experience_raw(raw, sources=_catalog_two_sources())
    assert result["status"] == "unresolved"
    assert result["zero_reason"] == "malformed-input"
    assert result["coercion_loss"] is False
    assert "cand-x" in result["report"]["reason"]


def test_unsupported_envelope_field_sets_coercion_loss():
    result = parse_experience_raw({"candidates": []}, sources=_catalog_two_sources())
    assert result["status"] == "unresolved"
    assert result["zero_reason"] == "malformed-input"
    assert result["coercion_loss"] is True


def test_frozen_conflicting_reuse_truth_matches_parser():
    inputs = {c["case_id"]: c for c in _load_inputs()["cases"]}
    truth = {c["case_id"]: c for c in _load_truth()["cases"]}
    frozen = truth["fact-03-r"]
    assert frozen["expected_status"] == "unresolved"
    assert frozen["zero_reason"] == "malformed-input"
    assert frozen["coercion_loss"] is False
    assert frozen["conflict"] is False
    assert frozen["expected_units"] == []
    parsed = parse_experience_raw(inputs["fact-03-r"]["raw"], sources=inputs["fact-03-r"]["sources"])
    assert parsed["status"] == frozen["expected_status"]
    assert parsed["zero_reason"] == frozen["zero_reason"]
    assert parsed["coercion_loss"] == frozen["coercion_loss"]
    assert parsed["conflict"] == frozen["conflict"]


def test_frozen_degenerate_envelope_truth_matches_parser():
    inputs = {c["case_id"]: c for c in _load_inputs()["cases"]}
    truth = {c["case_id"]: c for c in _load_truth()["cases"]}
    frozen = truth["noise-04-r"]
    assert frozen["expected_status"] == "unresolved"
    assert frozen["zero_reason"] == "malformed-input"
    assert frozen["coercion_loss"] is True
    assert frozen["expected_units"] == []
    parsed = parse_experience_raw(inputs["noise-04-r"]["raw"], sources=inputs["noise-04-r"]["sources"])
    assert parsed["status"] == frozen["expected_status"]
    assert parsed["zero_reason"] == frozen["zero_reason"]
    assert parsed["coercion_loss"] == frozen["coercion_loss"]


# -- QA round 3: wire-contract hardening ---------------------------------------


def _invalid_catalog():
    return {"run_id": "", "sources": []}


def _round_trip_candidate():
    return {
        "candidate_id": "cand-b1",
        "class": "fact",
        "scope": "scope-a",
        "payload": {
            "subject": "Ava",
            "predicate": "prefers",
            "object": "morning reviews",
            "polarity": "positive",
        },
        "evidence": [{"kind": "chunk", "id": "src-b1"}],
    }


def test_invalid_catalog_envelope_round_trips_through_scorer():
    envelope = parse_experience_raw([_round_trip_candidate()], sources=_invalid_catalog())
    assert envelope["status"] == "unresolved"
    assert envelope["zero_reason"] == "malformed-input"
    assert envelope["report"]["run_id"] is None
    result = score_experience([{"case_id": "fact-b1", "result": envelope}], [_fact_truth()])
    assert "invalid-report" not in [e["reason"] for e in result["errors"]["malformed"]]
    assert result["fact"]["tp"] == 0 and result["fact"]["fn"] == 1


def test_empty_input_invalid_catalog_round_trips_clean():
    envelope = parse_experience_raw(None, sources=_invalid_catalog())
    assert envelope["zero_reason"] == "no-match"
    assert envelope["report"]["run_id"] is None
    noise_truth = {
        "case_id": "noise-b1",
        "category": "noise",
        "source": {"fixture_id": "fx-b1", "text": "Static.", "span": "0:7"},
        "expected_status": "unresolved",
        "zero_reason": "no-match",
        "coercion_loss": False,
        "conflict": False,
        "expected_units": [],
    }
    result = score_experience([{"case_id": "noise-b1", "result": envelope}], [noise_truth])
    assert result["errors"] == EMPTY_ERRORS


def test_accepted_envelope_with_null_run_id_is_malformed():
    truth = [_fact_truth()]
    candidate = _candidate(truth[0]["expected_units"][0])
    good = _result(candidates=[candidate])
    good["report"]["run_id"] = None
    result = score_experience([{"case_id": "fact-b1", "result": good}], truth)
    assert any(e["reason"] == "invalid-report" for e in result["errors"]["malformed"])
    assert result["fact"]["tp"] == 0 and result["fact"]["fn"] == 1


def test_unresolved_envelope_with_nonstring_run_id_is_malformed():
    bad = _result(status="unresolved", zero_result=True, zero_reason="no-match")
    bad["report"]["run_id"] = 42
    result = score_experience([{"case_id": "fact-b1", "result": bad}], [_fact_truth()])
    assert any(e["reason"] == "invalid-report" for e in result["errors"]["malformed"])


@pytest.mark.parametrize("reason", [42, True, ["cp-x"], {"pair": "cp-x"}])
def test_conflict_true_requires_nonempty_string_reason(reason):
    truth = [_fact_truth()]
    candidate = _candidate(truth[0]["expected_units"][0])
    bad = _result(candidates=[candidate], conflict=True, conflict_reason=reason)
    result = score_experience([{"case_id": "fact-b1", "result": bad}], truth)
    assert any(e["reason"] == "invalid-conflict-reason" for e in result["errors"]["malformed"])
    assert result["fact"]["tp"] == 0 and result["fact"]["fn"] == 1


@pytest.mark.parametrize("reason", ["", []])
def test_conflict_false_rejects_non_null_reason(reason):
    truth = [_fact_truth()]
    candidate = _candidate(truth[0]["expected_units"][0])
    bad = _result(candidates=[candidate], conflict=False, conflict_reason=reason)
    result = score_experience([{"case_id": "fact-b1", "result": bad}], truth)
    assert any(e["reason"] == "unexpected-conflict-reason" for e in result["errors"]["malformed"])
    assert result["fact"]["tp"] == 0 and result["fact"]["fn"] == 1


def test_conflict_false_canonical_wire_value_is_none():
    truth = [_fact_truth()]
    candidate = _candidate(truth[0]["expected_units"][0])
    good = _result(candidates=[candidate], conflict=False, conflict_reason=None)
    result = score_experience([{"case_id": "fact-b1", "result": good}], truth)
    assert result["errors"] == EMPTY_ERRORS
    assert result["fact"]["tp"] == 1


def test_baseline_canonical_form_portable_across_home_roots(tmp_path):
    first_home = tmp_path / "home-a"
    second_home = tmp_path / "home-b"
    first_home.mkdir()
    second_home.mkdir()
    first_out = tmp_path / "first.json"
    second_out = tmp_path / "second.json"
    old = os.environ.get("MNEMOSEED_LOCAL_HOME")
    try:
        os.environ["MNEMOSEED_LOCAL_HOME"] = str(first_home)
        assert main([str(INPUTS_PATH), str(TRUTH_PATH), str(first_out)]) == 0
        os.environ["MNEMOSEED_LOCAL_HOME"] = str(second_home)
        assert main([str(INPUTS_PATH), str(TRUTH_PATH), str(second_out)]) == 0
    finally:
        if old is None:
            del os.environ["MNEMOSEED_LOCAL_HOME"]
        else:
            os.environ["MNEMOSEED_LOCAL_HOME"] = old
    first = json.loads(first_out.read_text(encoding="utf-8"))
    second = json.loads(second_out.read_text(encoding="utf-8"))
    assert first["isolation"]["home"] != second["isolation"]["home"]
    assert canonical_baseline(first) == canonical_baseline(second)
    assert canonical_sha256(first) == canonical_sha256(second)
    assert len(canonical_sha256(first)) == 64
    assert (
        canonical_sha256(first)
        == hashlib.sha256(json.dumps(canonical_baseline(first), sort_keys=True).encode("utf-8")).hexdigest()
    )
