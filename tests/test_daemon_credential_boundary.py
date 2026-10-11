"""The daemon credential boundary: the whole protected surface, one gate.

Every rule of the gate lives here rather than being spread over the suites that
happen to boot a daemon: what is open, what is refused, how the console earns
its cookie, what must never leak, which issuer is in play, and what a client
sees when the credential is wrong. The rest of the suite reaches the daemon
through ``auth_support``; nothing here bypasses the gate, because there is no
switch that would let it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import auth_support
import pytest
from _ports import unused_loopback_port
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.daemon.auth import (
    AUTH_DETAIL,
    CREDENTIAL_COOKIE_NAME,
    OPEN_PATHS,
    auth_middleware,
    console_documents,
)


def _config_text(home: Path, *, requests_log: bool = False) -> str:
    return (
        f'baseurl = "http://127.0.0.1:{unused_loopback_port()}"\n'
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        '[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        f"[logging]\nrequests = {'true' if requests_log else 'false'}\n"
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated config home holding a bootable embedded config."""
    (tmp_path / "config.toml").write_text(_config_text(tmp_path), encoding="utf-8")
    return tmp_path


LOOPBACK_DOCUMENT_HEADERS = {"host": "127.0.0.1:7788"}


# ---------------------------------------------------------------- the open set


def test_the_probes_answer_without_a_credential(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/health").status_code == 200


def test_the_open_set_is_exactly_the_probes_plus_the_enumerated_documents() -> None:
    assert OPEN_PATHS == {"/healthz", "/health"}
    assert console_documents() == {"/", "/index.html", "/app.js", "/style.css"}


@pytest.mark.parametrize(
    "path",
    [
        "/healthz/",
        "/HEALTHZ",
        "/healthz/../api/v1/audit",
        "/api/v1/audit",
        "/api/v1/config",
        "/api/v1/observability",
        "/api/v1/profiles",
        "/ingest",
        "/session/end",
        "/session/recent",
        "/session/windows",
        "/session/recall-pending",
        "/flush",
        "/memory/recall",
        "/memory/remember",
        "/memory/audit",
        "/memory/atlas",
        "/memory/reinforce",
        "/memory/supersede",
        "/memory/dream_once",
        "/memory/dream_status",
        "/mcp/handshake",
        "/daemon/shutdown",
        "/console.js",
        "/favicon.ico",
    ],
)
def test_every_path_outside_the_open_set_is_refused(path: str, home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        response = client.get(path)

    assert response.status_code == 401
    assert response.json() == {"detail": AUTH_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_refusal_never_echoes_the_presented_material(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        response = client.get("/api/v1/audit", headers={"Authorization": "Bearer a-credential-never-issued"})

    assert response.status_code == 401
    body = response.text
    assert body == json.dumps({"detail": AUTH_DETAIL})
    for echo in ("a-credential-never-issued", "Bearer", "Authorization"):
        assert echo not in body


# ---------------------------------------------------------------- the credential side


def test_the_credential_opens_the_protected_surface(home: Path) -> None:
    with auth_support.boot(home) as client:
        assert client.get("/api/v1/audit").status_code == 200
        assert client.get("/api/v1/config").status_code == 200
        assert client.get("/api/v1/observability").status_code == 200
        assert client.post("/mcp/handshake", json={}).status_code == 200
        # Closed to a caller without the credential; with one the request reaches
        # the route, and a TestClient boot's honest "no hook" answer is not an
        # authentication failure.
        assert client.post("/daemon/shutdown").status_code == 503


def test_a_wrong_credential_is_refused_like_a_missing_one(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        wrong = auth_support.credential_headers("a-credential-that-was-never-issued")
        assert client.get("/api/v1/audit", headers=wrong).status_code == 401


@pytest.mark.parametrize(
    "header",
    ["not-a-credential", "Bearer", "Bearer ", "Basic not-a-credential", "bearer"],
)
def test_a_malformed_authorization_header_carries_no_credential(header: str, home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        refused = client.get("/api/v1/audit", headers={"Authorization": header})
        assert refused.status_code == 401


def test_a_restart_keeps_the_issued_credential(home: Path) -> None:
    issued = auth_support.seed_credential(home)
    with auth_support.boot(home, credential=issued) as first:
        assert first.get("/api/v1/audit").status_code == 200
    with auth_support.boot(home, credential=issued) as second:
        assert second.get("/api/v1/audit").status_code == 200


def test_a_rotated_credential_refuses_the_previous_value(home: Path) -> None:
    from mnemoseed_local.credential import rotate_credential

    issued = auth_support.seed_credential(home)
    with auth_support.boot(home, credential=issued) as client:
        assert client.get("/api/v1/audit").status_code == 200

    with auth_support.isolated_home(home):
        rotate_credential()

    with auth_support.unauthenticated(home) as client:
        stale = auth_support.credential_headers(issued)
        assert client.get("/api/v1/audit", headers=stale).status_code == 401
    with auth_support.boot(home) as client:
        assert client.get("/api/v1/audit").status_code == 200


def test_the_console_documents_are_open_and_serve_their_bytes(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        for document in ("/", "/index.html", "/app.js", "/style.css"):
            response = client.get(document)
            assert response.status_code == 200, document
            assert response.text.strip() != "", document


# ---------------------------------------------------------------- console cookie grant


def test_a_loopback_document_response_grants_the_credential_as_a_cookie(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        response = client.get("/", headers=LOOPBACK_DOCUMENT_HEADERS)

    assert response.status_code == 200
    credential = auth_support.stored_credential(home)
    name, _, attributes = response.headers["set-cookie"].partition(";")
    assert name == f"{CREDENTIAL_COOKIE_NAME}={credential}"
    assert attributes.strip() == "HttpOnly; SameSite=Strict; Path=/"


def test_the_granted_cookie_authenticates_like_the_bearer_header(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        granted = client.get("/", headers=LOOPBACK_DOCUMENT_HEADERS).headers["set-cookie"]
        cookie = granted.partition(";")[0]
        assert client.get("/api/v1/audit", headers={"cookie": cookie}).status_code == 200


def test_a_non_loopback_host_document_response_never_carries_the_cookie(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        for host in ("testserver", "example.com", "attacker.example:7788"):
            response = client.get("/", headers={"host": host})
            assert response.status_code == 200, host
            assert "set-cookie" not in response.headers, host


def test_a_cross_origin_document_response_never_carries_the_cookie(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        cross_site = {**LOOPBACK_DOCUMENT_HEADERS, "origin": "http://attacker.example"}
        assert "set-cookie" not in client.get("/", headers=cross_site).headers

        same_site = {**LOOPBACK_DOCUMENT_HEADERS, "origin": "http://127.0.0.1:7788"}
        assert "set-cookie" in client.get("/", headers=same_site).headers

        other_loopback_name = {**LOOPBACK_DOCUMENT_HEADERS, "origin": "http://localhost:7788"}
        assert "set-cookie" not in client.get("/", headers=other_loopback_name).headers


def test_the_console_body_never_embeds_the_credential(home: Path) -> None:
    with auth_support.unauthenticated(home) as client:
        credential = auth_support.stored_credential(home)
        for document in ("/", "/index.html", "/app.js", "/style.css"):
            assert credential not in client.get(document, headers=LOOPBACK_DOCUMENT_HEADERS).text


# ---------------------------------------------------------------- leak guards


def test_the_credential_reaches_neither_the_log_nor_the_audit_nor_a_refusal(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with auth_support.boot(home) as client, caplog.at_level(logging.INFO):
        assert client.get("/api/v1/audit").status_code == 200
        audit_rows = json.dumps(client.get("/api/v1/audit").json(), ensure_ascii=False)
        memory_rows = json.dumps(client.post("/memory/audit", json={"limit": 50}).json())
        refused = client.get(
            "/api/v1/audit", headers={"Authorization": "Bearer a-credential-never-issued"}
        ).text
        console_document = client.get("/", headers=LOOPBACK_DOCUMENT_HEADERS).text
        console_script = client.get("/app.js", headers=LOOPBACK_DOCUMENT_HEADERS).text

    credential = auth_support.stored_credential(home)
    assert credential not in (home / "daemon.log").read_text(encoding="utf-8")
    assert credential not in audit_rows
    assert credential not in memory_rows
    assert credential not in refused
    assert credential not in console_document
    assert credential not in console_script
    assert credential not in caplog.text


def test_the_request_log_records_method_path_and_status_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "config.toml").write_text(_config_text(tmp_path, requests_log=True), encoding="utf-8")
    marker = {"X-MnemoSeed-Marker": "must-not-reach-the-log"}
    with auth_support.boot(tmp_path) as client, caplog.at_level(logging.INFO):
        assert client.post("/mcp/handshake", json={}, headers=marker).status_code == 200

    lines = [record.getMessage() for record in caplog.records if "->" in record.getMessage()]
    assert lines == ["POST /mcp/handshake -> 200"]


def test_a_refusal_produces_no_log_line_at_all(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "config.toml").write_text(_config_text(tmp_path, requests_log=True), encoding="utf-8")
    with auth_support.unauthenticated(tmp_path) as client, caplog.at_level(logging.INFO):
        assert client.get("/api/v1/audit").status_code == 401
        assert client.post("/ingest", json={}).status_code == 401

    assert not [record for record in caplog.records if "->" in record.getMessage()]


def test_the_credential_sources_open_no_logging_channel() -> None:
    offenders = auth_support.source_files_with_logging_calls(
        "src/mnemoseed_local/credential.py",
        "src/mnemoseed_local/daemon/auth.py",
    )
    assert offenders == {}


# ---------------------------------------------------------------- middleware shape


def test_the_authentication_middleware_wraps_the_logging_middleware() -> None:
    factories = [middleware.cls for middleware in create_app().user_middleware]
    assert factories[0] is auth_middleware
    assert len(factories) == 2


def test_the_middleware_is_not_a_base_http_middleware() -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    for middleware in create_app().user_middleware:
        assert not isinstance(middleware.cls, type) or not issubclass(middleware.cls, BaseHTTPMiddleware)


def test_a_crafted_path_is_refused_without_any_normalization(home: Path) -> None:
    """The gate compares the raw scope path: no rstrip, no normpath, no case
    folding, so a path that merely looks like an open one is refused."""
    with auth_support.unauthenticated(home) as client:
        assert client.get("/healthz/../api/v1/audit").status_code == 401
        assert client.get("/api/v1/../healthz").status_code == 401
        assert client.get("/api/v1/../healthz/../api/v1/audit").status_code == 401
        assert client.get("/healthz/").status_code == 401
        assert client.get("/HEALTHZ").status_code == 401


# ---------------------------------------------------------------- client channel


def test_the_client_attaches_the_credential_as_a_bearer_header(home: Path) -> None:
    from mnemoseed_local.rest_client import client_for

    issued = auth_support.seed_credential(home)
    with auth_support.isolated_home(home):
        client = client_for("http://127.0.0.1:7788", actor="cli")

    assert client.token == issued
    assert client._headers() == {
        "X-MnemoSeed-Actor": "cli",
        "Authorization": f"Bearer {issued}",
    }


def test_the_client_adds_nothing_issuer_specific_beyond_authorization() -> None:
    from mnemoseed_local.rest_client import DaemonClient

    headers = DaemonClient(base_url="http://127.0.0.1:7788", token="issued-by-anyone")._headers()
    assert set(headers) == {"X-MnemoSeed-Actor", "Authorization"}
    assert headers["Authorization"] == "Bearer issued-by-anyone"


def test_a_client_without_a_credential_attaches_no_authorization_header() -> None:
    from mnemoseed_local.rest_client import DaemonClient

    assert DaemonClient(base_url="http://127.0.0.1:7788")._headers() == {"X-MnemoSeed-Actor": "cli"}


def test_the_credential_field_is_the_last_field_so_every_construction_stays_legal() -> None:
    from dataclasses import fields

    from mnemoseed_local.rest_client import DaemonClient

    names = [field.name for field in fields(DaemonClient)]
    assert names == ["base_url", "profile_id", "actor", "timeout", "token"]


def test_a_replaced_client_carries_the_credential() -> None:
    from dataclasses import replace

    from mnemoseed_local.rest_client import DaemonClient

    client = DaemonClient(base_url="http://127.0.0.1:7788", token="persisted")
    assert replace(client, timeout=1.0).token == "persisted"


def test_a_client_home_without_a_credential_attaches_nothing(home: Path) -> None:
    from mnemoseed_local.rest_client import client_for

    with auth_support.isolated_home(home):
        assert client_for("http://127.0.0.1:7788").token is None


# ---------------------------------------------------------------- issuer swap


class CloudIssuerVerifier:
    """A future issuer: only its own credential verifies, the local file never
    does. No client can tell which one is guarding the surface."""

    def __init__(self, credential: str) -> None:
        self._credential = credential

    def verify(self, presented: str | None) -> bool:
        return presented is not None and presented.strip() == self._credential


@pytest.mark.parametrize("issuer", ["local-file", "cloud-issuer"])
def test_the_boundary_is_identical_under_either_issuer(issuer: str, home: Path) -> None:
    local = auth_support.seed_credential(home)
    cloud = "issued-by-the-cloud"
    app = create_app() if issuer == "local-file" else create_app(token_verifier=CloudIssuerVerifier(cloud))

    with auth_support.isolated_home(home):
        with TestClient(app, headers=auth_support.credential_headers(local)) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/api/v1/audit").status_code == (200 if issuer == "local-file" else 401)

        with TestClient(app, headers=auth_support.credential_headers(cloud)) as client:
            assert client.get("/api/v1/audit").status_code == (401 if issuer == "local-file" else 200)

        with TestClient(app, headers={}) as client:
            assert client.get("/api/v1/audit").status_code == 401


def test_the_unchanged_client_path_sees_the_same_boundary_on_the_wire(
    home: Path,
) -> None:
    """The issuer swap is walked by the product's own client — no test double in
    the path — so the 200/401 boundary is the one httpx actually sees."""
    from mnemoseed_local.mcp_gateway.reliable_client import GatewayClient
    from mnemoseed_local.rest_client import DaemonClient, DaemonRestError, client_for

    cloud = "issued-by-the-cloud"
    app = create_app(token_verifier=CloudIssuerVerifier(cloud))
    with auth_support.isolated_home(home), auth_support.live_server(app, unused_loopback_port()) as base_url:
        local_client = GatewayClient.wrap(client_for(base_url, actor="mcp"))
        with pytest.raises(DaemonRestError) as refused:
            local_client.post("/memory/recall", {"profile_id": "default", "query": "x"})
        assert refused.value.status == 401
        assert "daemon credential required" in str(refused.value.detail)

        accepted = GatewayClient.wrap(DaemonClient(base_url=base_url, actor="mcp", token=cloud))
        payload = accepted.post("/memory/recall", {"profile_id": "default", "query": "x"})
        assert "entries" in payload["memory"]


def test_the_gateway_never_retries_a_refused_credential() -> None:
    """A 401 is a rest error, not unavailability: the single fast retry stays
    reserved for a loopback refusal."""
    from mnemoseed_local.mcp_gateway.reliable_client import GatewayClient
    from mnemoseed_local.rest_client import DaemonRestError

    class RecordingClient:
        profile_id = "default"
        base_url = "http://127.0.0.1:7788"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            self.calls.append(path)
            raise DaemonRestError(401, AUTH_DETAIL)

    inner = RecordingClient()
    try:
        GatewayClient.wrap(inner).post("/memory/recall", {})
    except DaemonRestError as exc:
        assert exc.status == 401
    else:  # pragma: no cover - the raise above is the assertion
        raise AssertionError("a refused credential must surface as a rest error")
    assert inner.calls == ["/memory/recall"]


def test_the_mcp_tool_call_names_the_real_causes_of_a_refusal() -> None:
    from mnemoseed_local.mcp_gateway import server as mcp_server
    from mnemoseed_local.mcp_gateway.reliable_client import GatewayClient
    from mnemoseed_local.rest_client import DaemonRestError

    class RefusingClient:
        profile_id = "default"
        base_url = "http://127.0.0.1:7788"

        def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            raise DaemonRestError(401, AUTH_DETAIL)

    result = mcp_server.call_tool(GatewayClient.wrap(RefusingClient()), "recall", {"query": "x"})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "MNEMOSEED_LOCAL_HOME" in text
    assert "credential rotate" in text
    assert "not running" not in text
