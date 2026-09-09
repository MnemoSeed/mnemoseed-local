"""Tests for the blind baseline report verifier."""

from __future__ import annotations

import builtins
import copy
import hashlib
import json
import math
import os
import re
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.eval.m5prep_report_verify import verify_blind_report

EXPECTED_DIGEST = "4d97ec1c5429048fccaadd603997d52ed697a3c13aa3b28702844678961131a3"

CANONICAL_PREIMAGE: dict[str, Any] = {
    "baseline_version": 1,
    "claims": ["synthetic-only"],
    "files_read": ["inputs.json", "truth.json"],
    "isolation": {"forbidden_modules": [], "home": "<isolated-home>", "home_writes": []},
    "note": "synthetic-note",
    "ops": {"counts": {"socket.socket.connect": 0}, "guarded": ["socket.socket.connect"]},
    "score": {"fact": {"P": 1.0}},
    "sdt": {"k": "v"},
    "seal": "seal-001",
}


def make_stripped() -> dict[str, Any]:
    return {
        "baseline_version": 1,
        "claims": ["synthetic-only"],
        "files_read": ["C:/real/dir/inputs.json", "C:/real/dir/truth.json"],
        "isolation": {"forbidden_modules": [], "home": "C:/real/home-xyz", "home_writes": []},
        "note": "synthetic-note",
        "ops": {"counts": {"socket.socket.connect": 0}, "guarded": ["socket.socket.connect"]},
        "score": {"fact": {"P": 1.0}},
        "sdt": {"k": "v"},
        "seal": "seal-001",
        "started_at": "2026-01-01T00:00:00+00:00",
        "duration_s": 0.25,
        "out_path": "C:/real/out.json",
    }


def make_blind_report() -> dict[str, Any]:
    report = make_stripped()
    report["canonical_sha256"] = EXPECTED_DIGEST
    return report


def test_preimage_digest_matches_hardcoded() -> None:
    payload = json.dumps(CANONICAL_PREIMAGE, sort_keys=True).encode("utf-8")
    assert hashlib.sha256(payload).hexdigest() == EXPECTED_DIGEST


def test_happy_path_returns_empty() -> None:
    assert verify_blind_report(make_blind_report()) == []


def test_self_strip() -> None:
    good = make_blind_report()
    assert verify_blind_report(good) == []
    full_dump = json.dumps(good, sort_keys=True).encode("utf-8")
    assert hashlib.sha256(full_dump).hexdigest() != EXPECTED_DIGEST


def test_protected_score_mismatch() -> None:
    bad = make_blind_report()
    bad["score"] = {"fact": {"P": 0.5}}
    assert verify_blind_report(bad) == ["canonical-mismatch"]


def test_protected_sdt_mismatch() -> None:
    bad = make_blind_report()
    bad["sdt"] = {"k": "other"}
    assert verify_blind_report(bad) == ["canonical-mismatch"]


def test_protected_claims_mismatch() -> None:
    bad = make_blind_report()
    bad["claims"] = ["synthetic-only", "extra"]
    assert verify_blind_report(bad) == ["canonical-mismatch"]


def test_protected_seal_mismatch() -> None:
    bad = make_blind_report()
    bad["seal"] = "seal-002"
    assert verify_blind_report(bad) == ["canonical-mismatch"]


def test_protected_note_mismatch() -> None:
    bad = make_blind_report()
    bad["note"] = "other-note"
    assert verify_blind_report(bad) == ["canonical-mismatch"]


def test_protected_ops_counters_mismatch() -> None:
    bad = make_blind_report()
    bad["ops"] = {"counts": {"socket.socket.connect": 1}, "guarded": ["socket.socket.connect"]}
    assert verify_blind_report(bad) == ["canonical-mismatch"]


def test_protected_home_writes_mismatch() -> None:
    bad = make_blind_report()
    isolation = dict(bad["isolation"])
    isolation["home_writes"] = ["new-file"]
    bad["isolation"] = isolation
    assert verify_blind_report(bad) == ["canonical-mismatch"]


def test_protected_key_insert_mismatch() -> None:
    bad = make_blind_report()
    bad["extra_key"] = 1
    assert verify_blind_report(bad) == ["canonical-mismatch"]


def test_protected_key_delete_mismatch() -> None:
    bad = make_blind_report()
    del bad["seal"]
    assert verify_blind_report(bad) == ["canonical-mismatch"]


def test_tolerated_started_at() -> None:
    good = make_blind_report()
    good["started_at"] = "2030-05-05T12:00:00+00:00"
    assert verify_blind_report(good) == []


def test_tolerated_duration_s() -> None:
    good = make_blind_report()
    good["duration_s"] = 99.99
    assert verify_blind_report(good) == []


def test_tolerated_out_path() -> None:
    good = make_blind_report()
    good["out_path"] = "D:/elsewhere/out.json"
    assert verify_blind_report(good) == []
    good2 = make_blind_report()
    good2["out_path"] = None
    assert verify_blind_report(good2) == []


def test_tolerated_isolation_home() -> None:
    good = make_blind_report()
    isolation = dict(good["isolation"])
    isolation["home"] = "D:/totally/different/home"
    good["isolation"] = isolation
    assert verify_blind_report(good) == []


def test_tolerated_files_read_prefix() -> None:
    good = make_blind_report()
    good["files_read"] = ["D:/other/place/inputs.json", "D:/other/place/truth.json"]
    assert verify_blind_report(good) == []
    bare = make_blind_report()
    bare["files_read"] = ["inputs.json", "truth.json"]
    assert verify_blind_report(bare) == []


def test_tolerated_files_read_os_native_prefix() -> None:
    prefix = os.path.join("some", "nested", "dir")
    good = make_blind_report()
    good["files_read"] = [
        os.path.join(prefix, "inputs.json"),
        os.path.join(prefix, "truth.json"),
    ]
    assert verify_blind_report(good) == []


def test_tolerated_key_order() -> None:
    good = make_blind_report()
    reordered: dict[str, Any] = {}
    for key in reversed(list(good.keys())):
        reordered[key] = good[key]
    assert verify_blind_report(reordered) == []


def test_no_caller_mutation() -> None:
    good = make_blind_report()
    snapshot = copy.deepcopy(good)
    assert verify_blind_report(good) == []
    assert good == snapshot
    assert "canonical_sha256" in good


def test_non_dict() -> None:
    assert verify_blind_report(None) == ["report-not-mapping"]
    assert verify_blind_report([]) == ["report-not-mapping"]
    assert verify_blind_report("report") == ["report-not-mapping"]
    assert verify_blind_report(42) == ["report-not-mapping"]


def test_missing_key() -> None:
    assert verify_blind_report(make_stripped()) == ["report-missing-key:canonical_sha256"]
    assert verify_blind_report({}) == ["report-missing-key:canonical_sha256"]


def test_bad_canonical_field() -> None:
    for bad_value in (123, None, "", "short", "g" * 64, EXPECTED_DIGEST.upper(), "0x" + EXPECTED_DIGEST):
        bad = make_blind_report()
        bad["canonical_sha256"] = bad_value
        assert verify_blind_report(bad) == ["report-bad-canonical-field"]
    bad = make_blind_report()
    bad["canonical_sha256"] = EXPECTED_DIGEST + "extra"
    assert verify_blind_report(bad) == ["report-bad-canonical-field"]


def test_files_read_bare_string() -> None:
    bad = make_blind_report()
    bad["files_read"] = "inputs.json"
    assert verify_blind_report(bad) == ["canonical-malformed:files_read"]


def test_files_read_list_non_str() -> None:
    for bad_value in (["inputs.json", 1], [None], ["ok", None], "not-a-list"):
        bad = make_blind_report()
        bad["files_read"] = bad_value
        assert verify_blind_report(bad) == ["canonical-malformed:files_read"]


def test_unserializable_set() -> None:
    bad = make_blind_report()
    bad["sdt"] = {"k": {1, 2}}
    assert verify_blind_report(bad) == ["canonical-unserializable"]


def test_unserializable_cycle() -> None:
    bad = make_blind_report()
    bad["loop"] = bad
    try:
        assert verify_blind_report(bad) == ["canonical-unserializable"]
    finally:
        del bad["loop"]


def test_unserializable_nan() -> None:
    bad = make_blind_report()
    bad["score"] = {"fact": {"P": math.nan}}
    assert verify_blind_report(bad) == ["canonical-unserializable"]


def test_unserializable_pos_inf() -> None:
    bad = make_blind_report()
    bad["score"] = {"fact": {"P": math.inf}}
    assert verify_blind_report(bad) == ["canonical-unserializable"]


def test_unserializable_neg_inf() -> None:
    bad = make_blind_report()
    bad["score"] = {"fact": {"P": -math.inf}}
    assert verify_blind_report(bad) == ["canonical-unserializable"]


def _module_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "src" / "mnemoseed_local" / "eval" / "m5prep_report_verify.py").read_text(encoding="utf-8")


def test_source_imports_and_no_io() -> None:
    text = _module_source()
    assert "import json" in text
    assert "import re" in text
    assert "canonical_sha256" in text
    assert "experience_baseline" in text
    assert "dict(report)" in text
    assert 'pop("canonical_sha256"' in text or "pop('canonical_sha256'" in text
    assert "allow_nan" in text
    assert "fullmatch" in text
    assert "[0-9a-f]{64}" in text
    for banned in ("open(", "socket", "subprocess", "urllib", "http.client"):
        assert banned not in text
    for banned in ("parse_experience_raw", "score_experience", "run_baseline", "canonical_baseline"):
        assert banned not in text
    assert "CANONICAL_" not in text
    assert "deepcopy" not in text
    assert "report.pop" not in text
    assert re.search(r"^import\s+", text, re.M) is not None
    imports = [line.strip() for line in text.splitlines() if line.strip().startswith(("import ", "from "))]
    assert len(imports) == 3


def test_source_no_model_scoring() -> None:
    text = _module_source().lower()
    assert "model" not in text
    assert "scor" not in text
    assert "provider" not in text
    assert "daemon" not in text


def test_no_io_guards(monkeypatch: Any) -> None:
    def _deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("I/O must not be used")

    monkeypatch.setattr(builtins, "open", _deny)
    monkeypatch.setattr(socket, "socket", _deny)
    monkeypatch.setattr(subprocess, "Popen", _deny)
    monkeypatch.setattr(subprocess, "run", _deny)
    assert verify_blind_report(make_blind_report()) == []


def _run_shared_contract(verifier: Callable[[object], list[str]]) -> None:
    good = make_blind_report()
    assert verifier(good) == []
    bad_score = make_blind_report()
    bad_score["score"] = {"fact": {"P": 0.0}}
    assert verifier(bad_score) == ["canonical-mismatch"]
    assert verifier(None) == ["report-not-mapping"]
    assert verifier(make_stripped()) == ["report-missing-key:canonical_sha256"]
    bad_canonical = make_blind_report()
    bad_canonical["canonical_sha256"] = "short"
    assert verifier(bad_canonical) == ["report-bad-canonical-field"]
    bad_files = make_blind_report()
    bad_files["files_read"] = "inputs.json"
    assert verifier(bad_files) == ["canonical-malformed:files_read"]
    bad_unserializable = make_blind_report()
    bad_unserializable["sdt"] = {"k": {1, 2}}
    assert verifier(bad_unserializable) == ["canonical-unserializable"]


def test_mutation_oracle_always_empty_false_accept() -> None:
    def always_empty(_report: object) -> list[str]:
        return []

    _run_shared_contract(verify_blind_report)
    with pytest.raises(AssertionError):
        _run_shared_contract(always_empty)
    bad = make_blind_report()
    bad["score"] = {"fact": {"P": 0.0}}
    assert always_empty(bad) == []
    assert always_empty(bad) != ["canonical-mismatch"]


def test_mutation_oracle_not_stripping_rejected() -> None:
    def no_strip(report: object) -> list[str]:
        assert isinstance(report, dict)
        direct = hashlib.sha256(json.dumps(report, sort_keys=True).encode("utf-8")).hexdigest()
        return [] if direct == EXPECTED_DIGEST else ["canonical-mismatch"]

    _run_shared_contract(verify_blind_report)
    with pytest.raises(AssertionError):
        _run_shared_contract(no_strip)
    good = make_blind_report()
    assert no_strip(good) == ["canonical-mismatch"]
    assert no_strip(good) != []


def test_mutation_oracle_inplace_mutation_rejected() -> None:
    def inplace_mutant(report: object) -> list[str]:
        assert isinstance(report, dict)
        report.pop("canonical_sha256")
        return []

    victim = make_blind_report()
    snapshot = copy.deepcopy(victim)
    assert verify_blind_report(victim) == []
    assert victim == snapshot
    mutant_victim = make_blind_report()
    mutant_snapshot = copy.deepcopy(mutant_victim)
    assert inplace_mutant(mutant_victim) == []
    assert mutant_victim != mutant_snapshot
    assert "canonical_sha256" not in mutant_victim
    with pytest.raises(AssertionError):
        probe = make_blind_report()
        before = copy.deepcopy(probe)
        inplace_mutant(probe)
        assert probe == before
