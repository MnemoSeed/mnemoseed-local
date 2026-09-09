"""O-lane oracle tests: seal integrity, normalizer, scratch one-shot, isolation, corpus shape."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from mnemoseed_local.eval import m5prep_oracle as oracle

ROOT = Path(__file__).resolve().parents[1]
ORACLE_SRC = ROOT / "src" / "mnemoseed_local" / "eval" / "m5prep_oracle.py"
DEV_INPUTS = ROOT / "tests" / "fixtures" / "p008_m5prep" / "dev_inputs.json"
SEQ_INPUTS = ROOT / "tests" / "fixtures" / "p008_m5prep" / "sequestered_inputs.json"
SEQ_TRUTH = ROOT / "tests" / "fixtures" / "p008_m5prep" / "sequestered_truth.json"

FORBIDDEN_TREE_PATHS = [
    "src/mnemoseed_local/eval/experience_common.py",
    "src/mnemoseed_local/eval/experience_contract.py",
    "src/mnemoseed_local/eval/experience_metrics.py",
    "src/mnemoseed_local/eval/experience_baseline.py",
    "tests/test_p008_experience_common.py",
    "tests/test_p008_extraction_contract.py",
    "tests/test_p008_experience_metrics.py",
    "tests/test_p008_baseline.py",
    "tests/fixtures/p008_golden",
]

DEV_EXPECTED = {
    "dv-c01": {"category": "fact", "expected_status": "accepted", "zero_reason": None},
    "dv-c02": {"category": "fact", "expected_status": "accepted", "zero_reason": None},
    "dv-c03": {"category": "fact", "expected_status": "accepted", "zero_reason": None},
    "dv-c04": {"category": "lesson", "expected_status": "accepted", "zero_reason": None},
    "dv-c05": {"category": "lesson", "expected_status": "unresolved", "zero_reason": "all-filtered"},
    "dv-c06": {"category": "lesson", "expected_status": "unresolved", "zero_reason": "all-filtered"},
    "dv-c07": {"category": "lesson", "expected_status": "accepted", "zero_reason": None},
    "dv-c08": {"category": "intention", "expected_status": "accepted", "zero_reason": None},
    "dv-c09": {"category": "intention", "expected_status": "accepted", "zero_reason": None},
    "dv-c10": {"category": "skill_sequence", "expected_status": "accepted", "zero_reason": None},
    "dv-c11": {"category": "skill_sequence", "expected_status": "accepted", "zero_reason": None},
    "dv-c12": {"category": "noise", "expected_status": "unresolved", "zero_reason": "no-match"},
    "dv-c13": {"category": "noise", "expected_status": "unresolved", "zero_reason": "malformed-input"},
}

SCRATCH_SUFFIX = "aa0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab"


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _scratch_files(tmp_path: Path, stem: str) -> tuple[Path, Path]:
    seal = tmp_path / f"{stem}-scratch-seal-registry.jsonl"
    log = tmp_path / f"{stem}-scratch-oneshot-log.jsonl"
    return seal, log


def _scratch_seal(inputs_h: str, truth_h: str, n: int, seq: int = 1) -> dict:
    return {
        "seq": seq,
        "inputs_sha256": inputs_h,
        "truth_sha256": truth_h,
        "n_cases": n,
        "created_utc": "2026-09-07T00:00:00Z",
        "oracle_session": "scratch-oracle",
        "registrar_session": "scratch-registrar",
        "method": "sha256-raw-bytes-lowercase-hex",
        "status": "active",
    }


def _write_registry(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


@pytest.fixture()
def stub_scorer(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: list[tuple[int, int]] = []

    def fake(predicted: list, truth: list) -> dict:
        calls.append((len(predicted), len(truth)))
        return {"n_predicted": len(predicted), "n_truth": len(truth), "marker": "stub-only"}

    name = "score" + "_experience"
    dotted = "mnemoseed_local.eval.experience_metrics"
    stub = ModuleType(dotted)
    setattr(stub, name, fake)
    monkeypatch.setitem(sys.modules, dotted, stub)
    return {"calls": calls}


class TestNormalizeOracle:
    def test_collapse_and_casefold(self) -> None:
        assert oracle.normalize_oracle("  Hello   WORLD  ") == "hello world"

    def test_single_trailing_mark_only(self) -> None:
        assert oracle.normalize_oracle("done.") == "done"
        assert oracle.normalize_oracle("done!!") == "done!"
        assert oracle.normalize_oracle("really?!") == "really?"

    def test_chinese_sentence_punct(self) -> None:
        assert oracle.normalize_oracle("完成。") == "完成"
        assert oracle.normalize_oracle("好了！！") == "好了!"

    def test_preserve_internal_tokens(self) -> None:
        assert oracle.normalize_oracle("C++ guide") == "c++ guide"
        assert oracle.normalize_oracle("C# note") == "c# note"
        assert oracle.normalize_oracle("don't stop") == "don't stop"
        assert oracle.normalize_oracle("mix salt, pepper; stir") == "mix salt, pepper; stir"

    def test_scope_identifier_not_normalized(self) -> None:
        # The text normalizer never mangles scope identifiers (a separate envelope field).
        assert oracle.normalize_oracle("mq-home") == "mq-home"
        assert oracle.normalize_oracle("mq-lab-07") == "mq-lab-07"

    def test_non_string_rejected(self) -> None:
        with pytest.raises(TypeError):
            oracle.normalize_oracle(None)  # type: ignore[arg-type]

    def test_blank_becomes_empty(self) -> None:
        assert oracle.normalize_oracle("   ") == ""


class TestParseRegistry:
    def _entry(self, seq: int = 1, status: str = "active") -> dict:
        return {
            "status": status,
            "n_cases": 3,
            "method": "sha256-raw-bytes-lowercase-hex",
            "created_utc": "2026-09-07T00:00:00Z",
            "oracle_session": "o",
            "registrar_session": "r",
            "seq": seq,
            "inputs_sha256": _hex(f"in-{seq}"),
            "truth_sha256": _hex(f"truth-{seq}"),
        }

    def test_key_order_irrelevant(self, tmp_path: Path) -> None:
        entry = self._entry()
        shuffled = {k: entry[k] for k in reversed(list(entry.keys()))}
        path = tmp_path / "registry.jsonl"
        path.write_text(json.dumps(shuffled) + "\n", encoding="utf-8")
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == []
        assert len(parsed["entries"]) == 1

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.jsonl"
        path.write_text("\n" + json.dumps(self._entry()) + "\n\n", encoding="utf-8")
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == []
        assert len(parsed["entries"]) == 1

    def test_malformed_line(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.jsonl"
        path.write_text("{not json\n", encoding="utf-8")
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["malformed-line"]

    def test_non_object_line(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.jsonl"
        path.write_text("[1, 2]\n", encoding="utf-8")
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["malformed-line"]

    def test_missing_field(self, tmp_path: Path) -> None:
        entry = self._entry()
        del entry["registrar_session"]
        path = tmp_path / "registry.jsonl"
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["missing-field"]

    def test_bad_hex(self, tmp_path: Path) -> None:
        entry = self._entry()
        entry["truth_sha256"] = "xyz"
        path = tmp_path / "registry.jsonl"
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["bad-hex"]

    def test_uppercase_hex(self, tmp_path: Path) -> None:
        entry = self._entry()
        entry["truth_sha256"] = _hex("truth-1").upper()
        path = tmp_path / "registry.jsonl"
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["uppercase-hex"]

    def test_unknown_method(self, tmp_path: Path) -> None:
        entry = self._entry()
        entry["method"] = "git-hash-object"
        path = tmp_path / "registry.jsonl"
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["unknown-method"]

    def test_bad_status(self, tmp_path: Path) -> None:
        entry = self._entry(status="pending")
        path = tmp_path / "registry.jsonl"
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["bad-status"]

    def test_duplicate_seq(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [self._entry(seq=1), self._entry(seq=1)])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["duplicate-seq"]

    def test_duplicate_truth_hash_active(self, tmp_path: Path) -> None:
        first = self._entry(seq=1)
        second = self._entry(seq=2)
        second["truth_sha256"] = first["truth_sha256"]
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [first, second])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["duplicate-truth-hash"]

    def test_void_duplicate_truth_allowed(self, tmp_path: Path) -> None:
        first = self._entry(seq=1)
        second = self._entry(seq=2, status="void")
        second["truth_sha256"] = first["truth_sha256"]
        second["void_reason"] = "batch-rollback"
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [first, second])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == []

    def test_void_needs_reason(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [self._entry(status="void")])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["missing-field"]

    def test_scratch_prefix_accepted(self, tmp_path: Path) -> None:
        entry = self._entry()
        entry["inputs_sha256"] = "scratch-" + _hex("scratch-inputs")
        entry["truth_sha256"] = "scratch-" + _hex("other")
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [entry])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == []


class TestParseRegistryA2:
    """A2 same-seq active-to-void transition semantics (amendment dispatch-m5prep)."""

    def _active(self, seq: int = 1, truth_sha: str | None = None) -> dict:
        return {
            "status": "active",
            "n_cases": 3,
            "method": "sha256-raw-bytes-lowercase-hex",
            "created_utc": "2026-09-07T00:00:00Z",
            "oracle_session": "o",
            "registrar_session": "r",
            "seq": seq,
            "inputs_sha256": _hex(f"in-{seq}"),
            "truth_sha256": truth_sha if truth_sha is not None else _hex(f"truth-{seq}"),
        }

    def _void(
        self,
        active: dict,
        *,
        reason: str = "corpus-schema-mismatch",
        with_utc: bool = True,
        with_session: bool = True,
        drift: dict | None = None,
    ) -> dict:
        out = dict(active)
        out["status"] = "void"
        out["void_reason"] = reason
        if with_utc:
            out["voided_utc"] = "2026-09-07T12:00:00Z"
        if with_session:
            out["voided_by_session"] = "r2"
        if drift:
            out.update(drift)
        return out

    def test_matching_transition_is_effective_void(self, tmp_path: Path) -> None:
        active = self._active(1)
        void_row = self._void(active)
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [active, void_row])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == []
        assert len(parsed["entries"]) == 1
        assert parsed["entries"][0]["status"] == "void"
        assert parsed["entries"][0]["void_reason"] == "corpus-schema-mismatch"

    def test_transition_void_requires_full_fields(self, tmp_path: Path) -> None:
        active = self._active(1)
        void_row = self._void(active, with_utc=False)
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [active, void_row])
        parsed = oracle.parse_registry(str(path))
        assert "missing-field" in parsed["errors"]

    def test_transition_no_session_rejected(self, tmp_path: Path) -> None:
        active = self._active(1)
        void_row = self._void(active, with_session=False)
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [active, void_row])
        parsed = oracle.parse_registry(str(path))
        assert "missing-field" in parsed["errors"]

    def test_transition_identity_drift_rejected(self, tmp_path: Path) -> None:
        active = self._active(1)
        void_row = self._void(active, drift={"inputs_sha256": _hex("in-drift")})
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [active, void_row])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["void-transition-mismatch"]

    def test_transition_session_drift_rejected(self, tmp_path: Path) -> None:
        active = self._active(1)
        void_row = self._void(active, drift={"registrar_session": "other"})
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [active, void_row])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["void-transition-mismatch"]

    def test_active_after_active_duplicate(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [self._active(1), self._active(1)])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["duplicate-seq"]

    def test_void_after_void_duplicate(self, tmp_path: Path) -> None:
        active = self._active(1)
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [self._void(active), self._void(active)])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["duplicate-seq"]

    def test_second_row_after_effective_void_duplicate(self, tmp_path: Path) -> None:
        active = self._active(1)
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [active, self._void(active), self._void(active)])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["duplicate-seq"]

    def test_standalone_void_remains_valid(self, tmp_path: Path) -> None:
        void_row = self._void(self._active(1), with_utc=False, with_session=False)
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [void_row])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == []
        assert len(parsed["entries"]) == 1
        assert parsed["entries"][0]["status"] == "void"

    def test_standalone_void_plus_transition_duplicate(self, tmp_path: Path) -> None:
        void_row = self._void(self._active(1), with_utc=False, with_session=False)
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [void_row, self._active(1)])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == ["duplicate-seq"]

    def test_active_truth_checked_on_effective_entries(self, tmp_path: Path) -> None:
        active = self._active(1)
        void_row = self._void(active)
        active2 = self._active(2, truth_sha=active["truth_sha256"])
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [active, void_row, active2])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == []
        assert len(parsed["entries"]) == 2

    def test_transition_then_seq2_effective_count(self, tmp_path: Path) -> None:
        active = self._active(1)
        void_row = self._void(active)
        active2 = self._active(2)
        path = tmp_path / "registry.jsonl"
        _write_registry(path, [active, void_row, active2])
        parsed = oracle.parse_registry(str(path))
        assert parsed["errors"] == []
        assert len(parsed["entries"]) == 2
        assert sorted(e["status"] for e in parsed["entries"]) == ["active", "void"]


class TestVerifySeal:
    def _seal_for(self, inputs: bytes, truth: bytes, n: int) -> dict:
        return {
            "inputs_sha256": hashlib.sha256(inputs).hexdigest(),
            "truth_sha256": hashlib.sha256(truth).hexdigest(),
            "n_cases": n,
            "method": "sha256-raw-bytes-lowercase-hex",
            "created_utc": "2026-09-07T00:00:00Z",
            "oracle_session": "scratch-oracle",
        }

    def test_success(self) -> None:
        inputs = b'{"version": 1, "cases": []}'
        truth = b'{"version": 1, "cases": []}'
        assert oracle.verify_seal(inputs, truth, self._seal_for(inputs, truth, 0)) == []

    def test_tamper_fails(self) -> None:
        inputs = b'{"version": 1, "cases": []}'
        truth = b'{"version": 1, "cases": []}'
        seal = self._seal_for(inputs, truth, 0)
        tampered = b'{"version": 1, "cases": [1]}'
        assert "seal-mismatch:inputs_sha256" in oracle.verify_seal(tampered, truth, seal)
        assert "seal-mismatch:truth_sha256" in oracle.verify_seal(inputs, truth + b" ", seal)

    def test_count_mismatch(self) -> None:
        inputs = b'{"version": 1, "cases": [{"case_id": "x"}]}'
        truth = b'{"version": 1, "cases": []}'
        seal = self._seal_for(inputs, truth, 9)
        assert "seal-mismatch:n_cases" in oracle.verify_seal(inputs, truth, seal)

    def test_method_mismatch(self) -> None:
        inputs = b'{"version": 1, "cases": []}'
        truth = b'{"version": 1, "cases": []}'
        seal = self._seal_for(inputs, truth, 0)
        seal["method"] = "git-hash-object"
        assert "seal-mismatch:method" in oracle.verify_seal(inputs, truth, seal)

    def test_empty_session_rejected(self) -> None:
        inputs = b'{"version": 1, "cases": []}'
        truth = b'{"version": 1, "cases": []}'
        seal = self._seal_for(inputs, truth, 0)
        seal["oracle_session"] = "  "
        problems = oracle.verify_seal(inputs, truth, seal)
        assert "seal-mismatch:oracle_session" in problems

    def test_uppercase_hash_rejected(self) -> None:
        inputs = b'{"version": 1, "cases": []}'
        truth = b'{"version": 1, "cases": []}'
        seal = self._seal_for(inputs, truth, 0)
        seal["inputs_sha256"] = seal["inputs_sha256"].upper()
        assert "seal-mismatch:inputs_sha256" in oracle.verify_seal(inputs, truth, seal)


class TestBlindScoreScratch:
    def test_success_appends_log(self, tmp_path: Path, stub_scorer: dict) -> None:
        ih, th = "scratch-" + _hex("ih-1"), "scratch-" + _hex("th-1")
        sr, rl = _scratch_files(tmp_path, "ok")
        _write_registry(sr, [_scratch_seal(ih, th, 1)])
        p = [{"case_id": "s-c1", "result": {"status": "accepted"}}]
        t = [{"case_id": "s-c1", "category": "fact"}]
        s = _scratch_seal(ih, th, 1)
        out = oracle.blind_score(p, t, seal=s, registry_path=str(rl), seal_registry_path=str(sr))  # scratch
        assert set(out) == {"seal", "score", "registry_lines"}
        assert out["seal"] == {"inputs_sha256": ih, "truth_sha256": th, "seal_seq": 1}
        assert out["score"]["marker"] == "stub-only"
        assert out["registry_lines"] == 1
        line = json.loads(rl.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert line["v"] == 1 and line["verdict"] == "scored" and line["seal_seq"] == 1
        assert line["run_utc"].endswith("Z") and line["canonical_sha256"] is None
        assert line["ops_counters"] == {
            "socket.socket.connect": 0,
            "socket.create_connection": 0,
            "urllib.request.urlopen": 0,
            "http.client.HTTPConnection.request": 0,
        }

    def test_reuse_blocked(self, tmp_path: Path, stub_scorer: dict) -> None:
        ih, th = "scratch-" + _hex("ih-2"), "scratch-" + _hex("th-2")
        sr, rl = _scratch_files(tmp_path, "reuse")
        _write_registry(sr, [_scratch_seal(ih, th, 1)])
        p = [{"case_id": "s-c1", "result": {"status": "accepted"}}]
        t = [{"case_id": "s-c1", "category": "fact"}]
        s = _scratch_seal(ih, th, 1)
        oracle.blind_score(p, t, seal=s, registry_path=str(rl), seal_registry_path=str(sr))  # scratch
        with pytest.raises(ValueError, match="held-out-reuse-blocked"):
            oracle.blind_score(p, t, seal=s, registry_path=str(rl), seal_registry_path=str(sr))  # scratch

    def test_fresh_hashes_allowed(self, tmp_path: Path, stub_scorer: dict) -> None:
        sr, rl = _scratch_files(tmp_path, "fresh")
        rp, sp = str(rl), str(sr)
        first = _scratch_seal("scratch-" + _hex("ih-a"), "scratch-" + _hex("th-a"), 1, seq=1)
        second = _scratch_seal("scratch-" + _hex("ih-b"), "scratch-" + _hex("th-b"), 1, seq=2)
        _write_registry(sr, [first, second])
        p = [{"case_id": "s-c1", "result": {"status": "accepted"}}]
        t = [{"case_id": "s-c1", "category": "fact"}]
        oracle.blind_score(p, t, seal=first, registry_path=rp, seal_registry_path=sp)  # scratch
        out = oracle.blind_score(p, t, seal=second, registry_path=rp, seal_registry_path=sp)  # scratch
        assert out["registry_lines"] == 2

    def test_truth_edit_blocked(self, tmp_path: Path, stub_scorer: dict) -> None:
        ih, th = "scratch-" + _hex("ih-3"), "scratch-" + _hex("th-3")
        sr, rl = _scratch_files(tmp_path, "edit")
        _write_registry(sr, [_scratch_seal(ih, th, 2)])
        p = [{"case_id": "s-c1", "result": {"status": "accepted"}}]
        t = [{"case_id": "s-c1", "category": "fact"}]
        s = _scratch_seal(ih, th, 2)
        with pytest.raises(ValueError, match="post-hoc-truth-edit"):
            oracle.blind_score(p, t, seal=s, registry_path=str(rl), seal_registry_path=str(sr))  # scratch

    def test_misrouted_scratch(self, tmp_path: Path, stub_scorer: dict) -> None:
        ps = tmp_path / "real-seal-registry.jsonl"
        pl = tmp_path / "real-oneshot-log.jsonl"
        s = _scratch_seal("scratch-" + _hex("ih-4"), "scratch-" + _hex("th-4"), 1)
        with pytest.raises(ValueError, match="scratch-seal-misrouted"):
            oracle.blind_score([], [], seal=s, registry_path=str(pl), seal_registry_path=str(ps))  # scratch

    def test_unregistered(self, tmp_path: Path, stub_scorer: dict) -> None:
        sr, rl = _scratch_files(tmp_path, "unreg")
        _write_registry(sr, [_scratch_seal("scratch-" + _hex("ih-x"), "scratch-" + _hex("th-x"), 1)])
        s = _scratch_seal("scratch-" + _hex("ih-5"), "scratch-" + _hex("th-5"), 1)
        with pytest.raises(ValueError, match="unregistered-seal"):
            oracle.blind_score([], [], seal=s, registry_path=str(rl), seal_registry_path=str(sr))  # scratch

    def test_void_entry(self, tmp_path: Path, stub_scorer: dict) -> None:
        ih, th = "scratch-" + _hex("ih-6"), "scratch-" + _hex("th-6")
        sr, rl = _scratch_files(tmp_path, "void")
        entry = _scratch_seal(ih, th, 1)
        entry["status"] = "void"
        entry["void_reason"] = "batch-rollback"
        _write_registry(sr, [entry])
        s = _scratch_seal(ih, th, 1)
        with pytest.raises(ValueError, match="void-seal"):
            oracle.blind_score([], [], seal=s, registry_path=str(rl), seal_registry_path=str(sr))  # scratch

    def test_registry_parse_error_aborts(self, tmp_path: Path, stub_scorer: dict) -> None:
        sr, rl = _scratch_files(tmp_path, "badreg")
        sr.write_text("{broken\n", encoding="utf-8")
        s = _scratch_seal("scratch-" + _hex("ih-7"), "scratch-" + _hex("th-7"), 0)
        with pytest.raises(ValueError, match="malformed-line"):
            oracle.blind_score([], [], seal=s, registry_path=str(rl), seal_registry_path=str(sr))  # scratch

    def test_repeatability(self, tmp_path: Path, stub_scorer: dict) -> None:
        sr, rl = _scratch_files(tmp_path, "repeat")
        rp, sp = str(rl), str(sr)
        first = _scratch_seal("scratch-" + _hex("ih-r1"), "scratch-" + _hex("th-r1"), 1, seq=1)
        second = _scratch_seal("scratch-" + _hex("ih-r2"), "scratch-" + _hex("th-r2"), 1, seq=2)
        _write_registry(sr, [first, second])
        p = [{"case_id": "s-c1", "result": {"status": "accepted"}}]
        t = [{"case_id": "s-c1", "category": "fact"}]
        a = oracle.blind_score(p, t, seal=first, registry_path=rp, seal_registry_path=sp)  # scratch
        b = oracle.blind_score(p, t, seal=second, registry_path=rp, seal_registry_path=sp)  # scratch
        assert a["score"] == b["score"]

    def test_call_sites_carry_marker(self) -> None:
        needle = "blind" + "_score("
        lines = Path(__file__).read_text(encoding="utf-8").splitlines()
        flagged = [line for line in lines if needle in line]
        assert flagged
        assert all("scratch" in line for line in flagged)


class TestCorpusShape:
    def test_owned_files_exist(self) -> None:
        for path in (ORACLE_SRC, DEV_INPUTS, SEQ_INPUTS, SEQ_TRUTH):
            assert path.is_file(), str(path)

    def test_fixture_dir_closed(self) -> None:
        names = sorted(p.name for p in DEV_INPUTS.parent.iterdir() if p.is_file())
        assert names == ["dev_inputs.json", "sequestered_inputs.json", "sequestered_truth.json"]

    def test_transferred_batch1_present_with_hash(self) -> None:
        transferred = {
            "src/mnemoseed_local/eval/experience_common.py": "71a56fb59cd1cecce7b582c82b23cfd3d99fe25e",
            "tests/test_p008_experience_common.py": "c852d60d19f0f41d94f4753e1dc5cf68c6e101a5",
            "tests/fixtures/p008_golden/README.md": "ba362880799fb4014c49673d9b3693af3265a70c",
            "src/mnemoseed_local/eval/experience_contract.py": "0a5b1fef0288509d453e1995f82de9eca83b67be",
            "tests/test_p008_extraction_contract.py": "4736d7f0d62117d04f41b0baae9ce39c9314b0e9",
            "src/mnemoseed_local/eval/experience_metrics.py": "91d08b096d441816fc6e82bad1f38cdaea3e8375",
            "src/mnemoseed_local/eval/experience_baseline.py": "81dc1ece15fcf73e18a0686ff193d7082f96d89f",
            "tests/test_p008_experience_metrics.py": "d04fbf6f9dface63522804d2002b229522a1470a",
            "tests/test_p008_baseline.py": "cf0e0b05488fbcc4fc839ec0445fab97c3193bc4",
            "tests/fixtures/p008_golden/inputs.json": "f694cb8d569688953e95b7d3f2d0d7d86b26ddd8",
            "tests/fixtures/p008_golden/truth.json": "b2a92493e1c1c3efe9fae96aa67777765803a52f",
        }
        for rel, want in transferred.items():
            path = ROOT / rel
            assert path.is_file(), rel
            data = path.read_bytes()
            # Pinned OIDs are Git text blobs (LF); undo CRLF smudge from autocrlf checkouts.
            data = data.replace(b"\r\n", b"\n")
            header = b"blob " + str(len(data)).encode("ascii") + b"\x00"
            assert hashlib.sha1(header + data).hexdigest() == want, rel

    def test_dev_envelope(self) -> None:
        doc = _read_json(DEV_INPUTS)
        assert doc["version"] == 1
        assert len(doc["cases"]) >= 12
        by_id = {c["case_id"]: c for c in doc["cases"]}
        assert len(by_id) == len(doc["cases"])
        for case in doc["cases"]:
            assert case["variant"] in {"base", "robustness"}
            assert case["case_id"].startswith("dv-")
            sources = case["sources"]
            assert isinstance(sources["run_id"], str) and sources["run_id"].startswith("dv-")
            for src in sources["sources"]:
                assert src["kind"] in {"chunk", "session", "node"}
                assert src["fixture_id"].startswith("dv-")

    def test_dev_mini_truth(self) -> None:
        doc = _read_json(DEV_INPUTS)
        by_id = {c["case_id"]: c for c in doc["cases"]}
        assert set(by_id) == set(DEV_EXPECTED)
        for want in DEV_EXPECTED.values():
            assert want["category"] in {"fact", "lesson", "intention", "skill_sequence", "noise"}
            assert want["expected_status"] in {"accepted", "unresolved"}
        first = by_id["dv-c01"]["raw"]
        assert first["payload"] == {
            "subject": "Lena",
            "predicate": "prefers",
            "object": "barley tea",
            "polarity": "positive",
        }

    def test_sequestered_counts(self) -> None:
        inputs = _read_json(SEQ_INPUTS)
        truth = _read_json(SEQ_TRUTH)
        assert inputs["version"] == 1 and truth["version"] == 1
        assert len(inputs["cases"]) == len(truth["cases"]) == 27
        tally: dict[str, int] = {}
        for case in truth["cases"]:
            if case["case_id"].endswith("-ph"):
                continue
            tally[case["category"]] = tally.get(case["category"], 0) + 1
        assert tally.get("fact", 0) >= 6
        assert tally.get("lesson", 0) >= 5
        assert tally.get("intention", 0) >= 4
        assert tally.get("skill_sequence", 0) >= 3
        assert tally.get("noise", 0) >= 6

    def test_sequestered_namespaces(self) -> None:
        inputs = _read_json(SEQ_INPUTS)
        conflict_ids: dict[str, int] = {}
        paraphrase_ids: dict[str, int] = {}
        groups = (("conflict_pair_id", conflict_ids), ("paraphrase_group_id", paraphrase_ids))
        for case in inputs["cases"]:
            raw = case["raw"]
            raws = raw if isinstance(raw, list) else [raw]
            for item in raws:
                if not isinstance(item, dict):
                    continue
                for key, store in groups:
                    if item.get(key):
                        assert item[key].startswith("mq-")
                        store[item[key]] = store.get(item[key], 0) + 1
        assert conflict_ids.get("mq-k01") == 2
        assert paraphrase_ids.get("mq-p01") == 3

    def test_case_ids_mirror(self) -> None:
        inputs = _read_json(SEQ_INPUTS)
        truth = _read_json(SEQ_TRUTH)
        assert [c["case_id"] for c in inputs["cases"]] == [c["case_id"] for c in truth["cases"]]
        for case in inputs["cases"]:
            assert case["case_id"].startswith("mq-")

    def test_evidence_resolves(self) -> None:
        inputs = _read_json(SEQ_INPUTS)
        forged = 0
        for case in inputs["cases"]:
            catalog = {(s["kind"], s["id"]) for s in case["sources"]["sources"]}
            assert catalog
            raw = case["raw"]
            raws = raw if isinstance(raw, list) else [raw]
            for item in raws:
                if not isinstance(item, dict):
                    continue
                for pointer in item.get("evidence", []):
                    if (pointer["kind"], pointer["id"]) not in catalog:
                        forged += 1
        assert forged == 1

    def test_placeholder_excluded(self) -> None:
        inputs = _read_json(SEQ_INPUTS)
        placeholders = [c for c in inputs["cases"] if c["case_id"].endswith("-ph")]
        assert len(placeholders) == 1
        haystack = json.dumps(placeholders[0])
        assert "PLACEHOLDER" in haystack and "NOT a real window stress" in haystack

    def test_no_frozen_placeholders(self) -> None:
        for path in (DEV_INPUTS, SEQ_INPUTS, SEQ_TRUTH):
            text = path.read_text(encoding="utf-8")
            for token in ("TODO", "TBD", "FIXME", "ses_", "oracle_session_id", "XXXX"):
                assert token not in text, f"{path.name} contains {token}"


def _inputs_bytes(cases: list[dict]) -> bytes:
    return json.dumps({"version": 1, "cases": cases}, sort_keys=True).encode("utf-8")


def _input_case(case_id: str = "mq-x01", variant: str = "base") -> dict:
    return {
        "case_id": case_id,
        "raw": {"candidate_id": "mq-a01", "class": "fact", "scope": "mq-x", "payload": {}, "evidence": []},
        "sources": {"run_id": "mq-run-x", "sources": []},
        "variant": variant,
    }


def _truth_bytes(cases: list[dict]) -> bytes:
    return json.dumps({"version": 1, "cases": cases}, sort_keys=True).encode("utf-8")


def _truth_case(
    case_id: str = "mq-c01",
    units: list[dict] | None = None,
    *,
    category: str = "fact",
    expected_status: str = "accepted",
    zero_reason: str | None = None,
    coercion_loss: bool = False,
    conflict: bool = False,
) -> dict:
    return {
        "case_id": case_id,
        "category": category,
        "expected_status": expected_status,
        "zero_reason": zero_reason,
        "coercion_loss": coercion_loss,
        "conflict": conflict,
        "expected_units": units if units is not None else [],
    }


def _truth_unit(
    uid: str = "mq-a01",
    *,
    cls: str = "fact",
    scope: str = "mq-s",
    payload: dict | None = None,
    evidence: list | None = None,
    disposition: str = "accepted",
) -> dict:
    return {
        "id": uid,
        "class": cls,
        "scope": scope,
        "payload": payload if payload is not None else {},
        "evidence": evidence if evidence is not None else [],
        "disposition": disposition,
    }


class TestPublicSchemaValidators:
    def test_inputs_valid_envelope(self) -> None:
        problems = oracle.validate_inputs_schema(_inputs_bytes([_input_case()]))
        assert problems == []

    def test_inputs_reject_non_json(self) -> None:
        problems = oracle.validate_inputs_schema(b"not json")
        assert any("schema-abort:root" in p for p in problems)

    def test_inputs_reject_non_object(self) -> None:
        problems = oracle.validate_inputs_schema(b"[1, 2]")
        assert any("schema-abort:root" in p for p in problems)

    def test_inputs_reject_bad_version(self) -> None:
        doc = json.dumps({"version": 2, "cases": []}).encode("utf-8")
        problems = oracle.validate_inputs_schema(doc)
        assert any("schema-abort:root" in p for p in problems)

    def test_inputs_reject_cases_not_list(self) -> None:
        doc = json.dumps({"version": 1, "cases": {}}).encode("utf-8")
        problems = oracle.validate_inputs_schema(doc)
        assert any("schema-abort:root" in p for p in problems)

    def test_inputs_duplicate_case_id(self) -> None:
        doc = _inputs_bytes([_input_case("mq-x01"), _input_case("mq-x01")])
        problems = oracle.validate_inputs_schema(doc)
        assert any("duplicate-case-id" in p for p in problems)

    def test_inputs_empty_case_id(self) -> None:
        doc = _inputs_bytes([_input_case("")])
        problems = oracle.validate_inputs_schema(doc)
        assert any("empty-case-id" in p for p in problems)

    def test_inputs_missing_case_key(self) -> None:
        case = _input_case()
        del case["variant"]
        problems = oracle.validate_inputs_schema(_inputs_bytes([case]))
        assert any("missing-key:variant" in p for p in problems)

    def test_truth_valid_envelope(self) -> None:
        doc = _truth_bytes([_truth_case(units=[_truth_unit()])])
        problems = oracle.validate_truth_schema(doc)
        assert problems == []

    def test_candidate_id_temptation_rejected(self) -> None:
        unit = _truth_unit()
        unit["candidate_id"] = unit.pop("id")
        doc = _truth_bytes([_truth_case(units=[unit])])
        problems = oracle.validate_truth_schema(doc)
        assert any("missing-key:id" in p for p in problems), problems
        assert any("unexpected-key:candidate_id" in p for p in problems), problems

    def test_truth_missing_case_key(self) -> None:
        case = _truth_case()
        del case["conflict"]
        problems = oracle.validate_truth_schema(_truth_bytes([case]))
        assert any("missing-key:conflict" in p for p in problems)

    def test_truth_duplicate_case_id(self) -> None:
        doc = _truth_bytes([_truth_case("mq-c01"), _truth_case("mq-c01")])
        problems = oracle.validate_truth_schema(doc)
        assert any("duplicate-case-id" in p for p in problems)

    def test_truth_expected_units_not_list(self) -> None:
        case = _truth_case()
        case["expected_units"] = {}
        problems = oracle.validate_truth_schema(_truth_bytes([case]))
        assert any("expected-units-not-list" in p for p in problems)

    def test_truth_empty_unit_id(self) -> None:
        doc = _truth_bytes([_truth_case(units=[_truth_unit(uid="")])])
        problems = oracle.validate_truth_schema(doc)
        assert any("empty-unit-id" in p for p in problems)

    def test_truth_payload_not_dict(self) -> None:
        unit = _truth_unit()
        unit["payload"] = "not a dict"
        doc = _truth_bytes([_truth_case(units=[unit])])
        problems = oracle.validate_truth_schema(doc)
        assert any("payload-not-dict" in p for p in problems)

    def test_truth_evidence_not_list(self) -> None:
        unit = _truth_unit()
        unit["evidence"] = {"kind": "chunk", "id": "mq-e01"}
        doc = _truth_bytes([_truth_case(units=[unit])])
        problems = oracle.validate_truth_schema(doc)
        assert any("evidence-not-list" in p for p in problems)

    def test_truth_bad_class(self) -> None:
        doc = _truth_bytes([_truth_case(units=[_truth_unit(cls="bogus")])])
        problems = oracle.validate_truth_schema(doc)
        assert any("bad-class" in p for p in problems)

    def test_truth_bad_disposition(self) -> None:
        doc = _truth_bytes([_truth_case(units=[_truth_unit(disposition="shelved")])])
        problems = oracle.validate_truth_schema(doc)
        assert any("bad-disposition" in p for p in problems)

    def test_truth_bad_expected_status(self) -> None:
        case = _truth_case(units=[_truth_unit()])
        case["expected_status"] = "partial"
        problems = oracle.validate_truth_schema(_truth_bytes([case]))
        assert any("bad-expected-status" in p for p in problems)

    def test_truth_optional_category_allowed(self) -> None:
        case = _truth_case(units=[_truth_unit()])
        assert case["category"] == "fact"
        problems = oracle.validate_truth_schema(_truth_bytes([case]))
        assert problems == []


class TestEnvelopeFreezeA2:
    def test_dev_inputs_schema_valid(self) -> None:
        assert oracle.validate_inputs_schema(DEV_INPUTS.read_bytes()) == []

    def test_sequestered_inputs_schema_valid(self) -> None:
        assert oracle.validate_inputs_schema(SEQ_INPUTS.read_bytes()) == []

    def test_sequestered_truth_schema_valid(self) -> None:
        assert oracle.validate_truth_schema(SEQ_TRUTH.read_bytes()) == []

    def test_freeze_case_counts(self) -> None:
        inputs = _read_json(SEQ_INPUTS)
        truth = _read_json(SEQ_TRUTH)
        assert inputs["version"] == 1 and truth["version"] == 1
        assert len(inputs["cases"]) == 27 and len(truth["cases"]) == 27
        assert [c["case_id"] for c in inputs["cases"]] == [c["case_id"] for c in truth["cases"]]

    def test_freeze_unit_shape(self) -> None:
        truth = _read_json(SEQ_TRUTH)
        units: list[dict] = []
        for case in truth["cases"]:
            for unit in case["expected_units"]:
                assert set(unit) == {"id", "class", "scope", "payload", "evidence", "disposition"}
                assert unit["id"]
                assert isinstance(unit["payload"], dict)
                assert isinstance(unit["evidence"], list)
                units.append(unit)
        assert len(units) >= 15

    def test_freeze_conflict_unresolved_units(self) -> None:
        truth = _read_json(SEQ_TRUTH)
        by_id = {c["case_id"]: c for c in truth["cases"]}
        for cid in ("mq-c08", "mq-c09"):
            case = by_id[cid]
            assert case["conflict"] is True
            assert case["expected_status"] == "unresolved"
            assert case["zero_reason"] == "all-filtered"
            assert len(case["expected_units"]) == 2
            assert all(u["disposition"] == "unresolved" for u in case["expected_units"])

    def test_no_truth_candidate_id_key(self) -> None:
        truth_text = SEQ_TRUTH.read_text(encoding="utf-8")
        assert "candidate_id" not in truth_text


class TestIsolation:
    def test_source_has_no_runtime_tokens(self) -> None:
        text = ORACLE_SRC.read_text(encoding="utf-8")
        scrubbed = text
        for key in (
            '"socket.socket.connect"',
            '"socket.create_connection"',
            '"urllib.request.urlopen"',
            '"http.client.HTTPConnection.request"',
        ):
            scrubbed = scrubbed.replace(key, '""')
        lowered = scrubbed.lower()
        for token in ("daemon", "retriev", "recall", "hosts", "socket", "urllib", "http", "7788", "provider"):
            assert token not in lowered, token

    def test_single_scoring_path(self) -> None:
        text = ORACLE_SRC.read_text(encoding="utf-8")
        assert text.count("score" + "_experience(") == 1

    def test_fresh_interpreter_loads_no_guarded_modules(self) -> None:
        import os
        import subprocess

        probe = (
            "import sys; "
            "from mnemoseed_local.eval import m5prep_oracle; "
            "from mnemoseed_local.eval import m5prep_bar; "
            "bad = [m for m in sys.modules if m.startswith('mnemoseed_local.daemon') "
            "or m.startswith('mnemoseed_local.config') "
            "or (m.startswith('mnemoseed_local') and 'provider' in m) "
            "or m.startswith('mnemoseed_local.hosts')]; "
            "print(len(bad))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "0"

    def test_guarded_entry_points_idle(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import http.client
        import socket
        import urllib.request

        hits: list[str] = []

        def _wrap(label: str, target: object, attr: str) -> None:
            original = getattr(target, attr)

            def _spy(*args, **kwargs):  # type: ignore[no-untyped-def]
                hits.append(label)
                return original(*args, **kwargs)

            monkeypatch.setattr(target, attr, _spy)

        _wrap("socket.socket.connect", socket.socket, "connect")
        _wrap("socket.create_connection", socket, "create_connection")
        _wrap("urllib.request.urlopen", urllib.request, "urlopen")
        _wrap("http.client.HTTPConnection.request", http.client.HTTPConnection, "request")
        oracle.normalize_oracle("probe text.")
        _read_json(DEV_INPUTS)
        assert hits == []

    def test_home_dir_untouched(self) -> None:
        home_marker = Path.home() / ".mnemoseed-local"
        before = set(home_marker.iterdir()) if home_marker.exists() else set()
        oracle.normalize_oracle("home probe.")
        after = set(home_marker.iterdir()) if home_marker.exists() else set()
        assert before == after
