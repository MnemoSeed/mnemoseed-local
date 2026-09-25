"""Session-wide isolation guards for the test run."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import port_guard
import pytest

GUARD_MODULE = Path(port_guard.__file__).resolve()
# Installed on the child's first ``import socket`` rather than at startup: an
# eager install would put ``socket`` in every child's ``sys.modules``, and a
# child that must stay free of the network modules never imports it at all.
SITECUSTOMIZE = """\
import builtins

_GUARD_MODULE = {module}
_import = builtins.__import__
_installed = False


def _guarded_import(name, *args, **kwargs):
    global _installed
    module = _import(name, *args, **kwargs)
    if not _installed and name == "socket":
        import importlib.util

        _installed = True
        _spec = importlib.util.spec_from_file_location("port_guard", _GUARD_MODULE)
        _guard = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_guard)
        _guard.install()
    return module


builtins.__import__ = _guarded_import
"""


@pytest.fixture(autouse=True, scope="session")
def reserved_ports_stay_unused(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Refuse a reserved port here and in every child interpreter.

    A child inherits the refusal through a generated ``sitecustomize`` on an
    injected ``PYTHONPATH``. A child that drops ``PYTHONPATH`` from the
    environment it builds, or that runs an interpreter with ``-I``, ``-S`` or
    ``-E``, sits outside this boundary.
    """
    guard_dir = tmp_path_factory.mktemp("reserved-port-guard")
    (guard_dir / "sitecustomize.py").write_text(
        SITECUSTOMIZE.format(module=json.dumps(str(GUARD_MODULE))), encoding="utf-8"
    )
    inherited = os.environ.get("PYTHONPATH", "")
    entries = [str(guard_dir), *filter(None, inherited.split(os.pathsep))]
    os.environ["PYTHONPATH"] = os.pathsep.join(entries)
    port_guard.install()
    try:
        yield
    finally:
        port_guard.uninstall()
        if inherited:
            os.environ["PYTHONPATH"] = inherited
        else:
            os.environ.pop("PYTHONPATH", None)
