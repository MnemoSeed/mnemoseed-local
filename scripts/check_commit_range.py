"""Run the cheap gates on every commit of a range.

CI reads the tip of a branch, so a commit underneath it can be gate-red while the tip is
green, and a repair folded into a later commit or a merge leaves that commit red with
nothing to show for it. This reads the range a pull request adds, materializes every commit
in a worktree of its own, and fails when a commit does not pass the cheap static gates. The
suite and the type check read the tip, where their cost is paid once. Every non-merge commit
must also carry a `Seat: <seat-id>` trailer naming the session that made it; a commit that
fails that check is condemned before its tree is read, and the gates it was spared are
reported unread, which is not passing.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The verdict for a commit whose tree could not be materialized: it was not read, so
#: it is named with the red commits rather than passing unread.
UNREAD = "unreadable tree"

#: The gates cheap enough to run on every commit of a range, cheapest first. The
#: comment policy reads the diff against the commit's own parent.
CHEAP_GATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("changed comments", ("{python}", "scripts/check_changed_comments.py", "--base", "{parent}")),
    ("ruff check", ("ruff", "check")),
    ("ruff format", ("ruff", "format", "--check")),
)

#: The verdict for a commit whose message fails the seat trailer.
SEAT = "seat trailer"

#: The seats a commit may claim: the registered delegation seats, the built-in general
#: seat, and the reserved value for the orchestrating session. The list lives here
#: because the CI runner reads no user-level configuration. The trailer is
#: self-declaration, so this guards spelling only, never identity.
SEATS = frozenset(
    {
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
    }
)

#: The one stored line a passing seat trailer is, verbatim: key case and the single
#: space after the colon are part of the shape, and the parser's output cannot be
#: judged for them because it normalizes both away.
SEAT_LINE = re.compile(rb"Seat: ([a-z][a-z0-9-]*)")


def _git(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        input=stdin,
        check=True,
    )
    return result.stdout.strip()


def commits_of(base: str, head: str) -> tuple[str, ...]:
    """The commits one range adds, oldest first, a merge included."""
    return tuple(_git("rev-list", "--reverse", f"{base}..{head}").splitlines())


def _parents_of(commit: str) -> tuple[str, ...]:
    """One commit's parents, empty when it has none."""
    fields = _git("rev-list", "--parents", "-n", "1", commit).split()
    return tuple(fields[1:])


def _empty_tree() -> str:
    """The empty tree object, written so a commit without a parent can be diffed."""
    return _git("hash-object", "-w", "-t", "tree", "--stdin", stdin="")


def _base_of(commit: str) -> str:
    """What the comment policy diffs one commit against: the commit's parent, or the
    whole tree when the range starts at a commit that has none."""
    parents = _parents_of(commit)
    if parents:
        return parents[0]
    print(f"{commit[:12]} has no parent: its whole tree is read")
    return _empty_tree()


def _passes(gate: tuple[str, ...], parent: str, worktree: Path) -> bool:
    """Whether one gate passes in one commit's own tree."""
    command = [part.format(python=sys.executable, parent=parent) for part in gate]
    try:
        completed = subprocess.run(command, cwd=worktree)
    except FileNotFoundError as missing:
        print(f"    {missing}", file=sys.stderr)
        return False
    return completed.returncode == 0


def _remove_worktree(worktree: Path) -> None:
    """Remove one materialized tree, reporting a failure rather than masking the
    verdict that was already reached."""
    try:
        _git("worktree", "remove", "--force", str(worktree))
    except subprocess.CalledProcessError as failure:
        print(f"    cannot remove {worktree}: {failure}", file=sys.stderr)


def red_gates(commit: str, scratch: Path) -> tuple[str, ...]:
    """The cheap gates one commit fails, read from a worktree of its own."""
    worktree = scratch / commit[:12]
    try:
        _git("worktree", "add", "--quiet", "--detach", str(worktree), commit)
    except subprocess.CalledProcessError as failure:
        print(f"    cannot materialize {commit[:12]}: {failure}", file=sys.stderr)
        return (UNREAD,)
    try:
        parent = _base_of(commit)
        return tuple(label for label, gate in CHEAP_GATES if not _passes(gate, parent, worktree))
    finally:
        _remove_worktree(worktree)


def _raw_message(commit: str) -> bytes:
    """The commit object's message bytes as stored, headers dropped: the shape of
    the seat line is judged on what was committed, not on parsed output."""
    stored = subprocess.run(
        ["git", "cat-file", "commit", commit],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    parts = stored.split(b"\n\n", 1)
    return parts[1] if len(parts) > 1 else b""


def _trailer_block(message: bytes) -> tuple[bytes, ...]:
    """The message's last paragraph, the run of non-blank lines at its end, where
    the parser looks for the trailer block; a whitespace-only line ends a
    paragraph like an empty one."""
    lines = message.split(b"\n")
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start and lines[start - 1].strip():
        start -= 1
    return tuple(lines[start:end])


def _trailers(commit: str) -> tuple[str, ...]:
    """The trailers the commit's own parser reads out of the stored message: a
    `Seat:` line outside the block is not among them and counts as nothing."""
    message = _raw_message(commit).decode("utf-8", "replace")
    return tuple(_git("interpret-trailers", "--parse", stdin=message).splitlines())


def _seat_failure(commit: str) -> str | None:
    """Why the commit fails the seat trailer, or None when it passes or is a merge,
    which carries the seats of the commits it brings and answers to none of its own."""
    if len(_parents_of(commit)) >= 2:
        return None
    claimed = [line for line in _trailers(commit) if line.partition(":")[0] == "Seat"]
    if len(claimed) > 1:
        return "more than one Seat trailer"
    if not claimed:
        return "no Seat trailer"
    stored = [
        match.group(1).decode("ascii", "replace")
        for line in _trailer_block(_raw_message(commit))
        if (match := SEAT_LINE.fullmatch(line))
    ]
    if len(stored) != 1:
        return "malformed Seat trailer"
    if stored[0] not in SEATS:
        return f"unknown seat {stored[0]!r}"
    return None


def _repair(commit: str, tip: str) -> str:
    """The one repair a seat-red commit has: a trailer belongs to its own commit,
    so no commit after it can supply one, and commits on main are never rewritten."""
    if commit == tip:
        return f"repair {commit}: git commit --amend with the trailer added, then git push --force-with-lease"
    return (
        f"repair {commit} on its unmerged branch: git commit --fixup=amend:{commit}, "
        "git rebase -i --autosquash, then git push --force-with-lease"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="the commit the range starts after")
    parser.add_argument("head", help="the last commit of the range")
    args = parser.parse_args(argv)
    try:
        commits = commits_of(args.base, args.head)
    except subprocess.CalledProcessError:
        print(f"cannot read the range {args.base}..{args.head} in {REPO_ROOT}", file=sys.stderr)
        return 2

    if not commits:
        print(f"{args.base}..{args.head} reads no commits: nothing was read", file=sys.stderr)
        return 2

    scratch = Path(tempfile.mkdtemp(prefix="commit-range-"))
    red: list[str] = []
    try:
        for commit in commits:
            failure = _seat_failure(commit)
            broken = red_gates(commit, scratch) if failure is None else (SEAT,)
            print(f"{'RED ' if broken else 'green'} {_git('log', '-1', '--format=%h %s', commit)}")
            if broken:
                red.append(commit)
                print(f"    failed: {', '.join(broken)}")
            if failure is not None:
                print(f"    {failure} in {commit}", file=sys.stderr)
                print(f"    {_repair(commit, commits[-1])}", file=sys.stderr)
                print(
                    "    the other cheap gates were not read: unread is not passing",
                    file=sys.stderr,
                )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if red:
        print(
            f"{len(red)} of {len(commits)} commits in {args.base}..{args.head} does not pass "
            f"the cheap gates: {' '.join(red)}",
            file=sys.stderr,
        )
        return 1
    print(f"{len(commits)} commits in {args.base}..{args.head} pass the cheap gates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
