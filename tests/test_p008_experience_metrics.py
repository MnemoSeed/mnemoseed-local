"""Lane B tests for the experience scorer and the golden corpus.

Hand frozen predictions only; the unfinished parser lane is never imported
or read. Fixture assertions read the frozen JSON literally and never derive
gold through the scorer or the parser.
"""

import copy
import json
from pathlib import Path

import pytest

from mnemoseed_local.eval.experience_metrics import score_experience

ROOT = Path(__file__).resolve().parents[1]
INPUTS_PATH = ROOT / "tests" / "fixtures" / "p008_golden" / "inputs.json"
TRUTH_PATH = ROOT / "tests" / "fixtures" / "p008_golden" / "truth.json"
METRICS_PATH = ROOT / "src" / "mnemoseed_local" / "eval" / "experience_metrics.py"

BASE_COUNTS = {"fact": 8, "lesson": 5, "intention": 4, "skill": 3, "noise": 5}
BASE_TOTAL = 25
FORGED_SOURCE_CASES = {"fact-07", "fact-07-r", "lesson-04", "lesson-04-r"}
EMPTY_ERRORS = {"missing": [], "malformed": [], "disposition": [], "provenance": [], "duplicate": []}


def _load_inputs():
    with open(INPUTS_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _load_truth():
    with open(TRUTH_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _candidate(candidate_id, cls, scope, payload, evidence, group=None, pair=None, confidence=None):
    candidate = {
        "candidate_id": candidate_id,
        "class": cls,
        "scope": scope,
        "payload": copy.deepcopy(payload),
        "evidence": copy.deepcopy(evidence),
        "source_ref": {"fixture_id": "fx-eva", "span": "0:4"},
    }
    if group is not None:
        candidate["paraphrase_group_id"] = group
    if pair is not None:
        candidate["conflict_pair_id"] = pair
    if confidence is not None:
        candidate["confidence"] = confidence
    return candidate


def _candidate_from_unit(unit, candidate_id=None):
    evidence = copy.deepcopy(unit["evidence"])
    candidate = {
        "candidate_id": unit["id"] if candidate_id is None else candidate_id,
        "class": unit["class"],
        "scope": unit["scope"],
        "payload": copy.deepcopy(unit["payload"]),
        "evidence": evidence,
        "source_ref": {"fixture_id": "fx-eva", "span": "0:4"},
    }
    for key in ("paraphrase_group_id", "conflict_pair_id", "confidence", "weight"):
        if key in unit:
            candidate[key] = unit[key]
    return candidate


def _result(
    status="accepted",
    candidates=None,
    zero_result=False,
    zero_reason=None,
    coercion_loss=False,
    conflict=False,
    conflict_reason=None,
    run_id="run-eva",
    conflicts=None,
    reason="accepted",
    disposition=None,
):
    if candidates is None:
        candidates = []
    if conflicts is None:
        conflicts = []
    if conflict and conflict_reason is None:
        conflict_reason = "cp-eva: cand-a, cand-b"
    return {
        "status": status,
        "candidates": candidates,
        "zero_result": zero_result,
        "zero_reason": zero_reason,
        "coercion_loss": coercion_loss,
        "conflict": conflict,
        "conflict_reason": conflict_reason,
        "report": {
            "reason": reason,
            "disposition": status if disposition is None else disposition,
            "run_id": run_id,
            "conflicts": conflicts,
        },
    }


def _prediction(case_id, result):
    return {"case_id": case_id, "result": result}


def _fact_payload(subject="Alice", predicate="likes", obj="tea", polarity="positive"):
    return {"subject": subject, "predicate": predicate, "object": obj, "polarity": polarity}


def _fact_unit(unit_id="cand-f1", scope="scope-a", payload=None, evidence=None):
    if payload is None:
        payload = _fact_payload()
    if evidence is None:
        evidence = [{"kind": "chunk", "id": "src-f1"}]
    return {
        "id": unit_id,
        "class": "fact",
        "scope": scope,
        "payload": payload,
        "evidence": evidence,
        "disposition": "accepted",
    }


def _fact_case(case_id="fact-eva-01", units=None):
    if units is None:
        units = [_fact_unit()]
    return {
        "case_id": case_id,
        "category": "fact",
        "source": {"fixture_id": "fx-eva-01", "text": "Alice likes tea.", "span": "0:16"},
        "expected_status": "accepted",
        "zero_reason": None,
        "coercion_loss": False,
        "conflict": False,
        "expected_units": units,
    }


def _lesson_unit(unit_id="cand-l1", scope="scope-a", text="Retry after backoff.", evidence=None, group=None):
    if evidence is None:
        evidence = [{"kind": "chunk", "id": "src-l1"}]
    unit = {
        "id": unit_id,
        "class": "lesson",
        "scope": scope,
        "payload": {"text": text},
        "evidence": evidence,
        "disposition": "accepted",
    }
    if group is not None:
        unit["paraphrase_group_id"] = group
    return unit


def _lesson_case(case_id="lesson-eva-01", units=None):
    if units is None:
        units = [_lesson_unit()]
    return {
        "case_id": case_id,
        "category": "lesson",
        "source": {"fixture_id": "fx-eva-l1", "text": "Retry after backoff.", "span": "0:21"},
        "expected_status": "accepted",
        "zero_reason": None,
        "coercion_loss": False,
        "conflict": False,
        "expected_units": units,
    }


def _intention_case(case_id="intention-eva-01", status="pending"):
    return {
        "case_id": case_id,
        "category": "intention",
        "source": {"fixture_id": "fx-eva-i1", "text": "If the job fails, retry.", "span": "0:24"},
        "expected_status": "accepted",
        "zero_reason": None,
        "coercion_loss": False,
        "conflict": False,
        "expected_units": [
            {
                "id": "cand-i1",
                "class": "intention",
                "scope": "scope-a",
                "payload": {
                    "trigger_condition": "If the job fails",
                    "action": "Retry with backoff",
                    "status": status,
                },
                "evidence": [{"kind": "chunk", "id": "src-i1"}],
                "disposition": "accepted",
            }
        ],
    }


def _skill_case(case_id="skill-eva-01", rate=0.8, chain=None):
    if chain is None:
        chain = ["snapshot", "upload"]
    return {
        "case_id": case_id,
        "category": "skill_sequence",
        "source": {"fixture_id": "fx-eva-s1", "text": "Snapshot then upload.", "span": "0:21"},
        "expected_status": "accepted",
        "zero_reason": None,
        "coercion_loss": False,
        "conflict": False,
        "expected_units": [
            {
                "id": "cand-s1",
                "class": "skill_sequence",
                "scope": "scope-a",
                "payload": {"task_type": "Backup", "tool_chain": list(chain), "success_rate": rate},
                "evidence": [{"kind": "chunk", "id": "src-s1"}],
                "disposition": "accepted",
            }
        ],
    }


def _noise_case(case_id="noise-eva-01"):
    return {
        "case_id": case_id,
        "category": "noise",
        "source": {"fixture_id": "fx-eva-n1", "text": "No usable content here.", "span": "0:23"},
        "expected_status": "unresolved",
        "zero_reason": "no-match",
        "coercion_loss": False,
        "conflict": False,
        "expected_units": [],
    }


def _zero_result(reason):
    return _result(
        status="unresolved",
        zero_result=True,
        zero_reason=reason,
        reason=reason,
        disposition="unresolved",
    )


def _raw_candidates(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return "invalid-json"
        return _raw_candidates(parsed)
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    return "scalar"


def test_golden_files_exist_and_versioned():
    inputs = _load_inputs()
    truth = _load_truth()
    assert inputs["version"] == 1
    assert truth["version"] == 1
    assert len(inputs["cases"]) == 2 * BASE_TOTAL
    assert len(truth["cases"]) == 2 * BASE_TOTAL


def test_base_minimums_and_variant_pairing():
    inputs = _load_inputs()
    by_id = {case["case_id"]: case for case in inputs["cases"]}
    for prefix, count in BASE_COUNTS.items():
        bases = [cid for cid in by_id if cid.startswith(prefix + "-") and not cid.endswith("-r")]
        assert len(bases) == count
        for base in bases:
            assert by_id[base]["variant"] == "base"
            assert by_id[base + "-r"]["variant"] == "robustness"
    assert len([c for c in inputs["cases"] if c["variant"] == "robustness"]) == BASE_TOTAL


def test_input_transport_fields():
    inputs = _load_inputs()
    for case in inputs["cases"]:
        assert isinstance(case["case_id"], str) and case["case_id"]
        assert case["variant"] in ("base", "robustness")
        assert "raw" in case
        catalog = case["sources"]
        assert isinstance(catalog["run_id"], str) and catalog["run_id"]
        assert isinstance(catalog["sources"], list)
        for entry in catalog["sources"]:
            assert entry["kind"] in ("chunk", "session", "node")
            assert isinstance(entry["id"], str) and entry["id"]
            assert isinstance(entry["fixture_id"], str) and entry["fixture_id"]
            assert isinstance(entry["text"], str)
            assert entry["span"] is None or isinstance(entry["span"], str)


def test_input_raw_shape_coverage():
    inputs = _load_inputs()
    shapes = set()
    for case in inputs["cases"]:
        raw = case["raw"]
        if raw is None:
            shapes.add("null")
        elif isinstance(raw, str) and not raw.strip():
            shapes.add("blank")
        elif raw == []:
            shapes.add("empty-list")
        elif raw == {}:
            shapes.add("empty-dict")
        elif isinstance(raw, str):
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                shapes.add("invalid-json")
            else:
                shapes.add("json-string")
        elif isinstance(raw, list):
            shapes.add("candidate-list")
        else:
            shapes.add("scalar")
    for shape in ("null", "blank", "empty-list", "empty-dict", "invalid-json", "scalar", "candidate-list"):
        assert shape in shapes
    assert "json-string" in shapes


def test_input_evidence_resolves_except_forged_cases():
    inputs = _load_inputs()
    for case in inputs["cases"]:
        if isinstance(case["raw"], dict):
            continue
        catalog = {(entry["kind"], entry["id"]) for entry in case["sources"]["sources"]}
        candidates = _raw_candidates(case["raw"])
        if candidates in (None, "invalid-json", "scalar"):
            continue
        seen = {}
        for item in candidates:
            assert isinstance(item, dict)
            for field in ("candidate_id", "class", "scope", "payload", "evidence"):
                assert field in item
            key = (item["class"], item["scope"], json.dumps(item["payload"], sort_keys=True))
            if item["candidate_id"] in seen:
                assert seen[item["candidate_id"]] == key
            else:
                seen[item["candidate_id"]] = key
            if case["case_id"] in FORGED_SOURCE_CASES:
                continue
            assert isinstance(item["evidence"], list) and item["evidence"]
            for ref in item["evidence"]:
                assert (ref["kind"], ref["id"]) in catalog


def test_truth_fields_and_consistency():
    truth = _load_truth()
    for case in truth["cases"]:
        assert case["category"] in ("fact", "lesson", "intention", "skill_sequence", "noise")
        assert case["expected_status"] in ("accepted", "unresolved")
        accepted = [u for u in case["expected_units"] if u["disposition"] == "accepted"]
        if case["expected_status"] == "accepted":
            assert case["zero_reason"] is None
            assert accepted
        else:
            assert case["zero_reason"] in ("no-match", "malformed-input", "all-filtered")
            assert accepted == []
        assert isinstance(case["coercion_loss"], bool)
        assert isinstance(case["conflict"], bool)
        for unit in case["expected_units"]:
            assert unit["class"] in ("fact", "lesson", "intention", "skill_sequence")
            assert unit["disposition"] in ("accepted", "unresolved")
            for field in ("id", "scope", "payload", "evidence"):
                assert field in unit
            assert isinstance(unit["payload"], dict)
            assert isinstance(unit["evidence"], list)


def test_ids_mirror_between_inputs_and_truth():
    inputs = _load_inputs()
    truth = _load_truth()
    assert {c["case_id"] for c in inputs["cases"]} == {c["case_id"] for c in truth["cases"]}


def test_conflict_pair_shape():
    truth = _load_truth()
    by_id = {c["case_id"]: c for c in truth["cases"]}
    for cid in ("lesson-02", "lesson-02-r"):
        case = by_id[cid]
        assert case["conflict"] is True
        assert case["expected_status"] == "unresolved"
        assert case["zero_reason"] == "all-filtered"
        assert len(case["expected_units"]) == 2
        pairs = {u.get("conflict_pair_id") for u in case["expected_units"]}
        assert len(pairs) == 1 and None not in pairs
        assert all(u["disposition"] == "unresolved" for u in case["expected_units"])


def test_coercion_case_shape():
    truth = _load_truth()
    by_id = {c["case_id"]: c for c in truth["cases"]}
    for cid in ("lesson-03", "lesson-03-r"):
        case = by_id[cid]
        assert case["coercion_loss"] is True
        assert case["expected_status"] == "unresolved"
        assert case["zero_reason"] == "malformed-input"


def test_forged_source_stays_unresolved_despite_confidence():
    inputs = _load_inputs()
    truth = _load_truth()
    input_by_id = {c["case_id"]: c for c in inputs["cases"]}
    truth_by_id = {c["case_id"]: c for c in truth["cases"]}
    for cid in ("lesson-04", "lesson-04-r"):
        raws = _raw_candidates(input_by_id[cid]["raw"])
        assert any(isinstance(r, dict) and r.get("confidence", 0) >= 0.7 for r in raws)
        assert truth_by_id[cid]["expected_status"] == "unresolved"
        assert truth_by_id[cid]["zero_reason"] == "malformed-input"
        assert truth_by_id[cid]["coercion_loss"] is False


def test_placeholder_honesty():
    inputs = _load_inputs()
    truth = _load_truth()
    input_by_id = {c["case_id"]: c for c in inputs["cases"]}
    truth_by_id = {c["case_id"]: c for c in truth["cases"]}
    assert "note" in input_by_id["lesson-05"]
    assert truth_by_id["lesson-05"]["near_window_placeholder"] is True
    note = truth_by_id["lesson-05"]["note"]
    assert "placeholder" in note and "not a real" in note
    assert truth_by_id["lesson-05"]["expected_status"] == "accepted"


def test_wrong_type_inputs_are_literal():
    inputs = _load_inputs()
    by_id = {c["case_id"]: c for c in inputs["cases"]}
    fact08 = _raw_candidates(by_id["fact-08"]["raw"])[0]
    assert not isinstance(fact08["payload"]["subject"], str)
    intention03 = _raw_candidates(by_id["intention-03"]["raw"])[0]
    assert intention03["payload"]["status"] not in ("pending", "fired", "cancelled")
    skill03 = _raw_candidates(by_id["skill-03"]["raw"])[0]
    rate = skill03["payload"]["success_rate"]
    assert isinstance(rate, bool) or not isinstance(rate, (int, float))


def test_perfect_small_run():
    truth = [_fact_case(), _lesson_case(), _noise_case()]
    predicted = [
        _prediction("fact-eva-01", _result(candidates=[_candidate_from_unit(truth[0]["expected_units"][0])])),
        _prediction(
            "lesson-eva-01", _result(candidates=[_candidate_from_unit(truth[1]["expected_units"][0])])
        ),
        _prediction("noise-eva-01", _zero_result("no-match")),
    ]
    result = score_experience(predicted, truth)
    assert result["fact"] == {
        "P": 1.0,
        "R": 1.0,
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "P_reason": None,
        "R_reason": None,
    }
    assert result["experience"]["tp"] == 1
    assert result["experience"]["P"] == 1.0 and result["experience"]["R"] == 1.0
    assert result["misclass"] == []
    assert result["extra"] == []
    assert [u["outcome"] for u in result["units"]] == ["matched", "matched"]
    assert result["zero_reasons"] == {"no-match": 1}
    assert result["coercion_loss_count"] == 0
    assert result["errors"] == EMPTY_ERRORS
    assert len(result["NOT_OBSERVED"]) == 4


def test_cross_class_mismatch_is_misclass_without_credit():
    truth = [_fact_case()]
    rogue = _candidate(
        "cand-rogue", "lesson", "scope-a", {"text": "Alice likes tea."}, [{"kind": "chunk", "id": "src-f1"}]
    )
    result = score_experience([_prediction("fact-eva-01", _result(candidates=[rogue]))], truth)
    assert len(result["misclass"]) == 1
    entry = result["misclass"][0]
    assert entry["case_id"] == "fact-eva-01"
    assert entry["predicted_class"] == "lesson"
    assert entry["expected_class"] == "fact"
    assert result["fact"]["tp"] == 0 and result["fact"]["fp"] == 0 and result["fact"]["fn"] == 0
    assert result["fact"]["P"] is None and result["fact"]["R"] is None
    assert result["experience"]["tp"] == 0 and result["experience"]["fp"] == 0
    assert result["extra"] == []
    assert result["units"][0]["outcome"] == "misclass"


def test_scope_fence_blocks_match():
    truth = [_fact_case()]
    candidate = _candidate_from_unit(truth[0]["expected_units"][0])
    candidate["scope"] = "scope-b"
    result = score_experience([_prediction("fact-eva-01", _result(candidates=[candidate]))], truth)
    assert result["fact"]["tp"] == 0
    assert result["fact"]["fn"] == 1
    assert result["fact"]["fp"] == 1
    assert result["misclass"] == []
    assert result["errors"] == EMPTY_ERRORS


def test_wrong_provenance_cannot_match():
    truth = [_fact_case()]
    candidate = _candidate_from_unit(truth[0]["expected_units"][0])
    candidate["evidence"] = [{"kind": "chunk", "id": "src-forged"}]
    result = score_experience([_prediction("fact-eva-01", _result(candidates=[candidate]))], truth)
    assert result["fact"]["tp"] == 0
    assert result["fact"]["fn"] == 1 and result["fact"]["fp"] == 1
    assert len(result["errors"]["provenance"]) == 1
    assert result["errors"]["provenance"][0]["case_id"] == "fact-eva-01"


def test_high_confidence_cannot_mask_forged_source():
    truth = [_fact_case()]
    candidate = _candidate_from_unit(truth[0]["expected_units"][0])
    candidate["evidence"] = [{"kind": "chunk", "id": "src-forged"}]
    candidate["confidence"] = 0.99
    candidate["weight"] = 5.0
    result = score_experience([_prediction("fact-eva-01", _result(candidates=[candidate]))], truth)
    assert result["fact"]["tp"] == 0
    assert len(result["errors"]["provenance"]) == 1


def test_alias_predicates_do_not_match():
    truth = [_fact_case()]
    candidate = _candidate_from_unit(truth[0]["expected_units"][0])
    candidate["payload"]["predicate"] = "loves"
    result = score_experience([_prediction("fact-eva-01", _result(candidates=[candidate]))], truth)
    assert result["fact"]["tp"] == 0
    assert result["fact"]["fn"] == 1 and result["fact"]["fp"] == 1


def test_fact_polarity_mismatch():
    truth = [_fact_case()]
    candidate = _candidate_from_unit(truth[0]["expected_units"][0])
    candidate["payload"]["polarity"] = "negative"
    result = score_experience([_prediction("fact-eva-01", _result(candidates=[candidate]))], truth)
    assert result["fact"]["tp"] == 0 and result["fact"]["fn"] == 1


def test_intention_status_mismatch():
    truth = [_intention_case()]
    candidate = _candidate_from_unit(truth[0]["expected_units"][0])
    candidate["payload"]["status"] = "fired"
    result = score_experience([_prediction("intention-eva-01", _result(candidates=[candidate]))], truth)
    assert result["by_class"]["intention"]["tp"] == 0
    assert result["by_class"]["intention"]["fn"] == 1
    assert result["experience"]["tp"] == 0 and result["experience"]["fn"] == 1


def test_skill_rate_mismatch():
    truth = [_skill_case()]
    candidate = _candidate_from_unit(truth[0]["expected_units"][0])
    candidate["payload"]["success_rate"] = 0.2
    result = score_experience([_prediction("skill-eva-01", _result(candidates=[candidate]))], truth)
    assert result["by_class"]["skill_sequence"]["tp"] == 0
    assert result["by_class"]["skill_sequence"]["fn"] == 1


def test_skill_chain_order_mismatch():
    truth = [_skill_case()]
    candidate = _candidate_from_unit(truth[0]["expected_units"][0])
    candidate["payload"]["tool_chain"] = ["upload", "snapshot"]
    result = score_experience([_prediction("skill-eva-01", _result(candidates=[candidate]))], truth)
    assert result["by_class"]["skill_sequence"]["tp"] == 0
    assert result["by_class"]["skill_sequence"]["fn"] == 1


def test_group_equivalence_matches_with_union_evidence():
    union = [{"kind": "chunk", "id": "src-g1"}, {"kind": "chunk", "id": "src-g2"}]
    unit = _lesson_unit(unit_id="cand-g1", text="Retry after backoff.", evidence=union, group="pg-eva")
    truth = [_lesson_case(case_id="lesson-eva-g", units=[unit])]
    candidate = _candidate(
        "cand-g2", "lesson", "scope-a", {"text": "Retry following backoff."}, union, group="pg-eva"
    )
    result = score_experience([_prediction("lesson-eva-g", _result(candidates=[candidate]))], truth)
    assert result["by_class"]["lesson"]["tp"] == 1
    assert result["errors"] == EMPTY_ERRORS


def test_group_scope_fence():
    union = [{"kind": "chunk", "id": "src-g1"}]
    unit = _lesson_unit(unit_id="cand-g1", evidence=union, group="pg-eva")
    truth = [_lesson_case(case_id="lesson-eva-g", units=[unit])]
    candidate = _candidate(
        "cand-g2", "lesson", "scope-b", {"text": "Retry following backoff."}, union, group="pg-eva"
    )
    result = score_experience([_prediction("lesson-eva-g", _result(candidates=[candidate]))], truth)
    assert result["by_class"]["lesson"]["tp"] == 0
    assert result["by_class"]["lesson"]["fn"] == 1
    assert result["misclass"] == []


def test_partial_evidence_union_fails():
    union = [{"kind": "chunk", "id": "src-g1"}, {"kind": "chunk", "id": "src-g2"}]
    unit = _lesson_unit(unit_id="cand-g1", evidence=union, group="pg-eva")
    truth = [_lesson_case(case_id="lesson-eva-g", units=[unit])]
    candidate = _candidate("cand-g1", "lesson", "scope-a", {"text": "Retry after backoff."}, union[:1])
    result = score_experience([_prediction("lesson-eva-g", _result(candidates=[candidate]))], truth)
    assert result["by_class"]["lesson"]["tp"] == 0
    assert len(result["errors"]["provenance"]) == 1


def test_split_duplicate_is_extra_and_error():
    truth = [_fact_case()]
    first = _candidate_from_unit(truth[0]["expected_units"][0])
    second = _candidate_from_unit(truth[0]["expected_units"][0], candidate_id="cand-f1-dup")
    result = score_experience([_prediction("fact-eva-01", _result(candidates=[first, second]))], truth)
    assert result["fact"]["tp"] == 1 and result["fact"]["fp"] == 1
    assert len(result["errors"]["duplicate"]) >= 1
    assert any(item["reason"] == "split-duplicate" for item in result["extra"])


def test_repeated_candidate_id_is_duplicate_error():
    truth = [_fact_case()]
    first = _candidate_from_unit(truth[0]["expected_units"][0])
    second = _candidate_from_unit(truth[0]["expected_units"][0])
    result = score_experience([_prediction("fact-eva-01", _result(candidates=[first, second]))], truth)
    assert result["fact"]["tp"] == 1 and result["fact"]["fp"] == 1
    assert len(result["errors"]["duplicate"]) >= 1
    assert any(item["reason"] == "duplicate-id" for item in result["extra"])


def test_missing_predicted_case():
    result = score_experience([], [_fact_case()])
    assert len(result["errors"]["missing"]) == 1
    assert result["errors"]["missing"][0]["case_id"] == "fact-eva-01"
    assert result["fact"]["fn"] == 1 and result["fact"]["tp"] == 0
    assert result["fact"]["P"] is None and isinstance(result["fact"]["P_reason"], str)
    assert result["fact"]["R"] == 0.0


def test_unexpected_predicted_case():
    truth = [_fact_case()]
    rogue = _result(candidates=[_candidate_from_unit(truth[0]["expected_units"][0])])
    result = score_experience([_prediction("nope-01", rogue)], truth)
    assert any(e["case_id"] == "nope-01" for e in result["errors"]["malformed"])
    assert len(result["errors"]["missing"]) == 1
    assert result["fact"]["fn"] == 1 and result["fact"]["fp"] == 0


def test_duplicate_predicted_case_id():
    truth = [_fact_case()]
    good = _prediction(
        "fact-eva-01", _result(candidates=[_candidate_from_unit(truth[0]["expected_units"][0])])
    )
    result = score_experience([good, good], truth)
    assert len(result["errors"]["duplicate"]) == 1
    assert result["fact"]["tp"] == 1 and result["fact"]["fp"] == 0


def test_malformed_envelope_missing_keys():
    result = score_experience([{"case_id": "fact-eva-01", "result": {"status": "accepted"}}], [_fact_case()])
    assert len(result["errors"]["malformed"]) >= 1
    assert result["fact"]["tp"] == 0 and result["fact"]["fn"] == 1


def test_zero_without_reason_is_malformed():
    truth = [_noise_case()]
    bad = _result(
        status="unresolved", zero_result=True, zero_reason=None, reason="", disposition="unresolved"
    )
    result = score_experience([_prediction("noise-eva-01", bad)], truth)
    assert len(result["errors"]["malformed"]) == 1
    assert result["zero_reasons"] == {}


def test_reason_on_nonzero_is_malformed():
    truth = [_fact_case()]
    bad = _result(candidates=[_candidate_from_unit(truth[0]["expected_units"][0])], zero_reason="no-match")
    result = score_experience([_prediction("fact-eva-01", bad)], truth)
    assert len(result["errors"]["malformed"]) == 1
    assert result["fact"]["tp"] == 0


def test_disposition_status_mismatch():
    truth = [_fact_case()]
    result = score_experience([_prediction("fact-eva-01", _zero_result("malformed-input"))], truth)
    assert len(result["errors"]["disposition"]) == 2
    reasons = [e["reason"] for e in result["errors"]["disposition"]]
    assert any(r.startswith("status-mismatch") for r in reasons)
    assert "zero-reason-mismatch" in reasons
    assert result["fact"]["fn"] == 1 and result["fact"]["fp"] == 0


def test_coercion_flag_mismatch_visible_despite_perfect_pr():
    truth = [_fact_case()]
    good = _result(candidates=[_candidate_from_unit(truth[0]["expected_units"][0])], coercion_loss=True)
    result = score_experience([_prediction("fact-eva-01", good)], truth)
    assert result["fact"]["P"] == 1.0 and result["fact"]["R"] == 1.0
    assert len(result["errors"]["disposition"]) == 1
    assert result["coercion_loss_count"] == 1


def test_conflict_flag_mismatch():
    truth = [_fact_case()]
    good = _result(candidates=[_candidate_from_unit(truth[0]["expected_units"][0])], conflict=True)
    result = score_experience([_prediction("fact-eva-01", good)], truth)
    assert result["fact"]["tp"] == 1
    assert len(result["errors"]["disposition"]) == 1


def test_conflict_case_clean_when_matched():
    units = [
        dict(_lesson_unit(unit_id="cand-c1", text="Run migration first."), conflict_pair_id="cp-x"),
        dict(_lesson_unit(unit_id="cand-c2", text="Never run migration."), conflict_pair_id="cp-x"),
    ]
    for unit in units:
        unit["disposition"] = "unresolved"
    truth = [
        {
            "case_id": "lesson-eva-c",
            "category": "lesson",
            "source": {"fixture_id": "fx-eva-c", "text": "Migration order.", "span": "0:16"},
            "expected_status": "unresolved",
            "zero_reason": "all-filtered",
            "coercion_loss": False,
            "conflict": True,
            "expected_units": units,
        }
    ]
    good = _result(
        status="unresolved",
        zero_result=True,
        zero_reason="all-filtered",
        conflict=True,
        conflict_reason="cp-x: cand-c1, cand-c2",
        conflicts=[["cand-c1", "cand-c2"]],
        reason="all-filtered",
        disposition="unresolved",
    )
    result = score_experience([_prediction("lesson-eva-c", good)], truth)
    assert result["errors"] == EMPTY_ERRORS
    assert result["zero_reasons"] == {"all-filtered": 1}
    assert [u["outcome"] for u in result["units"]] == ["expected-unresolved", "expected-unresolved"]
    assert result["by_class"]["lesson"]["P"] is None and result["by_class"]["lesson"]["R"] is None


def test_denom_zero_yields_none_with_reason():
    truth = [_noise_case()]
    result = score_experience([_prediction("noise-eva-01", _zero_result("no-match"))], truth)
    for key in ("fact", "experience"):
        assert result[key]["P"] is None and result[key]["R"] is None
        assert isinstance(result[key]["P_reason"], str) and isinstance(result[key]["R_reason"], str)
    assert result["errors"] == EMPTY_ERRORS


def test_empty_batch():
    result = score_experience([], [])
    assert result["fact"]["P"] is None and result["fact"]["R"] is None
    assert result["experience"]["P"] is None and result["experience"]["R"] is None
    assert result["errors"] == EMPTY_ERRORS
    assert result["zero_reasons"] == {}
    assert result["coercion_loss_count"] == 0
    assert result["misclass"] == [] and result["units"] == [] and result["extra"] == []
    assert len(result["NOT_OBSERVED"]) == 4


def test_reproducible_and_pure():
    truth = [_fact_case(), _lesson_case(), _noise_case()]
    predicted = [
        _prediction("fact-eva-01", _result(candidates=[_candidate_from_unit(truth[0]["expected_units"][0])])),
        _prediction(
            "lesson-eva-01", _result(candidates=[_candidate_from_unit(truth[1]["expected_units"][0])])
        ),
        _prediction("noise-eva-01", _zero_result("no-match")),
    ]
    snapshot_predicted = copy.deepcopy(predicted)
    snapshot_truth = copy.deepcopy(truth)
    first = score_experience(predicted, truth)
    second = score_experience(predicted, truth)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert predicted == snapshot_predicted
    assert truth == snapshot_truth


def test_truth_malformed_raises():
    with pytest.raises(ValueError):
        score_experience([], [{"case_id": 7}])
    with pytest.raises(ValueError):
        score_experience([], [_fact_case(), _fact_case()])


def test_not_observed_topics():
    result = score_experience([], [])
    joined = " ".join(result["NOT_OBSERVED"])
    for topic in (
        "natural-language extraction quality",
        "real model quality",
        "real-data coverage",
        "live resource impact",
    ):
        assert topic in joined


def test_no_parser_or_canary_references():
    text = METRICS_PATH.read_text(encoding="utf-8")
    assert "canary" not in text
    assert "experience_contract" not in text
    assert "matches_fact" not in text


def _perfect_predictions(inputs, truth):
    catalogs = {}
    for case in inputs["cases"]:
        refs = {}
        for entry in case["sources"]["sources"]:
            refs[(entry["kind"], entry["id"])] = entry
        catalogs[case["case_id"]] = (case["sources"]["run_id"], refs)
    predictions = []
    for case in truth["cases"]:
        run_id, refs = catalogs[case["case_id"]]
        accepted = [u for u in case["expected_units"] if u["disposition"] == "accepted"]
        candidates = []
        for unit in accepted:
            first = refs[(unit["evidence"][0]["kind"], unit["evidence"][0]["id"])]
            candidate = {
                "candidate_id": unit["id"],
                "class": unit["class"],
                "scope": unit["scope"],
                "payload": copy.deepcopy(unit["payload"]),
                "evidence": copy.deepcopy(unit["evidence"]),
                "source_ref": {"fixture_id": first["fixture_id"], "span": first["span"]},
            }
            for key in ("paraphrase_group_id", "conflict_pair_id"):
                if key in unit:
                    candidate[key] = unit[key]
            candidates.append(candidate)
        if case["expected_status"] == "accepted":
            result = _result(candidates=candidates, run_id=run_id)
        else:
            pairs = {}
            for unit in case["expected_units"]:
                pair = unit.get("conflict_pair_id")
                if pair:
                    pairs.setdefault(pair, []).append(unit["id"])
            conflicts = [sorted(ids) for ids in pairs.values()]
            reason_text = None
            if case["conflict"] and conflicts:
                items = sorted(pairs.items())
                reason_text = "; ".join(pair + ": " + ", ".join(ids) for pair, ids in items)
            result = _result(
                status="unresolved",
                zero_result=True,
                zero_reason=case["zero_reason"],
                coercion_loss=case["coercion_loss"],
                conflict=case["conflict"],
                conflict_reason=reason_text,
                run_id=run_id,
                conflicts=conflicts,
                reason=case["zero_reason"],
                disposition="unresolved",
            )
        predictions.append({"case_id": case["case_id"], "result": result})
    return predictions


def test_full_corpus_perfect_parser_simulation():
    inputs = _load_inputs()
    truth = _load_truth()
    result = score_experience(_perfect_predictions(inputs, truth), truth["cases"])
    assert result["fact"] == {
        "P": 1.0,
        "R": 1.0,
        "tp": 13,
        "fp": 0,
        "fn": 0,
        "P_reason": None,
        "R_reason": None,
    }
    assert result["experience"] == {
        "P": 1.0,
        "R": 1.0,
        "tp": 18,
        "fp": 0,
        "fn": 0,
        "P_reason": None,
        "R_reason": None,
    }
    assert result["by_class"]["lesson"]["tp"] == 4
    assert result["by_class"]["lesson"]["P"] == 1.0 and result["by_class"]["lesson"]["R"] == 1.0
    assert result["by_class"]["intention"]["tp"] == 8
    assert result["by_class"]["skill_sequence"]["tp"] == 6
    assert result["misclass"] == []
    assert result["extra"] == []
    assert len([u for u in result["units"] if u["outcome"] == "matched"]) == 31
    assert len([u for u in result["units"] if u["outcome"] == "expected-unresolved"]) == 4
    assert result["zero_reasons"] == {"no-match": 6, "malformed-input": 17, "all-filtered": 2}
    assert result["coercion_loss_count"] == 3
    assert result["errors"] == EMPTY_ERRORS
