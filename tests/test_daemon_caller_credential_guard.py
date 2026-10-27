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
   protected path presents it there: a call that dials presents it under a header
   key the daemon reads — an ``Authorization`` or cookie key, a header set the
   acquisition seam or a module-local producer builds, a bearer literal, or an
   unpack the scan cannot read — a call that never dials presents it anywhere in
   its own arguments, and a text caller presents it on the statement it dials
   with. A call site that carries a protected path and presents nothing is
   reported by its callee.
2. **URL-shaped text.** The text scan reads the paths a caller dials from the
   daemon's own authority — a loopback URL, or a variable that declares one — and
   judges each of them against the open set, so an interpolated
   ``"$DaemonUrl/memory/remember"`` and a literal
   ``"http://127.0.0.1:7788/mcp"`` are both seen. A protected route segment named
   anywhere else is seen too, a hostname's last character included. A text caller
   that dials the daemon without showing the path it dials is a caller the scan
   cannot read, and that fails rather than passes.
3. **The whole caller surface.** Callers are scanned by suffix wherever they
   live: under the caller roots, and at the repository root itself, where the
   pinned suffix table is the layer's whole reach.
4. **Documentation is not a caller.** Prose may quote an API path; the pinned
   documentation suffixes are never scanned, so no document has to carry marker
   words to stay out of the scan.
5. **Routes, not path-shaped noise.** A literal that addresses a filesystem
   location or a command switch is not a route, so a classification a reader
   inspects reports only what the caller reaches.

Two shapes are limits the scan records rather than holes. An opaque helper that
hands its argument straight back (``def passthrough(value): return value``) is a
call whose return value the scan cannot type-check, so a header position it fills
counts as presenting the credential. A mention inside a comment is not read at all,
whatever comment syntax the caller's language has, which is what keeps prose out of
the scan.

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
#: belongs to no package directory (the boundary probe, the installers). The set
#: is pinned by the registry test, so a consumer language joins the scan surface
#: only by a deliberate edit here.
EXECUTABLE_SUFFIXES = frozenset(
    {
        ".bat",
        ".cjs",
        ".cmd",
        ".js",
        ".jsx",
        ".mjs",
        ".ps1",
        ".psm1",
        ".py",
        ".sh",
        ".ts",
        ".tsx",
    }
)

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

#: The acquisition seams that hand back the credential itself, as opposed to a
#: header set or a client built from it. A caller that hands one of these straight
#: into a header slot presents a value the daemon cannot read there.
RAW_ACQUISITION_CALLS = frozenset({"read_credential", "read_credential_at", "read_credential_or_none"})

#: The header keys the daemon reads a credential from: the bearer header, and the
#: console's same-origin grant cookie. A header set that keys the credential under
#: anything else reaches the protected path unauthenticated.
CREDENTIAL_KEYS = re.compile(r"authorization|cookie", re.I)

#: The client constructor that carries the credential into every request it makes;
#: ``client_for`` is an acquisition seam, counted with those above.
CLIENT_CONSTRUCTORS = frozenset({"DaemonClient"})

#: The HTTP call shapes a text consumer uses to reach a daemon surface.
HTTP_CALL_MARKERS = ("Invoke-RestMethod", "Invoke-WebRequest", "curl", "wget", "fetch(")

#: The callees of a call that dials an HTTP surface. Such a call reads the
#: credential from a header, so only a header position presents it there.
DIALING_CALLEES = frozenset(
    {"delete", "get", "head", "options", "patch", "post", "put", "request", "urlopen"}
)

#: The keyword arguments that carry a request's header set.
HEADER_KEYWORDS = frozenset({"headers"})

#: Header-shaped flags a text consumer passes on the statement it dials with.
HEADER_FLAGS = re.compile(r"-{1,2}[Hh]eaders?\b|-H\b")

#: Comment syntax of the text suffixes that carry one: the line marker, and the
#: block markers when the language has them. A marker inside a quoted value is not
#: a comment, so a dialled URL stays readable.
COMMENT_MARKERS: dict[str, tuple[str | None, tuple[str, str] | None]] = {
    ".cjs": ("//", ("/*", "*/")),
    ".js": ("//", ("/*", "*/")),
    ".jsx": ("//", ("/*", "*/")),
    ".mjs": ("//", ("/*", "*/")),
    ".ps1": ("#", ("<#", "#>")),
    ".psm1": ("#", ("<#", "#>")),
    ".sh": ("#", None),
    ".ts": ("//", ("/*", "*/")),
    ".tsx": ("//", ("/*", "*/")),
    ".yaml": ("#", None),
    ".yml": ("#", None),
}

#: The suffixes whose shell has no comment marker mid-line: it reads a leading
#: keyword as a comment instead, and separates statements with a single ``&``.
CMD_COMMENT_SUFFIXES = frozenset({".bat", ".cmd"})
CMD_COMMENT_STATEMENT = re.compile(r"^(?:rem\b|::)", re.I)

#: The separators that end one text statement and begin the next: a shell's command
#: list separator, or a pipeline.
STATEMENT_SEPARATORS = ("&&", "||", ";", "|")
CMD_STATEMENT_SEPARATORS = ("&&", "||", "|", "&")

#: A text statement that continues on the next physical line.
CONTINUED_STATEMENT = re.compile(r"[\\`]\s*$")

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

#: Path roots that address something other than the daemon surface: the
#: filesystem's own top-level directories. None of them is a route root, and a
#: one-letter root is a command switch (``cmd /c``) or a drive letter.
NON_ROUTE_ROOTS = frozenset(
    {
        "bin",
        "boot",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "media",
        "mnt",
        "opt",
        "private",
        "proc",
        "root",
        "run",
        "sbin",
        "srv",
        "sys",
        "tmp",
        "usr",
        "users",
        "var",
        "volumes",
        "windows",
    }
)

#: A literal URL a text statement dials: where a dial goes when the caller shows it.
_LITERAL_URL = re.compile(r"https?://[^\s'\"`]+")

#: A variable reference, as a shell or a PowerShell writes one, a scope prefix
#: included.
_VARIABLE = re.compile(r"\$(?:(?:env|global|local|private|script):)?(?P<name>\w+)")

#: Variable assignments and parameter declarations used to resolve target values.
_SHELL_BINDING = re.compile(r"^\s*(?:export\s+)?(?P<name>\w+)=(?P<value>.*)$", re.M)
_SIGIL_BINDING = re.compile(r"\$(?P<name>\w+)\s*=(?!=)(?P<value>[^\n]*)")
_PARAMETER_BLOCK = re.compile(r"\bparam\s*\((?P<body>[^()]*)\)", re.I)
_FUNCTION_SIGNATURE = re.compile(r"\bfunction\s+[\w:-]+\s*\((?P<body>[^()]*)\)", re.I)

#: The PowerShell cmdlets that dial, found as an unquoted token so that a help
#: sentence or a probe's quoted argument never reads as an invocation.
_PS_DIAL = re.compile(r"\b(?:Invoke-RestMethod|Invoke-WebRequest)\b", re.I)

#: The target parameter of those cmdlets, and of a ``fetch`` call.
_PS_TARGET = re.compile(r"(?:^|\s)-Uri\b", re.I)
_FETCH_CALL = re.compile(r"\bfetch\s*\(", re.I)

#: The shell commands that dial, and the probes that only ask whether one exists.
_SHELL_DIAL_WORD = re.compile(r"\bcurl(?:\.exe)?\b|\bwget\b", re.I)
_AVAILABILITY_PROBE = re.compile(
    r"(?:\bhave|\bwhich|\btype|\bwhere|\bcommand\s+-v|\bTest-CommandExists|\bGet-Command|\bTest-Path)"
    r"\s*$",
    re.I,
)

#: The curl and wget flags whose value is never the target. The table is closed:
#: an unseen long flag fails the target closed, an unseen short one is a switch.
CURL_VALUE_FLAGS = frozenset(
    {
        "-A",
        "-b",
        "-c",
        "-d",
        "-e",
        "-E",
        "-F",
        "-H",
        "-K",
        "-m",
        "-o",
        "-T",
        "-u",
        "-w",
        "-x",
        "-X",
        "-z",
        "--cacert",
        "--cert",
        "--config",
        "--connect-timeout",
        "--cookie",
        "--cookie-jar",
        "--data",
        "--data-binary",
        "--data-raw",
        "--data-urlencode",
        "--form",
        "--header",
        "--json",
        "--key",
        "--max-time",
        "--output",
        "--proxy",
        "--referer",
        "--request",
        "--resolve",
        "--retry",
        "--retry-delay",
        "--retry-max-time",
        "--time-cond",
        "--upload-file",
        "--url",
        "--user",
        "--user-agent",
        "--write-out",
    }
)
WGET_VALUE_FLAGS = frozenset(
    {
        "-A",
        "-a",
        "-B",
        "-D",
        "-e",
        "-i",
        "-l",
        "-O",
        "-o",
        "-P",
        "-Q",
        "-t",
        "-T",
        "-U",
        "-w",
        "--accept",
        "--append-output",
        "--base",
        "--bind-address",
        "--body-data",
        "--ca-certificate",
        "--certificate",
        "--directory-prefix",
        "--domains",
        "--execute",
        "--header",
        "--input-file",
        "--level",
        "--limit-rate",
        "--output-document",
        "--output-file",
        "--password",
        "--post-data",
        "--private-key",
        "--progress",
        "--quota",
        "--timeout",
        "--tries",
        "--user",
        "--user-agent",
        "--wait",
    }
)

#: The long switches the same tools read as booleans, so a download target behind
#: one stays reachable. Closed like the value table: an unseen long flag hides the
#: target rather than guessing its shape.
CURL_SWITCHES = frozenset(
    {
        "--compressed",
        "--create-dirs",
        "--fail",
        "--fail-with-body",
        "--globoff",
        "--head",
        "--http1.1",
        "--http2",
        "--http3",
        "--include",
        "--insecure",
        "--ipv4",
        "--ipv6",
        "--junk-session-cookies",
        "--list-only",
        "--location",
        "--no-buffer",
        "--no-keepalive",
        "--no-progress-meter",
        "--path-as-is",
        "--progress-bar",
        "--raw",
        "--remote-header-name",
        "--remote-name",
        "--retry-all-errors",
        "--show-error",
        "--silent",
        "--tcp-nodelay",
        "--tlsv1.2",
        "--tlsv1.3",
        "--verbose",
    }
)
WGET_SWITCHES = frozenset(
    {
        "--content-disposition",
        "--continue",
        "--convert-links",
        "--force-directories",
        "--inet4-only",
        "--inet6-only",
        "--mirror",
        "--no-check-certificate",
        "--no-clobber",
        "--no-host-directories",
        "--no-parent",
        "--no-verbose",
        "--page-requisites",
        "--quiet",
        "--recursive",
        "--show-progress",
        "--spider",
        "--timestamping",
        "--trust-server-names",
    }
)

#: A declared shell function: the body's own arguments are its positionals.
_SHELL_FUNCTION = re.compile(r"^\s*(?:function\s+)?\w+\s*\(\)\s*\{")

#: A shell heredoc's opening marker and the delimiter that closes it.
_HEREDOC_OPEN = re.compile(r"<<-?\s*['\"]?(?P<delimiter>\w+)['\"]?")

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
    "scripts/check_commit_range.py": OPEN_SET,
    "scripts/check_changed_comments.py": OPEN_SET,
    "scripts/gate.ps1": OPEN_SET,
    "scripts/probe_mcp_http.py": CREDENTIALED,
    "scripts/sanitize_atlas_evidence.py": OPEN_SET,
    "scripts/verify_release_install.py": OPEN_SET,
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
    """How one Python file hands the credential on: the locals bound to it, the
    module-local helpers that read it, and the locals that hold the credential
    itself rather than a header set or a client built from it."""

    identifiers: frozenset[str]
    producers: frozenset[str]
    values: frozenset[str]


@dataclass(frozen=True)
class TargetBindings:
    """How a text caller names the target it dials: the parameters its blocks
    declare, and the variables it binds to a literal URL."""

    declared: frozenset[str]
    url_bound: frozenset[str]


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


def _addresses_the_daemon(route: str) -> bool:
    """Whether a route-shaped path addresses the daemon surface rather than the
    filesystem or a command switch."""
    root = route.split("/")[1]
    return len(root) > 1 and root not in NON_ROUTE_ROOTS


def _protected_route(literal: str, open_paths: frozenset[str]) -> str | None:
    """The protected route a literal addresses, else None."""
    route = _route_path(literal)
    if route is not None and _ROUTE_SHAPE.match(route) and route not in open_paths:
        return route if _addresses_the_daemon(route) else None
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


def _returning_helpers(tree: ast.AST) -> tuple[tuple[str, ast.expr], ...]:
    """Every module-local helper with an expression it hands back: a function or an
    async function by each of its returns, and a lambda by the name it is bound to."""
    helpers: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            helpers.extend(
                (node.name, item.value)
                for item in ast.walk(node)
                if isinstance(item, ast.Return) and item.value is not None
            )
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Lambda)
        ):
            helpers.append((node.targets[0].id, node.value.body))
    return tuple(helpers)


def _credential_producers(tree: ast.AST) -> frozenset[str]:
    """Module-local helpers whose body hands back an acquisition call's value."""
    return frozenset(name for name, value in _returning_helpers(tree) if _is_acquisition(value))


def _credential_sources(tree: ast.AST) -> frozenset[str]:
    """Module-local helpers whose body hands back the credential itself."""
    return frozenset(
        name
        for name, value in _returning_helpers(tree)
        if isinstance(value, ast.Call) and _callee_name(value) in RAW_ACQUISITION_CALLS
    )


def _is_credential_value(value: ast.expr, values: frozenset[str], sources: frozenset[str]) -> bool:
    """Whether one expression's value is the credential itself, rather than a header
    set or a client built from it."""
    if isinstance(value, ast.Name):
        return value.id in values
    if not isinstance(value, ast.Call):
        return False
    called = _callee_name(value)
    return called in RAW_ACQUISITION_CALLS or called in sources


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
    sources = _credential_sources(tree)
    bindings = [
        (node.targets[0].id, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    ]
    identifiers: set[str] = set()
    values: set[str] = set()
    growing = True
    while growing:
        growing = False
        seam = CredentialSeam(frozenset(identifiers), producers, frozenset(values))
        for name, value in bindings:
            if name not in identifiers and _carries_credential(value, seam):
                identifiers.add(name)
                growing = True
            if name not in values and _is_credential_value(value, frozenset(values), sources):
                values.add(name)
                growing = True
    return CredentialSeam(frozenset(identifiers), producers, frozenset(values))


def _names_the_credential(node: ast.AST, seam: CredentialSeam) -> bool:
    """Whether an expression's own subtree names the credential's seam value, or
    carries a header-shaped literal."""
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and item.id in seam.identifiers:
            return True
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            if any(marker in item.value for marker in PRESENTATION_MARKERS):
                return True
    return False


def _dials_http(call: ast.Call) -> bool:
    """Whether one call site dials an HTTP surface, which reads the credential
    from a header rather than from any other argument."""
    return _callee_name(call) in DIALING_CALLEES


def _header_key(key: ast.expr | None) -> str | None:
    """The text of one header key, as far as it is readable."""
    if key is None:
        return None
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    if isinstance(key, ast.JoinedStr):
        return "".join(
            part.value
            for part in key.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return ""


def _header_entries(value: ast.expr) -> tuple[tuple[str | None, ast.expr], ...] | None:
    """The readable entries of a header set written as a literal or a ``dict(...)``
    call, None when the expression is not a header set at all. An entry whose key
    the scan cannot read carries a key of None."""
    if isinstance(value, ast.Dict):
        return tuple((_header_key(key), item) for key, item in zip(value.keys, value.values, strict=True))
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "dict":
        return tuple((keyword.arg, keyword.value) for keyword in value.keywords)
    return None


def _carries_credential_in_a_header_position(value: ast.expr, seam: CredentialSeam) -> bool:
    """Whether one header argument carries the credential where the daemon reads it:
    a header set under a key the daemon honours, a header set the seam built, or an
    opaque call whose return value the scan cannot type-check."""
    entries = _header_entries(value)
    if entries is not None:
        return any(
            (key is None or CREDENTIAL_KEYS.fullmatch(key) is not None)
            and (_carries_credential(item, seam) or _names_the_credential(item, seam))
            for key, item in entries
        )
    if isinstance(value, ast.Name):
        return value.id in seam.identifiers and value.id not in seam.values
    return _reaches_a_header_position(value, seam)


def _reaches_a_header_position(value: ast.expr, seam: CredentialSeam) -> bool:
    """Whether an expression that is not a header set carries the credential into a
    header position: the acquisition seam itself, a module-local producer, or a
    bearer marker."""
    if isinstance(value, ast.Call) and (
        _callee_name(value) in ACQUISITION_CALLS or _callee_name(value) in seam.producers
    ):
        return True
    return _names_the_credential(value, seam)


def _presents_in_headers(call: ast.Call, seam: CredentialSeam) -> bool:
    """Whether one dialing call presents the credential in a header position."""
    return any(
        keyword.arg is None or _carries_credential_in_a_header_position(keyword.value, seam)
        for keyword in call.keywords
        if keyword.arg is None or keyword.arg.lower() in HEADER_KEYWORDS
    )


def _presents_credential(call: ast.Call, seam: CredentialSeam) -> bool:
    """Whether one call site carries the credential in its own arguments, or is a
    request a credential-carrying client makes. A call that dials presents it in a
    header position; a call that never dials presents it wherever its callee
    reads it."""
    if isinstance(call.func, ast.Attribute) and _carries_credential(call.func.value, seam):
        return True
    if _dials_http(call):
        return _presents_in_headers(call, seam)
    return _names_the_credential(call, seam)


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


def _without_comments(source: str, suffix: str) -> str:
    """The source with its comments removed, quotes respected: a marker inside a
    dialled URL stays readable, and a block comment leaves its lines behind."""
    markers = COMMENT_MARKERS.get(suffix)
    if markers is None:
        return source
    line_marker, block = markers
    kept: list[str] = []
    quote = ""
    index = 0
    while index < len(source):
        char = source[index]
        if quote:
            kept.append(char)
            if char == "\\":
                if index + 1 < len(source):
                    kept.append(source[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if block is not None and source.startswith(block[0], index):
            end = source.find(block[1], index + len(block[0]))
            removed = source[index:] if end < 0 else source[index : end + len(block[1])]
            kept.append("\n" * removed.count("\n"))
            index += len(removed)
            continue
        if line_marker is not None and source.startswith(line_marker, index):
            end = source.find("\n", index)
            index = len(source) if end < 0 else end
            continue
        if char in "'\"`":
            quote = char
        kept.append(char)
        index += 1
    return "".join(kept)


def _separated(statement: str, suffix: str) -> tuple[str, ...]:
    """One line's statements, split at the separators that stand outside quotes."""
    separators = CMD_STATEMENT_SEPARATORS if suffix in CMD_COMMENT_SUFFIXES else STATEMENT_SEPARATORS
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(statement):
        char = statement[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
            current.append(char)
            index += 1
            continue
        separator = next((item for item in separators if statement.startswith(item, index)), None)
        if separator is None:
            current.append(char)
            index += 1
            continue
        parts.append("".join(current))
        current = []
        index += len(separator)
    parts.append("".join(current))
    return tuple(part for part in parts if part.strip())


def _is_a_comment_statement(statement: str, suffix: str) -> bool:
    """Whether one statement is a comment: a shell without a mid-line marker reads
    a leading keyword as one."""
    return suffix in CMD_COMMENT_SUFFIXES and CMD_COMMENT_STATEMENT.match(statement.strip()) is not None


def _statements(source: str, suffix: str) -> tuple[str, ...]:
    """The source's statements: comments and heredoc bodies removed, a statement
    continued over several physical lines joined into one, a line carrying several
    statements read as the statements it carries, and a comment statement dropped."""
    statements: list[str] = []
    pending = ""
    heredoc = ""
    for line in _without_comments(source, suffix).splitlines():
        if heredoc:
            if line.strip() == heredoc:
                heredoc = ""
            continue
        if suffix == ".sh":
            opening = _HEREDOC_OPEN.search(line)
            if opening is not None:
                heredoc = opening.group("delimiter")
        stripped = line.rstrip()
        continued = CONTINUED_STATEMENT.search(stripped) is not None
        body = CONTINUED_STATEMENT.sub("", stripped)
        pending = f"{pending} {body}".strip() if pending else body
        if not continued:
            statements.extend(_separated(pending, suffix))
            pending = ""
    if pending:
        statements.extend(_separated(pending, suffix))
    return tuple(
        statement.strip()
        for statement in statements
        if statement.strip() and not _is_a_comment_statement(statement, suffix)
    )


def _text_acquires_credential(source: str, suffix: str) -> bool:
    """Whether a text caller presents the credential on a statement it dials
    with."""
    return any(_presents_in_statement(body) for body in _dialing_statements(source, suffix))


def _dialing_statements(source: str, suffix: str) -> tuple[str, ...]:
    """The statements that reach a daemon surface, their comments removed."""
    return tuple(body for body in _statements(source, suffix) if _dials_a_surface(body))


def _presents_in_statement(statement: str) -> bool:
    """Whether one text statement presents the credential: a header flag, or a
    marker it carries."""
    return HEADER_FLAGS.search(statement) is not None or any(
        marker in statement for marker in ACQUISITION_MARKERS
    )


def _dials_a_surface(statement: str) -> bool:
    """Whether one statement reaches a daemon surface: a dialing command at a command
    position, or a URL from the daemon's own authority. An availability probe or a
    quoted mention names no dial."""
    masked = _mask_quoted(statement)
    if _PS_DIAL.search(masked) is not None:
        return True
    if _shell_dial(statement) is not None:
        return True
    if _FETCH_CALL.search(masked) is not None:
        return True
    return _DAEMON_AUTHORITY.search(statement) is not None


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


def _mask_quoted(text: str) -> str:
    """The text with quoted spans blanked out, their length kept: a command word or
    a cmdlet named inside prose is not an invocation, while positions stay valid."""
    kept: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            kept.append(" ")
            if char == "\\" and quote == '"' and index + 1 < len(text):
                kept.append(" ")
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
            kept.append(" ")
            index += 1
            continue
        kept.append(char)
        index += 1
    return "".join(kept)


def _word_spans(text: str) -> tuple[tuple[int, int], ...]:
    """The spans of the words one statement is made of, a quoted or bracketed group
    staying inside the word it opens."""
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        start = index
        depth = 0
        quote = ""
        while index < len(text):
            char = text[index]
            if quote:
                if char == "\\" and quote == '"' and index + 1 < len(text):
                    index += 2
                    continue
                if char == quote:
                    quote = ""
                index += 1
                continue
            if char in "'\"`":
                quote = char
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                if depth == 0:
                    index += 1
                    break
                depth -= 1
            elif char.isspace() and depth == 0:
                break
            index += 1
        spans.append((start, index))
    return tuple(spans)


def _first_argument(text: str, start: int) -> str | None:
    """The first argument of a call that opens at ``start``: the text up to the
    comma or closing bracket that ends it, nested groups included."""
    depth = 0
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(text):
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return text[start:index].strip() or None
            depth -= 1
        elif char == "," and depth == 0:
            return text[start:index].strip() or None
        index += 1
    return None


def _shell_dial(statement: str) -> re.Match[str] | None:
    """The curl or wget invocation in one statement, if it dials rather than asks
    whether the command exists."""
    masked = _mask_quoted(statement)
    for match in _SHELL_DIAL_WORD.finditer(masked):
        if _AVAILABILITY_PROBE.search(masked[: match.start()]) is None:
            return match
    return None


def _shell_target(statement: str, command: re.Match[str]) -> str | None:
    """The target operand of one curl or wget invocation: the first word that is
    neither a switch nor the value of one."""
    curl = command.group().lower().startswith("curl")
    flags = CURL_VALUE_FLAGS if curl else WGET_VALUE_FLAGS
    switches = CURL_SWITCHES if curl else WGET_SWITCHES
    base = command.end()
    words = _word_spans(statement[base:])
    index = 0
    while index < len(words):
        start, end = words[index]
        word = statement[base + start : base + end]
        index += 1
        if curl and word == "--url" and index < len(words):
            start, end = words[index]
            return statement[base + start : base + end]
        if not word.startswith("-") or word in {"-", "--"}:
            return word
        if word.startswith("--") and "=" in word:
            continue
        if word in flags:
            index += 1
        elif word.startswith("--") and word not in switches:
            return None
    return None


def _powershell_target(masked: str, statement: str, start: int) -> str | None:
    """The operand of the target parameter of one PowerShell invocation, the
    expression in that position read whole."""
    parameter = _PS_TARGET.search(masked, start)
    if parameter is None:
        return None
    words = _word_spans(statement[parameter.end() :])
    if not words:
        return None
    word_start, word_end = words[0]
    return statement[parameter.end() + word_start : parameter.end() + word_end]


def _target_operand(statement: str) -> str | None:
    """The operand that shows where one statement dials, as far as the statement's
    command shape makes it readable. Only a target position is read, so an output,
    header or body argument never shows where a call goes."""
    masked = _mask_quoted(statement)
    cmdlet = _PS_DIAL.search(masked)
    if cmdlet is not None:
        return _powershell_target(masked, statement, cmdlet.end())
    command = _shell_dial(statement)
    if command is not None:
        return _shell_target(statement, command)
    fetch = _FETCH_CALL.search(masked)
    if fetch is not None:
        return _first_argument(statement, fetch.end())
    authority = _DAEMON_AUTHORITY.search(statement)
    return authority.group() if authority is not None else None


def _target_bindings(source: str) -> TargetBindings:
    """The variables a text caller declares: the parameters its blocks and function
    signatures read, and the variables bound to a literal URL, directly or through
    one already understood."""
    names = {
        name
        for pattern in (_PARAMETER_BLOCK, _FUNCTION_SIGNATURE)
        for block in pattern.finditer(source)
        for name in _VARIABLE.findall(block.group("body"))
    }
    bound = [
        (match.group("name"), match.group("value"))
        for match in (*_SHELL_BINDING.finditer(source), *_SIGIL_BINDING.finditer(source))
    ]
    url_bound = {name for name, value in bound if _LITERAL_URL.search(value) is not None}
    growing = True
    while growing:
        growing = False
        for name, value in bound:
            if name in url_bound:
                continue
            if any(inner in names or inner in url_bound for inner in _VARIABLE.findall(value)):
                url_bound.add(name)
                growing = True
    return TargetBindings(frozenset(names), frozenset(url_bound))


def _target_is_readable(target: str | None, bindings: TargetBindings, forwards: bool) -> bool:
    """Whether one target operand shows where the call goes: a literal URL, a path or
    an authority the caller names, a parameter a block declares, a variable bound to
    a literal URL, or a positional a declared shell function forwards."""
    if not target or target.startswith("@"):
        return False
    if _LITERAL_URL.search(target) is not None or _DAEMON_AUTHORITY.search(target) is not None:
        return True
    if any(_addresses_the_daemon(path.group()) for path in _DIALED_PATH.finditer(target)):
        return True
    return any(
        name in bindings.declared
        or name in bindings.url_bound
        or (forwards and name.isdigit() and name != "0")
        for name in _VARIABLE.findall(target)
    )


def _brace_balance(statement: str) -> int:
    """The net brace depth one statement adds, quoted braces excluded."""
    masked = _mask_quoted(statement)
    return masked.count("{") - masked.count("}")


def _function_statements(statements: tuple[str, ...]) -> frozenset[int]:
    """The indices of the statements that sit inside a declared shell function, where
    the positionals are the function's own arguments."""
    inside: set[int] = set()
    depth = 0
    for index, statement in enumerate(statements):
        if depth > 0 or _SHELL_FUNCTION.match(statement) is not None:
            inside.add(index)
        depth = max(depth + _brace_balance(statement), 0)
    return frozenset(inside)


def _text_unreadable(source: str, suffix: str) -> bool:
    """Whether a dialing statement in this text caller hides the target it dials."""
    statements = _statements(source, suffix)
    bindings = _target_bindings(source)
    forwarding = _function_statements(statements)
    return any(
        not _target_is_readable(_target_operand(statement), bindings, index in forwarding)
        for index, statement in enumerate(statements)
        if _dials_a_surface(statement)
    )


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
    unreadable = _text_unreadable(source, path.suffix)
    if dials and _DAEMON_AUTHORITY.search(source) is not None and not dialed:
        unreadable = True
    return CallerScan(
        hits=tuple(
            sorted(_text_protected_hits(source) | {route for route in dialed if route not in open_paths})
        ),
        acquires=_text_acquires_credential(source, path.suffix),
        dialed=tuple(sorted(dialed)),
        dials=dials,
        unreadable=unreadable,
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

#: A caller that splices the credential into the request body the daemon reads
#: no header from.
_BODY_BORNE_CALLER = (
    "import httpx\n"
    "from mnemoseed_local.credential import read_credential_at\n"
    "credential = read_credential_at('home')\n"
    "httpx.post('http://127.0.0.1:1/memory/remember', json={'text': 'x' + credential})\n"
)

#: A caller that binds its header set to a positional argument the callee does
#: not read as headers.
_MISBOUND_HEADER_CALLER = (
    "import httpx\n"
    "from mnemoseed_local.credential import read_credential_at\n"
    "from mnemoseed_local.rest_client import credential_headers\n"
    "credential = read_credential_at('home')\n"
    "headers = credential_headers(credential)\n"
    "httpx.post('http://127.0.0.1:1/memory/remember', {}, headers)\n"
)

#: A caller whose header set a module-local helper produces.
_PRODUCER_HEADERS_CALLER = (
    "import httpx\n"
    "from mnemoseed_local.credential import read_credential_at\n"
    "from mnemoseed_local.rest_client import credential_headers\n"
    "credential = read_credential_at('home')\n"
    "def opts():\n"
    "    return credential_headers(credential)\n"
    "httpx.post('http://127.0.0.1:1/memory/remember', json={}, headers=opts())\n"
)

#: A caller that hands the credential and the route it dials to another consumer
#: of the daemon surface, which reads the credential from where it is handed.
_HANDOVER_CALLER = (
    "from mnemoseed_local.credential import read_credential_at\n"
    "credential = read_credential_at('home')\n"
    "write_client_config(credential, 'http://127.0.0.1:1/mcp')\n"
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


def test_a_dialing_call_presents_the_credential_only_in_a_header_position(tmp_path: Path) -> None:
    """The daemon reads a dialing call's credential from its headers: a value
    spliced into the body, or handed to an argument the callee does not read as
    headers, reaches the protected path unauthenticated."""
    body_borne = _caller_file(tmp_path, "body_borne.py", _BODY_BORNE_CALLER)
    misbound = _caller_file(tmp_path, "misbound.py", _MISBOUND_HEADER_CALLER)

    assert _classify(_scan(body_borne)) == PROTECTED_WITHOUT_CREDENTIAL
    assert _classify(_scan(misbound)) == PROTECTED_WITHOUT_CREDENTIAL


def test_a_header_set_produced_by_a_module_local_helper_attaches_at_the_call(tmp_path: Path) -> None:
    """A helper that returns the header set built from the credential is a
    producer of it, so the call it is handed to presents the credential."""
    caller = _caller_file(tmp_path, "producer_headers.py", _PRODUCER_HEADERS_CALLER)
    scan = _scan(caller)

    assert scan.acquires
    assert scan.unattached == ()
    assert _classify(scan) == CREDENTIALED


def test_a_call_that_does_not_dial_is_judged_on_its_whole_argument_subtree(tmp_path: Path) -> None:
    """A caller that hands the credential and a protected route to another
    consumer of the daemon surface presents it for that consumer, wherever the
    callee reads it from."""
    caller = _caller_file(tmp_path, "handover.py", _HANDOVER_CALLER)

    assert _classify(_scan(caller)) == CREDENTIALED


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


#: Text callers that dial with nothing in them to show where: a splat, and a target
#: kept in a variable the file never declares.
_HIDDEN_TARGET_SHAPES = (
    ("splat.ps1", "Invoke-RestMethod @params\n"),
    ("hidden-var.sh", 'curl "$Url"\n'),
)


@pytest.mark.parametrize(("filename", "source"), _HIDDEN_TARGET_SHAPES)
def test_a_text_caller_that_hides_its_whole_target_fails_closed(
    filename: str, source: str, tmp_path: Path
) -> None:
    """A caller that dials with no URL and no path anywhere could be dialing
    anything, so it fails like the caller that hides its path alone."""
    caller = _caller_file(tmp_path, filename, source)

    scan = _scan(caller)

    assert scan.unreadable
    assert _classify(scan) == PROTECTED_WITHOUT_CREDENTIAL


#: Dialing statements whose readable text sits outside the target position: an
#: output path, a header value, a header body, and a second call beside a visible
#: one. The target itself is declared nowhere.
_HIDDEN_TARGET_BY_POSITION = (
    (
        "output-masked.ps1",
        "param([string]$OutFile)\nInvoke-WebRequest -Uri $Url -OutFile $OutFile\n",
    ),
    (
        "header-masked.sh",
        'Header=x\ncurl "$Url" -H "$Header"\n',
    ),
    (
        "header-body.js",
        'fetch(url, {headers:{"X-Link":"https://example.com"}})\n',
    ),
    (
        "mixed-targets.sh",
        "curl 'https://example.com/install.sh'\ncurl \"$Url\"\n",
    ),
)


@pytest.mark.parametrize(("filename", "source"), _HIDDEN_TARGET_BY_POSITION)
def test_only_the_target_position_shows_where_a_text_caller_dials(
    filename: str, source: str, tmp_path: Path
) -> None:
    """An output, header or body argument is not the target, so a call whose
    target the file never declares fails closed however much readable text the
    statement carries elsewhere, and a visible call beside it does not lend it
    the target it hides."""
    caller = _caller_file(tmp_path, filename, source)

    scan = _scan(caller)

    assert scan.unreadable
    assert _classify(scan) == PROTECTED_WITHOUT_CREDENTIAL


def test_a_function_signature_declares_the_target_its_call_uses(tmp_path: Path) -> None:
    """A typed function signature declares its target as a param block does, so a
    call whose target that signature declares stays open, while one whose target
    it never declares fails closed."""
    declared = _caller_file(
        tmp_path,
        "declared-target.ps1",
        "function Test-Health([string]$Url) {\n    $null = Invoke-RestMethod -Uri $Url -TimeoutSec 2\n}\n",
    )
    unbound = _caller_file(
        tmp_path,
        "unbound-target.ps1",
        "function Save-Page([string]$OutFile) {\n    Invoke-WebRequest -Uri $Url -OutFile $OutFile\n}\n",
    )

    assert not _scan(declared).unreadable
    assert _classify(_scan(declared)) == OPEN_SET
    assert _scan(unbound).unreadable
    assert _classify(_scan(unbound)) == PROTECTED_WITHOUT_CREDENTIAL


#: The shipped installer invocations and probes: a literal target beside an output
#: argument, a value-taking switch before the target, a parameter-forwarding
#: scriptblock, an availability probe, and a shell downloader whose target is its
#: own argument. None of them dials a daemon surface.
_SHIPPED_INVOCATION_SHAPES = (
    (
        "install-webrequest.ps1",
        "$uvInstaller = Join-Path $env:TEMP 'mnemoseed-uv-install.ps1'\n"
        "Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $uvInstaller -UseBasicParsing\n",
    ),
    (
        "install-curl.ps1",
        "$uvInstaller = Join-Path $env:TEMP 'mnemoseed-uv-install.ps1'\n"
        "& curl.exe -fsSL -o $uvInstaller 'https://astral.sh/uv/install.ps1'\n",
    ),
    (
        "install-downloader.ps1",
        "$Downloader = { param([string]$Uri, [string]$OutFile) "
        "Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing }\n",
    ),
    (
        "install-probe.ps1",
        "if (Test-CommandExists 'curl.exe') { exit 0 }\n",
    ),
    (
        "install-probe-shell.sh",
        'have() { command -v "$1" >/dev/null 2>&1; }\nif have curl; then exit 0; fi\n',
    ),
    (
        "install-prose.sh",
        'die "neither curl nor wget is available; install one of them to download $1"\n',
    ),
    (
        "install-heredoc.sh",
        "usage() {\n"
        "    cat <<'EOF'\n"
        "  curl -fsSL https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.sh | sh\n"
        "  curl -fsSL ... | sh -s -- [--dry-run]\n"
        "EOF\n"
        "}\n",
    ),
    (
        "install-fetch-pipe.sh",
        "fetch_pipe() {\n"
        "    if have curl; then\n"
        '        curl -fsSL "$1"\n'
        "    elif have wget; then\n"
        '        wget -qO- "$1"\n'
        "    else\n"
        '        die "neither curl nor wget is available; install one of them to download $1"\n'
        "    fi\n"
        "}\n",
    ),
)


@pytest.mark.parametrize(("filename", "source"), _SHIPPED_INVOCATION_SHAPES)
def test_a_shipped_invocation_or_probe_names_no_daemon_dial(
    filename: str, source: str, tmp_path: Path
) -> None:
    """A download target read after an output switch, a parameter a block forwards,
    an availability probe, quoted prose and help text all stay on the open side:
    a probe is not a dial, and only the target position shows where a dial goes."""
    caller = _caller_file(tmp_path, filename, source)

    scan = _scan(caller)

    assert not scan.unreadable
    assert _classify(scan) == OPEN_SET


def test_a_text_caller_that_shows_the_url_it_dials_stays_open(tmp_path: Path) -> None:
    """A downloader that shows the URL it dials shows the scan where its dials go, so
    a host outside the daemon keeps it on the open side."""
    caller = _caller_file(
        tmp_path,
        "download.ps1",
        "Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $OutFile\n",
    )

    assert _classify(_scan(caller)) == OPEN_SET


def test_a_text_caller_that_reads_its_target_from_its_caller_stays_open(tmp_path: Path) -> None:
    """A dialing statement that reads only parameters the caller supplies — a staged
    download helper — is judged where the target is supplied."""
    caller = _caller_file(
        tmp_path,
        "helper.ps1",
        "function Get-Staged {\n"
        "    param([string]$Uri, [string]$OutFile)\n"
        "    Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing\n"
        "}\n",
    )

    assert _classify(_scan(caller)) == OPEN_SET


def test_a_text_caller_that_binds_the_url_it_dials_stays_open(tmp_path: Path) -> None:
    """A downloader that binds the URL it dials resolves its target inside the file,
    so it stays open like the caller that shows the URL inline."""
    source = '$UvUrl = "https://astral.sh/uv/install.ps1"\nInvoke-WebRequest -Uri $UvUrl\n'
    caller = _caller_file(tmp_path, "bound.ps1", source)

    assert _classify(_scan(caller)) == OPEN_SET


def test_a_text_caller_that_dials_only_an_open_route_stays_open(tmp_path: Path) -> None:
    """The liveness probe needs no credential, so a text caller that dials nothing
    else stays on the open side of the boundary."""
    source = 'Invoke-RestMethod -Uri "$DaemonUrl/healthz" -TimeoutSec 2\n'
    probe = _caller_file(tmp_path, "probe.ps1", source)

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


def test_a_text_caller_that_binds_its_header_set_before_it_dials_is_credentialed(tmp_path: Path) -> None:
    """A script that builds its header set once and dials with it presents the
    credential on the line it dials, so the text scan does not have to read the
    whole file to see it."""
    binding = _caller_file(
        tmp_path,
        "binding.ps1",
        "$headers = credential_headers $Credential\n"
        'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember" -Headers $headers -Method Post\n',
    )

    assert _classify(_scan(binding)) == CREDENTIALED


def test_a_text_caller_that_spreads_its_dial_over_continued_lines_is_credentialed(tmp_path: Path) -> None:
    """A shell statement that continues over several physical lines dials with
    what the whole statement carries."""
    caller = _caller_file(
        tmp_path,
        "continued.sh",
        "curl \\\n  -H 'Authorization: Bearer $CREDENTIAL' \\\n  http://127.0.0.1:7788/memory/remember\n",
    )

    assert _classify(_scan(caller)) == CREDENTIALED


@pytest.mark.parametrize(
    ("filename", "source"),
    (
        (
            "commented.ps1",
            "# Authorization: Bearer $Credential\n"
            'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember" -Method Post\n',
        ),
        (
            "trailing.ps1",
            'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember"  # Authorization: Bearer $Credential\n',
        ),
    ),
)
def test_a_comment_does_not_credential_a_text_caller(filename: str, source: str, tmp_path: Path) -> None:
    """A marker a file only names in a comment is not a presentation, whether the
    comment stands above the dialing line or trails it."""
    caller = _caller_file(tmp_path, filename, source)

    assert _classify(_scan(caller)) == PROTECTED_WITHOUT_CREDENTIAL


# ---------------------------------------------------------------- the header key

_HEADER_CALLER = (
    "import httpx\n"
    "from mnemoseed_local.credential import read_credential_at\n"
    "from mnemoseed_local.rest_client import credential_headers\n"
    "credential = read_credential_at('home')\n"
    "headers = credential_headers(credential)\n"
    "opts = credential_headers(credential)\n"
    "def passthrough(value):\n"
    "    return value\n"
    "{call}\n"
)

_DIAL = "httpx.post('http://127.0.0.1:1/memory/remember', json={}, "

#: The ways a dialing call can carry the credential in a header position. The
#: daemon reads only an Authorization bearer or the console cookie, so a value that
#: sits in the header set under another key presents nothing there.
_HEADER_POSITION_SHAPES = (
    ("keyed-seam-value", _DIAL + "headers={'Authorization': credential})", CREDENTIALED),
    ("keyed-bearer-literal", _DIAL + "headers={'Authorization': 'Bearer x'})", CREDENTIALED),
    ("produced-header-set", _DIAL + "headers=credential_headers(credential))", CREDENTIALED),
    ("produced-header-set-copied", _DIAL + "headers=credential_headers(credential).copy())", CREDENTIALED),
    ("bound-header-set", _DIAL + "headers=headers)", CREDENTIALED),
    ("unpacked-header-set", _DIAL + "**opts)", CREDENTIALED),
    ("opaque-passthrough", _DIAL + "headers=passthrough(credential))", CREDENTIALED),
    ("bare-seam-value", _DIAL + "headers=credential)", PROTECTED_WITHOUT_CREDENTIAL),
    ("wrong-key-call", _DIAL + "headers=dict(other=credential))", PROTECTED_WITHOUT_CREDENTIAL),
    ("wrong-key-literal", _DIAL + "headers={'X-Token': credential})", PROTECTED_WITHOUT_CREDENTIAL),
    (
        "wrong-key-x-authorization",
        _DIAL + "headers={'X-Authorization': credential})",
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "wrong-key-authorization-trace",
        _DIAL + "headers={'Authorization-Trace': credential})",
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "wrong-key-x-cookie",
        _DIAL + "headers={'X-Cookie': credential})",
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    ("mixed-case-exact-key", _DIAL + "headers={'aUtHoRiZaTiOn': credential})", CREDENTIALED),
)


@pytest.mark.parametrize(("label", "call", "expected"), _HEADER_POSITION_SHAPES)
def test_a_header_position_carries_the_credential_under_a_key_it_honours(
    label: str, call: str, expected: str, tmp_path: Path
) -> None:
    """A dialing call presents the credential when it sits under an Authorization
    key, or reaches the header position through a produced header set, a bearer
    literal or an unpack the scan cannot read. The seam value in a header slot of
    its own, or under a key the daemon does not read, presents nothing."""
    caller = _caller_file(tmp_path, f"{label}.py", _HEADER_CALLER.format(call=call))

    assert _classify(_scan(caller)) == expected


def test_a_lambda_producer_attaches_at_the_call(tmp_path: Path) -> None:
    """A module-local helper that hands back an acquisition call's value is a
    producer whether it is written as a function or bound as a lambda."""
    caller = _caller_file(
        tmp_path,
        "lambda_producer.py",
        "import httpx\n"
        "from mnemoseed_local.credential import read_credential_at\n"
        "from mnemoseed_local.rest_client import credential_headers\n"
        "credential = read_credential_at('home')\n"
        "mk = lambda: credential_headers(credential)\n"
        "httpx.post('http://127.0.0.1:1/memory/remember', json={}, headers=mk())\n",
    )

    assert _classify(_scan(caller)) == CREDENTIALED


# ---------------------------------------------------------------- text statements

#: Text callers whose comment syntax or statement separator decides whether the
#: marker in the file belongs to the statement that dials.
_TEXT_STATEMENT_SHAPES = (
    (
        "leading-block.js",
        '/* Authorization: Bearer x */ fetch("http://127.0.0.1:7788/memory/remember");\n',
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "trailing-block.ts",
        'fetch("http://127.0.0.1:7788/memory/remember") /* Authorization: Bearer x */\n',
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "multiline-block.mjs",
        'fetch("http://127.0.0.1:7788/memory/remember") /* Authorization: Bearer x\n * trailing\n */\n',
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "leading-block.ps1",
        '<# Authorization: Bearer x #> Invoke-RestMethod -Uri "$DaemonUrl/memory/remember" -Method Post\n',
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "trailing-block.ps1",
        'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember" <# Authorization: Bearer x #>\n',
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "multiline-block.ps1",
        'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember" <# Authorization: Bearer x\n * trailing #>\n',
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "rem.bat",
        "REM curl http://127.0.0.1:7788/memory/remember Authorization: Bearer x\n",
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "rem-joined.cmd",
        "curl http://127.0.0.1:7788/memory/remember & REM Authorization: Bearer x\n",
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "rem-only-command.cmd",
        "echo ready & REM curl http://127.0.0.1:7788/memory/remember Authorization: Bearer x\n",
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "label.cmd",
        ":: curl http://127.0.0.1:7788/memory/remember Authorization: Bearer x\n",
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "separated.sh",
        "curl http://127.0.0.1:7788/memory/remember; echo $Authorization\n",
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "separated.ps1",
        'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember"; Write-Host $Authorization\n',
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "and-separated.sh",
        "curl http://127.0.0.1:7788/memory/remember && echo 'Authorization: Bearer x'\n",
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "pipeline.ps1",
        'Invoke-RestMethod -Uri "$DaemonUrl/memory/remember" | '
        'ForEach-Object { "Authorization: Bearer $Credential" }\n',
        PROTECTED_WITHOUT_CREDENTIAL,
    ),
    (
        "quoted-separator.sh",
        "curl 'http://127.0.0.1:7788/memory/remember;p=1' -H 'Authorization: Bearer $CREDENTIAL'\n",
        CREDENTIALED,
    ),
    (
        "quoted-block.sh",
        'curl "http://127.0.0.1:7788/memory/remember" -H "Authorization: Bearer $CREDENTIAL"\n',
        CREDENTIALED,
    ),
    (
        "presenting-block.js",
        'fetch("http://127.0.0.1:7788/memory/remember", {headers: {Authorization: "Bearer x"}});\n',
        CREDENTIALED,
    ),
)


@pytest.mark.parametrize(("filename", "source", "expected"), _TEXT_STATEMENT_SHAPES)
def test_a_text_marker_counts_only_on_the_statement_that_dials(
    filename: str, source: str, expected: str, tmp_path: Path
) -> None:
    """A marker a caller only names in a comment — of any comment syntax its
    language has — or in another statement of the same line is not a presentation.
    A separator inside a quoted URL is not a separator."""
    caller = _caller_file(tmp_path, filename, source)

    assert _classify(_scan(caller)) == expected


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


#: The languages a consumer at the repository root can be written in. Each one is
#: a way to dial the daemon without a package directory to live in.
_ROOT_CONSUMER_NAMES = (
    "probe.cjs",
    "probe.js",
    "probe.jsx",
    "probe.mjs",
    "probe.ts",
    "probe.tsx",
    "probe.bat",
    "probe.cmd",
    "probe.ps1",
    "probe.psm1",
    "probe.sh",
)


@pytest.mark.parametrize("name", _ROOT_CONSUMER_NAMES)
def test_a_root_level_consumer_of_any_executable_suffix_is_scanned(name: str, tmp_path: Path) -> None:
    """The root layer reaches a consumer by suffix, so the suffix table is the
    whole reach of that layer."""
    root = _synthetic_root(tmp_path)
    (root / name).write_text('fetch("http://127.0.0.1:7788/memory/remember");\n', encoding="utf-8")

    scanned = _caller_files(root)
    assert name in scanned
    assert _classify(_scan(scanned[name])) == PROTECTED_WITHOUT_CREDENTIAL


def test_the_root_suffix_table_is_pinned() -> None:
    """The scanned suffixes are pinned, so widening or narrowing the scan surface
    is a deliberate edit rather than a silent one."""
    assert EXECUTABLE_SUFFIXES == frozenset(
        {
            ".bat",
            ".cjs",
            ".cmd",
            ".js",
            ".jsx",
            ".mjs",
            ".ps1",
            ".psm1",
            ".py",
            ".sh",
            ".ts",
            ".tsx",
        }
    )
    assert not EXECUTABLE_SUFFIXES & DOCUMENTATION_SUFFIXES


#: A caller that carries a command switch and a filesystem path beside the route it
#: dials. Neither is a route: reporting one reads as a finding that is not there.
_SWITCH_AND_PATH_CALLER = (
    "import subprocess\n"
    "from pathlib import Path\n"
    "import httpx\n"
    "from mnemoseed_local.credential import read_credential_at\n"
    "from mnemoseed_local.rest_client import credential_headers\n"
    "credential = read_credential_at('home')\n"
    "argv = ['cmd', '/c', 'echo']\n"
    "subprocess.run(argv, cwd='/tmp/build')\n"
    "Path('/tmp/check').read_text(encoding='utf-8')\n"
    "httpx.post('http://127.0.0.1:1/memory/remember', json={}, headers=credential_headers(credential))\n"
)


def test_a_command_switch_or_a_filesystem_path_is_not_reported_as_a_route(tmp_path: Path) -> None:
    """A path-shaped literal that addresses a switch or the filesystem is not a
    route the daemon serves."""
    caller = _caller_file(tmp_path, "switches.py", _SWITCH_AND_PATH_CALLER)
    scan = _scan(caller)

    assert scan.hits == ("/memory/remember",)
    assert _classify(scan) == CREDENTIALED
