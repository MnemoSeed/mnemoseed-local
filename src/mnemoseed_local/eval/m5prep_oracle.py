"""M5prep blind-oracle helpers (lane O).

Independent text normalizer plus dual-control seal and one-shot log helpers.
The normalizer is a frozen copy of the dispatch rule for oracle-side
comparison only. Seal helpers hash raw file bytes exactly as written.
Registry reads accept ``scratch-``-prefixed test hashes only alongside
scratch registry paths; any other value follows the strict hex rules.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SEAL_METHOD = "sha256-raw-bytes-lowercase-hex"
_TRAILING_MARKS = ".!?;:,。！？；：，、…"
_WS_PATTERN = re.compile(r"\s+")
_HEX_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_HEX_UPPER_PATTERN = re.compile(r"[0-9A-Fa-f]{64}\Z")
_SCRATCH_PREFIX = "scratch-"
_REQUIRED_FIELDS = (
    "seq",
    "inputs_sha256",
    "truth_sha256",
    "n_cases",
    "created_utc",
    "oracle_session",
    "registrar_session",
    "method",
    "status",
)
_OPS_KEYS = (
    "socket.socket.connect",
    "socket.create_connection",
    "urllib.request.urlopen",
    "http.client.HTTPConnection.request",
)


def normalize_oracle(text: str) -> str:
    """Freeze the dispatch text rule for oracle-side comparison."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    value = unicodedata.normalize("NFKC", text)
    value = value.casefold()
    value = _WS_PATTERN.sub(" ", value).strip()
    if value and value[-1] in _TRAILING_MARKS:
        value = value[:-1]
    return value.strip()


def _is_scratch_hash(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_SCRATCH_PREFIX)


def _hash_ok(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if _HEX_PATTERN.match(value):
        return True
    tail = value[len(_SCRATCH_PREFIX) :] if _is_scratch_hash(value) else ""
    return bool(tail) and _HEX_PATTERN.match(tail) is not None


def _hash_upper(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if _HEX_UPPER_PATTERN.match(value) and not _HEX_PATTERN.match(value):
        return True
    if _is_scratch_hash(value):
        tail = value[len(_SCRATCH_PREFIX) :]
        return _HEX_UPPER_PATTERN.match(tail) is not None and _HEX_PATTERN.match(tail) is None
    return False


_TRANSITION_IDENTITY = (
    "seq",
    "inputs_sha256",
    "truth_sha256",
    "n_cases",
    "created_utc",
    "oracle_session",
    "registrar_session",
    "method",
)

_INPUT_CASE_KEYS = ("case_id", "raw", "sources", "variant")
_TRUTH_CASE_KEYS = (
    "case_id",
    "expected_status",
    "zero_reason",
    "coercion_loss",
    "conflict",
    "expected_units",
)
_UNIT_KEYS = ("id", "class", "scope", "payload", "evidence", "disposition")
_EXPECTED_STATUSES = ("accepted", "unresolved")
_DISPATCH_CLASSES = ("fact", "lesson", "intention", "skill_sequence")
_DISPATCH_DISPOSITIONS = ("accepted", "unresolved")


def _transition_identity_match(active: dict[str, Any], void_row: dict[str, Any]) -> bool:
    return all(active.get(key) == void_row.get(key) for key in _TRANSITION_IDENTITY)


def parse_registry(path: str) -> dict[str, Any]:
    """Parse an append-only seal registry file order-independently.

    Effective entries follow the A2 terminal-transition rule: one active row
    followed by one matching void row renders an effective void; any other
    repeated-seq shape is ``duplicate-seq`` and a drifted transition is
    ``void-transition-mismatch``.
    """
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_truth: set[str] = set()
    pending: dict[Any, dict[str, Any]] = {}
    terminal: set[Any] = set()
    raw = Path(path).read_text(encoding="utf-8")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            record: Any = json.loads(line)
        except json.JSONDecodeError:
            errors.append("malformed-line")
            continue
        if not isinstance(record, dict):
            errors.append("malformed-line")
            continue
        if any(key not in record for key in _REQUIRED_FIELDS):
            errors.append("missing-field")
            continue
        if record["status"] == "void" and "void_reason" not in record:
            errors.append("missing-field")
            continue
        seq_value = record["seq"]
        count_value = record["n_cases"]
        if (
            isinstance(seq_value, bool)
            or not isinstance(seq_value, int)
            or isinstance(count_value, bool)
            or not isinstance(count_value, int)
        ):
            errors.append("malformed-line")
            continue
        hash_failure = ""
        for key in ("inputs_sha256", "truth_sha256"):
            candidate = record[key]
            if _hash_ok(candidate):
                continue
            hash_failure = "uppercase-hex" if _hash_upper(candidate) else "bad-hex"
            break
        if hash_failure:
            errors.append(hash_failure)
            continue
        if record["method"] != _SEAL_METHOD:
            errors.append("unknown-method")
            continue
        if record["status"] not in ("active", "void"):
            errors.append("bad-status")
            continue
        if seq_value in terminal:
            errors.append("duplicate-seq")
            continue
        if seq_value in pending:
            active_row = pending[seq_value]
            if record["status"] == "active":
                errors.append("duplicate-seq")
                continue
            if not _transition_identity_match(active_row, record):
                errors.append("void-transition-mismatch")
                del pending[seq_value]
                terminal.add(seq_value)
                continue
            missing = [key for key in ("voided_utc", "voided_by_session") if key not in record]
            del pending[seq_value]
            terminal.add(seq_value)
            if missing:
                errors.append("missing-field")
                continue
            entries.append(record)
            continue
        if record["status"] == "active":
            pending[seq_value] = record
            continue
        entries.append(record)
        terminal.add(seq_value)
    for active_row in pending.values():
        if active_row["truth_sha256"] in seen_truth:
            errors.append("duplicate-truth-hash")
            continue
        seen_truth.add(active_row["truth_sha256"])
        entries.append(active_row)
    return {"entries": entries, "errors": errors}


def _schema_abort(location: str, reason: str) -> str:
    return f"schema-abort:{location}:{reason}"


def validate_inputs_schema(inputs_bytes: bytes) -> list[str]:
    """Validate the input envelope against the A2 public ABI."""
    problems: list[str] = []
    try:
        doc: Any = json.loads(inputs_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return [_schema_abort("root", "not-json")]
    if not isinstance(doc, dict):
        return [_schema_abort("root", "not-object")]
    if doc.get("version") != 1:
        return [_schema_abort("root", "bad-version")]
    cases = doc.get("cases")
    if not isinstance(cases, list):
        return [_schema_abort("root", "cases-not-list")]
    seen: set[str] = set()
    for index, case in enumerate(cases):
        location = f"case:{index}"
        if not isinstance(case, dict):
            problems.append(_schema_abort(location, "not-object"))
            continue
        for key in _INPUT_CASE_KEYS:
            if key not in case:
                problems.append(_schema_abort(location, f"missing-key:{key}"))
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            problems.append(_schema_abort(location, "empty-case-id"))
        elif case_id in seen:
            problems.append(_schema_abort(location, "duplicate-case-id"))
        else:
            seen.add(case_id)
    return problems


def validate_truth_schema(truth_bytes: bytes) -> list[str]:
    """Validate the sealed truth envelope against the A2 public ABI."""
    problems: list[str] = []
    try:
        doc: Any = json.loads(truth_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return [_schema_abort("root", "not-json")]
    if not isinstance(doc, dict):
        return [_schema_abort("root", "not-object")]
    if doc.get("version") != 1:
        return [_schema_abort("root", "bad-version")]
    cases = doc.get("cases")
    if not isinstance(cases, list):
        return [_schema_abort("root", "cases-not-list")]
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            problems.append(_schema_abort("case:<unknown>", "not-object"))
            continue
        case_id = case.get("case_id")
        location = f"case:{case_id}" if isinstance(case_id, str) and case_id else "case:<unknown>"
        for key in _TRUTH_CASE_KEYS:
            if key not in case:
                problems.append(_schema_abort(location, f"missing-key:{key}"))
        if not isinstance(case_id, str) or not case_id:
            problems.append(_schema_abort(location, "empty-case-id"))
        elif case_id in seen:
            problems.append(_schema_abort(location, "duplicate-case-id"))
        else:
            seen.add(case_id)
        if case.get("expected_status") not in _EXPECTED_STATUSES:
            problems.append(_schema_abort(location, "bad-expected-status"))
        expected_units = case.get("expected_units")
        if not isinstance(expected_units, list):
            problems.append(_schema_abort(location, "expected-units-not-list"))
            expected_units = []
        for unit_index, unit in enumerate(expected_units):
            unit_location = f"{location}:unit:{unit_index}"
            if not isinstance(unit, dict):
                problems.append(_schema_abort(unit_location, "not-object"))
                continue
            for key in _UNIT_KEYS:
                if key not in unit:
                    problems.append(_schema_abort(unit_location, f"missing-key:{key}"))
            for key in unit:
                if key not in _UNIT_KEYS:
                    problems.append(_schema_abort(unit_location, f"unexpected-key:{key}"))
            unit_id = unit.get("id")
            if not isinstance(unit_id, str) or not unit_id:
                problems.append(_schema_abort(unit_location, "empty-unit-id"))
            if not isinstance(unit.get("payload"), dict):
                problems.append(_schema_abort(unit_location, "payload-not-dict"))
            if not isinstance(unit.get("evidence"), list):
                problems.append(_schema_abort(unit_location, "evidence-not-list"))
            if unit.get("class") not in _DISPATCH_CLASSES:
                problems.append(_schema_abort(unit_location, "bad-class"))
            if unit.get("disposition") not in _DISPATCH_DISPOSITIONS:
                problems.append(_schema_abort(unit_location, "bad-disposition"))
    return problems


def verify_seal(inputs_bytes: bytes, truth_bytes: bytes, seal: dict[str, Any]) -> list[str]:
    """Recompute raw-byte hashes and structural seal fields."""
    if not isinstance(seal, dict):
        return ["seal-mismatch:seal"]
    problems: list[str] = []
    for key in ("inputs_sha256", "truth_sha256"):
        candidate = seal.get(key)
        if not isinstance(candidate, str) or not _HEX_PATTERN.match(candidate):
            problems.append(f"seal-mismatch:{key}")
    if isinstance(inputs_bytes, bytes):
        digest = hashlib.sha256(inputs_bytes).hexdigest()
        if _hash_ok(seal.get("inputs_sha256")) and digest != seal.get("inputs_sha256"):
            problems.append("seal-mismatch:inputs_sha256")
    else:
        problems.append("seal-mismatch:inputs_sha256")
    if isinstance(truth_bytes, bytes):
        digest = hashlib.sha256(truth_bytes).hexdigest()
        if _hash_ok(seal.get("truth_sha256")) and digest != seal.get("truth_sha256"):
            problems.append("seal-mismatch:truth_sha256")
    else:
        problems.append("seal-mismatch:truth_sha256")
    if seal.get("method") != _SEAL_METHOD:
        problems.append("seal-mismatch:method")
    count_value = seal.get("n_cases")
    count_ok = isinstance(count_value, int) and not isinstance(count_value, bool)
    if isinstance(inputs_bytes, bytes):
        try:
            payload: Any = json.loads(inputs_bytes.decode("utf-8"))
            cases: Any = payload.get("cases") if isinstance(payload, dict) else None
            if not count_ok or not isinstance(cases, list) or len(cases) != count_value:
                problems.append("seal-mismatch:n_cases")
        except (UnicodeDecodeError, ValueError):
            problems.append("seal-mismatch:n_cases")
    elif not count_ok:
        problems.append("seal-mismatch:n_cases")
    for key in ("created_utc", "oracle_session"):
        candidate = seal.get(key)
        if not isinstance(candidate, str) or not candidate.strip():
            problems.append(f"seal-mismatch:{key}")
    return problems


def _resolve_seq(seal: dict[str, Any]) -> int:
    seq_value = seal.get("seal_seq", seal.get("seq"))
    if isinstance(seq_value, bool) or not isinstance(seq_value, int):
        raise ValueError("seal-mismatch:seq")
    return seq_value


def _scratch_paths_ok(scratch: bool, registry_path: str | None, seal_registry_path: str | None) -> bool:
    if not scratch:
        return True
    if registry_path is None or seal_registry_path is None:
        return False
    first = Path(registry_path).name
    second = Path(seal_registry_path).name
    return "scratch" in first and "scratch" in second


def blind_score(
    predicted: list[dict[str, Any]],
    truth: list[dict[str, Any]],
    *,
    seal: dict[str, Any],
    registry_path: str | None = None,
    seal_registry_path: str | None = None,
) -> dict[str, Any]:
    """Score once through the registry-logged path."""
    if not isinstance(seal, dict):
        raise ValueError("seal-mismatch:seal")
    inputs_hash = seal.get("inputs_sha256")
    truth_hash = seal.get("truth_sha256")
    if not isinstance(inputs_hash, str) or not _hash_ok(inputs_hash):
        raise ValueError("seal-mismatch:inputs_sha256")
    if not isinstance(truth_hash, str) or not _hash_ok(truth_hash):
        raise ValueError("seal-mismatch:truth_sha256")
    seq_value = _resolve_seq(seal)
    scratch = _is_scratch_hash(inputs_hash) or _is_scratch_hash(truth_hash)
    if not _scratch_paths_ok(scratch, registry_path, seal_registry_path):
        raise ValueError("scratch-seal-misrouted")
    if seal_registry_path is not None:
        try:
            parsed = parse_registry(seal_registry_path)
        except FileNotFoundError:
            parsed = {"entries": [], "errors": []}
        if parsed["errors"]:
            raise ValueError(str(parsed["errors"][0]))
        matched: dict[str, Any] | None = None
        for entry in parsed["entries"]:
            if (
                entry.get("inputs_sha256") == inputs_hash
                and entry.get("truth_sha256") == truth_hash
                and entry.get("seq") == seq_value
            ):
                matched = entry
                break
        if matched is None:
            raise ValueError("unregistered-seal")
        if matched.get("status") != "active":
            raise ValueError("void-seal")
    prior_same = 0
    if registry_path is not None:
        log_file = Path(registry_path)
        if log_file.exists():
            for line in log_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record: Any = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(record, dict)
                    and record.get("truth_sha256") == truth_hash
                    and record.get("verdict") != "void"
                ):
                    prior_same += 1
        if prior_same:
            raise ValueError("held-out-reuse-blocked")
    count_value = seal.get("n_cases")
    if isinstance(count_value, int) and not isinstance(count_value, bool):
        if not isinstance(truth, list) or len(truth) != count_value:
            raise ValueError("post-hoc-truth-edit")
    # Resolves in the integration tree that carries the frozen scorer.
    from mnemoseed_local.eval.experience_metrics import score_experience  # type: ignore[import-untyped]

    result = score_experience(predicted, truth)
    g_session = seal.get("g_session", "scratch-g-session" if scratch else "unknown")
    counters: dict[str, int] = {key: 0 for key in _OPS_KEYS}
    provided = seal.get("ops_counters")
    if isinstance(provided, dict):
        counters = {str(key): int(provided[key]) for key in provided}
    record_out: dict[str, Any] = {
        "v": 1,
        "inputs_sha256": inputs_hash,
        "truth_sha256": truth_hash,
        "seal_seq": seq_value,
        "g_session": str(g_session),
        "run_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worktree": str(seal.get("worktree", str(Path.cwd()))),
        "canonical_sha256": seal.get("canonical_sha256"),
        "ops_counters": counters,
        "verdict": "scored",
        "note": str(seal.get("note", "")),
    }
    registry_lines = 0
    if registry_path is not None:
        log_file = Path(registry_path)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record_out, sort_keys=True) + "\n")
        registry_lines = sum(1 for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip())
    return {
        "seal": {"inputs_sha256": inputs_hash, "truth_sha256": truth_hash, "seal_seq": seq_value},
        "score": result,
        "registry_lines": registry_lines,
    }
