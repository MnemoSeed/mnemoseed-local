"""B4b matrix script (repo-root dev tool) — pins the lite-tier calibration
cells, the pinned canary corpus, and the real probe/report-writer wiring
(architect review B1-B4 + P1).

The script must NOT hand-roll what the harness already provides: the ollama
tags probe (a stubbed empty-tags lambda silently skips the whole matrix) and
the report serializer (a hand-built dict loses the v1.1 payload). The bar
contract is pinned here: lite tier (num_ctx 8192, delta budget 8192) and the
corpus identity (factory seed 42 -> canary-00..canary-04). Tests reference the
script's named constants, never duplicated literals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    import run_b4b_matrix as b4b  # noqa: E402
except ModuleNotFoundError:
    pytest.skip("run_b4b_matrix not shipped (batch B offline face only)", allow_module_level=True)
from mnemoseed_local.eval.canary import canary_session  # noqa: E402
from mnemoseed_local.eval.matrix import _fetch_ollama_tags  # noqa: E402
from mnemoseed_local.eval.report import EvalReport  # noqa: E402


def test_b4b_pins_lite_tier_and_corpus_constants() -> None:
    """The bar pins are named constants on the script: lite tier numbers and
    the corpus seed/count. The pilot reports (d32000, seed 42000) predate them
    and are shape evidence only, never bar data."""
    assert b4b.LITE_NUM_CTX == 8192
    assert b4b.LITE_DELTA_BUDGET == 8192
    assert b4b.CANARY_SEED == 42
    assert b4b.CANARY_COUNT == 5


def test_build_cells_is_lite_tier_pairing() -> None:
    cells = b4b.build_cells()
    assert len(cells) == 4
    off = [c for c in cells if c.ensemble == "off"]
    verify = [c for c in cells if c.ensemble == "verify"]
    assert len(off) == 2
    assert len(verify) == 2
    assert {c.reflect.model for c in cells} == {"qwen3.5:4b", "gemma4:e4b"}
    assert {c.verifier.model for c in cells if c.verifier is not None} == {"gemma4:e4b"}
    for cell in cells:
        assert cell.delta_budget_tokens == b4b.LITE_DELTA_BUDGET
        assert dict(cell.reflect.params)["num_ctx"] == b4b.LITE_NUM_CTX
        assert dict(cell.reflect.params)["seed"] == 42
        assert cell.verifier is not None
        assert dict(cell.verifier.params)["num_ctx"] == b4b.LITE_NUM_CTX


def test_build_materials_is_pinned_canary_batch() -> None:
    """The B4b corpus is the pinned seed-42 batch: 5 canary materials named
    canary-00..canary-04, each session byte-identical to the factory at
    sub-seed 42*1000+i."""
    materials = b4b.build_materials()
    assert len(materials) == b4b.CANARY_COUNT
    assert [m.name for m in materials] == [f"canary-{i:02d}" for i in range(b4b.CANARY_COUNT)]
    for i, material in enumerate(materials):
        assert material.kind == "canary"
        expected = canary_session(b4b.CANARY_SEED * 1000 + i, session_id=f"canary-{i:02d}")
        assert material.session == expected


def test_main_uses_real_probe_and_report_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_matrix(
        cells, materials, *, root, fetch_tags=None, route_checker=None, env=None, base_url="..."
    ):
        captured["fetch_tags"] = fetch_tags
        captured["route_checker"] = route_checker
        captured["n_materials"] = len(materials)
        captured["n_cells"] = len(cells)
        return EvalReport(eval_version="v1.1", started_at="2026-08-20T00:00:00Z", cells=(), skipped=())

    monkeypatch.setattr(b4b, "run_matrix", fake_run_matrix)
    written: list[tuple[object, ...]] = []

    def fake_write_report(report, out_dir, matrix_slug):
        written.append((report, out_dir, matrix_slug))
        return Path(out_dir) / "report.json"

    monkeypatch.setattr(b4b, "write_report", fake_write_report)
    b4b.main()

    assert captured["fetch_tags"] is _fetch_ollama_tags  # real probe, not a stubbed lambda
    assert captured["route_checker"] is None  # no no-op route checker either
    assert captured["n_materials"] == 5
    assert captured["n_cells"] == 4
    assert len(written) == 1
    report, out_dir, slug = written[0]
    assert isinstance(report, EvalReport)  # write_report got the typed report, not a hand-built dict
    assert slug == "4cells-5materials"
