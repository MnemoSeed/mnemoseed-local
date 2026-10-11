"""The credential boundary is exercised by every daemon test — and bypassed by none.

Three mechanical pins stand in place of trust:

1. Every ``TestClient(...)`` construction in the suite either lives inside
   ``auth_support`` (the one entry point that seeds the credential into an
   isolated home and attaches it) or carries ``credential_headers(...)`` itself.
   A bare construction would test an unauthenticated surface, which is how a
   suite learns to ignore the gate. The few allowed exceptions are named with
   their reason.
2. No test source introduces a bypass switch for the boundary.
3. The gate itself branches on nothing a test could set: it reads no
   environment variable and takes no mode flag.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent

#: The one module allowed to construct a TestClient on the suite's behalf.
BOOT_OWNER = "auth_support.py"

#: The constructions the scan accepts, each with the reason it is unauthenticated.
ALLOWED_CONSTRUCTIONS = {
    "test_configwrite_ifmatch_extra.py": (
        "a bare FastAPI app carrying only the configwrite router: no credential gate exists on it"
    ),
    "test_configwrite_service.py": (
        "a bare FastAPI app carrying only the configwrite router: no credential gate exists on it"
    ),
    "test_daemon_credential_boundary.py": (
        "the issuer-swap app is driven with explicit headers, including the empty set that must be refused"
    ),
}

BYPASS_TOKENS = (
    "MNEMOSEED_LOCAL_SKIP_AUTH",
    "MNEMOSEED_LOCAL_TEST_TOKEN",
    "MNEMOSEED_LOCAL_DISABLE_AUTH",
    "skip_auth",
    "disable_auth",
    "bypass_auth",
)


def _sources() -> list[Path]:
    return sorted(path for path in TESTS.rglob("*.py") if "__pycache__" not in path.parts)


def _is_boot_call(node: ast.Call) -> bool:
    target = node.func
    if isinstance(target, ast.Attribute):
        return target.attr in {"boot", "unauthenticated"}
    return isinstance(target, ast.Name) and target.id in {"boot", "unauthenticated"}


def _carries_credential(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg == "headers":
            return "credential_headers" in ast.unparse(keyword.value)
    return False


def unguarded_test_clients() -> dict[str, list[str]]:
    """Every TestClient construction outside auth_support, by source."""
    offenders: dict[str, list[str]] = {}
    for path in _sources():
        if path.name == BOOT_OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "TestClient":
                continue
            if _is_boot_call(node) or _carries_credential(node):
                continue
            found.append(f"line {node.lineno}")
        if found:
            offenders[path.name] = found
    return offenders


def test_every_test_client_construction_is_authenticated() -> None:
    found = unguarded_test_clients()
    unexplained = {name: sites for name, sites in found.items() if name not in ALLOWED_CONSTRUCTIONS}
    assert unexplained == {}, "a TestClient was built without the credential channel"
    stale = sorted(set(ALLOWED_CONSTRUCTIONS) - set(found))
    assert stale == [], "the allow list names sources that no longer need it"


def test_no_source_introduces_an_authentication_bypass() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _sources():
        if path.name == Path(__file__).name:
            continue  # this file names the banned tokens in order to ban them
        source = path.read_text(encoding="utf-8")
        found = [token for token in BYPASS_TOKENS if token in source]
        if found:
            offenders[path.name] = found
    assert offenders == {}


def test_the_gate_itself_has_no_test_mode_branch() -> None:
    """The gate must not read anything a test could set: a switch that turns the
    boundary off for tests is the boundary's absence."""
    for relative in ("src/mnemoseed_local/credential.py", "src/mnemoseed_local/daemon/auth.py"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "environ" not in source, f"{relative} reads the environment"
        assert "getenv" not in source, f"{relative} reads the environment"
