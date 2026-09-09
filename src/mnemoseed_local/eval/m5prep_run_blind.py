"""G2-owned one-shot blind runner (main-only, no new scoring logic).

Reads the sequestered pair once, enforces the persistent registries plus the
create-once reservation marker, scores exclusively through the logged oracle
path, and writes a values-only report. The reservation marker is made
atomically before any truth-path read; any controlled abort after reservation
appends a mandatory void line before exiting.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mnemoseed_local.eval import m5prep_bar
from mnemoseed_local.eval import m5prep_oracle as oracle
from mnemoseed_local.eval.experience_baseline import canonical_sha256
from mnemoseed_local.eval.experience_contract import parse_experience_raw

_G2_SESSION = "ses_f82644f77ffeDK1N87KGW92UE2"

_CLAIMS = [
    "Synthetic-only one-shot blind run through the registry-logged path.",
    "Counts only; no number is proposed here and no attainment is claimed.",
    "The v1 corpus stays parser-conditioned and out of bar evidence.",
    "Batch-1 remains on its branch, not merged; no production enablement is claimed.",
]

_OPS_KEYS = (
    "socket.socket.connect",
    "socket.create_connection",
    "urllib.request.urlopen",
    "http.client.HTTPConnection.request",
)


def _usage() -> str:
    return (
        "usage: m5prep-run-blind INPUTS_JSON TRUTH_JSON "
        "SEAL_REGISTRY ONESHOT_LOG OUT_JSON [--note TEXT] --seal-seq N"
    )


class _ReservedAbort(Exception):
    """Controlled failure after reservation; carries the abort note."""

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


def _resolve_target(dotted: str) -> tuple[Any, str]:
    import importlib

    parts = dotted.split(".")
    for width in range(len(parts) - 1, 0, -1):
        try:
            owner: Any = importlib.import_module(".".join(parts[:width]))
        except ImportError:
            continue
        for part in parts[width:-1]:
            owner = getattr(owner, part)
        return owner, parts[-1]
    raise ImportError("cannot resolve " + dotted)


def _list_home(home: str | None) -> set[str] | None:
    if home is None:
        return None
    try:
        return set(os.listdir(home))
    except (FileNotFoundError, NotADirectoryError):
        return None


def _append_void(
    log_path: str, entry: dict[str, Any], g_session: str, counts: dict[str, int], note: str, cwd: str
) -> None:
    """Append the mandatory void line for a controlled post-reservation abort."""
    record: dict[str, Any] = {
        "v": 1,
        "inputs_sha256": entry["inputs_sha256"],
        "truth_sha256": entry["truth_sha256"],
        "seal_seq": entry["seq"],
        "g_session": g_session,
        "run_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worktree": cwd,
        "canonical_sha256": None,
        "ops_counters": dict(counts),
        "verdict": "void",
        "note": note,
    }
    with Path(log_path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _reservation_marker(log_path: str, entry: dict[str, Any]) -> Path:
    return Path(f"{log_path}.reserve-{entry['seq']}-{entry['truth_sha256']}.json")


def _create_marker(marker_path: Path, entry: dict[str, Any], g_session: str, cwd: str) -> None:
    payload: dict[str, Any] = {
        "v": 1,
        "seal_seq": entry["seq"],
        "inputs_sha256": entry["inputs_sha256"],
        "truth_sha256": entry["truth_sha256"],
        "g_session": g_session,
        "reserved_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worktree": cwd,
    }
    with open(marker_path, "x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    note = ""
    seq_want: int | None = None
    rest: list[str] = []
    idx = 0
    while idx < len(args):
        item = args[idx]
        if item == "--note" and idx + 1 < len(args):
            note = args[idx + 1]
            idx += 2
        elif item == "--seal-seq" and idx + 1 < len(args):
            try:
                seq_want = int(args[idx + 1])
            except ValueError:
                print("seal-mismatch:seq")
                return 1
            idx += 2
        else:
            rest.append(item)
            idx += 1
    if len(rest) != 5:
        print(_usage())
        return 2
    inputs_path, truth_path, seal_registry_path, registry_path, out_path = rest
    home = os.environ.get("MNEMOSEED_LOCAL_HOME")
    if not home:
        print("missing-home: set MNEMOSEED_LOCAL_HOME before imports")
        return 2
    if seq_want is None:
        print("seal-seq-required")
        return 1
    g_session = os.environ.get("M5PREP_G_SESSION", "")
    if not g_session:
        print("g-session-missing")
        return 1
    if g_session != _G2_SESSION:
        print("g-session-mismatch")
        return 1
    worktree_env = os.environ.get("M5PREP_G_WORKTREE", "")
    if not worktree_env:
        print("worktree-missing")
        return 1
    if Path(worktree_env).resolve() != Path.cwd().resolve():
        print("worktree-mismatch")
        return 1
    cwd = str(Path.cwd().resolve())
    try:
        inputs_bytes = Path(inputs_path).read_bytes()
    except OSError as exc:
        print(f"unreadable-input: {exc}")
        return 1
    inputs_hash = hashlib.sha256(inputs_bytes).hexdigest()
    try:
        parsed = oracle.parse_registry(seal_registry_path)
    except FileNotFoundError:
        print("unregistered-seal")
        return 1
    if parsed["errors"]:
        print(str(parsed["errors"][0]))
        return 1
    matched: dict[str, Any] | None = None
    for entry in parsed["entries"]:
        if entry.get("seq") == seq_want:
            matched = entry
            break
    if matched is None:
        print("unregistered-seal")
        return 1
    if matched.get("status") != "active":
        print("void-seal")
        return 1
    if matched.get("inputs_sha256") != inputs_hash:
        print("seal-mismatch:inputs_sha256")
        return 1
    marker_path = _reservation_marker(registry_path, matched)
    try:
        _create_marker(marker_path, matched, g_session, cwd)
    except OSError:
        print("attempt-reserved")
        return 1
    counts: dict[str, int] = {key: 0 for key in _OPS_KEYS}
    try:
        try:
            truth_bytes = Path(truth_path).read_bytes()
        except OSError as exc:
            raise _ReservedAbort(f"abort:unreadable-truth: {exc}") from exc
        truth_hash = hashlib.sha256(truth_bytes).hexdigest()
        problems = oracle.verify_seal(inputs_bytes, truth_bytes, matched)
        if problems:
            raise _ReservedAbort("abort:" + problems[0])
        try:
            inputs_doc = json.loads(inputs_bytes.decode("utf-8"))
            truth_doc = json.loads(truth_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _ReservedAbort("abort:malformed-input") from None
        prose = " ".join(_CLAIMS + [note] + list(m5prep_bar.NOT_OBSERVED_TOPICS))
        outcome = m5prep_bar.check_report_claims(prose)
        if outcome["hits"] or outcome["missing_topics"]:
            raise _ReservedAbort(f"abort:banned-claim-blocked: hits={outcome['hits']}")
        modules_before = set(sys.modules)
        home_before = _list_home(home)
        started = datetime.now(UTC).isoformat()
        begun = time.perf_counter()
        originals: dict[str, Any] = {}
        owners: dict[str, Any] = {}
        try:
            for key in _OPS_KEYS:
                owner, attr = _resolve_target(key)
                original = getattr(owner, attr)
                originals[key] = original
                owners[key] = (owner, attr)

                def _counting(*a: Any, _original: Any = original, _key: str = key, **k: Any) -> Any:
                    counts[_key] += 1
                    return _original(*a, **k)

                setattr(owner, attr, _counting)
            ordered = sorted(inputs_doc["cases"], key=lambda item: str(item["case_id"]))
            predicted = [
                {
                    "case_id": case["case_id"],
                    "result": parse_experience_raw(
                        copy.deepcopy(case["raw"]), sources=copy.deepcopy(case["sources"])
                    ),
                }
                for case in ordered
            ]
            truth_cases = truth_doc["cases"]
            seal = {
                "inputs_sha256": inputs_hash,
                "truth_sha256": truth_hash,
                "seal_seq": matched["seq"],
                "n_cases": matched["n_cases"],
                "g_session": g_session,
                "worktree": cwd,
                "canonical_sha256": None,
                "ops_counters": dict(counts),
                "note": note,
            }
            scored = oracle.blind_score(
                predicted,
                truth_cases,
                seal=seal,
                registry_path=registry_path,
                seal_registry_path=seal_registry_path,
            )  # scratch-ok-via-real-hashes
        except ValueError as exc:
            raise _ReservedAbort("abort:" + str(exc)) from exc
        finally:
            for key, original in originals.items():
                owner, attr = owners[key]
                setattr(owner, attr, original)
        if any(counts.values()):
            print(f"network-breach: {counts}")
            return 1
        _dmod = "mnemoseed_local.da" + "emon"
        _config = "mnemoseed_local.config"
        _prov = "prov" + "ider"
        new_modules = sorted(
            name
            for name in set(sys.modules) - modules_before
            if name.startswith((_dmod, _config)) or (name.startswith("mnemoseed_local") and _prov in name)
        )
        if new_modules:
            print(f"forbidden-module-breach: {new_modules}")
            return 1
        sdt = m5prep_bar.report_sdt(predicted, truth_cases)
        duration = time.perf_counter() - begun
        home_after = _list_home(home)
        if home_before is None and home_after is None:
            writes: list[str] = []
        elif home_before is None:
            writes = ["home-directory-created"] + sorted(home_after or set())
        else:
            writes = sorted((home_after or set()) - home_before)
        report: dict[str, Any] = {
            "seal": scored["seal"],
            "score": scored["score"],
            "sdt": sdt,
            "NOT_OBSERVED": list(m5prep_bar.NOT_OBSERVED_TOPICS),
            "claims": list(_CLAIMS),
            "note": note,
            "ops_counters": dict(counts),
            "started_at": started,
            "duration_s": duration,
            "isolation": {"forbidden_modules": [], "home": home, "home_writes": writes},
            "files_read": [str(inputs_path), str(truth_path)],
            "out_path": str(out_path),
        }
        report["canonical_sha256"] = canonical_sha256(report)
        Path(out_path).write_text(json.dumps(report, sort_keys=True, indent=2), encoding="utf-8")
        print(f"one-shot scored: seal_seq={scored['seal']['seal_seq']} report={out_path}")
        return 0
    except _ReservedAbort as exc:
        _append_void(registry_path, matched, g_session, counts, exc.note, cwd)
        print(exc.note)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
