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
# The watch sits on the import system rather than on ``builtins.__import__``, so
# a child that reaches the socket module by any route still gets the guard.
SITECUSTOMIZE = """\
import importlib.util
import sys

_GUARD_MODULE = {module}


def _install_guard():
    spec = importlib.util.spec_from_file_location("port_guard", _GUARD_MODULE)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.install()


class _GuardedLoader:
    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        _install_guard()

    def __getattr__(self, name):
        return getattr(self._loader, name)


class _SocketImportWatcher:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "socket":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec("socket")
        if spec is not None and spec.loader is not None:
            spec.loader = _GuardedLoader(spec.loader)
        return spec


if "socket" in sys.modules:
    _install_guard()
else:
    sys.meta_path.insert(0, _SocketImportWatcher())
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
