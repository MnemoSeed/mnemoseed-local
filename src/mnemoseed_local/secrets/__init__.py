"""SecretStore port + file/keychain backends (T2-1, Task A): restart-free
key custody.

Dream LLM keys never live in the config file — config stores only a
REFERENCE (``secrets:mnemoseed/dream/<role>``) and the key value lives either
in the OS keyring (Windows Credential Manager, macOS Keychain, Linux
libsecret when present) or in a per-user restricted file under
``<CONFIG_DIR>/secrets/``. The port is the whole seam: routing resolves
through it, the admin/key endpoints write through it, and a future backend is
a documented port implementation without touching any consumer.
"""

from __future__ import annotations

import os
from pathlib import Path

from mnemoseed_local.secrets.refs import (
    ENV_NAME_RE,
    is_secrets_ref,
    redact_key_ref_for_display,
    secret_name_from_ref,
)
from mnemoseed_local.secrets.store import (
    ChainSecretStore,
    FileSecretStore,
    KeyringSecretStore,
    SecretsError,
    SecretStore,
)

__all__ = [
    "ChainSecretStore",
    "ENV_NAME_RE",
    "FileSecretStore",
    "KeyringSecretStore",
    "SecretStore",
    "SecretsError",
    "is_secrets_ref",
    "redact_key_ref_for_display",
    "secret_name_from_ref",
    "default_secret_store",
]

#: Escape hatch (diagnostics/tests): "file" forces the restricted-file backend,
#: "keychain" forces the OS-keychain head attempt. Unset => auto-detect.
SECRET_BACKEND_ENV = "MNEMOSEED_SECRET_BACKEND"


def default_secret_store(home: Path | str) -> ChainSecretStore:
    """Build the auto-selecting store for one config home (Task A).

    The chain picks the keyring backend head when a probe roundtrip passes
    and falls back to the restricted-file backend otherwise — no config flag,
    behavior is automatic. ``MNEMOSEED_SECRET_BACKEND`` can force a backend.
    """
    forced = os.environ.get(SECRET_BACKEND_ENV, "").strip().lower()
    if forced == "file":
        return ChainSecretStore(home, primary=None, _force_file=True)
    if forced == "keychain":
        return ChainSecretStore(home, primary=KeyringSecretStore())
    return ChainSecretStore(home)
