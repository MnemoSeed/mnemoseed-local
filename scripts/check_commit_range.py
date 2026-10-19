"""Run the cheap gates on every commit of a range.

CI reads the tip of a branch, so a commit underneath it can be gate-red while the tip is
green, and a repair folded into a later commit or a merge leaves that commit red with
nothing to show for it. This reads the range a pull request adds, materializes every commit
in a worktree of its own, and fails when a commit does not pass the cheap static gates. The
suite and the type check read the tip, where their cost is paid once.
"""

from __future__ import annotations

import argparse
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


def _parent_of(commit: str) -> str | None:
    """One commit's first parent, or None when it has none."""
    fields = _git("rev-list", "--parents", "-n", "1", commit).split()
    return fields[1] if len(fields) > 1 else None


def _empty_tree() -> str:
    """The empty tree object, written so a commit without a parent can be diffed."""
    return _git("hash-object", "-w", "-t", "tree", "--stdin", stdin="")


def _base_of(commit: str) -> str:
    """What the comment policy diffs one commit against: the commit's parent, or the
    whole tree when the range starts at a commit that has none."""
    parent = _parent_of(commit)
    if parent is not None:
        return parent
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
            broken = red_gates(commit, scratch)
            print(f"{'RED ' if broken else 'green'} {_git('log', '-1', '--format=%h %s', commit)}")
            if broken:
                red.append(commit)
                print(f"    failed: {', '.join(broken)}")
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
