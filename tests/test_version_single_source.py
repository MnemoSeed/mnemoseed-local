"""Version single-source invariants.

The authoring source of truth for the project version is ``pyproject.toml``.
The package ``__version__`` and both install orchestrators must agree with it,
so a version bump is one edit plus the derived surfaces — never a hunt for
stale literals. The orchestrators install the ``codex`` extra, so their pin
names that extra at the same exact version.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_dunder_version_matches_pyproject() -> None:
    assert __version__ == _pyproject_version()


def test_dunder_version_matches_installed_metadata() -> None:
    assert __version__ == version("mnemoseed-local")


def test_installers_pin_the_exact_project_version() -> None:
    spec = f"mnemoseed-local[codex]=={_pyproject_version()}"
    # The trailing guard keeps the match exact: a longer version that merely
    # starts with the project version must not satisfy the pin.
    pattern = re.compile(rf"{re.escape(spec)}(?![\w.-])")
    for name in ("install.ps1", "install.sh"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert pattern.search(text), f"{name} must install the pinned {spec}"


PIN_VARIABLES = {"install.sh": "$CLI_PIN", "install.ps1": "$CliPin"}
UV_TOOL_INVOCATION = re.compile(r"^\s*(?:if\s+!\s+)?&?\s*uv\s+tool\s+(install|upgrade)\s+(?P<args>.*)$")
PYTHON_PIN = re.compile(r"--python[\s=]+3\.12\b")


def _code_lines(name: str) -> list[str]:
    """Installer lines with whole-line comments (and PS help block) dropped."""
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    if name.endswith(".ps1"):
        text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _branch_arguments(name: str) -> dict[str, str]:
    """Map install/upgrade to the argument text of the executed uv tool call."""
    branches: dict[str, str] = {}
    for line in _code_lines(name):
        match = UV_TOOL_INVOCATION.match(line)
        if match is not None:
            branches[match.group(1)] = match.group("args")
    assert set(branches) == {"install", "upgrade"}, (
        f"{name}: expected one real install and one real upgrade invocation, found {sorted(branches)}"
    )
    return branches


def _planned_arguments(name: str) -> dict[str, list[str]]:
    """Map install/upgrade to the dry-run plan lines announcing that call."""
    plans: dict[str, list[str]] = {"install": [], "upgrade": []}
    for line in _code_lines(name):
        if UV_TOOL_INVOCATION.match(line) is not None:
            continue
        if "plan" not in line.lower():
            continue
        for verb in plans:
            if re.search(rf"uv tool {verb}\b", line):
                plans[verb].append(line)
    return plans


@pytest.mark.parametrize("name", sorted(PIN_VARIABLES))
def test_actual_uv_tool_commands_request_python_312(name: str) -> None:
    for verb, args in _branch_arguments(name).items():
        assert PYTHON_PIN.search(args), (
            f"{name}: `uv tool {verb}` must pass an explicit --python 3.12"
            f" before the pinned spec, got {args!r}"
        )
        assert PIN_VARIABLES[name] in args, (
            f"{name}: `uv tool {verb}` must still install the pinned spec, got {args!r}"
        )


@pytest.mark.parametrize("name", sorted(PIN_VARIABLES))
def test_dry_run_plan_announces_the_python_request(name: str) -> None:
    for verb, lines in _planned_arguments(name).items():
        assert lines, f"{name}: dry-run plan must announce `uv tool {verb}`"
        assert any(PYTHON_PIN.search(line) for line in lines), (
            f"{name}: dry-run plan must show the --python 3.12 request for `uv tool {verb}`; got {lines!r}"
        )
        assert any(PIN_VARIABLES[name] in line for line in lines), (
            f"{name}: dry-run plan must keep the pinned spec for `uv tool {verb}`; got {lines!r}"
        )


# --- release-route binding: helpers, release manifest, negative controls -----

VERIFIER_SCRIPT = REPO_ROOT / "scripts" / "verify_release_install.py"


def _verifier_module() -> Any:
    spec = importlib.util.spec_from_file_location("release_route_verifier", VERIFIER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release = _verifier_module()

HELPER_URI_DECLARATION = re.compile(r"\$HelperBaseUri\s*=\s*'([^']+)'")
IMMUTABLE_HELPER_URI = re.compile(
    r"https://raw\.githubusercontent\.com/MnemoSeed/mnemoseed-local/([0-9a-f]{40})/scripts/"
)
PROJECT_HELPER_FETCH = re.compile(r"raw\.githubusercontent\.com/MnemoSeed/mnemoseed-local/\S*/scripts/")
FIXTURE_VERSION = "0.3.0"
FIXTURE_PIN = f"mnemoseed-local[codex]=={FIXTURE_VERSION}"
FIXTURE_HELPERS = (
    "scripts/windows-install-helpers.ps1",
    "scripts/windows-logon.ps1",
    "scripts/windows-logon-task.ps1",
)
WHEEL_BYTES = b"release-route fixture wheel bytes"
SDIST_BYTES = b"release-route fixture sdist bytes"


def test_install_ps1_fetches_helpers_from_one_immutable_revision() -> None:
    uris = [
        match.group(1)
        for line in _code_lines("install.ps1")
        if (match := HELPER_URI_DECLARATION.search(line)) is not None
    ]
    assert len(uris) == 1, f"expected exactly one helper base URI declaration, got {uris}"
    assert IMMUTABLE_HELPER_URI.search(uris[0]) is not None, (
        f"install.ps1 must fetch helpers from one pinned full-40-hex source revision, got {uris[0]!r}"
    )


def test_install_sh_fetches_no_project_helper_sources() -> None:
    for line in _code_lines("install.sh"):
        assert PROJECT_HELPER_FETCH.search(line) is None, (
            f"install.sh must not fetch project helper sources: {line}"
        )


@dataclass
class ReleaseRoute:
    root: Path
    source_commit: str
    helper_revision: str
    wheel: Path
    sdist: Path


def _git(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=release-fixture",
            "-c",
            "user.email=release-fixture@example.invalid",
            *argv,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.strip()


def _fixture_ps1(helper_revision: str) -> str:
    return (
        f"$CliPin = '{FIXTURE_PIN}'\n"
        "$HelperBaseUri = "
        f"'https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/{helper_revision}/scripts/'\n"
    )


def _write_fixture_repo(root: Path, *, helper_drift: bool) -> tuple[str, str]:
    """Two-commit checkout: commit one holds the helpers, commit two re-pins the
    installer helper URI at commit one, because an installer cannot embed its
    own commit revision. helper_drift adds a third commit that moves a helper
    after the pin, the state verification must refuse."""
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "mnemoseed-local"\nversion = "{FIXTURE_VERSION}"\n', encoding="utf-8"
    )
    (root / "install.sh").write_text(f"CLI_PIN='{FIXTURE_PIN}'\n", encoding="utf-8")
    (root / "install.ps1").write_text(_fixture_ps1("0" * 40), encoding="utf-8")
    for helper in FIXTURE_HELPERS:
        (root / helper).write_text("# fixture helper one\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "helpers", cwd=root)
    helper_revision = _git("rev-parse", "HEAD", cwd=root)
    (root / "install.ps1").write_text(_fixture_ps1(helper_revision), encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "pin helpers", cwd=root)
    if helper_drift:
        (root / FIXTURE_HELPERS[0]).write_text("# fixture helper two\n", encoding="utf-8")
        _git("add", "-A", cwd=root)
        _git("commit", "-q", "-m", "move a helper", cwd=root)
    return helper_revision, _git("rev-parse", "HEAD", cwd=root)


@pytest.fixture
def release_route(tmp_path: Path) -> ReleaseRoute:
    root = tmp_path / "checkout"
    root.mkdir()
    helper_revision, source_commit = _write_fixture_repo(root, helper_drift=False)
    wheel = tmp_path / f"mnemoseed_local-{FIXTURE_VERSION}-py3-none-any.whl"
    sdist = tmp_path / f"mnemoseed_local-{FIXTURE_VERSION}.tar.gz"
    wheel.write_bytes(WHEEL_BYTES)
    sdist.write_bytes(SDIST_BYTES)
    return ReleaseRoute(
        root=root,
        source_commit=source_commit,
        helper_revision=helper_revision,
        wheel=wheel,
        sdist=sdist,
    )


def _generate(release_route: ReleaseRoute) -> dict[str, Any]:
    return release.generate_release_manifest(
        release_route.root,
        wheel=release_route.wheel,
        sdist=release_route.sdist,
        source_commit=release_route.source_commit,
    )


def _verify(
    tmp_path: Path,
    release_route: ReleaseRoute,
    manifest: dict[str, Any],
    *,
    repo_root: Path | None = None,
    wheel: Path | None = None,
    sdist: Path | None = None,
    source_commit: str | None = None,
) -> None:
    verifier = release.Verifier(tmp_path / "report.json")
    release.verify_release_manifest(
        verifier,
        manifest,
        repo_root=repo_root or release_route.root,
        wheel=wheel or release_route.wheel,
        sdist=sdist or release_route.sdist,
        source_commit=source_commit or release_route.source_commit,
    )


def test_manifest_verification_accepts_the_coherent_release(
    tmp_path: Path, release_route: ReleaseRoute
) -> None:
    manifest = _generate(release_route)
    _verify(tmp_path, release_route, manifest)
    assert manifest["schema_version"] == 1
    assert manifest["package_version"] == FIXTURE_VERSION
    assert manifest["source_commit"] == release_route.source_commit
    assert manifest["installer_pin"] == FIXTURE_PIN
    assert manifest["helper_source_revision"]["fetch_revision"] == release_route.helper_revision
    assert set(manifest["helper_source_revision"]["helpers"]) == set(FIXTURE_HELPERS)
    assert manifest["routes"]["index"]["status"] == "BLOCKED_OPERATOR"
    assert release_route.source_commit in manifest["routes"]["source"]["requirement"]
    assert manifest["artifacts"]["wheel"]["sha256"] == hashlib.sha256(WHEEL_BYTES).hexdigest()
    assert manifest["artifacts"]["sdist"]["size_bytes"] == len(SDIST_BYTES)
    assert manifest["expected_cli_capabilities"]


@pytest.mark.parametrize("old_version", ["0.2.0", "0.0.1"])
def test_manifest_rejects_the_old_package_versions(
    tmp_path: Path, release_route: ReleaseRoute, old_version: str
) -> None:
    manifest = _generate(release_route)
    manifest["package_version"] = old_version
    with pytest.raises(release.Failure):
        _verify(tmp_path, release_route, manifest)


def test_manifest_rejects_a_wrong_source_revision(tmp_path: Path, release_route: ReleaseRoute) -> None:
    manifest = _generate(release_route)
    manifest["source_commit"] = "0" * 40
    with pytest.raises(release.Failure):
        _verify(tmp_path, release_route, manifest)


@pytest.mark.parametrize("malformed", ["main", "a" * 39, "a" * 41, "A" * 40])
def test_manifest_rejects_malformed_source_revisions(
    tmp_path: Path, release_route: ReleaseRoute, malformed: str
) -> None:
    manifest = _generate(release_route)
    manifest["source_commit"] = malformed
    with pytest.raises(release.Failure):
        _verify(tmp_path, release_route, manifest)


def test_manifest_rejects_drifted_installer_bytes(tmp_path: Path, release_route: ReleaseRoute) -> None:
    manifest = _generate(release_route)
    (release_route.root / "install.ps1").write_text(
        _fixture_ps1(release_route.helper_revision) + "# drift\n", encoding="utf-8"
    )
    with pytest.raises(release.Failure):
        _verify(tmp_path, release_route, manifest)


def test_manifest_rejects_an_old_installer_pin(tmp_path: Path, release_route: ReleaseRoute) -> None:
    manifest = _generate(release_route)
    manifest["installer_pin"] = "mnemoseed-local[codex]==0.2.0"
    with pytest.raises(release.Failure):
        _verify(tmp_path, release_route, manifest)


def test_manifest_generation_rejects_disagreeing_installer_pins(
    tmp_path: Path, release_route: ReleaseRoute
) -> None:
    drift = "CLI_PIN='mnemoseed-local[codex]==0.2.1'\n"
    (release_route.root / "install.sh").write_text(drift, encoding="utf-8")
    with pytest.raises(release.Failure):
        _generate(release_route)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize("field", ["sha256", "size_bytes"])
def test_manifest_rejects_a_mismatched_artifact(
    tmp_path: Path, release_route: ReleaseRoute, artifact: str, field: str
) -> None:
    manifest = _generate(release_route)
    manifest["artifacts"][artifact][field] = "0" * 64 if field == "sha256" else 1
    with pytest.raises(release.Failure):
        _verify(tmp_path, release_route, manifest)


def test_manifest_rejects_drifted_helper_bytes(tmp_path: Path, release_route: ReleaseRoute) -> None:
    manifest = _generate(release_route)
    (release_route.root / FIXTURE_HELPERS[-1]).write_text("# fixture helper moved\n", encoding="utf-8")
    with pytest.raises(release.Failure):
        _verify(tmp_path, release_route, manifest)


def test_manifest_rejects_a_helper_recorded_from_another_revision(
    tmp_path: Path, release_route: ReleaseRoute
) -> None:
    manifest = _generate(release_route)
    manifest["helper_source_revision"]["helpers"][FIXTURE_HELPERS[0]] = "d" * 64
    with pytest.raises(release.Failure):
        _verify(tmp_path, release_route, manifest)


@pytest.mark.parametrize("floating", ["main", "master"])
def test_manifest_rejects_a_floating_helper_fetch_revision(
    tmp_path: Path, release_route: ReleaseRoute, floating: str
) -> None:
    manifest = _generate(release_route)
    manifest["helper_source_revision"]["fetch_revision"] = floating
    with pytest.raises(release.Failure):
        _verify(tmp_path, release_route, manifest)


def test_manifest_rejects_helper_bytes_from_another_revision(tmp_path: Path) -> None:
    root = tmp_path / "drifted"
    root.mkdir()
    helper_revision, source_commit = _write_fixture_repo(root, helper_drift=True)
    wheel = tmp_path / "drifted.whl"
    sdist = tmp_path / "drifted.tar.gz"
    wheel.write_bytes(WHEEL_BYTES)
    sdist.write_bytes(SDIST_BYTES)
    manifest = release.generate_release_manifest(root, wheel=wheel, sdist=sdist, source_commit=source_commit)
    verifier = release.Verifier(tmp_path / "report.json")
    with pytest.raises(release.Failure):
        release.verify_release_manifest(
            verifier, manifest, repo_root=root, wheel=wheel, sdist=sdist, source_commit=source_commit
        )


def test_manifest_generation_rejects_a_floating_helper_uri(tmp_path: Path) -> None:
    root = tmp_path / "floating"
    root.mkdir()
    _write_fixture_repo(root, helper_drift=False)
    (root / "install.ps1").write_text(_fixture_ps1("main"), encoding="utf-8")
    wheel = tmp_path / "floating.whl"
    sdist = tmp_path / "floating.tar.gz"
    wheel.write_bytes(WHEEL_BYTES)
    sdist.write_bytes(SDIST_BYTES)
    with pytest.raises(release.Failure):
        release.generate_release_manifest(
            root, wheel=wheel, sdist=sdist, source_commit=_git("rev-parse", "HEAD", cwd=root)
        )


def test_the_index_route_stays_blocked_without_publication(
    tmp_path: Path, release_route: ReleaseRoute
) -> None:
    manifest = _generate(release_route)
    with pytest.raises(release.Failure) as excinfo:
        release.select_install_route(manifest, "index")
    assert "BLOCKED_OPERATOR" in str(excinfo.value)


def test_the_source_route_requires_the_frozen_revision(tmp_path: Path, release_route: ReleaseRoute) -> None:
    manifest = _generate(release_route)
    assert release.select_install_route(manifest, "source") == "source"
    manifest["routes"]["source"]["requirement"] = (
        "mnemoseed-local[codex] @ git+https://github.com/MnemoSeed/mnemoseed-local.git@main"
    )
    with pytest.raises(release.Failure):
        release.select_install_route(manifest, "source")


def test_an_unknown_route_is_refused(tmp_path: Path, release_route: ReleaseRoute) -> None:
    manifest = _generate(release_route)
    with pytest.raises(release.Failure):
        release.select_install_route(manifest, "auto")


def _proof_context(tmp_path: Path, version: str) -> Any:
    return release.Context(
        repo_root=tmp_path / "checkout",
        work_root=tmp_path / "work",
        uv=tmp_path / "uv",
        version=version,
        dist_dir=tmp_path / "dist",
        wheel=tmp_path / "dist" / "artifact.whl",
        sdist=tmp_path / "dist" / "artifact.tar.gz",
        tool_dir=tmp_path / "tools",
        tool_bin=tmp_path / "tool-bin",
        tool_python=tmp_path / "tools" / "python",
        cli=tmp_path / "tool-bin" / "mnemoseed-local",
    )


@pytest.mark.parametrize("old_version", ["0.2.0", "0.0.1"])
def test_the_installed_surface_rejects_old_release_clis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_version: str
) -> None:
    context = _proof_context(tmp_path, FIXTURE_VERSION)
    verifier = release.Verifier(tmp_path / "report.json")

    def run(argv: Sequence[str], **kwargs: object) -> Any:
        return release.Run(
            argv=[str(item) for item in argv],
            exit_code=0,
            stdout=f"{release.PROJECT} {old_version}\n",
            stderr="",
            duration_s=0.0,
        )

    monkeypatch.setattr(verifier, "run", run)
    with pytest.raises(release.Failure):
        release.proof_installed_surface(verifier, context)


def test_the_capture_recall_round_refuses_a_cli_without_the_setup_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _proof_context(tmp_path, FIXTURE_VERSION)
    verifier = release.Verifier(tmp_path / "report.json")

    def run(argv: Sequence[str], **kwargs: object) -> Any:
        command = [str(item) for item in argv]
        refused = command[1:3] == ["init", "--mode"]
        return release.Run(
            argv=command,
            exit_code=2 if refused else 0,
            stdout="" if refused else f"{release.PROJECT} {FIXTURE_VERSION}\n",
            stderr="unrecognized arguments: --mode" if refused else "",
            duration_s=0.0,
        )

    monkeypatch.setattr(verifier, "run", run)
    with pytest.raises(release.Failure):
        release.proof_capture_recall_round(verifier, context)
