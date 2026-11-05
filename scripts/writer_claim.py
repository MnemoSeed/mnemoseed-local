"""Per-branch single-writer claim registry (advisory slice).

Maintains O_EXCL claim artifacts and an append-only event ledger under the org
claims directory, with explicit owner/main release and no expiry logic. The
claim only makes same-branch collisions visible and refused at the claim file;
nothing else is gated.

Claims-dir resolution order: --claims-dir > MNEMOSEED_WRITER_CLAIMS_DIR >
<repo-main-root>/../org/claims, where the repo main root is the parent of
`git rev-parse --git-common-dir` (stable from any linked worktree).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "mnemoseed.writer-claim/1"
HISTORY_FILENAME = "history.jsonl"
CLAIMS_DIR_ENV = "MNEMOSEED_WRITER_CLAIMS_DIR"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_CORRUPT = 3
EXIT_ENVIRONMENT = 4

CREATE_ATTEMPTS = 5
_TRANSIENT_CREATE_ERRNOS = (errno.EACCES, errno.EPERM, errno.EBUSY)

_SAFE_BYTES = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789._-")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class HistoryCorrupt(RuntimeError):
    """The append-only ledger contains a line that is not a well-formed event."""


class GitFailure(RuntimeError):
    """A required git query failed."""


def branch_claim_filename(branch: str) -> str:
    encoded: list[str] = []
    for byte in branch.encode("utf-8"):
        encoded.append(chr(byte) if byte in _SAFE_BYTES else f"%{byte:02X}")
    return "".join(encoded)


def branch_from_claim_filename(filename: str) -> str:
    data = bytearray()
    index = 0
    while index < len(filename):
        char = filename[index]
        if char == "%":
            pair = filename[index + 1 : index + 3]
            if len(pair) != 2 or any(ch not in _HEX_DIGITS for ch in pair):
                raise ValueError(f"malformed percent-encoding in claim filename: {filename!r}")
            data.append(int(pair, 16))
            index += 3
        else:
            data.extend(char.encode("utf-8"))
            index += 1
    return data.decode("utf-8")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _git(cwd: Path, *args: str) -> str:
    proc = _run_git(cwd, *args)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitFailure(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.strip()


def _git_optional(cwd: Path, *args: str) -> str | None:
    proc = _run_git(cwd, *args)
    return proc.stdout.strip() if proc.returncode == 0 else None


def resolve_default_claims_dir(cwd: Path) -> Path:
    raw = _git(cwd, "rev-parse", "--git-common-dir")
    gitdir = Path(raw)
    if not gitdir.is_absolute():
        gitdir = cwd / gitdir
    return gitdir.resolve().parent.parent / "org" / "claims"


def resolve_claims_dir(explicit: str | None, cwd: Path) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get(CLAIMS_DIR_ENV)
    if from_env:
        return Path(from_env).expanduser().resolve()
    return resolve_default_claims_dir(cwd)


def resolve_base_head(cwd: Path, branch: str, explicit: str | None) -> str:
    """Claim-time anchor: explicit value wins; otherwise the branch tip when it
    exists, else HEAD (the start point a new `git worktree add -b` would take)."""
    if explicit is not None:
        sha = _git_optional(cwd, "rev-parse", "--verify", "--quiet", f"{explicit}^{{commit}}")
        if sha is None:
            raise GitFailure(f"--base-head {explicit!r} does not resolve to a commit")
        return sha
    sha = _git_optional(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if sha is None:
        sha = _git(cwd, "rev-parse", "HEAD")
    return sha


def ledger_path(claims_dir: Path, repo: str) -> Path:
    return claims_dir / repo / HISTORY_FILENAME


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HistoryCorrupt(f"{path}:{lineno}: line is not JSON") from exc
            seq = event.get("seq") if isinstance(event, dict) else None
            if not isinstance(seq, int) or isinstance(seq, bool):
                raise HistoryCorrupt(f"{path}:{lineno}: line is not a seq-numbered event")
            events.append(event)
    return events


def next_seq(events: list[dict[str, Any]]) -> int:
    # The counter base is 0 for an empty or missing ledger.
    return max((event["seq"] for event in events), default=0) + 1


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
    try:
        view = memoryview(line.encode("utf-8"))
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def read_claim_artifact(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a claim artifact; (artifact, None) when healthy, (None, reason) when damaged."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"unreadable: {exc}"
    try:
        artifact = json.loads(raw)
    except json.JSONDecodeError:
        return None, "unparseable-JSON"
    if not isinstance(artifact, dict):
        return None, "not-a-JSON-object"
    try:
        decoded = branch_from_claim_filename(path.stem)
    except ValueError:
        return None, "malformed-filename"
    if decoded != artifact.get("branch"):
        return None, "filename-branch-mismatch"
    return artifact, None


def create_exclusive(path: Path, body: str) -> bool:
    """O_CREAT|O_EXCL create; False when the file already exists. A failed
    write unlinks the file this call created so it cannot block later claims."""
    data = body.encode("utf-8")
    for attempt in range(CREATE_ATTEMPTS):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError as exc:
            # Transient Windows create contention (EPERM/EACCES/EBUSY) is retried
            # with bounded backoff; any other failure propagates.
            if exc.errno not in _TRANSIENT_CREATE_ERRNOS or attempt == CREATE_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
            continue
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
        except OSError:
            os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        os.close(fd)
        return True
    raise OSError(errno.EBUSY, "exclusive create attempts exhausted")


def claim_file_path(claims_dir: Path, repo: str, branch: str) -> Path:
    return claims_dir / repo / f"{branch_claim_filename(branch)}.json"


def artifact_branch_key(file: Path, artifact: dict[str, Any] | None) -> str | None:
    """Filter key: the verbatim content branch when healthy, else the decoded
    filename so damaged artifacts stay addressable under status --branch."""
    if artifact is not None and isinstance(artifact.get("branch"), str):
        return artifact["branch"]
    try:
        return branch_from_claim_filename(file.stem)
    except ValueError:
        return None


def cmd_claim(args: argparse.Namespace, claims_dir: Path, cwd: Path) -> int:
    ledger = ledger_path(claims_dir, args.repo)
    seq = next_seq(read_ledger(ledger))
    base_head = resolve_base_head(cwd, args.branch, args.base_head)
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "repo": args.repo,
        "branch": args.branch,
        "seat": args.seat,
        "worktree": str(Path(args.worktree).expanduser().resolve()),
        "base_head": base_head,
        "claimed_at": _utc_now(),
    }
    if args.issue is not None:
        artifact["issue"] = args.issue

    claim_file = claim_file_path(claims_dir, args.repo, args.branch)
    claim_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        created = create_exclusive(claim_file, _line(artifact))
    except OSError as exc:
        _emit({"result": "refused", "reason": "create-failed", "detail": str(exc), "file": str(claim_file)})
        return EXIT_REFUSED
    if not created:
        holder, corruption = read_claim_artifact(claim_file)
        snapshot: Any = holder if holder is not None else {"artifact_corrupt": corruption}
        append_line(
            ledger,
            _line(
                {
                    "seq": seq,
                    "type": "refused",
                    "ts": _utc_now(),
                    "repo": args.repo,
                    "branch": args.branch,
                    "attempted_by": args.seat,
                    "holder": snapshot,
                }
            ),
        )
        _emit(
            {
                "result": "refused",
                "repo": args.repo,
                "branch": args.branch,
                "attempted_by": args.seat,
                "holder": snapshot,
            }
        )
        return EXIT_REFUSED

    append_line(ledger, _line({"seq": seq, "type": "claim", "ts": artifact["claimed_at"], **artifact}))
    _emit({"result": "claimed", "file": str(claim_file), **artifact})
    return EXIT_OK


def cmd_release(args: argparse.Namespace, claims_dir: Path, cwd: Path) -> int:
    ledger = ledger_path(claims_dir, args.repo)
    events = read_ledger(ledger)
    claim_file = claim_file_path(claims_dir, args.repo, args.branch)
    if not claim_file.exists():
        _emit({"result": "refused", "reason": "no-active-claim", "repo": args.repo, "branch": args.branch})
        return EXIT_REFUSED
    artifact, corruption = read_claim_artifact(claim_file)
    if artifact is None:
        _emit({"result": "artifact-corrupt", "detail": corruption, "file": str(claim_file)})
        return EXIT_CORRUPT

    if args.main:
        mode, released_by, reason = "main", "main", args.reason
    elif artifact.get("seat") == args.seat:
        mode, released_by, reason = "owner", args.seat, None
    else:
        _emit(
            {
                "result": "refused",
                "reason": "seat-mismatch",
                "repo": args.repo,
                "branch": args.branch,
                "held_by": artifact.get("seat"),
            }
        )
        return EXIT_REFUSED

    event = {
        "seq": next_seq(events),
        "type": "release",
        "ts": _utc_now(),
        "repo": args.repo,
        "branch": args.branch,
        "released_by": released_by,
        "mode": mode,
        "reason": reason,
    }
    # The ledger event is appended before the unlink so a crash never loses the release record.
    append_line(ledger, _line(event))
    claim_file.unlink()
    _emit({"result": "released", "file": str(claim_file), **event})
    return EXIT_OK


def cmd_status(args: argparse.Namespace, claims_dir: Path, cwd: Path) -> int:
    repos = (
        [args.repo]
        if args.repo
        else sorted(entry.name for entry in claims_dir.iterdir() if entry.is_dir())
        if claims_dir.exists()
        else []
    )
    claims: list[dict[str, Any]] = []
    health: dict[str, str] = {}
    corrupt = False
    for repo in repos:
        try:
            read_ledger(ledger_path(claims_dir, repo))
            health[repo] = "ok"
        except HistoryCorrupt:
            health[repo] = "HISTORY_CORRUPT"
            corrupt = True
        repo_dir = claims_dir / repo
        for file in sorted(repo_dir.glob("*.json")) if repo_dir.exists() else []:
            artifact, corruption = read_claim_artifact(file)
            entry: dict[str, Any] = dict(artifact) if artifact is not None else {"repo": repo}
            entry["file"] = str(file.resolve())
            entry["corrupt"] = corruption
            if args.branch is not None and artifact_branch_key(file, artifact) != args.branch:
                continue
            claims.append(entry)
    _emit({"claims_dir": str(claims_dir), "claims": claims, "history": health})
    return EXIT_CORRUPT if corrupt else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="writer_claim",
        description="Per-branch single-writer claim registry (advisory).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--claims-dir",
        default=None,
        help="claims directory override (default: <repo-main-root>/../org/claims)",
    )

    claim = sub.add_parser(
        "claim",
        parents=[common],
        help="create the per-branch claim artifact or record the refusal",
    )
    claim.add_argument("--repo", required=True)
    claim.add_argument("--branch", required=True)
    claim.add_argument("--seat", required=True)
    claim.add_argument(
        "--worktree",
        required=True,
        help="worktree path the claim binds to (created after the claim)",
    )
    claim.add_argument(
        "--base-head",
        default=None,
        help="start commit; default: branch tip when it exists, else HEAD",
    )
    claim.add_argument("--issue", type=int, default=None)

    release = sub.add_parser(
        "release",
        parents=[common],
        help="owner release by seat match, or main release of a stale claim",
    )
    release.add_argument("--repo", required=True)
    release.add_argument("--branch", required=True)
    exclusive = release.add_mutually_exclusive_group(required=True)
    exclusive.add_argument("--seat", default=None)
    exclusive.add_argument("--main", action="store_true")
    release.add_argument(
        "--reason",
        default=None,
        help="required for --main; recorded verbatim in the ledger",
    )

    status = sub.add_parser("status", parents=[common], help="list claims and ledger health")
    status.add_argument("--repo", default=None)
    status.add_argument("--branch", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "release":
        if args.reason is not None and not args.main:
            parser.error("--reason requires --main")
        if args.main and (args.reason is None or not args.reason.strip()):
            parser.error("--main requires a non-empty --reason")

    identity_fields = ("repo", "branch", "seat") if args.command != "status" else ("repo", "branch")
    for field in identity_fields:
        value = getattr(args, field)
        if value is not None and not value:
            parser.error(f"--{field} must be a non-empty identifier")

    cwd = Path.cwd()
    handlers = {"claim": cmd_claim, "release": cmd_release, "status": cmd_status}
    try:
        claims_dir = resolve_claims_dir(args.claims_dir, cwd)
        return handlers[args.command](args, claims_dir, cwd)
    except HistoryCorrupt as exc:
        _emit({"result": "HISTORY_CORRUPT", "detail": str(exc)})
        return EXIT_CORRUPT
    except GitFailure as exc:
        _emit({"result": "git-failure", "detail": str(exc)})
        return EXIT_ENVIRONMENT
    except OSError as exc:
        _emit({"result": "environment-error", "detail": str(exc)})
        return EXIT_ENVIRONMENT


if __name__ == "__main__":
    raise SystemExit(main())
