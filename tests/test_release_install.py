"""Installed artifact provenance is bound to one isolated tool environment."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import subprocess
import sys
import venv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release_install_verifier", Path(__file__).resolve().parents[1] / "scripts" / "verify_release_install.py"
)
assert _SPEC is not None and _SPEC.loader is not None
release = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = release
_SPEC.loader.exec_module(release)

_PATH_FIELDS = ("interpreter", "prefix", "base_prefix", "package_file", "dist_location", "zstandard_file")
_INSTALLED_FIELDS = ("package_file", "dist_location", "zstandard_file")
_MISSING = object()


@dataclass
class Surface:
    context: Any
    verifier: Any
    payload: dict[str, object]

    def prove(self) -> None:
        release.proof_installed_surface(self.verifier, self.context)


def _path(path: Path, field: str) -> Path:
    if field == "dist_location":
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return path


def _symlink(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        if os.name == "nt" and (exc.winerror == 1314 or exc.errno in {errno.EACCES, errno.EPERM}):
            pytest.skip("Windows symlink privileges are unavailable")
        raise


@pytest.fixture
def surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Surface:
    tool_dir = tmp_path / "tools"
    environment = tool_dir / release.PROJECT
    bin_name = "Scripts" if os.name == "nt" else "bin"
    python_name = "python.exe" if os.name == "nt" else "python"
    interpreter = _path(environment / bin_name / python_name, "interpreter")
    site = environment / "lib" / "site-packages"
    package = _path(site / "mnemoseed_local" / "__init__.py", "package_file")
    zstandard = _path(site / "zstandard" / "__init__.py", "zstandard_file")
    base = tmp_path / "base-python"
    base.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    context = release.Context(
        repo_root=checkout,
        work_root=tmp_path / "work",
        uv=tmp_path / "uv",
        version="0.2.0",
        dist_dir=tmp_path / "dist",
        wheel=tmp_path / "dist" / "artifact.whl",
        sdist=tmp_path / "dist" / "artifact.tar.gz",
        tool_dir=tool_dir,
        tool_bin=tmp_path / "tool-bin",
        tool_python=interpreter,
        cli=tmp_path / "tool-bin" / "mnemoseed-local",
    )
    payload: dict[str, object] = {
        "interpreter": str(interpreter),
        "prefix": str(environment),
        "base_prefix": str(base),
        "package_file": str(package),
        "dist_location": str(site),
        "installed_version": context.version,
        "zstandard_version": "0.22.0",
        "zstandard_file": str(zstandard),
    }
    verifier = release.Verifier(tmp_path / "report.json")

    def run(argv: Sequence[object], **kwargs: object) -> Any:
        command = [str(item) for item in argv]
        if command == [str(context.installed_cli), "--version"]:
            output = f"{release.PROJECT} {context.version}\n"
        else:
            assert command[:3] == [str(context.installed_python), "-I", "-c"]
            assert len(command) == 4
            output = json.dumps(payload) + "\n"
        return release.Run(argv=command, exit_code=0, stdout=output, stderr="", duration_s=0.0)

    monkeypatch.setattr(verifier, "run", run)
    return Surface(context, verifier, payload)


def test_installed_surface_accepts_exact_environment_without_symlinks(surface: Surface) -> None:
    surface.prove()
    assert surface.verifier.report["tools"]["zstandard"] == "0.22.0"


def test_installed_surface_accepts_real_venv_symlink_outside_environment(surface: Surface) -> None:
    environment = surface.context.tool_dir / release.PROJECT
    interpreter = surface.context.installed_python
    interpreter.unlink()
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    if os.name == "nt" and not interpreter.is_symlink():
        interpreter.unlink()
        _symlink(interpreter, Path(sys.executable).resolve())
    assert interpreter.is_symlink()
    assert not interpreter.resolve().is_relative_to(environment.resolve())
    home = surface.context.work_root / "probe-home"
    home.mkdir(parents=True)
    env = dict(os.environ)
    env.update(HOME=str(home), USERPROFILE=str(home), MNEMOSEED_LOCAL_HOME=str(home / "memory"))
    completed = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-c",
            "import json, sys; print(json.dumps(dict("
            "interpreter=sys.executable, prefix=sys.prefix, base_prefix=sys.base_prefix)))",
        ],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    facts = json.loads(completed.stdout)
    assert Path(facts["prefix"]).resolve() == environment.resolve()
    assert Path(facts["base_prefix"]).resolve() != environment.resolve()
    assert Path(facts["interpreter"]).resolve() == interpreter.resolve()
    surface.payload.update(facts)

    surface.prove()


@pytest.mark.parametrize("location", ["sibling", "nested", "parent", "outside"])
def test_installed_surface_rejects_nonexact_prefix(surface: Surface, location: str) -> None:
    environment = surface.context.tool_dir / release.PROJECT
    alternatives = {
        "sibling": surface.context.tool_dir / f"{release.PROJECT}-other",
        "nested": environment / "nested",
        "parent": surface.context.tool_dir,
        "outside": surface.context.work_root / "other-environment",
    }
    prefix = alternatives[location]
    prefix.mkdir(parents=True, exist_ok=True)
    surface.payload["prefix"] = str(prefix)

    with pytest.raises(release.Failure):
        surface.prove()


def test_installed_surface_rejects_base_interpreter_environment(surface: Surface) -> None:
    surface.payload["base_prefix"] = surface.payload["prefix"]

    with pytest.raises(release.Failure):
        surface.prove()


def test_installed_surface_rejects_another_executable_with_same_target(surface: Surface) -> None:
    interpreter = surface.context.installed_python
    alias = interpreter.with_name("another-python.exe" if os.name == "nt" else "another-python")
    _symlink(alias, interpreter)
    assert alias.resolve() == interpreter.resolve()
    surface.payload["interpreter"] = str(alias)

    with pytest.raises(release.Failure):
        surface.prove()


def test_installed_surface_accepts_lexically_normalized_interpreter(surface: Surface) -> None:
    interpreter = surface.context.installed_python
    surface.payload["interpreter"] = str(
        interpreter.parent / ".." / interpreter.parent.name / interpreter.name
    )

    surface.prove()


def test_installed_surface_uses_native_interpreter_case_rules(surface: Surface) -> None:
    surface.payload["interpreter"] = str(surface.context.installed_python).swapcase()

    if os.name == "nt":
        surface.prove()
    else:
        with pytest.raises(release.Failure):
            surface.prove()


@pytest.mark.parametrize("field", _PATH_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_MISSING, id="missing"),
        pytest.param(None, id="null"),
        pytest.param(42, id="number"),
        pytest.param(False, id="boolean"),
        pytest.param([], id="array"),
        pytest.param({}, id="object"),
        pytest.param("", id="empty"),
        pytest.param(" \t ", id="blank"),
        pytest.param("relative/path", id="relative"),
    ],
)
def test_installed_surface_rejects_invalid_required_path(surface: Surface, field: str, value: object) -> None:
    if value is _MISSING:
        del surface.payload[field]
    else:
        surface.payload[field] = value

    with pytest.raises(release.Failure):
        surface.prove()


@pytest.mark.parametrize("field", _INSTALLED_FIELDS)
@pytest.mark.parametrize("location", ["sibling", "outside"])
def test_installed_surface_rejects_each_path_outside_exact_environment(
    surface: Surface, field: str, location: str
) -> None:
    root = (
        surface.context.tool_dir / f"{release.PROJECT}-other"
        if location == "sibling"
        else surface.context.work_root / "external"
    )
    surface.payload[field] = str(_path(root / field, field))

    with pytest.raises(release.Failure):
        surface.prove()


@pytest.mark.parametrize("field", _INSTALLED_FIELDS)
def test_installed_surface_rejects_each_path_resolving_outside_environment(
    surface: Surface, field: str
) -> None:
    target = _path(surface.context.work_root / "external" / field, field)
    link = surface.context.tool_dir / release.PROJECT / f"linked-{field}"
    _symlink(link, target)
    surface.payload[field] = str(link)

    with pytest.raises(release.Failure):
        surface.prove()


@pytest.mark.parametrize("field", _INSTALLED_FIELDS)
def test_installed_surface_rejects_each_checkout_path_even_inside_environment(
    surface: Surface, field: str
) -> None:
    checkout = surface.context.tool_dir / release.PROJECT / "checkout"
    surface.context.repo_root = checkout
    surface.payload[field] = str(_path(checkout / field, field))

    with pytest.raises(release.Failure):
        surface.prove()


def test_installed_surface_keeps_artifact_version_check(surface: Surface) -> None:
    surface.payload["installed_version"] = "different-version"

    with pytest.raises(release.Failure):
        surface.prove()
