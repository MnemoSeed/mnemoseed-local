"""The reload guard: an exception's identity does not survive a reload.

A reload re-executes a module in place, so the classes it defines come back as
new objects. A module that imported one keeps the old object, and the failure it
causes surfaces in a file the change never touched.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
import reload_guard

import mnemoseed_local.config

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent

#: The modules the package's own exceptions live in. Reloading one re-creates the
#: class every other module compares its instances against.
KNOWN_UNSAFE = {
    "mnemoseed_local.config": ("ConfigError",),
    "mnemoseed_local.credential": ("DaemonCredentialError",),
    "mnemoseed_local.rest_client": ("DaemonRestError", "DaemonUnavailableError"),
}

#: A module of the package that defines no exception, so reloading it is only a
#: re-read of its source.
SAFE_MODULE = "mnemoseed_local.comment_guard"

RELOAD_CASE = (
    "import importlib\n\nimport {module}\n\n\ndef test_reload() -> None:\n    importlib.reload({module})\n"
)

#: The unrelated test the reload breaks: it compares the class it imported against
#: the class the reloaded module raises.
IDENTITY_CASE = (
    "import pytest\n"
    "\n"
    "import mnemoseed_local.config\n"
    "from mnemoseed_local.config import ConfigError\n"
    "\n"
    "\n"
    "def test_the_module_raises_the_imported_class() -> None:\n"
    "    with pytest.raises(ConfigError):\n"
    "        raise mnemoseed_local.config.ConfigError('version', 'unreadable')\n"
)


#: A re-execution of a module's source in the module's own namespace: the classes
#: it defines come back as new objects, exactly as a reload leaves them, while no
#: reload happens and the guard sees nothing to refuse.
REEXECUTION_CASE = (
    "import pathlib\n"
    "\n"
    "import mnemoseed_local.config\n"
    "\n"
    "\n"
    "def test_reexecution() -> None:\n"
    '    source = pathlib.Path(mnemoseed_local.config.__file__).read_text(encoding="utf-8")\n'
    "    exec(source, mnemoseed_local.config.__dict__)\n"
)


def _reload_case(directory: Path, module: str, filename: str = "test_a_reload.py") -> Path:
    case = directory / filename
    case.write_text(RELOAD_CASE.format(module=module), encoding="utf-8")
    return case


def _reexecution_case(directory: Path) -> Path:
    case = directory / "test_a_reexecution.py"
    case.write_text(REEXECUTION_CASE, encoding="utf-8")
    return case


def _child_pytest(directory: Path, guarded: bool) -> subprocess.CompletedProcess[str]:
    """Run one directory of cases in a child interpreter, behind the guard or not."""
    home = directory / "home"
    home.mkdir(exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        str(directory),
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    if guarded:
        command += ["-p", "reload_guard"]
    return subprocess.run(
        command,
        cwd=str(directory),
        env={**os.environ, "MNEMOSEED_LOCAL_HOME": str(home), "PYTHONPATH": str(TESTS)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_the_reload_guard_reports_the_modules_that_define_an_exception() -> None:
    unsafe = reload_guard.unsafe_modules()

    for module, classes in KNOWN_UNSAFE.items():
        assert unsafe[module] == classes


def test_no_test_source_reloads_a_module_that_defines_an_exception() -> None:
    """The guard reads the suite's own sources, so a reload written in a test that
    a condition skips is reported too."""
    unsafe = reload_guard.unsafe_modules()
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line} reloads {module}"
        for path in sorted(TESTS.rglob("*.py"))
        for line, module in reload_guard.reloads_of_unsafe_modules(path.read_text(encoding="utf-8"), unsafe)
    ]

    assert not offenders, "a reload re-creates the classes it defines: " + "; ".join(offenders)


def test_the_session_refuses_a_reload_of_a_module_that_defines_an_exception() -> None:
    """The suite's own session is armed: the reload it would perform refuses a
    module that defines an exception, before re-executing it."""
    armed = importlib.reload

    with pytest.raises(reload_guard.ReloadUnsafeError, match="mnemoseed_local.config"):
        armed(mnemoseed_local.config)


def test_the_guard_fires_before_a_reload_can_break_an_unrelated_test(tmp_path: Path) -> None:
    """Without the guard the failure lands in the test beside the reload; with the
    guard the reloading test fails and the test beside it passes."""
    _reload_case(tmp_path, "mnemoseed_local.config")
    (tmp_path / "test_b_identity.py").write_text(IDENTITY_CASE, encoding="utf-8")

    without_guard = _child_pytest(tmp_path, guarded=False)
    with_guard = _child_pytest(tmp_path, guarded=True)

    assert without_guard.returncode != 0, without_guard.stdout
    assert "test_b_identity" in without_guard.stdout
    assert "test_a_reload" not in without_guard.stdout

    assert with_guard.returncode != 0, with_guard.stdout
    assert "test_a_reload" in with_guard.stdout
    assert "test_b_identity" not in with_guard.stdout
    assert "ConfigError" in with_guard.stdout


def test_no_test_source_re_executes_a_module_in_its_own_namespace() -> None:
    """A module re-executed through its own namespace re-creates the classes it
    defines, so the suite's sources are read for that shape as well."""
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line} re-executes {module or 'a module'} in place"
        for path in sorted(TESTS.rglob("*.py"))
        for line, module in reload_guard.in_place_reexecutions(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, "a re-execution in place re-creates the classes it defines: " + "; ".join(offenders)


def test_a_module_re_executed_in_its_own_namespace_is_reported() -> None:
    """The report reads the shape a reload-free re-execution is written as: an
    ``exec`` into a module's own namespace, whether spelled directly or through
    ``vars``. A namespace of the caller's own, ``runpy``, and a child interpreter
    re-create no class and are reported by nothing."""
    assert reload_guard.in_place_reexecutions(REEXECUTION_CASE) == ((8, "mnemoseed_local.config"),)
    assert reload_guard.in_place_reexecutions("exec(source, vars(mnemoseed_local.config))\n") == (
        (1, "mnemoseed_local.config"),
    )
    assert reload_guard.in_place_reexecutions("exec(source, {})\n") == ()
    assert reload_guard.in_place_reexecutions("exec(source, namespace)\n") == ()
    assert (
        reload_guard.in_place_reexecutions("import runpy\nrunpy.run_module('mnemoseed_local.config')\n") == ()
    )
    assert reload_guard.in_place_reexecutions("import runpy\nrunpy.run_path('config.py')\n") == ()


def test_re_executing_a_module_in_place_breaks_a_test_beside_it(tmp_path: Path) -> None:
    """A re-execution in place breaks the test beside it exactly as a reload does,
    and the armed guard stays silent: the shape is refused by nothing, so the
    report from the source is the whole defence."""
    _reexecution_case(tmp_path)
    (tmp_path / "test_b_identity.py").write_text(IDENTITY_CASE, encoding="utf-8")

    guarded = _child_pytest(tmp_path, guarded=True)

    assert guarded.returncode != 0, guarded.stdout
    assert "test_b_identity" in guarded.stdout
    assert "test_a_reexecution" not in guarded.stdout
    assert "ConfigError" in guarded.stdout
    assert reload_guard.in_place_reexecutions(REEXECUTION_CASE), "the shape is read from the source"


def test_a_reload_of_a_module_that_defines_no_exception_is_left_alone(tmp_path: Path) -> None:
    _reload_case(tmp_path, SAFE_MODULE)

    guarded = _child_pytest(tmp_path, guarded=True)

    assert guarded.returncode == 0, guarded.stdout
