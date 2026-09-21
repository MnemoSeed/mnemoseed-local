"""Executor slice for #206 single-vs-dual (eval-only, RED first, no network)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest


def _luna(**over: object):  # type: ignore[no-untyped-def]
    from mnemoseed_local.eval.sv_quality_execute import LunaSeat

    args: dict[str, object] = {
        "model": "luna-a",
        "base_url": "https://luna.example/v1",
        "api_key_env": "LUNA_API_KEY",
    }
    args.update(over)
    return LunaSeat(**args)  # type: ignore[arg-type]


def _config(tmp_path: Path, **over: object):  # type: ignore[no-untyped-def]
    from mnemoseed_local.eval.sv_quality_execute import build_executor_config

    args: dict[str, object] = {
        "run_root": tmp_path / "run",
        "repository_sha": "abc123",
        "port": 17891,
        "reflect_a": _luna(model="luna-a"),
        "reflect_b": _luna(model="luna-a"),
        "vote_b": _luna(model="luna-b"),
        "verifier": _luna(model="luna-v"),
    }
    args.update(over)
    return build_executor_config(**args)  # type: ignore[arg-type]


def _canary(recall: float | None, *, matched: int = 8, missed: int = 0, extra: int = 0):  # type: ignore[no-untyped-def]
    from mnemoseed_local.eval.metrics import CanaryMetrics

    return CanaryMetrics(
        facts_total=matched + missed,
        facts_matched=matched,
        canary_recall=recall,
        matched_fact_ids=tuple(f"m{i}" for i in range(matched)),
        missed_fact_ids=tuple(f"s{i}" for i in range(missed)),
        noise_pollution=0,
        polluting_nodes=(),
        core_yield=matched + extra,
        extra_core_nodes=tuple(f"x{i}" for i in range(extra)),
    )


def _cell_report(cell_id: str, material: str, recall: float | None, **over: object):  # type: ignore[no-untyped-def]
    from mnemoseed_local.eval.metrics import CostMetrics, VerifyMetrics
    from mnemoseed_local.eval.report import CellReport

    return CellReport(
        cell_id=cell_id,
        material=material,
        canary=_canary(recall, **over),
        verify=VerifyMetrics(None, 0, 0, 0, (), {}),
        cost=CostMetrics(1.0, 10, None, None, None),
    )


def _report(cells: list, skipped: list | None = None):  # type: ignore[no-untyped-def]
    from mnemoseed_local.eval.report import EvalReport

    return EvalReport(
        eval_version="v1.2",
        started_at="2026-09-21T00:00:00Z",
        cells=tuple(cells),
        skipped=tuple(skipped or ()),
    )


def test_cells_are_off_then_vote_without_seed() -> None:
    from mnemoseed_local.eval.sv_quality_execute import build_cells, build_executor_config

    config = build_executor_config(
        run_root=Path("x"),
        repository_sha="abc123",
        port=17891,
        reflect_a=_luna(model="luna-a"),
        reflect_b=_luna(model="luna-a"),
        vote_b=_luna(model="luna-b"),
        verifier=_luna(model="luna-v"),
    )
    cell_a, cell_b = build_cells(config)
    assert cell_a.ensemble == "off"
    assert cell_b.ensemble == "vote"
    assert cell_b.vote_b is not None
    for route in (cell_a.reflect, cell_b.reflect, cell_b.vote_b, cell_b.verifier):
        assert route is not None
        assert route.driver == "openai_compatible"
        assert "seed" not in dict(route.params)


def test_vote_b_distinct_and_fallback_and_muse_rejected(tmp_path: Path) -> None:

    with pytest.raises(ValueError):
        _config(tmp_path, vote_b=_luna(model="luna-a"))
    with pytest.raises(ValueError):
        _config(tmp_path, allow_fallback=True)
    with pytest.raises(ValueError):
        _config(tmp_path, reflect_a=_luna(model="muse-x"))


def test_24_materials_pairing_and_hash() -> None:
    from mnemoseed_local.eval.sv_quality_execute import build_materials
    from mnemoseed_local.eval.sv_quality_fixtures import (
        sv_canary_corpus_hash,
        sv_canary_material_ids,
    )

    materials = build_materials()
    assert len(materials) == 24
    assert [item.name for item in materials] == list(sv_canary_material_ids())
    assert len(sv_canary_corpus_hash()) == 64


def test_real_report_grouping_pairs_and_F1_gain_flips_verdict(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import (
        group_paired_canary,
    )

    config = _config(tmp_path)
    cell_a, cell_b = __import__(
        "mnemoseed_local.eval.sv_quality_execute", fromlist=["build_cells"]
    ).build_cells(config)
    materials = __import__(
        "mnemoseed_local.eval.sv_quality_execute", fromlist=["build_materials"]
    ).build_materials()
    names = [item.name for item in materials]
    cells = [_cell_report(cell_a.cell_id, name, 0.5, matched=4, missed=4, extra=4) for name in names]
    cells += [_cell_report(cell_b.cell_id, name, 1.0, matched=8, missed=0, extra=0) for name in names]
    metrics_a, metrics_b = group_paired_canary(_report(cells), cell_a.cell_id, cell_b.cell_id, names)
    assert len(metrics_a) == 24 and len(metrics_b) == 24
    from mnemoseed_local.eval.sv_quality_metrics import extraction_micro_f1

    assert extraction_micro_f1(metrics_b).f1 is not None
    assert extraction_micro_f1(metrics_b).f1 - extraction_micro_f1(metrics_a).f1 >= 0.05  # type: ignore[operator]


def test_report_skip_missing_duplicate_out_of_order_are_loud(tmp_path: Path) -> None:
    from mnemoseed_local.eval.report import SkippedCell
    from mnemoseed_local.eval.sv_quality_execute import build_cells, group_paired_canary
    from mnemoseed_local.eval.sv_quality_fixtures import sv_canary_material_ids

    config = _config(tmp_path)
    cell_a, cell_b = build_cells(config)
    names = list(sv_canary_material_ids())
    good = [_cell_report(cell_a.cell_id, n, 1.0) for n in names] + [
        _cell_report(cell_b.cell_id, n, 1.0) for n in names
    ]
    with pytest.raises(ValueError):
        group_paired_canary(
            _report(good[1:], [SkippedCell(cell_a.cell_id, "x")]), cell_a.cell_id, cell_b.cell_id, names
        )
    with pytest.raises(ValueError):
        group_paired_canary(_report(good[:-1]), cell_a.cell_id, cell_b.cell_id, names)
    with pytest.raises(ValueError):
        group_paired_canary(_report(good + [good[0]]), cell_a.cell_id, cell_b.cell_id, names)
    flipped = [_cell_report(cell_b.cell_id, n, 1.0) for n in names] + [
        _cell_report(cell_a.cell_id, n, 1.0) for n in names
    ]
    with pytest.raises(ValueError):
        group_paired_canary(_report(flipped), cell_a.cell_id, cell_b.cell_id, names)


def _decision_json(
    nomination_id: str, verdict: str, winner: str | None, loser: str | None, item: object, index: int
) -> str:  # type: ignore[no-untyped-def]
    item_any: dict = item  # type: ignore[assignment]
    return json.dumps(
        {
            "nomination_id": nomination_id,
            "profile_id": "sv-quality",
            "left_node_id": item_any["left"],
            "left_version": 3,
            "right_node_id": item_any["right"],
            "right_version": 7,
            "verdict": verdict,
            "winner_node_id": winner,
            "loser_node_id": loser,
            "target_node_ids": [item_any["left"], item_any["right"]],
            "evidence_event_ids": [index * 2 + 1, index * 2 + 2],
        }
    )


def _tracing_adjudication_factory(responses: dict[str, str], calls: list):  # type: ignore[no-untyped-def]
    def _factory(seat: object, *, role: str = "verify", **kwargs: object):  # type: ignore[no-untyped-def]
        class _Client:
            def chat(self, *, system: str, user: str) -> str:
                calls.append((role, system, user))
                payload = json.loads(user)
                key = str(payload.get("nomination_id", ""))
                return responses[key]

            def check(self) -> bool:
                return True

        return _Client()

    return _factory


def test_live_adjudication_counts_16_verify_32_vote(tmp_path: Path) -> None:
    from mnemoseed_local.dream.adjudicate import PAIR_ADJUDICATION_SYSTEM_PROMPT
    from mnemoseed_local.eval.sv_quality_execute import build_executor_config, run_executor
    from mnemoseed_local.eval.sv_quality_fixtures import all_fixtures

    calls: list = []
    responses: dict[str, str] = {}
    for index, item in enumerate(all_fixtures()):
        verdict = "conflict" if item.is_positive else "not_conflict"
        responses[f"{item.fixture_id}-nom"] = _decision_json(
            f"{item.fixture_id}-nom",
            verdict,
            item.expected_winner,
            item.expected_loser,
            {"left": item.left_node_id, "right": item.right_node_id},
            index,
        )
    matrix_calls: list = []

    def _matrix(cells: object, materials: object, **kwargs: object):  # type: ignore[no-untyped-def]
        matrix_calls.append((cells, materials))
        cells_list = list(cells)  # type: ignore[union-attr]
        names = [item.name for item in materials]  # type: ignore[union-attr]
        out = []
        for cell in cells_list:
            for name in names:
                out.append(_cell_report(cell.cell_id, name, 1.0))  # type: ignore[union-attr]
        return _report(out)

    def _factory(seat: object, **kwargs: object):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role", "verify"))

        class _Client:
            def chat(self, *, system: str, user: str) -> str:
                if role == "smoke":
                    return "{}"
                assert system == PAIR_ADJUDICATION_SYSTEM_PROMPT
                calls.append((role, system, user))
                payload = json.loads(user)
                return responses[str(payload.get("nomination_id", ""))]

            def check(self) -> bool:
                return True

        return _Client()

    config = build_executor_config(
        run_root=tmp_path / "run",
        repository_sha="abc123",
        port=17891,
        reflect_a=_luna(model="luna-a"),
        reflect_b=_luna(model="luna-a"),
        vote_b=_luna(model="luna-b"),
        verifier=_luna(model="luna-v"),
    )
    out = run_executor(config, seat_factory=_factory, matrix_runner=_matrix)  # type: ignore[arg-type]
    assert out["dry_run"] is False
    verify_calls = [call for call in calls if call[0] == "verify"]
    vote_calls = [call for call in calls if call[0] in ("vote_a", "vote_b")]
    assert len(verify_calls) == 16
    assert len(vote_calls) == 32
    stored = json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))
    assert stored["verdict"] == "fail"
    assert stored["recommendation"] == "recommend_single"


def test_degraded_shapes_never_fake_terminals(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import adjudicate_arm
    from mnemoseed_local.eval.sv_quality_fixtures import (
        adjudication_carrier_for,
        positive_fixtures,
    )
    from mnemoseed_local.storage.ports import Disposition

    item = positive_fixtures()[0]
    nomination, left, right = adjudication_carrier_for(item.fixture_id)
    bad_texts = ["not json", "", json.dumps({"verdict": "conflict"})]
    for text in bad_texts:
        row = adjudicate_arm("verify", [(text, "verify")], nomination, left, right)
        assert row.disposition is not Disposition.ACCEPTED
    mismatch = _decision_json(
        "wrong-nom",
        "conflict",
        item.expected_winner,
        item.expected_loser,
        {"left": item.left_node_id, "right": item.right_node_id},
        0,
    )
    row = adjudicate_arm("verify", [(mismatch, "verify")], nomination, left, right)
    assert row.disposition is Disposition.DEFERRED


def test_smoke_exactly_once_and_quota_stops_everything(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import (
        QuotaStop,
        run_executor,
    )

    calls: list[str] = []

    class _Seat:
        def chat(self, *, system: str, user: str) -> str:
            calls.append("smoke")
            return "{}"

        def check(self) -> bool:
            return True

    config = _config(tmp_path)

    def _factory(seat: object, **kwargs: object) -> _Seat:  # type: ignore[no-untyped-def]
        assert kwargs.get("max_tokens") == 16
        return _Seat()

    def _matrix(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append("matrix")
        raise AssertionError("must not reach matrix")

    with pytest.raises(QuotaStop):
        run_executor(config, seat_factory=_factory, matrix_runner=_matrix, quota_on_smoke=True)  # type: ignore[arg-type]
    assert calls.count("smoke") == 1
    assert "matrix" not in calls


def test_per_arm_fpr_and_safety_veto(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import run_executor

    def _matrix(cells: object, materials: object, **kwargs: object):  # type: ignore[no-untyped-def]
        cells_list = list(cells)  # type: ignore[union-attr]
        names = [item.name for item in materials]  # type: ignore[union-attr]
        out = []
        for cell in cells_list:
            for name in names:
                out.append(_cell_report(cell.cell_id, name, 1.0))  # type: ignore[union-attr]
        return _report(out)

    from mnemoseed_local.eval.sv_quality_fixtures import all_fixtures

    responses: dict[str, str] = {}
    for index, item in enumerate(all_fixtures()):
        # arm A perfect, arm B accepts one negative -> safety veto
        verdict = "conflict" if item.is_positive else "not_conflict"
        if item.fixture_id == "svq-neg-00":
            verdict = "conflict"
            winner, loser = item.left_node_id, item.right_node_id
        else:
            winner, loser = item.expected_winner, item.expected_loser
        responses[f"{item.fixture_id}-nom"] = _decision_json(
            f"{item.fixture_id}-nom",
            verdict,
            winner,
            loser,
            {"left": item.left_node_id, "right": item.right_node_id},
            index,
        )

    def _factory(seat: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if str(kwargs.get("role", "")) == "smoke":

            class _Smoke:
                def chat(self, *, system: str, user: str) -> str:
                    return "{}"

                def check(self) -> bool:
                    return True

            return _Smoke()

        class _Client:
            def chat(self, *, system: str, user: str) -> str:
                return responses[str(json.loads(user).get("nomination_id", ""))]

            def check(self) -> bool:
                return True

        return _Client()

    config = _config(tmp_path)
    out = run_executor(config, seat_factory=_factory, matrix_runner=_matrix)  # type: ignore[arg-type]
    assert json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))["verdict"] == "fail"


def test_per_arm_fpr_divergence_reaches_decide_distinctly(tmp_path: Path) -> None:
    import mnemoseed_local.eval.sv_quality_execute as execute_mod
    from mnemoseed_local.eval.sv_quality_execute import run_executor

    def _matrix(cells: object, materials: object, **kwargs: object):  # type: ignore[no-untyped-def]
        cells_list = list(cells)  # type: ignore[union-attr]
        names = [item.name for item in materials]  # type: ignore[union-attr]
        out = []
        for cell in cells_list:
            for name in names:
                out.append(_cell_report(cell.cell_id, name, 1.0))  # type: ignore[union-attr]
        return _report(out)

    from mnemoseed_local.eval.sv_quality_fixtures import all_fixtures

    verify_responses: dict[str, str] = {}
    vote_responses: dict[str, str] = {}
    for index, item in enumerate(all_fixtures()):
        info = {"left": item.left_node_id, "right": item.right_node_id}
        if item.is_positive:
            verify_responses[f"{item.fixture_id}-nom"] = _decision_json(
                f"{item.fixture_id}-nom", "conflict", item.expected_winner, item.expected_loser, info, index
            )
            vote_responses[f"{item.fixture_id}-nom"] = _decision_json(
                f"{item.fixture_id}-nom", "conflict", item.expected_winner, item.expected_loser, info, index
            )
        elif item.fixture_id in ("svq-neg-00", "svq-neg-01", "svq-neg-02", "svq-neg-03"):
            verify_responses[f"{item.fixture_id}-nom"] = _decision_json(
                f"{item.fixture_id}-nom", "conflict", item.left_node_id, item.right_node_id, info, index
            )
            vote_responses[f"{item.fixture_id}-nom"] = _decision_json(
                f"{item.fixture_id}-nom", "not_conflict", None, None, info, index
            )
        else:
            verify_responses[f"{item.fixture_id}-nom"] = _decision_json(
                f"{item.fixture_id}-nom", "not_conflict", None, None, info, index
            )
            vote_responses[f"{item.fixture_id}-nom"] = _decision_json(
                f"{item.fixture_id}-nom", "not_conflict", None, None, info, index
            )

    def _factory(seat: object, **kwargs: object):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role", "verify"))

        class _Client:
            def chat(self, *, system: str, user: str) -> str:
                if role == "smoke":
                    return "{}"
                table = verify_responses if role == "verify" else vote_responses
                return table[str(json.loads(user).get("nomination_id", ""))]

            def check(self) -> bool:
                return True

        return _Client()

    seen: dict = {}
    real_decide = execute_mod.decide

    def _recording(**kwargs: object):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        calls = seen.setdefault("calls", 0)
        seen["calls"] = calls + 1
        return real_decide(**kwargs)  # type: ignore[arg-type]

    execute_mod.decide = _recording  # type: ignore[method-assign]
    try:
        config = _config(tmp_path)
        out = run_executor(config, seat_factory=_factory, matrix_runner=_matrix)  # type: ignore[arg-type]
    finally:
        execute_mod.decide = real_decide  # type: ignore[method-assign]
    assert seen["calls"] == 1
    assert seen["single_fpr"] == pytest.approx(0.5)
    assert seen["dual_fpr"] == pytest.approx(0.0)
    assert seen["safety_failed"] is True
    stored = json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))
    assert stored["verdict"] == "fail"
    assert stored["recommendation"] == "single_off"


def test_secret_and_bearer_variants_abort(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import write_raw_pair

    with pytest.raises(ValueError):
        write_raw_pair(
            tmp_path,
            "svq-pos-00",
            0,
            {"response_text": "BEARER sk-live-secret"},
            api_key_value="sk-live-secret",
        )
    with pytest.raises(ValueError):
        write_raw_pair(
            tmp_path,
            "svq-pos-01",
            0,
            {"note": "api-key: sk-live-secret"},
            api_key_value="sk-live-secret",
        )
    with pytest.raises(ValueError):
        write_raw_pair(
            tmp_path,
            "svq-pos-02",
            0,
            {"url": "https://user:sk-live-secret@luna.example"},
            api_key_value="sk-live-secret",
        )


def test_manifest_costs_derive_per_arm_from_cell_reports(tmp_path: Path) -> None:
    from mnemoseed_local.eval.metrics import CostMetrics, VerifyMetrics
    from mnemoseed_local.eval.report import CellReport
    from mnemoseed_local.eval.sv_quality_execute import build_cells, run_executor
    from mnemoseed_local.eval.sv_quality_fixtures import all_fixtures

    config = _config(tmp_path)
    cell_a, cell_b = build_cells(config)

    def _costly(cell_id: str, material: str, duration: float, tokens: int, collapses: int) -> CellReport:
        return CellReport(
            cell_id=cell_id,
            material=material,
            canary=_canary(1.0),
            verify=VerifyMetrics(None, 0, 0, 0, (), {}),
            cost=CostMetrics(duration, tokens, None, None, None),
            reflect_collapse_attempts=collapses,
        )

    def _matrix(cells: object, materials: object, **kwargs: object):  # type: ignore[no-untyped-def]
        from mnemoseed_local.eval.report import EvalReport

        names = [item.name for item in materials]  # type: ignore[union-attr]
        out = [_costly(cell_a.cell_id, name, 11.5, 111, 1) for name in names]
        out += [_costly(cell_b.cell_id, name, 22.5, 222, 2) for name in names]
        return EvalReport(eval_version="v1.2", started_at="2026-09-21T00:00:00Z", cells=tuple(out))

    responses: dict[str, str] = {}
    for index, item in enumerate(all_fixtures()):
        verdict = "conflict" if item.is_positive else "not_conflict"
        responses[f"{item.fixture_id}-nom"] = _decision_json(
            f"{item.fixture_id}-nom",
            verdict,
            item.expected_winner,
            item.expected_loser,
            {"left": item.left_node_id, "right": item.right_node_id},
            index,
        )

    def _factory(seat: object, **kwargs: object):  # type: ignore[no-untyped-def]
        class _Client:
            def chat(self, *, system: str, user: str) -> str:
                if str(kwargs.get("role", "")) == "smoke":
                    return "{}"
                return responses[str(json.loads(user).get("nomination_id", ""))]

            def check(self) -> bool:
                return True

        return _Client()

    out = run_executor(config, seat_factory=_factory, matrix_runner=_matrix)  # type: ignore[arg-type]
    stored = json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))
    assert stored["tokens"] == {"single": 111 * 24, "dual": 222 * 24}
    assert stored["durations"] == {"single": 11.5 * 24, "dual": 22.5 * 24}
    assert stored["retries"] == {"single": 24, "dual": 48}
    assert stored["tokens"]["single"] != stored["tokens"]["dual"]


def test_secret_value_only_without_pattern_words_aborts(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import write_raw_pair

    with pytest.raises(ValueError):
        write_raw_pair(
            tmp_path,
            "svq-pos-03",
            0,
            {"note": "sk-live-secret-xyz-9"},
            api_key_value="sk-live-secret-xyz-9",
        )


def test_all_seats_secrets_list_scanned(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import write_raw_pair

    with pytest.raises(ValueError):
        write_raw_pair(
            tmp_path,
            "svq-pos-04",
            0,
            {"note": "second-seat-value-abc"},
            secrets=["second-seat-value-abc"],
        )


def test_raw_overwrite_refused(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import write_raw_pair

    payload = {"response_text": "ok"}
    write_raw_pair(tmp_path, "svq-pos-00", 0, payload)
    with pytest.raises(RuntimeError):
        write_raw_pair(tmp_path, "svq-pos-00", 0, payload)


def test_flaky_smoke_gets_single_attempt(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import QuotaStop, run_executor

    calls: list[str] = []

    class _Seat:
        def chat(self, *, system: str, user: str) -> str:
            calls.append("smoke")
            from mnemoseed_local.llm.types import LLMUnavailable

            raise LLMUnavailable("transport down")

        def check(self) -> bool:
            return True

    config = _config(tmp_path)

    def _factory(seat: object, **kwargs: object) -> _Seat:  # type: ignore[no-untyped-def]
        return _Seat()

    def _matrix(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise AssertionError("unreached")

    with pytest.raises(QuotaStop):
        run_executor(config, seat_factory=_factory, matrix_runner=_matrix)  # type: ignore[arg-type]
    assert calls == ["smoke"]


def test_degraded_counts_derive_from_pairing(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import run_executor

    def _matrix(cells: object, materials: object, **kwargs: object):  # type: ignore[no-untyped-def]
        cells_list = list(cells)  # type: ignore[union-attr]
        names = [item.name for item in materials]  # type: ignore[union-attr]
        out = []
        for cell in cells_list:
            for index, name in enumerate(names):
                recall = None if (cell.ensemble == "off" and index < 2) else 1.0  # type: ignore[union-attr]
                out.append(_cell_report(cell.cell_id, name, recall))  # type: ignore[union-attr]
        return _report(out)

    from mnemoseed_local.eval.sv_quality_fixtures import all_fixtures

    responses: dict[str, str] = {}
    for index, item in enumerate(all_fixtures()):
        verdict = "conflict" if item.is_positive else "not_conflict"
        responses[f"{item.fixture_id}-nom"] = _decision_json(
            f"{item.fixture_id}-nom",
            verdict,
            item.expected_winner,
            item.expected_loser,
            {"left": item.left_node_id, "right": item.right_node_id},
            index,
        )

    def _factory(seat: object, **kwargs: object):  # type: ignore[no-untyped-def]
        class _Client:
            def chat(self, *, system: str, user: str) -> str:
                if str(kwargs.get("role", "")) == "smoke":
                    return "{}"
                return responses[str(json.loads(user).get("nomination_id", ""))]

            def check(self) -> bool:
                return True

        return _Client()

    config = _config(tmp_path)
    out = run_executor(config, seat_factory=_factory, matrix_runner=_matrix)  # type: ignore[arg-type]
    stored = json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))
    assert stored["degraded"] == {"single": 2, "dual": 2}
    assert stored["verdict"] == "fail"
    assert stored["recommendation"] == "recommend_single"


def test_programming_errors_escape_quota(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_execute import run_executor

    config = _config(tmp_path)

    def _factory(seat: object, **kwargs: object):  # type: ignore[no-untyped-def]
        class _Client:
            def chat(self, *, system: str, user: str) -> str:
                raise TypeError("programmer bug")

            def check(self) -> bool:
                return True

        return _Client()

    def _matrix(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise AssertionError("unreached")

    with pytest.raises(TypeError):
        run_executor(config, seat_factory=_factory, matrix_runner=_matrix)  # type: ignore[arg-type]


def test_prereg_covers_all_seats_and_settings() -> None:
    from mnemoseed_local.eval.sv_quality_execute import prereg_for

    base_kwargs = {
        "prompt_version": "v1",
        "canary_seed": 2060901,
        "reflect_a": ("luna-a", "https://a"),
        "reflect_b": ("luna-a", "https://a"),
        "vote_b": ("luna-b", "https://b"),
        "verifier": ("luna-v", "https://v"),
        "material_ids": ("m",),
        "oracle_version": "v1",
        "denominator": "terminal-only-v1",
    }
    base = prereg_for(**base_kwargs)  # type: ignore[arg-type]
    for key, alt in [
        ("reflect_a", ("luna-x", "https://a")),
        ("reflect_b", ("luna-a", "https://z")),
        ("vote_b", ("luna-c", "https://b")),
        ("verifier", ("luna-v", "https://w")),
        ("prompt_version", "v2"),
        ("oracle_version", "v2"),
        ("denominator", "other"),
    ]:
        changed = dict(base_kwargs)
        changed[key] = alt
        assert prereg_for(**changed) != base  # type: ignore[arg-type]


def test_fpr_only_improvement_passes_without_f1_gain() -> None:
    from mnemoseed_local.eval.sv_quality_metrics import decide

    passing = decide(
        single_f1=0.70,
        dual_f1=0.70,
        single_fpr=0.50,
        dual_fpr=0.40,
        canary_informative=24,
        canary_total=24,
        positive_informative=8,
        positive_total=8,
        negative_informative=8,
        negative_total=8,
        safety_failed=False,
    )
    assert passing.verdict == "pass"
    assert passing.recommendation == "retain_dual"
    same = decide(
        single_f1=0.70,
        dual_f1=0.70,
        single_fpr=0.30,
        dual_fpr=0.30,
        canary_informative=24,
        canary_total=24,
        positive_informative=8,
        positive_total=8,
        negative_informative=8,
        negative_total=8,
        safety_failed=False,
    )
    assert same.verdict == "fail"


def test_vote_counts_cannot_decide() -> None:
    import mnemoseed_local.eval.sv_quality_execute as execute_mod

    assert "VoteMetrics" not in inspect.getsource(execute_mod)


def test_confirm_guard_and_no_public_cli() -> None:
    from mnemoseed_local.eval.sv_quality_execute import main

    assert main([]) == 2
    assert main(["--run-root", "x"]) == 2
    assert main(["--run-root", "x", "--confirm-luna-burn"]) == 2
    import mnemoseed_local.cli as cli_mod
    import mnemoseed_local.eval.__main__ as eval_main

    assert "sv_quality_execute" not in inspect.getsource(cli_mod)
    assert "sv_quality_execute" not in inspect.getsource(eval_main)
