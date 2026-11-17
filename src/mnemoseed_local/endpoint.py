"""Endpoint record and unauthenticated instance handshake.

The daemon publishes a per-start record under the config home. A client reads
that record, cheaply pre-filters on PID liveness, then confirms the instance id
through the unauthenticated /healthz route before presenting the credential.
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from mnemoseed_local import config as config_mod

ENDPOINT_FILENAME = "endpoint.json"
HEALTHZ_TIMEOUT_S = 2.0
OVERRIDE_WARNING = "endpoint override set; daemon instance verification skipped"
MISSING_RECORD_WARNING = "no daemon endpoint record; instance verification skipped"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class StaleEndpointError(Exception):
    """The endpoint record is absent, dead, or serves a different instance."""


@dataclass(frozen=True)
class EndpointRecord:
    pid: int
    instance_id: str
    base_url: str
    started_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "instance_id": self.instance_id,
            "base_url": self.base_url,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> EndpointRecord | None:
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        instance_id = data.get("instance_id")
        base_url = data.get("base_url")
        started_at = data.get("started_at")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return None
        if not isinstance(instance_id, str) or not instance_id:
            return None
        if not isinstance(base_url, str) or not base_url:
            return None
        if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
            return None
        return cls(pid, instance_id, base_url, float(started_at))


def endpoint_path() -> Path:
    """The record path, resolved from the config home at call time."""
    return config_mod.CONFIG_DIR / ENDPOINT_FILENAME


def write_endpoint(record: EndpointRecord, *, path: Path | None = None) -> Path:
    target = path if path is not None else endpoint_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record.to_dict()), encoding="utf-8")
    return target


def clear_endpoint(*, path: Path | None = None) -> None:
    target = path if path is not None else endpoint_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass


def read_endpoint(*, path: Path | None = None) -> EndpointRecord | None:
    target = path if path is not None else endpoint_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError, ValueError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return EndpointRecord.from_dict(data)


def _pid_alive(pid: int) -> bool:
    """Cheap existence check; the instance handshake is the authority."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError, ValueError:
        return False
    return True


def resolve(
    requested_base_url: str,
    *,
    override: bool = False,
    allow_missing: bool = False,
    path: Path | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    transport: httpx.BaseTransport | None = None,
    warn: Callable[[str], None] | None = None,
) -> str:
    """Return a verified base URL, or raise StaleEndpointError.

    The verification GET carries no headers, so a stale endpoint never receives
    the daemon credential. An explicit override skips the check with a warning;
    ``allow_missing`` does the same when no daemon is running.
    """
    requested = requested_base_url.rstrip("/")
    if override:
        (warn if warn is not None else _warn)(OVERRIDE_WARNING)
        return requested

    record = read_endpoint(path=path)
    if record is None:
        if allow_missing:
            (warn if warn is not None else _warn)(MISSING_RECORD_WARNING)
            return requested
        raise StaleEndpointError(
            f"no daemon endpoint record at {path if path is not None else endpoint_path()}"
        )
    if not (pid_alive if pid_alive is not None else _pid_alive)(record.pid):
        raise StaleEndpointError(f"endpoint record pid {record.pid} is not running")

    health_url = f"{record.base_url.rstrip('/')}/healthz"
    try:
        if transport is not None:
            client = httpx.Client(timeout=HEALTHZ_TIMEOUT_S, transport=transport)
        else:
            client = httpx.Client(timeout=HEALTHZ_TIMEOUT_S)
        try:
            response = client.get(health_url)
        finally:
            client.close()
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise StaleEndpointError(f"endpoint verification failed at {health_url}: {exc}") from exc

    observed = body.get("instance_id") if isinstance(body, dict) else None
    if not isinstance(observed, str) or observed != record.instance_id:
        raise StaleEndpointError(
            f"endpoint instance mismatch at {health_url}: record {record.instance_id!r}, served {observed!r}"
        )
    return record.base_url.rstrip("/")


def _warn(message: str) -> None:
    warnings.warn(message, stacklevel=2)
