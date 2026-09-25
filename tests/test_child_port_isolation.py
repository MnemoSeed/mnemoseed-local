"""Reserved-port isolation for the test session and for every child it starts.

The session guard refuses a bind, a dial or a datagram transmit on a port
reserved for the installed daemon. A Python child inherits the same refusal
through a generated ``sitecustomize`` on an injected ``PYTHONPATH``. PowerShell
and Node children offer no injection point, so their sources carry a static
constraint instead, which is a pin and not an enforcement boundary. The scan
matches a PowerShell command name whatever its case, because PowerShell command
names are case-insensitive, and it still reads only the files in the tree under
the suffixes it knows: a source a test writes at run time is not in it, and a
source under any other suffix is not either.

The Python-child boundary is narrower still, and the tests below pin where it
ends: an interpreter that isolates itself, and a child handed an environment
without the injected entry, both load no guard. Restoring the socket surface is
likewise confined to the guard and the session fixture by a static constraint.
None of these is an enforced boundary, and no child of any kind is stopped at
the syscall.

Every stream attempt below names a reserved port through an over-long address
tuple, which CPython refuses while parsing the argument, so no bind or connect
syscall runs even when the guard is absent. A well-formed address would be
honoured by the kernel: a two-element tuple binds 7788 outright on a host where
nothing listens there, which is the hazard the guard exists to prevent. A
datagram send has no such unusable shape, so its probe stands a recorder in for
the guarded transmit and a refusal that never happens is counted rather than
sent.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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
GUARDED_OPERATIONS = ("bind", "connect", "connect_ex", "create_connection", "sendto")
# A datagram send has no address shape the argument parser refuses, so its probe
# reaches a well-formed destination with a recorder in for the transmit.
DATAGRAM_OPERATIONS = ("sendto",)
# sendmsg can take a destination address as its fourth argument, but CPython
# offers it only where the platform provides the ancillary-data call. A host that
# offers it keeps a datagram path this guard does not wrap.
DESTINATION_ADDRESSING = ("sendto", "sendmsg")
UNGUARDED_DESTINATION_CALLS = frozenset({"sendmsg"})
CHILD_SOCKET_ROUTES = ("import-statement", "importlib")
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
CHILD_SOURCE = '''\
"""Report whether this interpreter refuses a reserved port.

The premise probe runs first; a stream attempt runs only once the premise shows
the address shape cannot reach the kernel. A datagram attempt names a
well-formed address and hands the guarded transmit to a recorder, so a refusal
that never happens is counted instead of sent.
"""

import json
import sys

ROUTE = sys.argv[1]
PORT = int(sys.argv[2])
MARKER = sys.argv[3]
STREAM_OPERATIONS = sys.argv[4].split(",")
ORIGINALS_ATTRIBUTE = sys.argv[5]
DATAGRAM_OPERATIONS = sys.argv[6].split(",")
BYPASS = sys.argv[7]

if ROUTE == "importlib":
    import importlib

    importlib.import_module("socket")
else:
    import socket

socket = sys.modules["socket"]
transmits = []


def stand_in(operation):
    """Point a guarded transmit at a recorder, so a missed refusal stays off the network."""
    originals = getattr(socket, ORIGINALS_ATTRIBUTE, None)
    if originals is None or operation not in originals:
        return None

    def record(self, data, address):
        transmits.append(address)
        return 0

    originals[operation] = record
    return record


def attempt_stream(operation, port=PORT):
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


def attempt_datagram(operation):
    record = stand_in(operation)
    if record is None:
        return "no-transmit-path"
    if BYPASS == operation:
        setattr(socket.socket, operation, record)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        getattr(probe, operation)(b"", ("127.0.0.1", PORT))
    except OSError as error:
        return "refused:" + type(error).__name__
    except (TypeError, ValueError):
        return "shape-rejected"
    finally:
        probe.close()
    return "not-refused"


guard_ports = getattr(socket, MARKER, None)
report = {
    "guard_ports": sorted(guard_ports) if guard_ports is not None else None,
    "shape_rejected": attempt_stream("bind", 0) == "shape-rejected",
}
if report["shape_rejected"]:
    for operation in STREAM_OPERATIONS + DATAGRAM_OPERATIONS:
        if operation in DATAGRAM_OPERATIONS:
            report[operation] = attempt_datagram(operation)
        else:
            report[operation] = attempt_stream(operation)
report["transmits"] = len(transmits)
print(json.dumps(report))
sys.exit(0 if report["shape_rejected"] else 3)
'''


EXEC_ONLY_LOADER_CHILD = '''\
"""Import the socket module through a loader that implements only exec_module.

PEP 451 leaves create_module optional, so the guard's wrapper around a finder's
loader has to tolerate a loader that has none. The finder sits behind the
guard's own socket watcher, which wraps whatever loader this finder returns.
"""

import importlib.machinery
import json
import sys


class ExecOnlyLoader:
    """A valid loader whose only import hook is exec_module."""

    def __init__(self, loader):
        self._loader = loader

    def exec_module(self, module):
        self._loader.exec_module(module)


class SocketFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "socket":
            return None
        spec = importlib.machinery.PathFinder.find_spec("socket")
        if spec is None or spec.loader is None:
            return None
        spec.loader = ExecOnlyLoader(spec.loader)
        return spec


sys.meta_path.insert(1, SocketFinder())

import socket

guard_ports = getattr(socket, sys.argv[1], None)
print(
    json.dumps(
        {
            "loader_offers_create_module": hasattr(ExecOnlyLoader, "create_module"),
            "socket_loaded": hasattr(socket, "AF_INET"),
            "guard_ports": sorted(guard_ports) if guard_ports is not None else None,
        }
    )
)
'''


def unconstructible_address(port: int) -> tuple[object, ...]:
    """An address shape CPython rejects before any syscall, port included."""
    return ("127.0.0.1", port, 0)


@contextmanager
def transmit_stand_in(*operations: str) -> Iterator[list[object]]:
    """Stand a recorder in for a guarded transmit, so a missed refusal reaches no network.

    The recorder is installed where the guard holds the original it would call,
    so a refused call never runs it and an unreached refusal never reaches a
    socket. Whatever the probe put there is put back on the way out.
    """
    originals = port_guard.patched_originals()
    recorded: list[object] = []
    replaced = {name: originals[name] for name in operations}

    def record(self: object, data: object, address: object) -> int:
        recorded.append(address)
        return 0

    originals.update(dict.fromkeys(operations, record))
    try:
        yield recorded
    finally:
        originals.update(replaced)


def network_primitive_violations(path: Path, text: str) -> list[str]:
    """Report the live network primitives a test-owned child source must not name.

    A PowerShell command name is case-insensitive, so that source and those
    names are compared folded. A JavaScript identifier is not, so that source is
    compared as written. A match reports the name as the scan spells it.
    """
    suffix = path.suffix.lower()
    powershell = suffix in POWERSHELL_SUFFIXES
    primitives = POWERSHELL_NETWORK_PRIMITIVES if powershell else JAVASCRIPT_NETWORK_PRIMITIVES
    subject = text.casefold() if powershell else text
    found = [
        primitive
        for primitive in primitives
        if (primitive.casefold() if powershell else primitive) in subject
    ]
    if not powershell and "fetch(" in text and FETCH_OVERRIDE not in text:
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


def child_probe(
    tmp_path: Path,
    port: int,
    *flags: str,
    keep_pythonpath: bool = True,
    route: str = "import-statement",
    bypass: str = "",
) -> dict[str, object]:
    """Report what a child interpreter does with a reserved port under the given flags.

    ``keep_pythonpath`` decides whether the child inherits the injected path entry,
    which is what a test building its own environment controls. ``route`` decides
    how the child reaches the socket module. ``bypass`` names one operation whose
    refusal the child drops, leaving the recorder in its place.
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
            route,
            str(port),
            port_guard.GUARD_MARKER,
            ",".join(op for op in GUARDED_OPERATIONS if op not in DATAGRAM_OPERATIONS),
            port_guard.ORIGINALS,
            ",".join(DATAGRAM_OPERATIONS),
            bypass,
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


def assert_child_outside_boundary(payload: dict[str, object]) -> None:
    """A child that loaded no guard reports no ports and dies in argument parsing."""
    assert payload["guard_ports"] is None
    for operation in GUARDED_OPERATIONS:
        expected = "no-transmit-path" if operation in DATAGRAM_OPERATIONS else "shape-rejected"
        assert payload[operation] == expected, operation
    assert payload["transmits"] == 0


def test_guard_reserves_every_expected_port() -> None:
    assert RESERVED_PORTS == frozenset(EXPECTED_RESERVED_PORTS)


@pytest.mark.parametrize("port", EXPECTED_RESERVED_PORTS)
def test_session_refuses_every_reserved_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        with pytest.raises(ReservedPortError) as caught:
            probe.bind(unconstructible_address(port))

    assert str(port) in str(caught.value)


@pytest.mark.parametrize("port", EXPECTED_RESERVED_PORTS)
@pytest.mark.parametrize("operation", DATAGRAM_OPERATIONS)
def test_session_refuses_every_reserved_port_on_a_datagram_send(port: int, operation: str) -> None:
    """A datagram send names a well-formed destination, so the transmit is stood in for."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        with transmit_stand_in(operation) as recorded:
            with pytest.raises(ReservedPortError) as caught:
                getattr(probe, operation)(b"", ("127.0.0.1", port))

    assert str(port) in str(caught.value)
    assert recorded == []


def test_guard_delegates_a_datagram_port_outside_the_reserved_set() -> None:
    """The datagram refusal is by port, not blanket: a spare loopback port still delivers."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.settimeout(10)
        listener.bind(("127.0.0.1", 0))
        host, port = listener.getsockname()[:2]
        assert port not in RESERVED_PORTS
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.settimeout(10)
            sender.sendto(b"probe", (host, port))
            assert listener.recvfrom(64)[0] == b"probe"


def test_every_destination_addressing_call_is_guarded_or_pinned_as_a_gap() -> None:
    """Audit the socket calls that can name a destination port, and account for each.

    ``sendto`` is the one CPython offers on every platform, and the guard wraps
    it. ``sendmsg`` names a destination address as well, but only where the
    platform provides the ancillary-data call, so a host that has it keeps a
    datagram path the guard does not wrap and the pin below names it.
    """
    offered = {name for name in DESTINATION_ADDRESSING if hasattr(socket.socket, name)}
    accounted = port_guard.guarded_operations() | UNGUARDED_DESTINATION_CALLS

    assert offered <= accounted
    assert set(DATAGRAM_OPERATIONS) <= port_guard.guarded_operations()


@pytest.mark.parametrize("operation", DATAGRAM_OPERATIONS)
def test_the_datagram_probe_reports_a_transmit_the_guard_lets_through(tmp_path: Path, operation: str) -> None:
    """The datagram probe has teeth: it counts a transmit instead of sending one.

    The child drops the refusal and keeps the recorder the guard would have
    called, so the probe reports the missed refusal without any packet leaving.
    """
    payload = child_probe(tmp_path, EXPECTED_RESERVED_PORTS[0], bypass=operation)

    assert payload[operation] == "not-refused"
    assert payload["transmits"] == 1


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
@pytest.mark.parametrize("route", CHILD_SOCKET_ROUTES)
def test_child_process_refuses_every_reserved_port(tmp_path: Path, port: int, route: str) -> None:
    """A child interpreter refuses a reserved port on every guarded entry point."""
    payload = child_probe(tmp_path, port, route=route)

    assert payload["guard_ports"] == sorted(EXPECTED_RESERVED_PORTS)
    for operation in GUARDED_OPERATIONS:
        assert payload[operation] == f"refused:{ReservedPortError.__name__}", operation
    assert payload["transmits"] == 0


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


def test_a_child_whose_loader_has_no_create_module_still_loads_the_guard(tmp_path: Path) -> None:
    """PEP 451 leaves create_module optional, so a loader with only exec_module must import.

    The child's own finder hands the socket module a loader with no
    create_module; the guard's watcher wraps it and the import has to complete,
    leaving the child with the real socket module and the refusal installed.
    """
    script = tmp_path / "exec_only_loader.py"
    script.write_text(EXEC_ONLY_LOADER_CHILD, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    environment = {**os.environ, "MNEMOSEED_LOCAL_HOME": str(home)}
    completed = subprocess.run(
        [sys.executable, str(script), port_guard.GUARD_MARKER],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload: dict[str, object] = json.loads(completed.stdout.strip())
    assert payload["loader_offers_create_module"] is False
    assert payload["socket_loaded"] is True
    assert payload["guard_ports"] == sorted(EXPECTED_RESERVED_PORTS)


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


@pytest.mark.parametrize("spelled", ["invoke-restmethod", "iNVOKE-rESTmETHOD"])
def test_static_constraint_detects_a_powershell_primitive_in_any_case(spelled: str) -> None:
    """PowerShell command names are case-insensitive, so the source is compared folded."""
    primitive = "Invoke-RestMethod"
    source = f"$r = {spelled} http://127.0.0.1:7788\n"

    assert network_primitive_violations(Path("child.ps1"), source) == [primitive]


def test_static_constraint_keeps_a_javascript_identifier_case_sensitive() -> None:
    """A JavaScript identifier is not case-insensitive, so its source is compared as written."""
    assert network_primitive_violations(Path("child.mjs"), "const w = new websocket(url)\n") == []


def test_static_constraint_accepts_an_overridden_fetch() -> None:
    assert network_primitive_violations(Path("child.mjs"), "await fetch(url)\n") == ["fetch("]
    assert (
        network_primitive_violations(
            Path("child.mjs"), f"{FETCH_OVERRIDE} = async () => {{}}\nawait fetch(u)\n"
        )
        == []
    )
