from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_feature_index_points_to_atlas():
    text = (ROOT / "features" / "README.md").read_text(encoding="utf-8")
    assert "[Atlas](atlas.md)" in text


def test_verify_atlas_skill_is_v2_and_runs_existing_harness():
    path = ROOT / ".opencode" / "skills" / "verify-atlas" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: verify-atlas" in text
    assert "user-invocable: true" in text
    assert "scripts/atlas_preflight.py" in text
    assert "scripts/atlas_verify.py" in text
    assert "7788" in text and "4096" in text


def test_ci_uploads_sanitized_evidence_even_when_verification_fails():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    section = text[text.index("  atlas-browser:") : text.index("  install-script-smoke:")]
    assert "- name: Sanitize and retain Atlas evidence\n        if: always()" in section
    assert "- name: Upload Atlas evidence\n        if: always()" in section
    assert "actions/upload-artifact@v4" in section
    assert "evidence.txt" in section
