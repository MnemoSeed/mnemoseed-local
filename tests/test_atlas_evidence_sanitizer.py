import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "sanitize_atlas_evidence.py"


def run_sanitizer(content: str | None) -> str:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "evidence.txt"
        if content is not None:
            source.write_text(content, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(source), "abc123"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        return result.stdout


def test_pass_contains_only_allowlisted_status_and_oracle_fields():
    output = run_sanitizer(
        "PASS\nbase_url=http://127.0.0.1:1\n"
        "empty_count=0 items · Rescue 0 · Fading 0\n"
        "filled_count=1 item · Rescue 0 · Fading 0\n"
        "profile_isolation=PASS\nremember_to_atlas=PASS\n"
    )
    assert output == (
        "status=PASS\nsource_sha=abc123\nrun_id=<redacted>\n"
        "empty_count=0\nfilled_count=1\n"
        "profile_isolation=PASS\nremember_to_atlas=PASS\n"
    )


def test_secret_path_and_failure_text_never_leak():
    output = run_sanitizer(
        "FAIL\nSECRET_TOKEN=abc\n/Users/example/private/evidence.txt\nprofile_isolation=PASS\n"
    )
    assert output == "status=FAIL\nsource_sha=abc123\nrun_id=<redacted>\nprofile_isolation=PASS\n"


def test_unknown_missing_or_multiple_evidence_is_inconclusive_and_artifact_safe():
    for content in (None, "UNKNOWN\nSECRET_TOKEN=abc", "PASS\nPASS\n"):
        output = run_sanitizer(content)
        assert output == "status=INCONCLUSIVE\nsource_sha=abc123\nrun_id=<redacted>\n"
        assert "SECRET" not in output
        assert "/Users" not in output
