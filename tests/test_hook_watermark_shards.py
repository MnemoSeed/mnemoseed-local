"""Issue #171 lock-free watermark shards: behavioral coverage (TDD red first).

Real Node harness over the esbuild-bundled plugin (no regex-only oracles).
Synthetic session IDs/data only; isolated temp DATA_DIR per test; no network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_hook_ts_behavior import _bundle

DRIVER = Path(__file__).parent / "ts_hook" / "hook_driver.mjs"
WORKER = Path(__file__).parent / "ts_hook" / "watermark_worker.mjs"

# Independent strict final-shard matcher: byte-identical predicate to the
# production shard selector (legacy exact or
# hook-watermarks.<pid>.<uuid>.json). ASCII-only on purpose ([0-9], not \d).
LEGACY_BASENAME = "hook-watermarks.json"
FINAL_SHARD_RE = re.compile(r"hook-watermarks\.([0-9]+)\.([0-9a-fA-F-]{8,})\.json")


def _is_selected(name: str) -> bool:
    if ".tmp." in name:
        return False
    if name == LEGACY_BASENAME:
        return True
    return FINAL_SHARD_RE.fullmatch(name) is not None


def _run_watermark(bundle: Path, scenario: str) -> dict:
    env = dict(os.environ)
    env["MNEMOSEED_LOCAL_WATERMARK_TEST"] = "1"
    env.pop("MNEMOSEED_LOCAL_DEBUG", None)
    env["MNEMOSEED_LOCAL_DEBUG"] = "1"
    result = subprocess.run(
        ["node", str(DRIVER), str(bundle), scenario],
        shell=False,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, f"driver failed: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _merged_max(data_dir: Path) -> dict:
    merged: dict[str, float] = {}
    for child in data_dir.iterdir():
        if not _is_selected(child.name):
            continue
        try:
            payload = json.loads(child.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if not isinstance(value, (int, float)) or not (value >= 0) or value != value:
                continue
            if key not in merged or value > merged[key]:
                merged[key] = value
    return merged


def test_same_process_overlapping_persists_keep_every_key(tmp_path: Path) -> None:
    """P1: >=20 overlapping persists (awaited + fire-and-forget mix)."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-overlap")
    assert transcript["persistCount"] >= 20, transcript
    assert transcript["uncaught"] == 0, transcript
    assert transcript["shardValid"] is True, transcript
    assert transcript["lostKeys"] == [], transcript
    assert transcript["legacyTmpOrphans"] == 0, transcript
    assert transcript["ownTmpLeftovers"] == 0, transcript
    assert transcript["maxConcurrency"] == 1, transcript


def test_two_processes_converge_to_historical_max(tmp_path: Path) -> None:
    """P2: two separate Node processes sharing one DATA_DIR, >=50 rounds each."""
    if shutil.which("node") is None or shutil.which("npx") is None:
        pytest.skip("node toolchain unavailable")
    bundle = _bundle(tmp_path)
    data_dir = tmp_path / "shared-data"
    data_dir.mkdir()
    rounds = 50
    procs = []
    try:
        for index in (0, 1):
            env = dict(os.environ)
            env["MNEMOSEED_LOCAL_WATERMARK_TEST"] = "1"
            procs.append(
                subprocess.Popen(
                    ["node", str(WORKER), str(bundle), str(data_dir), str(index), str(rounds)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
            )
        outs = []
        for proc in procs:
            try:
                out, err = proc.communicate(timeout=180)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                out, err = proc.communicate(timeout=30)
                raise AssertionError(f"worker timeout: {err}") from exc
            assert proc.returncode == 0, f"worker failed rc={proc.returncode}: {err}"
            outs.append(json.loads(out.strip().splitlines()[-1]))
        assert {o["workerIndex"] for o in outs} == {0, 1}
        merged = _merged_max(data_dir)
        for k in range(8):
            assert merged.get(f"wm-test-shared-{k}") == 1000 + (rounds - 1) * 10 + 1, merged
        for index in (0, 1):
            assert merged.get(f"wm-test-w{index}-own") == 2000 + (rounds - 1) * 10, merged
        assert len([p for p in data_dir.iterdir() if ".tmp." in p.name]) == 0, "zero tmp leftovers"
        for child in data_dir.iterdir():
            if not _is_selected(child.name):
                continue
            try:
                payload = json.loads(child.read_text(encoding="utf-8"))
            except Exception:
                continue
            assert isinstance(payload, dict)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()


def test_eperm_injection_recovers_with_bounded_retry(tmp_path: Path) -> None:
    """P3: owned-target rename EPERM once, then continuous EPERM, then recover."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-eperm")
    assert transcript["uncaught"] == 0, transcript
    assert transcript["singleFaultRecovered"] is True, transcript
    assert transcript["continuousFaultRecovered"] is True, transcript
    assert transcript["targetValidAfter"] is True, transcript
    assert transcript["ownTmpLeftovers"] == 0, transcript


def test_fold_cases_pin_per_persist_enumeration_and_max(tmp_path: Path) -> None:
    """IMPORTANT-1: own-shard bytes prove the per-persist fold (no seam.load).

    Legacy-only keys fold, late peer shards are discovered on the next
    persist, higher values win by max, and decreasing values never regress.
    """
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-fold-cases")
    assert transcript["legacyFolded"] is True, transcript
    assert transcript["lateFolded"] is True, transcript
    assert transcript["noRegress"] is True, transcript


def test_legacy_and_shard_merge_by_max(tmp_path: Path) -> None:
    """P6: legacy (read-only) + peer shard maxima fold into the own shard bytes."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-merge")
    assert transcript["mergedOk"] is True, transcript
    assert transcript["legacyUnchanged"] is True, transcript


def test_corrupt_files_skipped_and_preserved(tmp_path: Path) -> None:
    """P4: bad JSON / bad keys skipped, bytes preserved, good keys converge."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-corrupt")
    assert transcript["corruptPreserved"] is True, transcript
    assert transcript["badKeysExcluded"] is True, transcript
    assert transcript["goodKeysConverged"] is True, transcript


def test_gc_only_deletes_eligible_dead_owner_shards(tmp_path: Path) -> None:
    """P5: dead+old deleted after fold (<=20); alive/EPERM/unknown preserved."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-gc")
    assert transcript["deadDeleted"] is True, transcript
    assert transcript["foldedBeforeDelete"] is True, transcript
    assert transcript["youngFolded"] is True, transcript
    assert transcript["youngPreserved"] is True, transcript
    assert transcript["ownPreserved"] is True, transcript
    assert transcript["ownBytesKept"] is True, transcript
    assert transcript["alivePreserved"] is True, transcript
    assert transcript["epermPreserved"] is True, transcript
    assert transcript["unknownPreserved"] is True, transcript
    assert transcript["corruptPreserved"] is True, transcript
    assert transcript["deleteBound"] is True, transcript


def test_mixed_version_legacy_folds_forward_without_writing_legacy(tmp_path: Path) -> None:
    """Mixed-version: old whole-map legacy write folds into the new shard."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-mixed")
    assert transcript["foldedOk"] is True, transcript
    assert transcript["legacyNotOverwritten"] is True, transcript


def test_owned_tmp_sweep_recovers_failed_cleanup(tmp_path: Path) -> None:
    """IMPORTANT-3: orphaned owned tmps are swept by the next persist."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-tmp-sweep")
    assert transcript["cleanStart"] is True, transcript
    assert transcript["orphanedOnFailure"] is True, transcript
    assert transcript["sweptOnNextPersist"] is True, transcript
    assert transcript["shardRecovered"] is True, transcript
    assert transcript["legacyTmpPreserved"] is True, transcript
    assert transcript["unrelatedPreserved"] is True, transcript


def test_early_ack_retained_and_invalid_rejected(tmp_path: Path) -> None:
    """IMPORTANT-4: pre-load ACKs accumulate; NaN/Infinity/negative ignored."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-early-ack")
    assert transcript["earlyRetained"] is True, transcript
    assert transcript["invalidIgnored"] is True, transcript
    assert transcript["realAckConverged"] is True, transcript


def test_note_during_io_flushed_in_same_persist(tmp_path: Path) -> None:
    """IMPORTANT-5: a note landing mid-write is flushed before persist returns."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-note-during-io")
    assert transcript["sameCycleFlushed"] is True, transcript


def test_strict_selectors_exclude_decoys(tmp_path: Path) -> None:
    """IMPORTANT-6: decoy filenames are excluded and preserved."""
    bundle = _bundle(tmp_path)
    transcript = _run_watermark(bundle, "watermark-selectors")
    assert transcript["decoysExcluded"] is True, transcript
    assert transcript["converged"] is True, transcript
    assert transcript["decoysPreserved"] is True, transcript


def test_hotpath_llm_wire_red_lines_hold() -> None:
    """P7: hot-path await, no-LLM, and wire pins hold alongside shards."""
    from importlib import resources

    source = (
        resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts").read_text(encoding="utf-8")
    )
    assert "REPLAY_OVERLAP_MS = 30000" in source
    assert ".chat(" not in source and "LLM" not in source
    assert "hook-watermarks.<pid>.<uuid>.json" in source or "hook-watermarks." in source
    assert "ESRCH" in source
    assert "journal" not in source.lower()
    assert sys.version_info >= (3, 12)
