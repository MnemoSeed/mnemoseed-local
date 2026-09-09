"""Deterministic scorer for frozen experience evaluation cases.

Compares parser style results against independently frozen truth without any
model, provider, daemon, or network use. Matching is exact through the shared
payload keys, fenced by class and scope, with declared fixture groups as the
only equivalence beyond normalized equality. Provenance is by explicit
evidence id only; confidence and weight are carried but never scored.
"""

from __future__ import annotations

from typing import Any

from mnemoseed_local.eval.experience_common import payload_key

STATUS_ACCEPTED = "accepted"
STATUS_UNRESOLVED = "unresolved"
CLASSES = ("fact", "lesson", "intention", "skill_sequence")
EXPERIENCE_CLASSES = ("lesson", "intention", "skill_sequence")
ZERO_REASONS = ("no-match", "malformed-input", "over-budget-overflow", "all-filtered")
RESULT_KEYS = (
    "status",
    "candidates",
    "zero_result",
    "zero_reason",
    "coercion_loss",
    "conflict",
    "conflict_reason",
    "report",
)
REPORT_KEYS = ("reason", "disposition", "run_id", "conflicts")
TRUTH_KEYS = ("case_id", "expected_status", "zero_reason", "coercion_loss", "conflict", "expected_units")
UNIT_KEYS = ("id", "class", "scope", "payload", "evidence", "disposition")
NO_PREDICTED = "no predicted positives"
NO_EXPECTED = "no expected positives"
NOT_OBSERVED = (
    "natural-language extraction quality: NOT_OBSERVED (synthetic-only baseline; no model calls)",
    "real model quality: NOT_OBSERVED (synthetic-only baseline; no model calls)",
    "real-data coverage and eligibility: NOT_OBSERVED (synthetic fixtures only)",
    "live resource impact: NOT_OBSERVED (no live reads or writes; isolated test profile)",
)


def score_experience(predicted: list[dict[str, Any]], truth: list[dict[str, Any]]) -> dict[str, Any]:
    """Score parser style predictions against frozen truth.

    Predicted entries are {case_id, result} mappings where result carries the
    frozen ParseResult envelope. Truth entries are frozen truth cases. Inputs
    are never mutated. Contract violations are reported in the errors buckets
    and never hidden by precision and recall.
    """
    if not isinstance(predicted, list) or not isinstance(truth, list):
        raise ValueError("score_experience expects lists for predicted and truth")
    truth_by_id = _index_truth(truth)
    tallies: dict[str, dict[str, int]] = {cls: {"tp": 0, "fp": 0, "fn": 0} for cls in CLASSES}
    misclass: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    errors: dict[str, list[dict[str, Any]]] = {
        "missing": [],
        "malformed": [],
        "disposition": [],
        "provenance": [],
        "duplicate": [],
    }
    zero_reasons: dict[str, int] = {}
    coercion_loss_count = 0
    seen: set[str] = set()
    for entry in predicted:
        if not isinstance(entry, dict):
            errors["malformed"].append({"case_id": "unknown", "reason": "malformed-prediction-entry"})
            continue
        case_id = entry.get("case_id")
        if not isinstance(case_id, str):
            errors["malformed"].append({"case_id": "unknown", "reason": "malformed-prediction-entry"})
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            errors["malformed"].append({"case_id": case_id, "reason": "malformed-prediction-entry"})
            continue
        if case_id in seen:
            errors["duplicate"].append({"case_id": case_id, "reason": "duplicate-predicted-case-id"})
            continue
        seen.add(case_id)
        if case_id not in truth_by_id:
            errors["malformed"].append({"case_id": case_id, "reason": "unexpected-case-id"})
            continue
        problems, view = _check_envelope(result)
        if problems or view is None:
            for problem in problems:
                errors["malformed"].append({"case_id": case_id, "reason": problem})
            _record_missed(truth_by_id[case_id], case_id, units, tallies)
            continue
        if view["zero_result"]:
            reason: str = view["zero_reason"]
            zero_reasons[reason] = zero_reasons.get(reason, 0) + 1
        if view["coercion_loss"]:
            coercion_loss_count += 1
        _score_case(case_id, view, truth_by_id[case_id], tallies, misclass, units, extra, errors)
    for case_id, case in truth_by_id.items():
        if case_id not in seen:
            errors["missing"].append({"case_id": case_id, "reason": "missing-predicted-case"})
            _record_missed(case, case_id, units, tallies)
    return {
        "fact": _summary(tallies["fact"]),
        "experience": _summary(_combine(tallies)),
        "by_class": {cls: _summary(tallies[cls]) for cls in CLASSES},
        "misclass": misclass,
        "units": units,
        "extra": extra,
        "zero_reasons": zero_reasons,
        "coercion_loss_count": coercion_loss_count,
        "errors": errors,
        "NOT_OBSERVED": list(NOT_OBSERVED),
    }


def _index_truth(truth: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index frozen truth by case id, rejecting malformed truth loudly."""
    indexed: dict[str, dict[str, Any]] = {}
    for case in truth:
        if not isinstance(case, dict):
            raise ValueError("truth entries must be dicts")
        for key in TRUTH_KEYS:
            if key not in case:
                raise ValueError("truth case misses required key " + key)
        case_id = case["case_id"]
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("truth case needs a nonempty string case_id")
        if case_id in indexed:
            raise ValueError("duplicate truth case_id " + case_id)
        status = case["expected_status"]
        reason = case["zero_reason"]
        if status not in (STATUS_ACCEPTED, STATUS_UNRESOLVED):
            raise ValueError("truth case has invalid expected_status for " + case_id)
        if reason is not None and reason not in ZERO_REASONS:
            raise ValueError("truth case has invalid zero_reason for " + case_id)
        if not isinstance(case["coercion_loss"], bool) or not isinstance(case["conflict"], bool):
            raise ValueError("truth case flags must be bool for " + case_id)
        units = case["expected_units"]
        if not isinstance(units, list):
            raise ValueError("truth case needs a units list for " + case_id)
        for unit in units:
            _check_truth_unit(unit, case_id)
        indexed[case_id] = case
    return indexed


def _check_truth_unit(unit: Any, case_id: str) -> None:
    """Validate one frozen truth unit, rejecting malformed truth loudly."""
    if not isinstance(unit, dict):
        raise ValueError("truth units must be dicts for " + case_id)
    for key in UNIT_KEYS:
        if key not in unit:
            raise ValueError("truth unit misses required key " + key + " for " + case_id)
    cls = unit["class"]
    if cls not in CLASSES:
        raise ValueError("truth unit has unknown class for " + case_id)
    if unit["disposition"] not in (STATUS_ACCEPTED, STATUS_UNRESOLVED):
        raise ValueError("truth unit has invalid disposition for " + case_id)
    if not isinstance(unit["payload"], dict) or not isinstance(unit["evidence"], list):
        raise ValueError("truth unit has invalid payload or evidence for " + case_id)


def _check_envelope(result: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
    """Validate a ParseResult envelope without ever silencing a defect."""
    problems: list[str] = []
    for key in RESULT_KEYS:
        if key not in result:
            problems.append("missing-result-key: " + key)
    if problems:
        return problems, None
    status = result["status"]
    candidates = result["candidates"]
    zero_result = result["zero_result"]
    zero_reason = result["zero_reason"]
    coercion_loss = result["coercion_loss"]
    conflict = result["conflict"]
    conflict_reason = result["conflict_reason"]
    report = result["report"]
    if status not in (STATUS_ACCEPTED, STATUS_UNRESOLVED):
        problems.append("invalid-status")
    if not isinstance(candidates, list):
        problems.append("invalid-candidates")
    if not isinstance(zero_result, bool):
        problems.append("invalid-zero-result")
    if zero_reason is not None and zero_reason not in ZERO_REASONS:
        problems.append("invalid-zero-reason")
    if not isinstance(coercion_loss, bool):
        problems.append("invalid-coercion-loss")
    if not isinstance(conflict, bool):
        problems.append("invalid-conflict")
    if conflict is True:
        if not conflict_reason:
            problems.append("empty-conflict-reason")
        elif not isinstance(conflict_reason, str):
            problems.append("invalid-conflict-reason")
    elif conflict is False and conflict_reason is not None:
        problems.append("unexpected-conflict-reason")
    problems.extend(_check_report(report, status, zero_result))
    if problems:
        return problems, None
    if status == STATUS_ACCEPTED:
        if not candidates:
            problems.append("accepted-without-candidates")
        if zero_result is not False:
            problems.append("accepted-zero-flag")
        if zero_reason is not None:
            problems.append("accepted-with-reason")
    else:
        if candidates:
            problems.append("unresolved-with-candidates")
        if zero_result is not True:
            problems.append("unresolved-without-zero-flag")
        if zero_reason not in ZERO_REASONS:
            problems.append("unresolved-without-reason")
    if problems:
        return problems, None
    view: dict[str, Any] = {
        "status": status,
        "candidates": candidates,
        "zero_result": zero_result,
        "zero_reason": zero_reason,
        "coercion_loss": coercion_loss,
        "conflict": conflict,
        "conflict_reason": conflict_reason,
        "report": report,
    }
    return [], view


def _check_report(report: Any, status: str, zero_result: bool) -> list[str]:
    """Validate the report block of a ParseResult envelope.

    The run id is a string except on zero-result envelopes from an invalid
    catalog, where the parser honestly reports null; accepted envelopes
    always carry a string run id.
    """
    if not isinstance(report, dict):
        return ["invalid-report"]
    for key in REPORT_KEYS:
        if key not in report:
            return ["invalid-report"]
    if not isinstance(report["reason"], str):
        return ["invalid-report"]
    if not isinstance(report["disposition"], str):
        return ["invalid-report"]
    run_id = report["run_id"]
    if status == STATUS_ACCEPTED or not zero_result:
        if not isinstance(run_id, str):
            return ["invalid-report"]
    elif run_id is not None and not isinstance(run_id, str):
        return ["invalid-report"]
    if not isinstance(report["conflicts"], list):
        return ["invalid-report"]
    return []


def _check_candidate(candidate: Any) -> tuple[list[str], dict[str, Any] | None]:
    """Validate one accepted candidate without ever silencing a defect."""
    if not isinstance(candidate, dict):
        return ["malformed-candidate"], None
    for key in ("candidate_id", "class", "scope", "payload", "evidence"):
        if key not in candidate:
            return ["missing-candidate-key: " + key], None
    cls = candidate["class"]
    if cls not in CLASSES:
        return ["unknown-candidate-class"], None
    if not isinstance(candidate["candidate_id"], str) or not isinstance(candidate["scope"], str):
        return ["malformed-candidate"], None
    if not isinstance(candidate["payload"], dict):
        return ["malformed-candidate"], None
    if _evidence_set(candidate["evidence"]) is None:
        return ["invalid-candidate-evidence"], None
    source_ref = candidate.get("source_ref")
    if not isinstance(source_ref, dict):
        return ["invalid-candidate-source-ref"], None
    if not isinstance(source_ref.get("fixture_id"), str):
        return ["invalid-candidate-source-ref"], None
    span = source_ref.get("span")
    if span is not None and not isinstance(span, str):
        return ["invalid-candidate-source-ref"], None
    view: dict[str, Any] = {
        "candidate_id": candidate["candidate_id"],
        "class": cls,
        "scope": candidate["scope"],
        "payload": candidate["payload"],
        "evidence": candidate["evidence"],
        "group": candidate.get("paraphrase_group_id"),
        "pair": candidate.get("conflict_pair_id"),
    }
    return [], view


def _evidence_set(evidence: Any) -> set[tuple[str, str]] | None:
    """Return explicit evidence ids as a set, or None when malformed."""
    if not isinstance(evidence, list) or not evidence:
        return None
    refs: set[tuple[str, str]] = set()
    for ref in evidence:
        if not isinstance(ref, dict):
            return None
        kind = ref.get("kind")
        rid = ref.get("id")
        if not isinstance(kind, str) or not isinstance(rid, str):
            return None
        refs.add((kind, rid))
    return refs


def _payload_equiv(cls: str, first: Any, first_group: Any, second: Any, second_group: Any) -> bool:
    """Decide payload equivalence through shared keys or declared groups."""
    if (
        isinstance(first_group, str)
        and first_group
        and isinstance(second_group, str)
        and first_group == second_group
    ):
        return True
    try:
        return bool(payload_key(cls, first) == payload_key(cls, second))
    except ValueError:
        return False


def _units_match(predicted: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Match one predicted candidate to one expected unit, fenced and provenanced."""
    if predicted["class"] != expected["class"] or predicted["scope"] != expected["scope"]:
        return False
    if not _payload_equiv(
        str(expected["class"]),
        predicted["payload"],
        predicted["group"],
        expected["payload"],
        expected.get("paraphrase_group_id"),
    ):
        return False
    have = _evidence_set(predicted["evidence"])
    want = _evidence_set(expected["evidence"])
    if have is None or want is None:
        return False
    return want.issubset(have)


def _payload_matches_ignoring_provenance(predicted: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Check class, scope, and payload agreement without provenance cover."""
    if predicted["class"] != expected["class"] or predicted["scope"] != expected["scope"]:
        return False
    return _payload_equiv(
        str(expected["class"]),
        predicted["payload"],
        predicted["group"],
        expected["payload"],
        expected.get("paraphrase_group_id"),
    )


def _score_case(
    case_id: str,
    view: dict[str, Any],
    truth_case: dict[str, Any],
    tallies: dict[str, dict[str, int]],
    misclass: list[dict[str, Any]],
    units: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    errors: dict[str, list[dict[str, Any]]],
) -> None:
    """Score one case with a valid envelope, keeping every defect visible."""
    if view["status"] != truth_case["expected_status"]:
        errors["disposition"].append(
            {"case_id": case_id, "reason": "status-mismatch: predicted " + str(view["status"])}
        )
    if view["zero_reason"] != truth_case["zero_reason"]:
        errors["disposition"].append({"case_id": case_id, "reason": "zero-reason-mismatch"})
    if view["coercion_loss"] != truth_case["coercion_loss"]:
        errors["disposition"].append({"case_id": case_id, "reason": "coercion-loss-mismatch"})
    if view["conflict"] != truth_case["conflict"]:
        errors["disposition"].append({"case_id": case_id, "reason": "conflict-mismatch"})
    truth_units: list[dict[str, Any]] = truth_case["expected_units"]
    accepted = [u for u in truth_units if u["disposition"] == STATUS_ACCEPTED]
    for unit in truth_units:
        if unit["disposition"] != STATUS_ACCEPTED:
            units.append(_unit_record(case_id, unit, "expected-unresolved", None, "frozen unresolved unit"))
    valid: list[dict[str, Any]] = []
    for candidate in view["candidates"]:
        problems, cview = _check_candidate(candidate)
        if problems or cview is None:
            for problem in problems:
                errors["malformed"].append({"case_id": case_id, "reason": problem})
            cls = candidate.get("class") if isinstance(candidate, dict) else None
            if isinstance(cls, str) and cls in tallies:
                tallies[cls]["fp"] += 1
                extra.append(_extra_record(case_id, candidate, str(cls), "malformed-candidate"))
            continue
        valid.append(cview)
    first_index: dict[str, int] = {}
    dup_ids: set[str] = set()
    for pos, cview in enumerate(valid):
        cid: str = cview["candidate_id"]
        if cid in first_index:
            dup_ids.add(cid)
        else:
            first_index[cid] = pos
    if dup_ids:
        errors["duplicate"].append(
            {"case_id": case_id, "reason": "duplicate-candidate-id: " + ", ".join(sorted(dup_ids))}
        )
    order_truth = sorted(range(len(accepted)), key=lambda i: str(accepted[i]["id"]))
    order_pred = sorted(range(len(valid)), key=lambda i: (str(valid[i]["candidate_id"]), i))
    matched_pred: set[int] = set()
    matched_truth: set[int] = set()
    for ti in order_truth:
        for pi in order_pred:
            if pi in matched_pred:
                continue
            if _units_match(valid[pi], accepted[ti]):
                matched_pred.add(pi)
                matched_truth.add(ti)
                cls = str(accepted[ti]["class"])
                tallies[cls]["tp"] += 1
                units.append(
                    _unit_record(case_id, accepted[ti], "matched", str(valid[pi]["candidate_id"]), "")
                )
                break
    for pi in order_pred:
        if pi in matched_pred:
            continue
        found = False
        for ti in order_truth:
            if ti in matched_truth:
                continue
            if accepted[ti]["class"] == valid[pi]["class"]:
                continue
            have = _evidence_set(valid[pi]["evidence"])
            want = _evidence_set(accepted[ti]["evidence"])
            if have is not None and want is not None and have.intersection(want):
                matched_pred.add(pi)
                matched_truth.add(ti)
                misclass.append(
                    {
                        "case_id": case_id,
                        "predicted_id": str(valid[pi]["candidate_id"]),
                        "predicted_class": str(valid[pi]["class"]),
                        "expected_class": str(accepted[ti]["class"]),
                        "expected_id": str(accepted[ti]["id"]),
                    }
                )
                units.append(
                    _unit_record(case_id, accepted[ti], "misclass", str(valid[pi]["candidate_id"]), "")
                )
                found = True
                break
        if found:
            continue
    for ti in order_truth:
        if ti not in matched_truth:
            cls = str(accepted[ti]["class"])
            tallies[cls]["fn"] += 1
            units.append(_unit_record(case_id, accepted[ti], "missed", None, "no predicted unit matched"))
    for pi in order_pred:
        if pi in matched_pred:
            continue
        cls = str(valid[pi]["class"])
        tallies[cls]["fp"] += 1
        reason = _extra_reason(case_id, valid[pi], accepted, first_index, pi, errors)
        extra.append(_extra_record(case_id, valid[pi], cls, reason))


def _extra_reason(
    case_id: str,
    predicted: dict[str, Any],
    accepted: list[dict[str, Any]],
    first_index: dict[str, int],
    pos: int,
    errors: dict[str, list[dict[str, Any]]],
) -> str:
    """Classify surplus output without ever merging it away."""
    cid = str(predicted["candidate_id"])
    if first_index.get(cid) != pos:
        return "duplicate-id"
    for unit in accepted:
        if _units_match(predicted, unit):
            errors["duplicate"].append({"case_id": case_id, "reason": "split-duplicate-output"})
            return "split-duplicate"
    for unit in accepted:
        if _payload_matches_ignoring_provenance(predicted, unit):
            errors["provenance"].append(
                {
                    "case_id": case_id,
                    "reason": "provenance-gap: candidate " + cid + " misses expected evidence",
                }
            )
            return "provenance"
    return "no-match"


def _unit_record(
    case_id: str, unit: dict[str, Any], outcome: str, predicted_id: str | None, detail: str
) -> dict[str, Any]:
    """Build one per-unit diagnostic record."""
    return {
        "case_id": case_id,
        "unit_id": str(unit["id"]),
        "class": str(unit["class"]),
        "scope": str(unit["scope"]),
        "outcome": outcome,
        "predicted_id": predicted_id,
        "detail": detail,
    }


def _extra_record(case_id: str, predicted: dict[str, Any], cls: str, reason: str) -> dict[str, Any]:
    """Build one surplus-prediction diagnostic record."""
    cid = predicted.get("candidate_id") if isinstance(predicted, dict) else None
    scope = predicted.get("scope") if isinstance(predicted, dict) else None
    return {
        "case_id": case_id,
        "candidate_id": cid if isinstance(cid, str) else "unknown",
        "class": cls,
        "scope": scope if isinstance(scope, str) else "unknown",
        "reason": reason,
    }


def _record_missed(
    truth_case: dict[str, Any], case_id: str, units: list[dict[str, Any]], tallies: dict[str, dict[str, int]]
) -> None:
    """Record frozen units as missed when no usable prediction exists."""
    for unit in truth_case["expected_units"]:
        if unit["disposition"] == STATUS_ACCEPTED:
            tallies[str(unit["class"])]["fn"] += 1
            units.append(_unit_record(case_id, unit, "missed", None, "no usable prediction"))
        else:
            units.append(_unit_record(case_id, unit, "expected-unresolved", None, "frozen unresolved unit"))


def _combine(tallies: dict[str, dict[str, int]]) -> dict[str, int]:
    """Aggregate experience classes without averaging rates."""
    combined = {"tp": 0, "fp": 0, "fn": 0}
    for cls in EXPERIENCE_CLASSES:
        for key in ("tp", "fp", "fn"):
            combined[key] += tallies[cls][key]
    return combined


def _summary(tally: dict[str, int]) -> dict[str, Any]:
    """Render precision and recall with explicit reasons for empty denominators."""
    tp = tally["tp"]
    fp = tally["fp"]
    fn = tally["fn"]
    if tp + fp > 0:
        precision: float | None = tp / (tp + fp)
        precision_reason: str | None = None
    else:
        precision = None
        precision_reason = NO_PREDICTED
    if tp + fn > 0:
        recall: float | None = tp / (tp + fn)
        recall_reason: str | None = None
    else:
        recall = None
        recall_reason = NO_EXPECTED
    return {
        "P": precision,
        "R": recall,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "P_reason": precision_reason,
        "R_reason": recall_reason,
    }
