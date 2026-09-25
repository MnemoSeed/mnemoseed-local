"""Reserved-port isolation for the test session and for every child it starts.

The session guard refuses a bind or a dial on a port reserved for the installed
daemon. A Python child inherits the same refusal through a generated
``sitecustomize`` on an injected ``PYTHONPATH``. PowerShell and Node children
offer no injection point, so their sources carry a static constraint instead,
which is a pin and not an enforcement boundary.

The Python-child boundary is narrower still, and the tests below pin where it
ends: an interpreter that isolates itself, and a child handed an environment
without the injected entry, both load no guard. Restoring the socket surface is
likewise confined to the guard and the session fixture by a static constraint.
None of these is an enforced boundary, and no child of any kind is stopped at
the syscall.

Every attempt below names a reserved port through an over-long address tuple,
which CPython refuses while parsing the argument, so no bind or connect syscall
runs even when the guard is absent. A well-formed address would be honoured by
the kernel: a two-element tuple binds 7788 outright on a host where nothing
listens there, which is the hazard the guard exists to prevent.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import port_guard
import pytest
from port_guard import RESERVED_PORTS, ReservedPortError

TESTS = Path(__file__).resolve().parent
# Stated apart from the guard's own set on purpose: an oracle derived from it
# would lose its case the moment a port leaves the set.
EXPECTED_RESERVED_PORTS = (4096, 7788)
CHILD_SOURCE_SUFFIXES = frozenset({".cjs", ".js", ".mjs", ".ps1", ".psm1"})
POWERSHELL_SUFFIXES = frozenset({".ps1", ".psm1"})
POWERSHELL_NETWORK_PRIMITIVES = (
    "Get-NetTCPConnection",
    "Invoke-RestMethod",
    "Invoke-WebRequest",
    "New-Object System.Net",
    "System.Net.Sockets",
    "TcpClient",
    "TcpListener",
    "Test-NetConnection",
)
JAVASCRIPT_NETWORK_PRIMITIVES = (
    "WebSocket",
    "createServer",
    "net.Socket",
    "net.connect",
    "node:dgram",
    "node:http",
    "node:net",
    "node:tls",
    'require("http',
    'require("net',
    "require('http",
    "require('net",
)
FETCH_OVERRIDE = "globalThis.fetch"
GUARDED_OPERATIONS = ("bind", "connect", "connect_ex", "create_connection")
NETWORK_MODULE_ROOTS = ("http", "socket", "ssl", "urllib")
HERMETIC_CHILD_SOURCE = """\
import json
import sys

print(json.dumps([name for name in sys.modules if name.split(".")[0] in {roots}]))
"""
INTERPRETER_ISOLATION_FLAGS = ("-E", "-I", "-S")
GUARD_OWNER_MODULES = frozenset({"conftest.py", "port_guard.py"})
_GUARD_MODULE_NAME = "port_guard"
UNINSTALL_REFERENCE = re.compile(
    rf"{_GUARD_MODULE_NAME}\s*\.\s*uninstall\b"
    rf"|\bfrom\s+{_GUARD_MODULE_NAME}\s+import\b[^\n]*\buninstall\b"
)
IMPORT_ROOTS = ("http", "socket", "ssl", "urllib")
FOOTPRINT_SOURCE = '''\
"""Report the networking imports a child holds when it dials nothing."""

import json
import sys

print(json.dumps(sorted(m for m in sys.modules if m.split(".")[0] in set(sys.argv[1:]))))
'''
CHILD_SOURCE = '''\
"""Report whether this interpreter refuses a reserved port.

The premise probe runs first; the reserved-port attempt runs only once the
premise shows the address shape cannot reach the kernel.
"""

import json
import sys

if sys.argv[1] == "importlib":
    import importlib

    importlib.import_module("socket")
else:
    import socket

socket = sys.modules["socket"]


def attempt(operation, port):
    address = ("127.0.0.1", port, 0)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if operation == "create_connection":
            socket.create_connection(address).close()
        else:
            getattr(probe, operation)(address)
    except OSError as error:
        return "refused:" + type(error).__name__
    except (TypeError, ValueError):
        return "shape-rejected"
    finally:
        probe.close()
    return "not-refused"


port = int(sys.argv[2])
guard_ports = getattr(socket, sys.argv[3], None)
operations = sys.argv[4].split(",")
report = {
    "guard_ports": sorted(guard_ports) if guard_ports is not None else None,
    "shape_rejected": attempt("bind", 0) == "shape-rejected",
}
if report["shape_rejected"]:
    for operation in operations:
        report[operation] = attempt(operation, port)
print(json.dumps(report))
sys.exit(0 if report["shape_rejected"] else 3)
'''


def unconstructible_address(port: int) -> tuple[object, ...]:
    """An address shape CPython rejects before any syscall, port included."""
    return ("127.0.0.1", port, 0)


def network_primitive_violations(path: Path, text: str) -> list[str]:
    """Report the live network primitives a test-owned child source must not name."""
    suffix = path.suffix.lower()
    primitives = (
        POWERSHELL_NETWORK_PRIMITIVES if suffix in POWERSHELL_SUFFIXES else JAVASCRIPT_NETWORK_PRIMITIVES
    )
    found = [primitive for primitive in primitives if primitive in text]
    if suffix not in POWERSHELL_SUFFIXES and "fetch(" in text and FETCH_OVERRIDE not in text:
        found.append("fetch(")
    return found


def child_sources() -> list[Path]:
    """Every non-Python child script a test can hand to an interpreter."""
    return sorted(
        path
        for path in TESTS.rglob("*")
        if path.is_file() and path.suffix.lower() in CHILD_SOURCE_SUFFIXES and "__pycache__" not in path.parts
    )


def guard_uninstall_violations(text: str) -> list[str]:
    """Report a reference to the guard's restore entry point from a test module."""
    return [match.group(0) for match in UNINSTALL_REFERENCE.finditer(text)]


def child_probe(tmp_path: Path, port: int, *flags: str, keep_pythonpath: bool = True) -> dict[str, object]:
    """Report what a child interpreter does with a reserved port under the given flags.

    ``keep_pythonpath`` decides whether the child inherits the injected path entry,
    which is what a test building its own environment controls.
    """
    script = tmp_path / "attempt_reserved_port.py"
    script.write_text(CHILD_SOURCE, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environment = {**os.environ, "MNEMOSEED_LOCAL_HOME": str(home)}
    if not keep_pythonpath:
        environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            *flags,
            str(script),
            str(port),
            port_guard.GUARD_MARKER,
            ",".join(GUARDED_OPERATIONS),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    report = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload: dict[str, object] = json.loads(report)
    assert payload["shape_rejected"] is True
    return payload


def child_import_footprint(tmp_path: Path) -> list[str]:
    """The networking imports a child holds when nothing in it needs them."""
    script = tmp_path / "import_nothing_networked.py"
    script.write_text(FOOTPRINT_SOURCE, encoding="utf-8")
    home = tmp_path / "footprint-home"
    home.mkdir(exist_ok=True)
    completed = subprocess.run(
        [sys.executable, str(script), *IMPORT_ROOTS],
        env={**os.environ, "MNEMOSEED_LOCAL_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report: list[str] = json.loads(completed.stdout.strip().splitlines()[-1])
    return report


def assert_child_outside_boundary(payload: dict[str, object]) -> None:
    """A child that loaded no guard reports no ports and dies in argument parsing."""
    assert payload["guard_ports"] is None
    for operation in GUARDED_OPERATIONS:
        assert payload[operation] == "shape-rejected", operation


def test_guard_reserves_every_expected_port() -> None:
    assert RESERVED_PORTS == frozenset(EXPECTED_RESERVED_PORTS)


@pytest.mark.parametrize("port", EXPECTED_RESERVED_PORTS)
def test_session_refuses_every_reserved_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        with pytest.raises(ReservedPortError) as caught:
            probe.bind(unconstructible_address(port))

    assert str(port) in str(caught.value)


def test_guard_delegates_a_port_outside_the_reserved_set() -> None:
    """The refusal is by port, not blanket: a spare loopback port still serves and dials."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        host, port = listener.getsockname()[:2]
        assert port not in RESERVED_PORTS
        with socket.create_connection((host, port), timeout=10):
            pass
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            assert probe.connect_ex((host, port)) == 0


@pytest.mark.parametrize("port", EXPECTED_RESERVED_PORTS)
def test_child_process_refuses_every_reserved_port(tmp_path: Path, port: int) -> None:
    """A child interpreter refuses a reserved port on every guarded entry point."""
    payload = child_probe(tmp_path, port)

    assert payload["guard_ports"] == sorted(EXPECTED_RESERVED_PORTS)
    for operation in GUARDED_OPERATIONS:
        assert payload[operation] == f"refused:{ReservedPortError.__name__}", operation


def test_a_child_that_dials_nothing_carries_no_guard_footprint(tmp_path: Path) -> None:
    """The guard loads without importing socket, so a child's import table stays its own."""
    assert child_import_footprint(tmp_path) == []


def test_a_child_that_never_imports_socket_keeps_no_socket(tmp_path: Path) -> None:
    """The child guard waits for the child's own ``import socket``.

    An eager install would put the network modules in every interpreter and
    take away a hermetic child's own property, so the wait is what keeps the
    guard compatible with a child that must stay free of them.
    """
    script = tmp_path / "hermetic_child.py"
    script.write_text(HERMETIC_CHILD_SOURCE.format(roots=json.dumps(NETWORK_MODULE_ROOTS)), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    completed = subprocess.run(
        [sys.executable, str(script)],
        env={**os.environ, "MNEMOSEED_LOCAL_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip()) == []


@pytest.mark.parametrize("flag", INTERPRETER_ISOLATION_FLAGS)
def test_isolated_interpreter_is_outside_the_child_boundary(tmp_path: Path, flag: str) -> None:
    """An interpreter that isolates itself loads no guard: a pinned limit, not an enforced one."""
    payload = child_probe(tmp_path, EXPECTED_RESERVED_PORTS[0], flag)

    assert_child_outside_boundary(payload)


def test_the_same_child_is_guarded_without_an_isolation_flag(tmp_path: Path) -> None:
    """The control for the pinned limit: the same child inherits the guard when nothing isolates it."""
    payload = child_probe(tmp_path, EXPECTED_RESERVED_PORTS[0])

    assert payload["guard_ports"] == sorted(EXPECTED_RESERVED_PORTS)


def test_child_without_the_injected_path_entry_is_outside_the_boundary(tmp_path: Path) -> None:
    """A child handed an environment that drops the injected entry loads no guard."""
    payload = child_probe(tmp_path, EXPECTED_RESERVED_PORTS[0], keep_pythonpath=False)

    assert_child_outside_boundary(payload)


def test_only_the_guard_owners_restore_the_socket_surface() -> None:
    """Restoring the socket surface belongs to the guard and the session fixture alone."""
    offenders = {
        str(path.relative_to(TESTS)): found
        for path in sorted(TESTS.rglob("*.py"))
        if path.name not in GUARD_OWNER_MODULES and "__pycache__" not in path.parts
        if (found := guard_uninstall_violations(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


def test_the_restore_reference_scan_detects_a_violation() -> None:
    """The restore scan must not pass for want of a match, and must spare another module."""
    assert guard_uninstall_violations(f"{_GUARD_MODULE_NAME}.uninstall()\n") != []
    assert guard_uninstall_violations(f"from {_GUARD_MODULE_NAME} import uninstall\n") != []
    assert guard_uninstall_violations("cc_install.uninstall()\n") == []


def test_test_child_sources_reach_no_live_port() -> None:
    """A PowerShell or Node child has no injection point, so its source is pinned."""
    offenders = {
        str(path.relative_to(TESTS)): found
        for path in child_sources()
        if (found := network_primitive_violations(path, path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


@pytest.mark.parametrize("primitive", POWERSHELL_NETWORK_PRIMITIVES + JAVASCRIPT_NETWORK_PRIMITIVES)
def test_static_constraint_detects_a_network_primitive(primitive: str) -> None:
    suffix = ".ps1" if primitive in POWERSHELL_NETWORK_PRIMITIVES else ".mjs"

    assert network_primitive_violations(Path(f"child{suffix}"), f"$value = '{primitive}'\n") == [primitive]


def test_static_constraint_accepts_an_overridden_fetch() -> None:
    assert network_primitive_violations(Path("child.mjs"), "await fetch(url)\n") == ["fetch("]
    assert (
        network_primitive_violations(
            Path("child.mjs"), f"{FETCH_OVERRIDE} = async () => {{}}\nawait fetch(u)\n"
        )
        == []
    )
