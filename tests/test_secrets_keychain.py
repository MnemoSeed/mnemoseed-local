"""Keychain backend (Task A): KeyringSecretStore + ChainSecretStore.

Behavior pinned here:

- The real OS keyring (Windows Credential Manager on this machine, Keychain
  on macOS, libsecret on Linux) roundtrips a QA-named entry and always cleans
  it up afterwards.
- ``ChainSecretStore`` auto-selects at build time: keyring head when an
  import + set/get/delete probe roundtrip passes, file-only otherwise.
- ``get`` tries backends in order (keyring head), ``set`` writes to the
  first functional backend and records which one stored the value,
  ``delete`` clears every backend, and ``masked_tail`` / ``backend_used``
  report the backend that actually stored the secret.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mnemoseed_local.secrets import ChainSecretStore, FileSecretStore, KeyringSecretStore
from mnemoseed_local.secrets.store import _probe_keyring_store


class _MemoryBackend:
    """A dict-backed stand-in SecretStore for chain-order tests."""

    def __init__(self, *, fail_set: bool = False, backend_name: str = "memory") -> None:
        self._values: dict[str, str] = {}
        self.fail_set = fail_set
        self.backend_name = backend_name

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        if self.fail_set:
            raise RuntimeError("backend down")
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._values.pop(name, None)

    def exists(self, name: str) -> bool:
        return name in self._values

    def masked_tail(self, name: str) -> str | None:
        value = self.get(name)
        return value[-4:] if value else None


def _chain(tmp_path: Path, head: object | None, monkeypatch: pytest.MonkeyPatch) -> ChainSecretStore:
    if head is None:
        monkeypatch.setattr("mnemoseed_local.secrets.store._probe_keyring_store", lambda: None)
    else:
        monkeypatch.setattr("mnemoseed_local.secrets.store._probe_keyring_store", lambda: head)
    return ChainSecretStore(tmp_path)


# ---------------------------------------------------------------- real keyring


def test_keyring_roundtrip_qa_entry_on_real_keyring() -> None:
    """The real OS keychain (Windows Credential Manager on this machine) stores
    and reads a QA-named entry, and the entry is always deleted afterwards."""
    if _probe_keyring_store() is None:
        pytest.skip("no usable OS keyring on this host")
    store = KeyringSecretStore()
    name = f"mnemoseed/qa/keyring-roundtrip-{os.getpid()}"
    try:
        store.set(name, "qa-secret-1234")
        assert store.get(name) == "qa-secret-1234"
        assert store.exists(name) is True
        assert store.masked_tail(name) == "1234"
        assert store.backend_name == "keychain"
    finally:
        store.delete(name)
    assert store.get(name) is None
    assert store.exists(name) is False


def test_chain_selects_real_keyring_head_on_this_machine(tmp_path) -> None:
    """On a host with a usable OS keyring, auto-detection puts the keychain head
    first and a set/get roundtrip lands in the keyring, not the file."""
    if _probe_keyring_store() is None:
        pytest.skip("no usable OS keyring on this host")
    chain = ChainSecretStore(tmp_path)
    assert [backend.backend_name for backend in chain.backends] == ["keychain", "file"]
    name = f"mnemoseed/qa/chain-head-{os.getpid()}"
    try:
        chain.set(name, "qa-chain-5678")
        assert chain.get(name) == "qa-chain-5678"
        assert chain.backend_used(name) == "keychain"
        assert FileSecretStore(tmp_path).get(name) is None
    finally:
        chain.delete(name)


# ---------------------------------------------------------------- chain assembly


def test_chain_uses_keyring_head_when_probe_passes(tmp_path, monkeypatch) -> None:
    head = _MemoryBackend(backend_name="keychain")
    chain = _chain(tmp_path, head, monkeypatch)
    assert [backend.backend_name for backend in chain.backends] == ["keychain", "file"]


def test_chain_file_only_when_keyring_unavailable(tmp_path, monkeypatch) -> None:
    """Keyring unavailable (simulated import/probe failure): the chain is a
    plain file store and the roundtrip behaves exactly like FileSecretStore."""
    chain = _chain(tmp_path, None, monkeypatch)
    assert [backend.backend_name for backend in chain.backends] == ["file"]
    chain.set("mnemoseed/dream/deep_reflection", "sk-file-value")
    assert chain.get("mnemoseed/dream/deep_reflection") == "sk-file-value"
    assert chain.masked_tail("mnemoseed/dream/deep_reflection") == "alue"
    assert chain.backend_used("mnemoseed/dream/deep_reflection") == "file"
    assert FileSecretStore(tmp_path).get("mnemoseed/dream/deep_reflection") == "sk-file-value"


# ---------------------------------------------------------------- precedence


def test_chain_get_precedence_keyring_then_file(tmp_path, monkeypatch) -> None:
    head = _MemoryBackend(backend_name="keychain")
    chain = _chain(tmp_path, head, monkeypatch)
    name = "mnemoseed/dream/deep_reflection"
    # value in the file only: the file answers (the head misses)
    FileSecretStore(tmp_path).set(name, "file-only-1111")
    assert chain.get(name) == "file-only-1111"
    assert chain.backend_used(name) == "file"
    # value in both: the keychain head wins
    head.set(name, "keyring-2222")
    assert chain.get(name) == "keyring-2222"
    assert chain.backend_used(name) == "keychain"
    # value in neither: None
    chain.delete(name)
    head.delete(name)
    assert chain.get(name) is None


# ---------------------------------------------------------------- set / writer tracking


def test_chain_set_writes_to_the_first_functional_backend(tmp_path, monkeypatch) -> None:
    """A broken keyring head must not break the write: the chain falls through
    to the file backend and records that the file stored the value."""
    head = _MemoryBackend(fail_set=True, backend_name="keychain")
    chain = _chain(tmp_path, head, monkeypatch)
    name = "mnemoseed/dream/deep_reflection"
    chain.set(name, "sk-fallback-9999")
    assert FileSecretStore(tmp_path).get(name) == "sk-fallback-9999"
    assert chain.get(name) == "sk-fallback-9999"
    assert chain.backend_used(name) == "file"
    assert chain.masked_tail(name) == "9999"


def test_chain_set_records_the_keychain_writer(tmp_path, monkeypatch) -> None:
    head = _MemoryBackend(backend_name="keychain")
    chain = _chain(tmp_path, head, monkeypatch)
    name = "mnemoseed/dream/deep_reflection"
    chain.set(name, "sk-chain-1234")
    assert head.get(name) == "sk-chain-1234"
    assert chain.backend_used(name) == "keychain"
    assert chain.masked_tail(name) == "1234"
    assert FileSecretStore(tmp_path).get(name) is None


def test_chain_masked_tail_reports_the_writer_backend(tmp_path, monkeypatch) -> None:
    head = _MemoryBackend(backend_name="keychain")
    chain = _chain(tmp_path, head, monkeypatch)
    name = "mnemoseed/dream/deep_reflection"
    FileSecretStore(tmp_path).set(name, "stale-file-value-1111")
    chain.set(name, "new-keyring-value-2222")
    # masked_tail answers from the backend that actually stored the value,
    # never from a stale copy in a sibling backend.
    assert chain.masked_tail(name) == "2222"
    assert chain.get(name) == "new-keyring-value-2222"


def test_chain_masked_tail_falls_back_to_the_first_holder(tmp_path, monkeypatch) -> None:
    head = _MemoryBackend(backend_name="keychain")
    chain = _chain(tmp_path, head, monkeypatch)
    name = "mnemoseed/dream/deep_reflection"
    FileSecretStore(tmp_path).set(name, "file-only-3333")  # written outside the chain
    assert chain.backend_used(name) == "file"
    assert chain.masked_tail(name) == "3333"


# ---------------------------------------------------------------- delete


def test_chain_delete_clears_every_backend(tmp_path, monkeypatch) -> None:
    head = _MemoryBackend(backend_name="keychain")
    chain = _chain(tmp_path, head, monkeypatch)
    name = "mnemoseed/dream/deep_reflection"
    chain.set(name, "sk-value")
    FileSecretStore(tmp_path).set(name, "sk-other")  # present in the file too
    chain.delete(name)
    assert chain.get(name) is None
    assert head.get(name) is None
    assert FileSecretStore(tmp_path).get(name) is None


def test_chain_delete_missing_name_is_a_noop(tmp_path, monkeypatch) -> None:
    head = _MemoryBackend(backend_name="keychain")
    chain = _chain(tmp_path, head, monkeypatch)
    chain.delete("mnemoseed/dream/deep_reflection")  # must not raise
