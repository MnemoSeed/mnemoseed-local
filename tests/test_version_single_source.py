"""Version single-source invariants.

The authoring source of truth for the project version is ``pyproject.toml``.
The package ``__version__`` and both install orchestrators must agree with it,
so a version bump is one edit plus the derived surfaces — never a hunt for
stale literals. The orchestrators install the ``codex`` extra, so their pin
names that extra at the same exact version.
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

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
