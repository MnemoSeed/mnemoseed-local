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

#: The seat vocabulary the tripwire must accept, pinned here: a seat the script's
#: own constant stops knowing turns this tuple's commits red.
REGISTERED_SEATS = (
    "general",
    "main",
    "mnemoseeding-explore",
    "product-manager",
    "senior-marketing-executive",
    "senior-qa-reviewer",
    "senior-software-engineer",
    "senior-software-engineer-muse",
    "senior-web-designer",
    "solution-architect",
)


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


def _commit(repo: Path, message: str, seat: str | None = "senior-software-engineer") -> str:
    trailer = "" if seat is None else f"\n\nSeat: {seat}"
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", f"{message}{trailer}")
    return _git(repo, "rev-parse", "HEAD")


def _verbatim_commit(repo: Path, message: bytes) -> str:
    """One commit whose stored message is exactly the given bytes: the raw-byte
    shapes under test do not survive the default message cleanup."""
    _git(repo, "add", "--all")
    stored = repo.parent / "message.txt"
    stored.write_bytes(message)
    _git(repo, "commit", "--quiet", "--cleanup=verbatim", "--file", str(stored))
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


def test_a_commit_without_a_seat_is_red_before_the_other_gates_are_read(tmp_path: Path) -> None:
    """A commit with no Seat trailer is condemned before its tree is read: its
    label is the seat trailer only, and the gates it was spared are reported
    unread, which is not the same as passing."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    unseated = _commit(repo, "add another module", seat=None)
    _write(repo, "third.py", "THIRD = 3\n")
    tip = _commit(repo, "add a third module")

    completed = _tripwire(repo, base, tip)

    assert _verdicts(completed.stdout) == {_short(repo, unseated): "RED", _short(repo, tip): "green"}
    assert "failed: seat trailer" in completed.stdout
    assert "failed: ruff check" not in completed.stdout
    assert "failed: ruff format" not in completed.stdout
    assert "failed: changed comments" not in completed.stdout
    assert unseated in completed.stderr
    assert "fixup=amend" in completed.stderr
    assert "--force-with-lease" in completed.stderr
    assert "not read" in completed.stderr
    assert completed.returncode == 1


def test_a_seat_red_tip_is_repaired_by_amend(tmp_path: Path) -> None:
    """The tip has no commit after it, so its repair is a plain amend of its own
    message; a mid-range commit is told the fixup path instead."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    tip = _commit(repo, "add another module", seat=None)

    completed = _tripwire(repo, base, tip)

    assert _verdicts(completed.stdout) == {_short(repo, tip): "RED"}
    assert "git commit --amend" in completed.stderr
    assert "fixup" not in completed.stderr
    assert completed.returncode == 1


def test_a_two_parent_merge_without_a_seat_is_green(tmp_path: Path) -> None:
    """A merge carries the seats of the commits it brings and answers to none of
    its own: two or more parents exempt its message from the seat trailer."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _git(repo, "checkout", "--quiet", "-b", "feature")
    _write(repo, "feature.py", "FEATURE = 1\n")
    feature = _commit(repo, "add the feature")
    _git(repo, "checkout", "--quiet", "main")
    _write(repo, "other.py", "OTHER = 2\n")
    elsewhere = _commit(repo, "add another module")
    _git(repo, "merge", "--quiet", "--no-commit", "feature")
    merge = _commit(repo, "merge the feature", seat=None)

    completed = _tripwire(repo, base, merge)

    assert _verdicts(completed.stdout) == {
        _short(repo, feature): "green",
        _short(repo, elsewhere): "green",
        _short(repo, merge): "green",
    }
    assert "pass the cheap gates" in completed.stdout
    assert completed.returncode == 0


def test_an_octopus_merge_without_a_seat_is_green(tmp_path: Path) -> None:
    """The exemption reads the parent count, so it holds for any merge width."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    branches = []
    for index in (1, 2, 3):
        _git(repo, "checkout", "--quiet", "-b", f"arm{index}")
        _write(repo, f"arm{index}.py", f"ARM{index} = {index}\n")
        _commit(repo, f"add arm {index}")
        _git(repo, "checkout", "--quiet", "main")
        branches.append(f"arm{index}")
    _git(repo, "merge", "--quiet", "--no-commit", *branches)
    merge = _commit(repo, "merge the three arms", seat=None)

    completed = _tripwire(repo, base, merge)

    assert _verdicts(completed.stdout)[_short(repo, merge)] == "green"
    assert completed.returncode == 0


def test_the_reserved_main_seat_is_a_vocabulary_value(tmp_path: Path) -> None:
    """A commit the orchestrating session makes itself is attributed with the
    reserved value main, green like any other registered seat."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    tip = _commit(repo, "add another module", seat="main")

    completed = _tripwire(repo, base, tip)

    assert _verdicts(completed.stdout) == {_short(repo, tip): "green"}
    assert completed.returncode == 0


def test_every_registered_seat_is_green(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    expected: dict[str, str] = {}
    tip = base
    for seat in REGISTERED_SEATS:
        _write(repo, f"{seat}.py", f"{seat.replace('-', '_').upper()} = 1\n")
        tip = _commit(repo, f"add {seat}", seat=seat)
        expected[_short(repo, tip)] = "green"

    completed = _tripwire(repo, base, tip)

    assert _verdicts(completed.stdout) == expected
    assert completed.returncode == 0


def test_a_lowercase_seat_key_is_red(tmp_path: Path) -> None:
    """The trailer key is matched as written: a lowercase seat line is no seat."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    lower = _commit(repo, "add another module\n\nseat: senior-software-engineer", seat=None)

    completed = _tripwire(repo, base, lower)

    assert _verdicts(completed.stdout) == {_short(repo, lower): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_a_seat_without_the_colon_space_is_red(tmp_path: Path) -> None:
    """The stored line is judged verbatim, where `Seat:main` keeps its missing
    space: the parser's normalized output would print it well-formed."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    tight = _commit(repo, "add another module\n\nSeat:main", seat=None)

    completed = _tripwire(repo, base, tight)

    assert _verdicts(completed.stdout) == {_short(repo, tight): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_a_seat_with_extra_spacing_is_red(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    loose = _commit(repo, "add another module\n\nSeat:  main", seat=None)

    completed = _tripwire(repo, base, loose)

    assert _verdicts(completed.stdout) == {_short(repo, loose): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_a_seat_outside_the_vocabulary_is_red(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    unknown = _commit(repo, "add another module\n\nSeat: ghost", seat=None)

    completed = _tripwire(repo, base, unknown)

    assert _verdicts(completed.stdout) == {_short(repo, unknown): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert "ghost" in completed.stderr
    assert completed.returncode == 1


def test_a_capitalized_seat_value_is_red(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    shifted = _commit(repo, "add another module\n\nSeat: Senior-Software-Engineer", seat=None)

    completed = _tripwire(repo, base, shifted)

    assert _verdicts(completed.stdout) == {_short(repo, shifted): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_more_than_one_seat_trailer_is_red(tmp_path: Path) -> None:
    """Two seat trailers in the block are two claims and no single answer, even
    when one of them is itself malformed."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    doubled = _commit(repo, "add another module\n\nSeat: main\nSeat:main", seat=None)

    completed = _tripwire(repo, base, doubled)

    assert _verdicts(completed.stdout) == {_short(repo, doubled): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_a_seat_line_outside_the_trailer_block_is_missing(tmp_path: Path) -> None:
    """Only the trailer block is counted: a `Seat:` line in the body is not
    parsed and the commit reads as having no seat at all."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    stray = _commit(repo, "add another module\n\nSeat: main\n\nA trailing body paragraph.", seat=None)

    completed = _tripwire(repo, base, stray)

    assert _verdicts(completed.stdout) == {_short(repo, stray): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_a_stray_seat_line_does_not_count_against_the_block_seat(tmp_path: Path) -> None:
    """Counting happens in the trailer block only: a stray seat line in the body
    is neither a second seat nor a red next to a valid block trailer."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    stray = _commit(
        repo,
        "add another module\n\nSeat: senior-software-engineer\n\nA trailing body paragraph.\n\nSeat: main",
        seat=None,
    )

    completed = _tripwire(repo, base, stray)

    assert _verdicts(completed.stdout) == {_short(repo, stray): "green"}
    assert "pass the cheap gates" in completed.stdout
    assert completed.returncode == 0


def test_a_seat_with_a_trailing_space_is_red(tmp_path: Path) -> None:
    """The stored line is judged verbatim: a trailing space after the value is a
    shape the parser normalizes away, and the stored bytes do not pass."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    spaced = _verbatim_commit(repo, b"add another module\n\nSeat: main \n")

    completed = _tripwire(repo, base, spaced)

    assert _verdicts(completed.stdout) == {_short(repo, spaced): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_a_crlf_seat_line_is_red(tmp_path: Path) -> None:
    """A CRLF-authored trailer carries its carriage return in the stored bytes,
    and the verbatim judgment reads it: the line is not the shape a seat is."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    crlf = _verbatim_commit(repo, b"add another module\r\n\r\nSeat: main\r\n")

    completed = _tripwire(repo, base, crlf)

    assert _verdicts(completed.stdout) == {_short(repo, crlf): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_a_whitespace_only_separator_does_not_rescue_a_malformed_seat(tmp_path: Path) -> None:
    """A whitespace-only line ends a paragraph like an empty one: the trailer
    block stays the message's last paragraph, and a malformed seat behind the
    separator is not rescued by the well-formed seat above it."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    split = _verbatim_commit(
        repo,
        b"subject\n\nSeat: main\n   \nSigned-off-by: Gate <g@x.y>\nSeat:main\n",
    )

    completed = _tripwire(repo, base, split)

    assert _verdicts(completed.stdout) == {_short(repo, split): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_a_trailing_whitespace_line_does_not_disturb_the_block_seat(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    padded = _verbatim_commit(repo, b"add another module\n\nSeat: main\n   \n")

    completed = _tripwire(repo, base, padded)

    assert _verdicts(completed.stdout) == {_short(repo, padded): "green"}
    assert "pass the cheap gates" in completed.stdout
    assert completed.returncode == 0


def test_a_mixed_ending_seat_line_is_red(tmp_path: Path) -> None:
    """An LF message with one CRLF-authored trailer carries the carriage return
    in its stored bytes, and the shape judgment reads it."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    mixed = _verbatim_commit(repo, b"add another module\n\nSeat: main\r\n")

    completed = _tripwire(repo, base, mixed)

    assert _verdicts(completed.stdout) == {_short(repo, mixed): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_an_orphan_root_without_a_seat_is_red(tmp_path: Path) -> None:
    """A parentless commit is not a merge: only two or more parents excuse a
    seat, so a root commit answers for its own like any other."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _git(repo, "checkout", "--quiet", "--orphan", "only")
    _app(repo, REPAIRED_COMMENT)
    root = _commit(repo, "start a branch of its own", seat=None)

    completed = _tripwire(repo, base, root)

    assert _verdicts(completed.stdout) == {_short(repo, root): "RED"}
    assert "failed: seat trailer" in completed.stdout
    assert completed.returncode == 1


def test_a_lowercase_seat_line_beside_the_block_seat_is_inert(tmp_path: Path) -> None:
    """A lowercase `seat:` line is no Seat claim: it is neither a second trailer
    nor an obstacle to the well-formed one in the same block."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    mixed = _commit(repo, "add another module\n\nSeat: main\nseat: ghost", seat=None)

    completed = _tripwire(repo, base, mixed)

    assert _verdicts(completed.stdout) == {_short(repo, mixed): "green"}
    assert "pass the cheap gates" in completed.stdout
    assert completed.returncode == 0


def test_a_tree_corrupt_commit_is_reported_unread_not_crashed(tmp_path: Path) -> None:
    """A commit whose tree cannot be read is a commit whose gates did not run:
    the seat verdict reads the commit object alone, and the unread tree is
    reported rather than ending the run on a traceback."""
    repo = _repository(tmp_path)
    base = _commit(repo, "baseline")
    _write(repo, "other.py", "OTHER = 2\n")
    tip = _commit(repo, "add another module")
    tree = _git(repo, "rev-parse", f"{tip}^{{tree}}")
    stored = repo / ".git" / "objects" / tree[:2] / tree[2:]
    stored.chmod(0o666)
    stored.write_bytes(b"corrupt")

    completed = _tripwire(repo, base, tip)

    assert "Traceback" not in completed.stderr
    assert _verdicts(completed.stdout) == {_short(repo, tip): "RED"}
    assert "failed: unreadable tree" in completed.stdout
    assert completed.returncode == 1
