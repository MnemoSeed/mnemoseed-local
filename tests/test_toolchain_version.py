"""Toolchain version single-source invariants.

The kernel interpreter is pinned in one place per tool: pyproject for the
package floor, ruff and mypy for their static targets, and CI for the runtime
used by every gate. They must agree so a runtime bump cannot be half-applied.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_requires_python_floor_is_314() -> None:
    assert _pyproject()["project"]["requires-python"] == ">=3.14"


def test_ruff_target_version_is_py314() -> None:
    assert _pyproject()["tool"]["ruff"]["target-version"] == "py314"


def test_mypy_python_version_is_314() -> None:
    assert _pyproject()["tool"]["mypy"]["python_version"] == "3.14"


def test_ci_requests_python_314_everywhere() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    requested = re.findall(r'python-version:\s*"([^"]+)"', ci)
    assert requested, "ci.yml must pin a setup-python version"
    assert set(requested) == {"3.14"}, f"expected every ci python-version to be 3.14, got {requested}"
