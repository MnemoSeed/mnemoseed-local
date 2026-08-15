"""Internal daemon REST client for the CLI surface (A2 local trim, FR-7.12).

Every state-changing CLI verb talks to the daemon through this client instead
of touching ``config.toml`` or the storage layers directly: base URL comes from
``--baseurl`` / ``config.baseurl``; the profile is ``MNEMOSEED_LOCAL_PROFILE_ID``
or the ``default`` profile (no identity/accounts in the local MVP); and every
call forwards ``X-MnemoSeed-Actor: cli`` so the daemon attributes audit entries
to the CLI surface.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

#: Audit-attribution header the daemon trusts (actor cli|console|mcp).
ACTOR_HEADER = "X-MnemoSeed-Actor"
DEFAULT_ACTOR = "cli"

#: The local MVP's single profile at the application boundary.
DEFAULT_PROFILE_ID = "default"

REQUEST_TIMEOUT_SECONDS = 30.0

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class DaemonUnavailableError(Exception):
    """The daemon did not answer (connect refused, timeout, DNS, ...)."""


class DaemonRestError(Exception):
    """The daemon answered with a non-2xx status."""

    def __init__(self, status: int, detail: Any) -> None:
        self.status = status
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"HTTP {status}{suffix}")


@dataclass(frozen=True)
class DaemonClient:
    """Small httpx JSON client pinned to one daemon and one actor identity."""

    base_url: str
    profile_id: str = DEFAULT_PROFILE_ID
    actor: str = DEFAULT_ACTOR
    timeout: float = REQUEST_TIMEOUT_SECONDS

    def _headers(self) -> dict[str, str]:
        return {ACTOR_HEADER: self.actor}

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            detail: Any = None
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = body.get("detail")
            except ValueError:
                pass
            raise DaemonRestError(response.status_code, detail)
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{self.base_url}{path}",
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise DaemonUnavailableError(f"cannot reach {self.base_url}: {exc}") from exc
        return self._decode(response)

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}{path}",
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise DaemonUnavailableError(f"cannot reach {self.base_url}: {exc}") from exc
        return self._decode(response)


def is_loopback(base_url: str) -> bool:
    """True for any loopback baseurl representation, else False."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return True
    return host.startswith("127.")


def _arg_baseurl(args: Any) -> str | None:
    value = getattr(args, "baseurl", None)
    return str(value).rstrip("/") if value else None


def resolve_client(args: Any, *, require_session: bool = False) -> DaemonClient:
    """Build a client from the CLI args + config.

    Base URL order: ``--baseurl``, then the config baseurl. The profile is
    ``MNEMOSEED_LOCAL_PROFILE_ID`` or the ``default`` profile.
    """
    from mnemoseed_local.config import ConfigError, load_config

    try:
        config_base = load_config().baseurl
    except ConfigError:
        config_base = "http://localhost:7788"
    base_url = _arg_baseurl(args) or config_base
    base_url = base_url.rstrip("/")
    profile_id = os.environ.get("MNEMOSEED_LOCAL_PROFILE_ID") or DEFAULT_PROFILE_ID
    if require_session and not profile_id:
        raise DaemonUnavailableError("no profile selected: set MNEMOSEED_LOCAL_PROFILE_ID")
    return DaemonClient(base_url=base_url, profile_id=profile_id)


# ---------------------------------------------------------------- config --force offline escape


def _toml_value(value: Any) -> str:
    """Render a scalar as TOML (JSON scalar syntax is valid TOML)."""
    rendered = json.dumps(value)
    if rendered in ("true", "false", "null"):
        return "false" if rendered == "null" else rendered
    return rendered


def _is_header(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[[")


def set_config_toml_offline(path: Path, key_path: str, value: Any) -> Path:
    """Offline ``--force`` escape: surgical line patch of ``config.toml``.

    Only the named key line changes (or a fresh ``key = value`` is inserted
    under the matching ``[section]``); every other byte — comments, unrelated
    keys, ordering — is preserved. Composite values (dict/list) are refused:
    those always need the daemon REST.
    """
    if isinstance(value, (dict, list)):
        raise DaemonRestError(422, f"key {key_path!r} has a composite value; use the daemon REST")
    if not path.exists():
        raise DaemonRestError(404, f"no config.toml to patch at {path} (run `mnemoseed-local init`)")
    segments = key_path.split(".")
    section = ".".join(segments[:-1])
    leaf = segments[-1]
    rendered = _toml_value(value)

    lines = path.read_text(encoding="utf-8").split("\n")
    current_section = ""
    replaced = False
    out: list[str] = []
    section_insert_at: int | None = None
    for index, line in enumerate(lines):
        if _is_header(line):
            current_section = line.strip()[1:-1].strip()
            section_insert_at = index + 1 if current_section == section else section_insert_at
        if current_section == section and not replaced:
            stripped = line.strip()
            if stripped.startswith(f"{leaf} =") or stripped == leaf:
                indent = line[: len(line) - len(line.lstrip(" \t"))]
                out.append(f"{indent}{leaf} = {rendered}")
                replaced = True
                continue
        out.append(line)
    if not replaced:
        if section and section_insert_at is not None:
            out.insert(section_insert_at, f"{leaf} = {rendered}")
        elif section:
            if out and out[-1].strip():
                out.append("")
            out.append(f"[{section}]")
            out.append(f"{leaf} = {rendered}")
        else:
            # root-level key: replace an existing top line or append.
            out.append(f"{leaf} = {rendered}")
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    return path
