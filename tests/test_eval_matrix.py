"""B3 T4 — replay materials + matrix runner (PRD-B3).

Replay = the B1-harness shape made durable: a frozen snapshot journal (real
sessions, real stamps) re-driven through a FRESH reflect/verify/merge on the
eval rig — chunk stamps ride verbatim (tier/origin never re-derived), the run
journal phases are reset (each eval cell reflects the material itself).

Matrix = roster × ensemble with honest skip semantics: an absent model marks
its cell skipped (never a silent no-run); material/load failures are typed
report rows; exit codes distinguish failed from skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mnemoseed_local.dream.snapshot import SnapshotPhase
from mnemoseed_local.eval.canary import canary_session
from mnemoseed_local.eval.harness import EvalCell, EvalRig, EvalRoute, RigPaths
from mnemoseed_local.eval.materials import (
    MaterialError,
    fresh_replay,
    load_replay,
    material_catalog,
)
from mnemoseed_local.eval.matrix import (
    ROSTER_DEFAULT,
    SEAT_SEED_DEFAULT,
    default_matrix,
    list_cells,
    matrix_exit_code,
    ollama_route,
    parse_extra_route,
    probe_ollama_models,
    run_matrix,
)
from mnemoseed_local.eval.report import SEAT_SEED_POLICY_FIXED, SEAT_SEED_POLICY_NONE, report_to_dict

STUB_A = EvalRoute(driver="stub", model="stub-a")
STUB_B = EvalRoute(driver="stub_verifier", model="stub-b")


@pytest.fixture
def frozen_snapshot(tmp_path: Path) -> Path:
    """A real dream journal: one stub-seat canary run, frozen on disk."""
    rig = EvalRig(
        RigPaths(root=tmp_path / "origin"),
        EvalCell(reflect=STUB_A, ensemble="off", verifier=STUB_B),
    )
    try:
        session = canary_session(41, facts=4, noise=2)
        run = rig.run_canary(session)
    finally:
        rig.close()
    assert run.merge_committed
    assert run.merge_summary is not None
    journal = tmp_path / "origin" / "dreams" / f"{run.merge_summary.snapshot_id}.json"
    assert journal.exists()
    return journal


def test_load_replay_reads_journal(frozen_snapshot: Path) -> None:
    material = load_replay(frozen_snapshot)
    assert material.kind == "replay"
    assert material.name == frozen_snapshot.stem
    assert material.snapshot is not None
    assert len(material.snapshot.chunks) == 6
    # the journal itself says MERGE_DONE; replay must still reflect fresh
    assert SnapshotPhase.MERGE_DONE.value in material.snapshot.phases


def test_fresh_replay_strips_run_history(frozen_snapshot: Path) -> None:
    material = load_replay(frozen_snapshot)
    fresh = fresh_replay(material.snapshot)  # type: ignore[arg-type]
    assert fresh.phases == frozenset({SnapshotPhase.SNAPSHOT_DONE.value})
    assert fresh.reflect_result is None
    # chunk stamps ride verbatim (tier/origin untouched)
    assert fresh.chunks == material.snapshot.chunks  # type: ignore[union-attr]
    assert fresh.snapshot_id == material.snapshot.snapshot_id  # type: ignore[union-attr]


def test_replay_run_re_reflects_freshly(frozen_snapshot: Path, tmp_path: Path) -> None:
    material = load_replay(frozen_snapshot)
    rig = EvalRig(
        RigPaths(root=tmp_path / "replay-rig"),
        EvalCell(reflect=STUB_A, ensemble="verify", verifier=STUB_B),
    )
    try:
        run = rig.run_snapshot(fresh_replay(material.snapshot))  # type: ignore[arg-type]
    finally:
        rig.close()
    assert run.merge_committed
    assert run.core_nodes, "replay produced no core nodes"
    verified = [a for a in run.audit if a.action == "ensemble_verified"]
    assert verified, "replay dream skipped the verify phase"
    # the material chunks ride on the read-back (scratch store holds no copies)
    assert len(run.chunks) == 6


def test_bad_replay_typed_error(tmp_path: Path) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(MaterialError) as excinfo:
        load_replay(bad)
    assert "broken.json" in str(excinfo.value)


def test_catalog_deterministic(frozen_snapshot: Path, tmp_path: Path) -> None:
    materials_dir = tmp_path / "materials"
    materials_dir.mkdir()
    journal = json.loads(frozen_snapshot.read_text(encoding="utf-8"))
    for name in ("b-session.json", "a-session.json"):
        (materials_dir / name).write_text(json.dumps(journal), encoding="utf-8")
    first = material_catalog(materials_dir, canary_seed=7, canary_count=1)
    second = material_catalog(materials_dir, canary_seed=7, canary_count=1)
    assert [m.name for m in first] == [m.name for m in second]
    kinds = [m.kind for m in first]
    assert kinds == sorted(kinds, key=lambda k: (k != "canary", k)), "canary entries lead"
    replays = [m.name for m in first if m.kind == "replay"]
    assert replays == sorted(replays)


def test_default_matrix_expansion() -> None:
    cells = default_matrix(roster=ROSTER_DEFAULT)
    assert len(cells) == 2 * len(ROSTER_DEFAULT)
    ids = [c.cell_id for c in cells]
    assert len(set(ids)) == len(ids)
    off = [c for c in cells if c.ensemble == "off"]
    verifying = [c for c in cells if c.ensemble == "verify"]
    assert {c.reflect.model for c in off} == set(ROSTER_DEFAULT)
    assert {c.reflect.model for c in verifying} == set(ROSTER_DEFAULT)
    assert all(c.verifier is not None and c.verifier.model == "gemma4:e4b" for c in verifying)


def test_default_matrix_custom_verifier() -> None:
    cells = default_matrix(roster=("qwen3.5:9b",), verifier_model="qwen3:8b")
    verify = next(c for c in cells if c.ensemble == "verify")
    assert verify.verifier is not None and verify.verifier.model == "qwen3:8b"


def test_probe_missing_model() -> None:
    def fake_tags(base_url: str, timeout: float) -> tuple[str, ...]:
        return ("gemma4:e4b",)

    probe = probe_ollama_models(("qwen3.5:9b", "gemma4:e4b"), fetch_tags=fake_tags)
    assert probe["gemma4:e4b"] is None
    assert probe["qwen3.5:9b"] is not None
    assert "qwen3.5:9b" in probe["qwen3.5:9b"]  # type: ignore[operator]


def test_probe_network_failure_marks_everything() -> None:
    def dead_tags(base_url: str, timeout: float) -> tuple[str, ...]:
        raise OSError("connection refused")

    probe = probe_ollama_models(("qwen3.5:9b", "gemma4:e4b"), fetch_tags=dead_tags)
    assert all(reason is not None for reason in probe.values())
    assert all("unreachable" in reason or "failed" in reason for reason in probe.values())  # type: ignore[operator]


def test_run_matrix_stub_plus_skipped(tmp_path: Path) -> None:
    cells = [
        EvalCell(reflect=STUB_A, ensemble="off", verifier=STUB_B),
        EvalCell(reflect=STUB_A, ensemble="verify", verifier=STUB_B),
        EvalCell(reflect=EvalRoute(driver="ollama", model="qwen3.5:9b"), ensemble="off"),
    ]
    materials = material_catalog(None, canary_seed=42, canary_count=1)

    def fake_tags(base_url: str, timeout: float) -> tuple[str, ...]:
        return ()

    report = run_matrix(cells, materials, root=tmp_path, fetch_tags=fake_tags)
    assert len(report.cells) == 2
    assert len(report.skipped) == 1
    assert report.skipped[0].reason.startswith("missing_model: qwen3.5:9b")
    assert all(cell.canary.canary_recall == 1.0 for cell in report.cells)


def test_matrix_failure_exit_semantics(tmp_path: Path) -> None:
    materials_dir = tmp_path / "materials"
    materials_dir.mkdir()
    (materials_dir / "broken.json").write_text("{nope", encoding="utf-8")
    cells = [EvalCell(reflect=STUB_A, ensemble="off", verifier=STUB_B)]
    report = run_matrix(cells, material_catalog(materials_dir, canary_seed=1, canary_count=0), root=tmp_path)
    assert report.cells == ()
    assert len(report.skipped) == 1
    assert report.skipped[0].reason.startswith("material_error:")
    assert matrix_exit_code(report) == 1
    clean = run_matrix(cells, material_catalog(None, canary_seed=1, canary_count=1), root=tmp_path / "ok")
    assert matrix_exit_code(clean) == 0


def test_listing_is_side_effect_free(tmp_path: Path) -> None:
    cells = default_matrix(roster=("qwen3.5:9b",))
    # --list never probes, never builds stores: the root dir stays untouched
    listing = list_cells(cells)
    assert listing == [c.cell_id for c in cells]
    assert not list(tmp_path.iterdir())


# ---------------------------------------------------------------- B4a per-seat fixed seed


def test_ollama_route_carries_default_fixed_seed() -> None:
    route = ollama_route("qwen3.5:9b")
    params = dict(route.params)
    assert params["seed"] == SEAT_SEED_DEFAULT
    assert params["seed"] == 42  # RCA-validated full-extraction seed
    assert params["think"] is False


def test_no_seat_seed_removes_seed_from_ollama_routes() -> None:
    route = ollama_route("qwen3.5:9b", seat_seed=None)
    assert "seed" not in dict(route.params)
    cells = default_matrix(roster=("qwen3.5:9b",), seat_seed=None)
    assert all("seed" not in dict(c.reflect.params) for c in cells)
    assert all("seed" not in dict(c.verifier.params) for c in cells if c.verifier is not None)


def test_cloud_extra_route_never_carries_seed() -> None:
    cloud = parse_extra_route("openai_compatible|kimi-k3|https://api.modal.test|KIMI_KEY")
    assert "seed" not in dict(cloud.params)
    local = parse_extra_route("ollama|qwen3:8b|http://localhost:11434")
    assert dict(local.params)["seed"] == SEAT_SEED_DEFAULT
    unseeded = parse_extra_route("ollama|qwen3:8b|http://localhost:11434", seat_seed=None)
    assert "seed" not in dict(unseeded.params)


def test_run_matrix_records_seat_seed_and_policy(tmp_path: Path) -> None:
    seeded = EvalCell(
        reflect=EvalRoute(driver="stub", model="stub-a", params=(("seed", SEAT_SEED_DEFAULT),)),
        ensemble="off",
        verifier=STUB_B,
    )
    materials = material_catalog(None, canary_seed=1, canary_count=1)
    report = run_matrix([seeded], materials, root=tmp_path / "seeded")
    assert report.seat_seed_policy == SEAT_SEED_POLICY_FIXED
    assert len(report.cells) == 1
    assert report.cells[0].seat_seed == SEAT_SEED_DEFAULT
    assert report.cells[0].reflect_collapse_attempts == 0
    assert report.cells[0].reflect_recovered is False
    plain = run_matrix(
        [EvalCell(reflect=STUB_A, ensemble="off", verifier=STUB_B)],
        material_catalog(None, canary_seed=1, canary_count=1),
        root=tmp_path / "plain",
    )
    assert plain.seat_seed_policy == SEAT_SEED_POLICY_NONE
    assert plain.cells[0].seat_seed is None


def test_matrix_cli_no_seat_seed_flag_parses() -> None:
    """--no-seat-seed exists on the matrix subcommand and parses cleanly."""
    from mnemoseed_local.eval.__main__ import main

    assert main(["matrix", "--list", "--no-seat-seed"]) == 0


def _rig_audit_count(root: Path, cell: EvalCell) -> int:
    """The largest audit row count of any rig store under ``root``.

    Reads each ``meta.db`` read-only (no rig construction, so no store wipe):
    the audit log is append-only, so a second run re-ingesting onto the same
    store doubles its rows. Fresh-run stores each hold one run's worth, so the
    max across stores is the contamination signal: no store may carry more
    than one run's worth of audit rows.
    """
    import sqlite3

    counts: list[int] = []
    for meta in root.glob("**/stores/meta.db"):
        conn = sqlite3.connect(f"file:{meta}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
            counts.append(int(row[0]) if row is not None else 0)
        finally:
            conn.close()
    return max(counts) if counts else 0


def test_run_matrix_same_root_twice_is_idempotent(tmp_path: Path) -> None:
    """A second run_matrix over the SAME root must not accumulate store state
    under the shared profile: reports must be byte-for-byte identical (modulo
    wall-clock normalization) and no rig store may carry more than one run's
    worth of data (the audit log is append-only, so accumulation doubles it)."""
    cells = [EvalCell(reflect=STUB_A, ensemble="off", verifier=STUB_B)]
    materials = material_catalog(None, canary_seed=1, canary_count=1)
    root = tmp_path / "root"

    r1 = run_matrix(cells, materials, root=root)
    baseline = _rig_audit_count(root, cells[0])

    r2 = run_matrix(cells, materials, root=root)

    d1 = report_to_dict(r1)
    d2 = report_to_dict(r2)
    d1["started_at"] = ""
    d2["started_at"] = ""
    for cell in d1["cells"]:
        cell["cost"]["duration_s"] = None
    for cell in d2["cells"]:
        cell["cost"]["duration_s"] = None
    assert d1 == d2
    assert [len(c["triples"]) for c in d2["cells"]] == [len(c["triples"]) for c in d1["cells"]]
    # cross-run isolation: run-2 must not re-ingest on top of run-1's store
    assert _rig_audit_count(root, cells[0]) == baseline
