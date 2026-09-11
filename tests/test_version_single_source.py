"""Version single-source invariants.

The authoring source of truth for the project version is ``pyproject.toml``.
The package ``__version__`` and both install orchestrators must agree with it,
so a version bump is one edit plus the derived surfaces — never a hunt for
stale literals.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

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
    pinned = f"mnemoseed-local=={_pyproject_version()}"
    for name in ("install.ps1", "install.sh"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert pinned in text, f"{name} must install the pinned {pinned}"
