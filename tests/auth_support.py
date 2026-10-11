"""The authenticated test surface: one entry point for a daemon under test.

Every daemon HTTP surface in the suite is reached through this module, so the
credential boundary is exercised by every test that touches the daemon and none
of them needs a switch that would bypass it:

- :func:`seed_credential` writes a fresh credential into an isolated config home
  (never the real one) and hands the value to the test.
- :func:`credential_headers` renders that value as the bearer header.
- :func:`boot` builds the real app in an isolated home and yields a client that
  presents the credential.
- :func:`unauthenticated` yields the same client with nothing attached, which is
  how a test proves a route refuses an unauthorized caller.

``boot`` also pins the config home for its block. That is the isolation
guarantee, not a convenience: the daemon issues (and reads) its credential from
the config home, so a boot that left the real home in place would write into
the installed runtime.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local import config as _config
from mnemoseed_local.credential import (
    CREDENTIAL_SECRET_NAME,
    generate_credential,
    read_credential_at,
)
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.secrets.store import FileSecretStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def seed_credential(home: Path) -> str:
    """Write a fresh daemon credential into the given config home; return it."""
    credential = generate_credential()
    FileSecretStore(home).set(CREDENTIAL_SECRET_NAME, credential)
    return credential


def stored_credential(home: Path) -> str:
    """The credential the home currently carries (what the daemon will accept)."""
    return read_credential_at(home)


def credential_headers(credential: str) -> dict[str, str]:
    """The bearer header a client presents for ``credential``."""
    from mnemoseed_local.rest_client import credential_headers as render

    return render(credential)


@contextmanager
def boot(home: Path, *, credential: str | None = None, lifespan: Any = None) -> Iterator[TestClient]:
    """Boot the real app in an isolated home and yield an authenticated client.

    Without ``credential`` a fresh one is seeded into the home, so the test knows
    the value. With it, the home already carries that value (a restart, or a
    rotation the test drives itself) and the client presents it unchanged.
    ``lifespan`` replaces the app's lifespan for a test that supplies its own
    boot state; the credential gate still applies.
    """
    issued = credential if credential is not None else seed_credential(home)
    app = create_app()
    if lifespan is not None:
        app.router.lifespan_context = lifespan
    with isolated_home(home), TestClient(app, headers=credential_headers(issued)) as client:
        yield client


@contextmanager
def unauthenticated(home: Path) -> Iterator[TestClient]:
    """The same isolated boot with no credential attached.

    Every protected route refuses these requests, which is the reverse
    assertion each migrated group rides: without the header, nothing is served.
    """
    seed_credential(home)
    with isolated_home(home), TestClient(create_app()) as client:
        yield client


@contextmanager
def isolated_home(home: Path) -> Iterator[None]:
    """Pin the config home for the block: no test path resolves the real one."""
    home.mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_config, "CONFIG_DIR", home)
        patch.setattr(_config, "CONFIG_PATH", home / "config.toml")
        yield


def source_files_with_logging_calls(*relative_paths: str) -> dict[str, list[str]]:
    """Files that call a logger or print, mapped to the calls they make.

    The credential sources must keep no logging call at all: a value cannot leak
    through a channel the module never opens.
    """
    offenders: dict[str, list[str]] = {}
    for relative in relative_paths:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        calls = sorted(
            {name for name in (_logging_call_name(node) for node in ast.walk(ast.parse(source))) if name}
        )
        if calls:
            offenders[relative] = calls
    return offenders


def _logging_call_name(node: ast.AST) -> str:
    if not isinstance(node, ast.Call):
        return ""
    target = node.func
    if isinstance(target, ast.Name) and target.id == "print":
        return "print"
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        if target.value.id in {"logger", "logging", "log"}:
            return f"{target.value.id}.{target.attr}"
    return ""


def live_server(app: Any, port: int) -> Iterator[str]:
    """A real uvicorn server on a spare port, yielded as its base URL.

    The client code path under test is httpx against a real socket, so the
    issuer-swap boundary is proven on the wire and not through a test double.
    """
    import threading
    import time

    import uvicorn

    @contextmanager
    def _serve() -> Iterator[str]:
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, name="credential-boundary-server", daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 30.0
            while not server.started:
                if time.monotonic() >= deadline:
                    raise AssertionError("the boundary server never started")
                time.sleep(0.02)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=30.0)

    return _serve()
