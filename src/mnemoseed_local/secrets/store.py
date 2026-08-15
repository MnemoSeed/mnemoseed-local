"""SecretStore port + file / keychain backends (T2-1, Task A).

The port is deliberately tiny: ``get`` / ``set`` / ``delete`` / ``exists`` /
``masked_tail``. Consumers (the role router, the admin key endpoints) never
see a value beyond the last four characters, so a response or audit payload
cannot leak a whole secret through the public surface.

The file backend stores one secret per name as ``<CONFIG_DIR>/secrets/
<sanitized>.key`` where the name is sanitized to ``[a-z0-9_.-]`` (the
``/`` in ``mnemoseed/dream/<role>`` becomes ``.``). Writes are atomic
(tmp + replace) so a crash never leaves a torn secret. Permissions follow the
user-profile boundary: POSIX gets an explicit 0700 directory + 0600 files;
on Windows the user-profile ACL is the enforcement boundary and no chmod is
attempted. Values are never logged anywhere in this module.

The keychain backend stores through the OS keyring package (Windows
Credential Manager, macOS Keychain, Linux libsecret when present). The chain
backend selects at build time: a probe set/get/delete roundtrip decides
whether the keychain head is usable; ``get`` tries backends in order, ``set``
writes to the first functional backend and records which one stored the
value, and ``delete`` clears every backend.
"""

from __future__ import annotations

import logging
import os
import re
import secrets as _secrets
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

#: The subdirectory under the config home that holds one file per secret.
SECRETS_DIR_NAME = "secrets"

#: Per-secret file suffix.
SECRET_FILE_SUFFIX = ".key"

#: The one-time write suffix (atomic replace target).
_TMP_SUFFIX = ".tmp"

#: Keyring service name: every secret is one credential under this service.
KEYRING_SERVICE_NAME = "mnemoseed"

#: Chain backend diagnostic labels (backend_used reporting, logging).
BACKEND_LABEL_KEYCHAIN = "keychain"
BACKEND_LABEL_FILE = "file"

logger = logging.getLogger(__name__)


class SecretsError(Exception):
    """Typed failure on the secret store surface."""


@runtime_checkable
class SecretStore(Protocol):
    """The key-custody port (T2-1): name-addressed secret values."""

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...
    def exists(self, name: str) -> bool: ...
    def masked_tail(self, name: str) -> str | None: ...


def sanitize_name(name: str) -> str:
    """Map a secret name to a safe filename stem: ``[a-z0-9_.-]`` only, with
    any other character (e.g. the ``/`` in ``mnemoseed/dream/<role>``) mapped
    to ``.``. The result never contains a path separator."""
    return re.sub(r"[^a-z0-9_.-]", ".", name.lower())


class FileSecretStore:
    """One file per name under ``<directory>/secrets/<sanitized>.key``."""

    #: Chain diagnostic label (Task A): this backend reports as ``file``.
    backend_name = BACKEND_LABEL_FILE

    def __init__(self, directory: Path | str) -> None:
        self._root = Path(directory).expanduser()
        self._secrets_dir = self._root / SECRETS_DIR_NAME

    def _path(self, name: str) -> Path:
        return self._secrets_dir / f"{sanitize_name(name)}{SECRET_FILE_SUFFIX}"

    def get(self, name: str) -> str | None:
        try:
            return self._path(name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        self._secrets_dir.mkdir(parents=True, exist_ok=True)
        self._harden_dir()
        target = self._path(name)
        tmp = target.with_name(f"{target.name}{_TMP_SUFFIX}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, value.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, target)
        self._harden_file(target)

    def delete(self, name: str) -> None:
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            pass

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def masked_tail(self, name: str) -> str | None:
        value = self.get(name)
        if value is None:
            return None
        return value[-4:] or None

    # ------------------------------------------------------------ permissions

    def _harden_dir(self) -> None:
        if os.name == "nt":
            return  # user-profile ACL is the enforcement boundary on Windows
        try:
            os.chmod(self._secrets_dir, 0o700)
        except OSError:
            raise SecretsError(f"cannot secure the secrets directory {self._secrets_dir}") from None

    def _harden_file(self, path: Path) -> None:
        if os.name == "nt":
            return
        try:
            os.chmod(path, 0o600)
        except OSError:
            raise SecretsError(f"cannot secure the secret file {path}") from None


class KeyringSecretStore:
    """One credential per name in the OS keyring (Windows Credential Manager,
    macOS Keychain, Linux libsecret when present).

    The store is constructed only after ``_probe_keyring_store`` confirmed a
    usable backend, so construction raises ImportError when the keyring
    package is absent. Runtime keyring failures (a backend going away, an OS
    unlock prompt refusing) degrade to ``None`` / no-op so the surrounding
    chain can fall through to the file backend instead of failing the caller.
    """

    #: Chain diagnostic label (Task A): this backend reports as ``keychain``.
    backend_name = BACKEND_LABEL_KEYCHAIN

    def __init__(self) -> None:
        import keyring  # noqa: PLC0415 - imported here so the probe gates it

        self._keyring = keyring

    @property
    def _service(self) -> str:
        return KEYRING_SERVICE_NAME

    def get(self, name: str) -> str | None:
        try:
            return self._keyring.get_password(self._service, name)
        except Exception:  # noqa: BLE001 - a broken keyring degrades to None
            return None

    def set(self, name: str, value: str) -> None:
        try:
            self._keyring.set_password(self._service, name, value)
        except Exception as exc:  # noqa: BLE001 - surfaced to the chain caller
            raise SecretsError(f"cannot write the secret to the OS keyring for {name!r}") from exc

    def delete(self, name: str) -> None:
        try:
            self._keyring.delete_password(self._service, name)
        except Exception:  # noqa: BLE001 - a missing entry / broken keyring is a no-op
            pass

    def exists(self, name: str) -> bool:
        return self.get(name) is not None

    def masked_tail(self, name: str) -> str | None:
        value = self.get(name)
        if value is None:
            return None
        return value[-4:] or None


def _probe_keyring_store() -> KeyringSecretStore | None:
    """Build-time probe: return a usable keyring store, or None.

    A store is usable only when the keyring package imports AND a full
    set/get/delete roundtrip succeeds against the host's default backend.
    The probe credential is always cleaned up before returning.
    """
    try:
        import keyring  # noqa: F401, PLC0415 - the import itself is the first gate
    except ImportError:
        return None
    store = KeyringSecretStore()
    probe_name = f"mnemoseed/probe/{uuid.uuid4().hex}"
    probe_value = _secrets.token_hex(16)
    try:
        store.set(probe_name, probe_value)
        if store.get(probe_name) != probe_value:
            return None
    except Exception:  # noqa: BLE001 - any keyring failure means file fallback
        return None
    finally:
        store.delete(probe_name)
    return store


class ChainSecretStore:
    """Auto-selecting store: keychain head when available, file otherwise.

    Build-time detection (Task A): a probe roundtrip decides whether the
    keyring backend is usable; ``primary`` lets callers pin an explicit head
    (tests, diagnostics). ``get`` tries backends in order; ``set`` writes to
    the FIRST functional backend and records which one stored the value
    (``backend_used`` / ``masked_tail`` report that backend); ``delete``
    clears every backend. File semantics are unchanged from
    :class:`FileSecretStore`.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        primary: SecretStore | None = None,
        _force_file: bool = False,
    ) -> None:
        file_store = FileSecretStore(directory)
        if _force_file:
            self._chain: tuple[SecretStore, ...] = (file_store,)
        elif primary is not None:
            self._chain = (primary, file_store)
        else:
            keyring_store = _probe_keyring_store()
            self._chain = (keyring_store, file_store) if keyring_store is not None else (file_store,)
        #: name -> backend label that last stored the value (set-time record).
        self._writer: dict[str, str] = {}

    @property
    def backends(self) -> tuple[SecretStore, ...]:
        """The ordered backend chain (head first); diagnostics + tests."""
        return self._chain

    @staticmethod
    def _label(store: SecretStore) -> str:
        return getattr(store, "backend_name", BACKEND_LABEL_FILE)

    def _backend_by_label(self, label: str) -> SecretStore | None:
        for backend in self._chain:
            if self._label(backend) == label:
                return backend
        return None

    def get(self, name: str) -> str | None:
        for backend in self._chain:
            try:
                value = backend.get(name)
            except Exception:  # noqa: BLE001 - a broken backend falls through
                continue
            if value is not None:
                return value
        return None

    def exists(self, name: str) -> bool:
        for backend in self._chain:
            try:
                if backend.exists(name):
                    return True
            except Exception:  # noqa: BLE001 - a broken backend falls through
                continue
        return False

    def set(self, name: str, value: str) -> None:
        first_error: Exception | None = None
        for backend in self._chain:
            try:
                backend.set(name, value)
            except Exception as exc:  # noqa: BLE001 - try the next backend
                if first_error is None:
                    first_error = exc
                continue
            label = self._label(backend)
            self._writer[name] = label
            logger.debug("secret %s stored in backend %s", name, label)
            return
        if first_error is not None:
            raise SecretsError(f"no secret backend accepted {name!r}") from first_error
        raise SecretsError(f"no secret backend available for {name!r}")

    def delete(self, name: str) -> None:
        for backend in self._chain:
            try:
                backend.delete(name)
            except Exception:  # noqa: BLE001 - best-effort removal across backends
                continue
        self._writer.pop(name, None)

    def masked_tail(self, name: str) -> str | None:
        label = self._writer.get(name)
        if label is not None:
            backend = self._backend_by_label(label)
            if backend is not None:
                return backend.masked_tail(name)
        for backend in self._chain:
            try:
                if backend.exists(name):
                    return backend.masked_tail(name)
            except Exception:  # noqa: BLE001 - a broken backend falls through
                continue
        return None

    def backend_used(self, name: str) -> str:
        """Which backend holds (or would receive) the named secret, as a
        diagnostic label: ``keychain`` | ``file``."""
        label = self._writer.get(name)
        if label is not None:
            return label
        for backend in self._chain:
            try:
                if backend.exists(name):
                    return self._label(backend)
            except Exception:  # noqa: BLE001 - a broken backend falls through
                continue
        return self._label(self._chain[0])
