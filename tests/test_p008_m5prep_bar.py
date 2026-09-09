"""M5prep bar-method tests (lane I, synthetic-only, no numeric bar).

Covers the I-owned surface of ``mnemoseed_local.eval.m5prep_bar``: token
normalization plus proposal validation, the source-constant guard, the SDT
shape oracle pinned to the dispatch Appendix-A mini-corpus, the claim
checker, the blindness leak-guard, and the zero-model isolation oracle.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from mnemoseed_local.eval import m5prep_bar

BAR_PATH = Path(m5prep_bar.__file__)
TEST_PATH = Path(__file__)
REPO_ROOT = BAR_PATH.parents[3]
DOC_PATH = REPO_ROOT / "docs" / "p008-m5prep-bar-method.md"
DOC_ZH_PATH = REPO_ROOT / "docs" / "zh" / "p008-m5prep-bar-method.md"
DEV_PATH = REPO_ROOT / "tests" / "fixtures" / "p008_m5prep" / "dev_inputs.json"
SCRATCH_SDT_PATH = REPO_ROOT / "tests" / "fixtures" / "p008_m5prep_scratch" / "sdt_paraphrase.json"

HEX64_RE = re.compile(r"[0-9a-f]{64}")

# Forbidden-pattern fragments are assembled dynamically so this file never
# carries the guarded literals itself.
_SQ = "sequestered" + "_"
_OM = "m5prep" + "_" + "oracle"


def _scan_text_for_leaks(text: str) -> list[str]:
    findings: list[str] = []
    if _SQ in text:
        findings.append("blindness-breach:" + _SQ)
    if _OM in text:
        findings.append("blindness-breach:" + _OM)
    if HEX64_RE.search(text):
        findings.append("hash-probe")
    return findings


def _import_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


# ---------------------------------------------------------------------------
# Shared AST predicate for the source-constant guard (dispatch section 8).
# ---------------------------------------------------------------------------


def _normalized_key_matches_banned(token: str) -> bool:
    return any(member in token for member in m5prep_bar.BANNED_BAR_TOKENS)


def _name_is_exempt(name: str) -> bool:
    return name.upper() in m5prep_bar.BENIGN_NUMERIC_NAMES


def _key_is_allowlisted(key: str) -> bool:
    return key.lower() in m5prep_bar.BENIGN_PROPOSAL_KEYS


def _check_tree_for_numeric_bars(tree: ast.AST) -> list[str]:
    findings: list[str] = []

    def check_name(name: str) -> str | None:
        if _name_is_exempt(name):
            return None
        token = m5prep_bar.normalize_bar_token(name)
        if _normalized_key_matches_banned(token):
            return "numeric-threshold-constant:" + name
        return None

    def check_key(key: str) -> str | None:
        if _key_is_allowlisted(key) or _name_is_exempt(key):
            return None
        token = m5prep_bar.normalize_bar_token(key)
        if _normalized_key_matches_banned(token):
            return "numeric-threshold-constant:" + key
        return None

    def operand_name(node: ast.AST) -> str | None:
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
                targets = list(node.targets)
                values = [node.value]
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                values = [node.value] if node.value is not None else []
            else:
                targets = [node.target]
                values = [node.value]
            for target in targets:
                if isinstance(target, ast.Name):
                    for value in values:
                        if (
                            isinstance(value, ast.Constant)
                            and isinstance(value.value, (int, float))
                            and not isinstance(value.value, bool)
                        ):
                            hit = check_name(target.id)
                            if hit:
                                findings.append(hit)
                        if isinstance(value, ast.Name):
                            hit = check_name(target.id) or check_name(value.id)
                            if hit:
                                findings.append(hit)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pos_args = node.args.args
            pos_defaults = list(node.args.defaults)
            start = len(pos_args) - len(pos_defaults)
            pending = [(arg, default) for arg, default in zip(pos_args[start:], pos_defaults, strict=True)]
            pending.extend(zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True))
            for arg, default in pending:
                if default is None:
                    continue
                if (
                    isinstance(default, ast.Constant)
                    and isinstance(default.value, (int, float))
                    and not isinstance(default.value, bool)
                ):
                    hit = check_name(arg.arg)
                    if hit:
                        findings.append(hit)
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            has_numeric = any(
                isinstance(s, ast.Constant)
                and isinstance(s.value, (int, float))
                and not isinstance(s.value, bool)
                for s in sides
            )
            if has_numeric:
                for side in sides:
                    name = operand_name(side)
                    if name is not None:
                        hit = check_name(name)
                        if hit:
                            findings.append(hit)
        elif isinstance(node, ast.IfExp):
            arms = [node.body, node.orelse]
            has_numeric_arm = any(
                isinstance(a, ast.Constant)
                and isinstance(a.value, (int, float))
                and not isinstance(a.value, bool)
                for a in arms
            )
            if has_numeric_arm:
                for sub in ast.walk(node.test):
                    name = operand_name(sub)
                    if name is not None:
                        hit = check_name(name)
                        if hit:
                            findings.append(hit)
        elif isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key_node, ast.Constant)
                    and isinstance(key_node.value, str)
                    and isinstance(value_node, ast.Constant)
                    and isinstance(value_node.value, (int, float))
                    and not isinstance(value_node.value, bool)
                ):
                    hit = check_key(key_node.value)
                    if hit:
                        findings.append(hit)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg is not None
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, (int, float))
                    and not isinstance(keyword.value.value, bool)
                ):
                    hit = check_name(keyword.arg)
                    if hit:
                        findings.append(hit)
            func = node.func
            func_name: str | None = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name is not None and not _name_is_exempt(func_name):
                token = m5prep_bar.normalize_bar_token(func_name)
                if _normalized_key_matches_banned(token):
                    for arg in node.args:
                        if (
                            isinstance(arg, ast.Constant)
                            and isinstance(arg.value, (int, float))
                            and not isinstance(arg.value, bool)
                        ):
                            findings.append("numeric-threshold-constant:" + func_name)
                            break
    return findings


def _check_source_for_numeric_bars(source: str) -> list[str]:
    return _check_tree_for_numeric_bars(ast.parse(source))


# ---------------------------------------------------------------------------
# Token normalization.
# ---------------------------------------------------------------------------


class TestNormalizeBarToken:
    def test_mixed_separators_unify(self) -> None:
        assert m5prep_bar.normalize_bar_token("Cut_Off") == "cutoff"
        assert m5prep_bar.normalize_bar_token("CUT_OFF") == "cutoff"
        assert m5prep_bar.normalize_bar_token("cut_off") == "cutoff"
        assert m5prep_bar.normalize_bar_token("Target-P") == "targetp"
        assert m5prep_bar.normalize_bar_token("targetp") == "targetp"
        assert m5prep_bar.normalize_bar_token("target_p") == "targetp"
        assert m5prep_bar.normalize_bar_token("MIN_P") == "minp"
        assert m5prep_bar.normalize_bar_token("MIN-P") == "minp"
        assert m5prep_bar.normalize_bar_token("min_p") == "minp"

    def test_compound_names_strip(self) -> None:
        assert m5prep_bar.normalize_bar_token("Quality_Gate") == "qualitygate"
        assert m5prep_bar.normalize_bar_token("Gate_Result") == "gateresult"
        assert m5prep_bar.normalize_bar_token("passBar") == "passbar"

    def test_benign_names_stable(self) -> None:
        assert m5prep_bar.normalize_bar_token("method") == "method"
        assert m5prep_bar.normalize_bar_token("n_cases") == "ncases"
        assert m5prep_bar.normalize_bar_token("Version") == "version"


# ---------------------------------------------------------------------------
# Proposal validation.
# ---------------------------------------------------------------------------


class TestValidateBarProposal:
    @pytest.mark.parametrize(
        "proposal",
        [
            {"target_p": 0.9},
            {"targetp": 0.85},
            {"Target-P": 0.7},
            {"cut_off": 0.5},
            {"MIN-P": 0.7},
            {"CUT_OFF": 0.8},
            {"min_p": 0.75},
            {"TARGET_R": 0.8},
            {"quality_gate": "strict"},
            {"gate_result": 1},
            {"criterion": 0.5},
            {"threshold": 0.6},
            {"dprime": 1.2},
            {"limit": 5},
            {"pass": True},
            {"verdict": "good"},
        ],
    )
    def test_numeric_bar_keys_rejected(self, proposal: dict) -> None:
        key = next(iter(proposal))
        assert m5prep_bar.validate_bar_proposal(proposal) == ["numeric-bar-rejected:" + key]

    def test_benign_proposal_passes(self) -> None:
        assert m5prep_bar.validate_bar_proposal({"version": 1, "n_cases": 24, "min_cases": 24}) == []

    def test_allowlist_case_insensitive_values_unrestricted(self) -> None:
        proposal = {
            "Method": "one-shot SDT shape with threshold 0.9 cutoff discussion",
            "N_CASES": "twenty-four",
            "Report_Fields": ["hit", "miss"],
            "NOT_OBSERVED": ["live resource impact"],
            "required_gates": {"gate_result": "pending"},
            "forbidden_claims": ["m5 ratified"],
            "corpus_size": 0.5,
        }
        assert m5prep_bar.validate_bar_proposal(proposal) == []

    def test_empty_proposal_passes(self) -> None:
        assert m5prep_bar.validate_bar_proposal({}) == []

    def test_values_never_scanned(self) -> None:
        assert m5prep_bar.validate_bar_proposal({"method": "cutoff 0.9"}) == []


class TestBannedSetCanonical:
    def test_canonical_set_exact(self) -> None:
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

    def test_benign_numeric_names_exact(self) -> None:
        assert m5prep_bar.BENIGN_NUMERIC_NAMES == frozenset({"BIN_EDGES", "N_BINS", "VERSION", "N_DECILES"})


# ---------------------------------------------------------------------------
# Source-constant guard over the shipped module.
# ---------------------------------------------------------------------------


class TestSourceConstantGuard:
    def test_shipped_module_clean(self) -> None:
        assert _check_source_for_numeric_bars(BAR_PATH.read_text("utf-8")) == []

    @pytest.mark.parametrize(
        "source",
        [
            "TARGET_R = 0.8",
            "CUT_OFF = 0.8",
            "MIN_P = 0.75",
            "TARGET_R: float = 0.8",
            "def f(limit=0.75):\n    return limit",
            "async def f(limit=0.75):\n    return limit",
            "def f(*, threshold=0.6):\n    return threshold",
            "if threshold > 0.8:\n    x = 1",
            "if 0.8 < cutoff:\n    x = 1",
            "if self.limit > 0.5:\n    x = 1",
            'config = {"cutoff": 0.5}',
            "f(threshold=0.6)",
            "threshold(0.6)",
            "ALIAS = TARGET_P",
            "TARGET_P = ALIAS",
            "x = 1 if threshold else 0",
            "x = 0.5 if cutoff > 1 else y",
            "(cutoff := 0.5)",
        ],
    )
    def test_injected_numeric_bars_flagged(self, source: str) -> None:
        findings = _check_source_for_numeric_bars(source)
        assert findings and all(f.startswith("numeric-threshold-constant:") for f in findings)

    @pytest.mark.parametrize(
        "source",
        [
            "N_BINS = 10",
            "BIN_EDGES = [0.0, 0.1]",
            "VERSION = 1",
            "N_DECILES = 10",
            "n_cases = 24",
            "x = 5",
            "def f(n=10):\n    return n",
            "range(10)",
            'config = {"version": 1}',
            'config = {"n_cases": 24}',
            'row = {"n": 0, "accuracy": None}',
            "if count > 3:\n    x = 1",
            "total = hit + miss",
        ],
    )
    def test_benign_numerics_pass(self, source: str) -> None:
        assert _check_source_for_numeric_bars(source) == []


# ---------------------------------------------------------------------------
# Blindness leak-guard (I-owned module plus I-owned tests only).
# ---------------------------------------------------------------------------


class TestBlindnessLeakGuard:
    def test_module_and_tests_carry_no_guarded_literals(self) -> None:
        for path in (BAR_PATH, TEST_PATH):
            assert _scan_text_for_leaks(path.read_text("utf-8")) == []

    def test_no_guarded_imports(self) -> None:
        for path in (BAR_PATH, TEST_PATH):
            tree = ast.parse(path.read_text("utf-8"))
            for name in _import_names(tree):
                assert _OM not in name

    def test_injected_open_of_guarded_path_detected(self) -> None:
        poisoned = "payload = open(" + repr(_SQ + "truth.json") + ").read()"
        assert _scan_text_for_leaks(poisoned)

    def test_injected_guarded_import_detected(self) -> None:
        poisoned = "from " + _OM + " import blind_score"
        assert _scan_text_for_leaks(poisoned)

    def test_injected_hash_probe_detected(self) -> None:
        assert _scan_text_for_leaks("digest = " + repr("a" * 64))

    def test_fresh_interpreter_sees_no_guarded_module(self) -> None:
        probe = (
            "import sys; "
            "from mnemoseed_local.eval import m5prep_bar; "
            "guarded = " + repr(_OM) + "; "
            "leak = " + repr(_SQ) + "; "
            "hits = [m for m in sys.modules if m == guarded or leak in m]; "
            "print(len(hits))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "0"


# ---------------------------------------------------------------------------
# SDT shape oracle pinned to the dispatch Appendix-A mini-corpus.
# ---------------------------------------------------------------------------


def _evidence(*ids: str) -> list[dict]:
    return [{"kind": "chunk", "id": value} for value in ids]


def _candidate(
    candidate_id: str,
    cls: str,
    scope: str,
    payload: dict,
    evidence: list[dict],
    confidence: float | None = None,
) -> dict:
    candidate: dict = {
        "candidate_id": candidate_id,
        "class": cls,
        "scope": scope,
        "payload": payload,
        "evidence": evidence,
    }
    if confidence is not None:
        candidate["confidence"] = confidence
    return candidate


def _accepted_envelope(case_id: str, candidates: list[dict]) -> dict:
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


def _zero_envelope(case_id: str, reason: str) -> dict:
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


def _truth_unit(cls: str, scope: str, payload: dict, evidence: list[dict], group: str | None = None) -> dict:
    unit: dict = {
        "class": cls,
        "scope": scope,
        "payload": payload,
        "evidence": evidence,
        "disposition": "accepted",
    }
    if group is not None:
        unit["paraphrase_group_id"] = group
    return unit


def appendix_a_predicted() -> list[dict]:
    fact_a1 = {"subject": "Ada", "predicate": "prefers", "object": "tea", "polarity": "positive"}
    fact_a4 = {"subject": "Bob", "predicate": "decided", "object": "retry", "polarity": "positive"}
    return [
        _accepted_envelope("c1", [_candidate("a1", "fact", "s1", fact_a1, _evidence("e1"), 0.9)]),
        _zero_envelope("c2", "no-match"),
        _accepted_envelope(
            "c3",
            [_candidate("x3", "lesson", "s1", {"text": "novel remark"}, _evidence("e3x"), 0.2)],
        ),
        _accepted_envelope("c4", [_candidate("a4", "lesson", "s1", fact_a4, _evidence("e4"), 0.7)]),
        _zero_envelope("c5", "no-match"),
    ]


def appendix_a_truth() -> list[dict]:
    fact_a1 = {"subject": "Ada", "predicate": "prefers", "object": "tea", "polarity": "positive"}
    fact_a4 = {"subject": "Bob", "predicate": "decided", "object": "retry", "polarity": "positive"}
    return [
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
            "expected_units": [_truth_unit("lesson", "s1", {"text": "check logs first"}, _evidence("e2"))],
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


def appendix_a_expected() -> dict:
    return {
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


class TestReportSDTAppendixA:
    def test_hand_computed_oracle_matches_byte_exact(self) -> None:
        assert m5prep_bar.report_sdt(appendix_a_predicted(), appendix_a_truth()) == appendix_a_expected()

    def test_flattened_candidates_rejected_malformed(self) -> None:
        flattened = [[_candidate("a1", "fact", "s1", {"text": "x"}, _evidence("e1"))]]
        with pytest.raises(ValueError, match="malformed"):
            m5prep_bar.report_sdt(flattened, appendix_a_truth())

    def test_result_envelope_as_list_rejected_malformed(self) -> None:
        predicted = [{"case_id": "c1", "result": []}]
        with pytest.raises(ValueError, match="malformed"):
            m5prep_bar.report_sdt(predicted, appendix_a_truth())

    def test_entry_missing_result_rejected_malformed(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            m5prep_bar.report_sdt([{"case_id": "c1"}], appendix_a_truth())

    def test_duplicate_predicted_case_rejected(self) -> None:
        predicted = appendix_a_predicted() + [appendix_a_predicted()[0]]
        with pytest.raises(ValueError, match="malformed"):
            m5prep_bar.report_sdt(predicted, appendix_a_truth())

    def test_unexpected_case_rejected(self) -> None:
        predicted = appendix_a_predicted() + [_zero_envelope("c9", "no-match")]
        with pytest.raises(ValueError, match="malformed"):
            m5prep_bar.report_sdt(predicted, appendix_a_truth())


class TestReportSDTBehavior:
    def test_shared_group_matches_across_text(self) -> None:
        scratch = json.loads(SCRATCH_SDT_PATH.read_text("utf-8"))
        report = m5prep_bar.report_sdt(scratch["predicted"], scratch["truth"])
        assert report["experience"] == {"hit": 1, "miss": 0, "false_alarm": 0}
        assert report["correct_rejection"] == 0

    def test_wrong_provenance_is_false_alarm_never_scored(self) -> None:
        payload = {"subject": "Ada", "predicate": "prefers", "object": "tea", "polarity": "positive"}
        predicted = [
            _accepted_envelope(
                "c1",
                [_candidate("a1", "fact", "s1", payload, _evidence("other"), 0.99)],
            )
        ]
        truth = [
            {
                "case_id": "c1",
                "category": "fact",
                "expected_status": "accepted",
                "zero_reason": None,
                "expected_units": [_truth_unit("fact", "s1", payload, _evidence("e1"))],
            }
        ]
        report = m5prep_bar.report_sdt(predicted, truth)
        assert report["fact"] == {"hit": 0, "miss": 1, "false_alarm": 1}
        assert report["confidence_deciles"][9] == {"bin": [0.9, 1.0], "n": 1, "accuracy": 0.0}

    def test_confidence_passthrough_never_masks_provenance(self) -> None:
        payload = {"subject": "Ada", "predicate": "prefers", "object": "tea", "polarity": "positive"}
        predicted = [
            _accepted_envelope("c1", [_candidate("a1", "fact", "s1", payload, _evidence("e1"), 0.01)])
        ]
        truth = [
            {
                "case_id": "c1",
                "category": "fact",
                "expected_status": "accepted",
                "zero_reason": None,
                "expected_units": [_truth_unit("fact", "s1", payload, _evidence("e1"))],
            }
        ]
        report = m5prep_bar.report_sdt(predicted, truth)
        assert report["fact"] == {"hit": 1, "miss": 0, "false_alarm": 0}
        assert report["confidence_deciles"][0] == {"bin": [0.0, 0.1], "n": 1, "accuracy": 1.0}

    def test_unscored_confidences_excluded_from_bins(self) -> None:
        payload = {"text": "check logs first"}
        candidates = [
            _candidate("u1", "lesson", "s1", payload, _evidence("e2")),
            _candidate("u2", "lesson", "s1", payload, _evidence("e2"), 2.0),
            _candidate("u3", "lesson", "s1", payload, _evidence("e2"), -0.5),
        ]
        outward = _candidate("u4", "lesson", "s1", payload, _evidence("e2"), 1.0)
        candidates.append(outward)
        predicted = [_accepted_envelope("c2", candidates)]
        truth = [
            {
                "case_id": "c2",
                "category": "lesson",
                "expected_status": "accepted",
                "zero_reason": None,
                "expected_units": [
                    _truth_unit("lesson", "s1", payload, _evidence("e2")),
                    _truth_unit("lesson", "s1", payload, _evidence("e2")),
                    _truth_unit("lesson", "s1", payload, _evidence("e2")),
                    _truth_unit("lesson", "s1", payload, _evidence("e2")),
                ],
            }
        ]
        report = m5prep_bar.report_sdt(predicted, truth)
        assert report["unscored_units"] == 3
        assert report["confidence_deciles"][9] == {"bin": [0.9, 1.0], "n": 1, "accuracy": 1.0}

    def test_misclass_entry_shape(self) -> None:
        report = m5prep_bar.report_sdt(appendix_a_predicted(), appendix_a_truth())
        assert report["misclass"] == [
            {"case_id": "c4", "expected_class": "fact", "predicted_class": "lesson"}
        ]

    def test_noise_case_with_two_units_counts_two_alarms(self) -> None:
        predicted = [
            _accepted_envelope(
                "c3",
                [
                    _candidate("x1", "lesson", "s1", {"text": "one"}, _evidence("a"), 0.4),
                    _candidate("x2", "lesson", "s1", {"text": "two"}, _evidence("b"), 0.5),
                ],
            )
        ]
        truth = [
            {
                "case_id": "c3",
                "category": "noise",
                "expected_status": "unresolved",
                "zero_reason": "no-match",
                "expected_units": [],
            }
        ]
        report = m5prep_bar.report_sdt(predicted, truth)
        assert report["experience"]["false_alarm"] == 2
        assert report["correct_rejection"] == 0


# ---------------------------------------------------------------------------
# Claim checker plus vendored phrase fidelity.
# ---------------------------------------------------------------------------


EXPECTED_VENDORED_PHRASES = (
    "real model quality is",
    "generaliz",
    "production-ready",
    "promotion gate passed",
    "efficacy",
    "simulat",
    "recall improv",
    "extraction precision",
)


class TestCheckReportClaims:
    def test_vendored_tuple_matches_appendix_c(self) -> None:
        assert m5prep_bar.VENDORED_BANNED_CLAIM_PHRASES == EXPECTED_VENDORED_PHRASES

    def test_vendored_canonical_hash(self) -> None:
        canonical = "\n".join(m5prep_bar.VENDORED_BANNED_CLAIM_PHRASES).encode("utf-8")
        expected = (
            "98fd2f1f"
            + "93425b57"
            + "30947231"
            + "88f0188c"
            + "ec297124"
            + "1711153f"
            + "8f39a262"
            + "7d077f59"
        )
        assert hashlib.sha256(canonical).hexdigest() == expected

    @pytest.mark.parametrize(
        ("prose", "phrase"),
        [
            ("real model quality is high", "real model quality is"),
            ("generalization improves", "generaliz"),
            ("the system is production-ready", "production-ready"),
            ("promotion gate passed review", "promotion gate passed"),
            ("high efficacy shown", "efficacy"),
            ("simulation results", "simulat"),
            ("recall improvement noted", "recall improv"),
            ("extraction precision rose", "extraction precision"),
            ("the m5 ratified milestone", "m5 ratified"),
            ("the change landed", "landed"),
            ("M5 RATIFIED", "m5 ratified"),
            ("LANDED", "landed"),
        ],
    )
    def test_biting_prose_flagged(self, prose: str, phrase: str) -> None:
        outcome = m5prep_bar.check_report_claims(prose)
        assert phrase in outcome["hits"]

    def test_clean_method_prose_passes_with_topics(self) -> None:
        prose = (
            "Future ratification would use a sequestered one-shot run plus the "
            "SDT-shape summary. Open topics: natural-language extraction quality: "
            "NOT_OBSERVED; real model quality: NOT_OBSERVED; real-data "
            "coverage/eligibility: NOT_OBSERVED; live resource impact: NOT_OBSERVED. "
            "No number is proposed here."
        )
        assert m5prep_bar.check_report_claims(prose) == {
            "hits": [],
            "missing_topics": [],
        }

    def test_missing_topics_reported(self) -> None:
        outcome = m5prep_bar.check_report_claims("A short note with no topic list.")
        assert outcome["hits"] == []
        assert outcome["missing_topics"] == [
            "natural-language extraction quality",
            "real model quality",
            "real-data coverage/eligibility",
            "live resource impact",
        ]

    def test_shipped_docs_pass_claim_check(self) -> None:
        for path in (DOC_PATH, DOC_ZH_PATH):
            outcome = m5prep_bar.check_report_claims(path.read_text("utf-8"))
            assert outcome == {"hits": [], "missing_topics": []}


# ---------------------------------------------------------------------------
# Zero-model isolation.
# ---------------------------------------------------------------------------


class TestZeroModelIsolation:
    GUARDED_IMPORT_ROOTS = ("socket", "urllib", "http", "ssl", "daemon", "hosts")

    def test_module_imports_only_neutral_stdlib(self) -> None:
        tree = ast.parse(BAR_PATH.read_text("utf-8"))
        allowed = {"unicodedata", "typing", "__future__"}
        for name in _import_names(tree):
            root = name.split(".")[0]
            assert root in allowed, name
            assert root not in self.GUARDED_IMPORT_ROOTS

    def test_module_carries_no_port_or_env_hooks(self) -> None:
        source = BAR_PATH.read_text("utf-8")
        assert "7788" not in source
        assert "environ" not in source
        assert "getenv" not in source

    def test_fresh_interpreter_loads_no_guarded_modules(self) -> None:
        probe = (
            "import sys; "
            "from mnemoseed_local.eval import m5prep_bar; "
            "roots = ('socket', 'urllib', 'http', 'ssl'); "
            "hits = [m for m in sys.modules if m.split('.')[0] in roots]; "
            "print(len(hits))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "0"

    def test_guarded_entry_points_observe_zero_calls(self) -> None:
        import http.client
        import socket
        import urllib.request

        counters = {"connect": 0, "create": 0, "urlopen": 0, "request": 0}
        real_connect = socket.socket.connect
        real_create = socket.create_connection
        real_urlopen = urllib.request.urlopen
        real_request = http.client.HTTPConnection.request

        def counting_connect(self, *args: object, **kwargs: object) -> object:
            counters["connect"] += 1
            return real_connect(self, *args, **kwargs)

        def counting_create(*args: object, **kwargs: object) -> object:
            counters["create"] += 1
            return real_create(*args, **kwargs)

        def counting_urlopen(*args: object, **kwargs: object) -> object:
            counters["urlopen"] += 1
            return real_urlopen(*args, **kwargs)

        def counting_request(self: object, *args: object, **kwargs: object) -> object:
            counters["request"] += 1
            return real_request(self, *args, **kwargs)  # type: ignore[arg-type]

        socket.socket.connect = counting_connect  # type: ignore[method-assign]
        socket.create_connection = counting_create  # type: ignore[assignment]
        urllib.request.urlopen = counting_urlopen  # type: ignore[assignment]
        http.client.HTTPConnection.request = counting_request  # type: ignore[method-assign]
        try:
            m5prep_bar.report_sdt(appendix_a_predicted(), appendix_a_truth())
            m5prep_bar.validate_bar_proposal({"version": 1, "n_cases": 24})
            m5prep_bar.check_report_claims("quiet note")
            m5prep_bar.normalize_bar_token("Cut_Off")
        finally:
            socket.socket.connect = real_connect  # type: ignore[method-assign]
            socket.create_connection = real_create  # type: ignore[assignment]
            urllib.request.urlopen = real_urlopen  # type: ignore[assignment]
            http.client.HTTPConnection.request = real_request  # type: ignore[method-assign]
        assert counters == {"connect": 0, "create": 0, "urlopen": 0, "request": 0}


# ---------------------------------------------------------------------------
# Public dev-split presence (shape only, never scored as held-out).
# ---------------------------------------------------------------------------


class TestDevSplitPresence:
    def test_dev_inputs_present_and_shaped(self) -> None:
        payload = json.loads(DEV_PATH.read_text("utf-8"))
        assert payload["version"] == 1
        assert len(payload["cases"]) >= 12
