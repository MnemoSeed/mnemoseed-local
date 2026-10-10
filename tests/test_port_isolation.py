"""Static enforcement of the reserved-port invariant, plus the child-source pins.

The suite refuses a reserved port nowhere at run time: nothing patches the
socket module, and a session that starts while the installed daemon already
holds a reserved port is reported, never blocked. What stands in place of the
runtime guard is this file — a scan of the test sources for a bind, a dial or a
datagram transmit that names a reserved port, and a scan of the non-Python child
scripts for the live network primitives they must not reach.

The scan matches a PowerShell command name whatever its case, because
PowerShell command names are case-insensitive, and it still reads only the
files in the tree under the suffixes it knows: a source a test writes at run
time is not in it, and a source under any other suffix is not either. Neither
scan is an enforced boundary at the syscall; each is a pin that fails the suite
when a test source drifts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _ports import RESERVED_PORTS

TESTS = Path(__file__).resolve().parent
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

#: Every entry point that can name a destination port, including the asyncio
#: forms an in-process guard never saw.
NETWORK_PRIMITIVES = (
    "bind",
    "connect",
    "connect_ex",
    "sendto",
    "create_connection",
    "open_connection",
    "create_datagram_endpoint",
)
#: The one site allowed to bind a reserved port: the session precondition's own
#: occupancy probe. Everything else in the tree must reach the network, if at
#: all, through a port the kernel chose.
ALLOWED_RESERVED_BIND_SITES = frozenset({("_ports.py", "reserved_port_occupancy")})


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


def _bound_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    return []


def _bindings(tree: ast.Module) -> dict[str, list[ast.expr]]:
    """Every name the module binds, with the expressions bound to it.

    Scopes are not separated: a name a function binds to a reserved port is a
    reserved name for the whole file, which is the conservative direction for a
    scan that refuses a bind instead of auditing one.
    """
    bindings: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets, value = [node.target], node.iter
        else:
            continue
        for target in targets:
            for name in _bound_names(target):
                bindings.setdefault(name, []).append(value)
    return bindings


def _names_reserved_port(node: ast.expr, resolved: frozenset[str]) -> bool:
    """Whether an expression names a reserved port, through a literal or a name."""
    return any(
        (isinstance(child, ast.Constant) and child.value in RESERVED_PORTS)
        or (isinstance(child, ast.Name) and child.id in resolved)
        for child in ast.walk(node)
    )


def _reserved_names(bindings: dict[str, list[ast.expr]]) -> frozenset[str]:
    """The module's names that hold a reserved port, transitively."""
    resolved: set[str] = set()
    growing = True
    while growing:
        growing = False
        for name, values in bindings.items():
            if name in resolved:
                continue
            resolved_names = frozenset(resolved)
            if any(_names_reserved_port(value, resolved_names) for value in values):
                resolved.add(name)
                growing = True
    return frozenset(resolved)


def _primitive_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"


def reserved_port_violations(
    root: Path,
    *,
    allowed: frozenset[tuple[str, str]] = ALLOWED_RESERVED_BIND_SITES,
) -> dict[str, list[str]]:
    """Every test source that names a reserved port on a network primitive.

    A call counts only when an argument names the port, through the literal
    itself or through a name the module binds to one; a port the kernel chose is
    never reported. Each entry names the file relative to the root and the
    reported sites as ``line <n> <primitive>() in <site>``.
    """
    violations: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        reserved = _reserved_names(_bindings(tree))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _primitive_name(node.func)
            if name not in NETWORK_PRIMITIVES:
                continue
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            if not any(_names_reserved_port(argument, reserved) for argument in arguments):
                continue
            site = _enclosing_function(parents, node)
            if (relative, site) in allowed:
                continue
            found.append(f"line {node.lineno} {name}() in {site}")
        if found:
            violations[relative] = found
    return violations


def test_no_test_source_names_a_reserved_port_on_a_network_primitive() -> None:
    assert reserved_port_violations(TESTS) == {}


def test_the_static_check_reports_a_source_that_dials_a_reserved_port(tmp_path: Path) -> None:
    source = "import socket\n\nprobe = socket.socket()\nprobe.connect(('127.0.0.1', 7788))\n"
    (tmp_path / "offender.py").write_text(source, encoding="utf-8")

    assert reserved_port_violations(tmp_path) == {"offender.py": ["line 4 connect() in <module>"]}


def test_the_static_check_reports_a_source_that_binds_a_reserved_named_constant(tmp_path: Path) -> None:
    source = "DAEMON_PORT = 7788\n\nlistener.bind(('127.0.0.1', DAEMON_PORT))\n"
    (tmp_path / "named.py").write_text(source, encoding="utf-8")

    assert reserved_port_violations(tmp_path) == {"named.py": ["line 3 bind() in <module>"]}


def test_the_static_check_sees_through_a_name_built_from_the_reserved_set(tmp_path: Path) -> None:
    source = "PORT = 4096\n\nfor port in (PORT,):\n    probe.sendto(b'', ('127.0.0.1', port))\n"
    (tmp_path / "iterator.py").write_text(source, encoding="utf-8")

    assert reserved_port_violations(tmp_path) == {"iterator.py": ["line 4 sendto() in <module>"]}


def test_the_static_check_leaves_a_kernel_chosen_port_alone(tmp_path: Path) -> None:
    source = "import socket\n\nwith socket.socket() as probe:\n    probe.bind(('127.0.0.1', 0))\n"
    (tmp_path / "spare.py").write_text(source, encoding="utf-8")

    assert reserved_port_violations(tmp_path) == {}


def test_the_allow_list_holds_nothing_but_the_occupancy_probe() -> None:
    """The scan reports exactly the allow-listed site once the allow list is lifted."""
    lifted = reserved_port_violations(TESTS, allowed=frozenset())

    assert list(lifted) == ["_ports.py"], lifted
    assert len(lifted["_ports.py"]) == 1, lifted
    assert "bind() in reserved_port_occupancy" in lifted["_ports.py"][0], lifted


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
