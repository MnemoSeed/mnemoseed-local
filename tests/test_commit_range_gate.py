"""The cheap gates are read on every commit of a range, not only on its tip.

CI reads the tip of a branch, so a commit underneath it can be gate-red while the
tip is green, and a repair folded into a later commit or a merge leaves that commit
red with nothing to show for it. These tests pin the tripwire that materializes
every commit of a range in a tree of its own and names the ones the cheap static
gates reject.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRIPWIRE = REPO_ROOT / "scripts" / "check_commit_range.py"
COMMENT_POLICY = REPO_ROOT / "scripts" / "check_changed_comments.py"

#: Both words are banned by the repository's changed-comment policy, so a commit
#: that adds one is gate-red at that commit and in no commit above it.
BANNED_COMMENT = "# temporary label\n"
REPAIRED_COMMENT = "# short label\n"

#: Per-command identity, so the scratch repositories need no configuration of
#: their own and none of the operator's is consulted.
IDENTITY = (
    "-c",
    "user.name=gate",
    "-c",
    "user.email=gate@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
)

CONFIGURED_APP = "VALUE = 1\n"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *IDENTITY, *args], cwd=repo, capture_output=True, text=True, encoding="utf-8"
    )
    assert completed.returncode == 0, f"git {' '.join(args)} failed: {completed.stderr}"
    return completed.stdout.strip()


def _write(repo: Path, relative: str, text: str) -> None:
    (repo / relative).write_text(text, encoding="utf-8", newline="\n")


def _repository(tmp_path: Path) -> Path:
    """A repository holding the tripwire, the policy script it runs, and the
    repository's own ruff settings, so the scratch gates read what the real ones do."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    _git(repo, "init", "--quiet", "-b", "main")
    shutil.copy(COMMENT_POLICY, repo / "scripts" / COMMENT_POLICY.name)
    shutil.copy(TRIPWIRE, repo / "scripts" / TRIPWIRE.name)
    shutil.copy(REPO_ROOT / "pyproject.toml", repo / "pyproject.toml")
    _write(repo, "app.py", CONFIGURED_APP)
    return repo


def _app(repo: Path, comment: str) -> None:
    _write(repo, "app.py", f"{comment}{CONFIGURED_APP}")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", message)
    return _git(repo, "rev-parse", "HEAD")


def _short(repo: Path, commit: str) -> str:
    return _git(repo, "rev-parse", "--short", commit)


def _tripwire(repo: Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / TRIPWIRE.name), base, head],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _verdicts(output: str) -> dict[str, str]:
    """Each commit's own verdict, keyed by the revision the run printed for it."""
    verdicts: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"green", "RED"}:
            verdicts[parts[1]] = parts[0]
    return verdicts


def test_a_gate_red_commit_under_a_green_tip_is_named(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _app(repo, BANNED_COMMENT)
    red = _commit(repo, "add a banned label")
    _app(repo, REPAIRED_COMMENT)
    tip = _commit(repo, "shorten the label")

    completed = _tripwire(repo, base, tip)

    assert _verdicts(completed.stdout) == {_short(repo, red): "RED", _short(repo, tip): "green"}
    assert "failed: changed comments" in completed.stdout
    assert red in completed.stderr
    assert completed.returncode == 1


def test_a_merge_that_carries_a_repair_does_not_redeem_the_red_commit(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _git(repo, "checkout", "--quiet", "-b", "feature")
    _app(repo, BANNED_COMMENT)
    red = _commit(repo, "add a banned label")
    _git(repo, "checkout", "--quiet", "main")
    _write(repo, "other.py", "OTHER = 2\n")
    elsewhere = _commit(repo, "add another module")
    _git(repo, "merge", "--quiet", "--no-commit", "feature")
    _app(repo, REPAIRED_COMMENT)
    merge = _commit(repo, "merge the feature with the label repaired")

    completed = _tripwire(repo, base, merge)

    assert _verdicts(completed.stdout) == {
        _short(repo, red): "RED",
        _short(repo, elsewhere): "green",
        _short(repo, merge): "green",
    }
    assert red in completed.stderr
    assert completed.returncode == 1


def test_a_range_whose_commits_all_pass_is_green(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    tip = _commit(repo, "add another module")

    completed = _tripwire(repo, base, tip)

    assert _verdicts(completed.stdout) == {_short(repo, tip): "green"}
    assert "pass the cheap gates" in completed.stdout
    assert completed.returncode == 0


def test_a_commit_that_fails_lint_under_a_green_tip_is_named(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "app.py", "import os\n\nVALUE = 1\n")
    red = _commit(repo, "import an unused module")
    _write(repo, "app.py", CONFIGURED_APP)
    tip = _commit(repo, "drop the unused import")

    completed = _tripwire(repo, base, tip)

    assert _verdicts(completed.stdout) == {_short(repo, red): "RED", _short(repo, tip): "green"}
    assert "failed: ruff check" in completed.stdout
    assert completed.returncode == 1


def test_a_commit_that_fails_the_format_gate_under_a_green_tip_is_named(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "app.py", "VALUE=1\n")
    red = _commit(repo, "drop the spacing")
    _write(repo, "app.py", CONFIGURED_APP)
    tip = _commit(repo, "restore the spacing")

    completed = _tripwire(repo, base, tip)

    assert _verdicts(completed.stdout) == {_short(repo, red): "RED", _short(repo, tip): "green"}
    assert "failed: ruff format" in completed.stdout
    assert completed.returncode == 1


def test_a_commit_a_type_check_would_reject_is_not_this_tripwire_s_business(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "app.py", 'def typed() -> int:\n    return "text"\n')
    tip = _commit(repo, "return the wrong type")

    completed = _tripwire(repo, base, tip)

    assert _verdicts(completed.stdout) == {_short(repo, tip): "green"}
    assert completed.returncode == 0


def test_a_range_that_reads_no_commits_is_reported_as_such(tmp_path: Path) -> None:
    """A range read backwards reads nothing, and nothing read is not a passing range."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    tip = _commit(repo, "add another module")

    completed = _tripwire(repo, tip, base)

    assert "pass the cheap gates" not in completed.stdout
    assert "reads no commits" in completed.stderr
    assert completed.returncode == 2


def test_a_commit_without_a_parent_is_read_against_the_empty_tree(tmp_path: Path) -> None:
    """A range can start at a commit that has no parent. There the comment policy reads
    the whole tree, so such a commit is read rather than counted red unread."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _git(repo, "checkout", "--quiet", "--orphan", "only")
    _app(repo, REPAIRED_COMMENT)
    root = _commit(repo, "start a branch of its own")

    completed = _tripwire(repo, base, root)

    assert _verdicts(completed.stdout) == {_short(repo, root): "green"}
    assert "no parent" in completed.stdout
    assert completed.returncode == 0


def test_a_commit_without_a_parent_is_read_for_its_comments(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _git(repo, "checkout", "--quiet", "--orphan", "only")
    _app(repo, BANNED_COMMENT)
    root = _commit(repo, "start a branch of its own")

    completed = _tripwire(repo, base, root)

    assert _verdicts(completed.stdout) == {_short(repo, root): "RED"}
    assert "failed: changed comments" in completed.stdout
    assert completed.returncode == 1


def test_a_commit_whose_tree_cannot_be_materialized_is_named(tmp_path: Path) -> None:
    """A tree the tripwire cannot materialize is a commit it did not read, and it says
    so instead of ending on the failure of the git command that tried."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    tip = _commit(repo, "add another module")
    (repo / ".git" / "worktrees").write_text("", encoding="utf-8")

    completed = _tripwire(repo, base, tip)

    assert "Traceback" not in completed.stderr
    assert _verdicts(completed.stdout) == {_short(repo, tip): "RED"}
    assert "failed: unreadable tree" in completed.stdout
    assert completed.returncode == 1
