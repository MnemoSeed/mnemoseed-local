"""ConfigWriteService REST router (PRD-07 FR-7.11, W1.1) — A2 local trim.

The /api/v1/config contract the CLI codes against:

- GET    /api/v1/config             resolved config, secrets redacted to
                                     env-var NAMES only; the body is
                                     ``{"config": {...}, "restart_required": {...}, "generation": int}``.
- POST   /api/v1/config/set         ``{key_path, value}`` -> ``{ok, version_id,
                                     restart_required}``; a typed failure is a
                                     422 whose message names the offending key.
- GET    /api/v1/config/versions    the versioned history.
- POST   /api/v1/config/rollback    ``{version_id}`` -> ``{ok, version_id,
                                     restored}``, append-only.

Actor attribution comes from the ``X-MnemoSeed-Actor`` header
(cli|console|mcp, default console). Config writes are config operations: they
are rejected (403) when the daemon's baseurl is non-loopback. There is no
identity/account layer in the local MVP.
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request

from mnemoseed_local.config import Config
from mnemoseed_local.configwrite.service import ConfigWriteError, ConfigWriteService, GenerationMismatchError
from mnemoseed_local.daemon.actor import resolve_actor

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

router = APIRouter(
    prefix="/api/v1",
    tags=["config"],
)


def _is_loopback(host: str | None) -> bool:
    """Loopback hosts, including IPv4-mapped IPv6 (the local client's address)."""
    if host is None:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _service(request: Request) -> ConfigWriteService:
    return cast(ConfigWriteService, request.app.state.configwrite)


def _reject_remote_writes(request: Request) -> None:
    """Config writes are loopback-only: a remote baseurl is refused (403)."""
    config = cast(Config, request.app.state.config)
    host = urlparse(config.baseurl).hostname
    if not _is_loopback(host):
        raise HTTPException(
            status_code=403,
            detail="config writes are rejected when the daemon baseurl is non-loopback",
        )


def _expected_generation(request: Request) -> int | None:
    """Parse If-Match into an expected generation, or None when absent.

    Weak validators (W/) and quotes are stripped per RFC 7232; inner and
    surrounding whitespace is trimmed. An empty, whitespace-only or
    non-integer value is treated as a mismatch and raises 409 with the
    current generation in the detail so callers can resync.
    """
    raw = request.headers.get("if-match")
    if raw is None:
        raw = request.headers.get("If-Match")
    if raw is None:
        return None
    candidate = raw.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    if len(candidate) >= 2 and candidate[0] == '"' and candidate[-1] == '"':
        candidate = candidate[1:-1].strip()
    if not candidate:
        raise HTTPException(
            status_code=409,
            detail=f"generation mismatch: current generation is {_service(request).generation}",
        )
    try:
        return int(candidate)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"generation mismatch: current generation is {_service(request).generation}",
        ) from exc


@router.get("/config")
def get_config(request: Request) -> dict[str, Any]:
    """FR-7.11: the resolved config for the settings page (names only)."""
    resolve_actor(request)
    return _service(request).get()


@router.post("/config/set")
def set_config(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.11: validate + persist one key-path write (registry -> patch ->
    versioned record -> audit -> live-apply)."""
    actor = resolve_actor(request)
    _reject_remote_writes(request)
    key_path = body.get("key_path")
    if not isinstance(key_path, str) or not key_path:
        raise HTTPException(status_code=422, detail="body.key_path must be a non-empty string")
    expected = _expected_generation(request)
    try:
        result = _service(request).set(key_path, body.get("value"), actor=actor, expected_generation=expected)
    except GenerationMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": result["ok"],
        "version_id": result["version_id"],
        "restart_required": result["restart_required"],
    }


@router.get("/config/versions")
def config_versions(request: Request) -> dict[str, Any]:
    """FR-7.11: the append-only versioned history (registry keys only)."""
    resolve_actor(request)
    return {"versions": _service(request).versions()}


@router.post("/config/rollback")
def config_rollback(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.11: restore a recorded version (append-only, a new record)."""
    actor = resolve_actor(request)
    _reject_remote_writes(request)
    expected = _expected_generation(request)
    try:
        result = _service(request).rollback(body.get("version_id"), actor=actor, expected_generation=expected)
    except GenerationMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": result["ok"], "version_id": result["version_id"], "restored": result["restored"]}
