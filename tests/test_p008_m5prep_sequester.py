"""G-lane sequester integration tests (scratch-only, no real unseal).

Covers the G-owned surface: seal/registry integrity on scratch bytes,
one-shot discipline through scratch registries, mechanical disjointness on
synthetic fixtures, vendored phrase equality, the canonical Appendix-A
oracle, numeric-bar AST guards, the single scoring path, isolation, and
persistent cross-process one-shot behavior. No test opens the real
sequestered truth or the real registries; every registry path here is a
``tmp_path`` scratch file.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mnemoseed_local.eval import m5prep_bar
from mnemoseed_local.eval import m5prep_oracle as oracle

ROOT = Path(__file__).resolve().parents[1]
ORACLE_SRC = ROOT / "src" / "mnemoseed_local" / "eval" / "m5prep_oracle.py"
BAR_SRC = ROOT / "src" / "mnemoseed_local" / "eval" / "m5prep_bar.py"
RUNNER_SRC = ROOT / "src" / "mnemoseed_local" / "eval" / "m5prep_run_blind.py"
TEST_SRC = Path(__file__)
REPORT_PATH = ROOT / "docs" / "p008-m5prep-report.md"

CALL_NAME = "score" + "_experience"
BLIND_CALL = "blind" + "_score("


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


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
    from types import ModuleType

    calls: list[tuple[int, int]] = []

    def fake(predicted: list, truth: list) -> dict:
        calls.append((len(predicted), len(truth)))
        return {"n_predicted": len(predicted), "n_truth": len(truth), "marker": "stub-only"}

    dotted = "mnemoseed_local.eval.experience_metrics"
    stub = ModuleType(dotted)
    setattr(stub, CALL_NAME, fake)
    monkeypatch.setitem(sys.modules, dotted, stub)
    return {"calls": calls}


def _norm_payload_strings(cases: list[dict]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for case in cases:
        for unit in case.get("expected_units", []):
            cls = str(unit.get("class", ""))
            payload = unit.get("payload", {})
            text = ""
            if cls == "fact":
                parts = [str(payload.get(k, "")) for k in ("subject", "predicate", "object", "polarity")]
                text = "|".join(oracle.normalize_oracle(p) for p in parts)
            elif cls == "lesson":
                text = oracle.normalize_oracle(str(payload.get("text", "")))
            elif cls == "intention":
                text = (
                    oracle.normalize_oracle(str(payload.get("trigger_condition", "")))
                    + "|"
                    + oracle.normalize_oracle(str(payload.get("action", "")))
                )
            elif cls == "skill_sequence":
                chain = payload.get("tool_chain", [])
                text = (
                    oracle.normalize_oracle(str(payload.get("task_type", "")))
                    + "|"
                    + ",".join(str(c) for c in chain)
                )
            grouped.setdefault(cls, set()).add(cls + ":" + text)
    return grouped


def check_disjointness(v1_cases: list[dict], seq_cases: list[dict]) -> list[str]:
    problems: list[str] = []
    v1_ids = {c["case_id"] for c in v1_cases}
    seq_ids = {c["case_id"] for c in seq_cases}
    clash = v1_ids & seq_ids
    if clash:
        problems.append(f"fixture-id-collision:{sorted(clash)[0]}")
    v1_pts = {
        (e.get("kind"), e.get("id"))
        for c in v1_cases
        for u in c.get("expected_units", [])
        for e in u.get("evidence", [])
    }
    seq_pts = {
        (e.get("kind"), e.get("id"))
        for c in seq_cases
        for u in c.get("expected_units", [])
        for e in u.get("evidence", [])
    }
    both = v1_pts & seq_pts
    if both:
        problems.append(f"evidence-pointer-collision:{sorted(both)[0]}")
    v1_norm = _norm_payload_strings(v1_cases)
    seq_norm = _norm_payload_strings(seq_cases)
    for cls in set(v1_norm) | set(seq_norm):
        overlap = v1_norm.get(cls, set()) & seq_norm.get(cls, set())
        if overlap:
            problems.append(f"payload-collision:{cls}:{sorted(overlap)[0]}")
    for cases, label in ((v1_cases, "v1"), (seq_cases, "seq")):
        for case in cases:
            for unit in case.get("expected_units", []):
                for key in ("conflict_pair_id", "paraphrase_group_id"):
                    val = unit.get(key)
                    if val and not str(val).startswith("mq-"):
                        problems.append(f"namespace-breach:{label}:{key}:{val}")
    v1_groups = {
        u.get("paraphrase_group_id")
        for c in v1_cases
        for u in c.get("expected_units", [])
        if u.get("paraphrase_group_id")
    }
    seq_groups = {
        u.get("paraphrase_group_id")
        for c in seq_cases
        for u in c.get("expected_units", [])
        if u.get("paraphrase_group_id")
    }
    shared_groups = (v1_groups & seq_groups) - {""}
    if shared_groups:
        problems.append(f"paraphrase-namespace-collision:{sorted(shared_groups)[0]}")
    return problems


def _synth_case(case_id: str, cls: str, payload: dict, ev_id: str, group: str | None = None) -> dict:
    unit: dict = {
        "class": cls,
        "scope": "s1",
        "payload": payload,
        "evidence": [{"kind": "chunk", "id": ev_id}],
        "disposition": "accepted",
    }
    if group is not None:
        unit["paraphrase_group_id"] = group
    return {"case_id": case_id, "expected_units": [unit]}


class TestSealRegistryIntegrity:
    def _seal_for(self, inputs: bytes, truth: bytes, n: int) -> dict:
        return {
            "inputs_sha256": hashlib.sha256(inputs).hexdigest(),
            "truth_sha256": hashlib.sha256(truth).hexdigest(),
            "n_cases": n,
            "method": "sha256-raw-bytes-lowercase-hex",
            "created_utc": "2026-09-07T00:00:00Z",
            "oracle_session": "scratch-oracle",
        }

    def test_tamper_without_new_entry_fails(self) -> None:
        inputs = b'{"version": 1, "cases": []}'
        truth = b'{"version": 1, "cases": []}'
        seal = self._seal_for(inputs, truth, 0)
        assert oracle.verify_seal(inputs, truth, seal) == []
        assert "seal-mismatch:inputs_sha256" in oracle.verify_seal(inputs + b" ", truth, seal)
        assert "seal-mismatch:truth_sha256" in oracle.verify_seal(inputs, truth + b" ", seal)

    def test_key_order_shuffled_accepted(self, tmp_path: Path) -> None:
        entry = _scratch_seal(_hex("k1"), _hex("k2"), 2)
        shuffled = {k: entry[k] for k in reversed(list(entry.keys()))}
        path = tmp_path / "order-scratch-seal-registry.jsonl"
        path.write_text(json.dumps(shuffled) + "\n", encoding="utf-8")
        assert oracle.parse_registry(str(path))["errors"] == []

    @pytest.mark.parametrize(
        ("mutate", "code"),
        [
            ("malformed", "malformed-line"),
            ("uppercase", "uppercase-hex"),
            ("method", "unknown-method"),
            ("dupseq", "duplicate-seq"),
            ("duptruth", "duplicate-truth-hash"),
        ],
    )
    def test_registry_rejects_fixtures(self, tmp_path: Path, mutate: str, code: str) -> None:
        if mutate == "malformed":
            path = tmp_path / "bad-scratch-seal-registry.jsonl"
            path.write_text("{broken\n", encoding="utf-8")
        elif mutate == "uppercase":
            entry = _scratch_seal(_hex("u1"), _hex("u2"), 1)
            entry["truth_sha256"] = _hex("u2").upper()
            path = tmp_path / "upper-scratch-seal-registry.jsonl"
            _write_registry(path, [entry])
        elif mutate == "method":
            entry = _scratch_seal(_hex("m1"), _hex("m2"), 1)
            entry["method"] = "git-hash-object"
            path = tmp_path / "method-scratch-seal-registry.jsonl"
            _write_registry(path, [entry])
        elif mutate == "dupseq":
            path = tmp_path / "dupseq-scratch-seal-registry.jsonl"
            _write_registry(
                path,
                [
                    _scratch_seal(_hex("d1"), _hex("d2"), 1, seq=1),
                    _scratch_seal(_hex("d3"), _hex("d4"), 1, seq=1),
                ],
            )
        else:
            first = _scratch_seal(_hex("t1"), _hex("shared"), 1, seq=1)
            second = _scratch_seal(_hex("t2"), _hex("shared"), 1, seq=2)
            path = tmp_path / "duptruth-scratch-seal-registry.jsonl"
            _write_registry(path, [first, second])
        assert oracle.parse_registry(str(path))["errors"] == [code]

    def test_void_never_authorizes(self, tmp_path: Path, stub_scorer: dict) -> None:
        ih, th = "scratch-" + _hex("v1"), "scratch-" + _hex("v2")
        sr, rl = _scratch_files(tmp_path, "void")
        entry = _scratch_seal(ih, th, 1)
        entry["status"] = "void"
        entry["void_reason"] = "batch-rollback"
        _write_registry(sr, [entry])
        seal = _scratch_seal(ih, th, 1)
        with pytest.raises(ValueError, match="void-seal"):
            oracle.blind_score(
                [], [], seal=seal, registry_path=str(rl), seal_registry_path=str(sr)
            )  # scratch


class TestOneShotDiscipline:
    def test_second_use_blocked_unless_fresh(self, tmp_path: Path, stub_scorer: dict) -> None:
        ih, th = "scratch-" + _hex("r1"), "scratch-" + _hex("r2")
        sr, rl = _scratch_files(tmp_path, "reuse")
        _write_registry(sr, [_scratch_seal(ih, th, 1)])
        seal = _scratch_seal(ih, th, 1)
        oracle.blind_score(
            [{"a": 1}], [{"a": 1}], seal=seal, registry_path=str(rl), seal_registry_path=str(sr)
        )  # scratch
        with pytest.raises(ValueError, match="held-out-reuse-blocked"):
            oracle.blind_score(
                [{"a": 1}], [{"a": 1}], seal=seal, registry_path=str(rl), seal_registry_path=str(sr)
            )  # scratch
        fresh = _scratch_seal("scratch-" + _hex("fresh-in"), "scratch-" + _hex("fresh-tr"), 1, seq=2)
        _write_registry(sr, [_scratch_seal(ih, th, 1), fresh])
        out = oracle.blind_score(
            [{"a": 1}], [{"a": 1}], seal=fresh, registry_path=str(rl), seal_registry_path=str(sr)
        )  # scratch
        assert out["registry_lines"] == 2

    def test_truth_edit_simulation_fails(self, tmp_path: Path, stub_scorer: dict) -> None:
        ih, th = "scratch-" + _hex("e1"), "scratch-" + _hex("e2")
        sr, rl = _scratch_files(tmp_path, "edit")
        _write_registry(sr, [_scratch_seal(ih, th, 2)])
        seal = _scratch_seal(ih, th, 2)
        with pytest.raises(ValueError, match="post-hoc-truth-edit"):
            oracle.blind_score(
                [{"a": 1}], [{"a": 1}], seal=seal, registry_path=str(rl), seal_registry_path=str(sr)
            )  # scratch

    def test_misrouted_scratch_rejected(self, tmp_path: Path, stub_scorer: dict) -> None:
        real_seal = tmp_path / "real-seal-registry.jsonl"
        real_log = tmp_path / "real-oneshot-log.jsonl"
        seal = _scratch_seal("scratch-" + _hex("s1"), "scratch-" + _hex("s2"), 1)
        with pytest.raises(ValueError, match="scratch-seal-misrouted"):
            oracle.blind_score(
                [], [], seal=seal, registry_path=str(real_log), seal_registry_path=str(real_seal)
            )  # scratch

    def test_unregistered_rejected(self, tmp_path: Path, stub_scorer: dict) -> None:
        sr, rl = _scratch_files(tmp_path, "unreg")
        _write_registry(sr, [_scratch_seal("scratch-" + _hex("x1"), "scratch-" + _hex("x2"), 1)])
        seal = _scratch_seal("scratch-" + _hex("y1"), "scratch-" + _hex("y2"), 1)
        with pytest.raises(ValueError, match="unregistered-seal"):
            oracle.blind_score(
                [], [], seal=seal, registry_path=str(rl), seal_registry_path=str(sr)
            )  # scratch

    def test_parse_error_aborts(self, tmp_path: Path, stub_scorer: dict) -> None:
        sr, rl = _scratch_files(tmp_path, "badreg")
        sr.write_text("{broken\n", encoding="utf-8")
        seal = _scratch_seal("scratch-" + _hex("z1"), "scratch-" + _hex("z2"), 0)
        with pytest.raises(ValueError, match="malformed-line"):
            oracle.blind_score(
                [], [], seal=seal, registry_path=str(rl), seal_registry_path=str(sr)
            )  # scratch

    def test_every_call_site_carries_marker(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            idx = 0
            while idx < len(lines):
                if BLIND_CALL in lines[idx]:
                    chunk = lines[idx]
                    depth = chunk.count("(") - chunk.count(")")
                    end = idx
                    while depth > 0 and end + 1 < len(lines):
                        end += 1
                        chunk += "\n" + lines[end]
                        depth += lines[end].count("(") - lines[end].count(")")
                    if "scratch" not in chunk:
                        offenders.append(f"{path.name}:{idx + 1}")
                    idx = end + 1
                else:
                    idx += 1
        assert offenders == []


class TestDisjointness:
    def test_disjoint_synthetic_passes(self) -> None:
        v1 = [
            _synth_case(
                "v-1",
                "fact",
                {"subject": "Ada", "predicate": "prefers", "object": "tea", "polarity": "positive"},
                "e1",
            )
        ]
        seq = [
            _synth_case(
                "s-1",
                "fact",
                {"subject": "Bob", "predicate": "decided", "object": "retry", "polarity": "positive"},
                "e9",
            )
        ]
        assert check_disjointness(v1, seq) == []

    def test_fixture_id_collision_named(self) -> None:
        v1 = [
            _synth_case(
                "same",
                "fact",
                {"subject": "a", "predicate": "b", "object": "c", "polarity": "positive"},
                "e1",
            )
        ]
        seq = [
            _synth_case(
                "same",
                "fact",
                {"subject": "x", "predicate": "y", "object": "z", "polarity": "positive"},
                "e2",
            )
        ]
        problems = check_disjointness(v1, seq)
        assert any(p.startswith("fixture-id-collision:same") for p in problems)

    def test_evidence_pointer_collision_named(self) -> None:
        v1 = [
            _synth_case(
                "v-1",
                "fact",
                {"subject": "a", "predicate": "b", "object": "c", "polarity": "positive"},
                "dup",
            )
        ]
        seq = [
            _synth_case(
                "s-1",
                "fact",
                {"subject": "x", "predicate": "y", "object": "z", "polarity": "positive"},
                "dup",
            )
        ]
        assert any(p.startswith("evidence-pointer-collision:") for p in check_disjointness(v1, seq))

    def test_payload_collision_uses_oracle_normalizer(self) -> None:
        v1 = [_synth_case("v-1", "lesson", {"text": "Check logs first."}, "e1")]
        seq = [_synth_case("s-1", "lesson", {"text": "check logs first"}, "e2")]
        assert any(p.startswith("payload-collision:lesson:") for p in check_disjointness(v1, seq))

    def test_non_prefixed_namespace_fails(self) -> None:
        unit_case = _synth_case("s-1", "lesson", {"text": "novel"}, "e2", group="p01")
        assert any(p.startswith("namespace-breach:") for p in check_disjointness([], [unit_case]))

    def test_shared_paraphrase_group_fails(self) -> None:
        v1 = [_synth_case("v-1", "lesson", {"text": "alpha"}, "e1", group="mq-g1")]
        seq = [_synth_case("s-1", "lesson", {"text": "beta"}, "e2", group="mq-g1")]
        assert any(p.startswith("paraphrase-namespace-collision:") for p in check_disjointness(v1, seq))

    def test_rule_strip_detected(self) -> None:
        v1 = [
            _synth_case(
                "same",
                "fact",
                {"subject": "a", "predicate": "b", "object": "c", "polarity": "positive"},
                "e1",
            )
        ]
        seq = [
            _synth_case(
                "same",
                "fact",
                {"subject": "x", "predicate": "y", "object": "z", "polarity": "positive"},
                "e2",
            )
        ]
        stripped = [p for p in check_disjointness(v1, seq) if not p.startswith("fixture-id")]
        assert stripped != check_disjointness(v1, seq)


class TestVendoredEquality:
    def test_phrase_list_matches_frozen_source(self) -> None:
        from mnemoseed_local.eval.experience_baseline import BANNED_CLAIM_PHRASES

        assert tuple(BANNED_CLAIM_PHRASES) == tuple(m5prep_bar.VENDORED_BANNED_CLAIM_PHRASES)
        canonical = "\n".join(m5prep_bar.VENDORED_BANNED_CLAIM_PHRASES).encode("utf-8")
        assert (
            hashlib.sha256(canonical).hexdigest()
            == "98fd2f1f93425b573094723188f0188cec2971241711153f8f39a2627d077f59"
        )

    def test_drift_detected(self) -> None:
        altered = tuple(m5prep_bar.VENDORED_BANNED_CLAIM_PHRASES)
        altered = altered[1:]
        assert altered != tuple(m5prep_bar.VENDORED_BANNED_CLAIM_PHRASES)
        assert (
            hashlib.sha256("\n".join(altered).encode("utf-8")).hexdigest()
            != "98fd2f1f93425b573094723188f0188cec2971241711153f8f39a2627d077f59"
        )


def _evidence(*ids: str) -> list[dict]:
    return [{"kind": "chunk", "id": value} for value in ids]


def _candidate(
    cid: str, cls: str, scope: str, payload: dict, evidence: list[dict], confidence: float
) -> dict:
    return {
        "candidate_id": cid,
        "class": cls,
        "scope": scope,
        "payload": payload,
        "evidence": evidence,
        "confidence": confidence,
    }


def _accepted(case_id: str, candidates: list[dict]) -> dict:
    return {
        "case_id": case_id,
        "result": {
            "status": "accepted",
            "candidates": candidates,
            "zero_result": False,
            "zero_reason": None,
            "conflict": False,
            "conflict_reason": None,
        },
    }


def _zero(case_id: str, reason: str) -> dict:
    return {
        "case_id": case_id,
        "result": {
            "status": "unresolved",
            "candidates": [],
            "zero_result": True,
            "zero_reason": reason,
            "conflict": False,
            "conflict_reason": None,
        },
    }


def _truth_unit(cls: str, scope: str, payload: dict, evidence: list[dict]) -> dict:
    return {"class": cls, "scope": scope, "payload": payload, "evidence": evidence, "disposition": "accepted"}


class TestAppendixACanonical:
    def test_hand_computed_oracle_matches(self) -> None:
        fact_a1 = {"subject": "Ada", "predicate": "prefers", "object": "tea", "polarity": "positive"}
        fact_a4 = {"subject": "Bob", "predicate": "decided", "object": "retry", "polarity": "positive"}
        predicted = [
            _accepted("c1", [_candidate("a1", "fact", "s1", fact_a1, _evidence("e1"), 0.9)]),
            _zero("c2", "no-match"),
            _accepted(
                "c3", [_candidate("x3", "lesson", "s1", {"text": "novel remark"}, _evidence("e3x"), 0.2)]
            ),
            _accepted("c4", [_candidate("a4", "lesson", "s1", fact_a4, _evidence("e4"), 0.7)]),
            _zero("c5", "no-match"),
        ]
        truth = [
            {
                "case_id": "c1",
                "category": "fact",
                "expected_status": "accepted",
                "zero_reason": None,
                "expected_units": [_truth_unit("fact", "s1", fact_a1, _evidence("e1"))],
            },
            {
                "case_id": "c2",
                "category": "lesson",
                "expected_status": "accepted",
                "zero_reason": None,
                "expected_units": [
                    _truth_unit("lesson", "s1", {"text": "check logs first"}, _evidence("e2"))
                ],
            },
            {
                "case_id": "c3",
                "category": "noise",
                "expected_status": "unresolved",
                "zero_reason": "no-match",
                "expected_units": [],
            },
            {
                "case_id": "c4",
                "category": "fact",
                "expected_status": "accepted",
                "zero_reason": None,
                "expected_units": [_truth_unit("fact", "s1", fact_a4, _evidence("e4"))],
            },
            {
                "case_id": "c5",
                "category": "noise",
                "expected_status": "unresolved",
                "zero_reason": "no-match",
                "expected_units": [],
            },
        ]
        expected = {
            "fact": {"hit": 1, "miss": 1, "false_alarm": 0},
            "experience": {"hit": 0, "miss": 1, "false_alarm": 1},
            "correct_rejection": 1,
            "misclass": [{"case_id": "c4", "expected_class": "fact", "predicted_class": "lesson"}],
            "zero_reason_histogram": {"no-match": 2},
            "confidence_deciles": [
                {"bin": [0.0, 0.1], "n": 0, "accuracy": None},
                {"bin": [0.1, 0.2], "n": 0, "accuracy": None},
                {"bin": [0.2, 0.3], "n": 1, "accuracy": 0.0},
                {"bin": [0.3, 0.4], "n": 0, "accuracy": None},
                {"bin": [0.4, 0.5], "n": 0, "accuracy": None},
                {"bin": [0.5, 0.6], "n": 0, "accuracy": None},
                {"bin": [0.6, 0.7], "n": 0, "accuracy": None},
                {"bin": [0.7, 0.8], "n": 1, "accuracy": 0.0},
                {"bin": [0.8, 0.9], "n": 0, "accuracy": None},
                {"bin": [0.9, 1.0], "n": 1, "accuracy": 1.0},
            ],
            "unscored_units": 0,
        }
        assert m5prep_bar.report_sdt(predicted, truth) == expected


def _check_tree(tree: ast.AST) -> list[str]:
    findings: list[str] = []

    def bad(name: str) -> str | None:
        if name.upper() in m5prep_bar.BENIGN_NUMERIC_NAMES:
            return None
        token = m5prep_bar.normalize_bar_token(name)
        if any(m in token for m in m5prep_bar.BANNED_BAR_TOKENS):
            return "numeric-threshold-constant:" + name
        return None

    def operand(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets: list[ast.AST] = []
            values: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets, values = list(node.targets), [node.value]
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                values = [node.value] if node.value is not None else []
            else:
                targets, values = [node.target], [node.value]
            for target in targets:
                if isinstance(target, ast.Name):
                    for value in values:
                        if (
                            isinstance(value, ast.Constant)
                            and isinstance(value.value, (int, float))
                            and not isinstance(value.value, bool)
                        ):
                            hit = bad(target.id)
                            if hit:
                                findings.append(hit)
                        if isinstance(value, ast.Name):
                            hit = bad(target.id) or bad(value.id)
                            if hit:
                                findings.append(hit)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.args
            defaults = list(node.args.defaults)
            start = len(args) - len(defaults)
            for arg, default in list(zip(args[start:], defaults, strict=True)) + list(
                zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
            ):
                if default is None:
                    continue
                if (
                    isinstance(default, ast.Constant)
                    and isinstance(default.value, (int, float))
                    and not isinstance(default.value, bool)
                ):
                    hit = bad(arg.arg)
                    if hit:
                        findings.append(hit)
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(
                isinstance(s, ast.Constant)
                and isinstance(s.value, (int, float))
                and not isinstance(s.value, bool)
                for s in sides
            ):
                for side in sides:
                    name = operand(side)
                    if name is not None:
                        hit = bad(name)
                        if hit:
                            findings.append(hit)
        elif isinstance(node, ast.Dict):
            for kn, vn in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(kn, ast.Constant)
                    and isinstance(kn.value, str)
                    and isinstance(vn, ast.Constant)
                    and isinstance(vn.value, (int, float))
                    and not isinstance(vn.value, bool)
                ):
                    key = kn.value
                    if (
                        key.lower() in m5prep_bar.BENIGN_PROPOSAL_KEYS
                        or key.upper() in m5prep_bar.BENIGN_NUMERIC_NAMES
                    ):
                        continue
                    token = m5prep_bar.normalize_bar_token(key)
                    if any(m in token for m in m5prep_bar.BANNED_BAR_TOKENS):
                        findings.append("numeric-threshold-constant:" + key)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if (
                    kw.arg is not None
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, (int, float))
                    and not isinstance(kw.value.value, bool)
                ):
                    hit = bad(kw.arg)
                    if hit:
                        findings.append(hit)
    return findings


class TestNumericBarGuard:
    def test_shipped_modules_carry_no_numeric_bar(self) -> None:
        for path in (BAR_SRC, ORACLE_SRC, RUNNER_SRC):
            assert _check_tree(ast.parse(path.read_text(encoding="utf-8"))) == [], str(path)

    def test_injected_variants_flagged(self) -> None:
        for src in (
            "TARGET_R = 0.8",
            "CUT_OFF = 0.8",
            "MIN_P = 0.75",
            "def f(limit=0.75):\n    return limit",
            'config = {"cutoff": 0.5}',
            "f(threshold=0.6)",
            "ALIAS = TARGET_P",
        ):
            assert _check_tree(ast.parse(src)), src

    def test_benign_numerics_pass(self) -> None:
        for src in ("N_BINS = 10", "BIN_EDGES = [0.0, 0.1]", "VERSION = 1", "x = 5"):
            assert _check_tree(ast.parse(src)) == [], src

    def test_canonical_set_pinned(self) -> None:
        assert m5prep_bar.BANNED_BAR_TOKENS == frozenset(
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


class TestSingleScoringPath:
    def test_exactly_one_call_site_in_oracle(self) -> None:
        hits: list[str] = []
        for path in (ORACLE_SRC, RUNNER_SRC, TEST_SRC):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = (
                        func.id
                        if isinstance(func, ast.Name)
                        else (func.attr if isinstance(func, ast.Attribute) else "")
                    )
                    if name == CALL_NAME:
                        hits.append(f"{path.name}:{node.lineno}")
        assert len(hits) == 1, hits
        assert hits[0].startswith("m5prep_oracle.py:")

    def test_runner_scores_only_through_logged_path(self) -> None:
        text = RUNNER_SRC.read_text(encoding="utf-8")
        assert CALL_NAME + "(" not in text
        assert BLIND_CALL in text


class TestNormalizerDivergence:
    def test_oracle_frozen_cases(self) -> None:
        assert oracle.normalize_oracle("  Hello   WORLD  ") == "hello world"
        assert oracle.normalize_oracle("C++ guide") == "c++ guide"
        assert oracle.normalize_oracle("C# note") == "c# note"
        assert oracle.normalize_oracle("don't stop") == "don't stop"
        assert oracle.normalize_oracle("done.") == "done"
        assert oracle.normalize_oracle("done!!") == "done!"

    def test_shared_module_read_only(self) -> None:
        from mnemoseed_local.eval import experience_common

        assert experience_common.normalize_exp("  Hello   WORLD  ") == "hello world"


class TestBannedClaims:
    def test_biting_prose_flagged(self) -> None:
        for prose in (
            "the m5 ratified milestone",
            "the change landed",
            "high efficacy shown",
            "generalization improves",
        ):
            assert m5prep_bar.check_report_claims(prose)["hits"], prose

    def test_clean_prose_passes(self) -> None:
        prose = (
            "Future ratification would use a sequestered one-shot run plus the "
            "SDT-shape summary. Open topics: natural-language extraction quality: "
            "NOT_OBSERVED; real model quality: NOT_OBSERVED; real-data "
            "coverage/eligibility: NOT_OBSERVED; live resource impact: NOT_OBSERVED. "
            "No number is proposed here."
        )
        assert m5prep_bar.check_report_claims(prose) == {"hits": [], "missing_topics": []}

    def test_report_file_passes_claim_check(self) -> None:
        outcome = m5prep_bar.check_report_claims(REPORT_PATH.read_text(encoding="utf-8"))
        assert outcome == {"hits": [], "missing_topics": []}


class TestIsolation:
    def test_owned_modules_import_clean(self) -> None:
        import ast as _ast

        for path in (RUNNER_SRC, ORACLE_SRC):
            text = path.read_text(encoding="utf-8").lower()
            scrubbed = text
            for key in (
                '"socket.socket.connect"',
                '"socket.create_connection"',
                '"urllib.request.urlopen"',
                '"http.client.httpconnection.request"',
            ):
                scrubbed = scrubbed.replace(key, '""')
            for token in ("daemon", "retriev", "recall", "hosts", "provider", "7788"):
                assert token not in scrubbed, f"{path.name}:{token}"
        for path in (RUNNER_SRC, BAR_SRC, ORACLE_SRC):
            assert "7788" not in path.read_text(encoding="utf-8"), path.name
            tree = _ast.parse(path.read_text(encoding="utf-8"))
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, _ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    assert "daemon" not in name and "hosts" not in name and "provider" not in name, (
                        f"{path.name}:{name}"
                    )

    def test_guarded_entry_points_idle(self, tmp_path: Path, stub_scorer: dict) -> None:
        import http.client
        import socket
        import urllib.request

        hits: list[str] = []
        real_connect = socket.socket.connect
        real_create = socket.create_connection
        real_urlopen = urllib.request.urlopen
        real_request = http.client.HTTPConnection.request

        def counting_connect(self, *a: object, **k: object) -> object:
            hits.append("connect")
            return real_connect(self, *a, **k)

        def counting_create(*a: object, **k: object) -> object:
            hits.append("create")
            return real_create(*a, **k)

        def counting_urlopen(*a: object, **k: object) -> object:
            hits.append("urlopen")
            return real_urlopen(*a, **k)

        def counting_request(self: object, *a: object, **k: object) -> object:
            hits.append("request")
            return real_request(self, *a, **k)  # type: ignore[arg-type]

        socket.socket.connect = counting_connect  # type: ignore[method-assign]
        socket.create_connection = counting_create  # type: ignore[assignment]
        urllib.request.urlopen = counting_urlopen  # type: ignore[assignment]
        http.client.HTTPConnection.request = counting_request  # type: ignore[method-assign]
        try:
            oracle.normalize_oracle("probe text.")
            m5prep_bar.report_sdt([], [])
        finally:
            socket.socket.connect = real_connect  # type: ignore[method-assign]
            socket.create_connection = real_create  # type: ignore[assignment]
            urllib.request.urlopen = real_urlopen  # type: ignore[assignment]
            http.client.HTTPConnection.request = real_request  # type: ignore[method-assign]
        assert hits == []

    def test_home_untouched(self) -> None:
        marker = Path.home() / ".mnemoseed-local"
        before = set(marker.iterdir()) if marker.exists() else set()
        oracle.normalize_oracle("home probe.")
        after = set(marker.iterdir()) if marker.exists() else set()
        assert before == after

    def test_fresh_interpreter_loads_no_guarded_modules(self) -> None:
        probe = (
            "import sys; "
            "from mnemoseed_local.eval import m5prep_run_blind; "
            "bad = [m for m in sys.modules if m.startswith('mnemoseed_local.daemon') "
            "or m.startswith('mnemoseed_local.config') "
            "or (m.startswith('mnemoseed_local') and 'provider' in m) "
            "or m.startswith('mnemoseed_local.hosts')]; "
            "print(len(bad))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, env=env, cwd=str(ROOT), check=False
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "0"


class TestRepeatabilityPortable:
    def test_two_scratch_runs_canon_equal(self, tmp_path: Path, stub_scorer: dict) -> None:
        from mnemoseed_local.eval.experience_baseline import canonical_baseline

        sr, rl = _scratch_files(tmp_path, "repeat")
        first = _scratch_seal("scratch-" + _hex("p1"), "scratch-" + _hex("q1"), 1, seq=1)
        second = _scratch_seal("scratch-" + _hex("p2"), "scratch-" + _hex("q2"), 1, seq=2)
        _write_registry(sr, [first, second])
        pred = [{"case_id": "s-c1", "result": {"status": "accepted"}}]
        truth = [{"case_id": "s-c1", "category": "fact"}]
        oracle.blind_score(
            pred, truth, seal=first, registry_path=str(rl), seal_registry_path=str(sr)
        )  # scratch
        oracle.blind_score(
            pred, truth, seal=second, registry_path=str(rl), seal_registry_path=str(sr)
        )  # scratch
        lines = [
            json.loads(line)
            for line in Path(str(rl)).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 2
        assert lines[0]["ops_counters"] == lines[1]["ops_counters"]

        report_a = {
            "started_at": "t1",
            "duration_s": 1.0,
            "out_path": "a",
            "score": {"x": 1},
            "isolation": {"home": "/tmp/one"},
            "files_read": ["/tmp/x/inputs.json"],
        }
        report_b = {
            "started_at": "t2",
            "duration_s": 9.0,
            "out_path": "b",
            "score": {"x": 1},
            "isolation": {"home": "/tmp/two"},
            "files_read": ["/tmp/y/inputs.json"],
        }
        assert canonical_baseline(report_a) == canonical_baseline(report_b)

    def test_canonical_hash_stable_across_homes(self, tmp_path: Path) -> None:
        from mnemoseed_local.eval.experience_baseline import canonical_sha256

        base = {"score": {"fact": {"hit": 1}}, "isolation": {"home": "X"}, "files_read": ["inputs.json"]}
        first_home = tmp_path / "home-one"
        second_home = tmp_path / "home-two"
        first_home.mkdir()
        second_home.mkdir()
        report_a = dict(
            base, isolation={"home": str(first_home)}, started_at="t1", duration_s=1.0, out_path="a"
        )
        report_b = dict(
            base, isolation={"home": str(second_home)}, started_at="t2", duration_s=2.0, out_path="b"
        )
        assert canonical_sha256(report_a) == canonical_sha256(report_b)


def _write_scratch_corpus(tmp_path: Path, stem: str, n: int = 2) -> tuple[Path, Path]:
    inputs_cases = []
    truth_cases = []
    for i in range(n):
        cid = f"{stem}-c{i}"
        inputs_cases.append(
            {
                "case_id": cid,
                "raw": {"kind": "noise", "text": f"scratch note {i}"},
                "sources": {
                    "run_id": f"{stem}-run",
                    "sources": [
                        {"kind": "chunk", "id": f"{stem}-e{i}", "fixture_id": f"{stem}-f{i}", "text": "t"}
                    ],
                },
                "variant": "base",
            }
        )
        truth_cases.append(
            {
                "case_id": cid,
                "category": "noise",
                "expected_status": "unresolved",
                "zero_reason": "no-match",
                "coercion_loss": False,
                "conflict": False,
                "expected_units": [],
            }
        )
    inputs_path = tmp_path / f"{stem}-scratch-inputs.json"
    truth_path = tmp_path / f"{stem}-scratch-truth.json"
    inputs_path.write_text(json.dumps({"version": 1, "cases": inputs_cases}), encoding="utf-8")
    truth_path.write_text(json.dumps({"version": 1, "cases": truth_cases}), encoding="utf-8")
    return inputs_path, truth_path


def _register_scratch_hashes(seal_path: Path, inputs_path: Path, truth_path: Path, seq: int = 1) -> dict:
    inputs_h = hashlib.sha256(inputs_path.read_bytes()).hexdigest()
    truth_h = hashlib.sha256(truth_path.read_bytes()).hexdigest()
    payload = json.loads(inputs_path.read_text(encoding="utf-8"))
    entry = _scratch_seal(inputs_h, truth_h, len(payload["cases"]), seq=seq)
    existing: list[dict] = []
    if seal_path.exists():
        existing = [
            json.loads(line) for line in seal_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
    existing.append(entry)
    _write_registry(seal_path, existing)
    return entry


def _runner_env(home: Path, *, worktree: str | None = None) -> dict[str, str]:
    """Build an isolated env for a scratch runner invocation."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["MNEMOSEED_LOCAL_HOME"] = str(home)
    env["M5PREP_G_SESSION"] = "ses_f82644f77ffeDK1N87KGW92UE2"
    env["M5PREP_G_WORKTREE"] = str(Path.cwd()) if worktree is None else worktree
    return env


def _base_args(
    inputs_path: Path, truth_path: Path, seal_path: Path, log_path: Path, out_path: Path
) -> list[str]:
    return [str(inputs_path), str(truth_path), str(seal_path), str(log_path), str(out_path)]


def _relocate(result: subprocess.CompletedProcess) -> str:
    return (result.stdout or "") + (result.stderr or "")


class TestRunnerOneShot:
    def test_runner_scores_once_then_blocks_reuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", "ses_f82644f77ffeDK1N87KGW92UE2")
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        inputs_path, truth_path = _write_scratch_corpus(tmp_path, "run")
        seal_path, log_path = _scratch_files(tmp_path, "run")
        entry = _register_scratch_hashes(seal_path, inputs_path, truth_path)
        out_path = tmp_path / "run-scratch-report.json"
        rc = main(_base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"])
        assert rc == 0
        assert out_path.is_file()
        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert report["seal"]["seal_seq"] == 1
        assert report["ops_counters"] == {
            "socket.socket.connect": 0,
            "socket.create_connection": 0,
            "urllib.request.urlopen": 0,
            "http.client.HTTPConnection.request": 0,
        }
        assert report["NOT_OBSERVED"] == [
            "natural-language extraction quality",
            "real model quality",
            "real-data coverage/eligibility",
            "live resource impact",
        ]
        marker = Path(str(log_path) + f".reserve-1-{entry['truth_sha256']}.json")
        assert marker.is_file()
        rc2 = main(
            _base_args(inputs_path, truth_path, seal_path, log_path, tmp_path / "second-scratch-report.json")
            + ["--seal-seq", "1"]
        )
        assert rc2 != 0

    def test_runner_rejects_banned_note(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", "ses_f82644f77ffeDK1N87KGW92UE2")
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        inputs_path, truth_path = _write_scratch_corpus(tmp_path, "ban")
        seal_path, log_path = _scratch_files(tmp_path, "ban")
        entry = _register_scratch_hashes(seal_path, inputs_path, truth_path)
        out_path = tmp_path / "ban-scratch-report.json"
        rc = main(
            _base_args(inputs_path, truth_path, seal_path, log_path, out_path)
            + ["--note", "the m5 ratified milestone", "--seal-seq", "1"]
        )
        assert rc != 0
        assert not out_path.is_file()
        marker = Path(str(log_path) + f".reserve-1-{entry['truth_sha256']}.json")
        assert marker.is_file()
        lines = _log_lines(log_path)
        assert len(lines) == 1
        assert lines[0]["verdict"] == "void"
        assert lines[0]["canonical_sha256"] is None
        assert lines[0]["note"].startswith("abort:")

    def test_runner_cross_process_persistent(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        inputs_path, truth_path = _write_scratch_corpus(tmp_path, "xproc")
        seal_path, log_path = _scratch_files(tmp_path, "xproc")
        _register_scratch_hashes(seal_path, inputs_path, truth_path)
        out_one = tmp_path / "one-scratch-report.json"
        out_two = tmp_path / "two-scratch-report.json"
        env = _runner_env(home, worktree=str(ROOT))
        first = subprocess.run(
            [
                sys.executable,
                "-m",
                "mnemoseed_local.eval.m5prep_run_blind",
                *_base_args(inputs_path, truth_path, seal_path, log_path, out_one),
                "--seal-seq",
                "1",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
            check=False,
        )
        assert first.returncode == 0, _relocate(first)
        second = subprocess.run(
            [
                sys.executable,
                "-m",
                "mnemoseed_local.eval.m5prep_run_blind",
                *_base_args(inputs_path, truth_path, seal_path, log_path, out_two),
                "--seal-seq",
                "1",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
            check=False,
        )
        assert second.returncode != 0
        assert "attempt-reserved" in _relocate(second)


def _marker_path(log_path: Path, seq: int, truth_sha: str) -> Path:
    return Path(str(log_path) + f".reserve-{seq}-{truth_sha}.json")


def _log_lines(log_path: Path) -> list[dict]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestRunnerReservation:
    """A2 create-once reservation contract on the runner (scratch-only)."""

    G2 = "ses_f82644f77ffeDK1N87KGW92UE2"

    def _setup(self, tmp_path: Path, stem: str) -> tuple[Path, Path, dict, Path, Path, Path]:
        home = tmp_path / f"{stem}-home"
        home.mkdir()
        inputs_path, truth_path = _write_scratch_corpus(tmp_path, stem)
        seal_path, log_path = _scratch_files(tmp_path, stem)
        entry = _register_scratch_hashes(seal_path, inputs_path, truth_path)
        out_path = tmp_path / f"{stem}-scratch-report.json"
        return home, inputs_path, entry, seal_path, log_path, out_path

    def test_missing_seal_seq_creates_neither_marker_nor_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "noseq")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        truth_path = tmp_path / "noseq-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path)
        rc = main(args)
        assert rc != 0
        assert not _marker_path(log_path, entry["seq"], entry["truth_sha256"]).exists()
        assert not log_path.exists()

    def test_unknown_seq_creates_neither_marker_nor_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "wrongseq")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        truth_path = tmp_path / "wrongseq-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "99"]
        rc = main(args)
        assert rc != 0
        assert not _marker_path(log_path, 99, entry["truth_sha256"]).exists()
        assert not log_path.exists()

    def test_void_seq_fails_before_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "voidseq")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        void_row = dict(entry)
        void_row["status"] = "void"
        void_row["void_reason"] = "corpus-schema-mismatch"
        void_row["voided_utc"] = "2026-09-08T10:20:00Z"
        void_row["voided_by_session"] = "r2"
        existing = [
            json.loads(line) for line in seal_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        existing.append(void_row)
        _write_registry(seal_path, existing)
        truth_path = tmp_path / "voidseq-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc != 0
        assert not _marker_path(log_path, 1, entry["truth_sha256"]).exists()
        assert not log_path.exists()

    def test_mismatched_inputs_fail_before_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "mismatch")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        second_inputs = inputs_path.read_bytes() + b" "
        inputs_path.write_bytes(second_inputs)
        truth_path = tmp_path / "mismatch-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc != 0
        assert not _marker_path(log_path, 1, entry["truth_sha256"]).exists()
        assert not log_path.exists()

    def test_missing_g_session_fails_before_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "nogsession")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.delenv("M5PREP_G_SESSION", raising=False)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        truth_path = tmp_path / "nogsession-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc != 0
        assert not _marker_path(log_path, 1, entry["truth_sha256"]).exists()

    def test_mismatched_g_session_fails_before_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "badgsession")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", "ses_other-session")
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        truth_path = tmp_path / "badgsession-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc != 0
        assert not _marker_path(log_path, 1, entry["truth_sha256"]).exists()

    def test_mismatched_worktree_fails_before_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "badwt")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(ROOT / "other-tree"))
        truth_path = tmp_path / "badwt-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc != 0
        assert not _marker_path(log_path, 1, entry["truth_sha256"]).exists()

    def test_marker_exists_before_any_truth_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "nottruth")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        truth_path = tmp_path / "missing-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc != 0
        marker = _marker_path(log_path, entry["seq"], entry["truth_sha256"])
        assert marker.is_file()
        lines = _log_lines(log_path)
        assert len(lines) == 1
        assert lines[0]["verdict"] == "void"
        assert lines[0]["note"].startswith("abort:")
        assert not out_path.is_file()

    def test_success_marker_schema_and_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "schema")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        truth_path = tmp_path / "schema-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc == 0
        marker = _marker_path(log_path, entry["seq"], entry["truth_sha256"])
        assert marker.is_file()
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert set(payload) == {
            "v",
            "seal_seq",
            "inputs_sha256",
            "truth_sha256",
            "g_session",
            "reserved_utc",
            "worktree",
        }
        assert payload["v"] == 1
        assert payload["seal_seq"] == entry["seq"]
        assert payload["inputs_sha256"] == entry["inputs_sha256"]
        assert payload["truth_sha256"] == entry["truth_sha256"]
        assert payload["g_session"] == self.G2
        assert payload["reserved_utc"].endswith("Z")
        assert payload["worktree"] == str(Path.cwd().resolve())
        lines = _log_lines(log_path)
        assert len(lines) == 1
        assert lines[0]["verdict"] == "scored"
        assert lines[0]["worktree"] == str(Path.cwd().resolve())

    def test_preexisting_marker_blocks_and_never_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "premark")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        marker = _marker_path(log_path, entry["seq"], entry["truth_sha256"])
        for marker_bytes in (b"", b"{not-json", b'{"v":1,"split"'):
            marker.write_bytes(marker_bytes)
            truth_path = tmp_path / "premark-scratch-truth.json"
            args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
            rc = main(args)
            assert rc != 0
            assert marker.read_bytes() == marker_bytes
            assert not log_path.exists()

    def test_race_permits_only_one_creator(self, tmp_path: Path) -> None:
        home = tmp_path / "race-home"
        home.mkdir()
        inputs_path, truth_path = _write_scratch_corpus(tmp_path, "race")
        seal_path, log_path = _scratch_files(tmp_path, "race")
        entry = _register_scratch_hashes(seal_path, inputs_path, truth_path)
        env = _runner_env(home, worktree=str(ROOT))
        cmd = [
            sys.executable,
            "-m",
            "mnemoseed_local.eval.m5prep_run_blind",
            *_base_args(inputs_path, truth_path, seal_path, log_path, tmp_path / "race-a.json"),
            "--seal-seq",
            "1",
        ]
        procs = [
            subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            for _ in range(2)
        ]
        results = [p.communicate() for p in procs]
        codes = [p.returncode for p in procs]
        assert codes.count(0) == 1, (codes, results)
        loser = results[codes.index(1)]
        assert "attempt-reserved" in ((loser[0] or "") + (loser[1] or ""))
        assert _marker_path(log_path, entry["seq"], entry["truth_sha256"]).is_file()
        lines = _log_lines(log_path)
        assert len(lines) == 1
        assert lines[0]["verdict"] == "scored"

    def test_controlled_abort_appends_single_void(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "abort")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        truth_path = tmp_path / "abort-scratch-truth.json"
        truth_path.write_bytes(truth_path.read_bytes() + b" ")
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc != 0
        assert _marker_path(log_path, entry["seq"], entry["truth_sha256"]).is_file()
        lines = _log_lines(log_path)
        assert len(lines) == 1
        assert lines[0]["verdict"] == "void"
        assert lines[0]["canonical_sha256"] is None
        assert lines[0]["seal_seq"] == entry["seq"]
        assert lines[0]["inputs_sha256"] == entry["inputs_sha256"]
        assert lines[0]["truth_sha256"] == entry["truth_sha256"]
        assert lines[0]["note"].startswith("abort:")
        assert not out_path.is_file()

    def test_no_void_after_immutable_scored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import socket

        from mnemoseed_local.eval import m5prep_run_blind
        from mnemoseed_local.eval.experience_contract import parse_experience_raw

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "postscore")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))

        def _noop_connect(*args: object, **kwargs: object) -> object:
            return None

        monkeypatch.setattr(socket, "create_connection", _noop_connect)

        def _provoking(raw: dict, *, sources: list) -> object:
            socket.create_connection(("127.0.0.1", 1))  # counted, never a live connect
            return parse_experience_raw(raw, sources=sources)

        monkeypatch.setattr(m5prep_run_blind, "parse_experience_raw", _provoking)
        truth_path = tmp_path / "postscore-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = m5prep_run_blind.main(args)
        assert rc != 0
        assert _marker_path(log_path, entry["seq"], entry["truth_sha256"]).is_file()
        lines = _log_lines(log_path)
        assert len(lines) == 1
        assert lines[0]["verdict"] == "scored"
        assert not out_path.is_file()

    def test_reserve_crash_blocks_reinvoke(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval import m5prep_run_blind

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "crash")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))

        def _hard_crash(raw: dict, *, sources: list) -> object:
            raise RuntimeError("unhandled crash after reservation")

        monkeypatch.setattr(m5prep_run_blind, "parse_experience_raw", _hard_crash)
        truth_path = tmp_path / "crash-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        with pytest.raises(RuntimeError):
            m5prep_run_blind.main(args)
        marker = _marker_path(log_path, entry["seq"], entry["truth_sha256"])
        assert marker.is_file()
        assert not log_path.exists()
        assert not out_path.is_file()
        for _ in range(2):
            rc = m5prep_run_blind.main(args)
            assert rc != 0
            assert marker.is_file()
            assert not log_path.exists()
            assert not out_path.is_file()

    def test_marker_creation_fsyncs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import os

        from mnemoseed_local.eval.m5prep_run_blind import main

        real_fsync = os.fsync
        fsync_calls: list[int] = []

        def _record_fsync(fd: int) -> None:
            fsync_calls.append(fd)
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", _record_fsync)
        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "fsync")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(Path.cwd()))
        truth_path = tmp_path / "fsync-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc == 0
        marker = _marker_path(log_path, entry["seq"], entry["truth_sha256"])
        assert marker.is_file()
        assert fsync_calls, "marker reservation must fsync before proceeding"

    def test_missing_worktree_env_fails_before_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "nowt")
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.delenv("M5PREP_G_WORKTREE", raising=False)
        truth_path = tmp_path / "nowt-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc != 0
        assert not _marker_path(log_path, 1, entry["truth_sha256"]).exists()
        assert not log_path.exists()

    def test_worktree_is_canonical_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from mnemoseed_local.eval.m5prep_run_blind import main

        home, inputs_path, entry, seal_path, log_path, out_path = self._setup(tmp_path, "canon")
        real_cwd = Path.cwd()
        tortuous = real_cwd / ".a2-cwd" / ".."
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tortuous))  # type: ignore[arg-type]
        monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
        monkeypatch.setenv("M5PREP_G_SESSION", self.G2)
        monkeypatch.setenv("M5PREP_G_WORKTREE", str(real_cwd.resolve()))
        truth_path = tmp_path / "canon-scratch-truth.json"
        args = _base_args(inputs_path, truth_path, seal_path, log_path, out_path) + ["--seal-seq", "1"]
        rc = main(args)
        assert rc == 0
        marker = _marker_path(log_path, entry["seq"], entry["truth_sha256"])
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload["worktree"] == str(real_cwd.resolve())
        lines = _log_lines(log_path)
        assert lines[0]["worktree"] == str(real_cwd.resolve())
        assert out_path.is_file()
