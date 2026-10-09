"""Docs honesty invariants.

The public README and the Chinese MVP docs make present-tense product claims;
these tests pin the claims that drifted from shipped reality, so a stale claim
cannot silently return. They assert documented surface, not editorial wording.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
ZH_README = (REPO_ROOT / "docs" / "zh" / "README.md").read_text(encoding="utf-8")
ZH_MVP = (REPO_ROOT / "docs" / "zh" / "MVP.md").read_text(encoding="utf-8")
ZH_DESIGN_07 = (REPO_ROOT / "docs" / "zh" / "design" / "07-durability-and-reliability.md").read_text(
    encoding="utf-8"
)


def test_readme_does_not_teach_the_removed_supervisor_chain() -> None:
    for gone in (
        "## Daemon supervision (optional)",
        "supervise.ps1",
        'Register-ScheduledTask -TaskName "MnemoSeedLocalDaemon"',
    ):
        assert gone not in README, f"README still teaches the removed supervisor chain: {gone!r}"


def test_readme_documents_manual_restart_recovery() -> None:
    assert "does not auto-restart" in README
    assert "mnemoseed-local up" in README


def test_readme_does_not_claim_opus_class_local_inference() -> None:
    assert "Opus-class" not in README


def test_readme_does_not_claim_no_model_runs_at_ingest() -> None:
    lowered = README.lower()
    for gone in (
        "no model at ingest",
        "no model runs at ingest",
        "no model runs on ingest",
    ):
        assert gone not in lowered, f"README still claims no model runs at ingest: {gone!r}"


def test_readme_does_not_claim_memory_plaintext_never_leaves_local_storage() -> None:
    lowered = README.lower()
    for gone in ("never leaves the machine", "never leaves the disk"):
        assert gone not in lowered, f"README still claims plaintext {gone}: {gone!r}"


def test_readme_does_not_attribute_prev_version_id_to_corrections_or_supersedes() -> None:
    lowered = README.lower()
    sentences = [
        sentence
        for sentence in re.split(r"[.!?]", " ".join(lowered.split()))
        if "prev_version_id" in sentence
    ]
    assert sentences, "README must document prev_version_id for this rule to hold"
    for sentence in sentences:
        assert "correction" not in sentence, f"README credits prev_version_id to a correction: {sentence!r}"
        assert "supersed" not in sentence, f"README credits prev_version_id to a supersede: {sentence!r}"


def test_readme_names_the_not_yet_surface() -> None:
    assert "What it does NOT do yet" in README
    lowered = README.lower()
    assert "learns from mistakes" not in lowered


def test_zh_readme_acknowledges_the_shipped_console() -> None:
    assert "没有控制台" not in ZH_README
    assert "控制台" in ZH_README


def test_design_07_contains_no_supervisor_chain_teaching() -> None:
    for gone in (
        "## Daemon supervision (optional)",
        "supervise.ps1",
        "AtLogOn",
        "bounded wrapper",
    ):
        assert gone not in ZH_DESIGN_07, f"design/07 still teaches the removed supervisor chain: {gone!r}"


def test_package_docstring_does_not_claim_no_console() -> None:
    from mnemoseed_local import __doc__ as pkg_doc

    assert pkg_doc is not None
    assert "no console" not in pkg_doc


def test_zh_readme_spells_dream_once_correctly() -> None:
    assert "dream --once" in ZH_README
    assert "dream once" not in ZH_README


def test_zh_mvp_does_not_claim_manual_only_dream() -> None:
    assert "dream --once（手动）" not in ZH_MVP


def test_zh_mvp_does_not_defer_installation() -> None:
    assert "一键安装属后续阶段" not in ZH_MVP
