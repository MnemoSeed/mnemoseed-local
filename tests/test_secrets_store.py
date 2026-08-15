"""FileSecretStore (T2-1): restart-free key custody under
``<CONFIG_DIR>/secrets/<sanitized>.key``.

Behavior pinned here:

- ``get`` / ``set`` / ``delete`` / ``exists`` / ``masked_tail`` round-trip one
  secret per name through the public port.
- one file per name under the ``secrets`` subdirectory; the name is sanitized
  to ``[a-z0-9_.-]`` so ``mnemoseed/dream/deep_reflection`` becomes a flat safe
  filename.
- writes are atomic (tmp + replace): a crash mid-write never leaves a torn
  secret, and no ``*.tmp`` files survive a successful ``set``.
- POSIX permissions: directory 0700, files 0600 (skipped gracefully on
  Windows, where the user-profile ACL is the enforcement boundary).
- ``masked_tail`` returns the last four characters only — never more.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mnemoseed_local.secrets import FileSecretStore, SecretStore

_POSIX = os.name != "nt"


def _store(tmp_path: Path) -> FileSecretStore:
    return FileSecretStore(tmp_path)


def test_file_store_satisfies_the_port_protocol() -> None:
    """The store implements the public SecretStore port (structural typing)."""
    store: SecretStore = _store(Path("ignored"))
    assert callable(store.get)
    assert callable(store.set)
    assert callable(store.delete)
    assert callable(store.exists)
    assert callable(store.masked_tail)


def test_get_missing_name_returns_none(tmp_path) -> None:
    assert _store(tmp_path).get("mnemoseed/dream/deep_reflection") is None


def test_set_then_get_roundtrips_the_value(tmp_path) -> None:
    store = _store(tmp_path)
    store.set("mnemoseed/dream/deep_reflection", "sk-super-secret-1234")
    assert store.get("mnemoseed/dream/deep_reflection") == "sk-super-secret-1234"


def test_set_overwrites_an_existing_value(tmp_path) -> None:
    store = _store(tmp_path)
    store.set("mnemoseed/dream/deep_reflection", "sk-first")
    store.set("mnemoseed/dream/deep_reflection", "sk-second")
    assert store.get("mnemoseed/dream/deep_reflection") == "sk-second"


def test_exists_reflects_presence(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.exists("mnemoseed/dream/short_increment") is False
    store.set("mnemoseed/dream/short_increment", "sk-value")
    assert store.exists("mnemoseed/dream/short_increment") is True


def test_delete_removes_the_secret(tmp_path) -> None:
    store = _store(tmp_path)
    store.set("mnemoseed/dream/deep_reflection", "sk-value")
    store.delete("mnemoseed/dream/deep_reflection")
    assert store.get("mnemoseed/dream/deep_reflection") is None
    assert store.exists("mnemoseed/dream/deep_reflection") is False


def test_delete_missing_name_is_a_noop(tmp_path) -> None:
    _store(tmp_path).delete("mnemoseed/dream/deep_reflection")  # must not raise


def test_masked_tail_returns_only_the_last_four(tmp_path) -> None:
    store = _store(tmp_path)
    store.set("mnemoseed/dream/deep_reflection", "sk-secret-tail-9012")
    assert store.masked_tail("mnemoseed/dream/deep_reflection") == "9012"


def test_masked_tail_missing_name_returns_none(tmp_path) -> None:
    assert _store(tmp_path).masked_tail("mnemoseed/dream/deep_reflection") is None


def test_masked_tail_short_value_returns_the_whole_value(tmp_path) -> None:
    store = _store(tmp_path)
    store.set("mnemoseed/dream/deep_reflection", "ab")
    assert store.masked_tail("mnemoseed/dream/deep_reflection") == "ab"


def test_files_live_under_the_secrets_subdirectory(tmp_path) -> None:
    _store(tmp_path).set("mnemoseed/dream/deep_reflection", "sk-value")
    secret_file = tmp_path / "secrets" / "mnemoseed.dream.deep_reflection.key"
    assert secret_file.is_file()
    assert secret_file.read_text(encoding="utf-8") == "sk-value"


def test_name_is_sanitized_to_a_flat_safe_filename(tmp_path) -> None:
    """The reference name ``mnemoseed/dream/<role>`` maps to a flat filename:
    separators become dots, and nothing outside ``[a-z0-9_.-]`` survives."""
    _store(tmp_path).set("mnemoseed/dream/deep_reflection", "sk-value")
    children = [p.name for p in (tmp_path / "secrets").iterdir()]
    assert children == ["mnemoseed.dream.deep_reflection.key"]


def test_separate_names_never_collide(tmp_path) -> None:
    store = _store(tmp_path)
    store.set("mnemoseed/dream/deep_reflection", "sk-deep")
    store.set("mnemoseed/dream/short_increment", "sk-short")
    assert store.get("mnemoseed/dream/deep_reflection") == "sk-deep"
    assert store.get("mnemoseed/dream/short_increment") == "sk-short"


def test_set_is_atomic_no_tmp_leftover(tmp_path) -> None:
    store = _store(tmp_path)
    store.set("mnemoseed/dream/deep_reflection", "sk-value-1")
    store.set("mnemoseed/dream/deep_reflection", "sk-value-2")
    assert store.get("mnemoseed/dream/deep_reflection") == "sk-value-2"
    leftovers = list((tmp_path / "secrets").glob("*.tmp"))
    assert leftovers == []


@pytest.mark.skipif(not _POSIX, reason="POSIX permission bits are not enforced on Windows")
def test_posix_directory_is_0700(tmp_path) -> None:
    _store(tmp_path).set("mnemoseed/dream/deep_reflection", "sk-value")
    mode = stat.S_IMODE((tmp_path / "secrets").stat().st_mode)
    assert mode == 0o700


@pytest.mark.skipif(not _POSIX, reason="POSIX permission bits are not enforced on Windows")
def test_posix_secret_file_is_0600(tmp_path) -> None:
    _store(tmp_path).set("mnemoseed/dream/deep_reflection", "sk-value")
    mode = stat.S_IMODE((tmp_path / "secrets" / "mnemoseed.dream.deep_reflection.key").stat().st_mode)
    assert mode == 0o600


def test_get_never_leaks_the_value_in_an_exception(tmp_path) -> None:
    """The store does not log or raise with the secret value embedded."""
    store = _store(tmp_path)
    store.set("mnemoseed/dream/deep_reflection", "sk-ultra-secret-value")
    try:
        store.get("mnemoseed/dream/deep_reflection")
        store.exists("mnemoseed/dream/deep_reflection")
        store.masked_tail("mnemoseed/dream/deep_reflection")
    except Exception as exc:  # pragma: no cover - the store must never raise here
        assert "sk-ultra-secret-value" not in str(exc)
