"""The daemon credential: one high-entropy bearer value per config home.

The daemon HTTP surface refuses every caller that cannot present the
credential. This module owns the value itself — generation, the file it
persists in, and the single verification seam — so exactly one place decides
what authenticates a request.

Three call-time rules shape the whole module:

1. ``CONFIG_DIR`` is imported inside the functions, never at module import.
   ``create_app()`` runs at import time in the daemon, so an import-time read
   would resolve (and could write) the real user home from any process that
   merely imports the app.
2. Only the daemon writes. The daemon's lifespan issues create-if-absent, so a
   restart keeps the value every client already holds. A client function only
   ever reads, and says so with a typed
   :class:`DaemonCredentialError` when the file is missing or unusable.
3. The value never reaches a log line, an audit row, an error body, or a
   console payload — this module contains no logging call at all.

Reusing ``FileSecretStore`` keeps custody in one place: the write is atomic and
the file lands with the restrictive permissions (POSIX 0700/0600; on Windows
the user-profile ACL is the enforcement boundary) the secrets module already
documents.
"""

from __future__ import annotations

import hmac
import secrets as _secrets
from pathlib import Path
from typing import Protocol, runtime_checkable

from mnemoseed_local.secrets.store import (
    SECRET_FILE_SUFFIX,
    SECRETS_DIR_NAME,
    FileSecretStore,
    sanitize_name,
)

#: Secret-store name of the daemon credential (one file per name).
CREDENTIAL_SECRET_NAME = "daemon/credential"

#: File name the credential persists as under ``<config home>/secrets``. The
#: OpenCode plugin reads the same name, so it is derived from the store's own
#: naming rule instead of being written twice.
CREDENTIAL_FILE_NAME = f"{sanitize_name(CREDENTIAL_SECRET_NAME)}{SECRET_FILE_SUFFIX}"

#: CSPRNG bytes behind an issued credential, before URL-safe encoding.
CREDENTIAL_ENTROPY_BYTES = 32


class DaemonCredentialError(Exception):
    """The daemon credential is missing or unusable (a client-side failure)."""


@runtime_checkable
class TokenVerifier(Protocol):
    """The authorization seam: does the value presented with a request
    authenticate it?

    A future cloud issuer replaces this one implementation and nothing else —
    the clients keep obtaining a value and attaching it as a bearer header.
    """

    def verify(self, presented: str | None) -> bool: ...


def generate_credential() -> str:
    """A fresh high-entropy credential value."""
    return _secrets.token_urlsafe(CREDENTIAL_ENTROPY_BYTES)


def credential_path(home: Path) -> Path:
    """The credential file under an explicit config home."""
    return Path(home) / SECRETS_DIR_NAME / CREDENTIAL_FILE_NAME


def _missing_hint(path: Path) -> str:
    return f"no daemon credential at {path}: start the daemon once ('mnemoseed-local up') so it issues one"


def _corrupt_hint(path: Path) -> str:
    return f"the daemon credential at {path} is empty; rotate it with 'mnemoseed-local credential rotate'"


def _unreadable_hint(path: Path, error: OSError) -> str:
    return f"the daemon credential at {path} cannot be read ({error})"


def read_credential_at(home: Path) -> str:
    """Read the credential persisted under an explicit config home."""
    path = credential_path(home)
    try:
        raw = FileSecretStore(home).get(CREDENTIAL_SECRET_NAME)
    except OSError as error:
        raise DaemonCredentialError(_unreadable_hint(path, error)) from error
    if raw is None:
        raise DaemonCredentialError(_missing_hint(path))
    value = raw.strip()
    if not value:
        raise DaemonCredentialError(_corrupt_hint(path))
    return value


def _config_dir() -> Path:
    from mnemoseed_local.config import CONFIG_DIR  # call time: a relocated home is honored

    return CONFIG_DIR


def read_credential() -> str:
    """The live config home's credential. Read-only: a client never issues."""
    return read_credential_at(_config_dir())


def read_credential_or_none() -> str | None:
    """The credential when the home carries one, else None (nothing attached).

    Every client path attaches the credential through this function, so a home
    that has none still talks to the daemon and is honestly refused (401)
    instead of failing before the request leaves.
    """
    try:
        return read_credential()
    except DaemonCredentialError:
        return None


def issue_credential() -> str:
    """Daemon-side issuance: create the credential when the home has none.

    A restart never re-signs — the persisted value comes back unchanged, so a
    client that read it earlier keeps working across restarts.
    """
    home = _config_dir()
    try:
        return read_credential_at(home)
    except DaemonCredentialError:
        pass
    value = generate_credential()
    FileSecretStore(home).set(CREDENTIAL_SECRET_NAME, value)
    return value


def rotate_credential() -> str:
    """Replace the persisted credential with a fresh one (the CLI verb)."""
    home = _config_dir()
    value = generate_credential()
    FileSecretStore(home).set(CREDENTIAL_SECRET_NAME, value)
    return value


class LocalFileTokenVerifier:
    """The local issuer's verifier: the persisted file is the authority.

    The config home is resolved on every call, so a rotated credential takes
    effect on the next request and a relocated home is honored. A missing,
    unreadable, empty, or blank presented value never verifies.
    """

    def verify(self, presented: str | None) -> bool:
        if presented is None:
            return False
        candidate = presented.strip()
        if not candidate:
            return False
        try:
            stored = read_credential()
        except DaemonCredentialError:
            return False
        return hmac.compare_digest(candidate, stored)
