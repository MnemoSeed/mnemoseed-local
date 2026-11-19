"""Credential-gated secret-value write route for the console provider wizard.

The request body is parsed by hand rather than through a Pydantic model: a
validation error on a model carrying the value would echo the submitted bytes
in a 422 response. Every failure path returns a fixed detail, so a value never
reaches the response, audit, daemon.log or exception-text sinks.

The default SecretStore is resolved on first use. Bare ``create_app()`` callers
never construct it, so they never trigger the OS-keyring build probe.
"""

from __future__ import annotations

import json
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request

from mnemoseed_local.config import LLM_ROLES
from mnemoseed_local.secrets.store import SecretStore

router = APIRouter(prefix="/api/v1/secrets", tags=["secrets"])

_ALLOWED_PREFIX = "mnemoseed/dream/"
_INVALID_REQUEST = "invalid request"
_INVALID_NAME = "invalid secret name"
_WRITE_FAILED = "secret write failed"


class LazySecretStore:
    """Defer default-store construction until the first value operation.

    CONFIG_DIR is read at resolution time so a home relocated after
    ``create_app()`` is honored. Construction is the only place the OS keyring
    probe can run; callers that inject a store bypass it entirely.
    """

    backend_name = "lazy"

    def __init__(self) -> None:
        self._store: SecretStore | None = None

    def _resolve(self) -> SecretStore:
        if self._store is None:
            from mnemoseed_local import config as config_mod
            from mnemoseed_local.secrets import default_secret_store

            self._store = default_secret_store(config_mod.CONFIG_DIR)
        return self._store

    def get(self, name: str) -> str | None:
        return self._resolve().get(name)

    def set(self, name: str, value: str) -> None:
        self._resolve().set(name, value)

    def delete(self, name: str) -> None:
        self._resolve().delete(name)

    def exists(self, name: str) -> bool:
        return self._resolve().exists(name)

    def masked_tail(self, name: str) -> str | None:
        return self._resolve().masked_tail(name)

    def backend_used(self, name: str) -> str:
        return _backend_label(self._resolve(), name)


def _backend_label(store: SecretStore, name: str) -> str:
    used = getattr(store, "backend_used", None)
    if callable(used):
        return str(used(name))
    return str(getattr(store, "backend_name", "unknown"))


def _is_allowed_name(name: str) -> bool:
    """Only the role slot namespace is writable here.

    The daemon credential slot and its sanitize aliases are outside the
    namespace, as is the deferred shared ``mnemoseed/llm`` namespace.
    """
    if not name.startswith(_ALLOWED_PREFIX):
        return False
    return name[len(_ALLOWED_PREFIX) :] in LLM_ROLES


@router.post("/set")
async def set_secret(request: Request) -> dict[str, Any]:
    """Write one provider key value to the shared SecretStore."""
    try:
        payload = json.loads(await request.body())
    except ValueError, UnicodeDecodeError:
        raise HTTPException(status_code=422, detail=_INVALID_REQUEST) from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail=_INVALID_REQUEST)
    name = payload.get("name")
    value = payload.get("value")
    if not isinstance(name, str) or not isinstance(value, str) or not name or not value:
        raise HTTPException(status_code=422, detail=_INVALID_REQUEST)
    if not _is_allowed_name(name):
        raise HTTPException(status_code=422, detail=_INVALID_NAME)
    store = cast(SecretStore, request.app.state.secret_store)
    try:
        store.set(name, value)
    except Exception:  # noqa: BLE001 - the fixed detail hides backend error text
        raise HTTPException(status_code=500, detail=_WRITE_FAILED) from None
    return {"ok": True, "name": name, "backend": _backend_label(store, name)}
