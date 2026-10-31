"""Explicit project-root -> profile binding: the version-1 local registry,
its conflict semantics, path identity, and the ``project bind | run`` CLI.

Isolation: every test writes into ``tmp_path`` (a temp app home), touches no
reserved port, and never reads the installed runtime's home.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemoseed_local.cli import main
from mnemoseed_local.hosts.common import project_binding as pb
from mnemoseed_local.rest_client import DaemonUnavailableError

WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows roots compare case-insensitively")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX roots compare case-sensitively")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MNEMOSEED_LOCAL_DEBUG", "MNEMOSEED_LOCAL_PROFILE_ID", "MNEMOSEED_LOCAL_BASEURL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    return home


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True)
    return path


def _make_dir_link(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _registry_client(
    monkeypatch: pytest.MonkeyPatch,
    profile_ids: list[str],
    *,
    archived: list[str] | None = None,
    fail: bool = False,
) -> None:
    class _FakeRegistryClient:
        def get(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
            if fail:
                raise DaemonUnavailableError("cannot reach http://localhost:7788")
            if path == "/api/v1/profiles":
                rows: list[dict[str, object]] = [{"profile_id": pid} for pid in profile_ids]
                rows += [{"profile_id": pid, "archived": True} for pid in archived or []]
                return {"profiles": rows}
            raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(
        "mnemoseed_local.rest_client.resolve_client", lambda args, **kwargs: _FakeRegistryClient()
    )


# ---------------------------------------------------------------- registry file


def test_bind_creates_version1_registry_file(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    result = pb.bind(home, root, "alpha")
    assert result.action == "created"
    path = pb.bindings_path(home)
    assert path == home / "project-bindings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "version": 1,
        "bindings": [{"project_root": str(root), "profile_id": "alpha"}],
    }


def test_bind_into_a_fresh_home_creates_it(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    assert pb.bind(home, root, "alpha").action == "created"
    assert pb.bindings_path(home).is_file()
    leftovers = [p.name for p in home.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_rebind_same_root_and_profile_is_a_byte_identical_noop(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(home, root, "alpha")
    path = pb.bindings_path(home)
    before = path.read_bytes()
    result = pb.bind(home, root, "alpha")
    assert result.action == "unchanged"
    assert path.read_bytes() == before


def test_rebind_same_root_different_profile_without_replace_is_rejected(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(home, root, "alpha")
    path = pb.bindings_path(home)
    before = path.read_bytes()
    with pytest.raises(pb.BindingError) as excinfo:
        pb.bind(home, root, "beta")
    assert "--replace" in str(excinfo.value)
    assert path.read_bytes() == before


def test_rebind_with_replace_writes_new_value_and_backs_up_previous(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(home, root, "alpha")
    path = pb.bindings_path(home)
    before = json.loads(path.read_text(encoding="utf-8"))
    result = pb.bind(home, root, "beta", replace=True)
    assert result.action == "replaced"
    assert result.backup_path is not None
    assert json.loads(result.backup_path.read_text(encoding="utf-8")) == before
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["bindings"] == [{"project_root": str(root), "profile_id": "beta"}]


# ---------------------------------------------------------------- refusal modes


def test_malformed_registry_bytes_are_reported_and_left_unchanged(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    path = pb.bindings_path(home)
    path.write_bytes(b"{ not json ")
    with pytest.raises(pb.BindingError):
        pb.load_bindings(path)
    with pytest.raises(pb.BindingError):
        pb.bind(home, root, "alpha")
    assert path.read_bytes() == b"{ not json "


def test_unknown_registry_version_is_rejected_without_repair(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    path = pb.bindings_path(home)
    path.write_text(json.dumps({"version": 2, "bindings": []}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(pb.BindingError) as excinfo:
        pb.bind(home, root, "alpha")
    assert "version" in str(excinfo.value)
    assert path.read_bytes() == before


def test_duplicate_normalized_roots_are_rejected(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    path = pb.bindings_path(home)
    entries = [
        {"project_root": str(root), "profile_id": "alpha"},
        {"project_root": str(root), "profile_id": "beta"},
    ]
    path.write_text(json.dumps({"version": 1, "bindings": entries}), encoding="utf-8")
    with pytest.raises(pb.BindingError):
        pb.load_bindings(path)


@WIN_ONLY
def test_duplicate_case_variant_roots_are_rejected_on_windows(tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    variant = str(root).upper()
    if variant == str(root):
        pytest.skip("test root has no case-variant spelling")
    path = tmp_path / "home" / "project-bindings.json"
    entries = [
        {"project_root": str(root), "profile_id": "alpha"},
        {"project_root": variant, "profile_id": "beta"},
    ]
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 1, "bindings": entries}), encoding="utf-8")
    with pytest.raises(pb.BindingError):
        pb.load_bindings(path)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1},
        {"version": 1.0, "bindings": []},
        {"version": "1", "bindings": []},
        {"version": 1, "bindings": {}},
        {"version": 1, "bindings": ["x"]},
        {"version": 1, "bindings": [{"project_root": "C:/x"}]},
        {"version": 1, "bindings": [{"project_root": "C:/x", "profile_id": "a", "extra": 1}]},
        {"version": 1, "bindings": [{"project_root": "relative/path", "profile_id": "a"}]},
        {"version": 1, "bindings": [{"project_root": "C:/x", "profile_id": ""}]},
        {"version": 1, "bindings": [{"project_root": "C:/x", "profile_id": "   "}]},
    ],
)
def test_malformed_registry_shapes_are_rejected(home: Path, payload: object) -> None:
    path = pb.bindings_path(home)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pb.BindingError):
        pb.load_bindings(path)
    assert path.read_bytes() == json.dumps(payload).encode("utf-8")


def test_unregistered_profile_is_rejected(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    with pytest.raises(pb.BindingError) as excinfo:
        pb.bind(home, root, "ghost", known_profile_ids={"alpha"})
    assert "ghost" in str(excinfo.value)
    assert "profile create" in str(excinfo.value)
    assert not pb.bindings_path(home).exists()


def test_empty_profile_id_is_rejected(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    with pytest.raises(pb.BindingError):
        pb.bind(home, root, "   ")
    assert not pb.bindings_path(home).exists()


def test_loading_never_rewrites_a_valid_registry(home: Path, tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(home, root, "alpha")
    path = pb.bindings_path(home)
    before = path.read_bytes()
    pb.load_bindings(path)
    assert path.read_bytes() == before


# ---------------------------------------------------------------- path identity


def test_canonical_root_requires_an_existing_absolute_directory(tmp_path: Path) -> None:
    with pytest.raises(pb.BindingError):
        pb.canonical_root("relative/dir")
    with pytest.raises(pb.BindingError):
        pb.canonical_root(tmp_path / "missing")
    a_file = _mkdir(tmp_path / "d") / "f.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(pb.BindingError):
        pb.canonical_root(a_file)


def test_canonical_root_rejects_symlink_or_junction_root(tmp_path: Path) -> None:
    target = _mkdir(tmp_path / "real")
    link = tmp_path / "alias"
    _make_dir_link(target, link)
    with pytest.raises(pb.BindingError):
        pb.canonical_root(link)


@WIN_ONLY
def test_windows_roots_match_case_insensitively(tmp_path: Path) -> None:
    real = pb.canonical_root(_mkdir(tmp_path / "Proj"))
    home = tmp_path / "home"
    pb.bind(home, real, "alpha")
    alternate = pb.canonical_root(real.with_name(real.name.lower()))
    bindings = pb.load_bindings(pb.bindings_path(home))
    assert pb.resolve_profile(alternate, bindings) == "alpha"


@POSIX_ONLY
def test_posix_roots_match_case_sensitively(tmp_path: Path) -> None:
    lower = pb.canonical_root(_mkdir(tmp_path / "proj"))
    upper = pb.canonical_root(_mkdir(tmp_path / "PROJ"))
    home = tmp_path / "home"
    pb.bind(home, lower, "alpha")
    bindings = pb.load_bindings(pb.bindings_path(home))
    assert pb.resolve_profile(lower, bindings) == "alpha"
    assert pb.resolve_profile(upper, bindings) is None


@WIN_ONLY
def test_registry_stored_in_upper_case_still_resolves_by_normcase(home: Path, tmp_path: Path) -> None:
    # The registry keeps the stored spelling untouched; only the comparison
    # key folds case, so a differently-cased stored root must still resolve.
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    stored = str(root).upper()
    if stored == str(root):
        pytest.skip("test root has no case-variant spelling")
    path = pb.bindings_path(home)
    path.write_text(
        json.dumps({"version": 1, "bindings": [{"project_root": stored, "profile_id": "alpha"}]}),
        encoding="utf-8",
    )
    bindings = pb.load_bindings(path)
    assert pb.resolve_profile(root, bindings) == "alpha"


@WIN_ONLY
def test_bind_case_variant_spelling_is_a_same_root_conflict(home: Path, tmp_path: Path) -> None:
    # The stored spelling and the requested spelling differ only in case; the
    # folded comparison key must recognize them as one root.
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    stored = str(root).upper()
    if stored == str(root):
        pytest.skip("test root has no case-variant spelling")
    path = pb.bindings_path(home)
    written = {"version": 1, "bindings": [{"project_root": stored, "profile_id": "alpha"}]}
    path.write_text(json.dumps(written), encoding="utf-8")
    with pytest.raises(pb.BindingError) as excinfo:
        pb.bind(home, root, "beta")
    assert "already bound" in str(excinfo.value)
    assert "--replace" in str(excinfo.value)
    assert "alpha" in str(excinfo.value)
    assert path.read_bytes() == json.dumps(written).encode("utf-8")
    result = pb.bind(home, root, "beta", replace=True)
    assert result.action == "replaced"
    bindings = pb.load_bindings(path)
    assert [b.project_root for b in bindings] == [str(root)]
    assert [b.profile_id for b in bindings] == ["beta"]


def test_resolve_is_exact_match_without_parent_inheritance(tmp_path: Path) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    child = _mkdir(root / "sub")
    parent = tmp_path
    home = tmp_path / "home"
    pb.bind(home, root, "alpha")
    bindings = pb.load_bindings(pb.bindings_path(home))
    assert pb.resolve_profile(root, bindings) == "alpha"
    assert pb.resolve_profile(child, bindings) is None
    assert pb.resolve_profile(pb.canonical_root(parent), bindings) is None


def test_same_folder_name_in_a_different_parent_never_matches(tmp_path: Path) -> None:
    home = tmp_path / "home"
    bound = pb.canonical_root(_mkdir(tmp_path / "one" / "proj"))
    namesake = pb.canonical_root(_mkdir(tmp_path / "two" / "proj"))
    pb.bind(home, bound, "alpha")
    bindings = pb.load_bindings(pb.bindings_path(home))
    assert pb.resolve_profile(bound, bindings) == "alpha"
    assert pb.resolve_profile(namesake, bindings) is None


def test_moved_root_needs_explicit_rebind_and_old_entry_stays(home: Path, tmp_path: Path) -> None:
    old = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(home, old, "alpha")
    moved = tmp_path / "moved"
    old.rename(moved)
    bindings = pb.load_bindings(pb.bindings_path(home))
    assert pb.resolve_profile(pb.canonical_root(moved), bindings) is None
    result = pb.bind(home, moved, "alpha")
    assert result.action == "created"
    bindings = pb.load_bindings(pb.bindings_path(home))
    assert [b.project_root for b in bindings] == [str(old), str(pb.canonical_root(moved))]
    assert [b.profile_id for b in bindings] == ["alpha", "alpha"]


def test_worktree_roots_bind_independently_and_may_share_a_profile(home: Path, tmp_path: Path) -> None:
    main_root = pb.canonical_root(_mkdir(tmp_path / "main"))
    worktree = pb.canonical_root(_mkdir(tmp_path / "wt"))
    pb.bind(home, main_root, "alpha")
    result = pb.bind(home, worktree, "alpha")
    assert result.action == "created"
    assert result.shared_roots == (main_root,)
    bindings = pb.load_bindings(pb.bindings_path(home))
    assert pb.roots_for_profile(bindings, "alpha") == [main_root, worktree]
    assert pb.resolve_profile(worktree, bindings) == "alpha"


def test_default_profile_data_is_never_migrated(home: Path, tmp_path: Path) -> None:
    config_bytes = b"[dream]\nauto_trigger = false\n"
    (home / "config.toml").write_bytes(config_bytes)
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    other = pb.canonical_root(_mkdir(tmp_path / "other"))
    pb.bind(home, root, "alpha")
    assert (home / "config.toml").read_bytes() == config_bytes
    bindings = pb.load_bindings(pb.bindings_path(home))
    assert pb.resolve_profile(other, bindings) is None


# ---------------------------------------------------------------- launch env


def test_launch_env_conflict_is_a_documented_error() -> None:
    pb.check_launch_profile_env("alpha", {})
    pb.check_launch_profile_env("alpha", {pb.PROFILE_ENV_VAR: "alpha"})
    pb.check_launch_profile_env("alpha", {pb.PROFILE_ENV_VAR: "   "})
    with pytest.raises(pb.BindingError) as excinfo:
        pb.check_launch_profile_env("alpha", {pb.PROFILE_ENV_VAR: "beta"})
    assert pb.PROFILE_ENV_VAR in str(excinfo.value)


# ---------------------------------------------------------------- CLI surface


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".mnemoseed-local"
    home.mkdir()
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", home / "config.toml")
    return home


def test_cli_bind_creates_the_registry_and_reports_the_binding(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_client(monkeypatch, ["alpha"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    data = json.loads((cli_home / "project-bindings.json").read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["bindings"] == [{"project_root": str(pb.canonical_root(root)), "profile_id": "alpha"}]


def test_cli_bind_rejects_an_unregistered_profile(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_client(monkeypatch, ["alpha"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "ghost"]) == 1
    assert "error:" in capsys.readouterr().err
    assert not (cli_home / "project-bindings.json").exists()


def test_cli_bind_when_the_daemon_cannot_be_reached(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_client(monkeypatch, [], fail=True)
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "alpha"]) == 1
    assert "error:" in capsys.readouterr().err
    assert not (cli_home / "project-bindings.json").exists()


def test_cli_bind_conflict_without_replace_and_explicit_replace(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_client(monkeypatch, ["alpha", "beta"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "alpha"]) == 0
    assert main(["project", "bind", "--root", str(root), "--profile", "beta"]) == 1
    assert "--replace" in capsys.readouterr().err
    assert main(["project", "bind", "--root", str(root), "--profile", "beta", "--replace"]) == 0
    out = capsys.readouterr().out
    assert "backed up" in out
    data = json.loads((cli_home / "project-bindings.json").read_text(encoding="utf-8"))
    assert data["bindings"] == [{"project_root": str(pb.canonical_root(root)), "profile_id": "beta"}]


def test_cli_bind_shows_the_shared_profile_fact(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_client(monkeypatch, ["alpha"])
    first = _mkdir(tmp_path / "main")
    second = _mkdir(tmp_path / "wt")
    assert main(["project", "bind", "--root", str(first), "--profile", "alpha"]) == 0
    assert main(["project", "bind", "--root", str(second), "--profile", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "shared" in out
    assert str(pb.canonical_root(first)) in out


def test_cli_bind_refuses_a_non_absolute_or_missing_root(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_client(monkeypatch, ["alpha"])
    assert main(["project", "bind", "--root", "relative/dir", "--profile", "alpha"]) == 1
    assert "absolute" in capsys.readouterr().err
    assert main(["project", "bind", "--root", str(tmp_path / "missing"), "--profile", "alpha"]) == 1
    assert not (cli_home / "project-bindings.json").exists()


def test_cli_bind_refuses_an_archived_profile(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_client(monkeypatch, ["alpha"], archived=["retired"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "retired"]) == 1
    err = capsys.readouterr().err
    assert "retired" in err
    assert "not registered" in err
    assert not (cli_home / "project-bindings.json").exists()


def test_cli_bind_to_default_prints_the_shared_namespace_note(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_client(monkeypatch, ["alpha"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "default"]) == 0
    out = capsys.readouterr().out
    assert "default" in out
    assert "not isolated" in out


def test_cli_run_launches_the_host_argv_with_the_bound_profile(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _registry_client(monkeypatch, ["alpha"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "alpha"]) == 0
    capsys.readouterr()  # the bind report is not part of the run's output
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["argv"] = argv
        captured["cwd"] = os.getcwd()
        captured["env"] = kwargs.get("env")
        captured["shell"] = kwargs.get("shell")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    code = main(["project", "run", "--root", str(root), "--", "somehost", "--flag", "v"])
    assert code == 0
    assert captured["argv"] == ["somehost", "--flag", "v"]
    assert isinstance(captured["env"], dict)
    env: dict[str, str] = captured["env"]
    assert env["MNEMOSEED_LOCAL_PROFILE_ID"] == "alpha"
    assert pb.root_key(Path(str(captured["cwd"]))) == pb.root_key(pb.canonical_root(root))
    assert captured["shell"] is not True
    assert capsys.readouterr().out == ""


def test_cli_run_rejects_a_conflicting_profile_env(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _registry_client(monkeypatch, ["alpha", "beta"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "alpha"]) == 0
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "beta")
    launches: list[object] = []

    def refusing_run(*argv: object, **kwargs: object) -> SimpleNamespace:
        launches.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", refusing_run)
    assert main(["project", "run", "--root", str(root), "--", "somehost"]) == 1
    assert "MNEMOSEED_LOCAL_PROFILE_ID" in capsys.readouterr().err
    assert launches == []


def test_cli_run_accepts_a_matching_profile_env(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _registry_client(monkeypatch, ["alpha"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "alpha"]) == 0
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "alpha")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert main(["project", "run", "--root", str(root), "--", "somehost"]) == 0


def test_cli_run_refuses_an_unbound_root(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _registry_client(monkeypatch, ["alpha"])
    other = _mkdir(tmp_path / "other")
    bound = _mkdir(tmp_path / "bound")
    assert main(["project", "bind", "--root", str(bound), "--profile", "alpha"]) == 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert main(["project", "run", "--root", str(other), "--", "somehost"]) == 1
    assert "project bind" in capsys.readouterr().err


def test_cli_run_refuses_a_moved_root_until_explicitly_rebound(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _registry_client(monkeypatch, ["alpha"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "alpha"]) == 0
    moved = tmp_path / "moved"
    root.rename(moved)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert main(["project", "run", "--root", str(moved), "--", "somehost"]) == 1
    assert "project bind" in capsys.readouterr().err


def test_cli_run_reports_malformed_registry_bytes_and_leaves_them(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _mkdir(tmp_path / "proj")
    path = cli_home / "project-bindings.json"
    path.write_bytes(b"{ broken ")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert main(["project", "run", "--root", str(root), "--", "somehost"]) == 1
    assert "error:" in capsys.readouterr().err
    assert path.read_bytes() == b"{ broken "


def test_cli_run_without_a_host_command_is_a_usage_error(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "run", "--root", str(root), "--"]) == 2
    assert "host command" in capsys.readouterr().err


def test_cli_run_launch_failure_is_reported(
    cli_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _registry_client(monkeypatch, ["alpha"])
    root = _mkdir(tmp_path / "proj")
    assert main(["project", "bind", "--root", str(root), "--profile", "alpha"]) == 0

    def missing_executable(argv: list[str], **kwargs: object) -> SimpleNamespace:
        raise FileNotFoundError("no such executable")

    monkeypatch.setattr(subprocess, "run", missing_executable)
    assert main(["project", "run", "--root", str(root), "--", "definitely-not-a-host"]) == 1
    assert "error:" in capsys.readouterr().err
