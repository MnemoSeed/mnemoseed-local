"""No-number SDT summary for M5prep (shape definitions only, no numeric bar).

This module describes how a future sequestered one-shot run would be
summarized: per-channel signal-detection counts, a zero-reason histogram,
and a confidence-decile table. It proposes no number, records no verdict,
and performs no data run itself.
"""

from __future__ import annotations

import unicodedata
from typing import Any

VERSION = 1
N_BINS = 10
N_DECILES = 10
BIN_EDGES = [i / N_BINS for i in range(N_BINS + 1)]

BANNED_BAR_TOKENS = frozenset(
    {
        "threshold",
        "thresholds",
        "cutoff",
        "cutoffs",
        "criterion",
        "criteria",
        "target",
        "targets",
        "targetp",
        "targetr",
        "minp",
        "minr",
        "dprime",
        "metad",
        "pass",
        "fail",
        "verdict",
        "passbar",
        "qualitygate",
        "bar",
        "bars",
        "gate",
        "gates",
        "kpi",
        "slo",
        "limit",
        "theta",
        "epsilon",
    }
)

BENIGN_NUMERIC_NAMES = frozenset({"BIN_EDGES", "N_BINS", "VERSION", "N_DECILES"})

BENIGN_PROPOSAL_KEYS = frozenset(
    {
        "method",
        "required_gates",
        "forbidden_claims",
        "not_observed",
        "report_fields",
        "version",
        "n_cases",
        "min_cases",
        "corpus_size",
    }
)

VENDORED_BANNED_CLAIM_PHRASES = (
    "real model quality is",
    "generaliz",
    "production-ready",
    "promotion gate passed",
    "efficacy",
    "simulat",
    "recall improv",
    "extraction precision",
)

EXTRA_BANNED_CLAIM_PHRASES = (
    "m5 ratified",
    "production-ready",
    "promotion gate passed",
    "landed",
    "generaliz",
    "efficacy",
)

NOT_OBSERVED_TOPICS = (
    "natural-language extraction quality",
    "real model quality",
    "real-data coverage/eligibility",
    "live resource impact",
)

__all__ = [
    "BENIGN_NUMERIC_NAMES",
    "BENIGN_PROPOSAL_KEYS",
    "BANNED_BAR_TOKENS",
    "BIN_EDGES",
    "EXTRA_BANNED_CLAIM_PHRASES",
    "NOT_OBSERVED_TOPICS",
    "N_BINS",
    "N_DECILES",
    "VENDORED_BANNED_CLAIM_PHRASES",
    "VERSION",
    "check_report_claims",
    "normalize_bar_token",
    "report_sdt",
    "validate_bar_proposal",
]

_TRAILING_MARKS = ".!?;:,。" + "！？；：，、"


def normalize_bar_token(name: str) -> str:
    """Lowercase ``name`` and drop every non-alphanumeric separator."""
    return "".join(char for char in name.lower() if char.isalnum())


def validate_bar_proposal(proposal: dict[str, Any]) -> list[str]:
    """Reject method proposals that name a numeric efficacy bar.

    Allowlisted keys pass with values unrestricted; every other key is
    rejected when its normalized form equals or contains a banned token.
    Values are otherwise never scanned.
    """
    findings: list[str] = []
    for key, value in proposal.items():
        name = key if isinstance(key, str) else str(key)
        if name.lower() in BENIGN_PROPOSAL_KEYS:
            continue
        token = normalize_bar_token(name)
        if any(member in token for member in BANNED_BAR_TOKENS):
            findings.append("numeric-bar-rejected:" + name)
            continue
        if name.lower() in ("pass", "fail", "verdict", "gate_result"):
            if isinstance(value, (bool, int, float)):
                findings.append("numeric-bar-rejected:" + name)
    return findings


def _norm_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value)
    text = text.casefold()
    text = " ".join(text.split())
    if text and text[-1] in _TRAILING_MARKS:
        text = text[:-1].strip()
    return text


def _norm_value(value: Any) -> Any:
    if isinstance(value, str):
        return _norm_text(value)
    return value


def _payload_key(cls: str, payload: Any) -> tuple[Any, ...] | None:
    if not isinstance(payload, dict):
        return None
    if cls == "fact":
        fields = ("subject", "predicate", "object", "polarity")
        if any(field not in payload for field in fields):
            return None
        return ("fact",) + tuple(_norm_value(payload[field]) for field in fields)
    if cls == "lesson":
        if "text" not in payload:
            return None
        return ("lesson", _norm_value(payload["text"]))
    if cls == "intention":
        if "trigger_condition" not in payload or "action" not in payload:
            return None
        return (
            "intention",
            _norm_value(payload["trigger_condition"]),
            _norm_value(payload["action"]),
            _norm_value(payload.get("status")),
        )
    if cls == "skill_sequence":
        if "task_type" not in payload:
            return None
        chain = payload.get("tool_chain")
        if isinstance(chain, (list, tuple)):
            chain = tuple(chain)
        return (
            "skill_sequence",
            _norm_value(payload["task_type"]),
            chain,
            _norm_value(payload.get("success_rate")),
        )
    return None


def _evidence_ids(unit: dict[str, Any]) -> set[tuple[str, str]]:
    ids: set[tuple[str, str]] = set()
    evidence = unit.get("evidence", [])
    if isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, dict):
                kind = entry.get("kind")
                ref = entry.get("id")
                if isinstance(kind, str) and isinstance(ref, str):
                    ids.add((kind, ref))
    return ids


def _unit_class(unit: dict[str, Any]) -> str:
    cls = unit.get("class", unit.get("kind"))
    if isinstance(cls, str):
        return cls
    return ""


def _unit_scope(unit: dict[str, Any]) -> Any:
    return unit.get("scope")


def _unit_group(unit: dict[str, Any]) -> str:
    group = unit.get("paraphrase_group_id")
    if isinstance(group, str) and group:
        return group
    return ""


def _channel(cls: str) -> str:
    if cls == "fact":
        return "fact"
    return "experience"


def _units_equivalent(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    if _unit_scope(expected) != _unit_scope(observed):
        return False
    cls = _unit_class(expected)
    expected_key = _payload_key(cls, expected.get("payload"))
    observed_key = _payload_key(cls, observed.get("payload"))
    if expected_key is not None and expected_key == observed_key:
        return True
    expected_group = _unit_group(expected)
    observed_group = _unit_group(observed)
    return bool(expected_group) and expected_group == observed_group


def _predicted_units(entry: dict[str, Any]) -> list[dict[str, Any]]:
    result = entry.get("result")
    if not isinstance(result, dict):
        raise ValueError("malformed predicted envelope: result must be a mapping")
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("malformed predicted envelope: candidates must be a list")
    if result.get("status") != "accepted":
        return []
    units: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("malformed predicted envelope: candidate must be a mapping")
        units.append(candidate)
    return units


def _truth_units(case: dict[str, Any]) -> list[dict[str, Any]]:
    raw_units = case.get("expected_units", [])
    if not isinstance(raw_units, list):
        raise ValueError("malformed truth case: expected_units must be a list")
    accepted: list[dict[str, Any]] = []
    for unit in raw_units:
        if not isinstance(unit, dict):
            raise ValueError("malformed truth case: unit must be a mapping")
        if unit.get("disposition", "accepted") == "accepted":
            accepted.append(unit)
    return accepted


def report_sdt(predicted: list[dict[str, Any]], truth: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize predicted envelopes against truth as SDT-shape counts.

    Matching is one-to-one per case: normalized structural payload equality
    or a shared nonempty paraphrase group, always fenced by class and scope,
    with predicted evidence covering the expected evidence ids. Counts only;
    no rates, no bars.
    """
    truth_by_id: dict[str, dict[str, Any]] = {}
    for case in truth:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise ValueError("malformed truth case: string case_id required")
        case_id = case["case_id"]
        if case_id in truth_by_id:
            raise ValueError("malformed truth: duplicate case_id")
        truth_by_id[case_id] = case
    seen: set[str] = set()
    fact_counts = {"hit": 0, "miss": 0, "false_alarm": 0}
    experience_counts = {"hit": 0, "miss": 0, "false_alarm": 0}
    correct_rejection = 0
    misclass: list[dict[str, str]] = []
    histogram: dict[str, int] = {}
    bins: list[dict[str, Any]] = []
    for edge in range(N_BINS):
        bins.append({"bin": [BIN_EDGES[edge], BIN_EDGES[edge + 1]], "n": 0, "accuracy": None})
    correct_per_bin = [0] * N_BINS
    unscored_units = 0
    for entry in predicted:
        if not isinstance(entry, dict):
            raise ValueError("malformed predicted envelope: flattened candidate lists are never scored")
        if not isinstance(entry.get("case_id"), str) or "result" not in entry:
            raise ValueError("malformed predicted envelope: case_id and result required")
        case_id = entry["case_id"]
        if case_id in seen:
            raise ValueError("malformed predicted envelopes: duplicate case_id")
        seen.add(case_id)
        if case_id not in truth_by_id:
            raise ValueError("malformed predicted envelopes: unexpected case_id")
        expected = _truth_units(truth_by_id[case_id])
        observed = _predicted_units(entry)
        if not expected and not observed:
            correct_rejection += 1
        else:
            matched_expected = [False] * len(expected)
            matched_observed = [False] * len(observed)
            correct_observed = [False] * len(observed)
            for i, unit in enumerate(expected):
                for j, candidate in enumerate(observed):
                    if matched_observed[j]:
                        continue
                    if _unit_class(candidate) != _unit_class(unit):
                        continue
                    if not _units_equivalent(unit, candidate):
                        continue
                    if not _evidence_ids(unit) <= _evidence_ids(candidate):
                        continue
                    matched_expected[i] = True
                    matched_observed[j] = True
                    correct_observed[j] = True
                    if _channel(_unit_class(unit)) == "fact":
                        fact_counts["hit"] += 1
                    else:
                        experience_counts["hit"] += 1
                    break
            for i, unit in enumerate(expected):
                if matched_expected[i]:
                    continue
                for j, candidate in enumerate(observed):
                    if matched_observed[j]:
                        continue
                    if _unit_class(candidate) == _unit_class(unit):
                        continue
                    if not _units_equivalent(unit, candidate):
                        continue
                    if not _evidence_ids(unit) <= _evidence_ids(candidate):
                        continue
                    matched_expected[i] = True
                    matched_observed[j] = True
                    misclass.append(
                        {
                            "case_id": case_id,
                            "expected_class": _unit_class(unit),
                            "predicted_class": _unit_class(candidate),
                        }
                    )
                    if _channel(_unit_class(unit)) == "fact":
                        fact_counts["miss"] += 1
                    else:
                        experience_counts["miss"] += 1
                    break
            for i, unit in enumerate(expected):
                if not matched_expected[i]:
                    if _channel(_unit_class(unit)) == "fact":
                        fact_counts["miss"] += 1
                    else:
                        experience_counts["miss"] += 1
            for j, candidate in enumerate(observed):
                if not matched_observed[j]:
                    if _channel(_unit_class(candidate)) == "fact":
                        fact_counts["false_alarm"] += 1
                    else:
                        experience_counts["false_alarm"] += 1
        if not observed:
            raw_result = entry["result"]
            reason = raw_result.get("zero_reason") if isinstance(raw_result, dict) else None
            if isinstance(reason, str) and reason:
                histogram[reason] = histogram.get(reason, 0) + 1
        for j, candidate in enumerate(observed):
            confidence = candidate.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                unscored_units += 1
                continue
            level = float(confidence)
            if not 0.0 <= level <= 1.0:
                unscored_units += 1
                continue
            cell = N_BINS - 1
            for edge in range(N_BINS):
                upper = BIN_EDGES[edge + 1]
                if level < upper or (edge == N_BINS - 1 and level <= upper):
                    cell = edge
                    break
            bins[cell]["n"] += 1
            if correct_observed[j]:
                correct_per_bin[cell] += 1
    for edge in range(N_BINS):
        total = bins[edge]["n"]
        if isinstance(total, int) and total > 0:
            bins[edge]["accuracy"] = correct_per_bin[edge] / total
    return {
        "fact": fact_counts,
        "experience": experience_counts,
        "correct_rejection": correct_rejection,
        "misclass": misclass,
        "zero_reason_histogram": histogram,
        "confidence_deciles": bins,
        "unscored_units": unscored_units,
    }


def check_report_claims(text: str) -> dict[str, Any]:
    """Scan prose for banned outcome claims and missing NOT_OBSERVED topics."""
    lowered = text.casefold()
    phrases: list[str] = []
    for phrase in VENDORED_BANNED_CLAIM_PHRASES:
        phrases.append(phrase)
    for phrase in EXTRA_BANNED_CLAIM_PHRASES:
        if phrase not in phrases:
            phrases.append(phrase)
    hits = [phrase for phrase in phrases if phrase in lowered]
    missing = [topic for topic in NOT_OBSERVED_TOPICS if topic not in lowered]
    return {"hits": hits, "missing_topics": missing}
