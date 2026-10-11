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


def test_readme_documents_the_daemon_credential_boundary() -> None:
    assert "## Daemon credential" in README
    assert "mnemoseed-local credential rotate" in README
    assert "uninstall --purge" in README
    assert "does **not** defend against" in README
    # naming discipline: the credential is never called a token
    lowered = README.lower()
    for phrase in ("bearer token", "auth token", "access token", "refresh token"):
        assert phrase not in lowered, f"README still calls the daemon credential a token: {phrase!r}"


def test_design_04_documents_the_credential_boundary_and_its_threat_model() -> None:
    design_04 = (REPO_ROOT / "docs" / "zh" / "design" / "04-trust-boundary-and-config.md").read_text(
        encoding="utf-8"
    )
    for anchor in (
        "3.1.1 daemon 凭据边界",
        "3.1.2 威胁模型诚实边界",
        "daemon.credential.key",
        "credential rotate",
        "auth_support.py",
        "通配符 bind + 无 TLS = 凭据局域网裸奔",
    ):
        assert anchor in design_04, f"design/04 must document {anchor!r}"


def test_design_05_replaces_the_no_token_claim() -> None:
    design_05 = (REPO_ROOT / "docs" / "zh" / "design" / "05-host-integration.md").read_text(encoding="utf-8")
    assert "本地 daemon 凭据" in design_05
    assert "没有账号体系、也没有 OAuth 流程" in design_05


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
