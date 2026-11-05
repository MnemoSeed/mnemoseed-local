"""Writer-claim CLI contract: per-branch O_EXCL artifacts, append-only ledger, explicit release.

The slice is advisory bookkeeping. These tests pin the artifact schema, the
ledger invariants, filename-encoding injectivity, and the worktree-safe default
claims-dir resolution. Every test runs against tmp claims dirs and tmp git
layouts; the real org HQ claims directory is never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "writer_claim.py"

sys.path.insert(0, str(REPO_ROOT))

from scripts.writer_claim import branch_claim_filename, branch_from_claim_filename  # noqa: E402

GIT_IDENTITY = (
    "-c",
    "user.name=gate",
    "-c",
    "user.email=gate@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
)
ENV_OVERRIDE = "MNEMOSEED_WRITER_CLAIMS_DIR"
SCHEMA = "mnemoseed.writer-claim/1"


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def init_repo(layout: Path) -> Path:
    repo = layout / "mainrepo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-qm", "seed")
    return repo


def cli_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != ENV_OVERRIDE}
    env.update(overrides)
    return env


def run_cli(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=env if env is not None else cli_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def ledger_path(claims: Path, repo: str = "mnemoseed-local") -> Path:
    return claims / repo / "history.jsonl"


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def artifact_path(claims: Path, branch: str, repo: str = "mnemoseed-local") -> Path:
    return claims / repo / f"{branch_claim_filename(branch)}.json"


def claim_cmd(
    branch: str,
    seat: str,
    worktree: Path,
    claims: Path | None = None,
    *,
    issue: int | None = None,
) -> list[str]:
    args = [
        "claim",
        "--repo",
        "mnemoseed-local",
        "--branch",
        branch,
        "--seat",
        seat,
        "--worktree",
        str(worktree),
    ]
    if claims is not None:
        args += ["--claims-dir", str(claims)]
    if issue is not None:
        args += ["--issue", str(issue)]
    return args


def release_cmd(
    branch: str, claims: Path | None = None, *, seat: str | None = None, main_reason: str | None = None
) -> list[str]:
    args = ["release", "--repo", "mnemoseed-local", "--branch", branch]
    if claims is not None:
        args += ["--claims-dir", str(claims)]
    if seat is not None:
        args += ["--seat", seat]
    if main_reason is not None:
        args += ["--main", "--reason", main_reason]
    return args


def status_cmd(claims: Path | None = None, *, branch: str | None = None) -> list[str]:
    args = ["status"]
    if claims is not None:
        args += ["--claims-dir", str(claims)]
    if branch is not None:
        args += ["--branch", branch]
    return args


def collect_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys |= collect_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= collect_keys(item)
    return keys


def assert_uppercase_hex(filename: str) -> None:
    index = 0
    while index < len(filename):
        if filename[index] == "%":
            pair = filename[index + 1 : index + 3]
            assert len(pair) == 2
            assert all(ch in "0123456789ABCDEF" for ch in pair)
            index += 3
        else:
            index += 1


def test_double_claim_refuses_and_keeps_first_artifact_byte_identical(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"
    base_head = git(repo, "rev-parse", "HEAD")

    first = run_cli(repo, *claim_cmd("feat/demo", "seat-a", layout / "wt-a", claims))
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["base_head"] == base_head

    artifact = artifact_path(claims, "feat/demo")
    assert artifact.exists()
    before = artifact.read_bytes()
    first_data = json.loads(artifact.read_text(encoding="utf-8"))

    second = run_cli(repo, *claim_cmd("feat/demo", "seat-b", layout / "wt-b", claims))
    assert second.returncode != 0
    assert "refused" in second.stdout
    assert artifact.read_bytes() == before

    events = read_events(ledger_path(claims))
    assert [event["type"] for event in events] == ["claim", "refused"]
    refused = events[-1]
    assert refused["attempted_by"] == "seat-b"
    assert refused["holder"] == first_data


def test_ledger_is_append_only_with_strictly_increasing_seq(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"
    ledger = ledger_path(claims)

    def appended_exactly_one_line(before: bytes) -> None:
        after = ledger.read_bytes()
        assert after.startswith(before)
        assert after.count(b"\n") == before.count(b"\n") + 1

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-a", layout / "wt-a", claims))
    assert proc.returncode == 0, proc.stderr
    assert len(read_events(ledger)) == 1

    snapshot = ledger.read_bytes()
    proc = run_cli(repo, *claim_cmd("feat/one", "seat-b", layout / "wt-b", claims))
    assert proc.returncode != 0
    appended_exactly_one_line(snapshot)
    snapshot = ledger.read_bytes()

    proc = run_cli(repo, *release_cmd("feat/one", claims, seat="seat-a"))
    assert proc.returncode == 0, proc.stderr
    appended_exactly_one_line(snapshot)
    snapshot = ledger.read_bytes()

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-c", layout / "wt-c", claims))
    assert proc.returncode == 0, proc.stderr
    appended_exactly_one_line(snapshot)

    events = read_events(ledger)
    assert [event["type"] for event in events] == ["claim", "refused", "release", "claim"]
    seqs = [event["seq"] for event in events]
    assert all(later > earlier for earlier, later in zip(seqs, seqs[1:], strict=False))
    assert len(seqs) == len(set(seqs))
    assert all(isinstance(seq, int) for seq in seqs)


def test_stale_claim_date_grants_no_expiry(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-a", layout / "wt-a", claims))
    assert proc.returncode == 0, proc.stderr

    artifact = artifact_path(claims, "feat/one")
    data = json.loads(artifact.read_text(encoding="utf-8"))
    data["claimed_at"] = "2000-01-01T00:00:00+00:00"
    artifact.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")

    stale = run_cli(repo, *claim_cmd("feat/one", "seat-b", layout / "wt-b", claims))
    assert stale.returncode != 0
    assert "refused" in stale.stdout
    assert artifact.exists()
    held = json.loads(artifact.read_text(encoding="utf-8"))
    assert held["claimed_at"] == "2000-01-01T00:00:00+00:00"


def test_owner_release_frees_branch_for_immediate_reclaim(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"
    worktree = layout / "wt-a"
    base_head = git(repo, "rev-parse", "HEAD")

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-a", worktree, claims))
    assert proc.returncode == 0, proc.stderr

    proc = run_cli(repo, *release_cmd("feat/one", claims, seat="seat-a"))
    assert proc.returncode == 0, proc.stderr
    assert not artifact_path(claims, "feat/one").exists()

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-b", layout / "wt-b", claims))
    assert proc.returncode == 0, proc.stderr

    events = read_events(ledger_path(claims))
    assert [event["type"] for event in events] == ["claim", "release", "claim"]
    release = events[1]
    assert release["mode"] == "owner"
    assert release["released_by"] == "seat-a"
    assert release["reason"] is None

    payload = json.loads(run_cli(repo, *status_cmd(claims, branch="feat/one")).stdout)
    assert len(payload["claims"]) == 1
    assert payload["claims"][0]["seat"] == "seat-b"
    assert payload["claims"][0]["worktree"] == str((layout / "wt-b").resolve())
    assert payload["claims"][0]["base_head"] == base_head


def test_main_release_requires_explicit_nonempty_reason(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-a", layout / "wt-a", claims))
    assert proc.returncode == 0, proc.stderr
    artifact = artifact_path(claims, "feat/one")

    missing_reason = run_cli(repo, *release_cmd("feat/one", claims, main_reason=""))
    assert missing_reason.returncode != 0
    assert artifact.exists()

    blank_reason = run_cli(repo, *release_cmd("feat/one", claims, main_reason="   "))
    assert blank_reason.returncode != 0
    assert artifact.exists()

    neither_form = run_cli(repo, *release_cmd("feat/one", claims))
    assert neither_form.returncode != 0
    assert artifact.exists()

    released = run_cli(repo, *release_cmd("feat/one", claims, main_reason="seat crashed mid-batch"))
    assert released.returncode == 0, released.stderr
    assert not artifact.exists()

    events = read_events(ledger_path(claims))
    assert events[-1]["type"] == "release"
    assert events[-1]["mode"] == "main"
    assert events[-1]["released_by"] == "main"
    assert events[-1]["reason"] == "seat crashed mid-batch"


def test_release_with_mismatched_seat_is_refused_without_side_effects(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-a", layout / "wt-a", claims))
    assert proc.returncode == 0, proc.stderr
    artifact = artifact_path(claims, "feat/one")
    ledger = ledger_path(claims)
    before = ledger.read_bytes()

    mismatch = run_cli(repo, *release_cmd("feat/one", claims, seat="seat-b"))
    assert mismatch.returncode != 0
    assert "refused" in mismatch.stdout
    assert artifact.exists()
    assert ledger.read_bytes() == before


def test_release_without_active_claim_is_refused(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"

    proc = run_cli(repo, *release_cmd("feat/none", claims, seat="seat-a"))
    assert proc.returncode != 0
    assert not ledger_path(claims).exists()
    assert not artifact_path(claims, "feat/none").exists()


def test_corrupt_ledger_tail_fails_loud_without_truncation(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"
    ledger = ledger_path(claims)

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-a", layout / "wt-a", claims))
    assert proc.returncode == 0, proc.stderr
    ledger.write_text(ledger.read_text(encoding="utf-8") + "THIS LINE IS NOT JSON\n", encoding="utf-8")
    before = ledger.read_bytes()

    status = run_cli(repo, *status_cmd(claims))
    assert status.returncode != 0
    assert "HISTORY_CORRUPT" in status.stdout

    claim = run_cli(repo, *claim_cmd("feat/two", "seat-b", layout / "wt-b", claims))
    assert claim.returncode != 0
    assert "HISTORY_CORRUPT" in claim.stdout
    assert ledger.read_bytes() == before
    assert not artifact_path(claims, "feat/two").exists()

    release = run_cli(repo, *release_cmd("feat/one", claims, seat="seat-a"))
    assert release.returncode != 0
    assert "HISTORY_CORRUPT" in release.stdout
    assert ledger.read_bytes() == before
    assert artifact_path(claims, "feat/one").exists()


def test_status_flags_artifact_whose_filename_and_branch_disagree(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-a", layout / "wt-a", claims))
    assert proc.returncode == 0, proc.stderr

    forged = claims / "mnemoseed-local" / "feat%2Ftwo.json"
    forged.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "repo": "mnemoseed-local",
                "branch": "feat/other",
                "seat": "seat-x",
                "worktree": str(layout / "wt-x"),
                "base_head": "0" * 40,
                "claimed_at": "2026-01-01T00:00:00+00:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    payload = json.loads(run_cli(repo, *status_cmd(claims)).stdout)
    flagged = [entry for entry in payload["claims"] if entry["file"].endswith("feat%2Ftwo.json")]
    assert len(flagged) == 1
    assert flagged[0]["corrupt"]

    release = run_cli(repo, *release_cmd("feat/two", claims, main_reason="inspect the forged file"))
    assert release.returncode != 0
    assert forged.exists()

    payload = json.loads(run_cli(repo, *status_cmd(claims, branch="feat/two")).stdout)
    assert len(payload["claims"]) == 1
    assert payload["claims"][0]["corrupt"]


def test_status_reports_holder_seat_worktree_and_base_head(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"
    worktree = layout / "wt-a"
    base_head = git(repo, "rev-parse", "HEAD")

    proc = run_cli(repo, *claim_cmd("feat/one", "seat-a", worktree, claims))
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(run_cli(repo, *status_cmd(claims, branch="feat/one")).stdout)
    assert len(payload["claims"]) == 1
    entry = payload["claims"][0]
    assert entry["branch"] == "feat/one"
    assert entry["seat"] == "seat-a"
    assert entry["worktree"] == str(worktree.resolve())
    assert entry["base_head"] == base_head
    assert entry["corrupt"] is None

    payload = json.loads(run_cli(repo, *status_cmd(claims, branch="feat/absent")).stdout)
    assert payload["claims"] == []


def test_branch_claim_filename_encoding_is_injective_and_uppercase(tmp_path: Path) -> None:
    branches = [
        "feat/228-writer-claim",
        "batch/221-windows-autostart",
        "Docs/Guide",
        "docs/guide",
        "a%b",
        "Feature/X",
        "feature.x_y-z",
        "0",
        "功能/分支",
    ]

    assert branch_claim_filename("feat/x") == "feat%2Fx"
    assert branch_claim_filename("a%b") == "a%25b"
    assert branch_claim_filename("Docs/Guide") == "%44ocs%2F%47uide"
    assert branch_claim_filename("Docs/Guide") != branch_claim_filename("docs/guide")

    filenames = {branch: branch_claim_filename(branch) for branch in branches}
    assert len(filenames) == len(branches)

    for branch, filename in filenames.items():
        assert_uppercase_hex(filename)
        assert branch_from_claim_filename(filename) == branch
        assert branch_claim_filename(branch_from_claim_filename(filename)) == filename

    with pytest.raises(ValueError):
        branch_from_claim_filename("bad%GG")
    with pytest.raises(ValueError):
        branch_from_claim_filename("bad%2")
    with pytest.raises(ValueError):
        branch_from_claim_filename("bad%2G")


def test_claim_artifact_schema_pins_fields_without_expiry(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"
    worktree = layout / "wt-schema"
    git(repo, "branch", "feat/schema")
    base_head = git(repo, "rev-parse", "refs/heads/feat/schema")

    proc = run_cli(repo, *claim_cmd("feat/schema", "seat-a", worktree, claims, issue=228))
    assert proc.returncode == 0, proc.stderr
    data = json.loads(artifact_path(claims, "feat/schema").read_text(encoding="utf-8"))

    assert data["schema"] == SCHEMA
    assert data["repo"] == "mnemoseed-local"
    assert data["branch"] == "feat/schema"
    assert data["seat"] == "seat-a"
    assert data["worktree"] == str(worktree.resolve())
    assert data["base_head"] == base_head
    assert data["issue"] == 228
    claimed = datetime.fromisoformat(data["claimed_at"])
    assert claimed.utcoffset() is not None
    assert claimed.utcoffset().total_seconds() == 0
    assert not any("expir" in key.lower() or key.lower() == "ttl" for key in collect_keys(data))

    events = read_events(ledger_path(claims))
    assert events[0]["seq"] == 1
    assert events[0]["type"] == "claim"
    for key, value in data.items():
        assert events[0][key] == value

    proc = run_cli(repo, *claim_cmd("feat/fresh", "seat-b", layout / "wt-fresh", claims))
    assert proc.returncode == 0, proc.stderr
    fresh = json.loads(artifact_path(claims, "feat/fresh").read_text(encoding="utf-8"))
    assert "issue" not in fresh
    assert fresh["base_head"] == git(repo, "rev-parse", "HEAD")
    assert read_events(ledger_path(claims))[1]["seq"] == 2


def test_default_claims_dir_resolves_to_main_checkout_sibling_org(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    org_claims = layout / "org" / "claims"
    worktree = layout / "wt-one"
    git(repo, "worktree", "add", str(worktree), "-b", "feat/oracle-nine")

    from_main = run_cli(repo, *status_cmd())
    assert from_main.returncode == 0, from_main.stderr
    main_payload = json.loads(from_main.stdout)
    assert main_payload["claims_dir"] == str(org_claims.resolve())

    claimed = run_cli(worktree, *claim_cmd("feat/oracle-nine", "seat-a", worktree, claims=None))
    assert claimed.returncode == 0, claimed.stderr
    assert (org_claims / "mnemoseed-local" / "feat%2Foracle-nine.json").exists()

    from_worktree = run_cli(worktree, *status_cmd())
    assert from_worktree.returncode == 0, from_worktree.stderr
    worktree_payload = json.loads(from_worktree.stdout)

    assert worktree_payload["claims_dir"] == main_payload["claims_dir"]
    assert worktree_payload["claims_dir"] == str(org_claims.resolve())
    resolved = Path(worktree_payload["claims_dir"])
    assert not resolved.is_relative_to(worktree)
    assert not (worktree / "org").exists()
    assert not (repo / "org").exists()
    assert not (repo / "claims").exists()


def test_claims_dir_env_override_and_explicit_precedence(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    env_dir = tmp_path / "env-claims"
    explicit_dir = tmp_path / "cli-claims"

    proc = run_cli(
        repo,
        *claim_cmd("feat/one", "seat-a", layout / "wt-a", claims=None),
        env=cli_env(**{ENV_OVERRIDE: str(env_dir)}),
    )
    assert proc.returncode == 0, proc.stderr
    assert artifact_path(env_dir, "feat/one").exists()
    assert not (layout / "org").exists()

    proc = run_cli(
        repo,
        *claim_cmd("feat/two", "seat-b", layout / "wt-b", explicit_dir),
        env=cli_env(**{ENV_OVERRIDE: str(env_dir)}),
    )
    assert proc.returncode == 0, proc.stderr
    assert artifact_path(explicit_dir, "feat/two").exists()
    assert not artifact_path(env_dir, "feat/two").exists()


def test_agents_md_carries_the_claim_workflow_bullet() -> None:
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    bullets = [block for block in text.split("\n- ") if "writer_claim.py" in block]
    assert len(bullets) == 1
    bullet = bullets[0]
    assert "before `git worktree add`" in bullet
    assert "writer_claim.py claim" in bullet
    assert "writer_claim.py release" in bullet
    assert 'release --main --reason "<stated reason>"' in bullet


def test_empty_identity_fields_are_usage_refused(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    repo = init_repo(layout)
    claims = tmp_path / "claims"
    worktree = str(layout / "wt-a")

    probes = [
        [
            "claim",
            "--repo",
            "",
            "--branch",
            "feat/one",
            "--seat",
            "seat-a",
            "--worktree",
            worktree,
            "--claims-dir",
            str(claims),
        ],
        [
            "claim",
            "--repo",
            "mnemoseed-local",
            "--branch",
            "",
            "--seat",
            "seat-a",
            "--worktree",
            worktree,
            "--claims-dir",
            str(claims),
        ],
        [
            "claim",
            "--repo",
            "mnemoseed-local",
            "--branch",
            "feat/one",
            "--seat",
            "",
            "--worktree",
            worktree,
            "--claims-dir",
            str(claims),
        ],
        ["release", "--repo", "", "--branch", "feat/one", "--seat", "seat-a", "--claims-dir", str(claims)],
        ["status", "--repo", "", "--claims-dir", str(claims)],
    ]
    for args in probes:
        proc = run_cli(repo, *args)
        assert proc.returncode == 2, (args, proc.stdout, proc.stderr)
        assert "non-empty" in proc.stderr

    assert not claims.exists()


def test_failed_artifact_write_unlinks_the_exclusive_create(tmp_path: Path, monkeypatch) -> None:
    import scripts.writer_claim as writer_claim

    target = tmp_path / "feat%2Fone.json"

    def broken_write(fd: int, data: Any) -> int:
        raise OSError("simulated write failure")

    monkeypatch.setattr(writer_claim.os, "write", broken_write)
    with pytest.raises(OSError):
        writer_claim.create_exclusive(target, "{}\n")
    assert not target.exists()
