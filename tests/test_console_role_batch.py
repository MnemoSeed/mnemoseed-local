"""Console role-bind batch contract: driver+model are written together.

A single-key write to an unconfigured role is refused by the configwrite
reload guard, so the console's LLM-role Save controls must send both halves of
the route in one atomic ``POST /api/v1/config/set-many``, driver first.
The shipped app.js is driven in node with stubbed DOM/fetch; no daemon, real
home directory or port 7788 is involved.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "mnemoseed_local" / "console" / "static"
HARNESS = Path(__file__).parent / "ts_hook" / "console_role_batch.mjs"
PRESELECTION_DRIVER = Path(__file__).parent / "ts_hook" / "provider_preselection.mjs"


def test_preselection() -> None:
    """Mandatory default-effect gate: the shipped wizard's first render has no
    preselected provider. ``check=True`` makes a missing node an ERROR (RED),
    never a skip."""
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required for the mandatory preselection gate")
    result = subprocess.run(
        [node, str(PRESELECTION_DRIVER), str(STATIC_DIR / "app.js")],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert "provider_preselection.mjs OK" in result.stdout


def test_role_bind_writes_driver_and_model_in_one_batch() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the console role-bind harness")

    result = subprocess.run(
        [node, str(HARNESS), str(STATIC_DIR / "app.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
