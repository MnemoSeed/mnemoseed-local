"""The in-repo daemon-caller credential guard.

The daemon surface refuses every request that cannot present the credential, so a
consumer outside the product source has to obtain the value from the daemon's own
config home and attach it as a bearer header. The scripts that stand up an
isolated daemon are exactly such consumers.

This module scans the in-repo caller surface — the executable scripts, the
feature documents, and the workflow definitions, minus the vendored design-skill
bundle — and fails when a caller reaches a protected path without obtaining the
credential. A registry pins the known-good classification of every scanned file,
so the scan has teeth in both directions: a caller that stops presenting the
credential, a newly added caller, and a deleted caller each fail it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from mnemoseed_local.daemon.auth import OPEN_PATHS, console_documents

REPO_ROOT = Path(__file__).resolve().parents[1]

CALLER_ROOTS = ("scripts", "features", ".github")

#: Tracked callers outside the roots: the boundary probe stands its own daemon up
#: directly at the repository root.
EXTRA_CALLERS = ("probe_browser_boundary.py",)

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

#: Protected route prefixes of the daemon surface, for the non-Python text scan.
#: The open set needs no prefix: those paths are literal in every consumer.
_PROTECTED_TEXT = re.compile(
    r"(?<![#\w])/(?:memory|session|ingest|config|daemon|flush)(?:/[A-Za-z0-9_-]+)*(?![\w.-])"
)

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
    "features/README.md": OPEN_SET,
    "features/atlas.md": OPEN_SET,
    "probe_browser_boundary.py": CREDENTIALED,
    "scripts/__init__.py": OPEN_SET,
    "scripts/atlas_preflight.py": OPEN_SET,
    "scripts/atlas_verify.py": CREDENTIALED,
    "scripts/check_changed_comments.py": OPEN_SET,
    "scripts/gate.ps1": OPEN_SET,
    "scripts/sanitize_atlas_evidence.py": OPEN_SET,
    "scripts/windows-install-helpers.ps1": OPEN_SET,
    "scripts/windows-logon-task.ps1": OPEN_SET,
    "scripts/windows-logon.ps1": OPEN_SET,
}


def _caller_files() -> dict[str, Path]:
    callers: dict[str, Path] = {}
    for root in CALLER_ROOTS:
        base = REPO_ROOT / root
        if base.is_dir():
            _collect(base.rglob("*"), callers)
    _collect((REPO_ROOT / relative for relative in EXTRA_CALLERS), callers)
    return callers


def _collect(paths: Iterable[Path], callers: dict[str, Path]) -> None:
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        if "__pycache__" in relative or any(part in relative for part in VENDORED_PARTS):
            continue
        callers[relative] = path


def _route_path(literal: str) -> str | None:
    """The route a literal addresses: a bare path, or the path of an inline URL."""
    if literal.startswith(("http://", "https://")):
        return urlsplit(literal).path or None
    return literal if literal.startswith("/") else None


def _python_protected_hits(source: str) -> list[str]:
    """Route-shaped literals a Python caller references outside the open set."""
    open_paths = OPEN_PATHS | console_documents()
    hits = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal = _route_path(node.value)
            if literal is not None and _ROUTE_SHAPE.match(literal) and literal not in open_paths:
                hits.add(literal)
    return sorted(hits)


def _text_protected_hits(source: str) -> list[str]:
    return sorted(set(_PROTECTED_TEXT.findall(source)))


def _python_acquires_credential(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            target = node.func
            called = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if called in ACQUISITION_CALLS:
                return True
    return False


def _text_acquires_credential(source: str) -> bool:
    return any(marker in source for marker in ACQUISITION_MARKERS)


def _scan(path: Path) -> tuple[list[str], bool]:
    """The protected path literals in one caller file, and whether it obtains a credential."""
    source = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        return _python_protected_hits(source), _python_acquires_credential(source)
    return _text_protected_hits(source), _text_acquires_credential(source)


def _classification(relative: str) -> str:
    hits, acquires = _scan(REPO_ROOT / relative)
    if not hits:
        return OPEN_SET
    return CREDENTIALED if acquires else PROTECTED_WITHOUT_CREDENTIAL


def test_no_caller_reaches_a_protected_path_without_a_credential():
    holes: dict[str, list[str]] = {}
    for relative, path in _caller_files().items():
        hits, acquires = _scan(path)
        if hits and not acquires:
            holes[relative] = hits
    detail = "; ".join(f"{relative} -> {hits}" for relative, hits in sorted(holes.items()))
    assert not holes, f"daemon caller reaching a protected path without a credential: {detail}"


def test_known_caller_registry_matches_the_tree():
    scanned = _caller_files()
    unregistered = sorted(set(scanned) - set(KNOWN_CALLERS))
    missing = sorted(set(KNOWN_CALLERS) - set(scanned))
    assert set(scanned) == set(KNOWN_CALLERS), (
        f"the in-repo caller surface changed; classify each file in KNOWN_CALLERS as "
        f"{CREDENTIALED!r} or {OPEN_SET!r}: unregistered={unregistered} missing={missing}"
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
