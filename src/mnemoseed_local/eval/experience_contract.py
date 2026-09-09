"""Parser and contract envelopes for experience evaluation.

Deterministic translation from frozen raw candidates to accepted
candidates with provenance closure. Transport-only helpers over plain
JSON dicts: stdlib-only with no storage, config, daemon, provider, or
network use.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

from mnemoseed_local.eval.experience_common import payload_key

SourceCatalog = dict[str, Any]
ParseResult = dict[str, Any]

EVIDENCE_KINDS = frozenset({"chunk", "session", "node"})
CANDIDATE_CLASSES = frozenset({"fact", "lesson", "intention", "skill_sequence"})
ZERO_REASONS = ("no-match", "malformed-input", "over-budget-overflow", "all-filtered")

_CATALOG_FIELDS = frozenset({"run_id", "sources"})
_SOURCE_FIELDS = frozenset({"kind", "id", "fixture_id", "text", "span"})
_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "class",
        "scope",
        "payload",
        "evidence",
        "paraphrase_group_id",
        "conflict_pair_id",
        "confidence",
        "weight",
    }
)
_POINTER_FIELDS = frozenset({"kind", "id"})
_CONFLICT_CLASS = "lesson"


class _Invalid(Exception):
    """Internal fail-closed signal with a precise reason fragment."""

    def __init__(self, detail: str, coercion_loss: bool) -> None:
        super().__init__(detail)
        self.detail = detail
        self.coercion_loss = coercion_loss


def _loads_strict(text: str) -> Any:
    """Parse JSON while rejecting duplicate keys and non-finite constants."""

    def _reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                raise _Invalid(f"duplicate JSON object key {key!r}", True)
            obj[key] = value
        return obj

    def _reject_constant(value: str) -> Any:
        raise _Invalid(f"non-finite JSON constant {value!r}", True)

    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate, parse_constant=_reject_constant)
    except _Invalid:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise _Invalid(f"invalid JSON: {exc}", False) from exc


def _check_json(value: Any, where: str) -> None:
    """Reject non-JSON values and non-finite floats without mutating input."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Invalid(f"non-finite number at {where}", True)
        return
    if isinstance(value, list):
        for pos, item in enumerate(value):
            _check_json(item, f"{where}[{pos}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _Invalid(f"non-string key at {where}", False)
            _check_json(item, f"{where}.{key}")
        return
    raise _Invalid(f"non-JSON value at {where} ({type(value).__name__})", False)


def _validate_catalog(sources: SourceCatalog) -> tuple[str, dict[tuple[str, str], dict[str, Any]]]:
    """Check the authoritative catalog and index sources by (kind, id)."""
    if not isinstance(sources, dict):
        raise _Invalid(f"source catalog must be an object, got {type(sources).__name__}", False)
    for key in sources:
        if not isinstance(key, str):
            raise _Invalid("source catalog has a non-string key", False)
    unknown = set(sources) - _CATALOG_FIELDS
    if unknown:
        raise _Invalid(f"unknown source catalog field {sorted(unknown)[0]!r}", True)
    run_id = sources.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise _Invalid("source catalog needs a nonempty string run_id", False)
    entries = sources.get("sources")
    if not isinstance(entries, list):
        raise _Invalid("source catalog needs a list of sources", False)
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for pos, entry in enumerate(entries):
        where = f"sources[{pos}]"
        if not isinstance(entry, dict):
            raise _Invalid(f"{where} must be an object", False)
        for key in entry:
            if not isinstance(key, str):
                raise _Invalid(f"{where} has a non-string key", False)
        extra = set(entry) - _SOURCE_FIELDS
        if extra:
            raise _Invalid(f"{where} has unknown field {sorted(extra)[0]!r}", True)
        kind = entry.get("kind")
        sid = entry.get("id")
        if kind not in EVIDENCE_KINDS:
            raise _Invalid(f"{where} has invalid evidence kind {kind!r}", False)
        if not isinstance(sid, str) or not sid:
            raise _Invalid(f"{where} needs a nonempty string id", False)
        fixture_id = entry.get("fixture_id")
        text = entry.get("text")
        span = entry.get("span")
        if not isinstance(fixture_id, str):
            raise _Invalid(f"{where} needs a string fixture_id", False)
        if not isinstance(text, str):
            raise _Invalid(f"{where} needs a string text", False)
        if span is not None and not isinstance(span, str):
            raise _Invalid(f"{where} needs a string or null span", False)
        pair = (kind, sid)
        if pair in index:
            raise _Invalid(f"duplicate source {kind!r} id {sid!r}", False)
        index[pair] = {"fixture_id": fixture_id, "span": span}
    return run_id, index


def _catalog_run_id(sources: Any) -> str | None:
    """Report the catalog run id, or null when the catalog itself is invalid."""
    try:
        run_id, _ = _validate_catalog(sources)
    except _Invalid:
        return None
    return run_id


def _optional_id(value: Any, where: str) -> str | None:
    """Read an optional grouping id; null stays null and only strings pass."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _Invalid(f"{where} must be a string or null", False)
    return value or None


def _validate_candidate(raw: Any, pos: int) -> dict[str, Any]:
    """Check one raw candidate and copy it into an internal record."""
    where = f"candidates[{pos}]"
    if not isinstance(raw, dict):
        raise _Invalid(f"{where} must be an object, got {type(raw).__name__}", False)
    for key in raw:
        if not isinstance(key, str):
            raise _Invalid(f"{where} has a non-string key", False)
    extra = set(raw) - _CANDIDATE_FIELDS
    if extra:
        raise _Invalid(f"{where} has unknown field {sorted(extra)[0]!r}", True)
    cid = raw.get("candidate_id")
    if not isinstance(cid, str) or not cid:
        raise _Invalid(f"{where} needs a nonempty string candidate_id", False)
    klass = raw.get("class")
    if klass not in CANDIDATE_CLASSES:
        raise _Invalid(f"{where} has invalid class {klass!r}", False)
    scope = raw.get("scope")
    if not isinstance(scope, str) or not scope:
        raise _Invalid(f"{where} needs a nonempty string scope", False)
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise _Invalid(f"{where} needs a dict payload", False)
    _check_json(payload, f"{where}.payload")
    try:
        key = payload_key(klass, payload)
    except ValueError as exc:
        raise _Invalid(f"{where} has invalid {klass} payload: {exc}", False) from exc
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise _Invalid(f"{where} needs a nonempty evidence list", False)
    pointers: list[tuple[str, str]] = []
    for num, pointer in enumerate(evidence):
        ptr_where = f"{where}.evidence[{num}]"
        if not isinstance(pointer, dict):
            raise _Invalid(f"{ptr_where} must be an object", False)
        for sub in pointer:
            if not isinstance(sub, str):
                raise _Invalid(f"{ptr_where} has a non-string key", False)
        rest = set(pointer) - _POINTER_FIELDS
        if rest:
            raise _Invalid(f"{ptr_where} has unknown field {sorted(rest)[0]!r}", True)
        kind = pointer.get("kind")
        sid = pointer.get("id")
        if kind not in EVIDENCE_KINDS:
            raise _Invalid(f"{ptr_where} has invalid evidence kind {kind!r}", False)
        if not isinstance(sid, str) or not sid:
            raise _Invalid(f"{ptr_where} needs a nonempty string id", False)
        pointers.append((kind, sid))
    group_raw = raw.get("paraphrase_group_id")
    pair_raw = raw.get("conflict_pair_id")
    record: dict[str, Any] = {
        "id": cid,
        "klass": klass,
        "scope": scope,
        "payload": copy.deepcopy(payload),
        "pointers": sorted(set(pointers)),
        "key": key,
        "group": _optional_id(group_raw, f"{where}.paraphrase_group_id"),
        "pair": _optional_id(pair_raw, f"{where}.conflict_pair_id"),
    }
    if "paraphrase_group_id" in raw:
        record["group_raw"] = copy.deepcopy(group_raw)
    if "conflict_pair_id" in raw:
        record["pair_raw"] = copy.deepcopy(pair_raw)
    if "confidence" in raw:
        _check_json(raw["confidence"], f"{where}.confidence")
        record["confidence"] = copy.deepcopy(raw["confidence"])
    if "weight" in raw:
        _check_json(raw["weight"], f"{where}.weight")
        record["weight"] = copy.deepcopy(raw["weight"])
    return record


def _same_repeat(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Decide whether two entries reuse one candidate id with identical content."""
    if first["klass"] != second["klass"] or first["scope"] != second["scope"]:
        return False
    if first["key"] != second["key"]:
        return False
    if set(first["pointers"]) != set(second["pointers"]):
        return False
    if first["group"] != second["group"] or first["pair"] != second["pair"]:
        return False
    for field in ("confidence", "weight"):
        if (field in first) != (field in second):
            return False
        if field in first and first[field] != second[field]:
            return False
    return True


def _partition(records: list[dict[str, Any]]) -> list[list[int]]:
    """Group record positions by class/scope-fenced payload or group equality."""
    parent = list(range(len(records)))

    def find(pos: int) -> int:
        while parent[pos] != pos:
            parent[pos] = parent[parent[pos]]
            pos = parent[pos]
        return pos

    def union(left: int, right: int) -> None:
        root = find(left)
        other = find(right)
        if root != other:
            parent[other] = root

    for left in range(len(records)):
        for right in range(left + 1, len(records)):
            first = records[left]
            second = records[right]
            if first["klass"] != second["klass"] or first["scope"] != second["scope"]:
                continue
            same_group = bool(first["group"]) and first["group"] == second["group"]
            if first["key"] == second["key"] or same_group:
                union(left, right)
    buckets: dict[int, list[int]] = {}
    for pos in range(len(records)):
        buckets.setdefault(find(pos), []).append(pos)
    return list(buckets.values())


def _declared_conflicts(records: list[dict[str, Any]]) -> list[tuple[str, str, list[str]]]:
    """List declared lesson pairs sharing scope and pair id with distinct ids."""
    buckets: dict[tuple[str, str], set[str]] = {}
    for record in records:
        if record["klass"] != _CONFLICT_CLASS or not record["pair"]:
            continue
        buckets.setdefault((record["scope"], record["pair"]), set()).add(record["id"])
    pairs = [(scope, pair, sorted(ids)) for (scope, pair), ids in buckets.items() if len(ids) >= 2]
    return sorted(pairs)


def _envelope(
    status: str,
    candidates: list[dict[str, Any]],
    zero_result: bool,
    zero_reason: str | None,
    coercion_loss: bool,
    conflict: bool,
    conflict_reason: str | None,
    reason: str,
    run_id: str | None,
    conflicts: list[list[str]],
) -> ParseResult:
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
            "disposition": status,
            "run_id": run_id,
            "conflicts": conflicts,
        },
    }


def _accepted_candidate(
    representative: dict[str, Any],
    union: list[tuple[str, str]],
    index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Render one accepted candidate with union evidence and a catalog source ref."""
    ref = index[representative["pointers"][0]]
    candidate: dict[str, Any] = {
        "candidate_id": representative["id"],
        "class": representative["klass"],
        "scope": representative["scope"],
        "payload": copy.deepcopy(representative["payload"]),
        "evidence": [{"kind": kind, "id": sid} for kind, sid in union],
    }
    if "group_raw" in representative:
        candidate["paraphrase_group_id"] = copy.deepcopy(representative["group_raw"])
    if "pair_raw" in representative:
        candidate["conflict_pair_id"] = copy.deepcopy(representative["pair_raw"])
    if "confidence" in representative:
        candidate["confidence"] = copy.deepcopy(representative["confidence"])
    if "weight" in representative:
        candidate["weight"] = copy.deepcopy(representative["weight"])
    candidate["source_ref"] = {"fixture_id": ref["fixture_id"], "span": ref["span"]}
    return candidate


def _shape_parsed(parsed: Any) -> list[Any] | None:
    """Normalize parsed JSON to a candidate list; null means explicit no-match."""
    if parsed is None:
        return None
    if isinstance(parsed, str):
        if not parsed.strip():
            return None
        raise _Invalid("JSON string value is not a candidate", False)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        if not parsed:
            return None
        return parsed
    raise _Invalid(f"JSON scalar {parsed!r} is not a candidate", False)


def parse_experience_raw(raw: Any, *, sources: SourceCatalog) -> ParseResult:
    """Parse frozen raw candidates into a contract envelope with provenance closure."""
    items: list[Any] | None
    if raw is None:
        items = None
    elif isinstance(raw, str):
        if not raw.strip():
            items = None
        else:
            try:
                items = _shape_parsed(_loads_strict(raw))
            except _Invalid as exc:
                return _envelope(
                    "unresolved",
                    [],
                    True,
                    "malformed-input",
                    exc.coercion_loss,
                    False,
                    None,
                    f"malformed-input: {exc.detail}",
                    _catalog_run_id(sources),
                    [],
                )
    elif isinstance(raw, list):
        items = None if not raw else raw
    elif isinstance(raw, dict):
        items = [raw]
    else:
        return _envelope(
            "unresolved",
            [],
            True,
            "malformed-input",
            False,
            False,
            None,
            f"malformed-input: input must be JSON text, dict, or list, got {type(raw).__name__}",
            _catalog_run_id(sources),
            [],
        )
    if items is None:
        return _envelope(
            "unresolved",
            [],
            True,
            "no-match",
            False,
            False,
            None,
            "no-match: empty input",
            _catalog_run_id(sources),
            [],
        )
    try:
        run_id, index = _validate_catalog(sources)
    except _Invalid as exc:
        return _envelope(
            "unresolved",
            [],
            True,
            "malformed-input",
            exc.coercion_loss,
            False,
            None,
            f"malformed-input: {exc.detail}",
            None,
            [],
        )
    try:
        records = [_validate_candidate(item, pos) for pos, item in enumerate(items)]
    except _Invalid as exc:
        return _envelope(
            "unresolved",
            [],
            True,
            "malformed-input",
            exc.coercion_loss,
            False,
            None,
            f"malformed-input: {exc.detail}",
            run_id,
            [],
        )
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        known = by_id.get(record["id"])
        if known is None:
            by_id[record["id"]] = record
        elif not _same_repeat(known, record):
            return _envelope(
                "unresolved",
                [],
                True,
                "malformed-input",
                False,
                False,
                None,
                f"malformed-input: conflicting reuse of candidate_id {record['id']!r}",
                run_id,
                [],
            )
    unique = list(by_id.values())
    for record in unique:
        for pointer in record["pointers"]:
            if pointer not in index:
                kind, sid = pointer
                return _envelope(
                    "unresolved",
                    [],
                    True,
                    "malformed-input",
                    False,
                    False,
                    None,
                    f"malformed-input: unresolved evidence {kind!r} id {sid!r} "
                    f"for candidate {record['id']!r}",
                    run_id,
                    [],
                )
    declared = _declared_conflicts(unique)
    conflicted: set[str] = set()
    conflicts: list[list[str]] = []
    conflict_reason: str | None = None
    if declared:
        declared_ids = {cid for _, _, ids in declared for cid in ids}
        for part in _partition(unique):
            relatives = {unique[pos]["id"] for pos in part}
            if relatives & declared_ids:
                conflicted.update(relatives)
        conflicts = sorted(ids for _, _, ids in declared)
        pieces = [
            f"conflict-pair {pair!r} in scope {scope!r}: {', '.join(ids)}" for scope, pair, ids in declared
        ]
        propagated = sorted(conflicted - declared_ids)
        if propagated:
            pieces.append(f"propagated-to: {', '.join(propagated)}")
        conflict_reason = "; ".join(pieces)
    survivors = [record for record in unique if record["id"] not in conflicted]
    if not survivors:
        return _envelope(
            "unresolved",
            [],
            True,
            "all-filtered",
            False,
            True,
            conflict_reason,
            f"all-filtered: all candidates removed by declared conflicts: {conflict_reason}",
            run_id,
            conflicts,
        )
    accepted: list[dict[str, Any]] = []
    for part in _partition(survivors):
        members = [survivors[pos] for pos in part]
        representative = min(members, key=lambda item: item["id"])
        union = sorted({pointer for member in members for pointer in member["pointers"]})
        accepted.append(_accepted_candidate(representative, union, index))
    accepted.sort(key=lambda item: item["candidate_id"])
    reason = "accepted-with-conflicts-filtered" if declared else "accepted"
    return _envelope(
        "accepted",
        accepted,
        False,
        None,
        False,
        bool(declared),
        conflict_reason,
        reason,
        run_id,
        conflicts,
    )


def build_e2_candidates(
    error_event: dict[str, Any],
    prefrozen_candidate: dict[str, Any],
    *,
    sources: SourceCatalog | None = None,
    enable_test_candidates: bool = False,
) -> list[dict[str, Any]]:
    """Nominate in-memory E2 candidates for a synthetic error event (test-only)."""
    if not enable_test_candidates:
        return []
    if not isinstance(sources, dict):
        return []
    if not isinstance(error_event, dict):
        return []
    pointer = error_event.get("evidence_ptr")
    if not isinstance(pointer, dict) or set(pointer) != {"kind", "id"}:
        return []
    kind = pointer.get("kind")
    sid = pointer.get("id")
    if kind not in EVIDENCE_KINDS or not isinstance(sid, str) or not sid:
        return []
    if not isinstance(prefrozen_candidate, dict):
        return []
    result = parse_experience_raw([prefrozen_candidate], sources=sources)
    if result["status"] != "accepted":
        return []
    wanted = (kind, sid)
    matched = [
        item
        for item in result["candidates"]
        if wanted in {(entry["kind"], entry["id"]) for entry in item["evidence"]}
    ]
    if not matched:
        return []
    return copy.deepcopy(matched)
