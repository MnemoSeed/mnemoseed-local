"""The in-repo daemon-caller credential guard.

The daemon surface refuses every request that cannot present the credential, so a
consumer outside the product source has to obtain the value from the daemon's own
config home and attach it as a bearer header. The scripts that stand up an
isolated daemon are exactly such consumers.

This module scans the in-repo caller surface — the executable scripts, the
feature documents, and the workflow definitions, minus the vendored design-skill
bundle and the pinned documentation suffixes — and fails when a caller reaches a
protected path without obtaining the credential and attaching it. Four rules give
the scan its shape:

1. **Obtained and attached.** A caller that reaches a protected path obtains the
   credential through an acquisition seam, and every call site that carries a
   protected path presents it there: the header set built from it, the credential
   itself, or a client constructed to hold it. A text caller presents it anywhere
   in the file, which is the granularity a script that builds its header set once
   and then dials is written at.
2. **URL-shaped text.** The text scan reads the paths a caller dials from the
   daemon's own authority — a loopback URL, or a variable that declares one — and
   judges each of them against the open set, so an interpolated
   ``"$DaemonUrl/memory/remember"`` and a literal
   ``"http://127.0.0.1:7788/mcp"`` are both seen. A protected route segment named
   anywhere else is seen too, a hostname's last character included. A text caller
   that dials the daemon without showing the path it dials is a caller the scan
   cannot read, and that fails rather than passes.
3. **The whole caller surface.** Callers are scanned by suffix wherever they
   live: under the caller roots, and at the repository root itself.
4. **Documentation is not a caller.** Prose may quote an API path; the pinned
   documentation suffixes are never scanned, so no document has to carry marker
   words to stay out of the scan.

A registry pins the known-good classification of every scanned file, so the scan
has teeth in both directions: a caller that stops presenting the credential, a
newly added caller, and a deleted caller each fail it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from mnemoseed_local.daemon.auth import OPEN_PATHS, console_documents

REPO_ROOT = Path(__file__).resolve().parents[1]

CALLER_ROOTS = ("scripts", "features", ".github")

#: Executable suffixes scanned at the repository root itself, where a consumer
#: belongs to no package directory (the boundary probe, the installers).
EXECUTABLE_SUFFIXES = frozenset({".py", ".ps1", ".sh"})

#: Documentation suffixes: prose may quote a daemon path but never dials one. The
#: list is pinned, so the exempt surface changes only by a deliberate edit here.
DOCUMENTATION_SUFFIXES = frozenset({".md"})

#: The vendored design-skill bundle: it serves its own local server with its own
#: token, and is never a consumer of the daemon surface.
VENDORED_PARTS = ("skills/impeccable", "agents/impeccable-", "hooks/impeccable.json")

#: The credential obtainment seams. A caller that reaches a protected path
#: through any other shape is a hole in the boundary.
ACQUISITION_CALLS = frozenset(
    {
        "read_credential",
        "read_credential_at",
        "read_credential_or_none",
        "credential_headers",
        "client_for",
    }
)
ACQUISITION_MARKERS = (*sorted(ACQUISITION_CALLS), "Authorization", "Bearer")

#: The client constructor that carries the credential into every request it makes;
#: ``client_for`` is an acquisition seam, counted with those above.
CLIENT_CONSTRUCTORS = frozenset({"DaemonClient"})

#: The HTTP call shapes a text consumer uses to reach a daemon surface.
HTTP_CALL_MARKERS = ("Invoke-RestMethod", "Invoke-WebRequest", "curl", "wget", "fetch(")

#: Header-shaped literals: the presentation a protected call site carries.
PRESENTATION_MARKERS = ("Authorization", "Bearer")

#: Protected route segments of the daemon surface, matched wherever they appear.
#: The open set needs no prefix: those paths are literal in every consumer. A
#: segment matches whatever precedes it, a hostname's last character included;
#: the one excluded predecessor is the console's own hash fragment.
_PROTECTED_TEXT = re.compile(
    r"(?<![#])/(?:memory|session|ingest|config|daemon|flush)(?:/[A-Za-z0-9_-]+)*(?![\w.-])"
)

#: The daemon's own authority in a text caller: a loopback URL, or a variable
#: that declares one. Only what follows such an authority is a path that caller
#: dials, which keeps another loopback service out of the surface.
_DAEMON_AUTHORITY = re.compile(
    r"https?://(?:127(?:\.\d+){3}|localhost|\[::1\])(?::\d+)?|\$\{?\w*[Dd]aemon\w*\}?"
)

#: The path a text caller dials from that authority, interpolation included.
_DIALED_PATH = re.compile(r"(?:/[A-Za-z0-9_.{}$-]+)+")

#: The shape of a daemon route literal: every shipped route is lowercase, which
#: keeps command flags such as ``/PID`` out of the scan.
_ROUTE_SHAPE = re.compile(r"^/[a-z0-9][a-z0-9/_-]*$")

CREDENTIALED = "credentialed"
OPEN_SET = "open-set"
PROTECTED_WITHOUT_CREDENTIAL = "protected-without-credential"

#: The known-good caller set: every in-repo caller file pinned to its class.
KNOWN_CALLERS: dict[str, str] = {
    ".github/FUNDING.yml": OPEN_SET,
    ".github/ISSUE_TEMPLATE/queue-issue.yml": OPEN_SET,
    ".github/workflows/ci.yml": OPEN_SET,
    ".github/workflows/release.yml": OPEN_SET,
    "install.sh": OPEN_SET,
    "install.ps1": OPEN_SET,
    "probe_browser_boundary.py": CREDENTIALED,
    "scripts/__init__.py": OPEN_SET,
    "scripts/atlas_preflight.py": OPEN_SET,
    "scripts/atlas_verify.py": CREDENTIALED,
    "scripts/check_changed_comments.py": OPEN_SET,
    "scripts/gate.ps1": OPEN_SET,
    "scripts/probe_mcp_http.py": CREDENTIALED,
    "scripts/sanitize_atlas_evidence.py": OPEN_SET,
    "scripts/windows-install-helpers.ps1": OPEN_SET,
    "scripts/windows-logon-task.ps1": OPEN_SET,
    "scripts/windows-logon.ps1": OPEN_SET,
}


@dataclass(frozen=True)
class CallerScan:
    """What one caller file reaches, and what it presents there."""

    hits: tuple[str, ...]
    acquires: bool
    unattached: tuple[str, ...] = ()
    dialed: tuple[str, ...] = ()
    dials: tuple[str, ...] = ()
    unreadable: bool = False


@dataclass(frozen=True)
class CredentialSeam:
    """How one Python file hands the credential on: the locals bound to it, and
    the module-local helpers that read it."""

    identifiers: frozenset[str]
    producers: frozenset[str]


def _caller_files(root: Path = REPO_ROOT) -> dict[str, Path]:
    callers: dict[str, Path] = {}
    for caller_root in CALLER_ROOTS:
        base = root / caller_root
        if base.is_dir():
            _collect(root, base.rglob("*"), callers)
    _collect(root, (path for path in root.iterdir() if path.suffix in EXECUTABLE_SUFFIXES), callers)
    return callers


def _collect(root: Path, paths: Iterable[Path], callers: dict[str, Path]) -> None:
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in relative or any(part in relative for part in VENDORED_PARTS):
            continue
        if path.suffix in DOCUMENTATION_SUFFIXES:
            continue
        callers[relative] = path


def _route_path(literal: str) -> str | None:
    """The route a literal addresses: a bare path, or the path of an inline URL."""
    if literal.startswith(("http://", "https://")):
        return urlsplit(literal).path or None
    return literal if literal.startswith("/") else None


def _protected_route(literal: str, open_paths: frozenset[str]) -> str | None:
    """The protected route a literal addresses, else None."""
    route = _route_path(literal)
    if route is not None and _ROUTE_SHAPE.match(route) and route not in open_paths:
        return route
    return None


def _protected_routes(node: ast.AST, open_paths: frozenset[str]) -> set[str]:
    """The protected routes the string literals under one node address."""
    routes: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            route = _protected_route(item.value, open_paths)
            if route is not None:
                routes.add(route)
    return routes


def _text_protected_hits(source: str) -> set[str]:
    """The protected route segments a text caller names, whatever precedes them."""
    return set(_PROTECTED_TEXT.findall(source))


def _callee_name(call: ast.Call) -> str:
    target = call.func
    return target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")


def _is_acquisition(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Call) and _callee_name(node) in ACQUISITION_CALLS


def _credential_producers(tree: ast.AST) -> frozenset[str]:
    """Module-local functions whose body hands back an acquisition call's value."""
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(isinstance(item, ast.Return) and _is_acquisition(item.value) for item in ast.walk(node))
    )


def _carries_credential(value: ast.expr, seam: CredentialSeam) -> bool:
    """Whether an expression's value carries the credential: the acquired value,
    the header set built from it, or a client constructed to hold either."""
    if isinstance(value, ast.Name):
        return value.id in seam.identifiers
    if not isinstance(value, ast.Call):
        return False
    called = _callee_name(value)
    if called in ACQUISITION_CALLS or called in seam.producers:
        return True
    if called not in CLIENT_CONSTRUCTORS:
        return False
    return any(
        keyword.arg == "token" and _carries_credential(keyword.value, seam) for keyword in value.keywords
    )


def _credential_seam(tree: ast.AST) -> CredentialSeam:
    """The locals and module-local helpers through which the credential travels."""
    producers = _credential_producers(tree)
    bindings = [
        (node.targets[0].id, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    ]
    identifiers: set[str] = set()
    seam = CredentialSeam(frozenset(), producers)
    growing = True
    while growing:
        growing = False
        seam = CredentialSeam(frozenset(identifiers), producers)
        for name, value in bindings:
            if name not in identifiers and _carries_credential(value, seam):
                identifiers.add(name)
                growing = True
    return CredentialSeam(frozenset(identifiers), producers)


def _presents_credential(call: ast.Call, seam: CredentialSeam) -> bool:
    """Whether one call site carries the credential in its own arguments, or is a
    request a credential-carrying client makes."""
    for node in ast.walk(call):
        if isinstance(node, ast.Name) and node.id in seam.identifiers:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(marker in node.value for marker in PRESENTATION_MARKERS):
                return True
    return isinstance(call.func, ast.Attribute) and _carries_credential(call.func.value, seam)


def _python_unattached_calls(
    tree: ast.AST, seam: CredentialSeam, open_paths: frozenset[str]
) -> tuple[str, ...]:
    """The call sites that carry a protected path and present nothing there."""
    unattached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and not _presents_credential(node, seam):
            if _protected_routes(node, open_paths):
                unattached.add(_callee_name(node) or "<call>")
    return tuple(sorted(unattached))


def _python_acquires_credential(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call) and _callee_name(node) in ACQUISITION_CALLS for node in ast.walk(tree)
    )


def _text_acquires_credential(source: str) -> bool:
    return any(marker in source for marker in ACQUISITION_MARKERS)


def _text_dials(source: str) -> tuple[str, ...]:
    """The HTTP call shapes this text file uses to reach a daemon surface."""
    return tuple(marker for marker in HTTP_CALL_MARKERS if marker in source)


def _text_dialed_routes(source: str) -> set[str]:
    """The paths this text file dials from the daemon's own authority."""
    routes: set[str] = set()
    for authority in _DAEMON_AUTHORITY.finditer(source):
        path = _DIALED_PATH.match(source, authority.end())
        if path is not None:
            routes.add(path.group())
    return routes


def _scan(path: Path) -> CallerScan:
    """What one caller file reaches, and what it presents there."""
    source = path.read_text(encoding="utf-8")
    open_paths = OPEN_PATHS | console_documents()
    if path.suffix == ".py":
        tree = ast.parse(source)
        seam = _credential_seam(tree)
        return CallerScan(
            hits=tuple(sorted(_protected_routes(tree, open_paths))),
            acquires=_python_acquires_credential(tree),
            unattached=_python_unattached_calls(tree, seam, open_paths),
        )
    dialed = _text_dialed_routes(source)
    dials = _text_dials(source)
    return CallerScan(
        hits=tuple(
            sorted(_text_protected_hits(source) | {route for route in dialed if route not in open_paths})
        ),
        acquires=_text_acquires_credential(source),
        dialed=tuple(sorted(dialed)),
        dials=dials,
        unreadable=bool(dials) and _DAEMON_AUTHORITY.search(source) is not None and not dialed,
    )


def _classify(scan: CallerScan) -> str:
    """The class one caller file earns from its own contents."""
    if not scan.hits:
        return PROTECTED_WITHOUT_CREDENTIAL if scan.unreadable else OPEN_SET
    if scan.acquires and not scan.unattached:
        return CREDENTIALED
    return PROTECTED_WITHOUT_CREDENTIAL


def _classification(relative: str) -> str:
    return _classify(_scan(REPO_ROOT / relative))


def _describe(relative: str, scan: CallerScan) -> str:
    return (
        f"{relative} -> hits={list(scan.hits)} acquires={scan.acquires} "
        f"unattached={list(scan.unattached)} dialed={list(scan.dialed)} "
        f"dials={list(scan.dials)} unreadable={scan.unreadable}"
    )


def test_no_caller_reaches_a_protected_path_without_a_credential():
    holes: dict[str, CallerScan] = {}
    for relative, path in _caller_files().items():
        scan = _scan(path)
        if _classify(scan) == PROTECTED_WITHOUT_CREDENTIAL:
            holes[relative] = scan
    detail = "; ".join(_describe(relative, scan) for relative, scan in sorted(holes.items()))
    assert not holes, f"daemon caller reaching a protected path without a credential: {detail}"


def test_known_caller_registry_matches_the_tree():
    scanned = _caller_files()
    unregistered = sorted(set(scanned) - set(KNOWN_CALLERS))
    missing = sorted(set(KNOWN_CALLERS) - set(scanned))
    assert set(scanned) == set(KNOWN_CALLERS), (
        f"the in-repo caller surface changed; classify each file in KNOWN_CALLERS as "
        f"{CREDENTIALED!r} or {OPEN_SET!r} (documentation suffixes are never scanned): "
        f"unregistered={unregistered} missing={missing}"
    )
    mismatches = {
        relative: (expected, _classification(relative))
        for relative, expected in KNOWN_CALLERS.items()
        if _classification(relative) != expected
    }
    detail = "; ".join(
        f"{relative} expected {expected} but scanned as {actual}"
        for relative, (expected, actual) in mismatches.items()
    )
    assert not mismatches, f"caller classification moved away from the pinned known-good set: {detail}"


# ---------------------------------------------------------------- per call site

#: A caller that obtains the credential and attaches it at the protected call.
_ATTACHED_CALLER = (
    "import httpx\n"
    "from mnemoseed_local.credential import read_credential_at\n"
    "from mnemoseed_local.rest_client import credential_headers\n"
    "credential = read_credential_at('home')\n"
    "headers = credential_headers(credential)\n"
    "httpx.post('http://127.0.0.1:1/memory/remember', json={}, headers=headers)\n"
)

#: The same caller with the header argument dropped at the protected call.
_DETACHED_CALLER = (
    "import httpx\n"
    "from mnemoseed_local.credential import read_credential_at\n"
    "from mnemoseed_local.rest_client import credential_headers\n"
    "credential = read_credential_at('home')\n"
    "headers = credential_headers(credential)\n"
    "httpx.post('http://127.0.0.1:1/memory/remember', json={})\n"
)

#: A caller that hands the credential to the client, which presents it itself.
_CLIENT_CARRIER = (
    "from mnemoseed_local.credential import read_credential_at\n"
    "from mnemoseed_local.rest_client import DaemonClient\n"
    "credential = read_credential_at('home')\n"
    "client = DaemonClient(base_url='http://127.0.0.1:1', token=credential)\n"
    "client.post('/memory/recall', {})\n"
)

#: The same caller with the client construction left tokenless.
_TOKENLESS_CARRIER = (
    "from mnemoseed_local.credential import read_credential_at\n"
    "from mnemoseed_local.rest_client import DaemonClient\n"
    "credential = read_credential_at('home')\n"
    "client = DaemonClient(base_url='http://127.0.0.1:1')\n"
    "client.post('/memory/recall', {})\n"
)

#: A caller whose credential comes from a module-local helper of its own.
_HELPER_ISSUED = (
    "from mnemoseed_local.credential import read_credential_at\n"
    "from mnemoseed_local.rest_client import DaemonClient\n"
    "def issued(home):\n"
    "    return read_credential_at(home)\n"
    "credential = issued('home')\n"
    "client = DaemonClient(base_url='http://127.0.0.1:1', token=credential)\n"
    "client.post('/memory/recall', {})\n"
)


def _caller_file(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_a_protected_call_site_must_reference_the_credential(tmp_path: Path) -> None:
    """Obtaining the credential is half the caller's job: the call that carries a
    protected path has to reference it in its own arguments too."""
    attached = _caller_file(tmp_path, "attached.py", _ATTACHED_CALLER)
    detached = _caller_file(tmp_path, "detached.py", _DETACHED_CALLER)

    assert _classify(_scan(attached)) == CREDENTIALED
    assert _classify(_scan(detached)) == PROTECTED_WITHOUT_CREDENTIAL


def test_a_client_constructed_with_the_credential_carries_it_to_every_call(tmp_path: Path) -> None:
    """A client built with the credential presents it on every request it makes,
    so its protected calls need no argument of their own."""
    carrier = _caller_file(tmp_path, "carrier.py", _CLIENT_CARRIER)
    tokenless = _caller_file(tmp_path, "tokenless.py", _TOKENLESS_CARRIER)

    assert _classify(_scan(carrier)) == CREDENTIALED
    assert _classify(_scan(tokenless)) == PROTECTED_WITHOUT_CREDENTIAL


def test_a_credential_read_by_a_module_local_helper_attaches_at_the_call(tmp_path: Path) -> None:
    """A helper that reads the credential is still the seam: the value it hands
    back has to reach the protected call."""
    issued = _caller_file(tmp_path, "helper_issued.py", _HELPER_ISSUED)
    dropped = _caller_file(
        tmp_path, "helper_dropped.py", _HELPER_ISSUED.replace("token=credential", "token=''")
    )

    assert _classify(_scan(issued)) == CREDENTIALED
    assert _classify(_scan(dropped)) == PROTECTED_WITHOUT_CREDENTIAL


# ---------------------------------------------------------------- the text surface

#: Text callers the scan must see through the URL they dial: an interpolated
#: PowerShell route, a literal curl invocation, a declared YAML route, a route
#: outside the protected prefixes, and a caller that keeps its whole path variable.
_TEXT_CALLER_SHAPES = (
    ("dial.ps1", 'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember" -Method Post\n'),
    ("dial.sh", "curl http://127.0.0.1:7788/memory/remember\n"),
    ("dial.yml", 'url: "http://127.0.0.1:7788/memory/remember"\n'),
    ("dial-mcp.ps1", 'Invoke-RestMethod -Uri "http://127.0.0.1:7788/mcp" -Method Post\n'),
    ("hidden.ps1", "Invoke-RestMethod -Uri ($DaemonUrl + $Route) -Method Post\n"),
)


@pytest.mark.parametrize(("filename", "source"), _TEXT_CALLER_SHAPES)
def test_a_text_caller_is_judged_by_the_url_it_dials(filename: str, source: str, tmp_path: Path) -> None:
    """A protected route is read wherever a text caller dials it, a hostname's
    last character and a whole variable path included."""
    caller = _caller_file(tmp_path, filename, source)

    assert _classify(_scan(caller)) == PROTECTED_WITHOUT_CREDENTIAL


def test_a_text_caller_that_only_dials_the_open_set_stays_open(tmp_path: Path) -> None:
    """The liveness probe needs no credential, so a text caller that dials nothing
    else stays on the open side of the boundary."""
    probe = _caller_file(tmp_path, "probe.ps1", 'Invoke-RestMethod -Uri "$DaemonUrl/healthz" -TimeoutSec 2\n')

    assert _classify(_scan(probe)) == OPEN_SET


def test_a_text_caller_that_dials_another_loopback_service_stays_open(tmp_path: Path) -> None:
    """A text caller that dials a loopback service that is not the daemon is not a
    consumer of the daemon surface."""
    probe = _caller_file(
        tmp_path,
        "other.ps1",
        'Invoke-RestMethod -Uri "$OllamaUrl/api/tags"\nInvoke-RestMethod -Uri "$DaemonUrl/healthz"\n',
    )

    assert _classify(_scan(probe)) == OPEN_SET


def test_a_text_caller_that_dials_a_protected_route_presents_the_credential(tmp_path: Path) -> None:
    """A PowerShell consumer that does present the header is a credentialed
    caller, not a hole."""
    presenting = _caller_file(
        tmp_path,
        "presenting.ps1",
        'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember" -Headers (credential_headers $Credential)\n',
    )

    assert _classify(_scan(presenting)) == CREDENTIALED


# ---------------------------------------------------------------- the scan surface


def _synthetic_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    return root


def test_a_root_level_executable_is_scanned_as_a_caller(tmp_path: Path) -> None:
    """A consumer at the repository root has no package directory to live in, so
    the scan reaches it by executable suffix rather than by a pinned list."""
    root = _synthetic_root(tmp_path)
    (root / "probe_root.py").write_text(
        "import httpx\nhttpx.post('http://127.0.0.1:1/memory/remember', json={})\n",
        encoding="utf-8",
    )

    scanned = _caller_files(root)
    assert "probe_root.py" in scanned
    assert _classify(_scan(scanned["probe_root.py"])) == PROTECTED_WITHOUT_CREDENTIAL


def test_a_documentation_suffix_is_never_scanned_as_a_caller(tmp_path: Path) -> None:
    """Prose may quote an API path; the pinned documentation suffixes are exempt,
    so no document has to carry marker words to stay out of the scan."""
    root = _synthetic_root(tmp_path)
    (root / "scripts" / "caller.ps1").write_text(
        'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember"\n', encoding="utf-8"
    )
    (root / "scripts" / "notes.md").write_text(
        'Example: curl "$DaemonUrl/memory/remember" presents the bearer header.\n', encoding="utf-8"
    )

    scanned = _caller_files(root)
    assert "scripts/caller.ps1" in scanned
    assert "scripts/notes.md" not in scanned
