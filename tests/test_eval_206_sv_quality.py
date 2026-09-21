"""Single-vs-dual quality prep after QA remediation (issue 206, eval-only)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

FROZEN_STAGED_HASH = "FDDEA62462EEC219EFDC291E8F6C924484361194D1E95A7C81CAA3FC4BC0CED2"


def test_staged_excluded_hash_is_owner_frozen_literal() -> None:
    from mnemoseed_local.eval.sv_quality_manifest import (
        STAGED_EXCLUDED_HASH,
        STAGED_EXCLUDED_PATH,
    )

    assert STAGED_EXCLUDED_PATH == "tests/test_eval_206_true_conflicts.py"
    assert STAGED_EXCLUDED_HASH == FROZEN_STAGED_HASH
    assert len(STAGED_EXCLUDED_HASH) == 64


def test_fixture_corpus_frozen_counts_and_shapes() -> None:
    from mnemoseed_local.eval.sv_quality_fixtures import (
        negative_fixtures,
        positive_fixtures,
    )

    assert len(positive_fixtures()) == 8
    assert len(negative_fixtures()) == 8
    shapes = {item.shape for item in negative_fixtures()}
    assert {
        "single_side",
        "polarity",
        "stale_version",
        "no_exact_identity",
        "protected",
        "object_level",
    } <= shapes


def test_all_negatives_expect_terminal_rejected() -> None:
    from mnemoseed_local.eval.sv_quality_fixtures import negative_fixtures
    from mnemoseed_local.storage.ports import Disposition

    for item in negative_fixtures():
        assert item.is_positive is False
        assert item.expected_disposition is Disposition.REJECTED
        assert item.expected_winner is None
        assert item.expected_loser is None


def _adjudication_input(fixture_id: str, verdict: str):  # type: ignore[no-untyped-def]
    from mnemoseed_local.eval.sv_quality_fixtures import adjudication_input_for

    return adjudication_input_for(fixture_id, verdict)


def test_agreeing_stub_seats_hit_expected_terminal() -> None:
    from mnemoseed_local.dream.adjudicate import adjudicate
    from mnemoseed_local.eval.sv_quality_fixtures import (
        negative_fixtures,
        positive_fixtures,
    )
    from mnemoseed_local.storage.ports import Disposition

    for item in positive_fixtures():
        result = adjudicate(_adjudication_input(item.fixture_id, "conflict"))
        assert result.disposition is Disposition.ACCEPTED
    for item in negative_fixtures():
        result = adjudicate(_adjudication_input(item.fixture_id, "not_conflict"))
        assert result.disposition is Disposition.REJECTED


def test_seat_disagreement_defers() -> None:
    from mnemoseed_local.dream.adjudicate import adjudicate
    from mnemoseed_local.eval.sv_quality_fixtures import (
        adjudication_input_for_mixed,
        positive_fixtures,
    )
    from mnemoseed_local.storage.ports import Disposition

    item = positive_fixtures()[0]
    result = adjudicate(adjudication_input_for_mixed(item.fixture_id))
    assert result.disposition is Disposition.DEFERRED


def test_canary_informative_counts_sessions_not_facts() -> None:
    from mnemoseed_local.eval.metrics import CanaryMetrics
    from mnemoseed_local.eval.sv_quality_metrics import canary_informative_count

    def _row(recall: float | None, total: int) -> CanaryMetrics:
        return CanaryMetrics(
            facts_total=total,
            facts_matched=0,
            canary_recall=recall,
            matched_fact_ids=(),
            missed_fact_ids=(),
            noise_pollution=0,
            polluting_nodes=(),
            core_yield=0,
            extra_core_nodes=(),
        )

    rows = [_row(0.5, 1000), _row(None, 1000), _row(1.0, 1)]
    assert canary_informative_count(rows) == 2
    assert canary_informative_count([]) == 0


def test_canary_gate_boundary_17_vs_18() -> None:
    from mnemoseed_local.eval.sv_quality_metrics import decide

    def _decide(informative: int):  # type: ignore[no-untyped-def]
        return decide(
            single_f1=0.6,
            dual_f1=0.9,
            single_fpr=0.5,
            dual_fpr=0.1,
            canary_informative=informative,
            canary_total=24,
            positive_informative=8,
            positive_total=8,
            negative_informative=8,
            negative_total=8,
            safety_failed=False,
        )

    assert _decide(17).verdict == "indeterminate"
    assert _decide(18).verdict == "pass"


def test_canary_24_sessions_through_existing_api() -> None:
    from mnemoseed_local.eval.canary import canary_sessions
    from mnemoseed_local.eval.sv_quality_fixtures import (
        SV_QUALITY_CANARY_SEED,
        sv_canary_corpus_hash,
        sv_canary_material_ids,
        sv_canary_sessions,
    )

    assert inspect.signature(canary_sessions).parameters.keys() >= {
        "seed",
        "sessions",
        "facts_per_session",
        "noise_per_session",
    }
    sessions = sv_canary_sessions()
    assert len(sessions) == 24
    assert sv_canary_material_ids() == tuple(item.session_id for item in sessions)
    assert len(set(sv_canary_material_ids())) == 24
    assert sv_canary_corpus_hash() == sv_canary_corpus_hash()
    assert len(sv_canary_corpus_hash()) == 64
    assert SV_QUALITY_CANARY_SEED == 2060901


def test_micro_f1_exact_numerics_and_noise_invariance() -> None:
    from mnemoseed_local.eval.metrics import CanaryMetrics
    from mnemoseed_local.eval.sv_quality_metrics import extraction_micro_f1

    def _row(tp: int, fn: int, fp: int, noise: int = 0) -> CanaryMetrics:
        return CanaryMetrics(
            facts_total=tp + fn,
            facts_matched=tp,
            canary_recall=(tp / (tp + fn)) if (tp + fn) else None,
            matched_fact_ids=tuple(f"m{i}" for i in range(tp)),
            missed_fact_ids=tuple(f"s{i}" for i in range(fn)),
            noise_pollution=noise,
            polluting_nodes=tuple(f"p{i}" for i in range(noise)),
            core_yield=tp + fp,
            extra_core_nodes=tuple(f"x{i}" for i in range(fp)),
        )

    score = extraction_micro_f1((_row(6, 2, 2),))
    assert score.precision == pytest.approx(0.75)
    assert score.recall == pytest.approx(0.75)
    assert score.f1 == pytest.approx(0.75)
    noisy = extraction_micro_f1((_row(6, 2, 2, noise=5),))
    assert noisy.f1 == pytest.approx(score.f1)
    assert extraction_micro_f1(()).f1 is None
    swapped = extraction_micro_f1((_row(6, 1, 3),))
    assert swapped.precision == pytest.approx(6 / 9)
    assert swapped.recall == pytest.approx(6 / 7)


def test_f1_threshold_49pp_fails_5pp_passes() -> None:
    from mnemoseed_local.eval.sv_quality_metrics import decide

    def _decide(dual: float):  # type: ignore[no-untyped-def]
        return decide(
            single_f1=0.60,
            dual_f1=dual,
            single_fpr=0.4,
            dual_fpr=0.4,
            canary_informative=24,
            canary_total=24,
            positive_informative=8,
            positive_total=8,
            negative_informative=8,
            negative_total=8,
            safety_failed=False,
        )

    assert _decide(0.649).verdict == "fail"
    assert _decide(0.65).verdict == "pass"


def test_fpr_20pct_boundary_and_zero_baseline() -> None:
    from mnemoseed_local.eval.sv_quality_metrics import decide

    def _decide(single: float, dual: float):  # type: ignore[no-untyped-def]
        return decide(
            single_f1=0.6,
            dual_f1=0.6,
            single_fpr=single,
            dual_fpr=dual,
            canary_informative=24,
            canary_total=24,
            positive_informative=8,
            positive_total=8,
            negative_informative=8,
            negative_total=8,
            safety_failed=False,
        )

    assert _decide(0.50, 0.40).verdict == "pass"
    assert _decide(0.50, 0.41).verdict == "fail"
    assert _decide(0.0, 0.0).verdict != "pass"


def test_terminals_degraded_and_gate_totals() -> None:
    from mnemoseed_local.eval.sv_quality_metrics import adjudication_score, decide
    from mnemoseed_local.eval.sv_quality_runner import AdjudicationRow
    from mnemoseed_local.storage.ports import Disposition

    rows = (
        AdjudicationRow("neg-1", False, Disposition.REJECTED),
        AdjudicationRow("neg-2", False, Disposition.ACCEPTED),
        AdjudicationRow("neg-3", False, Disposition.DEFERRED),
        AdjudicationRow("pos-1", True, Disposition.ACCEPTED),
        AdjudicationRow("pos-2", True, Disposition.DEFERRED),
    )
    score = adjudication_score(rows)
    assert (score.false_positives, score.true_negatives) == (1, 1)
    assert score.fpr == pytest.approx(0.5)
    assert score.safety_failed is True
    assert (score.informative_positives, score.informative_negatives) == (1, 2)
    gated = decide(
        single_f1=0.9,
        dual_f1=0.9,
        single_fpr=0.5,
        dual_fpr=0.5,
        canary_informative=24,
        canary_total=24,
        positive_informative=1,
        positive_total=8,
        negative_informative=2,
        negative_total=8,
        safety_failed=False,
    )
    assert gated.verdict == "indeterminate"


def test_vote_metrics_cannot_reach_decide() -> None:
    import mnemoseed_local.eval.sv_quality_metrics as metrics_mod
    from mnemoseed_local.eval.metrics import VoteMetrics
    from mnemoseed_local.eval.sv_quality_metrics import decide

    assert "VoteMetrics" not in inspect.signature(decide).parameters
    assert "VoteMetrics" not in inspect.getsource(metrics_mod)
    kwargs = {
        "single_f1": 0.60,
        "dual_f1": 0.65,
        "single_fpr": 0.4,
        "dual_fpr": 0.4,
        "canary_informative": 24,
        "canary_total": 24,
        "positive_informative": 8,
        "positive_total": 8,
        "negative_informative": 8,
        "negative_total": 8,
        "safety_failed": False,
    }
    first = decide(**kwargs)  # type: ignore[arg-type]
    vote = VoteMetrics("a", "b", 9, 0, 0, 0, 0)
    assert vote.agreement_triples == 9
    assert decide(**kwargs) == first  # type: ignore[arg-type]


def test_preregistration_sensitivity_per_field() -> None:
    from mnemoseed_local.eval.sv_quality_manifest import preregistration_hash

    def _base(**over: object):  # type: ignore[no-untyped-def]
        args: dict[str, object] = {
            "prompt_version": "v1",
            "canary_seed": 2060901,
            "routes": ("luna-a", "luna-b"),
            "material_ids": ("canary-00",),
            "oracle_version": "v1",
            "denominator": "terminal-only-v1",
        }
        args.update(over)
        return preregistration_hash(**args)  # type: ignore[arg-type]

    base = _base()
    assert _base(prompt_version="v2") != base
    assert _base(canary_seed=1) != base
    assert _base(routes=("luna-a", "luna-c")) != base
    assert _base(material_ids=("canary-01",)) != base
    assert _base(oracle_version="v2") != base
    assert _base(denominator="other") != base


def test_complete_dry_run_manifest_and_no_overwrite(tmp_path: Path) -> None:
    import json as _json

    from mnemoseed_local.eval.sv_quality_manifest import (
        STAGED_EXCLUDED_HASH,
        STAGED_EXCLUDED_PATH,
        write_manifest,
    )
    from mnemoseed_local.eval.sv_quality_runner import build_config, build_dry_run_manifest

    assert STAGED_EXCLUDED_HASH == FROZEN_STAGED_HASH
    config = build_config(run_root=tmp_path / "run", repository_sha="abc123", port=17891)
    manifest = build_dry_run_manifest(config)
    required = {
        "manifest_version",
        "run_id",
        "repository_sha",
        "hardware",
        "provider",
        "cell_a",
        "cell_b",
        "vote_b_distinct",
        "prompt_version",
        "canary_seed",
        "material_ids",
        "canary_hash",
        "positive_hash",
        "negative_hash",
        "preregistration_hash",
        "oracle_version",
        "denominator",
        "tokens",
        "durations",
        "failures",
        "degraded",
        "retries",
        "raw_paths",
        "raw_hashes",
        "verdict",
        "recommendation",
        "staged_excluded_path",
        "staged_excluded_hash",
        "created_at",
    }
    assert required <= set(manifest)
    assert manifest["material_ids"] is not None and len(manifest["material_ids"]) == 24
    assert manifest["staged_excluded_path"] == STAGED_EXCLUDED_PATH
    assert manifest["staged_excluded_hash"] == FROZEN_STAGED_HASH
    path = write_manifest(config.run_root, manifest)
    assert path.exists()
    stored = _json.loads(path.read_text(encoding="utf-8"))
    assert stored["staged_excluded_hash"] == FROZEN_STAGED_HASH
    with pytest.raises(RuntimeError):
        write_manifest(config.run_root, manifest)


def test_luna_allowlist_and_route_isolation(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_runner import build_config

    for bad in ("openai", "ollama", "muse-spark", "Muse", "claude-muse"):
        with pytest.raises(ValueError):
            build_config(
                run_root=tmp_path / f"r-{bad}",
                repository_sha="abc123",
                port=17891,
                provider=bad,
            )
    with pytest.raises(ValueError):
        build_config(
            run_root=tmp_path / "vote",
            repository_sha="abc123",
            port=17891,
            cell_a_model="luna-a",
            cell_b_model="luna-a",
            vote_b_model="luna-a",
        )
    with pytest.raises(ValueError):
        build_config(run_root=tmp_path / "fb", repository_sha="abc123", port=17891, allow_fallback=True)
    with pytest.raises(ValueError):
        build_config(run_root=tmp_path / "live", repository_sha="abc123", port=7788)


def test_run_root_traversal_and_live_home_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemoseed_local.eval.sv_quality_runner import build_config

    with pytest.raises(ValueError):
        build_config(
            run_root=tmp_path / ".." / "escape",
            repository_sha="abc123",
            port=17891,
        )
    monkeypatch.setenv("MNEMOSEED_HOME", str(tmp_path / "live-home"))
    with pytest.raises(ValueError):
        build_config(
            run_root=tmp_path / "live-home",
            repository_sha="abc123",
            port=17891,
        )


def test_pong_order_single_then_dual_and_quota_stops(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_runner import (
        QuotaRejected,
        SvQualityRunner,
        build_config,
    )

    calls: list[str] = []

    def _pong(cell_id: str) -> None:
        calls.append(cell_id)

    config = build_config(run_root=tmp_path / "run", repository_sha="abc123", port=17891)
    out = SvQualityRunner(config, _pong).run()
    assert calls == ["single", "dual"]
    assert out["dry_run"] is True
    assert out["attempts"] == {"single": 1, "dual": 1}

    seen: list[str] = []

    def _quota(cell_id: str) -> None:
        seen.append(cell_id)
        raise QuotaRejected("quota exhausted")

    config2 = build_config(run_root=tmp_path / "run2", repository_sha="abc123", port=17891)
    out2 = SvQualityRunner(config2, _quota).run()
    assert seen == ["single"]
    assert out2["quota_rejected"] is True


def test_real_refuses_with_zero_calls_and_no_reconfigure(tmp_path: Path) -> None:
    from mnemoseed_local.eval.sv_quality_runner import SvQualityRunner, build_config

    calls: list[str] = []

    def _pong(cell_id: str) -> None:
        calls.append(cell_id)

    config = build_config(run_root=tmp_path / "run", repository_sha="abc123", port=17891)
    runner = SvQualityRunner(config, _pong)
    with pytest.raises(RuntimeError):
        runner.run(allow_real=True)
    assert calls == []
    runner.run()
    with pytest.raises(RuntimeError):
        runner.reconfigure()
